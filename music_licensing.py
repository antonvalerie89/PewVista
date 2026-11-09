"""Song license choices and shared CCLI and OneLicense number storage."""

from __future__ import annotations

import json
from pathlib import Path

from app_paths import get_app_data_path


PUBLIC_DOMAIN_LABEL = "Public Domain"
# Public Domain is the default: it is stored as "no license" and shows nothing on screen.
MUSIC_LICENSE_PROVIDERS = ("CCLI", "OneLicense", "Individual")
MUSIC_LICENSE_CHOICES = (PUBLIC_DOMAIN_LABEL, *MUSIC_LICENSE_PROVIDERS)
# Songs used with individual permission need no church license number; they show this notice.
UNNUMBERED_PROVIDER_NOTICES = {"Individual": "Used with permission"}
MUSIC_LICENSE_FILENAME = "music_licenses.json"


def normalize_license_provider(value: object) -> str:
    """Normalize a saved song's provider to a supported selector value.

    What: Matches CCLI, OneLicense, or Individual case-insensitively; Public Domain means none.
    Why included: Old, edited, or malformed song rows must not create invalid choices.
    Where used: Library database reads/writes and song editor selection.
    Returns: Canonical provider name, or an empty string for Public Domain / no license.
    """
    normalized = str(value or "").strip().casefold().replace(" ", "")
    return {
        "ccli": "CCLI",
        "onelicense": "OneLicense",
        "individual": "Individual",
    }.get(normalized, "")


def format_music_license_attribution(provider: object, number: object, author: object = "", info: object = "") -> str:
    """Format provider number and author lines for a live song.

    What: Combines the song author with its license number, or with a fixed notice for
    songs used with individual permission (their own license text, if entered). Public
    Domain songs show nothing.
    Why included: License notices should identify both the registered license and author.
    Where used: Library preview and mirror/OBS corner attribution.
    Returns: Newline-separated attribution, or empty text when no values are available.
    """
    normalized_provider = normalize_license_provider(provider)
    normalized_number = str(number or "").strip()
    normalized_author = str(author or "").strip()
    lines = []
    if normalized_author:
        lines.append(f"Author: {normalized_author}")
    if normalized_provider in UNNUMBERED_PROVIDER_NOTICES:
        lines.append(str(info or "").strip() or UNNUMBERED_PROVIDER_NOTICES[normalized_provider])
    elif normalized_provider and normalized_number:
        label = "CCLI License" if normalized_provider == "CCLI" else "OneLicense"
        lines.append(f"{label}: {normalized_number}")
    return "\n".join(lines)


def load_music_license_settings(data_directory: Path | None = None) -> dict[str, object]:
    """Load shared copyright license numbers and first-run prompt state.

    What: Reads and validates the application's music_licenses.json settings.
    Why included: License numbers are shared across users and installations via app data.
    Where used: First-run setup, the Music Licenses menu, and live song attribution.
    Returns: A mapping containing ccli, onelicense, and initial_prompt_completed.
    """
    path = (Path(data_directory) if data_directory is not None else get_app_data_path()) / MUSIC_LICENSE_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {
        "ccli": str(value.get("ccli") or "").strip(),
        "onelicense": str(value.get("onelicense") or "").strip(),
        "initial_prompt_completed": bool(value.get("initial_prompt_completed", False)),
    }


def save_music_license_settings(settings: dict[str, object], data_directory: Path | None = None) -> Path:
    """Persist both provider numbers and the first-run prompt decision.

    What: Writes normalized license settings through a sibling temporary JSON file.
    Why included: Numbers must survive application restarts without entering program files.
    Where used: Save and Not Now actions in the Music Licenses dialog.
    Returns: Path to the saved settings file.
    """
    root = Path(data_directory) if data_directory is not None else get_app_data_path()
    root.mkdir(parents=True, exist_ok=True)
    path = root / MUSIC_LICENSE_FILENAME
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "ccli": str(settings.get("ccli") or "").strip(),
        "onelicense": str(settings.get("onelicense") or "").strip(),
        "initial_prompt_completed": bool(settings.get("initial_prompt_completed", True)),
    }
    temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary_path.replace(path)
    return path