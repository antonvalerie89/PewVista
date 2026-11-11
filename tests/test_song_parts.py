"""Regression tests for song part types (Verse, Chorus, Bridge, ...) in the Library."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tab_library

PACK = """# Test Hymn | Writer | 1880
[Verse]
First verse
[Chorus]
The chorus
[Verse]
Second verse
"""


class SongPartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.database = self.base / "library.db"
        self.packs = self.base / "song_packs"
        self.packs.mkdir()
        for target, value in (("get_library_db_path", self.database), ("get_song_pack_directories", [self.packs])):
            patcher = patch.object(tab_library, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(tab_library, "_song_packs_imported", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_part_names_number_only_verses(self) -> None:
        self.assertEqual(tab_library.describe_song_parts(["Verse", "Chorus", "verse", "Bridge", "Chorus", "bogus"]),
                         ["Verse 1", "Chorus", "Verse 2", "Bridge", "Chorus", "Verse 3"])
        self.assertEqual(tab_library.parse_verse_types_json('["Chorus"]', 3), ["Chorus", "Verse", "Verse"])
        self.assertEqual(tab_library.parse_verse_types_json("not json", 2), ["Verse", "Verse"])

    def test_pack_marks_choruses(self) -> None:
        song = tab_library.parse_song_pack(PACK)[0]
        self.assertEqual(song["verses"], ["First verse", "The chorus", "Second verse", "The chorus"])
        self.assertEqual(song["verse_types"], ["Verse", "Chorus", "Verse", "Chorus"])

    def test_save_and_load_keep_part_types(self) -> None:
        song_id = tab_library.save_song_record(None, "New Song", "G", ["A", "B", "C"], "", verse_types=["Verse", "Chorus", "Bridge"])
        song = next(item for item in tab_library.load_all_songs() if item["id"] == song_id)
        self.assertEqual(song["verse_types"], ["Verse", "Chorus", "Bridge"])
        tab_library.save_song_record(song_id, "New Song", "G", ["A", "B"], "")
        song = next(item for item in tab_library.load_all_songs() if item["id"] == song_id)
        self.assertEqual(song["verse_types"], ["Verse", "Verse"])

    def test_existing_unedited_pack_songs_get_chorus_marks(self) -> None:
        tab_library.ensure_library_schema()
        verses = tab_library.parse_song_pack(PACK)[0]["verses"]
        connection = sqlite3.connect(self.database)
        try:
            for title, stored in (("Test Hymn", verses), ("Edited Hymn", ["changed"])):
                connection.execute("INSERT INTO songs (title, verses_json, created_at, updated_at) VALUES (?, ?, 'now', 'now')",
                                   (title, json.dumps(stored)))
                connection.execute("INSERT INTO imported_song_pack_songs VALUES (?, 'now')", ("song:" + title.casefold(),))
            connection.commit()
        finally:
            connection.close()
        (self.packs / "pack.txt").write_text(PACK + PACK.replace("Test Hymn", "Edited Hymn"), encoding="utf-8")
        songs = {song["title"]: song for song in tab_library.load_all_songs()}
        self.assertEqual(songs["Test Hymn"]["verse_types"], ["Verse", "Chorus", "Verse", "Chorus"])
        self.assertEqual(songs["Edited Hymn"]["verse_types"], ["Verse"])


if __name__ == "__main__":
    unittest.main()
