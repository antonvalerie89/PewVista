"""Sign PewVista update packages and verify them before installation.

The signature is stored in the ZIP archive comment, so an update stays a single file and
older PewVista versions (which ignore the comment) can still install it. The private key
stays on the publisher's computer; installed apps only carry the public key.

Publisher commands (run from the source workspace):
    python update_signing.py public-key      Create the key if needed and print its public key
    python update_signing.py sign <zip>      Sign an update ZIP in place
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ImportError:
    InvalidSignature = None
    serialization = None
    Ed25519PrivateKey = None
    Ed25519PublicKey = None


TRUSTED_PUBLIC_KEYS: tuple[str, ...] = ("0993ccc8fb236908dd940c50456e37a1d58b503ec3a27562876d6e7faaa048d0",)
SIGNATURE_PREFIX = b"PEWVISTA-SIG-V1:"
SIGNED_MESSAGE_PREFIX = b"PEWVISTA-UPDATE-SIGNATURE-V1\n"
_END_RECORD_SIGNATURE = b"PK\x05\x06"
_END_RECORD_SIZE = 22


class UpdateSignatureError(ValueError):
    """Raised when an update package is unsigned, tampered with, or signed by someone else."""


def default_signing_key_path() -> Path:
    """What: Locate the publisher's private signing key outside the source workspace.

    Why included: The private key must never be committed, packaged, or shipped.
    Where used: Signing commands; PEWVISTA_SIGNING_KEY overrides the location.
    Returns: Path to the PEM private key file.
    """
    configured = os.environ.get("PEWVISTA_SIGNING_KEY", "").strip()
    if configured:
        return Path(configured)
    # Not under AppData: Microsoft Store Python silently redirects AppData writes into a
    # hidden per-app folder, which would hide the key from backups.
    return Path.home() / "PewVista Signing" / "update_signing_key.pem"


def _require_crypto() -> None:
    """What: Fail clearly when the cryptography package is missing from the build.

    Why included: Silently skipping verification would defeat update signing.
    Where used: Signing and verification entry points.
    Returns: None; raises UpdateSignatureError when unavailable.
    """
    if Ed25519PrivateKey is None:
        raise UpdateSignatureError("Update signature checking is unavailable because the 'cryptography' package is missing.")


def _locate_end_record(path: Path) -> tuple[int, int]:
    """What: Find the ZIP end-of-central-directory record and its comment length.

    Why included: The signature lives in the comment that follows this record.
    Where used: Digest calculation, signing, and verification.
    Returns: Absolute offset of the record and the current comment length.
    """
    size = path.stat().st_size
    tail_length = min(size, _END_RECORD_SIZE + 65535)
    with path.open("rb") as archive_file:
        archive_file.seek(size - tail_length)
        tail = archive_file.read()
    for offset in range(len(tail) - _END_RECORD_SIZE, -1, -1):
        if tail[offset:offset + 4] != _END_RECORD_SIGNATURE:
            continue
        comment_length = int.from_bytes(tail[offset + 20:offset + 22], "little")
        if offset + _END_RECORD_SIZE + comment_length == len(tail):
            return size - tail_length + offset, comment_length
    raise UpdateSignatureError("The update file is not a valid ZIP archive.")


def _unsigned_digest(path: Path, end_offset: int) -> bytes:
    """What: Hash the archive exactly as it was before the signature comment was added.

    Why included: The signature must cover every byte of the package except itself.
    Where used: Signing and verification.
    Returns: SHA-256 digest bytes.
    """
    digest = hashlib.sha256()
    remaining = end_offset + _END_RECORD_SIZE - 2
    with path.open("rb") as archive_file:
        while remaining > 0:
            chunk = archive_file.read(min(remaining, 4 * 1024 * 1024))
            if not chunk:
                raise UpdateSignatureError("The update file ended unexpectedly.")
            digest.update(chunk)
            remaining -= len(chunk)
    digest.update(b"\x00\x00")
    return digest.digest()


def load_or_create_signing_key(key_path: Path | None = None):
    """What: Load the publisher's private key, creating it on first use.

    Why included: The first signed build must create the key that installed apps trust.
    Where used: sign_update_archive() and the public-key command.
    Returns: An Ed25519 private key object.
    """
    _require_crypto()
    path = Path(key_path) if key_path is not None else default_signing_key_path()
    if path.is_file():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise UpdateSignatureError(f"The signing key at {path} is not an Ed25519 key.")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return key


def public_key_hex(private_key) -> str:
    """What: Express a private key's public half as hex for embedding in the app.

    Why included: TRUSTED_PUBLIC_KEYS stores public keys as plain hex strings.
    Where used: The public-key command and tests.
    Returns: 64-character hex string.
    """
    return private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def sign_update_archive(archive_path: Path, key_path: Path | None = None) -> None:
    """What: Sign an update ZIP in place, replacing any previous signature.

    Why included: Installed apps refuse update packages that are not signed by the publisher.
    Where used: create_windows_zip.ps1 after building an Update package.
    Returns: None; rewrites only the archive comment.
    """
    path = Path(archive_path)
    key = load_or_create_signing_key(key_path)
    end_offset, comment_length = _locate_end_record(path)
    if comment_length:
        with path.open("r+b") as archive_file:
            archive_file.seek(end_offset + 20)
            archive_file.write(b"\x00\x00")
            archive_file.truncate(end_offset + _END_RECORD_SIZE)
    signature = key.sign(SIGNED_MESSAGE_PREFIX + _unsigned_digest(path, end_offset))
    comment = SIGNATURE_PREFIX + base64.b64encode(signature)
    with path.open("r+b") as archive_file:
        archive_file.seek(end_offset + 20)
        archive_file.write(len(comment).to_bytes(2, "little"))
        archive_file.write(comment)
        archive_file.truncate()


def verify_update_archive(archive_path: Path) -> None:
    """What: Confirm an update ZIP is signed by a trusted publisher key and unmodified.

    Why included: Update packages are installed with administrator rights.
    Where used: windows_update.prepare_update_archive() before anything is extracted.
    Returns: None; raises UpdateSignatureError for unsigned, altered, or untrusted packages.
    """
    _require_crypto()
    path = Path(archive_path)
    end_offset, comment_length = _locate_end_record(path)
    with path.open("rb") as archive_file:
        archive_file.seek(end_offset + _END_RECORD_SIZE)
        comment = archive_file.read(comment_length)
    if not comment.startswith(SIGNATURE_PREFIX):
        raise UpdateSignatureError("This update is not signed by the PewVista publisher. It was not installed.")
    try:
        signature = base64.b64decode(comment[len(SIGNATURE_PREFIX):], validate=True)
    except ValueError as error:
        raise UpdateSignatureError("This update's signature is damaged. It was not installed.") from error
    message = SIGNED_MESSAGE_PREFIX + _unsigned_digest(path, end_offset)
    for trusted_key in TRUSTED_PUBLIC_KEYS:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(trusted_key)).verify(signature, message)
            return
        except (InvalidSignature, ValueError):
            continue
    raise UpdateSignatureError("This update was changed after it was signed, or it was not signed by the PewVista publisher. It was not installed.")


def main(arguments: list[str]) -> int:
    """What: Command-line entry point for publisher signing tasks.

    Why included: The PowerShell packaging scripts call this to sign Update ZIPs.
    Where used: create_windows_zip.ps1 and the publisher's one-time key setup.
    Returns: Process exit code.
    """
    if arguments[:1] == ["public-key"]:
        print(public_key_hex(load_or_create_signing_key()))
        return 0
    if len(arguments) == 2 and arguments[0] == "sign":
        sign_update_archive(Path(arguments[1]))
        print(f"Signed update package: {arguments[1]}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
