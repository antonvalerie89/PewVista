"""Compose service-preparation headings and blocks for the Display queue."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

from display_bridge import load_display_queue_state, save_display_queue_state, upsert_display_queue_item
from background_picker import VIDEO_EXTENSIONS, open_background_picker
from text_size_picker import open_text_size_picker
from overlay_layout import build_sized_text_fields, fit_background_image, fit_text_fields_layout, get_pil_font, get_text_bbox, render_text_fields_overlay
from app_paths import get_app_base_path, resolve_background_file
from perf_logging import log_exception
from video_loop import LoopingVideoReader

try:
    import imageio.v2 as imageio
except Exception:
    imageio = None

try:
    from PIL import Image, ImageDraw, ImageOps, ImageSequence, ImageTk
except ImportError:
    Image = None
    ImageDraw = None
    ImageOps = None
    ImageSequence = None
    ImageTk = None


HEADER_BODY_SEPARATOR = "\n\n"
PREPARATION_TAB_COLUMN_WEIGHTS = (4, 4, 3)


def build_preparation_tab(tab_frame: tk.Frame, context: dict[str, object]) -> None:
    """Build the Preparation editor, paginated block list, and output preview.

    What: Creates heading/body editors, font/alignment/background controls, rendered
    block cards, recall/save callbacks, and a preview backed by shared overlay layout.
    Why included: Service preparation text may span multiple display-sized blocks and
    must remain synchronized with Display and saved-service recall.
    Where used: main.create_tabs_content() for the Preparation notebook tab.
    Returns: None; registers shared preparation state and Display callbacks in context.
    """
    tab_widgets = context["tab_widgets"]
    tab_labels = context["tab_labels"]
    tab_inputs = context.get("tab_inputs")
    if not isinstance(tab_inputs, list):
        tab_inputs = []
    build_preview_placeholder = context.get("build_preview_placeholder")
    apply_layout_style = context["apply_layout_style"]
    get_display_bounds = context.get("get_display_bounds")
    get_preview_size = context.get("get_preview_size")
    notebook = context.get("notebook")
    display_font_family = context.get("display_font_family")
    display_font_style = context.get("display_font_style")
    preparation_state = context.get("preparation_state")
    display_state = context.get("display_state")
    start_background_task = context.get("start_background_task")

    if not callable(build_preview_placeholder):
        raise ValueError("Preparation tab requires build_preview_placeholder in context.")

    state: dict[str, object] = {
        "header_var": tk.StringVar(value=""),
        "background_path": "",
        "details_var": tk.StringVar(value="No preparation content yet."),
        "last_sent_at": "",
        "page_blocks": [],
        "preview_photo": None,
        "selected_block_index": 0,
        "text_size_var": tk.IntVar(value=14),
        "header_size_var": tk.IntVar(value=14),
        "text_alignment_var": tk.StringVar(value="center"),
        "header_alignment_var": tk.StringVar(value="center"),
        "preview_gif_path": "",
        "preview_gif_reader": None,
        "preview_gif_frame_count": 0,
        "preview_gif_frame_index": 0,
        "preview_gif_after_id": None,
        "preview_video_reader": None,
        "preview_video_path": "",
        "preview_video_started": 0.0,
        "preview_video_fps": 24.0,
        "preview_video_after_id": None,
        "preview_refresh_after_id": None,
        "preview_needs_refresh": True,
    }
    app_root = get_app_base_path()
    backgrounds_root = app_root / "Backgrounds"

    def is_preparation_tab_active() -> bool:
        """What: Check whether this preparation frame is the selected notebook page.

        Why included: Preview rendering and GIF timers should pause while hidden.
        Where used: Preview refresh and animation callbacks.
        Returns: True when active; defaults to True if no notebook is available or lookup fails.
        """
        if not isinstance(notebook, tk.Widget):
            return True
        try:
            return notebook.nametowidget(notebook.select()) == tab_frame
        except (tk.TclError, KeyError):
            return True

    for column_index, column_weight in enumerate(PREPARATION_TAB_COLUMN_WEIGHTS):
        tab_frame.columnconfigure(column_index, weight=column_weight)
    tab_frame.rowconfigure(0, weight=0)
    tab_frame.rowconfigure(1, weight=1)

    display_actions = tk.Frame(tab_frame)
    display_actions.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
    tab_widgets.append(display_actions)
    apply_layout_style([display_actions])

    preview_container: tk.Frame | None = None
    preview_label: tk.Label | None = None
    text_block: tk.Text | None = None
    block_cards_canvas: tk.Canvas | None = None
    block_cards_inner: tk.Frame | None = None
    selected_block_text: tk.Text | None = None
    background_display_var = tk.StringVar(value="None")
    size_buttons: dict[str, tk.Button] = {}

    def resolve_display_size() -> tuple[int, int]:
        """What: Read current secondary-display bounds or fall back to preview dimensions.

        Why included: Block splitting must fit the actual output area rather than a fixed preview.
        Where used: build_page_blocks() and layout_text_fits().
        Returns: Width/height pair with minimum output-safe dimensions.
        """
        if callable(get_display_bounds):
            try:
                display_bounds = get_display_bounds()
            except Exception:
                display_bounds = None
            if isinstance(display_bounds, dict):
                try:
                    return max(320, int(display_bounds.get("width") or 0)), max(180, int(display_bounds.get("height") or 0))
                except Exception:
                    pass
        return resolve_preview_size()

    def resolve_preview_size() -> tuple[int, int]:
        """What: Resolve preview callback dimensions or the current preview widget size.

        Why included: Preview rendering must match its current fixed-format container.
        Where used: refresh_preview_layout(), render_selected_block_preview(), and image rendering.
        Returns: Bounded preview width and height.
        """
        if callable(get_preview_size):
            try:
                preview_size = get_preview_size()
            except Exception:
                preview_size = None
            if isinstance(preview_size, tuple) and len(preview_size) >= 2:
                return max(180, int(preview_size[0])), max(100, int(preview_size[1]))
        if isinstance(preview_container, tk.Frame):
            preview_container.update_idletasks()
            return max(180, int(preview_container.winfo_width() or 320)), max(100, int(preview_container.winfo_height() or 180))
        return 360, 203

    def refresh_preview_layout() -> None:
        """What: Apply the resolved dimensions and text wrapping to the preview widgets.

        Why included: Monitor-size changes should not stretch or clip the preview.
        Where used: Preview refresh and resize handling.
        Returns: None; no-ops before the preview widgets exist.
        """
        if not isinstance(preview_container, tk.Frame) or not isinstance(preview_label, tk.Label):
            return
        preview_width, preview_height = resolve_preview_size()
        preview_container.configure(width=preview_width, height=preview_height)
        preview_label.configure(wraplength=max(120, preview_width - 24), padx=12, pady=12)

    def bind_mousewheel_scroll(widget: tk.Widget, scroll_target) -> None:
        """What: Bind mouse-wheel events from nested preparation widgets to a canvas.

        Why included: The pointer may rest on cards or text controls while the user scrolls.
        Where used: Block cards and preparation details controls.
        Returns: None; registers cross-platform wheel bindings when a canvas exists.
        """
        def handle_mousewheel(event) -> str:
            """What: Convert Windows delta or Linux wheel-button input to canvas units.

            Why included: Tk reports wheel direction differently across desktop platforms.
            Where used: Event bindings installed by bind_mousewheel_scroll().
            Returns: "break" after scrolling or consuming the event.
            """
            delta = 0
            if getattr(event, "delta", 0):
                delta = -1 * int(event.delta / 120) if event.delta else 0
            elif getattr(event, "num", None) == 4:
                delta = -1
            elif getattr(event, "num", None) == 5:
                delta = 1
            if delta:
                scroll_target.yview_scroll(delta, "units")
            return "break"

        widget.bind("<MouseWheel>", handle_mousewheel, add="+")
        widget.bind("<Button-4>", handle_mousewheel, add="+")
        widget.bind("<Button-5>", handle_mousewheel, add="+")

    def build_full_preview_text() -> str:
        """What: Combine the current theme header and body editor into preview text.

        Why included: Preview fallback and queue creation need a normalized full-content string.
        Where used: Preview building and empty-block fallback paths.
        Returns: Header/body text with the configured separator, or an empty string.
        """
        header_text = str(state["header_var"].get() or "").strip()
        body_text = text_block.get("1.0", tk.END).strip() if isinstance(text_block, tk.Text) else ""
        if header_text and body_text:
            return f"{header_text}{HEADER_BODY_SEPARATOR}{body_text}"
        parts = [part for part in (header_text, body_text) if part]
        return "\n".join(parts)

    def compose_display_block(header_text: str, block_text: str) -> str:
        """What: Join one preparation header with one body block when both are present.

        Why included: Each display page repeats its context header above its body block.
        Where used: build_page_blocks() when constructing content blocks.
        Returns: Combined text, single nonempty section, or an empty string.
        """
        cleaned_header = str(header_text or "").strip()
        cleaned_block = str(block_text or "").strip()
        if cleaned_header and cleaned_block:
            return f"{cleaned_header}{HEADER_BODY_SEPARATOR}{cleaned_block}"
        parts = [part for part in (cleaned_header, cleaned_block) if part]
        return "\n".join(parts)

    def build_block_entry(label: str, text: str, kind: str) -> dict[str, str]:
        """What: Create a normalized label/text/kind record for one display block.

        Why included: Block rendering and queue serialization use the same compact record shape.
        Where used: Header-only, content, and fallback block creation.
        Returns: Mapping with string label, text, and kind fields.
        """
        return {"label": str(label), "text": str(text), "kind": str(kind)}

    def layout_text_fits(render_text: str, font_value: tuple[str, int, str], display_width: int, display_height: int) -> bool:
        """What: Test whether rendered preparation fields fit within output dimensions.

        Why included: The block builder needs a reliable stopping point before text overflows.
        Where used: build_page_blocks() while packing body lines into pages.
        Returns: True when all fitted text and outlines fit the available bounds.
        """
        fields = build_sized_text_fields("preparation", render_text, font_value, get_text_sections())
        layouts = fit_text_fields_layout(fields, display_width, display_height, shrink_to_fit=False)
        total_height = max(4, round(display_height * 0.03)) * max(0, len(layouts) - 1)
        for layout in layouts:
            bounds = get_text_bbox(layout.text, get_pil_font(layout.font_value), layout.line_spacing)
            outline = max(1, round(layout.font_value[1] * 0.06))
            if bounds[2] - bounds[0] + outline * 2 > display_width - 36:
                return False
            total_height += bounds[3] - bounds[1] + outline * 2
        return total_height <= display_height - 36

    def get_text_sections() -> dict[str, object]:
        """What: Capture current header font/alignment and body alignment as renderer sections.

        Why included: Preview and queued output must retain distinct header/body formatting.
        Where used: Shared overlay field builders and queue item serialization.
        Returns: Section mapping for Preparation-specific text layout.
        """
        return {
            "header_text": str(state["header_var"].get() or "").strip(),
            "header_font_value": list(get_preview_font_value("header")),
            "header_alignment": str(state["header_alignment_var"].get() or "center"),
            "text_alignment": str(state["text_alignment_var"].get() or "center"),
        }

    def build_body_units() -> list[str]:
        """What: Normalize editor lines into ordered nonempty/blank paragraph units.

        Why included: Block splitting should preserve intentional paragraph gaps but discard
        leading/trailing blank rows.
        Where used: build_page_blocks() before testing output-sized groups.
        Returns: Normalized line units, or an empty list when no text widget/content exists.
        """
        if not isinstance(text_block, tk.Text):
            return []
        raw_text = text_block.get("1.0", tk.END).replace("\r", "").rstrip("\n")
        if not raw_text.strip():
            return []
        units: list[str] = []
        for raw_line in raw_text.split("\n"):
            if raw_line.strip():
                units.append(" ".join(raw_line.split()))
            else:
                units.append("")
        # Remove only edge blanks; interior empty units preserve intentional paragraph spacing.
        while units and units[0] == "":
            units.pop(0)
        while units and units[-1] == "":
            units.pop()
        return units

    def build_page_blocks(font_value: tuple[str, int, str]) -> list[dict[str, str]]:
        """What: Split preparation body units into display-sized blocks with repeated headers.

        Why included: Long material must be divided into readable projector pages without
        cutting text at arbitrary overflow boundaries.
        Where used: refresh_page_block_selector() when rebuilding block cards.
        Returns: Ordered block mappings, including a Header Only block when applicable.
        """
        header_text = str(state["header_var"].get() or "").strip()
        display_width, display_height = resolve_display_size()
        body_units = build_body_units()
        page_blocks: list[dict[str, str]] = []
        if header_text:
            page_blocks.append(build_block_entry("Header Only", header_text, "header"))
        if not body_units:
            return page_blocks or [build_block_entry("Preparation Preview", compose_display_block(header_text, ""), "content")]
        current_units: list[str] = []

        def join_units(units: list[str]) -> str:
            """What: Join current body units with newlines and trim outer whitespace.

            Why included: Candidate blocks use identical formatting during fit checks.
            Where used: build_page_blocks() for each prospective block.
            Returns: Normalized candidate body text.
            """
            return "\n".join(units).strip()

        def flush_current_units() -> None:
            """What: Commit the current fitting body lines as one labeled content block.

            Why included: The page builder needs one operation to number/reset accumulated lines.
            Where used: build_page_blocks() when a candidate line no longer fits and at completion.
            Returns: None; appends a block and clears current_units.
            """
            nonlocal current_units
            joined_text = join_units(current_units)
            if joined_text:
                block_number = sum(1 for block in page_blocks if str(block.get("kind") or "") == "content") + 1
                page_blocks.append(build_block_entry(f"Text Block {block_number}", compose_display_block(header_text, joined_text), "content"))
                current_units = []

        for unit_text in body_units:
            if unit_text == "":
                if current_units and current_units[-1] != "":
                    candidate_units = current_units + [""]
                    candidate_text = join_units(candidate_units)
                    if candidate_text and layout_text_fits(compose_display_block(header_text, candidate_text), font_value, display_width, display_height):
                        current_units = candidate_units
                    else:
                        flush_current_units()
                continue

            candidate_units = current_units + [unit_text]
            candidate_text = join_units(candidate_units)
            if candidate_text and layout_text_fits(compose_display_block(header_text, candidate_text), font_value, display_width, display_height):
                current_units = candidate_units
                continue

            if current_units:
                flush_current_units()

            line_words = unit_text.split()
            current_line = ""
            for word in line_words:
                candidate_line = word if not current_line else f"{current_line} {word}"
                candidate_units = current_units + [candidate_line]
                candidate_text = join_units(candidate_units)
                if candidate_text and layout_text_fits(compose_display_block(header_text, candidate_text), font_value, display_width, display_height):
                    current_line = candidate_line
                    continue
                if current_line:
                    current_units.append(current_line)
                    flush_current_units()
                    current_line = word
                else:
                    current_units = [word]
                    flush_current_units()
                    current_line = ""
            if current_line:
                current_units = [current_line]
        flush_current_units()
        if page_blocks:
            return page_blocks
        return [build_block_entry("Text Block 1", compose_display_block(header_text, join_units(body_units)), "content")]

    def get_selected_block_text() -> str:
        """What: Read the selected page block text, falling back to the full editor content.

        Why included: Preview and detail panels must display the same selected block.
        Where used: refresh_selected_block_details() and render_selected_block_preview().
        Returns: Selected block text or a full-content/placeholder fallback.
        """
        page_blocks = state.get("page_blocks")
        if isinstance(page_blocks, list) and page_blocks:
            selected_index = int(state.get("selected_block_index") or 0)
            if 0 <= selected_index < len(page_blocks):
                selected_block = page_blocks[selected_index]
                if isinstance(selected_block, dict):
                    return str(selected_block.get("text") or "")
            first_block = page_blocks[0]
            if isinstance(first_block, dict):
                return str(first_block.get("text") or "")
        return build_full_preview_text()

    def build_page_block_label(block_index: int, block_entry: dict[str, str]) -> str:
        """What: Format a compact card label and truncated one-line body preview.

        Why included: Users need to distinguish generated blocks without showing full content.
        Where used: refresh_page_block_selector() for each block card.
        Returns: Label string with block name and preview snippet.
        """
        label_text = str(block_entry.get("label") or f"Block {block_index + 1}")
        preview_line = " ".join(str(block_entry.get("text") or "").split())
        if len(preview_line) > 72:
            preview_line = f"{preview_line[:69].rstrip()}..."
        return f"{label_text}: {preview_line or 'Preparation Preview'}"

    def refresh_selected_block_details() -> None:
        """What: Replace the readonly detail text with the currently selected block.

        Why included: The block list needs a full-content inspection surface.
        Where used: Block selection and page-block selector refresh.
        Returns: None; no-ops when the detail Text widget is unavailable.
        """
        if not isinstance(selected_block_text, tk.Text):
            return
        selected_block_text.configure(state="normal")
        selected_block_text.delete("1.0", tk.END)
        selected_block_text.insert("1.0", get_selected_block_text())
        selected_block_text.configure(state="disabled")

    def refresh_block_card_styles() -> None:
        """What: Highlight the selected block card and raise the other card reliefs.

        Why included: The selected preview page must be visible in the block list.
        Where used: select_page_block() and selector reconstruction.
        Returns: None; skips styling when the card container is absent.
        """
        if not isinstance(block_cards_inner, tk.Frame):
            return
        selected_index = int(state.get("selected_block_index") or 0)
        for block_index, child in enumerate(block_cards_inner.winfo_children()):
            try:
                child.configure(relief="solid" if block_index == selected_index else "raised", bd=2 if block_index == selected_index else 1)
            except tk.TclError:
                pass

    def select_page_block(block_index: int) -> None:
        """What: Clamp and store a page-block index, then refresh its detail and preview.

        Why included: Preview, detail text, and queued selection must follow one selected block.
        Where used: Block-card button commands and text-size selection logic.
        Returns: None; updates selected block style, details, preview, and summary.
        """
        page_blocks = state.get("page_blocks") if isinstance(state.get("page_blocks"), list) else []
        if not page_blocks:
            state["selected_block_index"] = 0
        else:
            state["selected_block_index"] = min(max(0, int(block_index)), len(page_blocks) - 1)
        refresh_block_card_styles()
        refresh_selected_block_details()
        refresh_details()
        render_selected_block_preview()

    def refresh_page_block_selector() -> None:
        """What: Rebuild block cards from freshly fitted output-sized page blocks.

        Why included: Text, font, header, or output dimensions may change block boundaries.
        Where used: Preview refresh and text editing/size changes.
        Returns: None; clamps current selection and refreshes card/detail state.
        """
        preview_font = get_preview_font_value()
        page_blocks = build_page_blocks(preview_font)
        state["page_blocks"] = page_blocks
        selected_index = min(max(0, int(state.get("selected_block_index") or 0)), len(page_blocks) - 1)
        state["selected_block_index"] = selected_index
        if isinstance(block_cards_inner, tk.Frame):
            for child in block_cards_inner.winfo_children():
                child.destroy()
            for block_index, block_entry in enumerate(page_blocks):
                card_button = tk.Button(
                    block_cards_inner,
                    text=build_page_block_label(block_index, block_entry if isinstance(block_entry, dict) else build_block_entry(f"Block {block_index + 1}", str(block_entry or ""), "content")),
                    anchor="w",
                    justify="left",
                    wraplength=260,
                    padx=8,
                    pady=8,
                    command=lambda selected_index=block_index: select_page_block(selected_index),
                )
                card_button.grid(row=block_index, column=0, sticky="ew", pady=(0, 6))
                apply_layout_style([card_button], input_widgets=[card_button])
                if isinstance(block_cards_canvas, tk.Canvas):
                    bind_mousewheel_scroll(card_button, block_cards_canvas)
            block_cards_inner.columnconfigure(0, weight=1)
            block_cards_inner.update_idletasks()
            if isinstance(block_cards_canvas, tk.Canvas):
                block_cards_canvas.configure(scrollregion=block_cards_canvas.bbox("all"))
        refresh_block_card_styles()
        refresh_selected_block_details()

    def refresh_details() -> None:
        """What: Rebuild the Preparation summary from header, body, blocks, and style settings.

        Why included: The detail panel summarizes current editor state and send history.
        Where used: Text/header edits, block selection, background/font changes, and send success.
        Returns: None; updates details_var for the visible summary Label.
        """
        header_text = str(state["header_var"].get() or "").strip()
        body_text = text_block.get("1.0", tk.END).strip() if isinstance(text_block, tk.Text) else ""
        combined_text = build_full_preview_text()
        lines = [line for line in combined_text.splitlines() if line.strip()]
        page_blocks = state.get("page_blocks") if isinstance(state.get("page_blocks"), list) else []
        selected_block_index = min(max(0, int(state.get("selected_block_index") or 0)), max(0, len(page_blocks) - 1))
        selected_block_label = "None"
        if page_blocks and 0 <= selected_block_index < len(page_blocks):
            selected_block = page_blocks[selected_block_index]
            if isinstance(selected_block, dict):
                selected_block_label = str(selected_block.get("label") or "None")
        detail_lines = [
            f"Header: {header_text or 'None'}",
            f"Text Rows: {min(2, max(1, int((text_block.index('end-1c').split('.')[0]) if isinstance(text_block, tk.Text) else 1)))}" if isinstance(text_block, tk.Text) else "Text Rows: 0",
            f"Characters: {len(combined_text.strip())}",
            f"Lines: {len(lines)}",
            f"Display Blocks: {len(page_blocks) or 1}",
            f"Selected Block: {selected_block_label}",
            f"Font Size: {int(state['text_size_var'].get() or 14)}",
            f"Background: {str(state.get('background_path') or 'None') or 'None'}",
        ]
        last_sent_at = str(state.get("last_sent_at") or "").strip()
        if last_sent_at:
            detail_lines.append(f"Last Sent: {last_sent_at}")
        if not header_text and not body_text:
            detail_lines = ["No preparation content yet."]
        state["details_var"].set("\n".join(detail_lines))

    def render_selected_block_preview() -> None:
        """What: Render the selected block over its background into the preview label.

        Why included: Preview should show the same selected block sent to the projector.
        Where used: Block selection and preview refresh workflows.
        Returns: None; falls back to plain text when Pillow is unavailable.
        """
        if not isinstance(preview_container, tk.Frame) or not isinstance(preview_label, tk.Label):
            return
        refresh_preview_layout()
        preview_width, preview_height = resolve_preview_size()
        preview_text = get_selected_block_text()
        preview_font = get_preview_font_value()
        if ImageTk is not None:
            rendered_image = render_preview_image(preview_width, preview_height, preview_text, preview_font)
            if rendered_image is not None:
                preview_photo = ImageTk.PhotoImage(rendered_image)
                state["preview_photo"] = preview_photo
                preview_label.configure(text="", image=preview_photo, compound="center", justify="center", anchor="center", bg="#000000", fg="#ffffff", font=preview_font)
                return
        preview_label.configure(text=preview_text, image="", compound="center", justify="center", anchor="center", bg="#000000", fg="#ffffff", font=preview_font)

    def get_preview_font_value(field: str = "body") -> tuple[str, int, str]:
        """What: Resolve family, selected field size, and active display style for Pillow.

        Why included: Header/body fields can have independent sizes but share display typography.
        Where used: Fitting, preview rendering, and Preparation queue-item construction.
        Returns: Font family/size/style tuple for the requested header or body field.
        """
        size_var = state["header_size_var"] if field == "header" else state["text_size_var"]
        if not isinstance(preview_label, tk.Label):
            family = str(display_font_family.get() or "Segoe UI") if isinstance(display_font_family, tk.StringVar) else "Segoe UI"
            style = str(display_font_style.get() or "normal").lower() if isinstance(display_font_style, tk.StringVar) else "normal"
            return (family, int(size_var.get() or 14), style)
        named_font = tkfont.Font(font=preview_label.cget("font"))
        weight = str(named_font.cget("weight") or "normal")
        slant = str(named_font.cget("slant") or "roman")
        style_parts: list[str] = []
        if weight == "bold":
            style_parts.append("bold")
        if slant == "italic":
            style_parts.append("italic")
        style_name = " ".join(style_parts) if style_parts else "normal"
        size_value = int(size_var.get() or named_font.cget("size") or 14)
        family = str(display_font_family.get() or "Segoe UI") if isinstance(display_font_family, tk.StringVar) else str(named_font.cget("family") or "Segoe UI")
        selected_style = str(display_font_style.get() or "normal").lower() if isinstance(display_font_style, tk.StringVar) else style_name
        return (family, size_value, selected_style)

    def update_font_button_label() -> None:
        """What: Refresh the visible font-size labels for header and body controls.

        Why included: Font picker changes need immediate confirmation in the editor.
        Where used: Text-size selection and recalled service restoration.
        Returns: None; updates each registered size button.
        """
        for field, button in size_buttons.items():
            size_var = state["header_size_var"] if field == "Theme Header" else state["text_size_var"]
            button.configure(text=f"Font {int(size_var.get())}")

    def update_background_display() -> None:
        """What: Show the selected background's stored path or None in the setup panel.

        Why included: Users need to see which media will accompany Preparation output.
        Where used: Background selection and service recall/clear operations.
        Returns: None; updates background_display_var.
        """
        raw_value = str(state.get("background_path") or "").strip()
        background_display_var.set(raw_value or "None")

    def list_preparation_background_choices() -> list[str]:
        """What: List supported image/GIF files beneath the app Backgrounds directory.

        Why included: The shared picker needs project-relative media choices.
        Where used: choose_background() before opening the background dialog.
        Returns: Sorted app-relative paths; unsupported files and missing directories are skipped.
        """
        background_paths: list[str] = []
        if not backgrounds_root.exists():
            return background_paths
        for candidate in backgrounds_root.rglob("*"):
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}:
                continue
            try:
                relative_path = candidate.relative_to(app_root)
            except ValueError:
                relative_path = candidate
            background_paths.append(relative_path.as_posix())
        return sorted(background_paths, key=str.casefold)

    def resolve_preparation_background_path(background_path: str | None) -> Path | None:
        """What: Resolve a saved preparation background as absolute or app-relative path.

        Why included: Queue/service records may store either form and media may be missing.
        Where used: Background picker and preview image loading.
        Returns: Existing file Path, or None for blank/unavailable values.
        """
        return resolve_background_file(background_path, app_root)
    def cancel_preview_gif_animation() -> None:
        """What: Cancel the current GIF frame timer and clear its after identifier.

        Why included: Background changes and hidden tabs must not keep stale animation callbacks.
        Where used: GIF cache clearing and inactive-tab preview handling.
        Returns: None; ignores timer-cancellation errors.
        """
        after_id = state.get("preview_gif_after_id")
        if after_id and isinstance(preview_label, tk.Label):
            try:
                preview_label.after_cancel(after_id)
            except Exception:
                pass
        state["preview_gif_after_id"] = None

    def clear_preview_gif_cache() -> None:
        """What: Stop GIF playback, close the reader, and reset cached frame state.

        Why included: A replaced or failed GIF must release its file handle and frames.
        Where used: Background path changes and GIF load failures.
        Returns: None; clears path, reader, frame count, and index.
        """
        cancel_preview_gif_animation()
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

    def ensure_preview_gif_cache(candidate: Path) -> bool:
        """What: Open and retain an animated GIF reader when its path changes.

        Why included: Reusing the reader avoids reopening/decoding the GIF each preview frame.
        Where used: load_background_base_image() for .gif backgrounds.
        Returns: True when a multi-frame GIF reader is available; otherwise False.
        """
        if Image is None or ImageSequence is None:
            return False
        if str(state.get("preview_gif_path") or "") == str(candidate) and state.get("preview_gif_reader") is not None:
            return True
        clear_preview_gif_cache()
        try:
            gif_image = Image.open(candidate)
            if not getattr(gif_image, "is_animated", False):
                gif_image.close()
                return False
        except Exception:
            return False
        state["preview_gif_path"] = str(candidate)
        state["preview_gif_reader"] = gif_image
        state["preview_gif_frame_count"] = int(getattr(gif_image, "n_frames", 1) or 1)
        state["preview_gif_frame_index"] = 0
        return True

    def schedule_preview_gif_animation() -> None:
        """What: Schedule the next GIF frame using the reader's frame duration.

        Why included: Animated backgrounds need timed refreshes without blocking Tk.
        Where used: load_background_base_image() after a GIF frame is rendered.
        Returns: None; cancels the timer when the image is not animated.
        """
        cancel_preview_gif_animation()
        preview_reader = state.get("preview_gif_reader")
        frame_count = int(state.get("preview_gif_frame_count") or 0)
        if preview_reader is None or frame_count <= 1 or not isinstance(preview_label, tk.Label):
            return
        frame_index = int(state.get("preview_gif_frame_index") or 0) % frame_count
        try:
            preview_reader.seek(frame_index)
            delay_ms = int(preview_reader.info.get("duration") or 100)
        except Exception:
            delay_ms = 100
        delay_ms = max(20, delay_ms)

        def advance_preview_gif() -> None:
            """What: Advance the cached GIF frame and rerender only while the tab is active.

            Why included: Hidden-tab animation wastes UI work and should resume on reactivation.
            Where used: after callback scheduled by schedule_preview_gif_animation().
            Returns: None; marks preview stale when inactive or updates the next frame.
            """
            state["preview_gif_after_id"] = None
            if not is_preparation_tab_active():
                state["preview_needs_refresh"] = True
                return
            current_reader = state.get("preview_gif_reader")
            current_frame_count = int(state.get("preview_gif_frame_count") or 0)
            if current_reader is None or current_frame_count <= 1:
                return
            state["preview_gif_frame_index"] = (int(state.get("preview_gif_frame_index") or 0) + 1) % current_frame_count
            render_selected_block_preview()

        state["preview_gif_after_id"] = preview_label.after(delay_ms, advance_preview_gif)

    def close_preview_video() -> None:
        """What: Cancel video animation and close the retained decoder.

        Why included: Changed backgrounds and hidden/destroyed tabs must release media resources.
        Where used: Preparation preview media changes, tab hiding, and destruction.
        Returns: None; resets video state and tolerates already-closed resources.
        """
        after_id = state.get("preview_video_after_id")
        if after_id and isinstance(preview_label, tk.Label):
            try:
                preview_label.after_cancel(after_id)
            except tk.TclError:
                pass
        state["preview_video_after_id"] = None
        reader = state.get("preview_video_reader")
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
        state["preview_video_reader"] = None
        state["preview_video_path"] = ""

    def schedule_preview_video_animation() -> None:
        """What: Request another Preparation video frame at a 15 FPS cap.

        Why included: Decoding a single MP4 frame does not provide moving preview playback.
        Where used: Successful video-frame loading in load_background_base_image().
        Returns: None; keeps only one pending timer while the tab is active.
        """
        if state.get("preview_video_after_id") or not isinstance(preview_label, tk.Label):
            return

        def advance_video() -> None:
            """What: Render the next frame or stop when Preparation is hidden.

            Why included: Video callbacks must not continue after navigation or source changes.
            Where used: Preparation video timer callback.
            Returns: None; successful rendering schedules the next frame.
            """
            state["preview_video_after_id"] = None
            if not is_preparation_tab_active():
                state["preview_needs_refresh"] = True
                close_preview_video()
                return
            if state.get("preview_video_reader") is not None:
                render_selected_block_preview()

        state["preview_video_after_id"] = preview_label.after(67, advance_video)

    def load_background_base_image(preview_width: int, preview_height: int):
        """What: Load/crop the selected background or return a black fallback image.

        Why included: Preview rendering needs a fixed-size RGB base for overlay composition.
        Where used: render_preview_image().
        Returns: Pillow RGB image, tagged with its path when the source exists.
        """
        candidate = resolve_preparation_background_path(str(state.get("background_path") or ""))
        if candidate is None or not candidate.exists():
            clear_preview_gif_cache()
            close_preview_video()
            return Image.new("RGB", (preview_width, preview_height), "#000000")
        if candidate.suffix.lower() in VIDEO_EXTENSIONS:
            clear_preview_gif_cache()
            try:
                if imageio is None:
                    raise RuntimeError("Video previews require ImageIO and FFmpeg in this app build.")
                if state.get("preview_video_path") != str(candidate) or state.get("preview_video_reader") is None:
                    close_preview_video()
                    reader = LoopingVideoReader(candidate, reader_kwargs={"format": "FFMPEG"})
                    state["preview_video_reader"] = reader
                    state["preview_video_path"] = str(candidate)
                    state["preview_video_started"] = time.monotonic()
                    state["preview_video_fps"] = reader.fps
                frame, _frame_index = state["preview_video_reader"].frame_at_time(time.monotonic())
                loaded = fit_background_image(Image.fromarray(frame), (preview_width, preview_height), video=True)
                setattr(loaded, "_background_path", str(candidate))
                schedule_preview_video_animation()
                return loaded
            except Exception as error:
                close_preview_video()
                log_exception("preparation.video_preview", error, details=f"background={candidate}")
                return Image.new("RGB", (preview_width, preview_height), "#000000")
        close_preview_video()
        if candidate.suffix.lower() == ".gif" and ensure_preview_gif_cache(candidate):
            preview_reader = state.get("preview_gif_reader")
            frame_count = int(state.get("preview_gif_frame_count") or 1)
            frame_index = int(state.get("preview_gif_frame_index") or 0) % frame_count
            try:
                preview_reader.seek(frame_index)
                frame_image = fit_background_image(preview_reader.copy(), (preview_width, preview_height))
                setattr(frame_image, "_background_path", str(candidate))
                schedule_preview_gif_animation()
                return frame_image
            except Exception:
                pass
        clear_preview_gif_cache()
        try:
            with Image.open(candidate) as source_image:
                loaded = fit_background_image(source_image, (preview_width, preview_height))
                setattr(loaded, "_background_path", str(candidate))
                return loaded
        except Exception:
            blank = Image.new("RGB", (preview_width, preview_height), "#000000")
            setattr(blank, "_background_path", str(candidate))
            return blank

    def render_preview_image(preview_width: int, preview_height: int, preview_text: str, font_value: tuple[str, int, str]):
        """What: Compose a selected background and fitted Preparation fields for the preview.

        Why included: The UI preview should match the renderer's field layout and display dimensions.
        Where used: render_selected_block_preview().
        Returns: Composited RGB Pillow image, base image fallback, or None without Pillow.
        """
        if Image is None or ImageDraw is None or ImageOps is None:
            return None
        base_image = load_background_base_image(preview_width, preview_height)
        render_width, render_height = resolve_display_size()
        bg_path = str(state.get("background_path") or getattr(base_image, "_background_path", "") or "")
        fields = build_sized_text_fields("preparation", preview_text, font_value, get_text_sections())
        overlay_image = render_text_fields_overlay(render_width, render_height, fields, base_image=base_image, background_path=bg_path)
        if overlay_image is None:
            return base_image
        overlay_image = overlay_image.resize((preview_width, preview_height), Image.Resampling.LANCZOS)
        return Image.alpha_composite(base_image.convert("RGBA"), overlay_image).convert("RGB")

    def refresh_preview() -> None:
        """What: Refresh preview geometry, page blocks, details, and selected image.

        Why included: A single refresh path keeps all Preparation views synchronized.
        Where used: Initial setup, edits, background/font updates, recall, and tab activation.
        Returns: None; defers work and stops GIF playback while the tab is hidden.
        """
        if not isinstance(preview_container, tk.Frame) or not isinstance(preview_label, tk.Label):
            return
        refresh_preview_layout()
        if not is_preparation_tab_active():
            state["preview_needs_refresh"] = True
            cancel_preview_gif_animation()
            close_preview_video()
            return
        state["preview_needs_refresh"] = False
        refresh_page_block_selector()
        refresh_details()
        render_selected_block_preview()

    def schedule_preview_refresh(delay_ms: int = 160) -> None:
        """What: Debounce editor changes into one delayed Preparation preview refresh.

        Why included: Typing emits rapid Modified events that should not each trigger image rendering.
        Where used: Header and body edit callbacks.
        Returns: None; records/cancels the pending Tk timer.
        """
        if not is_preparation_tab_active():
            state["preview_needs_refresh"] = True
            return
        pending_after_id = state.get("preview_refresh_after_id")
        if pending_after_id is not None:
            try:
                tab_frame.after_cancel(pending_after_id)
            except tk.TclError:
                pass

        def run_refresh() -> None:
            """What: Clear the pending refresh ID and render if the tab is still active.

            Why included: Deferred work should avoid rendering hidden tabs.
            Where used: after callback scheduled by schedule_preview_refresh().
            Returns: None; marks preview stale when inactive.
            """
            state["preview_refresh_after_id"] = None
            if not is_preparation_tab_active():
                state["preview_needs_refresh"] = True
                return
            refresh_preview()

        state["preview_refresh_after_id"] = tab_frame.after(max(1, int(delay_ms)), run_refresh)

    def set_text_widget_value(widget: tk.Text, value: str) -> None:
        """What: Replace a Text widget's content and clear its modified flag.

        Why included: Recall/clear operations should not trigger edit handlers as if the user typed.
        Where used: Service restore and clearing recalled Preparation content.
        Returns: None; updates the supplied Text widget in place.
        """
        widget.delete("1.0", tk.END)
        if value:
            widget.insert("1.0", value)
        widget.edit_modified(False)

    def normalize_recalled_body_block(header_text: str, block_text: str) -> str:
        """What: Remove duplicated header text/prefix from one recalled body block.

        Why included: Legacy queue records may serialize the header inside every verse string.
        Where used: apply_restored_preparation_entry() before repopulating the editor.
        Returns: Trimmed body block, or empty text when it contains only the header.
        """
        normalized_header = str(header_text or "").strip()
        normalized_block = str(block_text or "").strip()
        if not normalized_block:
            return ""
        if normalized_header and normalized_block == normalized_header:
            return ""
        prefixed_header = f"{normalized_header}{HEADER_BODY_SEPARATOR}" if normalized_header else ""
        if prefixed_header and normalized_block.startswith(prefixed_header):
            return normalized_block[len(prefixed_header):].strip()
        return normalized_block

    def apply_restored_preparation_entry(entry: dict[str, object] | None) -> None:
        """What: Restore Preparation header, body blocks, background, font, and alignment from a service.

        Why included: Saved schedule recall must restore tab-owned preparation settings as well as
        the Display queue.
        Where used: Registered restore_service_entry callback invoked by main.py.
        Returns: None; clears recalled fields when no Preparation item is present.
        """
        queue_items = list(entry.get("queue_items") or []) if isinstance(entry, dict) else []
        preparation_item = None
        for queue_item in queue_items:
            if isinstance(queue_item, dict) and str(queue_item.get("source") or "").casefold() == "preparation":
                preparation_item = queue_item
                break
        if not isinstance(preparation_item, dict):
            clear_recalled_preparation_entry()
            return

        header_text = str(preparation_item.get("title") or "Preparation").strip()
        verses = [str(item).strip() for item in list(preparation_item.get("verses") or []) if str(item).strip()]
        verse_labels = [str(item).strip() for item in list(preparation_item.get("verse_labels") or []) if str(item).strip()]
        body_blocks: list[str] = []
        for verse_index, verse_text in enumerate(verses):
            verse_label = verse_labels[verse_index] if verse_index < len(verse_labels) else ""
            if verse_label.casefold() == "header only":
                continue
            normalized_body = normalize_recalled_body_block(header_text, verse_text)
            if normalized_body:
                body_blocks.append(normalized_body)
        if not body_blocks:
            normalized_text = normalize_recalled_body_block(header_text, str(preparation_item.get("text") or ""))
            if normalized_text:
                body_blocks.append(normalized_text)

        state["header_var"].set(header_text)
        state["background_path"] = str(preparation_item.get("background_path") or "").strip()
        sections = preparation_item.get("welcome_sections") if isinstance(preparation_item.get("welcome_sections"), dict) else {}
        raw_header_font = sections.get("header_font_value")
        if isinstance(raw_header_font, (list, tuple)) and len(raw_header_font) >= 2:
            try:
                state["header_size_var"].set(max(8, int(raw_header_font[1] or 14)))
            except (TypeError, ValueError):
                state["header_size_var"].set(14)
        state["header_alignment_var"].set(str(sections.get("header_alignment") or "center"))
        state["text_alignment_var"].set(str(sections.get("text_alignment") or "center"))
        raw_font_value = preparation_item.get("font_value")
        if isinstance(raw_font_value, (list, tuple)) and len(raw_font_value) >= 2:
            try:
                state["text_size_var"].set(max(8, int(raw_font_value[1] or 14)))
            except Exception:
                state["text_size_var"].set(14)
        if isinstance(text_block, tk.Text):
            set_text_widget_value(text_block, "\n\n".join(body_blocks))
        update_font_button_label()
        update_background_display()
        refresh_preview()

    def clear_recalled_preparation_entry() -> None:
        """What: Reset all Preparation fields and preview state for an empty recalled schedule.

        Why included: Switching to a schedule without preparation must not retain stale content.
        Where used: Registered clear_recalled_data callback invoked by main.py.
        Returns: None; resets fields and refreshes preview/details.
        """
        state["header_var"].set("")
        state["background_path"] = ""
        state["text_size_var"].set(14)
        state["header_size_var"].set(14)
        state["last_sent_at"] = ""
        state["header_alignment_var"].set("center")
        state["text_alignment_var"].set("center")
        state["selected_block_index"] = 0
        if isinstance(text_block, tk.Text):
            set_text_widget_value(text_block, "")
        update_font_button_label()
        update_background_display()
        refresh_preview()

    def choose_background() -> None:
        """What: Open the shared background picker for supported Preparation media.

        Why included: The tab needs a consistent way to select its presentation background.
        Where used: Background button command.
        Returns: None; selection callback stores the path and refreshes the preview.
        """
        def apply_selected_background(selected_value: str) -> None:
            """What: Store the chosen background path and refresh Preparation preview.

            Why included: The picker delegates persistence/state ownership to this tab.
            Where used: on_apply callback passed to open_background_picker().
            Returns: None; updates the tab's background state.
            """
            state["background_path"] = selected_value
            update_background_display()
            refresh_preview()

        open_background_picker(
            parent_window=tab_frame.winfo_toplevel(),
            dialog_title="Select Preparation Background",
            empty_message="No background files were found in the Backgrounds folder.",
            picker_heading="Preparation Background Files",
            choices=[],
            current_background=str(state.get("background_path") or "").strip(),
            resolve_background_path=resolve_preparation_background_path,
            video_extensions=VIDEO_EXTENSIONS,
            apply_layout_style=apply_layout_style,
            on_apply=apply_selected_background,
        )

    def choose_text_size(field: str) -> None:
        """What: Open the reusable size picker for the header or body field.

        Why included: Preparation fields support independent typography while sharing one dialog.
        Where used: Header/body font-size buttons.
        Returns: None; picker callback refreshes blocks, preview, and queued output.
        """
        size_var = state["header_size_var"] if field == "Theme Header" else state["text_size_var"]

        def apply_selected_size(_selected_field: str, size: int) -> None:
            """What: Apply a selected field size and rebuild dependent preview/queue content.

            Why included: Body size can change block boundaries; header size changes fit as well.
            Where used: on_apply callback passed to open_text_size_picker().
            Returns: None; selects a content block for body changes and updates the queued item.
            """
            size_var.set(size)
            update_font_button_label()
            if field == "Text Block":
                refresh_page_block_selector()
                page_blocks = state.get("page_blocks") if isinstance(state.get("page_blocks"), list) else []
                content_index = next(
                    (
                        index
                        for index, block in enumerate(page_blocks)
                        if isinstance(block, dict) and str(block.get("kind") or "").casefold() != "header"
                    ),
                    0,
                )
                select_page_block(content_index)
            else:
                refresh_preview()
            update_queued_preparation_item()

        open_text_size_picker(tab_frame.winfo_toplevel(), {field: int(size_var.get())}, apply_layout_style, apply_selected_size)

    def apply_text_alignment(field: str, alignment: str) -> None:
        """What: Update header/body alignment and synchronize preview plus queued content.

        Why included: Alignment is source-specific display metadata and must persist with the item.
        Where used: Header and Text Block alignment OptionMenu commands.
        Returns: None; refreshes preview and current queued Preparation item.
        """
        variable = state["header_alignment_var"] if field == "Theme Header" else state["text_alignment_var"]
        variable.set(alignment)
        refresh_preview()
        update_queued_preparation_item()

    def update_queued_preparation_item() -> None:
        """What: Update typography/alignment fields on an existing Preparation queue item.

        Why included: Editing style after sending should update the current queued presentation.
        Where used: Font-size and alignment callbacks.
        Returns: None; no-ops when no matching Preparation item is in the bridge.
        """
        queue_items = list(load_display_queue_state().get("queue_items") or [])
        item_index = next(
            (
                index
                for index, item in enumerate(queue_items)
                if isinstance(item, dict)
                and str(item.get("source") or "").casefold() == "preparation"
            ),
            None,
        )
        if item_index is None:
            return
        queue_item = dict(queue_items[item_index])
        sections = dict(queue_item.get("welcome_sections") or {})
        sections["header_font_value"] = list(get_preview_font_value("header"))
        sections["header_alignment"] = str(state["header_alignment_var"].get() or "center")
        sections["text_alignment"] = str(state["text_alignment_var"].get() or "center")
        queue_item["font_value"] = list(get_preview_font_value())
        queue_item["welcome_sections"] = sections
        verse_labels = queue_item.get("verse_labels") if isinstance(queue_item.get("verse_labels"), list) else []
        content_index = next(
            (
                index
                for index, label in enumerate(verse_labels)
                if str(label or "").casefold().startswith("text block")
            ),
            None,
        )
        if content_index is not None:
            queue_item["selected_verse_index"] = content_index
        queue_items[item_index] = queue_item
        save_display_queue_state({"queue_items": queue_items})
        select_verse_for_item = display_state.get("select_verse_for_item") if isinstance(display_state, dict) else None
        if content_index is not None and callable(select_verse_for_item):
            select_verse_for_item(str(queue_item.get("id") or ""), content_index)
        refresh_from_bridge = display_state.get("refresh_from_bridge") if isinstance(display_state, dict) else None
        if callable(refresh_from_bridge):
            refresh_from_bridge()

    def send_to_display() -> None:
        """What: Build a Preparation queue item from all fitted blocks and upsert it asynchronously.

        Why included: The selected service-preparation content must be available in the shared Display queue.
        Where used: Send to Display button command.
        Returns: None; confirms empty body text and reports the queue-write result.
        """
        header_text = str(state["header_var"].get() or "").strip()
        body_text = text_block.get("1.0", tk.END).strip() if isinstance(text_block, tk.Text) else ""
        page_blocks = state.get("page_blocks") if isinstance(state.get("page_blocks"), list) else []
        if not body_text:
            if not messagebox.askyesno("No text", "There is no text in the Preparation field.\n\nSend to Display anyway?\n\nYes: Send to Display\nNo: Don't send to Display", parent=tab_frame.winfo_toplevel(), default="no"):
                return
            page_blocks = [build_block_entry("Header Only", header_text, "header")] if header_text else [build_block_entry("No text", "", "content")]
        if not page_blocks:
            refresh_page_block_selector()
            page_blocks = state.get("page_blocks") if isinstance(state.get("page_blocks"), list) else []
        if not page_blocks:
            messagebox.showinfo("Preparation", "Nothing is available to send to Display.", parent=tab_frame.winfo_toplevel())
            return
        block_entries: list[dict[str, str]] = [block for block in page_blocks if isinstance(block, dict)]
        if not block_entries:
            messagebox.showinfo("Preparation", "Nothing is available to send to Display.", parent=tab_frame.winfo_toplevel())
            return
        content_entries = [block_entry for block_entry in block_entries if str(block_entry.get("kind") or "").strip().casefold() != "header"]
        verse_entries = block_entries if block_entries else [build_block_entry("Text Block 1", build_full_preview_text(), "content")]
        default_text = ""
        if content_entries:
            default_text = str(content_entries[0].get("text") or "")
        elif verse_entries:
            # Prefer body text, but retain the first available block when only legacy verse entries exist.
            default_text = str(verse_entries[0].get("text") or "")
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        queue_item = {
            "id": f"preparation-{timestamp}",
            "source": "preparation",
            "title": header_text or "Preparation",
            "subtitle": "Preparation Item",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "text": default_text,
            "verses": [str(block_entry.get("text") or "") for block_entry in verse_entries if str(block_entry.get("text") or "").strip()],
            "verse_labels": [str(block_entry.get("label") or "").strip() for block_entry in verse_entries if str(block_entry.get("text") or "").strip()],
            "font_value": list(get_preview_font_value()),
            "welcome_sections": get_text_sections(),
            "background_path": str(state.get("background_path") or "").strip(),
        }
        if not body_text and not header_text:
            queue_item["allow_empty_text"] = True
        sent_selection_count = len([entry for entry in verse_entries if str(entry.get("text") or "").strip()])

        def save_preparation_item() -> bool:
            """What: Upsert the composed Preparation item by source and retain its queue slot.

            Why included: Re-sending edited preparation (including when the theme header changes)
            should update the existing Preparation item rather than adding a second one.
            Where used: Worker callable passed to start_background_task().
            Returns: True for an updated existing row, False for a new row.
            """
            result = upsert_display_queue_item(queue_item)
            return not any(entry.get("id") == queue_item["id"] for entry in result["queue_items"])

        def handle_append_result(was_updated: bool) -> None:
            """What: Record the send time, refresh Display, and confirm a successful upsert.

            Why included: Users need confirmation and Display must reload bridge state after a write.
            Where used: on_success callback for the Preparation queue writer.
            Returns: None; confirms either a successful addition or update.
            """
            state["last_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            refresh_details()
            refresh_from_bridge = display_state.get("refresh_from_bridge") if isinstance(display_state, dict) else None
            if callable(refresh_from_bridge):
                refresh_from_bridge()
            messagebox.showinfo(
                "Preparation",
                "Preparation updated in Display." if was_updated else "Preparation added to Display.",
                parent=tab_frame.winfo_toplevel(),
            )

        if callable(start_background_task):
            start_background_task(
                "PreparationQueueWriter",
                save_preparation_item,
                on_success=handle_append_result,
                on_error=lambda error: messagebox.showerror("Preparation", f"Unable to send preparation content to Display.\n\n{error}", parent=tab_frame.winfo_toplevel()),
            )
            return

        handle_append_result(save_preparation_item())

    def handle_header_changed(*_args) -> None:
        """What: Schedule a preview refresh and notify Display when its Bible header source changes.

        Why included: Preparation theme header may also appear above live Bible columns.
        Where used: header_var StringVar trace.
        Returns: None; optional Display callback runs only when provided.
        """
        schedule_preview_refresh()
        on_theme_header_changed = preparation_state.get("on_theme_header_changed") if isinstance(preparation_state, dict) else None
        if callable(on_theme_header_changed):
            on_theme_header_changed()

    def handle_text_modified(_event=None) -> None:
        """What: Clear the Text widget modified flag and debounce preview rendering.

        Why included: Tk's Modified event remains set until cleared and can otherwise repeat.
        Where used: text_block <<Modified>> binding.
        Returns: None; schedules one delayed preview refresh.
        """
        if isinstance(text_block, tk.Text) and text_block.edit_modified():
            text_block.edit_modified(False)
        schedule_preview_refresh()

    for column_index in range(3):
        column_frame = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
        column_frame.grid(row=1, column=column_index, sticky="nsew", padx=8, pady=12)
        column_frame.columnconfigure(0, weight=1)

        if column_index == 0:
            column_frame.rowconfigure(6, weight=1)
            title = tk.Label(column_frame, text="Preparation Setup", justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            background_button = tk.Button(display_actions, text="Background")
            background_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
            background_display = tk.Label(column_frame, textvariable=background_display_var, anchor="w", justify="left", relief="solid", bd=1, padx=6, pady=4)
            background_display.grid(row=2, column=0, sticky="ew", pady=(10, 10))
            header_field_row = tk.Frame(column_frame)
            header_field_row.grid(row=3, column=0, sticky="ew")
            header_field_row.columnconfigure(0, weight=1)
            header_label = tk.Label(header_field_row, text="Theme Header", justify="left", anchor="w")
            header_label.grid(row=0, column=0, sticky="w")
            header_size_button = tk.Button(header_field_row, text=f"Font {int(state['header_size_var'].get())}", command=lambda: choose_text_size("Theme Header"))
            header_size_button.grid(row=0, column=1, sticky="e")
            header_alignment = tk.OptionMenu(header_field_row, state["header_alignment_var"], "left", "center", "right", command=lambda value: apply_text_alignment("Theme Header", value))
            header_alignment.grid(row=0, column=2, sticky="e", padx=(6, 0))
            size_buttons["Theme Header"] = header_size_button
            header_entry = tk.Entry(column_frame, textvariable=state["header_var"])
            header_entry.grid(row=4, column=0, sticky="ew", pady=(4, 10))
            text_field_row = tk.Frame(column_frame)
            text_field_row.grid(row=5, column=0, sticky="ew")
            text_field_row.columnconfigure(0, weight=1)
            text_label = tk.Label(text_field_row, text="Text Block", justify="left", anchor="w")
            text_label.grid(row=0, column=0, sticky="w")
            text_size_button = tk.Button(text_field_row, text=f"Font {int(state['text_size_var'].get())}", command=lambda: choose_text_size("Text Block"))
            text_size_button.grid(row=0, column=1, sticky="e")
            text_alignment = tk.OptionMenu(text_field_row, state["text_alignment_var"], "left", "center", "right", command=lambda value: apply_text_alignment("Text Block", value))
            text_alignment.grid(row=0, column=2, sticky="e", padx=(6, 0))
            size_buttons["Text Block"] = text_size_button
            text_block = tk.Text(column_frame, height=2, wrap="word")
            text_block.grid(row=6, column=0, sticky="nsew", pady=(4, 10))
            send_button = tk.Button(display_actions, text="Send to Display")
            send_button.grid(row=0, column=0, sticky="w")

            tab_widgets.extend([column_frame, title, background_button, background_display, header_field_row, header_label, header_size_button, header_alignment, header_entry, text_field_row, text_label, text_size_button, text_alignment, text_block, send_button])
            tab_labels.extend([title, header_label, text_label])
            header_alignment_menu = header_alignment["menu"]
            text_alignment_menu = text_alignment["menu"]
            tab_inputs.extend([header_size_button, text_size_button, header_alignment, text_alignment, header_alignment_menu, text_alignment_menu, background_button, header_entry, text_block, send_button])
            apply_layout_style([column_frame, header_field_row, text_field_row, background_button, background_display, header_entry, text_block, send_button], label_widgets=[title, header_label, text_label], input_widgets=[header_size_button, text_size_button, header_alignment, text_alignment, header_alignment_menu, text_alignment_menu, background_button, header_entry, text_block, send_button])
        elif column_index == 1:
            column_frame.rowconfigure(2, weight=1)
            column_frame.rowconfigure(3, weight=2)
            title = tk.Label(column_frame, text="Display Blocks", justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            details = tk.Label(column_frame, textvariable=state["details_var"], justify="left", anchor="nw", wraplength=320)
            details.grid(row=1, column=0, sticky="ew", pady=(0, 8))
            block_cards_host = tk.Frame(column_frame)
            block_cards_host.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
            block_cards_host.columnconfigure(0, weight=1)
            block_cards_host.rowconfigure(0, weight=1)
            block_cards_canvas = tk.Canvas(block_cards_host, highlightthickness=0, borderwidth=0)
            block_cards_canvas.grid(row=0, column=0, sticky="nsew")
            block_cards_scrollbar = tk.Scrollbar(block_cards_host, orient="vertical", command=block_cards_canvas.yview)
            block_cards_scrollbar.grid(row=0, column=1, sticky="ns")
            block_cards_canvas.configure(yscrollcommand=block_cards_scrollbar.set)
            block_cards_inner = tk.Frame(block_cards_canvas)
            block_cards_canvas_window = block_cards_canvas.create_window((0, 0), window=block_cards_inner, anchor="nw")
            selected_block_text = tk.Text(column_frame, height=8, wrap="word")
            selected_block_text.grid(row=3, column=0, sticky="nsew")
            selected_block_text.configure(state="disabled")
            block_cards_inner.bind("<Configure>", lambda _event: block_cards_canvas.configure(scrollregion=block_cards_canvas.bbox("all")), add="+")
            block_cards_canvas.bind("<Configure>", lambda _event: block_cards_canvas.itemconfigure(block_cards_canvas_window, width=max(block_cards_canvas.winfo_width(), block_cards_inner.winfo_reqwidth())), add="+")
            bind_mousewheel_scroll(block_cards_canvas, block_cards_canvas)
            bind_mousewheel_scroll(block_cards_inner, block_cards_canvas)
            bind_mousewheel_scroll(block_cards_host, block_cards_canvas)
            bind_mousewheel_scroll(selected_block_text, selected_block_text)

            tab_widgets.extend([column_frame, title, details, block_cards_host, block_cards_canvas, selected_block_text])
            tab_labels.extend([title, details])
            tab_inputs.extend([selected_block_text])
            apply_layout_style([column_frame, block_cards_host, block_cards_canvas, selected_block_text], label_widgets=[title, details], input_widgets=[selected_block_text])
        else:
            column_frame.rowconfigure(2, weight=1)
            title = tk.Label(column_frame, text="Preview / Details", justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            preview_container, preview_label = build_preview_placeholder(column_frame, "")
            preview_container.grid(row=1, column=0, sticky="n", pady=(0, 8))
            tab_widgets.extend([column_frame, title, preview_container, preview_label])
            tab_labels.extend([title, preview_label])
            apply_layout_style([column_frame, preview_container], label_widgets=[title, preview_label])

    state["header_var"].trace_add("write", handle_header_changed)
    if isinstance(text_block, tk.Text):
        text_block.edit_modified(False)
        text_block.bind("<<Modified>>", handle_text_modified, add="+")
    if isinstance(preview_container, tk.Frame):
        preview_container.bind("<Configure>", lambda _event: refresh_preview(), add="+")

    def handle_notebook_tab_changed(_event=None) -> None:
        """What: Refresh a stale preparation preview when its notebook page becomes active.

        Why included: GIF/preview updates pause while the tab is hidden to avoid background work.
        Where used: Notebook <<NotebookTabChanged>> binding installed during tab setup.
        Returns: None; performs work only when the Preparation tab is selected.
        """
        if is_preparation_tab_active():
            if state.get("preview_needs_refresh"):
                refresh_preview()
        else:
            state["preview_needs_refresh"] = True
            pending_after_id = state.get("preview_refresh_after_id")
            if pending_after_id is not None:
                try:
                    tab_frame.after_cancel(pending_after_id)
                except tk.TclError:
                    pass
                state["preview_refresh_after_id"] = None
            cancel_preview_gif_animation()
            close_preview_video()

    def handle_preview_destroy(event) -> None:
        """What: Release Preparation's GIF and video readers when its frame is destroyed.

        Why included: Decoder processes and file handles must not outlive the preview widgets.
        Where used: Preparation frame Destroy binding.
        Returns: None; ignores destruction events from child widgets.
        """
        if event.widget is tab_frame:
            clear_preview_gif_cache()
            close_preview_video()

    tab_frame.bind("<Destroy>", handle_preview_destroy, add="+")

    if isinstance(notebook, ttk.Notebook):
        notebook.bind("<<NotebookTabChanged>>", handle_notebook_tab_changed, add="+")
    update_font_button_label()
    update_background_display()
    background_button.configure(command=choose_background)
    send_button.configure(command=send_to_display)
    refresh_preview()

    if isinstance(preparation_state, dict):
        preparation_state["get_theme_header"] = lambda: str(state["header_var"].get() or "").strip()
        preparation_state["restore_service_entry"] = apply_restored_preparation_entry
        preparation_state["clear_recalled_data"] = clear_recalled_preparation_entry
        preparation_state["refresh_preview"] = refresh_preview