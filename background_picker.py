"""Shared native file selection for backgrounds in all content tabs."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable

from app_paths import get_app_base_path, get_app_data_path


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpg", ".mpeg"}


def open_background_picker(
    *,
    parent_window: tk.Widget,
    dialog_title: str,
    empty_message: str,
    picker_heading: str,
    choices: list[str],
    current_background: str,
    resolve_background_path: Callable[[str], Path | None],
    video_extensions: set[str],
    apply_layout_style: Callable[..., None],
    on_apply: Callable[[str], None],
    show_file_size: bool = False,
    load_video_frame: Callable[[Path], object] | None = None,
) -> None:
    """What: Open the operating system's file dialog for images, GIFs, and videos.

    Why included: Every tab must browse arbitrary folders even when its background list is empty.
    Where used: Library, Welcome, Preparation, and Bible background buttons.
    Returns: None; applies one existing media file or leaves the selection unchanged on cancel.
    """
    current_path = resolve_background_path(current_background) if current_background else None
    if current_path is not None:
        current_path = Path(current_path)
    shared_backgrounds = get_app_data_path() / "Backgrounds"
    shared_backgrounds.mkdir(parents=True, exist_ok=True)
    directories = [
        current_path.parent if current_path is not None else None,
        shared_backgrounds,
        get_app_base_path() / "Backgrounds",
        get_app_base_path(),
    ]
    initial_directory = next((directory for directory in directories if directory is not None and directory.is_dir()), Path.home())
    image_pattern = " ".join(f"*{suffix}" for suffix in sorted(IMAGE_EXTENSIONS))
    video_pattern = " ".join(f"*{suffix}" for suffix in sorted(VIDEO_EXTENSIONS))
    selected = filedialog.askopenfilename(
        parent=parent_window,
        title=dialog_title,
        initialdir=str(initial_directory),
        initialfile=current_path.name if current_path is not None and current_path.is_file() else "",
        filetypes=[
            ("Backgrounds (images, GIFs, videos)", f"{image_pattern} {video_pattern}"),
            ("Images and GIFs", image_pattern),
            ("Videos", video_pattern),
            ("All files", "*.*"),
        ],
    )
    if not selected:
        return
    selected_path = Path(selected)
    if not selected_path.is_file() or selected_path.suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        messagebox.showerror(dialog_title, "Select an existing image, GIF, or video file.", parent=parent_window)
        return
    on_apply(str(selected_path.resolve()))