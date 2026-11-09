"""Build the local secondary-screen preview and forward live content to OBS.

This module owns preview animations, fitted text overlays, and the handoff to
the separate OBS-backed output process; it does not implement OBS itself.
"""

from __future__ import annotations

import ctypes
import functools
import queue
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from obs_secondary_display import create_obs_secondary_mirror_window
from overlay_layout import SECONDARY_LAYOUT_SIZE_BOOST, build_sized_text_fields, choose_text_colors, fit_overlay_layout, fit_sectioned_overlay_layout, get_display_key_size, get_display_outline_width, get_effective_stroke_width, get_pil_font, is_display_key_hidden, normalize_font_value, render_bible_columns_frame, render_overlay_image, render_sectioned_overlay_image, render_text_fields_overlay, scale_overlay_font_value
from music_licensing import format_music_license_attribution, normalize_license_provider
from overlay_layout import render_scrolling_preview
from perf_logging import log_error, log_exception, log_timing
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


DEFAULT_BACKGROUND_PATH = Path("Resourses/PewVista.png")
PREVIEW_STROKE_WIDTH = 1
SCROLL_SPEED_DEFAULT = 7
SCROLL_SPEED_MIN = 1
SCROLL_SPEED_MAX = 20
PREVIEW_MAX_TEXT_HEIGHT_RATIO = 0.08
PREVIEW_MAX_TEXT_MULTIPLIER = 2
SECTION_PREVIEW_MAX_WIDTH = 300
SECTION_PREVIEW_MAX_HEIGHT = 160
WELCOME_LAYOUT_SIZE_BOOST = 1.15
PREVIEW_SCROLL_FRAME_MS = 40
LIVE_SONG_METADATA_MS = 3000
MIRROR_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpg", ".mpeg"}


def fit_section_preview_size(preview_width: int, preview_height: int) -> tuple[int, int]:
    """Clamp requested preview dimensions to the shared mirror-panel limits.

    What: Converts dimensions to integers and applies minimum/maximum width and height.
    Why included: The preview needs stable layout bounds even when monitor aspect ratios
    or window geometry produce very small/large requested sizes.
    Where used: MirrorDisplayController preview sizing.
    Returns: Bounded width and height; this helper does not distort an image itself.
    """
    bounded_width = max(180, min(SECTION_PREVIEW_MAX_WIDTH, int(preview_width)))
    bounded_height = max(100, min(SECTION_PREVIEW_MAX_HEIGHT, int(preview_height)))
    return bounded_width, bounded_height


def normalize_font_value(font_value: object) -> tuple[str, int, str] | None:
    """Validate serialized font data and normalize it to a renderer tuple.

    What: Accepts family/size/style sequences and safely converts values to strings
    and an integer size.
    Why included: Queue payloads may contain old or malformed font settings.
    Where used: Mirror payload signatures, previews, and secondary-output updates.
    Returns: Family/size/style tuple, or None for invalid input.
    """
    if not isinstance(font_value, (list, tuple)) or len(font_value) < 2:
        return None
    try:
        return (
            str(font_value[0] or "Segoe UI"),
            int(font_value[1] or 12),
            str(font_value[2] or "normal") if len(font_value) > 2 else "normal",
        )
    except Exception:
        return None


def resolve_preview_font_path(font_family: str, font_style: str) -> Path | None:
    """Resolve a supported Windows family/style to its installed font file.

    What: Looks up the family/style filename in the preview renderer's font map.
    Why included: Pillow text measurements should use the same installed face as Tk.
    Where used: get_preview_pil_font() before loading its cached font.
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
def _load_preview_pil_font_cached(font_path_str: str | None, size: int):
    """What: Load a TrueType preview font, falling back to Pillow's default face.

    Why included: Fitting repeatedly requests identical font files and sizes.
    Where used: get_preview_pil_font() through the bounded LRU cache.
    Returns: Pillow font instance; default font is used when the path fails.
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
    """Load the cached Pillow font for a preview font tuple.

    What: Validates Pillow and tuple input, clamps size to ten pixels or larger,
    resolves the Windows font path, and requests the cached face.
    Why included: Preview measurement/rendering needs repeatable installed-font metrics.
    Where used: Preview text fitting and image composition methods.
    Returns: Pillow font object, or None when Pillow/input is unavailable.
    """
    if ImageFont is None or not isinstance(font_value, tuple) or len(font_value) < 2:
        return None
    family = str(font_value[0])
    size = max(10, abs(int(font_value[1])))
    style = str(font_value[2]) if len(font_value) > 2 else "normal"
    font_path = resolve_preview_font_path(family, style)
    return _load_preview_pil_font_cached(str(font_path) if font_path else None, size)


_MIRROR_MEASURE_DRAW = None


def get_shared_mirror_measure_draw():
    """Return a lazily created Pillow draw surface for preview text measurements.

    What: Creates and retains a transparent 1x1 RGBA image and its ImageDraw object.
    Why included: Repeated text-width measurements should not allocate new images.
    Where used: wrap_preview_text_to_width() and preview fitting.
    Returns: Shared ImageDraw instance, or None when Pillow is unavailable.
    """
    global _MIRROR_MEASURE_DRAW
    if _MIRROR_MEASURE_DRAW is None and Image is not None and ImageDraw is not None:
        _MIRROR_MEASURE_DRAW = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
    return _MIRROR_MEASURE_DRAW


def wrap_preview_text_to_width(text: str, pil_font, max_width: int, stroke_width: int) -> str:
    """Wrap preview text to a measured pixel width, including outline thickness.

    What: Splits nonempty paragraphs at word boundaries while preserving blank lines.
    Why included: Preview lines must match actual font metrics and avoid clipping.
    Where used: fit_preview_text_for_render() for scrollable preview content.
    Returns: Newline-separated wrapped text; input is unchanged when measuring is unavailable.
    """
    if Image is None or ImageDraw is None or pil_font is None or max_width <= 0:
        return text
    measure_draw = get_shared_mirror_measure_draw()
    if measure_draw is None:
        return text

    def measure_width(value: str) -> float:
        """What: Measure one candidate line in pixels including its outline stroke.

        Why included: Wrapping must account for the visible edge, not glyph advance alone.
        Where used: wrap_preview_text_to_width() while adding words to a line.
        Returns: Measured width, or zero for empty text.
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


class MirrorDisplayController:
    """Coordinate the local Tk preview and external OBS-backed display output.

    What: Owns preview widgets, media/text caches, animation timers, payload signatures,
    and asynchronous updates to the secondary-display controller.
    Why included: Preview/output state should be synchronized without making this module
    responsible for application paths, monitor policy, or tab-specific queue ownership.
    Where used: Constructed by main.build_ui() and supplied to the Display section.
    Returns: Instances expose preview controls and live-payload methods to the app.
    """
    def __init__(
        self,
        root: tk.Tk,
        *,
        get_app_base_path,
        get_display_config,
        get_preview_size,
        resolve_background_path,
        serialize_overlay_font,
        build_hot_background_paths,
        font_value_factory,
        get_music_licenses=None,
    ) -> None:
        """What: Store injected app services and initialize preview/output state.

        Why included: Dependency callbacks keep path, monitor, font, and queue policies
        owned by main.py while the controller manages rendering mechanics.
        Where used: MirrorDisplayController construction in main.build_ui().
        Returns: None; creates Tk state and schedules UI-callback queue draining.
        """
        self.root = root
        self.get_app_base_path = get_app_base_path
        self.get_display_config = get_display_config
        self.get_preview_size = get_preview_size
        self.resolve_background_path = resolve_background_path
        self.serialize_overlay_font = serialize_overlay_font
        self.build_hot_background_paths = build_hot_background_paths
        self.font_value_factory = font_value_factory
        self.get_music_licenses = get_music_licenses
        self.enabled_var = tk.BooleanVar(master=root, value=False)
        self.enabled_var.trace_add("write", lambda *_args: self._sync_keep_awake())
        self.watchdog_closed = False
        self.root.after(10000, self._run_output_watchdog)
        self.preview_container: tk.Frame | None = None
        self.preview_label: tk.Label | None = None
        self.preview_content: tk.Frame | None = None
        self.preview_text = ""
        self.preview_key = ""
        self.song_title = ""
        self.license_provider = ""
        self.license_author = ""
        self.license_info = ""
        self.show_song_metadata = False
        self.song_metadata_after_id = None
        self.font_value = None
        self.scroll_enabled = False
        self.scroll_speed = 7
        self.background_path = ""
        self.source = ""
        self.welcome_sections: dict[str, object] | None = None
        self.display_columns: list[dict[str, object]] = []
        self.selected_verse_index = 0
        self.preview_image = None
        self.preview_scroll_after_id = None
        self.preview_scroll_offset = 0
        self.preview_scroll_cycle_height = 0
        self.preview_gif_after_id = None
        self.preview_refresh_after_id = None
        self.preview_gif_path = ""
        self.preview_gif_frames = ()
        self.preview_gif_durations = ()
        self.preview_gif_frame_index = 0
        self.preview_video_reader = None
        self.preview_video_path = ""
        self.preview_video_started = 0.0
        self.preview_video_fps = 24.0
        self.preview_video_after_id = None
        self.preview_video_lock = threading.Lock()
        self.preview_background_cache_key = None
        self.preview_background_cache_image = None
        self.preview_overlay_cache_key = None
        self.preview_overlay_cache_image = None
        self.preview_scroll_layer_key = None
        self.preview_scroll_layer_image = None
        self.preview_scroll_corner_image = None
        self.preview_scroll_cache = {}
        self.last_live_payload_signature = None
        self.last_secondary_signature = None
        self.pending_secondary_signature = None
        self.transition_revision = 0
        self.secondary_controller: dict[str, object] | None = None
        self.hot_background_paths: list[Path | str | None] = []
        self.ui_callback_queue: queue.SimpleQueue[object] = queue.SimpleQueue()
        self.secondary_push_lock = threading.Lock()
        self.bible_preview_render_lock = threading.Lock()
        self.preview_render_lock = threading.Lock()
        self.preview_render_worker_active = False
        self.pending_preview_render = None
        self.secondary_push_request_token = 0
        self.preview_render_request_token = 0
        self.root.after(15, self._drain_ui_callback_queue)

    def _drain_ui_callback_queue(self) -> None:
        """What: Execute queued callbacks on Tk's thread and schedule the next drain.

        Why included: Worker threads must hand widget updates back to the Tk event loop.
        Where used: Repeated with root.after() from __init__().
        Returns: None; stops rescheduling when the root has been destroyed.
        """
        try:
            while True:
                callback = self.ui_callback_queue.get_nowait()
                if callable(callback):
                    callback()
        except queue.Empty:
            # An empty queue is the normal end of this drain pass, not an application error.
            pass
        try:
            self.root.after(15, self._drain_ui_callback_queue)
        except tk.TclError:
            # A destroyed Tk root cannot schedule another poll, so the callback loop ends here.
            return

    def schedule_on_ui_thread(self, callback) -> None:
        """What: Enqueue a callable for the controller's Tk-thread queue drain.

        Why included: Background render/push workers cannot safely call Tk widgets.
        Where used: Preview rendering and secondary-output worker completion paths.
        Returns: None; callback executes during a later queue-drain cycle.
        """
        self.ui_callback_queue.put(callback)

    def build_live_payload_signature(self, payload: dict[str, object] | None) -> tuple[object, ...] | None:
        """What: Normalize fields that determine whether an incoming live payload changed.

        Why included: Duplicate updates should not reset preview animation or repush OBS.
        Where used: set_live_payload() before mutating controller state.
        Returns: Stable signature tuple, or None for a non-dictionary payload.
        """
        if not isinstance(payload, dict):
            return None
        welcome_sections = payload.get("welcome_sections") if isinstance(payload.get("welcome_sections"), dict) else None
        normalized_sections = tuple(sorted((str(key), str(value)) for key, value in welcome_sections.items())) if isinstance(welcome_sections, dict) else None
        raw_display_columns = payload.get("display_columns") if isinstance(payload.get("display_columns"), list) else []
        normalized_display_columns = tuple(
            (
                str(column.get("title") or "Bible"),
                str(column.get("theme_header") or ""),
                tuple(str(value or "").strip() for value in list(column.get("verses") or []) if str(value or "").strip()),
            )
            for column in raw_display_columns[:3]
            if isinstance(column, dict)
        )
        license_provider = normalize_license_provider(payload.get("license_provider"))
        license_author = str(payload.get("license_author") or "").strip()
        license_info = str(payload.get("license_info") or "").strip()
        license_settings = self.get_music_licenses() if callable(self.get_music_licenses) else {}
        license_number = str(license_settings.get(license_provider.casefold()) or "").strip() if isinstance(license_settings, dict) and license_provider else ""
        return (
            str(payload.get("queue_item_id") or "").strip(),
            str(payload.get("title") or "").strip(),
            str(payload.get("text") or ""),
            str(payload.get("preview_key") or "").strip(),
            tuple(normalize_font_value(payload.get("font_value")) or ("Segoe UI", 12, "normal")),
            bool(payload.get("scroll_enabled")),
            self._normalize_scroll_speed(payload.get("scroll_speed"), default=7),
            str(payload.get("source") or "").strip(),
            str(payload.get("background_path") or "").strip(),
            normalized_sections,
            int(payload.get("selected_verse_index") or 0),
            normalized_display_columns,
            license_provider,
            license_number,
            license_author,
            license_info,
        )

    def build_secondary_signature(self) -> tuple[object, ...]:
        """What: Build a stable signature of all state sent to the secondary display.

        Why included: Avoids redundant cross-process updates while detecting output changes.
        Where used: push_to_secondary() and request_push_to_secondary().
        Returns: Tuple containing visibility, media, text, typography, source, and selection.
        """
        normalized_font = normalize_font_value(self.font_value) or normalize_font_value(self.font_value_factory()) or ("Segoe UI", 12, "normal")
        normalized_sections = tuple(sorted((str(key), str(value)) for key, value in self.welcome_sections.items())) if isinstance(self.welcome_sections, dict) else None
        normalized_display_columns = tuple(
            (
                str(column.get("title") or "Bible"),
                str(column.get("theme_header") or ""),
                tuple(str(value or "").strip() for value in list(column.get("verses") or []) if str(value or "").strip()),
            )
            for column in self.display_columns[:3]
            if isinstance(column, dict)
        )
        return (
            bool(self.enabled_var.get()),
            str(self.background_path or self._default_background_path()),
            str(self.preview_text or ""),
            tuple(normalized_font),
            bool(self.scroll_enabled),
            int(self.scroll_speed or 7),
            self.get_live_corner_text(),
            str(self.source or ""),
            normalized_sections,
            int(self.transition_revision or 0),
            int(self.selected_verse_index or 0),
            normalized_display_columns,
        )

    def _default_background_path(self) -> Path:
        """What: Resolve the branded default background through the injected path service.

        Why included: Empty queue payloads and preview startup need a consistent fallback.
        Where used: Controller initialization, reset payloads, and display configuration.
        Returns: Resolved default-background Path.
        """
        return self.resolve_background_path(DEFAULT_BACKGROUND_PATH)

    def _sync_keep_awake(self) -> None:
        """What: Keep Windows and its displays awake while the secondary output is live.

        Why included: Monitor/projector power timeouts must not blank a service mid-song.
        Where used: enabled_var trace and close(); no-op on non-Windows platforms.
        Returns: None; failures are ignored so output control is never blocked.
        """
        if sys.platform != "win32":
            return
        es_continuous, es_system_required, es_display_required = 0x80000000, 0x00000001, 0x00000002
        keep_awake = bool(self.enabled_var.get()) and not self.watchdog_closed
        flags = es_continuous | ((es_system_required | es_display_required) if keep_awake else 0)
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
        except Exception:
            pass

    @staticmethod
    def _close_controller_quietly(controller: dict[str, object]) -> None:
        """What: Close a replaced OBS controller without surfacing shutdown errors.

        Why included: Watchdog rebuilds must not fail because the dead output misbehaves.
        Where used: _run_output_watchdog() rebuild path, on a worker thread.
        Returns: None.
        """
        try:
            controller["close"]()
        except Exception:
            pass

    def _run_output_watchdog(self) -> None:
        """What: Detect a stalled OBS output page and recover it automatically.

        Why included: Operators previously had to toggle the secondary screen by hand
        when the browser output or OBS stopped responding during a service.
        Where used: Repeating 10-second Tk timer started during controller initialization.
        Returns: None; reschedules itself until close() marks the controller closed.
        """
        if self.watchdog_closed:
            return
        try:
            controller = self.secondary_controller
            if bool(self.enabled_var.get()) and isinstance(controller, dict):
                age_callback = controller.get("state_poll_age")
                age = float(age_callback()) if callable(age_callback) else 0.0
                if age > 60.0:
                    log_error("mirror.output_watchdog", f"Secondary output unresponsive for {age:.0f}s; rebuilding OBS output.")
                    self.secondary_controller = None
                    threading.Thread(target=self._close_controller_quietly, args=(controller,),
                        name="SecondaryOutputRebuild", daemon=True).start()
                    self.last_secondary_signature = None
                    self.pending_secondary_signature = None
                    self.request_push_to_secondary(force=True)
                elif age > 20.0:
                    log_error("mirror.output_watchdog", f"Secondary output stale for {age:.0f}s; re-pushing output.")
                    self.request_push_to_secondary(force=True)
        except Exception as error:
            log_exception("mirror.output_watchdog", error)
        finally:
            if not self.watchdog_closed:
                self.root.after(10000, self._run_output_watchdog)

    def get_live_corner_text(self) -> str:
        """Build persistent key text and optional timed song metadata.

        What: Shows title, license, and author temporarily while retaining the song key.
        Why included: Attribution should be visible when a song changes without staying on screen.
        Where used: Local preview and OBS-backed secondary-screen rendering.
        Returns: Newline-separated current corner lines.
        """
        song_key = "" if is_display_key_hidden() else str(self.preview_key or "").strip()
        if self.source.casefold() != "library":
            return song_key
        provider = normalize_license_provider(self.license_provider)
        if not self.show_song_metadata or not provider:
            return song_key
        settings = self.get_music_licenses() if callable(self.get_music_licenses) else {}
        number = str(settings.get(provider.casefold()) or "").strip() if isinstance(settings, dict) and provider else ""
        attribution = format_music_license_attribution(provider, number, self.license_author, self.license_info)
        details = "\n".join(value for value in (str(self.song_title or "").strip(), attribution) if value)
        return f"{song_key}\n{details}" if song_key else f"\n{details}"

    def cancel_song_metadata_timer(self) -> None:
        """Cancel the pending timeout for temporary song title/license attribution.

        What: Cancels the Tk after callback when one is scheduled.
        Why included: A new live item or shutdown must not leave stale metadata callbacks.
        Where used: New payloads, refresh, and controller close.
        Returns: None; clears the tracked after identifier.
        """
        after_id = self.song_metadata_after_id
        self.song_metadata_after_id = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass

    def start_song_metadata_timer(self) -> None:
        """Show song metadata and schedule it to disappear after three seconds.

        What: Displays the current Library attribution and queues a 3000 ms timeout.
        Why included: Viewers need a brief attribution on song changes without a persistent overlay.
        Where used: Library live-payload updates and license-setting refresh.
        Returns: None; schedules hide_song_metadata() on the Tk event loop.
        """
        self.cancel_song_metadata_timer()
        self.show_song_metadata = self.source.casefold() == "library" and bool(normalize_license_provider(self.license_provider))
        if not self.show_song_metadata:
            return
        try:
            self.song_metadata_after_id = self.root.after(LIVE_SONG_METADATA_MS, self.hide_song_metadata)
        except tk.TclError:
            self.song_metadata_after_id = None

    def hide_song_metadata(self) -> None:
        """Hide temporary song metadata and refresh both live render targets.

        What: Removes title/license/author while leaving the song key and lyrics intact.
        Why included: Attribution display is intentionally temporary.
        Where used: Tk timeout scheduled by start_song_metadata_timer().
        Returns: None; schedules refreshed local preview and secondary output.
        """
        self.song_metadata_after_id = None
        if not self.show_song_metadata:
            return
        self.show_song_metadata = False
        self.clear_preview_overlay_cache()
        self.render_preview()
        self.request_push_to_secondary(force=True)

    def refresh_music_license_attribution(self) -> None:
        """Refresh the current live corner label after license numbers change.

        What: Invalidates overlay pixels, redraws the local preview, and pushes current state.
        Why included: Menu edits should update an already-live song without re-sending it.
        Where used: Music Licenses dialog save action in main.py.
        Returns: None; schedules local and external rendering.
        """
        if self.source.casefold() == "library":
            self.start_song_metadata_timer()
        self.clear_preview_overlay_cache()
        self.render_preview()
        self.request_push_to_secondary(force=True)

    def _normalize_scroll_speed(self, value: object, default: int = 7) -> int:
        """What: Convert a scroll setting to an integer within supported speed bounds.

        Why included: Persisted and UI-provided values may be absent or out of range.
        Where used: Payload signatures, state updates, and preview animation timing.
        Returns: Scroll speed between SCROLL_SPEED_MIN and SCROLL_SPEED_MAX.
        """
        try:
            numeric_value = int(value)
        except Exception:
            numeric_value = default
        return max(SCROLL_SPEED_MIN, min(SCROLL_SPEED_MAX, numeric_value))

    def is_preview_scroll_active(self, preview_text: str | None = None, scroll_enabled: bool | None = None) -> bool:
        """What: Determine whether nonempty preview text should use scrolling layout.

        Why included: An enabled toggle with empty content must not start an animation.
        Where used: Text fitting, render selection, and animation synchronization.
        Returns: True only when scrolling is enabled and text contains non-whitespace.
        """
        text_value = self.preview_text if preview_text is None else preview_text
        enabled_value = self.scroll_enabled if scroll_enabled is None else scroll_enabled
        return bool(enabled_value) and bool(str(text_value or "").strip())

    def get_preview_scroll_step(self) -> float:
        """What: Convert the configured speed into a per-frame pixel step.

        Why included: Preview scroll animation advances at a fixed timer interval, and
        fractional steps keep the same pixels-per-second at a higher frame rate.
        Where used: advance_preview_scroll() for each animation frame.
        Returns: Positive scroll increment matching the legacy 80 ms step's speed.
        """
        return max(0.5, round(self._normalize_scroll_speed(self.scroll_speed, default=SCROLL_SPEED_DEFAULT) / 4) / 2.0)

    def cancel_preview_scroll_animation(self) -> None:
        """What: Cancel the scheduled scroll callback when its label still exists.

        Why included: Stale timers must not render after content changes or shutdown.
        Where used: Payload reset, animation synchronization, and controller cleanup.
        Returns: None; clears the stored Tk after identifier.
        """
        if self.preview_scroll_after_id and isinstance(self.preview_label, tk.Label):
            try:
                self.preview_label.after_cancel(self.preview_scroll_after_id)
            except Exception:
                pass
        self.preview_scroll_after_id = None

    def cancel_preview_gif_animation(self) -> None:
        """What: Cancel the scheduled GIF-frame callback when its label still exists.

        Why included: A replaced/cleared background must stop its old frame timer.
        Where used: GIF cache reset, animation synchronization, and shutdown.
        Returns: None; clears the stored Tk after identifier.
        """
        if self.preview_gif_after_id and isinstance(self.preview_label, tk.Label):
            try:
                self.preview_label.after_cancel(self.preview_gif_after_id)
            except Exception:
                pass
        self.preview_gif_after_id = None

    def cancel_preview_refresh(self) -> None:
        """What: Cancel a pending after-idle preview refresh when possible.

        Why included: Coalesced refreshes should not render stale state after teardown.
        Where used: Scheduling a newer refresh and controller close.
        Returns: None; clears the pending callback identifier.
        """
        if self.preview_refresh_after_id and isinstance(self.preview_label, tk.Label):
            try:
                self.preview_label.after_cancel(self.preview_refresh_after_id)
            except Exception:
                pass
        self.preview_refresh_after_id = None

    def schedule_preview_refresh(self) -> None:
        """What: Coalesce preview rendering into one after-idle callback.

        Why included: Multiple state changes in one UI event should cause one redraw.
        Where used: set_live_payload() after preview state changes.
        Returns: None; callback renders current state then synchronizes animations.
        """
        self.cancel_preview_refresh()
        if not isinstance(self.preview_label, tk.Label):
            return

        def refresh_preview_now() -> None:
            """What: Clear the pending ID, render current state, and sync animations.

            Why included: The deferred callback must use the newest controller state.
            Where used: after_idle callback scheduled by schedule_preview_refresh().
            Returns: None; updates the local preview and animation timers.
            """
            self.preview_refresh_after_id = None
            self.render_preview()
            self.sync_preview_animations()

        self.preview_refresh_after_id = self.preview_label.after_idle(refresh_preview_now)

    def clear_preview_gif_cache(self) -> None:
        """What: Stop GIF animation and clear cached frames, timings, and index.

        Why included: Cached frames belong to one media path and must not leak across files.
        Where used: Background changes, static/video loading, and failed GIF decode.
        Returns: None; resets GIF-specific controller state.
        """
        self.cancel_preview_gif_animation()
        self.preview_gif_path = ""
        self.preview_gif_frames = ()
        self.preview_gif_durations = ()
        self.preview_gif_frame_index = 0

    def clear_preview_overlay_cache(self) -> None:
        """What: Invalidate the cached fitted text overlay and its key.

        Why included: Text, font, selection, or display settings can make the image stale.
        Where used: Live payload updates and overlay-cache invalidation paths.
        Returns: None; the next render recomposes the overlay.
        """
        self.preview_overlay_cache_key = None
        self.preview_overlay_cache_image = None

    def clear_preview_background_cache(self) -> None:
        """What: Invalidate the cached resized background image and its key.

        Why included: A changed background or preview dimension needs fresh media pixels.
        Where used: Background-path changes and preview media updates.
        Returns: None; the next render reloads/resizes the background.
        """
        self.preview_background_cache_key = None
        self.preview_background_cache_image = None

    def get_preview_overlay_cache_key(
        self,
        preview_text: str,
        font_value: tuple[str, int, str],
        corner_text: str,
        preview_width: int,
        preview_height: int,
        render_width: int,
        render_height: int,
        *,
        source_value: str,
        background_path: str,
        scroll_enabled: bool,
        welcome_sections: dict[str, object] | None,
    ) -> tuple[object, ...]:
        """What: Build a hashable key for every input that affects fitted overlay pixels.

        Why included: Preview overlay reuse is valid only while content, style, source,
        background, and target dimensions remain unchanged.
        Where used: render_preview_pil_image() before reusing a cached overlay.
        Returns: Tuple of normalized source/content/render settings.
        """
        sections = welcome_sections if isinstance(welcome_sections, dict) else None
        normalized_sections = tuple(sorted((str(key), str(value)) for key, value in sections.items())) if isinstance(sections, dict) else None
        return (
            str(source_value or ""),
            str(background_path or ""),
            str(preview_text or ""),
            tuple(normalize_font_value(font_value) or ("Segoe UI", 12, "normal")),
            str(corner_text or ""),
            bool(scroll_enabled),
            normalized_sections,
            int(preview_width),
            int(preview_height),
            int(render_width),
            int(render_height),
            get_display_outline_width(),
        )

    def get_cached_preview_background(self, resolved_path: Path, preview_width: int, preview_height: int):
        """What: Load, crop, and cache a preview-sized image or current GIF frame.

        Why included: Repeated UI redraws should avoid reopening and resizing unchanged media.
        Where used: Background worker in request_preview_render().
        Returns: RGB Pillow image tagged with its background path.
        """
        background_key: tuple[object, ...]
        path_str = str(resolved_path)
        if resolved_path.suffix.lower() in MIRROR_VIDEO_EXTENSIONS:
            self.clear_preview_gif_cache()
            if imageio is None:
                raise RuntimeError("Video previews require imageio and imageio-ffmpeg in this app build.")
            with self.preview_video_lock:
                if self.preview_video_path != path_str or self.preview_video_reader is None:
                    if self.preview_video_reader is not None:
                        self.preview_video_reader.close()
                    self.preview_video_reader = LoopingVideoReader(path_str)
                    self.preview_video_path = path_str
                    self.preview_video_started = time.monotonic()
                    self.preview_video_fps = self.preview_video_reader.fps
                frame, frame_index = self.preview_video_reader.frame_at_time(time.monotonic())
                preview_source = Image.fromarray(frame)
            rendered_image = ImageOps.fit(preview_source.convert("RGB"), (preview_width, preview_height))
            setattr(rendered_image, "_background_path", path_str)
            self.preview_background_cache_key = (path_str, int(preview_width), int(preview_height), "video", frame_index)
            self.preview_background_cache_image = rendered_image.copy()
            setattr(self.preview_background_cache_image, "_background_path", path_str)
            return rendered_image
        self.close_preview_video_reader()
        if resolved_path.suffix.lower() == ".gif" and self.ensure_preview_gif_cache(resolved_path):
            frames = self.preview_gif_frames if isinstance(self.preview_gif_frames, tuple) else ()
            frame_index = int(self.preview_gif_frame_index or 0) % len(frames) if frames else 0
            background_key = (path_str, int(preview_width), int(preview_height), frame_index)
            if self.preview_background_cache_key == background_key and self.preview_background_cache_image is not None:
                cached_copy = self.preview_background_cache_image.copy()
                setattr(cached_copy, "_background_path", path_str)
                return cached_copy
            preview_source = frames[frame_index].copy() if frames else Image.new("RGB", (preview_width, preview_height), "#000000")
        else:
            self.clear_preview_gif_cache()
            background_key = (path_str, int(preview_width), int(preview_height), "static")
            if self.preview_background_cache_key == background_key and self.preview_background_cache_image is not None:
                cached_copy = self.preview_background_cache_image.copy()
                setattr(cached_copy, "_background_path", path_str)
                return cached_copy
            with Image.open(resolved_path) as source_image:
                preview_source = ImageOps.exif_transpose(source_image).copy()
        fit_background = ImageOps.pad if preview_source.width < preview_source.height else ImageOps.fit
        rendered_image = fit_background(
            preview_source.convert("RGB"),
            (preview_width, preview_height),
            method=Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
        )
        setattr(rendered_image, "_background_path", path_str)
        self.preview_background_cache_key = background_key
        self.preview_background_cache_image = rendered_image.copy()
        setattr(self.preview_background_cache_image, "_background_path", path_str)
        return rendered_image

    def build_section(self, parent_frame: tk.Widget, panel_bg: str) -> dict[str, tk.Widget]:
        """What: Build the fixed-size preview panel and secondary-screen toggle.

        Why included: Main UI needs local visual feedback and one control for enabling
        external display output.
        Where used: main.build_ui() in the Secondary Screen top section.
        Returns: Mapping of the content, preview container/label, and toggle widgets.
        """
        content = tk.Frame(parent_frame, bg=panel_bg)
        content.pack(expand=True, fill="both")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        preview_width, preview_height = self.get_preview_size()
        preview_width = max(180, int(preview_width))
        preview_height = max(100, int(preview_height))
        preview_container = tk.Frame(content, bg=panel_bg, width=preview_width, height=preview_height)
        preview_container.grid(row=0, column=0)
        preview_container.grid_propagate(False)
        preview_container.columnconfigure(0, weight=1)
        preview_container.rowconfigure(0, weight=1)

        preview_label = tk.Label(
            preview_container,
            bg="#000000",
            fg="#ffffff",
            anchor="center",
            compound="center",
            text="Secondary Screen Preview",
            justify="center",
        )
        preview_label.grid(row=0, column=0, sticky="nsew")

        toggle = tk.Checkbutton(
            content,
            text="Activate Secondary Screen",
            variable=self.enabled_var,
            command=self.handle_secondary_toggle,
            anchor="center",
            justify="center",
        )
        toggle.grid(row=1, column=0, pady=(6, 0))

        self.preview_content = content
        self.preview_container = preview_container
        self.preview_label = preview_label
        content.bind("<Configure>", self.handle_preview_content_configure, add="+")
        content.after_idle(self.resize_preview_to_available_space)
        self.render_preview()
        self.sync_preview_animations()

        return {
            "content": content,
            "preview_container": preview_container,
            "preview_label": preview_label,
            "toggle": toggle,
        }

    def sync_preview_frame_size(self) -> None:
        """What: Set preview-label wrapping from the current container width.

        Why included: Fallback text should wrap to the visible preview rather than
        the original nominal monitor size.
        Where used: render_preview() before requesting an image redraw.
        Returns: None; updates label wraplength when widgets exist.
        """
        if not isinstance(self.preview_container, tk.Frame) or not isinstance(self.preview_label, tk.Label):
            return
        preview_width = max(1, int(self.preview_container.cget("width") or self.get_preview_render_size()[0]))
        self.preview_label.configure(wraplength=max(120, preview_width - 20))

    def handle_preview_content_configure(self, _event=None) -> None:
        """What: Forward a content-frame resize event to preview-size calculation.

        Why included: The preview should adapt when its panel or containing window resizes.
        Where used: Configure binding installed by build_section().
        Returns: None; delegates to resize_preview_to_available_space().
        """
        self.resize_preview_to_available_space()

    def resize_preview_to_available_space(self) -> None:
        """What: Resize the preview while preserving monitor aspect ratio and bounds.

        Why included: The preview must use available panel space without stretching,
        obscuring the toggle, or causing unnecessary renders.
        Where used: Initial after-idle setup and preview-content Configure events.
        Returns: None; reconfigures the container and requests a render only on size change.
        """
        if not isinstance(self.preview_content, tk.Frame) or not isinstance(self.preview_container, tk.Frame):
            return
        self.preview_content.update_idletasks()
        desired_width, desired_height = self.get_nominal_preview_size()
        available_width = max(1, int(self.preview_content.winfo_width() or desired_width))
        available_height = max(1, int(self.preview_content.winfo_height() or desired_height))
        toggle_height = 0
        if isinstance(self.preview_content, tk.Frame) and self.preview_content.grid_slaves(row=1):
            toggle_height = int(self.preview_content.grid_slaves(row=1)[0].winfo_reqheight() or 0)
        available_height = max(1, available_height - toggle_height - 6)
        aspect_ratio = desired_width / max(1, desired_height)
        preview_width = min(desired_width, available_width)
        preview_height = min(desired_height, available_height)
        if preview_width / max(1, preview_height) > aspect_ratio:
            preview_width = int(preview_height * aspect_ratio)
        else:
            preview_height = int(preview_width / max(0.1, aspect_ratio))
        preview_width = max(180, preview_width)
        preview_height = max(100, preview_height)
        current_width = int(self.preview_container.cget("width") or 0)
        current_height = int(self.preview_container.cget("height") or 0)
        if (current_width, current_height) == (preview_width, preview_height):
            return
        self.preview_container.configure(width=preview_width, height=preview_height)
        self.preview_label.configure(wraplength=max(120, preview_width - 20))
        self.request_preview_render()

    def get_preview_render_size(self) -> tuple[int, int]:
        """What: Return the actual configured preview size when available.

        Why included: Rendering should match the visible container rather than nominal size.
        Where used: Preview fitting and image creation.
        Returns: Current positive width/height, falling back to nominal preview dimensions.
        """
        if isinstance(self.preview_container, tk.Frame):
            current_width = int(self.preview_container.cget("width") or 0)
            current_height = int(self.preview_container.cget("height") or 0)
            if current_width > 0 and current_height > 0:
                return current_width, current_height
        return self.get_nominal_preview_size()

    def get_nominal_preview_size(self) -> tuple[int, int]:
        """What: Read and clamp the preferred preview size from the injected app callback.

        Why included: Monitor bounds determine a useful aspect-matched initial size.
        Where used: Preview container creation and fallback render dimensions.
        Returns: Width and height with minimum panel-safe bounds.
        """
        preview_width, preview_height = self.get_preview_size()
        return max(180, int(preview_width)), max(100, int(preview_height))

    def get_center_region_luminance(self, base_image) -> float:
        """What: Average grayscale brightness in the central overlay text region.

        Why included: Preview text color needs contrast against its background.
        Where used: Preview color selection when a background image is available.
        Returns: Mean luminance from 0 (black) through 255 (white).
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

    def choose_preview_text_colors(self, base_image, background_path: Path | str | None = None) -> tuple[str, str]:
        """What: Delegate preview fill/outline selection to shared overlay color logic.

        Why included: Local preview and secondary output should use the same color policy.
        Where used: render_preview_pil_image() when composing scrolling text.
        Returns: Text fill and outline color pair.
        """
        bg_path = background_path or getattr(base_image, "_background_path", None)
        return choose_text_colors(base_image, background_path=bg_path)

    def get_preview_max_text_size(self, preview_height: int, base_size: int) -> int:
        """What: Cap preview font candidates by both panel height and base font size.

        Why included: Preview text should not exceed its panel or grow without bound.
        Where used: fit_preview_text_for_render() for non-scrolling content.
        Returns: Maximum candidate font size in pixels.
        """
        scaled_height_cap = round(max(1, preview_height) * PREVIEW_MAX_TEXT_HEIGHT_RATIO)
        multiplied_base_cap = max(18, int(base_size) * PREVIEW_MAX_TEXT_MULTIPLIER)
        return max(18, min(scaled_height_cap, multiplied_base_cap))

    def fit_preview_text_for_render(self, base_image, preview_text: str, font_value: tuple[str, int, str], scroll_enabled: bool | None = None):
        """What: Wrap and fit preview text to the image, retaining scroll sizing when enabled.

        Why included: Local preview should approximate output layout while guaranteeing
        text remains within the fixed preview panel.
        Where used: render_preview_pil_image() for scrolling and regular text sources.
        Returns: Wrapped text, Pillow font handle, and line spacing; missing Pillow
        metrics produce the original text, None font, and zero spacing.
        """
        if ImageDraw is None or ImageFont is None:
            return preview_text, None, 0
        preview_width, preview_height = base_image.size
        base_family, base_size, base_style = font_value
        stroke_width = get_effective_stroke_width(base_size, PREVIEW_STROKE_WIDTH)
        usable_width = max(60, preview_width - ((stroke_width + 18) * 2))
        usable_height = max(60, preview_height - ((stroke_width + 18) * 2))
        if self.is_preview_scroll_active(preview_text=preview_text, scroll_enabled=scroll_enabled):
            selected_font = get_preview_pil_font((base_family, base_size, base_style))
            if selected_font is None:
                return preview_text, None, 0
            wrapped_text = wrap_preview_text_to_width(preview_text, selected_font, usable_width, stroke_width)
            selected_spacing = max(4, round(base_size * 0.28))
            return wrapped_text, selected_font, selected_spacing
        max_size = self.get_preview_max_text_size(preview_height, base_size)
        min_size = 12
        fallback_text = preview_text
        fallback_font = None
        fallback_spacing = 6
        measure_draw = get_shared_mirror_measure_draw()
        if measure_draw is None:
            return preview_text, None, 0
        for candidate_size in range(max_size, min_size - 1, -2):
            candidate_stroke_width = get_effective_stroke_width(candidate_size, PREVIEW_STROKE_WIDTH)
            candidate_font = get_preview_pil_font((base_family, candidate_size, base_style))
            if candidate_font is None:
                continue
            candidate_text = wrap_preview_text_to_width(preview_text, candidate_font, usable_width, candidate_stroke_width)
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

    def render_cached_scroll_preview(self, background, text: str, font_value, corner_text: str,
                                     render_width: int, render_height: int, offset: int, background_path: str):
        """What: Move a cached scrolling text strip over the preview-sized background.

        Why included: Animation ticks should not refit text, redraw strokes, or resize a projector-sized image.
        Where used: render_preview_pil_image() for scrolling Welcome content.
        Returns: RGB preview image and scroll cycle height; typography changes invalidate the strip.
        """
        result = render_scrolling_preview(background, text, font_value, render_width, render_height,
                    offset, self.preview_scroll_cache, background_path=background_path,
                    corner_text=corner_text, size_boost=WELCOME_LAYOUT_SIZE_BOOST)
        self.preview_scroll_layer_key = self.preview_scroll_cache.get("key")
        self.preview_scroll_layer_image = self.preview_scroll_cache.get("text_layer")
        self.preview_scroll_corner_image = self.preview_scroll_cache.get("corner_layer")
        return result

    def render_preview_pil_image(
        self,
        source_image,
        preview_text: str,
        font_value: tuple[str, int, str],
        corner_text: str = "",
        *,
        source_value: str = "",
        background_path: str = "",
        scroll_enabled: bool = False,
        welcome_sections: dict[str, object] | None = None,
        preview_scroll_offset: int = 0,
        preview_size: tuple[int, int] | None = None,
        display_columns: list[dict[str, object]] | None = None,
        selected_verse_index: int | None = None,
    ):
        """What: Composite the current source background and fitted content for the Tk preview.

        Why included: The preview must reflect output-sized text fitting, source-specific
        layouts, scroll state, and multi-column Bible selection.
        Where used: Background worker launched by request_preview_render().
        Returns: RGB Pillow preview image and next scroll-cycle height, or None image
        when Pillow image operations are unavailable.
        """
        if ImageOps is None or Image is None:
            return None
        if isinstance(preview_size, tuple) and len(preview_size) >= 2:
            preview_width, preview_height = max(1, int(preview_size[0])), max(1, int(preview_size[1]))
        else:
            preview_width, preview_height = self.get_preview_render_size()
        if source_image.size == (preview_width, preview_height):
            # Cached preview backgrounds arrive already fitted; skip a per-frame LANCZOS refit.
            rendered_image = source_image.convert("RGB")
        else:
            rendered_image = ImageOps.fit(
                source_image.convert("RGB"),
                (preview_width, preview_height),
                method=Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
            )
        display_bounds = self.get_display_config().get("display_bounds") if callable(self.get_display_config) else None
        render_width = max(1, int((display_bounds or {}).get("width") or 1920))
        render_height = max(1, int((display_bounds or {}).get("height") or 1080))
        bg_path = str(background_path or getattr(source_image, "_background_path", "") or "")
        if bg_path:
            setattr(rendered_image, "_background_path", bg_path)
            setattr(source_image, "_background_path", bg_path)
        if display_columns is None:
            display_columns = getattr(self, "display_columns", [])
        if selected_verse_index is None:
            selected_verse_index = int(getattr(self, "selected_verse_index", 0) or 0)
        if str(source_value).casefold() == "bible" and isinstance(display_columns, list) and display_columns:
            rendered_bible_frame = render_bible_columns_frame(
                source_image,
                preview_width,
                preview_height,
                render_width,
                render_height,
                display_columns,
                selected_verse_index,
                normalize_font_value(font_value),
                stroke_width=PREVIEW_STROKE_WIDTH,
                size_boost=SECONDARY_LAYOUT_SIZE_BOOST,
            )
            return rendered_bible_frame if rendered_bible_frame is not None else rendered_image.convert("RGB"), 0
        scroll_active = self.is_preview_scroll_active(preview_text=preview_text, scroll_enabled=scroll_enabled)
        next_scroll_cycle_height = 0
        if scroll_active:
            return self.render_cached_scroll_preview(rendered_image, preview_text, font_value, corner_text,
                                                     render_width, render_height, preview_scroll_offset, bg_path)
        fitted_background = ImageOps.fit(source_image.convert("RGB"), (render_width, render_height),
                                        method=Image.Resampling.LANCZOS)
        if bg_path:
            setattr(fitted_background, "_background_path", bg_path)
        fields = build_sized_text_fields(source_value, preview_text, font_value, welcome_sections)
        if fields and not scroll_active:
            overlay = render_text_fields_overlay(render_width, render_height, fields, base_image=fitted_background, background_path=bg_path)
            if overlay is not None:
                overlay = overlay.resize((preview_width, preview_height), Image.Resampling.LANCZOS)
                return Image.alpha_composite(rendered_image.convert("RGBA"), overlay).convert("RGB"), 0
        source_key = str(source_value).casefold()
        source_line_spacing_ratio = 0.28
        source_size_boost = SECONDARY_LAYOUT_SIZE_BOOST
        if source_key == "preparation":
            source_line_spacing_ratio = 0.14
            source_size_boost = 1.0
        elif source_key == "welcome":
            source_size_boost = WELCOME_LAYOUT_SIZE_BOOST
        full_layout = fit_overlay_layout(
            preview_text,
            scale_overlay_font_value(normalize_font_value(font_value), render_width, render_height),
            render_width,
            render_height,
            scroll_enabled=False,
            base_image=fitted_background,
            stroke_width=PREVIEW_STROKE_WIDTH,
            padding=18,
            max_text_height_ratio=PREVIEW_MAX_TEXT_HEIGHT_RATIO,
            max_text_multiplier=PREVIEW_MAX_TEXT_MULTIPLIER,
            min_text_size=12,
            line_spacing_ratio=source_line_spacing_ratio,
            size_boost=source_size_boost,
            corner_font_size=get_display_key_size(render_width, render_height),
        )
        overlay_cache_key = self.get_preview_overlay_cache_key(
            preview_text,
            font_value,
            corner_text,
            preview_width,
            preview_height,
            render_width,
            render_height,
            source_value=source_value,
            background_path=background_path,
            scroll_enabled=scroll_enabled,
            welcome_sections=welcome_sections,
        )
        cached_overlay_image = self.preview_overlay_cache_image if self.preview_overlay_cache_key == overlay_cache_key else None
        if cached_overlay_image is not None:
            return Image.alpha_composite(rendered_image.convert("RGBA"), cached_overlay_image.copy()).convert("RGB"), next_scroll_cycle_height
        sections = welcome_sections if isinstance(welcome_sections, dict) else None
        if source_key == "welcome" and sections and sections.get("combine_enabled"):
            section_layout = fit_sectioned_overlay_layout(
                str(sections.get("centered_text") or ""),
                str(sections.get("centered_detail_text") or ""),
                str(sections.get("detail_text") or ""),
                scale_overlay_font_value(normalize_font_value(font_value), render_width, render_height),
                render_width,
                render_height,
                base_image=fitted_background,
                stroke_width=PREVIEW_STROKE_WIDTH,
                padding=18,
                max_text_height_ratio=PREVIEW_MAX_TEXT_HEIGHT_RATIO,
                max_text_multiplier=PREVIEW_MAX_TEXT_MULTIPLIER,
                min_text_size=12,
                line_spacing_ratio=0.28,
                size_boost=WELCOME_LAYOUT_SIZE_BOOST,
            )
            full_overlay_image = render_sectioned_overlay_image(
                render_width,
                render_height,
                section_layout,
                stroke_width=PREVIEW_STROKE_WIDTH,
            )
        else:
            full_overlay_image = render_overlay_image(
                render_width,
                render_height,
                full_layout,
                corner_text=corner_text,
                stroke_width=PREVIEW_STROKE_WIDTH,
            )
        if full_overlay_image is None:
            return rendered_image.convert("RGB"), next_scroll_cycle_height
        scaled_overlay_image = full_overlay_image.resize(
            (preview_width, preview_height),
            Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
        ).convert("RGBA")
        self.preview_overlay_cache_key = overlay_cache_key
        self.preview_overlay_cache_image = scaled_overlay_image.copy()
        return Image.alpha_composite(rendered_image.convert("RGBA"), scaled_overlay_image).convert("RGB"), next_scroll_cycle_height

    def schedule_preview_scroll_animation(self) -> None:
        """What: Schedule one scroll frame when scrolling is enabled and measurable.

        Why included: Long preview text needs timed movement inside the local panel.
        Where used: Animation synchronization after each preview render.
        Returns: None; resets the offset when scrolling is inactive.
        """
        if self.preview_scroll_after_id or not self.is_preview_scroll_active() or not isinstance(self.preview_label, tk.Label):
            if not self.is_preview_scroll_active():
                self.cancel_preview_scroll_animation()
                self.preview_scroll_offset = 0
            return

        def advance_preview_scroll() -> None:
            """What: Advance the scroll offset, redraw, and schedule the next frame.

            Why included: A repeating Tk timer drives smooth movement without a worker.
            Where used: after callback scheduled by schedule_preview_scroll_animation().
            Returns: None; updates preview_scroll_offset and timer state.
            """
            self.preview_scroll_after_id = None
            if self.preview_render_worker_active:
                self.schedule_preview_scroll_animation()
                return
            cycle_height = max(1, int(self.preview_scroll_cycle_height or 0))
            next_offset = float(self.preview_scroll_offset or 0) + self.get_preview_scroll_step()
            self.preview_scroll_offset = next_offset % cycle_height if cycle_height > 0 else next_offset
            self.render_preview()
            self.schedule_preview_scroll_animation()

        self.preview_scroll_after_id = self.preview_label.after(PREVIEW_SCROLL_FRAME_MS, advance_preview_scroll)

    def ensure_preview_gif_cache(self, background_path: Path) -> bool:
        """What: Load and retain GIF frames/durations when the requested path changes.

        Why included: Preview animation should reuse decoded frames and report unsupported
        or unreadable media without disrupting other rendering.
        Where used: get_cached_preview_background() when the source is a GIF.
        Returns: True when cached frames are available, otherwise False.
        """
        cached_path = str(self.preview_gif_path or "")
        if cached_path == str(background_path) and isinstance(self.preview_gif_frames, tuple) and self.preview_gif_frames:
            return True
        try:
            with Image.open(background_path) as source_image:
                if ImageSequence is None or not getattr(source_image, "is_animated", False):
                    self.clear_preview_gif_cache()
                    return False
                frames: list[object] = []
                durations: list[int] = []
                for frame in ImageSequence.Iterator(source_image):
                    frames.append(frame.copy())
                    durations.append(int(frame.info.get("duration", source_image.info.get("duration", 100)) or 100))
        except Exception:
            self.clear_preview_gif_cache()
            return False
        if not frames:
            self.clear_preview_gif_cache()
            return False
        self.preview_gif_path = str(background_path)
        self.preview_gif_frames = tuple(frames)
        self.preview_gif_durations = tuple(max(20, duration) for duration in durations)
        self.preview_gif_frame_index = 0
        return True

    def schedule_preview_gif_animation(self) -> None:
        """What: Schedule the next GIF frame using that frame's stored duration.

        Why included: Animated backgrounds need per-frame timing and only one active timer.
        Where used: sync_preview_animations() after a render or frame change.
        Returns: None; cancels animation when the cache has no multi-frame GIF.
        """
        has_gif_frames = isinstance(self.preview_gif_frames, tuple) and len(self.preview_gif_frames) > 1
        if self.preview_gif_after_id or not has_gif_frames or not isinstance(self.preview_label, tk.Label):
            if not has_gif_frames:
                self.cancel_preview_gif_animation()
            return
        durations = self.preview_gif_durations if isinstance(self.preview_gif_durations, tuple) and self.preview_gif_durations else tuple(100 for _ in self.preview_gif_frames)
        frame_index = int(self.preview_gif_frame_index or 0) % len(self.preview_gif_frames)
        delay_ms = int(durations[frame_index]) if frame_index < len(durations) else 100
        delay_ms = max(20, delay_ms)

        def advance_preview_gif() -> None:
            """What: Advance the cached frame index, redraw the preview, and reschedule.

            Why included: Tk after callbacks provide animation without blocking the UI.
            Where used: Frame timer scheduled by schedule_preview_gif_animation().
            Returns: None; stops when there are no longer multiple cached frames.
            """
            current_frames = self.preview_gif_frames if isinstance(self.preview_gif_frames, tuple) else ()
            self.preview_gif_after_id = None
            if len(current_frames) <= 1:
                return
            if self.preview_render_worker_active:
                self.schedule_preview_gif_animation()
                return
            self.preview_gif_frame_index = (int(self.preview_gif_frame_index or 0) + 1) % len(current_frames)
            self.render_preview()
            self.schedule_preview_gif_animation()

        self.preview_gif_after_id = self.preview_label.after(delay_ms, advance_preview_gif)

    def close_preview_video_reader(self) -> None:
        """What: Close the current video decoder and reset its source path.

        Why included: Background changes and shutdown must release FFmpeg processes/file handles.
        Where used: Static/GIF background loading and controller shutdown.
        Returns: None; serializes decoder cleanup with video-frame rendering.
        """
        with self.preview_video_lock:
            if self.preview_video_reader is not None:
                try:
                    self.preview_video_reader.close()
                finally:
                    self.preview_video_reader = None
                    self.preview_video_path = ""

    def schedule_preview_video_animation(self) -> None:
        """What: Schedule local video-frame rendering at a capped 15 FPS.

        Why included: MP4 backgrounds need motion in the preview without starting parallel renders.
        Where used: sync_preview_animations() after preview image application.
        Returns: None; decoding remains in the existing preview background worker.
        """
        if self.preview_video_after_id or not isinstance(self.preview_label, tk.Label):
            return

        def advance_video() -> None:
            """What: Request the next video preview frame if the source is still active.

            Why included: Tk owns animation scheduling while the worker owns decoding.
            Where used: Local video preview after callback.
            Returns: None; reschedules only for a current video source.
            """
            self.preview_video_after_id = None
            if Path(self.background_path).suffix.lower() not in MIRROR_VIDEO_EXTENSIONS:
                return
            if not self.preview_render_worker_active:
                self.render_preview()
            self.schedule_preview_video_animation()

        self.preview_video_after_id = self.preview_label.after(67, advance_video)

    def sync_preview_animations(self) -> None:
        """What: Start or cancel scrolling and GIF timers based on current state.

        Why included: Preview animations should run only when their content is active.
        Where used: Preview rendering, payload changes, and image application callbacks.
        Returns: None; reconciles timer IDs with current scroll/GIF state.
        """
        if self.is_preview_scroll_active() and int(self.preview_scroll_cycle_height or 0) > 0:
            self.schedule_preview_scroll_animation()
        else:
            self.cancel_preview_scroll_animation()
            self.preview_scroll_offset = 0
        has_gif_frames = isinstance(self.preview_gif_frames, tuple) and len(self.preview_gif_frames) > 1
        if has_gif_frames:
            self.schedule_preview_gif_animation()
        else:
            self.cancel_preview_gif_animation()
        if Path(self.background_path).suffix.lower() in MIRROR_VIDEO_EXTENSIONS and self.preview_video_reader is not None:
            self.schedule_preview_video_animation()
        elif self.preview_video_after_id and isinstance(self.preview_label, tk.Label):
            self.preview_label.after_cancel(self.preview_video_after_id)
            self.preview_video_after_id = None

    def render_preview(self) -> None:
        """What: Synchronize preview wrapping and request a current-image render.

        Why included: Widget size and content can change independently; both must be
        reflected before the next displayed frame.
        Where used: Main preview refresh callbacks and animation frames.
        Returns: None; no-ops before the preview widgets are built.
        """
        if not isinstance(self.preview_container, tk.Frame) or not isinstance(self.preview_label, tk.Label):
            return
        self.sync_preview_frame_size()
        self.request_preview_render()

    def request_preview_render(self) -> None:
        """What: Snapshot preview state and render the image asynchronously.

        Why included: Pillow composition can be expensive; request tokens keep obsolete
        worker results from replacing newer content.
        Where used: render_preview() and preview-container resize handling.
        Returns: None; applies image/fallback updates through the Tk callback queue.
        """
        if not isinstance(self.preview_container, tk.Frame) or not isinstance(self.preview_label, tk.Label):
            return
        preview_width, preview_height = self.get_preview_render_size()
        display_text = self.preview_text if self.source.casefold() == "preparation" else (self.preview_text or "Secondary Screen Preview")
        corner_text = self.get_live_corner_text()
        preview_font = normalize_font_value(self.font_value) or normalize_font_value(self.font_value_factory()) or ("Segoe UI", 12, "normal")
        self.preview_container.configure(bg="#000000")
        if Image is None or ImageTk is None or ImageOps is None:
            self.preview_image = None
            self.preview_label.configure(
                bg="#000000",
                fg="#ffffff",
                text=display_text,
                justify="center",
                anchor="center",
                font=preview_font,
                image="",
            )
            return
        preview_scroll_offset = int(self.preview_scroll_offset or 0)
        preview_scroll_cycle_height = int(self.preview_scroll_cycle_height or 0)
        background_path = str(self.background_path or self._default_background_path())
        scroll_enabled = bool(self.scroll_enabled)
        source_value = str(self.source or "")
        welcome_sections = dict(self.welcome_sections) if isinstance(self.welcome_sections, dict) else None
        display_columns = [dict(column, verses=list(column.get("verses") or [])) for column in self.display_columns if isinstance(column, dict)]
        selected_verse_index = int(self.selected_verse_index or 0)
        self.preview_render_request_token += 1
        request_token = int(self.preview_render_request_token)

        def build_preview_image() -> None:
            """What: Resolve and render the background plus the captured overlay state.

            Why included: Image loading and fitting should happen outside the Tk event loop.
            Where used: build_latest_preview() on the MirrorPreviewRender worker.
            Returns: None; queues image application or a fallback on the UI thread.
            """
            try:
                resolved_path = self.resolve_background_path(Path(background_path))
                preview_source = self.get_cached_preview_background(resolved_path, preview_width, preview_height)
                rendered_image, next_scroll_cycle_height = self.render_preview_pil_image(
                    preview_source,
                    display_text,
                    preview_font,
                    corner_text=corner_text,
                    source_value=source_value,
                    background_path=background_path,
                    scroll_enabled=scroll_enabled,
                    welcome_sections=welcome_sections,
                    preview_scroll_offset=preview_scroll_offset,
                    preview_size=(preview_width, preview_height),
                    display_columns=display_columns,
                    selected_verse_index=selected_verse_index,
                )

                def apply_rendered_preview() -> None:
                    """What: Apply the rendered Pillow image if its request token is current.

                    Why included: Tk PhotoImage creation and widget changes must run on Tk's thread.
                    Where used: UI callback queued after successful preview composition.
                    Returns: None; updates preview_image, label, and animation timers.
                    """
                    if request_token != int(self.preview_render_request_token):
                        return
                    self.preview_scroll_cycle_height = next_scroll_cycle_height
                    if rendered_image is None:
                        self.preview_image = None
                        self.preview_label.configure(
                            bg="#000000",
                            fg="#ffffff",
                            text=display_text,
                            justify="center",
                            anchor="center",
                            font=preview_font,
                            image="",
                        )
                        self.sync_preview_animations()
                        return
                    self.preview_image = ImageTk.PhotoImage(rendered_image)
                    self.preview_label.configure(bg="#000000", fg="#ffffff", text="", justify="center", anchor="center", font=preview_font, image=self.preview_image)
                    self.sync_preview_animations()

                self.schedule_on_ui_thread(apply_rendered_preview)
            except Exception as error:
                log_exception("mirror.request_preview_render", error, details=f"source={source_value} background={background_path}")
                def apply_fallback_preview() -> None:
                    """What: Show plain preview text when the captured render fails.

                    Why included: A missing/corrupt image should not leave stale pixels displayed.
                    Where used: UI callback queued after a preview-worker exception.
                    Returns: None; ignores results for superseded preview requests.
                    """
                    if request_token != int(self.preview_render_request_token):
                        return
                    self.preview_image = None
                    self.preview_label.configure(
                        bg="#000000",
                        fg="#ffffff",
                        text=display_text,
                        justify="center",
                        anchor="center",
                        font=preview_font,
                        image="",
                    )
                    self.sync_preview_animations()

                self.schedule_on_ui_thread(apply_fallback_preview)

        def build_latest_preview() -> None:
            """What: Serialize Bible rendering and skip work for superseded requests.

            Why included: Bible composition is expensive and parallel renders can contend
            for shared state/resources.
            Where used: Worker target launched by request_preview_render().
            Returns: None; renders non-Bible previews directly and locks Bible previews.
            """
            if request_token != int(self.preview_render_request_token):
                return
            if source_value.casefold() != "bible":
                build_preview_image()
                return
            with self.bible_preview_render_lock:
                if request_token != int(self.preview_render_request_token):
                    return
                build_preview_image()

        with self.preview_render_lock:
            self.pending_preview_render = build_latest_preview
            if self.preview_render_worker_active:
                return
            self.preview_render_worker_active = True

        def drain_preview_requests() -> None:
            """What: Render at most one frame at a time, keeping only the newest pending request.

            Why included: Scroll/GIF ticks must not accumulate threads when rendering is slower than animation.
            Where used: The single MirrorPreviewRender worker started by request_preview_render().
            Returns: None; releases worker ownership when no pending frame remains.
            """
            while True:
                with self.preview_render_lock:
                    render_request = self.pending_preview_render
                    self.pending_preview_render = None
                    if render_request is None:
                        self.preview_render_worker_active = False
                        return
                render_request()

        threading.Thread(target=drain_preview_requests, name="MirrorPreviewRender", daemon=True).start()

    def ensure_secondary_controller(self, show_error: bool = True) -> dict[str, object] | None:
        """What: Lazily create the OBS-backed secondary controller and cache its callbacks.

        Why included: OBS startup should occur only when output is first needed.
        Where used: Secondary toggle and push operations.
        Returns: Controller callback mapping, or None when creation fails.
        """
        if isinstance(self.secondary_controller, dict):
            return self.secondary_controller
        try:
            controller = create_obs_secondary_mirror_window(
                app_base_path=self.get_app_base_path(),
                initial_window_mode="projector",
                default_background_path=self._default_background_path(),
                get_display_config=self.get_display_config,
                resolve_background_path=self.resolve_background_path,
                serialize_overlay_font=self.serialize_overlay_font,
                scroll_speed_options=("Slow", "Medium", "Fast"),
                get_scroll_timestamp=time.time,
                build_hot_background_paths=self.build_hot_background_paths,
                preload_backgrounds=lambda _background_paths: None,
            )
        except Exception as error:
            if show_error:
                messagebox.showerror("Secondary Screen", str(error), parent=self.root)
                self.enabled_var.set(False)
            return None
        self.secondary_controller = controller
        if self.hot_background_paths:
            try:
                controller["set_hot_backgrounds"](list(self.hot_background_paths))
            except Exception:
                pass
        return controller

    def set_hot_backgrounds(self, background_paths: list[Path | str | None]) -> None:
        """What: Store likely upcoming background paths and forward them to OBS if ready.

        Why included: Preloading can reduce the delay when queue selections change.
        Where used: Main queue-change callback through sync_secondary_hot_backgrounds().
        Returns: None; retains the list for a controller created later.
        """
        self.hot_background_paths = list(background_paths or [])
        if isinstance(self.secondary_controller, dict):
            try:
                self.secondary_controller["set_hot_backgrounds"](list(self.hot_background_paths))
            except Exception:
                pass

    def push_to_secondary(self) -> None:
        """What: Immediately synchronize visibility and live content to the OBS controller.

        Why included: Provides a direct path for forcing the current state to external output.
        Where used: Initial setup or explicit refresh paths that require synchronous dispatch.
        Returns: None; skips duplicate signatures and disables output after controller errors.
        """
        started_at = time.perf_counter()
        controller = self.secondary_controller
        if not self.enabled_var.get():
            self.last_secondary_signature = (False,)
            if isinstance(controller, dict):
                try:
                    controller["set_visible"](False)
                except Exception:
                    pass
            return
        if not isinstance(controller, dict):
            controller = self.ensure_secondary_controller()
        if not isinstance(controller, dict):
            return
        secondary_signature = self.build_secondary_signature()
        if secondary_signature == self.last_secondary_signature:
            return
        try:
            controller["set_visible"](True)
            controller["set_content"](
                self.resolve_background_path(Path(str(self.background_path or self._default_background_path()))),
                self.preview_text,
                normalize_font_value(self.font_value) or normalize_font_value(self.font_value_factory()) or ("Segoe UI", 12, "normal"),
                None,
                self.scroll_enabled,
                self.scroll_speed,
                None,
                self.get_live_corner_text(),
                None,
                None,
                self.source,
                self.welcome_sections,
                self.transition_revision,
                self.display_columns,
                self.selected_verse_index,
            )
            self.last_secondary_signature = secondary_signature
            log_timing("mirror.push_to_secondary", (time.perf_counter() - started_at) * 1000.0, threshold_ms=20.0, details=f"source={self.source} scroll={int(self.scroll_enabled)}")
        except Exception as error:
            messagebox.showerror("Secondary Screen", str(error), parent=self.root)
            self.enabled_var.set(False)

    def request_push_to_secondary(self, force: bool = False) -> None:
        """What: Snapshot live state and schedule a token-guarded worker push to OBS.

        Why included: Secondary-process updates must not block Tk, and stale requests must
        not overwrite newer selections.
        Where used: set_live_payload() and the secondary-screen toggle workflow.
        Returns: None; suppresses duplicate/pending signatures unless force is true.
        """
        enabled = bool(self.enabled_var.get())
        background_path = self.resolve_background_path(Path(str(self.background_path or self._default_background_path())))
        payload_text = str(self.preview_text or "")
        payload_font = normalize_font_value(self.font_value) or normalize_font_value(self.font_value_factory()) or ("Segoe UI", 12, "normal")
        payload_scroll_enabled = bool(self.scroll_enabled)
        payload_scroll_speed = int(self.scroll_speed or 7)
        payload_preview_key = self.get_live_corner_text()
        payload_source = str(self.source or "")
        payload_sections = dict(self.welcome_sections) if isinstance(self.welcome_sections, dict) else None
        payload_display_columns = list(self.display_columns) if isinstance(self.display_columns, list) else []
        payload_selected_verse_index = max(0, int(self.selected_verse_index or 0))
        transition_revision = int(self.transition_revision or 0)
        secondary_signature = self.build_secondary_signature() if enabled else (False,)
        if not force and (secondary_signature == self.last_secondary_signature or secondary_signature == self.pending_secondary_signature):
            return
        self.pending_secondary_signature = secondary_signature
        self.secondary_push_request_token += 1
        request_token = int(self.secondary_push_request_token)

        def run_push() -> None:
            """What: Serialize and perform the current visibility/content update off-thread.

            Why included: Controller calls and media preparation can take time; a lock and
            request token prevent concurrent stale pushes.
            Where used: Daemon thread launched by request_push_to_secondary().
            Returns: None; queues signature updates/errors for Tk-thread handling.
            """
            with self.secondary_push_lock:
                if request_token != int(self.secondary_push_request_token):
                    return
                try:
                    started_at = time.perf_counter()
                    controller = self.secondary_controller
                    if not enabled:
                        if isinstance(controller, dict):
                            controller["set_visible"](False)
                        def apply_hidden_signature() -> None:
                            """What: Commit the hidden-state signature if this request remains current.

                            Why included: Duplicate disabled-state pushes should be suppressed afterward.
                            Where used: UI queue callback after the secondary controller is hidden.
                            Returns: None; clears the matching pending signature.
                            """
                            if request_token != int(self.secondary_push_request_token):
                                return
                            self.last_secondary_signature = secondary_signature
                            if self.pending_secondary_signature == secondary_signature:
                                self.pending_secondary_signature = None

                        self.schedule_on_ui_thread(apply_hidden_signature)
                        log_timing("mirror.push_to_secondary", (time.perf_counter() - started_at) * 1000.0, threshold_ms=20.0, details="source= scroll=0")
                        return
                    if not isinstance(controller, dict):
                        controller = self.ensure_secondary_controller(show_error=False)
                    if not isinstance(controller, dict):
                        raise RuntimeError("Secondary screen controller is unavailable.")
                    controller["set_visible"](True)
                    controller["set_content"](
                        background_path,
                        payload_text,
                        payload_font,
                        None,
                        payload_scroll_enabled,
                        payload_scroll_speed,
                        None,
                        payload_preview_key,
                        None,
                        None,
                        payload_source,
                        payload_sections,
                        transition_revision,
                        payload_display_columns,
                        payload_selected_verse_index,
                    )
                    def apply_visible_signature() -> None:
                        """What: Commit the visible content signature if this request is still newest.

                        Why included: Signature state must reflect only output that was actually sent.
                        Where used: UI queue callback after controller.set_content() succeeds.
                        Returns: None; clears the matching pending signature.
                        """
                        if request_token != int(self.secondary_push_request_token):
                            return
                        self.last_secondary_signature = secondary_signature
                        if self.pending_secondary_signature == secondary_signature:
                            self.pending_secondary_signature = None

                    self.schedule_on_ui_thread(apply_visible_signature)
                    log_timing("mirror.push_to_secondary", (time.perf_counter() - started_at) * 1000.0, threshold_ms=20.0, details=f"source={payload_source} scroll={int(payload_scroll_enabled)}")
                except Exception as error:
                    self.last_secondary_signature = None
                    if self.pending_secondary_signature == secondary_signature:
                        self.pending_secondary_signature = None
                    self.schedule_on_ui_thread(lambda error=error: (messagebox.showerror("Secondary Screen", str(error), parent=self.root), self.enabled_var.set(False)))

        threading.Thread(target=run_push, name="MirrorSecondaryPush", daemon=True).start()

    def set_live_payload(self, payload: dict[str, object] | None, force: bool = False) -> None:
        """What: Normalize incoming Display content and synchronize preview/output state.

        Why included: Display-tab selection is the shared source of live content; unchanged
        payloads should not trigger expensive redraws or OBS updates.
        Where used: Injected into the Display tab as its live-change callback.
        Returns: None; clears state for an empty payload and schedules rendering/pushes otherwise.
        """
        payload_signature = self.build_live_payload_signature(payload)
        if not force and payload_signature == self.last_live_payload_signature:
            return
        started_at = time.perf_counter()
        if force:
            self.transition_revision += 1
        self.last_live_payload_signature = payload_signature
        if not payload:
            self.cancel_song_metadata_timer()
            self.show_song_metadata = False
            self.preview_text = ""
            self.preview_key = ""
            self.song_title = ""
            self.license_provider = ""
            self.license_author = ""
            self.license_info = ""
            self.font_value = None
            self.scroll_enabled = False
            self.scroll_speed = 7
            self.background_path = ""
            self.source = ""
            self.welcome_sections = None
            self.display_columns = []
            self.selected_verse_index = 0
            self.preview_scroll_offset = 0
            self.clear_preview_background_cache()
            self.clear_preview_overlay_cache()
        else:
            self.preview_text = str(payload.get("text") or "")
            self.preview_key = str(payload.get("preview_key") or "").strip()
            self.song_title = str(payload.get("title") or "").strip()
            self.license_provider = normalize_license_provider(payload.get("license_provider"))
            self.license_author = str(payload.get("license_author") or "").strip()
            self.license_info = str(payload.get("license_info") or "").strip()
            self.font_value = normalize_font_value(payload.get("font_value"))
            self.scroll_enabled = bool(payload.get("scroll_enabled"))
            self.scroll_speed = self._normalize_scroll_speed(payload.get("scroll_speed"), default=7)
            self.source = str(payload.get("source") or "").strip()
            self.welcome_sections = payload.get("welcome_sections") if isinstance(payload.get("welcome_sections"), dict) else None
            self.display_columns = payload.get("display_columns") if isinstance(payload.get("display_columns"), list) else []
            self.selected_verse_index = max(0, int(payload.get("selected_verse_index") or 0))
            background_value = str(payload.get("background_path") or "").strip()
            next_background_path = str(self.resolve_background_path(Path(background_value))) if background_value else ""
            background_changed = next_background_path != str(self.background_path or "")
            self.background_path = next_background_path
            if not self.is_preview_scroll_active():
                self.preview_scroll_offset = 0
            if background_changed:
                self.clear_preview_background_cache()
            self.clear_preview_overlay_cache()
            self.start_song_metadata_timer()
        self.schedule_preview_refresh()
        log_timing("mirror.set_live_payload", (time.perf_counter() - started_at) * 1000.0, threshold_ms=20.0, details=f"has_payload={int(bool(payload))}")
        if not bool(self.enabled_var.get()):
            self.last_secondary_signature = (False,)
            self.pending_secondary_signature = None
            return
        self.request_push_to_secondary(force=force)

    def handle_secondary_toggle(self) -> None:
        """What: Request output synchronization after the activation checkbox changes.

        Why included: Toggling output visibility should reuse the normal signature/dedup path.
        Where used: Activate Secondary Screen checkbutton in build_section().
        Returns: None; schedules a secondary-controller update.
        """
        self.request_push_to_secondary()

    def close(self) -> None:
        """What: Cancel preview timers and close the OBS-backed controller if it exists.

        Why included: Pending callbacks and external processes must not outlive the Tk app.
        Where used: Main-window WM_DELETE_WINDOW cleanup in main.py.
        Returns: None; ignores controller close errors during shutdown.
        """
        self.cancel_preview_refresh()
        self.cancel_song_metadata_timer()
        self.cancel_preview_scroll_animation()
        self.cancel_preview_gif_animation()
        self.watchdog_closed = True
        self._sync_keep_awake()
        if self.preview_video_after_id and isinstance(self.preview_label, tk.Label):
            self.preview_label.after_cancel(self.preview_video_after_id)
            self.preview_video_after_id = None
        self.preview_render_request_token += 1
        with self.preview_render_lock:
            self.pending_preview_render = None
        self.close_preview_video_reader()
        if isinstance(self.secondary_controller, dict):
            try:
                self.secondary_controller["close"]()
            except Exception:
                pass
