"""Convert hymn text files, OpenLyrics XML, or library songs into a PewVista song pack.

Usage examples:
    python scripts/convert_to_song_pack.py C:/Hymns --output starter_hymns_08.txt
    python scripts/convert_to_song_pack.py song1.xml song2.txt --output christmas.txt
    python scripts/convert_to_song_pack.py --from-db "D:/Exports/library.db" \
        --titles "At the Cross" "When We All Get to Heaven" --output starter_hymns_09.txt

Plain .txt input (one song per file):
    Title: Amazing Grace          (optional; otherwise the file name is the title)
    Author: John Newton           (optional)
    Year: 1779                    (optional)

    1. Amazing grace, how sweet the sound ...   (stanzas separated by blank lines)

    Chorus:                       ("Chorus", "Refrain", or "[Chorus]" marks the chorus)
    ...
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tab_library import parse_song_pack

PACK_DIR = WORKSPACE_ROOT / "Data" / "song_packs"
CHORUS_LABEL = re.compile(r"^\s*\[?\s*(chorus|refrain)\s*\]?\s*:?\s*", re.IGNORECASE)
VERSE_LABEL = re.compile(r"^\s*(\[?\s*verse\s*\d*\s*\]?\s*:?|\d+[.)])\s*", re.IGNORECASE)
HEADER = re.compile(r"^\s*(title|author|year)\s*:\s*(.*)$", re.IGNORECASE)


def clean_field(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).replace("|", "/").strip()


def song_from_text(path: Path) -> dict:
    """Read one plain-text song file into title/author/year and labelled blocks."""
    meta = {"title": path.stem.replace("_", " "), "author": "", "year": ""}
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    while lines and (HEADER.match(lines[0]) or not lines[0].strip()):
        match = HEADER.match(lines.pop(0))
        if match and match.group(2).strip():
            meta[match.group(1).lower()] = match.group(2).strip()
    blocks: list[tuple[str, str]] = []
    for stanza in re.split(r"\n\s*\n", "\n".join(lines)):
        stanza_lines = [line.rstrip() for line in stanza.strip().splitlines()]
        if not stanza_lines:
            continue
        kind = "chorus" if CHORUS_LABEL.match(stanza_lines[0]) else "verse"
        label = CHORUS_LABEL if kind == "chorus" else VERSE_LABEL
        stanza_lines[0] = label.sub("", stanza_lines[0], count=1)
        content = "\n".join(line for line in stanza_lines if line.strip())
        if content:
            blocks.append((kind, content))
    return {**meta, "blocks": blocks}


def song_from_openlyrics(path: Path) -> dict:
    """Read an OpenLyrics XML file, keeping the verse order of the lyrics section."""
    root = ET.parse(path).getroot()
    namespace = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""

    def find_text(xpath: str) -> str:
        node = root.find(xpath.replace("ns:", namespace))
        return "".join(node.itertext()).strip() if node is not None else ""

    year = find_text("ns:properties/ns:copyright")
    year_match = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", year)
    blocks: list[tuple[str, str]] = []
    for verse in root.iter(f"{namespace}verse"):
        name = (verse.get("name") or "v").lower()
        lines_out: list[str] = []
        for lines_node in verse.iter(f"{namespace}lines"):
            parts: list[str] = [lines_node.text or ""]
            for child in lines_node:
                if child.tag == f"{namespace}br":
                    parts.append("\n")
                elif child.tag != f"{namespace}comment":
                    parts.append("".join(child.itertext()))
                parts.append(child.tail or "")
            lines_out.extend(line.strip() for line in "".join(parts).splitlines() if line.strip())
        if lines_out:
            blocks.append(("chorus" if name.startswith("c") else "verse", "\n".join(lines_out)))
    return {
        "title": find_text("ns:properties/ns:titles/ns:title") or path.stem,
        "author": find_text("ns:properties/ns:authors/ns:author"),
        "year": year_match.group(1) if year_match else "",
        "blocks": blocks,
    }


def songs_from_db(db_path: Path, titles: list[str]) -> list[dict]:
    """Copy songs by title (case-insensitive) from an existing PewVista library database."""
    wanted = {title.strip().casefold(): title for title in titles}
    found: list[dict] = []
    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(songs)")}
        select = ["title", "verses_json"] + [name for name in ("author", "year") if name in columns]
        for row in connection.execute(f"SELECT {', '.join(select)} FROM songs"):
            record = dict(zip(select, row))
            if str(record["title"]).strip().casefold() in wanted:
                verses = [str(verse).strip() for verse in json.loads(record["verses_json"] or "[]") if str(verse).strip()]
                # Library songs already have choruses expanded in order, so keep every slide as a verse.
                found.append({"title": record["title"], "author": record.get("author", ""), "year": record.get("year", ""),
                              "blocks": [("verse", verse) for verse in verses]})
    missing = set(wanted) - {str(song["title"]).strip().casefold() for song in found}
    for key in sorted(missing):
        print(f"Not found in database: {wanted[key]}", file=sys.stderr)
    return found


def to_pack_text(songs: list[dict]) -> str:
    out: list[str] = []
    for song in songs:
        out.append(f"# {clean_field(song['title'])} | {clean_field(song['author'])} | {clean_field(song['year'])}")
        for kind, content in song["blocks"]:
            out.append("[Chorus]" if kind == "chorus" else "[Verse]")
            out.append(content)
    return "\n".join(out) + "\n"


def collect_inputs(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in (".txt", ".xml")))
        elif path.is_file():
            files.append(path)
        else:
            print(f"Skipping missing path: {path}", file=sys.stderr)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", type=Path, help=".txt / OpenLyrics .xml files or folders")
    parser.add_argument("--from-db", type=Path, help="PewVista library.db to copy songs from")
    parser.add_argument("--titles", nargs="*", default=[], help="Song titles to copy with --from-db")
    parser.add_argument("--output", required=True, help="Pack file name (placed in Data/song_packs) or full path")
    parser.add_argument("--overwrite", action="store_true", help="Replace the output file if it exists")
    args = parser.parse_args(argv)

    songs: list[dict] = []
    for path in collect_inputs(args.inputs):
        try:
            song = song_from_openlyrics(path) if path.suffix.lower() == ".xml" else song_from_text(path)
        except (OSError, ET.ParseError) as error:
            print(f"Could not read {path}: {error}", file=sys.stderr)
            continue
        if song["blocks"]:
            songs.append(song)
        else:
            print(f"No lyrics found in {path}", file=sys.stderr)
    if args.from_db:
        songs.extend(songs_from_db(args.from_db, args.titles))
    if not songs:
        print("No songs to write.", file=sys.stderr)
        return 1

    output = Path(args.output)
    if not output.is_absolute() and output.parent == Path("."):
        output = PACK_DIR / output
    if output.suffix.lower() != ".txt":
        output = output.with_suffix(".txt")
    if output.exists() and not args.overwrite:
        print(f"{output} already exists; use --overwrite to replace it.", file=sys.stderr)
        return 1
    text = to_pack_text(songs)
    parsed = parse_song_pack(text)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(f"Wrote {len(parsed)} song(s) to {output}")
    for song in parsed:
        print(f"  {song['title']} ({len(song['verses'])} slides)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
