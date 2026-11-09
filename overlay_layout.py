"""Shared Pillow font fitting, text layout, color choice, and overlay rendering."""

from __future__ import annotations

import functools
import queue
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageOps = None


DEFAULT_FONT_FAMILY = "Segoe UI"
DEFAULT_FONT_STYLE = "normal"
DEFAULT_FONT_SIZE = 12
PREVIEW_REFERENCE_WIDTH = 320
PREVIEW_REFERENCE_HEIGHT = 180
DEFAULT_FILL_COLOR = "#ffffff"
DEFAULT_STROKE_COLOR = "#000000"
DEFAULT_STROKE_WIDTH = 1
DEFAULT_STROKE_WIDTH_RATIO = 0.06
DISPLAY_FONT_FAMILY: str | None = None
DISPLAY_FONT_STYLE: str | None = None
DISPLAY_OUTLINE_WIDTH: int | None = None
DISPLAY_TEXT_COLOR: str = "Auto"
DISPLAY_KEY_SIZE = 12
DEFAULT_TEXT_PADDING = 18
DEFAULT_MAX_TEXT_HEIGHT_RATIO = 0.08
DEFAULT_MAX_TEXT_MULTIPLIER = 2
DEFAULT_MIN_TEXT_SIZE = 12
DEFAULT_LINE_SPACING_RATIO = 0.28
DEFAULT_CORNER_FONT_SIZE = 6
LUMINANCE_SWITCH_THRESHOLD = 140
SECONDARY_LAYOUT_SIZE_BOOST = 1.1
WELCOME_SECTION_GAP_MULTIPLIER = 2.0
_BIBLE_LOCKED_FONT_CACHE: dict[tuple[object, ...], tuple[str, int, str]] = {}
_BACKGROUND_LUMINANCE_CACHE: dict[str, tuple[float, int, float]] = {}
_BACKGROUND_LUMINANCE_LOCK = threading.Lock()


@dataclass(frozen=True)
class OverlayLayout:
    """Store a fitted text block and the rendering parameters chosen for it.

    What: Bundles wrapped text, primary/corner fonts, spacing, fill, and outline.
    Why included: Fitting and rendering need to share one immutable result object.
    Where used: Layout fitters and overlay image renderers in this module.
    Returns: Dataclass instances expose these values as fields.
    """
    text: str
    font_value: tuple[str, int, str]
    line_spacing: int
    fill_color: str
    stroke_color: str
    corner_font_value: tuple[str, int, str]


@dataclass(frozen=True)
class SectionedOverlayLayout:
    """Store independently fitted Welcome text sections and shared render styling.

    What: Holds centered, centered-detail, and left-detail strings with their common
    font, line spacing, fill, and outline colors.
    Why included: Welcome fields need distinct alignment while sharing fitted metrics.
    Where used: Sectioned layout fitter and Welcome overlay renderer.
    Returns: Dataclass instances expose the fitted section values as fields.
    """
    centered_text: str
    centered_detail_text: str
    detail_text: str
    font_value: tuple[str, int, str]
    line_spacing: int
    fill_color: str
    stroke_color: str


def fit_background_image(source_image, size: tuple[int, int], *, video: bool = False):
    """What: Orient and fit background media without cropping portrait images.

    Why included: Tab previews and live output must use the same image geometry.
    Where used: Shared preview background rendering; videos retain cover fitting.
    Returns: RGB Pillow image at the requested dimensions.
    """
    oriented = ImageOps.exif_transpose(source_image).convert("RGB")
    fit = ImageOps.pad if not video and oriented.width < oriented.height else ImageOps.fit
    return fit(oriented, size, method=Image.Resampling.LANCZOS)


def normalize_font_value(font_value: object) -> tuple[str, int, str]:
    """Normalize an arbitrary font value and apply active display overrides.

    What: Accepts a family/size/style sequence, falls back to defaults for invalid
    input, and substitutes configured global family or style overrides.
    Why included: Every renderer needs a safe, consistent font tuple.
    Where used: Font loading, sizing, fitting, and rendering helpers.
    Returns: Tuple of family, absolute integer size, and style.
    """
    if not isinstance(font_value, (list, tuple)) or len(font_value) < 2:
        family, size, style = DEFAULT_FONT_FAMILY, DEFAULT_FONT_SIZE, DEFAULT_FONT_STYLE
    else:
        try:
            family = str(font_value[0] or DEFAULT_FONT_FAMILY)
            size = abs(int(font_value[1] or DEFAULT_FONT_SIZE))
            style = str(font_value[2] or DEFAULT_FONT_STYLE) if len(font_value) > 2 else DEFAULT_FONT_STYLE
        except Exception:
            family, size, style = DEFAULT_FONT_FAMILY, DEFAULT_FONT_SIZE, DEFAULT_FONT_STYLE
    return (DISPLAY_FONT_FAMILY or family, size, DISPLAY_FONT_STYLE or style)


def set_display_font(family: object, style: object) -> None:
    """Set or clear the process-wide font family and style overrides.

    What: Trims values and stores non-empty family/style strings, or None to disable.
    Why included: Display Settings must affect all renderer calls consistently.
    Where used: Main application's display-settings change handler.
    Returns: None; updates module-level rendering preferences.
    """
    global DISPLAY_FONT_FAMILY, DISPLAY_FONT_STYLE
    normalized_family = str(family or "").strip()
    normalized_style = str(style or "").strip().lower()
    DISPLAY_FONT_FAMILY = normalized_family or None
    DISPLAY_FONT_STYLE = normalized_style or None


def set_display_outline_width(width: object | None) -> None:
    """Set an explicit text outline width or return to automatic sizing.

    What: Parses a non-negative integer; None, Auto, or invalid text clears the override.
    Why included: Display Settings allow a fixed outline while Auto scales with font size.
    Where used: Main application's outline-size menu handler.
    Returns: None; updates the global outline-width setting.
    """
    global DISPLAY_OUTLINE_WIDTH
    if width is None or str(width).strip().casefold() == "auto":
        DISPLAY_OUTLINE_WIDTH = None
        return
    try:
        DISPLAY_OUTLINE_WIDTH = max(0, int(width))
    except (TypeError, ValueError):
        DISPLAY_OUTLINE_WIDTH = None


def get_display_outline_width() -> int | None:
    """Read the current fixed outline-width override.

    What: Returns the configured integer without recalculating automatic sizing.
    Why included: Renderers need to distinguish fixed width from proportional Auto mode.
    Where used: Renderer configuration and display-settings consumers.
    Returns: Non-negative width, or None when automatic sizing is active.
    """
    return DISPLAY_OUTLINE_WIDTH


def set_display_text_color(color: object) -> None:
    """Set the global display text-color mode to Auto, White, or Black.

    What: Normalizes input and falls back to Auto for unsupported values.
    Why included: A single mode must govern preview and secondary-output colors.
    Where used: Main application's display text-color menu handler.
    Returns: None; updates the module-level color mode.
    """
    global DISPLAY_TEXT_COLOR
    normalized = str(color or "").strip().title()
    if normalized in ("White", "Black"):
        DISPLAY_TEXT_COLOR = normalized
    else:
        DISPLAY_TEXT_COLOR = "Auto"


def get_display_text_color() -> str:
    """Read the normalized global display text-color mode.

    What: Returns the current module-level setting without deriving image colors.
    Why included: Callers can verify whether renderer color selection is automatic.
    Where used: Display-settings tests and renderer integrations.
    Returns: One of Auto, White, or Black.
    """
    return DISPLAY_TEXT_COLOR


def set_display_key_size(size: object) -> None:
    """Set the base key/attribution label size used by all display renderers.

    What: Parses the preview-reference size; zero or below hides the song key.
    Why included: Song key display is optional and needs a user-selectable size.
    Where used: Main application's Settings > Secondary Screen > Key size selector.
    Returns: None; updates the shared renderer setting.
    """
    global DISPLAY_KEY_SIZE
    try:
        parsed = int(size)
    except (TypeError, ValueError):
        DISPLAY_KEY_SIZE = 12
        return
    DISPLAY_KEY_SIZE = 0 if parsed <= 0 else max(2, min(32, parsed))


def is_display_key_hidden() -> bool:
    """Report whether the operator chose to hide the song key entirely.

    What: Returns True when the configured key size is the hidden sentinel (0).
    Why included: Corner-text builders must omit the key while keeping license
    attribution readable at a normal size.
    Where used: Mirror and Library corner-text composition.
    Returns: True when the key must not be displayed.
    """
    return DISPLAY_KEY_SIZE == 0


def get_display_key_size(width: int, height: int) -> int:
    """Scale the selected preview key size proportionally to output dimensions.

    What: Applies the same preview-reference scale used by primary overlay text;
    a hidden key falls back to the default size so attribution stays readable.
    Why included: Preview and secondary-screen corner labels must keep matching proportions.
    Where used: Library, mirror, and OBS overlay fitters.
    Returns: Key font size in pixels for the requested render dimensions.
    """
    base_size = DISPLAY_KEY_SIZE or 12
    return max(2, round(base_size * get_overlay_scale(max(1, int(width)), max(1, int(height)))))


def get_corner_detail_size(key_size: int, width: int, height: int) -> int:
    """What: Size the smaller song-detail lines shown under the corner key.

    Why included: Title and license lines must stay smaller than the key at every output resolution.
    Where used: Corner-text rendering in the preview, mirror, and OBS overlays.
    Returns: Detail font size in pixels, always at least one pixel below the key size.
    """
    scale = get_overlay_scale(max(1, int(width)), max(1, int(height)))
    key_size = max(2, int(key_size))
    return max(1, min(key_size - 1, max(round(scale), key_size - round(2 * scale))))


def clear_background_luminance_cache() -> None:
    """Clear cached background-brightness samples after external media changes.

    What: Empties the luminance cache while holding its lock.
    Why included: Replaced media may retain a path and size yet need fresh color analysis.
    Where used: Background replacement and cache invalidation workflows.
    Returns: None; subsequent color selection re-samples the background.
    """
    with _BACKGROUND_LUMINANCE_LOCK:
        _BACKGROUND_LUMINANCE_CACHE.clear()


def start_async_preview_render(
    *,
    widget,
    state: dict[str, object],
    request_key: str,
    queue_key: str,
    is_active: Callable[[], bool],
    render_worker: Callable[[], object],
    apply_result: Callable[[object], None],
    poll_delay_ms: int = 15,
) -> None:
    """Render a preview on a worker and apply it only if its request is still current.

    What: Runs render_worker off-thread, then polls a size-one queue on Tk and checks
    activity, request ID, and queue identity before calling apply_result.
    Why included: Full-resolution preview composition must not freeze the UI, and stale
    results must not replace a newer selection.
    Where used: Library, Welcome, and Preparation background/preview workflows.
    Returns: None; schedules UI polling and applies the result on the widget's thread.
    """
    request_id = int(state.get(request_key) or 0) + 1
    state[request_key] = request_id
    render_queue: queue.Queue = queue.Queue(maxsize=1)
    state[queue_key] = render_queue

    def run_worker() -> None:
        """What: Execute render_worker and place its result in the bounded queue.

        Why included: Image composition may be slow and must stay off the Tk thread.
        Where used: Daemon thread launched by start_async_preview_render().
        Returns: None; converts worker exceptions to a None result and ignores a full queue.
        """
        try:
            result = render_worker()
        except Exception:
            result = None
        try:
            render_queue.put_nowait(result)
        except queue.Full:
            pass

    def poll_render_queue() -> None:
        """What: Poll for the worker result and apply it only for the active request.

        Why included: Tk updates belong on the UI thread, and superseded renders are stale.
        Where used: Initial poll and widget.after() retries in start_async_preview_render().
        Returns: None; reschedules while the queue is empty or calls apply_result once ready.
        """
        if not is_active():
            return
        if request_id != int(state.get(request_key) or 0):
            return
        if state.get(queue_key) is not render_queue:
            return
        try:
            result = render_queue.get_nowait()
        except queue.Empty:
            widget.after(poll_delay_ms, poll_render_queue)
            return
        apply_result(result)

    threading.Thread(target=run_worker, daemon=True).start()
    poll_render_queue()


def resolve_font_path(font_family: str, font_style: str) -> Path | None:
    """Resolve a supported Windows font family/style to its installed font file.

    What: Looks up the family/style filename in the renderer's supported font map.
    Why included: Pillow needs a filesystem path to render the same installed typeface.
    Where used: get_pil_font() before loading or reusing a cached font.
    Returns: Existing font Path, or None when unsupported or unavailable.
    """
    windows_fonts = Path("C:/Windows/Fonts")
    font_map = {
        "segoe ui": {"normal": "segoeui.ttf", "bold": "segoeuib.ttf", "italic": "segoeuii.ttf", "bold italic": "segoeuiz.ttf"},
        "arial": {"normal": "arial.ttf", "bold": "arialbd.ttf", "italic": "ariali.ttf", "bold italic": "arialbi.ttf"},
        "calibri": {"normal": "calibri.ttf", "bold": "calibrib.ttf", "italic": "calibrii.ttf", "bold italic": "calibriz.ttf"},
        "cambria": {"normal": "cambria.ttc", "bold": "cambriab.ttf", "italic": "cambriai.ttf", "bold italic": "cambriaz.ttf"},
        "tahoma": {"normal": "tahoma.ttf", "bold": "tahomabd.ttf", "italic": "tahoma.ttf", "bold italic": "tahomabd.ttf"},
        "georgia": {"normal": "georgia.ttf", "bold": "georgiab.ttf", "italic": "georgiai.ttf", "bold italic": "georgiaz.ttf"},
        "times new roman": {"normal": "times.ttf", "bold": "timesbd.ttf", "italic": "timesi.ttf", "bold italic": "timesbi.ttf"},
        "verdana": {"normal": "verdana.ttf", "bold": "verdanab.ttf", "italic": "verdanai.ttf", "bold italic": "verdanaz.ttf"},
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
def _load_pil_font_cached(font_path_str: str | None, size: int):
    """What: Load and cache a TrueType font, falling back to Pillow's default.

    Why included: Font decoding is repeated heavily during layout measurement.
    Where used: get_pil_font() through the bounded LRU cache.
    Returns: Pillow font object; default font is used if path loading fails.
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


def get_pil_font(font_value: object):
    """Load or reuse the Pillow font matching a normalized font value.

    What: Returns None without Pillow; otherwise normalizes settings, resolves the
    installed file, and retrieves a cached font object.
    Why included: Layout measurement and image rendering share identical font loading.
    Where used: Text fitting, metrics, and overlay renderers.
    Returns: Pillow font handle or None when the imaging library is unavailable.
    """
    if ImageFont is None:
        return None
    family, size, style = normalize_font_value(font_value)
    font_path = resolve_font_path(family, style)
    return _load_pil_font_cached(str(font_path) if font_path else None, size)


def get_line_spacing(font_size: int, ratio: float = DEFAULT_LINE_SPACING_RATIO) -> int:
    """Calculate a minimum-safe line gap from font size and a ratio.

    What: Multiplies a positive font size by ratio and enforces a four-pixel minimum.
    Why included: Fitting and drawing must use consistent multiline spacing.
    Where used: Text layout and overlay render functions.
    Returns: Integer line-spacing value in pixels.
    """
    return max(4, round(max(1, font_size) * ratio))


def render_scrolling_preview(background, text: str, font_value, render_width: int, render_height: int,
                             offset: int, cache: dict, *, background_path=None, corner_text: str = "",
                             size_boost: float = 1.15):
    """What: Rasterize scrolling text at output resolution once and scale it into any preview.

    Why included: Welcome and mirror previews must share typography and outline proportions with OBS.
    Where used: Scrolling Welcome and mirror preview renderers.
    Returns: Composited RGB preview and cycle height; caches text/corner layers by render settings.
    """
    if Image is None or ImageDraw is None:
        return background.convert("RGB"), 0
    width, height = background.size
    font_value = normalize_font_value(font_value)
    fill_color, stroke_color = choose_text_colors(background, background_path=background_path)
    key = (text, font_value, corner_text, width, height, render_width, render_height,
           size_boost, get_display_outline_width(), fill_color, stroke_color)
    if cache.get("key") != key:
        layout = fit_overlay_layout(text, scale_overlay_font_value(font_value, render_width, render_height),
                                    render_width, render_height, scroll_enabled=True,
                                    base_image=background, background_path=background_path, size_boost=size_boost)
        font = get_pil_font(layout.font_value)
        stroke = get_effective_stroke_width(layout.font_value[1])
        bounds = get_shared_measure_draw().multiline_textbbox((0, 0), layout.text, font=font,
                    align="center", spacing=layout.line_spacing, stroke_width=stroke)
        text_height = max(1, int(bounds[3] - bounds[1]))
        strip = Image.new("RGBA", (render_width, text_height), (0, 0, 0, 0))
        draw_x = (render_width - (bounds[2] - bounds[0])) / 2.0 - bounds[0]
        ImageDraw.Draw(strip).multiline_text((draw_x, -bounds[1]), layout.text, font=font,
                    fill=fill_color, stroke_width=stroke, stroke_fill=stroke_color,
                    spacing=layout.line_spacing, align="center")
        scaled_height = max(1, round(text_height * height / render_height))
        text_layer = strip.resize((width, scaled_height), Image.Resampling.LANCZOS)
        corner = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if corner_text.strip():
            corner_size = max(6, round(layout.font_value[1] * 0.32))
            corner_font = get_pil_font((font_value[0], corner_size, font_value[2]))
            full_corner = Image.new("RGBA", (render_width, render_height), (0, 0, 0, 0))
            ImageDraw.Draw(full_corner).multiline_text(
                (render_width - max(8, round(render_width * 0.03)), max(5, round(render_height * 0.025))),
                corner_text, font=corner_font, fill=fill_color, stroke_width=stroke,
                stroke_fill=stroke_color, align="right", anchor="ra")
            corner = full_corner.resize((width, height), Image.Resampling.LANCZOS)
        cache.update(key=key, text_layer=text_layer, corner_layer=corner)
    strip = cache["text_layer"]
    gap = max(18, round(height * 0.15))
    cycle_height = max(1, strip.height + height + gap)
    start_y = height - (int(offset) % cycle_height)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay.alpha_composite(strip, (0, start_y))
    overlay.alpha_composite(strip, (0, start_y + cycle_height))
    overlay = Image.alpha_composite(overlay, cache["corner_layer"])
    return Image.alpha_composite(background.convert("RGBA"), overlay).convert("RGB"), cycle_height


def get_effective_stroke_width(font_size: int, base_stroke_width: int = DEFAULT_STROKE_WIDTH) -> int:
    """Choose the configured outline width or derive one from the font size.

    What: Returns the explicit override when set; otherwise applies the proportional
    ratio with the caller's base width as a minimum.
    Why included: Outlines should remain legible as text is scaled for output.
    Where used: Text measurement, fitting, and image rasterization.
    Returns: Integer stroke width in pixels.
    """
    # Keeps the outline visually consistent regardless of any font size_boost applied per source.
    if DISPLAY_OUTLINE_WIDTH is not None:
        return DISPLAY_OUTLINE_WIDTH
    return max(int(base_stroke_width), round(max(1, int(font_size)) * DEFAULT_STROKE_WIDTH_RATIO))


def get_overlay_scale(width: int, height: int) -> float:
    """Compute a preview-to-output scale that never reduces the base font.

    What: Uses the smaller width/height ratio relative to the reference canvas and
    clamps the result to at least 1.0.
    Why included: Output text should grow with larger displays without shrinking below
    the configured preview size.
    Where used: scale_overlay_font_value() and Bible layout sizing.
    Returns: Positive floating-point scale factor.
    """
    width_scale = width / max(1, PREVIEW_REFERENCE_WIDTH)
    height_scale = height / max(1, PREVIEW_REFERENCE_HEIGHT)
    return max(1.0, min(width_scale, height_scale))


def scale_overlay_font_value(font_value: object, width: int, height: int) -> tuple[str, int, str]:
    """Scale a normalized font tuple for the requested render dimensions.

    What: Applies get_overlay_scale() to the base size while preserving family/style.
    Why included: Preview and secondary display need dimensions-aware typography.
    Where used: Overlay fitters and Bible-column renderers.
    Returns: Family, scaled integer size, and style tuple.
    """
    family, base_size, style = normalize_font_value(font_value)
    scale_factor = get_overlay_scale(width, height)
    scaled_size = max(base_size, round(base_size * scale_factor))
    return (family, scaled_size, style)


def get_center_region_luminance(base_image) -> float:
    """Measure average grayscale luminance in the image's central text region.

    What: Crops the middle 60% by width and 50% by height and averages its grayscale
    histogram.
    Why included: Text color should contrast with the area where the main overlay appears.
    Where used: Background luminance sampling and automatic color selection.
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


def get_background_average_luminance(
    background_path: Path | str | None, sample_image=None
) -> float | None:
    """Estimate and cache the center brightness of a background media file.

    What: Samples one image or up to 24 animation frames, caches by normalized path,
    modification time, and size, and uses a dark fallback for videos.
    Why included: Auto text color must remain stable across animated frames while
    choosing contrast for the background.
    Where used: choose_text_colors() when a background path is available.
    Returns: Average luminance, a sample-image estimate, or None when unavailable.
    """
    if not background_path:
        if sample_image is not None and Image is not None:
            try:
                return get_center_region_luminance(sample_image)
            except Exception:
                return None
        return None
    try:
        resolved = Path(background_path).resolve()
        if not resolved.is_file():
            if sample_image is not None and Image is not None:
                try:
                    return get_center_region_luminance(sample_image)
                except Exception:
                    return None
            return None
        stat = resolved.stat()
        cache_key = str(resolved).casefold()
        with _BACKGROUND_LUMINANCE_LOCK:
            cached = _BACKGROUND_LUMINANCE_CACHE.get(cache_key)
            # Size and mtime invalidate the cached sample when the background file changes in place.
            if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                return cached[2]

        ext = resolved.suffix.lower()
        if ext in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv"}:
            # Video decoding is intentionally avoided here; use the established dark-background default.
            luminance = 0.0
        elif Image is not None:
            # The image context closes the source promptly, including animated-image paths.
            with Image.open(resolved) as img:
                n_frames = getattr(img, "n_frames", 1)
                if n_frames > 1:
                    sample_indices = (
                        list(range(n_frames))
                        if n_frames <= 24
                        # Evenly spaced samples bound work while representing long animations.
                        else [round(i * (n_frames - 1) / 23) for i in range(24)]
                    )
                    luminances: list[float] = []
                    for idx in sample_indices:
                        img.seek(idx)
                        frame_rgb = img.convert("RGB")
                        luminances.append(get_center_region_luminance(frame_rgb))
                    luminance = sum(luminances) / max(1, len(luminances))
                else:
                    img_rgb = img.convert("RGB")
                    luminance = get_center_region_luminance(img_rgb)
        else:
            # Keep contrast selection predictable when Pillow cannot inspect the file.
            luminance = 0.0

        with _BACKGROUND_LUMINANCE_LOCK:
            _BACKGROUND_LUMINANCE_CACHE[cache_key] = (stat.st_mtime, stat.st_size, luminance)
        return luminance
    except Exception:
        # A usable in-memory preview can still provide contrast when the saved path is stale or unreadable.
        if sample_image is not None and Image is not None:
            try:
                return get_center_region_luminance(sample_image)
            except Exception:
                pass
        return None


def choose_text_colors(base_image=None, background_path: Path | str | None = None) -> tuple[str, str]:
    """Choose a readable text fill/outline pair from override or background brightness.

    What: Honors explicit White/Black modes, otherwise uses cached loop-average or
    image luminance to choose dark text on bright backgrounds and vice versa.
    Why included: Automatically selected text should remain legible over media.
    Where used: Overlay layout fitters and field renderers.
    Returns: Pair of fill and stroke color strings.
    """
    mode = DISPLAY_TEXT_COLOR.strip().title()
    if mode == "White":
        return ("#ffffff", "#000000")
    if mode == "Black":
        return ("#000000", "#ffffff")

    bg_path = background_path or getattr(base_image, "_background_path", None)
    luminance: float | None = None
    if bg_path:
        luminance = get_background_average_luminance(bg_path, sample_image=base_image)
    if luminance is None and base_image is not None:
        luminance = getattr(base_image, "_loop_average_luminance", None)
        if luminance is None and Image is not None:
            try:
                luminance = get_center_region_luminance(base_image)
            except Exception:
                luminance = None

    if luminance is not None and luminance >= LUMINANCE_SWITCH_THRESHOLD:
        return ("#000000", "#ffffff")
    return (DEFAULT_FILL_COLOR, DEFAULT_STROKE_COLOR)


_MEASURE_DRAW = None


def get_shared_measure_draw():
    """Return a lazily created one-pixel Pillow draw surface for text metrics.

    What: Creates one transparent RGBA image and ImageDraw object on first use.
    Why included: Reusing a measurement surface avoids repeated image allocations.
    Where used: Text wrapping and bounding-box measurement functions.
    Returns: Shared ImageDraw object, or None when Pillow is unavailable.
    """
    global _MEASURE_DRAW
    if _MEASURE_DRAW is None and Image is not None and ImageDraw is not None:
        _MEASURE_DRAW = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
    return _MEASURE_DRAW


def wrap_text_to_width(text: str, pil_font, max_width: int, stroke_width: int) -> str:
    """Wrap text to a pixel width while preserving blank lines and long words.

    What: Measures tokens with Pillow, wraps between words, and splits tokens that
    exceed the width one character at a time.
    Why included: Fitted overlays must remain inside their text region even for long
    unbroken words or URLs.
    Where used: Overlay fitters and field/Bible layout renderers.
    Returns: Newline-separated wrapped text; returns input unchanged if metrics fail.
    """
    if Image is None or ImageDraw is None or pil_font is None or max_width <= 0:
        return text
    measure_draw = get_shared_measure_draw()
    if measure_draw is None:
        return text

    def measure_width(value: str) -> float:
        """What: Measure one token's pixel width including its outline stroke.

        Why included: Wrapping decisions must reflect the visible rendered extent.
        Where used: wrap_token() and the main line-wrapping loop.
        Returns: Width in pixels, or zero for an empty string.
        """
        if not value:
            return 0.0
        box = measure_draw.textbbox((0, 0), value, font=pil_font, stroke_width=stroke_width)
        return float(box[2] - box[0])

    def wrap_token(token: str) -> list[str]:
        """What: Split a single overlong token into width-fitting character groups.

        Why included: A word wider than the canvas must not overflow its boundary.
        Where used: wrap_text_to_width() when a token alone exceeds max_width.
        Returns: Ordered token fragments; an empty token remains a one-item list.
        """
        parts: list[str] = []
        current = ""
        for character in token:
            candidate = f"{current}{character}"
            if current and measure_width(candidate) > max_width:
                parts.append(current)
                current = character
            else:
                current = candidate
        if current:
            parts.append(current)
        return parts or [token]

    wrapped_lines: list[str] = []
    for raw_line in text.splitlines() or [text]:
        if not raw_line.strip():
            wrapped_lines.append("")
            continue
        words = raw_line.split()
        current_line = ""
        for word in words:
            candidate = word if not current_line else f"{current_line} {word}"
            if not current_line and measure_width(word) > max_width:
                wrapped_parts = wrap_token(word)
                wrapped_lines.extend(wrapped_parts[:-1])
                current_line = wrapped_parts[-1]
                continue
            if current_line and measure_width(candidate) > max_width:
                wrapped_lines.append(current_line)
                if measure_width(word) > max_width:
                    wrapped_parts = wrap_token(word)
                    wrapped_lines.extend(wrapped_parts[:-1])
                    current_line = wrapped_parts[-1]
                else:
                    current_line = word
                continue
            current_line = candidate
        if current_line:
            wrapped_lines.append(current_line)
    return "\n".join(wrapped_lines)


def get_text_bbox(text: str, pil_font, line_spacing: int, anchor: str | None = None, align: str = "center", stroke_width: int | None = None) -> tuple[float, float, float, float]:
    """Measure multiline text bounds using the same spacing and outline as rendering.

    What: Calls Pillow's multiline_textbbox with alignment, anchor, and effective stroke.
    Why included: Fitters need rendered dimensions before drawing overlays.
    Where used: Overlay fitters, field-height calculations, and Bible font fitting.
    Returns: Left, top, right, bottom bounds as floats; zero bounds without Pillow.
    """
    if Image is None or ImageDraw is None or pil_font is None:
        return (0.0, 0.0, 0.0, 0.0)
    measure_draw = get_shared_measure_draw()
    if measure_draw is None:
        return (0.0, 0.0, 0.0, 0.0)
    effective_stroke_width = get_effective_stroke_width(getattr(pil_font, "size", DEFAULT_FONT_SIZE)) if stroke_width is None else int(stroke_width)
    return tuple(
        float(value)
        for value in measure_draw.multiline_textbbox(
            (0, 0),
            text,
            font=pil_font,
            align=align,
            spacing=line_spacing,
            stroke_width=effective_stroke_width,
            anchor=anchor,
        )
    )


def fit_sectioned_overlay_layout(
    centered_text: str,
    centered_detail_text: str,
    detail_text: str,
    font_value: object,
    width: int,
    height: int,
    *,
    base_image=None,
    background_path: Path | str | None = None,
    stroke_width: int = DEFAULT_STROKE_WIDTH,
    padding: int = DEFAULT_TEXT_PADDING,
    min_text_size: int = DEFAULT_MIN_TEXT_SIZE,
    max_text_height_ratio: float = DEFAULT_MAX_TEXT_HEIGHT_RATIO,
    max_text_multiplier: int = DEFAULT_MAX_TEXT_MULTIPLIER,
    line_spacing_ratio: float = DEFAULT_LINE_SPACING_RATIO,
    size_boost: float = 1.0,
) -> SectionedOverlayLayout:
    """Fit centered Welcome text and lower detail sections into one canvas.

    What: Tests descending font sizes, wraps each section with shared styling, and
    returns the largest candidate whose combined dimensions fit.
    Why included: Welcome fields have different alignment and spacing but share a
    single font family and must avoid overlapping the canvas edges.
    Where used: Welcome overlay preview and secondary-display render workflows.
    Returns: SectionedOverlayLayout with wrapped text, selected font, spacing, and colors.
    """
    normalized_font = normalize_font_value(font_value)
    family, requested_size, style = normalized_font
    requested_size = max(min_text_size, round(requested_size * size_boost))
    fill_color, stroke_color = choose_text_colors(base_image, background_path=background_path)
    requested_stroke_width = get_effective_stroke_width(requested_size, stroke_width)
    usable_text_width = max(60, int(width) - ((requested_stroke_width + padding) * 2))
    usable_text_height = max(60, int(height) - ((requested_stroke_width + padding) * 2))
    centered_source = str(centered_text or "").strip()
    centered_detail_source = str(centered_detail_text or "").strip()
    detail_source = str(detail_text or "").strip()
    if not centered_source and not centered_detail_source and not detail_source:
        return SectionedOverlayLayout(
            centered_text="",
            centered_detail_text="",
            detail_text="",
            font_value=(family, requested_size, style),
            line_spacing=get_line_spacing(requested_size, line_spacing_ratio),
            fill_color=fill_color,
            stroke_color=stroke_color,
        )

    max_size = max(
        18,
        min(
            round(max(1, int(height)) * max_text_height_ratio),
            max(18, requested_size * max_text_multiplier),
        ),
    )
    fallback_centered = centered_source
    fallback_centered_detail = centered_detail_source
    fallback_detail = detail_source
    fallback_size = requested_size
    fallback_spacing = get_line_spacing(requested_size, line_spacing_ratio)
    for candidate_size in range(max_size, min_text_size - 1, -2):
        candidate_stroke_width = get_effective_stroke_width(candidate_size, stroke_width)
        candidate_font = get_pil_font((family, candidate_size, style))
        if candidate_font is None:
            continue
        candidate_centered = wrap_text_to_width(centered_source, candidate_font, usable_text_width, candidate_stroke_width) if centered_source else ""
        candidate_centered_detail = wrap_text_to_width(centered_detail_source, candidate_font, usable_text_width, candidate_stroke_width) if centered_detail_source else ""
        candidate_detail = wrap_text_to_width(detail_source, candidate_font, usable_text_width, candidate_stroke_width) if detail_source else ""
        candidate_spacing = get_line_spacing(candidate_size, line_spacing_ratio)
        centered_box = get_text_bbox(candidate_centered, candidate_font, candidate_spacing, align="center") if candidate_centered else (0.0, 0.0, 0.0, 0.0)
        centered_detail_box = get_text_bbox(candidate_centered_detail, candidate_font, candidate_spacing, align="center") if candidate_centered_detail else (0.0, 0.0, 0.0, 0.0)
        detail_box = get_text_bbox(candidate_detail, candidate_font, candidate_spacing, align="left") if candidate_detail else (0.0, 0.0, 0.0, 0.0)
        centered_width = float(centered_box[2] - centered_box[0]) if candidate_centered else 0.0
        centered_height = float(centered_box[3] - centered_box[1]) if candidate_centered else 0.0
        centered_detail_width = float(centered_detail_box[2] - centered_detail_box[0]) if candidate_centered_detail else 0.0
        centered_detail_height = float(centered_detail_box[3] - centered_detail_box[1]) if candidate_centered_detail else 0.0
        detail_width = float(detail_box[2] - detail_box[0]) if candidate_detail else 0.0
        detail_height = float(detail_box[3] - detail_box[1]) if candidate_detail else 0.0
        top_gap = float(max(candidate_spacing, round(candidate_size * WELCOME_SECTION_GAP_MULTIPLIER * DEFAULT_LINE_SPACING_RATIO)) if candidate_centered and candidate_centered_detail else 0)
        middle_gap = float(candidate_spacing if candidate_centered and candidate_detail and not candidate_centered_detail else 0)
        bottom_gap = float(0 if candidate_centered_detail and candidate_detail else 0)
        total_height = centered_height + top_gap + centered_detail_height + middle_gap + bottom_gap + detail_height
        fallback_centered = candidate_centered
        fallback_centered_detail = candidate_centered_detail
        fallback_detail = candidate_detail
        fallback_size = candidate_size
        fallback_spacing = candidate_spacing
        if max(centered_width, centered_detail_width, detail_width) <= usable_text_width and total_height <= usable_text_height:
            return SectionedOverlayLayout(
                centered_text=candidate_centered,
                centered_detail_text=candidate_centered_detail,
                detail_text=candidate_detail,
                font_value=(family, candidate_size, style),
                line_spacing=candidate_spacing,
                fill_color=fill_color,
                stroke_color=stroke_color,
            )
    return SectionedOverlayLayout(
        centered_text=fallback_centered,
        centered_detail_text=fallback_centered_detail,
        detail_text=fallback_detail,
        font_value=(family, fallback_size, style),
        line_spacing=fallback_spacing,
        fill_color=fill_color,
        stroke_color=stroke_color,
    )


def fit_overlay_layout(
    text: str,
    font_value: object,
    width: int,
    height: int,
    *,
    scroll_enabled: bool,
    base_image=None,
    background_path: Path | str | None = None,
    stroke_width: int = DEFAULT_STROKE_WIDTH,
    padding: int = DEFAULT_TEXT_PADDING,
    min_text_size: int = DEFAULT_MIN_TEXT_SIZE,
    max_text_height_ratio: float = DEFAULT_MAX_TEXT_HEIGHT_RATIO,
    max_text_multiplier: int = DEFAULT_MAX_TEXT_MULTIPLIER,
    line_spacing_ratio: float = DEFAULT_LINE_SPACING_RATIO,
    size_boost: float = 1.0,
    corner_font_size: int = DEFAULT_CORNER_FONT_SIZE,
) -> OverlayLayout:
    """Wrap and fit one overlay string to the available canvas dimensions.

    What: Chooses colors and usable bounds, then reduces candidate font size until
    wrapped text fits; scrolling text is wrapped without vertical shrink-to-fit.
    Why included: Rendered text must stay readable and within its output region.
    Where used: Library, Bible, translation, and general overlay render paths.
    Returns: OverlayLayout containing the chosen wrapped text and render parameters.
    """
    normalized_font = normalize_font_value(font_value)
    family, requested_size, style = normalized_font
    requested_size = max(min_text_size, round(requested_size * size_boost))
    fill_color, stroke_color = choose_text_colors(base_image, background_path=background_path)
    requested_stroke_width = get_effective_stroke_width(requested_size, stroke_width)
    usable_text_width = max(60, int(width) - ((requested_stroke_width + padding) * 2))
    usable_text_height = max(60, int(height) - ((requested_stroke_width + padding) * 2))
    if not str(text or "").strip():
        return OverlayLayout(
            text=str(text or ""),
            font_value=(family, requested_size, style),
            line_spacing=get_line_spacing(requested_size, line_spacing_ratio),
            fill_color=fill_color,
            stroke_color=stroke_color,
            corner_font_value=(family, corner_font_size, style),
        )

    if scroll_enabled:
        scroll_font_value = (family, requested_size, style)
        scroll_font = get_pil_font(scroll_font_value)
        wrapped_text = wrap_text_to_width(str(text).strip(), scroll_font, usable_text_width, requested_stroke_width)
        scroll_size = normalize_font_value(scroll_font_value)[1]
        return OverlayLayout(
            text=wrapped_text,
            font_value=(family, scroll_size, style),
            line_spacing=get_line_spacing(scroll_size, line_spacing_ratio),
            fill_color=fill_color,
            stroke_color=stroke_color,
            corner_font_value=(family, corner_font_size, style),
        )

    max_size = max(
        18,
        min(
            round(max(1, int(height)) * max_text_height_ratio),
            max(18, requested_size * max_text_multiplier),
        ),
    )
    fallback_text = str(text).strip()
    fallback_size = requested_size
    fallback_spacing = get_line_spacing(requested_size, line_spacing_ratio)
    for candidate_size in range(max_size, min_text_size - 1, -2):
        candidate_stroke_width = get_effective_stroke_width(candidate_size, stroke_width)
        candidate_font = get_pil_font((family, candidate_size, style))
        if candidate_font is None:
            continue
        candidate_text = wrap_text_to_width(str(text).strip(), candidate_font, usable_text_width, candidate_stroke_width)
        candidate_spacing = get_line_spacing(candidate_size, line_spacing_ratio)
        candidate_box = get_text_bbox(candidate_text, candidate_font, candidate_spacing)
        fallback_text = candidate_text
        fallback_size = candidate_size
        fallback_spacing = candidate_spacing
        text_width = float(candidate_box[2] - candidate_box[0])
        text_height = float(candidate_box[3] - candidate_box[1])
        if text_width <= usable_text_width and text_height <= usable_text_height:
            return OverlayLayout(
                text=candidate_text,
                font_value=(family, candidate_size, style),
                line_spacing=get_line_spacing(candidate_size, line_spacing_ratio),
                fill_color=fill_color,
                stroke_color=stroke_color,
                corner_font_value=(family, corner_font_size, style),
            )
    return OverlayLayout(
        text=fallback_text,
        font_value=(family, fallback_size, style),
        line_spacing=get_line_spacing(fallback_size, line_spacing_ratio),
        fill_color=fill_color,
        stroke_color=stroke_color,
        corner_font_value=(family, corner_font_size, style),
    )


def fit_existing_wrapped_text_layout(
    wrapped_text: str,
    font_value: object,
    width: int,
    height: int,
    *,
    base_image=None,
    background_path: Path | str | None = None,
    stroke_width: int = DEFAULT_STROKE_WIDTH,
    padding: int = DEFAULT_TEXT_PADDING,
    min_text_size: int = DEFAULT_MIN_TEXT_SIZE,
    line_spacing_ratio: float = DEFAULT_LINE_SPACING_RATIO,
    corner_font_size: int = DEFAULT_CORNER_FONT_SIZE,
) -> OverlayLayout:
    """Fit deliberately line-broken text without changing its chosen wrapping.

    What: Tests font sizes from requested down to minimum and measures the original
    line breaks against available width and height.
    Why included: User-authored or verse-structured breaks must remain intact.
    Where used: Rendering paths that receive preformatted text blocks.
    Returns: OverlayLayout with the best fitting size or the minimum-size fallback.
    """
    normalized_font = normalize_font_value(font_value)
    family, requested_size, style = normalized_font
    fill_color, stroke_color = choose_text_colors(base_image, background_path=background_path)
    effective_stroke_width = get_effective_stroke_width(requested_size, stroke_width)
    usable_text_width = max(60, int(width) - ((effective_stroke_width + padding) * 2))
    usable_text_height = max(60, int(height) - ((effective_stroke_width + padding) * 2))
    fallback_size = max(min_text_size, requested_size)
    for candidate_size in range(max(min_text_size, requested_size), min_text_size - 1, -1):
        candidate_font = get_pil_font((family, candidate_size, style))
        if candidate_font is None:
            continue
        candidate_spacing = get_line_spacing(candidate_size, line_spacing_ratio)
        candidate_box = get_text_bbox(wrapped_text, candidate_font, candidate_spacing)
        text_width = float(candidate_box[2] - candidate_box[0])
        text_height = float(candidate_box[3] - candidate_box[1])
        fallback_size = candidate_size
        if text_width <= usable_text_width and text_height <= usable_text_height:
            return OverlayLayout(
                text=wrapped_text,
                font_value=(family, candidate_size, style),
                line_spacing=candidate_spacing,
                fill_color=fill_color,
                stroke_color=stroke_color,
                corner_font_value=(family, corner_font_size, style),
            )
    fallback_spacing = get_line_spacing(fallback_size, line_spacing_ratio)
    return OverlayLayout(
        text=wrapped_text,
        font_value=(family, fallback_size, style),
        line_spacing=fallback_spacing,
        fill_color=fill_color,
        stroke_color=stroke_color,
        corner_font_value=(family, corner_font_size, style),
    )


def render_overlay_image(
    width: int,
    height: int,
    layout: OverlayLayout,
    *,
    corner_text: str = "",
    stroke_width: int = DEFAULT_STROKE_WIDTH,
) :
    """Rasterize fitted centered text and an optional corner label to RGBA.

    What: Draws the primary layout at canvas center and optional corner text at the
    upper-right with matching fill and outline.
    Why included: Previews and display output need a reusable transparent text layer.
    Where used: Media-composition code and higher-level overlay renderers.
    Returns: Transparent Pillow RGBA image, or None when Pillow drawing is unavailable.
    """
    if Image is None or ImageDraw is None:
        return None
    overlay_image = Image.new("RGBA", (max(1, int(width)), max(1, int(height))), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay_image)
    preview_font = get_pil_font(layout.font_value)
    if preview_font is not None and str(layout.text or "").strip():
        draw.multiline_text(
            (width / 2.0, height / 2.0),
            layout.text,
            font=preview_font,
            fill=layout.fill_color,
            stroke_width=get_effective_stroke_width(layout.font_value[1], stroke_width),
            stroke_fill=layout.stroke_color,
            spacing=layout.line_spacing,
            align="center",
            anchor="mm",
        )
    if str(corner_text or "").strip():
        corner_font = get_pil_font(layout.corner_font_value)
        if corner_font is not None:
            margin_x = max(8, round(max(1, int(width)) * 0.03))
            margin_y = max(5, round(max(1, int(height)) * 0.025))
            corner_font_value = normalize_font_value(layout.corner_font_value) or (DEFAULT_FONT_FAMILY, DEFAULT_CORNER_FONT_SIZE, DEFAULT_FONT_STYLE)
            corner_stroke = get_effective_stroke_width(corner_font_value[1], stroke_width)
            corner_spacing = max(2, round(max(1, corner_font_value[1]) * DEFAULT_LINE_SPACING_RATIO))
            corner_lines = str(corner_text).splitlines()
            key_text = corner_lines[0].strip() if corner_lines else ""
            details_text = "\n".join(corner_lines[1:] if key_text else corner_lines).strip()
            current_top = margin_y
            if key_text:
                draw.multiline_text(
                    (width - margin_x, current_top),
                    key_text,
                    font=corner_font,
                    fill=layout.fill_color,
                    stroke_width=corner_stroke,
                    stroke_fill=layout.stroke_color,
                    spacing=corner_spacing,
                    align="right",
                    anchor="ra",
                )
                key_bounds = draw.multiline_textbbox(
                    (0, 0), key_text, font=corner_font, spacing=corner_spacing,
                    stroke_width=corner_stroke, anchor="ra",
                )
                current_top += max(1, key_bounds[3] - key_bounds[1]) + max(2, corner_font_value[1] // 4)
            if details_text:
                details_font_value = (corner_font_value[0], get_corner_detail_size(corner_font_value[1], width, height), corner_font_value[2])
                details_font = get_pil_font(details_font_value)
                if details_font is not None:
                    details_spacing = max(2, round(max(1, details_font_value[1]) * DEFAULT_LINE_SPACING_RATIO))
                    draw.multiline_text(
                        (width - margin_x, current_top),
                        details_text,
                        font=details_font,
                        fill=layout.fill_color,
                        stroke_width=get_effective_stroke_width(details_font_value[1], stroke_width),
                        stroke_fill=layout.stroke_color,
                        spacing=details_spacing,
                        align="right",
                        anchor="ra",
                    )
    return overlay_image


def normalize_text_alignment(value: object, default: str = "center") -> str:
    """Normalize an alignment option to left, center, or right.

    What: Trims and case-folds the requested value, falling back when unsupported.
    Why included: Saved field settings may contain invalid or legacy alignment text.
    Where used: build_sized_text_fields() and multi-field overlay layout.
    Returns: Supported alignment string or the supplied default.
    """
    alignment = str(value or default).strip().casefold()
    return alignment if alignment in {"left", "center", "right"} else default


def build_sized_text_fields(source: str, text: str, font_value: object, sections: dict[str, object] | None) -> list[dict[str, object]]:
    """Build ordered source-specific fields with per-field size and alignment.

    What: Builds ordered field dictionaries for Welcome and Preparation sources,
    including per-field font and alignment metadata where available.
    Why included: Multi-section content needs independent sizing/alignment while other
    content sources continue using the single-text renderer.
    Where used: Field-aware fit and render functions for Welcome and Preparation tabs.
    Returns: Ordered field mappings, or an empty list for unsupported/missing sections.
    """
    if not isinstance(sections, dict):
        return []
    if source.casefold() == "welcome" and isinstance(sections.get("field_fonts"), dict):
        fonts = sections["field_fonts"]
        alignments = sections.get("field_alignments") if isinstance(sections.get("field_alignments"), dict) else {}
        base_family, _base_size, base_style = normalize_font_value(font_value)

        def welcome_field_font(field_name: str) -> tuple[str, int, str]:
            """What: Apply the base display family/style to one field's saved size.

            Why included: Welcome fields can vary in size while staying typographically
            consistent with the app's active display font.
            Where used: Welcome field mappings built in build_sized_text_fields().
            Returns: Family, saved field size, and base style tuple.
            """
            _field_family, field_size, _field_style = normalize_font_value(fonts.get(field_name) or font_value)
            return base_family, field_size, base_style

        fields = [{"text": sections.get("centered_text") or text, "font_value": welcome_field_font("welcome"), "align": normalize_text_alignment(alignments.get("welcome")), "height_weight": 2, "gap_after_ratio": 0.08}]
        if sections.get("combine_enabled"):
            fields.extend([
            {"text": sections.get("centered_detail_text") or "", "font_value": welcome_field_font("birthday"), "align": normalize_text_alignment(alignments.get("birthday")), "height_weight": 1},
            {"text": sections.get("detail_text") or "", "font_value": welcome_field_font("upcoming"), "align": normalize_text_alignment(alignments.get("upcoming"), "left"), "height_weight": 1},
            ])
        return fields
    if source.casefold() == "preparation" and sections.get("header_font_value"):
        header = str(sections.get("header_text") or "").strip()
        body = text.strip()
        fields = []
        if header and (body == header or body.startswith(header + "\n\n")):
            fields.append({"text": header, "font_value": sections["header_font_value"], "align": normalize_text_alignment(sections.get("header_alignment")), "height_weight": 1})
            body = body[len(header):].strip()
        if body:
            fields.append({"text": body, "font_value": font_value, "align": normalize_text_alignment(sections.get("text_alignment")), "height_weight": 4})
        return fields
    return []


def fit_text_fields_layout(
    fields: list[dict[str, object]],
    width: int,
    height: int,
    *,
    base_image=None,
    background_path: Path | str | None = None,
    shrink_to_fit: bool = True,
) -> list[OverlayLayout]:
    """Prepare each nonempty field as a wrapped OverlayLayout.

    What: Scales each field font for output dimensions, wraps its text, and shares
    selected fill/outline colors across the result list.
    Why included: Multi-field overlays need per-field font and wrapping with common
    contrast settings.
    Where used: render_text_fields_overlay() for Welcome and Preparation content.
    Returns: One OverlayLayout per nonempty field.
    """
    valid_fields = [field for field in fields if str(field.get("text") or "").strip()]
    fill, stroke = choose_text_colors(base_image, background_path=background_path)
    usable_width = max(1, width - 36)
    usable_height = max(1, height - 36)
    gap = max(4, round(height * 0.03))

    layouts: list[OverlayLayout] = []
    for field in valid_fields:
        family, requested_size, style = scale_overlay_font_value(field.get("font_value"), width, height)
        font_value = (family, max(1, requested_size), style)
        font = get_pil_font(font_value)
        outline = get_effective_stroke_width(font_value[1])
        text = wrap_text_to_width(str(field["text"]).strip(), font, max(1, usable_width - outline * 2), outline)
        spacing = get_line_spacing(font_value[1], 0.14)
        layouts.append(OverlayLayout(text, font_value, spacing, fill, stroke, (family, 6, style)))
    return layouts


def render_text_fields_overlay(
    width: int,
    height: int,
    fields: list[dict[str, object]],
    *,
    base_image=None,
    background_path: Path | str | None = None,
    scroll_offset: float | None = None,
):
    """Draw fitted text fields to a transparent image with optional vertical scrolling.

    What: Fits valid fields, computes their heights and gaps, aligns them individually,
    and optionally repeats the text cycle at a scroll offset.
    Why included: Welcome/Preparation previews and display output need consistent
    multi-section placement and scrolling behavior.
    Where used: Tab preview and secondary display composition.
    Returns: RGBA overlay image, or None when Pillow drawing is unavailable.
    """
    if Image is None or ImageDraw is None:
        return None
    valid_fields = [field for field in fields if str(field.get("text") or "").strip()]
    layouts = fit_text_fields_layout(
        valid_fields,
        width,
        height,
        base_image=base_image,
        background_path=background_path,
        shrink_to_fit=scroll_offset is None,
    )
    overlay = Image.new("RGBA", (max(1, width), max(1, height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    gap = max(4, round(height * 0.03))
    heights = []
    for layout in layouts:
        bounds = get_text_bbox(layout.text, get_pil_font(layout.font_value), layout.line_spacing)
        heights.append(bounds[3] - bounds[1] + get_effective_stroke_width(layout.font_value[1]) * 2)
    field_gaps = [
        max(4, round(height * float(field.get("gap_after_ratio") or 0.03)))
        for field in valid_fields[:-1]
    ]
    total_height = sum(heights) + sum(field_gaps)
    cycle = total_height + height + max(18, round(height * 0.15))
    top = (height - total_height) / 2 if scroll_offset is None else height + max(18, round(height * 0.15)) - scroll_offset % cycle
    for field_index, (field, layout, field_height) in enumerate(zip(valid_fields, layouts, heights)):
        font = get_pil_font(layout.font_value)
        align = str(field.get("align") or "center")
        bounds = get_text_bbox(layout.text, font, layout.line_spacing, align=align)
        text_width = bounds[2] - bounds[0]
        left = 18 if align == "left" else width - 18 - text_width if align == "right" else (width - text_width) / 2
        outline = get_effective_stroke_width(layout.font_value[1])
        for offset in (0, cycle) if scroll_offset is not None else (0,):
            draw.multiline_text((left - bounds[0], top + outline - bounds[1] + offset), layout.text, font=font, fill=layout.fill_color, stroke_fill=layout.stroke_color, stroke_width=outline, spacing=layout.line_spacing, align=align)
        if field_index < len(field_gaps):
            top += field_height + field_gaps[field_index]
    return overlay


def render_bible_columns_overlay(
    width: int,
    height: int,
    columns: list[dict[str, object]],
    selected_verse_index: int,
    font_value: object,
    base_image=None,
    *,
    background_path: Path | str | None = None,
    stroke_width: int = DEFAULT_STROKE_WIDTH,
    size_boost: float = SECONDARY_LAYOUT_SIZE_BOOST,
    locked_font_value: object | None = None,
):
    """Render up to three Bible columns and their currently selected verses.

    What: Draws shared theme header when present, divides the body into equal-width
    columns, and fits each title and selected verse with common colors and fonts.
    Why included: Multi-version scripture output needs aligned readable columns and a
    stable shared size across verses.
    Where used: Mirror/OBS rendering and Bible-column preview paths.
    Returns: Transparent RGBA overlay, or None when Pillow drawing is unavailable.
    """
    if Image is None or ImageDraw is None:
        return None
    overlay_image = Image.new("RGBA", (max(1, int(width)), max(1, int(height))), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay_image)
    fill_color, stroke_color = choose_text_colors(base_image, background_path=background_path)
    valid_columns = [column for column in columns[:3] if isinstance(column, dict)]
    if not valid_columns:
        return overlay_image
    theme_header = str(valid_columns[0].get("theme_header") or "").strip()
    if theme_header:
        header_height = max(24, round(height * 0.18))
        header_layout = fit_overlay_layout(theme_header, font_value, width, header_height, scroll_enabled=False, base_image=base_image, stroke_width=stroke_width, padding=6, min_text_size=8, max_text_height_ratio=0.30, line_spacing_ratio=0.18)
        header_overlay = render_overlay_image(width, header_height, header_layout, stroke_width=stroke_width)
        body_columns = [dict(column, theme_header="") for column in valid_columns]
        body_overlay = render_bible_columns_overlay(width, height - header_height, body_columns, selected_verse_index, font_value, base_image, stroke_width=stroke_width, size_boost=size_boost, locked_font_value=locked_font_value)
        if header_overlay is not None:
            overlay_image.paste(header_overlay, (0, 0), header_overlay)
        if body_overlay is not None:
            overlay_image.paste(body_overlay, (0, header_height), body_overlay)
        return overlay_image
    column_width = max(1, int(width) // len(valid_columns))
    selected_index = max(0, int(selected_verse_index or 0))
    normalized_font = normalize_font_value(locked_font_value if locked_font_value is not None else font_value)
    for column_index, column in enumerate(valid_columns):
        left = column_index * column_width
        right = int(width) if column_index == len(valid_columns) - 1 else (column_index + 1) * column_width
        if column_index:
            draw.line((left, 8, left, max(8, int(height) - 8)), fill=stroke_color, width=stroke_width)
        title = str(column.get("title") or "Bible")
        verses = list(column.get("verses") or [])
        current_text = verses[selected_index] if selected_index < len(verses) else "-"
        region_width = max(30, right - left - 12)
        title_height = max(24, round(height * 0.18))
        body_height = max(24, int(height) - title_height - 36)
        layout_font_value = normalized_font if locked_font_value is not None else scale_overlay_font_value(normalized_font, region_width, title_height)
        body_font_value = normalized_font if locked_font_value is not None else scale_overlay_font_value(normalized_font, region_width, body_height)
        title_layout = fit_overlay_layout(title, layout_font_value, region_width, title_height, scroll_enabled=False, base_image=base_image, stroke_width=stroke_width, padding=6, min_text_size=8, max_text_height_ratio=0.18, max_text_multiplier=2, line_spacing_ratio=0.18, size_boost=size_boost)
        if locked_font_value is not None:
            locked_font = get_pil_font(normalized_font)
            body_text = wrap_text_to_width(str(current_text or "-"), locked_font, max(60, region_width - (stroke_width + 12) * 2), stroke_width)
            body_layout = OverlayLayout(body_text, normalized_font, get_line_spacing(normalized_font[1], 0.14), fill_color, stroke_color, (normalized_font[0], 6, normalized_font[2]))
        else:
            body_layout = fit_overlay_layout(str(current_text or "-"), body_font_value, region_width, body_height, scroll_enabled=False, base_image=base_image, stroke_width=stroke_width, padding=12, min_text_size=10, max_text_height_ratio=0.22, max_text_multiplier=2, line_spacing_ratio=0.14, size_boost=size_boost)
        title_layout = type(title_layout)(title_layout.text, title_layout.font_value, title_layout.line_spacing, fill_color, stroke_color, title_layout.corner_font_value)
        body_layout = type(body_layout)(body_layout.text, body_layout.font_value, body_layout.line_spacing, fill_color, stroke_color, body_layout.corner_font_value)
        title_overlay = render_overlay_image(region_width, title_height, title_layout, stroke_width=stroke_width)
        body_overlay = render_overlay_image(region_width, body_height, body_layout, stroke_width=stroke_width)
        if title_overlay is not None:
            overlay_image.paste(title_overlay, (left + 6, 8), title_overlay)
        if body_overlay is not None:
            overlay_image.paste(body_overlay, (left + 6, title_height + 26), body_overlay)
    return overlay_image


def get_bible_locked_font_value(
    columns: list[dict[str, object]],
    font_value: object,
    width: int,
    height: int,
    *,
    size_boost: float = SECONDARY_LAYOUT_SIZE_BOOST,
    stroke_width: int = DEFAULT_STROKE_WIDTH,
) -> tuple[str, int, str]:
    """Find and cache one font size that fits every Bible verse page.

    What: Measures candidates in body regions and chooses the smallest fitted
    size across all content, keyed by text, font, dimensions, and layout options.
    Why included: Verse sizes stay stable across pages without being limited by
    the smaller translation headings, which are fitted separately.
    Where used: render_bible_columns_frame() and the Bible overlay regression tests.
    Returns: Normalized family, common size, and style tuple.
    """
    family, requested_size, style = normalize_font_value(font_value)
    theme_header = str(columns[0].get("theme_header") or "").strip() if columns and isinstance(columns[0], dict) else ""
    cache_key = (
        tuple(
            (
                str(column.get("title") or "Bible"),
                tuple(str(value or "") for value in list(column.get("verses") or [])),
            )
            for column in columns[:3]
            if isinstance(column, dict)
        ),
        family,
        requested_size,
        style,
        max(1, int(width)),
        max(1, int(height)),
        float(size_boost),
        int(stroke_width),
        theme_header,
    )
    cached_font = _BIBLE_LOCKED_FONT_CACHE.get(cache_key)
    if cached_font is not None:
        return cached_font
    scaled_font = scale_overlay_font_value((family, requested_size, style), width, height)
    if theme_header:
        height = max(1, height - max(24, round(height * 0.18)))
    candidate_sizes: list[int] = []
    valid_columns = [column for column in columns[:3] if isinstance(column, dict)]
    column_width = max(1, int(width) // max(1, len(valid_columns)))
    region_width = max(30, column_width - 12)
    title_height = max(24, round(height * 0.18))
    body_height = max(24, int(height) - title_height - 36)
    fonts = {}

    def find_fitted_size(text: str, region_height: int, padding: int, minimum_size: int, height_ratio: float, spacing_ratio: float) -> int:
        """What: Binary-search the largest font candidate fitting one text region.

        Why included: Repeatedly decrementing every candidate is costly across all
        verses; measured fit is monotonic with decreasing font size.
        Where used: get_bible_locked_font_value() for each verse page.
        Returns: Largest tested size satisfying width and height constraints.
        """
        requested = max(minimum_size, round(scaled_font[1] * size_boost))
        maximum = max(18, min(round(region_height * height_ratio), max(18, requested)))
        sizes = list(reversed(range(maximum, minimum_size - 1, -2)))
        usable_width = max(60, region_width - (stroke_width + padding) * 2)
        usable_height = max(60, region_height - (stroke_width + padding) * 2)
        lower = 0
        upper = len(sizes) - 1
        fitted_size = minimum_size
        # Fit is monotonic by font size, so narrow the candidate range with binary search.
        while lower <= upper:
            middle = (lower + upper) // 2
            size = sizes[middle]
            if size not in fonts:
                fonts[size] = get_pil_font((family, size, style))
            font = fonts[size]
            wrapped = wrap_text_to_width(text.strip(), font, usable_width, stroke_width)
            bounds = get_text_bbox(wrapped, font, get_line_spacing(size, spacing_ratio))
            if bounds[3] - bounds[1] <= usable_height:
                fitted_size = size
                lower = middle + 1
            else:
                upper = middle - 1
        return fitted_size

    for column in valid_columns:
        for value in list(column.get("verses") or []):
            candidate_sizes.append(find_fitted_size(str(value or "-"), body_height, 12, 10, 0.22, 0.14))
    locked_size = max(8, min(candidate_sizes or [scaled_font[1]]))
    locked_font = (family, locked_size, style)
    _BIBLE_LOCKED_FONT_CACHE[cache_key] = locked_font
    if len(_BIBLE_LOCKED_FONT_CACHE) > 48:
        oldest_keys = list(_BIBLE_LOCKED_FONT_CACHE)[:-48]
        for oldest_key in oldest_keys:
            _BIBLE_LOCKED_FONT_CACHE.pop(oldest_key, None)
    return locked_font


def paginate_bible_columns(
    columns: list[dict[str, object]], font_value: object, width: int, height: int,
) -> tuple[list[dict[str, object]], list[tuple[int, int]]]:
    """What: Pack consecutive complete Bible verses into output-sized pages.

    Why included: Screens should use available space without dividing a verse between pages.
    Where used: Bible Send to Display passage preparation.
    Returns: Synchronized page columns and zero-based, end-exclusive verse ranges.
    """
    valid_columns = [column for column in columns[:3] if isinstance(column, dict)]
    verse_lists = [list(column.get("verses") or []) for column in valid_columns]
    verse_count = max((len(verses) for verses in verse_lists), default=0)
    if not verse_count:
        return [dict(column, verses=[]) for column in valid_columns], []
    locked_font = get_bible_locked_font_value(valid_columns, font_value, width, height)
    font = get_pil_font(locked_font)
    header = str(valid_columns[0].get("theme_header") or "").strip()
    body_display_height = max(1, height - max(24, round(height * 0.18))) if header else height
    region_width = max(30, width // len(valid_columns) - 12)
    usable_width = max(60, region_width - (DEFAULT_STROKE_WIDTH + 12) * 2)
    title_height = max(24, round(body_display_height * 0.18))
    usable_height = max(60, max(24, body_display_height - title_height - 36) - (DEFAULT_STROKE_WIDTH + 12) * 2)
    spacing = get_line_spacing(locked_font[1], 0.14)

    def page_text(verses: list, start: int, stop: int) -> str:
        """What: Stack whole verses for one translation and page range.

        Why included: All translations must share the same verse boundaries.
        Where used: Page fitting and final page-column construction.
        Returns: Complete verse text separated by blank lines.
        """
        return "\n\n".join(str(verses[index] or "-") if index < len(verses) else "-" for index in range(start, stop))

    def page_fits(start: int, stop: int) -> bool:
        """What: Measure a candidate page in every selected translation.

        Why included: The longest translation must fit before another verse is added.
        Where used: Greedy whole-verse page packing.
        Returns: True when all candidate columns fit the output body region.
        """
        for verses in verse_lists:
            wrapped = wrap_text_to_width(page_text(verses, start, stop), font, usable_width, DEFAULT_STROKE_WIDTH)
            bounds = get_text_bbox(wrapped, font, spacing)
            if bounds[3] - bounds[1] > usable_height:
                return False
        return True

    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(verse_count):
        if index > start and not page_fits(start, index + 1):
            ranges.append((start, index))
            start = index
    ranges.append((start, verse_count))
    return [dict(column, verses=[page_text(verses, start, stop) for start, stop in ranges])
            for column, verses in zip(valid_columns, verse_lists)], ranges


def render_bible_columns_frame(
    source_image,
    preview_width: int,
    preview_height: int,
    render_width: int,
    render_height: int,
    columns: list[dict[str, object]],
    selected_verse_index: int,
    font_value: object,
    *,
    stroke_width: int = DEFAULT_STROKE_WIDTH,
    size_boost: float = SECONDARY_LAYOUT_SIZE_BOOST,
):
    """Composite a fitted Bible-column overlay onto preview and output-sized backgrounds.

    What: Crops/resizes source image for both target sizes, renders columns at output
    resolution with locked typography, then scales the overlay onto the preview.
    Why included: Preview should reflect projector framing and typography while staying
    inexpensive to display in the UI.
    Where used: Bible preview and secondary display composition.
    Returns: Composited RGB preview image, or None when Pillow image operations are absent.
    """
    if Image is None or ImageOps is None:
        return None
    preview_background = ImageOps.fit(
        source_image.convert("RGB"),
        (max(1, int(preview_width)), max(1, int(preview_height))),
        method=Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
    )
    render_background = ImageOps.fit(
        source_image.convert("RGB"),
        (max(1, int(render_width)), max(1, int(render_height))),
        method=Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
    )
    bg_path = getattr(source_image, "_background_path", None)
    if bg_path:
        setattr(preview_background, "_background_path", bg_path)
        setattr(render_background, "_background_path", bg_path)
    overlay_image = render_bible_columns_overlay(
        int(render_width),
        int(render_height),
        columns,
        selected_verse_index,
        scale_overlay_font_value(font_value, int(render_width), int(render_height)),
        render_background,
        stroke_width=stroke_width,
        size_boost=size_boost,
        locked_font_value=get_bible_locked_font_value(columns, font_value, int(render_width), int(render_height), size_boost=size_boost, stroke_width=stroke_width),
    )
    if overlay_image is None:
        return preview_background.convert("RGB")
    resized_overlay = overlay_image.resize(
        (max(1, int(preview_width)), max(1, int(preview_height))),
        Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
    )
    return Image.alpha_composite(preview_background.convert("RGBA"), resized_overlay).convert("RGB")


def render_sectioned_overlay_image(
    width: int,
    height: int,
    layout: SectionedOverlayLayout,
    *,
    stroke_width: int = DEFAULT_STROKE_WIDTH,
):
    """Draw independently aligned Welcome sections into a transparent RGBA layer.

    What: Measures centered and detail sections, computes their combined vertical stack,
    and draws each with shared font, spacing, fill, and outline.
    Why included: Welcome layouts need centered primary text and separately aligned details.
    Where used: Welcome preview and secondary-display rendering.
    Returns: Transparent RGBA overlay, or None when Pillow drawing is unavailable.
    """
    if Image is None or ImageDraw is None:
        return None
    overlay_image = Image.new("RGBA", (max(1, int(width)), max(1, int(height))), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay_image)
    preview_font = get_pil_font(layout.font_value)
    if preview_font is None:
        return overlay_image
    stroke_width = get_effective_stroke_width(layout.font_value[1], stroke_width)

    centered_text = str(layout.centered_text or "").strip()
    centered_detail_text = str(layout.centered_detail_text or "").strip()
    detail_text = str(layout.detail_text or "").strip()
    centered_box = get_text_bbox(centered_text, preview_font, layout.line_spacing, align="center") if centered_text else (0.0, 0.0, 0.0, 0.0)
    centered_detail_box = get_text_bbox(centered_detail_text, preview_font, layout.line_spacing, align="center") if centered_detail_text else (0.0, 0.0, 0.0, 0.0)
    detail_box = get_text_bbox(detail_text, preview_font, layout.line_spacing, align="left") if detail_text else (0.0, 0.0, 0.0, 0.0)
    centered_height = float(centered_box[3] - centered_box[1]) if centered_text else 0.0
    centered_detail_height = float(centered_detail_box[3] - centered_detail_box[1]) if centered_detail_text else 0.0
    detail_height = float(detail_box[3] - detail_box[1]) if detail_text else 0.0
    top_gap = float(max(layout.line_spacing, round(max(1, int(layout.font_value[1])) * WELCOME_SECTION_GAP_MULTIPLIER * DEFAULT_LINE_SPACING_RATIO)) if centered_text and centered_detail_text else 0)
    middle_gap = float(layout.line_spacing if centered_text and detail_text and not centered_detail_text else 0)
    bottom_gap = 0.0
    total_height = centered_height + top_gap + centered_detail_height + middle_gap + bottom_gap + detail_height
    current_top = (height - total_height) / 2.0

    if centered_text:
        centered_x = (width / 2.0) - float(centered_box[0] + centered_box[2]) / 2.0
        draw.multiline_text(
            (centered_x, current_top - float(centered_box[1])),
            centered_text,
            font=preview_font,
            fill=layout.fill_color,
            stroke_width=stroke_width,
            stroke_fill=layout.stroke_color,
            spacing=layout.line_spacing,
            align="center",
        )
        current_top += centered_height + top_gap

    if centered_detail_text:
        centered_detail_x = (width / 2.0) - float(centered_detail_box[0] + centered_detail_box[2]) / 2.0
        draw.multiline_text(
            (centered_detail_x, current_top - float(centered_detail_box[1])),
            centered_detail_text,
            font=preview_font,
            fill=layout.fill_color,
            stroke_width=stroke_width,
            stroke_fill=layout.stroke_color,
            spacing=layout.line_spacing,
            align="center",
        )
        current_top += centered_detail_height + bottom_gap
    elif centered_text and detail_text:
        current_top += middle_gap

    if detail_text:
        left_x = float(stroke_width + DEFAULT_TEXT_PADDING) - float(detail_box[0])
        draw.multiline_text(
            (left_x, current_top - float(detail_box[1])),
            detail_text,
            font=preview_font,
            fill=layout.fill_color,
            stroke_width=stroke_width,
            stroke_fill=layout.stroke_color,
            spacing=layout.line_spacing,
            align="left",
        )
    return overlay_image


def encode_png_bytes(image) -> bytes:
    """Encode a Pillow image as PNG bytes for display-state transport.

    What: Writes the supplied image to an in-memory byte buffer using PNG encoding.
    Why included: The OBS state server exchanges image payloads as encoded bytes.
    Where used: Mirror/OBS payload serialization.
    Returns: PNG bytes, or empty bytes when the input image is None.
    """
    if image is None:
        return b""
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()