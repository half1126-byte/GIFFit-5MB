from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from converter import (  # noqa: E402
    ConversionError,
    _NoFitError,
    _adaptive_search,
    _build_width_ladder,
    _dimensions_for_width,
    _parse_ffmpeg_out_time,
    _parse_gifsicle_info,
    _parse_probe_payload,
    _unique_output_path,
)


class ProbeParsingTests(unittest.TestCase):
    def test_parses_counted_frames_fractional_fps_and_side_data_rotation(self) -> None:
        payload = {
            "streams": [
                {
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30/1",
                    "duration": "6.208333",
                    "nb_frames": "148",
                    "nb_read_frames": "149",
                    "tags": {"rotate": "0"},
                    "side_data_list": [{"rotation": -90}],
                }
            ],
            "format": {"duration": "6.229333"},
        }

        info = _parse_probe_payload(payload, "C:/영상/치과.mp4")

        self.assertEqual(info.path, Path("C:/영상/치과.mp4"))
        self.assertEqual((info.width, info.height), (1920, 1080))
        self.assertAlmostEqual(info.duration, 6.208333)
        self.assertAlmostEqual(info.fps, 30000 / 1001)
        self.assertEqual(info.frame_count, 149)
        self.assertEqual(info.rotation, 270)
        self.assertEqual(_dimensions_for_width(info, 608), (608, 1081))
        self.assertEqual(
            _dimensions_for_width(info, 608, even_height=True), (608, 1080)
        )

    def test_uses_format_duration_and_derives_fps_when_rates_are_unknown(self) -> None:
        payload = {
            "streams": [
                {
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "0/0",
                    "r_frame_rate": "N/A",
                    "nb_read_frames": "100",
                    "tags": {"rotate": "450"},
                }
            ],
            "format": {"duration": "4.0"},
        }

        info = _parse_probe_payload(payload, "clip.mp4")

        self.assertEqual(info.rotation, 90)
        self.assertEqual(info.duration, 4.0)
        self.assertEqual(info.fps, 25.0)

    def test_rejects_missing_video_stream(self) -> None:
        with self.assertRaises(ConversionError):
            _parse_probe_payload({"streams": []}, "audio.mp4")

    def test_rejects_video_when_frame_count_cannot_be_verified(self) -> None:
        payload = {
            "streams": [
                {
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "24/1",
                    "duration": "2.0",
                    "nb_frames": "N/A",
                    "nb_read_frames": "N/A",
                }
            ],
            "format": {"duration": "2.0"},
        }

        with self.assertRaises(ConversionError):
            _parse_probe_payload(payload, "unknown-frames.mp4")


class SearchTests(unittest.TestCase):
    def test_width_ladder_keeps_exact_endpoints_and_16px_grid(self) -> None:
        self.assertEqual(_build_width_ladder(161, 193), [161, 176, 192, 193])
        self.assertEqual(_build_width_ladder(160, 192), [160, 176, 192])

    def test_adaptive_search_uses_real_sizes_and_finds_largest_fit(self) -> None:
        @dataclass(frozen=True)
        class FakeCandidate:
            width: int
            size_bytes: int

        calls: list[int] = []

        def evaluate(width: int) -> FakeCandidate:
            calls.append(width)
            return FakeCandidate(width, width * width * 20)

        widths = _build_width_ladder(160, 640)
        outcome = _adaptive_search(
            widths,
            evaluate,
            target_limit=5_000_000,
            hard_limit=5_000_000,
        )

        self.assertEqual(outcome.best.width, 496)
        self.assertLessEqual(outcome.best.size_bytes, 5_000_000)
        self.assertEqual(outcome.attempts, len(set(calls)))
        self.assertEqual(len(calls), len(set(calls)), "candidate widths must be cached")
        self.assertIn(640, calls, "full-resolution pilot must run first")

    def test_search_uses_hard_limit_if_minimum_misses_only_safe_margin(self) -> None:
        @dataclass(frozen=True)
        class FakeCandidate:
            width: int
            size_bytes: int

        sizes = {160: 4_975_000, 176: 5_100_000, 192: 5_300_000}
        outcome = _adaptive_search(
            [160, 176, 192],
            lambda width: FakeCandidate(width, sizes[width]),
            target_limit=4_950_000,
            hard_limit=5_000_000,
        )

        self.assertEqual(outcome.best.width, 160)
        self.assertEqual(outcome.threshold, 5_000_000)

    def test_search_fails_when_minimum_exceeds_hard_limit(self) -> None:
        @dataclass(frozen=True)
        class FakeCandidate:
            width: int
            size_bytes: int

        with self.assertRaises(_NoFitError) as context:
            _adaptive_search(
                [160, 176, 192],
                lambda width: FakeCandidate(width, width * 40_000),
                target_limit=4_950_000,
                hard_limit=5_000_000,
            )
        self.assertEqual(context.exception.smallest_size, 6_400_000)


class OutputAndHelperTests(unittest.TestCase):
    def test_unique_output_name_never_reuses_existing_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            directory = Path(temp_name)
            source = directory / "한글 샘플.mp4"

            self.assertEqual(
                _unique_output_path(directory, source).name,
                "한글 샘플_5MB.gif",
            )
            (directory / "한글 샘플_5MB.gif").write_bytes(b"old")
            self.assertEqual(
                _unique_output_path(directory, source).name,
                "한글 샘플_5MB_2.gif",
            )
            (directory / "한글 샘플_5MB_2.gif").write_bytes(b"old")
            self.assertEqual(
                _unique_output_path(directory, source).name,
                "한글 샘플_5MB_3.gif",
            )

    def test_parses_gifsicle_frame_loop_and_delay_metadata(self) -> None:
        text = """\
* result.gif 149 images
  logical screen 400x225
  loop forever
  + image #0 400x225 disposal asis delay 0.04s
  + image #1 400x225 disposal asis delay 0.05s
"""
        info = _parse_gifsicle_info(text)
        self.assertEqual(info.frames, 149)
        self.assertTrue(info.loop_forever)
        self.assertEqual(info.delays, (0.04, 0.05))

    def test_parses_ffmpeg_progress_timestamp(self) -> None:
        self.assertAlmostEqual(
            _parse_ffmpeg_out_time("out_time=01:02:03.500000") or 0,
            3723.5,
        )
        self.assertIsNone(_parse_ffmpeg_out_time("frame=42"))


if __name__ == "__main__":
    unittest.main()
