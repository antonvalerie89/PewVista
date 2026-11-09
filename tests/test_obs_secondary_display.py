"""Regressions for writable installed OBS runtime and configuration paths."""

import hashlib
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

import obs_secondary_display as obs_module
from obs_secondary_display import DEFAULT_OBS_SETTINGS, SECONDARY_DISPLAY_HTML, ObsSecondaryDisplayController, ObsSecondaryDisplayError


class ObsBrowserScriptTests(unittest.TestCase):
    """What: Check the generated OBS browser-page script.

    Why included: The live screen depends on JavaScript built from Python strings.
    Where used: run_reliability_checks.bat release gate.
    Returns: None; each test asserts its own results.
    """
    def test_corner_split_preserves_javascript_escapes(self) -> None:
        """What: Keep escaped line breaks intact when corner text is split in JavaScript.

        Why included: Broken escapes would merge the song key and details onto one line.
        Where used: OBS browser script regression suite.
        Returns: None; asserts the escape sequence in the page source.
        """
        self.assertIn(r"String(value).split(/\r?\n/)", SECONDARY_DISPLAY_HTML)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to validate browser JavaScript")
    def test_generated_browser_script_has_valid_syntax(self) -> None:
        """What: Check that the OBS page's JavaScript parses without syntax errors.

        Why included: A syntax error would leave the live screen blank.
        Where used: OBS browser script regression suite.
        Returns: None; skipped when Node.js is unavailable.
        """
        script = SECONDARY_DISPLAY_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run([shutil.which("node"), "--check", "-"], input=script, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to exercise browser JavaScript")
    def test_browser_corner_details_use_scaled_smaller_font(self) -> None:
        """What: Render corner details smaller than the key, scaled to the output.

        Why included: Song details must stay readable but secondary to the key.
        Where used: OBS browser script regression suite.
        Returns: None; asserts the computed font sizes.
        """
        corner_code = SECONDARY_DISPLAY_HTML.split("    const cornerValue =", 1)[1].split("    const scrollEnabled =", 1)[0]
        program = """
const assert = require('node:assert/strict');
const state = {overlay_corner_text: 'C\\nSong title\\nAuthor: Ada\\nCCLI License: 12345', overlay_corner_font_size: 72, overlay_corner_detail_font_size: 60};
const cornerText = {children: [], replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); }};
const document = {createElement() { return {style: {}, textContent: ''}; }};
const cornerValue =""" + corner_code + """
renderCornerText(cornerValue);
assert.deepEqual(cornerText.children.map(child => child.style.fontSize), ['72px', '60px', '60px', '60px']);
assert.deepEqual(cornerText.children.map(child => child.textContent), ['C', 'Song title', 'Author: Ada', 'CCLI License: 12345']);
"""
        result = subprocess.run([shutil.which("node"), "-"], input=program, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


class InstalledObsPathTests(unittest.TestCase):
    """What: Exercise OBS path routing using disposable installed/source layouts.

    Why included: Standard users cannot write portable OBS configuration in Program Files.
    Where used: Windows runtime regression suite.
    Returns: Test results only; no real OBS process is launched.
    """

    def setUp(self) -> None:
        """What: Create fake packaged OBS and shared-data folders.

        Why included: Permission-path tests must not touch the real installed runtime.
        Where used: unittest fixture lifecycle.
        Returns: None; prepares a controller without its HTTP server or UI.
        """
        self.temporary = tempfile.TemporaryDirectory(prefix="LightObsPathTest-")
        self.root = Path(self.temporary.name)
        self.app = self.root / "Program Files" / "Fixture Church"
        self.shared = self.root / "ProgramData" / "Fixture Church"
        self.private = self.root / "Local" / "PewVista" / "Fixture Church"
        self.app.mkdir(parents=True)
        self.shared.mkdir(parents=True)
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "Local")})
        self.environment.start()
        (self.app / "installation.json").write_text(json.dumps({"data_directory": str(self.shared)}))
        self.bundled = self.app / "Resourses" / "obs_portable"
        self.executable = self.bundled / "bin" / "64bit" / "obs64.exe"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b"fake OBS runtime")
        self.controller = ObsSecondaryDisplayController.__new__(ObsSecondaryDisplayController)
        self.controller.app_base_path = self.app
        self.controller.settings = dict(DEFAULT_OBS_SETTINGS)

    def tearDown(self) -> None:
        """What: Delete the disposable runtime and data folders.

        Why included: Regression fixtures must not leave background/runtime files behind.
        Where used: unittest fixture lifecycle.
        Returns: None.
        """
        self.environment.stop()
        self.temporary.cleanup()

    def test_installed_config_and_launch_use_shared_runtime(self) -> None:
        """What: Prepare OBS and configure its websocket exclusively under shared data.

        Why included: This reproduces the config mkdir path that failed in Program Files.
        Where used: Installed OBS access-denied regression.
        Returns: None; asserts configuration and launch working directory are writable paths.
        """
        executable = self.controller._ensure_obs_executable()
        self.controller._configure_portable_obs_websocket(executable)
        config = self.controller._portable_obs_websocket_config_path()
        self.assertEqual(executable, self.private / "obs_portable/bin/64bit/obs64.exe")
        self.assertTrue(config.is_relative_to(self.private))
        self.assertTrue(json.loads(config.read_text())["server_enabled"])
        self.assertFalse((self.bundled / "config").exists())
        command, working_directory = self.controller._obs_launch_command(executable)
        self.assertIn("--portable", command)
        self.assertEqual(working_directory, executable.parent)

    def test_remote_control_password_is_generated_once_and_enforced(self) -> None:
        """What: Generate, persist, and reuse an OBS websocket password, then require it.

        Why included: The bundled OBS listens on every network interface; without a
        password any device on the church network could control the projector.
        Where used: OBS remote-control security regression.
        Returns: None; asserts persistence, reuse, and auth_required in OBS config.
        """
        first = self.controller._load_settings()
        password = str(first.get("password") or "")
        self.assertGreaterEqual(len(password), 24)
        saved = json.loads((self.shared / "obs_secondary_settings.json").read_text())
        self.assertEqual(saved["password"], password)
        self.assertEqual(self.controller._load_settings()["password"], password)
        self.controller.settings = first
        executable = self.controller._ensure_obs_executable()
        self.controller._configure_portable_obs_websocket(executable)
        config = json.loads(self.controller._portable_obs_websocket_config_path().read_text())
        self.assertTrue(config["auth_required"])
        self.assertEqual(config["server_password"], password)
        global_ini = (self.controller._portable_obs_root_path() / "config" / "obs-studio" / "global.ini").read_text(encoding="utf-8-sig")
        self.assertIn("EnableAutoUpdates=false", global_ini)
        self.assertNotIn("EnableAutoUpdates=true", global_ini)

    def test_shared_settings_cannot_choose_the_program_that_runs(self) -> None:
        """What: Ignore program paths and arguments from the shared settings file when installed.

        Why included: Every Windows account can edit the shared settings; honoring a path
        there would run a planted program under whoever next enables the live screen.
        Where used: Cross-account code-execution security regression.
        Returns: None; asserts the private runtime is used and planted values are ignored.
        """
        planted = self.shared / "planted.exe"
        planted.write_bytes(b"not OBS")
        self.controller.settings["obs_executable"] = str(planted)
        self.controller.settings["launch_arguments"] = ["--evil"]
        executable = self.controller._ensure_obs_executable()
        self.assertNotEqual(executable, planted)
        self.assertTrue(executable.is_relative_to(self.private))
        command, _working_directory = self.controller._obs_launch_command(executable)
        self.assertNotIn("--evil", command)
        self.assertIn("--minimize-to-tray", command)

    def test_media_endpoint_serves_only_listed_backgrounds(self) -> None:
        """What: Allow /media only for the current and preloaded background media.

        Why included: The loopback server must not hand arbitrary files to local programs.
        Where used: Local file-disclosure security regression.
        Returns: None; asserts listed media is allowed and everything else is refused.
        """
        from obs_secondary_display import SecondaryStateServer

        background = self.shared / "Backgrounds" / "sunday.jpg"
        upcoming = self.shared / "Backgrounds" / "loop.mp4"
        server = SecondaryStateServer()
        try:
            server.update_state({"background_path": str(background), "hot_background_paths": [str(upcoming)]})
            self.assertTrue(server.is_media_allowed(str(background)))
            self.assertTrue(server.is_media_allowed(str(upcoming)))
            self.assertFalse(server.is_media_allowed(str(self.shared / "members.db")))
            self.assertFalse(server.is_media_allowed(str(self.shared / "Backgrounds" / "other.jpg")))
            self.assertFalse(server.is_media_allowed(""))
        finally:
            server.shutdown()

    def test_old_explicit_bundled_path_redirects_to_shared_runtime(self) -> None:
        """What: Resolve an old saved obs_executable setting into shared storage.

        Why included: Existing installations can retain the original Program Files path.
        Where used: Legacy OBS settings migration regression.
        Returns: None; verifies the old path cannot bypass writable runtime preparation.
        """
        self.controller.settings["obs_executable"] = str(self.executable)
        executable = self.controller._ensure_obs_executable()
        self.assertTrue(executable.is_relative_to(self.private))
        self.assertNotEqual(executable, self.executable)

    def test_runtime_refresh_preserves_shared_profile(self) -> None:
        """What: Refresh changed runtime files while retaining shared OBS configuration.

        Why included: Application updates must not overwrite the operator's OBS profile.
        Where used: Installed OBS runtime upgrade regression.
        Returns: None; checks updated binary bytes and unchanged profile content.
        """
        executable = self.controller._ensure_obs_executable()
        profile = self.private / "obs_portable/config/obs-studio/global.ini"
        profile.parent.mkdir(parents=True)
        profile.write_text("saved operator profile")
        self.executable.write_bytes(b"new fake OBS runtime version")
        refreshed = self.controller._ensure_obs_executable()
        self.assertEqual(refreshed, executable)
        self.assertEqual(refreshed.read_bytes(), self.executable.read_bytes())
        self.assertEqual(profile.read_text(), "saved operator profile")

    def test_source_run_keeps_existing_portable_layout(self) -> None:
        """What: Retain the app-local portable OBS path for source/legacy runs.

        Why included: Developer and old portable workflows must not be migrated implicitly.
        Where used: Source-mode OBS compatibility regression.
        Returns: None; confirms discovery needs no shared runtime copy.
        """
        (self.app / "installation.json").unlink()
        self.assertEqual(self.controller._ensure_obs_executable(), self.executable)
        self.assertFalse((self.shared / "obs_portable").exists())

    def test_zip_fallback_extracts_to_shared_data_without_removing_config(self) -> None:
        """What: Extract a bundled OBS ZIP under shared data while preserving its profile.

        Why included: Missing runtime repair must not write to Program Files or delete settings.
        Where used: Installed OBS archive-bootstrap regression.
        Returns: None; validates extraction target and preserved config contents.
        """
        self.executable.unlink()
        archive_path = self.app / "Resourses/obs_bootstrap" / "OBS-Studio-32.1.2-Windows-x64.zip"
        archive_path.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("bin/64bit/obs64.exe", b"repaired OBS runtime")
            archive.writestr("config/obs-studio/global.ini", "packaged defaults")
        profile = self.private / "obs_portable/config/obs-studio/global.ini"
        profile.parent.mkdir(parents=True)
        profile.write_text("existing profile")
        executable = self.controller._ensure_obs_executable()
        self.assertTrue(executable.is_relative_to(self.private))
        self.assertEqual(executable.read_bytes(), b"repaired OBS runtime")
        self.assertEqual(profile.read_text(), "existing profile")

    def _serve_bytes(self, payload: bytes) -> str:
        """What: Serve fixed bytes from a loopback HTTP server for download tests.

        Why included: The OBS download fallback must be tested without the internet.
        Where used: Download provisioning tests.
        Returns: The URL of the served file.
        """
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/obs.zip"

    def test_missing_obs_is_copied_from_installed_obs_without_its_settings(self) -> None:
        """What: Reuse an installed OBS's program files when Setup could not prepare OBS.

        Why included: The installer no longer ships OBS; the church's own OBS settings must stay untouched.
        Where used: Offline-install OBS provisioning regression.
        Returns: None; asserts the private copy and that no config was copied.
        """
        self.executable.unlink()
        installed = self.root / "Program Files" / "obs-studio"
        for relative in ("bin/64bit/obs64.exe", "obs-plugins/64bit/obs-websocket.dll", "obs-plugins/64bit/obs-browser.dll", "data/obs-studio/locale.ini"):
            (installed / relative).parent.mkdir(parents=True, exist_ok=True)
            (installed / relative).write_bytes(b"installed " + relative.encode())
        (installed / "config").mkdir()
        (installed / "config" / "church-stream.ini").write_text("church settings")
        with patch.dict(os.environ, {"ProgramFiles": str(self.root / "Program Files"), "ProgramFiles(x86)": ""}), \
                patch.object(obs_module.urllib.request, "urlopen", side_effect=AssertionError("must not download")):
            executable = self.controller._ensure_obs_executable()
        self.assertEqual(executable, self.private / "obs_portable/bin/64bit/obs64.exe")
        self.assertEqual(executable.read_bytes(), b"installed bin/64bit/obs64.exe")
        self.assertTrue((self.private / "obs_portable/data/obs-studio/locale.ini").is_file())
        self.assertFalse((self.private / "obs_portable/config/church-stream.ini").exists())

    def test_missing_obs_is_downloaded_and_verified(self) -> None:
        """What: Download the official OBS ZIP, check its SHA-256, and extract it privately.

        Why included: PCs without OBS that were offline during Setup must still get OBS.
        Where used: OBS download provisioning regression.
        Returns: None; asserts extraction, config skipping, and hash rejection.
        """
        self.executable.unlink()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("bin/64bit/obs64.exe", b"downloaded OBS")
            archive.writestr("config/obs-studio/global.ini", "upstream defaults")
        payload = buffer.getvalue()
        url = self._serve_bytes(payload)
        empty_program_files = {"ProgramFiles": str(self.root / "none"), "ProgramFiles(x86)": ""}
        with patch.dict(os.environ, empty_program_files), patch.object(obs_module, "OBS_DOWNLOAD_URL", url), \
                patch.object(obs_module, "OBS_DOWNLOAD_SHA256", "0" * 64):
            with self.assertRaisesRegex(ObsSecondaryDisplayError, "security check"):
                self.controller._ensure_obs_executable()
        self.assertFalse((self.private / "obs_portable/bin/64bit/obs64.exe").exists())
        self.assertFalse((self.private / "obs-download.zip.part").exists())
        with patch.dict(os.environ, empty_program_files), patch.object(obs_module, "OBS_DOWNLOAD_URL", url), \
                patch.object(obs_module, "OBS_DOWNLOAD_SHA256", hashlib.sha256(payload).hexdigest()):
            executable = self.controller._ensure_obs_executable()
        self.assertEqual(executable.read_bytes(), b"downloaded OBS")
        self.assertFalse((self.private / "obs_portable/config/obs-studio/global.ini").exists())
        self.assertFalse((self.private / "obs-download.zip.part").exists())

    def test_setup_helper_and_app_pin_the_same_obs_download(self) -> None:
        """What: Keep Setup's and the app's OBS download address and hash identical.

        Why included: Both must fetch exactly the same verified OBS build.
        Where used: Release consistency regression.
        Returns: None.
        """
        helper = (Path(obs_module.__file__).with_name("install_obs_runtime.ps1")).read_text(encoding="utf-8")
        self.assertIn(f'"{obs_module.OBS_DOWNLOAD_URL}"', helper)
        self.assertIn(obs_module.OBS_DOWNLOAD_SHA256.upper(), helper)


if __name__ == "__main__":
    unittest.main()