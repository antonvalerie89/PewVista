"""Regression tests for subscription licensing, trial, offline window, and watermark."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import licensing
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MACHINE = "a" * 32


class LicensingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.key = Ed25519PrivateKey.generate()
        self.registry: dict[str, datetime] = {}
        self.now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        patches = [
            patch.dict(os.environ, {"PEWVISTA_ENFORCE_LICENSE": "1"}),
            patch.object(licensing, "get_app_data_path", return_value=self.data),
            patch.object(licensing, "TRUSTED_LICENSE_KEYS", (licensing.public_key_hex(self.key),)),
            patch.object(licensing, "get_machine_id", return_value=MACHINE),
            patch.object(licensing, "_read_registry_trial_start", side_effect=lambda: self.registry.get("trial")),
            patch.object(licensing, "_write_registry_trial_start", side_effect=lambda value: self.registry.__setitem__("trial", value)),
            patch.object(licensing, "_now", side_effect=lambda: self.now),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.temporary.cleanup)
        licensing.invalidate_status_cache()

    def token(self, days: int = 30, machine: str = MACHINE, subscription: str = "I-TEST123") -> str:
        payload = {"v": 1, "subscription_id": subscription, "machine_id": machine,
                   "issued_at": self.now.isoformat(), "valid_until": (self.now + timedelta(days=days)).isoformat()}
        return licensing.sign_token(payload, self.key)

    def status(self) -> licensing.LicenseStatus:
        return licensing.get_license_status(force=True)

    def test_trial_runs_15_days_then_shows_watermark(self) -> None:
        self.assertEqual(self.status().state, "trial")
        self.assertEqual(self.status().days_left, 15)
        self.assertEqual(licensing.get_watermark_text(), "")
        self.now += timedelta(days=14, hours=23)
        self.assertEqual(self.status().state, "trial")
        self.now += timedelta(hours=2)
        self.assertEqual(self.status().state, "unlicensed")
        licensing.invalidate_status_cache()
        self.assertEqual(licensing.get_watermark_text(), "NOT LICENSED")

    def test_deleting_license_file_does_not_restart_trial(self) -> None:
        self.status()
        self.now += timedelta(days=20)
        (self.data / licensing.LICENSE_FILE_NAME).unlink()
        self.assertEqual(self.status().state, "unlicensed")

    def test_clock_rollback_does_not_extend_trial(self) -> None:
        self.status()
        self.now += timedelta(days=16)
        self.assertEqual(self.status().state, "unlicensed")
        self.now -= timedelta(days=10)
        self.assertEqual(self.status().state, "unlicensed")

    def test_valid_token_licenses_until_offline_window_ends(self) -> None:
        self.status()
        self.now += timedelta(days=40)
        licensing.install_token(self.token(days=30))
        status = self.status()
        self.assertEqual((status.state, status.subscription_id), ("licensed", "I-TEST123"))
        self.assertEqual(licensing.get_watermark_text(), "")
        self.now += timedelta(days=30, minutes=1)
        self.assertEqual(self.status().state, "unlicensed")

    def test_lapsed_license_does_not_start_a_new_trial(self) -> None:
        licensing.install_token(self.token(days=30))
        self.now += timedelta(days=31)
        self.assertEqual(self.status().state, "unlicensed")

    def test_rejects_tampered_foreign_and_expired_codes(self) -> None:
        token = self.token()
        prefix, payload, signature = token.split(".")
        forged = json.loads(licensing._b64decode(payload))
        forged["valid_until"] = (self.now + timedelta(days=3650)).isoformat()
        tampered = ".".join([prefix, licensing._b64encode(json.dumps(forged).encode()), signature])
        other_key = licensing.sign_token(json.loads(licensing._b64decode(payload)), Ed25519PrivateKey.generate())
        for bad in (tampered, other_key, self.token(machine="b" * 32), self.token(days=-1), "I-NOT-A-CODE-" + "x"):
            with self.subTest(bad=bad[:20]):
                with self.assertRaises(licensing.LicenseError):
                    licensing.install_token(bad)

    def test_manual_code_activates_offline(self) -> None:
        code = licensing.issue_manual_token(MACHINE, "Grace Church", days=31, private_key=self.key)
        with patch.object(licensing, "LICENSE_SERVER_URL", ""):
            status = licensing.activate_subscription(code)
        self.assertEqual(status.state, "licensed")
        self.assertTrue(status.subscription_id.startswith("MANUAL-GRACECHURCH"))

    def test_subscription_id_without_server_gives_clear_error(self) -> None:
        with patch.object(licensing, "LICENSE_SERVER_URL", ""):
            with self.assertRaisesRegex(licensing.LicenseError, "not available yet"):
                licensing.activate_subscription("I-ABC")

    def test_source_runs_are_not_enforced(self) -> None:
        with patch.dict(os.environ, {"PEWVISTA_ENFORCE_LICENSE": ""}), patch.object(licensing.sys, "frozen", False, create=True):
            self.assertEqual(self.status().state, "developer")
            self.assertEqual(licensing.get_watermark_text(), "")

    def test_server_activation_refresh_and_revocation(self) -> None:
        test = self
        replies: dict[str, object] = {}
        requests: list[tuple[str, dict]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body))
                reply = replies.get(self.path) or {"token": test.token(subscription=body["subscription_id"])}
                data = json.dumps(reply).encode()
                self.send_response(403 if "error" in reply else 200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.object(licensing, "LICENSE_SERVER_URL", f"http://127.0.0.1:{server.server_address[1]}"):
            status = licensing.activate_subscription(" i-abc123 ")
            self.assertEqual((status.state, status.subscription_id), ("licensed", "I-ABC123"))
            self.assertEqual(requests[0][0], "/activate")
            self.assertEqual(requests[0][1]["machine_id"], MACHINE)
            self.assertFalse(licensing.refresh_license())
            self.now += timedelta(days=2)
            self.assertTrue(licensing.refresh_license())
            self.assertEqual(requests[-1][0], "/refresh")
            replies["/refresh"] = {"error": "The subscription is cancelled.", "revoked": True}
            self.now += timedelta(days=2)
            self.assertFalse(licensing.refresh_license())
            self.assertNotIn("token", json.loads((self.data / licensing.LICENSE_FILE_NAME).read_text()))
            self.now += timedelta(days=12)
            self.assertEqual(self.status().state, "unlicensed")
            replies["/activate"] = {"error": "This subscription is already used on 2 PCs."}
            with self.assertRaisesRegex(licensing.LicenseError, "2 PCs"):
                licensing.activate_subscription("I-ABC123")


class SecondaryScreenMarkTests(unittest.TestCase):
    def test_state_and_page_carry_license_mark(self) -> None:
        import obs_secondary_display as obs
        self.assertIn('id="licenseMark"', obs.SECONDARY_DISPLAY_HTML)
        self.assertIn("state.license_mark", obs.SECONDARY_DISPLAY_HTML)
        server = obs.SecondaryStateServer()
        self.addCleanup(server.shutdown)
        for mark in ("NOT LICENSED", ""):
            with patch.object(obs, "get_watermark_text", return_value=mark):
                self.assertEqual(server.get_state()["license_mark"], mark)


if __name__ == "__main__":
    unittest.main()
