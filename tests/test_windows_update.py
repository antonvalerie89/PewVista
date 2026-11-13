"""Regression checks for protected archive preparation and Windows update deployment."""

from pathlib import Path
from contextlib import closing
import json
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import update_signing
from windows_update import APP_NAME, PRODUCT_NAME, apply_optional_update_data, prepare_update_archive, run_release_build


class WindowsUpdateTests(unittest.TestCase):
    """What: Validate update archives and exercise installation on disposable app folders.

    Why included: Installing program files must never replace the target PC's database/settings files.
    Where used: Windows update regression suite.
    """

    def test_desktop_spec_includes_video_decoder_metadata(self):
        """What: Verify the desktop build includes ImageIO's distribution metadata.

        Why included: ImageIO's import-time version lookup fails in the EXE without it.
        Where used: Pre-build update checks guarding packaged MP4 preview playback.
        Returns: None; checks real metadata without compiling or launching the application.
        """
        import runpy
        from importlib.metadata import Distribution, version

        analysis = MagicMock()
        spec = self.installer.with_name("PewVista.spec")
        with patch("PyInstaller.utils.hooks.collect_submodules", return_value=[]):
            runpy.run_path(str(spec), init_globals={
                "Analysis": analysis, "PYZ": MagicMock(),
                "EXE": MagicMock(), "COLLECT": MagicMock(),
            })
        metadata_entries = [Path(source) for source, destination in analysis.call_args.kwargs["datas"]
                            if destination.lower().startswith("imageio-") and destination.endswith(".dist-info")]
        self.assertTrue(metadata_entries, "MP4 preview imports require bundled ImageIO metadata")
        self.assertTrue((metadata_entries[0] / "METADATA").is_file())
        self.assertEqual(Distribution.at(metadata_entries[0]).version, version("imageio"))

    def test_build_runner_streams_output(self):
        """What: Verify release-build output lines reach the supplied progress callback.

        Why included: Build progress needs to be visible without launching a real build.
        Where used: Windows release-build task regression.
        Returns: None; asserts output and result directory.
        """
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = iter(["[Step 3/5] Building executable...\n", "Release ready\n"])
        process.wait.return_value = 0
        lines = []
        workspace = self.base / "creator workspace"
        workspace.mkdir()
        (workspace / "main.py").touch()
        (workspace / "update_windows_app.bat").touch()
        with patch("windows_update.subprocess.Popen", return_value=process):
            result = run_release_build(workspace, lines.append)
        self.assertTrue(lines[0].startswith("Build log: "))
        self.assertEqual(lines[1:], ["[Step 3/5] Building executable...", "Release ready"])
        build_log = next((workspace / "build/release-logs").glob("*.log"))
        self.assertIn("Release ready", build_log.read_text())
        self.assertIn("Build exit code: 0", build_log.read_text())
        self.assertEqual(result, workspace / "dist/releases/latest")

    def test_build_runner_passes_explicit_optional_update_selection(self):
        """What: Pass only explicitly chosen databases and media into update packaging.

        Why included: Optional private content must remain excluded unless selected in the creator dialog.
        Where used: Build Release selection handoff regression.
        Returns: None; checks update selection config and referenced-media filter.
        """
        workspace = self.base / "creator workspace"
        workspace.mkdir()
        (workspace / "main.py").touch()
        (workspace / "update_windows_app.bat").touch()
        background_directory = self.base / "backgrounds"
        background_directory.mkdir()
        selected_background = background_directory / "selected.jpg"
        selected_background.write_bytes(b"selected")
        omitted_background = background_directory / "omitted.jpg"
        omitted_background.write_bytes(b"omitted")
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = iter(["Release ready\n"])
        process.wait.return_value = 0
        captured = {}

        def capture_process(*_args, **kwargs):
            """What: Capture the release-build process arguments instead of running a build.

            Why included: The test checks the release content selection passed to the build script.
            Where used: Patched subprocess launch in the release build test.
            Returns: The test's prepared mock build process.
            """
            captured.update(kwargs)
            selection_path = Path(kwargs["env"]["TLM_RELEASE_SELECTION"])
            captured["selection"] = json.loads(selection_path.read_text(encoding="utf-8"))
            captured["selection_exists_during_build"] = selection_path.is_file()
            return process

        with patch("windows_update.subprocess.Popen", side_effect=capture_process), \
                patch("data_transfer.export_database_archive") as export_archive:
            run_release_build(
                workspace, lambda _line: None,
                data_keys=("library", "services"),
                background_paths=(selected_background,),
                data_directory=self.shared_data,
            )
        self.assertEqual(captured["selection"]["backgrounds"], [str(selected_background.resolve())])
        self.assertTrue(captured["selection"]["data_archive"].endswith("selected-data.zip"))
        self.assertTrue(captured["selection_exists_during_build"])
        self.assertFalse(Path(captured["selection"]["data_archive"]).exists())
        export_archive.assert_called_once()
        self.assertEqual(export_archive.call_args.args[1], ["library", "services"])
        self.assertEqual(export_archive.call_args.kwargs["selected_media_paths"], (selected_background,))

    def test_build_runner_reports_failure(self):
        """What: Verify a nonzero release-build exit becomes a visible task error.

        Why included: Failed builds must not appear successful to the UI.
        Where used: Windows release-build task regression.
        Returns: None; asserts the raised error includes the exit code.
        """
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = iter(["[ERROR] Build failed\n"])
        process.wait.return_value = 1
        workspace = self.base / "creator workspace"
        workspace.mkdir()
        (workspace / "main.py").touch()
        (workspace / "update_windows_app.bat").touch()
        with patch("windows_update.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "exit 1") as failure:
                run_release_build(workspace, lambda line: None)
        self.assertIn("[ERROR] Build failed", str(failure.exception))
        build_log = next((workspace / "build/release-logs").glob("*.log"))
        self.assertIn("[ERROR] Build failed", build_log.read_text())

    def setUp(self) -> None:
        """What: Create fake installed program files and current user data.

        Why included: Native installer tests must never touch the real installation.
        Where used: unittest setup.
        Returns: None.
        """
        self.temporary = tempfile.TemporaryDirectory(prefix="LightUpdateTest-")
        self.base = Path(self.temporary.name)
        self.installed = self.base / APP_NAME
        self.shared_data = self.base / "Shared Data"
        self.shared_data.mkdir()
        (self.installed / "_internal").mkdir(parents=True)
        (self.installed / (APP_NAME + ".exe")).write_bytes(b"MZold program")
        (self.installed / "_internal" / "runtime.dll").write_bytes(b"old runtime")
        (self.shared_data / "library.db").write_bytes(b"LATEST USER SONGS")
        (self.shared_data / "ui_settings.json").write_text("latest settings")
        (self.installed / "installation.json").write_text(json.dumps({
            "church_name": "Fixture Church",
            "data_directory": str(self.shared_data),
            "default_background_path": "",
        }))
        self.installer = Path(__file__).resolve().parents[1] / "apply_windows_update.ps1"
        self.prepared = []
        # A throwaway key stands in for the publisher's; packaging scripts launched by tests
        # inherit PEWVISTA_SIGNING_KEY, and only this key is trusted during the test.
        self.signing_key_path = self.base / "test_signing_key.pem"
        test_key = update_signing.load_or_create_signing_key(self.signing_key_path)
        self.signing_environment = patch.dict(os.environ, {"PEWVISTA_SIGNING_KEY": str(self.signing_key_path)})
        self.signing_environment.start()
        self.trusted_keys = patch.object(update_signing, "TRUSTED_PUBLIC_KEYS", (update_signing.public_key_hex(test_key),))
        self.trusted_keys.start()

    def tearDown(self) -> None:
        """What: Remove fixture installations and prepared temporary payloads.

        Why included: Large update staging directories must not remain after tests.
        Where used: unittest cleanup.
        Returns: None.
        """
        self.trusted_keys.stop()
        self.signing_environment.stop()
        for prepared in self.prepared:
            shutil.rmtree(prepared.directory, ignore_errors=True)
        self.temporary.cleanup()

    def archive(self, additional=None, executable_name=None):
        """What: Write a minimal desktop update ZIP with optional fixture entries.

        Why included: Archive validation needs representative program/data paths.
        Where used: Update preparation tests.
        Returns: Fixture ZIP path.
        """
        path = self.base / "update.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(APP_NAME + "/" + (executable_name or (APP_NAME + ".exe")), b"MZnew program")
            archive.writestr(APP_NAME + "/_internal/runtime.dll", b"new runtime")
            for name, content in (additional or {}).items():
                archive.writestr(name, content)
        return path

    def prepare(self, archive):
        """What: Prepare an archive and track its temporary folder for cleanup.

        Why included: Tests use the same extraction path as the Actions button.
        Where used: Valid archive and installer tests.
        Returns: Prepared update object.
        """
        update_signing.sign_update_archive(archive, self.signing_key_path)
        prepared = prepare_update_archive(archive, self.installed, self.installer)
        self.prepared.append(prepared)
        return prepared

    def run_installer(self, prepared):
        """What: Execute the real PowerShell installer without launching the fake executable.

        Why included: Protected paths and rollback must be tested in Windows file operations.
        Where used: Installation and copy-failure tests.
        Returns: Captured process result.
        """
        if shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell is required")
        return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(prepared.installer), "-PayloadDirectory", str(prepared.payload),
            "-InstallDirectory", str(self.installed), "-NoAdmin", "-NoLaunch", "-NoPause"],
            capture_output=True, text=True, timeout=45)

    def test_preparation_ignores_older_user_data(self):
        """What: Filter databases/settings/configuration even when an older full-data ZIP contains them.

        Why included: Users may select historical packages that would otherwise overwrite newer data.
        Where used: Archive data-safety regression.
        Returns: None.
        """
        prepared = self.prepare(self.archive({
            APP_NAME + "/Data/library.db": b"OLD SONGS",
            APP_NAME + "/installation.json": b'{"data_directory":"C:\\\\unsafe"}',
            APP_NAME + "/config/settings.ini": b"old config",
            APP_NAME + "/Resourses/obs_portable/config/old.json": b"old OBS settings",
            APP_NAME + "/outside.sqlite3-wal": b"old journal",
            APP_NAME + "/.update-backups/old/Data/library.db": b"old backup",
        }))
        self.assertFalse((prepared.payload / "Data").exists())
        self.assertFalse((prepared.payload / "config").exists())
        self.assertFalse((prepared.payload / "outside.sqlite3-wal").exists())
        self.assertFalse((prepared.payload / ".update-backups").exists())
        self.assertFalse((prepared.payload / "installation.json").exists())
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")

    def test_unsigned_or_altered_updates_are_refused(self):
        """What: Refuse update packages that are unsigned, altered, or signed by another key.

        Why included: Updates are installed with administrator rights, so only packages
        signed by the publisher may be prepared.
        Where used: Update signature security regression.
        Returns: None; asserts nothing is extracted for any rejected package.
        """
        unsigned = self.archive()
        with self.assertRaisesRegex(ValueError, "not signed"):
            prepare_update_archive(unsigned, self.installed, self.installer)

        update_signing.sign_update_archive(unsigned, self.signing_key_path)
        data = bytearray(unsigned.read_bytes())
        data[40] ^= 0x01
        tampered = self.base / "tampered.zip"
        tampered.write_bytes(bytes(data))
        with self.assertRaisesRegex(ValueError, "changed after it was signed"):
            prepare_update_archive(tampered, self.installed, self.installer)

        stranger = self.base / "stranger.zip"
        stranger.write_bytes(self.archive().read_bytes())
        update_signing.sign_update_archive(stranger, self.base / "stranger_key.pem")
        with self.assertRaisesRegex(ValueError, "not signed by the PewVista publisher"):
            prepare_update_archive(stranger, self.installed, self.installer)

        prepared = self.prepare(self.archive())
        self.assertEqual((prepared.payload / (APP_NAME + ".exe")).read_bytes(), b"MZnew program")

    def test_elevated_preparation_rechecks_original_archive(self):
        """What: Verify and extract into an exact folder, refusing unsigned packages.

        Why included: The elevated updater re-checks the original ZIP itself rather than
        trusting a payload in the user's temp folder.
        Where used: Secure elevated update regression.
        Returns: None; asserts success output and the recorded refusal reason.
        """
        from windows_update import run_elevated_preparation

        (self.installed / "apply_windows_update.ps1").write_text("# installed updater fixture")
        signed = self.archive()
        update_signing.sign_update_archive(signed, self.signing_key_path)
        output = self.base / "secure" / "prepared"
        self.assertEqual(run_elevated_preparation(signed, output, self.installed), 0)
        self.assertEqual((output / "payload" / (APP_NAME + ".exe")).read_bytes(), b"MZnew program")

        unsigned = self.base / "unsigned.zip"
        with zipfile.ZipFile(unsigned, "w") as package:
            package.writestr(APP_NAME + "/" + APP_NAME + ".exe", b"MZnew program")
        refused = self.base / "secure" / "refused"
        self.assertEqual(run_elevated_preparation(unsigned, refused, self.installed), 1)
        self.assertFalse(refused.exists())
        self.assertIn("not signed", Path(str(refused) + ".error.txt").read_text(encoding="utf-8"))

    def test_preparation_accepts_pewvista_archive_root(self):
        """What: Accept update archives whose top folder is named PewVista.

        Why included: Newer packages use the product name while older ones use the legacy name.
        Where used: Update preparation regression suite.
        Returns: None; asserts the extracted executable and runtime.
        """
        archive = self.base / "PewVista-update.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("PewVista/release.json", json.dumps({"application": APP_NAME, "product_name": "PewVista", "package_type": "update"}))
            package.writestr("PewVista/PewVista.exe", b"MZnew PewVista executable")
            package.writestr("PewVista/_internal/runtime.dll", b"new runtime")
        prepared = self.prepare(archive)
        self.assertEqual((prepared.payload / "PewVista.exe").read_bytes(), b"MZnew PewVista executable")
        self.assertEqual((prepared.payload / "_internal" / "runtime.dll").read_bytes(), b"new runtime")

    def test_graphical_installer_success_and_failure(self):
        """What: Run the graphical update window with successful and invalid fixture payloads.

        Why included: The UI must report worker results without weakening data protection.
        Where used: Windows graphical installer integration regression.
        Returns: None; checks exit codes, updated files, and unchanged shared data.
        """
        if shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell is required")
        prepared = self.prepare(self.archive())
        command = ["powershell.exe", "-NoProfile", "-STA", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(prepared.installer), "-PayloadDirectory", str(prepared.payload),
                   "-InstallDirectory", str(self.installed), "-NoAdmin", "-NoLaunch", "-NoPause", "-Gui"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.installed / (APP_NAME + ".exe")).read_bytes(), b"MZnew program")
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")
        (prepared.payload / (APP_NAME + ".exe")).unlink()
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr + (self.installed / "update-install.log").read_text())
        self.assertEqual((self.installed / (APP_NAME + ".exe")).read_bytes(), b"MZnew program")
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")
        self.assertIn("prepared update is incomplete", (self.installed / "update-install.log").read_text())

    def test_rejects_traversal_and_duplicate_windows_paths(self):
        """What: Reject paths escaping the app folder and duplicate case-insensitive names.

        Why included: Selected ZIPs must not write arbitrary files or overwrite aliases during extraction.
        Where used: Archive path validation regression.
        Returns: None.
        """
        for entries in ({APP_NAME + "/../escape.txt": b"bad"},
                        {APP_NAME + "/_INTERNAL/RUNTIME.DLL": b"duplicate"}):
            with self.assertRaises(ValueError):
                self.prepare(self.archive(entries))
        self.assertFalse((self.base / "escape.txt").exists())

    def test_installer_preserves_data_and_backs_up_program(self):
        """What: Replace program files while retaining current user data and recoverable backups.

        Why included: The other PC's records must remain independent from the package builder's data.
        Where used: Native installer success regression.
        Returns: None.
        """
        prepared = self.prepare(self.archive())
        (self.installed / "Bibles").mkdir()
        (self.installed / "Bibles/my_bible.json").write_text("latest imported Bible")
        (prepared.payload / "Bibles").mkdir()
        (prepared.payload / "Bibles/my_bible.json").write_text("older bundled Bible")
        (prepared.payload / "Bibles/new_creator_bible.json").write_text("creator-added Bible")
        (prepared.payload / "Data").mkdir()
        (prepared.payload / "Data/library.db").write_bytes(b"MUST NEVER INSTALL")
        result = self.run_installer(prepared)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.installed / (APP_NAME + ".exe")).read_bytes(), b"MZnew program")
        self.assertEqual((self.installed / "_internal/runtime.dll").read_bytes(), b"new runtime")
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")
        self.assertEqual((self.shared_data / "ui_settings.json").read_text(), "latest settings")
        self.assertEqual((self.installed / "Bibles/my_bible.json").read_text(), "latest imported Bible")
        self.assertEqual((self.installed / "Bibles/new_creator_bible.json").read_text(), "creator-added Bible")
        backups = list((self.installed / ".update-backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "Data/library.db").read_bytes(), b"LATEST USER SONGS")
        self.assertEqual((backups[0] / "program" / (APP_NAME + ".exe")).read_bytes(), b"MZold program")

    def test_update_migrates_legacy_executable_to_pewvista(self):
        """What: Replace the legacy executable with PewVista.exe during an update.

        Why included: Installations from before the rename must end up running PewVista.exe.
        Where used: Update installer regression suite.
        Returns: None; asserts the new executable and the removed legacy one.
        """
        prepared = self.prepare(self.archive(executable_name="PewVista.exe"))
        result = self.run_installer(prepared)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.installed / "PewVista.exe").read_bytes(), b"MZnew program")
        self.assertFalse((self.installed / (APP_NAME + ".exe")).exists())
        backups = list((self.installed / ".update-backups").iterdir())
        self.assertEqual((backups[0] / "program" / (APP_NAME + ".exe")).read_bytes(), b"MZold program")

    def test_installer_skips_locked_shared_obs_runtime_in_backup(self):
        """What: Install an update while the shared OBS browser Cookies file is locked.

        Why included: OBS browser runtime files must not prevent backing up church data.
        Where used: Windows installer backup regression using disposable folders.
        Returns: None; verifies the runtime remains untouched and user data is backed up.
        """
        import ctypes
        from ctypes import wintypes

        if os.name != "nt":
            self.skipTest("Windows file locking is required")
        cookies = self.shared_data / "obs_portable/config/obs-studio/plugin_config/obs-browser/Network/Cookies"
        cookies.parent.mkdir(parents=True)
        cookies.write_bytes(b"OBS browser state")
        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        handle = create_file(str(cookies), 0x80000000, 0, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            prepared = self.prepare(self.archive())
            result = self.run_installer(prepared)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        self.assertEqual(cookies.read_bytes(), b"OBS browser state")
        backups = list((self.installed / ".update-backups").iterdir())
        self.assertEqual((backups[0] / "Data/library.db").read_bytes(), b"LATEST USER SONGS")
        self.assertEqual((backups[0] / "Data/ui_settings.json").read_text(), "latest settings")
        self.assertFalse((backups[0] / "Data/obs_portable").exists())

    def test_installer_closes_owned_obs_processes_only(self):
        """What: Stop disposable OBS stand-ins in bundled and shared runtime folders.

        Why included: Updates must not require Task Manager or stop another installation's OBS.
        Where used: Native updater process-shutdown regression, never using real OBS processes.
        Returns: None; verifies shutdown, directory-boundary isolation, and preserved data.
        """
        if os.name != "nt" or shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell and Windows processes are required")
        stand_in = Path(os.environ["SystemRoot"]) / "System32/ping.exe"
        private_root = self.base / "Users/operator/AppData/Local/PewVista"
        process_specs = (
            (self.installed / "Resourses/obs_portable/bin/64bit/obs64.exe", True),
            (self.shared_data / "obs_portable/bin/64bit/obs64.exe", True),
            (self.shared_data / "obs_portable/obs-plugins/64bit/obs-browser-page.exe", True),
            (self.shared_data / "obs_portable/bin/64bit/ffmpeg.exe", True),
            (private_root / self.shared_data.name / "obs_portable/bin/64bit/obs64.exe", True),
            (self.installed / "Resourses/grammar/java/bin/javaw.exe", True),
            (private_root / (self.shared_data.name + " Other") / "obs_portable/bin/64bit/obs64.exe", False),
            (self.shared_data / "obs_portable-other/bin/64bit/obs64.exe", False),
            (self.base / "Other OBS/bin/64bit/obs64.exe", False),
            (self.base / "Other OBS/bin/64bit/ffmpeg.exe", False),
        )
        processes = []
        try:
            for executable, should_close in process_specs:
                executable.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stand_in, executable)
                process = subprocess.Popen(
                    [str(executable), "-t", "127.0.0.1"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                processes.append((process, should_close))
            # A program elsewhere that has loaded a DLL from the program folder (as OBS did) also locks it.
            native_dll = Path(os.environ["SystemRoot"]) / "System32/vcruntime140.dll"
            if native_dll.is_file():
                locked_dll = self.installed / "_internal/locked-by-other.dll"
                locked_dll.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(native_dll, locked_dll)
                loader = ("Add-Type -Namespace Native -Name Kernel32 -MemberDefinition "
                          "'[DllImport(\"kernel32.dll\", CharSet=CharSet.Unicode)] public static extern System.IntPtr LoadLibrary(string path);'; "
                          f"[void][Native.Kernel32]::LoadLibrary('{locked_dll}'); Start-Sleep -Seconds 120")
                dll_user = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", loader],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                processes.append((dll_user, True))
                time.sleep(5)
            prepared = self.prepare(self.archive())
            for process, _should_close in processes:
                self.assertIsNone(process.poll(), "Disposable process exited before the update")
            result = self.run_installer(prepared)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for process, should_close in processes:
                if should_close:
                    self.assertIsNotNone(process.poll(), "An app-owned OBS stand-in remained open")
                else:
                    self.assertIsNone(process.poll(), "An unrelated OBS stand-in was closed")
            self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")
            self.assertEqual((self.installed / (APP_NAME + ".exe")).read_bytes(), b"MZnew program")
        finally:
            for process, _should_close in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)

    def test_installer_and_updater_share_the_same_process_closing_code(self):
        """What: Keep the file-lock closing logic identical in Setup and the updater.

        Why included: Both must close programs that hold PewVista's files before copying.
        Where used: Script consistency regression.
        Returns: None.
        """
        def block(name):
            text = self.installer.with_name(name).read_text(encoding="utf-8")
            start = text.index("function Close-ProcessesUsingFolder")
            return text[start:text.index("\nfunction ", text.index("function Test-FileLockError"))] if name == "install_windows_app.ps1" else text[start:text.index("\ntry {", start)]
        self.assertEqual(block("apply_windows_update.ps1").strip(), block("install_windows_app.ps1").strip())

    def test_installer_rolls_back_after_copy_failure(self):
        """What: Force a target-path conflict after program replacement and verify rollback.

        Why included: An incomplete update must retain recoverable program files and untouched user data.
        Where used: Native installer rollback regression.
        Returns: None.
        """
        prepared = self.prepare(self.archive({APP_NAME + "/zzz-conflict.dat": b"new file"}))
        (self.installed / "zzz-conflict.dat").mkdir()
        result = self.run_installer(prepared)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.installed / (APP_NAME + ".exe")).read_bytes(), b"MZold program")
        self.assertEqual((self.installed / "_internal/runtime.dll").read_bytes(), b"old runtime")
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")
        self.assertIn("Update failed", result.stdout)

    def test_update_zip_excludes_local_backup_folders(self):
        """What: Exclude app-local update backups when creating another update ZIP.

        Why included: Backup JSON and older program files must not leak into distributed updates.
        Where used: Windows update packaging regression.
        Returns: None.
        """
        if shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell is required")
        backups = self.installed / ".update-backups" / "previous"
        backups.mkdir(parents=True)
        (backups / "private.json").write_text("private backup data")
        background = self.installed / "Backgrounds/private.jpg"
        background.parent.mkdir()
        background.write_bytes(b"private background")
        target = self.base / "safe-update.zip"
        script = self.installer.with_name("create_windows_zip.ps1")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-AppDirectory", str(self.installed), "-ArchivePath", str(target)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            self.assertFalse(any("/.update-backups/" in name or "/Data/" in name for name in names))
            self.assertIn(APP_NAME + "/Backgrounds/", names)
            self.assertFalse(any("/Backgrounds/" in name and not name.endswith("/") for name in names))
            self.assertNotIn(APP_NAME + "/optional-update-data.zip", names)

    def test_build_staging_excludes_shared_background_collection(self):
        """What: Keep the shared background collection out of the build staging folder.

        Why included: Release packages must not redistribute a church's own media.
        Where used: Release packaging regression suite.
        Returns: None; asserts the staging copy excludes shared backgrounds.
        """
        build_script = self.installer.with_name("build_desktop_app.bat").read_text(encoding="utf-8")
        self.assertIn('/XD "%ROOT_DIR%Data\\Backgrounds"', build_script)
        self.assertNotIn('robocopy "%ROOT_DIR%Backgrounds"', build_script)

    def test_update_background_name_collision_preserves_both_files(self):
        """What: Keep both files when an update background has the same name as an existing one.

        Why included: Updating must never overwrite a church's own background media.
        Where used: Update media merge regression suite.
        Returns: None; asserts both files survive.
        """
        background = self.shared_data / "Backgrounds" / "song.jpg"
        background.parent.mkdir()
        background.write_bytes(b"existing background")
        prepared = self.prepare(self.archive({APP_NAME + "/Backgrounds/song.jpg": b"new selected background"}))
        result = self.run_installer(prepared)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(background.read_bytes(), b"existing background")
        self.assertEqual(next(background.parent.glob("song_*.jpg")).read_bytes(), b"new selected background")
        self.assertFalse((self.installed / "Backgrounds" / "song.jpg").exists())

    def test_fresh_install_places_selected_backgrounds_in_shared_data(self):
        """What: Place backgrounds selected for a release in the shared data folder on install.

        Why included: Standard users can only write media to shared data, not Program Files.
        Where used: Installer regression suite.
        Returns: None; asserts the background location after installation.
        """
        source = self.installed
        (source / "release.json").write_text(json.dumps({"application": APP_NAME, "package_type": "installation"}))
        for filename in ("apply_windows_update.ps1", "create_desktop_shortcut.ps1", "uninstall_windows_app.ps1", "uninstall_windows_app.bat"):
            shutil.copy2(self.installer.with_name(filename), source / filename)
        media = source / "Backgrounds" / "song.jpg"
        media.parent.mkdir()
        media.write_bytes(b"selected background")
        background = self.base / "default.jpg"
        background.write_bytes(b"default background")
        legacy = self.base / "legacy data"
        legacy.mkdir()
        destination = self.base / "fresh" / "Fixture Church"
        shared = self.base / "fresh shared data"
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                   str(self.installer.with_name("install_windows_app.ps1")), "-SourceDirectory", str(source),
                   "-DestinationDirectory", str(destination), "-DataDirectory", str(shared), "-ChurchName", "Fixture Church",
                   "-DefaultBackgroundFile", str(background), "-LegacyDataDirectory", str(legacy),
                   "-NoAdmin", "-NoShortcuts", "-NoLaunch", "-NoPause", "-AcceptTerms"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((shared / "Backgrounds" / "song.jpg").read_bytes(), b"selected background")
        self.assertFalse((destination / "Backgrounds" / "song.jpg").exists())

    def test_fresh_install_prepares_private_obs_from_installed_obs(self):
        """What: Let Setup create PewVista's private OBS copy from an OBS already on the PC.

        Why included: Packages no longer ship OBS; Setup must reuse or download it.
        Where used: Installer OBS provisioning regression.
        Returns: None; asserts the private copy exists and the church's OBS settings were not copied.
        """
        real_obs = Path(__file__).resolve().parents[1] / "Resourses" / "obs_portable" / "bin" / "64bit" / "obs64.exe"
        if not real_obs.is_file():
            self.skipTest("A real obs64.exe is needed for the version check")
        source = self.installed
        (source / "release.json").write_text(json.dumps({"application": APP_NAME, "package_type": "installation"}))
        for filename in ("apply_windows_update.ps1", "create_desktop_shortcut.ps1", "uninstall_windows_app.ps1", "uninstall_windows_app.bat", "install_obs_runtime.ps1"):
            shutil.copy2(self.installer.with_name(filename), source / filename)
        local_app_data = self.base / "LocalAppData"
        installed_obs = local_app_data / "Programs" / "obs-studio"
        (installed_obs / "bin" / "64bit").mkdir(parents=True)
        shutil.copy2(real_obs, installed_obs / "bin" / "64bit" / "obs64.exe")
        (installed_obs / "obs-plugins" / "64bit").mkdir(parents=True)
        for plugin in ("obs-websocket.dll", "obs-browser.dll"):
            (installed_obs / "obs-plugins" / "64bit" / plugin).write_bytes(b"plugin")
        (installed_obs / "data" / "obs-studio").mkdir(parents=True)
        (installed_obs / "config").mkdir()
        (installed_obs / "config" / "church-stream.ini").write_text("church settings")
        background = self.base / "default.jpg"
        background.write_bytes(b"default background")
        legacy = self.base / "legacy data"
        legacy.mkdir()
        destination = self.base / "fresh" / "Fixture Church"
        shared = self.base / "fresh shared data"
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                   str(self.installer.with_name("install_windows_app.ps1")), "-SourceDirectory", str(source),
                   "-DestinationDirectory", str(destination), "-DataDirectory", str(shared), "-ChurchName", "Fixture Church",
                   "-DefaultBackgroundFile", str(background), "-LegacyDataDirectory", str(legacy),
                   "-NoAdmin", "-NoShortcuts", "-NoLaunch", "-NoPause", "-AcceptTerms"]
        environment = dict(os.environ, LOCALAPPDATA=str(local_app_data))
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OBS Studio for PewVista: copied", result.stdout)
        private_obs = destination / "Resourses" / "obs_portable"
        self.assertTrue((private_obs / "bin" / "64bit" / "obs64.exe").is_file())
        self.assertFalse((private_obs / "config").exists())
        self.assertFalse(private_obs.with_name("obs_portable.staging").exists())

    def test_install_zip_contains_only_explicitly_selected_backgrounds(self):
        """What: Keep copyrighted backgrounds out of Install ZIPs unless selected.

        Why included: Fresh-install packages must not redistribute the creator's
        entire local background collection.
        Where used: Installation ZIP packaging regression.
        Returns: None; checks selected, unselected, and empty background cases.
        """
        if shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell is required")
        backgrounds = self.installed / "Backgrounds"
        backgrounds.mkdir()
        selected = backgrounds / "approved.jpg"
        selected.write_bytes(b"selected background")
        (backgrounds / "copyrighted.mp4").write_bytes(b"must not be distributed")
        shared_backgrounds = self.installed / "Data" / "Backgrounds"
        shared_backgrounds.mkdir(parents=True, exist_ok=True)
        (shared_backgrounds / "private.mp4").write_bytes(b"must not be distributed")
        selection_path = self.base / "release-selection.json"
        selection_path.write_text(json.dumps({"data_archive": "", "backgrounds": [str(selected)]}), encoding="utf-8")
        environment = os.environ.copy()
        environment["TLM_RELEASE_SELECTION"] = str(selection_path)
        target = self.base / "selected-install.zip"
        script = self.installer.with_name("create_windows_zip.ps1")
        install_backgrounds = script.with_name("install_backgrounds")
        bundled = {PRODUCT_NAME + "/Backgrounds/" + path.name for path in install_backgrounds.glob("*") if path.is_file()} if install_backgrounds.is_dir() else set()
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-AppDirectory", str(self.installed), "-ArchivePath", str(target),
            "-PackageType", "Installation"], capture_output=True, text=True, timeout=30, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            self.assertIn(PRODUCT_NAME + "/Backgrounds/approved.jpg", names)
            self.assertNotIn(PRODUCT_NAME + "/Backgrounds/copyrighted.mp4", names)
            self.assertTrue(bundled.issubset(names))

        selection_path.write_text(json.dumps({"data_archive": "", "backgrounds": []}), encoding="utf-8")
        empty_target = self.base / "empty-install.zip"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-AppDirectory", str(self.installed), "-ArchivePath", str(empty_target),
            "-PackageType", "Installation"], capture_output=True, text=True, timeout=30, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(empty_target) as archive:
            background_files = {name for name in archive.namelist() if "/Backgrounds/" in name and not name.endswith("/")}
            self.assertEqual(background_files, bundled)
            if not bundled:
                self.assertIn(PRODUCT_NAME + "/Backgrounds/", archive.namelist())

        full_target = self.base / "full-data-install.zip"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-AppDirectory", str(self.installed), "-ArchivePath", str(full_target),
            "-PackageType", "Installation", "-IncludeUserData"], capture_output=True, text=True, timeout=30, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(full_target) as archive:
            self.assertEqual({name for name in archive.namelist() if "/Backgrounds/" in name and not name.endswith("/")}, bundled)

    def test_selected_update_content_merges_without_replacing_existing_data(self):
        """What: Package selected Library and one background, then update and merge on a fixture PC.

        Why included: Opt-in update data must preserve local records and exclude unselected media.
        Where used: Creator ZIP, update preparation, installer, and startup merge integration.
        Returns: None; checks backups, selections, integrity, and repeated startup behavior.
        """
        from data_transfer import export_database_archive

        if shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell is required")
        source_data = self.base / "creator data"
        source_data.mkdir()
        for directory, value in ((source_data, "creator song"), (self.shared_data, "local song")):
            database = directory / "library.db"
            if database.exists():
                database.unlink()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE songs (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("INSERT INTO songs(value) VALUES (?)", (value,))
                connection.commit()
        data_archive = self.base / "selected-data.zip"
        export_database_archive(data_archive, ["library"], source_data, include_media=False)
        selected = self.base / "selected.jpg"
        selected.write_bytes(b"selected background")
        backgrounds = self.installed / "Backgrounds"
        backgrounds.mkdir()
        (backgrounds / "private.jpg").write_bytes(b"unselected")
        selection = self.base / "selection.json"
        selection.write_text(json.dumps({"data_archive": str(data_archive), "backgrounds": [str(selected)]}), encoding="utf-8")
        environment = os.environ.copy()
        environment["TLM_RELEASE_SELECTION"] = str(selection)
        target = self.base / "selected-update.zip"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.installer.with_name("create_windows_zip.ps1")), "-AppDirectory", str(self.installed), "-ArchivePath", str(target)], capture_output=True, text=True, timeout=30, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            self.assertIn(APP_NAME + "/Backgrounds/selected.jpg", names)
            self.assertNotIn(APP_NAME + "/Backgrounds/private.jpg", names)
            self.assertFalse(any(name.startswith(APP_NAME + "/Data/") for name in names))
            manifest = json.loads(archive.read(APP_NAME + "/release.json"))
            self.assertEqual(manifest["optional_data_sha256"], hashlib.sha256(data_archive.read_bytes()).hexdigest())
        prepared = self.prepare(target)
        result = self.run_installer(prepared)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(apply_optional_update_data(self.installed, self.shared_data), ("library",))
        with closing(sqlite3.connect(self.shared_data / "library.db")) as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT value FROM songs ORDER BY id")], ["local song", "creator song"])
        self.assertTrue(list((self.shared_data / ".import-backups").glob("*.zip")))
        self.assertEqual(apply_optional_update_data(self.installed, self.shared_data), ())
        self.assertEqual((self.shared_data / "Backgrounds/selected.jpg").read_bytes(), b"selected background")

    def test_optional_update_data_rejects_bad_checksum(self):
        """What: Reject a corrupted selected-data archive before changing receiving records.

        Why included: Incomplete or tampered transfer data must never reach the merge step.
        Where used: Update startup integrity regression.
        Returns: None; checks that the original database remains unchanged.
        """
        (self.installed / "release.json").write_text(json.dumps({"application": APP_NAME, "package_type": "update", "optional_data_sha256": "0" * 64}))
        (self.installed / "optional-update-data.zip").write_bytes(b"damaged archive")
        with self.assertRaisesRegex(ValueError, "checksum"):
            apply_optional_update_data(self.installed, self.shared_data)
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")

    def test_self_extract_bootstrap_unpacks_launches_and_cleans_temp(self):
        """What: Exercise the self-extract bootstrap using a fake installer in a temp package.

        Why included: Setup.exe must expand its embedded ZIP, pass paths with spaces, wait, and clean up.
        Where used: Self-extracting customer installer regression.
        Returns: None; writes only inside the disposable test directory.
        """
        if shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell is required")
        package_directory = self.base / "self extract package"
        source_directory = package_directory / "PewVista"
        source_directory.mkdir(parents=True)
        (source_directory / "release.json").write_text(json.dumps({
            "application": APP_NAME,
            "package_type": "installation",
        }))
        (source_directory / "install_windows_app.ps1").write_text(
            "param([string]$SourceDirectory)\n" 
            "[System.IO.File]::WriteAllText($env:SETUP_PROBE_MARKER, $SourceDirectory)\n"
        )
        archive_path = package_directory / "PewVista-Install-Windows-x64.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(source_directory / "release.json", "PewVista/release.json")
            archive.write(source_directory / "install_windows_app.ps1", "PewVista/install_windows_app.ps1")
        temporary_root = self.base / "bootstrap temp"
        temporary_root.mkdir()
        marker_path = self.base / "bootstrap-ran.txt"
        environment = os.environ.copy()
        environment["TEMP"] = str(temporary_root)
        environment["TMP"] = str(temporary_root)
        environment["SETUP_PROBE_MARKER"] = str(marker_path)
        bootstrap = Path(__file__).resolve().parents[1] / "setup_windows_self_extract.ps1"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
            "-File", str(bootstrap), "-PackageDirectory", str(package_directory)],
            capture_output=True, text=True, timeout=45, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PewVista", marker_path.read_text())
        self.assertEqual(list(temporary_root.iterdir()), [])

    def test_release_pair_and_fresh_install_do_not_ship_personal_data(self):
        """What: Build separate Install/Update ZIPs and perform a fresh installation in a fixture.

        Why included: Customers need setup files while updates must reject personal data and the wrong package type.
        Where used: Customer release/installation end-to-end regression.
        Returns: None; runs only against disposable folders and never creates real shortcuts.
        """
        if shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell is required")
        (self.installed / (APP_NAME + ".exe")).unlink()
        (self.installed / "PewVista.exe").write_bytes(b"MZPewVista program")
        for filename in ("apply_windows_update.ps1", "install_windows_app.ps1", "install_windows_app.bat", "create_desktop_shortcut.ps1", "uninstall_windows_app.ps1", "uninstall_windows_app.bat"):
            shutil.copy2(self.installer.with_name(filename), self.installed / filename)
        # Offline stand-in for the OBS helper; real provisioning is covered by its own test.
        (self.installed / "install_obs_runtime.ps1").write_text(
            "function Install-PewVistaObsRuntime { param($Destination, $Progress) return 'skipped in test' }\n", encoding="utf-8")
        references = self.installed / "Data" / "biblical_lexicon"
        references.mkdir(parents=True)
        (references / "reference.json").write_text('{"reference":true}')
        appearance_settings = {
            "theme": "Sage Linen",
            "font_family": "Arial",
            "font_style": "Bold",
            "font_size": 8,
            "display_font_family": "Calibri",
            "display_font_style": "bold",
            "display_outline_size": "7",
            "display_text_color": "Auto",
        }
        (self.installed / "Data" / "ui_settings.json").write_text(json.dumps(appearance_settings))
        creator_bible = self.installed / "Bibles" / "Creator Bible.pdf"
        creator_bible.parent.mkdir()
        creator_bible.write_bytes(b"already shipped Bible")
        background_directory = self.installed / "Backgrounds"
        background_directory.mkdir()
        selected_background = background_directory / "approved.jpg"
        selected_background.write_bytes(b"selected background")
        (background_directory / "copyrighted.mp4").write_bytes(b"must not be distributed")
        selection_path = self.base / "release-selection.json"
        selection_path.write_text(json.dumps({"data_archive": "", "backgrounds": [str(selected_background)]}), encoding="utf-8")
        environment = os.environ.copy()
        environment["TLM_RELEASE_SELECTION"] = str(selection_path)
        output = self.base / "releases"
        app_directory = self.base / "release program" / "PewVista"
        shutil.copytree(self.installed, app_directory)
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(self.installer.with_name("package_windows_release.ps1")),
            "-AppDirectory", str(app_directory), "-OutputRoot", str(output)], capture_output=True, text=True, timeout=90, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        release = next(output.iterdir())
        install_zip = release / "PewVista-Install-Windows-x64.zip"
        update_zip = release / "PewVista-Update-Windows-x64.zip"
        setup_executable = release / "PewVista Setup.exe"
        self.assertTrue(install_zip.is_file())
        self.assertTrue(update_zip.is_file())
        self.assertTrue(setup_executable.is_file())
        with setup_executable.open("rb") as setup_file:
            self.assertEqual(setup_file.read(2), b"MZ")
        self.assertEqual(release.name, "latest")
        with zipfile.ZipFile(update_zip) as archive:
            update_names = archive.namelist()
            self.assertFalse(any(name.startswith(APP_NAME + "/Bibles/") for name in update_names))
            self.assertIn(APP_NAME + "/Backgrounds/approved.jpg", update_names)
            self.assertNotIn(APP_NAME + "/Backgrounds/copyrighted.mp4", update_names)
        with zipfile.ZipFile(install_zip) as archive:
            install_names = archive.namelist()
            self.assertIn("PewVista/Backgrounds/approved.jpg", install_names)
            self.assertNotIn("PewVista/Backgrounds/copyrighted.mp4", install_names)
        new_creator_bible = app_directory / "Bibles" / "New Creator Bible.pdf"
        new_creator_bible.write_bytes(b"newly added creator Bible")
        for repeat_index in range(2):
            repeated = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(self.installer.with_name("package_windows_release.ps1")),
                "-AppDirectory", str(app_directory), "-OutputRoot", str(output)], capture_output=True, text=True, timeout=90)
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            if repeat_index == 0:
                delta_zip = output / "latest" / "PewVista-Update-Windows-x64.zip"
                with zipfile.ZipFile(delta_zip) as archive:
                    names = archive.namelist()
                    self.assertIn(APP_NAME + "/Bibles/New Creator Bible.pdf", names)
                    self.assertNotIn(APP_NAME + "/Bibles/Creator Bible.pdf", names)
        self.assertEqual({path.name for path in output.iterdir()}, {"latest", "previous"})
        self.assertEqual(len(list((output / "previous").glob("*.zip"))), 2)
        self.assertTrue((output / "latest" / "PewVista Setup.exe").is_file())
        self.assertTrue((output / "previous" / "PewVista Setup.exe").is_file())
        with zipfile.ZipFile(install_zip) as archive:
            names = archive.namelist()
            self.assertIn("PewVista/Data/biblical_lexicon/reference.json", names)
            self.assertIn("PewVista/Data/ui_settings.json", names)
            self.assertNotIn("PewVista/Data/library.db", names)
            self.assertNotIn("PewVista/installation.json", names)
            archive.extractall(self.base / "extracted")
        with self.assertRaisesRegex(ValueError, "Update ZIP"):
            self.prepare(install_zip)
        with zipfile.ZipFile(update_zip) as archive:
            self.assertNotIn(APP_NAME + "/Data/ui_settings.json", archive.namelist())
        prepared = self.prepare(update_zip)
        self.assertFalse((prepared.payload / "Data").exists())
        self.assertFalse((prepared.payload / "Data/ui_settings.json").exists())
        source = self.base / "extracted" / "PewVista"
        destination = self.base / "new customer" / "Grace Chapel"
        legacy_data = self.base / "legacy Data"
        legacy_data.mkdir()
        (legacy_data / "library.db").write_bytes(b"imported customer database")
        background = self.base / "Sunday background.png"
        background.write_bytes(b"fixture background bytes")
        shared_data = self.base / "shared customer data"
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                   str(source / "install_windows_app.ps1"), "-SourceDirectory", str(source),
               "-DestinationDirectory", str(destination), "-DataDirectory", str(shared_data),
               "-ChurchName", "Grace Chapel", "-DefaultBackgroundFile", str(background),
               "-LegacyDataDirectory", str(legacy_data), "-NoAdmin", "-NoShortcuts", "-NoLaunch", "-NoPause", "-AcceptTerms"]
        mismatched_command = command.copy()
        mismatched_folder = self.base / "new customer" / "Not Grace Chapel"
        mismatched_command[mismatched_command.index("-DestinationDirectory") + 1] = str(mismatched_folder)
        mismatch = subprocess.run(mismatched_command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("must be named", mismatch.stdout)
        self.assertFalse(mismatched_folder.exists())
        installed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual((destination / "PewVista.exe").read_bytes(), b"MZPewVista program")
        settings = json.loads((destination / "installation.json").read_text())
        self.assertEqual(settings["church_name"], "Grace Chapel")
        self.assertEqual(Path(settings["data_directory"]), shared_data)
        self.assertEqual(Path(settings["default_background_path"]).read_bytes(), background.read_bytes())
        self.assertTrue((shared_data / "biblical_lexicon/reference.json").is_file())
        self.assertEqual(json.loads((shared_data / "ui_settings.json").read_text()), appearance_settings)
        self.assertEqual((shared_data / "library.db").read_bytes(), b"imported customer database")
        self.assertFalse((destination / "Data").exists())
        (shared_data / "library.db").write_bytes(b"customer records")
        refused = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual((shared_data / "library.db").read_bytes(), b"customer records")

    def test_update_in_sandbox_leaves_working_installation_unchanged(self):
        """What: Clone an old working installation and apply an update only to the clone.

        Why included: Release testing must not replace the developer's original executable or databases.
        Where used: Isolated Windows update testing workflow regression.
        Returns: None; validates original bytes and persistent test labelling after update.
        """
        if shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell is required")
        sandbox_root = self.base / "sandbox"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(self.installer.with_name("test_windows_update.ps1")), "-SourceDirectory", str(self.installed),
            "-TestRoot", str(sandbox_root), "-NoLaunch"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sandbox = sandbox_root / PRODUCT_NAME
        self.assertTrue((sandbox / ".test-installation").is_file())
        sandbox_data = sandbox_root / "SharedData"
        self.assertEqual((sandbox_data / "library.db").read_bytes(), b"LATEST USER SONGS")
        archive = self.archive({APP_NAME + "/.test-installation": b"must not overwrite the test marker"})
        update_signing.sign_update_archive(archive, self.signing_key_path)
        prepared = prepare_update_archive(archive, sandbox, self.installer)
        self.prepared.append(prepared)
        installed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(prepared.installer), "-PayloadDirectory", str(prepared.payload), "-InstallDirectory", str(sandbox),
            "-NoAdmin", "-NoLaunch", "-NoPause"], capture_output=True, text=True, timeout=45)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual((sandbox / (APP_NAME + ".exe")).read_bytes(), b"MZnew program")
        self.assertEqual((sandbox_data / "library.db").read_bytes(), b"LATEST USER SONGS")
        self.assertNotEqual((sandbox / ".test-installation").read_bytes(), b"must not overwrite the test marker")
        self.assertEqual((self.installed / (APP_NAME + ".exe")).read_bytes(), b"MZold program")
        self.assertEqual((self.installed / "_internal/runtime.dll").read_bytes(), b"old runtime")
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")

    def run_uninstaller(self, action):
        """What: Run file-only uninstall against the disposable installation with an explicit data choice.

        Why included: Tests must not present confirmation prompts or target real app folders.
        Where used: Keep/remove/source-guard uninstall regressions.
        Returns: Captured native process result.
        """
        if shutil.which("powershell.exe") is None:
            self.skipTest("Windows PowerShell is required")
        return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(self.installer.with_name("uninstall_windows_app.ps1")), "-AppDirectory", str(self.installed),
            "-DataAction", action, "-ConfirmUninstall", "-NoAdmin", "-NoPause"], capture_output=True, text=True, timeout=30)

    def test_uninstall_keeps_personal_data(self):
        """What: Remove program files but retain databases, settings, imports, and local backups.

        Why included: Keeping personal data is the safe default uninstall behavior.
        Where used: File-only uninstaller preservation regression.
        Returns: None.
        """
        for folder in ("Bibles", "Backgrounds", "config", ".update-backups"):
            (self.installed / folder).mkdir()
            (self.installed / folder / "fixture.txt").write_text("keep this")
        for filename in ("uninstall_windows_app.ps1", "uninstall_windows_app.bat"):
            shutil.copy2(self.installer.with_name(filename), self.installed / filename)
        result = self.run_uninstaller("Keep")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.installed / (APP_NAME + ".exe")).exists())
        self.assertFalse((self.installed / "_internal").exists())
        self.assertFalse((self.installed / "uninstall_windows_app.ps1").exists())
        marker = json.loads((self.installed / ".light-ministries-uninstalled.json").read_text())
        self.assertEqual(marker["state"], "uninstalled")
        self.assertEqual(Path(marker["data_directory"]), self.shared_data)
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")
        self.assertEqual((self.shared_data / "ui_settings.json").read_text(), "latest settings")
        for folder in ("Bibles", "Backgrounds", "config", ".update-backups"):
            self.assertEqual((self.installed / folder / "fixture.txt").read_text(), "keep this")

    def test_reinstall_after_keep_uninstall_preserves_database_and_imports(self):
        """What: Reinstall into a marker-authorized folder after Keep-data uninstall.

        Why included: Customers must regain the app without losing shared records/imports.
        Where used: Reinstall-marker integration regression.
        Returns: None; verifies program refresh and preserved ProgramData content.
        """
        settings = {
            "church_name": APP_NAME,
            "data_directory": str(self.shared_data),
            "default_background_path": str(self.shared_data / "default_background" / "church-default.jpg"),
        }
        (self.shared_data / "default_background").mkdir()
        saved_background = self.shared_data / "default_background" / "church-default.jpg"
        saved_background.write_bytes(b"saved default background")
        (self.installed / "installation.json").write_text(json.dumps(settings))
        (self.installed / "Bibles").mkdir()
        (self.installed / "Bibles" / "imported.pdf").write_bytes(b"customer imported Bible")
        (self.installed / "Backgrounds").mkdir()
        (self.installed / "Backgrounds" / "church.jpg").write_bytes(b"customer background")
        (self.installed / "config").mkdir()
        (self.installed / "config" / "keep.json").write_text("customer config")
        for filename in ("uninstall_windows_app.ps1", "uninstall_windows_app.bat"):
            shutil.copy2(self.installer.with_name(filename), self.installed / filename)

        uninstalled = self.run_uninstaller("Keep")
        self.assertEqual(uninstalled.returncode, 0, uninstalled.stdout + uninstalled.stderr)
        self.assertTrue((self.installed / ".light-ministries-uninstalled.json").is_file())

        source = self.base / "reinstall package" / APP_NAME
        (source / "_internal").mkdir(parents=True)
        (source / "release.json").write_text(json.dumps({"application": APP_NAME, "package_type": "installation"}))
        (source / (APP_NAME + ".exe")).write_bytes(b"MZnew program")
        (source / "_internal" / "runtime.dll").write_bytes(b"new runtime")
        (source / "Bibles").mkdir()
        (source / "Bibles" / "imported.pdf").write_bytes(b"packaged default must not replace customer Bible")
        (source / "Backgrounds").mkdir()
        (source / "Backgrounds" / "church.jpg").write_bytes(b"packaged default must not replace customer background")
        (source / "Data" / "biblical_lexicon").mkdir(parents=True)
        (source / "Data" / "biblical_lexicon" / "reference.json").write_text("updated reference cache")
        for filename in ("install_windows_app.ps1", "apply_windows_update.ps1", "create_desktop_shortcut.ps1", "uninstall_windows_app.ps1", "uninstall_windows_app.bat"):
            shutil.copy2(self.installer.with_name(filename), source / filename)
        empty_legacy = self.base / "empty legacy Data"
        empty_legacy.mkdir()
        new_background = self.base / "replacement background.jpg"
        new_background.write_bytes(b"replacement default background")
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            str(source / "install_windows_app.ps1"), "-SourceDirectory", str(source),
            "-DestinationDirectory", str(self.installed), "-ChurchName", APP_NAME,
            "-DefaultBackgroundFile", str(new_background), "-LegacyDataDirectory", str(empty_legacy),
            "-NoAdmin", "-NoShortcuts", "-NoLaunch", "-NoPause", "-AcceptTerms"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.installed / (APP_NAME + ".exe")).read_bytes(), b"MZnew program")
        self.assertEqual((self.installed / "_internal" / "runtime.dll").read_bytes(), b"new runtime")
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")
        self.assertEqual((self.shared_data / "ui_settings.json").read_text(), "latest settings")
        self.assertEqual((self.installed / "Bibles" / "imported.pdf").read_bytes(), b"customer imported Bible")
        self.assertEqual((self.installed / "Backgrounds" / "church.jpg").read_bytes(), b"customer background")
        self.assertEqual((self.installed / "config" / "keep.json").read_text(), "customer config")
        self.assertEqual((self.shared_data / "biblical_lexicon" / "reference.json").read_text(), "updated reference cache")
        self.assertEqual(saved_background.read_bytes(), b"replacement default background")
        self.assertFalse((self.installed / ".light-ministries-uninstalled.json").exists())

    def test_reinstall_from_legacy_uninstall_residue_without_marker(self):
        """What: Recover an older uninstall using only recognized app leftovers and ProgramData.

        Why included: Existing customers may have uninstalled before marker support shipped.
        Where used: Legacy reinstall compatibility regression.
        Returns: None; verifies data retention and refusal of unrelated folders.
        """
        destination = self.base / "legacy residue" / "Legacy Chapel"
        (destination / "Bibles").mkdir(parents=True)
        (destination / "Bibles" / "customer.pdf").write_bytes(b"customer Bible")
        (destination / "Backgrounds").mkdir()
        (destination / "Backgrounds" / "customer.jpg").write_bytes(b"customer background")
        self.assertTrue((self.shared_data / "library.db").is_file())

        source = self.base / "legacy recovery package" / "Legacy Chapel"
        (source / "_internal").mkdir(parents=True)
        (source / "release.json").write_text(json.dumps({"application": APP_NAME, "package_type": "installation"}))
        (source / (APP_NAME + ".exe")).write_bytes(b"MZreinstalled program")
        (source / "_internal" / "runtime.dll").write_bytes(b"new runtime")
        (source / "Bibles").mkdir()
        (source / "Bibles" / "customer.pdf").write_bytes(b"packaged default Bible")
        (source / "Backgrounds").mkdir()
        (source / "Backgrounds" / "customer.jpg").write_bytes(b"packaged default background")
        (source / "Data" / "biblical_lexicon").mkdir(parents=True)
        (source / "Data" / "biblical_lexicon" / "reference.json").write_text("updated references")
        for filename in ("install_windows_app.ps1", "apply_windows_update.ps1", "create_desktop_shortcut.ps1", "uninstall_windows_app.ps1", "uninstall_windows_app.bat"):
            shutil.copy2(self.installer.with_name(filename), source / filename)
        legacy = self.base / "unused legacy source"
        legacy.mkdir()
        optional_background = self.base / "legacy reinstall background.jpg"
        optional_background.write_bytes(b"test background")
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            str(source / "install_windows_app.ps1"), "-SourceDirectory", str(source),
            "-DestinationDirectory", str(destination), "-DataDirectory", str(self.shared_data),
            "-ChurchName", "Legacy Chapel", "-DefaultBackgroundFile", str(optional_background), "-LegacyDataDirectory", str(legacy),
            "-NoAdmin", "-NoShortcuts", "-NoLaunch", "-NoPause", "-AcceptTerms"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((destination / (APP_NAME + ".exe")).read_bytes(), b"MZreinstalled program")
        self.assertEqual((destination / "Bibles" / "customer.pdf").read_bytes(), b"customer Bible")
        self.assertEqual((destination / "Backgrounds" / "customer.jpg").read_bytes(), b"customer background")
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")
        self.assertEqual((self.shared_data / "ui_settings.json").read_text(), "latest settings")
        self.assertEqual((self.shared_data / "biblical_lexicon" / "reference.json").read_text(), "updated references")
        self.assertFalse((destination / ".light-ministries-uninstalled.json").exists())

        unrelated = self.base / "unrelated existing folder" / "Legacy Chapel"
        unrelated.mkdir(parents=True)
        (unrelated / "personal.txt").write_text("do not overwrite")
        rejected = command.copy()
        rejected[rejected.index("-DestinationDirectory") + 1] = str(unrelated)
        result = subprocess.run(rejected, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((unrelated / "personal.txt").read_text(), "do not overwrite")

    def test_uninstall_removes_data_only_when_explicitly_selected(self):
        """What: Remove selected personal data while leaving unrelated files and external backups alone.

        Why included: A complete uninstall must remain scoped to this app's known files.
        Where used: Explicit data-removal regression.
        Returns: None.
        """
        (self.installed / "personal-notes.txt").write_text("unrelated file")
        (self.base / "external-backup.db").write_bytes(b"external backup")
        result = self.run_uninstaller("Remove")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.shared_data.exists())
        self.assertFalse((self.installed / (APP_NAME + ".exe")).exists())
        self.assertEqual((self.installed / "personal-notes.txt").read_text(), "unrelated file")
        self.assertEqual((self.base / "external-backup.db").read_bytes(), b"external backup")

    def test_uninstall_refuses_development_workspace(self):
        """What: Reject an app-like folder containing source workspace markers before removal.

        Why included: Running the uninstaller in the shared development folder must never remove the project.
        Where used: Workspace safety regression.
        Returns: None.
        """
        (self.installed / "main.py").write_text("development workspace marker")
        result = self.run_uninstaller("Remove")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("development workspace", result.stdout)
        self.assertEqual((self.installed / (APP_NAME + ".exe")).read_bytes(), b"MZold program")
        self.assertEqual((self.shared_data / "library.db").read_bytes(), b"LATEST USER SONGS")


if __name__ == "__main__":
    unittest.main()