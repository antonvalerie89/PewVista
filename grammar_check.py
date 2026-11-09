"""Private English grammar checks using a bundled local LanguageTool server."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app_paths import get_app_base_path


MINISTRY_AGREEMENT = re.compile(r"(?:^|(?<=[.!?\n]))[ \t]*(?:[\"']?)(Jesus)[ \t]+(?P<verb>are)\b", re.IGNORECASE)


@dataclass(frozen=True)
class GrammarIssue:
    """What: Describe a sentence-level issue and its suggested replacements.

    Why included: Tk receives plain offsets/results rather than engine-specific objects.
    Where used: Grammar highlighting and right-click correction menus.
    """

    start: int
    end: int
    message: str
    replacements: tuple[str, ...]
    rule_id: str


def _character_offset(text: str, utf16_offset: int) -> int:
    """What: Convert Java UTF-16 offsets to Python character offsets.

    Why included: Supplementary Unicode characters occupy two Java code units.
    Where used: Converting LanguageTool issue spans before returning them to Tk.
    Returns: Python character offset.
    """
    return len(text.encode("utf-16-le")[:utf16_offset * 2].decode("utf-16-le", errors="ignore"))


class GrammarEngine:
    """What: Manage a private local English LanguageTool process and parse its checks.

    Why included: Spelling dictionaries cannot diagnose subject-verb or sentence-level errors.
    Where used: SpellCheckController's separate grammar worker.
    """

    def __init__(self, resource_root: Path | None = None) -> None:
        """What: Initialize lazy server state without launching Java on the Tk thread.

        Why included: The grammar runtime is started only when a field is checked.
        Where used: SpellCheckController initialization and focused engine tests.
        Returns: None.
        """
        self.directory = resource_root or get_app_base_path() / "Resourses" / "grammar"
        self.process: subprocess.Popen | None = None
        self.port = 0
        self.lock = threading.Lock()
        self.closed = threading.Event()

    def _ensure_server(self) -> None:
        """What: Launch bundled Java on a private loopback port and wait for LanguageTool.

        Why included: Checks must remain offline and not depend on an installed system Java.
        Where used: check() on the grammar worker.
        Returns: None; raises a useful setup/startup error when unavailable.
        """
        if self.closed.is_set():
            raise RuntimeError("Grammar checker is closed.")
        if self.process is not None and self.process.poll() is None:
            return
        java = self.directory / "java" / "bin" / "java.exe"
        server = self.directory / "LanguageTool" / "languagetool-server.jar"
        if not java.is_file() or not server.is_file():
            raise RuntimeError("Local grammar resources are missing. Restore Resourses/grammar before checking grammar.")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.process = subprocess.Popen(
            [str(java), "-Xmx512m", "-cp", str(server.parent / "*"),
             "org.languagetool.server.HTTPServer", "--port", str(self.port)],
            cwd=str(server.parent), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not self.closed.is_set():
            if self.process.poll() is not None:
                raise RuntimeError("The local LanguageTool grammar server could not start.")
            try:
                with urlopen(f"http://127.0.0.1:{self.port}/v2/languages", timeout=1) as response:
                    if response.status == 200:
                        return
            except (OSError, URLError):
                self.closed.wait(0.2)
        self._stop_server()
        raise RuntimeError("Local grammar startup was cancelled or timed out.")

    def check(self, text: str) -> list[GrammarIssue]:
        """What: Check English grammar locally while leaving spelling to Hunspell.

        Why included: Text must never be sent to a public service or automatically rewritten.
        Where used: Debounced editor checks and explicit grammar context-menu actions.
        Returns: Grammar issue spans/messages/replacements; raises on engine failure.
        """
        if not text.strip():
            return []
        with self.lock:
            self._ensure_server()
            payload = urlencode({"text": text, "language": "en-GB", "disabledCategories": "TYPOS"}).encode("utf-8")
            request = Request(f"http://127.0.0.1:{self.port}/v2/check", data=payload)
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
        issues: list[GrammarIssue] = []
        for match in result.get("matches", []):
            offset = int(match["offset"])
            end = offset + int(match["length"])
            issues.append(GrammarIssue(
                start=_character_offset(text, offset), end=_character_offset(text, end),
                message=str(match.get("message") or "Grammar suggestion"),
                replacements=tuple(str(item["value"]) for item in match.get("replacements", [])[:8]),
                rule_id=str(match.get("rule", {}).get("id") or ""),
            ))
        for match in MINISTRY_AGREEMENT.finditer(text):
            start, end = match.span("verb")
            if not any(issue.start <= start and issue.end >= end for issue in issues):
                replacement = "IS" if match.group("verb").isupper() else "is"
                issues.append(GrammarIssue(start, end, "Jesus is a singular subject; use 'is' rather than 'are'.",
                                           (replacement,), "MINISTRY_JESUS_AGREEMENT"))
        return issues

    def _stop_server(self) -> None:
        """What: Terminate and reap only the Java process owned by this engine.

        Why included: Closing the app must not leave a local grammar server running.
        Where used: Failed startup and close().
        Returns: None.
        """
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None

    def close(self) -> None:
        """What: Cancel startup/checking and release the owned local server.

        Why included: Grammar resources have a separate process lifecycle from Tk widgets.
        Where used: SpellCheckController shutdown.
        Returns: None.
        """
        self.closed.set()
        with self.lock:
            self._stop_server()