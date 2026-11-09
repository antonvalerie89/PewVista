"""Regression tests for the seamless looping video preview source."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

try:
    import imageio.v2 as imageio
except ImportError:
    imageio = None

from video_loop import LoopingVideoReader


@unittest.skipIf(imageio is None, "imageio is required for video preview tests")
class LoopingVideoReaderTests(unittest.TestCase):
    """What: Exercise real decoding across loop boundaries and teardown.

    Why included: Preview scrolling froze whenever an MP4 background reached its end.
    Where used: run_reliability_checks.bat release gate.
    Returns: None; assertions verify continuous playback and file release.
    """

    def _write_clip(self, directory: Path, frame_count: int = 12, fps: int = 12) -> Path:
        """What: Write a short clip whose frames encode their own index in brightness.

        Why included: Tests need a real decoder without shipping media fixtures.
        Where used: Each test below. Returns: Path to the generated MP4.
        """
        frames = [np.full((32, 32, 3), 20 + index * 18, dtype=np.uint8) for index in range(frame_count)]
        clip = directory / "loop.mp4"
        imageio.mimwrite(clip, frames, fps=fps, macro_block_size=1)
        return clip

    def test_wall_clock_playback_loops_without_stalling(self):
        """What: Play several loops by wall clock and confirm wraps never block for long.

        Why included: Preview scrolling froze whenever an MP4 background reached its end.
        Where used: Seamless video loop regression suite.
        Returns: None; asserts repeated loops and a short worst-case stall.
        """
        with tempfile.TemporaryDirectory(prefix="PewVistaLoop-") as directory:
            clip = self._write_clip(Path(directory))
            source = LoopingVideoReader(clip)
            try:
                start = time.monotonic()
                indices = []
                worst_ms = 0.0
                while time.monotonic() - start < 3.2:
                    began = time.perf_counter()
                    _frame, index = source.frame_at_time(time.monotonic())
                    worst_ms = max(worst_ms, (time.perf_counter() - began) * 1000.0)
                    indices.append(index)
                    time.sleep(0.03)
                wraps = sum(1 for previous, current in zip(indices, indices[1:]) if current < previous)
                self.assertGreaterEqual(wraps, 2, f"Expected repeated loops, saw indices {indices}")
                self.assertTrue(all(index < 12 for index in indices), f"Index ran past the clip end: {indices}")
                self.assertLess(worst_ms, 400.0, f"Loop point stalled playback for {worst_ms:.0f} ms")
            finally:
                source.close()

    def test_sequential_playback_learns_exact_length(self):
        """What: Step frame by frame past the end and confirm a clean restart at frame zero.

        Why included: Timer-driven previews must learn the true clip length and wrap exactly.
        Where used: Seamless video loop regression suite.
        Returns: None; asserts the frame sequence and learned length.
        """
        with tempfile.TemporaryDirectory(prefix="PewVistaLoop-") as directory:
            clip = self._write_clip(Path(directory))
            source = LoopingVideoReader(clip)
            try:
                indices = [source.next_frame()[1] for _ in range(30)]
                self.assertEqual(indices[:12], list(range(12)))
                self.assertEqual(indices[12], 0)
                self.assertTrue(source._count_exact)
                self.assertEqual(source._frame_count, 12)
            finally:
                source.close()

    def test_close_releases_the_video_file(self):
        """What: Confirm close() stops every decoder, including a warm standby.

        Why included: A leaked ffmpeg process would lock the media file on Windows.
        Where used: Seamless video loop regression suite.
        Returns: None; asserts the video file can be deleted after closing.
        """
        directory = Path(tempfile.mkdtemp(prefix="PewVistaLoop-"))
        clip = self._write_clip(directory)
        source = LoopingVideoReader(clip)
        for _ in range(14):
            source.next_frame()
        source.close()
        clip.unlink()
        directory.rmdir()
        self.assertFalse(clip.exists())


if __name__ == "__main__":
    unittest.main()
