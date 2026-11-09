"""Manage the live Display queue, service recall, selection, and mirror updates."""

from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

from display_bridge import clear_display_queue_items, list_saved_service_entries, load_display_queue_state, move_display_queue_item, remove_display_queue_item, restore_display_queue_from_service_entry, set_display_queue_item_active, set_display_queue_item_selected_verse, toggle_display_queue_item_active
from perf_logging import log_exception, log_timing
from overlay_layout import get_bible_locked_font_value
from music_licensing import normalize_license_provider


DISPLAY_TAB_COLUMN_WEIGHTS = (4, 4, 3)


def preload_bible_payloads(payloads: list[dict[str, object]], width: int, height: int) -> None:
    """What: Warm the renderer's shared font-fitting cache for queued Bible passages.

    Why included: Measuring every verse should happen before first live activation, not during it.
    Where used: Display queue synchronization after Bible sends and service recall.
    Returns: None; does not activate, select, or publish any queued content.
    """
    for payload in payloads:
        columns = payload.get("display_columns")
        if isinstance(columns, list) and columns:
            get_bible_locked_font_value(columns, payload.get("font_value"), width, height)


def build_bible_preview_signature(payload: dict[str, object] | None, preview_index: int) -> tuple[object, ...]:
    """Build a stable cache key for Bible preview content and selected verse.

    What: Normalizes source/book/chapter/range metadata, column titles and verses, and
    the preview index into a tuple.
    Why included: Preview images must refresh when Bible content or selection changes.
    Where used: Bible preview caching and its regression tests.
    Returns: Signature tuple; malformed payloads map to a stable empty signature.
    """
    if not isinstance(payload, dict):
        return ("empty", 0)
    display_columns = payload.get("display_columns") or []
    column_signature = tuple(
        (
            str(column.get("title") or "Bible"),
            tuple(str(value or "").strip() for value in list(column.get("verses") or []) if str(value or "").strip()),
        )
        for column in display_columns[:3]
        if isinstance(column, dict)
    )
    return (
        str(payload.get("source_name") or ""),
        str(payload.get("book_name") or ""),
        int(payload.get("chapter_number") or 0),
        str(payload.get("range_label") or ""),
        tuple(payload.get("page_labels") or []),
        tuple(payload.get("verse_numbers") or []),
        column_signature,
        max(0, int(preview_index or 0)),
    )


def should_render_bible_columns_in_display_details(queue_item: dict[str, object] | None) -> bool:
    """Choose the generic verse-details controls for Bible queue items.

    What: Returns False for the specialized side-by-side Bible details renderer.
    Why included: The Display tab intentionally keeps generic selectable verse controls,
    while the secondary output renders Bible columns separately.
    Where used: rebuild_selected_song_verses_view() and a regression test.
    Returns: Always False under the current Display-tab interaction design.
    """
    return False


def build_display_tab(tab_frame: tk.Frame, context: dict[str, object]) -> None:
    """Build the live queue UI and expose its state callbacks to the application.

    What: Creates queue, service, and verse-detail controls; synchronizes bridge data;
    forwards active selections to the mirror; and registers callbacks in display_state.
    Why included: Display owns user interaction and selection, while display_bridge owns
    persistence and main.py owns cross-tab composition.
    Where used: main.create_tabs_content() for the Display notebook tab.
    Returns: None; registers widgets and callbacks through the supplied context.
    """
    tab_widgets = context["tab_widgets"]
    tab_labels = context["tab_labels"]
    tab_inputs = context["tab_inputs"]
    layout_style = context["apply_layout_style"]

    def apply_layout_style(widgets, label_widgets=(), input_widgets=()) -> None:
        """What: Register this tab's widgets and apply shared colors and role fonts.

        Why included: Dynamic queue rows must participate in the same live theme/font updates
        as controls created during initial tab construction.
        Where used: Queue/detail widget creation and refresh callbacks.
        Returns: None; updates tab widget registries and styles in place.
        """
        all_widgets = list(dict.fromkeys([*widgets, *(label_widgets or ()), *(input_widgets or ())]))
        for registry, group in ((tab_widgets, all_widgets), (tab_labels, label_widgets or ()), (tab_inputs, input_widgets or ())):
            for widget in group:
                if widget not in registry:
                    registry.append(widget)
                    widget.bind("<Destroy>", lambda event, current=widget, items=registry: items.remove(current) if event.widget is current and current in items else None, add="+")
        layout_style(all_widgets, label_widgets=label_widgets, input_widgets=input_widgets)

    display_state = context.get("display_state")
    get_theme_by_name = context.get("get_theme_by_name")
    handle_display_live_change = context.get("handle_display_live_change")
    handle_queue_items_changed = context.get("handle_queue_items_changed")
    notebook = context.get("notebook")
    selected_theme = context.get("selected_theme")
    selected_font_family = context.get("selected_font_family")
    selected_font_style = context.get("selected_font_style")
    selected_font_size = context.get("selected_font_size")
    preparation_state = context.get("preparation_state")
    queue_items_container: tk.Frame | None = None
    queue_scroll_canvas: tk.Canvas | None = None
    verses_items_container: tk.Frame | None = None
    verses_scroll_canvas: tk.Canvas | None = None
    service_entries_list: tk.Listbox | None = None
    clear_live: tk.Button | None = None
    refresh_services_button: tk.Button | None = None
    recall_service_button: tk.Button | None = None
    clear_queue: tk.Button | None = None
    status: tk.Label | None = None
    details_title_var = tk.StringVar(value="Selected Content")
    ui_callback_queue: queue.SimpleQueue[object] = queue.SimpleQueue()

    state: dict[str, object] = {
        "last_queue_signature": None,
        "last_details_signature": None,
        "details_rebuild_after_id": None,
        "last_live_payload": None,
        "last_saved_services_signature": None,
        "saved_services_request_token": 0,
        "queue_mutation_request_token": 0,
        "queue_refresh_request_token": 0,
        "queue_refresh_after_id": None,
        "queue_refresh_target_item_id": None,
        "saved_services_after_id": None,
        "push_live_after_id": None,
        "queue_item_widgets": {},
        "queue_items": [],
        "saved_services": [],
        "selected_queue_item_id": None,
        "selected_verse_index": 0,
        "verse_item_widgets": [],
        "force_live_push_after_refresh": False,
    }

    def is_display_tab_active() -> bool:
        """What: Check whether this Display frame is the notebook's selected page.

        Why included: Background UI polling can slow the app when the tab is hidden.
        Where used: Callback-drain interval selection and tab-refresh behavior.
        Returns: True when active; defaults to True if no notebook is provided or lookup fails.
        """
        if not isinstance(notebook, ttk.Notebook):
            return True
        try:
            return notebook.nametowidget(notebook.select()) is tab_frame
        except tk.TclError:
            return True

    def drain_ui_callback_queue() -> None:
        """What: Run queued worker completions on Tk's thread and schedule another drain.

        Why included: Database workers cannot safely update Tk widgets directly.
        Where used: Repeating after callback started during tab construction.
        Returns: None; polls faster while Display is visible and slower while hidden.
        """
        try:
            while True:
                callback = ui_callback_queue.get_nowait()
                if callable(callback):
                    callback()
        except queue.Empty:
            # Empty means this UI-thread drain has caught up with all currently queued work.
            pass
        try:
            tab_frame.after(15 if is_display_tab_active() else 250, drain_ui_callback_queue)
        except tk.TclError:
            return

    tab_frame.after(15, drain_ui_callback_queue)

    def schedule_on_ui_thread(callback) -> None:
        """What: Enqueue a callable for execution by drain_ui_callback_queue().

        Why included: Worker results must be marshalled back to Tk before UI changes.
        Where used: Queue/service worker completion handlers.
        Returns: None; callback runs during a later queue-drain cycle.
        """
        ui_callback_queue.put(callback)

    for column_index, column_weight in enumerate(DISPLAY_TAB_COLUMN_WEIGHTS):
        tab_frame.columnconfigure(column_index, weight=column_weight)
    tab_frame.rowconfigure(0, weight=1)

    def get_selected_service_entry() -> dict[str, str] | None:
        """What: Resolve the currently selected saved-service row to its cached mapping.

        Why included: Recall needs the service ID associated with the visible listbox selection.
        Where used: recall_selected_service().
        Returns: Selected saved-service mapping, or None when no valid selection exists.
        """
        if service_entries_list is None:
            return None
        selection = service_entries_list.curselection()
        if not selection:
            return None
        list_index = int(selection[0])
        entries = state.get("saved_services") or []
        if not isinstance(entries, list) or list_index >= len(entries):
            return None
        entry = entries[list_index]
        return entry if isinstance(entry, dict) else None

    def get_selected_queue_item() -> dict[str, object] | None:
        """What: Find the queue item matching the stored selected item ID.

        Why included: Selection is stored by stable ID so reorder operations do not change it.
        Where used: Detail refresh, payload generation, and queue actions.
        Returns: Matching queue mapping, or None when the selected item is absent.
        """
        selected_item_id = str(state.get("selected_queue_item_id") or "")
        if not selected_item_id:
            return None
        queue_items = state.get("queue_items") or []
        if not isinstance(queue_items, list):
            return None
        for queue_item in queue_items:
            if not isinstance(queue_item, dict):
                continue
            if str(queue_item.get("id") or "") == selected_item_id:
                return queue_item
        return None

    def get_active_queue_item() -> dict[str, object] | None:
        """What: Return the first queue item marked active for live output.

        Why included: The shared bridge enforces one active item, which feeds the mirror payload.
        Where used: push_live_selection() and status/detail updates.
        Returns: Active queue mapping, or None when no item is active.
        """
        queue_items = state.get("queue_items") or []
        if not isinstance(queue_items, list):
            return None
        for queue_item in queue_items:
            if not isinstance(queue_item, dict):
                continue
            if bool(queue_item.get("active")):
                return queue_item
        return None

    def build_items_signature(items: object) -> str:
        """What: Serialize a list of state items as compact deterministic JSON.

        Why included: Queue and saved-service refreshes can skip unchanged widget rebuilds.
        Where used: Queue and saved-service cache comparisons.
        Returns: Sorted-key JSON text; non-list input is treated as an empty list.
        """
        if not isinstance(items, list):
            items = []
        return json.dumps(items, sort_keys=True, separators=(",", ":"))

    def get_selected_queue_index() -> int:
        """What: Find the selected item's current position in the queue.

        Why included: Row actions and selection logic need an index derived from stable ID.
        Where used: Queue selection and view updates.
        Returns: Zero-based index, or -1 when no selected item is present.
        """
        selected_item_id = str(state.get("selected_queue_item_id") or "")
        if not selected_item_id:
            return -1
        queue_items = state.get("queue_items") or []
        if not isinstance(queue_items, list):
            return -1
        for index, queue_item in enumerate(queue_items):
            if isinstance(queue_item, dict) and str(queue_item.get("id") or "") == selected_item_id:
                return index
        return -1

    def format_queue_label(queue_item: dict[str, object]) -> str:
        """What: Choose the display title from title, unique key, or an Untitled fallback.

        Why included: Queue rows and live status need consistent non-empty labels.
        Where used: Queue row creation/update and status text.
        Returns: Title string for one queue item.
        """
        title = str(queue_item.get("title") or queue_item.get("unique_key") or "Untitled")
        return title

    def format_queue_source_indicator(queue_item: dict[str, object]) -> str:
        """What: Map a queue source identifier to its user-facing category label.

        Why included: Queue rows show the content type independently of its title.
        Where used: Source indicator labels in queue rows.
        Returns: Friendly source name, or a title-cased fallback label.
        """
        if not isinstance(queue_item, dict):
            return "Item"
        source_value = str(queue_item.get("source") or "item").strip().casefold()
        source_labels = {
            "library": "Song",
            "song": "Song",
            "welcome": "Welcome",
            "preparation": "Preaching",
            "bible": "Bible",
        }
        return source_labels.get(source_value, str(source_value or "Item").title())

    def get_queue_source_indicator_colors(queue_item: dict[str, object]) -> tuple[str, str]:
        """What: Select background and foreground colors for a source indicator.

        Why included: Source categories need visual distinction while keeping label contrast.
        Where used: update_queue_item_widgets() when styling a queue row.
        Returns: Indicator background/foreground color pair.
        """
        if not isinstance(queue_item, dict):
            return ("#d9d9d9", "#202020")
        source_value = str(queue_item.get("source") or "item").strip().casefold()
        source_colors = {
            "library": ("#2f6fed", "#ffffff"),
            "song": ("#2f6fed", "#ffffff"),
            "welcome": ("#2f9e44", "#ffffff"),
            "preparation": ("#d9480f", "#ffffff"),
            "bible": ("#7b2cbf", "#ffffff"),
        }
        return source_colors.get(source_value, ("#d9d9d9", "#202020"))

    def get_current_theme() -> dict[str, str] | None:
        """What: Read and resolve the currently selected theme through the injected provider.

        Why included: Queue/detail styles must follow the live app theme without owning theme data.
        Where used: Queue and verse selection-style refreshes.
        Returns: Palette dictionary, or None when no valid theme callback/value exists.
        """
        if not callable(get_theme_by_name) or selected_theme is None:
            return None
        try:
            theme_name = selected_theme.get()
        except Exception:
            return None
        theme = get_theme_by_name(theme_name)
        return theme if isinstance(theme, dict) else None

    def select_queue_item(item_id: str | None) -> None:
        """What: Store a selected queue ID and refresh details, styles, and live output.

        Why included: Selection changes reset verse position only when the selected item changes.
        Where used: Queue-row selection and programmatic selection updates.
        Returns: None; schedules the corresponding active payload update.
        """
        previous_item_id = str(state.get("selected_queue_item_id") or "")
        state["selected_queue_item_id"] = str(item_id or "") or None
        if str(state.get("selected_queue_item_id") or "") != previous_item_id:
            state["selected_verse_index"] = 0
        refresh_queue_item_selection_styles()
        refresh_details()
        request_push_live_selection()

    def extract_preview_key(queue_item: dict[str, object] | None) -> str:
        """What: Extract the final bracketed key from a queue item's subtitle.

        Why included: Song key metadata is displayed in preview/live corner text.
        Where used: build_live_payload() before mirror synchronization.
        Returns: Bracket contents, or an empty string when no pair exists.
        """
        if not isinstance(queue_item, dict):
            return ""
        subtitle = str(queue_item.get("subtitle") or "").strip()
        if not subtitle:
            return ""
        bracket_start = subtitle.rfind("[")
        bracket_end = subtitle.rfind("]")
        if bracket_start >= 0 and bracket_end > bracket_start:
            return subtitle[bracket_start + 1 : bracket_end].strip()
        return ""

    def build_live_payload(queue_item: dict[str, object] | None) -> dict[str, object] | None:
        """What: Build the normalized mirror payload for one active queue item.

        Why included: Display state must be translated into the mirror's source-aware format,
        including selected verse, font, scrolling, and Bible columns.
        Where used: push_live_selection() and live verse/status refreshes.
        Returns: Payload mapping for an active item, or None for absent/inactive items.
        """
        if not isinstance(queue_item, dict):
            return None
        if not bool(queue_item.get("active")):
            return None
        verses = get_queue_item_verses(queue_item)
        selected_verse_index = int(state.get("selected_verse_index") or 0)
        if verses:
            selected_verse_index = max(0, min(len(verses) - 1, selected_verse_index))
            text_value = verses[selected_verse_index]
        else:
            text_value = str(queue_item.get("text") or "").strip()
        try:
            scroll_speed_value = int(queue_item.get("scroll_speed") or 7)
        except Exception:
            scroll_speed_value = 7
        raw_font_value = queue_item.get("font_value")
        font_value = None
        if isinstance(raw_font_value, (list, tuple)) and len(raw_font_value) >= 2:
            try:
                font_value = (
                    str(raw_font_value[0] or "Segoe UI"),
                    int(raw_font_value[1] or 12),
                    str(raw_font_value[2] or "normal") if len(raw_font_value) > 2 else "normal",
                )
            except Exception:
                font_value = None
        if str(queue_item.get("source") or "").strip().casefold() == "bible" and selected_font_family is not None and selected_font_style is not None and selected_font_size is not None:
            try:
                font_value = (
                    str(selected_font_family.get() or "Segoe UI"),
                    int(selected_font_size.get() or 12),
                    str(selected_font_style.get() or "normal").lower(),
                )
            except Exception:
                pass
        display_columns = queue_item.get("display_columns") if isinstance(queue_item.get("display_columns"), list) else None
        if str(queue_item.get("source") or "").casefold() == "bible" and display_columns:
            get_theme_header = preparation_state.get("get_theme_header") if isinstance(preparation_state, dict) else None
            if callable(get_theme_header):
                theme_header = str(get_theme_header() or "").strip()
            else:
                preparation_item = next((item for item in state.get("queue_items") or [] if isinstance(item, dict) and str(item.get("source") or "").casefold() == "preparation"), None)
                theme_header = str(preparation_item.get("title") or "").strip() if preparation_item else ""
            if theme_header.casefold() == "preparation":
                theme_header = ""
            display_columns = [dict(column, theme_header=theme_header if index == 0 else "") for index, column in enumerate(display_columns) if isinstance(column, dict)]
        license_provider = normalize_license_provider(queue_item.get("license_provider"))
        return {
            "queue_item_id": str(queue_item.get("id") or "").strip(),
            "title": str(queue_item.get("title") or "").strip(),
            "text": text_value,
            "preview_key": extract_preview_key(queue_item),
            "license_provider": license_provider,
            "license_author": str(queue_item.get("license_author") or "").strip(),
            "license_info": str(queue_item.get("license_info") or "").strip(),
            "font_value": font_value,
            "background_path": str(queue_item.get("background_path") or "").strip(),
            "scroll_enabled": bool(queue_item.get("scroll_enabled")),
            "scroll_speed": scroll_speed_value,
            "source": str(queue_item.get("source") or "").strip(),
            "welcome_sections": queue_item.get("welcome_sections") if isinstance(queue_item.get("welcome_sections"), dict) else None,
            "display_columns": display_columns,
            "selected_verse_index": selected_verse_index,
        }

    def push_live_selection(force: bool = False) -> None:
        """What: Forward the active queue item's payload when it differs from the last one.

        Why included: Prevents duplicate preview and secondary-output updates.
        Where used: Debounced selection changes and initial Display queue synchronization.
        Returns: None; uses the injected live-change callback when available.
        """
        if not callable(handle_display_live_change):
            return
        live_payload = build_live_payload(get_active_queue_item())
        if not force and live_payload == state.get("last_live_payload"):
            return
        state["last_live_payload"] = live_payload
        handle_display_live_change(live_payload, force=force)

    def clear_live_output(force: bool = True) -> None:
        """What: Send an empty payload to clear the local and secondary live output.

        Why included: Explicit clear actions must remove previously active content.
        Where used: Clear Live control and queue-reset workflows.
        Returns: None; no-ops when the live-change callback is unavailable.
        """
        if not callable(handle_display_live_change):
            return
        state["last_live_payload"] = None
        handle_display_live_change(None, force=force)

    def request_push_live_selection(delay_ms: int = 0, force: bool = False) -> None:
        """What: Coalesce pending live-payload pushes into one delayed callback.

        Why included: Rapid row/verse changes should not queue redundant renders.
        Where used: Selection changes, verse selection, and theme-header updates.
        Returns: None; preserves a pending force flag across rescheduling.
        """
        pending_after_id = state.get("push_live_after_id")
        if pending_after_id is not None:
            try:
                tab_frame.after_cancel(pending_after_id)
            except tk.TclError:
                pass
        state["push_live_force"] = bool(force) or bool(state.get("push_live_force"))

        def run_push() -> None:
            """What: Clear the pending timer state and push the latest selection once.

            Why included: The delayed callback should read current state, not a stale snapshot.
            Where used: after callback scheduled by request_push_live_selection().
            Returns: None; forwards the accumulated force flag.
            """
            state["push_live_after_id"] = None
            force_push = bool(state.get("push_live_force"))
            state["push_live_force"] = False
            push_live_selection(force=force_push)

        state["push_live_after_id"] = tab_frame.after(max(0, int(delay_ms)), run_push)

    def refresh_live_bible_header() -> None:
        """What: Schedule a live update when the active Bible's Preparation header changes.

        Why included: The header is included in Bible output columns but belongs to Preparation.
        Where used: Registered in preparation_state as on_theme_header_changed.
        Returns: None; only pushes when the active item is Bible.
        """
        preload_queued_bibles()
        active_item = get_active_queue_item()
        if isinstance(active_item, dict) and str(active_item.get("source") or "").casefold() == "bible":
            request_push_live_selection(delay_ms=150)

    if isinstance(preparation_state, dict):
        preparation_state["on_theme_header_changed"] = refresh_live_bible_header

    def select_live_verse(verse_index: int) -> None:
        """What: Select a bounded verse, update controls/status, and persist its index.

        Why included: Verse navigation must synchronize the Display details and live output.
        Where used: Verse controls and row-specific verse-selection callbacks.
        Returns: None; persistence runs through a queue mutation worker.
        """
        queue_item = get_selected_queue_item()
        if queue_item is None:
            return
        verses = get_queue_item_verses(queue_item)
        if verses:
            state["selected_verse_index"] = max(0, min(len(verses) - 1, int(verse_index)))
        else:
            state["selected_verse_index"] = 0
        selected_index = int(state.get("selected_verse_index") or 0)
        queue_item["selected_verse_index"] = selected_index
        refresh_verse_selection_styles()
        if status is not None:
            live_payload = build_live_payload(queue_item)
            if live_payload is None:
                status.configure(
                    text=f"Live Item: None\nQueue Items: {len(state.get('queue_items') or [])}\nSelected Source: {str(queue_item.get('source') or 'item').title()} ({'Active' if bool(queue_item.get('active')) else 'Inactive'})"
                )
            else:
                verse_labels = get_queue_item_verse_labels(queue_item, len(verses))
                current_index = max(0, min(len(verse_labels) - 1, int(state.get("selected_verse_index") or 0))) if verse_labels else 0
                selected_label = verse_labels[current_index] if verse_labels else f"Verse {current_index + 1}"
                status.configure(
                    text=f"Live Item: {str(queue_item.get('title') or 'Untitled')} - {selected_label}\nQueue Items: {len(state.get('queue_items') or [])}\nSelected Source: {str(queue_item.get('source') or 'item').title()} (Active)"
                )
        request_push_live_selection(force=True)

        item_id = str(queue_item.get("id") or "")
        if item_id:
            run_queue_mutation(
                "set_selected_bible_verse",
                lambda: set_display_queue_item_selected_verse(item_id, selected_index),
                lambda _result: None,
            )

    def select_verse_for_item(item_id: str, verse_index: int) -> None:
        """What: Select a verse only when the requested ID is the current selected item.

        Why included: Tab callbacks must not change verse state for a row that lost selection.
        Where used: Callback exposed in display_state to other tab workflows.
        Returns: None; ignores missing or non-selected item IDs.
        """
        queue_item = get_selected_queue_item()
        if not isinstance(queue_item, dict) or str(queue_item.get("id") or "") != str(item_id or ""):
            return
        select_live_verse(verse_index)

    def bind_live_verse_selection(widget: tk.Widget, verse_index: int) -> None:
        """What: Require a double-click on verse text/frames while keeping explicit buttons unchanged.

        Why included: Accidental touchpad clicks must not call up different content.
        Where used: Verse detail headings, frames, and labels.
        Returns: None; registers a double-click binding on non-button widgets.
        """
        if isinstance(widget, tk.Button):
            return
        try:
            widget.configure(cursor="hand2")
        except tk.TclError:
            pass

        def handle_select(_event=None, chosen_index=verse_index) -> str:
            """What: Select the captured verse index and consume the click event.

            Why included: Prevents the click from triggering parent row selection afterward.
            Where used: Double-Button-1 binding installed by bind_live_verse_selection().
            Returns: Tk "break" marker after selecting the verse.
            """
            select_live_verse(chosen_index)
            return "break"

        widget.bind("<Double-Button-1>", handle_select, add="+")

    def bind_mousewheel_scroll(widget: tk.Widget, scroll_target: tk.Canvas | None) -> None:
        """What: Bind mouse-wheel events from nested controls to the queue canvas.

        Why included: Wheel input should scroll even when the pointer is over a row widget.
        Where used: Queue and verse-detail widget creation.
        Returns: None; no bindings are added when no canvas exists.
        """
        if scroll_target is None:
            return

        def handle_mousewheel(event) -> str:
            """What: Convert platform wheel input to canvas yview units.

            Why included: Windows delta and Linux button events encode direction differently.
            Where used: Mouse-wheel bindings installed by bind_mousewheel_scroll().
            Returns: "break" after consuming the event.
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

    def toggle_queue_item_active(item_id: str) -> None:
        """What: Toggle one persisted queue item's active/live status asynchronously.

        Why included: Only one queue item may be active for mirror output.
        Where used: Active checkbutton and title/row activation actions.
        Returns: None; refreshes the bridge-backed queue after the worker completes.
        """
        if not item_id:
            return
        def apply_toggled_state(updated_state) -> None:
            """What: Preserve row selection and request a refreshed queue after a toggle.

            Why included: The mutation worker result must be reflected by current bridge state.
            Where used: Success callback passed to run_queue_mutation().
            Returns: None; forces a live-payload refresh after the state reload.
            """
            queue_items = list(updated_state.get("queue_items") or []) if isinstance(updated_state, dict) else []
            target_item = next((item for item in queue_items if isinstance(item, dict) and str(item.get("id") or "") == item_id), None)
            state["selected_queue_item_id"] = item_id
            state["selected_verse_index"] = 0 if bool(getattr(target_item, 'get', lambda *_args: False)("active")) else int(state.get("selected_verse_index") or 0)
            state["force_live_push_after_refresh"] = True
            request_refresh_queue_from_bridge(item_id, delay_ms=0)

        run_queue_mutation(
            "toggle_queue_item_active",
            lambda: toggle_display_queue_item_active(item_id),
            apply_toggled_state,
        )

    def bind_queue_item_row_activation(widget: tk.Widget, item_id: str, excluded_widgets: tuple[tk.Widget, ...] = ()) -> None:
        """What: Bind row double-clicks to toggling its queue item's active status.

        Why included: Rows and source badges are convenient activation targets, while
        their dedicated controls must retain their own click behavior.
        Where used: create_queue_item_widgets() for row frame and source indicator.
        Returns: None; installs a Double-Button-1 handler.
        """
        try:
            widget.configure(cursor="hand2")
        except tk.TclError:
            pass

        def handle_row_activation(event=None, chosen_id=item_id, blocked_widgets=excluded_widgets) -> str:
            """What: Ignore clicks originating inside excluded controls, otherwise toggle the row.

            Why included: Nested button clicks should not also activate the entire row.
            Where used: Binding installed by bind_queue_item_row_activation().
            Returns: "break" to consume the handled event.
            """
            event_widget = getattr(event, "widget", None)
            for blocked_widget in blocked_widgets:
                current_widget = event_widget
                # Walk to the clicked widget's ancestors to distinguish button clicks from row clicks.
                while isinstance(current_widget, tk.Widget):
                    if current_widget == blocked_widget:
                        return "break"
                    current_name = str(current_widget)
                    blocked_name = str(blocked_widget)
                    if current_name == "." or current_name == blocked_name or not current_name.startswith(blocked_name + "."):
                        break
                    try:
                        parent_name = current_widget.winfo_parent()
                        current_widget = current_widget._nametowidget(parent_name) if parent_name else None
                    except Exception:
                        # A widget can disappear during the event; stop the ancestry check safely.
                        current_widget = None
            toggle_queue_item_active(chosen_id)
            return "break"

        widget.bind("<Double-Button-1>", handle_row_activation, add="+")

    def create_queue_item_widgets(item_id: str) -> dict[str, object]:
        """What: Create and register the controls for one queue item row.

        Why included: Queue rows are reused across refreshes to reduce widget churn.
        Where used: sync_queue_items_view() when an item ID has no cached widgets.
        Returns: Mapping of frame, selection controls, source badge, and row action buttons.
        """
        if queue_items_container is None:
            return {}
        item_frame = tk.Frame(queue_items_container, bd=2, relief="solid", padx=8, pady=8)
        item_frame.columnconfigure(1, weight=1)
        active_var = tk.BooleanVar(master=item_frame, value=False)
        active_toggle = tk.Checkbutton(
            item_frame,
            text="Active",
            variable=active_var,
            command=lambda chosen_id=item_id: toggle_queue_item_active(chosen_id),
        )
        active_toggle.grid(row=0, column=0, sticky="w", padx=(0, 8))
        title_button = tk.Button(
            item_frame,
            text="Untitled",
            anchor="w",
            justify="left",
        )
        title_button.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        button_row = tk.Frame(item_frame)
        button_row.grid(row=0, column=2, sticky="e")
        source_indicator = tk.Label(button_row, text="Item", justify="center", anchor="center")
        source_indicator.grid(row=0, column=0, padx=(0, 8))
        up_item_button = tk.Button(button_row, text="Up", command=lambda chosen_id=item_id: move_queue_item_by_id(chosen_id, -1))
        up_item_button.grid(row=0, column=1, padx=(0, 4))
        down_item_button = tk.Button(button_row, text="Down", command=lambda chosen_id=item_id: move_queue_item_by_id(chosen_id, 1))
        down_item_button.grid(row=0, column=2, padx=4)
        remove_item_button = tk.Button(button_row, text="Remove", command=lambda chosen_id=item_id: remove_queue_item_by_id(chosen_id))
        remove_item_button.grid(row=0, column=3, padx=(4, 0))
        apply_layout_style(
            [item_frame, active_toggle, title_button, button_row, up_item_button, down_item_button, remove_item_button],
            label_widgets=[source_indicator],
            input_widgets=[active_toggle, title_button, up_item_button, down_item_button, remove_item_button],
        )
        excluded_widgets = (active_toggle, up_item_button, down_item_button, remove_item_button)
        bind_queue_item_row_activation(item_frame, item_id, excluded_widgets)
        bind_queue_item_row_activation(title_button, item_id, excluded_widgets)
        bind_queue_item_row_activation(source_indicator, item_id, excluded_widgets)
        bind_mousewheel_scroll(active_toggle, queue_scroll_canvas)
        bind_mousewheel_scroll(item_frame, queue_scroll_canvas)
        bind_mousewheel_scroll(title_button, queue_scroll_canvas)
        bind_mousewheel_scroll(button_row, queue_scroll_canvas)
        bind_mousewheel_scroll(source_indicator, queue_scroll_canvas)
        bind_mousewheel_scroll(up_item_button, queue_scroll_canvas)
        bind_mousewheel_scroll(down_item_button, queue_scroll_canvas)
        bind_mousewheel_scroll(remove_item_button, queue_scroll_canvas)
        return {
            "frame": item_frame,
            "active_var": active_var,
            "active_toggle": active_toggle,
            "title": title_button,
            "button_row": button_row,
            "source_indicator": source_indicator,
            "up_button": up_item_button,
            "down_button": down_item_button,
            "remove_button": remove_item_button,
        }

    def update_queue_item_widgets(widgets: dict[str, object], queue_item: dict[str, object], index: int, total_items: int, selected_item_id: str, theme: dict[str, str] | None) -> None:
        """What: Update one existing queue row with content, ordering, active, and selection state.

        Why included: Reusing row widgets avoids rebuilding the full list on every bridge refresh.
        Where used: sync_queue_items_view() for each valid queue item.
        Returns: None; configures the row widgets in place.
        """
        item_id = str(queue_item.get("id") or "")
        item_frame = widgets.get("frame")
        active_var = widgets.get("active_var")
        title_button = widgets.get("title")
        source_indicator = widgets.get("source_indicator")
        up_item_button = widgets.get("up_button")
        down_item_button = widgets.get("down_button")
        remove_item_button = widgets.get("remove_button")
        active_toggle = widgets.get("active_toggle")
        button_row = widgets.get("button_row")
        if not isinstance(item_frame, tk.Frame) or not isinstance(title_button, tk.Button) or not isinstance(source_indicator, tk.Label):
            return
        is_active = bool(queue_item.get("active"))
        is_selected = item_id == selected_item_id
        if isinstance(active_var, tk.BooleanVar):
            active_var.set(is_active)
        item_frame.grid(row=index, column=0, sticky="ew", pady=(0, 8))
        item_frame.configure(highlightthickness=2 if is_selected else 0)
        title_button.configure(text=(format_queue_label(queue_item) if is_active else f"[Inactive] {format_queue_label(queue_item)}"))
        source_indicator.configure(text=format_queue_source_indicator(queue_item))
        source_background, source_foreground = get_queue_source_indicator_colors(queue_item)
        if isinstance(up_item_button, tk.Button):
            up_item_button.configure(state="disabled" if index == 0 else "normal")
        if isinstance(down_item_button, tk.Button):
            down_item_button.configure(state="disabled" if index >= total_items - 1 else "normal")
        apply_layout_style(
            [widget for widget in (item_frame, active_toggle, title_button, button_row, up_item_button, down_item_button, remove_item_button) if isinstance(widget, tk.Widget)],
            label_widgets=[source_indicator],
            input_widgets=[widget for widget in (active_toggle, title_button, up_item_button, down_item_button, remove_item_button) if isinstance(widget, tk.Widget)],
        )
        source_indicator.configure(bg=source_background, fg=source_foreground, padx=8, pady=3)
        if is_selected and theme is not None:
            title_button.configure(fg=theme["accent"], activeforeground=theme["accent"])
            item_frame.configure(highlightbackground=theme["accent"], highlightcolor=theme["accent"])
        else:
            default_border = theme["border"] if isinstance(theme, dict) else "#c8d0db"
            default_text = theme["text"] if isinstance(theme, dict) else "#202020"
            item_frame.configure(highlightbackground=default_border, highlightcolor=default_border)
            title_button.configure(fg=default_text, activeforeground=default_text)

    def sync_queue_items_view() -> None:
        """What: Reconcile cached queue-row widgets with the current queue mappings.

        Why included: Adds new rows, destroys removed rows, and updates existing rows efficiently.
        Where used: Queue state application and dynamic theme/style refreshes.
        Returns: None; updates the widget registry and scroll region.
        """
        sync_started_at = time.perf_counter()
        if queue_items_container is None:
            return
        queue_item_widgets = state.get("queue_item_widgets")
        if not isinstance(queue_item_widgets, dict):
            queue_item_widgets = {}
        queue_items = state.get("queue_items") or []
        selected_item_id = str(state.get("selected_queue_item_id") or "")
        theme = get_current_theme()
        valid_item_ids = {
            str(queue_item.get("id") or "")
            for queue_item in queue_items
            if isinstance(queue_item, dict) and str(queue_item.get("id") or "")
        }
        stale_item_ids = [item_id for item_id in list(queue_item_widgets.keys()) if item_id not in valid_item_ids]
        for item_id in stale_item_ids:
            widgets = queue_item_widgets.pop(item_id, None)
            if not isinstance(widgets, dict):
                continue
            item_frame = widgets.get("frame")
            if isinstance(item_frame, tk.Frame):
                item_frame.destroy()
        for index, queue_item in enumerate(queue_items):
            if not isinstance(queue_item, dict):
                continue
            item_id = str(queue_item.get("id") or "")
            if not item_id:
                continue
            widgets = queue_item_widgets.get(item_id)
            if not isinstance(widgets, dict):
                widgets = create_queue_item_widgets(item_id)
                queue_item_widgets[item_id] = widgets
            update_queue_item_widgets(widgets, queue_item, index, len(queue_items), selected_item_id, theme)
        queue_items_container.columnconfigure(0, weight=1)
        if queue_scroll_canvas is not None:
            queue_scroll_canvas.configure(scrollregion=queue_scroll_canvas.bbox("all"))
            queue_items_container.after_idle(
                lambda: queue_scroll_canvas.configure(scrollregion=queue_scroll_canvas.bbox("all"))
                if queue_scroll_canvas is not None and queue_scroll_canvas.winfo_exists()
                else None
            )
        state["queue_item_widgets"] = queue_item_widgets
        log_timing("display.sync_queue_items_view", (time.perf_counter() - sync_started_at) * 1000.0, threshold_ms=20.0, details=f"items={len(queue_items)}")

    def rebuild_queue_items_view() -> None:
        """What: Refresh the queue-row view using the incremental synchronization helper.

        Why included: Retains a semantic rebuild entry point for queue-state changes.
        Where used: apply_queue_state_from_bridge() when queue contents differ.
        Returns: None; delegates to sync_queue_items_view().
        """
        sync_queue_items_view()

    def refresh_queue_item_selection_styles() -> None:
        """What: Refresh selected-row border and title styling without rebuilding rows.

        Why included: Selection changes are frequent and should not recreate queue widgets.
        Where used: select_queue_item() and active-item state updates.
        Returns: None; skips rows whose widgets have been destroyed.
        """
        queue_item_widgets = state.get("queue_item_widgets")
        if not isinstance(queue_item_widgets, dict):
            return
        theme = get_current_theme()
        selected_item_id = str(state.get("selected_queue_item_id") or "")
        queue_items = state.get("queue_items") or []
        queue_by_id: dict[str, dict[str, object]] = {
            str(queue_item.get("id") or ""): queue_item
            for queue_item in queue_items
            if isinstance(queue_item, dict)
        }
        for item_id, widgets in queue_item_widgets.items():
            if not isinstance(widgets, dict):
                continue
            item_frame = widgets.get("frame")
            title_button = widgets.get("title")
            if not isinstance(item_frame, tk.Frame) or not isinstance(title_button, tk.Button):
                continue
            queue_item = queue_by_id.get(str(item_id))
            is_active = bool(queue_item.get("active")) if isinstance(queue_item, dict) else False
            is_selected = str(item_id) == selected_item_id
            try:
                item_frame.configure(highlightthickness=2 if is_selected else 0)
                title_button.configure(text=(format_queue_label(queue_item) if is_active else f"[Inactive] {format_queue_label(queue_item)}") if isinstance(queue_item, dict) else "Untitled")
                if is_selected and theme is not None:
                    item_frame.configure(highlightbackground=theme["accent"], highlightcolor=theme["accent"])
                    title_button.configure(fg=theme["accent"], activeforeground=theme["accent"])
                else:
                    default_border = theme["border"] if isinstance(theme, dict) else "#c8d0db"
                    default_text = theme["text"] if isinstance(theme, dict) else "#202020"
                    item_frame.configure(highlightbackground=default_border, highlightcolor=default_border)
                    title_button.configure(fg=default_text, activeforeground=default_text)
            except tk.TclError:
                continue

    def get_queue_item_verses(queue_item: dict[str, object] | None) -> list[str]:
        """What: Extract nonempty verse strings or fall back to the item's main text.

        Why included: Queue entries may represent multi-verse songs or a single text block.
        Where used: Live payload, verse controls, status, and details rendering.
        Returns: Ordered trimmed verse list; empty for missing/blank content.
        """
        if not isinstance(queue_item, dict):
            return []
        raw_verses = queue_item.get("verses")
        if isinstance(raw_verses, list):
            verses = [str(item).strip() for item in raw_verses if str(item).strip()]
            if verses:
                return verses
        text_value = str(queue_item.get("text") or "").strip()
        return [text_value] if text_value else []

    def get_queue_item_columns(queue_item: dict[str, object] | None) -> list[dict[str, object]]:
        """What: Normalize up to three structured Bible display columns.

        Why included: Malformed saved columns should not break the generic details view.
        Where used: Bible detail rendering and detail signatures.
        Returns: Column mappings containing title and trimmed verses.
        """
        if not isinstance(queue_item, dict):
            return []
        raw_columns = queue_item.get("display_columns")
        if not isinstance(raw_columns, list):
            return []
        columns: list[dict[str, object]] = []
        for raw_column in raw_columns[:3]:
            if not isinstance(raw_column, dict):
                continue
            raw_verses = raw_column.get("verses")
            if not isinstance(raw_verses, list):
                continue
            columns.append({
                "title": str(raw_column.get("title") or "Bible"),
                "verses": [str(value or "").strip() for value in raw_verses],
            })
        return columns

    def get_queue_item_verse_labels(queue_item: dict[str, object] | None, verse_count: int) -> list[str]:
        """What: Resolve saved verse labels or generate source-appropriate fallback labels.

        Why included: Preparation blocks and scripture verses need useful navigation names.
        Where used: Queue status and verse-detail headings.
        Returns: Exactly verse_count labels, using Block labels for Preparation.
        """
        if not isinstance(queue_item, dict):
            return [f"Verse {index + 1}" for index in range(max(0, verse_count))]
        raw_verse_labels = queue_item.get("verse_labels")
        if isinstance(raw_verse_labels, list):
            verse_labels = [str(item).strip() for item in raw_verse_labels if str(item).strip()]
            if len(verse_labels) >= verse_count:
                return verse_labels[:verse_count]
        source_value = str(queue_item.get("source") or "").strip().casefold()
        if source_value == "preparation":
            return [f"Block {index + 1}" for index in range(max(0, verse_count))]
        return [f"Verse {index + 1}" for index in range(max(0, verse_count))]

    def should_show_verse_heading(queue_item: dict[str, object] | None, verses: list[str]) -> bool:
        """What: Decide whether verse detail rows should include a separate heading.

        Why included: Welcome text is presented without verse headers, unlike songs and blocks.
        Where used: rebuild_selected_song_verses_view().
        Returns: True for multi-verse items and Library/Preparation sources, except Welcome.
        """
        if not isinstance(queue_item, dict):
            return False
        source_value = str(queue_item.get("source") or "").strip().casefold()
        if source_value == "welcome":
            return False
        return len(verses) > 1 or source_value in {"library", "preparation"}

    def get_details_title(queue_item: dict[str, object] | None) -> str:
        """What: Choose the detail-panel heading for the currently selected source.

        Why included: Welcome and Preparation details have source-specific panel names.
        Where used: refresh_details() when selection changes.
        Returns: Welcome Text, Preparation Blocks, or Selected Content.
        """
        if not isinstance(queue_item, dict):
            return "Selected Content"
        source_value = str(queue_item.get("source") or "").strip().casefold()
        if source_value == "welcome":
            return "Welcome Text"
        if source_value == "preparation":
            return "Preparation Blocks"
        return "Selected Content"

    def build_details_signature(queue_item: dict[str, object] | None):
        """What: Build a comparable signature from selected queue content and verse labels.

        Why included: Expensive detail widgets only need rebuilding when their data changes.
        Where used: refresh_details() and dynamic style refreshes.
        Returns: Tuple of stable item/content fields, or None when no item is selected.
        """
        if not isinstance(queue_item, dict):
            return None
        verses = get_queue_item_verses(queue_item)
        verse_labels = get_queue_item_verse_labels(queue_item, len(verses))
        return (
            str(queue_item.get("id") or ""),
            tuple(verses),
            tuple(verse_labels),
            tuple((str(column.get("title") or ""), tuple(column.get("verses") or [])) for column in get_queue_item_columns(queue_item)),
            should_show_verse_heading(queue_item, verses),
        )

    def refresh_verse_selection_styles() -> None:
        """What: Update Live/Show labels and highlight the selected verse row.

        Why included: Verse changes should be visually reflected without recreating all rows.
        Where used: select_live_verse() and selected-detail refreshes.
        Returns: None; updates cached verse widgets in place.
        """
        verse_item_widgets = state.get("verse_item_widgets")
        if not isinstance(verse_item_widgets, list):
            return
        theme = get_current_theme()
        selected_verse_index = max(0, int(state.get("selected_verse_index") or 0))
        for verse_index, widget_row in enumerate(verse_item_widgets):
            if not isinstance(widget_row, dict):
                continue
            verse_frame = widget_row.get("frame")
            control_button = widget_row.get("button")
            if not isinstance(verse_frame, tk.Frame) or not isinstance(control_button, tk.Button):
                continue
            is_selected = verse_index == selected_verse_index
            try:
                control_button.configure(text="Live" if is_selected else "Show")
                if is_selected and theme is not None:
                    verse_frame.configure(highlightthickness=2, highlightbackground=theme["accent"], highlightcolor=theme["accent"])
                    control_button.configure(fg=theme["accent"], activeforeground=theme["accent"])
                else:
                    default_border = theme["border"] if isinstance(theme, dict) else "#c8d0db"
                    default_text = theme["text"] if isinstance(theme, dict) else "#202020"
                    verse_frame.configure(highlightthickness=0, highlightbackground=default_border, highlightcolor=default_border)
                    control_button.configure(fg=default_text, activeforeground=default_text)
            except tk.TclError:
                continue

    def rebuild_selected_song_verses_view(queue_item: dict[str, object] | None) -> None:
        """What: Rebuild the details column for a selected queue item's verses or Bible columns.

        Why included: The middle Display panel exposes source content and verse navigation.
        Where used: schedule_details_rebuild() after selection or content changes.
        Returns: None; replaces detail widgets and updates their scroll region.
        """
        if verses_items_container is None:
            return
        for child in verses_items_container.winfo_children():
            child.destroy()
        state["verse_item_widgets"] = []
        verses = get_queue_item_verses(queue_item)
        display_columns = get_queue_item_columns(queue_item)
        if not verses:
            empty_label = tk.Label(verses_items_container, text="No queue item selected", justify="center", anchor="center")
            empty_label.grid(row=0, column=0, sticky="ew")
            apply_layout_style([verses_items_container], label_widgets=[empty_label])
            bind_mousewheel_scroll(empty_label, verses_scroll_canvas)
            return

        source_value = str((queue_item or {}).get("source") or "").strip().casefold()
        selected_index = max(0, min(int(state.get("selected_verse_index") or 0), max(0, len(verses) - 1)))
        if should_render_bible_columns_in_display_details(queue_item):
            bible_page = tk.Frame(verses_items_container, bd=1, relief="solid", padx=10, pady=12)
            bible_page.grid(row=0, column=0, sticky="nsew")
            bible_page.columnconfigure(0, weight=1)
            bible_page.columnconfigure(1, weight=1)
            bible_page.columnconfigure(2, weight=1)
            bible_page.rowconfigure(1, weight=1)

            nav_bar = tk.Frame(bible_page)
            nav_bar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
            nav_bar.columnconfigure(0, weight=1)
            nav_bar.columnconfigure(1, weight=1)
            prev_button = tk.Button(nav_bar, text="Previous", command=lambda: select_live_verse(max(0, selected_index - 1)))
            prev_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            next_button = tk.Button(nav_bar, text="Next", command=lambda: select_live_verse(min(len(verses) - 1, selected_index + 1)))
            next_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

            title_label = tk.Label(bible_page, text=str(queue_item.get("title") or "Bible"), anchor="w", justify="left")
            title_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10))

            for column_index, column in enumerate(display_columns[:3]):
                column_frame = tk.Frame(bible_page, bd=1, relief="solid", padx=8, pady=8)
                column_frame.grid(row=2, column=column_index, sticky="nsew", padx=(0 if column_index == 0 else 4, 0 if column_index == 2 else 4), pady=(0, 0))
                column_frame.columnconfigure(0, weight=1)
                column_title = tk.Label(column_frame, text=str(column.get("title") or "Bible"), anchor="center", justify="center")
                column_title.grid(row=0, column=0, sticky="ew", pady=(0, 6))
                column_verses = list(column.get("verses") or [])
                column_text = "-"
                if selected_index < len(column_verses):
                    column_text = str(column_verses[selected_index] or "-").strip() or "-"
                verse_text = tk.Label(column_frame, text=column_text, justify="left", anchor="nw", wraplength=220)
                verse_text.grid(row=1, column=0, sticky="nsew")
                apply_layout_style([column_frame], label_widgets=[column_title, verse_text], input_widgets=[])

            state["verse_item_widgets"] = [{"frame": bible_page, "button": prev_button}]
            verses_items_container.columnconfigure(0, weight=1)
            verses_items_container.update_idletasks()
            if verses_scroll_canvas is not None:
                verses_scroll_canvas.configure(scrollregion=verses_scroll_canvas.bbox("all"))
            return

        show_verse_heading = should_show_verse_heading(queue_item, verses)
        theme = get_current_theme()
        verse_labels = get_queue_item_verse_labels(queue_item, len(verses))
        verse_item_widgets: list[dict[str, tk.Widget]] = []
        for verse_index, verse_text in enumerate(verses):
            verse_frame = tk.Frame(verses_items_container, bd=1, relief="solid", padx=8, pady=8)
            verse_frame.grid(row=verse_index, column=0, sticky="ew", pady=(0, 8))
            verse_frame.columnconfigure(2, weight=1)
            control_button = tk.Button(
                verse_frame,
                text="Live" if verse_index == selected_index else "Show",
                command=lambda chosen_index=verse_index: select_live_verse(chosen_index),
            )
            verse_label = tk.Label(verse_frame, text=verse_text, justify="left", anchor="w", wraplength=460)
            if show_verse_heading:
                verse_title = tk.Label(verse_frame, text=verse_labels[verse_index] if verse_index < len(verse_labels) else f"Verse {verse_index + 1}", justify="left", anchor="w")
                verse_title.grid(row=0, column=0, sticky="nw", padx=(0, 12))
                control_button.grid(row=0, column=1, sticky="ne", padx=(0, 12))
                verse_label.grid(row=0, column=2, sticky="ew")
                apply_layout_style([verse_frame, control_button], label_widgets=[verse_title, verse_label], input_widgets=[control_button])
                bind_mousewheel_scroll(verse_title, verses_scroll_canvas)
                bind_live_verse_selection(verse_title, verse_index)
            else:
                control_button.grid(row=0, column=0, sticky="nw", padx=(0, 12))
                verse_label.grid(row=0, column=1, columnspan=2, sticky="ew")
                apply_layout_style([verse_frame, control_button], label_widgets=[verse_label], input_widgets=[control_button])
            if verse_index == selected_index and theme is not None:
                verse_frame.configure(highlightthickness=2, highlightbackground=theme["accent"], highlightcolor=theme["accent"])
                control_button.configure(fg=theme["accent"], activeforeground=theme["accent"])
            bind_live_verse_selection(verse_frame, verse_index)
            bind_live_verse_selection(control_button, verse_index)
            bind_mousewheel_scroll(verse_frame, verses_scroll_canvas)
            bind_mousewheel_scroll(control_button, verses_scroll_canvas)
            bind_mousewheel_scroll(verse_label, verses_scroll_canvas)
            bind_live_verse_selection(verse_label, verse_index)
            verse_item_widgets.append({"frame": verse_frame, "button": control_button})
        verses_items_container.columnconfigure(0, weight=1)
        state["verse_item_widgets"] = verse_item_widgets
        verses_items_container.update_idletasks()
        if verses_scroll_canvas is not None:
            verses_scroll_canvas.configure(scrollregion=verses_scroll_canvas.bbox("all"))

    def schedule_details_rebuild(details_signature) -> None:
        """What: Defer a details rebuild until idle and cancel any older pending rebuild.

        Why included: Multiple rapid selection changes should render only the latest details.
        Where used: refresh_details() when its content signature changes.
        Returns: None; stores the Tk after identifier in state.
        """
        pending_after_id = state.get("details_rebuild_after_id")
        if pending_after_id is not None:
            try:
                tab_frame.after_cancel(pending_after_id)
            except tk.TclError:
                pass

        def rebuild_when_idle() -> None:
            """What: Verify the current detail signature before rebuilding the view.

            Why included: A newer selection may replace this one before Tk reaches idle.
            Where used: after_idle callback scheduled by schedule_details_rebuild().
            Returns: None; discards stale callbacks.
            """
            state["details_rebuild_after_id"] = None
            current_item = get_selected_queue_item()
            if build_details_signature(current_item) != details_signature:
                return
            rebuild_selected_song_verses_view(current_item)

        state["details_rebuild_after_id"] = tab_frame.after_idle(rebuild_when_idle)

    def refresh_details() -> None:
        """What: Synchronize the details title, verse view, status, and action states.

        Why included: Queue selection and active state determine all middle-panel content.
        Where used: Selection, bridge refresh, and queue mutation completion.
        Returns: None; schedules detail reconstruction only when its signature changes.
        """
        queue_item = get_selected_queue_item()
        details_title_var.set(get_details_title(queue_item))
        if queue_item is None:
            if state.get("last_details_signature") is not None:
                state["last_details_signature"] = None
                schedule_details_rebuild(None)
            if status is not None:
                status.configure(text=f"Live Item: None\nQueue Items: {len(state.get('queue_items') or [])}\nSecondary Screen: Off")
            refresh_button.configure(state="normal")
            if clear_queue is not None:
                clear_queue.configure(state="normal" if state.get("queue_items") else "disabled")
            return
        details_signature = build_details_signature(queue_item)
        if details_signature != state.get("last_details_signature"):
            state["last_details_signature"] = details_signature
            schedule_details_rebuild(details_signature)
        else:
            refresh_verse_selection_styles()
        queue_items = state.get("queue_items") or []
        live_payload = build_live_payload(queue_item)
        live_text = str(live_payload.get("text") or "").strip() if isinstance(live_payload, dict) else ""
        active_label = "Active" if bool(queue_item.get("active")) else "Inactive"
        live_label = str(queue_item.get("title") or "Untitled") if queue_item else "None"
        if live_text and len(get_queue_item_verses(queue_item)) > 1:
            verse_labels = get_queue_item_verse_labels(queue_item, len(get_queue_item_verses(queue_item)))
            selected_verse_index = max(0, min(len(verse_labels) - 1, int(state.get("selected_verse_index") or 0))) if verse_labels else 0
            selected_label = verse_labels[selected_verse_index] if verse_labels else f"Verse {selected_verse_index + 1}"
            live_label = f"{live_label} - {selected_label}"
        if status is not None:
            if live_payload is None:
                status.configure(text=f"Live Item: None\nQueue Items: {len(queue_items)}\nSelected Source: {str(queue_item.get('source') or 'item').title()} ({active_label})")
            else:
                status.configure(text=f"Live Item: {live_label}\nQueue Items: {len(queue_items)}\nSelected Source: {str(queue_item.get('source') or 'item').title()} ({active_label})")
        refresh_button.configure(state="normal")
        if clear_queue is not None:
            clear_queue.configure(state="normal" if queue_items else "disabled")

    def preload_queued_bibles() -> None:
        """What: Capture Bible renderer settings and warm passage layouts on a worker.

        Why included: Sent and recalled items should be ready before Display is opened or activated.
        Where used: Applying bridge queue state and shared font/header setting changes.
        Returns: None; performs no live output and keeps all Tk reads on the UI thread.
        """
        payloads = []
        for item in state.get("queue_items") or []:
            if isinstance(item, dict) and str(item.get("source") or "").casefold() == "bible":
                payload = build_live_payload(dict(item, active=True))
                if payload is not None:
                    payloads.append(payload)
        if not payloads:
            state["bible_preload_signature"] = None
            return
        get_bounds = context.get("get_display_bounds")
        bounds = get_bounds() if callable(get_bounds) else {}
        width = max(1, int((bounds or {}).get("width") or 1920))
        height = max(1, int((bounds or {}).get("height") or 1080))
        signature = (repr(payloads), width, height)
        if signature == state.get("bible_preload_signature"):
            return
        state["bible_preload_signature"] = signature

        def preload_worker() -> None:
            """What: Fit all captured Bible passages without accessing Tk or live state.

            Why included: Font measurement can be slow and must not block tab switching.
            Where used: Background thread started by preload_queued_bibles().
            Returns: None; logs fitting failures for diagnosis.
            """
            try:
                preload_bible_payloads(payloads, width, height)
            except Exception as error:
                log_exception("display.preload_bibles", error)

        threading.Thread(target=preload_worker, name="DisplayBiblePreload", daemon=True).start()

    def apply_queue_state_from_bridge(bridge_state: dict[str, object], preserve_item_id: str | None = None) -> None:
        """What: Apply bridge data, restore a valid selection, and refresh queue/live views.

        Why included: The bridge is authoritative, while the tab retains selection and
        verse-navigation state between reads.
        Where used: Successful queue-load and mutation refresh callbacks.
        Returns: None; skips expensive row rebuilds when the queue signature is unchanged.
        """
        refresh_started_at = time.perf_counter()
        queue_items = list(bridge_state.get("queue_items") or [])
        queue_signature = build_items_signature(queue_items)
        same_queue = queue_signature == state.get("last_queue_signature")
        previous_selected_item_id = str(state.get("selected_queue_item_id") or "")
        state["queue_items"] = queue_items
        preload_queued_bibles()
        target_item_id = str(preserve_item_id or state.get("selected_queue_item_id") or "")
        if queue_items:
            matching_item_id = ""
            for queue_item in queue_items:
                if isinstance(queue_item, dict) and str(queue_item.get("id") or "") == target_item_id:
                    matching_item_id = target_item_id
                    break
            state["selected_queue_item_id"] = matching_item_id or str(queue_items[0].get("id") or "")
            selected_item = get_selected_queue_item()
            selected_verses = get_queue_item_verses(selected_item)
            if selected_verses:
                queued_index = selected_item.get("selected_verse_index") if isinstance(selected_item, dict) else None
                keep_local_index = state["selected_queue_item_id"] == previous_selected_item_id
                current_index = state.get("selected_verse_index") if keep_local_index else queued_index
                if current_index is None:
                    current_index = 0
                state["selected_verse_index"] = max(0, min(len(selected_verses) - 1, int(current_index or 0)))
            else:
                state["selected_verse_index"] = 0
        else:
            state["selected_queue_item_id"] = None
            state["selected_verse_index"] = 0
        force_live_push = bool(state.get("force_live_push_after_refresh"))
        state["force_live_push_after_refresh"] = False
        if queue_signature == state.get("last_queue_signature"):
            # If content is unchanged, update only selection-dependent UI instead of rebuilding every row.
            if str(state.get("selected_queue_item_id") or "") != previous_selected_item_id:
                refresh_queue_item_selection_styles()
                refresh_details()
                request_push_live_selection(force=force_live_push)
            elif force_live_push:
                request_push_live_selection(force=True)
            log_timing("display.refresh_queue_from_bridge", (time.perf_counter() - refresh_started_at) * 1000.0, threshold_ms=20.0, details=f"items={len(queue_items)} unchanged=1")
            return
        state["last_queue_signature"] = queue_signature
        rebuild_queue_items_view()
        refresh_details()
        request_push_live_selection(force=force_live_push)
        if callable(handle_queue_items_changed):
            handle_queue_items_changed()
        log_timing("display.refresh_queue_from_bridge", (time.perf_counter() - refresh_started_at) * 1000.0, threshold_ms=20.0, details=f"items={len(queue_items)} unchanged=0")

    def refresh_queue_from_bridge(preserve_item_id: str | None = None) -> None:
        """What: Load bridge queue state on a worker and apply only the newest response.

        Why included: Disk I/O must not block Tk, and overlapping refreshes can go stale.
        Where used: Initial tab load, refresh button, queue mutations, and recall.
        Returns: None; delegates UI updates to a token-checked callback.
        """
        request_token = int(state.get("queue_refresh_request_token") or 0) + 1
        state["queue_refresh_request_token"] = request_token

        def load_queue_state(expected_token=request_token, target_item_id=preserve_item_id) -> None:
            """What: Read normalized queue data from display_bridge and record query timing.

            Why included: SQLite/JSON access should happen outside Tk's event thread.
            Where used: Worker thread started by refresh_queue_from_bridge().
            Returns: None; queues the result callback unless loading raises.
            """
            started_at = time.perf_counter()
            try:
                bridge_state = load_display_queue_state()
            except Exception as error:
                log_exception("display.refresh_queue_from_bridge", error)
                return
            log_timing("display.load_display_queue_state", (time.perf_counter() - started_at) * 1000.0, threshold_ms=15.0)

            def apply_loaded_state() -> None:
                """What: Apply the captured bridge result only if its request token is current.

                Why included: A slower old refresh must not replace a newer queue state.
                Where used: UI-thread callback queued by load_queue_state().
                Returns: None; updates queue state and dependent views.
                """
                if expected_token != int(state.get("queue_refresh_request_token") or 0):
                    return
                apply_queue_state_from_bridge(bridge_state, target_item_id)

            schedule_on_ui_thread(apply_loaded_state)

        threading.Thread(target=load_queue_state, name="DisplayQueueRefresh", daemon=True).start()

    def request_refresh_queue_from_bridge(preserve_item_id: str | None = None, delay_ms: int = 30) -> None:
        """What: Debounce queue reads and retain the item ID to preserve across refresh.

        Why included: Several row actions may write in quick succession; coalescing
        refreshes avoids duplicate disk reads and redraws.
        Where used: Queue controls, service recall, and other callbacks.
        Returns: None; schedules refresh_queue_from_bridge() after delay_ms.
        """
        state["queue_refresh_target_item_id"] = str(preserve_item_id or state.get("queue_refresh_target_item_id") or "") or None
        pending_after_id = state.get("queue_refresh_after_id")
        if pending_after_id is not None:
            try:
                tab_frame.after_cancel(pending_after_id)
            except tk.TclError:
                pass

        def run_refresh() -> None:
            """What: Consume the pending target ID and start one bridge refresh.

            Why included: The timer should use the latest preserved selection value.
            Where used: after callback scheduled by request_refresh_queue_from_bridge().
            Returns: None; clears timer/target state before loading.
            """
            state["queue_refresh_after_id"] = None
            target_item_id = str(state.get("queue_refresh_target_item_id") or "") or None
            state["queue_refresh_target_item_id"] = None
            refresh_queue_from_bridge(target_item_id)

        state["queue_refresh_after_id"] = tab_frame.after(max(0, int(delay_ms)), run_refresh)

    def refresh_saved_services() -> None:
        """What: Load saved-service selector entries asynchronously and update its listbox.

        Why included: SQLite reads must not freeze queue interactions.
        Where used: Initial tab setup, refresh control, and save completion callback.
        Returns: None; token-checks both success and error UI callbacks.
        """
        request_token = int(state.get("saved_services_request_token") or 0) + 1
        state["saved_services_request_token"] = request_token

        def load_saved_services(expected_token=request_token) -> None:
            """What: Query compact saved-service entries on a worker thread.

            Why included: Service listing may perform disk I/O while the UI remains responsive.
            Where used: Worker launched by refresh_saved_services().
            Returns: None; schedules listbox/error updates on Tk's thread.
            """
            started_at = time.perf_counter()
            try:
                entries = list_saved_service_entries()
            except Exception as error:
                log_exception("display.refresh_saved_services", error)

                def handle_error() -> None:
                    """What: Disable recall only if this failure belongs to the latest request.

                    Why included: A stale worker failure should not override newer service data.
                    Where used: UI callback queued from load_saved_services() on error.
                    Returns: None; changes the Recall button state when it exists.
                    """
                    if expected_token != int(state.get("saved_services_request_token") or 0):
                        return
                    if recall_service_button is not None:
                        recall_service_button.configure(state="disabled")

                schedule_on_ui_thread(handle_error)
                return
            log_timing("display.list_saved_service_entries", (time.perf_counter() - started_at) * 1000.0, threshold_ms=15.0, details=f"entries={len(entries)}")

            def apply_entries() -> None:
                """What: Apply current saved-service results and rebuild labels if changed.

                Why included: Repeated identical service lists should not churn the listbox.
                Where used: UI callback queued after a successful service query.
                Returns: None; updates cached entries and recall-button availability.
                """
                if expected_token != int(state.get("saved_services_request_token") or 0):
                    return
                entries_signature = build_items_signature(entries)
                if entries_signature == state.get("last_saved_services_signature"):
                    if recall_service_button is not None:
                        recall_service_button.configure(state="normal" if entries else "disabled")
                    return
                state["last_saved_services_signature"] = entries_signature
                state["saved_services"] = entries
                if service_entries_list is not None:
                    service_entries_list.delete(0, tk.END)
                    for entry in entries:
                        service_entries_list.insert(tk.END, str(entry.get("label") or "Saved Service"))
                if recall_service_button is not None:
                    recall_service_button.configure(state="normal" if entries else "disabled")

            schedule_on_ui_thread(apply_entries)

        threading.Thread(target=load_saved_services, name="DisplaySavedServicesLoader", daemon=True).start()

    def request_refresh_saved_services(delay_ms: int = 60) -> None:
        """What: Coalesce saved-service list refreshes behind one Tk timer.

        Why included: Save/recall workflows can request several refreshes close together.
        Where used: Display state callback exposed to main.py and refresh controls.
        Returns: None; schedules refresh_saved_services() after delay_ms.
        """
        pending_after_id = state.get("saved_services_after_id")
        if pending_after_id is not None:
            try:
                tab_frame.after_cancel(pending_after_id)
            except tk.TclError:
                pass

        def run_refresh() -> None:
            """What: Clear the pending timer ID and launch the saved-service query.

            Why included: Keeps timer state accurate when a refresh callback fires.
            Where used: after callback scheduled by request_refresh_saved_services().
            Returns: None; delegates to refresh_saved_services().
            """
            state["saved_services_after_id"] = None
            refresh_saved_services()

        state["saved_services_after_id"] = tab_frame.after(max(0, int(delay_ms)), run_refresh)

    def recall_selected_service() -> None:
        """What: Restore the selected saved-service queue and appearance settings.

        Why included: The Display selector is the user entry point for service recall.
        Where used: Recall Service button command.
        Returns: None; prompts without changing state when no service is selected.
        """
        entry = get_selected_service_entry()
        if entry is None:
            messagebox.showinfo("Display", "Select a saved service to recall.", parent=tab_frame.winfo_toplevel())
            return
        restored_state = restore_display_queue_from_service_entry(str(entry.get("id") or ""))
        restore_display_settings = display_state.get("restore_display_settings") if isinstance(display_state, dict) else None
        if callable(restore_display_settings):
            restore_display_settings(restored_state.get("display_settings"))
        request_refresh_queue_from_bridge()

    def refresh_selected_from_file() -> None:
        """What: Reload the bridge while attempting to preserve the selected queue row.

        Why included: Display should reconcile external/content-tab changes on demand.
        Where used: Refresh button and notebook reactivation.
        Returns: None; schedules a bridge refresh for the current selected ID.
        """
        selected_item = get_selected_queue_item()
        selected_item_id = str(selected_item.get("id") or "") if selected_item else None
        request_refresh_queue_from_bridge(selected_item_id or None)

    def run_queue_mutation(task_name: str, worker, on_success) -> None:
        """What: Run one bridge mutation off-thread and marshal its latest result to Tk.

        Why included: Queue persistence must not stall UI actions; tokens reject stale completions.
        Where used: Active toggles, verse selection, reordering, removal, and clear-all.
        Returns: None; invokes on_success on the UI thread for the current mutation.
        """
        request_token = int(state.get("queue_mutation_request_token") or 0) + 1
        state["queue_mutation_request_token"] = request_token

        def run_worker(expected_token=request_token) -> None:
            """What: Execute a queue mutation and queue its token-checked result callback.

            Why included: Worker exceptions and timing are handled consistently for all actions.
            Where used: Daemon worker launched by run_queue_mutation().
            Returns: None; logs failures and skips callbacks for failed operations.
            """
            started_at = time.perf_counter()
            try:
                result = worker()
            except Exception as error:
                log_exception(f"display.{task_name}", error)
                return
            log_timing(f"display.{task_name}", (time.perf_counter() - started_at) * 1000.0, threshold_ms=15.0)

            def apply_result() -> None:
                """What: Deliver a mutation result only when no newer mutation superseded it.

                Why included: Out-of-order worker completion must not apply stale UI state.
                Where used: UI callback queued by run_worker().
                Returns: None; calls the supplied success handler when current.
                """
                if expected_token != int(state.get("queue_mutation_request_token") or 0):
                    return
                on_success(result)

            schedule_on_ui_thread(apply_result)

        threading.Thread(target=run_worker, name=f"Display{task_name}", daemon=True).start()

    def refresh_dynamic_styles() -> None:
        """What: Re-style queue rows, rebuild selected details, and refresh the live payload.

        Why included: Theme/font changes affect widgets and renderer output owned by this tab.
        Where used: Registered in display_state for main theme/font callbacks.
        Returns: None; schedules details rendering and live-payload synchronization.
        """
        preload_queued_bibles()
        sync_queue_items_view()
        details_signature = build_details_signature(get_selected_queue_item())
        state["last_details_signature"] = details_signature
        schedule_details_rebuild(details_signature)
        request_push_live_selection()

    if isinstance(display_state, dict):
        display_state["refresh_dynamic_styles"] = refresh_dynamic_styles
        display_state["refresh_render_settings"] = lambda: (preload_queued_bibles(), request_push_live_selection(force=True))
        display_state["refresh_from_bridge"] = lambda: request_refresh_queue_from_bridge(str(state.get("selected_queue_item_id") or "") or None)
        display_state["get_queue_items"] = lambda: list(state.get("queue_items") or [])
        display_state["get_selected_queue_item"] = lambda: get_selected_queue_item()
        display_state["select_verse_for_item"] = select_verse_for_item
        display_state["refresh_saved_services"] = request_refresh_saved_services

    def move_queue_item_by_id(item_id: str, step: int) -> None:
        """What: Persist a relative queue reorder and refresh while preserving that row.

        Why included: Row-level Up/Down controls operate by stable item ID.
        Where used: Queue-row action buttons and selected-row movement.
        Returns: None; ignores empty IDs and refreshes after mutation success.
        """
        if not item_id:
            return
        run_queue_mutation(
            "move_queue_item",
            lambda: move_display_queue_item(item_id, step),
            lambda _result: request_refresh_queue_from_bridge(item_id, delay_ms=10),
        )

    def set_queue_item_active(item_id: str, active: bool) -> None:
        """What: Persist an explicit active state and refresh the selected queue row.

        Why included: Callers that know the desired state need not invert a possibly stale flag.
        Where used: Programmatic active-state control paths.
        Returns: None; forces a live push after the bridge refresh when activating.
        """
        if not item_id:
            return
        def apply_active_state(updated_state) -> None:
            """What: Preserve target selection and refresh after the active-state write.

            Why included: The returned bridge state may change which item is active.
            Where used: Success callback for set_queue_item_active() mutation.
            Returns: None; requests a live-payload refresh.
            """
            state["selected_queue_item_id"] = item_id
            if active:
                state["selected_verse_index"] = 0
            state["force_live_push_after_refresh"] = True
            request_refresh_queue_from_bridge(item_id, delay_ms=0)

        run_queue_mutation(
            "set_queue_item_active",
            lambda: set_display_queue_item_active(item_id, active),
            apply_active_state,
        )

    if isinstance(display_state, dict):
        display_state["set_queue_item_active"] = set_queue_item_active

    def move_selected_item(step: int) -> None:
        """What: Move the currently selected queue item by the requested step.

        Why included: Footer actions reuse the stable-ID row mutation operation.
        Where used: Selected-item movement controls.
        Returns: None; no-ops when selection or ID is absent.
        """
        queue_item = get_selected_queue_item()
        if queue_item is None:
            return
        item_id = str(queue_item.get("id") or "")
        if not item_id:
            return
        move_queue_item_by_id(item_id, step)

    def remove_queue_item_by_id(item_id: str) -> None:
        """What: Persist removal by stable ID and refresh the queue from the bridge.

        Why included: Row Remove actions must not discard unrelated queue items.
        Where used: Remove button in each queue row.
        Returns: None; ignores empty IDs.
        """
        if not item_id:
            return
        run_queue_mutation(
            "remove_queue_item",
            lambda: remove_display_queue_item(item_id),
            lambda _result: request_refresh_queue_from_bridge(),
        )

    def remove_selected_item() -> None:
        """What: Remove the currently selected queue item by its stable ID.

        Why included: The selected-item action should use the same persistence path as row removal.
        Where used: Remove Selected control.
        Returns: None; no-ops when there is no selected item.
        """
        queue_item = get_selected_queue_item()
        if queue_item is None:
            return
        item_id = str(queue_item.get("id") or "")
        if not item_id:
            return
        remove_queue_item_by_id(item_id)

    def clear_queue_items() -> None:
        """What: Clear all persisted queue items through a background mutation.

        Why included: Clear-all is destructive and should refresh UI only after bridge success.
        Where used: Clear Queue control.
        Returns: None; queues a bridge refresh after clearing.
        """
        run_queue_mutation(
            "clear_queue_items",
            clear_display_queue_items,
            lambda _result: request_refresh_queue_from_bridge(),
        )

    def handle_notebook_tab_changed(_event=None) -> None:
        """What: Refresh queue state when the notebook switches back to Display.

        Why included: Other tabs or processes may update the shared bridge while Display is hidden.
        Where used: Notebook <<NotebookTabChanged>> binding.
        Returns: None; ignores changes to other tabs or unavailable notebook state.
        """
        if not isinstance(notebook, ttk.Notebook):
            return
        try:
            selected_tab = notebook.nametowidget(notebook.select())
        except tk.TclError:
            return
        if selected_tab == tab_frame:
            refresh_selected_from_file()

    for column_index in range(3):
        column_frame = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
        column_frame.grid(row=0, column=column_index, sticky="nsew", padx=8, pady=12)
        column_frame.columnconfigure(0, weight=1)
        if column_index == 0:
            column_frame.rowconfigure(1, weight=1)
            title = tk.Label(column_frame, text="Display Queue", justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            queue_wrapper = tk.Frame(column_frame)
            queue_wrapper.grid(row=1, column=0, sticky="nsew")
            queue_wrapper.columnconfigure(0, weight=1)
            queue_wrapper.rowconfigure(0, weight=1)
            queue_scroll_canvas = tk.Canvas(queue_wrapper, highlightthickness=0, bd=0)
            queue_scroll_canvas.grid(row=0, column=0, sticky="nsew")
            queue_scrollbar = tk.Scrollbar(queue_wrapper, orient="vertical", command=queue_scroll_canvas.yview)
            queue_scrollbar.grid(row=0, column=1, sticky="ns")
            queue_scroll_canvas.configure(yscrollcommand=queue_scrollbar.set)
            queue_items_container = tk.Frame(queue_scroll_canvas)
            queue_scroll_canvas.create_window((0, 0), window=queue_items_container, anchor="nw")
            queue_items_container.bind("<Configure>", lambda _event: queue_scroll_canvas.configure(scrollregion=queue_scroll_canvas.bbox("all")))
            queue_scroll_canvas.bind("<Configure>", lambda event: queue_scroll_canvas.itemconfigure("all", width=event.width))
            bind_mousewheel_scroll(queue_wrapper, queue_scroll_canvas)
            bind_mousewheel_scroll(queue_scroll_canvas, queue_scroll_canvas)
            bind_mousewheel_scroll(queue_items_container, queue_scroll_canvas)
            controls = tk.Frame(column_frame)
            controls.grid(row=2, column=0, sticky="ew", pady=(8, 0))
            controls.columnconfigure(0, weight=1)
            refresh_button = tk.Button(controls, text="Refresh")
            refresh_button.grid(row=0, column=0, padx=4, sticky="ew")
            control_widgets = [refresh_button]
            tab_widgets.extend([column_frame, title, queue_wrapper, queue_scroll_canvas, queue_scrollbar, queue_items_container, controls, *control_widgets])
            tab_labels.append(title)
            tab_inputs.extend([queue_scroll_canvas, *control_widgets])
            apply_layout_style([column_frame, queue_wrapper, queue_scroll_canvas, queue_scrollbar, queue_items_container, controls, *control_widgets], label_widgets=[title], input_widgets=[refresh_button])
        elif column_index == 1:
            column_frame.rowconfigure(1, weight=1)
            title = tk.Label(column_frame, textvariable=details_title_var, justify="center", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            verses_wrapper = tk.Frame(column_frame)
            verses_wrapper.grid(row=1, column=0, sticky="nsew")
            verses_wrapper.columnconfigure(0, weight=1)
            verses_wrapper.rowconfigure(0, weight=1)
            verses_scroll_canvas = tk.Canvas(verses_wrapper, highlightthickness=0, bd=0)
            verses_scroll_canvas.grid(row=0, column=0, sticky="nsew")
            verses_scrollbar = tk.Scrollbar(verses_wrapper, orient="vertical", command=verses_scroll_canvas.yview)
            verses_scrollbar.grid(row=0, column=1, sticky="ns")
            verses_scroll_canvas.configure(yscrollcommand=verses_scrollbar.set)
            verses_items_container = tk.Frame(verses_scroll_canvas)
            verses_scroll_canvas.create_window((0, 0), window=verses_items_container, anchor="nw")
            verses_items_container.bind("<Configure>", lambda _event: verses_scroll_canvas.configure(scrollregion=verses_scroll_canvas.bbox("all")))
            verses_scroll_canvas.bind("<Configure>", lambda event: verses_scroll_canvas.itemconfigure("all", width=event.width))
            bind_mousewheel_scroll(verses_wrapper, verses_scroll_canvas)
            bind_mousewheel_scroll(verses_scroll_canvas, verses_scroll_canvas)
            bind_mousewheel_scroll(verses_items_container, verses_scroll_canvas)
            tab_widgets.extend([column_frame, title, verses_wrapper, verses_scroll_canvas, verses_scrollbar, verses_items_container])
            tab_labels.append(title)
            tab_inputs.append(verses_scroll_canvas)
            apply_layout_style([column_frame, verses_wrapper, verses_scroll_canvas, verses_scrollbar, verses_items_container], label_widgets=[title])
        else:
            tab_widgets.append(column_frame)
            apply_layout_style([column_frame])

    refresh_button.configure(command=refresh_selected_from_file)
    if clear_live is not None:
        clear_live.configure(command=refresh_selected_from_file)
    if refresh_services_button is not None:
        refresh_services_button.configure(command=refresh_saved_services)
    if recall_service_button is not None:
        recall_service_button.configure(command=recall_selected_service)
    if clear_queue is not None:
        clear_queue.configure(command=clear_queue_items)
    if isinstance(notebook, ttk.Notebook):
        notebook.bind("<<NotebookTabChanged>>", handle_notebook_tab_changed, add="+")
    if is_display_tab_active():
        request_refresh_queue_from_bridge(delay_ms=0)
        refresh_saved_services()