"""Seamlessly looping video frame source shared by the preview renderers.

imageio's ffmpeg reader reports an unknown frame count, so wall-clock playback runs
past the end of a clip, and reopening a reader at the loop point costs hundreds of
milliseconds while a new ffmpeg process starts. This reader wraps before the end,
treats any end-of-stream failure as a loop, and warms the next loop's decoder on a
background thread so the loop point costs almost nothing.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

try:
    import imageio.v2 as imageio
except ImportError:
    imageio = None


PREWARM_SECONDS = 1.5


def _close_quietly(reader) -> None:
    """What: Close an imageio reader, ignoring shutdown errors.

    Why included: Retired decoders may already have exited; closing must never raise.
    Where used: Reader swaps and LoopingVideoReader.close(), usually off the caller's thread.
    Returns: None.
    """
    if reader is None:
        return
    try:
        reader.close()
    except Exception:
        pass


class LoopingVideoReader:
    """Decode a video clip as an endless loop for preview animation.

    What: Serves frames by wall-clock time or sequentially, wrapping at the clip end.
    Why included: Preview animations (including scrolling text) stalled or froze at
    every loop point while the decoder was reopened on the render path.
    Where used: Mirror, Welcome, Library, and Preparation preview video playback.
    Returns: Instances expose fps, path, frame_at_time(), next_frame(), and close().
    """

    def __init__(self, path: str | Path, reader_kwargs: dict | None = None) -> None:
        """What: Open the clip, read its first frame, and estimate its frame count.

        Why included: The estimate lets the next loop's decoder warm up before the end.
        Where used: Preview renderers when a video background becomes active.
        Returns: None; raises when imageio is unavailable or the clip cannot be decoded.
        """
        if imageio is None:
            raise RuntimeError("Video previews require imageio and imageio-ffmpeg in this app build.")
        self.path = str(path)
        self._reader_kwargs = dict(reader_kwargs or {})
        self._lock = threading.Lock()
        self._standby = None
        self._prewarming = False
        self._closed = False
        self._reader = self._open_reader()
        metadata = self._reader.get_meta_data() or {}
        try:
            self.fps = max(1.0, float(metadata.get("fps") or 24.0))
        except (TypeError, ValueError):
            self.fps = 24.0
        try:
            duration = float(metadata.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        estimated_frames = int(duration * self.fps) if duration > 0 else 0
        self._frame_count: int | None = estimated_frames if estimated_frames > 0 else None
        self._count_exact = False
        self._last_index = 0
        self._next_index = 0
        self._loop_started: float | None = None
        self._threads: list[threading.Thread] = []

    def _start_helper(self, target, name: str, *args) -> None:
        """What: Run a short background helper and remember it for orderly shutdown.

        Why included: close() must not return while a helper still holds the video file.
        Where used: Prewarming the next decoder and retiring exhausted decoders.
        Returns: None.
        """
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _open_reader(self):
        """What: Start a decoder and read frame zero so it is ready to serve.

        Why included: Warm readers make the loop swap immediate.
        Where used: Construction, background prewarming, and synchronous fallback.
        Returns: An imageio reader positioned at the first frame.
        """
        reader = imageio.get_reader(self.path, **self._reader_kwargs)
        try:
            reader.get_data(0)
        except Exception:
            _close_quietly(reader)
            raise
        return reader

    def _prewarm_worker(self) -> None:
        """What: Open the next loop's decoder in the background and park it as standby.

        Why included: Starting ffmpeg is slow; doing it before the loop point hides the cost.
        Where used: Thread started by _maybe_prewarm() near the end of each loop.
        Returns: None; discards the reader when the source closed meanwhile.
        """
        try:
            reader = self._open_reader()
        except Exception:
            reader = None
        with self._lock:
            self._prewarming = False
            if self._closed or reader is None or self._standby is not None:
                discard = reader
            else:
                self._standby = reader
                discard = None
        _close_quietly(discard)

    def _maybe_prewarm(self, index: int) -> None:
        """What: Begin warming the next decoder once playback nears the clip end.

        Why included: Prewarming every frame would waste processes; this triggers once per loop.
        Where used: _frame() before each decode.
        Returns: None.
        """
        if not self._frame_count:
            return
        if index < self._frame_count - int(PREWARM_SECONDS * self.fps):
            return
        with self._lock:
            if self._closed or self._prewarming or self._standby is not None:
                return
            self._prewarming = True
        self._start_helper(self._prewarm_worker, "VideoLoopPrewarm")

    def _swap_to_fresh_reader(self) -> None:
        """What: Replace the exhausted decoder with the warm standby, or open one now.

        Why included: Restarting at frame zero must not require respawning ffmpeg on the hot path.
        Where used: _wrap() at each loop point.
        Returns: None; retires the old decoder on a background thread.
        """
        with self._lock:
            standby = self._standby
            self._standby = None
        retired = self._reader
        self._reader = standby if standby is not None else self._open_reader()
        self._start_helper(_close_quietly, "VideoLoopRetire", retired)

    def _wrap(self, index: int, now: float | None) -> tuple[object, int]:
        """What: Restart the loop, preserving playback phase when the caller overshot.

        Why included: A late caller should continue mid-loop rather than jump back to frame zero.
        Where used: _frame() when the clip end is known or detected.
        Returns: The decoded frame and its index within the new loop.
        """
        count = self._frame_count or max(1, index)
        wrapped = index % count if count > 0 else 0
        self._swap_to_fresh_reader()
        try:
            frame = self._reader.get_data(wrapped)
        except Exception:
            wrapped = 0
            frame = self._reader.get_data(0)
        if now is not None:
            self._loop_started = now - wrapped / self.fps
        self._last_index = wrapped
        return frame, wrapped

    def _frame(self, index: int, now: float | None) -> tuple[object, int]:
        """What: Decode one frame, wrapping at the known or detected end of the clip.

        Why included: Shared core of wall-clock and sequential playback.
        Where used: frame_at_time() and next_frame().
        Returns: The decoded frame and the index actually served.
        """
        if self._closed:
            raise RuntimeError("Video preview source is closed.")
        if self._count_exact and self._frame_count and index >= self._frame_count:
            return self._wrap(index, now)
        self._maybe_prewarm(index)
        try:
            frame = self._reader.get_data(index)
        except Exception:
            if index > 0 and index == self._last_index + 1:
                # A failure right after the previous frame marks the true clip length.
                self._frame_count = index
                self._count_exact = True
            return self._wrap(index, now)
        self._last_index = index
        return frame, index

    def frame_at_time(self, now: float | None = None) -> tuple[object, int]:
        """What: Return the frame for the current wall-clock position in the loop.

        Why included: Worker-driven previews must keep real-time pacing despite render delays.
        Where used: Mirror, Welcome, and Preparation preview renderers, passing their own clock.
        Returns: The decoded frame and its index within the current loop.
        """
        current = time.monotonic() if now is None else float(now)
        if self._loop_started is None:
            self._loop_started = current
        index = max(0, int((current - self._loop_started) * self.fps))
        return self._frame(index, current)

    def next_frame(self) -> tuple[object, int]:
        """What: Return the next sequential frame, wrapping at the clip end.

        Why included: Timer-driven previews advance exactly one frame per tick.
        Where used: Library preview video playback.
        Returns: The decoded frame and its index within the current loop.
        """
        frame, index = self._frame(self._next_index, None)
        self._next_index = index + 1
        return frame, index

    def close(self) -> None:
        """What: Stop all decoders owned by this source, including a warm standby.

        Why included: ffmpeg subprocesses must not outlive the preview that used them.
        Where used: Preview teardown when the background changes or the app closes.
        Returns: None; safe to call more than once.
        """
        with self._lock:
            self._closed = True
            standby = self._standby
            self._standby = None
        reader = self._reader
        self._reader = None
        _close_quietly(reader)
        _close_quietly(standby)
        for thread in list(self._threads):
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._threads = []
