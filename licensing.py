"""PewVista subscription licensing: 15-day trial, signed per-PC licenses, 30-day offline use.

A license is a token signed by the publisher's Ed25519 key and bound to one PC. Tokens come
from the license server (PayPal subscription ID -> token) or are issued manually by the
publisher. When neither a valid token nor the trial applies, the secondary screen shows a red
"NOT LICENSED" marking; nothing else is blocked, so a church is never stopped mid-service.

License server protocol (JSON over HTTPS, POST to LICENSE_SERVER_URL + path):
    /activate    {"subscription_id", "machine_id", "machine_name", "app_version"} -> {"token"}
    /refresh     same body                                                        -> {"token"}
    /deactivate  {"subscription_id", "machine_id"}                                -> {"ok": true}
    Errors return {"error": "<message>"}; add "revoked": true when the subscription is not paid.
    The server allows MAX_DEVICES machine IDs per subscription and signs tokens that expire
    OFFLINE_GRACE_DAYS after issue.

Publisher commands (run from the source workspace):
    python licensing.py public-key                      Create the key if needed and print it
    python licensing.py issue <machine_id> <name> [--days 31]   Print a manual license code
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app_paths import get_app_data_path

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ImportError:
    InvalidSignature = None
    serialization = None
    Ed25519PrivateKey = None
    Ed25519PublicKey = None


TRIAL_DAYS = 15
OFFLINE_GRACE_DAYS = 30
MAX_DEVICES = 2
PRICE_TEXT = "US$10 per month"
# Filled in once the license server and PayPal subscription plan exist.
LICENSE_SERVER_URL = ""
PAYPAL_SUBSCRIBE_URL = ""
TRUSTED_LICENSE_KEYS: tuple[str, ...] = ("2ef566182c688283b7e4e758f8b7de3e187e5978e564cf893169689fc6a4de90",)
WATERMARK_TEXT = "NOT LICENSED"
TOKEN_PREFIX = "PVL1."
SIGNED_MESSAGE_PREFIX = b"PEWVISTA-LICENSE-V1\n"
LICENSE_FILE_NAME = "license.json"
REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
_REGISTRY_PATH = r"Software\PewVista"
_STATUS_CACHE_SECONDS = 30.0

_status_lock = threading.Lock()
_cached_status: tuple[float, "LicenseStatus"] | None = None


class LicenseError(ValueError):
    """Raised when a license code or server response cannot be used."""


@dataclass(frozen=True)
class LicenseStatus:
    """What: Describe this PC's current license state for the UI and secondary screen.

    Why included: One value drives the watermark, the window title, and the license dialog.
    Where used: get_license_status() callers.
    """
    state: str
    days_left: int = 0
    subscription_id: str = ""
    valid_until: str = ""
    message: str = ""

    @property
    def is_licensed(self) -> bool:
        return self.state in ("licensed", "developer")

    @property
    def shows_watermark(self) -> bool:
        return self.state == "unlicensed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def enforcement_enabled() -> bool:
    """What: Decide whether licensing applies to this run.

    Why included: Source runs on the publisher's PC must not be trial-limited, while tests
    and installed builds must enforce licensing.
    Where used: get_license_status().
    Returns: True for frozen builds or when PEWVISTA_ENFORCE_LICENSE=1.
    """
    return bool(getattr(sys, "frozen", False)) or os.environ.get("PEWVISTA_ENFORCE_LICENSE") == "1"


def get_machine_id() -> str:
    """What: Build a stable, anonymous identifier for this Windows PC.

    Why included: Licenses are limited to MAX_DEVICES PCs without sending hardware details.
    Where used: Activation, token checks, and the license dialog.
    Returns: 32 hex characters derived from the Windows MachineGuid.
    """
    raw = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as key:
            raw = str(winreg.QueryValueEx(key, "MachineGuid")[0])
    except (ImportError, OSError):
        raw = f"{uuid.getnode():012x}-{platform.node()}"
    return hashlib.sha256(("PewVista-machine:" + raw.strip().lower()).encode("utf-8")).hexdigest()[:32]


def get_license_file_path() -> Path:
    return get_app_data_path() / LICENSE_FILE_NAME


def _read_store() -> dict[str, object]:
    try:
        value = json.loads(get_license_file_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_store(store: dict[str, object]) -> None:
    path = get_license_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(store, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_registry_trial_start() -> datetime | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_PATH) as key:
            return _parse_time(winreg.QueryValueEx(key, "TrialStarted")[0])
    except (ImportError, OSError):
        return None


def _write_registry_trial_start(started: datetime) -> None:
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REGISTRY_PATH) as key:
            winreg.SetValueEx(key, "TrialStarted", 0, winreg.REG_SZ, started.isoformat())
    except (ImportError, OSError):
        pass


def _trusted_keys() -> tuple[str, ...]:
    return TRUSTED_LICENSE_KEYS


def verify_token(token: str, machine_id: str | None = None) -> dict[str, object]:
    """What: Check a license token's signature and that it belongs to this PC.

    Why included: The license file sits in an everyone-writable folder; only the
    publisher's signature makes it trustworthy.
    Where used: Status checks, activation, and manual license codes.
    Returns: The token payload; raises LicenseError when invalid. Expiry is not checked here.
    """
    if Ed25519PublicKey is None:
        raise LicenseError("License checking is unavailable because the 'cryptography' package is missing.")
    text = str(token or "").strip()
    if not text.startswith(TOKEN_PREFIX) or text.count(".") != 2:
        raise LicenseError("This is not a PewVista license code.")
    _, payload_text, signature_text = text.split(".")
    try:
        payload_bytes = _b64decode(payload_text)
        signature = _b64decode(signature_text)
    except ValueError as error:
        raise LicenseError("The license code is damaged.") from error
    for key_hex in _trusted_keys():
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex)).verify(signature, SIGNED_MESSAGE_PREFIX + payload_bytes)
            break
        except (InvalidSignature, ValueError):
            continue
    else:
        raise LicenseError("The license code was not issued by PewVista.")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except ValueError as error:
        raise LicenseError("The license code is damaged.") from error
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise LicenseError("This license code needs a newer version of PewVista.")
    if str(payload.get("machine_id") or "") != (machine_id or get_machine_id()):
        raise LicenseError("This license code belongs to a different PC.")
    if _parse_time(payload.get("valid_until")) is None:
        raise LicenseError("The license code has no expiry date.")
    return payload


def sign_token(payload: dict[str, object], private_key) -> str:
    """What: Sign a license payload with the publisher's private key.

    Why included: Used by the publisher command and the license server.
    Where used: issue_manual_token() and tests.
    Returns: A PVL1 license code string.
    """
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = private_key.sign(SIGNED_MESSAGE_PREFIX + payload_bytes)
    return TOKEN_PREFIX + _b64encode(payload_bytes) + "." + _b64encode(signature)


def invalidate_status_cache() -> None:
    global _cached_status
    with _status_lock:
        _cached_status = None


def get_license_status(force: bool = False) -> LicenseStatus:
    """What: Work out whether this PC is licensed, in trial, or unlicensed.

    Why included: Drives the red secondary-screen marking and the license dialog.
    Where used: Secondary screen state, window title, and Help > License and Subscription.
    Returns: A LicenseStatus; cached briefly because the secondary screen asks often.
    """
    global _cached_status
    with _status_lock:
        if not force and _cached_status is not None and time.monotonic() - _cached_status[0] < _STATUS_CACHE_SECONDS:
            return _cached_status[1]
    status = _compute_status()
    with _status_lock:
        _cached_status = (time.monotonic(), status)
    return status


def _compute_status() -> LicenseStatus:
    if not enforcement_enabled():
        return LicenseStatus("developer", message="Source run: licensing is not enforced.")
    store = _read_store()
    now = _now()
    # A clock turned back cannot extend a trial or license beyond the latest time already seen.
    last_seen = _parse_time(store.get("last_seen"))
    effective_now = max(now, last_seen) if last_seen else now
    if not last_seen or now > last_seen + timedelta(hours=1):
        store["last_seen"] = now.isoformat()
        _try_write(store)
    # Record the trial start on first run even when licensed, so a later lapse cannot start a new trial.
    trial_start = _trial_start(store, now)
    token = str(store.get("token") or "")
    if token:
        try:
            payload = verify_token(token)
            valid_until = _parse_time(payload.get("valid_until"))
            if valid_until and effective_now <= valid_until:
                return LicenseStatus("licensed", days_left=max(0, (valid_until - effective_now).days),
                                     subscription_id=str(payload.get("subscription_id") or ""),
                                     valid_until=valid_until.isoformat(), message="Licensed")
        except LicenseError:
            pass
    days_used = max(0, (effective_now - trial_start).days)
    if days_used < TRIAL_DAYS:
        return LicenseStatus("trial", days_left=TRIAL_DAYS - days_used, message=f"Free trial: {TRIAL_DAYS - days_used} day(s) left")
    if token:
        return LicenseStatus("unlicensed", message="The license has expired. Connect to the internet or renew the subscription.")
    return LicenseStatus("unlicensed", message="The free trial has ended.")


def _try_write(store: dict[str, object]) -> None:
    try:
        _write_store(store)
    except OSError:
        pass


def _trial_start(store: dict[str, object], now: datetime) -> datetime:
    """What: Return the earliest recorded trial start, recording one on first run.

    Why included: The trial is kept in both the data folder and the user registry so
    deleting one copy does not restart it.
    Where used: _compute_status().
    Returns: The trial start time.
    """
    candidates = [value for value in (_parse_time(store.get("trial_started")), _read_registry_trial_start()) if value]
    started = min(candidates) if candidates else now
    if _parse_time(store.get("trial_started")) != started:
        store["trial_started"] = started.isoformat()
        _try_write(store)
    if _read_registry_trial_start() != started:
        _write_registry_trial_start(started)
    return started


def install_token(token: str) -> LicenseStatus:
    """What: Verify and save a license code for this PC.

    Why included: Activation results and manual codes are stored the same way.
    Where used: activate_subscription(), refresh_license(), and the license dialog.
    Returns: The new license status; raises LicenseError when the code is invalid or expired.
    """
    payload = verify_token(token)
    valid_until = _parse_time(payload.get("valid_until"))
    if valid_until is None or valid_until < _now():
        raise LicenseError("This license code has already expired.")
    store = _read_store()
    store["token"] = str(token).strip()
    store["subscription_id"] = str(payload.get("subscription_id") or "")
    store["last_refresh"] = _now().isoformat()
    _write_store(store)
    invalidate_status_cache()
    return get_license_status(force=True)


def remove_token() -> None:
    store = _read_store()
    for key in ("token", "subscription_id", "last_refresh"):
        store.pop(key, None)
    _write_store(store)
    invalidate_status_cache()


def _app_version() -> str:
    try:
        from windows_update import get_release_version
        return str(get_release_version())
    except Exception:
        return ""


def _post(path: str, body: dict[str, object], timeout: float = 15.0) -> dict[str, object]:
    """What: Send one JSON request to the license server.

    Why included: Activation, refresh, and deactivation share error handling.
    Where used: activate_subscription(), refresh_license(), deactivate_this_pc().
    Returns: The decoded JSON reply; raises LicenseError with a readable message.
    """
    if not LICENSE_SERVER_URL:
        raise LicenseError("Online activation is not available yet. Ask PewVista for a license code.")
    request = urllib.request.Request(LICENSE_SERVER_URL.rstrip("/") + path, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": "PewVista"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            reply = json.loads(error.read().decode("utf-8"))
        except ValueError:
            reply = {"error": f"The license server returned an error ({error.code})."}
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise LicenseError("Could not reach the license server. Check the internet connection.") from error
    if not isinstance(reply, dict):
        raise LicenseError("The license server sent an unexpected reply.")
    if reply.get("error"):
        error = LicenseError(str(reply["error"]))
        error.revoked = bool(reply.get("revoked"))
        raise error
    return reply


def _request_body(subscription_id: str) -> dict[str, object]:
    return {"subscription_id": subscription_id, "machine_id": get_machine_id(),
            "machine_name": platform.node()[:64], "app_version": _app_version()}


def activate_subscription(subscription_id: str) -> LicenseStatus:
    """What: Activate this PC with a PayPal subscription ID (or paste of a license code).

    Why included: Churches type what PayPal emailed them; manual codes also work offline.
    Where used: The license dialog's Activate button.
    Returns: The new status; raises LicenseError with a readable reason.
    """
    value = str(subscription_id or "").strip()
    if value.startswith(TOKEN_PREFIX):
        return install_token(value)
    if not value:
        raise LicenseError("Enter the PayPal subscription ID or license code.")
    reply = _post("/activate", _request_body(value.upper()))
    return install_token(str(reply.get("token") or ""))


def refresh_license() -> bool:
    """What: Renew the token while online so the 30-day offline window keeps moving.

    Why included: Cancelled subscriptions stop renewing; paid ones never see the marking.
    Where used: start_background_refresh() once a day.
    Returns: True when a new token was saved; network failures are silent.
    """
    store = _read_store()
    subscription_id = str(store.get("subscription_id") or "")
    if not subscription_id or not LICENSE_SERVER_URL or not enforcement_enabled():
        return False
    last_refresh = _parse_time(store.get("last_refresh"))
    if last_refresh and _now() - last_refresh < timedelta(seconds=REFRESH_INTERVAL_SECONDS):
        return False
    try:
        reply = _post("/refresh", _request_body(subscription_id))
        install_token(str(reply.get("token") or ""))
        return True
    except LicenseError as error:
        if getattr(error, "revoked", False):
            remove_token()
        return False


def deactivate_this_pc() -> None:
    """What: Release this PC's license slot so another PC can be activated.

    Why included: Each subscription covers MAX_DEVICES PCs; churches replace computers.
    Where used: The license dialog's Remove button.
    Returns: None; the local license is removed even if the server cannot be reached.
    """
    subscription_id = str(_read_store().get("subscription_id") or "")
    try:
        if subscription_id and LICENSE_SERVER_URL:
            _post("/deactivate", {"subscription_id": subscription_id, "machine_id": get_machine_id()})
    finally:
        remove_token()


def start_background_refresh(on_change=None) -> threading.Thread:
    """What: Refresh the license in the background now and every few hours.

    Why included: Network calls must never block the UI or a live service.
    Where used: main.py at startup.
    Returns: The daemon thread.
    """
    def run() -> None:
        while True:
            try:
                if refresh_license() and on_change is not None:
                    on_change()
            except Exception:
                pass
            time.sleep(6 * 60 * 60)

    thread = threading.Thread(target=run, name="PewVistaLicenseRefresh", daemon=True)
    thread.start()
    return thread


def get_watermark_text() -> str:
    """What: Return the secondary-screen marking for the current license state.

    Why included: Unlicensed copies keep working but show a visible red notice.
    Where used: obs_secondary_display state for the live screen.
    Returns: WATERMARK_TEXT when unlicensed, otherwise an empty string.
    """
    try:
        return WATERMARK_TEXT if get_license_status().shows_watermark else ""
    except Exception:
        return ""


def default_signing_key_path() -> Path:
    configured = os.environ.get("PEWVISTA_LICENSE_KEY", "").strip()
    return Path(configured) if configured else Path.home() / "PewVista Signing" / "license_signing_key.pem"


def load_or_create_signing_key(key_path: Path | None = None):
    """What: Load the publisher's license key, creating it on first use.

    Why included: The private key signs licenses and must never ship with the app.
    Where used: Publisher commands and the future license server.
    Returns: An Ed25519 private key.
    """
    if Ed25519PrivateKey is None:
        raise LicenseError("The 'cryptography' package is required.")
    path = Path(key_path) if key_path is not None else default_signing_key_path()
    if path.is_file():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise LicenseError(f"The license key at {path} is not an Ed25519 key.")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return key


def public_key_hex(private_key) -> str:
    return private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def issue_manual_token(machine_id: str, customer: str, days: int = 31, private_key=None) -> str:
    """What: Create a license code for one PC without the license server.

    Why included: Lets the publisher license churches that pay another way or before the
    server is live.
    Where used: The `issue` publisher command and tests.
    Returns: A PVL1 license code.
    """
    key = private_key if private_key is not None else load_or_create_signing_key()
    issued = _now().replace(microsecond=0)
    payload = {"v": 1, "subscription_id": "MANUAL-" + "".join(ch for ch in customer.upper() if ch.isalnum())[:24],
               "machine_id": machine_id.strip().lower(), "customer": customer.strip()[:80],
               "issued_at": issued.isoformat(), "valid_until": (issued + timedelta(days=int(days))).isoformat()}
    return sign_token(payload, key)


def _main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="PewVista license publisher tools")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("public-key", help="Create the license key if needed and print its public key")
    issue = commands.add_parser("issue", help="Print a manual license code for one PC")
    issue.add_argument("machine_id")
    issue.add_argument("customer")
    issue.add_argument("--days", type=int, default=31)
    args = parser.parse_args(argv)
    if args.command == "public-key":
        print(public_key_hex(load_or_create_signing_key()))
    else:
        print(issue_manual_token(args.machine_id, args.customer, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
