"""Read and compare Bible passages from the app's normalized local caches."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from background_picker import open_background_picker
from display_bridge import append_display_queue_item
from overlay_layout import paginate_bible_columns
from tab_translation import build_source_book_catalog, load_book_chapters
from app_paths import get_app_base_path as default_get_app_base_path, get_app_data_path, resolve_background_file


def discover_cached_bible_sources(cache_root: Path) -> list[dict[str, object]]:
    """Discover reader sources that have at least one normalized cached book.

    What: Scans cache subdirectories, reads a sample book JSON, and builds source records
    with ID, display name, cache directory, and language code.
    Why included: The comparison reader consumes normalized caches rather than parsing
    every original Bible format itself.
    Where used: refresh_sources() when populating the Bible selector.
    Returns: Sources sorted by case-insensitive display name; unusable folders are skipped.
    """
    sources: list[dict[str, object]] = []
    if not cache_root.exists():
        return sources
    used_names: set[str] = set()
    for cache_directory in sorted((path for path in cache_root.iterdir() if path.is_dir()), key=lambda item: item.name.casefold()):
        sample_files = sorted(cache_directory.glob("book_*.json"), key=lambda item: item.name.casefold())
        if not sample_files:
            continue
        try:
            payload = json.loads(sample_files[0].read_text(encoding="utf-8"))
        except Exception:
            continue
        source_name = str(payload.get("source_name") or cache_directory.name).strip() or cache_directory.name
        display_name = source_name
        if display_name.casefold() in used_names:
            display_name = f"{source_name} ({cache_directory.name})"
        used_names.add(display_name.casefold())
        sources.append(
            {
                "id": cache_directory.name,
                "display_name": display_name,
                "cache_directory": cache_directory,
                "language_code": str(payload.get("language_code") or ""),
            }
        )
    return sorted(sources, key=lambda item: str(item.get("display_name") or "").casefold())


def _bind_mousewheel_cycle(combo: ttk.Combobox, values: list[str]) -> None:
    """What: Bind mouse-wheel input to cycle through the combobox's current values.

    Why included: Book/chapter/verse selectors should be navigable without opening the menu.
    Where used: Reader tab setup for the four readonly selection comboboxes.
    Returns: None; registers Windows and Linux wheel event bindings.
    """
    def _handle(_event) -> str:
        """What: Move to the adjacent combobox choice and emit selection notification.

        Why included: Wheel navigation must trigger the same update path as a menu choice.
        Where used: Mouse-wheel bindings installed by _bind_mousewheel_cycle().
        Returns: "break" to consume the event, including when values are empty.
        """
        current_choices = list(combo["values"])
        if not current_choices:
            return "break"
        current_value = str(combo.get()).strip()
        if not current_value:
            combo.set(current_choices[0])
            return "break"
        try:
            current_index = current_choices.index(current_value)
        except ValueError:
            combo.set(current_choices[0])
            return "break"
        delta = 0
        if getattr(_event, "delta", 0):
            delta = -1 if _event.delta > 0 else 1
        elif getattr(_event, "num", None) == 4:
            delta = -1
        elif getattr(_event, "num", None) == 5:
            delta = 1
        if not delta:
            return "break"
        new_index = (current_index + delta) % len(current_choices)
        combo.set(current_choices[new_index])
        combo.event_generate("<<ComboboxSelected>>")
        return "break"

    combo.bind("<MouseWheel>", _handle, add="+")
    combo.bind("<Button-4>", _handle, add="+")
    combo.bind("<Button-5>", _handle, add="+")


def build_bible_reader_tab(tab_frame: tk.Frame, context: dict[str, object]) -> None:
    """Build the local Bible comparison reader and its Display handoff controls.

    What: Loads cached Bible sources, builds multi-source book/chapter/verse selectors,
    renders a synchronized comparison grid, and queues selected passages for Display.
    Why included: Bible comparison reuses normalized chapter caches while keeping source
    discovery and parsing shared with the Translation tab.
    Where used: main.create_tabs_content() for the Bible reader notebook page.
    Returns: None; registers widgets and persists reader selections in Bible settings.
    """
    apply_layout_style = context["apply_layout_style"]

    def style_widgets(widgets, label_widgets=(), input_widgets=()) -> None:
        """What: Register dynamically created reader widgets and apply shared styling.

        Why included: Rebuilt comparison cells must respond to app-wide theme/font changes.
        Where used: Controls, comparison-grid cells, and placeholders.
        Returns: None; adds widgets to context registries and styles them.
        """
        all_widgets = list(dict.fromkeys([*widgets, *label_widgets, *input_widgets]))
        for key, group in (("tab_widgets", all_widgets), ("tab_labels", label_widgets), ("tab_inputs", input_widgets)):
            registry = context.get(key)
            if isinstance(registry, list):
                for widget in group:
                    if widget not in registry:
                        registry.append(widget)
                        widget.bind("<Destroy>", lambda event, current=widget, items=registry: items.remove(current) if event.widget is current and current in items else None, add="+")
        apply_layout_style(all_widgets, label_widgets=label_widgets, input_widgets=input_widgets)

    get_app_base_path = context.get("get_app_base_path")
    app_base_path = Path(get_app_base_path()) if callable(get_app_base_path) else default_get_app_base_path()
    settings_path = get_app_data_path(app_base_path) / "bible_settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}

    tab_frame.columnconfigure(0, weight=1)
    tab_frame.columnconfigure(1, weight=0)
    tab_frame.rowconfigure(0, weight=0)
    tab_frame.rowconfigure(1, weight=1)

    actions = tk.Frame(tab_frame)
    actions.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
    actions.columnconfigure(2, weight=1)

    left_frame = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
    left_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=12)
    left_frame.columnconfigure(0, weight=1)
    left_frame.rowconfigure(0, weight=0)
    left_frame.rowconfigure(1, weight=1)

    title_label = tk.Label(left_frame, text="Bible Comparison", justify="center", anchor="center")
    title_label.grid(row=0, column=0, sticky="ew", pady=(0, 10))

    controls_frame = tk.Frame(left_frame)
    controls_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
    controls_frame.columnconfigure(0, weight=1)
    controls_frame.columnconfigure(1, weight=1)
    controls_frame.columnconfigure(2, weight=1)
    controls_frame.columnconfigure(3, weight=1)
    controls_frame.columnconfigure(4, weight=1)
    controls_frame.columnconfigure(5, weight=1)

    bible_selection_label = tk.Label(controls_frame, text="Bibles", anchor="w", justify="left")
    bible_selection_label.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(0, 6), pady=(0, 4))
    book_label = tk.Label(controls_frame, text="Book", anchor="w", justify="left")
    book_label.grid(row=0, column=2, sticky="ew", padx=6, pady=(0, 4))
    chapter_label = tk.Label(controls_frame, text="Chapter", anchor="w", justify="left")
    chapter_label.grid(row=0, column=3, sticky="ew", padx=6, pady=(0, 4))
    start_verse_label = tk.Label(controls_frame, text="Start", anchor="w", justify="left")
    start_verse_label.grid(row=0, column=4, sticky="ew", padx=6, pady=(0, 4))
    end_verse_label = tk.Label(controls_frame, text="End", anchor="w", justify="left")
    end_verse_label.grid(row=0, column=5, sticky="ew", padx=6, pady=(0, 4))

    book_var = tk.StringVar(value=str(settings.get("book") or ""))
    chapter_var = tk.StringVar(value=str(settings.get("chapter") or ""))
    start_verse_var = tk.StringVar(value=str(settings.get("start_verse") or ""))
    end_verse_var = tk.StringVar(value=str(settings.get("end_verse") or ""))

    saved_bibles = settings.get("selected_bibles")
    selected_bibles: list[str] = [name for name in saved_bibles if isinstance(name, str)] if isinstance(saved_bibles, list) else []
    bible_check_vars: dict[str, tk.BooleanVar] = {}
    bible_menu_button = tk.Menubutton(controls_frame, text="Select Bibles", relief="raised", bd=2)
    bible_menu_button.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 6))
    bible_selection_menu = tk.Menu(bible_menu_button, tearoff=False)
    bible_menu_button.configure(menu=bible_selection_menu)
    book_combo = ttk.Combobox(controls_frame, textvariable=book_var, state="readonly", style="Content.TCombobox")
    book_combo.grid(row=1, column=2, sticky="ew", padx=6)
    chapter_combo = ttk.Combobox(controls_frame, textvariable=chapter_var, state="readonly", style="Content.TCombobox")
    chapter_combo.grid(row=1, column=3, sticky="ew", padx=6)
    start_verse_combo = ttk.Combobox(controls_frame, textvariable=start_verse_var, state="readonly", width=8, style="Content.TCombobox")
    start_verse_combo.grid(row=1, column=4, sticky="ew", padx=6)
    end_verse_combo = ttk.Combobox(controls_frame, textvariable=end_verse_var, state="readonly", width=8, style="Content.TCombobox")
    end_verse_combo.grid(row=1, column=5, sticky="ew", padx=6)

    comparison_frame = tk.Frame(left_frame, bd=1, relief="solid", padx=8, pady=8)
    comparison_frame.grid(row=1, column=0, sticky="nsew")
    comparison_frame.columnconfigure(0, weight=1)
    comparison_frame.columnconfigure(1, weight=1)
    comparison_frame.rowconfigure(0, weight=1)

    state: dict[str, object] = {
        "sources": [],
        "source_by_display": {},
        "book_catalog_cache": {},
        "background_path": str(settings.get("background_path") or ""),
        "restoring_settings": True,
    }

    def save_bible_settings() -> bool:
        """What: Persist selected sources, passage range, and background using temp-file replace.

        Why included: Reader selections should survive application restarts without partial JSON.
        Where used: Selection changes and background updates.
        Returns: True when unchanged or saved; False after a reported write error.
        """
        updated_settings = dict(settings, selected_bibles=get_selected_bible_names(), book=book_var.get(), chapter=chapter_var.get(), start_verse=start_verse_var.get(), end_verse=end_verse_var.get(), background_path=str(state.get("background_path") or ""))
        if updated_settings == settings:
            return True
        temporary_path = settings_path.with_suffix(".json.tmp")
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(json.dumps(updated_settings, indent=2), encoding="utf-8")
            temporary_path.replace(settings_path)
        except OSError as error:
            messagebox.showerror("Bible Settings", f"Unable to remember the Bible selection.\n\n{error}", parent=tab_frame.winfo_toplevel())
            return False
        settings.update(updated_settings)
        return True

    def get_selected_bible_names() -> list[str]:
        """What: Return unique nonempty Bible display names in selection order.

        Why included: Menu variables and saved settings can contain repeated values.
        Where used: Source selection, comparison grid, and Display payload creation.
        Returns: Ordered list of selected source names.
        """
        names: list[str] = []
        for name in selected_bibles:
            if name and name not in names:
                names.append(name)
        return names

    def update_selection_button_text() -> None:
        """What: Summarize the current Bible selection on the menu button.

        Why included: The closed selector should communicate selection count/names.
        Where used: Select/clear-all menu actions and source-list refresh.
        Returns: None; updates the Menubutton label.
        """
        names = get_selected_bible_names()
        if not names:
            bible_menu_button.configure(text="Select Bibles")
            return
        if len(names) <= 2:
            bible_menu_button.configure(text=f"{len(names)} selected: {', '.join(names)}")
            return
        bible_menu_button.configure(text=f"{len(names)} Bibles selected")

    def select_all_bibles() -> None:
        """What: Select every discovered source and refresh dependent reader controls.

        Why included: Comparing all available translations should be one menu action.
        Where used: Select all Bibles command in the source menu.
        Returns: None; rebuilds menu, book options, and comparison grid.
        """
        source_names = list((state.get("source_by_display") or {}).keys())
        selected_bibles.clear()
        selected_bibles.extend(source_names)
        for source_name in source_names:
            bible_check_vars.setdefault(source_name, tk.BooleanVar()).set(True)
        update_selection_button_text()
        rebuild_bible_selection_menu()
        update_book_options()
        update_comparison_grid()

    def clear_bible_selection() -> None:
        """What: Clear all Bible selections and reset the dependent comparison view.

        Why included: Users need a quick way to remove all comparison columns.
        Where used: Clear selection command in the source menu.
        Returns: None; updates source checks, book options, and grid.
        """
        selected_bibles.clear()
        for variable in bible_check_vars.values():
            variable.set(False)
        update_selection_button_text()
        rebuild_bible_selection_menu()
        update_book_options()
        update_comparison_grid()

    def rebuild_bible_selection_menu() -> None:
        """What: Rebuild source checkbuttons and select-all/clear commands from current sources.

        Why included: Discovered sources and selections can change as cache files are added.
        Where used: Source refresh and selection updates.
        Returns: None; shows a disabled empty-state command when no Bibles are available.
        """
        bible_selection_menu.delete(0, "end")
        selected_names = get_selected_bible_names()
        source_names = list((state.get("source_by_display") or {}).keys())
        if not source_names:
            bible_selection_menu.add_command(label="No Bibles found", state="disabled")
            return
        bible_selection_menu.add_command(label="Select all Bibles", command=select_all_bibles)
        bible_selection_menu.add_command(label="Clear selection", command=clear_bible_selection)
        bible_selection_menu.add_separator()
        for source_name in source_names:
            variable = bible_check_vars.setdefault(source_name, tk.BooleanVar(value=source_name in selected_names))
            variable.set(source_name in selected_names)
            bible_selection_menu.add_checkbutton(
                label=source_name,
                variable=variable,
                command=lambda selected_name=source_name: on_bible_selection_changed(selected_name),
            )

    def on_bible_selection_changed(changed_name: str) -> None:
        """What: Reconcile checked menu variables into selected_bibles and refresh the reader.

        Why included: Menu variables are widget state; selected_bibles is the ordered model.
        Where used: Each source checkbutton command.
        Returns: None; updates button text, book choices, and comparison columns.
        """
        source_names = list((state.get("source_by_display") or {}).keys())
        checked_names = [name for name in source_names if bible_check_vars.get(name) and bible_check_vars[name].get()]
        selected_bibles.clear()
        selected_bibles.extend(checked_names)
        selected_names = get_selected_bible_names()
        for source_name, variable in bible_check_vars.items():
            variable.set(source_name in selected_names)
        update_selection_button_text()
        rebuild_bible_selection_menu()
        update_book_options()
        update_comparison_grid()

    def get_source_lookup() -> dict[str, dict[str, object]]:
        """What: Return the cache of Bible sources indexed by display name.

        Why included: Selection code needs one lookup without repeatedly scanning all sources.
        Where used: Source resolution and comparison rendering helpers.
        Returns: Source-name to source-record mapping.
        """
        return state.get("source_by_display") or {}

    def get_source_for_name(name: str) -> dict[str, object] | None:
        """What: Resolve a display name to its source mapping when present.

        Why included: Saved selection names may refer to sources removed from the cache.
        Where used: Book catalog and verse lookup workflows.
        Returns: Source mapping, or None when the name is unavailable.
        """
        source = get_source_lookup().get(name.strip())
        return source if isinstance(source, dict) else None

    def get_book_catalog(source: dict[str, object]) -> list[dict[str, object]]:
        """What: Load and cache the normalized book catalog for one source ID.

        Why included: Catalog creation may read many cached book files and should be reused.
        Where used: Book selector population and selected-book lookup.
        Returns: Ordered book records for the source.
        """
        source_id = str(source.get("id") or "")
        cached_catalog = (state.get("book_catalog_cache") or {}).get(source_id)
        if isinstance(cached_catalog, list):
            return cached_catalog
        catalog = build_source_book_catalog(source)
        state.setdefault("book_catalog_cache", {})[source_id] = catalog
        return catalog

    def get_selected_book_entry(source: dict[str, object] | None) -> dict[str, object] | None:
        """What: Match the selected book number to a source catalog entry.

        Why included: Chapter/verse controls need the selected book's path and counts.
        Where used: Option updates, comparison rendering, and Display handoff.
        Returns: Matching book record, or None when source/selection is invalid.
        """
        if not isinstance(source, dict):
            return None
        selected_book_value = book_var.get().strip()
        selected_number = selected_book_value.partition(" ")[0]
        for entry in get_book_catalog(source):
            if not isinstance(entry, dict):
                continue
            if selected_number.isdigit() and int(entry.get("number") or 0) == int(selected_number):
                return entry
        return None

    def get_verse_text(source: dict[str, object] | None, book_entry: dict[str, object] | None, chapter_number: int, verse_number: int) -> str:
        """What: Load cached chapter data and return one verse's text.

        Why included: Comparison cells and queued selections need source-specific verse content.
        Where used: update_comparison_grid() and send_selection_to_display().
        Returns: Trimmed verse text, or an empty string when source/book data is unavailable.
        """
        if not isinstance(source, dict) or not isinstance(book_entry, dict):
            return ""
        book_path = book_entry.get("book_path")
        if not isinstance(book_path, Path):
            return ""
        chapters = load_book_chapters(book_path)
        return str((chapters.get(chapter_number) or {}).get(verse_number) or "").strip()

    def get_primary_selected_source() -> dict[str, object] | None:
        """What: Resolve a selected Bible containing the chosen book, or the first available source.

        Why included: A source containing the selected book must supply valid chapter/verse controls.
        Where used: update_book_options() and update_verse_range_controls().
        Returns: First source containing the book, first available source as fallback, or None.
        """
        fallback_source = None
        for source_name in get_selected_bible_names():
            source = get_source_for_name(source_name)
            if isinstance(source, dict):
                if fallback_source is None:
                    fallback_source = source
                if get_selected_book_entry(source) is not None:
                    return source
        return fallback_source

    def update_verse_range_controls() -> None:
        """What: Populate chapter and verse options and clamp the chosen range to valid values.

        Why included: A new book/chapter can invalidate previously saved verse bounds.
        Where used: Book/chapter changes and source-selection updates.
        Returns: None; refreshes the comparison grid after normalizing controls.
        """
        source = get_primary_selected_source()
        book_entry = get_selected_book_entry(source)
        if not isinstance(book_entry, dict):
            chapter_combo.configure(values=[])
            chapter_var.set("")
            start_verse_combo.configure(values=[])
            end_verse_combo.configure(values=[])
            start_verse_var.set("")
            end_verse_var.set("")
            update_comparison_grid()
            return
        chapters = load_book_chapters(book_entry["book_path"])
        chapter_values = [str(number) for number in sorted(chapters)]
        chapter_combo.configure(values=chapter_values)
        if not chapter_values:
            chapter_var.set("")
            start_verse_combo.configure(values=[])
            end_verse_combo.configure(values=[])
            start_verse_var.set("")
            end_verse_var.set("")
            update_comparison_grid()
            return
        current_chapter = chapter_var.get().strip()
        if current_chapter not in chapter_values:
            chapter_var.set(chapter_values[0])
            current_chapter = chapter_values[0]
        verse_values = [str(number) for number in sorted(chapters[int(current_chapter)])]
        start_verse_combo.configure(values=verse_values)
        end_verse_combo.configure(values=verse_values)
        if not verse_values:
            start_verse_var.set("")
            end_verse_var.set("")
            update_comparison_grid()
            return
        start_value = start_verse_var.get().strip()
        end_value = end_verse_var.get().strip()
        if start_value not in verse_values:
            start_verse_var.set(verse_values[0])
            start_value = verse_values[0]
        if end_value not in verse_values:
            end_verse_var.set(verse_values[-1])
            end_value = verse_values[-1]
        if int(start_value) > int(end_value):
            end_verse_var.set(start_value)
        update_comparison_grid()

    def update_book_options() -> None:
        """What: Populate book choices from the union of all selected Bible catalogs.

        Why included: The source selection determines the shared book list for comparison.
        Where used: Bible selection and source refresh.
        Returns: None; preserves the selected book number and clears options when all sources are absent.
        """
        books_by_number = {}
        for name in get_selected_bible_names():
            source = get_source_for_name(name)
            if isinstance(source, dict):
                for entry in get_book_catalog(source):
                    if isinstance(entry, dict):
                        books_by_number.setdefault(int(entry.get("number") or 0), entry)
        if not books_by_number:
            book_combo.configure(values=[])
            book_var.set("")
            update_verse_range_controls()
            return
        book_catalog = [books_by_number[number] for number in sorted(books_by_number)]
        book_values = [f"{int(entry.get('number') or 0):02d} {str(entry.get('name') or 'Book')}" for entry in book_catalog if isinstance(entry, dict)]
        book_combo.configure(values=book_values)
        current_book = book_var.get().strip()
        if current_book not in book_values:
            current_number = current_book.partition(" ")[0]
            matching = next((value for value in book_values if value.partition(" ")[0] == current_number), None)
            book_var.set(matching or (book_values[0] if book_values else ""))
        update_verse_range_controls()

    def refresh_sources() -> None:
        """What: Discover normalized cache folders and reconcile valid source selections.

        Why included: Source availability can change as Bible imports are normalized/cached.
        Where used: Reader-tab startup after controls and callbacks are constructed.
        Returns: None; resets selectors and comparison panes when no source is available.
        """
        cache_root = app_base_path / "Bibles" / ".read_bible_cache"
        sources = discover_cached_bible_sources(cache_root)
        state["sources"] = sources
        source_map = {str(source.get("display_name") or "Bible"): source for source in sources if isinstance(source, dict)}
        state["source_by_display"] = source_map
        ordered_names = list(source_map.keys())
        if ordered_names:
            valid_selected_names = [name for name in get_selected_bible_names() if name in ordered_names]
            if not valid_selected_names and not isinstance(saved_bibles, list):
                valid_selected_names = ordered_names[: min(2, len(ordered_names))]
            selected_bibles.clear()
            selected_bibles.extend(valid_selected_names)
            for source_name in ordered_names:
                bible_check_vars[source_name] = tk.BooleanVar(value=source_name in selected_bibles)
            update_selection_button_text()
            rebuild_bible_selection_menu()
            update_book_options()
        else:
            selected_bibles.clear()
            bible_check_vars.clear()
            update_selection_button_text()
            rebuild_bible_selection_menu()
            book_combo.configure(values=[])
            chapter_combo.configure(values=[])
            start_verse_combo.configure(values=[])
            end_verse_combo.configure(values=[])
            for pane in comparison_frame.winfo_children():
                pane.destroy()

    def update_comparison_grid() -> None:
        """What: Render the chosen verse range across each selected Bible source.

        Why included: The reader's central workflow is side-by-side comparison of translations.
        Where used: Bible/book/chapter/range selection changes and source refresh.
        Returns: None; recreates comparison cells and their synchronized scroll handlers.
        """
        if not state["restoring_settings"]:
            save_bible_settings()
        for child in comparison_frame.winfo_children():
            child.destroy()
        comparison_frame.columnconfigure(0, weight=1)
        comparison_frame.columnconfigure(1, weight=0)
        comparison_frame.rowconfigure(0, weight=1)
        comparison_frame.rowconfigure(1, weight=0)
        source_names = get_selected_bible_names()
        if not source_names:
            placeholder = tk.Label(comparison_frame, text="Select Bibles to compare.", anchor="center", justify="center")
            placeholder.grid(row=0, column=0, sticky="nsew")
            style_widgets([placeholder], label_widgets=[placeholder])
            return
        if not book_var.get().strip() or not chapter_var.get().strip() or not start_verse_var.get().strip() or not end_verse_var.get().strip():
            placeholder = tk.Label(comparison_frame, text="Select a book, chapter, and verse range to compare.", anchor="center", justify="center")
            placeholder.grid(row=0, column=0, sticky="nsew")
            style_widgets([placeholder], label_widgets=[placeholder])
            return
        selected_start = int(start_verse_var.get())
        selected_end = int(end_verse_var.get())
        if selected_end < selected_start:
            selected_end = selected_start
        visible_names = source_names
        canvas = tk.Canvas(comparison_frame, highlightthickness=0)
        vertical_scrollbar = ttk.Scrollbar(comparison_frame, orient="vertical", command=canvas.yview)
        horizontal_scrollbar = ttk.Scrollbar(comparison_frame, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vertical_scrollbar.set, xscrollcommand=horizontal_scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")

        grid_frame = tk.Frame(canvas)
        style_widgets([canvas, grid_frame, vertical_scrollbar, horizontal_scrollbar])
        canvas_window = canvas.create_window((0, 0), window=grid_frame, anchor="nw")
        verse_column_width = 72
        bible_column_width = 260 if len(visible_names) <= 2 else 220

        grid_frame.columnconfigure(0, weight=0, minsize=verse_column_width)
        for column_index in range(1, len(visible_names) + 1):
            grid_frame.columnconfigure(column_index, weight=1, minsize=bible_column_width)

        def update_scroll_region(_event=None) -> None:
            """What: Recompute the canvas scrollable bounds after grid geometry changes.

            Why included: The comparison grid can grow as more verses or columns are shown.
            Where used: Grid-frame Configure binding.
            Returns: None; updates the canvas scrollregion.
            """
            canvas.configure(scrollregion=canvas.bbox("all"))

        def resize_grid_width(event) -> None:
            """What: Keep the embedded grid at least wide enough for all verse/Bible columns.

            Why included: Horizontal scrolling must remain available instead of compressing cells.
            Where used: Comparison canvas Configure binding.
            Returns: None; changes the canvas window item's width.
            """
            minimum_width = verse_column_width + (bible_column_width * len(visible_names))
            canvas.itemconfigure(canvas_window, width=max(event.width, minimum_width))

        def scroll_reading_pane(event) -> str:
            """What: Translate vertical mouse-wheel input into canvas scroll units.

            Why included: Reader content is nested, so the canvas should scroll under any child.
            Where used: bind_reading_pane_mousewheel() for comparison widgets.
            Returns: "break" after scrolling or consuming an unsupported event.
            """
            if getattr(event, "delta", 0):
                scroll_units = -3 if event.delta > 0 else 3
            elif getattr(event, "num", None) == 4:
                scroll_units = -3
            elif getattr(event, "num", None) == 5:
                scroll_units = 3
            else:
                return "break"
            canvas.yview_scroll(scroll_units, "units")
            return "break"

        def scroll_reading_pane_sideways(event) -> str:
            """What: Translate Shift+wheel input into horizontal canvas scrolling.

            Why included: Multiple Bible columns may exceed the visible comparison width.
            Where used: Shift-wheel binding in bind_reading_pane_mousewheel().
            Returns: "break" after scrolling or consuming an unsupported event.
            """
            if getattr(event, "delta", 0):
                scroll_units = -3 if event.delta > 0 else 3
            else:
                return "break"
            canvas.xview_scroll(scroll_units, "units")
            return "break"

        def bind_reading_pane_mousewheel(widget: tk.Widget) -> None:
            """What: Bind vertical and Shift-horizontal scrolling to one reader widget.

            Why included: Pointer location should not determine whether nested content scrolls.
            Where used: Canvas, grid frame, and individual comparison cells.
            Returns: None; adds event bindings to the supplied widget.
            """
            widget.bind("<MouseWheel>", scroll_reading_pane, add="+")
            widget.bind("<Button-4>", scroll_reading_pane, add="+")
            widget.bind("<Button-5>", scroll_reading_pane, add="+")
            widget.bind("<Shift-MouseWheel>", scroll_reading_pane_sideways, add="+")

        def add_grid_cell(row: int, column: int, text: str, *, is_header: bool = False) -> None:
            """What: Create, style, place, and scroll-bind one comparison grid cell.

            Why included: Verse numbers, source headings, and verse text share consistent geometry.
            Where used: update_comparison_grid() for headers and every selected verse/source pair.
            Returns: None; adds a Label to grid_frame.
            """
            cell = tk.Label(
                grid_frame,
                text=text,
                anchor="center" if is_header else "nw",
                justify="center" if is_header else "left",
                relief="solid",
                bd=1,
                padx=6,
                pady=6,
                wraplength=verse_column_width - 12 if column == 0 else bible_column_width - 18,
            )
            cell.grid(row=row, column=column, sticky="nsew")
            style_widgets([cell], label_widgets=[cell])
            bind_reading_pane_mousewheel(cell)

        bind_reading_pane_mousewheel(canvas)
        bind_reading_pane_mousewheel(grid_frame)

        add_grid_cell(0, 0, "Verse", is_header=True)
        for index, bible_name in enumerate(visible_names, start=1):
            add_grid_cell(0, index, bible_name, is_header=True)

        chapter_number = int(chapter_var.get())
        book_entries = {
            bible_name: get_selected_book_entry(get_source_for_name(bible_name))
            for bible_name in visible_names
        }
        for row_index, verse_number in enumerate(range(selected_start, selected_end + 1), start=1):
            add_grid_cell(row_index, 0, str(verse_number), is_header=True)
            for column_index, bible_name in enumerate(visible_names, start=1):
                source = get_source_for_name(bible_name)
                book_entry = book_entries.get(bible_name)
                verse_value = get_verse_text(source, book_entry, chapter_number, verse_number)
                add_grid_cell(row_index, column_index, verse_value or "[Verse not available]")

        grid_frame.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", resize_grid_width)

    def send_selection_to_display() -> None:
        """What: Build a structured Bible queue item and append it to the shared Display bridge.

        Why included: Users can present the selected book/chapter/range without re-entering text.
        Where used: Send to Display button command.
        Returns: None; validates selectors and reports asynchronous persistence results.
        """
        names = get_selected_bible_names()
        try:
            chapter_number = int(chapter_var.get())
            first_verse = int(start_verse_var.get())
            last_verse = int(end_verse_var.get())
        except ValueError:
            messagebox.showinfo("Bible", "Select a Bible, book, chapter and verse range first.", parent=tab_frame.winfo_toplevel())
            return
        if not names or not book_var.get() or last_verse < first_verse:
            messagebox.showinfo("Bible", "Select a Bible, book, chapter and valid verse range first.", parent=tab_frame.winfo_toplevel())
            return
        columns = []
        for name in names:
            source = get_source_for_name(name)
            entry = get_selected_book_entry(source)
            chapters = load_book_chapters(entry["book_path"]) if entry is not None else {}
            chapter = chapters.get(chapter_number) or {}
            columns.append({
                "title": name,
                "verses": [f"{number}. {chapter.get(number) or '[Verse not available]'}" for number in range(first_verse, last_verse + 1)],
            })
        book_entry = get_selected_book_entry(get_primary_selected_source())
        book_name = str(book_entry.get("name") or book_var.get()) if book_entry else book_var.get()
        now = datetime.now()
        font_value = []
        for key, default in (("selected_font_family", "Segoe UI"), ("selected_font_size", 12), ("selected_font_style", "normal")):
            variable = context.get(key)
            font_value.append(variable.get() if variable is not None else default)
        font_value[2] = str(font_value[2]).lower()
        preparation_state = context.get("preparation_state")
        get_header = preparation_state.get("get_theme_header") if isinstance(preparation_state, dict) else None
        if callable(get_header):
            columns[0]["theme_header"] = str(get_header() or "").strip()
        get_bounds = context.get("get_display_bounds")
        bounds = get_bounds() if callable(get_bounds) else {}
        columns, page_ranges = paginate_bible_columns(
            columns, font_value, max(1, int((bounds or {}).get("width") or 1920)),
            max(1, int((bounds or {}).get("height") or 1080)),
        )
        verse_labels = [f"{book_name} {chapter_number}:{first_verse + start}" +
                        (f"-{first_verse + stop - 1}" if stop - start > 1 else "")
                        for start, stop in page_ranges]
        item = {
            "id": f"bible-{now.strftime('%Y%m%d%H%M%S%f')}",
            "source": "bible",
            "title": f"{book_name} {chapter_number}:{first_verse}-{last_verse}",
            "subtitle": ", ".join(names),
            "created_at": now.isoformat(timespec="seconds"),
            "text": columns[0]["verses"][0],
            "verses": columns[0]["verses"],
            "verse_labels": verse_labels,
            "display_columns": columns,
            "font_value": font_value,
            "selected_verse_index": 0,
            "background_path": str(state.get("background_path") or ""),
        }

        def on_error(error) -> None:
            """What: Re-enable the send button and display a queue-write failure.

            Why included: A failed worker must not leave the action disabled without feedback.
            Where used: Error callback supplied to start_background_task().
            Returns: None; updates button/status and shows an error dialog.
            """
            send_button.configure(state="normal")
            messagebox.showerror("Bible", f"Unable to send to Display.\n\n{error}", parent=tab_frame.winfo_toplevel())

        def on_success(_result) -> None:
            """What: Re-enable sending and confirm the passage was added to Display.

            Why included: The user needs feedback after the queue bridge write succeeds.
            Where used: Success callback supplied to start_background_task().
            Returns: None; updates send button and status label.
            """
            send_button.configure(state="normal")
            send_status.configure(text="Added to Display")
            display_state = context.get("display_state")
            refresh = display_state.get("refresh_from_bridge") if isinstance(display_state, dict) else None
            if callable(refresh):
                refresh()

        send_button.configure(state="disabled")
        send_status.configure(text="")
        start_background_task = context.get("start_background_task")
        if callable(start_background_task):
            start_background_task("BibleQueueWriter", lambda: append_display_queue_item(item), on_success=on_success, on_error=on_error)
        else:
            try:
                result = append_display_queue_item(item)
            except Exception as error:
                on_error(error)
            else:
                on_success(result)

    def choose_background() -> None:
        """What: Open the shared media picker and update the saved Bible background.

        Why included: Comparison output can use a presentation background like other content tabs.
        Where used: Background button in the reader action row.
        Returns: None; applies an existing file selected through the native dialog.
        """

        def resolve_background_path(value: str) -> Path | None:
            """What: Resolve a picker choice as absolute or app-relative media path.

            Why included: The picker stores relative app paths but may receive absolute paths.
            Where used: open_background_picker() while loading its choices.
            Returns: Existing file path, or None when absent.
            """
            return resolve_background_file(value, app_base_path, get_app_data_path())

        def apply_background(value: str) -> None:
            """What: Persist a chosen background and update its button label.

            Why included: A failed settings write should leave the previous background intact.
            Where used: on_apply callback passed to open_background_picker().
            Returns: None; restores previous state when save_bible_settings() fails.
            """
            previous_background = state["background_path"]
            state["background_path"] = value
            if not save_bible_settings():
                state["background_path"] = previous_background
                return
            background_display.configure(text=Path(value).name if value else "Default background")

        open_background_picker(
            parent_window=tab_frame.winfo_toplevel(),
            dialog_title="Select Bible Background",
            empty_message="No background files were found in the Backgrounds folder.",
            picker_heading="Bible Background Files",
            choices=[],
            current_background=str(state.get("background_path") or ""),
            resolve_background_path=resolve_background_path,
            video_extensions={".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpg", ".mpeg"},
            apply_layout_style=apply_layout_style,
            on_apply=apply_background,
        )

    send_button = tk.Button(actions, text="Send to Display", command=send_selection_to_display)
    send_button.grid(row=0, column=0, sticky="w", padx=(0, 6))
    background_button = tk.Button(actions, text="Background", command=choose_background)
    background_button.grid(row=0, column=1, sticky="w", padx=6)
    saved_background = str(state.get("background_path") or "")
    if saved_background and resolve_background_file(saved_background, app_base_path, get_app_data_path()) is None:
        # A background saved by an older install location no longer exists; show and use the default.
        saved_background = ""
        state["background_path"] = ""
    background_display = tk.Label(actions, text=Path(saved_background).name if saved_background else "Default background", anchor="w", wraplength=240)
    background_display.grid(row=0, column=2, sticky="ew", padx=6)
    send_status = tk.Label(actions, text="", anchor="w")
    send_status.grid(row=0, column=3, sticky="ew", padx=6)
    style_widgets([actions, send_button, background_button, background_display, send_status], label_widgets=[background_display, send_status], input_widgets=[send_button, background_button])

    def trigger_refresh(_event=None) -> None:
        """What: Revalidate passage options and redraw comparison after a selector change.

        Why included: Book, chapter, and range choices determine the visible grid.
        Where used: Combobox <<ComboboxSelected>> bindings.
        Returns: None; updates valid options before rendering.
        """
        update_book_options()
        update_comparison_grid()

    def bind_mousewheel_combo(combo: ttk.Combobox) -> None:
        """What: Enable mouse-wheel cycling for one readonly passage selector.

        Why included: The same navigation behavior should apply to book, chapter, and verse menus.
        Where used: Initial reader-tab control setup for all four comboboxes.
        Returns: None; delegates bindings to _bind_mousewheel_cycle().
        """
        _bind_mousewheel_cycle(combo, list(combo["values"]))

    bible_menu_button.bind("<Button-1>", lambda _event: rebuild_bible_selection_menu(), add="+")
    book_combo.bind("<<ComboboxSelected>>", trigger_refresh, add="+")
    chapter_combo.bind("<<ComboboxSelected>>", trigger_refresh, add="+")
    start_verse_combo.bind("<<ComboboxSelected>>", trigger_refresh, add="+")
    end_verse_combo.bind("<<ComboboxSelected>>", trigger_refresh, add="+")
    bind_mousewheel_combo(book_combo)
    bind_mousewheel_combo(chapter_combo)
    bind_mousewheel_combo(start_verse_combo)
    bind_mousewheel_combo(end_verse_combo)

    style_widgets(
        [tab_frame, left_frame, controls_frame, comparison_frame, bible_menu_button, bible_selection_menu, book_combo, chapter_combo, start_verse_combo, end_verse_combo],
        label_widgets=[title_label, bible_selection_label, book_label, chapter_label, start_verse_label, end_verse_label],
        input_widgets=[bible_menu_button, bible_selection_menu, book_combo, chapter_combo, start_verse_combo, end_verse_combo],
    )

    notebook = context.get("notebook")
    is_tab_active = context.get("is_tab_active")

    def is_bible_tab_active() -> bool:
        if callable(is_tab_active):
            return bool(is_tab_active(tab_frame))
        if not isinstance(notebook, ttk.Notebook):
            return True
        try:
            return notebook.nametowidget(notebook.select()) is tab_frame
        except tk.TclError:
            return True

    def ensure_bible_tab_loaded(_event=None) -> None:
        if not is_bible_tab_active():
            return
        if not state.get("sources"):
            refresh_sources()
            state["restoring_settings"] = False

    if isinstance(notebook, ttk.Notebook):
        notebook.bind("<<NotebookTabChanged>>", ensure_bible_tab_loaded, add="+")
    if is_bible_tab_active():
        refresh_sources()
        state["restoring_settings"] = False
