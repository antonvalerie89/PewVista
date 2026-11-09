"""Thread-safe, size-limited timing and error logs for diagnosing the app."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import threading
import time
import traceback as traceback_module

from app_paths import get_app_base_path, get_app_data_path


TIMING_LOG_RELATIVE_PATH = Path("Data") / "ui_timing.log"
ERROR_LOG_RELATIVE_PATH = Path("Data") / "ui_errors.log"
MAX_LOG_BYTES = 512 * 1024
MAX_LOG_BACKUPS = 3
_TIMING_LOG_LOCK = threading.Lock()
_ERROR_LOG_LOCK = threading.Lock()


def _get_timing_log_path() -> Path:
    """What: Resolve the timing-log path beneath the application root.

    Why included: Source and packaged runs need logs beside their app data.
    Where used: log_timing() when appending a timing record.
    Returns: Absolute Path to Data/ui_timing.log.
    """
    return get_app_data_path() / TIMING_LOG_RELATIVE_PATH.name


def _get_error_log_path() -> Path:
    """What: Resolve the error-log path beneath the application root.

    Why included: Error records must use the same portable data location in source
    and frozen deployments.
    Where used: log_error() and log_exception().
    Returns: Absolute Path to Data/ui_errors.log.
    """
    return get_app_data_path() / ERROR_LOG_RELATIVE_PATH.name


def _rotate_log_if_needed(path: Path, incoming_size: int) -> None:
    """What: Move oversized log files through a bounded numbered backup chain.

    Why included: Diagnostic output must not grow without limit on long-running
    or repeatedly failing installations.
    Where used: _append_log_line() before writing each record.
    Returns: None; filesystem errors are tolerated so logging cannot crash the app.
    """
    try:
        if path.exists() and path.stat().st_size + incoming_size <= MAX_LOG_BYTES:
            return
    except OSError:
        return
    for backup_index in range(MAX_LOG_BACKUPS, 0, -1):
        backup_path = path.with_name(f"{path.name}.{backup_index}")
        previous_path = path if backup_index == 1 else path.with_name(f"{path.name}.{backup_index - 1}")
        try:
            if backup_path.exists():
                backup_path.unlink()
        except OSError:
            pass
        try:
            if previous_path.exists():
                previous_path.replace(backup_path)
        except OSError:
            continue


def _append_log_line(path: Path, lock: threading.Lock, line: str) -> None:
    """What: Create the log directory, serialize a write, rotate, and append a line.

    Why included: Multiple worker threads can log concurrently, and each log has a
    fixed size limit with backups.
    Where used: log_timing(), log_error(), and log_exception().
    Returns: None; ignores filesystem errors to keep diagnostics non-fatal.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock:
        try:
            _rotate_log_if_needed(path, len(line.encode("utf-8", errors="ignore")))
            with path.open("a", encoding="utf-8") as log_file:
                log_file.write(line)
        except OSError:
            return


def log_timing(label: str, duration_ms: float, *, threshold_ms: float = 25.0, details: str | None = None) -> None:
    """Record a timestamped timing entry when a measured operation is slow.

    What: Filters durations below threshold_ms and appends qualifying entries with
    an optional details suffix.
    Why included: Slow-path telemetry helps locate UI or I/O stalls without
    producing a log line for every fast operation.
    Where used: Instrumented application startup, rendering, persistence, and workers.
    Returns: None; writes to the bounded timing log when the threshold is met.
    """
    if duration_ms < threshold_ms:
        return
    detail_suffix = f" | {details}" if details else ""
    log_line = f"{datetime.now().isoformat(timespec='seconds')} | {label} | {duration_ms:.2f}ms{detail_suffix}\n"
    _append_log_line(_get_timing_log_path(), _TIMING_LOG_LOCK, log_line)


def log_error(label: str, message: str, *, details: str | None = None) -> None:
    """Record a timestamped concise error message in the application error log.

    What: Formats the label, message, and optional detail string as one line.
    Why included: Recoverable failures need diagnostic context even when no exception
    traceback is available.
    Where used: Application error paths and background-task duplicate-launch handling.
    Returns: None; appends to the bounded error log.
    """
    detail_suffix = f" | {details}" if details else ""
    log_line = f"{datetime.now().isoformat(timespec='seconds')} | {label} | {message}{detail_suffix}\n"
    _append_log_line(_get_error_log_path(), _ERROR_LOG_LOCK, log_line)


def log_exception(label: str, error: BaseException, *, details: str | None = None, traceback_obj=None) -> None:
    """Record an exception summary and its traceback for later diagnosis.

    What: Formats the exception and either the supplied traceback object or the
    exception's current traceback, then appends both to the error log.
    Why included: Unexpected failures need stack context while remaining non-fatal
    to the application's error-reporting path.
    Where used: Central startup, callback, and background-worker exception handlers.
    Returns: None; appends a multi-line record to the bounded error log.
    """
    detail_suffix = f" | {details}" if details else ""
    traceback_text = ""
    if traceback_obj is not None:
        traceback_text = "".join(traceback_module.format_tb(traceback_obj))
    else:
        traceback_text = "".join(traceback_module.format_exception(type(error), error, error.__traceback__))
    log_line = (
        f"{datetime.now().isoformat(timespec='seconds')} | {label} | {type(error).__name__}: {error}{detail_suffix}\n"
        f"{traceback_text}\n"
    )
    _append_log_line(_get_error_log_path(), _ERROR_LOG_LOCK, log_line)


@contextmanager
def timed_block(label: str, *, threshold_ms: float = 25.0, details: str | None = None):
    """Measure a code block and report its duration when it is slow.

    What: Captures a perf_counter timestamp around the yielded context and sends the
    elapsed milliseconds to log_timing(), including when the block raises.
    Why included: Callers can instrument multi-statement operations without manually
    duplicating timer and finally-block logic.
    Where used: Optional context-manager instrumentation for application operations.
    Returns: A context manager; the block's own result or exception passes through.
    """
    start_time = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        log_timing(label, duration_ms, threshold_ms=threshold_ms, details=details)