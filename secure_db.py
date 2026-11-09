"""Open the members database encrypted at rest with SQLCipher.

members.db holds members' contact details and birth dates. On each computer it is
encrypted with a random key that Windows protects for that computer (DPAPI machine
scope), so copies of the file or its daily backups cannot be read elsewhere. Member
exports for another computer are re-encrypted with a password the operator chooses.

Other databases are opened unchanged through the same engine, so connections, rows,
errors, and the backup API stay compatible across every database file.
"""

from __future__ import annotations

import contextlib
import contextvars
import ctypes
import os
import secrets
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

try:
    import sqlcipher3.dbapi2 as _engine
    ENCRYPTION_AVAILABLE = True
    try:
        # Trying the computer key before a transfer password is expected to fail sometimes;
        # keep SQLCipher from printing those normal misses as errors.
        _quiet = _engine.connect(":memory:")
        _quiet.execute("PRAGMA cipher_log_level = NONE")
        _quiet.close()
    except Exception:
        pass
except ImportError:
    import sqlite3 as _engine
    ENCRYPTION_AVAILABLE = False

from sqlite3 import PARSE_COLNAMES, PARSE_DECLTYPES  # noqa: F401  (constants match both engines)

Connection = _engine.Connection
Cursor = _engine.Cursor
Row = _engine.Row
Error = _engine.Error
DatabaseError = _engine.DatabaseError
IntegrityError = _engine.IntegrityError
OperationalError = _engine.OperationalError

MEMBERS_FILENAME = "members.db"
KEY_FILENAME = "members.key"
_PLAINTEXT_HEADER = b"SQLite format 3\x00"
_MIGRATION_LOCK = threading.Lock()
_ACTIVE_DATA_DIRECTORY: contextvars.ContextVar[Path | None] = contextvars.ContextVar("pewvista_data_directory", default=None)
_TRANSFER_PASSWORD: contextvars.ContextVar[str | None] = contextvars.ContextVar("pewvista_members_password", default=None)


class MembersLockedError(_engine.DatabaseError):
    """Raised when an encrypted members database cannot be unlocked."""


@contextlib.contextmanager
def data_directory(path: Path):
    """What: Set which data folder's key protects members files outside that folder.

    Why included: Transfer staging and backup copies live in temporary folders.
    Where used: Database export, import, and daily backup functions.
    Returns: Context manager; restores the previous folder on exit.
    """
    token = _ACTIVE_DATA_DIRECTORY.set(Path(path))
    try:
        yield
    finally:
        _ACTIVE_DATA_DIRECTORY.reset(token)


@contextlib.contextmanager
def transfer_password(password: str | None):
    """What: Make the operator's transfer password available while opening members files.

    Why included: Exported member databases are protected by a password instead of a
    computer-bound key so they can be opened on the receiving computer.
    Where used: Database export and import.
    Returns: Context manager; clears the password on exit.
    """
    token = _TRANSFER_PASSWORD.set(password or None)
    try:
        yield
    finally:
        _TRANSFER_PASSWORD.reset(token)


def current_transfer_password() -> str | None:
    """What: Return the transfer password active for this export or import.

    Why included: Export re-keys the member snapshot with exactly that password.
    Where used: data_transfer export staging.
    Returns: The password, or None outside a transfer.
    """
    return _TRANSFER_PASSWORD.get()


def _dpapi(data: bytes, protect: bool) -> bytes:
    """What: Protect or unprotect bytes with Windows DPAPI in computer scope.

    Why included: The database key must be unusable if the key file is copied elsewhere.
    Where used: Machine key file creation and loading.
    Returns: Protected or recovered bytes; plain passthrough on non-Windows test hosts.
    """
    if sys.platform != "win32":
        return data
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        """What: Mirror the Windows DATA_BLOB structure used by DPAPI.

        Why included: CryptProtectData and CryptUnprotectData exchange data through this structure.
        Where used: _dpapi().
        Returns: Instances hold a byte count and a pointer to the bytes.
        """
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = DataBlob()
    ui_forbidden, local_machine = 0x1, 0x4
    crypt32 = ctypes.windll.crypt32
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, ui_forbidden | local_machine, ctypes.byref(result))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, ui_forbidden, ctypes.byref(result))
    if not ok:
        raise OSError(ctypes.GetLastError(), "Windows could not protect the members database key.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def machine_key(directory: Path) -> str:
    """What: Load this computer's members key for a data folder, creating it once.

    Why included: Every members file in that folder shares one computer-bound key.
    Where used: Opening, creating, and migrating members databases.
    Returns: 64-character hex key.
    """
    key_path = Path(directory) / KEY_FILENAME
    if not key_path.is_file():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with key_path.open("xb") as key_file:
                key_file.write(_dpapi(secrets.token_bytes(32), protect=True))
        except FileExistsError:
            pass
    raw = _dpapi(key_path.read_bytes(), protect=False)
    if len(raw) != 32:
        raise MembersLockedError("The members database key file is damaged.")
    return raw.hex()


def _key_directories(path: Path) -> list[Path]:
    """What: List the data folders whose computer key may apply to a members file.

    Why included: The working file uses its own folder's key, while temporary copies
    (transfer staging, backups) use the active data folder's key.
    Where used: connect(), re-keying, and import staging.
    Returns: Folders in priority order; the first one keys newly created files.
    """
    directories: list[Path] = []
    if (path.parent / KEY_FILENAME).is_file():
        directories.append(path.parent)
    active = _ACTIVE_DATA_DIRECTORY.get()
    if active is not None and all(Path(active).resolve() != existing.resolve() for existing in directories):
        directories.append(Path(active))
    return directories or [path.parent]


def _candidate_pragmas(path: Path) -> list[str]:
    """What: Build every key statement that may unlock a members file, in priority order.

    Why included: One place decides which computer keys and transfer password to try.
    Where used: connect() and _source_pragma().
    Returns: Key statements; the first one keys newly created files.
    """
    candidates = [_key_pragma(machine_key(directory), raw=True) for directory in _key_directories(path)]
    password = _TRANSFER_PASSWORD.get()
    if password:
        candidates.append(_key_pragma(password, raw=False))
    return candidates


def _key_pragma(key: str, raw: bool) -> str:
    """What: Build the SQLCipher key statement for a raw hex key or a password.

    Why included: PRAGMA key cannot be parameterized, so values are escaped here.
    Where used: connect(), rekey helpers, and migration.
    Returns: SQL statement text.
    """
    if raw:
        return f"PRAGMA key = \"x'{key}'\""
    return "PRAGMA key = '" + key.replace("'", "''") + "'"


def is_members_path(path: object) -> bool:
    """What: Report whether a path names a members database file.

    Why included: Only member data is encrypted; other databases open unchanged.
    Where used: connect().
    Returns: True for members.db files.
    """
    return Path(str(path)).name.casefold() == MEMBERS_FILENAME


def is_plaintext(path: Path) -> bool:
    """What: Detect an unencrypted SQLite file by its standard header.

    Why included: Older installations and transfer archives contain plaintext members data.
    Where used: connect(), migration, and import staging.
    Returns: True when the file exists and starts with the SQLite header.
    """
    try:
        with Path(path).open("rb") as database_file:
            return database_file.read(16) == _PLAINTEXT_HEADER
    except OSError:
        return False


def _open(database: str, uri: bool, timeout: float, pragma: str | None, **kwargs):
    """What: Open one connection, apply a key, and prove it can read the schema.

    Why included: A wrong key only fails on first read; the connection must be discarded then.
    Where used: connect().
    Returns: Ready connection; raises DatabaseError when the key does not fit.
    """
    connection = _engine.connect(database, uri=uri, timeout=timeout, **kwargs)
    try:
        if pragma:
            connection.execute(pragma)
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except Exception:
        connection.close()
        raise


def connect(database, uri: bool = False, timeout: float = 5.0, **kwargs):
    """What: Open any app database, unlocking members files transparently.

    Why included: Callers keep their normal SQLite code while member data stays encrypted.
    Where used: members.py, data_transfer.py, and the daily backup.
    Returns: An open connection of this module's engine.
    """
    database_text = str(database)
    path = Path(database_text)
    if uri and database_text.startswith("file:"):
        from urllib.parse import unquote, urlparse
        parsed_path = unquote(urlparse(database_text).path)
        path = Path(parsed_path[1:] if len(parsed_path) > 2 and parsed_path[2] == ":" else parsed_path)
    if not ENCRYPTION_AVAILABLE or not is_members_path(path):
        return _engine.connect(database, uri=uri, timeout=timeout, **kwargs)
    if path.is_file() and path.stat().st_size > 0 and is_plaintext(path):
        return _engine.connect(database, uri=uri, timeout=timeout, **kwargs)
    candidates = _candidate_pragmas(path)
    if not path.is_file() or path.stat().st_size == 0:
        return _open(database, uri, timeout, candidates[0], **kwargs)
    for pragma in candidates:
        try:
            return _open(database, uri, timeout, pragma, **kwargs)
        except _engine.DatabaseError:
            continue
    raise MembersLockedError(
        "The members data could not be unlocked. If it came from another computer, "
        "enter the password that was chosen when it was exported."
    )


def _reencrypt(source: Path, destination: Path, source_pragma: str | None, destination_pragma: str) -> None:
    """What: Copy a members database into a new file under a different key.

    Why included: SQLCipher's backup API cannot copy between differently keyed files.
    Where used: Migration, export re-keying, and import staging.
    Returns: None; preserves schema, rows, and user_version.
    """
    if destination.exists():
        destination.unlink()
    connection = _engine.connect(str(source))
    try:
        if source_pragma:
            connection.execute(source_pragma)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        attach_key = destination_pragma.split("=", 1)[1].strip()
        connection.execute(f"ATTACH DATABASE ? AS converted KEY {attach_key}", (str(destination),))
        connection.execute("SELECT sqlcipher_export('converted')")
        connection.execute(f"PRAGMA converted.user_version = {version}")
        connection.commit()
        connection.execute("DETACH DATABASE converted")
    finally:
        connection.close()


def _replace_file(source: Path, target: Path) -> None:
    """What: Atomically swap a converted database into place and drop stale sidecars.

    Why included: A half-written members file must never replace the original.
    Where used: Migration and re-keying helpers.
    Returns: None.
    """
    os.replace(source, target)
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(target) + suffix).unlink(missing_ok=True)


def _source_pragma(path: Path) -> str | None:
    """What: Find the key statement that currently unlocks a members file.

    Why included: Conversions must read the file under whichever key it uses now.
    Where used: rekey_to_password() and adopt_into_machine_key().
    Returns: Key statement, or None for plaintext files.
    """
    if is_plaintext(path):
        return None
    for pragma in _candidate_pragmas(path):
        try:
            _open(str(path), False, 5.0, pragma).close()
            return pragma
        except _engine.DatabaseError:
            continue
    raise MembersLockedError("The members data could not be unlocked.")


def rekey_to_password(path: Path, password: str) -> None:
    """What: Re-encrypt a members file with a transfer password.

    Why included: Exports must open on the receiving computer, not only this one.
    Where used: Database export after the snapshot is written.
    Returns: None; replaces the file in place.
    """
    path = Path(path)
    converted = path.with_name(path.name + ".rekey")
    _reencrypt(path, converted, _source_pragma(path), _key_pragma(password, raw=False))
    _replace_file(converted, path)


def adopt_into_machine_key(path: Path) -> None:
    """What: Re-encrypt an imported members file with this computer's key.

    Why included: After staging, every members copy uses one key so merges and
    replacements can work with the normal SQLite backup API.
    Where used: Database import staging, for plaintext or password-protected files.
    Returns: None; replaces the file in place.
    """
    path = Path(path)
    source_pragma = _source_pragma(path)
    target_pragma = _key_pragma(machine_key(Path(_ACTIVE_DATA_DIRECTORY.get() or path.parent)), raw=True)
    if source_pragma == target_pragma:
        return
    converted = path.with_name(path.name + ".rekey")
    _reencrypt(path, converted, source_pragma, target_pragma)
    _replace_file(converted, path)


def ensure_encrypted(path: Path) -> bool:
    """What: Encrypt a plaintext working members database and its older backups.

    Why included: Existing installations must gain encryption without losing data.
    Where used: members.py before every access, and transfer/backup entry points.
    Returns: True when a migration happened.
    """
    path = Path(path)
    if not ENCRYPTION_AVAILABLE or not path.is_file() or not is_plaintext(path):
        return False
    with _MIGRATION_LOCK:
        if not is_plaintext(path):
            return False
        checkpoint = _engine.connect(str(path))
        try:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            checkpoint.close()
        key_pragma = _key_pragma(machine_key(path.parent), raw=True)
        converted = path.with_name(path.name + ".encrypting")
        _reencrypt(path, converted, None, key_pragma)
        _replace_file(converted, path)
        _reencrypt_backups(path.parent, key_pragma)
    return True


def _reencrypt_backups(directory: Path, key_pragma: str) -> None:
    """What: Replace plaintext members copies inside earlier daily backup ZIPs.

    Why included: Backups made before encryption would otherwise still expose member data.
    Where used: ensure_encrypted() after a successful migration.
    Returns: None; failures leave that backup unchanged.
    """
    for backup in sorted((Path(directory) / ".backups").glob("backup-*.zip")):
        try:
            with zipfile.ZipFile(backup) as archive:
                if MEMBERS_FILENAME not in archive.namelist():
                    continue
                contents = {name: archive.read(name) for name in archive.namelist()}
            if contents[MEMBERS_FILENAME][:16] != _PLAINTEXT_HEADER:
                continue
            staging = Path(tempfile.mkdtemp(prefix="pewvista-reencrypt-"))
            try:
                plain = staging / "plain.db"
                plain.write_bytes(contents[MEMBERS_FILENAME])
                encrypted = staging / "encrypted.db"
                _reencrypt(plain, encrypted, None, key_pragma)
                contents[MEMBERS_FILENAME] = encrypted.read_bytes()
                rewritten = backup.with_suffix(".rewrite")
                with zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, data in contents.items():
                        archive.writestr(name, data)
                os.replace(rewritten, backup)
            finally:
                for leftover in staging.iterdir():
                    leftover.unlink(missing_ok=True)
                staging.rmdir()
        except Exception:
            continue
