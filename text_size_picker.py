"""Reusable Tk dialog for selecting a text field and its font size."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Callable


TEXT_SIZE_OPTIONS = tuple(range(8, 41, 2))


def open_text_size_picker(parent: tk.Widget, field_sizes: dict[str, int], apply_layout_style, on_apply: Callable[[str, int], None]) -> None:
    """Open a shared dialog for selecting the size of one or more text fields.

    What: Displays supported font sizes, optionally adds a field selector, applies
    the supplied styling callback, and forwards the choice to on_apply.
    Why included: Welcome and Preparation share size-selection UI while retaining
    ownership of their own field state.
    Where used: Text-size commands in tab_welcome.py and tab_preparation.py.
    Returns: None; does not mutate field_sizes or owner state directly.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Text Size")
    dialog.resizable(False, False)
    dialog.transient(parent)
    dialog.grab_set()
    container = tk.Frame(dialog, padx=12, pady=12)
    container.grid(sticky="nsew")
    container.columnconfigure(0, weight=1)
    heading = tk.Label(container, text="Text Size", anchor="center")
    heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    field_var = tk.StringVar(value=next(iter(field_sizes)))
    field_selector = tk.Frame(container)
    has_multiple_fields = len(field_sizes) > 1
    if has_multiple_fields:
        field_selector.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    field_selector.columnconfigure(0, weight=1)
    size_list = tk.Listbox(container, height=8, exportselection=False)
    size_list.grid(row=2 if has_multiple_fields else 1, column=0, sticky="ew")
    for size in TEXT_SIZE_OPTIONS:
        size_list.insert(tk.END, str(size))

    def select_field(_event=None) -> None:
        """What: Select the current field's size in the size Listbox.

        Why included: Switching fields should show the existing size rather than
        leave the previous field's choice highlighted.
        Where used: Field radiobutton commands and initial dialog setup.
        Returns: None; clears selection when the current size is unsupported.
        """
        size_list.selection_clear(0, tk.END)
        current_size = field_sizes[field_var.get()]
        if current_size in TEXT_SIZE_OPTIONS:
            index = TEXT_SIZE_OPTIONS.index(current_size)
            size_list.selection_set(index)
            size_list.activate(index)
            size_list.see(index)

    field_buttons = []
    if has_multiple_fields:
        for index, field in enumerate(field_sizes):
            field_button = tk.Radiobutton(
                field_selector,
                text=field,
                variable=field_var,
                value=field,
                command=select_field,
                anchor="w",
            )
            field_button.grid(row=index, column=0, sticky="ew")
            field_buttons.append(field_button)

    def apply_size(size: int) -> None:
        """What: Send the chosen field and size to the owner, then close the dialog.

        Why included: The picker is reusable and must delegate state mutation to
        the tab that owns the selected text field.
        Where used: Default button and apply_selection() after list selection.
        Returns: None; invokes on_apply and destroys the popup.
        """
        on_apply(field_var.get(), size)
        dialog.destroy()

    def apply_selection() -> None:
        """What: Validate the Listbox choice and commit its corresponding size.

        Why included: Prevents an empty selection from indexing the size options.
        Where used: Apply button command in the text-size dialog.
        Returns: None; displays a prompt when no size is selected.
        """
        selection = size_list.curselection()
        if not selection:
            messagebox.showinfo("Text Size", "Select a text size first.", parent=dialog)
            return
        apply_size(TEXT_SIZE_OPTIONS[selection[0]])

    controls = tk.Frame(container)
    controls.grid(row=3 if has_multiple_fields else 2, column=0, sticky="ew", pady=(8, 0))
    for column in range(3):
        controls.columnconfigure(column, weight=1)
    default_button = tk.Button(controls, text="Default", command=lambda: apply_size(14))
    default_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
    apply_button = tk.Button(controls, text="Apply", command=apply_selection)
    apply_button.grid(row=0, column=1, sticky="ew", padx=4)
    cancel_button = tk.Button(controls, text="Cancel", command=dialog.destroy)
    cancel_button.grid(row=0, column=2, sticky="ew", padx=(4, 0))
    style_widgets = [dialog, container, heading, size_list, controls, default_button, apply_button, cancel_button]
    input_widgets = [size_list, default_button, apply_button, cancel_button]
    if has_multiple_fields:
        style_widgets.extend([field_selector, *field_buttons])
        input_widgets.extend(field_buttons)
    apply_layout_style(style_widgets, label_widgets=[heading], input_widgets=input_widgets)
    select_field()