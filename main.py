"""Application entry point: compose Tk tabs, shared state, and service workflows.

This module owns the main window, cross-tab callbacks, background-task bridge,
saved-service recall, theme/font settings, and startup/shutdown coordination.
Feature-specific widgets and rendering are implemented in their tab modules.
"""

from __future__ import annotations

import calendar
import ctypes
import json
import os
import queue
import sys
import threading
import time
from ctypes import wintypes
from datetime import date, datetime
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

from app_paths import PRODUCT_NAME, get_app_base_path, get_app_data_path, get_app_icon_path, get_branding_settings, get_default_background_path, resolve_background_file, save_branding_settings
from display_bridge import delete_service_entry, list_service_history_entries, list_service_entries_by_schedule, reset_active_display_queue_file, restore_display_queue_from_service_schedule, save_service_entry, validate_persistence_storage
from data_transfer import DATABASES as TRANSFER_DATABASES, DataTransferError, checkpoint_databases, export_database_archive, export_service_days_archive, import_database_archive, import_service_days_archive, importable_databases, importable_service_dates, list_service_dates, migrate_legacy_backgrounds, run_daily_backup
from tab_add_bible import build_bible_tab
from tab_bible import build_bible_reader_tab
from tab_display import build_display_tab
from tab_library import build_library_tab
from members import open_members_popup
from mirror import DEFAULT_BACKGROUND_PATH, MirrorDisplayController
from obs_secondary_display import OBS_INSTALLER_RELATIVE_PATH, OBS_PORTABLE_DIR_RELATIVE_PATH, OBS_PORTABLE_ZIP_RELATIVE_PATH
from overlay_layout import set_display_font, set_display_key_size, set_display_outline_width, set_display_text_color
from perf_logging import log_error, log_exception, log_timing
from tab_preparation import build_preparation_tab
from printer import open_print_popup
from tab_translation import build_translation_tab
from tab_welcome import build_welcome_tab
from spell_check import SpellCheckController
from windows_update import apply_optional_update_data, discard_prepared_update, launch_prepared_update, prepare_update_archive
from music_licensing import load_music_license_settings, save_music_license_settings
import licensing
import auto_update
import tempfile


class RECT(ctypes.Structure):
    """Describe a Windows rectangle in physical screen coordinates.

    What: Mirrors the Win32 RECT memory layout: left, top, right, bottom edges.
    Why included: User32 APIs return edges, not x/y/width/height; right and
    bottom are exclusive, so dimensions are calculated by subtracting edges.
    Where used: Fields rcMonitor and rcWork in MONITORINFO, then monitor
    enumeration and window-placement helpers below.
    """
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class MONITORINFO(ctypes.Structure):
    """Describe the Win32 structure used to query one monitor's geometry.

    What: Holds its byte size, full screen rectangle, work-area rectangle, and flags.
    Why included: ``GetMonitorInfoW`` writes into this exact C-compatible layout;
    an incorrect field order or width would corrupt the returned coordinates.
    Where used: get_window_work_area() reads rcWork; get_preferred_display_bounds()
    reads rcMonitor and checks dwFlags for MONITORINFOF_PRIMARY.
    """
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1


THEMES = {
    "Light": {
        "window_bg": "#eef1f5",
        "panel_bg": "#ffffff",
        "border": "#c8d0db",
        "text": "#16202a",
        "accent": "#255fa8",
        "menu_bg": "#dfeaf7",
        "menu_fg": "#0e2238",
        "tabs_bg": "#d7e3f3",
        "tabs_fg": "#16202a",
    },
    "Warm Sand": {
        "window_bg": "#f4efe5",
        "panel_bg": "#fffaf2",
        "border": "#d7c6a8",
        "text": "#3a2c1d",
        "accent": "#b76d2c",
        "menu_bg": "#f0dfc6",
        "menu_fg": "#352515",
        "tabs_bg": "#ead4b2",
        "tabs_fg": "#3a2c1d",
    },
    "Ocean Blue": {
        "window_bg": "#e8f1f8",
        "panel_bg": "#f8fbfe",
        "border": "#9eb8cc",
        "text": "#163247",
        "accent": "#2d6f9f",
        "menu_bg": "#d4e5f1",
        "menu_fg": "#133145",
        "tabs_bg": "#c3dced",
        "tabs_fg": "#163247",
    },
    "Forest Light": {
        "window_bg": "#edf4ee",
        "panel_bg": "#fbfdfb",
        "border": "#aabfae",
        "text": "#203326",
        "accent": "#4f7a57",
        "menu_bg": "#dbe9de",
        "menu_fg": "#1d3123",
        "tabs_bg": "#cfe1d2",
        "tabs_fg": "#203326",
    },
    "Rose Dawn": {
        "window_bg": "#f8ecef",
        "panel_bg": "#fff9fa",
        "border": "#d5b2bb",
        "text": "#4a2730",
        "accent": "#b45d73",
        "menu_bg": "#f1d9df",
        "menu_fg": "#47232c",
        "tabs_bg": "#e8c8d1",
        "tabs_fg": "#4a2730",
    },
    "Golden Wheat": {
        "window_bg": "#f7f1e3",
        "panel_bg": "#fffdf7",
        "border": "#d7c48e",
        "text": "#4a3b18",
        "accent": "#c28b2c",
        "menu_bg": "#f1e2b8",
        "menu_fg": "#453512",
        "tabs_bg": "#ead49a",
        "tabs_fg": "#4a3b18",
    },
    "Charcoal Teal": {
        "window_bg": "#242b2f",
        "panel_bg": "#2e373c",
        "border": "#4f6669",
        "text": "#edf4f3",
        "accent": "#3fa08f",
        "menu_bg": "#39484b",
        "menu_fg": "#edf4f3",
        "tabs_bg": "#355055",
        "tabs_fg": "#edf4f3",
    },
    "Burgundy Hall": {
        "window_bg": "#312126",
        "panel_bg": "#3c2a31",
        "border": "#6b4a56",
        "text": "#f6eef1",
        "accent": "#a44d66",
        "menu_bg": "#4a333b",
        "menu_fg": "#f6eef1",
        "tabs_bg": "#583841",
        "tabs_fg": "#f6eef1",
    },
    "Lavender Mist": {
        "window_bg": "#f2eef8",
        "panel_bg": "#fcfbff",
        "border": "#c7bddb",
        "text": "#34284a",
        "accent": "#7e66b7",
        "menu_bg": "#e4ddf3",
        "menu_fg": "#312647",
        "tabs_bg": "#d6cbee",
        "tabs_fg": "#34284a",
    },
    "Copper Clay": {
        "window_bg": "#f3ece8",
        "panel_bg": "#fefaf8",
        "border": "#cfae9d",
        "text": "#4c3126",
        "accent": "#b66b43",
        "menu_bg": "#ead5ca",
        "menu_fg": "#472d22",
        "tabs_bg": "#dfc0b0",
        "tabs_fg": "#4c3126",
    },
    "Sage Linen": {
        "window_bg": "#f1f2ea",
        "panel_bg": "#fcfcf8",
        "border": "#bcc3aa",
        "text": "#344032",
        "accent": "#6e8a5c",
        "menu_bg": "#e3e6d8",
        "menu_fg": "#313d30",
        "tabs_bg": "#d4dac4",
        "tabs_fg": "#344032",
    },
    "Midnight Amber": {
        "window_bg": "#1f232a",
        "panel_bg": "#2a3038",
        "border": "#555f71",
        "text": "#f6f3ea",
        "accent": "#d18a33",
        "menu_bg": "#343c47",
        "menu_fg": "#f6f3ea",
        "tabs_bg": "#404b58",
        "tabs_fg": "#f6f3ea",
    },
    "Ice Silver": {
        "window_bg": "#edf2f5",
        "panel_bg": "#fbfdfe",
        "border": "#b5c1c9",
        "text": "#26333d",
        "accent": "#5c88a3",
        "menu_bg": "#dce6ec",
        "menu_fg": "#24313a",
        "tabs_bg": "#ccd9e2",
        "tabs_fg": "#26333d",
    },
    "Emerald Night": {
        "window_bg": "#1f2925",
        "panel_bg": "#29362f",
        "border": "#4f675c",
        "text": "#edf5ef",
        "accent": "#44a06b",
        "menu_bg": "#32423a",
        "menu_fg": "#edf5ef",
        "tabs_bg": "#3b5147",
        "tabs_fg": "#edf5ef",
    },
    "Slate Night": {
        "window_bg": "#202731",
        "panel_bg": "#2b3440",
        "border": "#4b5b6d",
        "text": "#f0f4f8",
        "accent": "#4aa3ff",
        "menu_bg": "#354252",
        "menu_fg": "#f0f4f8",
        "tabs_bg": "#31404f",
        "tabs_fg": "#f0f4f8",
    },
}

FONT_FAMILIES = ("Segoe UI", "Arial", "Calibri", "Cambria", "Tahoma")
FONT_STYLES = {
    "Regular": "normal",
    "Bold": "bold",
    "Italic": "italic",
    "Bold Italic": "bold italic",
}
FONT_SIZES = tuple(range(8, 33, 2))
SERVICE_TIME_OPTIONS = ("Morning", "Night", "Other")
MIRROR_PREVIEW_WIDTH = 360
MIRROR_PREVIEW_HEIGHT = 203
CONTENT_TAB_COLUMN_WEIGHTS = (5, 8, 5)
DEFAULT_LIBRARY_FONT_FAMILY = "Segoe UI"
DISPLAY_OUTLINE_OPTIONS = ("Auto", *(str(size) for size in range(0, 11)))
DISPLAY_TEXT_COLOR_OPTIONS = ("Auto", "White", "Black")
DISPLAY_KEY_SIZE_OPTIONS = tuple(range(2, 33, 2))
DEFAULT_DISPLAY_KEY_SIZE = 12


def run_startup_diagnostics(app_base_path: Path | None = None) -> list[str]:
    """What: Check that the default background, OBS runtime, and Data folder exist.

    Why included: Report missing deployment files before a user tries to display
    content; diagnostics warn but do not prevent the rest of the app from opening.
    Where used: Called by build_ui() after the root window and logging are ready.
    Returns: Human-readable issues for the startup warning dialog.
    """
    resolved_base_path = Path(app_base_path) if app_base_path is not None else get_app_base_path()
    issues: list[str] = []
    default_background_path = get_default_background_path(resolved_base_path)
    if not default_background_path.exists():
        issues.append(f"Missing default background: {default_background_path}")
    obs_zip_path = resolved_base_path / OBS_PORTABLE_ZIP_RELATIVE_PATH
    obs_installer_path = resolved_base_path / OBS_INSTALLER_RELATIVE_PATH
    obs_portable_dir = resolved_base_path / OBS_PORTABLE_DIR_RELATIVE_PATH
    private_obs_dir = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "PewVista" / get_app_data_path(resolved_base_path).name / "obs_portable"
    if not any(path.exists() for path in (obs_portable_dir, obs_zip_path, obs_installer_path, private_obs_dir / "bin" / "64bit" / "obs64.exe")):
        issues.append("OBS Studio (the secondary-screen player) is not on this PC yet. PewVista downloads it (about 185 MB) the first time the secondary screen starts; an internet connection is needed then.")
    obs_settings_path = get_app_data_path(resolved_base_path) / "obs_secondary_settings.json"
    if not obs_settings_path.parent.exists():
        issues.append(f"Missing settings directory: {obs_settings_path.parent}")
    for issue in issues:
        log_error("main.startup_diagnostics", issue)
    return issues


def get_data_directory() -> Path:
    """Resolve the writable folder where this application keeps user state.

    What: Creates and returns ``Data`` beneath get_app_base_path().
    Why included: A clean install may not contain ``Data`` yet, and settings
    writes need a parent directory before the first save can succeed.
    Where used: get_ui_settings_path() uses it for ui_settings.json; that path is
    used by load_ui_settings() and save_ui_settings(). Other databases use the
    same app root through app_paths.get_app_base_path().
    Returns: The created/existing directory as a pathlib.Path.
    """
    path = get_app_data_path()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_ui_settings_path() -> Path:
    """What: Return Data/ui_settings.json beside the app root.

    Why included: Centralize the settings path so load/save use the same location
    in both source runs and frozen Windows builds.
    Where used: Called by load_ui_settings() and save_ui_settings().
    Returns: The settings file path; get_data_directory() creates its parent.
    """
    return get_data_directory() / "ui_settings.json"


def get_theme_by_name(theme_name: str | None) -> dict:
    """What: Resolve a saved/menu theme name to its color palette.

    Why included: Old or edited settings may contain a theme no longer offered;
    returning Light keeps all callers safe from missing-key errors.
    Where used: Theme application, popup styling, and startup initialization.
    Returns: A complete palette dictionary.
    """
    return THEMES.get(str(theme_name or ""), THEMES["Light"])


def load_json_file_with_recovery(path: Path) -> dict:
    """What: Load a JSON object from the target or its sibling temporary file.

    Why included: A prior interrupted settings replacement can leave only the
    temp file; recovering it avoids resetting valid user preferences.
    Where used: load_ui_settings() for the main appearance settings.
    Returns: A dictionary, or an empty dictionary when neither file is valid.
    """
    candidate_paths = [path, path.with_suffix(path.suffix + ".tmp")]
    for candidate_path in candidate_paths:
        if not candidate_path.exists():
            continue
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            if candidate_path != path:
                try:
                    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                except OSError:
                    pass
            return payload
    return {}


def load_ui_settings() -> dict:
    """What: Load theme, app font, display font, outline, and color preferences.

    Why included: Reapply a user's appearance choices each time the program opens.
    Where used: Called near the start of build_ui() before widgets are created.
    Returns: The saved settings dictionary, or empty data for first-run defaults.
    """
    settings_path = get_ui_settings_path()
    if not settings_path.exists():
        tmp_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
        if not tmp_path.exists():
            return {}
    return load_json_file_with_recovery(settings_path)


def save_ui_settings(
    theme: str,
    font_family: str,
    font_style: str,
    font_size: int,
    display_font_family: str,
    display_font_style: str,
    display_outline_size: str,
    display_text_color: str = "Auto",
    display_key_size: int = DEFAULT_DISPLAY_KEY_SIZE,
) -> None:
    """What: Serialize current app/display appearance choices to UI settings JSON.

    Why included: Keep theme/font changes across restarts and tolerate transient
    Windows file locks by writing a temp file and retrying its replacement.
    Where used: Theme, font, and Display Settings menu change handlers.
    Returns: None; persistence is performed as a side effect.
    """
    payload = {
        "theme": str(theme or "Light"),
        "font_family": str(font_family or DEFAULT_LIBRARY_FONT_FAMILY),
        "font_style": str(font_style or "Bold"),
        "font_size": int(font_size),
        "display_font_family": str(display_font_family or DEFAULT_LIBRARY_FONT_FAMILY),
        "display_font_style": str(display_font_style or "normal"),
        "display_outline_size": str(display_outline_size or "Auto"),
        "display_text_color": str(display_text_color or "Auto"),
        "display_key_size": 0 if int(display_key_size) <= 0 else max(2, min(32, int(display_key_size))),
    }
    settings_path = get_ui_settings_path()
    temporary_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
    serialized_payload = json.dumps(payload, indent=2)
    temporary_path.write_text(serialized_payload, encoding="utf-8")
    for attempt in range(4):
        try:
            temporary_path.replace(settings_path)
            break
        except OSError:
            if attempt < 3:
                time.sleep(0.02)
            else:
                try:
                    settings_path.write_text(serialized_payload, encoding="utf-8")
                    temporary_path.unlink(missing_ok=True)
                except Exception:
                    pass


def ensure_ttk_theme_loaded(style: ttk.Style, force_fallback: bool = False) -> None:
    """What: Select an available ttk theme, preferring the current/native theme.

    Why included: Named ttk styles behave differently across Windows themes; a
    known fallback is needed before configuring tab and combobox appearances.
    Where used: configure_tabs_notebook_style().
    Returns: None; updates the supplied ttk.Style in place.
    """
    try:
        available_themes = tuple(style.theme_names())
    except tk.TclError:
        return
    if not available_themes:
        return
    if not force_fallback:
        try:
            current_theme = style.theme_use()
        except tk.TclError:
            current_theme = ""
        if current_theme in available_themes:
            return
    for fallback_theme in ("vista", "xpnative", "clam", "default", "alt", "classic"):
        if fallback_theme not in available_themes:
            continue
        try:
            style.theme_use(fallback_theme)
            return
        except tk.TclError:
            continue


def try_configure_ttk_style(style: ttk.Style, style_name: str, **kwargs) -> None:
    """What: Apply a ttk style configuration and suppress unsupported Tk options.

    Why included: Different Windows themes expose different style properties;
    optional decoration must not prevent startup.
    Where used: configure_tabs_notebook_style().
    Returns: None; unsupported settings are ignored.
    """
    try:
        style.configure(style_name, **kwargs)
    except tk.TclError:
        pass


def try_map_ttk_style(style: ttk.Style, style_name: str, **kwargs) -> None:
    """What: Apply state-specific ttk colors while ignoring unsupported mappings.

    Why included: Selected/active states vary by theme; a mapping failure should
    leave the default style intact rather than break the UI.
    Where used: configure_tabs_notebook_style().
    Returns: None; mutates the supplied ttk.Style when supported.
    """
    try:
        style.map(style_name, **kwargs)
    except tk.TclError:
        pass


def get_ui_font_set(family: str, style_name: str, size: int) -> tuple[tkfont.Font, tkfont.Font, tkfont.Font]:
    """What: Build separate header, label, and input Tk fonts from user settings.

    Why included: Headers need emphasis while editable controls remain readable
    and normal-weight even when labels use bold/italic styling.
    Where used: Main/tab/popup style application helpers.
    Returns: (header_font, label_font, input_font).
    """
    header_font = tkfont.Font(
        family=family,
        size=max(10, int(size) + 1),
        weight="bold" if "Bold" in style_name else "normal",
        slant="italic" if "Italic" in style_name else "roman",
    )
    label_font = tkfont.Font(family=family, size=int(size), weight="bold" if "Bold" in style_name else "normal", slant="italic" if "Italic" in style_name else "roman")
    input_font = tkfont.Font(family=family, size=int(size), weight="normal", slant="roman")
    return header_font, label_font, input_font


def set_widget_colors(widget: tk.Widget, theme: dict) -> None:
    """What: Apply the palette values supported by a widget's Tk class.

    Why included: Tk and ttk widgets use different option names; the class-based
    branches set compatible colors and ignore options rejected by native controls.
    Where used: apply_theme(), tab style callbacks, and popup styling.
    Returns: None; modifies the widget in place.
    """
    def safe_configure(**kwargs) -> None:
        """Apply supported color options independently to one Tk widget.

        What: Calls widget.configure() once per provided option.
        Why included: Native Tk widgets differ in which color options they accept;
        a failure on one option should not suppress all remaining colors.
        Where used: Nested in set_widget_colors() for each widget class branch.
        Returns: None; ignores TclError for individual unsupported options.
        """
        for key, value in kwargs.items():
            try:
                widget.configure(**{key: value})
            except tk.TclError:
                pass

    widget_class = widget.winfo_class()
    background = theme["panel_bg"]
    foreground = theme["text"]
    active_background = theme["menu_bg"]
    active_foreground = theme["menu_fg"]
    if widget_class in {"Frame", "Labelframe", "Toplevel"}:
        safe_configure(bg=background)
        return
    if widget_class == "Label":
        safe_configure(bg=background, fg=foreground)
        return
    if widget_class in {"Button", "Menubutton"}:
        safe_configure(bg=theme["menu_bg"], fg=theme["menu_fg"], activebackground=active_background, activeforeground=active_foreground, disabledforeground=theme["border"])
        return
    if widget_class in {"Checkbutton", "Radiobutton"}:
        safe_configure(bg=theme["menu_bg"], fg=theme["menu_fg"], activebackground=active_background, activeforeground=active_foreground, selectcolor=background, disabledforeground=theme["border"])
        return
    if widget_class in {"Entry", "Text"}:
        safe_configure(bg="#ffffff", fg=foreground, insertbackground=foreground, disabledforeground=theme["border"])
        return
    if widget_class == "Listbox":
        safe_configure(bg="#ffffff", fg=foreground, disabledforeground=theme["border"])
        return
    if widget_class == "Canvas":
        safe_configure(bg=background, highlightbackground=theme["border"])
        return
    if widget_class == "Menu":
        safe_configure(background=theme["menu_bg"], foreground=theme["menu_fg"], activebackground=active_background, activeforeground=active_foreground)
        return
    safe_configure(bg=background, fg=foreground)


def set_widget_font(widget: tk.Widget, font: tkfont.Font) -> None:
    """What: Assign a Tk font object to a widget when that widget supports fonts.

    Why included: Some native controls reject font configuration; styling should
    remain best-effort rather than abort the theme/font update.
    Where used: Main, tab, calendar, and popup styling.
    Returns: None; unsupported widget options are ignored.
    """
    try:
        widget.configure(font=font)
    except tk.TclError:
        pass


def configure_tabs_notebook_style(notebook: ttk.Notebook, theme: dict, family: str, style_name: str, size: int) -> None:
    """What: Configure the named ttk styles for content tabs and comboboxes.

    Why included: Notebook tabs and read-only selectors use ttk styling rather
    than the classic Tk color options handled by set_widget_colors().
    Where used: Initial tab setup and every theme/font change.
    Returns: None; updates the Style object associated with the notebook.
    """
    style = ttk.Style(notebook)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    else:
        ensure_ttk_theme_loaded(style)
    size_scale = max(0.85, float(size) / 12.0)
    tab_padding = (max(8, int(10 * size_scale)), max(4, int(6 * size_scale)))
    tab_font_parts = [family, int(size)]
    if "Bold" in style_name:
        tab_font_parts.append("bold")
    if "Italic" in style_name:
        tab_font_parts.append("italic")
    tab_font = tuple(tab_font_parts)
    try_configure_ttk_style(style, "ContentTabs.TNotebook", background=theme["panel_bg"], borderwidth=0)
    try_configure_ttk_style(style, "ContentTabs.TNotebook.Tab", background=theme["tabs_bg"], foreground=theme["tabs_fg"], padding=tab_padding, font=tab_font)
    try_map_ttk_style(style, "ContentTabs.TNotebook.Tab", background=[("selected", theme["accent"]), ("active", theme["menu_bg"])], foreground=[("disabled", theme["border"]), ("selected", theme["menu_fg"]), ("active", theme["text"]), ("!selected", theme["tabs_fg"])])
    try_configure_ttk_style(style, "Content.TCombobox", background=theme["menu_bg"], foreground=theme["text"], fieldbackground=theme["panel_bg"], arrowcolor=theme["text"], font=(family, int(size), "normal"))
    try_map_ttk_style(style, "Content.TCombobox", fieldbackground=[("readonly", theme["panel_bg"])], foreground=[("disabled", theme["border"]), ("readonly", theme["text"])], selectbackground=[("readonly", theme["accent"])], selectforeground=[("readonly", theme["menu_fg"])])
    # Default ttk styles color popup dialogs (Settings, Licenses, and others) to match the theme.
    try_configure_ttk_style(style, "TFrame", background=theme["panel_bg"])
    try_configure_ttk_style(style, "TLabel", background=theme["panel_bg"], foreground=theme["text"])
    try_configure_ttk_style(style, "TCheckbutton", background=theme["panel_bg"], foreground=theme["text"])
    try_configure_ttk_style(style, "TRadiobutton", background=theme["panel_bg"], foreground=theme["text"])
    try_configure_ttk_style(style, "TLabelframe", background=theme["panel_bg"], bordercolor=theme["border"])
    try_configure_ttk_style(style, "TLabelframe.Label", background=theme["panel_bg"], foreground=theme["text"])
    try_configure_ttk_style(style, "TButton", background=theme["menu_bg"], foreground=theme["menu_fg"], bordercolor=theme["border"])
    try_map_ttk_style(style, "TButton", background=[("pressed", theme["accent"]), ("active", theme["accent"])], foreground=[("disabled", theme["border"]), ("pressed", theme["menu_fg"]), ("active", "#ffffff")])
    try_configure_ttk_style(style, "TEntry", fieldbackground=theme["panel_bg"], foreground=theme["text"], insertcolor=theme["text"], bordercolor=theme["border"])
    try_configure_ttk_style(style, "TCombobox", background=theme["menu_bg"], foreground=theme["text"], fieldbackground=theme["panel_bg"], arrowcolor=theme["text"], bordercolor=theme["border"])
    try_map_ttk_style(style, "TCombobox", fieldbackground=[("readonly", theme["panel_bg"])], foreground=[("disabled", theme["border"]), ("readonly", theme["text"])], selectbackground=[("readonly", theme["accent"])], selectforeground=[("readonly", theme["menu_fg"])])
    try_configure_ttk_style(style, "TNotebook", background=theme["panel_bg"], bordercolor=theme["border"])
    try_configure_ttk_style(style, "TNotebook.Tab", background=theme["tabs_bg"], foreground=theme["tabs_fg"], padding=tab_padding)
    try_map_ttk_style(style, "TNotebook.Tab", background=[("selected", theme["accent"]), ("active", theme["menu_bg"])], foreground=[("selected", theme["menu_fg"]), ("active", theme["text"]), ("!selected", theme["tabs_fg"])])
    try:
        # Dropdown lists of comboboxes are classic Tk listboxes configured through the option database.
        notebook.option_add("*TCombobox*Listbox.background", theme["panel_bg"])
        notebook.option_add("*TCombobox*Listbox.foreground", theme["text"])
        notebook.option_add("*TCombobox*Listbox.selectBackground", theme["accent"])
        notebook.option_add("*TCombobox*Listbox.selectForeground", theme["menu_fg"])
    except tk.TclError:
        pass


def build_preview_placeholder(parent_frame: tk.Widget, title: str) -> tuple[tk.Frame, tk.Label]:
    """What: Create the fixed-size black preview frame and centered text/image label.

    Why included: Tabs need identical preview geometry before media has loaded;
    fixed dimensions avoid layout shifts during animation and resizing.
    Where used: Welcome, Library, and Preparation tab builders.
    Returns: The container frame and its preview label.
    """
    preview_container = tk.Frame(parent_frame, width=MIRROR_PREVIEW_WIDTH, height=MIRROR_PREVIEW_HEIGHT, bd=1, relief="solid")
    preview_container.grid_propagate(False)
    preview_container.columnconfigure(0, weight=1)
    preview_container.rowconfigure(0, weight=1)
    preview_label = tk.Label(preview_container, text=title, justify="center", anchor="center", bg="#000000", fg="#ffffff", wraplength=MIRROR_PREVIEW_WIDTH - 24, padx=12, pady=12)
    preview_label.grid(row=0, column=0, sticky="nsew")
    return preview_container, preview_label


def open_date_picker(current_parent: tk.Misc, current_var: tk.StringVar, theme_name: str, font_family: str, font_style: str, font_size: int, on_date_selected=None) -> None:
    """What: Show a themed month/year calendar and commit the chosen ISO date.

    Why included: Service date input needs calendar selection without accepting
    arbitrary text that would not match saved-service schedule keys.
    Where used: The main Info form and Members date-of-birth picker callback.
    Returns: None; updates current_var and calls the optional owner callback.
    """
    today = date.today()
    initial_date = today
    current_text = str(current_var.get() or "").strip()
    if current_text:
        for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y"):
            try:
                initial_date = date.fromisoformat(current_text) if date_format == "%Y-%m-%d" else datetime.strptime(current_text, date_format).date()
                break
            except Exception:
                continue

    theme = get_theme_by_name(theme_name)
    header_font, label_font, input_font = get_ui_font_set(font_family, font_style, font_size)
    size_scale = max(0.85, float(font_size) / 12.0)
    outer_padding = max(12, int(16 * size_scale))
    section_padding = max(6, int(12 * size_scale))
    cell_size = max(32, int(34 * size_scale))
    cell_pad = max(1, int(2 * size_scale))
    action_pad = max(4, int(6 * size_scale))

    dialog = tk.Toplevel(current_parent)
    dialog.title("Select Date")
    dialog.resizable(False, False)
    dialog.transient(current_parent.winfo_toplevel())
    dialog.grab_set()
    set_widget_colors(dialog, theme)

    container = tk.Frame(dialog, padx=outer_padding, pady=outer_padding)
    container.grid(sticky="nsew")
    container.columnconfigure(0, weight=1)
    set_widget_colors(container, theme)

    displayed_year = tk.IntVar(value=initial_date.year)
    displayed_month = tk.IntVar(value=initial_date.month)
    displayed_month_text = tk.StringVar(value=calendar.month_name[initial_date.month])
    displayed_year_text = tk.StringVar(value=str(initial_date.year))
    selected_date = {"value": initial_date}

    header_row = tk.Frame(container)
    header_row.grid(row=0, column=0, sticky="ew")
    set_widget_colors(header_row, theme)

    prev_button = tk.Button(header_row, text="<")
    prev_button.grid(row=0, column=0, sticky="w")
    month_frame = tk.Frame(header_row)
    month_frame.grid(row=0, column=1, sticky="w", padx=(action_pad, action_pad // 2))
    set_widget_colors(month_frame, theme)
    month_caption = tk.Label(month_frame, text="Month", anchor="w")
    month_caption.grid(row=0, column=0, sticky="w", padx=(0, action_pad // 2))
    month_dropdown = ttk.Combobox(month_frame, values=tuple(calendar.month_name[index] for index in range(1, 13)), textvariable=displayed_month_text, state="readonly", width=10)
    month_dropdown.grid(row=0, column=1, sticky="w")
    year_frame = tk.Frame(header_row)
    year_frame.grid(row=0, column=2, sticky="w", padx=(action_pad // 2, action_pad))
    set_widget_colors(year_frame, theme)
    year_caption = tk.Label(year_frame, text="Year", anchor="w")
    year_caption.grid(row=0, column=0, sticky="w", padx=(0, action_pad // 2))
    year_values = tuple(str(year_value) for year_value in range(1900, 2101))
    year_dropdown = ttk.Combobox(year_frame, values=year_values, textvariable=displayed_year_text, state="readonly", width=6)
    year_dropdown.grid(row=0, column=1, sticky="w")
    next_button = tk.Button(header_row, text=">")
    next_button.grid(row=0, column=3, sticky="e")

    weekday_row = tk.Frame(container)
    weekday_row.grid(row=1, column=0, sticky="ew", pady=(section_padding, cell_pad * 2))
    set_widget_colors(weekday_row, theme)
    for column_index in range(7):
        weekday_row.columnconfigure(column_index, weight=1)

    weekday_labels: list[tk.Label] = []
    for column_index, weekday_name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
        weekday_label = tk.Label(weekday_row, text=weekday_name, anchor="center")
        weekday_label.grid(row=0, column=column_index, sticky="ew", padx=cell_pad)
        weekday_labels.append(weekday_label)

    calendar_grid = tk.Frame(container)
    calendar_grid.grid(row=2, column=0, sticky="nsew")
    set_widget_colors(calendar_grid, theme)
    for column_index in range(7):
        calendar_grid.columnconfigure(column_index, weight=1, minsize=cell_size)
    for row_index in range(6):
        calendar_grid.rowconfigure(row_index, weight=1, minsize=cell_size)

    preview_label = tk.Label(container, text="", justify="left", anchor="w")
    preview_label.grid(row=3, column=0, sticky="ew", pady=(section_padding, 0))

    footer_row = tk.Frame(container)
    footer_row.grid(row=4, column=0, sticky="ew", pady=(section_padding, 0))
    footer_row.columnconfigure(0, weight=1)
    footer_row.columnconfigure(1, weight=1)
    footer_row.columnconfigure(2, weight=1)
    set_widget_colors(footer_row, theme)

    day_buttons: list[tk.Button] = []

    def style_day_button(button: tk.Button, button_date: date | None, is_current_month: bool) -> None:
        """Apply the visual state of one date cell in the calendar popup.

        What: Uses separate palette colors for outside-month, today, and selected days.
        Why included: The calendar must distinguish navigation context from the
        pending date without changing the date value itself.
        Where used: refresh_calendar() for each of the 42 day buttons.
        Returns: None; configures the supplied button in place.
        """
        base_background = theme["menu_bg"] if is_current_month else theme["panel_bg"]
        base_foreground = theme["text"] if is_current_month else theme["border"]
        if button_date == today:
            base_background = theme["tabs_bg"]
        if button_date == selected_date["value"]:
            base_background = theme["accent"]
            base_foreground = theme["menu_fg"]
        button.configure(
            bg=base_background,
            fg=base_foreground,
            activebackground=theme["accent"],
            activeforeground=theme["menu_fg"],
            disabledforeground=theme["border"],
            relief="solid",
            bd=1,
            highlightthickness=0,
        )

    def refresh_calendar() -> None:
        """Redraw the calendar controls for the displayed month/year.

        What: Computes six Monday-first weeks, updates labels, and binds each cell
        to its represented date, including adjacent-month dates in blank cells.
        Why included: The grid has a stable 6x7 shape as months change, avoiding
        layout movement and keeping week alignment consistent.
        Where used: Initial picker setup, month navigation, dropdown changes, and
        select_day() after choosing a date.
        Returns: None; updates day buttons and the selected-date preview label.
        """
        displayed = date(displayed_year.get(), displayed_month.get(), 1)
        displayed_month_text.set(calendar.month_name[displayed.month])
        displayed_year_text.set(str(displayed.year))
        preview_label.configure(text=selected_date["value"].strftime("%A, %d %b %Y"))

        month_days = calendar.Calendar(firstweekday=0).monthdatescalendar(displayed.year, displayed.month)
        while len(month_days) < 6:
            # Keep the date grid at six rows so month changes never move the picker controls.
            last_week = month_days[-1]
            month_days.append([day + __import__("datetime").timedelta(days=7) for day in last_week])

        for button_index, button in enumerate(day_buttons):
            week_index, weekday_index = divmod(button_index, 7)
            button_date = month_days[week_index][weekday_index]
            is_current_month = button_date.month == displayed.month
            button.configure(text=str(button_date.day), command=lambda chosen_date=button_date: select_day(chosen_date))
            style_day_button(button, button_date, is_current_month)

    def shift_month(offset: int) -> None:
        """Move the visible month backward or forward, including year rollover.

        What: Applies a signed month offset and wraps across January/December.
        Why included: Previous/Next buttons should navigate without invalid months.
        Where used: Previous and Next buttons in open_date_picker().
        Returns: None; updates month/year variables and redraws the calendar.
        """
        month_value = displayed_month.get() + offset
        year_value = displayed_year.get()
        if month_value < 1:
            # The two branches are mutually exclusive: one signed step crosses at most one year boundary.
            month_value = 12
            year_value -= 1
        elif month_value > 12:
            month_value = 1
            year_value += 1
        displayed_year.set(year_value)
        displayed_month.set(month_value)
        refresh_calendar()

    def apply_month_year_selection(*_args) -> None:
        """Validate the month/year dropdown pair and synchronize calendar state.

        What: Converts the month name and year text to checked integer variables.
        Why included: StringVar traces can fire on intermediate or invalid edits;
        invalid values must not create an invalid date object.
        Where used: Combobox selection events and the two StringVar trace handlers.
        Returns: None; leaves the current calendar unchanged when input is invalid.
        """
        month_text = str(displayed_month_text.get() or "").strip()
        month_lookup = {calendar.month_name[index]: index for index in range(1, 13)}
        month_value = month_lookup.get(month_text)
        if month_value is None:
            return
        try:
            year_value = int(str(displayed_year_text.get() or "").strip())
        except Exception:
            return
        if year_value < 1:
            return
        displayed_year.set(year_value)
        displayed_month.set(month_value)
        refresh_calendar()

    def select_day(chosen_date: date) -> None:
        """Set the pending date when a calendar-day or Today button is pressed.

        What: Updates selected_date and moves the displayed month/year to the choice.
        Why included: The chosen value may belong to an adjacent-month cell.
        Where used: Day-button commands and the Today button.
        Returns: None; redraws the calendar but does not commit to the caller yet.
        """
        selected_date["value"] = chosen_date
        displayed_year.set(chosen_date.year)
        displayed_month.set(chosen_date.month)
        displayed_month_text.set(calendar.month_name[chosen_date.month])
        displayed_year_text.set(str(chosen_date.year))
        refresh_calendar()

    def apply_selected_date(close_dialog: bool = True) -> None:
        """Commit the pending calendar choice to the caller's date variable.

        What: Writes ISO YYYY-MM-DD text and invokes the optional callback.
        Why included: Schedule keys use a stable machine-readable date format.
        Where used: Apply button and callers that need date selection without closing.
        Returns: None; optionally destroys the dialog after notification.
        """
        current_var.set(selected_date["value"].isoformat())
        if callable(on_date_selected):
            try:
                on_date_selected()
            except TypeError:
                on_date_selected(None)
        if close_dialog:
            dialog.destroy()

    prev_button.configure(command=lambda: shift_month(-1))
    next_button.configure(command=lambda: shift_month(1))
    month_dropdown.bind("<<ComboboxSelected>>", apply_month_year_selection)
    year_dropdown.bind("<<ComboboxSelected>>", apply_month_year_selection)

    for row_index in range(6):
        for column_index in range(7):
            day_button = tk.Button(calendar_grid, text="")
            day_button.grid(row=row_index, column=column_index, sticky="nsew", padx=cell_pad, pady=cell_pad, ipadx=action_pad, ipady=action_pad)
            day_buttons.append(day_button)

    today_button = tk.Button(footer_row, text="Today", command=lambda: select_day(today))
    today_button.grid(row=0, column=0, sticky="ew", padx=(0, action_pad))
    apply_button = tk.Button(footer_row, text="Apply", command=apply_selected_date)
    apply_button.grid(row=0, column=1, sticky="ew", padx=action_pad)
    close_button = tk.Button(footer_row, text="Close", command=dialog.destroy)
    close_button.grid(row=0, column=2, sticky="ew", padx=(action_pad, 0))

    set_widget_font(month_caption, label_font)
    set_widget_font(month_dropdown, input_font)
    set_widget_font(year_caption, label_font)
    set_widget_font(year_dropdown, input_font)
    set_widget_font(preview_label, label_font)
    for label in weekday_labels:
        set_widget_font(label, label_font)
    for button in [prev_button, next_button, today_button, apply_button, close_button, *day_buttons]:
        set_widget_font(button, input_font)
    for widget in [prev_button, month_frame, month_caption, year_frame, year_caption, next_button, preview_label, today_button, apply_button, close_button, *weekday_labels]:
        set_widget_colors(widget, theme)

    displayed_month_text.trace_add("write", apply_month_year_selection)
    displayed_year_text.trace_add("write", apply_month_year_selection)

    refresh_calendar()
    dialog.focus_force()


def create_tabs_content(parent: tk.Widget, selected_theme, selected_font_family, selected_font_style, selected_font_size, selected_date_text, extra_context: dict[str, object] | None = None) -> dict[str, object]:
    """What: Build the seven tab frames and call each tab module's builder.

    Why included: Tabs share theme/font variables, queue state, preview helpers,
    and worker callbacks; this function is the composition boundary for them.
    Where used: Called once by build_ui() to fill the lower notebook section.
    Returns: Notebook/widgets/state registries used by main and cross-tab callbacks.
    """
    notebook = ttk.Notebook(parent, style="ContentTabs.TNotebook")
    tab_frames: list[tk.Frame] = []
    tab_labels: list[tk.Widget] = []
    tab_widgets: list[tk.Widget] = []
    tab_inputs: list[tk.Widget] = []
    display_state: dict[str, object] = {}
    shared_context = dict(extra_context or {})
    shared_context.update(
        {
            "selected_font_family": selected_font_family,
            "selected_font_style": selected_font_style,
            "selected_font_size": selected_font_size,
        }
    )

    def is_tab_active(tab_frame: tk.Widget) -> bool:
        """What: Compare a tab frame with the notebook's currently selected frame.

        Why included: Media previews and cache builders should pause when hidden.
        Where used: Passed to tab modules as a guard for timers/background work.
        Returns: True only when tab_frame is currently selected.
        """
        try:
            return notebook.nametowidget(notebook.select()) is tab_frame
        except (tk.TclError, KeyError):
            return False

    def apply_layout_style(widgets, label_widgets=None, input_widgets=None) -> None:
        """What: Apply the selected theme and role-specific fonts to tab widgets.

        Why included: Tab modules create their own controls but must follow the
        same app-wide appearance selections.
        Where used: Injected into each tab builder and reused for tab refreshes.
        Returns: None; updates the widget registry in place.
        """
        theme = get_theme_by_name(selected_theme.get())
        _, label_font, input_font = get_ui_font_set(selected_font_family.get(), selected_font_style.get(), selected_font_size.get())
        for widget in widgets:
            set_widget_colors(widget, theme)
        for widget in label_widgets or []:
            set_widget_font(widget, label_font)
        for widget in input_widgets or []:
            set_widget_font(widget, input_font)

    layout_welcome_state = {
        "send_to_display_var": tk.BooleanVar(value=False),
        "combine_text_var": tk.BooleanVar(value=False),
        "activate_scroll_var": tk.BooleanVar(value=False),
        "scroll_speed_var": tk.IntVar(value=7),
        "text_size_var": tk.IntVar(value=14),
        "selected_date_var": selected_date_text,
    }
    layout_preparation_state: dict[str, object] = {}
    layout_library_state: dict[str, object] = {}

    for tab_name in ("Display", "Welcome", "Library", "Preparation", "Bible", "Add Bible", "Translation"):
        tab_frame = tk.Frame(notebook)
        tab_frame.rowconfigure(0, weight=1)
        if tab_name in {"Display", "Welcome", "Library", "Preparation", "Translation"}:
            for column_index, column_weight in enumerate(CONTENT_TAB_COLUMN_WEIGHTS):
                tab_frame.columnconfigure(column_index, weight=column_weight, uniform="content_tab_columns")
        elif tab_name == "Bible":
            tab_frame.columnconfigure(0, weight=1)
        else:
            for column_index in range(3):
                tab_frame.columnconfigure(column_index, weight=1, uniform=f"{tab_name}_columns")
        notebook.add(tab_frame, text=tab_name)
        tab_frames.append(tab_frame)
        tab_widgets.append(tab_frame)

        if tab_name == "Display":
            build_display_tab(
                tab_frame,
                {
                    "tab_widgets": tab_widgets,
                    "tab_labels": tab_labels,
                    "tab_inputs": tab_inputs,
                    "apply_layout_style": apply_layout_style,
                    "display_state": display_state,
                    "preparation_state": layout_preparation_state,
                    "get_theme_by_name": get_theme_by_name,
                    "notebook": notebook,
                    "selected_theme": selected_theme,
                    **shared_context,
                },
            )
        elif tab_name == "Welcome":
            build_welcome_tab(
                tab_frame,
                {
                    "tab_widgets": tab_widgets,
                    "tab_labels": tab_labels,
                    "tab_inputs": tab_inputs,
                    "build_preview_placeholder": build_preview_placeholder,
                    "apply_layout_style": apply_layout_style,
                    "get_display_bounds": get_preferred_display_bounds,
                    "get_preview_size": lambda: get_preview_size_for_bounds(get_preferred_display_bounds()),
                    "handle_welcome_display_change": shared_context.get("handle_display_live_change"),
                    "display_state": display_state,
                    "display_font_family": shared_context.get("display_font_family"),
                    "display_font_style": shared_context.get("display_font_style"),
                    "welcome_state": layout_welcome_state,
                    **shared_context,
                },
            )
        elif tab_name == "Library":
            build_library_tab(
                tab_frame,
                {
                    "tab_widgets": tab_widgets,
                    "tab_labels": tab_labels,
                    "tab_inputs": tab_inputs,
                    "build_preview_placeholder": build_preview_placeholder,
                    "apply_layout_style": apply_layout_style,
                    "get_display_bounds": get_preferred_display_bounds,
                    "get_preview_size": lambda: get_preview_size_for_bounds(get_preferred_display_bounds()),
                    "library_state": layout_library_state,
                    "display_state": display_state,
                    **shared_context,
                },
            )
        elif tab_name == "Preparation":
            build_preparation_tab(
                tab_frame,
                {
                    "tab_widgets": tab_widgets,
                    "tab_labels": tab_labels,
                    "tab_inputs": tab_inputs,
                    "build_preview_placeholder": build_preview_placeholder,
                    "apply_layout_style": apply_layout_style,
                    "get_display_bounds": get_preferred_display_bounds,
                    "get_preview_size": lambda: get_preview_size_for_bounds(get_preferred_display_bounds()),
                    "preparation_state": layout_preparation_state,
                    "display_state": display_state,
                    "notebook": notebook,
                    "display_font_family": shared_context.get("display_font_family"),
                    "display_font_style": shared_context.get("display_font_style"),
                },
            )
        elif tab_name == "Bible":
            build_bible_reader_tab(
                tab_frame,
                {
                    "tab_widgets": tab_widgets,
                    "tab_labels": tab_labels,
                    "tab_inputs": tab_inputs,
                    "preparation_state": layout_preparation_state,
                    "apply_layout_style": apply_layout_style,
                    "display_state": display_state,
                    **shared_context,
                },
            )
        elif tab_name == "Add Bible":
            build_bible_tab(
                tab_frame,
                {
                    "tab_widgets": tab_widgets,
                    "tab_labels": tab_labels,
                    "tab_inputs": tab_inputs,
                    "apply_layout_style": apply_layout_style,
                    "get_app_base_path": get_app_base_path,
                    **shared_context,
                },
            )
        elif tab_name == "Translation":
            build_translation_tab(
                tab_frame,
                {
                    "tab_widgets": tab_widgets,
                    "tab_labels": tab_labels,
                    "tab_inputs": tab_inputs,
                    "apply_layout_style": apply_layout_style,
                    "get_app_base_path": get_app_base_path,
                    "notebook": notebook,
                    "is_tab_active": is_tab_active,
                    **shared_context,
                },
            )

    notebook.pack(expand=True, fill="both")
    return {
        "notebook": notebook,
        "frames": tab_frames,
        "display_state": display_state,
        "labels": tab_labels,
        "widgets": tab_widgets,
        "inputs": tab_inputs,
        "welcome_state": layout_welcome_state,
        "preparation_state": layout_preparation_state,
        "library_state": layout_library_state,
    }


def apply_theme(root: tk.Tk, container: tk.Frame, top_sections: list[dict], bottom_section: dict, menu_widgets: list[tk.Menu], theme_name: str) -> None:
    """What: Apply one named palette to the root, shared panels, controls, and tabs.

    Why included: Theme changes affect classic Tk widgets, menus, and ttk styles,
    each of which needs its own compatible styling path.
    Where used: Startup, Theme menu changes, and service recall.
    Returns: None; widgets are reconfigured in place.
    """
    theme = get_theme_by_name(theme_name)
    root.configure(bg=theme["window_bg"])
    container.configure(bg=theme["window_bg"])
    for section in top_sections:
        section["frame"].configure(bg=theme["panel_bg"], fg=theme["text"], highlightbackground=theme["border"])
        for widget in section.get("widgets", []):
            set_widget_colors(widget, theme)
    bottom_section["frame"].configure(bg=theme["panel_bg"], fg=theme["text"], highlightbackground=theme["border"])
    bottom_section["body"].configure(bg=theme["panel_bg"])
    for widget in bottom_section.get("widgets", []):
        set_widget_colors(widget, theme)
    for menu_widget in menu_widgets:
        try:
            menu_widget.configure(background=theme["menu_bg"], foreground=theme["menu_fg"], activebackground=theme["accent"], activeforeground=theme["menu_fg"])
        except tk.TclError:
            pass
    configure_tabs_notebook_style(bottom_section["notebook"], theme, bottom_section["selected_font_family"].get(), bottom_section["selected_font_style"].get(), bottom_section["selected_font_size"].get())
    for child in root.winfo_children():
        if isinstance(child, tk.Toplevel):
            try:
                child.configure(bg=theme["panel_bg"])
            except tk.TclError:
                pass
    try:
        root.option_add("*Toplevel.background", theme["panel_bg"])
    except tk.TclError:
        pass


def apply_font_settings(ui_parts: dict[str, object], family: str, style_name: str, size: int) -> None:
    """What: Recreate header/label/input fonts and apply them to registered widgets.

    Why included: Font choices can change at runtime and affect both top panels
    and content-tab labels/inputs.
    Where used: Font menu selection and saved-service display-setting recall.
    Returns: None; updates widgets and notebook style in place.
    """
    header_font, label_font, input_font = get_ui_font_set(family, style_name, size)
    for section in ui_parts["top_sections"]:
        try:
            section["frame"].configure(font=header_font)
        except tk.TclError:
            pass
        for widget in section.get("label_widgets", []):
            set_widget_font(widget, label_font)
        for widget in section.get("input_widgets", []):
            set_widget_font(widget, input_font)
    bottom_section = ui_parts["bottom_section"]
    try:
        bottom_section["frame"].configure(font=header_font)
    except tk.TclError:
        pass
    for widget in bottom_section.get("label_widgets", []):
        set_widget_font(widget, label_font)
    for widget in bottom_section.get("input_widgets", []):
        set_widget_font(widget, input_font)
    configure_tabs_notebook_style(bottom_section["notebook"], get_theme_by_name(ui_parts["selected_theme"].get()), family, style_name, size)


def clear_background_cache() -> None:
    """What: Remove generated files in background_cache, leaving source media intact.

    Why included: Cached/converted previews can be regenerated and otherwise
    accumulate disk space across long-running use.
    Where used: Main-window shutdown.
    Returns: None; cache deletion failures are ignored during cleanup.
    """
    cache_directory = get_app_base_path() / "background_cache"
    if not cache_directory.exists():
        return
    for child in cache_directory.iterdir():
        if not child.is_file():
            continue
        try:
            child.unlink()
        except OSError:
            pass


def get_window_work_area(window: tk.Tk) -> tuple[int, int, int, int] | None:
    """What: Query the nearest monitor's usable work area as x/y/width/height.

    Why included: Main-window minimum size and placement must avoid taskbars.
    Where used: apply_startup_window_bounds() after the window handle is created.
    Returns: Work-area tuple from MONITORINFO.rcWork, or None on Win32/API failure.
    """
    user32 = getattr(ctypes, "windll", None)
    if user32 is None:
        return None
    try:
        monitor_from_window = user32.user32.MonitorFromWindow
        get_monitor_info = user32.user32.GetMonitorInfoW
    except AttributeError:
        return None
    monitor_handle = monitor_from_window(window.winfo_id(), MONITOR_DEFAULTTONEAREST)
    if not monitor_handle:
        return None
    monitor_info = MONITORINFO()
    monitor_info.cbSize = ctypes.sizeof(MONITORINFO)
    if not get_monitor_info(monitor_handle, ctypes.byref(monitor_info)):
        return None
    work_area = monitor_info.rcWork
    return work_area.left, work_area.top, work_area.right - work_area.left, work_area.bottom - work_area.top


def ensure_process_dpi_awareness() -> None:
    """What: Enable per-monitor DPI awareness before creating Tk windows.

    Why included: Windows otherwise reports virtualized bounds that misplace the
    projector on high-DPI or mixed-DPI monitor setups.
    Where used: build_ui() before tk.Tk() and monitor enumeration.
    Returns: None; falls back to the older system DPI API when needed.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_DISPLAY_BOUNDS_CACHE: dict[str, object] = {"at": 0.0, "bounds": None}


def get_preferred_display_bounds() -> dict[str, int]:
    """What: Choose the first non-primary monitor, or the primary when alone.

    Why included: OBS projector and preview sizes must target the presentation
    display rather than assuming the Windows primary monitor.
    Where used: mirror/controller callbacks and preview-size calculations.
    Returns: x/y/width/height dictionary; uses 1920x1080 if Win32 lookup fails.
    """
    cached_bounds = _DISPLAY_BOUNDS_CACHE.get("bounds")
    if isinstance(cached_bounds, dict) and time.monotonic() - float(_DISPLAY_BOUNDS_CACHE.get("at") or 0.0) < 2.0:
        # Scroll/GIF/video preview frames re-query bounds; a short cache keeps
        # per-frame monitor enumeration off the render paths.
        return dict(cached_bounds)
    bounds = _enumerate_preferred_display_bounds()
    _DISPLAY_BOUNDS_CACHE["bounds"] = dict(bounds)
    _DISPLAY_BOUNDS_CACHE["at"] = time.monotonic()
    return bounds


def _enumerate_preferred_display_bounds() -> dict[str, int]:
    """What: Enumerate monitors through Win32 and pick the presentation display.

    Why included: The uncached enumeration must stay separate from its cache policy.
    Where used: get_preferred_display_bounds() on cache misses.
    Returns: x/y/width/height dictionary; uses 1920x1080 if Win32 lookup fails.
    """
    user32 = getattr(ctypes, "windll", None)
    if user32 is None:
        return {"x": 0, "y": 0, "width": 1920, "height": 1080}
    monitors: list[dict[str, int | bool]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_long)

    def enum_proc(hmonitor, _hdc, _rect, _data):
        """What: Record full bounds and primary status for one enumerated monitor.

        Why included: EnumDisplayMonitors supplies only a handle; GetMonitorInfoW
        provides the work and physical-screen rectangles needed by the app.
        Where used: Registered as the callback in get_preferred_display_bounds().
        Returns: 1 to continue Win32 enumeration.
        """
        monitor_info = MONITORINFO()
        monitor_info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.user32.GetMonitorInfoW(hmonitor, ctypes.byref(monitor_info)):
            bounds = monitor_info.rcMonitor
            monitors.append(
                {
                    "x": int(bounds.left),
                    "y": int(bounds.top),
                    "width": int(bounds.right - bounds.left),
                    "height": int(bounds.bottom - bounds.top),
                    "is_primary": bool(monitor_info.dwFlags & MONITORINFOF_PRIMARY),
                }
            )
        return 1

    try:
        user32.user32.EnumDisplayMonitors(None, None, callback_type(enum_proc), 0)
    except Exception:
        monitors = []
    if not monitors:
        return {"x": 0, "y": 0, "width": 1920, "height": 1080}
    for monitor in monitors:
        if not monitor["is_primary"]:
            return {"x": int(monitor["x"]), "y": int(monitor["y"]), "width": int(monitor["width"]), "height": int(monitor["height"])}
    primary = monitors[0]
    return {"x": int(primary["x"]), "y": int(primary["y"]), "width": int(primary["width"]), "height": int(primary["height"])}


def get_preview_size_for_bounds(display_bounds: dict[str, int]) -> tuple[int, int]:
    """What: Fit the preview within its fixed maximum while matching monitor ratio.

    Why included: The in-app preview should resemble the secondary output without
    stretching or changing its layout when display resolutions differ.
    Where used: Welcome, Library, Preparation, and Mirror preview callbacks.
    Returns: A bounded preview width/height pair.
    """
    width = max(1, int(display_bounds.get("width") or 1920))
    height = max(1, int(display_bounds.get("height") or 1080))
    aspect_ratio = width / max(1, height)
    preview_width = MIRROR_PREVIEW_WIDTH
    preview_height = int((preview_width / max(0.1, aspect_ratio)) + 0.5)
    if preview_height > MIRROR_PREVIEW_HEIGHT:
        preview_height = MIRROR_PREVIEW_HEIGHT
        preview_width = int((preview_height * aspect_ratio) + 0.5)
    return max(180, int(preview_width)), max(100, int(preview_height))


def serialize_overlay_font(font_value) -> dict | None:
    """What: Convert (family, size, style) into a JSON-safe font dictionary.

    Why included: Overlay state is sent to OBS as JSON, which cannot serialize
    Tk/Pillow font objects or tuples reliably across the process boundary.
    Where used: MirrorDisplayController while building secondary-screen payloads.
    Returns: A family/size/style dictionary, or None for invalid input.
    """
    if not isinstance(font_value, tuple) or len(font_value) < 2:
        return None
    return {
        "family": str(font_value[0]),
        "size": abs(int(font_value[1])),
        "style": str(font_value[2]) if len(font_value) > 2 else "normal",
    }


def resolve_background_path(background_path: Path) -> Path:
    """What: Resolve absolute/app-relative media paths and substitute the default.

    Why included: Saved queue paths may be missing or relative to the app folder,
    and the default sentinel must follow the church's current branding choice.
    Where used: Mirror rendering and hot-background preloading.
    Returns: An existing path when available, otherwise the branded default image.
    """
    default_background_path = get_default_background_path()
    if not str(background_path).strip() or Path(background_path) == DEFAULT_BACKGROUND_PATH:
        return default_background_path
    return resolve_background_file(background_path, get_app_base_path(), get_app_data_path()) or default_background_path


def build_hot_background_paths(background_paths: list[Path | str | None]) -> list[str]:
    """What: Filter and normalize queued background references to existing paths.

    Why included: The secondary renderer preloads likely upcoming media to reduce
    switching delay; invalid/empty references should not enter that list.
    Where used: sync_secondary_hot_backgrounds() in build_ui().
    Returns: Resolved path strings suitable for the mirror controller.
    """
    resolved_paths: list[str] = []
    for item in background_paths:
        if item is None:
            continue
        resolved = resolve_background_path(Path(str(item)))
        if resolved.exists():
            resolved_paths.append(str(resolved))
    return resolved_paths


def apply_startup_window_bounds(window: tk.Tk) -> None:
    """What: Set a usable minimum size and maximize the app on its current monitor.

    Why included: The three-column Tk layout needs a minimum viewport and should
    avoid taskbars when Windows exposes a usable work area.
    Where used: build_ui() immediately after creating the root window.
    Returns: None; falls back to Tk screen dimensions if Win32 lookup fails.
    """
    window.update_idletasks()
    work_area = get_window_work_area(window)
    if work_area is not None:
        _, _, width, height = work_area
        window.minsize(min(1280, width), min(760, height))
    else:
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        window.minsize(min(1280, screen_width), min(760, screen_height))
    try:
        window.state("zoomed")
    except tk.TclError:
        if work_area is not None:
            left, top, width, height = work_area
            window.geometry(f"{width}x{height}+{left}+{top}")
        else:
            window.geometry(f"{window.winfo_screenwidth()}x{window.winfo_screenheight()}+0+0")


def apply_application_icon(window: tk.Tk) -> None:
    """What: Load the installation-selected or bundled ICO file into a Tk window.

    Why included: Keep the taskbar/window identity consistent with the end user's installer choice.
    Where used: build_ui() during root-window initialization.
    Returns: None; silently skips a missing or unsupported icon.
    """
    icon_path = get_app_icon_path()
    if not icon_path.exists():
        return
    try:
        window.iconbitmap(default=str(icon_path))
    except tk.TclError:
        pass


def require_terms_acceptance(parent: tk.Misc) -> bool:
    """What: Show the Terms and Conditions in a modal dialog and require explicit acceptance.

    Why included: The terms must be approved for every installation and again for every update.
    Where used: install_windows_update() before an update package is prepared.
    Returns: True when the user accepts; False when declined or the dialog is closed.
    """
    terms_path = get_app_base_path() / "TERMS_AND_CONDITIONS.txt"
    try:
        terms_text = licensing.fill_price_placeholders(terms_path.read_text(encoding="utf-8"))
    except OSError:
        terms_text = ("TERMS_AND_CONDITIONS.txt was not found beside the app.\n\n"
            "By choosing \"I accept\" you accept the distributor's Terms and Conditions for installing this update.")
    dialog = tk.Toplevel(parent)
    dialog.title("Terms and Conditions")
    dialog.geometry("640x480")
    dialog.transient(parent.winfo_toplevel())
    dialog.grab_set()
    apply_application_icon(dialog)
    result = {"accepted": False}

    def accept() -> None:
        """What: Record acceptance and close the dialog.

        Why included: wait_window() needs a flag distinguishing acceptance from dismissal.
        Where used: The I-accept button below.
        Returns: None.
        """
        result["accepted"] = True
        dialog.destroy()

    header = ttk.Label(dialog, text="Please read and accept the Terms and Conditions to continue.", padding=(12, 10))
    header.pack(side="top", anchor="w")
    buttons = ttk.Frame(dialog, padding=(12, 8))
    buttons.pack(side="bottom", fill="x")
    ttk.Button(buttons, text="I accept", command=accept).pack(side="right", padx=(8, 0))
    ttk.Button(buttons, text="Decline", command=dialog.destroy).pack(side="right")
    body = ttk.Frame(dialog, padding=(12, 0))
    body.pack(side="top", fill="both", expand=True)
    text = tk.Text(body, wrap="word")
    scrollbar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scrollbar.set)
    text.insert("1.0", terms_text)
    text.configure(state="disabled")
    scrollbar.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    parent.wait_window(dialog)
    return result["accepted"]


def get_release_version() -> str:
    """What: Read the installed release version from the manifest beside the app.

    Why included: Users and support need to see which packaged release is running.
    Where used: Help menu version entry in build_ui().
    Returns: The packaged release version, or a development label for source runs.
    """
    manifest_path = get_app_base_path() / "release.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "development build"
    version = str(manifest.get("version") or "").strip()
    return version or "unknown"


def show_text_document(parent: tk.Misc, title: str, document_path: Path) -> None:
    """What: Open a read-only scrollable window showing a bundled text document.

    Why included: The Help menu must present the terms and operating manual in-app.
    Where used: Help menu commands in build_ui().
    Returns: None; the window closes through its own controls.
    """
    try:
        content = licensing.fill_price_placeholders(document_path.read_text(encoding="utf-8"))
    except OSError:
        content = f"{document_path.name} was not found beside the app."
    window = tk.Toplevel(parent)
    window.title(title)
    window.geometry("680x520")
    window.transient(parent.winfo_toplevel())
    apply_application_icon(window)
    buttons = ttk.Frame(window, padding=(12, 8))
    buttons.pack(side="bottom", fill="x")
    ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")
    body = ttk.Frame(window, padding=(12, 10))
    body.pack(side="top", fill="both", expand=True)
    text = tk.Text(body, wrap="word")
    scrollbar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scrollbar.set)
    text.insert("1.0", content)
    text.configure(state="disabled")
    scrollbar.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)


def build_ui() -> tk.Tk:
    """What: Initialize storage, create the root, wire shared services, and build tabs.

    Why included: This is the composition root; tabs need shared settings, queue,
    worker, monitor, and mirror callbacks assembled in one place.
    Where used: The module entry point calls it before root.mainloop().
    Returns: The configured Tk root window.
    """
    startup_issues: list[str] = []
    if getattr(sys, "frozen", False):
        try:
            migrate_legacy_backgrounds(get_app_base_path(), get_app_data_path())
        except Exception as error:
            log_exception("main.background_migration", error)
            startup_issues.append(f"Background migration could not complete; original files were retained: {error}")
        try:
            apply_optional_update_data(get_app_base_path(), get_app_data_path())
        except Exception as error:
            log_exception("main.optional_update_data", error)
            startup_issues.append(f"Optional update data could not be merged: {error}")
    try:
        saved_settings = load_ui_settings()
    except Exception as error:
        log_exception("main.load_ui_settings", error)
        saved_settings = {}
        startup_issues.append("UI settings could not be loaded and were reset to defaults.")
    try:
        startup_issues.extend(validate_persistence_storage())
    except Exception as error:
        log_exception("main.validate_persistence_storage", error)
        startup_issues.append("Persistence storage validation failed during startup.")
    try:
        reset_active_display_queue_file()
    except Exception as error:
        log_exception("main.reset_active_display_queue_file", error)
        startup_issues.append("Display queue could not be reset during startup.")

    ensure_process_dpi_awareness()
    root = tk.Tk()

    def compose_window_title() -> str:
        """What: Build the window title from the product name and the church's own name.

        Why included: Each church brands its installation; the title must follow edits immediately.
        Where used: Startup and the Help menu's church name and background editor.
        Returns: Title with church name, license state, and a TEST COPY marker when applicable.
        """
        church_name = str(get_branding_settings().get("church_name") or "").strip()
        display = f"{PRODUCT_NAME} - {church_name}" if church_name else PRODUCT_NAME
        try:
            license_status = licensing.get_license_status()
        except Exception as error:
            log_exception("main.license_status", error)
            license_status = None
        if license_status is not None and license_status.state == "trial":
            display += f" [Free trial: {license_status.days_left} day(s) left]"
        elif license_status is not None and license_status.state == "unlicensed":
            display += " [NOT LICENSED]"
        return f"{display} [TEST COPY]" if (get_app_base_path() / ".test-installation").is_file() else display

    root.title(compose_window_title())
    apply_application_icon(root)
    root.minsize(1280, 760)
    apply_startup_window_bounds(root)
    spell_checker = SpellCheckController(root)
    ui_callback_queue: queue.SimpleQueue[object] = queue.SimpleQueue()
    background_task_lock = threading.Lock()
    active_background_tasks: dict[str, threading.Thread] = {}
    app_state = {"closing": False}

    def get_music_licenses() -> dict[str, object]:
        """Read the shared CCLI and OneLicense numbers for Library and Display.

        What: Loads normalized license settings from the configured app data folder.
        Why included: The creator may update the registered numbers after installation.
        Where used: Library previews, live song attribution, and the settings dialog.
        Returns: Current settings mapping with ccli and onelicense values.
        """
        return load_music_license_settings()

    def report_tk_callback_exception(exc_type, exc_value, exc_traceback) -> None:
        """What: Log an uncaught Tk callback exception and show its message.

        Why included: Tk otherwise reports callback failures outside the app's
        managed error log, making runtime problems harder to diagnose.
        Where used: Installed as root.report_callback_exception during startup.
        Returns: None; reports through the error log and a message dialog.
        """
        error = exc_value if isinstance(exc_value, BaseException) else Exception(str(exc_value))
        log_exception("main.tk_callback", error, traceback_obj=exc_traceback)
        messagebox.showerror("Application Error", f"An unexpected error occurred.\n\n{error}", parent=root)

    root.report_callback_exception = report_tk_callback_exception

    def report_worker_thread_exception(args) -> None:
        """What: Log an uncaught exception from any worker thread to the error log.

        Why included: Daemon render/push threads otherwise die silently, hiding the
        cause of stalled previews or output.
        Where used: Installed as threading.excepthook during startup.
        Returns: None; never raises from the hook itself.
        """
        try:
            thread_name = getattr(args.thread, "name", None) or "unknown"
            error = args.exc_value if isinstance(args.exc_value, BaseException) else RuntimeError(str(args.exc_type))
            log_exception(f"thread.{thread_name}", error, traceback_obj=args.exc_traceback)
        except Exception:
            pass

    threading.excepthook = report_worker_thread_exception

    def drain_ui_callback_queue() -> None:
        """What: Drain queued completion/progress callbacks on Tk's own thread.

        Why included: Worker callbacks may update widgets only after being
        marshalled back to Tk; uncaught callback errors use the app error handler.
        Where used: Repeated by root.after() every 15 ms until the window closes.
        Returns: None; schedules the next poll while Tk remains available.
        """
        try:
            while True:
                # Drain all currently queued worker callbacks in one UI-thread pass.
                callback = ui_callback_queue.get_nowait()
                if callable(callback):
                    try:
                        callback()
                    except Exception as error:
                        report_tk_callback_exception(type(error), error, error.__traceback__)
        except queue.Empty:
            pass
        try:
            root.after(15, drain_ui_callback_queue)
        except tk.TclError:
            return

    root.after(15, drain_ui_callback_queue)

    try:
        startup_issues.extend(run_startup_diagnostics())
    except Exception as error:
        log_exception("main.startup_diagnostics", error)
        startup_issues.append("Startup diagnostics could not complete.")
    if startup_issues:
        root.after(
            0,
            lambda issues=list(startup_issues): messagebox.showwarning(
                "Startup Diagnostics",
                "The application started with runtime warnings:\n\n" + "\n".join(issues[:5]),
                parent=root,
            ),
        )

    def schedule_on_ui_thread(callback) -> None:
        """What: Put a callable into the queue drained by drain_ui_callback_queue().

        Why included: Tk is single-threaded; worker code must not directly mutate
        a widget or trigger Tcl calls from a background thread.
        Where used: Worker success, error, and progress handling.
        Returns: None; ignores new work after app shutdown starts.
        """
        if app_state["closing"]:
            return
        ui_callback_queue.put(callback)

    licensing.start_background_refresh(on_change=lambda: schedule_on_ui_thread(lambda: root.title(compose_window_title())))

    def start_background_task(task_name: str, worker, on_success=None, on_error=None, on_progress=None) -> None:
        """Coordinate one background task and return all callbacks to Tk safely.

        What: Start worker on a daemon thread and forward result/error/progress
        through ui_callback_queue. Duplicate task names are refused while active.
        Why included: I/O and database work must not freeze Tk, and Tk widgets may
        only be changed by the UI thread.
        Where used: Service save/recall, history loading, and tab background jobs.
        Returns: None; completion is delivered through the optional callbacks.
        """
        with background_task_lock:
            existing_task = active_background_tasks.get(task_name)
            if existing_task is not None and existing_task.is_alive():
                log_error(f"main.{task_name}", "Skipped duplicate background task launch.")
                return

        def run_worker() -> None:
            """What: Run the worker, handle exceptions, and remove its active-task entry.

            Why included: One wrapper centralizes cleanup and guarantees UI callbacks
            use the main thread even when the worker fails.
            Where used: Launched as a daemon thread by start_background_task().
            Returns: None; sends outputs through queued callbacks.
            """
            def report_progress(*args, **kwargs) -> None:
                """Forward a worker progress event to its optional UI callback.

                What: Captures positional and keyword arguments and queues a closure
                that invokes on_progress on Tk's event thread.
                Why included: Worker progress handlers can update widgets only through
                the same thread-safe handoff as task results.
                Where used: Passed to workers by start_background_task() when an
                on_progress callback is supplied.
                Returns: None; queues nothing when no progress callback is callable.
                """
                if callable(on_progress):
                    schedule_on_ui_thread(lambda args=args, kwargs=kwargs: on_progress(*args, **kwargs))

            try:
                if callable(on_progress):
                    result = worker(report_progress)
                else:
                    result = worker()
            except Exception as error:
                log_exception(f"main.{task_name}", error)
                if callable(on_error):
                    schedule_on_ui_thread(lambda error=error: on_error(error))
                return
            finally:
                with background_task_lock:
                    active_background_tasks.pop(task_name, None)
            if callable(on_success) and not app_state["closing"]:
                schedule_on_ui_thread(lambda result=result: on_success(result))

        background_thread = threading.Thread(target=run_worker, name=task_name, daemon=True)
        with background_task_lock:
            active_background_tasks[task_name] = background_thread
        background_thread.start()

    def refresh_music_license_outputs() -> None:
        """Refresh the current song preview and live corner after number edits.

        What: Repaints Library preview and asks the mirror to resolve fresh license numbers.
        Why included: Changing a license number should update already-live song attribution.
        Where used: Save action in the Music Licenses dialog.
        Returns: None; preview and OBS refresh are scheduled by their owners.
        """
        mirror_controller.refresh_music_license_attribution()
        library_state = tabs_content.get("library_state") if isinstance(tabs_content, dict) else None
        refresh_preview = library_state.get("refresh_preview") if isinstance(library_state, dict) else None
        if callable(refresh_preview):
            refresh_preview()

    def open_music_license_settings(first_run: bool = False) -> None:
        """Edit shared CCLI and OneLicense numbers, or defer first-run setup.

        What: Shows two license-number fields and saves them in shared app data.
        Why included: Live music attribution requires each church's registered numbers.
        Where used: First packaged launch and the Music Licenses menu command.
        Returns: None; the optional first-run prompt can be completed later from the menu.
        """
        current_settings = get_music_licenses()
        dialog = tk.Toplevel(root)
        dialog.title("Music Licenses")
        dialog.transient(root)
        dialog.resizable(False, False)
        dialog.columnconfigure(1, weight=1)
        ttk.Label(
            dialog,
            text="Enter your church's music license numbers. You can leave them blank and add them later.",
            wraplength=440,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(16, 12))
        ccli_var = tk.StringVar(dialog, value=str(current_settings.get("ccli") or ""))
        onelicense_var = tk.StringVar(dialog, value=str(current_settings.get("onelicense") or ""))
        ttk.Label(dialog, text="CCLI License Number").grid(row=1, column=0, sticky="w", padx=(16, 12), pady=6)
        ccli_entry = ttk.Entry(dialog, textvariable=ccli_var, width=34)
        ccli_entry.grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=6)
        ttk.Label(dialog, text="OneLicense Number").grid(row=2, column=0, sticky="w", padx=(16, 12), pady=6)
        onelicense_entry = ttk.Entry(dialog, textvariable=onelicense_var, width=34)
        onelicense_entry.grid(row=2, column=1, sticky="ew", padx=(0, 16), pady=6)
        buttons = ttk.Frame(dialog)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", padx=16, pady=16)

        def finish_later() -> None:
            """Remember that the first-run question was deferred without changing numbers.

            What: Marks the initial prompt complete and closes it.
            Why included: Missing license details must not block using the application.
            Where used: Add Later button and first-run dialog close button.
            Returns: None; the Music Licenses menu remains available.
            """
            if first_run:
                save_music_license_settings({**current_settings, "initial_prompt_completed": True})
            dialog.destroy()

        def save_numbers() -> None:
            """Save both shared license numbers and refresh current song attribution.

            What: Persists trimmed values and completes first-run setup.
            Why included: Library and Display need a consistent source for live notices.
            Where used: Save button in the Music Licenses dialog.
            Returns: None; closes the dialog and refreshes preview/output.
            """
            save_music_license_settings({
                "ccli": ccli_var.get(),
                "onelicense": onelicense_var.get(),
                "initial_prompt_completed": True,
            })
            dialog.destroy()
            refresh_music_license_outputs()

        ttk.Button(buttons, text="Save", command=save_numbers).pack(side="right")
        if first_run:
            ttk.Button(buttons, text="Add Later", command=finish_later).pack(side="right", padx=(0, 8))
            dialog.protocol("WM_DELETE_WINDOW", finish_later)
        else:
            ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=(0, 8))
            dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        ccli_entry.focus_set()

    update_state = {"busy": False}
    data_transfer_state = {"busy": False}

    def choose_database_selection(title: str, available: tuple[str, ...], include_modes: bool = False):
        """What: Collect checked database keys and optional per-database import modes.

        Why included: Transfers must be selectable and imports need explicit merge/replace decisions.
        Where used: Data menu database ZIP export and import commands.
        Returns: Selected keys, optionally paired with their chosen modes, or None on cancel.
        """
        dialog = tk.Toplevel(root)
        dialog.title(title)
        dialog.transient(root)
        dialog.resizable(True, True)
        dialog.minsize(420, 280)
        dialog.geometry("720x420")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(dialog, text="Select databases and choose how each import is handled:", wraplength=660).grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(16, 8))
        choices = ttk.Frame(dialog)
        choices.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=16)
        choices.columnconfigure(0, weight=1)
        if include_modes:
            ttk.Label(choices, text="Database").grid(row=0, column=0, sticky="w", padx=(4, 12), pady=(4, 8))
            ttk.Label(choices, text="Import mode").grid(row=0, column=1, sticky="w", padx=4, pady=(4, 8))
        variables: dict[str, tk.BooleanVar] = {}
        mode_variables: dict[str, tk.StringVar] = {}
        row_offset = 1 if include_modes else 0
        for index, key in enumerate(available):
            variable = tk.BooleanVar(value=True)
            variables[key] = variable
            row = index + row_offset
            ttk.Checkbutton(choices, text=TRANSFER_DATABASES[key][1], variable=variable).grid(row=row, column=0, sticky="w", pady=5)
            if include_modes:
                mode_variable = tk.StringVar(value="Merge (add missing)")
                mode_variables[key] = mode_variable
                ttk.Combobox(choices, textvariable=mode_variable, values=("Merge (add missing)", "Replace"), state="readonly", width=24).grid(row=row, column=1, sticky="w", padx=4, pady=4)
        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, sticky="ew", padx=16, pady=16)
        result: dict[str, object] = {"selected": None}

        def select_all(value: bool) -> None:
            """What: Set every database checkbox to the same selected state.

            Why included: Multi-database transfers need efficient select-all/none controls.
            Where used: Buttons in the database selection dialog.
            Returns: None; updates the dialog's checkbox variables.
            """
            for variable in variables.values():
                variable.set(value)

        def accept() -> None:
            """What: Validate the database selection and return chosen keys/modes.

            Why included: Transfers must have at least one selected database.
            Where used: Continue button in the database selection dialog.
            Returns: None; stores the modal result and closes the dialog when valid.
            """
            selected = tuple(key for key in available if variables[key].get())
            if not selected:
                messagebox.showwarning(title, "Select at least one database.", parent=dialog)
                return
            if include_modes:
                modes = {key: ("replace" if mode_variables[key].get() == "Replace" else "merge") for key in selected}
                result["selected"] = (selected, modes)
            else:
                result["selected"] = selected
            dialog.destroy()

        ttk.Button(buttons, text="Select All", command=lambda: select_all(True)).pack(side="left")
        ttk.Button(buttons, text="Select None", command=lambda: select_all(False)).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Continue", command=accept).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        root.wait_window(dialog)
        return result["selected"]

    def choose_service_day_selection(title: str, service_dates: tuple[str, ...], include_mode: bool = False):
        """What: Collect one or more saved service dates and an optional import mode.

        Why included: Users need to transfer only the date-specific service records they choose.
        Where used: Data menu service-day ZIP export and import commands.
        Returns: Selected date strings, optionally paired with merge/replace mode, or None on cancel.
        """
        dialog = tk.Toplevel(root)
        dialog.title(title)
        dialog.transient(root)
        dialog.resizable(True, True)
        dialog.minsize(440, 300)
        dialog.geometry("560x420")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(dialog, text="Select the saved service dates to transfer:", wraplength=510).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))
        body = ttk.Frame(dialog)
        body.grid(row=1, column=0, sticky="nsew", padx=16)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        canvas = tk.Canvas(body, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        list_frame = ttk.Frame(canvas)
        list_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        variables: dict[str, tk.BooleanVar] = {}
        for index, service_date in enumerate(service_dates):
            variable = tk.BooleanVar(value=True)
            variables[service_date] = variable
            ttk.Checkbutton(list_frame, text=service_date, variable=variable).grid(row=index, column=0, sticky="w", pady=3)
        mode = tk.StringVar(value="Merge")
        if include_mode:
            mode_frame = ttk.Frame(dialog)
            mode_frame.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 0))
            ttk.Label(mode_frame, text="Import behavior for selected dates:").pack(side="left")
            ttk.Combobox(mode_frame, textvariable=mode, values=("Merge", "Replace"), state="readonly", width=14).pack(side="left", padx=(8, 0))
        buttons = ttk.Frame(dialog)
        buttons.grid(row=3, column=0, sticky="ew", padx=16, pady=16)
        result: dict[str, object] = {"selection": None}

        def set_all(value: bool) -> None:
            """What: Toggle all service-date checkboxes together.

            Why included: Multi-day transfers need quick select-all/none controls.
            Where used: Buttons in the service-date selection dialog.
            Returns: None; updates the dialog's date variables.
            """
            for variable in variables.values():
                variable.set(value)

        def accept() -> None:
            """What: Validate selected service dates and capture the chosen mode.

            Why included: Day transfers must not run with an empty date selection.
            Where used: Continue button in the service-date selection dialog.
            Returns: None; stores the modal result and closes the dialog when valid.
            """
            selected = tuple(day for day in service_dates if variables[day].get())
            if not selected:
                messagebox.showwarning(title, "Select at least one service date.", parent=dialog)
                return
            result["selection"] = (selected, mode.get().casefold()) if include_mode else selected
            dialog.destroy()

        ttk.Button(buttons, text="Select All", command=lambda: set_all(True)).pack(side="left")
        ttk.Button(buttons, text="Select None", command=lambda: set_all(False)).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Continue", command=accept).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        root.wait_window(dialog)
        return result["selection"]

    def open_background_folder() -> None:
        """What: Open the shared background folder in File Explorer.

        Why included: Operators need a direct way to inspect shared app data and backups.
        Where used: Data menu Open Shared Data Folder command.
        Returns: None; reports an Explorer launch error to the user.
        """
        data_directory = get_app_data_path() / "Backgrounds"
        data_directory.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(data_directory))
        except OSError as error:
            messagebox.showerror("Open Background Folder", str(error), parent=root)

    def begin_data_transfer(title: str, task_name: str, worker, finished_message: str) -> None:
        """What: Run a transfer worker with progress, completion, and error feedback.

        Why included: ZIP and SQLite work must not block Tk, and transfer stages need visible progress.
        Where used: Database and service-day import/export commands in the Data menu.
        Returns: None; starts a background task and manages its progress window.
        """
        if data_transfer_state["busy"] or update_state["busy"]:
            return
        data_transfer_state["busy"] = True
        for index in (0, 2, 3, 5, 6):
            data_directory_menu.entryconfigure(index, state="disabled")
        window = tk.Toplevel(root)
        window.title(title)
        window.geometry("680x300")
        window.minsize(500, 240)
        window.columnconfigure(0, weight=1)
        status = tk.StringVar(value="Preparing database transfer...")
        ttk.Label(window, textvariable=status, wraplength=620).grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        progress = ttk.Progressbar(window, mode="determinate", maximum=100)
        progress.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        output = tk.Text(window, height=8, wrap="word", state="disabled")
        output.grid(row=2, column=0, sticky="nsew", padx=14)
        window.rowconfigure(2, weight=1)
        buttons = ttk.Frame(window)
        buttons.grid(row=3, column=0, sticky="ew", padx=14, pady=14)
        buttons.columnconfigure(0, weight=1)
        close_button = ttk.Button(buttons, text="Minimize", command=window.iconify)
        close_button.grid(row=0, column=1)
        window.protocol("WM_DELETE_WINDOW", window.iconify)

        def append_line(line: str) -> None:
            """What: Append one progress or result line to the transfer window.

            Why included: The visible log gives users detail beyond the progress bar.
            Where used: Transfer progress, success, and failure callbacks.
            Returns: None; updates the read-only output widget.
            """
            output.configure(state="normal")
            output.insert("end", line + "\n")
            output.see("end")
            output.configure(state="disabled")

        def show_progress(stage: str, key: str, completed: int, total: int) -> None:
            """What: Render the current transfer stage and fractional progress.

            Why included: Large imports/exports need clear per-stage progress feedback.
            Where used: Background transfer worker progress callback.
            Returns: None; updates the status label and determinate progress bar.
            """
            label = TRANSFER_DATABASES.get(key, ("", "", ""))[1] if key else ""
            stages = {"snapshot": "Reading a consistent database snapshot", "archive": "Writing export ZIP", "archive_media": "Adding referenced backgrounds", "validate": "Validating imported databases", "merge": "Merging missing records", "replace": "Replacing selected database", "import": "Importing selected database", "backgrounds": "Adding missing backgrounds", "complete": "Complete"}
            stage_label = stages.get(stage, stage.replace("_", " ").title())
            status.set(f"{stage_label}: {label} ({completed * 100 // max(1, total)}%)" if label else f"{stage_label} ({completed * 100 // max(1, total)}%)")
            progress.configure(maximum=max(1, total), value=min(completed, max(1, total)))

        def finish(error: BaseException | None = None) -> None:
            """What: Restore menu availability and show the transfer result.

            Why included: Data actions must become usable again after success or failure.
            Where used: Transfer task success and error callbacks.
            Returns: None; updates the progress window and optional error dialog.
            """
            data_transfer_state["busy"] = False
            for index in (0, 2, 3, 5, 6):
                data_directory_menu.entryconfigure(index, state="normal")
            close_button.configure(text="Close", command=window.destroy)
            window.protocol("WM_DELETE_WINDOW", window.destroy)
            if error is None:
                status.set(finished_message)
                append_line(finished_message)
            else:
                status.set("Database transfer failed.")
                append_line(str(error))
                messagebox.showerror(title, str(error), parent=window)

        def task_succeeded(result) -> None:
            """What: Report success and any pre-import backup path.

            Why included: Users need to know where rollback data was stored.
            Where used: Background transfer task success callback.
            Returns: None; finishes the transfer window successfully.
            """
            if isinstance(result, tuple) and len(result) == 2 and result[1] is not None:
                append_line(f"Pre-import backup: {result[1]}")
            finish()

        start_background_task(task_name, worker, on_success=task_succeeded,
            on_error=lambda error: finish(error), on_progress=show_progress)

    def export_selected_databases() -> None:
        """What: Export user-selected app databases to a portable ZIP.

        Why included: Users need cross-installation and cross-PC database transfer.
        Where used: Data menu Export Databases command.
        Returns: None; prompts for database choices and a ZIP destination.
        """
        if data_transfer_state["busy"] or update_state["busy"]:
            return
        data_directory = get_app_data_path()
        available = tuple(key for key in TRANSFER_DATABASES if (data_directory / TRANSFER_DATABASES[key][0]).is_file())
        if not available:
            messagebox.showinfo("Export Databases", "No app databases were found yet.", parent=root)
            return
        selected = choose_database_selection("Export Databases", available)
        if not selected:
            return
        members_password = None
        if "members" in selected:
            members_password = simpledialog.askstring("Protect Member Details",
                "Choose a password for the exported member details (at least 8 characters).\n\n"
                "You will need it to import them on the other computer. It cannot be recovered.",
                show="*", parent=root)
            if not members_password:
                return
            if len(members_password) < 8:
                messagebox.showerror("Export Databases", "The password must be at least 8 characters.", parent=root)
                return
            confirmation = simpledialog.askstring("Protect Member Details", "Type the password again to confirm:", show="*", parent=root)
            if confirmation != members_password:
                messagebox.showerror("Export Databases", "The passwords did not match. Nothing was exported.", parent=root)
                return
        destination = filedialog.asksaveasfilename(parent=root, title="Save portable database ZIP", defaultextension=".zip", initialfile="PewVista Data.zip", filetypes=[("Database transfer ZIP", "*.zip")])
        if not destination:
            return
        begin_data_transfer("Export Databases", "DatabaseExport",
            lambda report: export_database_archive(Path(destination), selected, data_directory, report, members_password=members_password),
            f"Selected databases exported to {destination}")

    def import_selected_databases() -> None:
        """What: Validate a transfer ZIP and merge/replace selected databases.

        Why included: Imported records must be selectable and backed up before changes.
        Where used: Data menu Import Databases command.
        Returns: None; confirms per-database behavior and starts a background import.
        """
        if data_transfer_state["busy"] or update_state["busy"]:
            return
        archive_path = filedialog.askopenfilename(parent=root, title="Select a portable database ZIP", filetypes=[("Database transfer ZIP", "*.zip")])
        if not archive_path:
            return
        try:
            available = importable_databases(Path(archive_path))
        except DataTransferError as error:
            messagebox.showerror("Import Databases", str(error), parent=root)
            return
        selection = choose_database_selection("Import Databases", available, include_modes=True)
        if not selection:
            return
        selected, modes = selection
        actions = "\n".join(f"{TRANSFER_DATABASES[key][1]}: {'Replace all records' if modes[key] == 'replace' else 'Merge missing records'}" for key in selected)
        if not messagebox.askyesno("Confirm Database Import", f"Import behavior for each selected database:\n\n{actions}\n\nA backup of selected current databases will be saved in .import-backups first. Save your work before continuing and restart the app after import.", parent=root):
            return
        members_password = None
        if "members" in selected:
            members_password = simpledialog.askstring("Member Details Password",
                "Enter the password chosen when these member details were exported.\n\n"
                "Leave it empty for exports made by older PewVista versions.",
                show="*", parent=root)
            if members_password is None:
                return
        data_directory = get_app_data_path()
        begin_data_transfer("Import Databases", "DatabaseImport",
            lambda report: import_database_archive(Path(archive_path), selected, data_directory, report, modes=modes, members_password=members_password or None),
            "Selected database import completed. Restart the app to reload the imported records.")

    def export_selected_service_days() -> None:
        """What: Export selected saved-service dates with referenced songs and media.

        Why included: Operators need to transfer particular services without unrelated dates.
        Where used: Data menu Export Service Day(s) command.
        Returns: None; creates a portable service-day ZIP at the chosen destination.
        """
        if data_transfer_state["busy"] or update_state["busy"]:
            return
        try:
            available_dates = list_service_dates(get_app_data_path())
        except DataTransferError as error:
            messagebox.showerror("Export Service Days", str(error), parent=root)
            return
        if not available_dates:
            messagebox.showinfo("Export Service Days", "No saved service dates were found.", parent=root)
            return
        selected_dates = choose_service_day_selection("Export Service Days", available_dates)
        if not selected_dates:
            return
        destination = filedialog.asksaveasfilename(parent=root, title="Save selected service-day ZIP", defaultextension=".zip", initialfile="PewVista Service Days.zip", filetypes=[("Service day transfer ZIP", "*.zip")])
        if not destination:
            return
        begin_data_transfer("Export Service Days", "ServiceDayExport",
            lambda report: export_service_days_archive(Path(destination), selected_dates, get_app_data_path(), report),
            f"Selected dates and their referenced songs/backgrounds exported to {destination}")

    def import_selected_service_days() -> None:
        """What: Import selected dates from a service-day ZIP using merge or replace.

        Why included: Selected services and their missing songs/backgrounds must transfer together.
        Where used: Data menu Import Service Day(s) command.
        Returns: None; confirms selected dates and starts a backed-up background task.
        """
        if data_transfer_state["busy"] or update_state["busy"]:
            return
        archive_path = filedialog.askopenfilename(parent=root, title="Select a service-day transfer ZIP", filetypes=[("Service day transfer ZIP", "*.zip")])
        if not archive_path:
            return
        try:
            available_dates = importable_service_dates(Path(archive_path))
        except DataTransferError as error:
            messagebox.showerror("Import Service Days", str(error), parent=root)
            return
        selection = choose_service_day_selection("Import Service Days", available_dates, include_mode=True)
        if not selection:
            return
        selected_dates, mode = selection
        mode_description = "Merge missing service records" if mode == "merge" else "Replace service records on selected dates"
        if not messagebox.askyesno("Confirm Service-Day Import", f"{mode_description}:\n\n" + "\n".join(selected_dates) + "\n\nReferenced songs missing from Library and missing backgrounds will be added. Existing Library songs and unselected service dates will be preserved. Current data will be backed up before import.", parent=root):
            return
        begin_data_transfer("Import Service Days", "ServiceDayImport",
            lambda report: import_service_days_archive(Path(archive_path), selected_dates, mode, get_app_data_path(), report),
            "Selected service days imported with their referenced songs/backgrounds. Restart the app to reload them.")

    def install_windows_update(selected: str | Path | None = None, confirmed: bool = False) -> None:
        """What: Prepare a desktop update ZIP and hand installation to an external process.

        Why included: Other PCs need an in-app update workflow that preserves their own databases.
        Where used: Help > Install Update (file picker) and the automatic update download.
        Returns: None; closes this app only after preparation and installer launch succeed.
        """
        if update_state["busy"]:
            return
        if not getattr(sys, "frozen", False):
            messagebox.showinfo("Install Update", "Use Install Update in the packaged Windows app. The Python workspace is updated through its build scripts.", parent=root)
            return
        if selected is None:
            selected = filedialog.askopenfilename(parent=root, title=f"Select {PRODUCT_NAME} update ZIP", filetypes=[("Windows update ZIP", "*.zip")])
        if not selected:
            return
        if not confirmed and not messagebox.askyesno("Install Update", "Install this ZIP from a trusted source?\n\nThe app and its bundled OBS will close after preparation. Your databases and settings will be preserved, and existing data/program files will be backed up. The app will restart after installation.", parent=root):
            return
        if not require_terms_acceptance(root):
            messagebox.showinfo("Install Update", "The update was cancelled because the Terms and Conditions were not accepted.", parent=root)
            return
        install_directory = get_app_base_path()
        update_state["busy"] = True
        help_menu.entryconfigure(update_entry_index, state="disabled", label="Preparing update...")

        def prepare_worker(report_progress):
            """What: Validate and extract the selected archive without touching installed files.

            Why included: Large ZIP extraction must not freeze Tk or close the app prematurely.
            Where used: WindowsUpdatePrepare background task.
            Returns: Prepared payload and helper paths.
            """
            return prepare_update_archive(Path(selected), install_directory,
                install_directory / "apply_windows_update.ps1", progress=report_progress)

        def show_progress(completed: int, total: int) -> None:
            """What: Display update extraction progress on the Tk thread.

            Why included: Users need visible progress while preparing a large package.
            Where used: Update worker progress callback.
            Returns: None.
            """
            help_menu.entryconfigure(update_entry_index, label=f"Preparing update... {completed * 100 // max(1, total)}%")

        def preparation_failed(error: BaseException) -> None:
            """What: Restore the update menu command and report preparation/launch failures.

            Why included: A rejected update must leave the existing app available.
            Where used: Update task error callback and installer launch failure.
            Returns: None.
            """
            update_state["busy"] = False
            help_menu.entryconfigure(update_entry_index, state="normal", label="Install Update...")
            messagebox.showerror("Install Update", str(error), parent=root)

        def preparation_ready(prepared) -> None:
            """What: Start the prepared external installer before closing this application.

            Why included: The installer must wait until Windows releases the running executable.
            Where used: Successful WindowsUpdatePrepare completion callback.
            Returns: None; leaves the app open if process creation fails.
            """
            try:
                launch_prepared_update(prepared, install_directory, os.getpid())
            except Exception as error:
                discard_prepared_update(prepared)
                preparation_failed(error)
                return
            handle_app_close()

        start_background_task("WindowsUpdatePrepare", prepare_worker,
            on_success=preparation_ready, on_error=preparation_failed, on_progress=show_progress)

    def check_for_updates(manual: bool = False) -> None:
        """What: Look for a newer release online and offer to download and install it.

        Why included: Churches should get fixes without finding update files themselves.
        Where used: Help > Check for Updates and the daily startup check.
        Returns: None; network work runs in the background and only asks, never installs silently.
        """
        if update_state["busy"]:
            return
        if not getattr(sys, "frozen", False):
            if manual:
                messagebox.showinfo("Check for Updates", "Updates apply to the installed Windows app, not the Python workspace.", parent=root)
            return

        def check_failed(error: BaseException) -> None:
            if manual:
                messagebox.showerror("Check for Updates", str(error), parent=root)

        def check_finished(feed) -> None:
            if not feed:
                if manual:
                    messagebox.showinfo("Check for Updates", f"{PRODUCT_NAME} is up to date (version {get_release_version()}).", parent=root)
                return
            notes = str(feed.get("notes") or "").strip()
            size_mb = int(feed.get("size") or 0) // (1024 * 1024)
            question = (f"A new {PRODUCT_NAME} version is available: {feed['version']}\n"
                        f"Installed: {get_release_version()}\n\n" + (f"What's new:\n{notes}\n\n" if notes else "")
                        + f"Download ({size_mb} MB) and install it now? The app restarts afterwards; "
                        "your data and settings are kept. Choose No to be reminded later.")
            if not messagebox.askyesno("Update Available", question, parent=root):
                return
            update_state["busy"] = True
            help_menu.entryconfigure(update_entry_index, state="disabled", label="Downloading update...")
            target = Path(tempfile.gettempdir()) / "PewVista-update" / auto_update.UPDATE_FILE_NAME

            def download_progress(received: int, total: int) -> None:
                if total:
                    help_menu.entryconfigure(update_entry_index, label=f"Downloading update... {received * 100 // total}%")

            def download_failed(error: BaseException) -> None:
                update_state["busy"] = False
                help_menu.entryconfigure(update_entry_index, state="normal", label="Install Update...")
                messagebox.showerror("Update", str(error), parent=root)

            def download_finished(path) -> None:
                update_state["busy"] = False
                help_menu.entryconfigure(update_entry_index, state="normal", label="Install Update...")
                install_windows_update(path, confirmed=True)

            start_background_task("AutoUpdateDownload", lambda report: auto_update.download_update(feed, target, progress=report),
                on_success=download_finished, on_error=download_failed, on_progress=download_progress)

        start_background_task("AutoUpdateCheck", lambda: auto_update.check_for_update(get_release_version()),
            on_success=check_finished, on_error=check_failed)

    container = tk.Frame(root, bg=THEMES["Light"]["window_bg"])
    container.pack(expand=True, fill="both")
    container.columnconfigure(0, weight=1)
    container.columnconfigure(1, weight=1)
    container.columnconfigure(2, weight=1)
    container.rowconfigure(0, weight=2, minsize=290)
    container.rowconfigure(1, weight=3)

    selected_theme = tk.StringVar(value=str(saved_settings.get("theme") or "Light"))
    selected_font_family = tk.StringVar(value=str(saved_settings.get("font_family") or DEFAULT_LIBRARY_FONT_FAMILY))
    selected_font_style = tk.StringVar(value=str(saved_settings.get("font_style") or "Bold"))
    selected_font_size = tk.IntVar(value=int(saved_settings.get("font_size") or 12))
    selected_display_font_family = tk.StringVar(value=str(saved_settings.get("display_font_family") or selected_font_family.get()))
    selected_display_font_style = tk.StringVar(value=str(saved_settings.get("display_font_style") or selected_font_style.get()).lower())
    saved_outline_size = str(saved_settings.get("display_outline_size") or "Auto")
    if saved_outline_size not in DISPLAY_OUTLINE_OPTIONS:
        saved_outline_size = "Auto"
    selected_display_outline_size = tk.StringVar(value=saved_outline_size)
    saved_text_color = str(saved_settings.get("display_text_color") or "Auto").strip().title()
    if saved_text_color not in DISPLAY_TEXT_COLOR_OPTIONS:
        saved_text_color = "Auto"
    selected_display_text_color = tk.StringVar(value=saved_text_color)
    try:
        saved_key_size = int(saved_settings.get("display_key_size", DEFAULT_DISPLAY_KEY_SIZE))
    except (TypeError, ValueError):
        saved_key_size = DEFAULT_DISPLAY_KEY_SIZE
    if saved_key_size != 0 and saved_key_size not in DISPLAY_KEY_SIZE_OPTIONS:
        saved_key_size = DEFAULT_DISPLAY_KEY_SIZE
    selected_display_key_size = tk.IntVar(value=saved_key_size)
    set_display_font(selected_display_font_family.get(), selected_display_font_style.get())
    set_display_outline_width(selected_display_outline_size.get())
    set_display_text_color(selected_display_text_color.get())
    set_display_key_size(selected_display_key_size.get())
    selected_date_text = tk.StringVar(value=date.today().isoformat())
    selected_time = tk.StringVar(value=SERVICE_TIME_OPTIONS[0])
    preacher_name = tk.StringVar(value="")

    menu_bar = tk.Menu(root)
    root.configure(menu=menu_bar)
    history_menu = tk.Menu(menu_bar, tearoff=0)
    data_directory_menu = tk.Menu(menu_bar, tearoff=0)
    menu_bar.add_command(label="Settings", command=lambda: open_settings_dialog())
    menu_bar.add_cascade(label="History", menu=history_menu)
    menu_bar.add_command(label="Licenses", command=open_music_license_settings)
    data_directory_menu.add_command(label="Open Background Folder", command=open_background_folder)
    data_directory_menu.add_separator()
    data_directory_menu.add_command(label="Export Databases...", command=export_selected_databases)
    data_directory_menu.add_command(label="Import Databases...", command=import_selected_databases)
    data_directory_menu.add_separator()
    data_directory_menu.add_command(label="Export Service Day(s)...", command=export_selected_service_days)
    data_directory_menu.add_command(label="Import Service Day(s)...", command=import_selected_service_days)
    menu_bar.add_cascade(label="Data", menu=data_directory_menu)
    def open_branding_editor() -> None:
        """What: Edit the church name and default background used across the app.

        Why included: Installations from before the rename, and later branding changes,
        need these set without reinstalling; saved values must survive every update.
        Where used: Help menu > Church Name, Background and Icon.
        Returns: None; saves to branding.json in the shared data folder.
        """
        branding = get_branding_settings()
        dialog = tk.Toplevel(root)
        dialog.title("Church Name, Background and Icon")
        dialog.transient(root)
        dialog.grab_set()
        apply_application_icon(dialog)
        body = ttk.Frame(dialog, padding=(14, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="Church name shown in the window title:").grid(row=0, column=0, columnspan=3, sticky="w")
        name_value = tk.StringVar(value=str(branding.get("church_name") or "").strip())
        ttk.Entry(body, textvariable=name_value, width=64).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 12))
        ttk.Label(body, text="Church default background (blank uses the bundled PewVista image):").grid(row=2, column=0, columnspan=3, sticky="w")
        background_value = tk.StringVar(value=str(branding.get("default_background_path") or "").strip())
        ttk.Entry(body, textvariable=background_value, state="readonly").grid(row=3, column=0, sticky="ew", pady=(4, 0))

        def browse_background() -> None:
            """What: Choose the default background image file.

            Why included: The shared background should be picked, not typed.
            Where used: The Browse button in this dialog.
            Returns: None; updates the background field on selection.
            """
            selected = filedialog.askopenfilename(parent=dialog, title="Choose the church default background",
                filetypes=[("Image backgrounds", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All files", "*.*")])
            if selected:
                background_value.set(selected)

        ttk.Button(body, text="Browse...", command=browse_background).grid(row=3, column=1, padx=(8, 0), pady=(4, 0))
        ttk.Button(body, text="Clear", command=lambda: background_value.set("")).grid(row=3, column=2, padx=(8, 0), pady=(4, 0))
        ttk.Label(body, text="App icon (.ico, blank uses the PewVista icon):").grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))
        icon_value = tk.StringVar(value=str(branding.get("custom_icon_path") or "").strip())
        ttk.Entry(body, textvariable=icon_value, state="readonly").grid(row=5, column=0, sticky="ew", pady=(4, 0))

        def browse_icon() -> None:
            """What: Choose the application icon file.

            Why included: The icon brands the window, dialogs, and shortcuts.
            Where used: The icon Browse button in this dialog.
            Returns: None; updates the icon field on selection.
            """
            selected = filedialog.askopenfilename(parent=dialog, title="Choose the app icon",
                filetypes=[("Icon files", "*.ico"), ("All files", "*.*")])
            if selected:
                icon_value.set(selected)

        ttk.Button(body, text="Browse...", command=browse_icon).grid(row=5, column=1, padx=(8, 0), pady=(4, 0))
        ttk.Button(body, text="Clear", command=lambda: icon_value.set("")).grid(row=5, column=2, padx=(8, 0), pady=(4, 0))

        def save_branding() -> None:
            """What: Validate and persist the branding, then apply it immediately.

            Why included: Saved choices must take effect without a restart and survive updates.
            Where used: The Save button in this dialog.
            Returns: None; keeps the dialog open when validation or saving fails.
            """
            background = background_value.get().strip()
            if background and not Path(background).is_file():
                messagebox.showerror("Church Branding", "The selected background file could not be found.", parent=dialog)
                return
            icon = icon_value.get().strip()
            if icon:
                if not Path(icon).is_file():
                    messagebox.showerror("Church Branding", "The selected icon file could not be found.", parent=dialog)
                    return
                if Path(icon).suffix.lower() != ".ico":
                    messagebox.showerror("Church Branding", "The app icon must be a .ico file.", parent=dialog)
                    return
            try:
                save_branding_settings({"church_name": name_value.get().strip(),
                    "default_background_path": background, "custom_icon_path": icon})
            except OSError as error:
                messagebox.showerror("Church Branding", f"The branding could not be saved: {error}", parent=dialog)
                return
            root.title(compose_window_title())
            apply_application_icon(root)
            try:
                mirror_controller.clear_preview_background_cache()
                mirror_controller.schedule_preview_refresh()
                if bool(mirror_controller.enabled_var.get()):
                    mirror_controller.request_push_to_secondary(force=True)
            except Exception as error:
                log_exception("main.apply_branding_refresh", error)
            messagebox.showinfo("Church Branding", "Saved and applied. Items with their own background keep it.", parent=dialog)
            dialog.destroy()

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Save", command=save_branding).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")

    def open_license_dialog() -> None:
        """What: Show the license state and let the church activate, subscribe, or free a PC.

        Why included: Subscriptions are activated per PC; the red secondary-screen marking
        disappears as soon as a valid license is saved.
        Where used: Help > License and Subscription.
        Returns: None; network work runs on a background thread.
        """
        dialog = tk.Toplevel(root)
        dialog.title("License and Subscription")
        dialog.transient(root)
        dialog.grab_set()
        apply_application_icon(dialog)
        body = ttk.Frame(dialog, padding=(14, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        status_value = tk.StringVar()
        ttk.Label(body, textvariable=status_value, font=("Segoe UI", 11, "bold"), wraplength=520, justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text=(f"Subscription: {licensing.get_price_text()}, {licensing.TRIAL_DAYS}-day free trial, up to "
                              f"{licensing.MAX_DEVICES} PCs per subscription. After subscribing, PayPal emails a "
                              f"subscription ID (starts with I-). Each PC must connect to the internet at least "
                              f"once every {licensing.OFFLINE_GRACE_DAYS} days."),
                  wraplength=520, justify="left").grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 12))
        ttk.Label(body, text="PayPal subscription ID or license code:").grid(row=2, column=0, columnspan=3, sticky="w")
        code_value = tk.StringVar()
        code_entry = ttk.Entry(body, textvariable=code_value, width=60)
        code_entry.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        activate_button = ttk.Button(body, text="Activate")
        activate_button.grid(row=3, column=1, padx=(8, 0), pady=(4, 0))
        machine_id = licensing.get_machine_id()
        ttk.Label(body, text=f"This PC's ID: {machine_id}").grid(row=4, column=0, sticky="w", pady=(12, 0))

        def copy_machine_id() -> None:
            """What: Copy a license request with the church name and this PC's ID.

            Why included: Manual license codes are bound to one PC and issued per church.
            Where used: The Copy ID button.
            Returns: None.
            """
            church_name = str(get_branding_settings().get("church_name") or "").strip()
            root.clipboard_clear()
            root.clipboard_append(licensing.build_license_request(church_name, machine_id))
            messagebox.showinfo("License", "Copied the church name and this PC's ID. Paste them into an email or WhatsApp message to PewVista.", parent=dialog)

        ttk.Button(body, text="Copy ID", command=copy_machine_id).grid(row=4, column=1, padx=(8, 0), pady=(12, 0))

        def refresh_status() -> None:
            """What: Show the current license state in the dialog and window title.

            Why included: Activation and removal must be reflected immediately.
            Where used: Dialog start and after each action.
            Returns: None.
            """
            status = licensing.get_license_status(force=True)
            if status.state == "licensed":
                until = status.valid_until[:10]
                text = f"Licensed ({status.subscription_id}). Valid on this PC until {until}; renews automatically while online."
            elif status.state == "developer":
                text = status.message
            elif status.state == "trial":
                text = status.message
            else:
                text = "NOT LICENSED. " + status.message
            status_value.set(text)
            remove_button.configure(state="normal" if status.state == "licensed" else "disabled")
            root.title(compose_window_title())

        def finish_action(message: str | None = None, error: Exception | None = None) -> None:
            """What: Re-enable the dialog after a background license action.

            Why included: Activation runs off the UI thread.
            Where used: Activate and Remove callbacks.
            Returns: None.
            """
            if not dialog.winfo_exists():
                return
            activate_button.configure(state="normal")
            refresh_status()
            if error is not None:
                messagebox.showerror("License", str(error), parent=dialog)
            elif message:
                messagebox.showinfo("License", message, parent=dialog)

        def activate() -> None:
            """What: Activate this PC with the entered subscription ID or license code.

            Why included: The church enters what PayPal or PewVista sent them.
            Where used: The Activate button and Enter key.
            Returns: None; work runs in the background.
            """
            value = code_value.get().strip()
            if not value:
                messagebox.showerror("License", "Enter the PayPal subscription ID or license code.", parent=dialog)
                return
            activate_button.configure(state="disabled")
            start_background_task("license_activate", lambda: licensing.activate_subscription(value),
                on_success=lambda status: finish_action("This PC is now licensed. Thank you for supporting PewVista."),
                on_error=lambda error: finish_action(error=error))

        def remove_license() -> None:
            """What: Free this PC's license slot so another PC can use it.

            Why included: Subscriptions cover a fixed number of PCs.
            Where used: The Remove from this PC button.
            Returns: None; work runs in the background.
            """
            if not messagebox.askyesno("License", "Remove the license from this PC? The secondary screen will show NOT LICENSED until it is activated again.", parent=dialog):
                return
            start_background_task("license_remove", licensing.deactivate_this_pc,
                on_success=lambda _result: finish_action("The license was removed from this PC."),
                on_error=lambda error: finish_action(error=error))

        def open_subscribe_page() -> None:
            """What: Open the PayPal subscription page in the web browser.

            Why included: Payment happens on PayPal, never inside the app.
            Where used: The Subscribe button.
            Returns: None.
            """
            import webbrowser
            webbrowser.open(licensing.PAYPAL_SUBSCRIBE_URL)

        activate_button.configure(command=activate)
        code_entry.bind("<Return>", lambda _event: activate())
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        subscribe_button = ttk.Button(buttons, text=f"Subscribe with PayPal ({licensing.get_price_text()})", command=open_subscribe_page)
        subscribe_button.pack(side="left")
        if not licensing.PAYPAL_SUBSCRIBE_URL:
            subscribe_button.configure(state="disabled")
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="right")
        remove_button = ttk.Button(buttons, text="Remove from this PC", command=remove_license)
        remove_button.pack(side="right", padx=(0, 8))
        refresh_status()
        code_entry.focus_set()

    help_menu = tk.Menu(menu_bar, tearoff=0)
    help_menu.add_command(label="Church Name, Background and Icon...", command=open_branding_editor)
    help_menu.add_command(label="License and Subscription...", command=open_license_dialog)
    help_menu.add_command(label="Operating Manual",
        command=lambda: show_text_document(root, "Operating Manual", get_app_base_path() / "OPERATING_MANUAL.txt"))
    help_menu.add_command(label="Terms and Conditions",
        command=lambda: show_text_document(root, "Terms and Conditions", get_app_base_path() / "TERMS_AND_CONDITIONS.txt"))
    help_menu.add_command(label="Security and Privacy",
        command=lambda: show_text_document(root, "Security and Privacy", get_app_base_path() / "SECURITY_AND_PRIVACY.txt"))
    help_menu.add_command(label="Third-Party Software Licenses",
        command=lambda: show_text_document(root, "Third-Party Software Licenses", get_app_base_path() / "THIRD_PARTY_NOTICES.txt"))
    help_menu.add_command(label="Bible Credits and Licenses",
        command=lambda: show_text_document(root, "Bible Credits and Licenses", get_app_base_path() / "Bibles" / "licenses" / "BIBLE_CREDITS.txt"))
    help_menu.add_separator()
    help_menu.add_command(label="Install Update...", command=install_windows_update)
    update_entry_index = int(help_menu.index("end"))
    help_menu.add_command(label="Check for Updates...", command=lambda: check_for_updates(manual=True))
    if auto_update.check_due():
        root.after(20000, check_for_updates)
    help_menu.add_separator()
    help_menu.add_command(label=f"Version: {get_release_version()}", state="disabled")

    top_sections: list[dict[str, object]] = []
    top_titles = ["Info", "Secondary Screen", "Actions"]

    def current_overlay_font_value() -> tuple[str, int, str]:
        """Return the main font settings in the renderer's expected tuple form.

        What: Reads family, clamps size to the display minimum, and lowercases style.
        Why included: The overlay renderer uses a normalized font representation
        shared by preview and output code.
        Where used: MirrorDisplayController's font_value_factory callback.
        Returns: (family, size, lowercase style).
        """
        return (
            selected_font_family.get(),
            max(12, int(selected_font_size.get())),
            selected_font_style.get().lower(),
        )

    def refresh_secondary_display_bounds() -> dict[str, int]:
        """Re-query the preferred monitor geometry for projector placement.

        What: Returns the current preferred display bounds from the platform helper.
        Why included: Monitor topology can change while the app remains open.
        Where used: get_display_config() and the preview-size callback passed to
        MirrorDisplayController.
        Returns: Bounds mapping with the monitor's position and dimensions.
        """
        return get_preferred_display_bounds()

    def get_display_config() -> dict[str, object]:
        """Build the display configuration consumed by the mirror controller.

        What: Selects projector mode and attaches a fresh preferred-monitor bounds map.
        Why included: The controller needs app-level display choices without owning
        monitor discovery or main-window state.
        Where used: get_display_config callback supplied to MirrorDisplayController.
        Returns: Mapping containing window_mode and display_bounds.
        """
        return {
            "window_mode": "projector",
            "display_bounds": dict(refresh_secondary_display_bounds()),
        }

    display_state_holder: dict[str, object] = {}

    def sync_secondary_hot_backgrounds() -> None:
        """Refresh OBS's preload list from the current Display queue.

        What: Reads queue items through Display's state getter and extracts paths.
        Why included: Preloading likely backgrounds reduces visual delay during
        live transitions; malformed or unavailable state safely produces no list.
        Where used: After Display state is initialized and when queue items change.
        Returns: None; updates MirrorDisplayController's hot-background cache.
        """
        display_state = display_state_holder.get("display_state")
        if not isinstance(display_state, dict):
            return
        get_queue_items = display_state.get("get_queue_items")
        if not callable(get_queue_items):
            return
        try:
            queue_items = list(get_queue_items() or [])
        except Exception:
            queue_items = []
        hot_backgrounds: list[str] = []
        for queue_item in queue_items:
            if not isinstance(queue_item, dict):
                continue
            hot_backgrounds.append(str(queue_item.get("background_path") or ""))
        mirror_controller.set_hot_backgrounds(hot_backgrounds)

    def handle_display_live_change(payload: dict[str, object] | None, force: bool = False) -> None:
        """Forward a live Display selection to the preview and OBS controller.

        What: Passes the payload and force flag to set_live_payload().
        Why included: Display owns queue selection, while the mirror controller
        owns local preview and external output synchronization.
        Where used: Injected into tab context as handle_display_live_change.
        Returns: None; controller processes the update.
        """
        mirror_controller.set_live_payload(payload, force=force)

    mirror_controller = MirrorDisplayController(
        root,
        get_app_base_path=get_app_base_path,
        get_display_config=get_display_config,
        get_preview_size=lambda: get_preview_size_for_bounds(refresh_secondary_display_bounds()),
        resolve_background_path=resolve_background_path,
        serialize_overlay_font=serialize_overlay_font,
        build_hot_background_paths=build_hot_background_paths,
        font_value_factory=current_overlay_font_value,
        get_music_licenses=get_music_licenses,
    )
    if getattr(sys, "frozen", False):
        # Start OBS while the operator is still preparing, so the first live item is instant.
        root.after(2000, mirror_controller.prewarm_secondary_output)

    for column_index, title in enumerate(top_titles):
        frame = tk.LabelFrame(container, text=title, bg=THEMES["Light"]["panel_bg"], padx=16, pady=16, bd=1, relief="solid", font=(selected_font_family.get(), selected_font_size.get(), FONT_STYLES[selected_font_style.get()]), labelanchor="n")
        frame.grid(row=0, column=column_index, padx=6, pady=6, sticky="nsew")
        section = {"frame": frame, "widgets": [], "label_widgets": [], "input_widgets": []}

        if title == "Info":
            form = tk.Frame(frame, bg=THEMES["Light"]["panel_bg"])
            form.pack(expand=True, fill="both")
            form.columnconfigure(1, weight=1)
            button_row = tk.Frame(form, bg=THEMES["Light"]["panel_bg"])
            button_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
            button_row.columnconfigure(0, weight=1)
            button_row.columnconfigure(1, weight=1)
            save_button = tk.Button(button_row, text="Save", state="disabled")
            save_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
            print_button = tk.Button(button_row, text="Print", state="disabled")
            print_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
            date_label = tk.Label(form, text="Date")
            date_label.grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
            date_entry = tk.Entry(form, textvariable=selected_date_text, state="readonly")
            date_entry.grid(row=1, column=1, sticky="ew", pady=4)
            date_entry.bind(
                "<Button-1>",
                lambda _event, current_parent=root, current_var=selected_date_text, theme_var=selected_theme, family_var=selected_font_family, style_var=selected_font_style, size_var=selected_font_size: open_date_picker(
                    current_parent,
                    current_var,
                    theme_var.get(),
                    family_var.get(),
                    style_var.get(),
                    size_var.get(),
                    on_date_selected=request_service_recall,
                ),
            )
            try:
                date_entry.configure(cursor="hand2")
            except tk.TclError:
                pass
            time_label = tk.Label(form, text="Time")
            time_label.grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
            time_menu = tk.OptionMenu(form, selected_time, *SERVICE_TIME_OPTIONS, command=lambda _value: request_service_recall())
            time_menu.grid(row=2, column=1, sticky="ew", pady=4)
            preacher_label = tk.Label(form, text="Preacher")
            preacher_label.grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
            preacher_entry = tk.Entry(form, textvariable=preacher_name)
            preacher_entry.grid(row=3, column=1, sticky="ew", pady=4)
            section["widgets"].extend([form, button_row, save_button, print_button, date_label, date_entry, time_label, time_menu, time_menu["menu"], preacher_label, preacher_entry])
            section["label_widgets"].extend([date_label, time_label, preacher_label])
            section["input_widgets"].extend([save_button, print_button, date_entry, time_menu, time_menu["menu"], preacher_entry])
        elif title == "Secondary Screen":
            mirror_widgets = mirror_controller.build_section(frame, THEMES["Light"]["panel_bg"])
            section["widgets"].extend(list(mirror_widgets.values()))
            section["label_widgets"].append(mirror_widgets["preview_label"])
            section["input_widgets"].append(mirror_widgets["toggle"])
        top_sections.append(section)

    bottom_frame = tk.LabelFrame(container, text="Tabs", bg=THEMES["Light"]["panel_bg"], padx=16, pady=16, bd=1, relief="solid", font=(selected_font_family.get(), selected_font_size.get(), FONT_STYLES[selected_font_style.get()]), labelanchor="n")
    bottom_frame.grid(row=1, column=0, columnspan=3, padx=6, pady=6, sticky="nsew")
    bottom_body = tk.Frame(bottom_frame, bg=THEMES["Light"]["panel_bg"])
    bottom_body.pack(expand=True, fill="both")

    tabs_content = create_tabs_content(
        bottom_body,
        selected_theme,
        selected_font_family,
        selected_font_style,
        selected_font_size,
        selected_date_text,
        extra_context={
            "handle_display_live_change": handle_display_live_change,
            "handle_queue_items_changed": sync_secondary_hot_backgrounds,
            "start_background_task": start_background_task,
            "get_display_bounds": get_preferred_display_bounds,
            "display_font_family": selected_display_font_family,
            "display_font_style": selected_display_font_style,
            "get_music_licenses": get_music_licenses,
        },
    )
    display_state_holder["display_state"] = tabs_content.get("display_state")
    sync_secondary_hot_backgrounds()

    bottom_section = {
        "frame": bottom_frame,
        "body": bottom_body,
        "notebook": tabs_content["notebook"],
        "widgets": [bottom_body, *tabs_content["widgets"]],
        "label_widgets": tabs_content["labels"],
        "input_widgets": tabs_content["inputs"],
        "display_state": tabs_content["display_state"],
        "selected_font_family": selected_font_family,
        "selected_font_style": selected_font_style,
        "selected_font_size": selected_font_size,
    }
    service_restore_state = {"updating": False, "after_id": None, "last_slot": None, "selected_entry_id": None, "request_token": 0}
    service_save_state = {"in_progress": False}
    menu_widgets = [menu_bar, history_menu, data_directory_menu, help_menu]
    ui_parts = {
        "top_sections": top_sections,
        "bottom_section": bottom_section,
        "selected_theme": selected_theme,
        "menu_widgets": menu_widgets,
    }

    members_popup_state: dict[str, tk.Toplevel | None] = {"window": None}
    history_popup_state: dict[str, object] = {"window": None, "tree": None, "details": None, "delete_button": None, "entries": [], "request_token": 0}

    def open_print_window() -> None:
        """Open the print popup with current service and appearance state.

        What: Builds a date/time document title and supplies queue, theme, font,
        popup styling, and notebook styling callbacks to the printer module.
        Why included: Printing needs coordinated state from the main window and tabs.
        Where used: Print button in the Info section.
        Returns: None; printer module creates or focuses its popup.
        """
        document_title = f"{PRODUCT_NAME} {selected_date_text.get()} {selected_time.get()}".strip()
        open_print_popup(
            root,
            bottom_section.get("display_state") if isinstance(bottom_section, dict) else None,
            apply_layout_style_for_popup,
            lambda: get_theme_by_name(selected_theme.get()),
            document_title,
            lambda notebook: configure_tabs_notebook_style(
                notebook,
                get_theme_by_name(selected_theme.get()),
                selected_font_family.get(),
                selected_font_style.get(),
                selected_font_size.get(),
            ),
            lambda: (selected_font_family.get(), selected_font_style.get()),
        )


    def get_display_queue_items() -> list[dict[str, object]]:
        """Return a defensive list copy of the Display tab's queue items.

        What: Calls the state API getter and converts its result to a list.
        Why included: Main service persistence should not depend on Display's widgets.
        Where used: save_current_service() to capture the service snapshot.
        Returns: Queue dictionaries, or an empty list if the API is unavailable
        or raises while reading state.
        """
        display_state = bottom_section.get("display_state") if isinstance(bottom_section, dict) else None
        if not isinstance(display_state, dict):
            return []
        getter = display_state.get("get_queue_items")
        if not callable(getter):
            return []
        try:
            return list(getter() or [])
        except Exception:
            return []

    def refresh_display_from_bridge() -> None:
        """Tell Display to reload queue state after recall changes the bridge.

        What: Calls the optional refresh_from_bridge function in Display state.
        Why included: Restoring service data updates the shared bridge outside
        Display's normal editing workflow.
        Where used: Recall result handling after clearing or restoring a service.
        Returns: None; does nothing when the state callback is absent.
        """
        display_state = bottom_section.get("display_state") if isinstance(bottom_section, dict) else None
        if not isinstance(display_state, dict):
            return
        refresh_from_bridge = display_state.get("refresh_from_bridge")
        if callable(refresh_from_bridge):
            refresh_from_bridge()

    def refresh_saved_services_list() -> None:
        """Refresh the Display tab's selector of persisted service snapshots.

        What: Calls Display's optional refresh_saved_services state callback.
        Why included: A newly saved service must appear in the tab's selector.
        Where used: Successful completion of save_current_service().
        Returns: None; no-op when the Display state callback is unavailable.
        """
        display_state = bottom_section.get("display_state") if isinstance(bottom_section, dict) else None
        if not isinstance(display_state, dict):
            return
        refresh_saved_services = display_state.get("refresh_saved_services")
        if callable(refresh_saved_services):
            refresh_saved_services()

    def apply_recalled_welcome_entry(entry: dict[str, object] | None) -> None:
        """Restore or clear the Welcome tab from a recalled service entry.

        What: Selects restore_service_entry for a record, otherwise clear_recalled_data.
        Why included: Schedule recall must restore Welcome content alongside the queue.
        Where used: apply_recall_result() for a found record or empty schedule.
        Returns: None; no-op when Welcome state or its callback is unavailable.
        """
        welcome_state = tabs_content.get("welcome_state") if isinstance(tabs_content, dict) else None
        if not isinstance(welcome_state, dict):
            return
        restore_callback = welcome_state.get("restore_service_entry") if isinstance(entry, dict) else welcome_state.get("clear_recalled_data")
        if callable(restore_callback):
            if isinstance(entry, dict):
                restore_callback(entry)
            else:
                restore_callback()

    def apply_recalled_preparation_entry(entry: dict[str, object] | None) -> None:
        """Restore or clear preparation content from a recalled service entry.

        What: Dispatches to the Preparation tab's restore or clear state callback.
        Why included: Recalled service state spans queue items and tab-owned fields.
        Where used: apply_recall_result() when a schedule is found or empty.
        Returns: None; no-op when Preparation state or its callback is unavailable.
        """
        preparation_state = tabs_content.get("preparation_state") if isinstance(tabs_content, dict) else None
        if not isinstance(preparation_state, dict):
            return
        restore_callback = preparation_state.get("restore_service_entry") if isinstance(entry, dict) else preparation_state.get("clear_recalled_data")
        if callable(restore_callback):
            if isinstance(entry, dict):
                restore_callback(entry)
            else:
                restore_callback()

    def request_service_recall() -> None:
        """Debounce schedule edits before starting a service lookup.

        What: Cancels any pending timer and schedules recall after 180 ms.
        Why included: Date and time controls can emit closely spaced changes;
        coalescing them avoids redundant database queries.
        Where used: Date-picker callback and service-time menu command.
        Returns: None; records the pending Tk after identifier in restore state.
        """
        pending_after_id = service_restore_state.get("after_id")
        if pending_after_id is not None:
            try:
                root.after_cancel(pending_after_id)
            except tk.TclError:
                pass
        service_restore_state["after_id"] = root.after(180, apply_service_recall_for_selected_schedule)

    def get_current_service_display_settings() -> dict[str, object]:
        """Capture current appearance choices for a service snapshot.

        What: Reads theme, app font, display font, outline, and text color variables.
        Why included: Recalling a service restores its saved presentation settings.
        Where used: save_current_service() before its worker starts.
        Returns: Mapping of persisted display-setting names to current values.
        """
        return {
            "theme": selected_theme.get(),
            "font_family": selected_font_family.get(),
            "font_style": selected_font_style.get(),
            "font_size": int(selected_font_size.get()),
            "display_font_family": selected_display_font_family.get(),
            "display_font_style": selected_display_font_style.get(),
            "display_outline_size": selected_display_outline_size.get(),
            "display_text_color": selected_display_text_color.get(),
        }

    def save_current_service() -> None:
        """Save the current service snapshot without blocking the Tk event loop.

        What: Validates queue and duplicate-save state, captures UI values, then
        starts the database worker with UI-thread success/error callbacks.
        Why included: Database writes must not freeze the application, and the
        worker needs an immutable snapshot of current widget values.
        Where used: Save button in the Info section.
        Returns: None; reports empty queues and resets save state through callbacks.
        """
        queue_items = get_display_queue_items()
        if not queue_items:
            messagebox.showinfo("Save Service", "There is no Display queue content to save.", parent=root)
            return
        if service_save_state.get("in_progress"):
            return
        selected_date_value = str(selected_date_text.get() or "").strip()
        selected_time_value = str(selected_time.get() or "").strip()
        selected_slot = (selected_date_value, selected_time_value)
        selected_service_entry_id = service_restore_state.get("selected_entry_id") if service_restore_state.get("last_slot") == selected_slot else None
        if selected_service_entry_id is None:
            matching_entries = list_service_entries_by_schedule(selected_date_value, selected_time_value)
            if len(matching_entries) > 1:
                request_service_recall()
                messagebox.showinfo("Choose Saved Service", "Select which saved version to update before saving this date and time.", parent=root)
                return
            if len(matching_entries) == 1:
                selected_service_entry_id = matching_entries[0]["id"]
        service_save_state["in_progress"] = True
        preacher_name_value = str(preacher_name.get() or "").strip()
        display_settings = get_current_service_display_settings()

        def save_service_worker() -> dict[str, object]:
            """Persist the captured service snapshot on a background thread.

            What: Calls save_service_entry() with captured schedule, queue, and settings.
            Why included: Keeps SQLite work off Tk's event thread and records timing.
            Where used: Worker argument passed by save_current_service().
            Returns: The saved service-entry mapping.
            """
            started_at = time.perf_counter()
            saved_entry = save_service_entry(
                selected_date_value,
                selected_time_value,
                preacher_name_value,
                queue_items,
                display_settings=display_settings,
                service_entry_id=selected_service_entry_id,
            )
            log_timing("main.save_service_entry", (time.perf_counter() - started_at) * 1000.0, threshold_ms=15.0, details=f"items={len(queue_items)}")
            return saved_entry

        def handle_save_success(saved_entry: dict[str, object]) -> None:
            """Finish a successful save on Tk's thread and update the user interface.

            What: Clears the in-progress flag, refreshes choices, and shows confirmation.
            Why included: UI state and dialogs must be updated only on Tk's thread.
            Where used: on_success callback supplied to start_background_task().
            Returns: None; updates save state and opens an informational dialog.
            """
            service_save_state["in_progress"] = False
            service_restore_state["selected_entry_id"] = str(saved_entry.get("id") or "") or None
            service_restore_state["last_slot"] = (str(saved_entry.get("service_date") or selected_date_value), str(saved_entry.get("service_time") or selected_time_value))
            refresh_saved_services_list()
            messagebox.showinfo(
                "Save Service",
                f"Saved {len(queue_items)} Display item(s) for {saved_entry.get('service_date') or selected_date_value} {saved_entry.get('service_time') or selected_time_value}.",
                parent=root,
            )

        def handle_save_error(error: BaseException) -> None:
            """Restore save availability and report a failed database write.

            What: Clears the in-progress flag and displays the exception details.
            Why included: Failed writes must not leave Save permanently locked.
            Where used: on_error callback supplied to start_background_task().
            Returns: None; shows an error dialog on Tk's thread.
            """
            service_save_state["in_progress"] = False
            messagebox.showerror("Save Service", f"Failed to save the current service.\n\n{error}", parent=root)

        start_background_task("ServiceSaveWorker", save_service_worker, on_success=handle_save_success, on_error=handle_save_error)

    def apply_service_recall_for_selected_schedule() -> None:
        """Load and apply the saved service for the currently selected schedule.

        What: Debounces via the scheduled callback state, skips duplicate slots,
        assigns a request token, then starts database lookup on a worker thread.
        Why included: Schedule recall must keep Tk responsive and prevent a slower
        earlier result from replacing a newer date/time selection. Recalled queue
        items are restored inactive by the bridge workflow.
        Where used: request_service_recall() timer callback.
        Returns: None; delegates lookup, UI application, and errors to task callbacks.
        """
        service_restore_state["after_id"] = None
        if service_restore_state.get("updating"):
            return
        selected_slot = (str(selected_date_text.get() or "").strip(), str(selected_time.get() or "").strip())
        if service_restore_state.get("last_slot") == selected_slot:
            return
        request_token = int(service_restore_state.get("request_token") or 0) + 1
        service_restore_state["request_token"] = request_token

        def choose_saved_service_record(entries: list[dict[str, str]], slot: tuple[str, str]) -> str | None:
            """What: Let the user choose one saved service for a duplicate date/time slot.

            Why included: Multiple distinct versions for one schedule must not be silently conflated.
            Where used: Date/time recall when more than one service row matches.
            Returns: Selected service-entry ID, or None when cancelled.
            """
            dialog = tk.Toplevel(root)
            dialog.title("Choose Saved Service")
            dialog.transient(root)
            dialog.geometry("620x380")
            dialog.minsize(460, 260)
            dialog.columnconfigure(0, weight=1)
            dialog.rowconfigure(1, weight=1)
            ttk.Label(dialog, text=f"More than one service is saved for {slot[0]} at {slot[1]}. Choose which one to recall:", wraplength=580).grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
            listbox = tk.Listbox(dialog, exportselection=False, height=min(10, len(entries)))
            listbox.grid(row=1, column=0, sticky="nsew", padx=14)
            scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=listbox.yview)
            scrollbar.grid(row=1, column=1, sticky="ns", padx=(0, 14))
            listbox.configure(yscrollcommand=scrollbar.set)
            for entry in entries:
                listbox.insert("end", entry.get("label") or f"Saved service {entry.get('id', '')}")
            if entries:
                listbox.selection_set(0)
                listbox.activate(0)
            selection: dict[str, str | None] = {"id": None}

            def choose() -> None:
                """What: Return the service-entry ID highlighted in the chooser.

                Why included: Duplicate schedule slots need an explicit record choice.
                Where used: Recall Selected button or double-click in the chooser.
                Returns: None; saves the selection and closes the dialog.
                """
                current = listbox.curselection()
                if not current:
                    messagebox.showwarning("Choose Saved Service", "Select a saved service first.", parent=dialog)
                    return
                selection["id"] = entries[int(current[0])]["id"]
                dialog.destroy()

            buttons = ttk.Frame(dialog)
            buttons.grid(row=2, column=0, columnspan=2, sticky="ew", padx=14, pady=14)
            ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
            ttk.Button(buttons, text="Recall Selected", command=choose).pack(side="right", padx=(0, 8))
            listbox.bind("<Double-Button-1>", lambda _event: choose())
            dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
            dialog.grab_set()
            root.wait_window(dialog)
            return selection["id"]

        def load_recall_data(slot=selected_slot) -> list[dict[str, str]]:
            """List saved versions for one schedule off the UI thread.

            What: Calls list_service_entries_by_schedule() and records timing.
            Why included: The lookup may involve database I/O and must not block Tk.
            Where used: Worker callable passed by apply_service_recall_for_selected_schedule().
            Returns: Compact saved-service metadata for the selected date/time.
            """
            started_at = time.perf_counter()
            entries = list_service_entries_by_schedule(slot[0], slot[1])
            log_timing("main.list_service_entries_by_schedule", (time.perf_counter() - started_at) * 1000.0, threshold_ms=15.0, details=f"slot={slot[0]} {slot[1]} records={len(entries)}")
            return entries

        def apply_selected_service(restored_entry: dict[str, object] | None, expected_token=request_token, slot=selected_slot) -> None:
            """Apply a specifically selected saved record to tab fields and the bridge.

            What: Ignores stale tokens; clears recalled state for a missing record,
            otherwise restores preacher, appearance, Welcome, Preparation, and queue data.
            Why included: Asynchronous results may arrive after the user changes slots.
            Where used: on_success callback for the selected-record restore worker.
            Returns: None; refreshes Display after bridge or tab state is updated.
            """
            if expected_token != int(service_restore_state.get("request_token") or 0):
                return
            if restored_entry is None:
                service_restore_state["selected_entry_id"] = None
                service_restore_state["updating"] = True
                try:
                    preacher_name.set("")
                    apply_recalled_welcome_entry(None)
                    apply_recalled_preparation_entry(None)
                    reset_active_display_queue_file()
                finally:
                    service_restore_state["updating"] = False
                service_restore_state["last_slot"] = slot
                refresh_display_from_bridge()
                return
            service_restore_state["last_slot"] = slot
            service_restore_state["selected_entry_id"] = str(restored_entry.get("id") or "") or None
            service_restore_state["updating"] = True
            try:
                preacher_name.set(str(restored_entry.get("preacher_name") or ""))
                apply_service_display_settings(restored_entry.get("display_settings"))
                apply_recalled_welcome_entry(restored_entry)
                apply_recalled_preparation_entry(restored_entry)
            finally:
                service_restore_state["updating"] = False
            refresh_display_from_bridge()

        def apply_recall_result(entries: list[dict[str, str]], expected_token=request_token, slot=selected_slot) -> None:
            """What: Select one matching record or clear recalled content for the slot.

            Why included: A date/time can have multiple distinct saved service versions.
            Where used: ServiceRecallLoader success callback.
            Returns: None; opens a chooser when needed and starts exact-ID restoration.
            """
            if expected_token != int(service_restore_state.get("request_token") or 0):
                return
            if not entries:
                apply_selected_service(None, expected_token, slot)
                return
            selected_id = entries[0]["id"] if len(entries) == 1 else choose_saved_service_record(entries, slot)
            if not selected_id:
                service_restore_state["selected_entry_id"] = None
                service_restore_state["last_slot"] = slot
                return

            def restore_selected() -> dict[str, object] | None:
                """What: Restore the selected service ID for the captured date/time.

                Why included: Exact-ID restoration must not silently choose a sibling record.
                Where used: ServiceRecallSelected background task.
                Returns: Restored service mapping, or None if the record disappeared.
                """
                started_at = time.perf_counter()
                restored = restore_display_queue_from_service_schedule(slot[0], slot[1], selected_id)
                log_timing("main.restore_selected_service", (time.perf_counter() - started_at) * 1000.0, threshold_ms=15.0, details=f"slot={slot[0]} {slot[1]} id={selected_id}")
                return restored

            start_background_task("ServiceRecallSelected", restore_selected,
                on_success=lambda entry: apply_selected_service(entry, expected_token, slot),
                on_error=handle_recall_error)

        def handle_recall_error(error: BaseException, expected_token=request_token) -> None:
            """Report only a lookup failure that still belongs to the selected slot.

            What: Compares the captured token with current recall state before showing
            the exception in an error dialog.
            Why included: Stale failures are irrelevant after a newer schedule is chosen.
            Where used: on_error callback for the service-recall worker.
            Returns: None; stale errors are silently discarded.
            """
            if expected_token != int(service_restore_state.get("request_token") or 0):
                return
            messagebox.showerror("Recall Service", f"Failed to recall the selected schedule.\n\n{error}", parent=root)

        start_background_task("ServiceRecallLoader", load_recall_data, on_success=apply_recall_result, on_error=handle_recall_error)

    def format_history_entry_label(entry: dict[str, object]) -> str:
        """Format the identifying text shown for one saved history row.

        What: Combines non-empty date and time, falls back to an ID label, and appends
        the preacher when present.
        Why included: History rows need a compact label that distinguishes schedules.
        Where used: apply_history_entries() when inserting Treeview rows.
        Returns: A single-line label for the supplied service entry.
        """
        service_date = str(entry.get("service_date") or "").strip()
        service_time = str(entry.get("service_time") or "").strip()
        preacher = str(entry.get("preacher_name") or "").strip()
        label_parts = [part for part in (service_date, service_time) if part]
        label = " - ".join(label_parts) if label_parts else f"Service {entry.get('id') or ''}".strip()
        if preacher:
            label = f"{label} | {preacher}"
        return label

    def unique_non_empty_text(values: list[object]) -> list[str]:
        """Normalize a sequence of display strings and remove duplicates.

        What: Converts values to trimmed strings, discarding blanks and later repeats.
        Why included: Legacy history snapshots may contain repeated or empty sections.
        Where used: Preparation and Bible history formatting helpers.
        Returns: Ordered unique non-empty text values.
        """
        unique_values: list[str] = []
        seen_values: set[str] = set()
        for value in values:
            text_value = str(value or "").strip()
            if not text_value or text_value in seen_values:
                continue
            seen_values.add(text_value)
            unique_values.append(text_value)
        return unique_values

    def normalize_preparation_history_blocks(title: object, text_value: object, verses: object) -> list[str]:
        """Clean legacy preparation text blocks before displaying service history.

        What: Combines the main text and optional verses, removes repeated titles and
        title prefixes, then trims and deduplicates the remaining blocks.
        Why included: Older saved entries may store a title both as metadata and text.
        Where used: build_history_details_text() for each Preparation queue item.
        Returns: Ordered, normalized preparation blocks.
        """
        normalized_title = str(title or "").strip()
        normalized_blocks: list[str] = []
        raw_blocks: list[object] = [text_value]
        if isinstance(verses, list):
            raw_blocks.extend(verses)
        for raw_block in raw_blocks:
            block_text = str(raw_block or "").strip()
            if not block_text:
                continue
            if normalized_title and block_text == normalized_title:
                continue
            prefixed_title = f"{normalized_title}\n\n" if normalized_title else ""
            if prefixed_title and block_text.startswith(prefixed_title):
                block_text = block_text[len(prefixed_title):].strip()
            if block_text:
                normalized_blocks.append(block_text)
        return unique_non_empty_text(normalized_blocks)

    def build_history_details_text(entry: dict[str, object]) -> str:
        """Render service metadata and categorized queue content for History.

        What: Groups saved queue items into Welcome, Songs, Preparation, and Bible
        sections, includes service metadata, and supplies empty-state text per section.
        Why included: Persisted service mappings need a readable representation in
        the detail pane without depending on their original tab widgets.
        Where used: render_selected_history_entry() when a history row is selected.
        Returns: Multi-section plain text for the History detail widget.
        """
        queue_items = list(entry.get("queue_items") or [])
        welcome_items = [item for item in queue_items if isinstance(item, dict) and str(item.get("source") or "").casefold() == "welcome"]
        song_items = [item for item in queue_items if isinstance(item, dict) and str(item.get("source") or "").casefold() in {"library", "song", "songs", "song_list"}]
        preparation_items = [item for item in queue_items if isinstance(item, dict) and str(item.get("source") or "").casefold() == "preparation"]
        bible_items = [item for item in queue_items if isinstance(item, dict) and str(item.get("source") or "").casefold() == "bible"]

        detail_sections: list[str] = []
        metadata_lines = [
            f"Date: {str(entry.get('service_date') or '').strip() or '-'}",
            f"Time: {str(entry.get('service_time') or '').strip() or '-'}",
            f"Preacher: {str(entry.get('preacher_name') or '').strip() or '-'}",
            f"Saved: {str(entry.get('saved_at') or '').strip() or '-'}",
        ]
        detail_sections.append("Service Info\n" + "\n".join(metadata_lines))

        welcome_lines: list[str] = []
        welcome_field_one = str(entry.get("welcome_field_one") or "").strip()
        welcome_field_two = str(entry.get("welcome_field_two") or "").strip()
        if welcome_field_one:
            welcome_lines.append(welcome_field_one)
        if welcome_field_two:
            welcome_lines.append(welcome_field_two)
        for item in welcome_items:
            text_value = str(item.get("text") or "").strip()
            if text_value:
                welcome_lines.append(text_value)
        detail_sections.append("Welcome Note\n" + ("\n\n".join(unique_non_empty_text(welcome_lines)) if unique_non_empty_text(welcome_lines) else "No welcome note saved."))

        song_lines = unique_non_empty_text([str(item.get("title") or "Untitled Song").strip() for item in song_items])
        detail_sections.append("Songs\n" + ("\n".join(song_lines) if song_lines else "No songs saved."))

        preparation_lines: list[str] = []
        for item in preparation_items:
            title = str(item.get("title") or "Preparation").strip()
            unique_parts = normalize_preparation_history_blocks(title, item.get("text"), item.get("verses"))
            if unique_parts:
                preparation_lines.append(f"{title}\n" + "\n\n".join(unique_parts))
            else:
                preparation_lines.append(title)
        detail_sections.append("Preparation\n" + ("\n\n".join(unique_non_empty_text(preparation_lines)) if preparation_lines else "No preparation items saved."))

        bible_lines: list[str] = []
        for item in bible_items:
            title = str(item.get("title") or "Bible").strip()
            subtitle = str(item.get("subtitle") or "").strip()
            verses = unique_non_empty_text(list(item.get("verses") or []))
            text_value = str(item.get("text") or "").strip()
            block_lines = [title]
            if subtitle:
                block_lines.append(subtitle)
            if text_value:
                block_lines.append(text_value)
            if verses:
                block_lines.append("\n".join(verses))
            bible_lines.append("\n".join(unique_non_empty_text(block_lines)))
        detail_sections.append("Bible\n" + ("\n\n".join(unique_non_empty_text(bible_lines)) if bible_lines else "No bible items saved."))
        return "\n\n".join(detail_sections)

    def render_selected_history_entry(_event=None) -> None:
        """Replace the History detail pane with the selected service record.

        What: Validates popup, widgets, and selection; finds the matching cached entry
        and inserts its formatted text into the disabled Text widget.
        Why included: Treeview rows only show compact labels; users need full details.
        Where used: History Treeview selection binding and initial row selection.
        Returns: None; leaves the pane unchanged when no valid selection exists.
        """
        history_window = history_popup_state.get("window")
        history_tree = history_popup_state.get("tree")
        details_widget = history_popup_state.get("details")
        if not isinstance(history_window, tk.Toplevel) or not history_window.winfo_exists():
            return
        if history_tree is None or details_widget is None:
            return
        selection = history_tree.selection()
        if not selection:
            return
        selected_id = str(selection[0])
        entry = next((item for item in list(history_popup_state.get("entries") or []) if str(item.get("id") or "") == selected_id), None)
        if entry is None:
            return
        details_widget.configure(state="normal")
        details_widget.delete("1.0", tk.END)
        details_widget.insert("1.0", build_history_details_text(entry))
        details_widget.configure(state="disabled")

    def delete_selected_history_entry() -> None:
        """What: Delete only the selected History record after explicit confirmation.

        Why included: Same-slot saved versions need independent, user-controlled removal.
        Where used: History popup Delete Selected Service button.
        Returns: None; deletes by record ID in a worker and refreshes History.
        """
        history_tree = history_popup_state.get("tree")
        delete_button = history_popup_state.get("delete_button")
        if history_tree is None:
            return
        selection = history_tree.selection()
        if not selection:
            messagebox.showinfo("Delete Saved Service", "Select a saved service first.", parent=history_popup_state.get("window") or root)
            return
        selected_id = str(selection[0])
        selected_entry = next((entry for entry in list(history_popup_state.get("entries") or []) if str(entry.get("id") or "") == selected_id), None)
        if selected_entry is None:
            return
        label = format_history_entry_label(selected_entry)
        if not messagebox.askyesno("Delete Saved Service", f"Permanently delete this saved service?\n\n{label}\n\nOnly this selected history record will be deleted.", parent=history_popup_state.get("window") or root):
            return
        if delete_button is not None:
            delete_button.configure(state="disabled")

        def delete_worker() -> bool:
            """What: Delete the selected History row by exact database ID.

            Why included: SQLite deletion must run off Tk's UI thread.
            Where used: HistoryDelete background task.
            Returns: True when the selected row existed and was deleted.
            """
            return delete_service_entry(selected_id)

        def delete_succeeded(deleted: bool) -> None:
            """What: Refresh History after a successful targeted deletion.

            Why included: Deleted versions must disappear without disturbing siblings.
            Where used: HistoryDelete success callback.
            Returns: None; updates the tree and reports the result.
            """
            if delete_button is not None:
                delete_button.configure(state="normal")
            if not deleted:
                messagebox.showwarning("Delete Saved Service", "That history record no longer exists.", parent=history_popup_state.get("window") or root)
                refresh_history_tree()
                return
            if service_restore_state.get("selected_entry_id") == selected_id:
                service_restore_state["selected_entry_id"] = None
            refresh_history_tree()
            messagebox.showinfo("Delete Saved Service", "The selected saved service was deleted.", parent=history_popup_state.get("window") or root)

        def delete_failed(error: BaseException) -> None:
            """What: Re-enable deletion and display its failure reason.

            Why included: History controls must recover after a failed database write.
            Where used: HistoryDelete error callback.
            Returns: None; restores the button and reports the exception.
            """
            if delete_button is not None:
                delete_button.configure(state="normal")
            messagebox.showerror("Delete Saved Service", str(error), parent=history_popup_state.get("window") or root)

        start_background_task("HistoryDelete", delete_worker, on_success=delete_succeeded, on_error=delete_failed)

    def refresh_history_tree() -> None:
        """Clear stale History content and request the latest saved records.

        What: Resets rows and details to a loading state, increments a request token,
        then starts a worker with success and error handlers.
        Why included: Database reads should not freeze the popup, and token checks
        prevent old results from overwriting a newer refresh.
        Where used: History popup creation, popup reactivation, and refresh action.
        Returns: None; results are applied asynchronously on Tk's thread.
        """
        history_tree = history_popup_state.get("tree")
        details_widget = history_popup_state.get("details")
        if history_tree is None:
            return
        for item_id in history_tree.get_children():
            history_tree.delete(item_id)
        if details_widget is not None:
            details_widget.configure(state="normal")
            details_widget.delete("1.0", tk.END)
            details_widget.insert("1.0", "Loading history...")
            details_widget.configure(state="disabled")
        request_token = int(history_popup_state.get("request_token") or 0) + 1
        history_popup_state["request_token"] = request_token

        def load_history_entries() -> list[dict[str, object]]:
            """Read recent service entries on the worker thread and log query time.

            What: Loads up to 250 history mappings from persistence.
            Why included: SQLite I/O must not block interaction with the popup.
            Where used: Worker callable started by refresh_history_tree().
            Returns: The ordered service-history entries.
            """
            started_at = time.perf_counter()
            entries = list_service_history_entries(limit=250)
            log_timing("main.list_service_history_entries", (time.perf_counter() - started_at) * 1000.0, threshold_ms=15.0, details=f"entries={len(entries)}")
            return entries

        def apply_history_entries(entries: list[dict[str, object]], expected_token=request_token) -> None:
            """Populate History rows if this response belongs to the latest refresh.

            What: Caches entries, inserts labeled Treeview rows, and selects the first
            row; displays an empty-state message when no records are returned.
            Why included: Refreshes can overlap and only the newest result is current.
            Where used: on_success callback for the history-loading worker.
            Returns: None; updates popup widgets in place.
            """
            if expected_token != int(history_popup_state.get("request_token") or 0):
                return
            active_tree = history_popup_state.get("tree")
            active_details = history_popup_state.get("details")
            if active_tree is None:
                return
            history_popup_state["entries"] = entries
            for entry in entries:
                active_tree.insert("", "end", iid=str(entry.get("id") or ""), text=format_history_entry_label(entry), values=(str(entry.get("saved_at") or ""),))
            if entries:
                first_id = str(entries[0].get("id") or "")
                if first_id:
                    active_tree.selection_set(first_id)
                    active_tree.focus(first_id)
                    render_selected_history_entry()
            elif active_details is not None:
                active_details.configure(state="normal")
                active_details.delete("1.0", tk.END)
                active_details.insert("1.0", "No saved history found.")
                active_details.configure(state="disabled")

        def handle_history_error(error: BaseException, expected_token=request_token) -> None:
            """Display a current History query failure in the details pane.

            What: Ignores stale request tokens and writes the error message when current.
            Why included: An older failed request should not replace newer popup content.
            Where used: on_error callback for the history-loading worker.
            Returns: None; updates the details widget when it is available.
            """
            if expected_token != int(history_popup_state.get("request_token") or 0):
                return
            active_details = history_popup_state.get("details")
            if active_details is not None:
                active_details.configure(state="normal")
                active_details.delete("1.0", tk.END)
                active_details.insert("1.0", f"Failed to load history.\n\n{error}")
                active_details.configure(state="disabled")

        start_background_task("HistoryLoader", load_history_entries, on_success=apply_history_entries, on_error=handle_history_error)

    def open_history_window() -> None:
        """Show the History popup, creating it once and refreshing its records.

        What: Raises an existing popup and refreshes it, or constructs the window,
        Treeview/detail panes, bindings, and close handling before loading records.
        Why included: Provides browsing and inspection of persisted service snapshots.
        Where used: History menu command.
        Returns: None; stores popup widgets in history_popup_state.
        """
        existing_window = history_popup_state.get("window")
        if isinstance(existing_window, tk.Toplevel) and existing_window.winfo_exists():
            existing_window.deiconify()
            existing_window.lift()
            existing_window.focus_force()
            refresh_history_tree()
            return
        popup = tk.Toplevel(root)
        popup.title("History")
        popup.minsize(980, 620)
        popup.transient(root)

        wrapper = tk.Frame(popup)
        wrapper.pack(expand=True, fill="both", padx=12, pady=12)
        wrapper.columnconfigure(0, weight=3, uniform="history_columns")
        wrapper.columnconfigure(1, weight=5, uniform="history_columns")
        wrapper.rowconfigure(0, weight=1)

        tree_frame = tk.LabelFrame(wrapper, text="Saved Services", padx=10, pady=10)
        tree_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        details_frame = tk.LabelFrame(wrapper, text="History Details", padx=10, pady=10)
        details_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        details_frame.columnconfigure(0, weight=1)
        details_frame.rowconfigure(0, weight=1)

        history_tree = ttk.Treeview(tree_frame, columns=("saved_at",), show="tree headings", selectmode="browse")
        history_tree.heading("#0", text="Service")
        history_tree.heading("saved_at", text="Saved At")
        history_tree.column("#0", width=260, stretch=True)
        history_tree.column("saved_at", width=150, stretch=False, anchor="w")
        history_tree.grid(row=0, column=0, sticky="nsew")
        tree_scrollbar = tk.Scrollbar(tree_frame, orient="vertical", command=history_tree.yview)
        tree_scrollbar.grid(row=0, column=1, sticky="ns")
        history_tree.configure(yscrollcommand=tree_scrollbar.set)
        delete_history_button = ttk.Button(tree_frame, text="Delete Selected Service...", command=delete_selected_history_entry)
        delete_history_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        details_text = tk.Text(details_frame, wrap="word", state="disabled")
        details_text.grid(row=0, column=0, sticky="nsew")
        details_scrollbar = tk.Scrollbar(details_frame, orient="vertical", command=details_text.yview)
        details_scrollbar.grid(row=0, column=1, sticky="ns")
        details_text.configure(yscrollcommand=details_scrollbar.set)

        apply_layout_style_for_popup(
            [popup, wrapper, tree_frame, details_frame, history_tree, tree_scrollbar, details_text, details_scrollbar],
            label_widgets=[tree_frame, details_frame],
            input_widgets=[history_tree, details_text],
        )

        history_tree.bind("<<TreeviewSelect>>", render_selected_history_entry, add="+")
        history_popup_state["window"] = popup
        history_popup_state["tree"] = history_tree
        history_popup_state["details"] = details_text
        history_popup_state["delete_button"] = delete_history_button
        refresh_history_tree()

        def clear_history_popup_reference() -> None:
            """Release History widget references as the popup is closed.

            What: Clears cached window, tree, detail, and entry state, then destroys
            the popup window.
            Why included: A later open must not reuse destroyed Tk widgets.
            Where used: WM_DELETE_WINDOW handler installed by open_history_window().
            Returns: None; resets History popup state.
            """
            history_popup_state["window"] = None
            history_popup_state["tree"] = None
            history_popup_state["details"] = None
            history_popup_state["delete_button"] = None
            history_popup_state["entries"] = []
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", clear_history_popup_reference)

    def open_members_window() -> None:
        """Open or raise the singleton member-management popup.

        What: Raises the cached live window, or creates the popup with shared theme,
        style, and date-picker callbacks and registers close cleanup.
        Why included: Member management is a separate workflow but shares main-app
        appearance and date selection behavior.
        Where used: Members command in the main menu bar.
        Returns: None; tracks the popup in members_popup_state.
        """
        existing_window = members_popup_state.get("window")
        if isinstance(existing_window, tk.Toplevel) and existing_window.winfo_exists():
            existing_window.deiconify()
            existing_window.lift()
            existing_window.focus_force()
            return
        popup = open_members_popup(
            root,
            lambda widgets, label_widgets=None, input_widgets=None: apply_layout_style_for_popup(widgets, label_widgets, input_widgets),
            lambda: get_theme_by_name(selected_theme.get()),
            lambda current_parent, current_var: open_date_picker(
                current_parent,
                current_var,
                selected_theme.get(),
                selected_font_family.get(),
                selected_font_style.get(),
                selected_font_size.get(),
            ),
        )
        members_popup_state["window"] = popup

        def clear_popup_reference() -> None:
            """Clear the singleton reference before destroying the Members popup.

            What: Resets the tracked window reference and destroys the popup.
            Why included: Later menu commands must create a fresh window rather than
            attempt to raise a destroyed one.
            Where used: WM_DELETE_WINDOW handler installed by open_members_window().
            Returns: None; resets member popup state.
            """
            members_popup_state["window"] = None
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", clear_popup_reference)

    def apply_layout_style_for_popup(widgets, label_widgets=None, input_widgets=None) -> None:
        """Apply main-window palette/font roles to an independently built popup.

        What: Applies the current theme to supplied widgets and assigns label/input
        font roles to the corresponding widget groups.
        Why included: Popup modules create controls outside the tab registries but
        must stay visually consistent with live Theme and Font settings.
        Where used: Member and printer popup builders.
        Returns: None; updates all supplied widgets in place.
        """
        theme = get_theme_by_name(selected_theme.get())
        _, label_font, input_font = get_ui_font_set(selected_font_family.get(), selected_font_style.get(), selected_font_size.get())
        for widget in widgets:
            set_widget_colors(widget, theme)
        for widget in label_widgets or []:
            set_widget_font(widget, label_font)
        for widget in input_widgets or []:
            set_widget_font(widget, input_font)

    def handle_theme_change(theme_name: str) -> None:
        """Apply a selected palette throughout the app and persist preferences.

        What: Recolors registered UI, refreshes Display's dynamic styles, and saves
        all current UI and renderer settings.
        Why included: Theme changes must update both shared controls and tab-owned
        styles while surviving the next application launch.
        Where used: Theme menu radio-button commands.
        Returns: None; updates widgets and settings storage.
        """
        apply_theme(root, container, top_sections, bottom_section, menu_widgets, theme_name)
        refresh_dynamic_styles = bottom_section.get("display_state", {}).get("refresh_dynamic_styles") if isinstance(bottom_section.get("display_state"), dict) else None
        if callable(refresh_dynamic_styles):
            refresh_dynamic_styles()
        save_ui_settings(selected_theme.get(), selected_font_family.get(), selected_font_style.get(), selected_font_size.get(), selected_display_font_family.get(), selected_display_font_style.get(), selected_display_outline_size.get(), selected_display_text_color.get(), selected_display_key_size.get())

    def handle_font_change() -> None:
        """Apply and persist the selected main-interface font configuration.

        What: Updates registered fonts, refreshes Display's dynamic styles, and saves
        current application and renderer preferences.
        Why included: Font menu changes affect both static widgets and dynamically
        rendered tab content.
        Where used: Main Font family, style, and size menu commands.
        Returns: None; updates widgets and settings storage.
        """
        apply_font_settings(ui_parts, selected_font_family.get(), selected_font_style.get(), selected_font_size.get())
        refresh_dynamic_styles = bottom_section.get("display_state", {}).get("refresh_dynamic_styles") if isinstance(bottom_section.get("display_state"), dict) else None
        if callable(refresh_dynamic_styles):
            refresh_dynamic_styles()
        save_ui_settings(selected_theme.get(), selected_font_family.get(), selected_font_style.get(), selected_font_size.get(), selected_display_font_family.get(), selected_display_font_style.get(), selected_display_outline_size.get(), selected_display_text_color.get(), selected_display_key_size.get())

    def handle_display_settings_change() -> None:
        """Apply display typography/color choices to previews and external output.

        What: Updates renderer settings, refreshes Display and tab previews, redraws
        the mirror preview, pushes enabled output, and saves preferences.
        Why included: Display controls alter rendered content outside normal UI styling.
        Where used: Display Settings menu commands and recalled service settings.
        Returns: None; synchronizes renderer state, previews, and persistence.
        """
        set_display_font(selected_display_font_family.get(), selected_display_font_style.get())
        set_display_outline_width(selected_display_outline_size.get())
        set_display_text_color(selected_display_text_color.get())
        set_display_key_size(selected_display_key_size.get())
        display_state = bottom_section.get("display_state")
        refresh_render_settings = display_state.get("refresh_render_settings") if isinstance(display_state, dict) else None
        if callable(refresh_render_settings):
            refresh_render_settings()
        for state_key in ("welcome_state", "preparation_state", "library_state"):
            tab_state = tabs_content.get(state_key)
            refresh_preview = tab_state.get("refresh_preview") if isinstance(tab_state, dict) else None
            if callable(refresh_preview):
                refresh_preview()
        mirror_controller.render_preview()
        if hasattr(mirror_controller, "enabled_var") and mirror_controller.enabled_var.get():
            mirror_controller.last_secondary_signature = None
            mirror_controller.push_to_secondary()
        save_ui_settings(selected_theme.get(), selected_font_family.get(), selected_font_style.get(), selected_font_size.get(), selected_display_font_family.get(), selected_display_font_style.get(), selected_display_outline_size.get(), selected_display_text_color.get(), selected_display_key_size.get())

    def apply_service_display_settings(settings: object) -> None:
        """Restore only recognized appearance values from a service snapshot.

        What: Validates saved theme/font/size/display choices against supported option
        sets, applies valid values, then refreshes UI and renderer settings.
        Why included: Saved snapshots may be incomplete, malformed, or from older
        versions and must not inject unsupported UI values.
        Where used: Registered as Display's restore_display_settings callback and
        called by service recall handling.
        Returns: None; invalid or absent fields leave their current values unchanged.
        """
        if not isinstance(settings, dict) or not settings:
            return
        theme_name = str(settings.get("theme") or "")
        if theme_name in THEMES:
            selected_theme.set(theme_name)
        font_family = str(settings.get("font_family") or "")
        if font_family in FONT_FAMILIES:
            selected_font_family.set(font_family)
        font_style = str(settings.get("font_style") or "")
        if font_style in FONT_STYLES:
            selected_font_style.set(font_style)
        try:
            selected_font_size.set(max(8, min(32, int(settings.get("font_size") or selected_font_size.get()))))
        except (TypeError, ValueError):
            pass
        display_family = str(settings.get("display_font_family") or "")
        if display_family in FONT_FAMILIES:
            selected_display_font_family.set(display_family)
        display_style = str(settings.get("display_font_style") or "").lower()
        if display_style in FONT_STYLES.values():
            selected_display_font_style.set(display_style)
        outline_size = str(settings.get("display_outline_size") or "")
        if outline_size in DISPLAY_OUTLINE_OPTIONS:
            selected_display_outline_size.set(outline_size)
        text_color = str(settings.get("display_text_color") or "").strip().title()
        if text_color in DISPLAY_TEXT_COLOR_OPTIONS:
            selected_display_text_color.set(text_color)
        apply_theme(root, container, top_sections, bottom_section, menu_widgets, selected_theme.get())
        apply_font_settings(ui_parts, selected_font_family.get(), selected_font_style.get(), selected_font_size.get())
        handle_display_settings_change()

    display_state = bottom_section.get("display_state")
    if isinstance(display_state, dict):
        display_state["restore_display_settings"] = apply_service_display_settings

    if isinstance(top_sections[0], dict):
        for widget in top_sections[0].get("input_widgets") or []:
            if isinstance(widget, tk.Button) and str(widget.cget("text")) == "Print":
                widget.configure(state="normal", command=open_print_window)
            if isinstance(widget, tk.Button) and str(widget.cget("text")) == "Save":
                widget.configure(state="normal", command=save_current_service)

    settings_dialog_state: dict[str, object] = {"window": None}

    def open_settings_dialog() -> None:
        """What: Open the tabbed Settings window for main and secondary screen appearance.

        Why included: Theme, main-screen fonts, and live-screen settings share one popup
        to keep the menu bar short; every change applies immediately like the old menus.
        Where used: Settings entry in the menu bar.
        Returns: None; re-focuses the existing window instead of opening a second one.
        """
        existing = settings_dialog_state.get("window")
        if isinstance(existing, tk.Toplevel) and existing.winfo_exists():
            existing.lift()
            existing.focus_set()
            return
        dialog = tk.Toplevel(root)
        settings_dialog_state["window"] = dialog
        dialog.title("Settings")
        dialog.transient(root)
        apply_application_icon(dialog)
        notebook = ttk.Notebook(dialog)
        notebook.pack(fill="both", expand=True, padx=12, pady=(12, 8))
        main_tab = ttk.Frame(notebook, padding=(16, 14))
        secondary_tab = ttk.Frame(notebook, padding=(16, 14))
        notebook.add(main_tab, text="Main Screen")
        notebook.add(secondary_tab, text="Secondary Screen")
        main_tab.columnconfigure(1, weight=1)
        secondary_tab.columnconfigure(1, weight=1)

        def add_choice_row(parent: ttk.Frame, row: int, label_text: str, values: list[str], current_value: str, apply_choice) -> None:
            """What: Add one labeled read-only combobox that applies its selection immediately.

            Why included: All settings rows share the same label/selector/apply pattern.
            Where used: Main Screen and Secondary Screen tab construction below.
            Returns: None; the combobox applies changes through apply_choice.
            """
            ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky="w", pady=(0, 10))
            box = ttk.Combobox(parent, state="readonly", values=values, width=28)
            box.set(current_value)
            box.grid(row=row, column=1, sticky="ew", padx=(14, 0), pady=(0, 10))
            box.bind("<<ComboboxSelected>>", lambda _event: apply_choice(box.get()))

        def apply_theme_choice(value: str) -> None:
            """What: Apply the chosen theme name from the Settings window.

            Why included: The Theme dropdown must update the variable and recolor the app in one step.
            Where used: Theme row in the Settings window's Main Screen tab.
            Returns: None.
            """
            selected_theme.set(value)
            handle_theme_change(value)

        def apply_font_choice(variable: tk.Variable, value: object) -> None:
            """What: Store a main-screen font choice and reapply fonts.

            Why included: Family, style, and size rows share one apply path.
            Where used: Main Screen tab rows. Returns: None.
            """
            variable.set(value)
            handle_font_change()

        add_choice_row(main_tab, 0, "Theme", list(THEMES), selected_theme.get(), apply_theme_choice)
        add_choice_row(main_tab, 1, "Font family", list(FONT_FAMILIES), selected_font_family.get(),
            lambda value: apply_font_choice(selected_font_family, value))
        add_choice_row(main_tab, 2, "Font style", list(FONT_STYLES), selected_font_style.get(),
            lambda value: apply_font_choice(selected_font_style, value))
        add_choice_row(main_tab, 3, "Font size", [str(size) for size in FONT_SIZES], str(selected_font_size.get()),
            lambda value: apply_font_choice(selected_font_size, int(value)))

        display_style_names = {value: name for name, value in FONT_STYLES.items()}

        def apply_display_choice(variable: tk.Variable, value: object) -> None:
            """What: Store a live-screen choice and reapply display settings.

            Why included: Every Secondary Screen row shares one apply path.
            Where used: Secondary Screen tab rows. Returns: None.
            """
            variable.set(value)
            handle_display_settings_change()

        add_choice_row(secondary_tab, 0, "Script family", list(FONT_FAMILIES), selected_display_font_family.get(),
            lambda value: apply_display_choice(selected_display_font_family, value))
        add_choice_row(secondary_tab, 1, "Script style", list(FONT_STYLES),
            display_style_names.get(selected_display_font_style.get(), selected_display_font_style.get().title()),
            lambda value: apply_display_choice(selected_display_font_style, FONT_STYLES.get(value, str(value).lower())))
        add_choice_row(secondary_tab, 2, "Outline size", list(DISPLAY_OUTLINE_OPTIONS), selected_display_outline_size.get(),
            lambda value: apply_display_choice(selected_display_outline_size, value))
        add_choice_row(secondary_tab, 3, "Text color", list(DISPLAY_TEXT_COLOR_OPTIONS), selected_display_text_color.get(),
            lambda value: apply_display_choice(selected_display_text_color, value))
        add_choice_row(secondary_tab, 4, "Key size", ["None"] + [f"{size} px" for size in DISPLAY_KEY_SIZE_OPTIONS],
            "None" if int(selected_display_key_size.get() or 0) <= 0 else f"{selected_display_key_size.get()} px",
            lambda value: apply_display_choice(selected_display_key_size,
                0 if str(value).strip().casefold() == "none" else int(str(value).split()[0])))

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="right")

    history_menu.add_command(label="Open History", command=open_history_window)
    menu_bar.add_command(label="Members", command=open_members_window)
    menu_bar.add_cascade(label="Help", menu=help_menu)

    apply_theme(root, container, top_sections, bottom_section, menu_widgets, selected_theme.get())
    apply_font_settings(ui_parts, selected_font_family.get(), selected_font_style.get(), selected_font_size.get())
    mirror_controller.render_preview()

    def report_data_health_issues(issues) -> None:
        """What: Warn the operator about failed database integrity checks.

        Why included: Corruption should surface before a service, not during one.
        Where used: Success callback of the DataHealthBackup background task.
        Returns: None; healthy results stay silent.
        """
        if issues:
            messagebox.showwarning("Data Health", "Database checks reported problems:\n\n" + "\n".join(issues) +
                "\n\nRestore from Data > Import Databases... or a backup in the shared data folder's .backups directory.", parent=root)

    root.after(5000, lambda: start_background_task("DataHealthBackup",
        lambda *_args: run_daily_backup(get_app_data_path()),
        on_success=report_data_health_issues,
        on_error=lambda error: log_exception("main.data_health_backup", error)))

    def handle_app_close() -> None:
        """Shut down background/UI activity and release application resources.

        What: Marks app state as closing, clears tracked tasks, closes the mirror
        controller, clears cached backgrounds, and destroys the root window.
        Why included: Pending UI callbacks and display workers must not use widgets
        or media resources after the application exits.
        Where used: Root window WM_DELETE_WINDOW protocol handler.
        Returns: None; closes the application window and its display resources.
        """
        app_state["closing"] = True
        with background_task_lock:
            active_background_tasks.clear()
        spell_checker.close()
        mirror_controller.close()
        clear_background_cache()
        try:
            checkpoint_databases(get_app_data_path())
        except Exception as error:
            log_exception("main.shutdown_checkpoint", error)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", handle_app_close)
    if getattr(sys, "frozen", False) and not bool(get_music_licenses().get("initial_prompt_completed")):
        root.after(0, lambda: open_music_license_settings(first_run=True))
    return root


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--prepare-update":
        from windows_update import run_elevated_preparation
        sys.exit(run_elevated_preparation(Path(sys.argv[2]), Path(sys.argv[3]), get_app_base_path()))
    app = build_ui()
    app.mainloop()
