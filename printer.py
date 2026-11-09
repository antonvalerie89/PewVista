"""Preview and print selected Display queue content through Windows GDI."""

from __future__ import annotations

import ctypes
import json
import math
import subprocess
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes
from tkinter import messagebox, ttk


class SIZE(ctypes.Structure):
    """Represent the Win32 GDI structure for measured text dimensions.

    What: Defines horizontal cx and vertical cy fields using Windows LONG types.
    Why included: GDI's GetTextExtentPoint32W API writes dimensions through a pointer.
    Where used: measure_text_size() in the printer measurement path.
    Returns: Structure fields are populated by the native API call.
    """
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class DOCINFOW(ctypes.Structure):
    """Represent the Unicode print-job metadata structure expected by GDI.

    What: Declares document name, output, datatype, and job-type fields in Win32 order.
    Why included: StartDocW requires this exact ctypes layout for the print job.
    Where used: print_sections_to_printer() before page output begins.
    Returns: Structure fields carry the job metadata supplied to GDI.
    """
    _fields_ = [
        ("cbSize", ctypes.c_int),
        ("lpszDocName", wintypes.LPCWSTR),
        ("lpszOutput", wintypes.LPCWSTR),
        ("lpszDatatype", wintypes.LPCWSTR),
        ("fwType", wintypes.DWORD),
    ]


HORZRES = 8
VERTRES = 10
LOGPIXELSX = 88
LOGPIXELSY = 90
FW_NORMAL = 400
FW_BOLD = 700

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
PRINT_MARGIN_INCH = 0.5

PAGE_PREVIEW_ASPECT_RATIO = A4_HEIGHT_MM / A4_WIDTH_MM
PAGE_PREVIEW_OUTER_PADDING = 20
PAGE_PREVIEW_MARGIN_MM = 12.7
PAGE_PREVIEW_RIGHT_MARGIN_SCALE = 1.18
PAGE_PREVIEW_HEADER_GAP = 14
PAGE_PREVIEW_TITLE_GAP = 12
PAGE_PREVIEW_SECTION_GAP = 18
SONG_PREVIEW_TITLE_GAP = 8
SONG_PREVIEW_SECTION_GAP = 8
SONG_PRINT_SECTION_GAP_INCH = 0.2
DEFAULT_TEXT_SIZE = 11
SONG_LIST_TEXT_SIZE = 23
PREVIEW_TEXT_ZOOM = 1.0


def list_installed_printers() -> list[dict[str, object]]:
    """Query installed Windows printers and put the default device first.

    What: Uses PowerShell CIM JSON output, validates printer records, and sorts by
    default status then case-insensitive name.
    Why included: The print popup needs available device metadata for user selection.
    Where used: open_print_popup() when populating its printer selector.
    Returns: Printer mappings, or an empty list when Windows/query/JSON is unavailable.
    """
    if getattr(ctypes, "windll", None) is None:
        return []
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_Printer | Select-Object Name,Default,DriverName,PortName | ConvertTo-Json -Compress",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except Exception:
        return []
    raw_output = str(result.stdout or "").strip()
    if not raw_output:
        return []
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return []
    rows = payload if isinstance(payload, list) else [payload]
    printers: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        printers.append(
            {
                "name": name,
                "default": bool(row.get("Default")),
                "driver_name": str(row.get("DriverName") or "").strip(),
                "port_name": str(row.get("PortName") or "").strip(),
            }
        )
    printers.sort(key=lambda item: (not bool(item.get("default")), str(item.get("name") or "").casefold()))
    return printers


def extract_key_from_subtitle(subtitle: str) -> str:
    """Extract a trailing bracketed song key from a queue-item subtitle.

    What: Uses the final opening and closing bracket pair and trims its contents.
    Why included: Printed song titles can include the musical key stored in subtitle text.
    Where used: Print-section and song-list builders.
    Returns: Key text, or an empty string when no valid bracket pair exists.
    """
    normalized = str(subtitle or "").strip()
    start_index = normalized.rfind("[")
    end_index = normalized.rfind("]")
    if start_index >= 0 and end_index > start_index:
        return normalized[start_index + 1 : end_index].strip()
    return ""


def filter_queue_items(
    queue_items: list[dict[str, object]],
    selected_item_id: str | None = None,
    scope: str = "selected",
) -> list[dict[str, object]]:
    """Select queue items according to the requested print scope.

    What: Drops malformed entries and, for selected scope with an ID, keeps only
    the matching queue item; other scopes retain all valid entries.
    Why included: The print workflow supports printing one displayed item or the queue.
    Where used: Both print section builders.
    Returns: Filtered queue item mappings in their original order.
    """
    normalized_scope = str(scope or "selected").strip().casefold()
    normalized_selected_id = str(selected_item_id or "").strip()
    filtered_items: list[dict[str, object]] = []
    for queue_item in queue_items:
        if not isinstance(queue_item, dict):
            continue
        queue_item_id = str(queue_item.get("id") or "").strip()
        if normalized_scope == "selected" and normalized_selected_id and queue_item_id != normalized_selected_id:
            continue
        filtered_items.append(queue_item)
    return filtered_items


def extract_queue_item_print_text(queue_item: dict[str, object]) -> str:
    """Choose printable text from a queue item, preferring its verse list.

    What: Joins non-empty verses with blank lines or falls back to the item's text.
    Why included: Verse-based items should print each verse as a separate paragraph.
    Where used: build_print_sections_from_queue().
    Returns: Trimmed printable body text, possibly empty.
    """
    verses_value = queue_item.get("verses")
    if isinstance(verses_value, list):
        verses = [str(item).strip() for item in verses_value if str(item).strip()]
        if verses:
            return "\n\n".join(verses)
    return str(queue_item.get("text") or "").strip()


def format_queue_item_print_title(queue_item: dict[str, object]) -> str:
    """What: Format a queue title with its content category and optional musical key.

    Why included: Preparation must be identified as Preaching in both print modes.
    Where used: Full-content print sections and numbered service lists.
    Returns: Printable title, preserving custom headings and song keys.
    """
    title = str(queue_item.get("title") or "Untitled").strip() or "Untitled"
    if str(queue_item.get("source") or "").strip().casefold() == "preparation":
        title = "Preaching" if title.casefold() in {"preparation", "preaching", "preach", "preech", "untitled"} else f"Preaching: {title}"
    key_value = extract_key_from_subtitle(str(queue_item.get("subtitle") or ""))
    return f"{title} [{key_value}]" if key_value else title


def build_print_sections_from_queue(
    queue_items: list[dict[str, object]],
    selected_item_id: str | None = None,
    scope: str = "selected",
) -> list[dict[str, object]]:
    """Convert selected queue entries into titled preview/print sections.

    What: Filters the queue, extracts body text, appends any song key to the title,
    and skips items without printable content.
    Why included: Preview pagination and the GDI printer share a section-based format.
    Where used: open_print_popup() when resolving the selected/all print scope.
    Returns: Ordered section mappings containing title, body, and layout flags.
    """
    sections: list[dict[str, object]] = []
    for queue_item in filter_queue_items(queue_items, selected_item_id=selected_item_id, scope=scope):
        body = extract_queue_item_print_text(queue_item)
        if not body:
            continue
        title = format_queue_item_print_title(queue_item)
        sections.append(
            {
                "title": title,
                "body": body,
                "compact": True,
                "keep_together": False,
                "restart_next_copy_on_new_page_if_split": False,
            }
        )
    return sections


def build_song_list_sections_from_queue(
    queue_items: list[dict[str, object]],
    selected_item_id: str | None = None,
    scope: str = "selected",
    repeat_count: int = 1,
) -> list[dict[str, object]]:
    """Create numbered song-list sections for the selected queue items.

    What: Formats titles and optional keys, then repeats the complete numbered body
    according to a minimum-one copy count.
    Why included: Song-list print mode needs compact numbered copies and page restart rules.
    Where used: Song-list print mode in open_print_popup().
    Returns: Repeated section mappings, or an empty list when no songs are selected.
    """
    lines: list[str] = []
    for queue_item in filter_queue_items(queue_items, selected_item_id=selected_item_id, scope=scope):
        title = format_queue_item_print_title(queue_item)
        lines.append(f"{len(lines) + 1}. {title}")
    if not lines:
        return []
    body = "\n".join(lines)
    resolved_repeat_count = max(1, int(repeat_count or 1))
    return [
        {
            "title": "",
            "body": body,
            "compact": True,
            "keep_together": True,
            "restart_next_copy_on_new_page_if_split": True,
            "show_title": False,
            "section_gap": SONG_PREVIEW_SECTION_GAP,
            "print_section_gap_inches": SONG_PRINT_SECTION_GAP_INCH,
            "copy_index": copy_index + 1,
            "copy_total": resolved_repeat_count,
        }
        for copy_index in range(resolved_repeat_count)
    ]


def preview_margin_pixels(page_width: int, page_height: int) -> tuple[int, int]:
    """Scale A4 physical margin settings into preview-canvas pixels.

    What: Computes horizontal and vertical margins from page proportions and applies
    the additional right-side preview adjustment.
    Why included: The page preview should approximate printable margins consistently.
    Where used: Preview-page layout calculations in open_print_popup().
    Returns: Horizontal and vertical pixel margins.
    """
    base_margin_x = max(8, int(round(float(page_width) * (PAGE_PREVIEW_MARGIN_MM / A4_WIDTH_MM))))
    margin_y = max(8, int(round(float(page_height) * (PAGE_PREVIEW_MARGIN_MM / A4_HEIGHT_MM))))
    right_adjusted_margin_x = max(base_margin_x, int(round(float(base_margin_x) * PAGE_PREVIEW_RIGHT_MARGIN_SCALE)))
    return right_adjusted_margin_x, margin_y


def preview_font_size_pixels(print_size: int, screen_dpi: float, preview_scale: float) -> int:
    """Convert a print point size to Tk's pixel-based negative font size.

    What: Applies screen DPI and clamped preview scale, rounds up, and returns a
    negative pixel value for Tk font construction.
    Why included: Printer points and screen preview pixels use different units.
    Where used: Preview font setup in the print popup.
    Returns: Negative integer pixel size, with at least one pixel of text height.
    """
    resolved_points = max(1.0, float(print_size or 1))
    resolved_dpi = max(72.0, float(screen_dpi or 96.0))
    resolved_scale = max(0.1, min(1.0, float(preview_scale)))
    pixel_size = max(1, int(math.ceil((resolved_points * resolved_dpi / 72.0) * resolved_scale * PREVIEW_TEXT_ZOOM)))
    return -pixel_size


def build_preview_font(family: str, size: int, bold: bool = False, italic: bool = False) -> tkfont.Font:
    """Build a Tk preview font with the requested family, weight, and slant.

    What: Normalizes zero size and maps bold/italic flags to Tk font attributes.
    Why included: Preview typography should reflect print settings in screen pixels.
    Where used: Page preview title and body rendering.
    Returns: Configured tkinter.font.Font instance.
    """
    resolved_size = int(size or 1)
    if resolved_size == 0:
        resolved_size = 1
    return tkfont.Font(
        family=str(family or "Segoe UI"),
        size=resolved_size,
        weight="bold" if bold else "normal",
        slant="italic" if italic else "roman",
    )


def wrap_text_for_preview(text: str, font: tkfont.Font, max_width: int) -> list[str]:
    """Wrap preview text to a measured pixel width while preserving paragraph gaps.

    What: Adds words until the Tk font measurement exceeds max_width, retaining empty
    input lines as blank preview lines.
    Why included: Page previews must match visible screen font metrics and paragraph breaks.
    Where used: paginate_preview_sections() for section titles and bodies.
    Returns: Wrapped lines, or a one-empty-line list for empty content.
    """
    wrapped_lines: list[str] = []
    allowed_width = max(1, int(max_width))
    for paragraph in str(text or "").splitlines():
        stripped = paragraph.strip()
        if not stripped:
            wrapped_lines.append("")
            continue
        words = stripped.split()
        current_line = words[0]
        for word in words[1:]:
            candidate = f"{current_line} {word}"
            if font.measure(candidate) <= allowed_width:
                current_line = candidate
            else:
                wrapped_lines.append(current_line)
                current_line = word
        wrapped_lines.append(current_line)
    return wrapped_lines or [""]


def measure_preview_block_height(lines: list[str], font: tkfont.Font) -> int:
    """Estimate the pixel height of a wrapped preview text block.

    What: Multiplies line count by Tk's linespace metric with a one-pixel minimum.
    Why included: Pagination must reserve enough vertical room before drawing a block.
    Where used: paginate_preview_sections() for titles and body text.
    Returns: Positive integer preview height in pixels.
    """
    return max(1, len(lines) * max(1, int(font.metrics("linespace"))))


def paginate_preview_sections(
    sections: list[dict[str, object]],
    title_font: tkfont.Font,
    body_font: tkfont.Font,
    usable_width: int,
    usable_height: int,
) -> list[list[dict[str, object]]]:
    """Paginate preview sections into page-sized title, body, and gap elements.

    What: Wraps content, accounts for block heights, splits long bodies, and honors
    keep-together and restart-next-copy flags.
    Why included: Users need an accurate page preview before sending content to GDI.
    Where used: open_print_popup() when refreshing its preview pages.
    Returns: Pages containing typed layout-item mappings; at least one page is returned.
    """
    pages: list[list[dict[str, object]]] = [[]]
    remaining_height = usable_height
    body_line_height = max(1, int(body_font.metrics("linespace")))
    force_next_section_new_page = False

    def start_new_page() -> None:
        """What: Append a blank preview page and restore its available height.

        Why included: Pagination branches need one consistent page-reset operation.
        Where used: paginate_preview_sections() when content no longer fits.
        Returns: None; mutates the enclosing pages and remaining_height values.
        """
        nonlocal remaining_height
        pages.append([])
        remaining_height = usable_height

    for section in sections:
        if force_next_section_new_page and pages[-1]:
            start_new_page()
        force_next_section_new_page = False

        section_title = str(section.get("title") or "Untitled").strip() or "Untitled"
        show_title = bool(section.get("show_title", True))
        section_body = str(section.get("body") or "").strip()
        compact = bool(section.get("compact"))
        keep_together = bool(section.get("keep_together"))
        restart_next = bool(section.get("restart_next_copy_on_new_page_if_split"))

        title_gap = (SONG_PREVIEW_TITLE_GAP if compact else PAGE_PREVIEW_TITLE_GAP) if show_title else 0
        section_gap = 0 if keep_together else (SONG_PREVIEW_SECTION_GAP if compact else PAGE_PREVIEW_SECTION_GAP)
        default_section_gap = 0 if keep_together else (SONG_PREVIEW_SECTION_GAP if compact else PAGE_PREVIEW_SECTION_GAP)
        section_gap = max(0, int(section.get("section_gap", default_section_gap) or 0))

        title_lines = wrap_text_for_preview(section_title, title_font, usable_width) if show_title else []
        body_lines = wrap_text_for_preview(section_body, body_font, usable_width)
        title_height = measure_preview_block_height(title_lines, title_font) if title_lines else 0
        body_height = measure_preview_block_height(body_lines, body_font)
        full_section_height = title_height + title_gap + body_height + section_gap

        # Move an intact section first when it can fit on a fresh page but not in the current remainder.
        if keep_together and full_section_height <= usable_height and full_section_height > remaining_height and pages[-1]:
            start_new_page()

        if title_height > remaining_height and pages[-1]:
            start_new_page()

        if title_lines:
            pages[-1].append({"kind": "title", "text": "\n".join(title_lines), "font": "title", "height": title_height})
            remaining_height -= title_height

        if title_gap > 0:
            if title_gap > remaining_height and pages[-1]:
                start_new_page()
            pages[-1].append({"kind": "gap", "height": title_gap})
            remaining_height -= title_gap

        line_index = 0
        section_spilled = False
        while line_index < len(body_lines):
            available_line_count = max(1, remaining_height // body_line_height)
            if available_line_count <= 0 and pages[-1]:
                section_spilled = True
                start_new_page()
                # Recompute capacity after the page reset before consuming any body lines.
                continue
            chunk_lines = body_lines[line_index : line_index + available_line_count]
            chunk_height = len(chunk_lines) * body_line_height
            if chunk_height > remaining_height and pages[-1]:
                section_spilled = True
                start_new_page()
                continue
            pages[-1].append({"kind": "body", "text": "\n".join(chunk_lines), "font": "body", "height": chunk_height})
            remaining_height -= chunk_height
            line_index += len(chunk_lines)
            if line_index < len(body_lines):
                section_spilled = True
                start_new_page()

        if section_gap > 0:
            if section_gap > remaining_height and pages[-1]:
                start_new_page()
            pages[-1].append({"kind": "gap", "height": section_gap})
            remaining_height -= section_gap

        if restart_next and section_spilled:
            # Keep the next section with the next copy when this section crossed a page boundary.
            force_next_section_new_page = True

    while pages and not any(item.get("kind") in {"title", "body"} for item in pages[-1]):
        # Trailing spacing can create an otherwise blank page; omit it from the preview.
        pages.pop()
    return pages or [[{"kind": "body", "text": "", "font": "body", "height": body_line_height}]]


def measure_text_size(hdc, text: str) -> tuple[int, int]:
    """Measure text dimensions using the selected printer's GDI device context.

    What: Calls GetTextExtentPoint32W and falls back to conservative dimensions if GDI
    reports a failure.
    Why included: Printer wrapping and pagination must use device-specific metrics.
    Where used: wrap_text_for_printer() and font measurement during printing.
    Returns: Positive width and height in printer pixels.
    """
    size = SIZE()
    gdi32 = ctypes.windll.gdi32
    content = text or " "
    if not gdi32.GetTextExtentPoint32W(hdc, content, len(content), ctypes.byref(size)):
        return max(1, len(content) * 8), 16
    return max(1, int(size.cx)), max(1, int(size.cy))


def wrap_text_for_printer(hdc, text: str, max_width: int) -> list[str]:
    """Wrap printer text using GDI measurements for the selected device/font.

    What: Preserves blank paragraphs and wraps words whenever measured width exceeds
    max_width.
    Why included: Screen preview metrics cannot predict printer DPI or font extents.
    Where used: print_sections_to_printer() before writing section lines.
    Returns: Ordered wrapped lines, including an empty line for empty input.
    """
    wrapped_lines: list[str] = []
    for paragraph in str(text or "").splitlines():
        stripped = paragraph.strip()
        if not stripped:
            wrapped_lines.append("")
            continue
        words = stripped.split()
        current_line = words[0]
        for word in words[1:]:
            candidate = f"{current_line} {word}"
            candidate_width, _ = measure_text_size(hdc, candidate)
            if candidate_width <= max_width:
                current_line = candidate
            else:
                wrapped_lines.append(current_line)
                current_line = word
        wrapped_lines.append(current_line)
    return wrapped_lines or [""]


def create_printer_font(hdc, family: str, point_size: int, weight: int = FW_NORMAL, italic: bool = False):
    """Create a GDI font whose pixel height matches a point size at printer DPI.

    What: Reads vertical device DPI, converts points to a negative pixel height, and
    creates a font with requested family, weight, and italic setting.
    Why included: Printer fonts must be sized for the physical output device.
    Where used: print_sections_to_printer() for title and body text.
    Returns: Native GDI font handle; a false handle signals creation failure.
    """
    dpi_y = max(72, int(ctypes.windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSY) or 96))
    height = -max(1, int(round(point_size * dpi_y / 72.0)))
    return ctypes.windll.gdi32.CreateFontW(height, 0, 0, 0, weight, 1 if italic else 0, 0, 0, 0, 0, 0, 0, 0, family)


def print_sections_to_printer(
    printer_name: str,
    sections: list[dict[str, object]],
    document_title: str,
    copies: int = 1,
    body_font_size: int = 11,
    title_font_size: int | None = None,
    font_family: str = "Segoe UI",
    italic: bool = False,
    body_bold: bool = False,
) -> None:
    """Print titled content sections through the selected Windows GDI printer.

    What: Validates sections, derives printable bounds/fonts from device DPI, wraps
    and paginates lines, and sends text using GDI page/document calls.
    Why included: The app prints Display queue content with device-accurate layout
    rather than relying on a browser or platform-neutral print dialog.
    Where used: Print action in open_print_popup().
    Returns: None; raises ValueError/OSError for invalid content or printer failures,
    and restores/deletes GDI resources in all cases.
    """
    resolved_printer_name = str(printer_name or "").strip()
    if not resolved_printer_name:
        raise ValueError("Select a printer before printing.")

    printable_sections = [
        section
        for section in sections
        if isinstance(section, dict) and (str(section.get("title") or "").strip() or str(section.get("body") or "").strip())
    ]
    if not printable_sections:
        raise ValueError("There is no printable content in the current Display queue.")

    gdi32 = ctypes.windll.gdi32
    hdc = gdi32.CreateDCW("WINSPOOL", resolved_printer_name, None, None)
    if not hdc:
        raise OSError(f"Unable to open printer '{resolved_printer_name}'.")

    title_font = None
    body_font = None
    previous_font = None
    page_started = False
    try:
        page_width = max(1, int(gdi32.GetDeviceCaps(hdc, HORZRES) or 0))
        page_height = max(1, int(gdi32.GetDeviceCaps(hdc, VERTRES) or 0))
        dpi_x = max(72, int(gdi32.GetDeviceCaps(hdc, LOGPIXELSX) or 96))
        dpi_y = max(72, int(gdi32.GetDeviceCaps(hdc, LOGPIXELSY) or 96))
        margin_x = max(36, int(round(dpi_x * PRINT_MARGIN_INCH)))
        margin_y = max(36, int(round(dpi_y * PRINT_MARGIN_INCH)))
        usable_width = max(120, page_width - (margin_x * 2))
        usable_bottom = max(margin_y + 120, page_height - margin_y)
        full_page_content_height = max(120, usable_bottom - margin_y)

        resolved_body_font_size = max(8, int(body_font_size or DEFAULT_TEXT_SIZE))
        resolved_title_font_size = max(resolved_body_font_size + 2, int(title_font_size or (resolved_body_font_size + 3)))
        title_font = create_printer_font(hdc, font_family, resolved_title_font_size, weight=FW_BOLD, italic=italic)
        body_font = create_printer_font(hdc, font_family, resolved_body_font_size, weight=FW_BOLD if body_bold else FW_NORMAL, italic=italic)
        if not title_font or not body_font:
            raise OSError("Unable to create printer fonts.")

        document_info = DOCINFOW()
        document_info.cbSize = ctypes.sizeof(DOCINFOW)
        document_info.lpszDocName = str(document_title or "PewVista")
        if gdi32.StartDocW(hdc, ctypes.byref(document_info)) <= 0:
            raise OSError("Unable to start the print job.")

        y_position = margin_y

        def begin_page() -> None:
            """What: Start a GDI page only when one is not already active.

            Why included: Section and copy boundaries can request page transitions
            without duplicating native StartPage calls.
            Where used: print_sections_to_printer() before writing and after page breaks.
            Returns: None; raises OSError when the printer cannot start a page.
            """
            nonlocal page_started, y_position
            if not page_started:
                if gdi32.StartPage(hdc) <= 0:
                    raise OSError("Unable to start a printer page.")
                page_started = True
                y_position = margin_y

        def end_page() -> None:
            """What: End the current GDI page only when one has been started.

            Why included: Error and copy-boundary paths may reach cleanup without an
            active page.
            Where used: print_sections_to_printer() at overflow, copy, and job boundaries.
            Returns: None; clears the enclosing page_started flag.
            """
            nonlocal page_started
            if page_started:
                gdi32.EndPage(hdc)
                page_started = False

        def select_font(font_handle) -> tuple[int, int]:
            """What: Select one GDI font and measure a sample line's dimensions.

            Why included: Title/body line advancement depends on the active font metrics.
            Where used: print_sections_to_printer() around section measurement and output.
            Returns: Width and height of the sample text; stores the previously selected font.
            """
            nonlocal previous_font
            previous_font = gdi32.SelectObject(hdc, font_handle)
            return measure_text_size(hdc, "Ag")

        def restore_font() -> None:
            """What: Restore the previously selected GDI font when available.

            Why included: GDI resources should not remain selected when a font is deleted.
            Where used: print_sections_to_printer() after measurement and text blocks.
            Returns: None; clears the saved previous-font handle after restoration.
            """
            nonlocal previous_font
            if previous_font:
                gdi32.SelectObject(hdc, previous_font)
                previous_font = None

        for copy_index in range(max(1, int(copies))):
            for section_index, section in enumerate(printable_sections):
                section_title = str(section.get("title") or "Untitled").strip() or "Untitled"
                show_title = bool(section.get("show_title", True))
                section_body = str(section.get("body") or "").strip()
                if not section_body:
                    continue

                compact = bool(section.get("compact"))
                keep_together = bool(section.get("keep_together"))
                restart_next = bool(section.get("restart_next_copy_on_new_page_if_split"))

                begin_page()

                _, title_line_height = select_font(title_font)
                title_lines = wrap_text_for_printer(hdc, section_title, usable_width)
                restore_font()

                _, body_line_height = select_font(body_font)
                body_lines = wrap_text_for_printer(hdc, section_body, usable_width)
                restore_font()

                title_spacing = max(4, int(dpi_y * (0.045 if compact else 0.08)))
                body_spacing = max(3, int(dpi_y * (0.03 if compact else 0.04)))
                default_section_spacing = 0 if keep_together else max(10, int(dpi_y * (0.12 if compact else 0.22)))
                explicit_print_section_gap_inches = section.get("print_section_gap_inches")
                if explicit_print_section_gap_inches is not None:
                    section_spacing = max(0, int(dpi_y * float(explicit_print_section_gap_inches)))
                else:
                    section_spacing = max(0, int(section.get("section_gap", default_section_spacing) or 0))

                title_block_height = (len(title_lines) * title_line_height) + (max(0, len(title_lines) - 1) * title_spacing) if show_title else 0
                body_block_height = (len(body_lines) * body_line_height) + (max(0, len(body_lines) - 1) * body_spacing)
                title_gap_height = title_spacing if show_title and title_lines else 0
                estimated_section_height = title_block_height + title_gap_height + body_block_height + section_spacing

                if keep_together and estimated_section_height <= full_page_content_height and y_position + estimated_section_height > usable_bottom and y_position > margin_y:
                    end_page()
                    begin_page()

                if title_block_height > (usable_bottom - y_position) and y_position > margin_y:
                    end_page()
                    begin_page()

                if show_title and title_lines:
                    select_font(title_font)
                    for title_line_index, line in enumerate(title_lines):
                        if y_position + title_line_height > usable_bottom:
                            restore_font()
                            end_page()
                            begin_page()
                            select_font(title_font)
                        gdi32.TextOutW(hdc, margin_x, y_position, line, len(line))
                        y_position += title_line_height
                        if title_line_index < len(title_lines) - 1:
                            y_position += title_spacing
                    restore_font()
                    y_position += title_spacing

                select_font(body_font)
                section_spilled = False
                for body_line_index, line in enumerate(body_lines):
                    if y_position + body_line_height > usable_bottom:
                        section_spilled = True
                        restore_font()
                        end_page()
                        begin_page()
                        select_font(body_font)
                    gdi32.TextOutW(hdc, margin_x, y_position, line, len(line))
                    y_position += body_line_height
                    if body_line_index < len(body_lines) - 1:
                        y_position += body_spacing
                restore_font()

                if section_spacing > 0:
                    y_position += section_spacing

                if restart_next and section_spilled:
                    # Start the next section on a fresh page so repeated copies remain aligned.
                    end_page()

                if copy_index < max(1, int(copies)) - 1 and section_index == len(printable_sections) - 1:
                    end_page()

        end_page()
        gdi32.EndDoc(hdc)
    finally:
        # GDI objects must be deselected and released even when a page operation raises.
        if previous_font:
            gdi32.SelectObject(hdc, previous_font)
        if title_font:
            gdi32.DeleteObject(title_font)
        if body_font:
            gdi32.DeleteObject(body_font)
        gdi32.DeleteDC(hdc)


def open_print_popup(
    parent: tk.Misc,
    display_state: dict[str, object] | None,
    apply_layout_style,
    get_theme,
    document_title: str,
    configure_notebook_style=None,
    get_font_settings=None,
) -> None:
    """Open the print dialog with printer selection, scope, and paginated previews.

    What: Reads Display queue callbacks, builds per-source preview tabs, updates pages
    as options change, and dispatches valid content to the Windows GDI print routine.
    Why included: Users need to inspect scope and pagination before printing, while
    printer-specific output remains isolated from queue mutation.
    Where used: Main-window Print action through main.open_print_window().
    Returns: None; shows an error/info dialog and opens no popup when prerequisites fail.
    """
    get_queue_items = display_state.get("get_queue_items") if isinstance(display_state, dict) else None
    get_selected_queue_item = display_state.get("get_selected_queue_item") if isinstance(display_state, dict) else None
    if not callable(get_queue_items):
        messagebox.showerror("Print", "Display queue data is not available.", parent=parent)
        return

    queue_items = list(get_queue_items() or [])
    if not queue_items:
        messagebox.showinfo("Print", "There is no content in the Display queue to print.", parent=parent)
        return

    selected_item = get_selected_queue_item() if callable(get_selected_queue_item) else None
    selected_item_id = str(selected_item.get("id") or "") if isinstance(selected_item, dict) else ""
    printers = list_installed_printers()
    if not printers:
        messagebox.showerror("Print", "No Windows printers were found on this computer.", parent=parent)
        return

    popup = tk.Toplevel(parent)
    popup.title("Print Display Queue")
    popup.transient(parent.winfo_toplevel())
    popup.grab_set()
    popup.minsize(760, 620)

    printer_names = [str(item.get("name") or "") for item in printers if str(item.get("name") or "").strip()]
    default_printer_name = printer_names[0] if printer_names else ""
    for item in printers:
        if bool(item.get("default")):
            default_printer_name = str(item.get("name") or "").strip() or default_printer_name
            break

    printer_var = tk.StringVar(value=default_printer_name)
    scope_var = tk.StringVar(value="all")
    copies_var = tk.IntVar(value=1)
    song_list_repeat_var = tk.IntVar(value=1)
    text_size_var = tk.IntVar(value=SONG_LIST_TEXT_SIZE)
    summary_var = tk.StringVar(value="")

    font_family = "Segoe UI"
    font_style = "Normal"
    if callable(get_font_settings):
        try:
            popup_font_settings = get_font_settings()
        except Exception:
            popup_font_settings = None
        if isinstance(popup_font_settings, (list, tuple)) and len(popup_font_settings) >= 2:
            font_family = str(popup_font_settings[0] or font_family)
            font_style = str(popup_font_settings[1] or font_style)
    italic_preview = "italic" in font_style.casefold()

    container = tk.Frame(popup, padx=16, pady=16)
    container.pack(expand=True, fill="both")
    container.columnconfigure(1, weight=1)
    container.rowconfigure(3, weight=1)

    source_specs = [
        ("song_list", "Song List"),
        ("songs", "Songs"),
        ("preparation", "Preparation"),
        ("bible", "Bible"),
    ]
    source_label_by_key = {source_key: source_label for source_key, source_label in source_specs}

    header = tk.Label(container, text="Print Display Queue", anchor="w", justify="left")
    header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))

    printer_label = tk.Label(container, text="Printer")
    printer_label.grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
    printer_menu = tk.OptionMenu(container, printer_var, *printer_names)
    printer_menu.grid(row=1, column=1, sticky="ew", pady=4)

    scope_label = tk.Label(container, text="Content")
    scope_label.grid(row=2, column=0, sticky="nw", padx=(0, 10), pady=4)
    scope_frame = tk.Frame(container)
    scope_frame.grid(row=2, column=1, sticky="ew", pady=4)
    scope_selected = tk.Radiobutton(scope_frame, text="Selected item", variable=scope_var, value="selected")
    scope_selected.grid(row=0, column=0, sticky="w")
    scope_all = tk.Radiobutton(scope_frame, text="Entire queue", variable=scope_var, value="all")
    scope_all.grid(row=1, column=0, sticky="w")
    if not selected_item_id:
        scope_selected.configure(state="disabled")
        scope_var.set("all")

    preview_label = tk.Label(container, text="Preview")
    preview_label.grid(row=3, column=0, sticky="nw", padx=(0, 10), pady=4)
    notebook_frame = tk.Frame(container)
    notebook_frame.grid(row=3, column=1, sticky="nsew", pady=4)
    notebook_frame.columnconfigure(0, weight=1)
    notebook_frame.rowconfigure(0, weight=1)
    preview_notebook = ttk.Notebook(notebook_frame, style="ContentTabs.TNotebook")
    preview_notebook.grid(row=0, column=0, sticky="nsew")
    if callable(configure_notebook_style):
        try:
            configure_notebook_style(preview_notebook)
        except Exception:
            pass

    preview_views: dict[str, dict[str, object]] = {}
    source_key_by_tab_id: dict[str, str] = {}

    def get_active_source_key() -> str:
        """What: Map the selected preview notebook tab to its source key.

        Why included: Scope, text size, summary, and print sections depend on the active tab.
        Where used: Popup callbacks for tab changes, preview refresh, and printing.
        Returns: Active source key, defaulting to songs if Tk selection is unavailable.
        """
        try:
            selected_tab_id = preview_notebook.select()
        except tk.TclError:
            return "songs"
        return source_key_by_tab_id.get(selected_tab_id, "songs")

    source_default_text_sizes = {
        "song_list": SONG_LIST_TEXT_SIZE,
        "songs": DEFAULT_TEXT_SIZE,
        "preparation": DEFAULT_TEXT_SIZE,
        "bible": DEFAULT_TEXT_SIZE,
    }
    source_text_sizes = dict(source_default_text_sizes)
    active_source_state = {"key": "song_list"}
    syncing_text_size = {"active": False}

    def sync_text_size_for_source(source_key: str) -> None:
        """What: Save the prior tab's text size and load the requested tab's size.

        Why included: Each print source keeps independent text-size preferences.
        Where used: Tab-change handling and initial print-popup setup.
        Returns: None; guarded against recursive StringVar trace events.
        """
        if syncing_text_size["active"]:
            return
        previous_source_key = str(active_source_state.get("key") or source_key)
        try:
            source_text_sizes[previous_source_key] = max(8, int(text_size_var.get()))
        except Exception:
            pass
        desired_size = max(8, int(source_text_sizes.get(source_key, source_default_text_sizes.get(source_key, DEFAULT_TEXT_SIZE))))
        active_source_state["key"] = source_key
        try:
            current_size = max(8, int(text_size_var.get()))
        except Exception:
            current_size = desired_size
        if current_size == desired_size:
            return
        syncing_text_size["active"] = True
        try:
            text_size_var.set(desired_size)
        finally:
            syncing_text_size["active"] = False

    def get_song_list_repeat_count() -> int:
        """What: Read and clamp the Song Lists repeat control to at least one copy.

        Why included: Invalid spinbox content must not break preview generation.
        Where used: Song-list section construction and preview summary.
        Returns: Positive integer repeat count, defaulting to one on invalid input.
        """
        try:
            return max(1, int(song_list_repeat_var.get() or 1))
        except Exception:
            return 1

    def get_sections(source_key: str | None = None) -> list[dict[str, object]]:
        """What: Build print sections for song-list or individual-song source tabs.

        Why included: Preview and printing must use the same content and scope selection.
        Where used: refresh_preview() and handle_print().
        Returns: Section mappings for supported sources; empty list for others.
        """
        active_source_key = str(source_key or get_active_source_key() or "songs")
        if active_source_key == "song_list":
            return build_song_list_sections_from_queue(
                queue_items,
                selected_item_id=selected_item_id,
                scope=scope_var.get(),
                repeat_count=get_song_list_repeat_count(),
            )
        if active_source_key == "songs":
            return build_print_sections_from_queue(queue_items, selected_item_id=selected_item_id, scope=scope_var.get())
        return []

    def get_empty_source_message(source_key: str) -> str:
        """What: Return a source-specific message for a preview with no printable content.

        Why included: Empty tabs should explain why no sections appear.
        Where used: refresh_preview() when a source has no generated sections.
        Returns: User-facing empty-state text.
        """
        if source_key == "preparation":
            return "No printable Preparation content is available yet."
        if source_key == "bible":
            return "No printable Bible content is available yet."
        return "No printable content for the current selection."

    def update_song_list_repeat_controls() -> None:
        """What: Enable repeat-count controls only on the Song List preview tab.

        Why included: Repeat count has no meaning for ordinary content sections.
        Where used: Notebook tab changes and initial popup setup.
        Returns: None; updates widget states in place.
        """
        desired_state = "normal" if get_active_source_key() == "song_list" else "disabled"
        for widget in (song_list_repeat_spinbox,):
            try:
                widget.configure(state=desired_state)
            except Exception:
                pass
        try:
            song_list_repeat_label.configure(state=desired_state)
        except Exception:
            pass

    def preview_page_box(stage_canvas: tk.Canvas) -> tuple[int, int, int, int, int, int]:
        """What: Compute a centered A4 page rectangle fitted inside one preview canvas.

        Why included: Page framing must remain stable as the popup is resized.
        Where used: Page drawing and per-tab preview font/pagination sizing.
        Returns: Canvas size, page origin, and page width/height tuple in pixels.
        """
        stage_canvas.update_idletasks()
        stage_width = max(380, int(stage_canvas.winfo_width() or 0))
        stage_height = max(320, int(stage_canvas.winfo_height() or 0))
        max_page_width = max(260, stage_width - (PAGE_PREVIEW_OUTER_PADDING * 2))
        max_page_height = max(280, stage_height - (PAGE_PREVIEW_OUTER_PADDING * 2))
        page_width = min(max_page_width, int(max_page_height / PAGE_PREVIEW_ASPECT_RATIO))
        page_height = int(page_width * PAGE_PREVIEW_ASPECT_RATIO)
        if page_height > max_page_height:
            page_height = max_page_height
            page_width = int(page_height / PAGE_PREVIEW_ASPECT_RATIO)
        page_width = max(260, page_width)
        page_height = max(340, page_height)
        page_left = int((stage_width - page_width) / 2)
        page_top = int((stage_height - page_height) / 2)
        return stage_width, stage_height, page_left, page_top, page_width, page_height

    def layout_preview_page(source_key: str) -> None:
        """What: Redraw page chrome, document header, and current page text elements.

        Why included: Page changes and canvas resizes need a coherent visual preview.
        Where used: Canvas Configure bindings, refresh_preview(), and page navigation.
        Returns: None; updates the selected source's preview canvas.
        """
        preview_view = preview_views.get(source_key) or {}
        stage_canvas = preview_view.get("stage_canvas")
        if not isinstance(stage_canvas, tk.Canvas):
            return

        _stage_width, _stage_height, page_left, page_top, page_width, page_height = preview_page_box(stage_canvas)
        page_right = page_left + page_width
        page_bottom = page_top + page_height

        stage_canvas.delete("page_chrome")
        stage_canvas.delete("page_content")
        stage_canvas.create_rectangle(page_left + 8, page_top + 10, page_right + 8, page_bottom + 10, fill="#b7bec7", outline="", tags="page_chrome")
        stage_canvas.create_rectangle(page_left, page_top, page_right, page_bottom, fill="#fffdf8", outline="#cfd5dd", width=1, tags="page_chrome")

        margin_x, margin_y = preview_margin_pixels(page_width, page_height)
        content_left = page_left + margin_x
        content_top = page_top + margin_y
        content_right = page_right - margin_x
        content_bottom = page_bottom - margin_y
        stage_canvas.create_rectangle(content_left, content_top, content_right, content_bottom, outline="#e1e5ea", width=1, dash=(3, 3), tags="page_chrome")

        preview_pages = preview_view.get("preview_pages") or []
        page_index = int(preview_view.get("active_page_index") or 0)
        if preview_pages:
            page_index = max(0, min(len(preview_pages) - 1, page_index))
            preview_view["active_page_index"] = page_index

        page_label = preview_view.get("page_label")
        if isinstance(page_label, tk.Label):
            page_label.configure(text=f"Page {page_index + 1} of {max(1, len(preview_pages))}")

        header_font = preview_view.get("header_font")
        title_font = preview_view.get("title_font")
        body_font = preview_view.get("body_font")
        header_text = str(preview_view.get("header_text") or "")
        if not isinstance(header_font, tkfont.Font) or not isinstance(title_font, tkfont.Font) or not isinstance(body_font, tkfont.Font):
            return

        y_position = content_top
        header_lines = header_text.splitlines() if header_text else [""]
        header_line_height = max(1, int(header_font.metrics("linespace")))
        for line in header_lines:
            stage_canvas.create_text(content_left, y_position, text=str(line or " "), anchor="nw", font=header_font, fill="#202631", tags="page_content")
            y_position += header_line_height
        y_position += PAGE_PREVIEW_HEADER_GAP

        current_page_items = preview_pages[page_index] if preview_pages else [{"kind": "body", "text": "No printable content for the current selection.", "font": "body", "height": max(1, int(body_font.metrics("linespace")))}]
        for item in current_page_items:
            kind = str(item.get("kind") or "body")
            if kind == "gap":
                y_position += int(item.get("height") or 0)
                continue
            current_font = title_font if str(item.get("font") or "body") == "title" else body_font
            line_height = max(1, int(current_font.metrics("linespace")))
            block_lines = str(item.get("text") or " ").splitlines() or [""]
            for line in block_lines:
                stage_canvas.create_text(content_left, y_position, text=str(line or " "), anchor="nw", font=current_font, fill="#202631", tags="page_content")
                y_position += line_height

    for source_key, source_label in source_specs:
        source_tab = tk.Frame(preview_notebook)
        source_tab.columnconfigure(0, weight=1)
        source_tab.rowconfigure(0, weight=1)
        stage_canvas = tk.Canvas(source_tab, highlightthickness=0, bd=0)
        stage_canvas.grid(row=0, column=0, sticky="nsew")
        nav_bar = tk.Frame(source_tab)
        nav_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        nav_bar.columnconfigure(1, weight=1)
        prev_button = tk.Button(nav_bar, text="Previous")
        prev_button.grid(row=0, column=0, sticky="w")
        page_label = tk.Label(nav_bar, text="Page 1 of 1", anchor="center", justify="center")
        page_label.grid(row=0, column=1, sticky="ew")
        next_button = tk.Button(nav_bar, text="Next")
        next_button.grid(row=0, column=2, sticky="e")
        preview_notebook.add(source_tab, text=source_label)
        preview_views[source_key] = {
            "source_tab": source_tab,
            "stage_canvas": stage_canvas,
            "nav_bar": nav_bar,
            "prev_button": prev_button,
            "page_label": page_label,
            "next_button": next_button,
            "preview_pages": [],
            "active_page_index": 0,
            "header_text": "",
        }
        source_key_by_tab_id[str(source_tab)] = source_key
        stage_canvas.bind("<Configure>", lambda _event, key=source_key: layout_preview_page(key), add="+")

    def change_preview_page(source_key: str, step: int) -> None:
        """What: Move a source preview by a signed page step within valid bounds.

        Why included: Previous/Next must not select pages outside the paginated result.
        Where used: Navigation buttons in each preview tab.
        Returns: None; updates active_page_index and redraws the page.
        """
        preview_view = preview_views.get(source_key) or {}
        preview_pages = preview_view.get("preview_pages") or []
        if not isinstance(preview_pages, list) or not preview_pages:
            preview_view["active_page_index"] = 0
            layout_preview_page(source_key)
            return
        current_index = int(preview_view.get("active_page_index") or 0)
        preview_view["active_page_index"] = max(0, min(len(preview_pages) - 1, current_index + int(step)))
        layout_preview_page(source_key)

    for source_key, preview_view in preview_views.items():
        prev_button = preview_view.get("prev_button")
        next_button = preview_view.get("next_button")
        if isinstance(prev_button, tk.Button):
            prev_button.configure(command=lambda key=source_key: change_preview_page(key, -1))
        if isinstance(next_button, tk.Button):
            next_button.configure(command=lambda key=source_key: change_preview_page(key, 1))

    footer = tk.Frame(container)
    footer.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
    footer.columnconfigure(1, weight=0)
    footer.columnconfigure(3, weight=0)
    footer.columnconfigure(5, weight=1)

    copies_label = tk.Label(footer, text="Copies")
    copies_label.grid(row=0, column=0, sticky="w", padx=(0, 10))
    copies_spinbox = tk.Spinbox(footer, from_=1, to=99, textvariable=copies_var, width=6)
    copies_spinbox.grid(row=0, column=1, sticky="w")

    song_list_repeat_label = tk.Label(footer, text="Song Lists")
    song_list_repeat_label.grid(row=0, column=2, sticky="w", padx=(18, 10))
    song_list_repeat_spinbox = tk.Spinbox(footer, from_=1, to=24, textvariable=song_list_repeat_var, width=6)
    song_list_repeat_spinbox.grid(row=0, column=3, sticky="w")

    text_size_label = tk.Label(footer, text="Text Size")
    text_size_label.grid(row=0, column=4, sticky="w", padx=(18, 10))
    text_size_spinbox = tk.Spinbox(footer, from_=8, to=48, textvariable=text_size_var, width=6)
    text_size_spinbox.grid(row=0, column=5, sticky="w")

    summary_label = tk.Label(footer, textvariable=summary_var, anchor="w", justify="left")
    summary_label.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 0))

    action_row = tk.Frame(container)
    action_row.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
    cancel_button = tk.Button(action_row, text="Cancel", command=popup.destroy)
    cancel_button.grid(row=0, column=0, padx=(0, 8))

    def refresh_preview(*_args) -> None:
        """What: Rebuild fonts, page dimensions, pagination, and summary for every tab.

        Why included: Printer, scope, size, repeat, and canvas changes all affect preview.
        Where used: Variable traces, tab changes, and initial dialog setup.
        Returns: None; resets each source to its first page and redraws it.
        """
        try:
            body_print_size = max(8, int(text_size_var.get()))
        except Exception:
            body_print_size = DEFAULT_TEXT_SIZE

        title_print_size = max(body_print_size + 2, body_print_size + 3)
        header_print_size = max(title_print_size + 1, body_print_size + 4)
        active_source_key = get_active_source_key()
        active_source_label = source_label_by_key.get(active_source_key, "Songs")
        active_song_list_repeat_count = get_song_list_repeat_count()
        active_sections_count = 0

        for source_key, preview_view in preview_views.items():
            stage_canvas = preview_view.get("stage_canvas")
            if isinstance(stage_canvas, tk.Canvas):
                _stage_width, _stage_height, _page_left, _page_top, page_width, page_height = preview_page_box(stage_canvas)
            else:
                page_width = 595
                page_height = int(page_width * PAGE_PREVIEW_ASPECT_RATIO)

            try:
                screen_dpi = float(popup.winfo_fpixels("1i"))
            except Exception:
                screen_dpi = 96.0
            a4_width_pixels = max(1.0, screen_dpi * (A4_WIDTH_MM / 25.4))
            preview_scale = max(0.1, min(1.0, float(page_width) / a4_width_pixels))

            body_preview_size = preview_font_size_pixels(body_print_size, screen_dpi, preview_scale)
            title_preview_size = preview_font_size_pixels(title_print_size, screen_dpi, preview_scale)
            header_preview_size = preview_font_size_pixels(header_print_size, screen_dpi, preview_scale)

            title_font = build_preview_font(font_family, title_preview_size, bold=True, italic=italic_preview)
            body_font = build_preview_font(font_family, body_preview_size, bold=(source_key == "song_list"), italic=italic_preview)
            header_font = build_preview_font(font_family, header_preview_size, bold=True, italic=italic_preview)

            margin_x, margin_y = preview_margin_pixels(page_width, page_height)
            usable_width = max(120, page_width - (margin_x * 2))
            header_lines = wrap_text_for_preview(document_title, header_font, usable_width)
            header_height = measure_preview_block_height(header_lines, header_font)
            content_height = max(180, page_height - (margin_y * 2))
            usable_height = max(120, content_height - header_height - PAGE_PREVIEW_HEADER_GAP)

            sections = get_sections(source_key)
            if not sections:
                sections = [{"title": source_label_by_key.get(source_key, "Preview"), "body": get_empty_source_message(source_key), "compact": False, "keep_together": True}]

            preview_view["title_font"] = title_font
            preview_view["body_font"] = body_font
            preview_view["header_font"] = header_font
            preview_view["header_text"] = "\n".join(header_lines)
            preview_view["preview_pages"] = paginate_preview_sections(sections, title_font, body_font, usable_width, usable_height)
            preview_view["active_page_index"] = 0
            layout_preview_page(source_key)

            if source_key == active_source_key:
                active_sections_count = len(get_sections(source_key))

        if active_source_key == "song_list":
            summary_var.set(f"Tab: {active_source_label} | Lists: {active_song_list_repeat_count} | Sections: {active_sections_count}")
        else:
            summary_var.set(f"Tab: {active_source_label} | Sections: {active_sections_count}")

    def handle_print() -> None:
        """What: Validate print controls, build active-source sections, and send the job.

        Why included: User input and empty content must be checked before invoking GDI.
        Where used: Print button command in the popup footer.
        Returns: None; reports validation/device errors or closes after successful printing.
        """
        active_source_key = get_active_source_key()
        active_source_label = source_label_by_key.get(active_source_key, "Songs")

        try:
            copies_value = max(1, int(copies_var.get()))
        except Exception:
            messagebox.showerror("Print", "Copies must be a whole number.", parent=popup)
            copies_spinbox.focus_set()
            return

        try:
            body_font_size_value = max(8, int(text_size_var.get()))
        except Exception:
            messagebox.showerror("Print", "Text size must be a whole number.", parent=popup)
            text_size_spinbox.focus_set()
            return

        sections = get_sections(active_source_key)
        if not sections:
            messagebox.showinfo("Print", f"There is no printable content in the {active_source_label} tab.", parent=popup)
            return

        try:
            print_sections_to_printer(
                printer_var.get(),
                sections,
                document_title,
                copies=copies_value,
                body_font_size=body_font_size_value,
                font_family=font_family,
                italic=italic_preview,
                body_bold=(active_source_key == "song_list"),
            )
        except Exception as exc:
            messagebox.showerror("Print", str(exc), parent=popup)
            return

        messagebox.showinfo("Print", "Print job sent successfully.", parent=popup)
        popup.destroy()

    print_button = tk.Button(action_row, text="Print", command=handle_print)
    print_button.grid(row=0, column=1)

    def handle_tab_changed(_event=None) -> None:
        """What: Synchronize source-specific controls and redraw after a tab switch.

        Why included: Each source has its own text size and print controls.
        Where used: Preview notebook's <<NotebookTabChanged>> binding.
        Returns: None; updates text-size state, repeat controls, and preview pages.
        """
        sync_text_size_for_source(get_active_source_key())
        update_song_list_repeat_controls()
        refresh_preview()

    for variable in (scope_var, printer_var, copies_var, song_list_repeat_var, text_size_var):
        try:
            variable.trace_add("write", refresh_preview)
        except Exception:
            pass

    preview_notebook.bind("<<NotebookTabChanged>>", handle_tab_changed, add="+")
    popup.bind("<Escape>", lambda _event: popup.destroy())

    preview_widgets = [preview_notebook]
    preview_label_widgets = [header, printer_label, scope_label, preview_label, copies_label, song_list_repeat_label, text_size_label, summary_label]
    preview_input_widgets = [printer_menu, printer_menu["menu"], copies_spinbox, song_list_repeat_spinbox, text_size_spinbox, cancel_button, print_button, scope_selected, scope_all]

    for preview_view in preview_views.values():
        preview_widgets.extend(
            [
                preview_view["source_tab"],
                preview_view["stage_canvas"],
                preview_view["nav_bar"],
                preview_view["prev_button"],
                preview_view["page_label"],
                preview_view["next_button"],
            ]
        )
        preview_label_widgets.append(preview_view["page_label"])
        preview_input_widgets.extend([preview_view["prev_button"], preview_view["next_button"]])

    apply_layout_style(
        [
            popup,
            container,
            scope_frame,
            notebook_frame,
            footer,
            action_row,
            printer_menu,
            printer_menu["menu"],
            copies_spinbox,
            song_list_repeat_spinbox,
            text_size_spinbox,
            cancel_button,
            print_button,
            scope_selected,
            scope_all,
            *preview_widgets,
        ],
        label_widgets=preview_label_widgets,
        input_widgets=preview_input_widgets,
    )

    theme = get_theme() if callable(get_theme) else None
    if isinstance(theme, dict):
        for preview_view in preview_views.values():
            stage_canvas = preview_view.get("stage_canvas")
            source_tab = preview_view.get("source_tab")
            nav_bar = preview_view.get("nav_bar")
            prev_button = preview_view.get("prev_button")
            page_label = preview_view.get("page_label")
            next_button = preview_view.get("next_button")
            for widget in (source_tab, nav_bar):
                if isinstance(widget, tk.Frame):
                    try:
                        widget.configure(bg=theme["panel_bg"])
                    except Exception:
                        pass
            if isinstance(stage_canvas, tk.Canvas):
                try:
                    stage_canvas.configure(bg=theme["window_bg"], highlightbackground=theme["border"])
                except Exception:
                    pass
            for widget in (page_label,):
                if isinstance(widget, tk.Label):
                    try:
                        widget.configure(bg=theme["panel_bg"], fg=theme["text"])
                    except Exception:
                        pass
            for widget in (prev_button, next_button):
                if isinstance(widget, tk.Button):
                    try:
                        widget.configure(bg=theme["menu_bg"], fg=theme["menu_fg"], activebackground=theme["accent"], activeforeground=theme["menu_fg"])
                    except Exception:
                        pass

    sync_text_size_for_source(get_active_source_key())
    update_song_list_repeat_controls()
    refresh_preview()
    popup.wait_visibility()
    popup.lift()
    popup.focus_force()
