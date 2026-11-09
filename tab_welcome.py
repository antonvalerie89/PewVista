"""Compose welcome and birthday text, backgrounds, previews, and queue items."""

from __future__ import annotations

import functools
import json
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

from display_bridge import load_display_queue_state, save_display_queue_state, upsert_display_queue_item
from background_picker import open_background_picker
from members import get_upcoming_birthdays, parse_member_date
from overlay_layout import fit_overlay_layout, fit_sectioned_overlay_layout, get_display_outline_width, get_effective_stroke_width, normalize_font_value, render_overlay_image, render_sectioned_overlay_image, scale_overlay_font_value, start_async_preview_render
from text_size_picker import open_text_size_picker
from overlay_layout import build_sized_text_fields, choose_text_colors, fit_background_image, render_text_fields_overlay
from overlay_layout import render_scrolling_preview
from video_loop import LoopingVideoReader
from app_paths import get_app_base_path, get_app_data_path, resolve_background_file
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


WELCOME_PREVIEW_WIDTH = 360
WELCOME_PREVIEW_HEIGHT = 203
WELCOME_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpg", ".mpeg"}
WELCOME_PREVIEW_MAX_TEXT_HEIGHT_RATIO = 0.08
WELCOME_PREVIEW_MAX_TEXT_MULTIPLIER = 2
WELCOME_PREVIEW_STROKE_WIDTH = 1
WELCOME_DEFAULT_FONT_SIZE = 14
WELCOME_SCROLL_SPEED_MIN = 1
WELCOME_SCROLL_SPEED_MAX = 20
WELCOME_SCROLL_SPEED_DEFAULT = 7
WELCOME_LAYOUT_SIZE_BOOST = 1.15
WELCOME_TAB_COLUMN_WEIGHTS = (4, 4, 3)


def get_welcome_note_db_path() -> Path:
    """Resolve the application-local SQLite path for saved Welcome notes.

    What: Places welcome_note.db under the app's Data directory.
    Why included: Note templates need persistent storage independent of active service content.
    Where used: Connection and CRUD helpers below.
    Returns: Absolute Path to the Welcome-note database.
    """
    return get_app_data_path() / "welcome_note.db"


def connect_welcome_note_db() -> sqlite3.Connection:
    """Open the Welcome-note database with a bounded busy timeout and WAL settings.

    What: Creates the parent directory, opens SQLite, and attempts WAL/NORMAL mode.
    Why included: UI note operations may overlap with reads/writes from other app tasks.
    Where used: Schema, listing, save, and delete helpers.
    Returns: Open sqlite3.Connection owned by the caller.
    """
    db_path = get_welcome_note_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=15.0)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        # These pragmas reduce contention when supported, but note storage can still work without them.
        pass
    return connection


def ensure_welcome_note_schema() -> None:
    """Ensure the saved-note table, settings column, and title index exist.

    What: Creates missing schema and adds settings_json for older note databases.
    Why included: Existing templates and layout settings must survive application upgrades.
    Where used: Before Welcome-note reads and writes.
    Returns: None; commits schema changes.
    """
    with closing(connect_welcome_note_db()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS welcome_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                note_text TEXT NOT NULL DEFAULT '',
                settings_json TEXT NOT NULL DEFAULT '{}',
                source_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(welcome_notes)")}
        if "settings_json" not in columns:
            # Older databases lack layout metadata, so add the column before any note is restored.
            connection.execute("ALTER TABLE welcome_notes ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}'")
        try:
            connection.execute("CREATE INDEX IF NOT EXISTS idx_welcome_notes_title ON welcome_notes (title)")
        except Exception:
            # The index is an optimization; a failure should not make saved notes inaccessible.
            pass
        connection.commit()


def load_welcome_notes() -> list[dict[str, object]]:
    """Load saved Welcome-note templates in title order.

    What: Ensures schema, queries note/settings/source/timestamp fields, and converts rows to dicts.
    Why included: The note selector and editor need a stable Python record shape.
    Where used: refresh_welcome_notes() in the Welcome tab.
    Returns: List of saved-note mappings ordered case-insensitively by title.
    """
    ensure_welcome_note_schema()
    with closing(connect_welcome_note_db()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, title, note_text, settings_json, source_key, created_at, updated_at FROM welcome_notes ORDER BY LOWER(title), id"
        ).fetchall()
    return [dict(row) for row in rows]


def save_welcome_note(note_id: int | None, title: str, note_text: str, source_key: str = "", settings: dict[str, object] | None = None) -> int:
    """Create or update one saved Welcome-note template.

    What: Inserts a new note or updates title/text/timestamp; settings are updated only when
    supplied so legacy edits preserve existing layout metadata.
    Why included: Reusable Welcome content needs persistent text and formatting choices.
    Where used: Save Note button command.
    Returns: Persistent database ID for the inserted or updated note.
    """
    ensure_welcome_note_schema()
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    with closing(connect_welcome_note_db()) as connection:
        if note_id is None:
            # No selected ID means this is a new template, not an edit to an existing row.
            cursor = connection.execute(
                "INSERT INTO welcome_notes (title, note_text, settings_json, source_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (title, note_text, json.dumps(settings or {}), source_key, timestamp, timestamp),
            )
            connection.commit()
            return int(cursor.lastrowid)
        connection.execute(
            "UPDATE welcome_notes SET title = ?, note_text = ?, updated_at = ? WHERE id = ?",
            (title, note_text, timestamp, note_id),
        )
        if settings is not None:
            # Omitted settings mean a text-only edit; preserve the template's prior layout choices.
            connection.execute("UPDATE welcome_notes SET settings_json = ? WHERE id = ?", (json.dumps(settings), note_id))
        connection.commit()
        return int(note_id)


def delete_welcome_note(note_id: int) -> None:
    """Delete one saved Welcome-note record by database ID.

    What: Ensures schema and commits a parameterized DELETE statement.
    Why included: Users can remove obsolete reusable note templates.
    Where used: Delete Note button command after editor selection.
    Returns: None; commits the deletion.
    """
    ensure_welcome_note_schema()
    with closing(connect_welcome_note_db()) as connection:
        connection.execute("DELETE FROM welcome_notes WHERE id = ?", (note_id,))
        connection.commit()


def resolve_welcome_preview_font_path(font_family: str, font_style: str) -> Path | None:
    """Resolve a supported Windows font family/style to its installed font file.

    What: Maps configured family/style names to known Windows font filenames.
    Why included: Pillow preview fitting needs installed-font metrics.
    Where used: get_welcome_preview_pil_font().
    Returns: Existing font Path, or None when unsupported/unavailable.
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
def _load_welcome_preview_pil_font_cached(font_path_str: str | None, size: int):
    """What: Load a TrueType Welcome preview font or Pillow's default fallback.

    Why included: Repeated field fitting should reuse identical family/size faces.
    Where used: get_welcome_preview_pil_font() through the LRU cache.
    Returns: Pillow font object.
    """
    if font_path_str is None:
        try:
            return ImageFont.load_default(size)
        except TypeError:
            # Older Pillow releases do not accept a requested size for the default font.
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(font_path_str, size)
    except OSError:
        # A removed or unreadable Windows font should not prevent the preview from rendering.
        return ImageFont.load_default()


def get_welcome_preview_pil_font(font_value):
    """Load the cached Pillow font matching a Welcome field's font tuple.

    What: Validates Pillow/input, clamps size, resolves the Windows font, and loads cached face.
    Why included: Preview fitting needs repeatable typeface metrics.
    Where used: Welcome text sizing and preview image rendering.
    Returns: Pillow font handle, or None when Pillow/input is unavailable.
    """
    if ImageFont is None or not isinstance(font_value, tuple) or len(font_value) < 2:
        return None
    family = str(font_value[0])
    size = max(10, abs(int(font_value[1])))
    style = str(font_value[2]) if len(font_value) > 2 else "normal"
    font_path = resolve_welcome_preview_font_path(family, style)
    return _load_welcome_preview_pil_font_cached(str(font_path) if font_path else None, size)


def get_welcome_preview_max_text_size(preview_height: int, base_size: int) -> int:
    """Cap candidate font size by preview height and configured field size.

    What: Applies the height-ratio cap and base-size multiplier cap.
    Why included: Multi-field preview text must not overflow its fixed viewport.
    Where used: Welcome preview text fitting.
    Returns: Maximum font size in pixels.
    """
    scaled_height_cap = round(max(1, preview_height) * WELCOME_PREVIEW_MAX_TEXT_HEIGHT_RATIO)
    multiplied_base_cap = max(18, int(base_size) * WELCOME_PREVIEW_MAX_TEXT_MULTIPLIER)
    return max(18, min(scaled_height_cap, multiplied_base_cap))


_WELCOME_MEASURE_DRAW = None


def get_shared_welcome_measure_draw():
    """Return a cached Pillow draw surface for Welcome preview measurements.

    What: Creates one transparent 1x1 image and its ImageDraw object lazily.
    Why included: Repeated width calculations should avoid allocating scratch images.
    Where used: Text wrapping and preview-fitting loops.
    Returns: Shared ImageDraw instance, or None when Pillow is unavailable.
    """
    global _WELCOME_MEASURE_DRAW
    if _WELCOME_MEASURE_DRAW is None and Image is not None and ImageDraw is not None:
        _WELCOME_MEASURE_DRAW = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
    return _WELCOME_MEASURE_DRAW


def wrap_welcome_preview_text_to_width(text: str, pil_font, max_width: int, stroke_width: int) -> str:
    """Wrap Welcome text to a pixel width measured with its outline stroke.

    What: Adds words while measured width fits and preserves blank input lines.
    Why included: Preview fields must avoid clipping while retaining paragraph breaks.
    Where used: fit_preview_text_for_render() and sectioned preview drawing.
    Returns: Wrapped text, or original text when Pillow metrics are unavailable.
    """
    if Image is None or ImageDraw is None or pil_font is None or max_width <= 0:
        return text
    measure_draw = get_shared_welcome_measure_draw()
    if measure_draw is None:
        return text

    def measure_width(value: str) -> float:
        """What: Measure one candidate line's pixel width including its outline.

        Why included: Word wrapping must match the visible rendered extent.
        Where used: wrap_welcome_preview_text_to_width() while building lines.
        Returns: Width in pixels, or zero for empty text.
        """
        if not value:
            return 0.0
        box = measure_draw.textbbox((0, 0), value, font=pil_font, stroke_width=stroke_width)
        return float(box[2] - box[0])

    wrapped_lines: list[str] = []
    for raw_line in text.splitlines() or [text]:
        if not raw_line.strip():
            # Keep deliberate paragraph breaks instead of collapsing blank lines during wrapping.
            wrapped_lines.append("")
            continue
        words = raw_line.split()
        current_line = ""
        for word in words:
            candidate = word if not current_line else f"{current_line} {word}"
            if current_line and measure_width(candidate) > max_width:
                # Commit the last fitting line and retry this word on a fresh line.
                wrapped_lines.append(current_line)
                current_line = word
            else:
                current_line = candidate
        if current_line:
            wrapped_lines.append(current_line)
    return "\n".join(wrapped_lines)


def get_welcome_background_root() -> Path:
    """Resolve the shared app Backgrounds directory.

    What: Joins the application root with the Backgrounds directory.
    Why included: Welcome, Library, and Preparation use a consistent bundled media location.
    Where used: list_welcome_background_choices().
    Returns: Background root Path; it may not exist.
    """
    return get_app_base_path() / "Backgrounds"


def list_welcome_background_choices() -> list[str]:
    """List files under Backgrounds as sorted app-relative picker values.

    What: Recursively collects files and formats each path with forward slashes.
    Why included: Relative references remain portable with the application directory.
    Where used: choose_welcome_background() when opening the shared picker.
    Returns: Sorted path strings, or an empty list when the folder is absent.
    """
    backgrounds_roots = (get_welcome_background_root(), get_app_data_path() / "Backgrounds")
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


def resolve_welcome_background_path(background_path: str | None) -> Path | None:
    """Resolve a saved Welcome background path as absolute or app-relative.

    What: Checks absolute paths first, then tries the app root for relative paths.
    Why included: Persisted records may outlive media moves or app launches.
    Where used: Preview background loading and picker path handling.
    Returns: Existing file Path, or None for blank/missing media.
    """
    return resolve_background_file(background_path, get_app_base_path(), get_app_data_path())


def build_welcome_tab(tab_frame: tk.Frame, context: dict[str, object]) -> None:
    """Build the Welcome tab's editors, saved-note controls, preview, and queue actions.

    What: Composes the three-field Welcome UI and connects its display, persistence, and preview callbacks.
    Why included: Welcome messages need a dedicated workflow for editing, formatting, previewing, and sending service content.
    Where used: Main application tab composition when the Welcome tab is initialized.
    Returns: None; registers widgets and callbacks in the supplied context/state.
    """
    tab_widgets = context["tab_widgets"]
    tab_labels = context["tab_labels"]
    tab_inputs = context["tab_inputs"]
    build_preview_placeholder = context["build_preview_placeholder"]
    apply_layout_style = context["apply_layout_style"]
    get_display_bounds = context.get("get_display_bounds")
    get_preview_size = context.get("get_preview_size")
    display_font_family = context.get("display_font_family")
    display_font_style = context.get("display_font_style")
    notebook = context.get("notebook")
    welcome_state = context.get("welcome_state")
    handle_welcome_display_change = context.get("handle_welcome_display_change")
    display_state = context.get("display_state")
    start_background_task = context.get("start_background_task")
    state: dict[str, object] = {
        "background_path": "",
        "preview_photo": None,
        "preview_refresh_after_id": None,
        "preview_scroll_after_id": None,
        "preview_gif_after_id": None,
        "preview_video_after_id": None,
        "preview_video_reader": None,
        "preview_video_path": "",
        "preview_video_lock": threading.Lock(),
        "preview_scroll_offset": 0,
        "preview_scroll_cycle_height": 0,
        "preview_gif_path": "",
        "preview_gif_reader": None,
        "preview_gif_frame_count": 0,
        "preview_gif_frame_index": 0,
        "preview_render_request_id": 0,
        "preview_render_queue": None,
        "welcome_notes": [],
        "selected_welcome_note_id": None,
    }
    note_title_var = tk.StringVar()
    welcome_note_list: tk.Listbox | None = None

    def is_welcome_tab_active() -> bool:
        """What: Check whether the Welcome tab currently owns the notebook selection.

        Why included: Background preview work should pause when another tab is active.
        Where used: Preview refresh and animation scheduling callbacks.
        Returns: True when Welcome is selected or no notebook is available.
        """
        if not isinstance(notebook, ttk.Notebook):
            return True
        try:
            selected_tab = notebook.nametowidget(notebook.select())
        except tk.TclError:
            return True
        return selected_tab == tab_frame

    def get_text_size_var():
        """What: Retrieve the host's shared text-size variable when available.

        Why included: The Welcome field should honor the font size already owned by app state.
        Where used: Initializing the Welcome Note size control.
        Returns: Host-provided Tk IntVar, or None.
        """
        if isinstance(welcome_state, dict):
            text_size_var = welcome_state.get("text_size_var")
            if isinstance(text_size_var, tk.IntVar):
                return text_size_var
        return None

    welcome_size_var = get_text_size_var() or tk.IntVar(value=WELCOME_DEFAULT_FONT_SIZE)
    birthday_size_var = tk.IntVar(value=WELCOME_DEFAULT_FONT_SIZE)
    upcoming_size_var = tk.IntVar(value=WELCOME_DEFAULT_FONT_SIZE)
    field_size_vars = {"Welcome Note": welcome_size_var, "Birthday Message": birthday_size_var, "Upcoming Birthdays": upcoming_size_var}
    field_alignment_vars = {
        "Welcome Note": tk.StringVar(value="center"),
        "Birthday Message": tk.StringVar(value="center"),
        "Upcoming Birthdays": tk.StringVar(value="left"),
    }
    field_size_buttons: dict[str, tk.Button] = {}

    def get_selected_date_var():
        """What: Retrieve the shared service-date variable from the host state.

        Why included: Upcoming birthdays are calculated relative to the selected service date.
        Where used: Date resolution and the Welcome date-change trace.
        Returns: Tk variable when supplied by the host, otherwise None.
        """
        if isinstance(welcome_state, dict):
            selected_date_var = welcome_state.get("selected_date_var")
            if isinstance(selected_date_var, tk.StringVar):
                return selected_date_var
        return None

    def get_combine_text_var():
        """What: Retrieve or initialize the BooleanVar controlling combined birthday text.

        Why included: Preview and display payloads must share the user's combine setting.
        Where used: Preview section construction and restored queue entries.
        Returns: Shared Tk BooleanVar.
        """
        if isinstance(welcome_state, dict):
            combine_text_var = welcome_state.get("combine_text_var")
            if isinstance(combine_text_var, tk.BooleanVar):
                return combine_text_var
        return None

    def get_send_to_display_var():
        """What: Retrieve the shared BooleanVar for automatic Welcome display updates.

        Why included: The owning UI state controls whether edits are pushed live.
        Where used: push_active_welcome_to_display() and send action handling.
        Returns: Host-provided Tk BooleanVar, or None.
        """
        if isinstance(welcome_state, dict):
            send_to_display_var = welcome_state.get("send_to_display_var")
            if isinstance(send_to_display_var, tk.BooleanVar):
                return send_to_display_var
        return None

    def get_activate_scroll_var():
        """What: Retrieve or initialize the Welcome scrolling-enabled BooleanVar.

        Why included: Preview rendering and payload creation use a consistent scroll setting.
        Where used: Scroll rendering, controls, restore, and queue payload creation.
        Returns: Shared Tk BooleanVar.
        """
        if isinstance(welcome_state, dict):
            activate_scroll_var = welcome_state.get("activate_scroll_var")
            if isinstance(activate_scroll_var, tk.BooleanVar):
                return activate_scroll_var
        return None

    def get_scroll_speed_var():
        """What: Retrieve or initialize the Welcome scroll-speed IntVar.

        Why included: The speed slider and local preview need one shared value.
        Where used: Speed normalization, state synchronization, and display payload creation.
        Returns: Shared Tk IntVar.
        """
        if isinstance(welcome_state, dict):
            scroll_speed_var = welcome_state.get("scroll_speed_var")
            if isinstance(scroll_speed_var, tk.IntVar):
                return scroll_speed_var
        return None

    def is_preview_scroll_active() -> bool:
        """What: Determine whether the Welcome preview scroll loop should run.

        Why included: Scrolling should run only when enabled and there is preview text.
        Where used: Preview sizing, rendering, and animation timers.
        Returns: True when the shared scroll setting is enabled.
        """
        activate_scroll_var = get_activate_scroll_var()
        return isinstance(activate_scroll_var, tk.BooleanVar) and activate_scroll_var.get() and bool(get_preview_text_value().strip())

    def get_preview_scroll_step() -> int:
        """What: Convert the selected scroll speed into a bounded frame-step distance.

        Why included: The Tk preview timer needs a predictable pixels-per-tick value.
        Where used: schedule_preview_scroll_animation().
        Returns: Positive integer scroll distance in pixels per tick.
        """
        scroll_speed_var = get_scroll_speed_var()
        speed_value = int(scroll_speed_var.get() or WELCOME_SCROLL_SPEED_DEFAULT) if isinstance(scroll_speed_var, tk.IntVar) else WELCOME_SCROLL_SPEED_DEFAULT
        normalized_speed = max(WELCOME_SCROLL_SPEED_MIN, min(WELCOME_SCROLL_SPEED_MAX, speed_value))
        return max(1, round(normalized_speed / 4))

    def cancel_preview_scroll_animation(preview_label: tk.Label) -> None:
        """What: Cancel and clear the scheduled Welcome scrolling timer.

        Why included: Replaced or hidden previews must not keep ticking.
        Where used: Preview pause, scroll rescheduling, and tab lifecycle handling.
        Returns: None.
        """
        after_id = state.get("preview_scroll_after_id")
        if after_id:
            try:
                preview_label.after_cancel(after_id)
            except Exception:
                pass
        state["preview_scroll_after_id"] = None

    def cancel_preview_gif_animation(preview_label: tk.Label) -> None:
        """What: Cancel and clear the scheduled animated-GIF frame timer.

        Why included: GIF callbacks should stop when media changes or the tab is inactive.
        Where used: GIF cache cleanup and preview pause/rescheduling.
        Returns: None.
        """
        after_id = state.get("preview_gif_after_id")
        if after_id:
            try:
                preview_label.after_cancel(after_id)
            except Exception:
                pass
        state["preview_gif_after_id"] = None

    def clear_preview_gif_cache(preview_label: tk.Label) -> None:
        """What: Stop GIF playback, close its reader, and reset cached frame state.

        Why included: Switching away from a GIF must release file resources.
        Where used: Preview rendering when media changes or GIF loading fails.
        Returns: None.
        """
        cancel_preview_gif_animation(preview_label)
        preview_reader = state.get("preview_gif_reader")
        if preview_reader is not None:
            try:
                preview_reader.close()
            except Exception:
                pass
        state["preview_gif_path"] = ""
        state["preview_gif_reader"] = None
        state["preview_gif_frame_count"] = 0
        state["preview_gif_frame_index"] = 0

    def stop_preview_video(preview_label: tk.Label) -> None:
        """What: Cancel video animation and close the Welcome preview decoder.

        Why included: Hidden tabs, changed backgrounds, and shutdown must release video resources.
        Where used: Preview pause, non-video rendering, and preview widget destruction.
        Returns: None; serializes cleanup with background frame decoding.
        """
        after_id = state.get("preview_video_after_id")
        if after_id:
            try:
                preview_label.after_cancel(after_id)
            except tk.TclError:
                pass
        state["preview_video_after_id"] = None
        with state["preview_video_lock"]:
            reader = state.get("preview_video_reader")
            if reader is not None:
                try:
                    reader.close()
                finally:
                    state["preview_video_reader"] = None
                    state["preview_video_path"] = ""

    def pause_preview_activity(preview_label: tk.Label) -> None:
        """What: Cancel preview timers and invalidate any pending background render.

        Why included: Inactive tabs should not update widgets or retain stale results.
        Where used: Welcome notebook-tab change handling.
        Returns: None.
        """
        cancel_preview_refresh(preview_label)
        cancel_preview_scroll_animation(preview_label)
        cancel_preview_gif_animation(preview_label)
        state["preview_render_request_id"] = int(state.get("preview_render_request_id") or 0) + 1
        state["preview_render_queue"] = None
        stop_preview_video(preview_label)

    def cancel_preview_refresh(preview_label: tk.Label) -> None:
        """What: Cancel and clear a deferred Welcome preview refresh.

        Why included: New edits replace queued refreshes rather than stacking them.
        Where used: Preview debounce scheduling and pause_preview_activity().
        Returns: None.
        """
        after_id = state.get("preview_refresh_after_id")
        if after_id:
            try:
                preview_label.after_cancel(after_id)
            except Exception:
                pass
        state["preview_refresh_after_id"] = None

    def schedule_preview_gif_animation(preview_container: tk.Frame, preview_label: tk.Label) -> None:
        """What: Schedule the next animated-GIF frame using its frame duration.

        Why included: Welcome previews should animate without rendering when hidden.
        Where used: GIF frame display and the GIF animation callback itself.
        Returns: None; stores the Tk after identifier in preview state.
        """
        cancel_preview_gif_animation(preview_label)
        if not is_welcome_tab_active():
            return
        preview_reader = state.get("preview_gif_reader")
        frame_count = int(state.get("preview_gif_frame_count") or 0)
        if preview_reader is None or frame_count <= 1:
            return
        frame_index = int(state.get("preview_gif_frame_index") or 0) % frame_count
        try:
            preview_reader.seek(frame_index)
            delay_ms = int(preview_reader.info.get("duration") or 100)
        except Exception:
            delay_ms = 100
        delay_ms = max(20, delay_ms)

        def advance_preview_gif() -> None:
            """What: Advance the cached GIF frame and schedule its next preview update.

            Why included: GIF playback is driven by Tk's event loop and current tab visibility.
            Where used: after() callback created by schedule_preview_gif_animation().
            Returns: None.
            """
            state["preview_gif_after_id"] = None
            if not is_welcome_tab_active():
                return
            current_reader = state.get("preview_gif_reader")
            current_frame_count = int(state.get("preview_gif_frame_count") or 0)
            if current_reader is None or current_frame_count <= 1:
                return
            next_index = (int(state.get("preview_gif_frame_index") or 0) + 1) % current_frame_count
            state["preview_gif_frame_index"] = next_index
            if is_preview_scroll_active() and state.get("preview_scroll_after_id") is not None:
                # The scroll loop already re-renders frequently and will pick up
                # this frame on its next tick, so avoid rendering twice as often.
                schedule_preview_gif_animation(preview_container, preview_label)
                return
            render_background_preview(preview_container, preview_label)

        state["preview_gif_after_id"] = preview_label.after(delay_ms, advance_preview_gif)

    def ensure_preview_gif_cache(background_path: Path, preview_label: tk.Label) -> bool:
        """What: Reuse or open the animated GIF reader and initialize frame tracking.

        Why included: Keeping one reader avoids reopening media on each preview tick.
        Where used: render_background_preview() for GIF backgrounds.
        Returns: True when the path is a readable animated GIF.
        """
        cached_path = str(state.get("preview_gif_path") or "")
        if cached_path == str(background_path) and state.get("preview_gif_reader") is not None:
            return True
        clear_preview_gif_cache(preview_label)
        try:
            gif_image = Image.open(background_path)
            if ImageSequence is None or not getattr(gif_image, "is_animated", False):
                gif_image.close()
                return False
        except Exception:
            return False
        state["preview_gif_path"] = str(background_path)
        state["preview_gif_reader"] = gif_image
        state["preview_gif_frame_count"] = int(getattr(gif_image, "n_frames", 1) or 1)
        state["preview_gif_frame_index"] = 0
        return True

    def schedule_preview_scroll_animation(preview_container: tk.Frame, preview_label: tk.Label) -> None:
        """What: Start or stop the Welcome text scroll loop based on tab and setting.

        Why included: Scrolling must follow current visibility and user selection.
        Where used: Preview refresh and the scroll animation callback.
        Returns: None; stores the Tk after identifier in preview state.
        """
        cancel_preview_scroll_animation(preview_label)
        if not is_welcome_tab_active():
            state["preview_scroll_offset"] = 0
            return
        if not is_preview_scroll_active():
            state["preview_scroll_offset"] = 0
            return

        def advance_preview_scroll() -> None:
            """What: Advance the scroll offset, redraw, and schedule the next tick.

            Why included: The scrolling preview runs incrementally on Tk's event loop.
            Where used: after() callback created by schedule_preview_scroll_animation().
            Returns: None.
            """
            if not is_welcome_tab_active():
                state["preview_scroll_after_id"] = None
                return
            cycle_height = max(1, int(state.get("preview_scroll_cycle_height") or 0))
            next_offset = int(state.get("preview_scroll_offset") or 0) + get_preview_scroll_step()
            state["preview_scroll_offset"] = next_offset % cycle_height if cycle_height > 0 else next_offset
            render_background_preview(preview_container, preview_label)
            state["preview_scroll_after_id"] = preview_label.after(40, advance_preview_scroll)

        state["preview_scroll_after_id"] = preview_label.after(40, advance_preview_scroll)

    def resolve_selected_date():
        """What: Parse the selected service date or use today's date as fallback.

        Why included: Birthday lookahead must remain usable when the shared date is absent or invalid.
        Where used: Upcoming birthday calculation and date-change refreshes.
        Returns: Resolved datetime.date.
        """
        selected_date_var = get_selected_date_var()
        parsed_date = parse_member_date(selected_date_var.get()) if isinstance(selected_date_var, tk.StringVar) else None
        return parsed_date if parsed_date is not None else __import__("datetime").date.today()

    def get_preview_text_value() -> str:
        """What: Combine the Welcome, birthday, and upcoming-birthday fields as configured.

        Why included: Preview and display output need one text representation of the selected fields.
        Where used: Display payload construction and preview rendering.
        Returns: Combined text, or the preview placeholder when fields are empty.
        """
        welcome_value = welcome_text.get("1.0", tk.END).strip()
        combine_text_var = get_combine_text_var()
        if not (isinstance(combine_text_var, tk.BooleanVar) and combine_text_var.get()):
            return welcome_value or "Welcome Preview"

        birthday_value = birthday_text.get("1.0", tk.END).strip()
        upcoming_value = upcoming_text.get("1.0", tk.END).strip()
        combined_text = ""

        if welcome_value:
            combined_text = welcome_value
        if birthday_value:
            combined_text = f"{combined_text}\n\n{birthday_value}" if combined_text else birthday_value
        if upcoming_value:
            if birthday_value:
                combined_text = f"{combined_text}\n{upcoming_value}" if combined_text else upcoming_value
            else:
                combined_text = f"{combined_text}\n\n{upcoming_value}" if combined_text else upcoming_value
        return combined_text or "Welcome Preview"

    def get_preview_text_sections() -> dict[str, object]:
        """What: Build structured text, per-field fonts, alignments, and combine settings.

        Why included: The overlay renderer needs independent Welcome and birthday layout data.
        Where used: Display payload construction and preview overlay generation.
        Returns: Mapping of section text, settings, font values, and alignments.
        """
        welcome_value = welcome_text.get("1.0", tk.END).strip()
        birthday_value = birthday_text.get("1.0", tk.END).strip()
        upcoming_value = upcoming_text.get("1.0", tk.END).strip()
        combine_text_var = get_combine_text_var()
        combine_enabled = isinstance(combine_text_var, tk.BooleanVar) and combine_text_var.get()
        detail_lines: list[str] = []
        if upcoming_value:
            detail_lines.append(upcoming_value)
        return {
            "combine_enabled": combine_enabled,
            "centered_text": welcome_value,
            "centered_detail_text": birthday_value,
            "detail_text": "\n\n".join(line for line in detail_lines if line).strip(),
            "field_fonts": {
                "welcome": [*get_preview_font_value(preview_label)[:1], int(welcome_size_var.get()), get_preview_font_value(preview_label)[2]],
                "birthday": [*get_preview_font_value(preview_label)[:1], int(birthday_size_var.get()), get_preview_font_value(preview_label)[2]],
                "upcoming": [*get_preview_font_value(preview_label)[:1], int(upcoming_size_var.get()), get_preview_font_value(preview_label)[2]],
            },
            "field_alignments": {
                "welcome": field_alignment_vars["Welcome Note"].get(),
                "birthday": field_alignment_vars["Birthday Message"].get(),
                "upcoming": field_alignment_vars["Upcoming Birthdays"].get(),
            },
        }

    def build_welcome_display_payload() -> dict[str, object]:
        """What: Collect the current Welcome text, typography, background, and scroll settings.

        Why included: Display bridge and saved notes consume a consistent Welcome payload.
        Where used: Queue submission, note persistence, and live display updates.
        Returns: Mapping containing Welcome display content and section metadata.
        """
        activate_scroll_var = get_activate_scroll_var()
        scroll_speed_var = get_scroll_speed_var()
        scroll_enabled = bool(activate_scroll_var.get()) if isinstance(activate_scroll_var, tk.BooleanVar) else False
        scroll_speed = int(scroll_speed_var.get() or WELCOME_SCROLL_SPEED_DEFAULT) if isinstance(scroll_speed_var, tk.IntVar) else WELCOME_SCROLL_SPEED_DEFAULT
        scroll_speed = max(WELCOME_SCROLL_SPEED_MIN, min(WELCOME_SCROLL_SPEED_MAX, scroll_speed))
        return {
            "text": get_preview_text_value(),
            "font_value": get_preview_font_value(preview_label),
            "background_path": str(state.get("background_path") or ""),
            "scroll_enabled": scroll_enabled,
            "scroll_speed": scroll_speed,
            "source": "welcome",
            "welcome_sections": get_preview_text_sections(),
        }

    def set_text_widget_value(widget: tk.Text, value: str) -> None:
        """What: Replace a Tk text widget's contents and clear its modified flag.

        Why included: Restoring saved text should not trigger user-edit callbacks.
        Where used: Welcome recall, note selection, and editor reset operations.
        Returns: None.
        """
        widget.delete("1.0", tk.END)
        if value:
            widget.insert("1.0", value)
        widget.edit_modified(False)

    def apply_restored_welcome_entry(entry: dict[str, object] | None) -> None:
        """What: Restore Welcome fields and settings from a saved display queue entry.

        Why included: Recalled service content should reproduce its text, media, and layout choices.
        Where used: Welcome state restore callback registered with the parent application.
        Returns: None.
        """
        welcome_item = None
        queue_items = list(entry.get("queue_items") or []) if isinstance(entry, dict) else []
        for queue_item in queue_items:
            if isinstance(queue_item, dict) and str(queue_item.get("source") or "").casefold() == "welcome":
                welcome_item = queue_item
                break
        welcome_sections = welcome_item.get("welcome_sections") if isinstance(welcome_item, dict) and isinstance(welcome_item.get("welcome_sections"), dict) else {}
        welcome_text_value = str(welcome_sections.get("centered_text") or (welcome_item.get("text") if isinstance(welcome_item, dict) else "") or "").strip()
        birthday_text_value = str(welcome_sections.get("centered_detail_text") or "").strip()
        combine_enabled = bool(welcome_sections.get("combine_enabled"))
        background_value = str(welcome_item.get("background_path") or "").strip() if isinstance(welcome_item, dict) else ""
        scroll_enabled = bool(welcome_item.get("scroll_enabled")) if isinstance(welcome_item, dict) else False
        try:
            restored_scroll_speed = int(welcome_item.get("scroll_speed") or WELCOME_SCROLL_SPEED_DEFAULT) if isinstance(welcome_item, dict) else WELCOME_SCROLL_SPEED_DEFAULT
        except Exception:
            restored_scroll_speed = WELCOME_SCROLL_SPEED_DEFAULT

        restore_field_fonts(welcome_item or {})
        state["background_path"] = background_value
        update_background_display()
        set_text_widget_value(welcome_text, welcome_text_value)
        set_text_widget_value(birthday_text, birthday_text_value)

        combine_text_var = get_combine_text_var()
        if isinstance(combine_text_var, tk.BooleanVar):
            combine_text_var.set(combine_enabled)
        activate_scroll_var = get_activate_scroll_var()
        if isinstance(activate_scroll_var, tk.BooleanVar):
            activate_scroll_var.set(scroll_enabled)
        scroll_speed_var = get_scroll_speed_var()
        if isinstance(scroll_speed_var, tk.IntVar):
            scroll_speed_var.set(max(WELCOME_SCROLL_SPEED_MIN, min(WELCOME_SCROLL_SPEED_MAX, restored_scroll_speed)))
        update_scroll_speed_display()
        sync_scroll_speed_state(scroll_speed)
        if "detail_text" in welcome_sections:
            upcoming_text.configure(state="normal")
            set_text_widget_value(upcoming_text, str(welcome_sections.get("detail_text") or ""))
            upcoming_text.configure(state="disabled")
        else:
            refresh_upcoming_birthdays(upcoming_text)
        schedule_preview_refresh(preview_container, preview_label, welcome_text, delay_ms=1)

    def restore_field_fonts(payload: dict[str, object]) -> None:
        """What: Restore field font sizes and alignments from saved Welcome section metadata.

        Why included: Per-field typography needs to survive service recall and note selection.
        Where used: Applying a recalled Welcome queue item or note.
        Returns: None.
        """
        sections = payload.get("welcome_sections") or {}
        fonts = sections.get("field_fonts") or {}
        alignments = sections.get("field_alignments") or {}
        fallback = normalize_font_value(payload.get("font_value") or ("Segoe UI", WELCOME_DEFAULT_FONT_SIZE, "normal"))
        for key, variable in (("welcome", welcome_size_var), ("birthday", birthday_size_var), ("upcoming", upcoming_size_var)):
            font = normalize_font_value(fonts.get(key) or fallback)
            variable.set(font[1])
        for key, variable in (("welcome", field_alignment_vars["Welcome Note"]), ("birthday", field_alignment_vars["Birthday Message"]), ("upcoming", field_alignment_vars["Upcoming Birthdays"])):
            alignment = str(alignments.get(key) or ("left" if key == "upcoming" else "center")).casefold()
            variable.set(alignment if alignment in {"left", "center", "right"} else ("left" if key == "upcoming" else "center"))
        update_font_button_label(font_button, preview_label)

    def clear_recalled_welcome_entry() -> None:
        """What: Reset recalled Welcome fields and settings to their initial state.

        Why included: Clearing service recall must remove stale Welcome content and media.
        Where used: clear-recalled-data callback registered with parent service state.
        Returns: None.
        """
        restore_field_fonts({})
        state["background_path"] = ""
        update_background_display()
        set_text_widget_value(welcome_text, "")
        set_text_widget_value(birthday_text, "")
        combine_text_var = get_combine_text_var()
        if isinstance(combine_text_var, tk.BooleanVar):
            combine_text_var.set(False)
        activate_scroll_var = get_activate_scroll_var()
        if isinstance(activate_scroll_var, tk.BooleanVar):
            activate_scroll_var.set(False)
        scroll_speed_var = get_scroll_speed_var()
        if isinstance(scroll_speed_var, tk.IntVar):
            scroll_speed_var.set(WELCOME_SCROLL_SPEED_DEFAULT)
        update_scroll_speed_display()
        sync_scroll_speed_state(scroll_speed)
        refresh_upcoming_birthdays(upcoming_text)
        schedule_preview_refresh(preview_container, preview_label, welcome_text, delay_ms=1)

    def push_active_welcome_to_display() -> None:
        """What: Send the current Welcome payload through the display-change callback when enabled.

        Why included: The shared send toggle controls live propagation of Welcome edits.
        Where used: Host state callback when Welcome content changes.
        Returns: None.
        """
        send_to_display_var = get_send_to_display_var()
        if not callable(handle_welcome_display_change):
            return
        if not (isinstance(send_to_display_var, tk.BooleanVar) and send_to_display_var.get()):
            return
        handle_welcome_display_change(build_welcome_display_payload())

    def update_queued_welcome_item() -> None:
        """What: Update an existing Welcome item in the shared display queue with current settings.

        Why included: Font, alignment, background, and scroll edits should update queued content.
        Where used: Font-size and field-alignment controls.
        Returns: None; does nothing when no Welcome queue item exists.
        """
        payload = build_welcome_display_payload()
        bridge_state = load_display_queue_state()
        queue_items = list(bridge_state.get("queue_items") or [])
        item_index = next(
            (
                index
                for index, item in enumerate(queue_items)
                if isinstance(item, dict) and str(item.get("source") or "").casefold() == "welcome"
            ),
            None,
        )
        if item_index is None:
            return
        queue_item = dict(queue_items[item_index])
        queue_item.update(
            {
                "text": str(payload.get("text") or ""),
                "font_value": list(payload.get("font_value")) if isinstance(payload.get("font_value"), tuple) else None,
                "background_path": str(payload.get("background_path") or "").strip(),
                "scroll_enabled": bool(payload.get("scroll_enabled")),
                "scroll_speed": int(payload.get("scroll_speed") or 0),
                "welcome_sections": payload.get("welcome_sections") if isinstance(payload.get("welcome_sections"), dict) else None,
            }
        )
        queue_items[item_index] = queue_item
        save_display_queue_state({"queue_items": queue_items})
        refresh_from_bridge = display_state.get("refresh_from_bridge") if isinstance(display_state, dict) else None
        if callable(refresh_from_bridge):
            refresh_from_bridge()

    def resolve_preview_size() -> tuple[int, int]:
        """What: Obtain a bounded preview width and height from the shared display settings.

        Why included: Preview rendering must reflect output size while retaining usable minimums.
        Where used: Preview layout and image rendering.
        Returns: Width and height in pixels.
        """
        if callable(get_preview_size):
            try:
                preview_size = get_preview_size()
            except Exception:
                preview_size = None
            if isinstance(preview_size, tuple) and len(preview_size) == 2:
                return max(180, int(preview_size[0])), max(100, int(preview_size[1]))
        return 360, 203

    def sync_preview_size(preview_container: tk.Frame, preview_label: tk.Label) -> None:
        """What: Apply the resolved preview dimensions and matching text wrap width.

        Why included: The widget frame and renderer need consistent viewport geometry.
        Where used: Background preview rendering.
        Returns: None.
        """
        preview_width, preview_height = resolve_preview_size()
        preview_container.configure(width=preview_width, height=preview_height)
        preview_label.configure(wraplength=max(120, preview_width - 24))

    def get_preview_font_value(preview_label: tk.Label) -> tuple[str, int, str]:
        """What: Resolve the selected Welcome font family, size, and style.

        Why included: Tk preview and image overlay renderers require normalized font settings.
        Where used: Preview rendering and display payload generation.
        Returns: Font family, point size, and style string.
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
        text_size_var = welcome_size_var
        override_size = int(text_size_var.get()) if isinstance(text_size_var, tk.IntVar) else 0
        font_size = override_size if override_size > 0 else int(named_font.cget("size") or 12)
        family = str(display_font_family.get() or "Segoe UI") if isinstance(display_font_family, tk.StringVar) else str(named_font.cget("family") or "Segoe UI")
        selected_style = str(display_font_style.get() or "normal").lower() if isinstance(display_font_style, tk.StringVar) else style_name
        return (family, font_size, selected_style)

    def get_current_preview_font_size(preview_label: tk.Label) -> int:
        """What: Read the effective font size from the current Welcome preview font.

        Why included: Callers need the displayed size after field overrides are applied.
        Where used: Welcome preview sizing helpers.
        Returns: Effective font size.
        """
        return int(get_preview_font_value(preview_label)[1])

    def update_font_button_label(font_button: tk.Button, preview_label: tk.Label) -> None:
        """What: Refresh all Welcome field font buttons to show their selected sizes.

        Why included: The three field controls should reflect independent size settings.
        Where used: Font restore and font-size picker callbacks.
        Returns: None.
        """
        for field, button in field_size_buttons.items():
            button.configure(text=f"Font {int(field_size_vars[field].get())}")

    def draw_preview_sections(draw, preview_width: int, preview_height: int, stroke_width: int, preview_font, line_spacing: int, fill_color: str, stroke_color: str) -> bool:
        """What: Draw combined Welcome and birthday sections with centered and left alignment.

        Why included: Combined display mode preserves distinct visual placement for each section.
        Where used: Sectioned overlay rendering for the Welcome preview.
        Returns: True when sectioned drawing handled the content; otherwise False.
        """
        sections = get_preview_text_sections()
        if not sections.get("combine_enabled"):
            return False
        centered_text = str(sections.get("centered_text") or "").strip()
        centered_detail_text = str(sections.get("centered_detail_text") or "").strip()
        detail_section_text = str(sections.get("detail_text") or "").strip()
        if not centered_detail_text and not detail_section_text:
            return False

        usable_width = max(60, preview_width - ((stroke_width + 18) * 2))
        centered_wrapped = wrap_welcome_preview_text_to_width(centered_text, preview_font, usable_width, stroke_width) if centered_text else ""
        centered_detail_wrapped = wrap_welcome_preview_text_to_width(centered_detail_text, preview_font, usable_width, stroke_width) if centered_detail_text else ""
        detail_wrapped = wrap_welcome_preview_text_to_width(detail_section_text, preview_font, usable_width, stroke_width)

        centered_box = draw.multiline_textbbox((0, 0), centered_wrapped, font=preview_font, align="center", spacing=line_spacing, stroke_width=stroke_width) if centered_wrapped else (0, 0, 0, 0)
        centered_detail_box = draw.multiline_textbbox((0, 0), centered_detail_wrapped, font=preview_font, align="center", spacing=line_spacing, stroke_width=stroke_width) if centered_detail_wrapped else (0, 0, 0, 0)
        detail_box = draw.multiline_textbbox((0, 0), detail_wrapped, font=preview_font, align="left", spacing=line_spacing, stroke_width=stroke_width)

        centered_height = float(centered_box[3] - centered_box[1]) if centered_wrapped else 0.0
        centered_detail_height = float(centered_detail_box[3] - centered_detail_box[1]) if centered_detail_wrapped else 0.0
        detail_height = float(detail_box[3] - detail_box[1])
        top_gap = float(max(line_spacing, round(max(1, int(preview_font.size if hasattr(preview_font, 'size') else 12)) * 0.56)) if centered_wrapped and centered_detail_wrapped else 0)
        middle_gap = float(line_spacing if centered_wrapped and detail_wrapped and not centered_detail_wrapped else 0)
        total_height = centered_height + top_gap + centered_detail_height + middle_gap + detail_height
        current_top = (preview_height - total_height) / 2.0

        if centered_wrapped:
            centered_x = (preview_width / 2.0) - float(centered_box[0] + centered_box[2]) / 2.0
            draw.multiline_text(
                (centered_x, current_top - float(centered_box[1])),
                centered_wrapped,
                font=preview_font,
                fill=fill_color,
                stroke_width=stroke_width,
                stroke_fill=stroke_color,
                spacing=line_spacing,
                align="center",
            )
            current_top += centered_height + top_gap

        if centered_detail_wrapped:
            centered_detail_x = (preview_width / 2.0) - float(centered_detail_box[0] + centered_detail_box[2]) / 2.0
            draw.multiline_text(
                (centered_detail_x, current_top - float(centered_detail_box[1])),
                centered_detail_wrapped,
                font=preview_font,
                fill=fill_color,
                stroke_width=stroke_width,
                stroke_fill=stroke_color,
                spacing=line_spacing,
                align="center",
            )
            current_top += centered_detail_height
        elif centered_wrapped and detail_wrapped:
            current_top += middle_gap

        left_x = float(stroke_width + 18) - float(detail_box[0])
        draw.multiline_text(
            (left_x, current_top - float(detail_box[1])),
            detail_wrapped,
            font=preview_font,
            fill=fill_color,
            stroke_width=stroke_width,
            stroke_fill=stroke_color,
            spacing=line_spacing,
            align="left",
        )
        return True

    def refresh_upcoming_birthdays(upcoming_widget: tk.Text) -> None:
        """What: Populate the read-only birthday field with people in the next seven days.

        Why included: Welcome content can include an up-to-date service birthday list.
        Where used: Initial tab setup, date changes, and recalled-content fallback.
        Returns: None.
        """
        birthday_lines = []
        for birthday_item in get_upcoming_birthdays(resolve_selected_date(), days_ahead=7):
            birthday_date = birthday_item["birthday"]
            birthday_lines.append(
                f"{birthday_date.strftime('%b %d')}, {birthday_item.get('name') or ''}, {birthday_item.get('surname') or ''}".strip(" ,")
            )
        display_text = "\n".join(birthday_lines) if birthday_lines else "No upcoming birthdays"
        upcoming_widget.configure(state="normal")
        upcoming_widget.delete("1.0", "end")
        upcoming_widget.insert("1.0", display_text)
        upcoming_widget.configure(state="disabled")

    def get_center_region_luminance(base_image) -> float:
        """What: Calculate average grayscale luminance in the center crop of an image.

        Why included: Center-region brightness informs readable overlay text colors.
        Where used: Shared text-color selection for preview overlays.
        Returns: Mean luminance from 0 (black) to 255 (white).
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
        """What: Select readable text and outline colors using the background's path metadata.

        Why included: Welcome preview colors should adapt to the displayed image.
        Where used: Scrolling preview image renderer.
        Returns: Text-fill and outline color strings.
        """
        bg_path = getattr(base_image, "_background_path", None) or str(state.get("background_path") or "")
        return choose_text_colors(base_image, background_path=bg_path)

    def fit_preview_text_for_render(base_image, preview_text: str, font_value: tuple[str, int, str]):
        """What: Wrap and size Welcome text to fit the preview or scrolling viewport.

        Why included: Text must remain readable and unclipped across image dimensions.
        Where used: render_preview_pil_image() for scrolling text overlays.
        Returns: Wrapped text, fitted Pillow font, and line spacing.
        """
        if ImageDraw is None or ImageFont is None:
            return preview_text, None, 0
        preview_width, preview_height = base_image.size
        stroke_width = get_effective_stroke_width(font_value[1], WELCOME_PREVIEW_STROKE_WIDTH)
        usable_width = max(60, preview_width - ((stroke_width + 18) * 2))
        usable_height = max(60, preview_height - ((stroke_width + 18) * 2))
        base_family, base_size, base_style = font_value
        activate_scroll_var = get_activate_scroll_var()
        if isinstance(activate_scroll_var, tk.BooleanVar) and activate_scroll_var.get():
            selected_font = get_welcome_preview_pil_font((base_family, base_size, base_style))
            if selected_font is None:
                return preview_text, None, 0
            wrapped_text = wrap_welcome_preview_text_to_width(preview_text, selected_font, usable_width, stroke_width)
            selected_spacing = max(4, round(base_size * 0.28))
            return wrapped_text, selected_font, selected_spacing
        max_size = get_welcome_preview_max_text_size(preview_height, base_size)
        min_size = 12
        fallback_text = preview_text
        fallback_font = None
        fallback_spacing = 6
        measure_draw = get_shared_welcome_measure_draw()
        if measure_draw is None:
            return preview_text, None, 0
        for candidate_size in range(max_size, min_size - 1, -2):
            candidate_stroke_width = get_effective_stroke_width(candidate_size, WELCOME_PREVIEW_STROKE_WIDTH)
            candidate_font = get_welcome_preview_pil_font((base_family, candidate_size, base_style))
            if candidate_font is None:
                continue
            candidate_text = wrap_welcome_preview_text_to_width(preview_text, candidate_font, usable_width, candidate_stroke_width)
            candidate_spacing = max(4, round(candidate_size * 0.28))
            text_box = measure_draw.multiline_textbbox(
                (0, 0),
                candidate_text,
                font=candidate_font,
                align="center",
                spacing=candidate_spacing,
                stroke_width=candidate_stroke_width,
            )
            text_width = float(text_box[2] - text_box[0])
            text_height = float(text_box[3] - text_box[1])
            fallback_text = candidate_text
            fallback_font = candidate_font
            fallback_spacing = candidate_spacing
            if text_width <= usable_width and text_height <= usable_height:
                return candidate_text, candidate_font, candidate_spacing
        return fallback_text, fallback_font, fallback_spacing

    def render_preview_pil_image(source_image, preview_text: str, font_value: tuple[str, int, str]):
        """What: Compose a background image with the current Welcome text overlay.

        Why included: The local preview must reflect the display's text layout and animation settings.
        Where used: Synchronous GIF preview and asynchronous background-image rendering.
        Returns: Rendered Pillow image, or None when image operations are unavailable.
        """
        if ImageOps is None or Image is None:
            return None
        preview_width, preview_height = resolve_preview_size()
        bg_path = str(state.get("background_path") or getattr(source_image, "_background_path", "") or "")
        is_video = Path(bg_path).suffix.lower() in WELCOME_VIDEO_EXTENSIONS
        is_animated_source = is_video or Path(bg_path).suffix.lower() == ".gif"
        fitted_cache_key = (bg_path, preview_width, preview_height)
        if not is_animated_source and bg_path and state.get("preview_fitted_key") == fitted_cache_key and state.get("preview_fitted_image") is not None:
            # Static backgrounds fit identically every frame; reuse the result during scrolling.
            rendered_image = state["preview_fitted_image"]
        else:
            rendered_image = fit_background_image(source_image, (preview_width, preview_height), video=is_video)
            if not is_animated_source and bg_path:
                state["preview_fitted_key"] = fitted_cache_key
                state["preview_fitted_image"] = rendered_image
        if bg_path:
            setattr(rendered_image, "_background_path", bg_path)
        if not preview_text.strip() or ImageDraw is None or ImageFont is None:
            return rendered_image
        if not is_preview_scroll_active():
            bounds = get_display_bounds() if callable(get_display_bounds) else {}
            render_width = int((bounds or {}).get("width") or 1920)
            render_height = int((bounds or {}).get("height") or 1080)
            fields = build_sized_text_fields("welcome", preview_text, font_value, get_preview_text_sections())
            key = (repr(fields), str(state.get("background_path") or ""), render_width, render_height, get_display_outline_width(), choose_preview_text_colors(rendered_image))
            if state.get("cached_text_overlay_key") != key:
                state["cached_text_overlay_image"] = render_text_fields_overlay(render_width, render_height, fields, base_image=rendered_image, background_path=bg_path)
                state["cached_text_overlay_key"] = key
            overlay = state.get("cached_text_overlay_image")
            if overlay is not None:
                overlay = overlay.resize((preview_width, preview_height), Image.Resampling.LANCZOS)
                return Image.alpha_composite(rendered_image.convert("RGBA"), overlay).convert("RGB")
        if is_preview_scroll_active():
            bounds = get_display_bounds() if callable(get_display_bounds) else {}
            rendered, cycle_height = render_scrolling_preview(
                rendered_image, preview_text, font_value,
                max(1, int((bounds or {}).get("width") or 1920)),
                max(1, int((bounds or {}).get("height") or 1080)),
                int(state.get("preview_scroll_offset") or 0),
                state.setdefault("preview_scroll_cache", {}),
                background_path=bg_path, size_boost=WELCOME_LAYOUT_SIZE_BOOST,
            )
            state["preview_scroll_cycle_height"] = cycle_height
            return rendered
        state["preview_scroll_cycle_height"] = 0
        display_bounds = get_display_bounds() if callable(get_display_bounds) else None
        render_width = max(1, int((display_bounds or {}).get("width") or 1920))
        render_height = max(1, int((display_bounds or {}).get("height") or 1080))
        sections = get_preview_text_sections()
        combine_enabled = bool(sections.get("combine_enabled"))
        cache_key = (
            str(state.get("background_path") or ""),
            preview_text,
            tuple(normalize_font_value(font_value)),
            render_width,
            render_height,
            combine_enabled,
            str(sections.get("centered_text") or ""),
            str(sections.get("centered_detail_text") or ""),
            str(sections.get("detail_text") or ""),
            get_display_outline_width(),
        )
        # Rebuilding this overlay involves an expensive font-size search, so cache it
        # across repeated GIF animation frames instead of redoing it every tick.
        if state.get("cached_text_overlay_key") == cache_key:
            full_overlay_image = state.get("cached_text_overlay_image")
        else:
            fitted_background = fit_background_image(source_image, (render_width, render_height), video=is_video)
            bg_path = str(state.get("background_path") or getattr(source_image, "_background_path", "") or "")
            if bg_path:
                setattr(fitted_background, "_background_path", bg_path)
            if combine_enabled:
                section_layout = fit_sectioned_overlay_layout(
                    str(sections.get("centered_text") or ""),
                    str(sections.get("centered_detail_text") or ""),
                    str(sections.get("detail_text") or ""),
                    scale_overlay_font_value(normalize_font_value(font_value), render_width, render_height),
                    render_width,
                    render_height,
                    base_image=fitted_background,
                    background_path=bg_path,
                    stroke_width=WELCOME_PREVIEW_STROKE_WIDTH,
                    padding=18,
                    max_text_height_ratio=WELCOME_PREVIEW_MAX_TEXT_HEIGHT_RATIO,
                    max_text_multiplier=WELCOME_PREVIEW_MAX_TEXT_MULTIPLIER,
                    min_text_size=12,
                    line_spacing_ratio=0.28,
                    size_boost=WELCOME_LAYOUT_SIZE_BOOST,
                )
                full_overlay_image = render_sectioned_overlay_image(
                    render_width,
                    render_height,
                    section_layout,
                    stroke_width=WELCOME_PREVIEW_STROKE_WIDTH,
                )
            else:
                layout = fit_overlay_layout(
                    preview_text,
                    scale_overlay_font_value(normalize_font_value(font_value), render_width, render_height),
                    render_width,
                    render_height,
                    scroll_enabled=False,
                    base_image=fitted_background,
                    background_path=bg_path,
                    stroke_width=WELCOME_PREVIEW_STROKE_WIDTH,
                    padding=18,
                    max_text_height_ratio=WELCOME_PREVIEW_MAX_TEXT_HEIGHT_RATIO,
                    max_text_multiplier=WELCOME_PREVIEW_MAX_TEXT_MULTIPLIER,
                    min_text_size=12,
                    line_spacing_ratio=0.28,
                    size_boost=WELCOME_LAYOUT_SIZE_BOOST,
                    corner_font_size=6,
                )
                full_overlay_image = render_overlay_image(
                    render_width,
                    render_height,
                    layout,
                    corner_text="",
                    stroke_width=WELCOME_PREVIEW_STROKE_WIDTH,
                )
            state["cached_text_overlay_key"] = cache_key
            state["cached_text_overlay_image"] = full_overlay_image
        if full_overlay_image is None:
            return rendered_image
        overlay_image = full_overlay_image.resize((preview_width, preview_height), Image.Resampling.LANCZOS)
        return Image.alpha_composite(rendered_image.convert("RGBA"), overlay_image).convert("RGB")

    def update_background_display() -> None:
        """What: Show the selected background path or the empty-state label.

        Why included: The background selector needs visible confirmation of its current choice.
        Where used: Background selection and recalled-content restore/reset.
        Returns: None.
        """
        raw_value = str(state.get("background_path") or "").strip()
        background_display_var.set(raw_value or "None")

    def render_background_preview(preview_container: tk.Frame, preview_label: tk.Label) -> None:
        """What: Load and display the current background with Welcome text in the preview widgets.

        Why included: Preview updates must handle static images, GIFs, videos, and missing Pillow support.
        Where used: Refresh scheduling, media changes, and animation callbacks.
        Returns: None; image results are applied to Tk preview widgets.
        """
        sync_preview_size(preview_container, preview_label)
        preview_width, preview_height = resolve_preview_size()
        preview_text = get_preview_text_value()
        background_path = resolve_welcome_background_path(str(state.get("background_path") or ""))
        preview_font_value = get_preview_font_value(preview_label)
        preview_label.configure(compound="center", justify="center", anchor="center", fg="#ffffff")
        is_video = background_path is not None and background_path.suffix.lower() in WELCOME_VIDEO_EXTENSIONS
        if not is_video:
            stop_preview_video(preview_label)
        if Image is None or ImageOps is None or ImageTk is None:
            clear_preview_gif_cache(preview_label)
            state["preview_photo"] = None
            preview_container.configure(bg="#000000")
            preview_label.configure(image="", text=preview_text, bg="#000000")
            return
        if background_path is None:
            clear_preview_gif_cache(preview_label)
            black_frame = Image.new("RGB", (preview_width, preview_height), "#000000")
            rendered_image = render_preview_pil_image(black_frame, preview_text, preview_font_value)
            if rendered_image is None:
                state["preview_photo"] = None
                preview_container.configure(bg="#000000")
                preview_label.configure(image="", text=preview_text, bg="#000000")
                return
            preview_photo = ImageTk.PhotoImage(rendered_image)
            state["preview_photo"] = preview_photo
            preview_container.configure(bg="#000000")
            preview_label.configure(image=preview_photo, text="", bg="#000000")
            return
        if background_path.suffix.lower() == ".gif" and ensure_preview_gif_cache(background_path, preview_label):
            preview_reader = state.get("preview_gif_reader")
            frame_count = int(state.get("preview_gif_frame_count") or 0)
            if preview_reader is not None and frame_count > 0:
                frame_index = int(state.get("preview_gif_frame_index") or 0) % frame_count
                try:
                    preview_reader.seek(frame_index)
                    preview_source = preview_reader.copy()
                except EOFError:
                    preview_reader.seek(0)
                    preview_source = preview_reader.copy()
                    state["preview_gif_frame_index"] = 0
                rendered_image = render_preview_pil_image(preview_source, preview_text, preview_font_value)
                if rendered_image is None:
                    state["preview_photo"] = None
                    preview_container.configure(bg="#000000")
                    preview_label.configure(image="", text=preview_text, bg="#000000")
                    return
                preview_photo = ImageTk.PhotoImage(rendered_image)
                state["preview_photo"] = preview_photo
                preview_container.configure(bg="#000000")
                preview_label.configure(image=preview_photo, text="", bg="#000000")
                if state.get("preview_gif_after_id") is None:
                    schedule_preview_gif_animation(preview_container, preview_label)
                return
        clear_preview_gif_cache(preview_label)
        if not is_video:
            preview_label.configure(image="", text="Loading preview...", bg="#000000")

        def render_worker() -> object:
            """What: Load the chosen background file and render it without touching Tk widgets.

            Why included: Image decoding and composition run in the background task worker.
            Where used: start_async_preview_render() invoked by render_background_preview().
            Returns: Rendered image object, or None if loading fails.
            """
            try:
                if is_video:
                    if imageio is None:
                        return None
                    with state["preview_video_lock"]:
                        path_str = str(background_path)
                        if state.get("preview_video_path") != path_str or state.get("preview_video_reader") is None:
                            old_reader = state.get("preview_video_reader")
                            if old_reader is not None:
                                old_reader.close()
                            reader = LoopingVideoReader(path_str)
                            state["preview_video_reader"] = reader
                            state["preview_video_path"] = path_str
                            state["preview_video_started"] = time.monotonic()
                            state["preview_video_fps"] = reader.fps
                        frame_data, _frame_index = state["preview_video_reader"].frame_at_time(time.monotonic())
                        preview_source = Image.fromarray(frame_data)
                else:
                    path_str = str(background_path)
                    cached_source = state.get("preview_source_cache")
                    if isinstance(cached_source, tuple) and cached_source[0] == path_str:
                        # Scroll/animation frames must not re-decode the full-size background.
                        preview_source = cached_source[1]
                    else:
                        with Image.open(background_path) as source_image:
                            preview_source = source_image.copy()
                        state["preview_source_cache"] = (path_str, preview_source)
            except Exception:
                return None
            return render_preview_pil_image(preview_source, preview_text, preview_font_value)

        def apply_result(rendered_image: object) -> None:
            """What: Apply an asynchronous render result to the Tk preview label.

            Why included: Tk image objects and widget mutations must be created on the UI thread.
            Where used: Completion callback passed to start_async_preview_render().
            Returns: None.
            """
            if rendered_image is None or ImageTk is None:
                state["preview_photo"] = None
                preview_container.configure(bg="#000000")
                preview_label.configure(image="", text=preview_text, bg="#000000")
                return
            preview_photo = ImageTk.PhotoImage(rendered_image)
            state["preview_photo"] = preview_photo
            preview_container.configure(bg="#000000")
            preview_label.configure(image=preview_photo, text="", bg="#000000")

            if is_video and state.get("preview_video_after_id") is None:
                def advance_video() -> None:
                    """What: Request the next Welcome video preview frame.

                    Why included: Motion should continue only while the selected tab/source is current.
                    Where used: Timer scheduled after an asynchronous video render completes.
                    Returns: None; decodes the next frame through the existing worker.
                    """
                    state["preview_video_after_id"] = None
                    if is_welcome_tab_active() and resolve_welcome_background_path(str(state.get("background_path") or "")) == background_path:
                        render_background_preview(preview_container, preview_label)

                state["preview_video_after_id"] = preview_label.after(67, advance_video)

        start_async_preview_render(
            widget=preview_label,
            state=state,
            request_key="preview_render_request_id",
            queue_key="preview_render_queue",
            is_active=is_welcome_tab_active,
            render_worker=render_worker,
            apply_result=apply_result,
        )

    def refresh_preview(preview_container: tk.Frame, preview_label: tk.Label, welcome_text: tk.Text) -> None:
        """What: Refresh the visible Welcome preview and its scroll animation.

        Why included: Text, font, background, and scroll changes need a synchronized render.
        Where used: Debounced edits and direct preview control updates.
        Returns: None.
        """
        if not is_welcome_tab_active():
            pause_preview_activity(preview_label)
            return
        if not is_preview_scroll_active():
            state["preview_scroll_offset"] = 0
        render_background_preview(preview_container, preview_label)
        schedule_preview_scroll_animation(preview_container, preview_label)

    def schedule_preview_refresh(preview_container: tk.Frame, preview_label: tk.Label, welcome_text: tk.Text, delay_ms: int = 60) -> None:
        """What: Debounce a Welcome preview refresh through the Tk event loop.

        Why included: Rapid edits should coalesce into one render and inactive tabs should stay idle.
        Where used: Text/date/settings traces and preview geometry changes.
        Returns: None; stores the scheduled callback identifier in preview state.
        """
        cancel_preview_refresh(preview_label)
        if not is_welcome_tab_active():
            return

        def run_refresh() -> None:
            """What: Clear the pending refresh identifier and run the preview update.

            Why included: The delayed callback delegates rendering only after the debounce expires.
            Where used: after() callback created by schedule_preview_refresh().
            Returns: None.
            """
            state["preview_refresh_after_id"] = None
            refresh_preview(preview_container, preview_label, welcome_text)

        state["preview_refresh_after_id"] = preview_label.after(max(1, int(delay_ms)), run_refresh)

    def sync_scroll_speed_state(scroll_speed_widget: tk.Widget) -> None:
        """What: Enable or disable the scroll-speed widget from the scroll toggle.

        Why included: Speed has no effect when Welcome scrolling is inactive.
        Where used: Scroll setting traces and restored Welcome state.
        Returns: None.
        """
        activate_scroll_var = get_activate_scroll_var()
        scroll_enabled = bool(activate_scroll_var.get()) if isinstance(activate_scroll_var, tk.BooleanVar) else False
        next_state = "normal" if scroll_enabled else "disabled"
        try:
            scroll_speed_widget.configure(state=next_state)
        except tk.TclError:
            pass

    def normalize_scroll_speed_value() -> None:
        """What: Clamp the shared scroll-speed variable to the supported range.

        Why included: Persisted or typed values must remain valid for preview scheduling.
        Where used: Scroll-speed variable trace and tab initialization.
        Returns: None.
        """
        scroll_speed_var = get_scroll_speed_var()
        if not isinstance(scroll_speed_var, tk.IntVar):
            return
        try:
            current_value = int(scroll_speed_var.get())
        except Exception:
            current_value = WELCOME_SCROLL_SPEED_DEFAULT
        scroll_speed_var.set(max(WELCOME_SCROLL_SPEED_MIN, min(WELCOME_SCROLL_SPEED_MAX, current_value)))

    def choose_welcome_font_size(field: str) -> None:
        """What: Open the text-size picker for one Welcome text field.

        Why included: Welcome, birthday, and upcoming fields use independently selectable sizes.
        Where used: Font-size buttons for each Welcome text field.
        Returns: None.
        """
        def apply_selected_size(_selected_field: str, size: int) -> None:
            """What: Apply the selected size and refresh section preview and queue state.

            Why included: Font-picker results need to update the chosen field and visible output.
            Where used: Selection callback supplied to open_text_size_picker().
            Returns: None.
            """
            field_size_vars[field].set(size)
            if field != "Welcome Note":
                combine_text_var = get_combine_text_var()
                if isinstance(combine_text_var, tk.BooleanVar):
                    combine_text_var.set(True)
            update_font_button_label(font_button, preview_label)
            refresh_preview(preview_container, preview_label, welcome_text)
            update_queued_welcome_item()

        open_text_size_picker(tab_frame.winfo_toplevel(), {field: int(field_size_vars[field].get())}, apply_layout_style, apply_selected_size)

    def apply_field_alignment(field: str, alignment: str) -> None:
        """What: Store a field's alignment and refresh the preview and queued item.

        Why included: Each Welcome section can be aligned independently in combined mode.
        Where used: Alignment OptionMenu callbacks.
        Returns: None.
        """
        field_alignment_vars[field].set(alignment)
        if field != "Welcome Note":
            combine_text_var = get_combine_text_var()
            if isinstance(combine_text_var, tk.BooleanVar):
                combine_text_var.set(True)
        refresh_preview(preview_container, preview_label, welcome_text)
        update_queued_welcome_item()

    def choose_welcome_background() -> None:
        """What: Open the shared background picker for Welcome media.

        Why included: Users can choose a still image, GIF, or supported video background.
        Where used: Welcome Background button command.
        Returns: None.
        """
        def apply_selected_background(selected_value: str) -> None:
            """What: Store the picked background and redraw the Welcome preview.

            Why included: Picker selection must update both visible path and rendered output.
            Where used: on_apply callback passed to open_background_picker().
            Returns: None.
            """
            state["background_path"] = selected_value
            update_background_display()
            refresh_preview(preview_container, preview_label, welcome_text)

        open_background_picker(
            parent_window=tab_frame.winfo_toplevel(),
            dialog_title="Select Welcome Background",
            empty_message="No background files were found in the Backgrounds folder.",
            picker_heading="Welcome Background Files",
            choices=[],
            current_background=str(state.get("background_path") or "").strip(),
            resolve_background_path=resolve_welcome_background_path,
            video_extensions=WELCOME_VIDEO_EXTENSIONS,
            apply_layout_style=apply_layout_style,
            on_apply=apply_selected_background,
        )

    for column_index, column_weight in enumerate(WELCOME_TAB_COLUMN_WEIGHTS):
        tab_frame.columnconfigure(column_index, weight=column_weight)
    tab_frame.rowconfigure(0, weight=0)
    tab_frame.rowconfigure(1, weight=1)

    display_actions = tk.Frame(tab_frame)
    display_actions.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
    tab_widgets.append(display_actions)
    apply_layout_style([display_actions])

    for column_index in range(3):
        column_frame = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
        column_frame.grid(row=1, column=column_index, sticky="nsew", padx=8, pady=12)
        column_frame.columnconfigure(0, weight=1)
        if column_index == 0:
            background_button = tk.Button(display_actions, text="Background")
            background_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
            background_display_var = tk.StringVar(value="None")
            background_display = tk.Label(column_frame, textvariable=background_display_var, anchor="w", justify="left", relief="solid", bd=1, padx=6, pady=4)
            background_display.grid(row=1, column=0, sticky="ew", pady=(12, 0))
            welcome_field_row = tk.Frame(column_frame)
            welcome_field_row.grid(row=2, column=0, sticky="ew", pady=(12, 0))
            welcome_field_row.columnconfigure(0, weight=1)
            title = tk.Label(welcome_field_row, text="Welcome Note", justify="left", anchor="w")
            title.grid(row=0, column=0, sticky="w")
            font_button = tk.Button(welcome_field_row, text=f"Font {welcome_size_var.get()}")
            font_button.grid(row=0, column=1, sticky="e")
            field_size_buttons["Welcome Note"] = font_button
            welcome_alignment = tk.OptionMenu(welcome_field_row, field_alignment_vars["Welcome Note"], "left", "center", "right", command=lambda value: apply_field_alignment("Welcome Note", value))
            welcome_alignment.grid(row=0, column=2, sticky="e", padx=(6, 0))
            welcome_text = tk.Text(column_frame, height=5, wrap="word")
            welcome_text.grid(row=3, column=0, sticky="ew")
            send_button = tk.Button(display_actions, text="Send to Display")
            send_button.grid(row=0, column=0, sticky="w")
            combine_checkbox = tk.Checkbutton(column_frame, text="Combine Birthdays With Welcome Text", variable=get_combine_text_var())
            combine_checkbox.grid(row=5, column=0, sticky="w", pady=(8, 0))
            scroll_row = tk.Frame(column_frame)
            scroll_row.grid(row=6, column=0, sticky="ew", pady=(8, 0))
            scroll_row.columnconfigure(1, weight=1)
            scroll_checkbox = tk.Checkbutton(scroll_row, text="Activate Scroll", variable=get_activate_scroll_var())
            scroll_checkbox.grid(row=0, column=0, sticky="w")
            scroll_speed_display_var = tk.StringVar(value=str(WELCOME_SCROLL_SPEED_DEFAULT))
            scroll_speed = tk.Scale(scroll_row, from_=WELCOME_SCROLL_SPEED_MIN, to=WELCOME_SCROLL_SPEED_MAX, orient="horizontal", variable=get_scroll_speed_var(), showvalue=False, resolution=1)
            scroll_speed.grid(row=0, column=1, sticky="ew", padx=(8, 0))
            scroll_speed_display = tk.Label(scroll_row, textvariable=scroll_speed_display_var, width=3, justify="center", anchor="center")
            scroll_speed_display.grid(row=0, column=2, sticky="e", padx=(8, 0))
            birthday_field_row = tk.Frame(column_frame)
            birthday_field_row.grid(row=7, column=0, sticky="ew", pady=(12, 0))
            birthday_field_row.columnconfigure(0, weight=1)
            birthday_label = tk.Label(birthday_field_row, text="Birthday Message", justify="left", anchor="w")
            birthday_label.grid(row=0, column=0, sticky="w")
            birthday_size_button = tk.Button(birthday_field_row, text=f"Font {birthday_size_var.get()}")
            birthday_size_button.grid(row=0, column=1, sticky="e")
            field_size_buttons["Birthday Message"] = birthday_size_button
            birthday_alignment = tk.OptionMenu(birthday_field_row, field_alignment_vars["Birthday Message"], "left", "center", "right", command=lambda value: apply_field_alignment("Birthday Message", value))
            birthday_alignment.grid(row=0, column=2, sticky="e", padx=(6, 0))
            birthday_text = tk.Text(column_frame, height=2, wrap="word")
            birthday_text.grid(row=8, column=0, sticky="ew")
            upcoming_field_row = tk.Frame(column_frame)
            upcoming_field_row.grid(row=9, column=0, sticky="ew", pady=(12, 0))
            upcoming_field_row.columnconfigure(0, weight=1)
            upcoming_label = tk.Label(upcoming_field_row, text="Upcoming Birthdays", justify="left", anchor="w")
            upcoming_label.grid(row=0, column=0, sticky="w")
            upcoming_size_button = tk.Button(upcoming_field_row, text=f"Font {upcoming_size_var.get()}")
            upcoming_size_button.grid(row=0, column=1, sticky="e")
            field_size_buttons["Upcoming Birthdays"] = upcoming_size_button
            upcoming_alignment = tk.OptionMenu(upcoming_field_row, field_alignment_vars["Upcoming Birthdays"], "left", "center", "right", command=lambda value: apply_field_alignment("Upcoming Birthdays", value))
            upcoming_alignment.grid(row=0, column=2, sticky="e", padx=(6, 0))
            upcoming_text = tk.Text(column_frame, height=8, wrap="word")
            upcoming_text.grid(row=10, column=0, sticky="nsew")
            upcoming_text.configure(state="disabled")
            alignment_controls = [welcome_alignment, birthday_alignment, upcoming_alignment]
            tab_widgets.extend([column_frame, welcome_field_row, font_button, welcome_alignment, background_button, background_display, title, welcome_text, send_button, combine_checkbox, scroll_row, scroll_checkbox, scroll_speed, scroll_speed_display, birthday_field_row, birthday_size_button, birthday_alignment, birthday_label, birthday_text, upcoming_field_row, upcoming_size_button, upcoming_alignment, upcoming_label, upcoming_text])
            tab_labels.extend([title, birthday_label, upcoming_label, scroll_speed_display])
            alignment_menus = [control["menu"] for control in alignment_controls]
            tab_inputs.extend([font_button, birthday_size_button, upcoming_size_button, *alignment_controls, *alignment_menus, background_button, welcome_text, send_button, combine_checkbox, scroll_checkbox, scroll_speed, birthday_text, upcoming_text])
            apply_layout_style([column_frame, welcome_field_row, birthday_field_row, upcoming_field_row, background_display, welcome_text, scroll_row, birthday_text, upcoming_text, font_button, birthday_size_button, upcoming_size_button, *alignment_controls, *alignment_menus, background_button, send_button, combine_checkbox, scroll_checkbox, scroll_speed], label_widgets=[title, birthday_label, upcoming_label], input_widgets=[font_button, birthday_size_button, upcoming_size_button, *alignment_controls, *alignment_menus, background_button, welcome_text, send_button, combine_checkbox, scroll_checkbox, scroll_speed, birthday_text, upcoming_text])
        elif column_index == 1:
            column_frame.rowconfigure(2, weight=1)
            title = tk.Label(column_frame, text="Welcome Notes", justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            note_title_entry = tk.Entry(column_frame, textvariable=note_title_var)
            note_title_entry.grid(row=1, column=0, sticky="ew", pady=(0, 8))
            welcome_note_list = tk.Listbox(column_frame, height=8, exportselection=False)
            welcome_note_list.grid(row=2, column=0, sticky="nsew")
            note_actions = tk.Frame(column_frame)
            note_actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
            note_actions.columnconfigure(0, weight=1)
            note_actions.columnconfigure(1, weight=1)
            note_actions.columnconfigure(2, weight=1)
            new_note_button = tk.Button(note_actions, text="New")
            new_note_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
            save_note_button = tk.Button(note_actions, text="Save")
            save_note_button.grid(row=0, column=1, sticky="ew", padx=4)
            delete_note_button = tk.Button(note_actions, text="Delete")
            delete_note_button.grid(row=0, column=2, sticky="ew", padx=(4, 0))
            tab_widgets.extend([column_frame, title, note_title_entry, welcome_note_list, note_actions, new_note_button, save_note_button, delete_note_button])
            tab_labels.append(title)
            tab_inputs.extend([note_title_entry, welcome_note_list, new_note_button, save_note_button, delete_note_button])
            apply_layout_style([column_frame, note_title_entry, welcome_note_list, note_actions, new_note_button, save_note_button, delete_note_button], label_widgets=[title], input_widgets=[note_title_entry, welcome_note_list, new_note_button, save_note_button, delete_note_button])
        elif column_index == 2:
            title = tk.Label(column_frame, text="Preview", justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew")
            preview_container, preview_label = build_preview_placeholder(column_frame, "Welcome Preview")
            preview_container.grid(row=1, column=0, sticky="n", pady=(8, 0))
            tab_widgets.extend([column_frame, title, preview_container, preview_label])
            tab_labels.extend([title, preview_label])
            apply_layout_style([column_frame, preview_container], label_widgets=[title, preview_label])
        else:
            tab_widgets.append(column_frame)
            apply_layout_style([column_frame])

    sync_preview_size(preview_container, preview_label)
    update_background_display()
    update_font_button_label(font_button, preview_label)
    normalize_scroll_speed_value()

    def update_scroll_speed_display() -> None:
        """What: Display the clamped current scroll-speed value beside its slider.

        Why included: The slider hides its native numeric value for a compact control row.
        Where used: Initialization and scroll-speed variable trace.
        Returns: None.
        """
        scroll_speed_var = get_scroll_speed_var()
        speed_value = int(scroll_speed_var.get() or WELCOME_SCROLL_SPEED_DEFAULT) if isinstance(scroll_speed_var, tk.IntVar) else WELCOME_SCROLL_SPEED_DEFAULT
        scroll_speed_display_var.set(str(max(WELCOME_SCROLL_SPEED_MIN, min(WELCOME_SCROLL_SPEED_MAX, speed_value))))

    update_scroll_speed_display()
    sync_scroll_speed_state(scroll_speed)
    refresh_preview(preview_container, preview_label, welcome_text)
    refresh_upcoming_birthdays(upcoming_text)

    selected_date_var = get_selected_date_var()
    if isinstance(selected_date_var, tk.StringVar):
        selected_date_var.trace_add("write", lambda *_args: (refresh_upcoming_birthdays(upcoming_text), schedule_preview_refresh(preview_container, preview_label, welcome_text)))

    combine_text_var = get_combine_text_var()
    if isinstance(combine_text_var, tk.BooleanVar):
        combine_text_var.trace_add("write", lambda *_args: schedule_preview_refresh(preview_container, preview_label, welcome_text))

    activate_scroll_var = get_activate_scroll_var()
    if isinstance(activate_scroll_var, tk.BooleanVar):
        activate_scroll_var.trace_add("write", lambda *_args: (sync_scroll_speed_state(scroll_speed), schedule_preview_refresh(preview_container, preview_label, welcome_text)))

    scroll_speed_var = get_scroll_speed_var()
    if isinstance(scroll_speed_var, tk.IntVar):
        scroll_speed_var.trace_add("write", lambda *_args: (normalize_scroll_speed_value(), update_scroll_speed_display(), schedule_preview_refresh(preview_container, preview_label, welcome_text)))

    def handle_welcome_text_modified(_event=None) -> None:
        """What: Clear the Welcome Text modified flag and schedule a preview refresh.

        Why included: Tk emits repeated modified events until the flag is reset.
        Where used: <<Modified>> binding on the Welcome Note text widget.
        Returns: None.
        """
        if welcome_text.edit_modified():
            welcome_text.edit_modified(False)
        schedule_preview_refresh(preview_container, preview_label, welcome_text)

    def handle_birthday_text_modified(_event=None) -> None:
        """What: Clear the birthday Text modified flag and schedule a preview refresh.

        Why included: Birthday message edits must update the combined preview without retrigger loops.
        Where used: <<Modified>> binding on the Birthday Message text widget.
        Returns: None.
        """
        if birthday_text.edit_modified():
            birthday_text.edit_modified(False)
        schedule_preview_refresh(preview_container, preview_label, welcome_text)

    def queue_welcome_for_display() -> None:
        """What: Validate current Welcome content and add it to the shared Display queue.

        Why included: Completed Welcome messages need a persistent, selectable display queue entry.
        Where used: send_welcome_to_display() button command.
        Returns: None; reports empty content or asynchronous write errors with dialogs.
        """
        payload = build_welcome_display_payload()
        text_value = str(payload.get("text") or "").strip()
        if not text_value or text_value == "Welcome Preview":
            messagebox.showinfo("Welcome", "Enter welcome content before sending to Display.", parent=tab_frame.winfo_toplevel())
            return
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        queue_item = {
            "id": f"welcome-{timestamp}",
            "source": "welcome",
            "title": "Welcome",
            "subtitle": "Welcome Message",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "text": text_value,
            "font_value": list(payload.get("font_value")) if isinstance(payload.get("font_value"), tuple) else None,
            "background_path": str(payload.get("background_path") or "").strip(),
            "scroll_enabled": bool(payload.get("scroll_enabled")),
            "scroll_speed": int(payload.get("scroll_speed") or 0),
            "welcome_sections": payload.get("welcome_sections") if isinstance(payload.get("welcome_sections"), dict) else None,
        }

        def write_welcome_item() -> bool:
            """What: Persist the prepared Welcome item using the display bridge.

            Why included: Queue writes run through the same bridge as other display sources.
            Where used: Background task worker or synchronous queue fallback.
            Returns: True for an updated existing row, False for a new row.
            """
            result = upsert_display_queue_item(queue_item)
            return not any(entry.get("id") == queue_item["id"] for entry in result["queue_items"])

        def refresh_display_after_welcome_write(was_updated: bool = False) -> None:
            """What: Refresh the Display tab after the Welcome queue write completes.

            Why included: The shared queue view must reflect newly persisted content.
            Where used: Background task success callback and synchronous fallback.
            Returns: None.
            """
            refresh_from_bridge = display_state.get("refresh_from_bridge") if isinstance(display_state, dict) else None
            if callable(refresh_from_bridge):
                refresh_from_bridge()
            messagebox.showinfo("Welcome", "Welcome note updated in Display." if was_updated else "Welcome note added to Display.", parent=tab_frame.winfo_toplevel())

        if callable(start_background_task):
            start_background_task(
                "WelcomeQueueWriter",
                write_welcome_item,
                on_success=refresh_display_after_welcome_write,
                on_error=lambda error: messagebox.showerror("Welcome", f"Unable to send welcome content to Display.\n\n{error}", parent=tab_frame.winfo_toplevel()),
            )
            return

        refresh_display_after_welcome_write(write_welcome_item())

    def send_welcome_to_display() -> None:
        """What: Disable automatic-send mode and submit the current Welcome content.

        Why included: Explicit send should enqueue exactly one item rather than enabling live updates.
        Where used: Send to Display button command.
        Returns: None.
        """
        send_to_display_var = get_send_to_display_var()
        if isinstance(send_to_display_var, tk.BooleanVar):
            send_to_display_var.set(False)
        queue_welcome_for_display()

    def refresh_welcome_notes() -> None:
        """What: Load saved note records and repopulate the Welcome notes listbox.

        Why included: The editor needs an up-to-date selector for persisted templates.
        Where used: Initial tab setup, save/delete actions, and Welcome tab activation.
        Returns: None.
        """
        if welcome_note_list is None:
            return
        notes = load_welcome_notes()
        state["welcome_notes"] = notes
        welcome_note_list.delete(0, tk.END)
        for note in notes:
            welcome_note_list.insert(tk.END, str(note.get("title") or "Untitled"))

    def select_welcome_note(_event=None) -> None:
        """What: Load the selected saved note into the editor and restore its settings.

        Why included: Users can reuse saved message text and its associated layout options.
        Where used: <<ListboxSelect>> event on the Welcome notes list.
        Returns: None.
        """
        if welcome_note_list is None:
            return
        selection = welcome_note_list.curselection()
        if not selection:
            return
        notes = list(state.get("welcome_notes") or [])
        index = int(selection[0])
        if index >= len(notes):
            return
        note = notes[index]
        state["selected_welcome_note_id"] = int(note.get("id") or 0)
        note_title_var.set(str(note.get("title") or ""))
        welcome_text.delete("1.0", tk.END)
        welcome_text.insert("1.0", str(note.get("note_text") or ""))
        welcome_text.edit_modified(False)
        try:
            saved_settings = json.loads(str(note.get("settings_json") or "{}"))
        except (TypeError, ValueError):
            saved_settings = {}
        if not isinstance(saved_settings, dict):
            saved_settings = {}
        apply_restored_welcome_entry({"queue_items": [dict(saved_settings, source="welcome", text=str(note.get("note_text") or ""))]})
        schedule_preview_refresh(preview_container, preview_label, welcome_text, delay_ms=1)

    def clear_welcome_note_editor() -> None:
        """What: Clear the active note selection, title, text, and pending preview.

        Why included: New Note begins an independent template without editing the previous one.
        Where used: New Note button command and successful note deletion.
        Returns: None.
        """
        state["selected_welcome_note_id"] = None
        note_title_var.set("")
        welcome_text.delete("1.0", tk.END)
        welcome_text.edit_modified(False)
        schedule_preview_refresh(preview_container, preview_label, welcome_text, delay_ms=1)

    def save_current_welcome_note() -> None:
        """What: Validate the note title and persist current Welcome text/settings.

        Why included: Reusable messages need their display configuration saved alongside text.
        Where used: Save Note button command.
        Returns: None; displays a warning if the required title is blank.
        """
        title = note_title_var.get().strip()
        text = welcome_text.get("1.0", "end-1c").strip()
        if not title:
            messagebox.showwarning("Welcome Notes", "Enter a title first.", parent=tab_frame.winfo_toplevel())
            return
        note_id = save_welcome_note(state.get("selected_welcome_note_id"), title, text, settings=build_welcome_display_payload())
        state["selected_welcome_note_id"] = note_id
        refresh_welcome_notes()

    def delete_current_welcome_note() -> None:
        """What: Delete the currently selected saved note and clear the editor.

        Why included: Obsolete templates should be removable from the Welcome note library.
        Where used: Delete Note button command.
        Returns: None; does nothing when there is no valid selection.
        """
        note_id = state.get("selected_welcome_note_id")
        if not isinstance(note_id, int) or note_id <= 0:
            return
        delete_welcome_note(note_id)
        clear_welcome_note_editor()
        refresh_welcome_notes()

    welcome_text.edit_modified(False)
    birthday_text.edit_modified(False)
    welcome_text.bind("<<Modified>>", handle_welcome_text_modified, add="+")
    birthday_text.bind("<<Modified>>", handle_birthday_text_modified, add="+")
    send_button.configure(command=send_welcome_to_display)
    font_button.configure(command=lambda: choose_welcome_font_size("Welcome Note"))
    birthday_size_button.configure(command=lambda: choose_welcome_font_size("Birthday Message"))
    upcoming_size_button.configure(command=lambda: choose_welcome_font_size("Upcoming Birthdays"))
    if welcome_note_list is not None:
        welcome_note_list.bind("<<ListboxSelect>>", select_welcome_note, add="+")
    new_note_button.configure(command=clear_welcome_note_editor)
    save_note_button.configure(command=save_current_welcome_note)
    delete_note_button.configure(command=delete_current_welcome_note)
    refresh_welcome_notes()
    background_button.configure(command=choose_welcome_background)
    preview_container.bind("<Configure>", lambda _event: schedule_preview_refresh(preview_container, preview_label, welcome_text), add="+")
    preview_label.bind("<Destroy>", lambda _event: pause_preview_activity(preview_label), add="+")

    def handle_notebook_tab_changed(_event=None) -> None:
        """What: Refresh Welcome notes and preview on activation, or pause previews when hidden.

        Why included: Background rendering and animation should follow notebook visibility.
        Where used: <<NotebookTabChanged>> binding on the parent notebook.
        Returns: None.
        """
        if not isinstance(notebook, ttk.Notebook):
            return
        if is_welcome_tab_active():
            refresh_welcome_notes()
            schedule_preview_refresh(preview_container, preview_label, welcome_text, delay_ms=1)
            return
        pause_preview_activity(preview_label)

    if isinstance(notebook, ttk.Notebook):
        notebook.bind("<<NotebookTabChanged>>", handle_notebook_tab_changed, add="+")

    if isinstance(welcome_state, dict):
        welcome_state["restore_service_entry"] = apply_restored_welcome_entry
        welcome_state["clear_recalled_data"] = clear_recalled_welcome_entry
        welcome_state["refresh_preview"] = lambda: refresh_preview(preview_container, preview_label, welcome_text)