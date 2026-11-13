"""Exercise the Tk app's queue, service recall, and menu workflows end to end."""

from __future__ import annotations

import gc
import shutil
import tempfile
import time
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import ttk

import display_bridge
import main
import perf_logging


def _build_queue_item(item_id: str, title: str, *, active: bool = False) -> dict[str, object]:
    """What: Build a minimally complete queue-item mapping for UI exercises.

    Why included: Tests need representative persisted items without relying on tab UI.
    Where used: Queue refresh, reorder, and activation scenarios in run_ui_smoke_test().
    Returns: Queue item dictionary with predictable media and display fields.
    """
    return {
        "id": item_id,
        "active": active,
        "source": "welcome" if item_id.startswith("welcome") else ("bible" if item_id.startswith("bible") else "library"),
        "title": title,
        "subtitle": "",
        "created_at": "2026-07-15T00:00:00",
        "text": f"Text for {title}",
        "verses": [],
        "verse_labels": [],
        "font_value": ["Segoe UI", 12, "normal"],
        "background_path": "Backgrounds/Images/default_background.png",
        "scroll_enabled": bool(item_id.startswith("welcome")),
        "scroll_speed": 7,
    }


def _assert(condition: bool, message: str) -> None:
    """What: Raise an explanatory AssertionError when a smoke-test condition is false.

    Why included: The UI smoke test runs as a standalone script without pytest.
    Where used: Assertions throughout run_ui_smoke_test().
    Returns: None when true; raises AssertionError otherwise.
    """
    if not condition:
        raise AssertionError(message)


def _read_text_if_exists(path: Path) -> str:
    """What: Read a UTF-8 diagnostic file when it exists and is accessible.

    Why included: Failure messages can include captured application error logs.
    Where used: Save-workflow assertions in run_ui_smoke_test().
    Returns: File text, or an empty string when missing or unreadable.
    """
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def _iter_widgets(widget: tk.Misc):
    """What: Yield a Tk widget and all descendants in depth-first order.

    Why included: Smoke tests locate controls in the real nested application UI.
    Where used: Widget-finder helpers below.
    Returns: Iterator of the supplied widget tree.
    """
    yield widget
    for child in widget.winfo_children():
        yield from _iter_widgets(child)


def _find_notebook(root: tk.Tk) -> ttk.Notebook:
    """What: Find the first ttk Notebook below the application root.

    Why included: UI tests need to select tabs and inspect their widget trees.
    Where used: run_ui_smoke_test() during application setup.
    Returns: Notebook widget; raises AssertionError when none exists.
    """
    for widget in _iter_widgets(root):
        if isinstance(widget, ttk.Notebook):
            return widget
    raise AssertionError("Notebook widget not found")


def _find_button_by_text(root: tk.Misc, text: str) -> tk.Button:
    """What: Locate a Tk Button whose displayed text exactly matches the query.

    Why included: Tests invoke visible controls as users would, without private state APIs.
    Where used: Refresh and Save actions in run_ui_smoke_test().
    Returns: Matching button; raises AssertionError when not found.
    """
    for widget in _iter_widgets(root):
        if isinstance(widget, tk.Button) and str(widget.cget("text")) == text:
            return widget
    raise AssertionError(f"Button '{text}' not found")


def _find_tab_frame_by_label(notebook: ttk.Notebook, label: str) -> tk.Widget:
    """What: Resolve a notebook page by its visible tab label.

    Why included: Tests need tab widgets without depending on generated Tk names.
    Where used: Welcome and Preparation widget discovery helpers.
    Returns: Tab frame; raises AssertionError when the label is absent.
    """
    for tab_id in notebook.tabs():
        if str(notebook.tab(tab_id, "text")) == label:
            return notebook.nametowidget(tab_id)
    raise AssertionError(f"Tab '{label}' not found")


def _find_welcome_text_widgets(notebook: ttk.Notebook) -> tuple[tk.Text, tk.Text]:
    """What: Find the editable Welcome Note and Birthday Message text controls.

    Why included: Service recall must restore both fields independently.
    Where used: run_ui_smoke_test() to verify Welcome schedule recall.
    Returns: Tuple of editable Text widgets in row order; raises if unavailable.
    """
    welcome_tab = _find_tab_frame_by_label(notebook, "Welcome")
    birthday_label = next((widget for widget in _iter_widgets(welcome_tab) if isinstance(widget, tk.Label) and str(widget.cget("text")) == "Birthday Message"), None)
    welcome_label = next((widget for widget in _iter_widgets(welcome_tab) if isinstance(widget, tk.Label) and str(widget.cget("text")) == "Welcome Note"), None)
    if not isinstance(birthday_label, tk.Label) or not isinstance(welcome_label, tk.Label):
        raise AssertionError("Welcome editable text widgets not found")
    welcome_row = welcome_label.nametowidget(welcome_label.winfo_parent())
    welcome_column = welcome_row.nametowidget(welcome_row.winfo_parent())
    text_widgets = sorted(
        (
            widget
            for widget in welcome_column.winfo_children()
            if isinstance(widget, tk.Text) and str(widget.cget("state")) != "disabled"
        ),
        key=lambda widget: int(widget.grid_info().get("row", -1)),
    )
    if len(text_widgets) < 2:
        raise AssertionError("Welcome editable text widgets not found")
    return text_widgets[0], text_widgets[1]


def _find_preparation_widgets(notebook: ttk.Notebook) -> tuple[tk.Entry, tk.Text]:
    """What: Find the Preparation header Entry and editable body Text widget.

    Why included: Recalled preparation content must restore its title and blocks.
    Where used: run_ui_smoke_test() for morning, night, and empty schedule recalls.
    Returns: Pair of editor widgets; raises AssertionError if either is missing.
    """
    preparation_tab = _find_tab_frame_by_label(notebook, "Preparation")
    header_entry = next((widget for widget in _iter_widgets(preparation_tab) if isinstance(widget, tk.Entry)), None)
    text_widget = next((widget for widget in _iter_widgets(preparation_tab) if isinstance(widget, tk.Text) and str(widget.cget("state")) == "normal"), None)
    if not isinstance(header_entry, tk.Entry) or not isinstance(text_widget, tk.Text):
        raise AssertionError("Preparation editor widgets not found")
    return header_entry, text_widget


def _find_preacher_entry(root: tk.Misc) -> tk.Entry:
    """What: Find the Entry aligned with the visible Preacher label.

    Why included: The UI smoke test verifies schedule-specific preacher restoration.
    Where used: run_ui_smoke_test() after locating the main form.
    Returns: Matching Entry; raises AssertionError if not found.
    """
    for widget in _iter_widgets(root):
        if not isinstance(widget, tk.Label) or str(widget.cget("text")) != "Preacher":
            continue
        parent = widget.nametowidget(widget.winfo_parent())
        target_row = int(widget.grid_info().get("row", -1))
        for sibling in parent.winfo_children():
            if isinstance(sibling, tk.Entry) and int(sibling.grid_info().get("row", -2)) == target_row:
                return sibling
    raise AssertionError("Preacher entry not found")


def _find_time_option_menu(root: tk.Misc) -> tk.Menubutton:
    """What: Locate the option menu containing Morning, Night, and Other choices.

    Why included: Service recall is driven through the same time selector as the UI.
    Where used: run_ui_smoke_test() to switch between saved schedules.
    Returns: Matching Menubutton; raises AssertionError when absent.
    """
    for widget in _iter_widgets(root):
        if isinstance(widget, tk.Menubutton):
            menu_name = str(widget.cget("menu") or "")
            if not menu_name:
                continue
            menu_widget = widget.nametowidget(menu_name)
            labels = [str(menu_widget.entrycget(index, "label")) for index in range(int(menu_widget.index("end") or -1) + 1)]
            if {"Morning", "Night", "Other"}.issubset(set(labels)):
                return widget
    raise AssertionError("Time option menu not found")


def _find_queue_row_frame(root: tk.Misc, title_text: str) -> tk.Frame | None:
    """What: Find a queue-row frame with a title button and reorder/remove controls.

    Why included: Tests need to exercise row actions without relying on internal IDs.
    Where used: Queue reorder and activation scenarios in run_ui_smoke_test().
    Returns: Matching Frame, or None when the requested row is not rendered.
    """
    for widget in _iter_widgets(root):
        if not isinstance(widget, tk.Frame):
            continue
        children = widget.winfo_children()
        titles = [child for child in children if isinstance(child, tk.Button) and title_text in str(child.cget("text"))]
        controls = [child for child in _iter_widgets(widget) if isinstance(child, tk.Button) and str(child.cget("text")) in {"Up", "Down", "Remove"}]
        if titles and controls:
            return widget
    return None


def _find_row_control(row_frame: tk.Frame, text: str) -> tk.Button:
    """What: Find a queue-row Button by its displayed command text.

    Why included: Allows tests to invoke user-facing reorder actions.
    Where used: run_ui_smoke_test() for Up and Down row controls.
    Returns: Matching Button; raises AssertionError when missing.
    """
    for widget in _iter_widgets(row_frame):
        if isinstance(widget, tk.Button) and str(widget.cget("text")) == text:
            return widget
    raise AssertionError(f"Row control '{text}' not found")


def _find_row_checkbutton(row_frame: tk.Frame, text: str) -> tk.Checkbutton:
    """What: Find a queue-row Checkbutton by its visible label.

    Why included: Activation behavior is verified through the rendered row control.
    Where used: run_ui_smoke_test() for queue Active toggles.
    Returns: Matching Checkbutton; raises AssertionError when missing.
    """
    for widget in _iter_widgets(row_frame):
        if isinstance(widget, tk.Checkbutton) and str(widget.cget("text")) == text:
            return widget
    raise AssertionError(f"Row checkbutton '{text}' not found")


def _pump_until(root: tk.Tk, predicate, timeout_seconds: float = 5.0) -> None:
    """What: Process Tk events until a predicate succeeds or the timeout expires.

    Why included: Workers and after callbacks must run while smoke tests await UI state.
    Where used: Throughout run_ui_smoke_test() after invoking asynchronous actions.
    Returns: None on success; raises AssertionError when the deadline is exceeded.
    """
    deadline = time.time() + timeout_seconds
    # Keep Tk responsive while background workers and scheduled callbacks satisfy the predicate.
    while time.time() < deadline:
        root.update_idletasks()
        root.update()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for UI condition")


def _queue_ids() -> list[str]:
    """What: Read ordered queue item IDs from the shared display bridge.

    Why included: Tests compare persisted queue order after UI actions complete.
    Where used: Queue refresh/reorder/recall waits in run_ui_smoke_test().
    Returns: List of string IDs from the currently persisted queue.
    """
    return [str(item.get("id") or "") for item in list(display_bridge.load_display_queue_state().get("queue_items") or [])]


def _build_saved_welcome_queue_item(item_id: str, welcome_text: str, birthday_text: str) -> dict[str, object]:
    """What: Build a persisted Welcome queue item with its two saved message fields.

    Why included: Recall tests need service data in the shape consumed by Welcome.
    Where used: Morning and night fixtures in run_ui_smoke_test().
    Returns: Active Welcome item mapping with serialized welcome_sections.
    """
    return {
        "id": item_id,
        "active": True,
        "source": "welcome",
        "title": "Welcome",
        "subtitle": "Welcome Message",
        "created_at": "2026-07-15T00:00:00",
        "text": welcome_text,
        "font_value": ["Segoe UI", 12, "normal"],
        "background_path": "Backgrounds/Images/default_background.png",
        "scroll_enabled": False,
        "scroll_speed": 7,
        "welcome_sections": {
            "combine_enabled": True,
            "centered_text": welcome_text,
            "centered_detail_text": birthday_text,
            "detail_text": "",
        },
    }


def _build_saved_preparation_queue_item(item_id: str, header_text: str, body_blocks: list[str]) -> dict[str, object]:
    """What: Build a persisted Preparation item with header and ordered text blocks.

    Why included: Recall tests verify legacy-compatible verse and label serialization.
    Where used: Morning and night fixtures in run_ui_smoke_test().
    Returns: Inactive Preparation queue-item mapping.
    """
    verses = [header_text]
    verse_labels = ["Header Only"]
    for block_index, block_text in enumerate(body_blocks, start=1):
        verses.append(f"{header_text}\n\n{block_text}")
        verse_labels.append(f"Text Block {block_index}")
    return {
        "id": item_id,
        "active": False,
        "source": "preparation",
        "title": header_text,
        "subtitle": "Preparation Item",
        "created_at": "2026-07-15T00:00:00",
        "text": verses[1] if len(verses) > 1 else header_text,
        "verses": verses,
        "verse_labels": verse_labels,
        "font_value": ["Segoe UI", 24, "normal"],
        "background_path": "Backgrounds/Images/default_background.png",
    }


def run_ui_smoke_test(temp_root: Path) -> None:
    """Exercise queue, save/recall, settings, and preview behavior through real Tk UI.

    What: Redirects persistence and dialogs to temporary/test state, drives controls,
    checks queue operations and service restoration, and verifies display settings.
    Why included: Covers cross-module workflows that isolated backend checks cannot
    validate, including worker-to-Tk callback behavior.
    Where used: main_cli() in the standalone UI reliability smoke test.
    Returns: None; restores monkeypatched globals in finally and raises on failures.
    """
    original_bridge_relative_path = display_bridge.DISPLAY_QUEUE_BRIDGE_RELATIVE_PATH
    original_service_relative_path = display_bridge.SERVICE_DB_RELATIVE_PATH
    original_timing_relative_path = perf_logging.TIMING_LOG_RELATIVE_PATH
    original_error_relative_path = perf_logging.ERROR_LOG_RELATIVE_PATH
    original_showinfo = main.messagebox.showinfo
    original_showwarning = main.messagebox.showwarning
    original_showerror = main.messagebox.showerror
    original_save_service_entry = main.save_service_entry
    original_ui_settings_path = main.get_ui_settings_path
    save_call_state = {"count": 0}
    message_state = {"info": [], "warning": [], "error": []}
    try:
        display_bridge.DISPLAY_QUEUE_BRIDGE_RELATIVE_PATH = temp_root / "display_queue_bridge.json"
        display_bridge.SERVICE_DB_RELATIVE_PATH = temp_root / "service.db"
        perf_logging.TIMING_LOG_RELATIVE_PATH = temp_root / "ui_timing.log"
        perf_logging.ERROR_LOG_RELATIVE_PATH = temp_root / "ui_errors.log"
        main.get_ui_settings_path = lambda: temp_root / "ui_settings.json"
        main.messagebox.showinfo = lambda *args, **kwargs: message_state["info"].append((args, kwargs))
        main.messagebox.showwarning = lambda *args, **kwargs: message_state["warning"].append((args, kwargs))
        main.messagebox.showerror = lambda *args, **kwargs: message_state["error"].append((args, kwargs))

        def tracked_save_service_entry(*args, **kwargs):
            """What: Count service-save invocations while delegating to the real writer.

            Why included: The smoke test confirms Save reaches persistence exactly
            through the application workflow.
            Where used: Temporarily replaces main.save_service_entry during this test.
            Returns: The original writer's saved service mapping.
            """
            save_call_state["count"] += 1
            return original_save_service_entry(*args, **kwargs)

        main.save_service_entry = tracked_save_service_entry

        root = main.build_ui()
        root.withdraw()
        _assert(root.title() == "PewVista", f"Expected product title PewVista, got {root.title()!r}")
        notebook = _find_notebook(root)
        notebook.select(0)
        _pump_until(root, lambda: notebook.index(notebook.select()) == 0)
        preacher_entry = _find_preacher_entry(root)
        welcome_text_widget, birthday_text_widget = _find_welcome_text_widgets(notebook)
        preparation_header_entry, preparation_text_widget = _find_preparation_widgets(notebook)

        initial_queue = [
            _build_queue_item("welcome-1", "Welcome", active=True),
            _build_queue_item("song-1", "Song One"),
            _build_queue_item("bible-1", "Bible Reading"),
        ]
        display_bridge.save_display_queue_state({"queue_items": initial_queue}, record_snapshot=False)
        _find_button_by_text(root, "Refresh").invoke()
        _pump_until(root, lambda: _queue_ids() == ["welcome-1", "song-1", "bible-1"])
        _pump_until(root, lambda: _find_queue_row_frame(root, "Welcome") is not None)

        welcome_row = _find_queue_row_frame(root, "Welcome")
        _assert(welcome_row is not None, "Welcome queue row should exist after refresh")
        down_button = _find_row_control(welcome_row, "Down")
        _assert(str(down_button.cget("state")) == "normal", f"Welcome Down button should be enabled, got {down_button.cget('state')}")
        down_button.invoke()
        _pump_until(root, lambda: _queue_ids() == ["song-1", "welcome-1", "bible-1"])
        _pump_until(
            root,
            lambda: (_find_queue_row_frame(root, "Welcome") is not None and str(_find_row_control(_find_queue_row_frame(root, "Welcome"), "Up").cget("state")) == "normal"),
        )

        welcome_row = _find_queue_row_frame(root, "Welcome")
        _assert(welcome_row is not None, "Welcome queue row should still exist after moving down")
        up_button = _find_row_control(welcome_row, "Up")
        _assert(str(up_button.cget("state")) == "normal", f"Welcome Up button should be enabled after moving down, got {up_button.cget('state')}")
        up_button.invoke()
        _pump_until(root, lambda: _queue_ids() == ["welcome-1", "song-1", "bible-1"])

        save_button = _find_button_by_text(root, "Save")
        _assert(str(save_button.cget("state")) == "normal", f"Save button should be enabled, got {save_button.cget('state')}")
        message_state["info"].clear()
        message_state["error"].clear()
        save_button.invoke()
        _pump_until(root, lambda: int(save_call_state.get("count") or 0) >= 1 or bool(message_state["info"]) or bool(message_state["error"]))
        _assert(not message_state["error"], f"Unexpected error dialog during save: {message_state['error']}\n{_read_text_if_exists(temp_root / 'ui_errors.log')}")
        _pump_until(root, lambda: len(display_bridge.list_saved_service_entries()) == 1)

        replacement_queue = [_build_queue_item("song-2", "Song Two", active=True)]
        display_bridge.save_display_queue_state({"queue_items": replacement_queue}, record_snapshot=False)
        _find_button_by_text(root, "Refresh").invoke()
        _pump_until(root, lambda: _queue_ids() == ["song-2"])
        _assert(str(save_button.cget("state")) == "normal", f"Save button should stay enabled, got {save_button.cget('state')}")
        message_state["info"].clear()
        message_state["error"].clear()
        save_button.invoke()
        _pump_until(root, lambda: int(save_call_state.get("count") or 0) >= 2 or len(message_state["info"]) > 0 or len(message_state["error"]) > 0)
        _assert(not message_state["error"], f"Unexpected error dialog during second save: {message_state['error']}\n{_read_text_if_exists(temp_root / 'ui_errors.log')}")
        _pump_until(root, lambda: len(display_bridge.list_saved_service_entries()) == 1)

        selected_date_value = date.today().isoformat()
        morning_queue = [
            _build_saved_welcome_queue_item("welcome-morning", "Morning Welcome", "Morning Birthday"),
            _build_saved_preparation_queue_item("prep-morning", "Morning Header", ["Morning Block One", "Morning Block Two"]),
        ]
        night_queue = [
            _build_saved_welcome_queue_item("welcome-night", "Night Welcome", "Night Birthday"),
            _build_saved_preparation_queue_item("prep-night", "Night Header", ["Night Block One", "Night Block Two"]),
        ]
        display_bridge.save_service_entry(selected_date_value, "Morning", "Morning Preacher", morning_queue)
        display_bridge.save_service_entry(selected_date_value, "Night", "Night Preacher", night_queue)
        display_bridge.save_display_queue_state({"queue_items": [_build_queue_item("song-3", "Song Three", active=True)]}, record_snapshot=False)
        _find_button_by_text(root, "Refresh").invoke()
        _pump_until(root, lambda: _queue_ids() == ["song-3"])

        toggle_queue = [
            _build_queue_item("song-toggle-1", "Song Toggle One", active=True),
            _build_queue_item("song-toggle-2", "Song Toggle Two", active=False),
        ]
        display_bridge.save_display_queue_state({"queue_items": toggle_queue}, record_snapshot=False)
        _find_button_by_text(root, "Refresh").invoke()
        _pump_until(root, lambda: _queue_ids() == ["song-toggle-1", "song-toggle-2"])
        _pump_until(root, lambda: _find_queue_row_frame(root, "Song Toggle One") is not None and _find_queue_row_frame(root, "Song Toggle Two") is not None)
        row_one = _find_queue_row_frame(root, "Song Toggle One")
        row_two = _find_queue_row_frame(root, "Song Toggle Two")
        _assert(row_one is not None and row_two is not None, "Toggle test rows should exist")
        row_two_active = _find_row_checkbutton(row_two, "Active")
        row_one_active = _find_row_checkbutton(row_one, "Active")
        row_two_active.invoke()
        _pump_until(
            root,
            lambda: [str(item.get("id") or "") for item in display_bridge.load_display_queue_state().get("queue_items") or [] if bool(item.get("active"))] == ["song-toggle-2"],
        )
        row_one = _find_queue_row_frame(root, "Song Toggle One")
        _assert(row_one is not None, "Song Toggle One row should still exist after activation switch")
        row_one_active = _find_row_checkbutton(row_one, "Active")
        row_one_active.invoke()
        _pump_until(
            root,
            lambda: [str(item.get("id") or "") for item in display_bridge.load_display_queue_state().get("queue_items") or [] if bool(item.get("active"))] == ["song-toggle-1"],
        )

        time_menu_button = _find_time_option_menu(root)
        time_menu = root.nametowidget(str(time_menu_button.cget("menu")))
        night_index = next(index for index in range(int(time_menu.index("end") or -1) + 1) if str(time_menu.entrycget(index, "label")) == "Night")
        morning_index = next(index for index in range(int(time_menu.index("end") or -1) + 1) if str(time_menu.entrycget(index, "label")) == "Morning")
        other_index = next(index for index in range(int(time_menu.index("end") or -1) + 1) if str(time_menu.entrycget(index, "label")) == "Other")

        time_menu.invoke(night_index)
        _pump_until(root, lambda: _queue_ids() == ["welcome-night", "prep-night"])
        _assert(not any(bool(item.get("active")) for item in display_bridge.load_display_queue_state().get("queue_items") or []), "Night recall should restore all queue items inactive")
        _pump_until(root, lambda: preacher_entry.get().strip() == "Night Preacher")
        _pump_until(root, lambda: welcome_text_widget.get("1.0", tk.END).strip() == "Night Welcome")
        _pump_until(root, lambda: birthday_text_widget.get("1.0", tk.END).strip() == "Night Birthday")
        _pump_until(root, lambda: preparation_header_entry.get().strip() == "Night Header")
        _pump_until(root, lambda: preparation_text_widget.get("1.0", tk.END).strip() == "Night Block One\n\nNight Block Two")

        time_menu.invoke(morning_index)
        _pump_until(root, lambda: _queue_ids() == ["welcome-morning", "prep-morning"])
        _assert(not any(bool(item.get("active")) for item in display_bridge.load_display_queue_state().get("queue_items") or []), "Morning recall should restore all queue items inactive")
        try:
            _pump_until(root, lambda: preacher_entry.get().strip() == "Morning Preacher", timeout_seconds=5.0)
        except AssertionError as error:
            raise AssertionError(
                "Morning recall did not update the preacher field. "
                f"Observed preacher='{preacher_entry.get().strip()}', "
                f"welcome='{welcome_text_widget.get('1.0', tk.END).strip()}', "
                f"birthday='{birthday_text_widget.get('1.0', tk.END).strip()}', "
                f"dialogs={message_state}"
            ) from error
        _pump_until(root, lambda: welcome_text_widget.get("1.0", tk.END).strip() == "Morning Welcome")
        _pump_until(root, lambda: birthday_text_widget.get("1.0", tk.END).strip() == "Morning Birthday")
        _pump_until(root, lambda: preparation_header_entry.get().strip() == "Morning Header")
        _pump_until(root, lambda: preparation_text_widget.get("1.0", tk.END).strip() == "Morning Block One\n\nMorning Block Two")

        time_menu.invoke(other_index)
        _pump_until(root, lambda: _queue_ids() == [])
        _pump_until(root, lambda: preacher_entry.get().strip() == "")
        _pump_until(root, lambda: welcome_text_widget.get("1.0", tk.END).strip() == "")
        _pump_until(root, lambda: birthday_text_widget.get("1.0", tk.END).strip() == "")
        _pump_until(root, lambda: preparation_header_entry.get().strip() == "")
        _pump_until(root, lambda: preparation_text_widget.get("1.0", tk.END).strip() == "")

        import overlay_layout
        from PIL import Image

        # Test Settings popup -> Secondary Screen tab selectors (text color, key size)
        menu_bar = root.nametowidget(str(root.cget("menu")))
        menu_labels = [
            str(menu_bar.entrycget(i, "label"))
            for i in range(int(menu_bar.index("end") or -1) + 1)
            if menu_bar.type(i) != "tearoff"
        ]
        _assert("Licenses" in menu_labels, f"Music Licenses menu entry missing: {menu_labels}")
        _assert("Settings" in menu_labels, f"Settings menu entry missing: {menu_labels}")
        data_menu_index = next(
            i for i in range(int(menu_bar.index("end") or -1) + 1)
            if menu_bar.type(i) != "tearoff" and str(menu_bar.entrycget(i, "label")) == "Data"
        )
        data_menu = root.nametowidget(str(menu_bar.entrycget(data_menu_index, "menu")))
        _assert(str(data_menu.entrycget(0, "label")) == "Open Background Folder", "Background folder shortcut missing")
        settings_menu_index = next(
            i for i in range(int(menu_bar.index("end") or -1) + 1)
            if menu_bar.type(i) != "tearoff" and str(menu_bar.entrycget(i, "label")) == "Settings"
        )
        existing_toplevels = {str(w) for w in root.winfo_children() if isinstance(w, tk.Toplevel)}
        menu_bar.invoke(settings_menu_index)
        _pump_until(root, lambda: any(
            isinstance(w, tk.Toplevel) and str(w) not in existing_toplevels for w in root.winfo_children()))
        settings_window = next(
            w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and str(w) not in existing_toplevels)
        settings_notebook = next(w for w in settings_window.winfo_children() if isinstance(w, ttk.Notebook))
        tab_titles = [str(settings_notebook.tab(tab_id, "text")) for tab_id in settings_notebook.tabs()]
        _assert(tab_titles == ["Main Screen", "Secondary Screen"], f"Unexpected Settings tabs: {tab_titles}")
        main_screen_tab = root.nametowidget(settings_notebook.tabs()[0])
        main_boxes = [w for w in main_screen_tab.winfo_children() if isinstance(w, ttk.Combobox)]
        _assert(len(main_boxes) == 4, f"Expected 4 Main Screen selectors, got {len(main_boxes)}")
        secondary_tab = root.nametowidget(settings_notebook.tabs()[1])
        secondary_boxes = [w for w in secondary_tab.winfo_children() if isinstance(w, ttk.Combobox)]
        _assert(len(secondary_boxes) == 5, f"Expected 5 Secondary Screen selectors, got {len(secondary_boxes)}")
        text_color_box = secondary_boxes[3]
        key_size_box = secondary_boxes[4]
        color_labels = list(text_color_box["values"])
        _assert(color_labels == ["Auto", "White", "Black"], f"Expected ['Auto', 'White', 'Black'], got {color_labels}")
        key_size_labels = list(key_size_box["values"])
        expected_key_sizes = ["None"] + [f"{size} px" for size in range(2, 33, 2)]
        _assert(key_size_labels == expected_key_sizes, f"Expected {expected_key_sizes}, got {key_size_labels}")
        key_size_box.set("20 px")
        key_size_box.event_generate("<<ComboboxSelected>>")
        _pump_until(root, lambda: overlay_layout.DISPLAY_KEY_SIZE == 20)
        _assert(main.load_ui_settings().get("display_key_size") == 20, "Selected Key Size was not saved")

        # Test hiding the key entirely, then restoring a visible size
        key_size_box.set("None")
        key_size_box.event_generate("<<ComboboxSelected>>")
        _pump_until(root, lambda: overlay_layout.DISPLAY_KEY_SIZE == 0)
        _assert(overlay_layout.is_display_key_hidden(), "Key size None did not hide the key")
        _assert(overlay_layout.get_display_key_size(1920, 1080) >= 2, "Hidden key must keep a readable attribution size")
        _assert(main.load_ui_settings().get("display_key_size") == 0, "Key size None was not saved")
        key_size_box.set("20 px")
        key_size_box.event_generate("<<ComboboxSelected>>")
        _pump_until(root, lambda: overlay_layout.DISPLAY_KEY_SIZE == 20)
        _assert(not overlay_layout.is_display_key_hidden(), "Key did not become visible again")

        # Test selecting White and Black
        text_color_box.set("White")
        text_color_box.event_generate("<<ComboboxSelected>>")
        _pump_until(root, lambda: overlay_layout.get_display_text_color() == "White")

        text_color_box.set("Black")
        text_color_box.event_generate("<<ComboboxSelected>>")
        _pump_until(root, lambda: overlay_layout.get_display_text_color() == "Black")

        text_color_box.set("Auto")
        text_color_box.event_generate("<<ComboboxSelected>>")
        _pump_until(root, lambda: overlay_layout.get_display_text_color() == "Auto")

        # Test dark theme and GUI font family/style/size propagation across Entry, Text, Listbox, and Treeview
        theme_box, family_box, style_box, size_box = main_boxes
        theme_box.set("Midnight Amber")
        theme_box.event_generate("<<ComboboxSelected>>")
        style_box.set("Italic")
        style_box.event_generate("<<ComboboxSelected>>")
        size_box.set("16")
        size_box.event_generate("<<ComboboxSelected>>")
        root.update()
        dark_palette = main.THEMES["Midnight Amber"]
        _assert(
            str(preacher_entry.cget("bg")).lower() == dark_palette["panel_bg"].lower(),
            f"Dark theme Entry bg should match panel_bg {dark_palette['panel_bg']}, got {preacher_entry.cget('bg')}",
        )
        _assert(
            str(preacher_entry.cget("fg")).lower() == dark_palette["text"].lower(),
            f"Dark theme Entry fg should match text {dark_palette['text']}, got {preacher_entry.cget('fg')}",
        )
        _assert(
            str(welcome_text_widget.cget("bg")).lower() == dark_palette["panel_bg"].lower(),
            f"Dark theme Text bg should match panel_bg {dark_palette['panel_bg']}, got {welcome_text_widget.cget('bg')}",
        )
        entry_font = main.tkfont.Font(font=preacher_entry.cget("font"))
        _assert(int(entry_font.cget("size")) == 16, f"Expected input font size 16, got {entry_font.cget('size')}")
        _assert(str(entry_font.cget("slant")) == "italic", f"Expected italic input font, got {entry_font.cget('slant')}")
        ttk_style = ttk.Style(notebook)
        _assert(
            str(ttk_style.lookup("Treeview", "background")).lower() == dark_palette["panel_bg"].lower(),
            f"Treeview background should match dark theme {dark_palette['panel_bg']}, got {ttk_style.lookup('Treeview', 'background')}",
        )

        # Test History popup styling under dark theme
        history_menu_index = next(
            i for i in range(int(menu_bar.index("end") or -1) + 1)
            if menu_bar.type(i) != "tearoff" and str(menu_bar.entrycget(i, "label")) == "History"
        )
        history_menu_widget = root.nametowidget(str(menu_bar.entrycget(history_menu_index, "menu")))
        before_history_wins = {str(w) for w in root.winfo_children() if isinstance(w, tk.Toplevel)}
        history_menu_widget.invoke(0)
        _pump_until(root, lambda: any(isinstance(w, tk.Toplevel) and str(w) not in before_history_wins for w in root.winfo_children()))
        history_win = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and str(w) not in before_history_wins)
        _assert(str(history_win.cget("bg")).lower() == dark_palette["panel_bg"].lower(), "History popup bg did not match dark theme")
        history_win.destroy()

        # Test Bible tab scrollable multi-select popup with pinned Done button
        bible_tab_index = next(
            idx for idx, tab_id in enumerate(notebook.tabs())
            if str(notebook.tab(tab_id, "text")) == "Bible"
        )
        notebook.select(bible_tab_index)
        _pump_until(root, lambda: notebook.index(notebook.select()) == bible_tab_index)
        bible_tab_frame = root.nametowidget(notebook.tabs()[bible_tab_index])
        select_bibles_btn = next(
            w for w in _iter_widgets(bible_tab_frame)
            if isinstance(w, tk.Button) and ("Select Bibles" in str(w.cget("text")) or "selected" in str(w.cget("text")))
        )
        before_bible_wins = {str(w) for w in root.winfo_children() if isinstance(w, tk.Toplevel)}
        select_bibles_btn.invoke()
        _pump_until(root, lambda: any(isinstance(w, tk.Toplevel) and str(w) not in before_bible_wins for w in root.winfo_children()))
        bible_popup = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and str(w) not in before_bible_wins)
        _assert(str(bible_popup.cget("bg")).lower() == dark_palette["panel_bg"].lower(), "Bible selector popup bg did not match dark theme")
        done_button = next(
            (w for w in _iter_widgets(bible_popup) if isinstance(w, tk.Button) and str(w.cget("text")) == "Done"),
            None,
        )
        _assert(done_button is not None, "Bible selector popup must have a Done button")
        done_button.invoke()
        _pump_until(root, lambda: not bible_popup.winfo_exists())

        # Test right-click context menu on Entry/Text widgets (Cut, Copy, Paste, Select All, Clear)
        from types import SimpleNamespace
        from unittest.mock import patch as mock_patch
        from spell_check import SpellCheckController
        test_spell = SpellCheckController(root)
        try:
            with mock_patch.object(tk.Menu, "tk_popup"):
                preacher_entry.delete(0, "end")
                preacher_entry.insert(0, "Pastor John")
                root.update()
                test_spell._popup(SimpleNamespace(widget=preacher_entry, x=5, y=5, x_root=20, y_root=20))
                _assert(test_spell.menu is not None, "Right-click context menu was not created")
                menu_items = [
                    str(test_spell.menu.entrycget(i, "label"))
                    for i in range(int(test_spell.menu.index("end") or -1) + 1)
                    if test_spell.menu.type(i) == "command"
                ]
                for expected_action in ("Cut", "Copy", "Paste", "Delete", "Select All", "Clear"):
                    _assert(expected_action in menu_items, f"Missing {expected_action} in right-click menu: {menu_items}")
        finally:
            test_spell.close()

        # Restore Light theme and default font settings
        theme_box.set("Light")
        theme_box.event_generate("<<ComboboxSelected>>")
        style_box.set("Bold")
        style_box.event_generate("<<ComboboxSelected>>")
        size_box.set("12")
        size_box.event_generate("<<ComboboxSelected>>")
        settings_window.destroy()

        # Test animated GIF stability: frames do not flicker between black and white
        test_gif_path = temp_root / "test_loop.gif"
        frame_bright = Image.new("RGB", (80, 80), (255, 255, 255))
        frame_dim = Image.new("RGB", (80, 80), (60, 60, 60))
        frame_bright.save(test_gif_path, save_all=True, append_images=[frame_dim], loop=0)
        setattr(frame_bright, "_background_path", str(test_gif_path))
        setattr(frame_dim, "_background_path", str(test_gif_path))

        c_bright = overlay_layout.choose_text_colors(frame_bright)
        c_dim = overlay_layout.choose_text_colors(frame_dim)
        _assert(c_bright == c_dim, f"Animated frames must not flash colors: {c_bright} != {c_dim}")

        root.destroy()
    finally:
        main.messagebox.showinfo = original_showinfo
        main.messagebox.showwarning = original_showwarning
        main.messagebox.showerror = original_showerror
        main.save_service_entry = original_save_service_entry
        main.get_ui_settings_path = original_ui_settings_path
        display_bridge.DISPLAY_QUEUE_BRIDGE_RELATIVE_PATH = original_bridge_relative_path
        display_bridge.SERVICE_DB_RELATIVE_PATH = original_service_relative_path
        perf_logging.TIMING_LOG_RELATIVE_PATH = original_timing_relative_path
        perf_logging.ERROR_LOG_RELATIVE_PATH = original_error_relative_path


def main_cli() -> None:
    """What: Run the UI smoke workflow in disposable storage and clean up afterward.

    Why included: Direct execution should leave application data untouched even if
    the test creates Tk resources or filesystem logs.
    Where used: Module entry point when this script is launched directly.
    Returns: None; prints success after completion and propagates test failures.
    """
    temp_root = Path(tempfile.mkdtemp(prefix="the_light_ui_smoke_"))
    try:
        run_ui_smoke_test(temp_root)
    finally:
        gc.collect()
        for _attempt in range(5):
            try:
                shutil.rmtree(temp_root)
                break
            except PermissionError:
                time.sleep(0.1)
    print("UI reliability smoke test passed.")


if __name__ == "__main__":
    main_cli()
