"""Export and import selected app databases in portable, validated ZIP files."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import secure_db as sqlite3
import tempfile
from typing import Callable
import zipfile

from app_paths import get_app_base_path, get_app_data_path, resolve_background_file


TRANSFER_FORMAT = "the-light-ministries-databases"
TRANSFER_VERSION = 1
MAX_DATABASE_BYTES = 20 * 1024 * 1024 * 1024
PROGRESS = Callable[[str, str, int, int], None]
DATABASES: dict[str, tuple[str, str, str]] = {
    "library": ("library.db", "Library songs", "songs"),
    "members": ("members.db", "Members", "members"),
    "services": ("service.db", "Services and saved displays", "service_entries"),
    "welcome_notes": ("welcome_note.db", "Welcome notes", "welcome_notes"),
}


class DataTransferError(ValueError):
    """Raised when a transfer archive or database is invalid or unsafe."""


BACKUP_RETENTION_COUNT = 7


def checkpoint_databases(data_directory: Path | None = None) -> None:
    """What: Truncate each database's write-ahead log into its main file.

    Why included: Unchecked WAL sidecars grow unbounded and complicate copies/backups.
    Where used: Application shutdown and the daily backup worker.
    Returns: None; locked or missing databases are skipped silently.
    """
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    with sqlite3.data_directory(root):
        _checkpoint_databases(root)


def _checkpoint_databases(root: Path) -> None:
    """What: Checkpoint each database file in one data folder.

    Why included: Separates the per-folder work from the key-folder context set by checkpoint_databases().
    Where used: checkpoint_databases().
    Returns: None; locked or unreadable databases are skipped.
    """
    for database_path in sorted(root.glob("*.db")):
        try:
            connection = sqlite3.connect(str(database_path), timeout=1.0)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
        except sqlite3.Error:
            continue


def run_daily_backup(data_directory: Path | None = None) -> tuple[str, ...]:
    """What: Integrity-check every database and keep a daily rolling backup ZIP.

    Why included: Databases were only copied during updates; silent corruption or an
    accidental deletion between updates could lose a church's songs and services.
    Where used: Background health task started shortly after application startup.
    Returns: Human-readable issues (failed checks), empty when everything is healthy.
    """
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    try:
        sqlite3.ensure_encrypted(root / DATABASES["members"][0])
    except Exception as error:
        return (f"Member details could not be encrypted: {error}",)
    with sqlite3.data_directory(root):
        return _run_daily_backup(root)


def _run_daily_backup(root: Path) -> tuple[str, ...]:
    """What: Integrity-check every database in one folder and write the daily backup ZIP.

    Why included: Runs inside the member-key context so encrypted member data is backed up encrypted.
    Where used: run_daily_backup().
    Returns: Human-readable problems found; empty when everything is healthy.
    """
    issues: list[str] = []
    database_paths = sorted(root.glob("*.db"))
    for database_path in database_paths:
        try:
            connection = sqlite3.connect(str(database_path), timeout=5.0)
            try:
                row = connection.execute("PRAGMA quick_check").fetchone()
            finally:
                connection.close()
            if not row or str(row[0]).lower() != "ok":
                issues.append(f"Database integrity check failed: {database_path.name} ({row[0] if row else 'no result'})")
        except sqlite3.Error as error:
            issues.append(f"Database could not be checked: {database_path.name} ({error})")
    backups_directory = root / ".backups"
    existing_backups = sorted(backups_directory.glob("backup-*.zip"))
    if existing_backups:
        newest_age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - existing_backups[-1].stat().st_mtime)
        if newest_age_seconds < 24 * 3600:
            return tuple(issues)
    backups_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    staging_directory = Path(tempfile.mkdtemp(prefix="pewvista-backup-"))
    try:
        for database_path in database_paths:
            try:
                source = sqlite3.connect(str(database_path), timeout=5.0)
                try:
                    target = sqlite3.connect(str(staging_directory / database_path.name))
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                finally:
                    source.close()
            except sqlite3.Error as error:
                issues.append(f"Database could not be backed up: {database_path.name} ({error})")
        for settings_path in sorted(root.glob("*.json")):
            try:
                shutil.copy2(settings_path, staging_directory / settings_path.name)
            except OSError:
                continue
        staged_files = sorted(staging_directory.iterdir())
        if staged_files:
            archive_path = backups_directory / f"backup-{timestamp}.zip"
            temporary_archive = backups_directory / f".backup-{timestamp}.partial"
            with zipfile.ZipFile(temporary_archive, "w", zipfile.ZIP_DEFLATED) as archive:
                for staged_file in staged_files:
                    archive.write(staged_file, staged_file.name)
            temporary_archive.replace(archive_path)
            for stale_backup in sorted(backups_directory.glob("backup-*.zip"))[:-BACKUP_RETENTION_COUNT]:
                try:
                    stale_backup.unlink()
                except OSError:
                    continue
    finally:
        shutil.rmtree(staging_directory, ignore_errors=True)
    return tuple(issues)


def available_databases(data_directory: Path | None = None) -> tuple[str, ...]:
    """What: List supported app databases present in a data directory.

    Why included: Export choices should only offer files that can be snapshotted.
    Where used: Data menu database-export selection.
    Returns: Database keys in application-defined display order.
    """
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    return tuple(key for key, (filename, _, _) in DATABASES.items() if (root / filename).is_file())


def _normalize_selection(selected: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """What: Validate and deduplicate selected database keys.

    Why included: Transfers must reject empty or unsupported selections before I/O.
    Where used: Database ZIP export and import entry points.
    Returns: Unique valid keys in original selection order.
    """
    keys = tuple(dict.fromkeys(str(key) for key in selected))
    if not keys:
        raise DataTransferError("Select at least one database.")
    unknown = [key for key in keys if key not in DATABASES]
    if unknown:
        raise DataTransferError("The selection contains an unsupported database.")
    return keys


def _readonly_uri(path: Path) -> str:
    """What: Build a read-only SQLite URI for a local database file.

    Why included: Validation and snapshot readers must not mutate their source DB.
    Where used: Database validation, snapshots, and merge sources.
    Returns: URI-mode SQLite connection string.
    """
    return path.resolve().as_uri() + "?mode=ro"


def _validate_database(path: Path, key: str) -> int:
    """What: Verify file size, SQLite integrity, and the expected app table.

    Why included: Corrupt or unrelated DBs must be rejected before transfer.
    Where used: ZIP export, import staging, and backup validation.
    Returns: SQLite user_version for the validated database.
    """
    filename, label, expected_table = DATABASES[key]
    del filename, label
    if not path.is_file() or path.stat().st_size <= 0:
        raise DataTransferError(f"The {DATABASES[key][1]} database is missing or empty.")
    if path.stat().st_size > MAX_DATABASE_BYTES:
        raise DataTransferError(f"The {DATABASES[key][1]} database exceeds the supported transfer size.")
    try:
        connection = sqlite3.connect(_readonly_uri(path), uri=True, timeout=30.0)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise DataTransferError(f"The {DATABASES[key][1]} database failed its integrity check.")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if expected_table not in tables:
                raise DataTransferError(f"The selected file is not a valid {DATABASES[key][1]} database.")
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise DataTransferError(f"Could not read the {DATABASES[key][1]} database: {error}") from error


def _quoted(identifier: str) -> str:
    """What: Quote a SQLite identifier after escaping embedded quote characters.

    Why included: Dynamic schema names must remain valid and safe in generated SQL.
    Where used: Schema-aware merge and database path-rewrite statements.
    Returns: A double-quoted SQLite identifier.
    """
    return '"' + identifier.replace('"', '""') + '"'


def _service_date_key(value: object) -> str:
    """What: Normalize supported service-date formats to an ISO date key.

    Why included: Equivalent legacy date strings should match across installations.
    Where used: Service-day selection, export, and import.
    Returns: ISO YYYY-MM-DD text or a case-folded original for unknown formats.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        for format_string in ("%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(text, format_string).date().isoformat()
            except ValueError:
                pass
    return text.casefold()


def _record_key(table: str, row: dict[str, object]) -> tuple[object, ...] | None:
    """What: Build an exact data-field identity while omitting local row IDs.

    Why included: Merge skips only identical records, not same-title or same-date variants.
    Where used: Database merge and service-day import deduplication.
    Returns: Canonical field/value tuple, or None when no comparable fields exist.
    """
    return tuple((name, _canonical_value(name, value)) for name, value in sorted(row.items()) if name != "id") or None


def _canonical_value(column: str, value: object) -> object:
    """What: Normalize JSON-backed fields for exact duplicate comparison.

    Why included: JSON whitespace/key order should not create duplicate records.
    Where used: _record_key() during merge imports.
    Returns: Canonical JSON text, original invalid text, or unchanged scalar value.
    """
    if value is None:
        return None
    if column in {"verses_json", "verse_types_json", "font_settings_json", "welcome_settings", "display_tab_items",
                  "presentation_settings", "display_settings", "settings_json"}:
        try:
            return json.dumps(json.loads(str(value)), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)
    return value


def _known_tables(key: str) -> tuple[str, ...]:
    """What: Map a database key to its supported app-owned tables.

    Why included: Transfer should not merge SQLite internals or unrelated tables.
    Where used: Additive database import.
    Returns: Ordered tuple of application table names.
    """
    return {
        "library": ("songs",),
        "members": ("members", "member_extra_fields"),
        "services": ("service_entries", "service_info"),
        "welcome_notes": ("welcome_notes",),
    }[key]


def _allow_multiple_service_info_rows(connection: sqlite3.Connection) -> None:
    """What: Migrate a legacy singleton service_info table to multiple rows.

    Why included: Distinct saved date selections from other PCs must coexist.
    Where used: Service database and selected service-day imports.
    Returns: None; alters the supplied transaction only when the legacy check exists.
    """
    definition = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='service_info'"
    ).fetchone()
    if not definition or "check(id=1)" not in "".join(str(definition[0]).casefold().split()):
        return
    connection.execute(
        "CREATE TABLE service_info_multi (id INTEGER PRIMARY KEY AUTOINCREMENT, service_date TEXT NOT NULL DEFAULT '', service_time TEXT NOT NULL DEFAULT '', preacher_name TEXT NOT NULL DEFAULT '')"
    )
    connection.execute(
        "INSERT INTO service_info_multi (id, service_date, service_time, preacher_name) SELECT id, service_date, service_time, preacher_name FROM service_info"
    )
    connection.execute("DROP TABLE service_info")
    connection.execute("ALTER TABLE service_info_multi RENAME TO service_info")


def _merge_database(
    source_path: Path,
    target_path: Path,
    key: str,
    progress: PROGRESS | None,
    id_map_out: dict[int, int] | None = None,
) -> tuple[int, int]:
    """What: Add missing rows while retaining existing destination records.

    Why included: Cross-PC import must preserve local data and remap database-local IDs.
    Where used: Full database and service-day imports.
    Returns: Inserted/skipped row counts; optional source-to-local song IDs are returned by mapping.
    """
    source = sqlite3.connect(_readonly_uri(source_path), uri=True, timeout=30.0)
    target = sqlite3.connect(target_path, timeout=30.0)
    source.row_factory = sqlite3.Row
    target.row_factory = sqlite3.Row
    imported = 0
    skipped = 0
    member_id_map: dict[int, int] = {}
    try:
        target.execute("PRAGMA foreign_keys = ON")
        target.execute("BEGIN IMMEDIATE")
        if key == "services":
            _allow_multiple_service_info_rows(target)
        for table in _known_tables(key):
            source_definition = source.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if source_definition is None:
                continue
            target_definition = target.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if target_definition is None:
                target.execute(source_definition["sql"])
                for index in source.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)
                ):
                    target.execute(index["sql"])
            source_columns = [row["name"] for row in source.execute(f"PRAGMA table_info({_quoted(table)})")]
            target_columns = [row["name"] for row in target.execute(f"PRAGMA table_info({_quoted(table)})")]
            shared_columns = [name for name in source_columns if name in target_columns]
            source_rows = source.execute(f"SELECT * FROM {_quoted(table)}").fetchall()
            target_rows = target.execute(f"SELECT * FROM {_quoted(table)}").fetchall()
            existing_keys = {_record_key(table, dict(row)) for row in target_rows}
            existing_keys.discard(None)
            existing_ids_by_key: dict[tuple[object, ...], int] = {}
            if table in {"songs", "members"}:
                for target_row in target_rows:
                    identity = _record_key(table, dict(target_row))
                    if identity is not None and target_row["id"] is not None:
                        existing_ids_by_key.setdefault(identity, int(target_row["id"]))
            if table == "member_extra_fields":
                existing_fields = {_record_key(table, dict(row)) for row in target_rows}
                existing_fields.discard(None)
                if "member_id" not in source_columns or "field_name" not in source_columns or not member_id_map:
                    skipped += len(source_rows)
                    continue
                for row in source_rows:
                    source_member_id = int(row["member_id"])
                    local_member_id = member_id_map.get(source_member_id)
                    if local_member_id is None:
                        skipped += 1
                        continue
                    mapped_record = dict(row)
                    mapped_record["member_id"] = local_member_id
                    field_key = _record_key(table, mapped_record)
                    if field_key in existing_fields:
                        skipped += 1
                        continue
                    columns = [name for name in shared_columns if name != "id"]
                    values = [row[name] for name in columns]
                    values[columns.index("member_id")] = local_member_id
                    placeholders = ",".join("?" for _ in columns)
                    target.execute(
                        f"INSERT INTO {_quoted(table)} ({','.join(_quoted(name) for name in columns)}) VALUES ({placeholders})",
                        values,
                    )
                    existing_fields.add(field_key)
                    imported += 1
                if progress is not None:
                    progress("merge", key, imported + skipped, max(1, imported + skipped + len(source_rows)))
                continue

            if table == "members":
                for row in target_rows:
                    if row["id"] is not None:
                        member_id_map.setdefault(int(row["id"]), int(row["id"]))

            for row in source_rows:
                record = dict(row)
                identity = _record_key(table, record)
                if identity in existing_keys:
                    if table == "members" and record.get("id") is not None:
                        local_id = existing_ids_by_key.get(identity)
                        if local_id is not None:
                            member_id_map[int(record["id"])] = local_id
                    if table == "songs" and record.get("id") is not None and id_map_out is not None:
                        local_id = existing_ids_by_key.get(identity)
                        if local_id is not None:
                            id_map_out[int(record["id"])] = local_id
                    skipped += 1
                    continue
                columns = [name for name in shared_columns if name != "id"]
                if not columns:
                    skipped += 1
                    continue
                placeholders = ",".join("?" for _ in columns)
                target.execute(
                    f"INSERT INTO {_quoted(table)} ({','.join(_quoted(name) for name in columns)}) VALUES ({placeholders})",
                    [record[name] for name in columns],
                )
                local_id = int(target.execute("SELECT last_insert_rowid()").fetchone()[0])
                if table == "members" and record.get("id") is not None:
                    member_id_map[int(record["id"])] = local_id
                if table == "songs" and record.get("id") is not None and id_map_out is not None:
                    id_map_out[int(record["id"])] = local_id
                if table in {"songs", "members"}:
                    existing_ids_by_key[identity] = local_id
                existing_keys.add(identity)
                imported += 1
            if progress is not None:
                progress("merge", key, imported + skipped, max(1, imported + skipped + len(source_rows)))
        target.commit()
    except Exception as error:
        target.rollback()
        if isinstance(error, DataTransferError):
            raise
        raise DataTransferError(f"Could not merge {DATABASES[key][1]}: {error}") from error
    finally:
        target.close()
        source.close()
    return imported, skipped


def _replace_database(source_path: Path, target_path: Path, key: str, progress: PROGRESS | None) -> None:
    """What: Replace a selected database from its validated staging snapshot.

    Why included: Import UI offers explicit per-database Replace alongside Merge.
    Where used: Full database imports and rollback restoration.
    Returns: None; reports SQLite backup progress to the supplied callback.
    """
    source = sqlite3.connect(_readonly_uri(source_path), uri=True, timeout=30.0)
    target = sqlite3.connect(target_path, timeout=30.0)
    try:
        def report(_status: int, remaining: int, total: int) -> None:
            """What: Forward SQLite backup completion to the progress listener.

            Why included: Large replacements need observable progress.
            Where used: sqlite3.Connection.backup() callback.
            Returns: None; emits completed and total page counts.
            """
            if progress is not None:
                progress("replace", key, max(0, total - remaining), max(1, total))
        source.backup(target, pages=256, progress=report, sleep=0.01)
        target.commit()
        result = target.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise DataTransferError(f"Replaced {DATABASES[key][1]} failed its integrity check.")
    except sqlite3.Error as error:
        raise DataTransferError(f"Could not replace {DATABASES[key][1]}: {error}") from error
    finally:
        target.close()
        source.close()


def _snapshot_database(source: Path, destination: Path, key: str, progress: PROGRESS | None = None) -> None:
    """What: Create a consistent SQLite snapshot, including committed WAL content.

    Why included: Exports and rollback backups must not capture partial live writes.
    Where used: Database ZIP exports and all import backup paths.
    Returns: None; validates the completed destination snapshot.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_connection = sqlite3.connect(_readonly_uri(source), uri=True, timeout=30.0)
        destination_connection = sqlite3.connect(destination, timeout=30.0)
        try:
            def report(_status: int, remaining: int, total: int) -> None:
                """What: Forward SQLite snapshot page progress to the UI callback.

                Why included: Snapshotting large databases can take noticeable time.
                Where used: sqlite3.Connection.backup() callback.
                Returns: None; reports completed and total pages.
                """
                if progress is not None:
                    progress("snapshot", key, max(0, total - remaining), max(1, total))
            source_connection.backup(destination_connection, pages=256, progress=report, sleep=0.01)
            destination_connection.commit()
        finally:
            destination_connection.close()
            source_connection.close()
    except sqlite3.Error as error:
        raise DataTransferError(f"Could not safely snapshot {DATABASES[key][1]}: {error}") from error
    _validate_database(destination, key)


def _sha256(path: Path) -> str:
    """What: Hash a file using bounded-memory chunks.

    Why included: Transfer packages must detect corruption across PCs.
    Where used: Database and media manifest generation and validation.
    Returns: Lowercase hexadecimal SHA-256 digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_record(path: Path, key: str) -> dict[str, object]:
    """What: Create validated manifest metadata for one database snapshot.

    Why included: Receivers need to verify database identity, size, and contents.
    Where used: Portable ZIP creation.
    Returns: JSON-serializable database metadata.
    """
    return {
        "key": key,
        "filename": DATABASES[key][0],
        "size": path.stat().st_size,
        "sha256": _sha256(path),
        "user_version": _validate_database(path, key),
    }


def _write_transfer_archive(
    destination: Path,
    snapshots: dict[str, Path],
    progress: PROGRESS | None = None,
    scope: str = "databases",
    service_dates: tuple[str, ...] | list[str] | None = None,
    media_files: dict[str, Path] | None = None,
    media_sources: dict[str, list[str]] | None = None,
) -> None:
    """What: Write database snapshots and optional referenced media into a ZIP.

    Why included: Portable transfers need a verifiable manifest and atomic archive creation.
    Where used: Database/service-day exports and pre-import backup archives.
    Returns: None; publishes the completed archive at the destination path.
    """
    records = [_manifest_record(snapshots[key], key) for key in snapshots]
    manifest = {
        "format": TRANSFER_FORMAT,
        "format_version": TRANSFER_VERSION,
        "application": "The Light Ministries",
        "scope": scope,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "databases": records,
    }
    if service_dates is not None:
        manifest["service_dates"] = list(service_dates)
    media_records = []
    for archive_path, path in (media_files or {}).items():
        media_records.append({
            "archive_path": archive_path,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
            "source_paths": (media_sources or {}).get(archive_path, []),
        })
    if media_records:
        manifest["media_files"] = media_records
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
            total = sum(path.stat().st_size for path in snapshots.values()) + sum(path.stat().st_size for path in (media_files or {}).values())
            completed = 0
            for key, path in snapshots.items():
                entry = zipfile.ZipInfo(f"databases/{DATABASES[key][0]}")
                entry.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as source, archive.open(entry, "w") as target:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(block)
                        completed += len(block)
                        if progress is not None:
                            progress("archive", key, completed, max(1, total))
            for archive_path, path in (media_files or {}).items():
                entry = zipfile.ZipInfo(archive_path)
                entry.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as source, archive.open(entry, "w") as target:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(block)
                        completed += len(block)
                        if progress is not None:
                            progress("archive_media", "services", completed, max(1, total))
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def export_database_archive(
    destination: Path,
    selected: tuple[str, ...] | list[str],
    data_directory: Path | None = None,
    progress: PROGRESS | None = None,
    include_media: bool = True,
    selected_media_paths: tuple[Path, ...] | None = None,
    members_password: str | None = None,
) -> Path:
    """What: Export databases, protecting member details with the operator's password.

    Why included: Member data is encrypted for this computer only; exports must be
    re-encrypted with a password so the receiving computer can open them.
    Where used: Data menu Export Databases action.
    Returns: Path to the finished portable ZIP.
    """
    keys = _normalize_selection(selected)
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    if "members" in keys:
        if sqlite3.ENCRYPTION_AVAILABLE and not members_password:
            raise DataTransferError("Choose a password to protect the exported member details.")
        sqlite3.ensure_encrypted(root / DATABASES["members"][0])
    with sqlite3.data_directory(root), sqlite3.transfer_password(members_password):
        return _export_database_archive(destination, keys, root, progress, include_media, selected_media_paths)


def _export_database_archive(
    destination: Path,
    selected: tuple[str, ...] | list[str],
    data_directory: Path | None = None,
    progress: PROGRESS | None = None,
    include_media: bool = True,
    selected_media_paths: tuple[Path, ...] | None = None,
) -> Path:
    """What: Export selected consistent databases and their referenced backgrounds.

    Why included: Users need safe cross-installation database transfer.
    Where used: Data menu Export Databases action.
    Returns: Path to the finished portable ZIP.
    """
    keys = _normalize_selection(selected)
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    missing = [DATABASES[key][1] for key in keys if not (root / DATABASES[key][0]).is_file()]
    if missing:
        raise DataTransferError("These selected databases do not exist yet: " + ", ".join(missing))
    target = Path(destination)
    with tempfile.TemporaryDirectory(prefix="LightDataExport-") as temporary:
        snapshots: dict[str, Path] = {}
        media_references: set[str] = set()
        for index, key in enumerate(keys, 1):
            snapshot = Path(temporary) / DATABASES[key][0]
            _snapshot_database(root / DATABASES[key][0], snapshot, key, progress)
            if key == "members" and sqlite3.ENCRYPTION_AVAILABLE:
                sqlite3.rekey_to_password(snapshot, sqlite3.current_transfer_password())
            snapshots[key] = snapshot
            if include_media and key in {"services", "library"}:
                media_references.update(_database_background_references(snapshot, key))
            if progress is not None:
                progress("snapshot", key, index, len(keys))
        if selected_media_paths is not None:
            allowed_paths = {Path(path).resolve() for path in selected_media_paths}
            media_references = {reference for reference in media_references if _resolve_background_reference(get_app_base_path(), root, reference) in allowed_paths}
        media_files, media_sources = _prepare_media_attachments(get_app_base_path(), root, media_references)
        _write_transfer_archive(target, snapshots, progress, media_files=media_files, media_sources=media_sources)
    return target


def list_service_dates(data_directory: Path | None = None) -> tuple[str, ...]:
    """What: Read the distinct dates represented in saved service records.

    Why included: The day-transfer dialog must offer only dates with saved records.
    Where used: Data menu Export Service Day(s) selector.
    Returns: Unique service dates, newest first.
    """
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    path = root / DATABASES["services"][0]
    if not path.is_file():
        return ()
    try:
        connection = sqlite3.connect(_readonly_uri(path), uri=True, timeout=30.0)
        try:
            dates = {
                _service_date_key(row[0])
                for row in connection.execute("SELECT service_date FROM service_entries")
                if _service_date_key(row[0])
            }
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise DataTransferError(f"Could not read saved service dates: {error}") from error
    return tuple(sorted(dates, reverse=True))


def _normalize_selected_service_dates(dates: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """What: Normalize and deduplicate the user-selected service dates.

    Why included: Equivalent date formats must select the same stored day.
    Where used: Service-day export and import entry points.
    Returns: Unique normalized date keys; raises when none are selected.
    """
    selected = tuple(dict.fromkeys(_service_date_key(value) for value in dates if _service_date_key(value)))
    if not selected:
        raise DataTransferError("Select at least one saved service date.")
    return selected


def _collect_background_references(value: object, references: set[str]) -> None:
    """What: Recursively collect non-empty background_path values from saved payloads.

    Why included: Service snapshots nest media paths inside JSON queue/settings data.
    Where used: Service-day and database transfer media collection.
    Returns: None; adds references to the supplied set.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() == "background_path" and isinstance(child, str) and child.strip():
                references.add(child.strip())
            _collect_background_references(child, references)
    elif isinstance(value, list):
        for child in value:
            _collect_background_references(child, references)


def _resolve_background_reference(app_root: Path, data_root: Path, reference: str) -> Path | None:
    """What: Resolve an absolute or app/shared-root-relative background reference.

    Why included: Exports must locate media selected on the source installation.
    Where used: Transfer background attachment preparation.
    Returns: Existing resolved file path, or None if the referenced image is unavailable.
    """
    resolved_reference = resolve_background_file(reference, app_root, data_root)
    if resolved_reference is not None:
        return resolved_reference.resolve()
    candidate = Path(reference)
    original = candidate if candidate.is_absolute() else app_root / candidate
    if original.resolve().is_relative_to((app_root / "Backgrounds").resolve()) and (data_root / ".background-migration.json").is_file():
        return None
    candidates = [candidate] if candidate.is_absolute() else [app_root / candidate, data_root / candidate]
    candidates.extend((app_root / "Backgrounds" / candidate.name, data_root / "Backgrounds" / candidate.name))
    for path in candidates:
        try:
            resolved = path.resolve(strict=True)
            if resolved.is_file():
                return resolved
        except OSError:
            continue
    return None


def _prepare_media_attachments(app_root: Path, data_root: Path, references: set[str]) -> tuple[dict[str, Path], dict[str, list[str]]]:
    """What: Deduplicate referenced media by content and build portable ZIP paths.

    Why included: Only selected services' media should travel, without repeated copies.
    Where used: Database and service-day export preparation.
    Returns: Archive-path-to-file mapping and original-reference mappings.
    """
    attachments: dict[str, Path] = {}
    source_paths: dict[str, list[str]] = {}
    resolved_paths: dict[Path, str] = {}
    total_bytes = 0
    for reference in sorted(references):
        source = _resolve_background_reference(app_root, data_root, reference)
        if source is None:
            continue
        archive_path = resolved_paths.get(source)
        if archive_path is None:
            digest = _sha256(source)
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", source.name).strip("._") or "background"
            archive_path = f"media/{digest[:24]}_{safe_name}"
            resolved_paths[source] = archive_path
            attachments[archive_path] = source
            total_bytes += source.stat().st_size
            if total_bytes > MAX_DATABASE_BYTES:
                raise DataTransferError("The selected backgrounds exceed the supported transfer size.")
        source_paths.setdefault(archive_path, []).append(reference)
    return attachments, source_paths


def _referenced_library_songs(service_database: Path, selected_dates: set[str]) -> dict[int, dict[str, object]]:
    """What: Extract Library song snapshots referenced by selected service queues.

    Why included: Receiving installations need missing source songs for imported services.
    Where used: Service-day export before filtering the Library snapshot.
    Returns: Source song IDs mapped to reconstructable Library records.
    """
    connection = sqlite3.connect(_readonly_uri(service_database), uri=True, timeout=30.0)
    try:
        rows = connection.execute("SELECT service_date, display_tab_items FROM service_entries").fetchall()
    finally:
        connection.close()
    songs: dict[int, dict[str, object]] = {}
    for service_date, display_items in rows:
        if _service_date_key(service_date) not in selected_dates:
            continue
        try:
            items = json.loads(str(display_items or "[]"))
        except (TypeError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or str(item.get("source") or "").casefold() != "library":
                continue
            source_id = str(item.get("source_item_id") or "").strip()
            if not source_id:
                parts = str(item.get("id") or "").split("-")
                source_id = parts[1] if len(parts) > 2 and parts[0] == "library" else ""
            if source_id.isdigit():
                song_id = int(source_id)
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                verses = item.get("verses")
                if not isinstance(verses, list):
                    verses = [str(item.get("text") or "")]
                subtitle = str(item.get("subtitle") or "")
                key_match = re.search(r"\[([^\]]+)\]\s*$", subtitle)
                song_key = key_match.group(1).strip() if key_match else ""
                created_at = str(item.get("created_at") or "")
                songs[song_id] = {
                    "id": song_id,
                    "title": title,
                    "song_key": song_key,
                    "verses_json": json.dumps([str(verse) for verse in verses], ensure_ascii=False),
                    "created_at": created_at,
                    "updated_at": created_at,
                    "background_path": str(item.get("background_path") or ""),
                    "font_settings_json": "{}",
                }
    return songs


def _filter_library_snapshot(source_path: Path, destination: Path, referenced_songs: dict[int, dict[str, object]], progress: PROGRESS | None) -> bool:
    """What: Build a compact Library DB containing only referenced songs.

    Why included: Day-specific exports should not ship the entire Library database.
    Where used: Service-day ZIP export.
    Returns: True when a valid filtered Library snapshot was written.
    """
    if not referenced_songs:
        return False
    if source_path.is_file():
        _snapshot_database(source_path, destination, "library", progress)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(destination)
        try:
            connection.execute("""CREATE TABLE songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, song_key TEXT NOT NULL DEFAULT '',
                verses_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                background_path TEXT NOT NULL DEFAULT '', font_settings_json TEXT NOT NULL DEFAULT '{}'
            )""")
            connection.commit()
        finally:
            connection.close()
    connection = sqlite3.connect(destination)
    try:
        existing_ids = {int(row[0]) for row in connection.execute("SELECT id FROM songs")}
        keep_ids = existing_ids.intersection(referenced_songs)
        if keep_ids:
            placeholders = ",".join("?" for _ in keep_ids)
            connection.execute(f"DELETE FROM songs WHERE id NOT IN ({placeholders})", tuple(sorted(keep_ids)))
        else:
            connection.execute("DELETE FROM songs")
        for song_id, song in referenced_songs.items():
            if song_id in existing_ids:
                continue
            columns = ("id", "title", "song_key", "verses_json", "created_at", "updated_at", "background_path", "font_settings_json")
            connection.execute(
                f"INSERT INTO songs ({','.join(_quoted(column) for column in columns)}) VALUES ({','.join('?' for _ in columns)})",
                [song[column] for column in columns],
            )
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone():
            connection.execute("UPDATE sqlite_sequence SET seq=(SELECT COALESCE(MAX(id),0) FROM songs) WHERE name='songs'")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    _validate_database(destination, "library")
    return destination.stat().st_size > 0


def _database_background_references(database_path: Path, key: str) -> set[str]:
    """What: Collect background paths stored in Library or Services database rows.

    Why included: Selected database transfers should include their referenced images.
    Where used: Database and service-day export media collection.
    Returns: Unique saved background references.
    """
    connection = sqlite3.connect(_readonly_uri(database_path), uri=True, timeout=30.0)
    references: set[str] = set()
    try:
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        table = "service_entries" if key == "services" else "songs"
        if table not in table_names:
            return references
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")}
        if key == "services":
            for row in connection.execute("SELECT background_path,welcome_settings,display_tab_items,presentation_settings,display_settings FROM service_entries"):
                _collect_background_references({
                    "background_path": row[0], "welcome_settings": _safe_json(row[1]),
                    "display_tab_items": _safe_json(row[2]), "presentation_settings": _safe_json(row[3]),
                    "display_settings": _safe_json(row[4]),
                }, references)
        elif "background_path" in columns:
            references.update(str(row[0]).strip() for row in connection.execute("SELECT background_path FROM songs") if str(row[0] or "").strip())
    finally:
        connection.close()
    return references


def _safe_json(value: object) -> object:
    """What: Parse a JSON-backed database field with a safe empty fallback.

    Why included: Legacy/corrupt optional settings must not block media path scans.
    Where used: Database background-reference discovery and rewriting.
    Returns: Decoded JSON value or an empty mapping on invalid input.
    """
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}


def _rewrite_background_value(value: object, path_map: dict[str, str]) -> tuple[object, bool]:
    """What: Recursively replace imported background references in JSON values.

    Why included: Service queue payloads persist paths at arbitrary nested levels.
    Where used: Rewriting staged SQLite JSON fields before import.
    Returns: Rewritten JSON value and whether any path changed.
    """
    changed = False
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, child in value.items():
            if str(key).casefold() == "background_path" and isinstance(child, str) and child in path_map:
                result[key] = path_map[child]
                changed = True
            else:
                result[key], child_changed = _rewrite_background_value(child, path_map)
                changed = changed or child_changed
        return result, changed
    if isinstance(value, list):
        result_list = []
        for child in value:
            rewritten, child_changed = _rewrite_background_value(child, path_map)
            result_list.append(rewritten)
            changed = changed or child_changed
        return result_list, changed
    return value, False


def _rewrite_database_background_paths(database_path: Path, key: str, path_map: dict[str, str]) -> None:
    """What: Rewrite direct and nested background fields in a staged DB.

    Why included: Imported rows must point to local shared Backgrounds files.
    Where used: Generic and service-day import staging.
    Returns: None; updates only staged records whose references are in path_map.
    """
    if not path_map:
        return
    tables = ("songs",) if key == "library" else ("service_entries",) if key == "services" else ("welcome_notes",) if key == "welcome_notes" else ()
    connection = sqlite3.connect(database_path, timeout=30.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        existing_tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in tables:
            if table not in existing_tables:
                continue
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")}
            json_columns = columns.intersection({"welcome_settings", "display_tab_items", "presentation_settings", "display_settings", "settings_json"})
            select_columns = ["rowid AS __rowid__"]
            if "background_path" in columns:
                select_columns.append("background_path")
            select_columns.extend(sorted(json_columns))
            selected_sql = ["rowid AS __rowid__"] + [_quoted(column) for column in select_columns[1:]]
            rows = connection.execute(f"SELECT {','.join(selected_sql)} FROM {_quoted(table)}").fetchall()
            for row in rows:
                updates: dict[str, str] = {}
                if "background_path" in columns and row["background_path"] in path_map:
                    updates["background_path"] = path_map[row["background_path"]]
                for column in json_columns:
                    original = row[column]
                    rewritten, changed = _rewrite_background_value(_safe_json(original), path_map)
                    if changed:
                        updates[column] = json.dumps(rewritten, ensure_ascii=False)
                if updates:
                    assignments = ",".join(f"{_quoted(column)}=?" for column in updates)
                    connection.execute(f"UPDATE {_quoted(table)} SET {assignments} WHERE rowid=?", [*updates.values(), row["__rowid__"]])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def copy_legacy_backgrounds(app_root: Path, data_root: Path) -> dict[str, str]:
    """What: Copy legacy backgrounds into shared data, verifying each copy and avoiding overwrites.

    Why included: Older installs kept backgrounds beside the program, which standard users cannot write to.
    Where used: migrate_legacy_backgrounds() during startup migration.
    Returns: Mapping of original file paths to their copied shared-data paths.
    """
    legacy_root = Path(app_root) / "Backgrounds"
    shared_root = Path(data_root) / "Backgrounds"
    if not legacy_root.is_dir() or legacy_root.resolve() == shared_root.resolve():
        return {}
    copied: dict[str, str] = {}
    for source in sorted(legacy_root.rglob("*")):
        if source.is_symlink():
            raise DataTransferError(f"Background migration cannot follow a symbolic link: {source}")
        if not source.is_file():
            continue
        relative = source.relative_to(legacy_root)
        target = shared_root / relative
        source_hash = _sha256(source)
        if target.exists() and (not target.is_file() or _sha256(target) != source_hash):
            target = target.with_name(f"{target.stem}_{source_hash[:12]}{target.suffix}")
            if target.exists() and (not target.is_file() or _sha256(target) != source_hash):
                raise DataTransferError(f"Background migration found a conflicting destination: {target}")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".background-migration-", delete=False) as temporary:
                temporary_path = Path(temporary.name)
            try:
                shutil.copyfile(source, temporary_path)
                if _sha256(temporary_path) != source_hash:
                    raise DataTransferError(f"Background verification failed: {source}")
                with target.open("xb") as destination:
                    try:
                        with temporary_path.open("rb") as incoming:
                            shutil.copyfileobj(incoming, destination)
                    except Exception:
                        destination.close()
                        target.unlink(missing_ok=True)
                        raise
            finally:
                temporary_path.unlink(missing_ok=True)
        if _sha256(target) != source_hash:
            raise DataTransferError(f"Background verification failed: {target}")
        copied[str(source.resolve())] = str(target.resolve())
    return copied


def migrate_legacy_backgrounds(app_root: Path, data_root: Path) -> int:
    """What: Copy legacy media and rewrite saved references after backing up affected data.

    Why included: Services and songs must keep showing their backgrounds after media moves to shared data.
    Where used: Application startup, once per data folder.
    Returns: Number of database references that were updated.
    """
    app_root, data_root = Path(app_root).resolve(), Path(data_root).resolve()
    legacy_root = app_root / "Backgrounds"
    shared_root = data_root / "Backgrounds"
    if not legacy_root.is_dir():
        return 0
    receipt = data_root / ".background-migration.json"
    copied: dict[str, str] = {}
    try:
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        records = saved["files"]
        sources = [path for path in legacy_root.rglob("*") if path.is_file()]
        if len(sources) != len(records):
            raise ValueError("Background list changed")
        for source in sources:
            if source.is_symlink():
                raise ValueError("Background is a symbolic link")
            record = records[str(source.resolve())]
            target = Path(record["target"])
            if not target.resolve().is_relative_to(shared_root.resolve()) or not target.is_file():
                raise ValueError("Background destination changed")
            source_stat, target_stat = source.stat(), target.stat()
            if [source_stat.st_size, source_stat.st_mtime_ns, target_stat.st_size, target_stat.st_mtime_ns] != record["stats"]:
                raise ValueError("Background file changed")
            copied[str(source.resolve())] = str(target.resolve())
    except (OSError, ValueError, TypeError, KeyError):
        copied = copy_legacy_backgrounds(app_root, data_root)
    if not copied:
        return 0
    normalized_paths: dict[str, str] = {}
    for source, target in copied.items():
        source_path = Path(source)
        for value in (str(source_path), str(source_path.relative_to(app_root))):
            normalized_paths[os.path.normcase(os.path.normpath(value))] = target
    references: set[str] = set()
    databases: list[tuple[str, Path]] = []
    for key in ("library", "services", "welcome_notes"):
        database_path = data_root / DATABASES[key][0]
        if not database_path.is_file():
            continue
        databases.append((key, database_path))
        connection = sqlite3.connect(_readonly_uri(database_path), uri=True, timeout=30.0)
        try:
            table = DATABASES[key][2]
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")}
            selected = sorted(columns.intersection({"background_path", "welcome_settings", "display_tab_items", "presentation_settings", "display_settings", "settings_json"}))
            if not selected:
                continue
            for row in connection.execute(f"SELECT {','.join(_quoted(column) for column in selected)} FROM {_quoted(table)}"):
                for column, value in zip(selected, row):
                    if column == "background_path" and isinstance(value, str):
                        references.add(value)
                    elif value:
                        _collect_background_references(json.loads(value), references)
        finally:
            connection.close()
    json_files: list[tuple[Path, object]] = []
    for name in ("bible_settings.json", "display_queue_bridge.json"):
        path = data_root / name
        if path.is_file():
            content = json.loads(path.read_text(encoding="utf-8"))
            _collect_background_references(content, references)
            json_files.append((path, content))
    path_map = {reference: normalized_paths[normalized] for reference in references
                if (normalized := os.path.normcase(os.path.normpath(reference))) in normalized_paths}
    if path_map:
        backup_root = data_root / ".background-migration-backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_dir = Path(tempfile.mkdtemp(prefix="migration-", dir=backup_root))
        for _key, database_path in databases:
            source_connection = sqlite3.connect(_readonly_uri(database_path), uri=True, timeout=30.0)
            target_connection = sqlite3.connect(backup_dir / database_path.name)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
                source_connection.close()
        for path, _content in json_files:
            shutil.copy2(path, backup_dir / path.name)
        for key, database_path in databases:
            _rewrite_database_background_paths(database_path, key, path_map)
        for path, content in json_files:
            rewritten, changed = _rewrite_background_value(content, path_map)
            if changed:
                temporary = path.with_suffix(path.suffix + ".background-migration.tmp")
                temporary.write_text(json.dumps(rewritten, indent=2, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
    records = {}
    for source, target in copied.items():
        source_stat, target_stat = Path(source).stat(), Path(target).stat()
        records[source] = {"target": target, "stats": [source_stat.st_size, source_stat.st_mtime_ns, target_stat.st_size, target_stat.st_mtime_ns]}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(json.dumps({"files": records}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(receipt)
    return len(copied)


def _rewrite_service_library_ids(database_path: Path, song_ids: dict[int, int], selected_dates: set[str]) -> None:
    """What: Remap Library source IDs embedded in selected service queue JSON.

    Why included: A receiving Library may assign different primary keys to imported songs.
    Where used: Service-day import after missing songs are merged into Library.
    Returns: None; updates only selected dates and referenced Library queue items.
    """
    if not song_ids:
        return
    connection = sqlite3.connect(database_path, timeout=30.0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT id,service_date,display_tab_items FROM service_entries").fetchall()
        for entry_id, service_date, display_text in rows:
            if _service_date_key(service_date) not in selected_dates:
                continue
            try:
                items = json.loads(str(display_text or "[]"))
            except (TypeError, ValueError):
                continue
            if not isinstance(items, list):
                continue
            changed = False
            for item in items:
                if not isinstance(item, dict) or str(item.get("source") or "").casefold() != "library":
                    continue
                source_id = str(item.get("source_item_id") or "").strip()
                if not source_id:
                    parts = str(item.get("id") or "").split("-")
                    source_id = parts[1] if len(parts) > 2 and parts[0] == "library" else ""
                if not source_id.isdigit() or int(source_id) not in song_ids:
                    continue
                old_id = int(source_id)
                local_id = song_ids[old_id]
                item["source_item_id"] = str(local_id)
                item_id = str(item.get("id") or "")
                match = re.match(r"^library-(\d+)-(.*)$", item_id)
                if match:
                    item["id"] = f"library-{local_id}-{match.group(2)}"
                changed = True
            if changed:
                connection.execute("UPDATE service_entries SET display_tab_items=? WHERE id=?", (json.dumps(items, ensure_ascii=True), entry_id))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _install_transfer_media(
    archive: zipfile.ZipFile,
    manifest: dict[str, object],
    entries: dict[str, zipfile.ZipInfo],
    data_root: Path,
    staging_directory: Path,
    progress: PROGRESS | None,
) -> tuple[dict[str, str], list[Path]]:
    """What: Verify and install referenced media into shared Backgrounds if missing.

    Why included: Cross-PC service imports need playable local images without overwriting existing files.
    Where used: Database and service-day import before DB reference rewriting.
    Returns: Original-to-local path mapping and files created for rollback cleanup.
    """
    media_records = manifest.get("media_files", [])
    path_map: dict[str, str] = {}
    created: list[Path] = []
    if not media_records:
        return path_map, created
    staging_directory.mkdir(parents=True, exist_ok=True)
    media_root = data_root / "Backgrounds"
    media_root.mkdir(parents=True, exist_ok=True)
    application_media_root = get_app_base_path() / "Backgrounds"
    try:
        for index, record in enumerate(media_records, 1):
            archive_path = str(record["archive_path"])
            staged_file = staging_directory / PurePosixPath(archive_path).name
            digest = hashlib.sha256()
            with archive.open(entries[archive_path], "r") as source, staged_file.open("wb") as target:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(block)
                    digest.update(block)
            expected_hash = str(record["sha256"])
            if digest.hexdigest() != expected_hash:
                raise DataTransferError(f"Checksum verification failed for background {PurePosixPath(archive_path).name}.")
            existing = next((path for search_root in (media_root, application_media_root)
                if search_root.is_dir() for path in search_root.rglob("*")
                if path.is_file() and _sha256(path) == expected_hash), None)
            if existing is not None:
                installed_path = existing
            else:
                original_names = record.get("source_paths") or [PurePosixPath(archive_path).name]
                filename = Path(str(original_names[0]).replace("\\", "/")).name
                filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename).strip("._") or PurePosixPath(archive_path).name
                installed_path = media_root / filename
                if installed_path.exists() and _sha256(installed_path) != expected_hash:
                    original = Path(filename)
                    installed_path = media_root / f"{original.stem}_{expected_hash[:12]}{original.suffix}"
                if not installed_path.exists():
                    temporary_target = installed_path.with_name(installed_path.name + ".importing")
                    shutil.copyfile(staged_file, temporary_target)
                    os.replace(temporary_target, installed_path)
                    created.append(installed_path)
            for source_path in record.get("source_paths", []):
                path_map[str(source_path)] = str(installed_path)
            if progress is not None:
                progress("backgrounds", "services", index, len(media_records))
        return path_map, created
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def export_service_days_archive(
    destination: Path,
    service_dates: tuple[str, ...] | list[str],
    data_directory: Path | None = None,
    progress: PROGRESS | None = None,
) -> Path:
    """What: Export selected saved dates with referenced songs and background files.

    Why included: Churches need portable day-level transfers without unrelated schedules.
    Where used: Data menu Export Service Day(s) action.
    Returns: Path to the filtered service-day ZIP.
    """
    selected_dates = _normalize_selected_service_dates(service_dates)
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    source = root / DATABASES["services"][0]
    if not source.is_file():
        raise DataTransferError("The Services database does not exist yet.")
    target = Path(destination)
    with tempfile.TemporaryDirectory(prefix="LightServiceDayExport-") as temporary:
        snapshot = Path(temporary) / "service.db"
        _snapshot_database(source, snapshot, "services", progress)
        selected_set = set(selected_dates)
        connection = sqlite3.connect(snapshot)
        try:
            entry_rows = connection.execute("SELECT id, service_date FROM service_entries").fetchall()
            retained_ids = [int(row[0]) for row in entry_rows if _service_date_key(row[1]) in selected_set]
            if not retained_ids:
                raise DataTransferError("No saved service records were found for the selected date(s).")
            keep_ids = set(retained_ids)
            unwanted_ids = [int(row[0]) for row in entry_rows if int(row[0]) not in keep_ids]
            if unwanted_ids:
                connection.executemany("DELETE FROM service_entries WHERE id=?", [(row_id,) for row_id in unwanted_ids])
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_info'").fetchone():
                info_rows = connection.execute("SELECT id, service_date FROM service_info").fetchall()
                unwanted_info = [int(row[0]) for row in info_rows if _service_date_key(row[1]) not in selected_set]
                if unwanted_info:
                    connection.executemany("DELETE FROM service_info WHERE id=?", [(row_id,) for row_id in unwanted_info])
            connection.commit()
            connection.execute("VACUUM")
        finally:
            connection.close()
        _validate_database(snapshot, "services")
        snapshots = {"services": snapshot}
        referenced_songs = _referenced_library_songs(snapshot, selected_set)
        library_path = root / DATABASES["library"][0]
        if referenced_songs:
            library_snapshot = Path(temporary) / "library.db"
            if _filter_library_snapshot(library_path, library_snapshot, referenced_songs, progress):
                snapshots["library"] = library_snapshot
        media_references: set[str] = set()
        media_references.update(_database_background_references(snapshot, "services"))
        if "library" in snapshots:
            media_references.update(_database_background_references(snapshots["library"], "library"))
        media_files, media_sources = _prepare_media_attachments(get_app_base_path(), root, media_references)
        _write_transfer_archive(target, snapshots, progress, scope="service_days", service_dates=selected_dates,
            media_files=media_files, media_sources=media_sources)
    return target


def _read_manifest(archive_path: Path) -> tuple[dict[str, object], dict[str, zipfile.ZipInfo]]:
    """What: Validate transfer ZIP paths, manifest records, sizes, and media entries.

    Why included: Imports must reject malformed, oversized, or unsafe archive contents.
    Where used: Database and service-day import/selection workflows.
    Returns: Validated manifest and entry lookup by database/media path.
    """
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise DataTransferError(f"Could not open this database ZIP: {error}") from error
    try:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise DataTransferError("The ZIP contains duplicate file paths.")
        if "manifest.json" not in names:
            raise DataTransferError("This ZIP has no database-transfer manifest.")
        manifest_info = archive.getinfo("manifest.json")
        if manifest_info.file_size > 1024 * 1024:
            raise DataTransferError("The database-transfer manifest is too large.")
        try:
            manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DataTransferError("The database-transfer manifest is invalid.") from error
        if (manifest.get("format") != TRANSFER_FORMAT or manifest.get("format_version") != TRANSFER_VERSION or
                manifest.get("application") != "The Light Ministries"):
            raise DataTransferError("This is not a supported PewVista database ZIP.")
        records = manifest.get("databases")
        if not isinstance(records, list) or not records:
            raise DataTransferError("The ZIP does not contain any databases.")
        available: dict[str, zipfile.ZipInfo] = {}
        expected_names = {"manifest.json"}
        total_size = 0
        for record in records:
            if not isinstance(record, dict):
                raise DataTransferError("The ZIP manifest contains an invalid database record.")
            key = record.get("key")
            if key not in DATABASES or key in available:
                raise DataTransferError("The ZIP manifest contains an unknown or repeated database.")
            filename = DATABASES[key][0]
            if record.get("filename") != filename:
                raise DataTransferError("A database name does not match its application type.")
            entry_name = f"databases/{filename}"
            if entry_name not in names:
                raise DataTransferError(f"The ZIP is missing {filename}.")
            info = archive.getinfo(entry_name)
            if (info.external_attr >> 16) & 0xF000 == 0xA000:
                raise DataTransferError("Symbolic links are not allowed in database ZIPs.")
            expected_size = record.get("size")
            if not isinstance(expected_size, int) or expected_size <= 0 or expected_size != info.file_size:
                raise DataTransferError(f"The size recorded for {filename} is invalid.")
            total_size += expected_size
            if total_size > MAX_DATABASE_BYTES:
                raise DataTransferError("The selected transfer exceeds the supported total database size.")
            checksum = record.get("sha256")
            if not isinstance(checksum, str) or len(checksum) != 64:
                raise DataTransferError(f"The checksum recorded for {filename} is invalid.")
            available[key] = info
            expected_names.add(entry_name)
        media_records = manifest.get("media_files", [])
        if not isinstance(media_records, list):
            raise DataTransferError("The ZIP media manifest is invalid.")
        for record in media_records:
            if not isinstance(record, dict):
                raise DataTransferError("The ZIP contains an invalid background record.")
            entry_name = record.get("archive_path")
            path = PurePosixPath(str(entry_name or ""))
            if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "media" or any(part in {".", ".."} for part in path.parts):
                raise DataTransferError("The ZIP contains an unsafe background path.")
            if entry_name in available or entry_name not in names:
                raise DataTransferError("The ZIP contains a missing or duplicate background file.")
            info = archive.getinfo(entry_name)
            if (info.external_attr >> 16) & 0xF000 == 0xA000:
                raise DataTransferError("Symbolic links are not allowed in database ZIPs.")
            size = record.get("size")
            checksum = record.get("sha256")
            source_paths = record.get("source_paths")
            if not isinstance(size, int) or size <= 0 or size != info.file_size:
                raise DataTransferError("A background file size does not match its manifest.")
            if not isinstance(checksum, str) or len(checksum) != 64:
                raise DataTransferError("A background file checksum is invalid.")
            if not isinstance(source_paths, list) or not source_paths or any(not isinstance(value, str) or not value for value in source_paths):
                raise DataTransferError("A background file has no valid source path mapping.")
            total_size += size
            if total_size > MAX_DATABASE_BYTES:
                raise DataTransferError("The database and background files exceed the supported transfer size.")
            available[str(entry_name)] = info
            expected_names.add(str(entry_name))
        if set(names) != expected_names:
            raise DataTransferError("The ZIP contains unexpected files; only a valid database export can be imported.")
        return manifest, available
    finally:
        archive.close()


def importable_databases(archive_path: Path) -> tuple[str, ...]:
    """What: List supported databases present in a validated transfer ZIP.

    Why included: Import selection should only show databases actually in the archive.
    Where used: Data menu Import Databases selector.
    Returns: Database keys in application display order.
    """
    _, entries = _read_manifest(Path(archive_path))
    return tuple(key for key in DATABASES if key in entries)


def importable_service_dates(archive_path: Path) -> tuple[str, ...]:
    """What: Read selected dates recorded in a validated service-day ZIP.

    Why included: The import UI must offer only dates included in the archive.
    Where used: Data menu Import Service Day(s) selector.
    Returns: Unique normalized service-date keys.
    """
    manifest, entries = _read_manifest(Path(archive_path))
    database_keys = set(entries).intersection(DATABASES)
    if manifest.get("scope") != "service_days" or "services" not in database_keys or not database_keys.issubset({"services", "library"}):
        raise DataTransferError("Select a service-day export ZIP.")
    dates = manifest.get("service_dates")
    if not isinstance(dates, list):
        raise DataTransferError("This service ZIP has no selected-date list.")
    return _normalize_selected_service_dates([str(value) for value in dates])


def import_database_archive(
    archive_path: Path,
    selected: tuple[str, ...] | list[str],
    data_directory: Path | None = None,
    progress: PROGRESS | None = None,
    modes: dict[str, str] | None = None,
    members_password: str | None = None,
) -> tuple[tuple[str, ...], Path]:
    """What: Import databases, unlocking password-protected member details.

    Why included: Imported member data is re-encrypted for this computer immediately.
    Where used: Data menu Import Databases action.
    Returns: Imported database keys and the pre-import backup ZIP path.
    """
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    sqlite3.ensure_encrypted(root / DATABASES["members"][0])
    with sqlite3.data_directory(root), sqlite3.transfer_password(members_password):
        return _import_database_archive(archive_path, selected, root, progress, modes)


def _import_database_archive(
    archive_path: Path,
    selected: tuple[str, ...] | list[str],
    data_directory: Path | None = None,
    progress: PROGRESS | None = None,
    modes: dict[str, str] | None = None,
) -> tuple[tuple[str, ...], Path]:
    """What: Validate selected DBs, back them up, then merge or replace per choice.

    Why included: Cross-PC imports need integrity checks and recoverable destination data.
    Where used: Data menu Import Databases action.
    Returns: Imported database keys and the pre-import backup ZIP path.
    """
    keys = _normalize_selection(selected)
    requested_modes = modes or {}
    database_modes = {key: str(requested_modes.get(key, "merge")).casefold() for key in keys}
    invalid_modes = [key for key, mode in database_modes.items() if mode not in {"merge", "replace"}]
    if invalid_modes:
        raise DataTransferError("Choose Merge or Replace for each selected database.")
    manifest, entries = _read_manifest(Path(archive_path))
    missing = [key for key in keys if key not in entries]
    if missing:
        raise DataTransferError("The selected ZIP does not contain: " + ", ".join(DATABASES[key][1] for key in missing))
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="LightDataImport-") as temporary:
        stage = Path(temporary) / "incoming"
        rollback = Path(temporary) / "rollback"
        stage.mkdir()
        rollback.mkdir()
        staged: dict[str, Path] = {}
        total_bytes = sum(int(next(record["size"] for record in manifest["databases"] if record["key"] == key)) for key in keys)
        copied = 0
        with zipfile.ZipFile(archive_path, "r") as archive:
            for key in keys:
                record = next(item for item in manifest["databases"] if item["key"] == key)
                path = stage / DATABASES[key][0]
                digest = hashlib.sha256()
                with archive.open(entries[key], "r") as source, path.open("wb") as target:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(block)
                        digest.update(block)
                        copied += len(block)
                        if progress is not None:
                            progress("validate", key, copied, max(1, total_bytes))
                if digest.hexdigest() != record["sha256"]:
                    raise DataTransferError(f"Checksum verification failed for {DATABASES[key][1]}.")
                _validate_database(path, key)
                if key == "members" and sqlite3.ENCRYPTION_AVAILABLE:
                    sqlite3.adopt_into_machine_key(path)
                staged[key] = path
        snapshots: dict[str, Path | None] = {}
        backup_files: dict[str, Path] = {}
        for key in keys:
            target = root / DATABASES[key][0]
            if target.exists():
                backup = rollback / DATABASES[key][0]
                _snapshot_database(target, backup, key, progress)
                snapshots[key] = backup
                backup_files[key] = backup
            else:
                snapshots[key] = None
        backup_archive: Path | None = None
        if backup_files:
            backup_directory = root / ".import-backups"
            backup_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_archive = backup_directory / f"before-import-{stamp}.zip"
            _write_transfer_archive(backup_archive, backup_files, progress)
            for stale_backup in sorted(backup_directory.glob("*.zip"))[:-2]:
                try:
                    stale_backup.unlink()
                except OSError:
                    continue
        created_media: list[Path] = []
        attempted: list[str] = []
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                media_path_map, created_media = _install_transfer_media(
                    archive, manifest, entries, root, stage / "media", progress
                )
            for key, path in staged.items():
                _rewrite_database_background_paths(path, key, media_path_map)
            for index, key in enumerate(keys, 1):
                target_path = root / DATABASES[key][0]
                attempted.append(key)
                if database_modes[key] == "replace":
                    _replace_database(staged[key], target_path, key, progress)
                else:
                    _merge_database(staged[key], target_path, key, progress)
                if progress is not None:
                    progress("import", key, index, len(keys))
        except Exception as error:
            rollback_errors: list[str] = []
            for key in reversed(attempted):
                target_path = root / DATABASES[key][0]
                snapshot = snapshots[key]
                try:
                    if snapshot is None:
                        target_path.unlink(missing_ok=True)
                        target_path.with_name(target_path.name + "-wal").unlink(missing_ok=True)
                        target_path.with_name(target_path.name + "-shm").unlink(missing_ok=True)
                    else:
                        source_connection = sqlite3.connect(_readonly_uri(snapshot), uri=True, timeout=30.0)
                        target_connection = sqlite3.connect(target_path, timeout=30.0)
                        try:
                            source_connection.backup(target_connection, pages=256, sleep=0.01)
                            target_connection.commit()
                        finally:
                            target_connection.close()
                            source_connection.close()
                except Exception as rollback_error:
                    rollback_errors.append(f"{DATABASES[key][1]}: {rollback_error}")
            message = f"Import failed; selected databases were rolled back: {error}"
            if rollback_errors:
                message += " Rollback issues: " + "; ".join(rollback_errors)
            for path in created_media:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    rollback_errors.append(f"Background cleanup {path.name}: {cleanup_error}")
            raise DataTransferError(message) from error
    if progress is not None:
        progress("complete", "", len(keys), len(keys))
    return keys, backup_archive


def import_service_days_archive(
    archive_path: Path,
    service_dates: tuple[str, ...] | list[str],
    mode: str = "merge",
    data_directory: Path | None = None,
    progress: PROGRESS | None = None,
) -> tuple[tuple[str, ...], Path | None]:
    """What: Import selected dates, service rows, referenced songs, and backgrounds.

    Why included: Day transfers must carry required media without altering other dates.
    Where used: Data menu Import Service Day(s) action.
    Returns: Selected normalized dates and optional pre-import backup ZIP path.
    """
    selected_dates = _normalize_selected_service_dates(service_dates)
    mode = str(mode).casefold()
    if mode not in {"merge", "replace"}:
        raise DataTransferError("Choose Merge or Replace for the selected service dates.")
    manifest, entries = _read_manifest(Path(archive_path))
    database_keys = set(entries).intersection(DATABASES)
    if manifest.get("scope") != "service_days" or "services" not in database_keys or not database_keys.issubset({"services", "library"}):
        raise DataTransferError("Select a service-day export ZIP.")
    archive_dates = set(importable_service_dates(Path(archive_path)))
    if not set(selected_dates).issubset(archive_dates):
        raise DataTransferError("The archive does not contain all selected service dates.")
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    root.mkdir(parents=True, exist_ok=True)
    normalized_dates = set(selected_dates)
    with tempfile.TemporaryDirectory(prefix="LightServiceDayImport-") as temporary:
        stage = Path(temporary) / "incoming"
        rollback = Path(temporary) / "rollback"
        stage.mkdir()
        rollback.mkdir()
        staged: dict[str, Path] = {}
        records = {record["key"]: record for record in manifest["databases"]}
        total = sum(int(records[key]["size"]) for key in database_keys)
        copied = 0
        with zipfile.ZipFile(archive_path, "r") as archive:
            for key in ("services", "library"):
                if key not in database_keys:
                    continue
                destination = stage / DATABASES[key][0]
                digest = hashlib.sha256()
                with archive.open(entries[key], "r") as source, destination.open("wb") as target:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(block)
                        digest.update(block)
                        copied += len(block)
                        if progress is not None:
                            progress("validate", key, copied, max(1, total))
                if digest.hexdigest() != records[key]["sha256"]:
                    raise DataTransferError(f"Checksum verification failed for {DATABASES[key][1]}.")
                _validate_database(destination, key)
                staged[key] = destination
            path_map, created_media = _install_transfer_media(archive, manifest, entries, root, stage / "media", progress)
        for key, path in staged.items():
            _rewrite_database_background_paths(path, key, path_map)

        target_paths = {key: root / DATABASES[key][0] for key in staged}
        snapshots: dict[str, Path | None] = {}
        backup_files: dict[str, Path] = {}
        for key, target_path in target_paths.items():
            if target_path.is_file():
                backup = rollback / target_path.name
                _snapshot_database(target_path, backup, key, progress)
                snapshots[key] = backup
                backup_files[key] = backup
            else:
                snapshots[key] = None
        backup_name: Path | None = None
        if backup_files:
            backup_directory = root / ".import-backups"
            backup_directory.mkdir(parents=True, exist_ok=True)
            backup_name = backup_directory / f"before-service-days-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.zip"
            _write_transfer_archive(backup_name, backup_files, progress)
            for stale_backup in sorted(backup_directory.glob("*.zip"))[:-2]:
                try:
                    stale_backup.unlink()
                except OSError:
                    continue

        attempted: list[str] = []
        try:
            library_id_map: dict[int, int] = {}
            if "library" in staged:
                attempted.append("library")
                _merge_database(staged["library"], target_paths["library"], "library", progress, id_map_out=library_id_map)
            _rewrite_service_library_ids(staged["services"], library_id_map, normalized_dates)

            service_path = target_paths["services"]
            source = sqlite3.connect(_readonly_uri(staged["services"]), uri=True, timeout=30.0)
            target = sqlite3.connect(service_path, timeout=30.0)
            source.row_factory = sqlite3.Row
            target.row_factory = sqlite3.Row
            attempted.append("services")
            try:
                target.execute("PRAGMA foreign_keys = ON")
                target.execute("BEGIN IMMEDIATE")
                for table in ("service_entries", "service_info"):
                    source_definition = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if source_definition is not None and target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
                        target.execute(source_definition["sql"])
                        for index in source.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)):
                            target.execute(index["sql"])
                _allow_multiple_service_info_rows(target)
                for table in ("service_entries", "service_info"):
                    source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    target_tables = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if table not in source_tables or table not in target_tables:
                        continue
                    source_columns = [row[1] for row in source.execute(f"PRAGMA table_info({_quoted(table)})")]
                    target_columns = [row[1] for row in target.execute(f"PRAGMA table_info({_quoted(table)})")]
                    shared_columns = [name for name in source_columns if name in target_columns and name != "id"]
                    source_rows = source.execute(f"SELECT * FROM {_quoted(table)}").fetchall()
                    target_rows = target.execute(f"SELECT * FROM {_quoted(table)}").fetchall()
                    selected_rows = [row for row in source_rows if _service_date_key(row["service_date"]) in normalized_dates]
                    if mode == "replace":
                        existing_ids = [int(row["id"]) for row in target_rows if _service_date_key(row["service_date"]) in normalized_dates]
                        if existing_ids:
                            target.executemany("DELETE FROM service_entries WHERE id=?", [(row_id,) for row_id in existing_ids])
                        target_rows = [row for row in target_rows if _service_date_key(row["service_date"]) not in normalized_dates]
                    existing_keys = {_record_key(table, dict(row)) for row in target_rows}
                    existing_keys.discard(None)
                    for row in selected_rows:
                        identity = _record_key(table, dict(row))
                        if identity in existing_keys:
                            continue
                        target.execute(
                            f"INSERT INTO {_quoted(table)} ({','.join(_quoted(name) for name in shared_columns)}) VALUES ({','.join('?' for _ in shared_columns)})",
                            [row[name] for name in shared_columns],
                        )
                        existing_keys.add(identity)
                    if progress is not None:
                        progress("merge" if mode == "merge" else "replace", "services", len(selected_rows), max(1, len(selected_rows)))
                target.commit()
                integrity = target.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise DataTransferError("The imported Services database failed its integrity check.")
            except Exception:
                target.rollback()
                raise
            finally:
                target.close()
                source.close()
        except Exception as error:
            rollback_errors: list[str] = []
            for key in reversed(attempted):
                target_path = target_paths[key]
                snapshot = snapshots[key]
                try:
                    if snapshot is None:
                        target_path.unlink(missing_ok=True)
                        target_path.with_name(target_path.name + "-wal").unlink(missing_ok=True)
                        target_path.with_name(target_path.name + "-shm").unlink(missing_ok=True)
                    else:
                        _replace_database(snapshot, target_path, key, progress)
                except Exception as rollback_error:
                    rollback_errors.append(f"{DATABASES[key][1]}: {rollback_error}")
            for path in created_media:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    rollback_errors.append(f"Background cleanup {path.name}: {cleanup_error}")
            message = f"Could not import selected service dates; destination was restored: {error}"
            if rollback_errors:
                message += " Rollback issues: " + "; ".join(rollback_errors)
            if isinstance(error, DataTransferError) and not rollback_errors:
                raise
            raise DataTransferError(message) from error
    if progress is not None:
        progress("complete", "services", 1, 1)
    return selected_dates, backup_name
