from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
import sys
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import converter as converter_module  # noqa: E402
from converter import (  # noqa: E402
    CancelledError,
    ConversionEngine,
    ConversionError,
    ConversionSettings,
    ProbeInfo,
    _Candidate,
    _CommandResult,
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
                    "index": 3,
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30/1",
                    "duration": "6.208333",
                    "nb_frames": "148",
                    "nb_read_frames": "149",
                    "tags": {"rotate": "0"},
                    "side_data_list": [{"rotation": -90}],
                    "disposition": {"default": 1, "attached_pic": 0},
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
        self.assertEqual(info.stream_index, 3)
        self.assertEqual(_dimensions_for_width(info, 608), (608, 1081))
        self.assertEqual(
            _dimensions_for_width(info, 608, even_height=True), (608, 1080)
        )

    def test_uses_format_duration_and_derives_fps_when_rates_are_unknown(self) -> None:
        payload = {
            "streams": [
                {
                    "index": 0,
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "0/0",
                    "r_frame_rate": "N/A",
                    "nb_read_frames": "100",
                    "tags": {"rotate": "450"},
                    "disposition": {"default": 1, "attached_pic": 0},
                }
            ],
            "format": {"duration": "4.0"},
        }

        info = _parse_probe_payload(payload, "clip.mp4")

        self.assertEqual(info.rotation, 90)
        self.assertEqual(info.duration, 4.0)
        self.assertEqual(info.fps, 25.0)
        self.assertEqual(info.stream_index, 0)

    def test_prefers_default_non_attached_stream_and_keeps_absolute_index(self) -> None:
        def stream(
            index: int, width: int, *, default: int = 0, attached_pic: int = 0
        ) -> dict[str, object]:
            return {
                "index": index,
                "width": width,
                "height": 720,
                "avg_frame_rate": "25/1",
                "duration": "2.0",
                "nb_read_frames": "50",
                "disposition": {
                    "default": default,
                    "attached_pic": attached_pic,
                },
            }

        payload = {
            "streams": [
                stream(2, 600, default=1, attached_pic=1),
                stream(4, 960),
                stream(7, 1280, default=1),
            ]
        }

        info = _parse_probe_payload(payload, "multi-stream.mkv")

        self.assertEqual(info.stream_index, 7)
        self.assertEqual(info.width, 1280)

    def test_uses_first_non_attached_stream_when_none_is_default(self) -> None:
        payload = {
            "streams": [
                {
                    "index": 1,
                    "width": 300,
                    "height": 300,
                    "avg_frame_rate": "1/1",
                    "duration": "1",
                    "nb_read_frames": "1",
                    "disposition": {"default": 1, "attached_pic": 1},
                },
                {
                    "index": 5,
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "24/1",
                    "duration": "1",
                    "nb_read_frames": "24",
                    "disposition": {"default": 0, "attached_pic": 0},
                },
                {
                    "index": 8,
                    "width": 1280,
                    "height": 720,
                    "avg_frame_rate": "24/1",
                    "duration": "1",
                    "nb_read_frames": "24",
                    "disposition": {"default": 0, "attached_pic": 0},
                },
            ]
        }

        info = _parse_probe_payload(payload, "multi-stream.mkv")

        self.assertEqual(info.stream_index, 5)
        self.assertEqual(info.width, 640)

    def test_rejects_non_quarter_turn_display_rotation(self) -> None:
        payload = {
            "streams": [
                {
                    "index": 0,
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "24/1",
                    "duration": "1",
                    "nb_read_frames": "24",
                    "side_data_list": [{"rotation": 45}],
                    "disposition": {"default": 1, "attached_pic": 0},
                }
            ]
        }

        with self.assertRaisesRegex(ConversionError, "multiples of 90"):
            _parse_probe_payload(payload, "rotated.mp4")

    def test_rejects_missing_video_stream(self) -> None:
        with self.assertRaises(ConversionError):
            _parse_probe_payload({"streams": []}, "audio.mp4")

    def test_rejects_video_when_frame_count_cannot_be_verified(self) -> None:
        payload = {
            "streams": [
                {
                    "index": 0,
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "24/1",
                    "duration": "2.0",
                    "nb_frames": "N/A",
                    "nb_read_frames": "N/A",
                    "disposition": {"default": 1, "attached_pic": 0},
                }
            ],
            "format": {"duration": "2.0"},
        }

        with self.assertRaises(ConversionError):
            _parse_probe_payload(payload, "unknown-frames.mp4")


class SettingsTests(unittest.TestCase):
    def test_global_limit_accepts_exact_boundary_and_rejects_one_byte_over(self) -> None:
        self.assertEqual(
            ConversionSettings(limit_bytes=5_000_000).limit_bytes,
            5_000_000,
        )
        with self.assertRaisesRegex(ValueError, "global maximum"):
            ConversionSettings(limit_bytes=5_000_001)

    def test_global_limit_still_accepts_smallest_positive_value(self) -> None:
        self.assertEqual(ConversionSettings(limit_bytes=1).limit_bytes, 1)


class EngineCommandTests(unittest.TestCase):
    def test_probe_requests_all_video_streams_and_dispositions(self) -> None:
        payload = {
            "streams": [
                {
                    "index": 4,
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "24/1",
                    "duration": "1",
                    "nb_read_frames": "24",
                    "disposition": {"default": 1, "attached_pic": 0},
                }
            ]
        }

        class RecordingEngine(ConversionEngine):
            def __init__(self) -> None:
                super().__init__("ffmpeg", "ffprobe", "gifsicle")
                self.command: list[str] = []

            def _run(self, argv: object, **kwargs: object) -> _CommandResult:
                self.command = [str(value) for value in argv]  # type: ignore[union-attr]
                return _CommandResult(0, json.dumps(payload), "")

        engine = RecordingEngine()
        info = engine._probe_media(Path("multi-stream.mkv"))

        select_position = engine.command.index("-select_streams")
        entries_position = engine.command.index("-show_entries")
        self.assertEqual(engine.command[select_position + 1], "v")
        self.assertIn("stream=index", engine.command[entries_position + 1])
        self.assertIn(
            "stream_disposition=default,attached_pic",
            engine.command[entries_position + 1],
        )
        self.assertEqual(info.stream_index, 4)

    def test_encoder_filter_uses_absolute_selected_stream_index(self) -> None:
        class RecordingEngine(ConversionEngine):
            def __init__(self) -> None:
                super().__init__("ffmpeg", "ffprobe", "gifsicle")
                self.commands: list[list[str]] = []

            def _run(self, argv: object, **kwargs: object) -> _CommandResult:
                command = [str(value) for value in argv]  # type: ignore[union-attr]
                self.commands.append(command)
                Path(command[-1]).write_bytes(b"GIF89a")
                return _CommandResult(0, "", "")

        info = ProbeInfo(
            path=Path("multi-stream.mkv"),
            width=640,
            height=360,
            duration=1.0,
            fps=24.0,
            frame_count=24,
            rotation=0,
            stream_index=6,
        )
        engine = RecordingEngine()
        with tempfile.TemporaryDirectory() as temp_name:
            raw_path = Path(temp_name) / "raw.gif"
            engine._encode_raw_gif(
                Path("multi-stream.mkv"), info, 320, raw_path, attempt=1
            )

        command = engine.commands[0]
        filter_position = command.index("-filter_complex")
        self.assertTrue(command[filter_position + 1].startswith("[0:6]"))

    def test_final_validation_enables_strict_decoder_and_gifsicle_checks(self) -> None:
        info = ProbeInfo(
            path=Path("source.mp4"),
            width=10,
            height=10,
            duration=1.0,
            fps=2.0,
            frame_count=2,
            rotation=0,
            stream_index=0,
        )

        class ValidationEngine(ConversionEngine):
            def __init__(self, decode_stderr: str = "") -> None:
                super().__init__("ffmpeg", "ffprobe", "gifsicle")
                self.commands: list[list[str]] = []
                self.decode_stderr = decode_stderr

            def _probe_media(self, path: Path) -> ProbeInfo:
                return info

            def _run(self, argv: object, **kwargs: object) -> _CommandResult:
                command = [str(value) for value in argv]  # type: ignore[union-attr]
                self.commands.append(command)
                if "--info" in command:
                    return _CommandResult(
                        0,
                        "* final.gif 2 images\n  loop forever\n",
                        "",
                    )
                return _CommandResult(0, "", self.decode_stderr)

        with tempfile.TemporaryDirectory() as temp_name:
            candidate_path = Path(temp_name) / "candidate.gif"
            candidate_path.write_bytes(b"GIF89a")
            candidate = _Candidate(10, 10, 6, 2, 1.0, candidate_path)
            engine = ValidationEngine()

            engine._validate_candidate(candidate, info, ConversionSettings())

        gifsicle_command = next(
            command for command in engine.commands if "--info" in command
        )
        decode_command = next(
            command for command in engine.commands if "-f" in command
        )
        self.assertIn("--no-ignore-errors", gifsicle_command)
        self.assertIn("-xerror", decode_command)

    def test_final_validation_rejects_decoder_stderr_even_on_zero_exit(self) -> None:
        info = ProbeInfo(
            Path("source.mp4"), 10, 10, 1.0, 2.0, 2, 0, stream_index=0
        )

        class ValidationEngine(ConversionEngine):
            def __init__(self) -> None:
                super().__init__("ffmpeg", "ffprobe", "gifsicle")

            def _probe_media(self, path: Path) -> ProbeInfo:
                return info

            def _run(self, argv: object, **kwargs: object) -> _CommandResult:
                command = [str(value) for value in argv]  # type: ignore[union-attr]
                if "--info" in command:
                    return _CommandResult(
                        0,
                        "* final.gif 2 images\n  loop forever\n",
                        "",
                    )
                return _CommandResult(0, "", "corrupt frame detected")

        with tempfile.TemporaryDirectory() as temp_name:
            candidate_path = Path(temp_name) / "candidate.gif"
            candidate_path.write_bytes(b"GIF89a")
            candidate = _Candidate(10, 10, 6, 2, 1.0, candidate_path)

            with self.assertRaisesRegex(ConversionError, "corrupt frame detected"):
                ValidationEngine()._validate_candidate(
                    candidate, info, ConversionSettings()
                )


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


class PublicationOrderingTests(unittest.TestCase):
    @staticmethod
    def _engine(cancel_event: threading.Event | None = None) -> ConversionEngine:
        class MinimalEngine(ConversionEngine):
            def probe(self, path: str | Path) -> ProbeInfo:
                return ProbeInfo(
                    path=Path(path),
                    width=160,
                    height=90,
                    duration=1.0,
                    fps=10.0,
                    frame_count=10,
                    rotation=0,
                    stream_index=0,
                )

            def _build_candidate(
                self,
                source: Path,
                info: ProbeInfo,
                width: int,
                temp_dir: Path,
                settings: ConversionSettings,
                attempt: int,
            ) -> _Candidate:
                candidate_path = temp_dir / "candidate.gif"
                candidate_path.write_bytes(b"GIF89a")
                return _Candidate(width, 90, 6, 10, 1.0, candidate_path)

            def _validate_candidate(
                self,
                candidate: _Candidate,
                source_info: ProbeInfo,
                settings: ConversionSettings,
            ) -> _Candidate:
                return candidate

        return MinimalEngine(
            "ffmpeg",
            "ffprobe",
            "gifsicle",
            cancel_event=cancel_event,
        )

    def test_cleanup_failure_never_leaves_a_published_result(self) -> None:
        class FailingCleanupDirectory:
            def __init__(self, *, prefix: str, dir: str) -> None:
                self.name = tempfile.mkdtemp(prefix=prefix, dir=dir)

            def __enter__(self) -> str:
                return self.name

            def __exit__(self, *args: object) -> None:
                raise OSError("forced cleanup failure")

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            output_dir = root / "output"

            with mock.patch.object(
                converter_module.tempfile,
                "TemporaryDirectory",
                FailingCleanupDirectory,
            ):
                with self.assertRaisesRegex(OSError, "forced cleanup failure"):
                    self._engine().convert(source, output_dir, ConversionSettings())

            self.assertFalse(list(output_dir.glob("*_5MB*.gif")))
            self.assertFalse(list(output_dir.glob(".giffit-ready-*.gif")))

    def test_cancellation_after_workspace_cleanup_prevents_publication(self) -> None:
        cancel_event = threading.Event()
        real_temporary_directory = tempfile.TemporaryDirectory

        class CancelAfterCleanupDirectory:
            def __init__(self, *, prefix: str, dir: str) -> None:
                self.inner = real_temporary_directory(prefix=prefix, dir=dir)

            def __enter__(self) -> str:
                return self.inner.__enter__()

            def __exit__(self, *args: object) -> bool | None:
                result = self.inner.__exit__(*args)
                cancel_event.set()
                return result

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            output_dir = root / "output"

            with mock.patch.object(
                converter_module.tempfile,
                "TemporaryDirectory",
                CancelAfterCleanupDirectory,
            ):
                with self.assertRaises(CancelledError):
                    self._engine(cancel_event).convert(
                        source, output_dir, ConversionSettings()
                    )

            self.assertFalse(list(output_dir.glob("*_5MB*.gif")))
            self.assertFalse(list(output_dir.glob(".giffit-ready-*.gif")))


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
