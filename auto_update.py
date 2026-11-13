"""Check a public update feed, download the newest signed update ZIP, and publish the feed.

The feed is a small JSON file (latest.json) next to the update ZIP on a public download host:
    {"version": "20261109-202626-123", "url": "https://.../PewVista-Update-Windows-x64.zip",
     "sha256": "<hex>", "size": 123, "notes": "What's new"}
The ZIP itself is still signature-checked by windows_update.prepare_update_archive() before
anything is installed, so a compromised host cannot install altered files.

Publisher command (run from the source workspace after packaging a release):
    python auto_update.py feed <release folder> <public base URL> [--notes "What's new"]
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app_paths import get_app_data_path

# Public folder that holds latest.json and the update ZIP; blank disables automatic checks.
UPDATE_FEED_URL = "https://github.com/antonvalerie89/PewVista/releases/latest/download"
FEED_FILE_NAME = "latest.json"
UPDATE_FILE_NAME = "PewVista-Update-Windows-x64.zip"
CHECK_INTERVAL = timedelta(hours=20)
_STATE_FILE_NAME = "update_check.json"


class UpdateCheckError(RuntimeError):
    """Raised when the update feed or download cannot be used."""


def is_newer(remote_version: str, local_version: str) -> bool:
    """What: Compare release versions stamped as yyyyMMdd-HHmmss-fff.

    Why included: Stamps sort chronologically as text; development runs never update.
    Where used: check_for_update().
    Returns: True when remote_version is a later release.
    """
    remote, local = str(remote_version or "").strip(), str(local_version or "").strip()
    if not remote or not local or not local[:8].isdigit() or not remote[:8].isdigit():
        return False
    return remote > local


def _state_path() -> Path:
    return get_app_data_path() / _STATE_FILE_NAME


def _read_state() -> dict[str, object]:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(state: dict[str, object]) -> None:
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def check_due() -> bool:
    """What: Decide whether the daily background check should run now.

    Why included: Avoids contacting the host on every launch.
    Where used: main.py startup check.
    Returns: True when no check happened within CHECK_INTERVAL.
    """
    if not UPDATE_FEED_URL:
        return False
    try:
        last = datetime.fromisoformat(str(_read_state().get("last_check") or ""))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last >= CHECK_INTERVAL


def fetch_feed(feed_url: str | None = None, timeout: float = 15.0) -> dict[str, object]:
    """What: Download and validate latest.json.

    Why included: The feed tells the app which version is newest and where to get it.
    Where used: check_for_update().
    Returns: The feed mapping; raises UpdateCheckError when unusable.
    """
    base = (feed_url if feed_url is not None else UPDATE_FEED_URL).rstrip("/")
    if not base:
        raise UpdateCheckError("Automatic updates are not set up yet.")
    request = urllib.request.Request(f"{base}/{FEED_FILE_NAME}", headers={"User-Agent": "PewVista", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            feed = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise UpdateCheckError("Could not check for updates. Check the internet connection.") from error
    if (not isinstance(feed, dict) or not str(feed.get("version") or "").strip()
            or not str(feed.get("url") or "").startswith("https://") or len(str(feed.get("sha256") or "")) != 64):
        raise UpdateCheckError("The update information is incomplete.")
    return feed


def check_for_update(local_version: str, feed_url: str | None = None) -> dict[str, object] | None:
    """What: Return the feed when a newer release exists, recording the check time.

    Why included: Used by both the daily background check and Help > Check for Updates.
    Where used: main.py.
    Returns: The feed for a newer version, or None when up to date.
    """
    feed = fetch_feed(feed_url)
    state = _read_state()
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    _write_state(state)
    return feed if is_newer(str(feed["version"]), local_version) else None


def download_update(feed: dict[str, object], destination: Path, progress=None) -> Path:
    """What: Download the update ZIP and verify its SHA-256 against the feed.

    Why included: Catches broken downloads before the signature check and installation.
    Where used: main.py automatic update workflow.
    Returns: The downloaded file path; raises UpdateCheckError on failure.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(str(feed["url"]), headers={"User-Agent": "PewVista"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length") or feed.get("size") or 0)
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received, total)
        if digest.hexdigest().lower() != str(feed["sha256"]).lower():
            raise UpdateCheckError("The downloaded update failed its security check. Try again later.")
        partial.replace(destination)
        return destination
    except (urllib.error.URLError, OSError) as error:
        raise UpdateCheckError("The update could not be downloaded. Check the internet connection and try again.") from error
    finally:
        try:
            partial.unlink()
        except OSError:
            pass


def read_package_version(update_zip: Path) -> str:
    """What: Read the release version stamped inside an update ZIP.

    Why included: The feed version must match what installed apps report.
    Where used: write_feed().
    Returns: The version string.
    """
    with zipfile.ZipFile(update_zip) as archive:
        for name in archive.namelist():
            if name.count("/") == 1 and name.endswith("/release.json"):
                version = str(json.loads(archive.read(name).decode("utf-8-sig")).get("version") or "").strip()
                if version:
                    return version
    raise UpdateCheckError(f"{update_zip} has no release version.")


def write_feed(release_directory: Path, base_url: str, notes: str = "") -> Path:
    """What: Create latest.json for a packaged release.

    Why included: Publishing a release is: upload the update ZIP and this file.
    Where used: The `feed` publisher command.
    Returns: Path of the written latest.json.
    """
    release_directory = Path(release_directory)
    update_zip = release_directory / UPDATE_FILE_NAME
    digest = hashlib.sha256()
    with update_zip.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    feed = {"version": read_package_version(update_zip), "url": f"{base_url.rstrip('/')}/{UPDATE_FILE_NAME}",
            "sha256": digest.hexdigest(), "size": update_zip.stat().st_size, "notes": notes}
    target = release_directory / FEED_FILE_NAME
    target.write_text(json.dumps(feed, indent=2), encoding="utf-8")
    return target


def _main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="PewVista update feed publisher")
    commands = parser.add_subparsers(dest="command", required=True)
    feed = commands.add_parser("feed", help="Write latest.json for a packaged release")
    feed.add_argument("release_directory", type=Path)
    feed.add_argument("base_url")
    feed.add_argument("--notes", default="")
    args = parser.parse_args(argv)
    print(write_feed(args.release_directory, args.base_url, args.notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
