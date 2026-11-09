"""Tests for portable selected-database export and import."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3 as plain_sqlite
import secure_db as sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from data_transfer import (
    DATABASES,
    DataTransferError,
    export_database_archive,
    export_service_days_archive,
    import_database_archive,
    import_service_days_archive,
    importable_databases,
    importable_service_dates,
    migrate_legacy_backgrounds,
    _resolve_background_reference,
)
from display_bridge import delete_service_entry, list_service_entries_by_schedule, list_service_history_entries, load_service_entry_by_schedule, save_service_entry
from app_paths import resolve_background_file


def create_database(directory: Path, key: str, value: str) -> Path:
    """What: Create a minimal keyed test database in a temporary folder.

    Why included: Transfer tests need isolated source and destination records.
    Where used: Database ZIP export/import regression fixtures.
    Returns: Path to the created SQLite file.
    """
    filename, _label, table = DATABASES[key]
    path = directory / filename
    directory.mkdir(parents=True, exist_ok=True)
    # Plaintext like an installation from before encryption; PewVista migrates it on first use.
    connection = plain_sqlite.connect(path)
    try:
        connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(f"INSERT INTO {table} (value) VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()
    return path


def database_values(path: Path, table: str) -> list[str]:
    """What: Read fixture values from a test database and close it immediately.

    Why included: Assertions need simple content checks without leaking Windows file handles.
    Where used: Database merge, replace, and backup tests.
    Returns: Values ordered by local record ID.
    """
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute(f"SELECT value FROM {table} ORDER BY id")]
    finally:
        connection.close()


def create_service_database(path: Path, entries: list[tuple[int, str, str, str, str]]) -> None:
    """What: Create a minimal saved-services DB from schedule fixture tuples.

    Why included: Service-day tests need same/different date rows across installations.
    Where used: Service-day ZIP export and import tests.
    Returns: None; writes and closes the temporary DB.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("""CREATE TABLE service_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, saved_at TEXT NOT NULL, service_date TEXT NOT NULL,
            service_time TEXT NOT NULL, preacher_name TEXT NOT NULL, welcome_field_one TEXT NOT NULL,
            welcome_field_two TEXT NOT NULL, background_path TEXT NOT NULL DEFAULT '',
            welcome_settings TEXT NOT NULL DEFAULT '{}', display_tab_items TEXT NOT NULL DEFAULT '[]',
            presentation_settings TEXT NOT NULL DEFAULT '{}', display_settings TEXT NOT NULL DEFAULT '{}'
        )""")
        connection.execute("""CREATE TABLE service_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT, service_date TEXT NOT NULL DEFAULT '',
            service_time TEXT NOT NULL DEFAULT '', preacher_name TEXT NOT NULL DEFAULT ''
        )""")
        for row_id, saved_at, service_date, service_time, preacher in entries:
            connection.execute("INSERT INTO service_entries VALUES (?, ?, ?, ?, ?, '', '', '', '{}', '[]', '{}', '{}')", (row_id, saved_at, service_date, service_time, preacher))
        connection.commit()
    finally:
        connection.close()


TEST_PASSWORD = "transfer-test-password"


class DataTransferTests(unittest.TestCase):
    """What: Exercise database and service-day export, import, merge, and encryption.

    Why included: Transfers move church data between computers and must never lose or expose it.
    Where used: run_reliability_checks.bat release gate.
    Returns: None; each test asserts its own results.
    """
    def setUp(self) -> None:
        """What: Create isolated temporary source, destination, and ZIP paths.

        Why included: Every transfer test must avoid real app databases.
        Where used: unittest fixture lifecycle.
        Returns: None.
        """
        self.temporary = tempfile.TemporaryDirectory(prefix="LightDataTransferTest-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source install"
        self.destination = self.root / "different PC data"
        self.archive = self.root / "Church data.zip"
        # Backup copies extracted to temporary folders are read with the destination's key.
        self.key_scope = sqlite3.data_directory(self.destination)
        self.key_scope.__enter__()

    def tearDown(self) -> None:
        """What: Remove all temporary transfer test files.

        Why included: Test archives and SQLite DBs must not linger or affect other tests.
        Where used: unittest fixture lifecycle.
        Returns: None.
        """
        self.key_scope.__exit__(None, None, None)
        self.temporary.cleanup()

    def test_background_migration_preserves_media_and_rewrites_references(self) -> None:
        """What: Migrate legacy backgrounds without overwriting existing media and update saved references.

        Why included: Moving media to shared data must not break songs or services that reference it.
        Where used: Background migration regression suite.
        Returns: None; asserts copied files and rewritten references.
        """
        legacy = self.source / "Backgrounds" / "Images" / "shared.png"
        existing = self.destination / "Backgrounds" / "Images" / "shared.png"
        legacy.parent.mkdir(parents=True)
        existing.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy background")
        existing.write_bytes(b"different background")
        relative = str(legacy.relative_to(self.source))
        absolute = str(legacy.resolve())
        fixtures = (
            ("library.db", "CREATE TABLE songs (id INTEGER PRIMARY KEY, background_path TEXT)", "INSERT INTO songs VALUES (1, ?)", (relative,)),
            ("welcome_note.db", "CREATE TABLE welcome_notes (id INTEGER PRIMARY KEY, settings_json TEXT)", "INSERT INTO welcome_notes VALUES (1, ?)", (json.dumps({"background_path": absolute}),)),
            ("service.db", "CREATE TABLE service_entries (id INTEGER PRIMARY KEY, background_path TEXT, display_tab_items TEXT)", "INSERT INTO service_entries VALUES (1, ?, ?)", (absolute, json.dumps([{"background_path": relative}]))),
        )
        for name, schema, insert, values in fixtures:
            connection = sqlite3.connect(self.destination / name)
            try:
                connection.execute(schema)
                connection.execute(insert, values)
                connection.commit()
            finally:
                connection.close()
        settings = self.destination / "bible_settings.json"
        settings.write_text(json.dumps({"background_path": relative}), encoding="utf-8")
        self.assertEqual(migrate_legacy_backgrounds(self.source, self.destination), 1)
        migrated = next(existing.parent.glob("shared_*.png")).resolve()
        self.assertEqual(legacy.read_bytes(), migrated.read_bytes())
        self.assertEqual(existing.read_bytes(), b"different background")
        for name, query in (("library.db", "SELECT background_path FROM songs"), ("service.db", "SELECT background_path FROM service_entries")):
            connection = sqlite3.connect(self.destination / name)
            try:
                self.assertEqual(connection.execute(query).fetchone()[0], str(migrated))
            finally:
                connection.close()
        connection = sqlite3.connect(self.destination / "welcome_note.db")
        try:
            self.assertEqual(json.loads(connection.execute("SELECT settings_json FROM welcome_notes").fetchone()[0])["background_path"], str(migrated))
        finally:
            connection.close()
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["background_path"], str(migrated))
        backups = list((self.destination / ".background-migration-backups").glob("migration-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "library.db").is_file())
        with patch("data_transfer.copy_legacy_backgrounds", side_effect=AssertionError("Unexpected repeat copy")):
            self.assertEqual(migrate_legacy_backgrounds(self.source, self.destination), 1)
        self.assertEqual(len(list((self.destination / ".background-migration-backups").glob("migration-*"))), 1)
        legacy.unlink()
        self.assertEqual(resolve_background_file(absolute, self.source, self.destination), migrated)
        self.assertEqual(resolve_background_file(relative, self.source, self.destination), migrated)
        if os.name == "nt":
            self.assertEqual(resolve_background_file(absolute.upper(), self.source, self.destination), migrated)
        import main
        import tab_library
        import tab_welcome
        for module, resolver in ((main, main.resolve_background_path), (tab_library, tab_library.resolve_library_background_path), (tab_welcome, tab_welcome.resolve_welcome_background_path)):
            with patch.object(module, "get_app_base_path", return_value=self.source), patch.object(module, "get_app_data_path", return_value=self.destination):
                self.assertEqual(resolver(Path(absolute) if module is main else absolute), migrated)
        self.assertEqual(_resolve_background_reference(self.source, self.destination, relative), migrated)
        migrated.unlink()
        self.assertIsNone(resolve_background_file(relative, self.source, self.destination))
        self.assertIsNone(_resolve_background_reference(self.source, self.destination, relative))

    def test_export_contains_only_selected_databases_and_manifest(self) -> None:
        """What: Verify export ZIP scope, manifest metadata, and progress events.

        Why included: Exports must contain only the user-selected DBs and no source paths.
        Where used: Portable database export regression suite.
        Returns: None; asserts archive entries and manifest contents.
        """
        create_database(self.source, "library", "song from source")
        create_database(self.source, "members", "member from source")
        progress = []
        export_database_archive(self.archive, ["library"], self.source,
            progress=lambda stage, key, done, total: progress.append((stage, key, done, total)))
        self.assertEqual(importable_databases(self.archive), ("library",))
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "databases/library.db"})
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["application"], "The Light Ministries")
            self.assertNotIn(str(self.source), archive.read("manifest.json").decode("utf-8"))
        self.assertTrue(progress)

    def test_update_export_attaches_only_selected_background_references(self) -> None:
        """What: Export songs with two referenced backgrounds but select only one media file.

        Why included: Optional update contents must never include unselected backgrounds.
        Where used: Creator update database snapshot and cross-PC media rewrite checks.
        Returns: None; verifies exactly one attached image and its rewritten imported path.
        """
        from contextlib import closing

        self.source.mkdir(parents=True)
        self.destination.mkdir(parents=True)
        selected = self.source / "selected.jpg"
        excluded = self.source / "excluded.jpg"
        selected.write_bytes(b"chosen background")
        excluded.write_bytes(b"private background")
        with closing(sqlite3.connect(self.source / "library.db")) as connection:
            connection.execute("CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT NOT NULL, background_path TEXT NOT NULL)")
            connection.executemany("INSERT INTO songs(title, background_path) VALUES (?, ?)", [("selected", str(selected)), ("excluded", str(excluded))])
            connection.commit()
        export_database_archive(self.archive, ["library"], self.source, selected_media_paths=(selected,))
        with zipfile.ZipFile(self.archive) as archive:
            media = [name for name in archive.namelist() if name.startswith("media/")]
            self.assertEqual(len(media), 1)
            self.assertEqual(archive.read(media[0]), b"chosen background")
        import_database_archive(self.archive, ["library"], self.destination)
        with closing(sqlite3.connect(self.destination / "library.db")) as connection:
            paths = dict(connection.execute("SELECT title, background_path FROM songs"))
        self.assertEqual(Path(paths["selected"]).read_bytes(), b"chosen background")
        self.assertEqual(paths["excluded"], str(excluded))

    def test_cross_install_import_merges_missing_records_and_creates_backup(self) -> None:
        """What: Import selected records between isolated installation data folders.

        Why included: Cross-PC merge must add missing records, retain local records, and back up.
        Where used: Generic database import integration tests.
        Returns: None; verifies selection, non-selected DBs, and backup archive.
        """
        create_database(self.source, "library", "source song")
        create_database(self.source, "members", "source member")
        create_database(self.destination, "library", "destination song")
        create_database(self.destination, "members", "destination member")
        create_database(self.destination, "services", "destination service")
        export_database_archive(self.archive, ["library", "members"], self.source, members_password=TEST_PASSWORD)

        imported, backup = import_database_archive(self.archive, ["library"], self.destination)

        self.assertEqual(imported, ("library",))
        self.assertEqual(database_values(self.destination / "library.db", "songs"), ["destination song", "source song"])
        self.assertEqual(database_values(self.destination / "members.db", "members"), ["destination member"])
        self.assertEqual(database_values(self.destination / "service.db", "service_entries"), ["destination service"])
        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        self.assertTrue(backup.parent.name == ".import-backups")
        with zipfile.ZipFile(backup) as archive:
            self.assertEqual(database_values_from_archive(archive, "databases/library.db", "songs"), ["destination song"])
        import_database_archive(self.archive, ["library"], self.destination)
        self.assertEqual(database_values(self.destination / "library.db", "songs"), ["destination song", "source song"])

    def test_member_id_collisions_are_remapped_with_extra_fields(self) -> None:
        """What: Merge members whose source IDs collide with destination IDs.

        Why included: Extra member fields must follow the mapped destination member.
        Where used: Members database cross-install merge regression.
        Returns: None; verifies local IDs and foreign-key relationships.
        """
        source_path = self.source / "members.db"
        destination_path = self.destination / "members.db"
        for path in (source_path, destination_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        source = plain_sqlite.connect(source_path)
        try:
            source.execute("CREATE TABLE members (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, surname TEXT NOT NULL, date_of_birth TEXT NOT NULL)")
            source.execute("CREATE TABLE member_extra_fields (id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER NOT NULL, field_name TEXT NOT NULL, field_value TEXT NOT NULL, FOREIGN KEY(member_id) REFERENCES members(id))")
            source.execute("INSERT INTO members VALUES (1,'New','Member','2000-01-01')")
            source.execute("INSERT INTO member_extra_fields(member_id,field_name,field_value) VALUES (1,'Ministry','Choir')")
            source.commit()
        finally:
            source.close()
        destination = plain_sqlite.connect(destination_path)
        try:
            destination.execute("CREATE TABLE members (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, surname TEXT NOT NULL, date_of_birth TEXT NOT NULL)")
            destination.execute("CREATE TABLE member_extra_fields (id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER NOT NULL, field_name TEXT NOT NULL, field_value TEXT NOT NULL, FOREIGN KEY(member_id) REFERENCES members(id))")
            destination.execute("INSERT INTO members VALUES (1,'Existing','Member','1990-01-01')")
            destination.execute("INSERT INTO member_extra_fields(member_id,field_name,field_value) VALUES (1,'Ministry','Elders')")
            destination.commit()
        finally:
            destination.close()

        export_database_archive(self.archive, ["members"], self.source, members_password=TEST_PASSWORD)
        import_database_archive(self.archive, ["members"], self.destination, members_password=TEST_PASSWORD)

        destination = sqlite3.connect(destination_path)
        try:
            members = destination.execute("SELECT id,name,surname,date_of_birth FROM members ORDER BY id").fetchall()
            extra_fields = destination.execute("SELECT member_id,field_name,field_value FROM member_extra_fields ORDER BY id").fetchall()
        finally:
            destination.close()
        self.assertEqual(members, [(1, "Existing", "Member", "1990-01-01"), (2, "New", "Member", "2000-01-01")])
        self.assertEqual(extra_fields, [(1, "Ministry", "Elders"), (2, "Ministry", "Choir")])


    def test_member_details_are_encrypted_at_rest_and_in_exports(self) -> None:
        """What: Keep member details unreadable on disk, in exports, and with a wrong password.

        Why included: Member contact details and birth dates are personal information.
        Where used: Member-data encryption security regression.
        Returns: None; asserts encryption, password protection, and a successful round trip.
        """
        import sqlite3 as plain_sqlite

        self.source.mkdir(parents=True)
        legacy = plain_sqlite.connect(self.source / "members.db")
        legacy.execute("CREATE TABLE members (id INTEGER PRIMARY KEY, name TEXT, surname TEXT, date_of_birth TEXT)")
        legacy.execute("INSERT INTO members (name, surname, date_of_birth) VALUES ('Confidential', 'Person', '1980-05-05')")
        legacy.commit()
        legacy.close()

        with self.assertRaisesRegex(DataTransferError, "password"):
            export_database_archive(self.archive, ["members"], self.source)
        export_database_archive(self.archive, ["members"], self.source, members_password=TEST_PASSWORD)
        self.assertNotIn(b"Confidential", (self.source / "members.db").read_bytes())
        with zipfile.ZipFile(self.archive) as archive:
            self.assertNotIn(b"Confidential", archive.read("databases/members.db"))

        with self.assertRaisesRegex(DataTransferError, "unlocked"):
            import_database_archive(self.archive, ["members"], self.destination, members_password="wrong-password")
        import_database_archive(self.archive, ["members"], self.destination, members_password=TEST_PASSWORD)
        self.assertNotIn(b"Confidential", (self.destination / "members.db").read_bytes())
        imported = sqlite3.connect(self.destination / "members.db")
        try:
            self.assertEqual(imported.execute("SELECT name FROM members").fetchall(), [("Confidential",)])
        finally:
            imported.close()

    def test_import_can_merge_and_replace_different_databases(self) -> None:
        """What: Apply Merge to one selected DB and Replace to another in one transfer.

        Why included: Per-database import choices must remain independent and backed up.
        Where used: Database import mode regression suite.
        Returns: None; checks final data and pre-import backup contents.
        """
        create_database(self.source, "library", "incoming song")
        create_database(self.source, "members", "incoming member")
        create_database(self.destination, "library", "existing song")
        create_database(self.destination, "members", "existing member")
        export_database_archive(self.archive, ["library", "members"], self.source, members_password=TEST_PASSWORD)

        imported, backup = import_database_archive(
            self.archive,
            ["library", "members"],
            self.destination,
            modes={"library": "merge", "members": "replace"},
            members_password=TEST_PASSWORD,
        )

        self.assertEqual(imported, ("library", "members"))
        self.assertEqual(database_values(self.destination / "library.db", "songs"), ["existing song", "incoming song"])
        self.assertEqual(database_values(self.destination / "members.db", "members"), ["incoming member"])
        self.assertIsNotNone(backup)
        with zipfile.ZipFile(backup) as archive:
            self.assertEqual(database_values_from_archive(archive, "databases/library.db", "songs"), ["existing song"])
            self.assertEqual(database_values_from_archive(archive, "databases/members.db", "members"), ["existing member"])

    def test_same_song_title_with_different_verses_is_added_as_new_row(self) -> None:
        """What: Merge a song sharing a title/key but containing different verses.

        Why included: Song titles are not unique; distinct verse payloads must survive import.
        Where used: Cross-install Library merge regression.
        Returns: None; verifies both records remain and exact duplicates are skipped.
        """
        source_path = self.source / "library.db"
        destination_path = self.destination / "library.db"
        for path in (source_path, destination_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        schema = """CREATE TABLE songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, song_key TEXT NOT NULL,
            verses_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            background_path TEXT NOT NULL DEFAULT '', font_settings_json TEXT NOT NULL DEFAULT '{}'
        )"""
        source = sqlite3.connect(source_path)
        try:
            source.execute(schema)
            source.execute("INSERT INTO songs VALUES (1,'Same Title','C','[\"Source verse\"]','2025-01-01','2025-01-01','','{}')")
            source.execute("INSERT INTO songs VALUES (2,'Same Lyrics','G','[\"Line one\", \"Line two\"]','2024-01-01','2024-01-01','','{}')")
            source.commit()
        finally:
            source.close()
        destination = sqlite3.connect(destination_path)
        try:
            destination.execute(schema)
            destination.execute("INSERT INTO songs VALUES (1,'Same Title','C','[\"Destination verse\"]','2024-01-01','2024-01-01','','{}')")
            destination.execute("INSERT INTO songs VALUES (2,'Same Lyrics','G','[\"Line one\",\"Line two\"]','2024-01-01','2024-01-01','','{}')")
            destination.commit()
        finally:
            destination.close()

        export_database_archive(self.archive, ["library"], self.source)
        import_database_archive(self.archive, ["library"], self.destination)

        destination = sqlite3.connect(destination_path)
        try:
            rows = destination.execute("SELECT title, verses_json FROM songs ORDER BY id").fetchall()
        finally:
            destination.close()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], ("Same Title", '["Destination verse"]'))
        self.assertEqual(rows[1], ("Same Lyrics", '["Line one\",\"Line two"]'))
        self.assertEqual(rows[2], ("Same Title", '["Source verse"]'))

    def test_service_merge_keeps_different_rows_for_same_date_and_time(self) -> None:
        """What: Merge service snapshots that share a schedule slot but differ in content.

        Why included: Distinct saved service versions must not be collapsed by date/time.
        Where used: Cross-install Services database merge regression.
        Returns: None; verifies both rows and service-info selections remain.
        """
        source_path = self.source / "service.db"
        destination_path = self.destination / "service.db"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        service_entries_schema = """CREATE TABLE service_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, saved_at TEXT NOT NULL, service_date TEXT NOT NULL,
            service_time TEXT NOT NULL, preacher_name TEXT NOT NULL, welcome_field_one TEXT NOT NULL,
            welcome_field_two TEXT NOT NULL, background_path TEXT NOT NULL DEFAULT '',
            welcome_settings TEXT NOT NULL DEFAULT '{}', display_tab_items TEXT NOT NULL DEFAULT '[]',
            presentation_settings TEXT NOT NULL DEFAULT '{}', display_settings TEXT NOT NULL DEFAULT '{}'
        )"""
        source = sqlite3.connect(source_path)
        try:
            source.execute(service_entries_schema)
            source.execute("CREATE TABLE service_info (id INTEGER PRIMARY KEY AUTOINCREMENT, service_date TEXT NOT NULL DEFAULT '', service_time TEXT NOT NULL DEFAULT '', preacher_name TEXT NOT NULL DEFAULT '')")
            source.execute("INSERT INTO service_entries VALUES (1,'2025-01-01T10:00:00','2025-06-01','Morning','Source Preacher','','','','{}','[]','{}','{}')")
            source.execute("INSERT INTO service_info VALUES (1,'2025-06-01','Morning','Source Preacher')")
            source.commit()
        finally:
            source.close()
        destination = sqlite3.connect(destination_path)
        try:
            destination.execute(service_entries_schema)
            destination.execute("CREATE TABLE service_info (id INTEGER PRIMARY KEY CHECK (id = 1), service_date TEXT NOT NULL DEFAULT '', service_time TEXT NOT NULL DEFAULT '', preacher_name TEXT NOT NULL DEFAULT '')")
            destination.execute("INSERT INTO service_entries VALUES (1,'2024-01-01T10:00:00','2025-06-01','Morning','Destination Preacher','','','','{}','[]','{}','{}')")
            destination.execute("INSERT INTO service_info VALUES (1,'2025-05-01','Night','Destination Preacher')")
            destination.commit()
        finally:
            destination.close()

        export_database_archive(self.archive, ["services"], self.source)
        import_database_archive(self.archive, ["services"], self.destination)

        from unittest.mock import patch
        from display_bridge import cleanup_duplicate_service_entries
        with patch("display_bridge.SERVICE_DB_RELATIVE_PATH", destination_path):
            self.assertEqual(cleanup_duplicate_service_entries(), 0)
        connection = sqlite3.connect(destination_path)
        try:
            rows = connection.execute("SELECT preacher_name FROM service_entries ORDER BY id").fetchall()
            service_info_count = connection.execute("SELECT COUNT(*) FROM service_info").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(rows, [("Destination Preacher",), ("Source Preacher",)])
        self.assertEqual(service_info_count, 2)

    def test_same_schedule_lists_versions_and_loads_selected_record(self) -> None:
        """What: List multiple saved services for one date/time and load one selected ID.

        Why included: Schedule recall must let the operator choose the correct version.
        Where used: Service-date selection regression.
        Returns: None; verifies selector metadata and selected preacher are exact.
        """
        service_path = self.source / "service.db"
        service_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(service_path)
        try:
            connection.execute("""CREATE TABLE service_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, saved_at TEXT NOT NULL, service_date TEXT NOT NULL,
                service_time TEXT NOT NULL, preacher_name TEXT NOT NULL, welcome_field_one TEXT NOT NULL,
                welcome_field_two TEXT NOT NULL, background_path TEXT NOT NULL DEFAULT '',
                welcome_settings TEXT NOT NULL DEFAULT '{}', display_tab_items TEXT NOT NULL DEFAULT '[]',
                presentation_settings TEXT NOT NULL DEFAULT '{}', display_settings TEXT NOT NULL DEFAULT '{}'
            )""")
            connection.execute("INSERT INTO service_entries VALUES (1,'2025-01-01T09:00:00','2025-06-01','Morning','First Preacher','','','','{}','[]','{}','{}')")
            connection.execute("INSERT INTO service_entries VALUES (2,'2025-01-02T09:00:00','2025-06-01','Morning','Second Preacher','','','','{}','[]','{}','{}')")
            connection.commit()
        finally:
            connection.close()
        from unittest.mock import patch
        with patch("display_bridge.SERVICE_DB_RELATIVE_PATH", service_path):
            entries = list_service_entries_by_schedule("2025-06-01", "Morning")
            selected = load_service_entry_by_schedule("2025-06-01", "Morning", entries[0]["id"])
        self.assertEqual(len(entries), 2)
        self.assertIn("Second Preacher", entries[0]["label"])
        self.assertEqual(selected["preacher_name"], "Second Preacher")

    def test_save_rewrites_selected_service_and_history_deletes_only_selected_id(self) -> None:
        """What: Save over a selected service version and delete one selected History row.

        Why included: Saving/deleting one version must leave same-slot siblings intact.
        Where used: Service save and History deletion regression.
        Returns: None; verifies row identity, rewrite, and targeted deletion.
        """
        service_path = self.source / "service.db"
        service_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(service_path)
        try:
            connection.execute("""CREATE TABLE service_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, saved_at TEXT NOT NULL, service_date TEXT NOT NULL,
                service_time TEXT NOT NULL, preacher_name TEXT NOT NULL, welcome_field_one TEXT NOT NULL,
                welcome_field_two TEXT NOT NULL, background_path TEXT NOT NULL DEFAULT '',
                welcome_settings TEXT NOT NULL DEFAULT '{}', display_tab_items TEXT NOT NULL DEFAULT '[]',
                presentation_settings TEXT NOT NULL DEFAULT '{}', display_settings TEXT NOT NULL DEFAULT '{}'
            )""")
            connection.execute("INSERT INTO service_entries VALUES (1,'2025-01-01T09:00:00','2025-06-01','Morning','First Preacher','','','','{}','[]','{}','{}')")
            connection.execute("INSERT INTO service_entries VALUES (2,'2025-01-02T09:00:00','2025-06-01','Morning','Second Preacher','','','','{}','[]','{}','{}')")
            connection.commit()
        finally:
            connection.close()
        from unittest.mock import patch
        with patch("display_bridge.SERVICE_DB_RELATIVE_PATH", service_path):
            saved = save_service_entry("2025-06-01", "Morning", "Rewritten First", [], service_entry_id="1")
            connection = sqlite3.connect(service_path)
            try:
                rows = connection.execute("SELECT id,preacher_name FROM service_entries ORDER BY id").fetchall()
            finally:
                connection.close()
            self.assertEqual(saved["id"], "1")
            self.assertEqual(rows, [(1, "Rewritten First"), (2, "Second Preacher")])
            self.assertTrue(delete_service_entry("2"))
            self.assertFalse(delete_service_entry("2"))
            remaining = list_service_history_entries()
        self.assertEqual([entry["id"] for entry in remaining], ["1"])

    def test_service_day_transfer_only_affects_selected_dates(self) -> None:
        """What: Export/import selected service dates with referenced songs and backgrounds.

        Why included: Cross-PC day transfers must preserve dependencies without touching other dates.
        Where used: Service-day transfer integration regression.
        Returns: None; checks ZIP contents, media placement, song ID mapping, and date scoping.
        """
        source_path = self.source / "service.db"
        source_library_path = self.source / "library.db"
        destination_path = self.destination / "service.db"
        create_service_database(source_path, [
            (1, "2025-01-03T10:00:00", "2025-06-01", "Morning", "Source distinct"),
            (2, "2025-01-02T10:00:00", "2025-06-01", "Morning", "Source duplicate"),
            (3, "2025-01-01T10:00:00", "2025-06-02", "Night", "Other day"),
        ])
        create_service_database(destination_path, [
            (1, "2024-01-03T10:00:00", "2025-06-01", "Morning", "Destination version"),
            (2, "2024-01-01T10:00:00", "2025-07-01", "Night", "Keep this other day"),
        ])
        source_background_root = self.source / "Backgrounds"
        source_background_root.mkdir()
        service_background = source_background_root / "day-service-bg-test.jpg"
        song_background = source_background_root / "day-song-bg-test.jpg"
        service_background.write_bytes(b"service background image")
        song_background.write_bytes(b"song background image")
        song_connection = sqlite3.connect(source_library_path)
        try:
            song_connection.execute("""CREATE TABLE songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, song_key TEXT NOT NULL DEFAULT '',
                verses_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                background_path TEXT NOT NULL DEFAULT '', font_settings_json TEXT NOT NULL DEFAULT '{}'
            )""")
            song_connection.execute("INSERT INTO songs VALUES (7,'Cross-PC Song','C','[\"Verse one\",\"Verse two\"]','2025-01-01','2025-01-01','Backgrounds/day-song-bg-test.jpg','{}')")
            queue_item = {
                "id": "library-7-0-20250101000000",
                "source": "library",
                "source_item_id": "7",
                "title": "Cross-PC Song",
                "subtitle": "Verse 1 [C]",
                "text": "Verse one",
                "verses": ["Verse one", "Verse two"],
                "background_path": "Backgrounds/day-song-bg-test.jpg",
            }
            song_connection.commit()
        finally:
            song_connection.close()
        service_connection = sqlite3.connect(source_path)
        try:
            service_connection.execute(
                "UPDATE service_entries SET background_path=?, display_tab_items=? WHERE id=1",
                ("Backgrounds/day-service-bg-test.jpg", json.dumps([queue_item])),
            )
            service_connection.execute(
                "UPDATE service_entries SET display_tab_items=? WHERE id=2",
                (json.dumps([{"source": "welcome", "background_path": "Backgrounds/day-service-bg-test.jpg"}]),),
            )
            service_connection.commit()
        finally:
            service_connection.close()
        export_service_days_archive(self.archive, ["2025-06-01"], self.source)
        self.assertEqual(importable_service_dates(self.archive), ("2025-06-01",))
        with zipfile.ZipFile(self.archive) as archive:
            filtered_database = self.root / "selected days only.db"
            filtered_database.write_bytes(archive.read("databases/service.db"))
            self.assertIn("databases/library.db", archive.namelist())
            self.assertEqual(len([name for name in archive.namelist() if name.startswith("media/")]), 2)
        connection = sqlite3.connect(filtered_database)
        try:
            exported_days = {row[0] for row in connection.execute("SELECT DISTINCT service_date FROM service_entries")}
        finally:
            connection.close()
        self.assertEqual(exported_days, {"2025-06-01"})

        _, backup = import_service_days_archive(self.archive, ["2025-06-01"], "merge", self.destination)
        self.assertIsNotNone(backup)
        connection = sqlite3.connect(destination_path)
        try:
            merged = connection.execute("SELECT service_date,preacher_name FROM service_entries ORDER BY id").fetchall()
        finally:
            connection.close()
        self.assertEqual(merged, [
            ("2025-06-01", "Destination version"),
            ("2025-07-01", "Keep this other day"),
            ("2025-06-01", "Source distinct"),
            ("2025-06-01", "Source duplicate"),
        ])

        import_service_days_archive(self.archive, ["2025-06-01"], "replace", self.destination)
        connection = sqlite3.connect(destination_path)
        try:
            replaced = connection.execute("SELECT service_date,preacher_name FROM service_entries ORDER BY id").fetchall()
        finally:
            connection.close()
        self.assertEqual(replaced, [
            ("2025-07-01", "Keep this other day"),
            ("2025-06-01", "Source distinct"),
            ("2025-06-01", "Source duplicate"),
        ])
        self.assertEqual((self.destination / "Backgrounds" / service_background.name).read_bytes(), service_background.read_bytes())
        self.assertEqual((self.destination / "Backgrounds" / song_background.name).read_bytes(), song_background.read_bytes())
        library_connection = sqlite3.connect(self.destination / "library.db")
        try:
            imported_song = library_connection.execute("SELECT id,background_path FROM songs WHERE title='Cross-PC Song'").fetchone()
        finally:
            library_connection.close()
        self.assertIsNotNone(imported_song)
        self.assertTrue(Path(imported_song[1]).is_file())
        service_connection = sqlite3.connect(destination_path)
        try:
            imported_queue = json.loads(service_connection.execute("SELECT display_tab_items FROM service_entries WHERE preacher_name='Source distinct'").fetchone()[0])
            imported_service_background = service_connection.execute("SELECT background_path FROM service_entries WHERE preacher_name='Source distinct'").fetchone()[0]
        finally:
            service_connection.close()
        self.assertEqual(imported_queue[0]["source_item_id"], str(imported_song[0]))
        self.assertTrue(Path(imported_queue[0]["background_path"]).is_file())
        self.assertTrue(Path(imported_service_background).is_file())

    def test_invalid_zip_does_not_change_destination(self) -> None:
        """What: Reject an invalid transfer ZIP before modifying destination data.

        Why included: Untrusted archives must not alter the user's current database.
        Where used: Transfer ZIP validation regression.
        Returns: None; asserts the destination row remains unchanged.
        """
        create_database(self.destination, "library", "keep this song")
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"format": "wrong", "format_version": 1}))
            archive.writestr("../library.db", b"bad")
        with self.assertRaises(DataTransferError):
            import_database_archive(self.archive, ["library"], self.destination)
        self.assertEqual(database_values(self.destination / "library.db", "songs"), ["keep this song"])

    def test_import_rejects_unknown_database_selection(self) -> None:
        """What: Reject keys that are not supported application databases.

        Why included: Import/export selections must be restricted to known DB files.
        Where used: Transfer API input-validation regression.
        Returns: None; asserts a clear DataTransferError.
        """
        with self.assertRaises(DataTransferError):
            export_database_archive(self.archive, ["not_a_database"], self.source)


def database_values_from_archive(archive: zipfile.ZipFile, entry: str, table: str) -> list[str]:
    """What: Read ordered values from a database member inside a backup ZIP.

    Why included: Backup tests verify the prior destination contents were captured.
    Where used: Pre-import backup assertions.
    Returns: Values read from the extracted temporary SQLite snapshot.
    """
    with tempfile.TemporaryDirectory(prefix="LightDataBackupCheck-") as temporary:
        database = Path(temporary) / Path(entry).name
        database.write_bytes(archive.read(entry))
        return database_values(database, table)


if __name__ == "__main__":
    unittest.main()
