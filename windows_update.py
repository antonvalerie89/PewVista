"""Prepare trusted local Windows update archives without touching installed data."""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from datetime import datetime
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import zipfile

from update_signing import verify_update_archive

APP_NAME = "The Light Ministries"
PRODUCT_NAME = "PewVista"
EXECUTABLE_NAME = PRODUCT_NAME + ".exe"
LEGACY_EXECUTABLE_NAME = APP_NAME + ".exe"
PROTECTED_ROOTS = {"data", "config", ".update-backups", ".test-installation"}


def get_installed_application_identity(install_directory: Path) -> str:
    """What: Read the application identity stamped into installation.json by setup.

    Why included: Every church names its program folder and shortcuts uniquely, so
    update packages must be matched to the stable identity recorded at installation.
    Where used: Update archive validation and the optional startup data merge.
    Returns: The recorded identity, or the built-in legacy identity for older installs.
    """
    settings_path = Path(install_directory) / "installation.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return APP_NAME
    if not isinstance(settings, dict):
        return APP_NAME
    return str(settings.get("application") or "").strip() or APP_NAME


def apply_optional_update_data(install_directory: Path, data_directory: Path) -> tuple[str, ...]:
    """What: Verify and merge the current update's optional database archive once.

    Why included: Selected creator records must merge without replacing receiving church data.
    Where used: Application startup after the external update installer finishes.
    Returns: Imported database keys, or an empty tuple when no new data is present.
    """
    from data_transfer import import_database_archive, importable_databases

    root = Path(install_directory)
    manifest_path = root / "release.json"
    if not manifest_path.is_file():
        return ()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("application") not in {get_installed_application_identity(root), APP_NAME} or manifest.get("package_type") != "update":
        return ()
    expected_hash = str(manifest.get("optional_data_sha256") or "").lower()
    if not expected_hash:
        return ()
    if len(expected_hash) != 64 or any(character not in "0123456789abcdef" for character in expected_hash):
        raise ValueError("The update's optional data checksum is invalid.")
    archive_path = root / "optional-update-data.zip"
    digest = hashlib.sha256()
    with archive_path.open("rb") as archive_file:
        for block in iter(lambda: archive_file.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected_hash:
        raise ValueError("The optional update data did not pass its checksum. No data was imported.")
    data_directory = Path(data_directory)
    data_directory.mkdir(parents=True, exist_ok=True)
    receipt = data_directory / "optional-update-imports.json"
    try:
        imported = json.loads(receipt.read_text(encoding="utf-8"))
    except FileNotFoundError:
        imported = []
    if not isinstance(imported, list) or not all(isinstance(item, str) for item in imported):
        raise ValueError("The optional update import history is invalid; no data was imported.")
    if expected_hash in imported:
        return ()
    keys = importable_databases(archive_path)
    if not keys or any(key not in {"library", "services"} for key in keys):
        raise ValueError("Optional update data may contain only Library and service history.")
    imported_keys, _backup = import_database_archive(archive_path, keys, data_directory, modes={key: "merge" for key in keys})
    temporary_receipt = receipt.with_suffix(".json.tmp")
    temporary_receipt.write_text(json.dumps([*imported, expected_hash]), encoding="utf-8")
    temporary_receipt.replace(receipt)
    return imported_keys


def run_release_build(workspace: Path, progress, *, data_keys=(), background_paths=(), data_directory: Path | None = None) -> Path:
    """What: Run the creator's isolated Windows release build and stream its console output.

    Why included: Release compilation and ZIP creation must not block the Tk application.
    Where used: Update menu Build Release (Creator) workflow in main.py.
    Returns: Path to the published latest release folder; raises on build failure.
    """
    workspace = Path(workspace).resolve()
    script = workspace / "update_windows_app.bat"
    if not script.is_file() or not (workspace / "main.py").is_file():
        raise ValueError("Select the creator's source workspace containing main.py and update_windows_app.bat.")
    log_directory = workspace / "build" / "release-logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / f"release-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    recent_output: deque[str] = deque(maxlen=20)
    progress(f"Build log: {log_path}")
    with tempfile.TemporaryDirectory(prefix="LightReleaseSelection-") as temporary, log_path.open("w", encoding="utf-8", buffering=1) as log:
        selection_path = Path(temporary) / "selection.json"
        data_archive = ""
        if data_keys:
            from data_transfer import export_database_archive
            data_archive = str(Path(temporary) / "selected-data.zip")
            progress("Preparing selected database snapshots...")
            export_database_archive(Path(data_archive), list(data_keys), data_directory, selected_media_paths=tuple(Path(path) for path in background_paths))
        backgrounds = [str(Path(path).resolve(strict=True)) for path in background_paths]
        if len({Path(path).name.casefold() for path in backgrounds}) != len(backgrounds):
            raise ValueError("Selected backgrounds must have distinct filenames.")
        selection_path.write_text(json.dumps({"data_archive": data_archive, "backgrounds": backgrounds}), encoding="utf-8")
        environment = os.environ.copy()
        environment["TLM_RELEASE_SELECTION"] = str(selection_path)
        with subprocess.Popen(
            ["cmd.exe", "/d", "/c", str(script), "--no-pause"],
            cwd=str(workspace), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=environment,
            text=True, errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as process:
            if process.stdout is not None:
                for line in process.stdout:
                    log.write(line)
                    recent_output.append(line.rstrip())
                    progress(line.rstrip())
            return_code = process.wait()
        log.write(f"\nBuild exit code: {return_code}\n")
    if return_code:
        details = "\n".join(recent_output)
        raise RuntimeError(f"Release build failed (exit {return_code}).\nLog: {log_path}\n\n{details}\n\nYour working EXE was not replaced.")
    return workspace / "dist" / "releases" / "latest"


@dataclass(frozen=True)
class PreparedUpdate:
    """What: Hold the temporary payload and trusted installer paths for a prepared update.

    Why included: Preparation must finish before the running application is closed.
    Where used: Actions-panel update preparation and installer launch.
    """

    directory: Path
    payload: Path
    installer: Path
    archive: Path | None = None


def is_protected_update_path(path: PurePosixPath) -> bool:
    """What: Identify user data, database sidecars, and installed configuration paths.

    Why included: Even older full-data ZIPs must not overwrite another PC's newer records.
    Where used: Archive validation and extraction.
    Returns: True when the path must never be installed as program content.
    """
    parts = tuple(part.casefold() for part in path.parts)
    if parts[0] in PROTECTED_ROOTS or parts[0] == "installation.json" or parts[:3] == ("resourses", "obs_portable", "config"):
        return True
    name = parts[-1]
    for suffix in ("-wal", "-shm", "-journal"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name.endswith((".db", ".sqlite", ".sqlite3"))


def prepare_update_archive(archive_path: Path, install_directory: Path, installer_path: Path, progress=None, staging_directory: Path | None = None) -> PreparedUpdate:
    """What: Validate and extract a local update ZIP into a separate temporary directory.

    Why included: Invalid, unsafe, or incomplete archives must fail before installed files are changed.
    Where used: The Actions Install Update background worker.
    Returns: Prepared payload and trusted helper paths; removes temporary files on failure.
    """
    install_directory = Path(install_directory).resolve()
    if not any((install_directory / name).is_file() for name in (EXECUTABLE_NAME, LEGACY_EXECUTABLE_NAME)):
        raise ValueError("Select the installed desktop application, not a Python source workspace.")
    if not Path(installer_path).is_file():
        raise ValueError("The Windows update installer is missing. Restore apply_windows_update.ps1.")
    verify_update_archive(Path(archive_path))
    if staging_directory is None:
        directory = Path(tempfile.mkdtemp(prefix="TheLightUpdate-"))
    else:
        directory = Path(staging_directory)
        if directory.exists():
            raise ValueError("The update staging folder already exists.")
        directory.mkdir(parents=True)
    payload = directory / "payload"
    try:
        with zipfile.ZipFile(archive_path) as archive:
            expected_identity = get_installed_application_identity(install_directory)
            manifest_name = PRODUCT_NAME + "/release.json"
            if manifest_name not in archive.namelist():
                manifest_name = APP_NAME + "/release.json"
            if manifest_name in archive.namelist():
                manifest = json.loads(archive.read(manifest_name).decode("utf-8-sig"))
                if manifest.get("application") not in {expected_identity, APP_NAME} or manifest.get("package_type") != "update":
                    raise ValueError(f"Select the {PRODUCT_NAME} Update ZIP, not its Installation ZIP.")
            members = []
            seen = set()
            for member in archive.infolist():
                name = member.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or len(path.parts) < 1 or path.parts[0].casefold() not in {APP_NAME.casefold(), PRODUCT_NAME.casefold()}:
                    raise ValueError(f"This is not a {PRODUCT_NAME} desktop update.")
                if any(part in {".", ".."} or ":" in part or part.endswith((".", " ")) for part in path.parts):
                    raise ValueError("The update contains an unsafe file path.")
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("Update archives cannot contain symbolic links.")
                if member.is_dir() or len(path.parts) == 1:
                    continue
                relative = PurePosixPath(*path.parts[1:])
                if is_protected_update_path(relative):
                    continue
                identity = str(relative).casefold()
                if identity in seen:
                    raise ValueError("The update contains duplicate Windows file paths.")
                seen.add(identity)
                members.append((member, relative))
            if not any(name.casefold() in seen for name in (EXECUTABLE_NAME, LEGACY_EXECUTABLE_NAME)) or not any(name.startswith("_internal/") for name in seen):
                raise ValueError("The ZIP is missing the program executable or its _internal runtime folder.")
            total = sum(member.file_size for member, _ in members)
            required = total + 100 * 1024 * 1024
            if shutil.disk_usage(directory).free < required:
                raise ValueError("Not enough free space to extract this update.")
            if shutil.disk_usage(install_directory).free < required:
                raise ValueError("Not enough free space for update backup and installation.")
            last_percent = -1
            for index, (member, relative) in enumerate(members, 1):
                destination = payload.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                percent = index * 100 // max(1, len(members))
                if progress is not None and percent != last_percent:
                    progress(index, len(members))
                    last_percent = percent
        executable_path = next(payload / name for name in (EXECUTABLE_NAME, LEGACY_EXECUTABLE_NAME) if (payload / name).is_file())
        with executable_path.open("rb") as executable:
            if executable.read(2) != b"MZ":
                raise ValueError("The ZIP does not contain a valid Windows executable.")
        installer = directory / "apply_windows_update.ps1"
        shutil.copy2(installer_path, installer)
        return PreparedUpdate(directory, payload, installer, Path(archive_path).resolve())
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def launch_prepared_update(update: PreparedUpdate, install_directory: Path, process_id: int) -> None:
    """What: Start the graphical installer that waits for the current app to exit.

    Why included: A running Windows executable cannot safely replace its own program files.
    Where used: Successful Actions-panel update preparation, immediately before app shutdown.
    Returns: None; raises on launch failure so the existing app can remain open.
    """
    command = [
        "powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(update.installer),
        "-PayloadDirectory", str(update.payload), "-InstallDirectory", str(install_directory),
        "-WaitForProcessId", str(process_id),
        "-CleanupDirectory", str(update.directory), "-Gui",
    ]
    installed_script = Path(install_directory) / "apply_windows_update.ps1"
    if update.archive is not None and installed_script.is_file():
        # Run the administrator-protected script, and let it re-verify the original ZIP
        # itself instead of trusting the payload left in this user's temp folder.
        command[command.index(str(update.installer))] = str(installed_script)
        command += ["-UpdateArchive", str(update.archive)]
    subprocess.Popen(command, cwd=str(install_directory), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run_elevated_preparation(archive_path: Path, output_directory: Path, install_directory: Path) -> int:
    """What: Verify and extract an update into an administrator-only folder.

    Why included: The elevated updater must not trust files a standard user could change;
    it calls the installed PewVista.exe in this mode to re-check the original package.
    Where used: main.py --prepare-update, launched by apply_windows_update.ps1.
    Returns: 0 on success; 1 after writing the reason beside the output folder.
    """
    try:
        install_directory = Path(install_directory)
        prepare_update_archive(Path(archive_path), install_directory, install_directory / "apply_windows_update.ps1",
                               staging_directory=Path(output_directory))
        return 0
    except Exception as error:
        try:
            Path(str(output_directory) + ".error.txt").write_text(str(error), encoding="utf-8")
        except OSError:
            pass
        return 1


def discard_prepared_update(update: PreparedUpdate) -> None:
    """What: Remove a prepared temporary payload after installer launch fails.

    Why included: A failed handoff must not strand a multi-gigabyte extraction.
    Where used: Actions update launch-failure handling.
    Returns: None.
    """
    shutil.rmtree(update.directory, ignore_errors=True)