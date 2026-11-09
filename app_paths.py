"""Resolve shared paths for source runs and PyInstaller desktop builds."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PRODUCT_NAME = "PewVista"

_SETTINGS_FILE_CACHE: dict[str, tuple[int, dict[str, object]]] = {}


def _read_settings_file(settings_path: Path) -> dict[str, object]:
    """What: Read a small JSON settings file through an mtime-validated cache.

    Why included: Branding resolution runs on hot render paths; repeat calls must
    cost a stat() instead of a read and JSON parse.
    Where used: get_installation_settings() and get_branding_settings().
    Returns: Cached or freshly parsed mapping; an empty mapping when missing/invalid.
    """
    key = str(settings_path)
    try:
        mtime = settings_path.stat().st_mtime_ns
    except OSError:
        _SETTINGS_FILE_CACHE.pop(key, None)
        return {}
    cached = _SETTINGS_FILE_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        value = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    _SETTINGS_FILE_CACHE[key] = (mtime, value)
    return value


def get_app_base_path() -> Path:
    """Resolve the application root for source and frozen executions.

    What: Returns the executable directory for a PyInstaller build or the source
    project directory when running the Python entry point.
    Why included: Bundled resources stay beside the executable while shared app data
    is resolved separately through get_app_data_path().
    Where used: Path builders in display_bridge.py, members.py, perf_logging.py,
    tab_library.py, tab_welcome.py, and other modules that access app resources.
    Returns: Resolved Path to the application root directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_installation_settings(app_base_path: Path | None = None) -> dict[str, object]:
    """What: Read optional machine-wide branding/background settings written by setup.

    Why included: Installer choices should be shared by every Windows account.
    Where used: Window title, default presentation background, and shared data path.
    Returns: Valid installation mapping, or an empty mapping for source/legacy runs.
    """
    root = Path(app_base_path) if app_base_path is not None else get_app_base_path()
    return dict(_read_settings_file(root / "installation.json"))


def get_app_data_path(app_base_path: Path | None = None) -> Path:
    """Resolve writable app data, using shared ProgramData for installed products.

    What: Uses the setup-configured shared folder, or local ``Data`` for source/legacy runs.
    Why included: Program Files is read-only to standard users and customer accounts share one database.
    Where used: Database, log, settings, cache, and import path builders.
    Returns: Data directory path; callers create it when they write.
    """
    root = Path(app_base_path) if app_base_path is not None else get_app_base_path()
    settings = get_installation_settings(root)
    configured = str(settings.get("data_directory") or "").strip()
    if configured:
        shared_path = Path(configured)
        if shared_path.is_absolute():
            return shared_path
    return root / "Data"


def resolve_background_file(value: str | Path | None, app_base_path: Path | None = None, data_path: Path | None = None) -> Path | None:
    """What: Resolve a saved media reference to an existing file, following background migration.

    Why included: Older installs kept backgrounds beside the program; saved paths must still find the moved files.
    Where used: Background resolution in main.py, the mirror, the Bible tab, and data transfers.
    Returns: Existing file path, or None when the media cannot be found.
    """
    if not str(value or "").strip():
        return None
    root = Path(app_base_path) if app_base_path is not None else get_app_base_path()
    data = Path(data_path) if data_path is not None else get_app_data_path(root)
    candidate = Path(str(value).strip())
    original = candidate if candidate.is_absolute() else root / candidate
    if original.is_file():
        return original
    legacy_root = (root / "Backgrounds").resolve()
    legacy_path = original.resolve()
    try:
        relative = legacy_path.relative_to(legacy_root)
    except ValueError:
        shared_candidate = data / candidate if not candidate.is_absolute() else candidate
        return shared_candidate if shared_candidate.is_file() else None
    shared_root = (data / "Backgrounds").resolve()
    receipt_path = data / ".background-migration.json"
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            records = receipt.get("files", {})
            record = records.get(str(legacy_path))
            if record is None:
                normalized = os.path.normcase(os.path.normpath(str(legacy_path)))
                record = next((item for name, item in records.items() if os.path.normcase(os.path.normpath(name)) == normalized), None)
            if record is not None:
                target = Path(record["target"]).resolve()
                return target if target.is_relative_to(shared_root) and target.is_file() else None
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None
    target = shared_root / relative
    return target if target.is_file() else None


def get_branding_settings(app_base_path: Path | None = None) -> dict[str, object]:
    """What: Merge installer branding with in-app overrides stored in shared data.

    Why included: Standard users cannot edit installation.json under Program Files, and
    installations from before the PewVista rename never had one; shared data survives updates.
    Where used: Display name, default background, icon resolution, and the Help branding editor.
    Returns: Installation mapping with branding.json keys replacing installer values.
    """
    root = Path(app_base_path) if app_base_path is not None else get_app_base_path()
    settings: dict[str, object] = dict(get_installation_settings(root))
    overrides = _read_settings_file(get_app_data_path(root) / "branding.json")
    for key in ("church_name", "default_background_path", "custom_icon_path"):
        if key in overrides:
            settings[key] = str(overrides.get(key) or "").strip()
    return settings


def save_branding_settings(values: dict[str, str], app_base_path: Path | None = None) -> None:
    """What: Persist in-app branding choices to branding.json in the shared data folder.

    Why included: Branding edits must survive every update and avoid read-only Program Files.
    Where used: The Help menu's church name and background editor.
    Returns: None; creates the data folder and branding.json when missing.
    """
    root = Path(app_base_path) if app_base_path is not None else get_app_base_path()
    data_directory = get_app_data_path(root)
    data_directory.mkdir(parents=True, exist_ok=True)
    overrides_path = data_directory / "branding.json"
    try:
        existing = json.loads(overrides_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    for key in ("church_name", "default_background_path", "custom_icon_path"):
        if key in values:
            existing[key] = str(values.get(key) or "").strip()
    overrides_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def get_app_display_name(app_base_path: Path | None = None) -> str:
    """What: Return the church/program name chosen during installation or in the app.

    Why included: Customers can brand their shared installation without changing its package identity.
    Where used: Main window and generated desktop/Start Menu shortcuts.
    Returns: Configured church name, or a neutral first-run label.
    """
    value = str(get_branding_settings(app_base_path).get("church_name") or "").strip()
    return value or "Your Church"


def get_default_background_path(app_base_path: Path | None = None) -> Path:
    """Resolve the installer's optional shared background or bundled default.

    What: Selects a valid configured image, then falls back to the packaged church image.
    Why included: A custom background must be shared across user accounts and restored if missing.
    Where used: Main path resolution and the mirror controller's initial background.
    Returns: Existing custom/bundled background path, or the bundled path if neither exists.
    """
    root = Path(app_base_path) if app_base_path is not None else get_app_base_path()
    configured = str(get_branding_settings(root).get("default_background_path") or "").strip()
    candidate = Path(configured) if configured else None
    if candidate is not None and candidate.is_absolute() and candidate.is_file():
        return candidate
    return root / "Resourses" / "PewVista.png"


def get_app_icon_path(app_base_path: Path | None = None) -> Path:
    """Resolve the installer's optional custom icon or the bundled PewVista icon.

    What: Selects a valid configured ``.ico``, then falls back to the packaged PewVista icon.
    Why included: An icon chosen at installation must brand every account's windows and shortcuts.
    Where used: Root-window icon loading and generated desktop/Start Menu shortcuts.
    Returns: Existing custom icon path, or the bundled ``PewVista.ico`` path.
    """
    root = Path(app_base_path) if app_base_path is not None else get_app_base_path()
    configured = str(get_branding_settings(root).get("custom_icon_path") or "").strip()
    candidate = Path(configured) if configured else None
    if candidate is not None and candidate.is_absolute() and candidate.is_file():
        return candidate
    return root / "Resourses" / "PewVista.ico"
