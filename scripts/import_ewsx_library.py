"""Inspect/import EWSX presentation archives into the app's song library."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tab_library import ensure_library_schema, get_library_db_path


RTF_CONTROL_PATTERN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?")
RTF_HEX_PATTERN = re.compile(r"\\'([0-9a-fA-F]{2})")
RTF_DESTINATION_PATTERN = re.compile(r"\\(?:fonttbl|colortbl|stylesheet|info|generator|filetbl|pict|object|\*)")


def rtf_to_text(value: str | bytes | None) -> str:
    """Convert an EasyWorship RTF text resource into normalized plain text.

    What: Decodes source bytes, skips hidden destinations, translates Unicode and
    paragraph controls, removes remaining formatting, and normalizes whitespace.
    Why included: Song verses are stored as RTF resources in the EWSX database.
    Where used: extract_songs() for each text-bearing presentation element.
    Returns: Plain text with empty lines removed; None input becomes an empty string.
    """
    if value is None:
        return ""
    text = value.decode("cp1252", errors="replace") if isinstance(value, bytes) else str(value)
    text = RTF_HEX_PATTERN.sub(lambda match: bytes.fromhex(match.group(1)).decode("cp1252", errors="replace"), text)
    cleaned_characters: list[str] = []
    group_depth = 0
    skipped_group_depth: int | None = None
    index = 0
    # Scan by index so an entire nested destination group can be skipped without losing brace balance.
    while index < len(text):
        character = text[index]
        if character == "{":
            group_depth += 1
            destination_match = RTF_DESTINATION_PATTERN.match(text, index + 1)
            if destination_match and skipped_group_depth is None:
                skipped_group_depth = group_depth
            if skipped_group_depth is None:
                cleaned_characters.append(character)
            index += 1
            continue
        if character == "}":
            if skipped_group_depth == group_depth:
                skipped_group_depth = None
            if skipped_group_depth is None:
                cleaned_characters.append(character)
            group_depth = max(0, group_depth - 1)
            index += 1
            continue
        if skipped_group_depth is None:
            cleaned_characters.append(character)
        index += 1
    text = "".join(cleaned_characters)
    text = text.replace("\\par", "\n").replace("\\line", "\n").replace("\\tab", "\t")
    text = re.sub(r"\\u(-?\d+)\??", lambda match: chr(int(match.group(1)) if int(match.group(1)) >= 0 else int(match.group(1)) + 65536), text)
    text = RTF_CONTROL_PATTERN.sub("", text)
    text = text.replace("{", "").replace("}", "").replace("\\", "")
    text = re.sub(r"(?m)^d(?=[A-Z0-9])", "", text)
    text = re.sub(r"(?m)^d\s*$", "", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_main_database(ewsx_path: Path, temporary_root: Path) -> Path:
    """Extract the archive's main.db file into a caller-owned temporary directory.

    What: Finds the first ZIP member whose name ends with main.db and writes its
    bytes to the temporary root.
    Why included: EWSX is an archive; SQLite inspection requires an extracted file.
    Where used: main() before inspection or song extraction.
    Returns: Path to the extracted database; raises ValueError when absent.
    """
    with zipfile.ZipFile(ewsx_path) as archive:
        database_infos = [info for info in archive.infolist() if info.filename.casefold().endswith("main.db")]
        if not database_infos:
            raise ValueError("The EWSX archive does not contain main.db.")
        database_path = temporary_root / "main.db"
        with archive.open(database_infos[0]) as source:
            source._expected_crc = None
            database_path.write_bytes(source.read())
        return database_path


def inspect_database(database_path: Path) -> None:
    """Print SQLite schema samples and inspect presentation text resources.

    What: Lists tables, row counts, columns, sample text, and a bounded sample of
    presentation slides and their text-bearing elements.
    Why included: EWSX database layouts and resource content need inspection before
    relying on archive extraction rules.
    Where used: main() when the --inspect option is supplied.
    Returns: None; writes diagnostic findings to stdout.
    """
    with sqlite3.connect(database_path) as connection:
        table_names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        print(f"Tables: {table_names}")
        for table_name in table_names:
            columns = connection.execute(f"PRAGMA table_info([{table_name}])").fetchall()
            row_count = connection.execute(f"SELECT COUNT(*) FROM [{table_name}]").fetchone()[0]
            text_columns = [column[1] for column in columns if str(column[2]).casefold() in {"text", "varchar", "string"}]
            text_sample = []
            if text_columns and row_count:
                selected_columns = ", ".join(f"[{column}]" for column in text_columns)
                text_sample = connection.execute(f"SELECT {selected_columns} FROM [{table_name}] LIMIT 2").fetchall()
            print(f"TABLE {table_name}: rows={row_count}, columns={[column[1] for column in columns]}, text_sample={text_sample}")
        presentations = connection.execute(
            "SELECT rowid, title FROM presentation WHERE TRIM(COALESCE(title, '')) <> '' ORDER BY rowid LIMIT 5"
        ).fetchall()
        for presentation_id, title in presentations:
            slides = connection.execute(
                "SELECT rowid, title, order_index FROM slide WHERE presentation_id = ? ORDER BY order_index, rowid",
                (presentation_id,),
            ).fetchall()
            print(f"PRESENTATION {presentation_id}: {title!r}, slides={len(slides)}")
            for slide_id, slide_title, slide_order in slides[:3]:
                elements = connection.execute(
                    "SELECT rowid, order_index, foreground_resource_id, element_type FROM element WHERE slide_id = ? ORDER BY order_index, rowid",
                    (slide_id,),
                ).fetchall()
                print(f"  SLIDE {slide_id}: {slide_title!r}, order={slide_order}, elements={elements[:8]}")
                for element_id, element_order, resource_id, element_type in elements[:8]:
                    if resource_id is None:
                        continue
                    resource_row = connection.execute(
                        "SELECT rtf FROM resource_text WHERE resource_id = ? LIMIT 1",
                        (resource_id,),
                    ).fetchone()
                    if resource_row:
                        raw_text = resource_row[0]
                        print(f"    TEXT element={element_id} order={element_order} type={element_type} resource={resource_id} rtf_type={type(raw_text).__name__} rtf_prefix={str(raw_text)[:120]!r}")


def extract_songs(database_path: Path) -> list[dict[str, object]]:
    """Extract presentation titles and ordered text resources as song records.

    What: Reads non-placeholder presentations, visits slides and text elements in
    stored order, converts RTF resources, and assigns stable source keys.
    Why included: The importer maps EasyWorship presentation content into the app's
    title-and-verses song schema.
    Where used: main() after extracting the archive database.
    Returns: Song mappings containing source_key, title, and ordered verses.
    """
    songs: list[dict[str, object]] = []
    with sqlite3.connect(database_path) as connection:
        presentations = connection.execute(
            "SELECT rowid, presentation_uid, title FROM presentation WHERE TRIM(COALESCE(title, '')) <> '' ORDER BY rowid"
        ).fetchall()
        for presentation_id, presentation_uid, title in presentations:
            title_text = " ".join(str(title or "").split())
            if not title_text or title_text.casefold() in {"blank", "master"}:
                continue
            verse_parts: list[str] = []
            slides = connection.execute(
                "SELECT rowid FROM slide WHERE presentation_id = ? ORDER BY order_index, rowid",
                (presentation_id,),
            ).fetchall()
            for (slide_id,) in slides:
                elements = connection.execute(
                    "SELECT order_index, foreground_resource_id FROM element WHERE slide_id = ? AND element_type = 6 ORDER BY order_index, rowid",
                    (slide_id,),
                ).fetchall()
                for _, resource_id in elements:
                    if resource_id is None:
                        continue
                    resource_row = connection.execute(
                        "SELECT rtf FROM resource_text WHERE resource_id = ? LIMIT 1",
                        (resource_id,),
                    ).fetchone()
                    if not resource_row:
                        continue
                    plain_text = rtf_to_text(resource_row[0])
                    if plain_text:
                        verse_parts.append(plain_text)
            songs.append(
                {
                    "source_key": f"ewsx:{presentation_uid or presentation_id}",
                    "title": title_text,
                    "verses": verse_parts,
                }
            )
    return songs


def import_songs(songs: list[dict[str, object]], source_name: str) -> tuple[int, int]:
    """Insert or update valid extracted songs in the app's library database.

    What: Ensures the schema, skips records without a title or verses, updates a
    matching source key, and inserts new songs with timestamps and default metadata.
    Why included: Re-running an archive import should update existing songs instead
    of creating duplicate records.
    Where used: main() unless inspection or dry-run mode prevents database changes.
    Returns: Pair of inserted and updated counts; skipped count is printed.
    """
    ensure_library_schema()
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    inserted = 0
    updated = 0
    skipped = 0
    with sqlite3.connect(get_library_db_path()) as connection:
        for song in songs:
            title = str(song.get("title") or "").strip()
            source_key = str(song.get("source_key") or "").strip()
            verses = [str(verse).strip() for verse in list(song.get("verses") or []) if str(verse).strip()]
            if not title or not verses:
                skipped += 1
                continue
            existing = connection.execute("SELECT id FROM songs WHERE song_key = ? LIMIT 1", (source_key,)).fetchone()
            if existing:
                connection.execute(
                    "UPDATE songs SET title = ?, verses_json = ?, updated_at = ? WHERE id = ?",
                    (title, json.dumps(verses, ensure_ascii=False), timestamp, int(existing[0])),
                )
                updated += 1
                continue
            connection.execute(
                "INSERT INTO songs (title, song_key, verses_json, created_at, updated_at, background_path, font_settings_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, source_key, json.dumps(verses, ensure_ascii=False), timestamp, timestamp, "", "{}"),
            )
            inserted += 1
    print(f"Imported {inserted} songs from {source_name}; updated {updated}; skipped {skipped}.")
    return inserted, updated


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the EWSX inspection/import utility.

    What: Defines the required archive path and optional inspection and dry-run flags.
    Why included: Keeps command syntax and help output centralized for the script.
    Where used: main() before opening the requested archive.
    Returns: Parsed argparse Namespace.
    """
    parser = argparse.ArgumentParser(description="Inspect or import an EasyWorship EWSX music archive.")
    parser.add_argument("ewsx_path", type=Path)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Show import counts without changing the library database.")
    return parser.parse_args()


def main() -> int:
    """Run the archive workflow and scope extracted files to a temporary directory.

    What: Validates the archive path, extracts main.db, optionally inspects it,
    extracts songs, reports a sample, and imports unless inspect/dry-run mode applies.
    Why included: Provides the script's CLI orchestration while ensuring temporary
    database files are cleaned up after processing.
    Where used: Module entry point through SystemExit(main()).
    Returns: Process status code 0 on successful completion; errors propagate.
    """
    args = parse_args()
    if not args.ewsx_path.exists():
        raise FileNotFoundError(args.ewsx_path)
    with tempfile.TemporaryDirectory(prefix="ewsx_import_", ignore_cleanup_errors=True) as temporary_directory:
        database_path = extract_main_database(args.ewsx_path, Path(temporary_directory))
        if args.inspect:
            inspect_database(database_path)
        songs = extract_songs(database_path)
        print(f"Found {len(songs)} presentations with text; sample titles={[song['title'] for song in songs[:5]]}")
        if not args.inspect and not args.dry_run:
            import_songs(songs, args.ewsx_path.name)
        elif args.dry_run:
            print(f"Dry run: would import {len(songs)} presentations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
