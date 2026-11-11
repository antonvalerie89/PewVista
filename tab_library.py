"""Edit song/verse records and preview or queue selected library content."""

from __future__ import annotations

import functools
import json
import queue
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox
from tkinter import ttk

from display_bridge import load_display_queue_state, upsert_display_queue_item
from background_picker import open_background_picker
from overlay_layout import SECONDARY_LAYOUT_SIZE_BOOST, choose_text_colors, fit_background_image, fit_overlay_layout, get_display_key_size, get_display_outline_width, get_display_text_color, is_display_key_hidden, normalize_font_value, render_overlay_image, scale_overlay_font_value
from app_paths import get_app_base_path, get_app_data_path, resolve_background_file
from music_licensing import MUSIC_LICENSE_CHOICES, PUBLIC_DOMAIN_LABEL, format_music_license_attribution, normalize_license_provider
from perf_logging import log_exception
from video_loop import LoopingVideoReader

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence, ImageTk
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageOps = None
    ImageSequence = None
    ImageTk = None

try:
    import imageio.v2 as imageio
except ImportError:
    imageio = None


VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpg", ".mpeg"}
PREVIEW_MAX_FPS = 12.0
LIBRARY_PREVIEW_MAX_TEXT_HEIGHT_RATIO = 0.08
LIBRARY_PREVIEW_MAX_TEXT_MULTIPLIER = 2
LIBRARY_PREVIEW_STROKE_WIDTH = 1
LIBRARY_TAB_COLUMN_WEIGHTS = (4, 4, 3)


def resolve_preview_font_path(font_family: str, font_style: str) -> Path | None:
    """Resolve a supported Windows font family/style to its installed font file.

    What: Looks up the font filename for the requested family and style.
    Why included: Pillow preview fitting needs the installed face's pixel metrics.
    Where used: get_preview_pil_font() before loading a cached font.
    Returns: Existing font Path, or None when unsupported or unavailable.
    """
    windows_fonts = Path("C:/Windows/Fonts")
    font_map = {
        "segoe ui": {"normal": "segoeui.ttf", "bold": "segoeuib.ttf", "italic": "segoeuii.ttf", "bold italic": "segoeuiz.ttf"},
        "arial": {"normal": "arial.ttf", "bold": "arialbd.ttf", "italic": "ariali.ttf", "bold italic": "arialbi.ttf"},
        "calibri": {"normal": "calibri.ttf", "bold": "calibrib.ttf", "italic": "calibrii.ttf", "bold italic": "calibriz.ttf"},
        "cambria": {"normal": "cambria.ttc", "bold": "cambriab.ttf", "italic": "cambriai.ttf", "bold italic": "cambriaz.ttf"},
        "tahoma": {"normal": "tahoma.ttf", "bold": "tahomabd.ttf", "italic": "tahoma.ttf", "bold italic": "tahomabd.ttf"},
    }
    family_fonts = font_map.get(str(font_family).casefold())
    if family_fonts is None:
        return None
    file_name = family_fonts.get(str(font_style).casefold(), family_fonts.get("normal"))
    if not file_name:
        return None
    candidate = windows_fonts / file_name
    return candidate if candidate.exists() else None


@functools.lru_cache(maxsize=128)
def _load_library_preview_pil_font_cached(font_path_str: str | None, size: int):
    """What: Load a TrueType Library preview font or Pillow's default fallback.

    Why included: Preview fitting requests the same faces repeatedly while users edit text.
    Where used: get_preview_pil_font() through the bounded LRU cache.
    Returns: Pillow font object.
    """
    if font_path_str is None:
        try:
            return ImageFont.load_default(size)
        except TypeError:
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(font_path_str, size)
    except OSError:
        return ImageFont.load_default()


def get_preview_pil_font(font_value):
    """Load the cached Pillow font matching a Library preview font tuple.

    What: Validates Pillow/input, normalizes family/size/style, resolves a font file,
    and obtains its cached handle.
    Why included: Preview measurements must use repeatable font metrics.
    Where used: Verse preview text fitting and overlay rendering.
    Returns: Pillow font, or None when Pillow/input is unavailable.
    """
    if ImageFont is None or not isinstance(font_value, tuple) or len(font_value) < 2:
        return None
    family = str(font_value[0])
    size = max(10, abs(int(font_value[1])))
    style = str(font_value[2]) if len(font_value) > 2 else "normal"
    font_path = resolve_preview_font_path(family, style)
    return _load_library_preview_pil_font_cached(str(font_path) if font_path else None, size)


def get_library_preview_max_text_size(preview_height: int, base_size: int) -> int:
    """Calculate the maximum Library preview font size from panel/base limits.

    What: Applies both the preview-height ratio cap and the configured-size multiplier cap.
    Why included: Preview text must remain readable without exceeding its fixed panel.
    Where used: fit_preview_text() and fit_preview_text_for_render().
    Returns: Maximum font candidate size in pixels.
    """
    scaled_height_cap = round(max(1, preview_height) * LIBRARY_PREVIEW_MAX_TEXT_HEIGHT_RATIO)
    multiplied_base_cap = max(18, int(base_size) * LIBRARY_PREVIEW_MAX_TEXT_MULTIPLIER)
    return max(18, min(scaled_height_cap, multiplied_base_cap))


_LIBRARY_MEASURE_DRAW = None


def get_shared_library_measure_draw():
    """Return a lazily created one-pixel Pillow draw surface for text metrics.

    What: Caches an ImageDraw object backed by a transparent 1x1 image.
    Why included: Repeated word-width measurements should not allocate new images.
    Where used: wrap_preview_text_to_width() and preview fitting loops.
    Returns: Shared ImageDraw instance, or None when Pillow is unavailable.
    """
    global _LIBRARY_MEASURE_DRAW
    if _LIBRARY_MEASURE_DRAW is None and Image is not None and ImageDraw is not None:
        _LIBRARY_MEASURE_DRAW = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
    return _LIBRARY_MEASURE_DRAW


def wrap_preview_text_to_width(text: str, pil_font, max_width: int, stroke_width: int) -> str:
    """Wrap Library preview text to measured width, including its outline.

    What: Adds words while measured line width fits and preserves blank input lines.
    Why included: Verse preview must not clip text at the fixed panel boundary.
    Where used: fit_preview_text() and fit_preview_text_for_render().
    Returns: Wrapped text; returns input unchanged when Pillow metrics are unavailable.
    """
    if Image is None or ImageDraw is None or pil_font is None or max_width <= 0:
        return text
    measure_draw = get_shared_library_measure_draw()
    if measure_draw is None:
        return text

    def measure_width(value: str) -> float:
        """What: Measure one candidate preview line in pixels including the stroke.

        Why included: Word wrapping must account for the visible outline as well as glyphs.
        Where used: wrap_preview_text_to_width() while assembling each line.
        Returns: Pixel width, or zero for empty text.
        """
        if not value:
            return 0.0
        box = measure_draw.textbbox((0, 0), value, font=pil_font, stroke_width=stroke_width)
        return float(box[2] - box[0])

    wrapped_lines: list[str] = []
    for raw_line in text.splitlines() or [text]:
        if not raw_line.strip():
            wrapped_lines.append("")
            continue
        words = raw_line.split()
        current_line = ""
        for word in words:
            candidate = word if not current_line else f"{current_line} {word}"
            if current_line and measure_width(candidate) > max_width:
                wrapped_lines.append(current_line)
                current_line = word
            else:
                current_line = candidate
        if current_line:
            wrapped_lines.append(current_line)
    return "\n".join(wrapped_lines)


def get_library_db_path() -> Path:
    """Resolve the application-local SQLite database path for Library songs.

    What: Places library.db under the app Data directory.
    Why included: Song records must persist consistently in source and packaged runs.
    Where used: connect_library_db() before schema/read/write operations.
    Returns: Absolute Path to the song database.
    """
    return get_app_data_path() / "library.db"


def connect_library_db() -> sqlite3.Connection:
    """Open the Library database with a bounded wait and concurrency-friendly pragmas.

    What: Creates the parent directory, opens SQLite with a 15-second timeout, and
    attempts WAL/NORMAL synchronous mode.
    Why included: UI and worker actions may touch the database concurrently.
    Where used: Schema, song listing, save, and delete functions.
    Returns: Open sqlite3.Connection owned by the caller.
    """
    db_path = get_library_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=15.0)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        pass
    return connection


def ensure_library_schema() -> None:
    """Ensure the songs table and title lookup index exist.

    What: Creates missing schema objects without dropping or rewriting existing rows.
    Why included: Clean installs and app upgrades need a compatible song store.
    Where used: Song list, save, and delete persistence entry points.
    Returns: None; commits schema creation.
    """
    with connect_library_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                song_key TEXT NOT NULL DEFAULT '',
                verses_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                background_path TEXT NOT NULL DEFAULT '',
                font_settings_json TEXT NOT NULL DEFAULT '{}',
                license_provider TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                year TEXT NOT NULL DEFAULT '',
                license_info TEXT NOT NULL DEFAULT '',
                verse_types_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        try:
            connection.execute("CREATE INDEX IF NOT EXISTS idx_songs_title ON songs (title)")
        except Exception:
            pass
        song_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(songs)")}
        if "license_provider" not in song_columns:
            connection.execute("ALTER TABLE songs ADD COLUMN license_provider TEXT NOT NULL DEFAULT ''")
        if "author" not in song_columns:
            connection.execute("ALTER TABLE songs ADD COLUMN author TEXT NOT NULL DEFAULT ''")
        if "year" not in song_columns:
            connection.execute("ALTER TABLE songs ADD COLUMN year TEXT NOT NULL DEFAULT ''")
        if "license_info" not in song_columns:
            connection.execute("ALTER TABLE songs ADD COLUMN license_info TEXT NOT NULL DEFAULT ''")
        if "verse_types_json" not in song_columns:
            connection.execute("ALTER TABLE songs ADD COLUMN verse_types_json TEXT NOT NULL DEFAULT '[]'")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS imported_song_pack_songs (song_ref TEXT PRIMARY KEY, imported_at TEXT NOT NULL)"
        )
        connection.commit()


_song_packs_imported = False


def get_song_pack_directories() -> list[Path]:
    """Return folders that may contain bundled song packs.

    What: Data/song_packs is copied by Setup only, and Update ZIPs never include Data.
    Why included: Starter songs ship in the full installer but not in update packages.
    Where used: import_song_packs().
    Returns: Candidate directories (they may not exist).
    """
    return [get_app_data_path() / "song_packs"]


def parse_song_pack(text: str) -> list[dict[str, object]]:
    """Parse the plain-text song pack format.

    What: "# Title | Author | Year" starts a song; "[Chorus]" and "[Verse]" start
    blocks. The chorus is repeated after every verse, as songs are projected in order.
    Why included: Plain text keeps bundled hymns easy to proofread and extend.
    Where used: import_song_packs() and tests.
    Returns: Song dictionaries with title, author, year, and verses.
    """
    songs: list[dict[str, object]] = []
    current: dict | None = None
    block: list[str] | None = None

    def close_block() -> None:
        if current is not None and block:
            content = "\n".join(block).strip()
            if content:
                current["blocks"].append((current.pop("_kind"), content))

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("# "):
            close_block()
            block = None
            fields = [part.strip() for part in line[2:].split("|")] + ["", ""]
            current = {"title": fields[0], "author": fields[1], "year": fields[2], "blocks": []}
            songs.append(current)
        elif current is not None and line.strip().lower() in ("[chorus]", "[verse]"):
            close_block()
            block = []
            current["_kind"] = line.strip().lower()
        elif block is not None:
            block.append(line)
    close_block()
    parsed: list[dict[str, object]] = []
    for song in songs:
        chorus = next((content for kind, content in song["blocks"] if kind == "[chorus]"), "")
        verses: list[str] = []
        verse_types: list[str] = []
        for kind, content in song["blocks"]:
            if kind == "[verse]":
                verses.append(content)
                verse_types.append("Verse")
                if chorus:
                    verses.append(chorus)
                    verse_types.append("Chorus")
        if not verses and chorus:
            verses.append(chorus)
            verse_types.append("Chorus")
        if song["title"] and verses:
            parsed.append({"title": song["title"], "author": song["author"], "year": song["year"], "verses": verses, "verse_types": verse_types})
    return parsed


def import_song_packs(directories: list[Path] | None = None) -> int:
    """Add bundled pack songs that were never imported into this library.

    What: Records every pack song reference once, skipping titles already present,
    so deleted or edited songs are never re-added or overwritten.
    Why included: New installs receive the starter hymns and updates can add songs.
    Where used: load_all_songs() once per process.
    Returns: Number of songs inserted.
    """
    ensure_library_schema()
    inserted = 0
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    for directory in directories if directories is not None else get_song_pack_directories():
        if not directory.is_dir():
            continue
        for pack_path in sorted(directory.glob("*.txt")):
            try:
                songs = parse_song_pack(pack_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            with connect_library_db() as connection:
                done = {row[0] for row in connection.execute("SELECT song_ref FROM imported_song_pack_songs")}
                titles = {str(row[0]).strip().casefold() for row in connection.execute("SELECT title FROM songs")}
                for song in songs:
                    song_ref = "song:" + str(song["title"]).strip().casefold()
                    if song_ref in done:
                        # Hymns imported before part types existed: mark choruses if still unedited.
                        connection.execute(
                            "UPDATE songs SET verse_types_json = ? WHERE LOWER(title) = LOWER(?) AND verse_types_json = '[]' AND verses_json = ?",
                            (json.dumps(song.get("verse_types") or [], ensure_ascii=False), song["title"], json.dumps(song["verses"], ensure_ascii=False)),
                        )
                        continue
                    if str(song["title"]).strip().casefold() not in titles:
                        connection.execute(
                            """
                            INSERT INTO songs (title, song_key, verses_json, created_at, updated_at, background_path, font_settings_json, license_provider, author, year, license_info, verse_types_json)
                            VALUES (?, '', ?, ?, ?, '', '{}', '', ?, ?, '', ?)
                            """,
                            (song["title"], json.dumps(song["verses"], ensure_ascii=False), timestamp, timestamp, song["author"], song["year"],
                             json.dumps(song.get("verse_types") or [], ensure_ascii=False)),
                        )
                        titles.add(str(song["title"]).strip().casefold())
                        inserted += 1
                    connection.execute("INSERT INTO imported_song_pack_songs (song_ref, imported_at) VALUES (?, ?)", (song_ref, timestamp))
                    done.add(song_ref)
                connection.commit()
    return inserted


def load_all_songs() -> list[dict[str, object]]:
    """Load all songs ordered by title and decode each record's verse list.

    What: Ensures schema, fetches song fields, and converts SQLite rows into dictionaries.
    Why included: List filtering and the editor consume a stable Python data shape.
    Where used: refresh_song_list() in the Library tab.
    Returns: Song mappings sorted by case-insensitive title and ID.
    """
    global _song_packs_imported
    ensure_library_schema()
    if not _song_packs_imported:
        _song_packs_imported = True
        try:
            import_song_packs()
        except Exception as error:
            log_exception("tab_library.import_song_packs", error)
    with connect_library_db() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, title, song_key, verses_json, created_at, updated_at, background_path, font_settings_json, license_provider, author, year, license_info, verse_types_json FROM songs ORDER BY LOWER(title), id"
        ).fetchall()
    songs: list[dict[str, object]] = []
    for row in rows:
        verses = parse_verses_json(row["verses_json"])
        songs.append(
            {
                "id": row["id"],
                "title": row["title"],
                "song_key": row["song_key"],
                "verses": verses,
                "verse_types": parse_verse_types_json(row["verse_types_json"], len(verses)),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "background_path": row["background_path"],
                "font_settings_json": row["font_settings_json"],
                "license_provider": normalize_license_provider(row["license_provider"]),
                "author": str(row["author"] or "").strip(),
                "year": str(row["year"] or "").strip(),
                "license_info": str(row["license_info"] or "").strip(),
            }
        )
    return songs


def parse_verses_json(payload: str | None) -> list[str]:
    """Decode a saved verse array and discard invalid or empty entries.

    What: Parses JSON and normalizes list elements to trimmed strings.
    Why included: Legacy database content may be blank, malformed, or not a list.
    Where used: load_all_songs() when rebuilding song mappings.
    Returns: Ordered nonempty verse strings, or an empty list on invalid data.
    """
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


SONG_PART_TYPES = ("Verse", "Chorus", "Pre-Chorus", "Bridge", "Refrain", "Intro", "Outro", "Tag", "Ending")


def normalize_part_type(value: object) -> str:
    """What: Map any saved or typed song-part name to one of SONG_PART_TYPES.

    Why included: Old songs and imports may hold blank or unknown part names.
    Where used: Loading, saving, and the editor's part dropdown.
    Returns: A known part type; "Verse" when unknown.
    """
    text = str(value or "").strip().casefold()
    return next((part for part in SONG_PART_TYPES if part.casefold() == text), "Verse")


def parse_verse_types_json(payload: str | None, verse_count: int) -> list[str]:
    """What: Decode saved part types and align them with the song's verses.

    Why included: Songs saved before part types existed have none; they default to Verse.
    Where used: load_all_songs().
    Returns: Exactly verse_count part types.
    """
    try:
        parsed = json.loads(payload or "[]")
    except json.JSONDecodeError:
        parsed = []
    parts = [normalize_part_type(item) for item in parsed] if isinstance(parsed, list) else []
    return (parts + ["Verse"] * verse_count)[:verse_count]


def describe_song_parts(part_types: list[str]) -> list[str]:
    """What: Build slide names such as "Verse 1", "Chorus", "Verse 2", "Bridge".

    Why included: The Display queue and live navigation show what each slide is.
    Where used: Send to Display and Show Live.
    Returns: One display name per part; only verses are numbered.
    """
    names: list[str] = []
    verse_number = 0
    for part in part_types:
        part = normalize_part_type(part)
        if part == "Verse":
            verse_number += 1
            names.append(f"Verse {verse_number}")
        else:
            names.append(part)
    return names


def save_song_record(song_id: int | None, title: str, song_key: str, verses: list[str], background_path: str, license_provider: str = "", author: str = "", year: str = "", license_info: str = "", verse_types: list[str] | None = None) -> int:
    """Insert a new song or update an existing song record.

    What: Serializes verses, their part types, timestamps, license provider and information, author, and year.
    Why included: Editor saves need one transactional persistence boundary.
    Where used: save_current_song() after validating the title.
    Returns: Persistent database ID for inserted or updated record.
    """
    ensure_library_schema()
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    verses_json = json.dumps(verses, ensure_ascii=False)
    verse_types_json = json.dumps([normalize_part_type(part) for part in (verse_types or [])][:len(verses)], ensure_ascii=False)
    normalized_provider = normalize_license_provider(license_provider)
    normalized_author = str(author or "").strip()
    normalized_year = str(year or "").strip()
    normalized_info = str(license_info or "").strip() if normalized_provider == "Individual" else ""
    with connect_library_db() as connection:
        if song_id is None:
            cursor = connection.execute(
                """
                INSERT INTO songs (title, song_key, verses_json, created_at, updated_at, background_path, font_settings_json, license_provider, author, year, license_info, verse_types_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (title, song_key, verses_json, timestamp, timestamp, background_path, "{}", normalized_provider, normalized_author, normalized_year, normalized_info, verse_types_json),
            )
            connection.commit()
            return int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE songs
            SET title = ?, song_key = ?, verses_json = ?, updated_at = ?, background_path = ?, license_provider = ?, author = ?, year = ?, license_info = ?, verse_types_json = ?
            WHERE id = ?
            """,
            (title, song_key, verses_json, timestamp, background_path, normalized_provider, normalized_author, normalized_year, normalized_info, verse_types_json, song_id),
        )
        connection.commit()
        return int(song_id)


def delete_song_record(song_id: int) -> None:
    """Delete one song row by ID after the UI has obtained user confirmation.

    What: Ensures schema and commits a parameterized DELETE statement.
    Why included: Keeps destructive database behavior separate from Tk dialogs.
    Where used: delete_current_song() after confirmation.
    Returns: None; commits the deletion.
    """
    ensure_library_schema()
    with connect_library_db() as connection:
        connection.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        connection.commit()


def resolve_library_background_path(background_path: str | None) -> Path | None:
    """Resolve a saved absolute or application-relative background path.

    What: Checks an absolute path first, then resolves relative to the app root.
    Why included: Song records may retain media references across source/packaged runs.
    Where used: Preview background loading and media-picker display.
    Returns: Existing file Path, or None for blank/missing media.
    """
    return resolve_background_file(background_path, get_app_base_path(), get_app_data_path())


def get_library_background_root() -> Path:
    """Resolve the app's Backgrounds folder used by the Library picker.

    What: Joins the app root with its Backgrounds directory.
    Why included: The picker should offer bundled/user media from one known location.
    Where used: list_library_background_choices().
    Returns: Background root Path; it may not exist yet.
    """
    return get_app_base_path() / "Backgrounds"


def list_library_background_choices() -> list[str]:
    """List background files beneath the Library background root.

    What: Recursively gathers files and converts paths relative to the app root.
    Why included: Relative references remain portable when the app folder moves.
    Where used: choose_background() to populate the shared picker.
    Returns: Case-insensitively sorted path strings.
    """
    backgrounds_roots = (get_library_background_root(), get_app_data_path() / "Backgrounds")
    background_paths: set[str] = set()
    app_root = get_app_base_path()
    data_root = get_app_data_path()
    for backgrounds_root in backgrounds_roots:
        if not backgrounds_root.exists():
            continue
        for candidate in backgrounds_root.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                relative_path = candidate.relative_to(app_root)
            except ValueError:
                try:
                    relative_path = candidate.relative_to(data_root)
                except ValueError:
                    relative_path = candidate
            background_paths.add(relative_path.as_posix())
    return sorted(background_paths, key=str.casefold)


def build_library_tab(tab_frame: tk.Frame, context: dict[str, object]) -> None:
    """Build the song editor, searchable SQLite list, and media/verse preview.

    What: Creates song CRUD controls, editable verse rows, background selection, async media
    preview, and a callback that sends the selected verse to Display.
    Why included: Library content, metadata, and selected verse state need one coordinated UI.
    Where used: main.create_tabs_content() for the Library notebook tab.
    Returns: None; registers app styling widgets and persists songs through SQLite helpers.
    """
    tab_widgets = context["tab_widgets"]
    tab_labels = context["tab_labels"]
    tab_inputs = context["tab_inputs"]
    build_preview_placeholder = context["build_preview_placeholder"]
    apply_layout_style = context["apply_layout_style"]
    get_display_bounds = context.get("get_display_bounds")
    get_preview_size = context.get("get_preview_size")
    get_music_licenses = context.get("get_music_licenses")
    start_background_task = context.get("start_background_task")
    notebook = context.get("notebook")

    state: dict[str, object] = {
        "songs": [],
        "filtered_song_ids": [],
        "selected_song_id": None,
        "verse_rows": [],
        "selected_verse_index": None,
        "preview_photo": None,
        "preview_after_id": None,
        "preview_reader": None,
        "preview_media_path": None,
        "preview_media_generation": 0,
        "preview_text": "",
        "preview_fill_color": None,
        "preview_stroke_color": None,
        "preview_overlay_cache_key": None,
        "preview_overlay_image": None,
        "preview_render_request_id": 0,
        "preview_render_queue": None,
        "preview_background_target": "",
        "preview_text_render_after_id": None,
        "fitted_background_cache": {},
    }

    def is_library_tab_active() -> bool:
        """What: Check whether this Library frame is the selected notebook page.

        Why included: Preview media and render workers should pause when the tab is hidden.
        Where used: Preview GIF/video timers and asynchronous render polling.
        Returns: True when active; defaults to True if there is no notebook or lookup fails.
        """
        if not isinstance(notebook, ttk.Notebook):
            return True
        try:
            selected_tab = notebook.nametowidget(notebook.select())
        except tk.TclError:
            return True
        return selected_tab == tab_frame

    search_text_var = tk.StringVar()
    title_var = tk.StringVar()
    key_var = tk.StringVar()
    license_provider_var = tk.StringVar(value=PUBLIC_DOMAIN_LABEL)
    author_var = tk.StringVar()
    year_var = tk.StringVar()
    license_info_var = tk.StringVar()
    background_var = tk.StringVar()
    background_display_var = tk.StringVar(value="None")

    for column_index, column_weight in enumerate(LIBRARY_TAB_COLUMN_WEIGHTS):
        tab_frame.columnconfigure(column_index, weight=column_weight)
    tab_frame.rowconfigure(0, weight=0)
    tab_frame.rowconfigure(1, weight=1)

    display_actions = tk.Frame(tab_frame)
    display_actions.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 0))
    editor_actions = tk.Frame(tab_frame)
    editor_actions.grid(row=0, column=1, sticky="ew", padx=8, pady=(8, 0))
    tab_widgets.extend([display_actions, editor_actions])
    apply_layout_style([display_actions, editor_actions])

    for column_index in range(3):
        column_frame = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
        column_frame.grid(row=1, column=column_index, sticky="nsew", padx=8, pady=12)
        column_frame.columnconfigure(0, weight=1)
        if column_index == 0:
            column_frame.rowconfigure(2, weight=1)
            title = tk.Label(column_frame, text="Library Songs", justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            search_entry = tk.Entry(column_frame, textvariable=search_text_var)
            search_entry.grid(row=1, column=0, sticky="ew", pady=(0, 8))
            song_list = tk.Listbox(column_frame, height=14)
            song_list.grid(row=2, column=0, sticky="nsew")
            show_live_button = tk.Button(display_actions, text="Show Live", state="disabled")
            show_live_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
            add_button = tk.Button(display_actions, text="Send to Display", state="disabled")
            add_button.grid(row=0, column=0, sticky="w")
            tab_widgets.extend([column_frame, title, search_entry, song_list, add_button, show_live_button])
            tab_labels.append(title)
            tab_inputs.extend([search_entry, song_list, add_button, show_live_button])
            apply_layout_style([column_frame, search_entry, song_list, add_button, show_live_button], label_widgets=[title], input_widgets=[search_entry, song_list, add_button, show_live_button])
        elif column_index == 1:
            column_frame.rowconfigure(1, weight=1)
            title = tk.Label(column_frame, text="Song Editor", justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            form = tk.Frame(column_frame)
            form.grid(row=1, column=0, sticky="nsew")
            form.columnconfigure(1, weight=1)
            form.columnconfigure(3, weight=0)
            title_label = tk.Label(form, text="Title")
            title_label.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
            title_entry = tk.Entry(form)
            title_entry.grid(row=0, column=1, sticky="ew", pady=4)
            key_label = tk.Label(form, text="Key")
            key_label.grid(row=0, column=2, sticky="e", padx=(12, 8), pady=4)
            key_entry = tk.Entry(form)
            key_entry.grid(row=0, column=3, sticky="ew", pady=4)
            license_label = tk.Label(form, text="Music License")
            license_label.grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
            license_picker = ttk.Combobox(
                form,
                textvariable=license_provider_var,
                values=MUSIC_LICENSE_CHOICES,
                state="readonly",
                width=18,
            )
            license_picker.grid(row=1, column=1, sticky="ew", pady=4)
            author_label = tk.Label(form, text="Author")
            author_label.grid(row=1, column=2, sticky="e", padx=(12, 8), pady=4)
            author_entry = tk.Entry(form, textvariable=author_var)
            author_entry.grid(row=1, column=3, sticky="ew", pady=4)
            year_label = tk.Label(form, text="Year")
            year_label.grid(row=1, column=4, sticky="e", padx=(12, 8), pady=4)
            year_entry = tk.Entry(form, textvariable=year_var, width=6)
            year_entry.grid(row=1, column=5, sticky="w", pady=4)
            license_info_label = tk.Label(form, text="License info")
            license_info_label.grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
            license_info_entry = tk.Entry(form, textvariable=license_info_var)
            license_info_entry.grid(row=2, column=1, columnspan=5, sticky="ew", pady=4)
            background_label = tk.Label(form, text="Background")
            background_label.grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
            background_value_label = tk.Label(form, textvariable=background_display_var, anchor="w", justify="left", relief="solid", bd=1, padx=6, pady=4)
            background_value_label.grid(row=3, column=1, columnspan=5, sticky="ew", pady=4)
            background_button = tk.Button(display_actions, text="Background")
            background_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
            verses_label = tk.Label(form, text="Verses")
            verses_label.grid(row=4, column=0, sticky="nw", padx=(0, 8), pady=(8, 4))
            verses_area_frame = tk.Frame(form, bd=1, relief="solid", padx=6, pady=6)
            verses_area_frame.grid(row=4, column=1, columnspan=5, sticky="nsew", pady=(8, 0))
            verses_area_frame.columnconfigure(0, weight=1)
            verses_area_frame.rowconfigure(0, weight=1)
            verses_host = tk.Frame(verses_area_frame)
            verses_host.grid(row=0, column=0, sticky="nsew")
            verses_host.columnconfigure(0, weight=1)
            verses_host.rowconfigure(0, weight=1)
            verses_canvas = tk.Canvas(verses_host, highlightthickness=0, borderwidth=0)
            verses_canvas.grid(row=0, column=0, sticky="nsew")
            verses_scrollbar = tk.Scrollbar(verses_host, orient="vertical", command=verses_canvas.yview)
            verses_scrollbar.grid(row=0, column=1, sticky="ns")
            verses_canvas.configure(yscrollcommand=verses_scrollbar.set)
            verses_container = tk.Frame(verses_canvas)
            verses_container.columnconfigure(0, weight=1)
            verses_canvas_window = verses_canvas.create_window((0, 0), window=verses_container, anchor="nw")
            form.rowconfigure(4, weight=1)
            new_button = tk.Button(editor_actions, text="New Song")
            add_verse_button = tk.Button(editor_actions, text="Add Verse")
            save_button = tk.Button(editor_actions, text="Save")
            clear_button = tk.Button(editor_actions, text="Clear")
            delete_button = tk.Button(editor_actions, text="Delete")
            editor_buttons = [new_button, add_verse_button, save_button, clear_button, delete_button]
            for button_index, button in enumerate(editor_buttons):
                button.grid(row=0, column=button_index, sticky="w", padx=(0, 4))
            tab_widgets.extend([column_frame, title, form, title_label, title_entry, key_label, key_entry, license_label, license_picker, author_label, author_entry, year_label, year_entry, license_info_label, license_info_entry, background_label, background_value_label, background_button, verses_label, verses_area_frame, verses_host, verses_canvas, verses_scrollbar, verses_container, *editor_buttons])
            tab_labels.extend([title, title_label, key_label, license_label, author_label, year_label, license_info_label, background_label, verses_label])
            tab_inputs.extend([title_entry, key_entry, license_picker, author_entry, year_entry, license_info_entry, background_button, *editor_buttons])
            apply_layout_style([column_frame, form, title_entry, key_entry, license_picker, author_entry, year_entry, license_info_entry, background_value_label, background_button, verses_area_frame, verses_host, verses_canvas, verses_container, *editor_buttons], label_widgets=[title, title_label, key_label, license_label, author_label, background_label, verses_label], input_widgets=[title_entry, key_entry, license_picker, author_entry, background_button, *editor_buttons])
        else:
            column_frame.rowconfigure(2, weight=1)
            title = tk.Label(column_frame, text="Preview / Details", justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            preview_container, preview_label = build_preview_placeholder(column_frame, "Library Preview")
            preview_container.grid(row=1, column=0, sticky="n", pady=(0, 8))
            details = tk.Label(column_frame, text="No song selected", justify="left", anchor="nw")
            details.grid(row=2, column=0, sticky="nsew")
            tab_widgets.extend([column_frame, title, preview_container, preview_label, details])
            tab_labels.extend([title, preview_label, details])
            apply_layout_style([column_frame, preview_container], label_widgets=[title, preview_label, details])

    title_entry.configure(textvariable=title_var)
    key_entry.configure(textvariable=key_var)

    def refresh_verses_scroll_region(_event=None) -> None:
        """What: Recompute the verse canvas scrollregion after its rows resize.

        Why included: Added/removed verses change the scrollable editor height.
        Where used: Verses-container Configure event binding.
        Returns: None; updates the canvas region in place.
        """
        verses_canvas.configure(scrollregion=verses_canvas.bbox("all"))

    def sync_verses_canvas_width(_event=None) -> None:
        """What: Keep the embedded verse-row frame aligned to the canvas viewport width.

        Why included: Text rows should use available width without horizontal drift.
        Where used: Verses-canvas Configure event binding.
        Returns: None; updates the embedded canvas-window width.
        """
        verses_canvas.itemconfigure(verses_canvas_window, width=verses_canvas.winfo_width())

    def scroll_verses_by_mousewheel(event: tk.Event) -> str:
        """What: Translate platform mouse-wheel input to vertical verse-canvas scrolling.

        Why included: Windows delta and Linux button events encode direction differently.
        Where used: Verse editor host, canvas, row, and control bindings.
        Returns: "break" after scrolling or consuming an unsupported event.
        """
        if getattr(event, "delta", 0):
            direction = -1 if event.delta > 0 else 1
        elif getattr(event, "num", None) == 4:
            direction = -1
        elif getattr(event, "num", None) == 5:
            direction = 1
        else:
            return "break"
        verses_canvas.yview_scroll(direction, "units")
        return "break"

    def scroll_text_widget_by_mousewheel(event: tk.Event, text_widget: tk.Text) -> str:
        """What: Scroll one verse Text widget using normalized platform wheel direction.

        Why included: Text widgets own their yview and should scroll independently of the outer canvas.
        Where used: bind_text_verse_scroll() on individual verse editors.
        Returns: "break" after handling the wheel event.
        """
        if getattr(event, "delta", 0):
            direction = -1 if event.delta > 0 else 1
        elif getattr(event, "num", None) == 4:
            direction = -1
        elif getattr(event, "num", None) == 5:
            direction = 1
        else:
            return "break"
        text_widget.yview_scroll(direction, "units")
        return "break"

    def bind_outer_verse_scroll(widget: tk.Widget) -> None:
        """What: Bind platform wheel events on one widget to the outer verse canvas.

        Why included: Nested row widgets should remain scrollable when hovered.
        Where used: Verse editor container and row controls.
        Returns: None; adds three cross-platform bindings.
        """
        widget.bind("<MouseWheel>", scroll_verses_by_mousewheel)
        widget.bind("<Button-4>", scroll_verses_by_mousewheel)
        widget.bind("<Button-5>", scroll_verses_by_mousewheel)

    def bind_text_verse_scroll(text_widget: tk.Text) -> None:
        """What: Bind wheel events on a verse Text widget to its own scroll view.

        Why included: Long verse text should scroll inside its editor, not move the row list.
        Where used: add_verse_row() for each verse editor.
        Returns: None; registers Windows and Linux wheel handlers.
        """
        text_widget.bind("<MouseWheel>", lambda event, current=text_widget: scroll_text_widget_by_mousewheel(event, current))
        text_widget.bind("<Button-4>", lambda event, current=text_widget: scroll_text_widget_by_mousewheel(event, current))
        text_widget.bind("<Button-5>", lambda event, current=text_widget: scroll_text_widget_by_mousewheel(event, current))

    verses_container.bind("<Configure>", refresh_verses_scroll_region)
    verses_canvas.bind("<Configure>", sync_verses_canvas_width)
    bind_outer_verse_scroll(verses_host)
    bind_outer_verse_scroll(verses_canvas)
    bind_outer_verse_scroll(verses_container)

    def get_song_by_id(song_id: int | None) -> dict[str, object] | None:
        """What: Find one cached song mapping by persistent database ID.

        Why included: The UI tracks stable IDs across filtered list redraws.
        Where used: Editor selection, preview, save/delete, and queue actions.
        Returns: Matching song mapping, or None when ID is absent/not cached.
        """
        if song_id is None:
            return None
        for song in state["songs"]:
            if song["id"] == song_id:
                return song
        return None

    def resolve_preview_size() -> tuple[int, int]:
        """What: Resolve the active preview dimensions from the app callback or widget.

        Why included: Rendering and text fitting must follow monitor aspect and current panel size.
        Where used: Preview layout and Pillow composition.
        Returns: Width/height tuple clamped to preview minimums.
        """
        if callable(get_preview_size):
            try:
                preview_size = get_preview_size()
            except Exception:
                preview_size = None
            if isinstance(preview_size, tuple) and len(preview_size) >= 2:
                return max(180, int(preview_size[0])), max(100, int(preview_size[1]))
        preview_container.update_idletasks()
        return max(180, int(preview_container.winfo_width() or 320)), max(100, int(preview_container.winfo_height() or 180))

    def refresh_preview_text_layout() -> None:
        """What: Apply current preview dimensions and fallback-text wrapping to widgets.

        Why included: The preview container can change size when the window or display changes.
        Where used: refresh_preview_background() before rendering media.
        Returns: None; updates container and label geometry.
        """
        preview_width, preview_height = resolve_preview_size()
        preview_container.configure(width=preview_width, height=preview_height)
        preview_label.configure(wraplength=max(120, preview_width - 24), padx=12, pady=12)

    def invalidate_preview_overlay_cache(reset_colors: bool = False) -> None:
        """What: Clear cached overlay pixels/keys and optionally cached contrast colors.

        Why included: Changed verse, font, background, or display settings invalidate prior pixels.
        Where used: Selection, background, and renderer-setting updates.
        Returns: None; forces fresh fitting and overlay composition on next render.
        """
        state["preview_overlay_cache_key"] = None
        state["preview_overlay_image"] = None
        state["cached_text_overlay_key"] = None
        state["cached_text_overlay_image"] = None
        if reset_colors:
            state["preview_fill_color"] = None
            state["preview_stroke_color"] = None

    def get_preview_font_value() -> tuple[str, int, str]:
        """What: Read the current Tk preview font as a normalized Pillow-style tuple.

        Why included: Pillow measurement/rendering needs family, size, and style separate from Tk.
        Where used: Preview fitting and queue-item font serialization.
        Returns: Family, integer size, and normal/bold/italic style string.
        """
        named_font = tkfont.Font(font=preview_label.cget("font"))
        weight = str(named_font.cget("weight") or "normal")
        slant = str(named_font.cget("slant") or "roman")
        style_parts: list[str] = []
        if weight == "bold":
            style_parts.append("bold")
        if slant == "italic":
            style_parts.append("italic")
        style_name = " ".join(style_parts) if style_parts else "normal"
        return (str(named_font.cget("family") or "Segoe UI"), int(named_font.cget("size") or 12), style_name)

    def get_preview_corner_text() -> str:
        """What: Build the selected song's key and license corner attribution.

        Why included: Song key and provider number must accompany the live lyrics.
        Where used: Library preview composition and direct Show Live action.
        Returns: Newline-separated key and license label, omitting unavailable values.
        """
        song = get_song_by_id(state.get("selected_song_id"))
        song_key = str(song.get("song_key") or "").strip() if isinstance(song, dict) else ""
        if is_display_key_hidden():
            song_key = ""
        provider = normalize_license_provider(license_provider_var.get())
        settings = get_music_licenses() if callable(get_music_licenses) else {}
        license_number = str(settings.get(provider.casefold()) or "").strip() if isinstance(settings, dict) and provider else ""
        license_label = format_music_license_attribution(provider, license_number, author_var.get(), license_info_var.get())
        return "\n".join(value for value in (song_key, license_label) if value)

    def get_current_background_path() -> str:
        """What: Resolve and cache the background chosen in the editor or saved song.

        Why included: Preview and sent queue items must use the same media reference.
        Where used: Contrast selection and preview frame composition.
        Returns: Existing resolved path string, or empty when no background is set.
        """
        cached = str(state.get("background_path") or "").strip()
        if cached:
            return cached
        raw = background_var.get().strip()
        if not raw:
            song = get_song_by_id(state.get("selected_song_id"))
            if isinstance(song, dict):
                raw = str(song.get("background_path") or "").strip()
        resolved = resolve_library_background_path(raw)
        path_str = str(resolved) if resolved else ""
        if path_str:
            state["background_path"] = path_str
        return path_str

    def get_center_region_luminance(base_image) -> float:
        """What: Average grayscale brightness behind the center-aligned preview text.

        Why included: Automatic text colors should contrast with the main overlay region.
        Where used: choose_preview_text_colors() when selecting preview fill/stroke.
        Returns: Mean luminance from zero (black) through 255 (white).
        """
        width, height = base_image.size
        left = max(0, int(width * 0.2))
        top = max(0, int(height * 0.25))
        right = min(width, int(width * 0.8))
        bottom = min(height, int(height * 0.75))
        sample = base_image.crop((left, top, right, bottom)).convert("L")
        histogram = sample.histogram()
        total_pixels = max(1, sum(histogram))
        luminance_sum = sum(index * count for index, count in enumerate(histogram))
        return float(luminance_sum) / float(total_pixels)

    def choose_preview_text_colors(base_image) -> tuple[str, str]:
        """What: Delegate preview fill/outline selection to shared overlay color logic.

        Why included: Library preview and secondary output should use the same contrast policy.
        Where used: ensure_preview_text_colors() before fitting verse text.
        Returns: Fill and outline color pair.
        """
        bg_path = getattr(base_image, "_background_path", None) or get_current_background_path()
        return choose_text_colors(base_image, background_path=bg_path)

    def ensure_preview_text_colors(base_image) -> tuple[str, str]:
        """What: Return cached preview colors or calculate and cache them from the background.

        Why included: Animated/media previews should avoid repeated luminance analysis.
        Where used: Preview image and scrolling-overlay rendering.
        Returns: Cached or newly selected fill/stroke color pair.
        """
        fill_color = state.get("preview_fill_color")
        stroke_color = state.get("preview_stroke_color")
        if isinstance(fill_color, str) and isinstance(stroke_color, str):
            return fill_color, stroke_color
        fill_color, stroke_color = choose_preview_text_colors(base_image)
        state["preview_fill_color"] = fill_color
        state["preview_stroke_color"] = stroke_color
        return fill_color, stroke_color

    def fit_preview_text(base_image, verse_text: str):
        """What: Wrap and shrink a verse until its text fits the preview image.

        Why included: The local preview has fixed dimensions and must not clip long verses.
        Where used: get_preview_overlay_image() for the simple local overlay path.
        Returns: Wrapped text, Pillow font, and spacing; unavailable metrics return None/zero.
        """
        if ImageDraw is None or ImageFont is None:
            return verse_text, None, 0
        preview_width, preview_height = base_image.size
        stroke_width = LIBRARY_PREVIEW_STROKE_WIDTH
        usable_width = max(60, preview_width - ((stroke_width + 18) * 2))
        usable_height = max(60, preview_height - ((stroke_width + 18) * 2))
        base_family, base_size, base_style = get_preview_font_value()
        max_size = get_library_preview_max_text_size(preview_height, base_size)
        min_size = 12
        fallback_text = verse_text
        fallback_font = None
        fallback_spacing = 6
        measure_draw = get_shared_library_measure_draw()
        if measure_draw is None:
            return verse_text, None, 0
        for candidate_size in range(max_size, min_size - 1, -2):
            candidate_font = get_preview_pil_font((base_family, candidate_size, base_style))
            if candidate_font is None:
                continue
            candidate_text = wrap_preview_text_to_width(verse_text, candidate_font, usable_width, stroke_width)
            candidate_spacing = max(4, round(candidate_size * 0.28))
            text_box = measure_draw.multiline_textbbox(
                (0, 0),
                candidate_text,
                font=candidate_font,
                align="center",
                spacing=candidate_spacing,
                stroke_width=stroke_width,
            )
            text_width = float(text_box[2] - text_box[0])
            text_height = float(text_box[3] - text_box[1])
            fallback_text = candidate_text
            fallback_font = candidate_font
            fallback_spacing = candidate_spacing
            if text_width <= usable_width and text_height <= usable_height:
                return candidate_text, candidate_font, candidate_spacing
        return fallback_text, fallback_font, fallback_spacing

    def get_preview_overlay_image(base_image):
        """What: Create or reuse a cached transparent overlay for the current verse.

        Why included: Text overlay composition is repeated during background animation.
        Where used: Preview frame composition before alpha compositing.
        Returns: Transparent RGBA overlay, or None when text/Pillow rendering is unavailable.
        """
        verse_text = str(state.get("preview_text") or "").strip()
        if not verse_text or Image is None or ImageDraw is None or ImageFont is None:
            return None
        preview_width, preview_height = base_image.size
        fill_color, stroke_color = ensure_preview_text_colors(base_image)
        overlay_cache_key = (
            preview_width,
            preview_height,
            verse_text,
            *get_preview_font_value(),
            fill_color,
            stroke_color,
        )
        cached_key = state.get("preview_overlay_cache_key")
        cached_overlay = state.get("preview_overlay_image")
        if cached_key == overlay_cache_key and cached_overlay is not None:
            return cached_overlay
        wrapped_text, preview_font, line_spacing = fit_preview_text(base_image, verse_text)
        if preview_font is None:
            return None
        overlay_image = Image.new("RGBA", (preview_width, preview_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay_image)
        stroke_width = LIBRARY_PREVIEW_STROKE_WIDTH
        draw.multiline_text(
            (preview_width / 2.0, preview_height / 2.0),
            wrapped_text,
            font=preview_font,
            fill=fill_color,
            stroke_width=stroke_width,
            stroke_fill=stroke_color,
            spacing=line_spacing,
            align="center",
            anchor="mm",
        )
        state["preview_overlay_cache_key"] = overlay_cache_key
        state["preview_overlay_image"] = overlay_image
        return overlay_image

    def get_fitted_background_cached(source_image, bg_path, size: tuple[int, int], video: bool):
        """What: Fit a static background once per path and size, reusing the decoded result.

        Why included: Re-decoding a large church photo for every text re-render stalled typing.
        Where used: render_preview_pil_image() for both preview- and output-sized fits.
        Returns: Fitted background image; animated sources bypass the cache per frame.
        """
        suffix = Path(str(bg_path or "")).suffix.lower()
        if video or not bg_path or suffix == ".gif" or suffix in VIDEO_EXTENSIONS:
            fitted = fit_background_image(source_image, size, video=video)
            if bg_path:
                setattr(fitted, "_background_path", bg_path)
            return fitted
        key = (str(bg_path), int(size[0]), int(size[1]))
        cache = state.get("fitted_background_cache")
        if not isinstance(cache, dict):
            cache = {}
            state["fitted_background_cache"] = cache
        fitted = cache.get(key)
        if fitted is None:
            fitted = fit_background_image(source_image, size, video=False)
            setattr(fitted, "_background_path", bg_path)
            if len(cache) >= 8:
                cache.clear()
            cache[key] = fitted
        return fitted

    def render_preview_pil_image(source_image, preview_size: tuple[int, int] | None = None, preview_text: str | None = None, font_value: tuple[str, int, str] | None = None, fill_color: str | None = None, stroke_color: str | None = None):
        """What: Fit the source background and compose the selected song overlay for preview.

        Why included: Preview rendering must match output-sized text fitting and honor cached
        colors/overlay pixels while remaining responsive to dimensions.
        Where used: Worker and media playback paths in refresh_preview_background().
        Returns: Composited RGB Pillow image, or None when Pillow image operations are unavailable.
        """
        if ImageOps is None or Image is None:
            return None
        if preview_size is None:
            preview_width, preview_height = resolve_preview_size()
        else:
            preview_width, preview_height = preview_size
        bg_path = getattr(source_image, "_background_path", None) or get_current_background_path()
        is_video = Path(str(bg_path or "")).suffix.lower() in VIDEO_EXTENSIONS
        rendered_image = get_fitted_background_cached(source_image, bg_path, (preview_width, preview_height), is_video)

        active_preview_text = str(state.get("preview_text") or "") if preview_text is None else str(preview_text or "")
        if not active_preview_text.strip() or ImageDraw is None or ImageFont is None:
            return rendered_image

        active_font_value = get_preview_font_value() if font_value is None else font_value
        display_bounds = get_display_bounds() if callable(get_display_bounds) else None
        render_width = max(1, int((display_bounds or {}).get("width") or 1920))
        render_height = max(1, int((display_bounds or {}).get("height") or 1080))
        corner_text = get_preview_corner_text()

        cache_key = (
            bg_path,
            active_preview_text,
            corner_text,
            render_width,
            render_height,
            tuple(normalize_font_value(active_font_value)),
            get_display_outline_width(),
            get_display_text_color(),
            fill_color,
            stroke_color,
        )
        if state.get("cached_text_overlay_key") == cache_key and state.get("cached_text_overlay_image") is not None:
            full_overlay_image = state.get("cached_text_overlay_image")
        else:
            fitted_background = get_fitted_background_cached(source_image, bg_path, (render_width, render_height), is_video)
            layout = fit_overlay_layout(
                active_preview_text,
                scale_overlay_font_value(normalize_font_value(active_font_value), render_width, render_height),
                render_width,
                render_height,
                scroll_enabled=False,
                base_image=fitted_background,
                background_path=bg_path,
                stroke_width=LIBRARY_PREVIEW_STROKE_WIDTH,
                padding=18,
                max_text_height_ratio=LIBRARY_PREVIEW_MAX_TEXT_HEIGHT_RATIO,
                max_text_multiplier=LIBRARY_PREVIEW_MAX_TEXT_MULTIPLIER,
                min_text_size=12,
                line_spacing_ratio=0.28,
                size_boost=SECONDARY_LAYOUT_SIZE_BOOST,
                corner_font_size=get_display_key_size(render_width, render_height),
            )
            if fill_color is not None or stroke_color is not None:
                layout = type(layout)(
                    text=layout.text,
                    font_value=layout.font_value,
                    line_spacing=layout.line_spacing,
                    fill_color=fill_color or layout.fill_color,
                    stroke_color=stroke_color or layout.stroke_color,
                    corner_font_value=layout.corner_font_value,
                )
            full_overlay_image = render_overlay_image(
                render_width,
                render_height,
                layout,
                corner_text=corner_text,
                stroke_width=LIBRARY_PREVIEW_STROKE_WIDTH,
            )
            state["cached_text_overlay_key"] = cache_key
            state["cached_text_overlay_image"] = full_overlay_image

        if full_overlay_image is None:
            return rendered_image
        overlay_image = full_overlay_image.resize((preview_width, preview_height), Image.Resampling.LANCZOS)
        return Image.alpha_composite(rendered_image.convert("RGBA"), overlay_image).convert("RGB")

    def fit_preview_text_for_render(base_image, verse_text: str, font_value: tuple[str, int, str]):
        """What: Fit a verse using an explicitly supplied font tuple and preview dimensions.

        Why included: Worker rendering snapshots font state so UI edits cannot mutate a render mid-flight.
        Where used: render_preview_pil_image() when composing the full-resolution overlay.
        Returns: Wrapped text, Pillow font, and line spacing.
        """
        if ImageDraw is None or ImageFont is None:
            return verse_text, None, 0
        preview_width, preview_height = base_image.size
        stroke_width = LIBRARY_PREVIEW_STROKE_WIDTH
        usable_width = max(60, preview_width - ((stroke_width + 18) * 2))
        usable_height = max(60, preview_height - ((stroke_width + 18) * 2))
        base_family, base_size, base_style = font_value
        max_size = get_library_preview_max_text_size(preview_height, base_size)
        min_size = 12
        fallback_text = verse_text
        fallback_font = None
        fallback_spacing = 6
        measure_draw = get_shared_library_measure_draw()
        if measure_draw is None:
            return verse_text, None, 0
        for candidate_size in range(max_size, min_size - 1, -2):
            candidate_font = get_preview_pil_font((base_family, candidate_size, base_style))
            if candidate_font is None:
                continue
            candidate_text = wrap_preview_text_to_width(verse_text, candidate_font, usable_width, stroke_width)
            candidate_spacing = max(4, round(candidate_size * 0.28))
            text_box = measure_draw.multiline_textbbox(
                (0, 0),
                candidate_text,
                font=candidate_font,
                align="center",
                spacing=candidate_spacing,
                stroke_width=stroke_width,
            )
            text_width = float(text_box[2] - text_box[0])
            text_height = float(text_box[3] - text_box[1])
            fallback_text = candidate_text
            fallback_font = candidate_font
            fallback_spacing = candidate_spacing
            if text_width <= usable_width and text_height <= usable_height:
                return candidate_text, candidate_font, candidate_spacing
        return fallback_text, fallback_font, fallback_spacing

    def compose_preview_frame(source_image):
        """What: Render one preview frame and wrap it as a Tk PhotoImage.

        Why included: Static/GIF/video media share one path from Pillow output to the preview label.
        Where used: GIF/video playback callbacks and preview rendering.
        Returns: PhotoImage, or None when image conversion is unavailable/fails.
        """
        if ImageOps is None or ImageTk is None:
            return None
        rendered_image = render_preview_pil_image(source_image)
        if rendered_image is None:
            return None
        return ImageTk.PhotoImage(rendered_image)

    def stop_preview_media() -> None:
        """What: Cancel media timers, close the active reader, and advance its generation token.

        Why included: Old GIF/video callbacks must not update a preview after its source changes.
        Where used: Background changes, hidden-tab cleanup, and new media loads.
        Returns: None; clears reader/path/timer state.
        """
        preview_after_id = state.get("preview_after_id")
        if preview_after_id is not None:
            try:
                preview_container.after_cancel(preview_after_id)
            except Exception:
                pass
        state["preview_after_id"] = None
        preview_reader = state.get("preview_reader")
        if preview_reader is not None:
            try:
                preview_reader.close()
            except Exception:
                pass
        state["preview_reader"] = None
        state["preview_media_path"] = None
        state["preview_media_generation"] = int(state.get("preview_media_generation") or 0) + 1

    def cancel_preview_render_work() -> None:
        """What: Invalidate pending preview-render requests and release their queue reference.

        Why included: A slow worker result should not replace newer text/background content.
        Where used: stop/pause preview workflows and background changes.
        Returns: None; increments the render request ID.
        """
        state["preview_render_request_id"] = int(state.get("preview_render_request_id") or 0) + 1
        state["preview_render_queue"] = None

    def pause_preview_activity() -> None:
        """What: Stop media playback and invalidate pending image-render work.

        Why included: Hidden Library tabs should not consume CPU or touch stale widgets.
        Where used: Notebook tab-change handler when Library becomes inactive.
        Returns: None; delegates cleanup to the media/render cancellation helpers.
        """
        stop_preview_media()
        cancel_preview_render_work()

    def render_preview_image(source_image):
        """What: Compose a source image and current verse overlay into a Tk preview photo.

        Why included: Animated and video frame handlers need the same text-composition logic.
        Where used: play_preview_gif() and play_preview_video().
        Returns: PhotoImage or None.
        """
        return compose_preview_frame(source_image)

    def display_preview_photo(preview_photo) -> None:
        """What: Retain and display the latest preview PhotoImage or fallback text.

        Why included: Tk requires a live Python reference to keep PhotoImage pixels visible.
        Where used: Static render polling and animated media frame callbacks.
        Returns: None; updates state and preview_label.
        """
        state["preview_photo"] = preview_photo
        if preview_photo is None:
            preview_label.configure(image="", text=str(state.get("preview_text") or ""))
        else:
            preview_label.configure(image=preview_photo, text="")

    def play_preview_gif(resolved_path: Path) -> bool:
        """What: Open an animated GIF and schedule its frames in the active preview.

        Why included: GIF backgrounds should animate locally instead of showing only frame one.
        Where used: refresh_preview_background() for GIF file paths.
        Returns: True when playback starts; False when decoder/widget/tab is unavailable.
        """
        if Image is None or ImageSequence is None or ImageTk is None:
            return False
        if not is_library_tab_active():
            return False
        try:
            gif_image = Image.open(resolved_path)
        except Exception:
            return False
        state["preview_reader"] = gif_image
        generation = int(state.get("preview_media_generation") or 0)
        frame_count = int(getattr(gif_image, "n_frames", 1) or 1)
        path_str = str(resolved_path)

        def advance(index: int = 0) -> None:
            """What: Decode/render one GIF frame and schedule the next duration-aware frame.

            Why included: Frame callbacks must stop when the tab/media generation changes.
            Where used: after callback chain started by play_preview_gif().
            Returns: None; exits when hidden, superseded, or the reader is absent.
            """
            if not is_library_tab_active():
                return
            if generation != int(state.get("preview_media_generation") or 0):
                return
            active_gif = state.get("preview_reader")
            if active_gif is None:
                return
            try:
                active_gif.seek(index)
                frame_image = active_gif.copy()
            except EOFError:
                active_gif.seek(0)
                frame_image = active_gif.copy()
                index = 0
            setattr(frame_image, "_background_path", path_str)
            preview_photo = render_preview_image(frame_image)
            if preview_photo is not None:
                display_preview_photo(preview_photo)
            delay_ms = int(frame_image.info.get("duration") or active_gif.info.get("duration") or 100)
            next_index = (index + 1) % max(1, frame_count)
            state["preview_after_id"] = preview_container.after(max(20, delay_ms), lambda: advance(next_index))

        advance(0)
        return True

    def play_preview_video(resolved_path: Path) -> bool:
        """What: Start throttled frame-by-frame preview playback for a video background.

        Why included: The local preview should show motion without exceeding its frame-rate cap.
        Where used: refresh_preview_background() for supported video extensions.
        Returns: True when an imageio reader starts; False when unavailable or inactive.
        """
        if imageio is None or Image is None:
            return False
        if not is_library_tab_active():
            return False
        try:
            reader = LoopingVideoReader(resolved_path)
        except Exception:
            return False
        preview_fps = min(max(1.0, reader.fps), PREVIEW_MAX_FPS)
        delay_ms = max(20, int(round(1000 / preview_fps)))
        state["preview_reader"] = reader
        generation = int(state.get("preview_media_generation") or 0)
        path_str = str(resolved_path)

        def advance() -> None:
            """What: Read, render, and schedule one video frame, looping seamlessly.

            Why included: The looping reader restarts the clip without blocking the UI thread.
            Where used: after callback chain started by play_preview_video().
            Returns: None; ends quietly when tab/media generation is stale or decoding fails.
            """
            if not is_library_tab_active():
                return
            if generation != int(state.get("preview_media_generation") or 0):
                return
            active_reader = state.get("preview_reader")
            if active_reader is None:
                return
            try:
                frame_data, _frame_index = active_reader.next_frame()
            except Exception:
                try:
                    active_reader.close()
                except Exception:
                    pass
                state["preview_reader"] = None
                return
            try:
                frame_image = Image.fromarray(frame_data)
                setattr(frame_image, "_background_path", path_str)
                preview_photo = render_preview_image(frame_image)
            except Exception:
                preview_photo = None
            if preview_photo is not None:
                display_preview_photo(preview_photo)
            state["preview_after_id"] = preview_container.after(delay_ms, advance)

        advance()
        return True

    def refresh_preview_background(background_path: str | None = None) -> None:
        """What: Stop old preview work and load/render the selected background.

        Why included: Background changes invalidate contrast/overlay caches and may switch media type.
        Where used: Song selection, background picker, and empty-preview fallback.
        Returns: None; starts GIF/video playback or asynchronous static-image rendering.
        """
        stop_preview_media()
        invalidate_preview_overlay_cache(reset_colors=True)
        pending_text_render = state.get("preview_text_render_after_id")
        if pending_text_render is not None:
            try:
                preview_container.after_cancel(pending_text_render)
            except Exception:
                pass
            state["preview_text_render_after_id"] = None
        state["preview_render_request_id"] = int(state.get("preview_render_request_id") or 0) + 1
        preview_request_id = int(state["preview_render_request_id"])
        state["preview_render_queue"] = None
        target_path = background_path if background_path is not None else background_var.get().strip()
        state["preview_background_target"] = str(target_path or "")
        resolved_path = resolve_library_background_path(target_path)
        state["background_path"] = str(resolved_path) if resolved_path else ""
        if not is_library_tab_active():
            return
        preview_container.configure(bg="#000000")
        preview_label.configure(bg="#000000", fg="#ffffff", compound="center")
        refresh_preview_text_layout()
        if Image is None or ImageOps is None or ImageTk is None:
            state["preview_photo"] = None
            preview_label.configure(image="", text=str(state.get("preview_text") or ""))
            return
        if resolved_path is None:
            black_frame = Image.new("RGB", resolve_preview_size(), "#000000")
            display_preview_photo(compose_preview_frame(black_frame))
            return
        if ImageDraw is None or ImageFont is None:
            state["preview_photo"] = None
            preview_label.configure(image="", text=str(state.get("preview_text") or ""))
            return
        state["preview_media_path"] = str(resolved_path)
        file_extension = resolved_path.suffix.lower()
        if file_extension == ".gif" and play_preview_gif(resolved_path):
            return
        if file_extension in VIDEO_EXTENSIONS and play_preview_video(resolved_path):
            return
        preview_size = resolve_preview_size()
        preview_text = str(state.get("preview_text") or "")
        font_value = get_preview_font_value()
        fill_color = state.get("preview_fill_color")
        stroke_color = state.get("preview_stroke_color")
        preview_label.configure(text="Loading preview...", image="")
        render_queue: queue.Queue = queue.Queue(maxsize=1)
        state["preview_render_queue"] = render_queue

        def render_preview_worker() -> None:
            """What: Load a static background and compose the captured preview snapshot off-thread.

            Why included: Image decoding/fitting should not freeze text editing.
            Where used: Worker thread launched by refresh_preview_background().
            Returns: None; stores a rendered image or None in the size-one result queue.
            """
            try:
                with Image.open(resolved_path) as source_image:
                    setattr(source_image, "_background_path", str(resolved_path))
                    rendered_image = render_preview_pil_image(
                        source_image,
                        preview_size=preview_size,
                        preview_text=preview_text,
                        font_value=font_value,
                        fill_color=fill_color if isinstance(fill_color, str) else None,
                        stroke_color=stroke_color if isinstance(stroke_color, str) else None,
                    )
            except Exception:
                rendered_image = None
            try:
                render_queue.put_nowait(rendered_image)
            except queue.Full:
                pass

        def poll_preview_render_queue() -> None:
            """What: Poll the render queue and apply only the latest active-tab result.

            Why included: Tk image/widget updates must occur on the UI thread, and stale work is ignored.
            Where used: Initial poll and after callback retries in refresh_preview_background().
            Returns: None; reschedules while empty and applies fallback on decode failure.
            """
            if not is_library_tab_active():
                return
            if preview_request_id != int(state.get("preview_render_request_id") or 0):
                return
            if state.get("preview_render_queue") is not render_queue:
                return
            try:
                rendered_image = render_queue.get_nowait()
            except queue.Empty:
                preview_container.after(15, poll_preview_render_queue)
                return
            if rendered_image is None or ImageTk is None:
                state["preview_photo"] = None
                preview_label.configure(image="", text=str(state.get("preview_text") or ""))
                return
            try:
                preview_photo = ImageTk.PhotoImage(rendered_image)
            except Exception:
                state["preview_photo"] = None
                preview_label.configure(image="", text=str(state.get("preview_text") or ""))
                return
            display_preview_photo(preview_photo)

        threading.Thread(target=render_preview_worker, daemon=True).start()
        poll_preview_render_queue()

    preview_container.bind("<Configure>", lambda _event: refresh_preview_text_layout(), add="+")

    def handle_notebook_tab_changed(_event=None) -> None:
        """What: Resume current song preview on Library activation and stop it when hidden.

        Why included: Media playback and render callbacks should not run in background tabs.
        Where used: Notebook <<NotebookTabChanged>> binding.
        Returns: None; refreshes selected details on activation or pauses preview activity.
        """
        if not isinstance(notebook, ttk.Notebook):
            return
        if is_library_tab_active():
            current_song = get_song_by_id(state.get("selected_song_id"))
            if current_song is None:
                refresh_preview_background(None)
            else:
                refresh_details(current_song)
            return
        pause_preview_activity()

    def refresh_selected_verse_visuals() -> None:
        """What: Highlight the selected verse editor row and reset other row borders.

        Why included: The verse sent to Display should be apparent in the editor.
        Where used: Verse selection, removal, and row creation.
        Returns: None; tolerates widgets destroyed during row rebuilds.
        """
        selected_verse_index = state.get("selected_verse_index")
        for row_index, verse_row in enumerate(state["verse_rows"]):
            is_selected = row_index == selected_verse_index
            try:
                verse_row["verse_frame"].configure(bd=2 if is_selected else 1)
            except tk.TclError:
                pass

    def update_send_to_display_button_state() -> None:
        """What: Enable the Send button only when the selected song contains nonblank verses.

        Why included: Empty songs cannot produce a meaningful Display queue item.
        Where used: Verse selection/edit and song-editor updates.
        Returns: None; configures add_button state.
        """
        current_song = get_song_by_id(state.get("selected_song_id"))
        verses = current_song.get("verses", []) if current_song is not None else []
        is_enabled = current_song is not None and any(str(verse).strip() for verse in verses)
        add_button.configure(state="normal" if is_enabled else "disabled")
        show_live_button.configure(state="normal" if is_enabled else "disabled")

    def set_send_to_display_button_for_song(song: dict[str, object] | None) -> None:
        """What: Set Send button availability from a supplied song mapping.

        Why included: Editor population needs to initialize action state without another lookup.
        Where used: populate_editor() and refresh_details().
        Returns: None; enables the action only for songs with nonempty verses.
        """
        verses = song.get("verses", []) if song else []
        is_enabled = song is not None and any(str(verse).strip() for verse in verses)
        add_button.configure(state="normal" if is_enabled else "disabled")
        show_live_button.configure(state="normal" if is_enabled else "disabled")

    def send_selected_verse_to_display(show_live: bool = False) -> None:
        """What: Queue the selected Library song, or show it directly without changing the schedule.

        Why included: Send to Display edits the schedule; Show Live must only update live output.
        Where used: Send to Display and Show Live button commands.
        Returns: None; validates selection and reports whether the song was added or updated.
        """
        current_song = get_song_by_id(state.get("selected_song_id"))
        if current_song is None:
            messagebox.showinfo("Library", "Select a song before sending it to Display.", parent=tab_frame.winfo_toplevel())
            return
        verses = get_editor_verses()
        if not verses:
            messagebox.showinfo("Library", "Add at least one verse before sending it to Display.", parent=tab_frame.winfo_toplevel())
            return
        selected_verse_index = state.get("selected_verse_index")
        if isinstance(selected_verse_index, int) and 0 <= int(selected_verse_index) < len(verses):
            verse_index = int(selected_verse_index)
        else:
            verse_index = 0
            state["selected_verse_index"] = verse_index
            refresh_selected_verse_visuals()
        verse_text = str(verses[verse_index] or "").strip()
        if not verse_text:
            messagebox.showinfo("Library", "The selected verse is empty.", parent=tab_frame.winfo_toplevel())
            return
        verse_labels = describe_song_parts(get_editor_verse_types())
        if show_live:
            show_selected_song = context.get("handle_display_live_change")
            if not callable(show_selected_song):
                messagebox.showerror("Show Live", "Live output is not available.", parent=tab_frame.winfo_toplevel())
                return
            show_selected_song({
                "title": str(current_song.get("title") or "Untitled"),
                "text": verse_text,
                "license_provider": normalize_license_provider(license_provider_var.get()),
                "license_author": author_var.get().strip(),
                "license_info": license_info_var.get().strip(),
                "preview_key": key_var.get().strip(),
                "font_value": get_preview_font_value(),
                "background_path": background_var.get().strip(),
                "source": "library",
                "selected_verse_index": verse_index,
                "scroll_enabled": False,
            }, force=True)
            return
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        title = str(current_song.get("title") or "Untitled")
        song_key = str(current_song.get("song_key") or "").strip()
        subtitle = verse_labels[verse_index] if verse_index < len(verse_labels) else f"Verse {verse_index + 1}"
        if song_key:
            subtitle = f"{subtitle} [{song_key}]"
        queue_item = {
            "id": f"library-{int(current_song.get('id') or 0)}-{verse_index}-{timestamp}",
            "source": "library",
            "source_item_id": str(current_song.get("id") or ""),
            "title": title,
            "subtitle": subtitle,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "text": verse_text,
            "verses": [str(verse).strip() for verse in verses if str(verse).strip()],
            "verse_labels": verse_labels,
            "selected_verse_index": verse_index,
            "font_value": list(get_preview_font_value()),
            "background_path": str(current_song.get("background_path") or "").strip(),
            "license_provider": normalize_license_provider(license_provider_var.get()),
            "license_author": author_var.get().strip(),
            "license_info": license_info_var.get().strip(),
        }

        def append_song_item() -> tuple[bool, str]:
            """What: Upsert the song by stable Library identity and preserve existing queue state.

            Why included: Re-sending edited lyrics should update content without requiring removal.
            Where used: Worker callable for the Send to Display action.
            Returns: Updated-row flag and the persistent queue item ID.
            """
            result = upsert_display_queue_item(queue_item, match_title=True)
            saved_item = next(entry for entry in result["queue_items"] if str(entry.get("source") or "").casefold() == "library" and str(entry.get("source_item_id") or "") == queue_item["source_item_id"])
            return saved_item["id"] != queue_item["id"], str(saved_item["id"])

        def confirm_song_write(result: tuple[bool, str]) -> None:
            """What: Refresh Display and confirm that the selected song was added or updated.

            Why included: The queue must show changed lyrics immediately and the user needs completion feedback.
            Where used: Library queue writer success callback and synchronous fallback.
            Returns: None.
            """
            was_updated, item_id = result
            display_state = context.get("display_state")
            refresh = display_state.get("refresh_from_bridge") if isinstance(display_state, dict) else None
            if callable(refresh):
                refresh()
            messagebox.showinfo("Library", "Song updated in Display." if was_updated else "Song added to Display.", parent=tab_frame.winfo_toplevel())

        if callable(start_background_task):
            start_background_task(
                "LibraryQueueWriter",
                append_song_item,
                on_success=confirm_song_write,
                on_error=lambda error: messagebox.showerror("Library", f"Unable to send the selected song to Display.\n\n{error}", parent=tab_frame.winfo_toplevel()),
            )
            return

        confirm_song_write(append_song_item())

    def schedule_preview_text_rerender(target_background: str) -> None:
        """What: Re-render the static preview once, shortly after typing pauses.

        Why included: Full per-keystroke preview reloads froze typing and cursor placement.
        Where used: select_verse() when only the verse text changed.
        Returns: None; animated previews pick the new text up on their next frame.
        """
        pending = state.get("preview_text_render_after_id")
        if pending is not None:
            try:
                preview_container.after_cancel(pending)
            except Exception:
                pass
            state["preview_text_render_after_id"] = None
        if state.get("preview_after_id") is not None:
            return

        def rerender_static_preview() -> None:
            """What: Apply the latest verse text to the unchanged static background.

            Why included: The debounce must render only the final text state once.
            Where used: after() callback scheduled below.
            Returns: None; skips hidden tabs entirely.
            """
            state["preview_text_render_after_id"] = None
            if is_library_tab_active():
                refresh_preview_background(target_background)

        state["preview_text_render_after_id"] = preview_container.after(300, rerender_static_preview)

    def select_verse(index: int) -> None:
        """What: Store the selected verse, refresh its row/preview, and update Send availability.

        Why included: Preview and queue action state must follow the selected editor verse.
        Where used: Verse row click/focus/key handlers and Add Verse Break.
        Returns: None; reloads the preview background only when it actually changed.
        """
        if index < 0 or index >= len(state["verse_rows"]):
            return
        selection_changed = state.get("selected_verse_index") != index
        state["selected_verse_index"] = index
        if selection_changed:
            refresh_selected_verse_visuals()
        song = get_song_by_id(state.get("selected_song_id"))
        verses = get_editor_verses()
        if song is not None and index < len(verses):
            next_preview_text = verses[index]
            text_changed = next_preview_text != str(state.get("preview_text") or "")
            state["preview_text"] = next_preview_text
            target_background = background_var.get().strip() or str(song.get("background_path") or "")
            background_changed = target_background != str(state.get("preview_background_target") or "")
            if selection_changed or background_changed:
                invalidate_preview_overlay_cache()
                refresh_preview_background(target_background)
            elif text_changed:
                invalidate_preview_overlay_cache()
                schedule_preview_text_rerender(target_background)
        update_send_to_display_button_state()

    def get_editor_verses() -> list[str]:
        """What: Read nonempty trimmed text from every verse editor row in order.

        Why included: Persistence and queue payloads use a clean verse array.
        Where used: Save, Send to Display, and editor-state checks.
        Returns: Ordered verse strings from the current editor.
        """
        verses: list[str] = []
        for verse_row in state["verse_rows"]:
            verse_text_widget = verse_row["text_widget"]
            verse_text = verse_text_widget.get("1.0", tk.END).strip()
            if verse_text:
                verses.append(verse_text)
        return verses

    def get_editor_verse_types() -> list[str]:
        """What: Read the part type of every nonempty verse row, aligned with get_editor_verses().

        Why included: Saving and display labels need the type of each saved part.
        Where used: Save, Send to Display, and Show Live.
        Returns: Ordered part types such as Verse, Chorus, or Bridge.
        """
        return [normalize_part_type(row["part_var"].get()) for row in state["verse_rows"]
                if row["text_widget"].get("1.0", tk.END).strip()]

    def refresh_verse_row_labels() -> None:
        """What: Renumber verse editor rows after insertion or removal.

        Why included: Displayed numbering should match the row's current list position.
        Where used: add_verse_row() and its remove callback.
        Returns: None; updates number labels in place.
        """
        for row_index, verse_row in enumerate(state["verse_rows"], start=1):
            verse_row["number_label"].configure(text=f"{row_index}.")

    def add_verse_row(verse_text: str = "", part_type: str = "Verse") -> None:
        """What: Create a selectable verse row with a part-type dropdown, text editor, and Remove control.

        Why included: Songs store lyrics as ordered parts (verse, chorus, bridge, ...) rather than one text blob.
        Where used: Editor initialization, song loading, and Add Verse Break.
        Returns: None; appends row state and wires selection/removal/scroll handlers.
        """
        row_index = len(state["verse_rows"])
        verse_row_frame = tk.Frame(verses_container)
        verse_row_frame.grid(row=row_index, column=0, sticky="ew", pady=(0, 8))
        verse_row_frame.columnconfigure(1, weight=1)

        number_label = tk.Label(verse_row_frame, text=f"{row_index + 1}.", anchor="w", justify="left")
        number_label.grid(row=0, column=0, sticky="nw", padx=(0, 8), pady=(6, 0))
        part_var = tk.StringVar(value=normalize_part_type(part_type))
        part_combo = ttk.Combobox(verse_row_frame, textvariable=part_var, values=SONG_PART_TYPES, state="readonly", width=10)
        part_combo.grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=(4, 0))

        verse_frame = tk.Frame(verse_row_frame, bd=1, relief="solid", padx=6, pady=6)
        verse_frame.grid(row=0, column=1, rowspan=2, sticky="ew")
        verse_frame.columnconfigure(0, weight=1)
        verse_text_widget = tk.Text(verse_frame, height=4, wrap="word")
        verse_text_widget.grid(row=0, column=0, sticky="ew")
        if verse_text:
            verse_text_widget.insert("1.0", verse_text)

        remove_button = tk.Button(verse_row_frame, text="Remove")
        remove_button.grid(row=0, column=2, sticky="ne", padx=(8, 0))

        verse_row = {
            "frame": verse_row_frame,
            "number_label": number_label,
            "part_var": part_var,
            "part_combo": part_combo,
            "verse_frame": verse_frame,
            "text_widget": verse_text_widget,
            "remove_button": remove_button,
        }
        state["verse_rows"].append(verse_row)

        def handle_select(_event=None, current_row=verse_row) -> str | None:
            """What: Select the verse row captured when its click/focus event was bound.

            Why included: Row identity may shift when an earlier row is removed.
            Where used: Verse frame, label, text, and focus/key event bindings.
            Returns: None for removed rows; otherwise selects the row and returns None.
            """
            try:
                row_index = state["verse_rows"].index(current_row)
            except ValueError:
                return None
            select_verse(row_index)
            return None

        def remove_current_row(current_row=verse_row) -> None:
            """What: Clear the only row or remove this row and repair verse selection/indexing.

            Why included: A song editor must retain one editable row while preserving valid selection.
            Where used: Remove button for this verse row.
            Returns: None; renumbers rows and refreshes selection/scroll visuals.
            """
            if len(state["verse_rows"]) <= 1:
                current_row["text_widget"].delete("1.0", tk.END)
                current_row["text_widget"].focus_set()
                state["selected_verse_index"] = 0
                refresh_selected_verse_visuals()
                return
            removed_index = state["verse_rows"].index(current_row)
            current_row["frame"].destroy()
            state["verse_rows"] = [row for row in state["verse_rows"] if row is not current_row]
            for new_index, remaining_row in enumerate(state["verse_rows"]):
                remaining_row["frame"].grid_configure(row=new_index)
            selected_verse_index = state.get("selected_verse_index")
            if selected_verse_index is None:
                state["selected_verse_index"] = 0
            elif removed_index < selected_verse_index:
                state["selected_verse_index"] = max(0, int(selected_verse_index) - 1)
            elif removed_index == selected_verse_index:
                state["selected_verse_index"] = max(0, min(int(selected_verse_index), len(state["verse_rows"]) - 1))
            refresh_verse_row_labels()
            refresh_selected_verse_visuals()
            refresh_verses_scroll_region()

        remove_button.configure(command=remove_current_row)
        part_combo.bind("<<ComboboxSelected>>", handle_select, add="+")
        verse_row_frame.bind("<Button-1>", handle_select, add="+")
        number_label.bind("<Button-1>", handle_select, add="+")
        verse_frame.bind("<Button-1>", handle_select, add="+")
        verse_text_widget.bind("<Button-1>", handle_select, add="+")
        verse_text_widget.bind("<FocusIn>", handle_select, add="+")
        verse_text_widget.bind("<KeyRelease>", handle_select, add="+")
        apply_layout_style(
            [verse_row_frame, verse_frame, verse_text_widget, remove_button],
            label_widgets=[number_label],
            input_widgets=[verse_text_widget, remove_button],
        )
        bind_outer_verse_scroll(verse_row_frame)
        bind_outer_verse_scroll(number_label)
        bind_outer_verse_scroll(verse_frame)
        bind_text_verse_scroll(verse_text_widget)
        bind_outer_verse_scroll(remove_button)
        refresh_verse_row_labels()
        refresh_selected_verse_visuals()
        refresh_verses_scroll_region()

    def refresh_details(song: dict[str, object] | None = None) -> None:
        """What: Update song metadata summary, selected preview text, background, and Send state.

        Why included: Editor selection and details/preview panels must represent the same song.
        Where used: Song selection, list refresh, save/delete, and empty editor setup.
        Returns: None; clears preview state for a missing song.
        """
        if song is None:
            details.configure(text="No song selected")
            state["preview_text"] = "Library Preview"
            invalidate_preview_overlay_cache()
            refresh_preview_background(None)
            state["selected_verse_index"] = None
            set_send_to_display_button_for_song(None)
            return
        verses = song.get("verses", [])
        selected_verse_index = state.get("selected_verse_index")
        if verses:
            preview_index = 0 if selected_verse_index is None else max(0, min(int(selected_verse_index), len(verses) - 1))
            state["selected_verse_index"] = preview_index
            preview_text = verses[preview_index]
        else:
            preview_text = "No verses added"
        state["preview_text"] = preview_text
        invalidate_preview_overlay_cache()
        refresh_preview_background(background_var.get().strip() or str(song.get("background_path") or ""))
        detail_lines = [
            f"Title: {song.get('title') or ''}",
            f"Key: {song.get('song_key') or ''}",
            f"Music License: {normalize_license_provider(license_provider_var.get()) or PUBLIC_DOMAIN_LABEL}",
            f"Author: {str(song.get('author') or '').strip() or author_var.get().strip() or 'None'}",
            f"Year: {str(song.get('year') or '').strip() or year_var.get().strip() or 'None'}",
            f"Verses: {len(verses)}",
            f"Background: {song.get('background_path') or 'None'}",
        ]
        details.configure(text="\n".join(detail_lines))
        set_send_to_display_button_for_song(song)

    def sync_background_display() -> None:
        """What: Mirror the current background variable into its visible display label.

        Why included: The saved path and user-facing media name should stay synchronized.
        Where used: Song population and background selection.
        Returns: None; shows None when no background is selected.
        """
        raw_value = background_var.get().strip()
        background_display_var.set(raw_value or "None")

    def choose_background() -> None:
        """What: Open the shared background picker with supported Library media choices.

        Why included: Song records can retain a selected image/GIF/video background.
        Where used: Background button command in the Library editor.
        Returns: None; selection callback updates the current song and preview.
        """
        def load_video_frame(resolved_path: Path):
            """What: Decode the first video frame for the background picker thumbnail.

            Why included: Video choices need a visual poster in the shared media picker.
            Where used: load_video_frame callback supplied to open_background_picker().
            Returns: RGB Pillow image, or None when imageio/Pillow cannot decode the file.
            """
            if imageio is None or Image is None:
                return None
            reader = imageio.get_reader(str(resolved_path))
            try:
                frame_data = reader.get_data(0)
            finally:
                try:
                    reader.close()
                except Exception:
                    pass
            return Image.fromarray(frame_data).convert("RGB")

        def apply_selected_background(selected_value: str) -> None:
            """What: Store the selected path, synchronize its label, and refresh song preview.

            Why included: The background picker delegates song persistence/state ownership to Library.
            Where used: on_apply callback passed to open_background_picker().
            Returns: None; schedules preview update after the picker closes.
            """
            background_var.set(selected_value)
            sync_background_display()
            current_song = get_song_by_id(state.get("selected_song_id"))

            def refresh_after_close() -> None:
                """What: Refresh the selected song preview after the modal picker has closed.

                Why included: Destroying the picker before render prevents overlapping Tk dialogs.
                Where used: after_idle callback from apply_selected_background().
                Returns: None; refreshes either the empty preview or current song details.
                """
                if current_song is None:
                    refresh_preview_background(selected_value or None)
                    return
                refresh_details(current_song)

            tab_frame.winfo_toplevel().after_idle(refresh_after_close)

        open_background_picker(
            parent_window=tab_frame.winfo_toplevel(),
            dialog_title="Select Background",
            empty_message="No background files were found in the Backgrounds folder.",
            picker_heading="Background Files",
            choices=[],
            current_background=background_var.get().strip(),
            resolve_background_path=resolve_library_background_path,
            video_extensions=VIDEO_EXTENSIONS,
            apply_layout_style=apply_layout_style,
            on_apply=apply_selected_background,
            show_file_size=True,
            load_video_frame=load_video_frame,
        )

    def populate_editor(song: dict[str, object] | None) -> None:
        """What: Populate title/key/background and recreate verse rows from a song mapping.

        Why included: Selection, save, and clear actions share one editor synchronization path.
        Where used: Song list selection and CRUD completion.
        Returns: None; creates one blank row when the song has no verses.
        """
        title_var.set(str(song.get("title") or "") if song else "")
        key_var.set(str(song.get("song_key") or "") if song else "")
        license_provider_var.set((normalize_license_provider(song.get("license_provider")) or PUBLIC_DOMAIN_LABEL) if song else PUBLIC_DOMAIN_LABEL)
        author_var.set(str(song.get("author") or "") if song else "")
        year_var.set(str(song.get("year") or "") if song else "")
        license_info_var.set(str(song.get("license_info") or "") if song else "")
        update_license_info_visibility()
        background_var.set(str(song.get("background_path") or "") if song else "")
        sync_background_display()
        for verse_row in state["verse_rows"]:
            verse_row["frame"].destroy()
        state["verse_rows"] = []
        verses = song.get("verses", []) if song else []
        verse_types = list(song.get("verse_types") or []) if song else []
        if verses:
            for verse_index, verse in enumerate(verses):
                add_verse_row(str(verse), verse_types[verse_index] if verse_index < len(verse_types) else "Verse")
            state["selected_verse_index"] = 0
        else:
            add_verse_row("")
            state["selected_verse_index"] = None if song is None else 0
        set_send_to_display_button_for_song(song)
        refresh_selected_verse_visuals()
        refresh_details(song)

    def refresh_song_list(*_args) -> None:
        """What: Reload songs, apply search, rebuild list labels, and preserve a valid selection.

        Why included: Search changes and persistence operations must keep editor/list state aligned.
        Where used: Search variable trace and song create/save/delete workflows.
        Returns: None; selects the first matching song when the prior selection is unavailable.
        """
        search_term = search_text_var.get().strip().lower()
        songs = load_all_songs()
        state["songs"] = songs
        filtered = [song for song in songs if not search_term or search_term in str(song.get("title") or "").lower() or search_term in str(song.get("song_key") or "").lower()]
        state["filtered_song_ids"] = [song["id"] for song in filtered]
        song_list.delete(0, tk.END)
        for song in filtered:
            label = str(song.get("title") or "Untitled")
            song_key = str(song.get("song_key") or "").strip()
            if song_key:
                label = f"{label} ({song_key})"
            song_list.insert(tk.END, label)

        current_song = get_song_by_id(state.get("selected_song_id"))
        if current_song is None:
            if filtered:
                first_song = filtered[0]
                state["selected_song_id"] = first_song["id"]
                song_list.selection_clear(0, tk.END)
                song_list.selection_set(0)
                song_list.activate(0)
                populate_editor(first_song)
                return
            refresh_details(None)
            return
        if current_song["id"] not in state["filtered_song_ids"]:
            if filtered:
                first_song = filtered[0]
                state["selected_song_id"] = first_song["id"]
                song_list.selection_clear(0, tk.END)
                song_list.selection_set(0)
                song_list.activate(0)
                populate_editor(first_song)
                return
            state["selected_song_id"] = None
            song_list.selection_clear(0, tk.END)
            refresh_details(None)
            return
        list_index = state["filtered_song_ids"].index(current_song["id"])
        song_list.selection_clear(0, tk.END)
        song_list.selection_set(list_index)
        song_list.activate(list_index)
        refresh_details(current_song)

    def select_song_from_list(_event=None) -> None:
        """What: Resolve the selected list row to a song and load it into the editor.

        Why included: A Listbox index is transient; the selected persistent ID identifies the record.
        Where used: Song Listbox <<ListboxSelect>> binding.
        Returns: None; ignores empty/out-of-range selections.
        """
        selection = song_list.curselection()
        if not selection:
            return
        list_index = int(selection[0])
        if list_index >= len(state["filtered_song_ids"]):
            return
        song_id = state["filtered_song_ids"][list_index]
        song = get_song_by_id(song_id)
        state["selected_song_id"] = song_id
        populate_editor(song)

    def clear_editor(reset_selection: bool = True) -> None:
        """What: Clear song fields/verses and optionally release list selection state.

        Why included: New-song and post-delete flows must not retain stale song metadata.
        Where used: New Song, Clear, and delete completion actions.
        Returns: None; delegates field/row reset to populate_editor(None).
        """
        if reset_selection:
            state["selected_song_id"] = None
            song_list.selection_clear(0, tk.END)
            state["selected_verse_index"] = None
        populate_editor(None)

    def create_new_song() -> None:
        """What: Start a blank song entry and focus the title field.

        Why included: Creating a record should clear the prior selection before typing.
        Where used: New Song button command.
        Returns: None; resets editor selection and gives keyboard focus to title_entry.
        """
        clear_editor(reset_selection=True)
        title_entry.focus_set()

    def add_verse_break() -> None:
        """What: Add an empty verse row, select it, and focus its text editor.

        Why included: Multi-verse songs need explicit user-controlled verse boundaries.
        Where used: Add Verse button command.
        Returns: None; updates row labels, selection styling, and scroll region.
        """
        add_verse_row("")
        new_index = len(state["verse_rows"]) - 1
        select_verse(new_index)
        state["verse_rows"][-1]["text_widget"].focus_set()

    def save_current_song() -> None:
        """What: Validate the title, persist the current song, then refresh/select its record.

        Why included: The editor needs one path for both creating and updating song records.
        Where used: Save button command.
        Returns: None; shows a warning and focuses title_entry when title is blank.
        """
        title = title_var.get().strip()
        if not title:
            messagebox.showwarning("Library", "Title is required before saving.", parent=tab_frame.winfo_toplevel())
            title_entry.focus_set()
            return
        verses = get_editor_verses()
        song_id = save_song_record(
            state.get("selected_song_id"),
            title,
            key_var.get().strip(),
            verses,
            background_var.get().strip(),
            license_provider_var.get(),
            author_var.get(),
            year_var.get(),
            license_info_var.get(),
            get_editor_verse_types(),
        )
        state["selected_song_id"] = song_id
        refresh_song_list()
        song = get_song_by_id(song_id)
        populate_editor(song)

    def delete_current_song() -> None:
        """What: Confirm and permanently delete the selected song record.

        Why included: Deletion is destructive and must be explicitly approved by the user.
        Where used: Delete button command.
        Returns: None; leaves data unchanged when no song is selected or deletion is declined.
        """
        song_id = state.get("selected_song_id")
        if song_id is None:
            messagebox.showinfo("Library", "Select a song before deleting.", parent=tab_frame.winfo_toplevel())
            return
        song = get_song_by_id(song_id)
        song_title = str(song.get("title") or "this song") if song else "this song"
        if not messagebox.askyesno("Library", f"Delete '{song_title}'?", parent=tab_frame.winfo_toplevel()):
            return
        delete_song_record(int(song_id))
        clear_editor(reset_selection=True)
        refresh_song_list()

    search_text_var.trace_add("write", refresh_song_list)
    def update_license_info_visibility() -> None:
        """What: Show the License info field only when Individual is selected.

        Why included: Only songs used with individual permission need their own license text.
        Where used: Music License selection and loading a song into the editor.
        Returns: None; hides or shows the License info row.
        """
        if normalize_license_provider(license_provider_var.get()) == "Individual":
            license_info_label.grid()
            license_info_entry.grid()
        else:
            license_info_label.grid_remove()
            license_info_entry.grid_remove()

    update_license_info_visibility()
    license_picker.bind(
        "<<ComboboxSelected>>",
        lambda _event: (update_license_info_visibility(), refresh_details(get_song_by_id(state.get("selected_song_id")))),
    )
    song_list.bind("<<ListboxSelect>>", select_song_from_list)
    if isinstance(notebook, ttk.Notebook):
        notebook.bind("<<NotebookTabChanged>>", handle_notebook_tab_changed, add="+")
    new_button.configure(command=create_new_song)
    add_button.configure(command=send_selected_verse_to_display)
    show_live_button.configure(command=lambda: send_selected_verse_to_display(show_live=True))
    background_button.configure(command=choose_background)
    add_verse_button.configure(command=add_verse_break)
    save_button.configure(command=save_current_song)
    clear_button.configure(command=lambda: clear_editor(reset_selection=False))
    delete_button.configure(command=delete_current_song)

    populate_editor(None)
    refresh_song_list()

    def refresh_preview_from_settings() -> None:
        """What: Invalidate cached preview colors/overlay and render using current settings.

        Why included: Main app display-font/color changes affect Library preview pixels.
        Where used: Registered as library_state refresh_preview callback for main.py.
        Returns: None; uses the selected song background or its saved fallback.
        """
        invalidate_preview_overlay_cache(reset_colors=True)
        current_song = get_song_by_id(state.get("selected_song_id"))
        bg = background_var.get().strip()
        if not bg and isinstance(current_song, dict):
            bg = str(current_song.get("background_path") or "").strip()
        refresh_preview_background(bg or None)

    library_state = context.get("library_state")
    if isinstance(library_state, dict):
        library_state["refresh_preview"] = refresh_preview_from_settings
