"""Regression tests for the automatic update feed, download check, and feed publishing."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import auto_update


class AutoUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        patcher = patch.object(auto_update, "get_app_data_path", return_value=self.base / "data")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.files: dict[str, bytes] = {}

        files = self.files

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = files.get(self.path.lstrip("/"))
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def test_version_comparison(self) -> None:
        self.assertTrue(auto_update.is_newer("20261110-080000-000", "20261109-202626-123"))
        self.assertFalse(auto_update.is_newer("20261109-202626-123", "20261109-202626-123"))
        self.assertFalse(auto_update.is_newer("20261108-000000-000", "20261109-202626-123"))
        self.assertFalse(auto_update.is_newer("20261110-080000-000", "development build"))

    def test_check_reports_newer_release_and_records_time(self) -> None:
        feed = {"version": "20261110-080000-000", "url": "https://example.invalid/u.zip", "sha256": "a" * 64}
        self.files["latest.json"] = json.dumps(feed).encode()
        with patch.object(auto_update, "UPDATE_FEED_URL", self.url):
            self.assertTrue(auto_update.check_due())
            self.assertEqual(auto_update.check_for_update("20261109-000000-000")["version"], feed["version"])
            self.assertFalse(auto_update.check_due())
            self.assertIsNone(auto_update.check_for_update("20261110-080000-000"))

    def test_bad_feed_and_offline_errors(self) -> None:
        self.files["latest.json"] = json.dumps({"version": "1", "url": "http://insecure", "sha256": "x"}).encode()
        with self.assertRaisesRegex(auto_update.UpdateCheckError, "incomplete"):
            auto_update.fetch_feed(self.url)
        with self.assertRaisesRegex(auto_update.UpdateCheckError, "not set up"):
            auto_update.fetch_feed("")
        with self.assertRaisesRegex(auto_update.UpdateCheckError, "internet"):
            auto_update.fetch_feed("http://127.0.0.1:9")

    def test_download_checks_hash(self) -> None:
        payload = b"signed update zip bytes"
        self.files["u.zip"] = payload
        target = self.base / "download" / "u.zip"
        feed = {"url": f"{self.url}/u.zip", "sha256": hashlib.sha256(payload).hexdigest()}
        seen: list[tuple[int, int]] = []
        self.assertEqual(auto_update.download_update(feed, target, progress=lambda r, t: seen.append((r, t))).read_bytes(), payload)
        self.assertEqual(seen[-1], (len(payload), len(payload)))
        target.unlink()
        with self.assertRaisesRegex(auto_update.UpdateCheckError, "security check"):
            auto_update.download_update({**feed, "sha256": "0" * 64}, target)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name("u.zip.part").exists())

    def test_publisher_writes_feed_from_update_zip(self) -> None:
        release = self.base / "release"
        release.mkdir()
        update = release / auto_update.UPDATE_FILE_NAME
        with zipfile.ZipFile(update, "w") as archive:
            archive.writestr("The Light Ministries/release.json", json.dumps({"version": "20261110-080000-000"}))
            archive.writestr("The Light Ministries/PewVista.exe", b"MZ")
        feed = json.loads(auto_update.write_feed(release, "https://downloads.example/pewvista/", "Fixes").read_text())
        self.assertEqual(feed["version"], "20261110-080000-000")
        self.assertEqual(feed["url"], "https://downloads.example/pewvista/" + auto_update.UPDATE_FILE_NAME)
        self.assertEqual(feed["sha256"], hashlib.sha256(update.read_bytes()).hexdigest())
        self.assertEqual(feed["notes"], "Fixes")


if __name__ == "__main__":
    unittest.main()
