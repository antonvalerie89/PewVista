"""Persist and normalize the Display queue and saved-service records.

The queue JSON is the live handoff between content tabs and Display; service
entries are stored in SQLite and restore queue snapshots for later use.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from datetime import datetime
import threading

from app_paths import get_app_base_path, get_app_data_path
from music_licensing import normalize_license_provider


DISPLAY_QUEUE_BRIDGE_RELATIVE_PATH = Path("Data") / "display_queue_bridge.json"
SERVICE_DB_RELATIVE_PATH = Path("Data") / "service.db"
_DISPLAY_QUEUE_BRIDGE_LOCK = threading.RLock()


def get_display_queue_bridge_path() -> Path:
    """Resolve the JSON file used for the live cross-tab queue.

    What: Joins the configured queue-relative path to the application root.
    Why included: Content tabs and Display must read and write the same handoff file.
    Where used: Queue load, save, and startup validation functions in this module.
    Returns: Absolute Path to the display queue bridge JSON file.
    """
    configured = DISPLAY_QUEUE_BRIDGE_RELATIVE_PATH
    if configured.is_absolute():
        return configured
    return get_app_data_path() / configured.name


def get_service_db_path() -> Path:
    """Resolve the SQLite database path for saved service schedules.

    What: Joins the service database-relative path to the application root.
    Why included: Schedule recall, service saves, and History share one persistent DB.
    Where used: Service connection and existence checks in this module.
    Returns: Absolute Path to the service database file.
    """
    configured = SERVICE_DB_RELATIVE_PATH
    if configured.is_absolute():
        return configured
    return get_app_data_path() / configured.name


def _append_ui_error_log(message: str) -> None:
    """What: Append a timestamped persistence-recovery message to the UI error log.

    Why included: Startup repairs should leave an operational trail without making
    logging failure prevent application recovery.
    Where used: validate_persistence_storage() when it repairs queue storage.
    Returns: None; silently ignores file-write errors.
    """
    log_path = get_app_data_path() / "ui_errors.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{datetime.now().isoformat(timespec='seconds')} | {message}\n")
    except OSError:
        return


def _connect_service_db() -> sqlite3.Connection:
    """Open the service SQLite database with concurrency-friendly settings.

    What: Creates the parent directory, opens a connection with a 15-second timeout,
    and enables WAL plus NORMAL synchronous mode when supported.
    Why included: UI and worker tasks may access saved services concurrently.
    Where used: Schema setup, save, lookup, history, cleanup, and restore routines.
    Returns: Open sqlite3.Connection owned by the caller.
    """
    service_db_path = get_service_db_path()
    service_db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(service_db_path, timeout=15.0)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        pass
    return connection


def _ensure_service_db_schema(connection: sqlite3.Connection) -> None:
    """Create the service schema and add supported columns to older databases.

    What: Ensures service_entries and service_info tables and lookup indexes exist,
    adding display_settings when migrating a prior schema.
    Why included: Existing user databases must remain readable across app upgrades.
    Where used: validate_persistence_storage() and service database operations.
    Returns: None; executes schema changes on the supplied open connection.
    """
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        pass
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS service_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            saved_at TEXT NOT NULL,
            service_date TEXT NOT NULL,
            service_time TEXT NOT NULL,
            preacher_name TEXT NOT NULL,
            welcome_field_one TEXT NOT NULL,
            welcome_field_two TEXT NOT NULL,
            background_path TEXT NOT NULL DEFAULT '',
            welcome_settings TEXT NOT NULL DEFAULT '{}',
            display_tab_items TEXT NOT NULL DEFAULT '[]',
            presentation_settings TEXT NOT NULL DEFAULT '{}',
            display_settings TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(service_entries)").fetchall()}
    if "display_settings" not in columns:
        connection.execute("ALTER TABLE service_entries ADD COLUMN display_settings TEXT NOT NULL DEFAULT '{}'")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS service_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_date TEXT NOT NULL DEFAULT '',
            service_time TEXT NOT NULL DEFAULT '',
            preacher_name TEXT NOT NULL DEFAULT ''
        )
        """
    )
    service_info_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='service_info'"
    ).fetchone()
    if service_info_sql and "check(id=1)" in "".join(str(service_info_sql[0]).casefold().split()):
        connection.execute(
            "CREATE TABLE service_info_multi (id INTEGER PRIMARY KEY AUTOINCREMENT, service_date TEXT NOT NULL DEFAULT '', service_time TEXT NOT NULL DEFAULT '', preacher_name TEXT NOT NULL DEFAULT '')"
        )
        connection.execute(
            "INSERT INTO service_info_multi (id, service_date, service_time, preacher_name) SELECT id, service_date, service_time, preacher_name FROM service_info"
        )
        connection.execute("DROP TABLE service_info")
        connection.execute("ALTER TABLE service_info_multi RENAME TO service_info")
    try:
        connection.execute("CREATE INDEX IF NOT EXISTS idx_service_entries_date_time ON service_entries (service_date, service_time)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_service_entries_saved_at ON service_entries (saved_at DESC)")
    except Exception:
        pass


def validate_persistence_storage() -> list[str]:
    """Check the live queue JSON and ensure service database tables exist.

    What: Validates or initializes the queue JSON, preserves invalid JSON under a
    timestamped filename before resetting it, and ensures the service schema exists.
    Why included: Startup should recover from damaged persistence without silently
    destroying the original evidence.
    Where used: Application startup diagnostics in main.py.
    Returns: Human-readable recovery issues; an empty list means no repair was needed.
    """
    issues: list[str] = []
    bridge_path = get_display_queue_bridge_path()
    bridge_path.parent.mkdir(parents=True, exist_ok=True)
    if bridge_path.exists():
        try:
            with bridge_path.open("r", encoding="utf-8") as bridge_file:
                _normalize_state(json.load(bridge_file))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            try:
                invalid_copy = bridge_path.with_suffix(f".invalid-{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
                bridge_path.replace(invalid_copy)
            except OSError:
                pass
            save_display_queue_state({"queue_items": []}, record_snapshot=False)
            issues.append("Reset invalid display queue bridge file.")
    else:
        save_display_queue_state({"queue_items": []}, record_snapshot=False)
    with _connect_service_db() as connection:
        _ensure_service_db_schema(connection)
        connection.commit()
    for issue in issues:
        _append_ui_error_log(issue)
    return issues


def _normalize_queue_item(raw_item) -> dict[str, object] | None:
    """Convert legacy/current queue records into the canonical item schema.

    What: Converts a legacy/current mapping into canonical queue keys, normalizes
    text, verses, fonts, background and Bible columns, and keeps source-specific data.
    Why included: The bridge is a compatibility boundary for data produced by older
    tabs, saved services, and the current Display controls.
    Where used: Queue-state normalization and restoration from service records.
    Returns: Canonical item mapping, or None for a non-dictionary input.
    """
    if not isinstance(raw_item, dict):
        return None
    payload = raw_item.get("payload")
    allow_empty_text = str(raw_item.get("source") or "").casefold() == "preparation" and raw_item.get("allow_empty_text") is True
    text_value = str(raw_item.get("text") or "").strip()
    if not text_value and isinstance(payload, dict):
        # Older content tabs stored printable text only inside their nested payload.
        text_value = str(payload.get("overlay_text") or payload.get("verse_text") or "").strip()
    if not text_value and not allow_empty_text:
        # Keep title-only legacy items selectable even when they have no body text.
        text_value = str(raw_item.get("title") or "").strip()
    raw_verses = raw_item.get("verses")
    verses: list[str] = []
    if isinstance(raw_verses, list):
        verses = [str(item).strip() for item in raw_verses if str(item).strip()]
    raw_verse_labels = raw_item.get("verse_labels")
    verse_labels: list[str] = []
    if isinstance(raw_verse_labels, list):
        verse_labels = [str(item).strip() for item in raw_verse_labels if str(item).strip()]
    raw_font_value = raw_item.get("font_value")
    if raw_font_value in (None, "") and isinstance(payload, dict):
        raw_font_value = payload.get("font_value")
    font_value: tuple[str, int, str] | None = None
    if isinstance(raw_font_value, (list, tuple)) and len(raw_font_value) >= 2:
        try:
            font_family = str(raw_font_value[0] or "Segoe UI")
            font_size = int(raw_font_value[1] or 12)
            font_style = str(raw_font_value[2] or "normal") if len(raw_font_value) > 2 else "normal"
            font_value = (font_family, font_size, font_style)
        except Exception:
            font_value = None
    source_value = str(raw_item.get("source") or "item")
    source_key = source_value.casefold()
    background_value = str(raw_item.get("background_path") or "").strip()
    if not background_value and isinstance(payload, dict):
        background_value = str(payload.get("background_path") or "").strip()
    try:
        selected_verse_index = max(0, int(raw_item.get("selected_verse_index") or 0))
    except (TypeError, ValueError):
        selected_verse_index = 0
    normalized_item = {
        "id": str(raw_item.get("id") or ""),
        "active": bool(raw_item.get("active", False)),
        "source": source_value,
        "title": str(raw_item.get("title") or "Untitled"),
        "subtitle": str(raw_item.get("subtitle") or "").strip(),
        "created_at": str(raw_item.get("created_at") or ""),
        "text": text_value,
        "verses": verses,
        "verse_labels": verse_labels,
        "font_value": list(font_value) if isinstance(font_value, tuple) else None,
        "background_path": background_value,
        "selected_verse_index": selected_verse_index,
    }
    raw_columns = raw_item.get("display_columns")
    if allow_empty_text:
        normalized_item["allow_empty_text"] = True
    if raw_item.get("source_item_id") not in (None, ""):
        normalized_item["source_item_id"] = str(raw_item["source_item_id"])
    if source_key == "library":
        license_provider = normalize_license_provider(raw_item.get("license_provider"))
        if license_provider:
            normalized_item["license_provider"] = license_provider
        license_author = str(raw_item.get("license_author") or "").strip()
        if license_author:
            normalized_item["license_author"] = license_author
        license_info = str(raw_item.get("license_info") or "").strip()
        if license_info:
            normalized_item["license_info"] = license_info
    if isinstance(raw_columns, list):
        normalized_columns: list[dict[str, object]] = []
        for raw_column in raw_columns:
            if not isinstance(raw_column, dict) or not isinstance(raw_column.get("verses"), list):
                # Discard malformed columns individually so valid columns can still render.
                continue
            normalized_columns.append(
                {
                    "title": str(raw_column.get("title") or "Bible"),
                    "verses": [str(value or "").strip() for value in raw_column.get("verses")],
                }
            )
        if normalized_columns:
            normalized_item["display_columns"] = normalized_columns
    if source_key == "welcome":
        # Scroll and welcome-section fields belong only to Welcome; other sources have different layouts.
        scroll_enabled_value = bool(raw_item.get("scroll_enabled"))
        if not scroll_enabled_value and isinstance(payload, dict):
            scroll_enabled_value = bool(payload.get("scroll_enabled"))
        raw_scroll_speed = raw_item.get("scroll_speed")
        if raw_scroll_speed in (None, "") and isinstance(payload, dict):
            raw_scroll_speed = payload.get("scroll_speed")
        try:
            scroll_speed_value = int(raw_scroll_speed)
        except Exception:
            scroll_speed_value = 0
        normalized_item["scroll_enabled"] = scroll_enabled_value
        normalized_item["scroll_speed"] = scroll_speed_value
        raw_sections = raw_item.get("welcome_sections")
        if isinstance(raw_sections, dict):
            normalized_item["welcome_sections"] = dict(raw_sections)
    if source_key == "preparation" and isinstance(raw_item.get("welcome_sections"), dict):
        normalized_item["welcome_sections"] = dict(raw_item["welcome_sections"])
    return normalized_item


def _normalize_state(raw_state) -> dict[str, list[dict[str, object]]]:
    """Normalize the top-level queue container and discard malformed items.

    What: Requires a mapping with a list-valued queue_items field and normalizes
    each valid entry through _normalize_queue_item().
    Why included: All consumers need the same predictable queue-state shape.
    Where used: Queue loading, saving, startup validation, and signature generation.
    Returns: Mapping with a queue_items list, empty when the container is invalid.
    """
    if not isinstance(raw_state, dict):
        return {"queue_items": []}
    queue_items = raw_state.get("queue_items")
    if not isinstance(queue_items, list):
        queue_items = []
    normalized_items = []
    for raw_item in queue_items:
        normalized_item = _normalize_queue_item(raw_item)
        if normalized_item is not None:
            normalized_items.append(normalized_item)
    return {"queue_items": normalized_items}


def _load_json_object(raw_value: object) -> object:
    """What: Decode a non-empty JSON string, returning None for invalid input.

    Why included: Legacy service rows may contain absent, blank, or malformed JSON.
    Where used: Service-entry merge, queue reconstruction, and history decoding.
    Returns: Decoded JSON value, or None when decoding is unavailable or fails.
    """
    if isinstance(raw_value, str) and raw_value.strip():
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return None
    return None


def _normalized_queue_items_signature(queue_items: object) -> str:
    """What: Serialize normalized queue items deterministically for equality checks.

    Why included: Recall can skip rewriting the bridge when queue content is unchanged.
    Where used: Service-entry and schedule queue restoration.
    Returns: Compact JSON signature with sorted mapping keys.
    """
    normalized_state = _normalize_state({"queue_items": list(queue_items or []) if isinstance(queue_items, list) else []})
    return json.dumps(normalized_state.get("queue_items") or [], sort_keys=True, separators=(",", ":"))


def _normalize_service_date_text(service_date: object) -> str:
    """What: Normalize recognized date formats to ISO YYYY-MM-DD text.

    Why included: Equivalent legacy date strings must match the same schedule slot.
    Where used: Service save, exact-row cleanup, lookup, history, and restore operations.
    Returns: ISO date for supported formats, otherwise trimmed original text.
    """
    raw_text = str(service_date or "").strip()
    if not raw_text:
        return ""
    for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw_text, date_format).date().isoformat()
        except ValueError:
            continue
    return raw_text


def _deduplicate_service_entries(connection: sqlite3.Connection, service_date: object | None = None, service_time: object | None = None) -> int:
    """Remove only exact duplicate service rows, ignoring their local database IDs.

    What: Find and delete rows whose complete saved service fields match.
    Why included: Imported services can share a date/time while containing distinct
    saved content; only rows whose complete saved fields match can be discarded.
    Where used: Save-time slot cleanup and cleanup_duplicate_service_entries().
    Returns: Number of removed duplicate rows; changes remain on the caller's transaction.
    """
    rows = connection.execute(
        """
        SELECT id, saved_at, service_date, service_time, preacher_name,
               welcome_field_one, welcome_field_two, background_path,
               welcome_settings, display_tab_items, presentation_settings, display_settings
        FROM service_entries
        ORDER BY datetime(saved_at) DESC, id DESC
        """
    ).fetchall()
    normalized_target_date = _normalize_service_date_text(service_date) if service_date is not None else None
    target_time = str(service_time or "").strip() if service_time is not None else None
    seen: set[tuple[object, ...]] = set()
    duplicate_ids: list[int] = []
    for row in rows:
        normalized_row_date = _normalize_service_date_text(row["service_date"])
        row_time = str(row["service_time"] or "").strip()
        if normalized_target_date is not None and normalized_row_date != normalized_target_date:
            continue
        if target_time is not None and row_time != target_time:
            continue
        key = tuple(row[name] for name in (
            "saved_at", "service_date", "service_time", "preacher_name", "welcome_field_one",
            "welcome_field_two", "background_path", "welcome_settings", "display_tab_items",
            "presentation_settings", "display_settings",
        ))
        if key in seen:
            duplicate_ids.append(int(row["id"]))
        else:
            seen.add(key)
    if duplicate_ids:
        connection.executemany("DELETE FROM service_entries WHERE id = ?", [(row_id,) for row_id in duplicate_ids])
    return len(duplicate_ids)


def _load_matching_service_entry_row(connection: sqlite3.Connection, service_date: object, service_time: object) -> sqlite3.Row | None:
    """What: Find the newest row matching a normalized service date/time pair.

    Why included: Save updates and schedule recall must address one consistent slot.
    Where used: save_service_entry() and load_service_entry_by_schedule().
    Returns: Matching sqlite3.Row, or None when the slot has no saved record.
    """
    _ensure_service_db_schema(connection)
    normalized_target_date = _normalize_service_date_text(service_date)
    target_time = str(service_time or "").strip()
    rows = connection.execute(
        """
        SELECT id, saved_at, service_date, service_time, preacher_name,
               welcome_field_one, welcome_field_two, background_path,
               welcome_settings, display_tab_items, presentation_settings, display_settings
        FROM service_entries
        ORDER BY datetime(saved_at) DESC, id DESC
        """
    ).fetchall()
    for row in rows:
        row_time = str(row["service_time"] or "").strip()
        if target_time and row_time != target_time:
            continue
        if _normalize_service_date_text(row["service_date"]) != normalized_target_date:
            continue
        return row
    return None


def _build_queue_items_from_service_entry(row: sqlite3.Row) -> list[dict[str, object]]:
    """What: Reconstruct canonical queue items from serialized service-row fields.

    Why included: Persisted entries may contain partial legacy tab-specific data
    and must be converted into the current live queue schema.
    Where used: History listing, schedule lookup, and service restore functions.
    Returns: Normalized queue item mappings in their saved order.
    """
    raw_display_items = _load_json_object(row["display_tab_items"])
    welcome_settings = _load_json_object(row["welcome_settings"])
    presentation_settings = _load_json_object(row["presentation_settings"])
    display_items = raw_display_items if isinstance(raw_display_items, list) else []
    queue_items: list[dict[str, object]] = []
    saved_at = str(row["saved_at"] or "")
    for item_index, raw_item in enumerate(display_items):
        if not isinstance(raw_item, dict):
            continue
        candidate_item = dict(raw_item)
        source = str(candidate_item.get("source") or "item")
        title = str(candidate_item.get("title") or source.title() or "Untitled")
        subtitle = str(candidate_item.get("subtitle") or "").strip()
        text_value = str(candidate_item.get("text") or "").strip()
        source_key = source.casefold()
        if not text_value and source_key == "welcome":
            welcome_lines = []
            welcome_field_one = str(row["welcome_field_one"] or "").strip()
            welcome_field_two = str(row["welcome_field_two"] or "").strip()
            combine_fields = isinstance(welcome_settings, dict) and bool(welcome_settings.get("welcome_combine_fields"))
            if welcome_field_one:
                welcome_lines.append(welcome_field_one)
            if combine_fields and welcome_field_two:
                welcome_lines.append(welcome_field_two)
            text_value = "\n\n".join(line for line in welcome_lines if line)
        if not text_value and source_key == "presentation" and isinstance(presentation_settings, dict):
            text_value = str(presentation_settings.get("text") or "").strip()
            if not subtitle:
                subtitle = str(presentation_settings.get("title") or "").strip()
        if not text_value and not (source_key == "preparation" and candidate_item.get("allow_empty_text") is True):
            text_value = title
        candidate_item["id"] = str(candidate_item.get("id") or f"service-{int(row['id'])}-{item_index}")
        candidate_item["source"] = source
        candidate_item["title"] = title
        candidate_item["subtitle"] = subtitle
        candidate_item["created_at"] = str(candidate_item.get("created_at") or saved_at)
        candidate_item["text"] = text_value
        normalized_item = _normalize_queue_item(candidate_item)
        if normalized_item is not None:
            queue_items.append(normalized_item)
    return queue_items


def load_display_queue_state() -> dict[str, list[dict[str, object]]]:
    """Read and normalize the live queue JSON used across application tabs.

    What: Reads under the process lock, normalizes valid data, and returns an empty
    queue when the file is missing or unreadable.
    Why included: Consumers need a stable state shape even during startup or recovery.
    Where used: Display controls, content-tab bridge synchronization, and service logic.
    Returns: Normalized state mapping containing queue_items.
    """
    bridge_path = get_display_queue_bridge_path()
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        if not bridge_path.exists():
            return {"queue_items": []}
        try:
            with bridge_path.open("r", encoding="utf-8") as bridge_file:
                return _normalize_state(json.load(bridge_file))
        except (OSError, json.JSONDecodeError):
            return {"queue_items": []}


def _windows_resilient_replace(source: Path, target: Path, max_attempts: int = 5, delay_seconds: float = 0.02) -> None:
    """Replace a target file with retries and a copy fallback for Windows locks.

    What: Retries Path.replace() with increasing short delays, then copies and removes
    the temporary source if replacement remains blocked.
    Why included: Antivirus scanners and other Windows processes can temporarily hold
    the bridge file open during atomic queue updates.
    Where used: save_display_queue_state() when committing its temporary JSON file.
    Returns: None; re-raises the final replace error if the fallback also fails.
    """
    for attempt in range(max_attempts):
        try:
            source.replace(target)
            return
        except OSError as error:
            if attempt < max_attempts - 1:
                # Windows scanners may release the target shortly, so retry before using a copy fallback.
                time.sleep(delay_seconds * (attempt + 1))
            else:
                try:
                    # Preserve queue updates even when atomic replacement stays blocked.
                    shutil.copy2(source, target)
                    source.unlink(missing_ok=True)
                    return
                except Exception:
                    raise error


def save_display_queue_state(state: dict[str, object], record_snapshot: bool = False) -> dict[str, list[dict[str, object]]]:
    """Normalize and atomically publish queue state to the shared bridge file.

    What: Serializes normalized state to a unique temporary file, flushes it to disk,
    and replaces the live bridge while holding the process lock.
    Why included: Readers should not observe partially written JSON, and all tabs
    must agree on the current live queue.
    Where used: Queue mutation operations and Display/content-tab synchronization.
    Returns: The normalized state that was persisted; record_snapshot is retained
    for API compatibility and does not create a service snapshot here.
    """
    normalized_state = _normalize_state(state)
    bridge_path = get_display_queue_bridge_path()
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        # Serialize writers, then publish only after the complete JSON is flushed to a sibling temp file.
        bridge_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = bridge_path.with_name(f"{bridge_path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        try:
            with temp_path.open("w", encoding="utf-8") as bridge_file:
                json.dump(normalized_state, bridge_file, indent=2)
                bridge_file.flush()
                os.fsync(bridge_file.fileno())
            _windows_resilient_replace(temp_path, bridge_path)
        finally:
            try:
                if temp_path.exists():
                    # A failed write/replace must not leave stale temp files beside the live bridge.
                    temp_path.unlink()
            except OSError:
                pass
    return normalized_state


def append_display_queue_item(item: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    """Append a new item to the live cross-tab queue.

    What: Loads current state, appends a copy of the supplied mapping, and persists it.
    Why included: Content tabs need a shared operation for adding newly prepared items.
    Where used: Tab workflows that publish Welcome, Bible, Library, or Preparation items.
    Returns: The normalized persisted queue state.
    """
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        state = load_display_queue_state()
        state["queue_items"].append(dict(item))
        return save_display_queue_state(state)


def upsert_display_queue_item(item: dict[str, object], *, match_title: bool = False) -> dict[str, list[dict[str, object]]]:
    """Insert or replace a source item while retaining its id and active state.

    What: Matches stable source record IDs when supplied, otherwise source and optional title; replacement retains the prior stable
    id, active state, and selected verse index when available.
    Why included: Editing a source item should update its existing queue slot instead
    of creating a duplicate or losing live selection.
    Where used: Content-tab update workflows when an item is revised in place.
    Returns: The normalized persisted queue state.
    """
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        state = load_display_queue_state()
        queue_items = list(state.get("queue_items") or [])
        source = str(item.get("source") or "").casefold()
        title = str(item.get("title") or "").strip().casefold()
        source_item_id = str(item.get("source_item_id") or "")

        def matches_existing(existing_item, *, allow_title_fallback: bool = False) -> bool:
            """What: Match stable source identity before falling back to legacy source/title matching.

            Why included: Renaming an existing Library song must update its queue slot, not append another,
            and recalled/imported service items whose IDs differ from the current Library DB must still update.
            Where used: upsert_display_queue_item() while locating the replacement index.
            Returns: True for the same source-owned record.
            """
            if not isinstance(existing_item, dict) or str(existing_item.get("source") or "").casefold() != source:
                return False
            if source_item_id:
                existing_source_id = str(existing_item.get("source_item_id") or "")
                if not existing_source_id and source == "library":
                    parts = str(existing_item.get("id") or "").split("-")
                    if len(parts) >= 3 and parts[0] == "library" and parts[1].isdigit():
                        existing_source_id = parts[1]
                if existing_source_id:
                    if existing_source_id == source_item_id:
                        return True
                    if not allow_title_fallback:
                        return False
            return not match_title or str(existing_item.get("title") or "").strip().casefold() == title

        existing_index = next(
            (
                index
                for index, existing_item in enumerate(queue_items)
                if matches_existing(existing_item)
            ),
            None,
        )
        if existing_index is None and match_title and title:
            existing_index = next(
                (
                    index
                    for index, existing_item in enumerate(queue_items)
                    if matches_existing(existing_item, allow_title_fallback=True)
                ),
                None,
            )
        replacement = dict(item)
        if existing_index is None:
            queue_items.append(replacement)
        else:
            existing_item = queue_items[existing_index]
            # Preserve user-controlled queue identity/state while replacing source-owned content.
            replacement["id"] = str(existing_item.get("id") or replacement.get("id") or "")
            replacement["active"] = bool(existing_item.get("active"))
            if "selected_verse_index" in existing_item:
                replacement["selected_verse_index"] = existing_item["selected_verse_index"]
                verses = replacement.get("verses")
                if isinstance(verses, list) and verses:
                    replacement["selected_verse_index"] = min(int(existing_item["selected_verse_index"]), len(verses) - 1)
                    idx = max(0, int(replacement.get("selected_verse_index") or 0))
                    if idx < len(verses):
                        replacement["text"] = str(verses[idx] or "").strip()
                    labels = replacement.get("verse_labels")
                    if isinstance(labels, list) and idx < len(labels):
                        label = str(labels[idx] or "").strip()
                        subtitle = str(replacement.get("subtitle") or "")
                        bracket_start = subtitle.rfind(" [")
                        suffix = subtitle[bracket_start:] if bracket_start >= 0 and subtitle.endswith("]") else ""
                        replacement["subtitle"] = f"{label}{suffix}" if label else subtitle
            queue_items[existing_index] = replacement
        return save_display_queue_state({"queue_items": queue_items})


def remove_display_queue_item(item_id: str) -> dict[str, list[dict[str, object]]]:
    """Remove one live queue item by its stable identifier.

    What: Filters the current queue and persists the remaining entries.
    Why included: Display row controls need to remove one item without replacing
    unrelated queue content.
    Where used: Remove actions in the Display tab's queue rows.
    Returns: The normalized persisted queue state.
    """
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        state = load_display_queue_state()
        state["queue_items"] = [item for item in state["queue_items"] if str(item.get("id") or "") != item_id]
        return save_display_queue_state(state)


def move_display_queue_item(item_id: str, step: int) -> dict[str, list[dict[str, object]]]:
    """Move one queue item by a signed number of positions.

    What: Locates the stable ID, clamps its destination to queue boundaries, and
    persists the reordered list; missing IDs and edge no-ops leave state unchanged.
    Why included: Display users need deterministic ordering controls.
    Where used: Up and Down actions in Display queue rows.
    Returns: Current or updated normalized queue state.
    """
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        state = load_display_queue_state()
        queue_items = list(state["queue_items"])
        current_index = next((index for index, item in enumerate(queue_items) if str(item.get("id") or "") == item_id), None)
        if current_index is None:
            return state
        target_index = max(0, min(len(queue_items) - 1, current_index + int(step)))
        if target_index == current_index:
            return state
        queue_item = queue_items.pop(current_index)
        queue_items.insert(target_index, queue_item)
        state["queue_items"] = queue_items
        return save_display_queue_state(state)


def set_display_queue_item_active(item_id: str, active: bool) -> dict[str, list[dict[str, object]]]:
    """Set one item's active state while enforcing a single live queue item.

    What: Updates the target flag and deactivates other entries when activating it.
    Why included: External display output represents one selected item at a time.
    Where used: Active-state controls in the Display queue.
    Returns: Updated normalized state, or unchanged state when the ID is absent.
    """
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        state = load_display_queue_state()
        updated = False
        for queue_item in state["queue_items"]:
            is_target = str(queue_item.get("id") or "") == item_id
            if is_target:
                queue_item["active"] = bool(active)
                updated = True
                continue
            if active:
                queue_item["active"] = False
        if not updated:
            return state
        return save_display_queue_state(state)


def set_display_queue_item_selected_verse(item_id: str, selected_verse_index: int) -> dict[str, list[dict[str, object]]]:
    """Select a verse index for one queue item and clamp it to valid bounds.

    What: Stores an index between zero and the final available verse index.
    Why included: Verse navigation must not persist an out-of-range selection.
    Where used: Verse-selection controls in Display for multi-verse queue items.
    Returns: Updated normalized state, or unchanged state if the item is absent.
    """
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        state = load_display_queue_state()
        for queue_item in state["queue_items"]:
            if str(queue_item.get("id") or "") != item_id:
                continue
            verses = queue_item.get("verses")
            verse_count = len(verses) if isinstance(verses, list) else 0
            upper_bound = max(0, verse_count - 1)
            queue_item["selected_verse_index"] = max(0, min(int(selected_verse_index), upper_bound))
            return save_display_queue_state(state)
        return state


def toggle_display_queue_item_active(item_id: str) -> dict[str, list[dict[str, object]]]:
    """Toggle one item's live state and preserve the single-active-item invariant.

    What: Inverts the target active flag and deactivates all other items if it becomes
    active.
    Why included: The Display checkbox is a toggle, while only one queue item may feed
    the live output.
    Where used: Active checkbuttons in Display queue rows.
    Returns: Updated normalized state, or unchanged state when the item is missing.
    """
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        state = load_display_queue_state()
        target_item = None
        for queue_item in state["queue_items"]:
            if str(queue_item.get("id") or "") == item_id:
                target_item = queue_item
                break
        if not isinstance(target_item, dict):
            return state
        next_active = not bool(target_item.get("active"))
        updated = False
        for queue_item in state["queue_items"]:
            is_target = str(queue_item.get("id") or "") == item_id
            if is_target:
                queue_item["active"] = next_active
                updated = True
                continue
            if next_active:
                queue_item["active"] = False
        if not updated:
            return state
        return save_display_queue_state(state)


def clear_display_queue_items() -> dict[str, list[dict[str, object]]]:
    """Remove all items from the live Display queue.

    What: Persists an empty queue through the shared state writer.
    Why included: Provides an explicit user-facing clear operation distinct from recall.
    Where used: Clear-all action in Display queue controls.
    Returns: The normalized empty queue state.
    """
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        return save_display_queue_state({"queue_items": []})


def reset_active_display_queue_file() -> dict[str, list[dict[str, object]]]:
    """Reset live queue state without recording a saved-service snapshot.

    What: Writes an empty queue and explicitly disables snapshot recording.
    Why included: Clearing recalled data is not itself a request to save history.
    Where used: Service recall handling when the selected schedule has no saved entry.
    Returns: The normalized empty queue state.
    """
    with _DISPLAY_QUEUE_BRIDGE_LOCK:
        return save_display_queue_state({"queue_items": []}, record_snapshot=False)


def save_service_entry(
    service_date: object,
    service_time: object,
    preacher_name: object,
    queue_items: list[dict[str, object]] | None,
    display_settings: dict[str, object] | None = None,
    service_entry_id: object | None = None,
) -> dict[str, object]:
    """Insert a service or update the explicitly selected saved record.

    What: Normalizes inputs, updates the selected same-slot record when its ID is
    supplied; otherwise updates the latest slot or inserts a new one.
    Why included: Service recall needs the queue and display choices captured together,
    with one authoritative entry per schedule slot.
    Where used: Save Service workflow in main.py and service persistence tests.
    Returns: Saved entry mapping with ID, schedule metadata, queue, and display settings.
    """
    normalized_state = _normalize_state({"queue_items": list(queue_items or [])})
    normalized_service_date = _normalize_service_date_text(service_date)
    normalized_service_time = str(service_time or "").strip()
    normalized_preacher_name = str(preacher_name or "").strip()
    saved_at = datetime.now().replace(microsecond=0).isoformat()
    connection = _connect_service_db()
    try:
        connection.row_factory = sqlite3.Row
        _ensure_service_db_schema(connection)
        if service_entry_id is not None:
            existing_row = connection.execute(
                "SELECT id, service_date, service_time FROM service_entries WHERE id = ?",
                (str(service_entry_id),),
            ).fetchone()
            if existing_row is None or (
                _normalize_service_date_text(existing_row["service_date"]) != normalized_service_date
                or str(existing_row["service_time"] or "").strip() != normalized_service_time
            ):
                raise ValueError("The selected saved service no longer exists for this date and time. Select it again before saving.")
        else:
            _deduplicate_service_entries(connection, normalized_service_date, normalized_service_time)
            existing_row = _load_matching_service_entry_row(connection, normalized_service_date, normalized_service_time)
        display_items_json = json.dumps(normalized_state.get("queue_items") or [], ensure_ascii=True)
        display_settings_json = json.dumps(display_settings if isinstance(display_settings, dict) else {}, ensure_ascii=True)
        if existing_row is None:
            cursor = connection.execute(
                """
                INSERT INTO service_entries (
                    saved_at, service_date, service_time, preacher_name,
                    welcome_field_one, welcome_field_two, background_path,
                    welcome_settings, display_tab_items, presentation_settings, display_settings
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    saved_at,
                    normalized_service_date,
                    normalized_service_time,
                    normalized_preacher_name,
                    "",
                    "",
                    "",
                    "{}",
                    display_items_json,
                    "{}",
                    display_settings_json,
                ),
            )
            service_entry_id = int(cursor.lastrowid)
        else:
            service_entry_id = int(existing_row["id"])
            connection.execute(
                """
                UPDATE service_entries
                SET saved_at = ?,
                    service_date = ?,
                    service_time = ?,
                    preacher_name = ?,
                    display_tab_items = ?,
                    display_settings = ?
                WHERE id = ?
                """,
                (
                    saved_at,
                    normalized_service_date,
                    normalized_service_time,
                    normalized_preacher_name,
                    display_items_json,
                    display_settings_json,
                    service_entry_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO service_info (service_date, service_time, preacher_name)
            SELECT ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM service_info
                WHERE service_date = ? AND service_time = ? AND preacher_name = ?
            )
            """,
            (normalized_service_date, normalized_service_time, normalized_preacher_name,
             normalized_service_date, normalized_service_time, normalized_preacher_name),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "id": str(service_entry_id),
        "saved_at": saved_at,
        "service_date": normalized_service_date,
        "service_time": normalized_service_time,
        "preacher_name": normalized_preacher_name,
        "queue_items": list(normalized_state.get("queue_items") or []),
        "display_settings": dict(display_settings) if isinstance(display_settings, dict) else {},
    }


def cleanup_duplicate_service_entries() -> int:
    """Merge duplicate date/time service rows in the persisted database.

    What: Skips database creation when absent; otherwise removes exact duplicate rows and commits.
    Why included: Repeated transfers should not accumulate byte-for-byte identical saved records.
    Where used: Persistence maintenance and startup/diagnostic cleanup workflows.
    Returns: Number of duplicate rows removed, or zero when the database is absent.
    """
    service_db_path = get_service_db_path()
    if not service_db_path.exists():
        return 0
    connection = _connect_service_db()
    try:
        connection.row_factory = sqlite3.Row
        deleted_rows = _deduplicate_service_entries(connection)
        connection.commit()
    finally:
        connection.close()
    return deleted_rows


def delete_service_entry(service_entry_id: object) -> bool:
    """What: Delete one saved service history row by its exact local record ID.

    Why included: Users must be able to remove one version without deleting same-slot services.
    Where used: History popup Delete Selected Service action.
    Returns: True if one row was deleted, otherwise False.
    """
    service_db_path = get_service_db_path()
    if not service_db_path.is_file():
        return False
    connection = _connect_service_db()
    try:
        cursor = connection.execute("DELETE FROM service_entries WHERE id = ?", (str(service_entry_id),))
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def list_service_history_entries(limit: int = 100) -> list[dict[str, object]]:
    """Load recent service records in the expanded format used by History.

    What: Queries newest saved records, including distinct versions for one schedule slot,
    decodes JSON settings, and rebuilds normalized queue items.
    Why included: History needs complete inspectable snapshots rather than selector labels.
    Where used: History window loading in main.py.
    Returns: Ordered service-entry mappings, or an empty list when no database exists.
    """
    service_db_path = get_service_db_path()
    if not service_db_path.exists():
        return []
    connection = _connect_service_db()
    try:
        connection.row_factory = sqlite3.Row
        _ensure_service_db_schema(connection)
        rows = connection.execute(
            """
            SELECT id, saved_at, service_date, service_time, preacher_name,
                   welcome_field_one, welcome_field_two, welcome_settings,
                   display_tab_items, presentation_settings, display_settings
            FROM service_entries
            ORDER BY datetime(saved_at) DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        connection.close()
    history_entries: list[dict[str, object]] = []
    for row in rows:
        slot_key = (_normalize_service_date_text(row["service_date"]), str(row["service_time"] or "").strip())
        history_entries.append(
            {
                "id": str(row["id"]),
                "saved_at": str(row["saved_at"] or "").strip(),
                "service_date": slot_key[0],
                "service_time": slot_key[1],
                "preacher_name": str(row["preacher_name"] or "").strip(),
                "welcome_field_one": str(row["welcome_field_one"] or "").strip(),
                "welcome_field_two": str(row["welcome_field_two"] or "").strip(),
                "welcome_settings": _load_json_object(row["welcome_settings"]),
                "presentation_settings": _load_json_object(row["presentation_settings"]),
                "display_settings": _load_json_object(row["display_settings"]),
                "queue_items": _build_queue_items_from_service_entry(row),
            }
        )
    return history_entries


def list_saved_service_entries(limit: int = 100) -> list[dict[str, str]]:
    """Load compact labels for the Display tab's saved-service selector.

    What: Queries recent schedule metadata and formats date/time, preacher, and save time.
    Why included: Users need to choose a saved service without loading full queue data.
    Where used: Saved-service selector refresh in the Display tab.
    Returns: Ordered mappings with string id and label fields; empty if DB is absent.
    """
    service_db_path = get_service_db_path()
    if not service_db_path.exists():
        return []
    with _connect_service_db() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, saved_at, service_date, service_time, preacher_name
            FROM service_entries
            ORDER BY datetime(saved_at) DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    entries: list[dict[str, str]] = []
    for row in rows:
        service_date = str(row["service_date"] or "").strip()
        service_time = str(row["service_time"] or "").strip()
        saved_at = str(row["saved_at"] or "").strip()
        preacher_name = str(row["preacher_name"] or "").strip()
        label_parts = [part for part in (service_date, service_time) if part]
        label = " - ".join(label_parts) if label_parts else saved_at or f"Service {int(row['id'])}"
        if preacher_name:
            label = f"{label} | {preacher_name}"
        if saved_at:
            label = f"{label} | Saved {saved_at}"
        entries.append(
            {
                "id": str(row["id"]),
                "label": label,
            }
        )
    return entries


def list_service_entries_by_schedule(service_date: object, service_time: object) -> list[dict[str, str]]:
    """What: List all saved service versions matching a normalized date/time slot.

    Why included: Multiple distinct services for one slot need explicit user selection.
    Where used: Main-window date/time recall chooser.
    Returns: Newest-first selector records with ID, preacher, and save time.
    """
    service_db_path = get_service_db_path()
    if not service_db_path.exists():
        return []
    normalized_date = _normalize_service_date_text(service_date)
    normalized_time = str(service_time or "").strip()
    connection = _connect_service_db()
    try:
        connection.row_factory = sqlite3.Row
        _ensure_service_db_schema(connection)
        rows = connection.execute(
            """
            SELECT id, saved_at, service_date, service_time, preacher_name
            FROM service_entries
            ORDER BY datetime(saved_at) DESC, id DESC
            """
        ).fetchall()
    finally:
        connection.close()
    entries: list[dict[str, str]] = []
    for row in rows:
        row_date = _normalize_service_date_text(row["service_date"])
        row_time = str(row["service_time"] or "").strip()
        if row_date != normalized_date or row_time != normalized_time:
            continue
        saved_at = str(row["saved_at"] or "").strip()
        preacher = str(row["preacher_name"] or "").strip()
        label_parts = [part for part in (preacher, f"Saved {saved_at}" if saved_at else f"Record {row['id']}") if part]
        entries.append({
            "id": str(row["id"]),
            "service_date": row_date,
            "service_time": row_time,
            "preacher_name": preacher,
            "saved_at": saved_at,
            "label": " | ".join(label_parts) or f"Record {row['id']}",
        })
    return entries


def load_service_entry_by_schedule(service_date: object, service_time: object, service_entry_id: object | None = None) -> dict[str, object] | None:
    """Load one full saved service record for a normalized date/time slot.

    What: Finds the matching row and returns schedule details, decoded display settings,
    and reconstructed queue items.
    Why included: Schedule recall needs a consistent record independent of the selector UI.
    Where used: restore_display_queue_from_service_schedule() during main-window recall.
    Returns: Service mapping when found, otherwise None.
    """
    service_db_path = get_service_db_path()
    if not service_db_path.exists():
        return None
    connection = _connect_service_db()
    try:
        connection.row_factory = sqlite3.Row
        if service_entry_id is None:
            row = _load_matching_service_entry_row(connection, service_date, service_time)
        else:
            _ensure_service_db_schema(connection)
            row = connection.execute(
                """
                SELECT id, saved_at, service_date, service_time, preacher_name,
                       welcome_field_one, welcome_field_two, welcome_settings,
                       display_tab_items, presentation_settings, display_settings
                FROM service_entries WHERE id = ?
                """,
                (str(service_entry_id),),
            ).fetchone()
            if row is not None and (
                _normalize_service_date_text(row["service_date"]) != _normalize_service_date_text(service_date)
                or str(row["service_time"] or "").strip() != str(service_time or "").strip()
            ):
                row = None
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "saved_at": str(row["saved_at"] or ""),
        "service_date": _normalize_service_date_text(row["service_date"]),
        "service_time": str(row["service_time"] or "").strip(),
        "preacher_name": str(row["preacher_name"] or "").strip(),
        "queue_items": _build_queue_items_from_service_entry(row),
        "display_settings": _load_json_object(row["display_settings"]),
    }


def _build_inactive_queue_items(queue_items: list[dict[str, object]] | None) -> list[dict[str, object]]:
    """What: Copy valid queue mappings and force every copied item inactive.

    Why included: Recalling a service should prepare its queue without unexpectedly
    sending content live to the secondary display.
    Where used: History-entry and schedule-based queue restoration.
    Returns: New list of item copies with active set to False.
    """
    inactive_items: list[dict[str, object]] = []
    for queue_item in list(queue_items or []):
        if not isinstance(queue_item, dict):
            continue
        inactive_item = dict(queue_item)
        inactive_item["active"] = False
        inactive_items.append(inactive_item)
    return inactive_items


def restore_display_queue_from_service_entry(service_entry_id: str) -> dict[str, object]:
    """Restore a selected History record into the live queue in an inactive state.

    What: Looks up the entry, reconstructs and deactivates its items, writes only when
    the normalized queue differs, and attaches its saved display settings.
    Why included: Users can inspect or prepare a historical service without activating
    external output as a side effect.
    Where used: History restore workflow that selects a saved service by ID.
    Returns: Restored state with display settings, or current queue state if not found.
    """
    service_db_path = get_service_db_path()
    if not service_db_path.exists():
        return load_display_queue_state()
    with _connect_service_db() as connection:
        connection.row_factory = sqlite3.Row
        _ensure_service_db_schema(connection)
        row = connection.execute(
            """
            SELECT id, saved_at, service_date, service_time, preacher_name,
                   welcome_field_one, welcome_field_two, welcome_settings,
                   display_tab_items, presentation_settings, display_settings
            FROM service_entries
            WHERE id = ?
            """,
            (str(service_entry_id or ""),),
        ).fetchone()
    if row is None:
        return load_display_queue_state()
    queue_items = _build_inactive_queue_items(_build_queue_items_from_service_entry(row))
    current_state = load_display_queue_state()
    if _normalized_queue_items_signature(current_state.get("queue_items")) == _normalized_queue_items_signature(queue_items):
        restored_state = current_state
    else:
        restored_state = save_display_queue_state({"queue_items": queue_items}, record_snapshot=False)
    restored_state["display_settings"] = _load_json_object(row["display_settings"])
    return restored_state


def restore_display_queue_from_service_schedule(service_date: object, service_time: object, service_entry_id: object | None = None) -> dict[str, object] | None:
    """Find a saved service by date/time and restore it without live activation.

    What: Loads the date/time record, makes all queue items inactive, compares their
    deterministic signature with the current queue, and writes only when changed.
    Why included: Schedule changes should not activate output or cause redundant
    bridge writes and redraws.
    Where used: Main-window debounced date/time recall workflow.
    Returns: Service mapping with a changed flag, or None when no saved slot exists.
    """
    service_entry = load_service_entry_by_schedule(service_date, service_time, service_entry_id)
    if service_entry is None:
        return None
    queue_items = _build_inactive_queue_items(service_entry.get("queue_items") if isinstance(service_entry, dict) else None)
    service_entry["queue_items"] = queue_items
    current_state = load_display_queue_state()
    changed = _normalized_queue_items_signature(current_state.get("queue_items")) != _normalized_queue_items_signature(queue_items)
    if changed:
        save_display_queue_state({"queue_items": queue_items}, record_snapshot=False)
    service_entry["changed"] = changed
    return service_entry