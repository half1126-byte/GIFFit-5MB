"""Adaptive, frame-preserving video to GIF conversion.

The engine deliberately measures real, fully optimized GIF files.  GIF size is
not sufficiently predictable for a bitrate-style calculation, so a pilot
encode and a bounded search are used to find the largest useful resolution
under the requested byte limit.

Only the Python standard library is used.  FFmpeg, FFprobe and gifsicle are
invoked with argument arrays (never through a shell), which is important for
Windows paths containing spaces or non-ASCII characters.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, Mapping, Sequence, TypeVar


ProgressCallback = Callable[[str, float | None, str, dict[str, Any]], None]


class ConversionError(RuntimeError):
    """Raised when a source cannot be converted within the requested rules."""


class CancelledError(ConversionError):
    """Raised when the caller requests cancellation."""


@dataclass(frozen=True)
class ProbeInfo:
    path: Path
    width: int
    height: int
    duration: float
    fps: float
    frame_count: int
    rotation: int


@dataclass(frozen=True)
class ConversionSettings:
    limit_bytes: int = 5_000_000
    safe_ratio: float = 0.99
    quality_mode: str = "balanced"
    min_width: int = 160

    def __post_init__(self) -> None:
        if self.limit_bytes <= 0:
            raise ValueError("limit_bytes must be greater than zero")
        if not 0 < self.safe_ratio <= 1:
            raise ValueError("safe_ratio must be greater than 0 and at most 1")
        if self.quality_mode not in {"quality", "balanced", "resolution"}:
            raise ValueError(
                "quality_mode must be 'quality', 'balanced', or 'resolution'"
            )
        if self.min_width <= 0:
            raise ValueError("min_width must be greater than zero")


@dataclass(frozen=True)
class ConversionResult:
    input_path: Path
    output_path: Path
    width: int
    height: int
    size_bytes: int
    frames: int
    duration: float
    attempts: int


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


class _ProcessError(ConversionError):
    def __init__(self, argv: Sequence[str], result: _CommandResult) -> None:
        self.argv = tuple(argv)
        self.result = result
        diagnostic = (result.stderr or result.stdout).strip()
        if len(diagnostic) > 4_000:
            diagnostic = diagnostic[-4_000:]
        executable = Path(argv[0]).name if argv else "process"
        message = f"{executable} exited with code {result.returncode}"
        if diagnostic:
            message += f": {diagnostic}"
        super().__init__(message)


class _NoFitError(Exception):
    def __init__(self, smallest_size: int) -> None:
        self.smallest_size = smallest_size
        super().__init__(str(smallest_size))


@dataclass(frozen=True)
class _Candidate:
    width: int
    height: int
    size_bytes: int
    frames: int
    duration: float
    path: Path


@dataclass(frozen=True)
class _GifInfo:
    frames: int | None
    loop_forever: bool
    delays: tuple[float, ...]


_T = TypeVar("_T")


@dataclass(frozen=True)
class _SearchOutcome(Generic[_T]):
    best: _T
    attempts: int
    tested: Mapping[int, _T]
    threshold: int


def _parse_rate(value: Any) -> float:
    """Parse an FFprobe decimal or rational rate, returning zero if unknown."""

    if value is None:
        return 0.0
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return 0.0
    try:
        if "/" in text:
            numerator_text, denominator_text = text.split("/", 1)
            denominator = float(denominator_text)
            if denominator == 0:
                return 0.0
            value_float = float(numerator_text) / denominator
        else:
            value_float = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    if not math.isfinite(value_float) or value_float <= 0:
        return 0.0
    return value_float


def _positive_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _normalise_rotation(value: Any) -> int:
    try:
        degrees = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(degrees):
        return 0
    return int(round(degrees / 90.0) * 90) % 360


def _parse_probe_payload(payload: Mapping[str, Any], path: str | Path) -> ProbeInfo:
    """Turn FFprobe JSON data into stable, typed metadata."""

    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ConversionError("No decodable video stream was found")
    stream = streams[0]
    if not isinstance(stream, Mapping):
        raise ConversionError("FFprobe returned invalid stream metadata")

    width = _positive_int(stream.get("width"))
    height = _positive_int(stream.get("height"))
    if width == 0 or height == 0:
        raise ConversionError("The video has an invalid or missing resolution")

    frame_count = _positive_int(stream.get("nb_read_frames"))
    if frame_count == 0:
        frame_count = _positive_int(stream.get("nb_frames"))
    if frame_count == 0:
        raise ConversionError(
            "The source frame count could not be verified; conversion was stopped "
            "to avoid silently dropping frames"
        )

    duration = _positive_float(stream.get("duration"))
    if duration == 0:
        format_info = payload.get("format")
        if isinstance(format_info, Mapping):
            duration = _positive_float(format_info.get("duration"))

    fps = _parse_rate(stream.get("avg_frame_rate"))
    if fps == 0:
        fps = _parse_rate(stream.get("r_frame_rate"))
    if fps == 0 and frame_count and duration:
        fps = frame_count / duration
    if duration == 0 and frame_count and fps:
        duration = frame_count / fps

    rotation_value: Any = None
    tags = stream.get("tags")
    if isinstance(tags, Mapping):
        rotation_value = tags.get("rotate")
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for entry in side_data:
            if isinstance(entry, Mapping) and entry.get("rotation") is not None:
                rotation_value = entry.get("rotation")
                break

    return ProbeInfo(
        path=Path(path),
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        frame_count=frame_count,
        rotation=_normalise_rotation(rotation_value),
    )


def _parse_probe_json(text: str, path: str | Path) -> ProbeInfo:
    try:
        payload = json.loads(text.lstrip("\ufeff"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConversionError(f"FFprobe returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ConversionError("FFprobe returned an invalid JSON document")
    return _parse_probe_payload(payload, path)


def _display_dimensions(info: ProbeInfo) -> tuple[int, int]:
    if info.rotation in {90, 270}:
        return info.height, info.width
    return info.width, info.height


def _dimensions_for_width(
    info: ProbeInfo, width: int, *, even_height: bool = False
) -> tuple[int, int]:
    source_width, source_height = _display_dimensions(info)
    if width <= 0 or source_width <= 0 or source_height <= 0:
        raise ValueError("width and source dimensions must be positive")
    height = max(1, int(math.floor((width * source_height / source_width) + 0.5)))
    if even_height and height % 2:
        # GIF supports odd sizes, but a few FFmpeg filter builds do not.  The
        # one-pixel downward fallback avoids unexpected upscaling or padding.
        height = max(2, height - 1)
    return width, height


def _build_width_ladder(min_width: int, max_width: int, step: int = 16) -> list[int]:
    """Return a sorted discrete search space including both exact endpoints."""

    if min_width <= 0 or max_width <= 0 or step <= 0:
        raise ValueError("widths and step must be positive")
    if min_width > max_width:
        raise ValueError("min_width cannot exceed max_width")
    widths = {min_width, max_width}
    first_grid_width = ((min_width + step - 1) // step) * step
    widths.update(range(first_grid_width, max_width + 1, step))
    return sorted(widths)


def _nearest_width_index(widths: Sequence[int], desired: float) -> int:
    position = bisect_left(widths, desired)
    if position <= 0:
        return 0
    if position >= len(widths):
        return len(widths) - 1
    before = widths[position - 1]
    after = widths[position]
    return position - 1 if desired - before <= after - desired else position


def _adaptive_search(
    widths: Sequence[int],
    evaluate: Callable[[int], _T],
    target_limit: int,
    hard_limit: int,
) -> _SearchOutcome[_T]:
    """Pilot, estimate and bracket-search a real-encode resolution ladder.

    ``evaluate`` must return an object with ``width`` and ``size_bytes``
    attributes.  Each width is evaluated at most once.  The target limit is
    preferred; the hard limit is used only when even the minimum width cannot
    meet the safety margin.
    """

    if not widths:
        raise ValueError("width ladder cannot be empty")
    ordered = sorted(set(int(width) for width in widths))
    if ordered != list(widths):
        raise ValueError("width ladder must be sorted and contain no duplicates")
    if target_limit <= 0 or hard_limit <= 0 or target_limit > hard_limit:
        raise ValueError("invalid target or hard byte limit")

    cache: dict[int, _T] = {}

    def at(index: int) -> _T:
        width = ordered[index]
        if width not in cache:
            candidate = evaluate(width)
            if int(getattr(candidate, "width")) != width:
                raise ConversionError("Candidate width does not match the request")
            size = int(getattr(candidate, "size_bytes"))
            if size <= 0:
                raise ConversionError("Candidate encoder returned an empty file")
            cache[width] = candidate
        return cache[width]

    def size(candidate: _T) -> int:
        return int(getattr(candidate, "size_bytes"))

    last_index = len(ordered) - 1
    pilot = at(last_index)
    if size(pilot) <= target_limit:
        return _SearchOutcome(pilot, len(cache), dict(cache), target_limit)

    ratio = math.sqrt(target_limit / size(pilot))
    estimated_width = ordered[-1] * max(0.0, min(1.0, ratio))
    estimated_index = _nearest_width_index(ordered, estimated_width)
    estimated = at(estimated_index)
    minimum = at(0)

    if size(minimum) <= target_limit:
        threshold = target_limit
    elif size(minimum) <= hard_limit:
        # Retain the best legal result rather than failing solely because the
        # configured safety margin cannot be met at the minimum resolution.
        threshold = hard_limit
    else:
        raise _NoFitError(size(minimum))

    if size(pilot) <= threshold:
        return _SearchOutcome(pilot, len(cache), dict(cache), threshold)

    passing_index = 0
    failing_index = last_index
    if 0 < estimated_index < last_index:
        if size(estimated) <= threshold:
            passing_index = estimated_index
        else:
            failing_index = estimated_index

    while failing_index - passing_index > 1:
        middle = (passing_index + failing_index) // 2
        candidate = at(middle)
        if size(candidate) <= threshold:
            passing_index = middle
        else:
            failing_index = middle

    fitting_indices = [
        index
        for index, width in enumerate(ordered)
        if width in cache and size(cache[width]) <= threshold
    ]
    best_index = max(fitting_indices) if fitting_indices else 0

    # Palette quantisation makes size nearly, but not perfectly, monotonic.
    # Check higher adjacent widths until three consecutive real failures.
    failure_streak = 0
    index = best_index + 1
    while index <= last_index and failure_streak < 3:
        candidate = at(index)
        if size(candidate) <= threshold:
            best_index = index
            failure_streak = 0
        else:
            failure_streak += 1
        index += 1

    fitting = [candidate for candidate in cache.values() if size(candidate) <= threshold]
    if not fitting:
        raise _NoFitError(size(minimum))
    best = max(fitting, key=lambda item: (int(getattr(item, "width")), -size(item)))
    return _SearchOutcome(best, len(cache), dict(cache), threshold)


def _parse_gifsicle_info(text: str) -> _GifInfo:
    frame_match = re.search(r"\b(\d+)\s+images?\b", text, flags=re.IGNORECASE)
    frames = int(frame_match.group(1)) if frame_match else None
    loop_forever = bool(
        re.search(r"\bloop\s+(?:forever|count\s+0)\b", text, flags=re.IGNORECASE)
    )
    delays = tuple(
        float(value)
        for value in re.findall(
            r"\bdelay\s+([0-9]+(?:\.[0-9]+)?)s\b", text, flags=re.IGNORECASE
        )
    )
    return _GifInfo(frames=frames, loop_forever=loop_forever, delays=delays)


def _parse_ffmpeg_out_time(line: str) -> float | None:
    if not line.startswith("out_time="):
        return None
    value = line.partition("=")[2].strip()
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", value)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _unique_output_path(output_dir: str | Path, input_path: str | Path) -> Path:
    directory = Path(output_dir)
    stem = Path(input_path).stem or "output"
    first = directory / f"{stem}_5MB.gif"
    if not first.exists():
        return first
    counter = 2
    while True:
        candidate = directory / f"{stem}_5MB_{counter}.gif"
        if not candidate.exists():
            return candidate
        counter += 1


class ConversionEngine:
    """Windows-friendly adaptive GIF converter."""

    _LOSSINESS = {"quality": 0, "balanced": 5, "resolution": 8}

    def __init__(
        self,
        ffmpeg_path: str | Path,
        ffprobe_path: str | Path,
        gifsicle_path: str | Path,
        progress_callback: ProgressCallback | None = None,
        cancel_event: Any | None = None,
    ) -> None:
        self.ffmpeg_path = str(ffmpeg_path)
        self.ffprobe_path = str(ffprobe_path)
        self.gifsicle_path = str(gifsicle_path)
        self.progress_callback = progress_callback
        self.cancel_event = cancel_event

    def _emit(
        self,
        stage: str,
        progress: float | None,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if progress is not None:
            progress = max(0.0, min(1.0, float(progress)))
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(stage, progress, message, dict(details or {}))
        except Exception:
            # A UI callback must not corrupt or strand a conversion process.
            return

    def _is_cancelled(self) -> bool:
        event = self.cancel_event
        if event is None:
            return False
        checker = getattr(event, "is_set", None)
        if callable(checker):
            return bool(checker())
        return bool(event)

    def _check_cancelled(self) -> None:
        if self._is_cancelled():
            raise CancelledError("Conversion was cancelled")

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1.5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _run(
        self,
        argv: Sequence[str | Path],
        *,
        stage: str,
        duration: float = 0.0,
        message: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> _CommandResult:
        self._check_cancelled()
        command = [str(argument) for argument in argv]
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise ConversionError(f"Could not start {command[0]}: {exc}") from exc

        output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def pump(name: str, stream: Any) -> None:
            try:
                for line in iter(stream.readline, ""):
                    output_queue.put((name, line))
            finally:
                try:
                    stream.close()
                finally:
                    output_queue.put((name, None))

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=pump, args=("stdout", process.stdout), daemon=True
        )
        stderr_thread = threading.Thread(
            target=pump, args=("stderr", process.stderr), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        closed_streams = 0
        try:
            while closed_streams < 2 or process.poll() is None or not output_queue.empty():
                if self._is_cancelled():
                    self._stop_process(process)
                    raise CancelledError("Conversion was cancelled")
                try:
                    stream_name, line = output_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    closed_streams += 1
                    continue
                if stream_name == "stdout":
                    stdout_lines.append(line)
                    out_time = _parse_ffmpeg_out_time(line.strip())
                    if out_time is not None:
                        progress = out_time / duration if duration > 0 else None
                        self._emit(stage, progress, message, details)
                    elif line.strip() == "progress=end":
                        self._emit(stage, 1.0, message, details)
                else:
                    stderr_lines.append(line)
            returncode = process.wait()
        except BaseException:
            self._stop_process(process)
            raise
        finally:
            stdout_thread.join(timeout=0.5)
            stderr_thread.join(timeout=0.5)

        result = _CommandResult(
            returncode=returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )
        if returncode != 0:
            raise _ProcessError(command, result)
        return result

    def _probe_media(self, path: Path) -> ProbeInfo:
        result = self._run(
            [
                self.ffprobe_path,
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                (
                    "stream=width,height,avg_frame_rate,r_frame_rate,duration,"
                    "nb_frames,nb_read_frames:stream_tags=rotate:"
                    "stream_side_data=rotation:format=duration"
                ),
                "-of",
                "json",
                "--",
                str(path),
            ],
            stage="probe",
            message="Reading video metadata",
            details={"path": str(path)},
        )
        return _parse_probe_json(result.stdout, path)

    def probe(self, path: str | Path) -> ProbeInfo:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ConversionError(f"Input video does not exist: {source}")
        self._emit("probe", 0.0, "Reading video metadata", {"path": str(source)})
        info = self._probe_media(source)
        self._emit(
            "probe",
            1.0,
            "Video metadata ready",
            {
                "path": str(source),
                "width": info.width,
                "height": info.height,
                "frames": info.frame_count,
                "duration": info.duration,
            },
        )
        return info

    @staticmethod
    def _filter_graph(width: int, height: int, *, srgb: bool) -> str:
        scale = (
            f"scale=w={width}:h={height}:"
            "flags=lanczos+accurate_rnd:in_range=auto:out_range=full"
        )
        if srgb:
            colour = (
                f"{scale},zscale=matrix=gbr:transfer=iec61966-2-1:"
                "primaries=bt709:range=full,format=gbrp"
            )
        else:
            colour = f"{scale},format=rgb24"
        return (
            f"[0:v:0]{colour},split=2[v][p0];"
            "[p0]palettegen=max_colors=256:reserve_transparent=1:"
            "stats_mode=full[p];"
            "[v][p]paletteuse=dither=floyd_steinberg:"
            "diff_mode=rectangle[out]"
        )

    def _encode_raw_gif(
        self,
        source: Path,
        info: ProbeInfo,
        requested_width: int,
        raw_path: Path,
        attempt: int,
    ) -> tuple[int, int]:
        exact_width, exact_height = _dimensions_for_width(info, requested_width)
        variants: list[tuple[str, int, int, bool]] = [
            ("sRGB", exact_width, exact_height, True),
            ("RGB fallback", exact_width, exact_height, False),
        ]
        if exact_height % 2:
            even_width, even_height = _dimensions_for_width(
                info, requested_width, even_height=True
            )
            variants.append(("even-height fallback", even_width, even_height, False))

        errors: list[str] = []
        for variant_index, (label, width, height, srgb) in enumerate(variants):
            self._check_cancelled()
            raw_path.unlink(missing_ok=True)
            details = {
                "attempt": attempt,
                "width": width,
                "height": height,
                "color_pipeline": label,
            }
            if variant_index:
                self._emit(
                    "encode",
                    None,
                    f"Retrying {width}x{height} with {label}",
                    details,
                )
            command = [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-progress",
                "pipe:1",
                "-i",
                str(source),
                "-filter_complex",
                self._filter_graph(width, height, srgb=srgb),
                "-map",
                "[out]",
                "-an",
                "-map_metadata",
                "-1",
                "-fps_mode",
                "passthrough",
                "-gifflags",
                "+offsetting+transdiff",
                "-loop",
                "0",
                str(raw_path),
            ]
            try:
                self._run(
                    command,
                    stage="encode",
                    duration=info.duration,
                    message=f"Encoding {width}x{height} candidate",
                    details=details,
                )
            except _ProcessError as exc:
                errors.append(str(exc))
                continue
            if raw_path.is_file() and raw_path.stat().st_size > 0:
                return width, height
            errors.append(f"{label} produced no GIF data")

        diagnostic = errors[-1] if errors else "unknown FFmpeg error"
        raise ConversionError(
            f"FFmpeg could not encode width {requested_width}: {diagnostic}"
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
        raw_path = temp_dir / f"candidate-{width}.raw.gif"
        optimized_path = temp_dir / f"candidate-{width}.gif"
        optimized_path.unlink(missing_ok=True)
        actual_width, actual_height = self._encode_raw_gif(
            source, info, width, raw_path, attempt
        )

        raw_info = self._probe_media(raw_path)
        if info.frame_count and raw_info.frame_count != info.frame_count:
            raise ConversionError(
                "FFmpeg did not preserve all source frames "
                f"({raw_info.frame_count} of {info.frame_count})"
            )

        lossiness = self._LOSSINESS[settings.quality_mode]
        gifsicle_command = [
            self.gifsicle_path,
            "-O3",
            "-Okeep-empty",
            "--careful",
        ]
        if lossiness:
            gifsicle_command.append(f"--lossy={lossiness}")
        gifsicle_command.extend([str(raw_path), "-o", str(optimized_path)])
        self._emit(
            "optimize",
            None,
            f"Optimizing {actual_width}x{actual_height} candidate",
            {
                "attempt": attempt,
                "width": actual_width,
                "height": actual_height,
                "lossiness": lossiness,
            },
        )
        self._run(
            gifsicle_command,
            stage="optimize",
            message="Optimizing GIF",
            details={"attempt": attempt, "width": actual_width},
        )
        if not optimized_path.is_file() or optimized_path.stat().st_size <= 0:
            raise ConversionError("gifsicle produced no output")

        optimized_info = self._probe_media(optimized_path)
        if raw_info.frame_count and optimized_info.frame_count != raw_info.frame_count:
            raise ConversionError(
                "gifsicle changed the logical frame count "
                f"({optimized_info.frame_count} of {raw_info.frame_count})"
            )
        if optimized_info.width != actual_width or optimized_info.height != actual_height:
            raise ConversionError(
                "gifsicle changed the candidate dimensions unexpectedly"
            )

        size_bytes = optimized_path.stat().st_size
        raw_path.unlink(missing_ok=True)
        self._emit(
            "search",
            None,
            f"{actual_width}x{actual_height}: {size_bytes:,} bytes",
            {
                "attempt": attempt,
                "width": actual_width,
                "height": actual_height,
                "size_bytes": size_bytes,
                "limit_bytes": settings.limit_bytes,
            },
        )
        return _Candidate(
            width=width,
            height=actual_height,
            size_bytes=size_bytes,
            frames=optimized_info.frame_count,
            duration=optimized_info.duration,
            path=optimized_path,
        )

    def _validate_candidate(
        self,
        candidate: _Candidate,
        source_info: ProbeInfo,
        settings: ConversionSettings,
    ) -> _Candidate:
        self._check_cancelled()
        self._emit(
            "validate",
            0.0,
            "Validating the final GIF",
            {"path": str(candidate.path)},
        )
        try:
            with candidate.path.open("rb") as handle:
                header = handle.read(6)
        except OSError as exc:
            raise ConversionError(f"Could not read the final GIF: {exc}") from exc
        if header not in {b"GIF87a", b"GIF89a"}:
            raise ConversionError("The final file does not have a valid GIF header")

        actual_size = candidate.path.stat().st_size
        if actual_size > settings.limit_bytes:
            raise ConversionError(
                f"Final GIF exceeds the hard limit: {actual_size:,} > "
                f"{settings.limit_bytes:,} bytes"
            )

        final_info = self._probe_media(candidate.path)
        if final_info.width != candidate.width or final_info.height != candidate.height:
            raise ConversionError("Final GIF dimensions do not match the selected candidate")
        if source_info.frame_count and final_info.frame_count != source_info.frame_count:
            raise ConversionError(
                "Final GIF frame count does not match the source "
                f"({final_info.frame_count} of {source_info.frame_count})"
            )
        if source_info.duration and final_info.duration:
            tolerance = max(0.06, source_info.duration * 0.01)
            if abs(final_info.duration - source_info.duration) > tolerance:
                raise ConversionError(
                    "Final GIF duration differs from the source by more than "
                    f"{tolerance:.3f} seconds"
                )

        gifsicle_info_result = self._run(
            [self.gifsicle_path, "--info", str(candidate.path)],
            stage="validate",
            message="Checking GIF animation metadata",
            details={"path": str(candidate.path)},
        )
        parsed_info = _parse_gifsicle_info(
            gifsicle_info_result.stdout + "\n" + gifsicle_info_result.stderr
        )
        if not parsed_info.loop_forever:
            raise ConversionError("Final GIF is not configured to loop forever")
        if parsed_info.frames is not None and parsed_info.frames != final_info.frame_count:
            raise ConversionError("GIF logical image count is inconsistent")

        self._run(
            [
                self.ffmpeg_path,
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(candidate.path),
                "-map",
                "0:v:0",
                "-f",
                "null",
                os.devnull,
            ],
            stage="validate",
            duration=final_info.duration,
            message="Fully decoding the final GIF",
            details={"path": str(candidate.path)},
        )
        self._emit(
            "validate",
            1.0,
            "Final GIF passed validation",
            {
                "frames": final_info.frame_count,
                "duration": final_info.duration,
                "size_bytes": actual_size,
            },
        )
        return _Candidate(
            width=final_info.width,
            height=final_info.height,
            size_bytes=actual_size,
            frames=final_info.frame_count,
            duration=final_info.duration,
            path=candidate.path,
        )

    @staticmethod
    def _publish(candidate_path: Path, output_dir: Path, input_path: Path) -> Path:
        # The task temp directory is created inside output_dir, so rename is an
        # atomic same-volume operation on Windows.  Windows rename refuses to
        # replace an existing destination, preserving every prior result.
        for _ in range(10_000):
            destination = _unique_output_path(output_dir, input_path)
            try:
                os.rename(candidate_path, destination)
                return destination
            except FileExistsError:
                continue
            except OSError as exc:
                raise ConversionError(f"Could not publish the final GIF: {exc}") from exc
        raise ConversionError("Could not allocate a unique output filename")

    def convert(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        settings: ConversionSettings,
    ) -> ConversionResult:
        self._check_cancelled()
        if not isinstance(settings, ConversionSettings):
            raise TypeError("settings must be a ConversionSettings instance")
        source = Path(input_path).expanduser().resolve()
        if not source.is_file():
            raise ConversionError(f"Input video does not exist: {source}")
        destination_dir = Path(output_dir).expanduser().resolve()
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConversionError(f"Could not create output directory: {exc}") from exc

        info = self.probe(source)
        display_width, _ = _display_dimensions(info)
        minimum_width = min(settings.min_width, display_width)
        widths = _build_width_ladder(minimum_width, display_width, step=16)
        safe_limit = max(1, int(settings.limit_bytes * settings.safe_ratio))
        self._emit(
            "search",
            0.0,
            "Starting adaptive size search",
            {
                "min_width": minimum_width,
                "max_width": display_width,
                "target_bytes": safe_limit,
                "hard_limit_bytes": settings.limit_bytes,
            },
        )

        attempts = 0
        try:
            temp_context = tempfile.TemporaryDirectory(
                prefix=".giffit-", dir=str(destination_dir)
            )
        except OSError as exc:
            raise ConversionError(f"Could not create conversion workspace: {exc}") from exc

        with temp_context as temp_name:
            temp_dir = Path(temp_name)

            def evaluate(width: int) -> _Candidate:
                nonlocal attempts
                self._check_cancelled()
                attempts += 1
                self._emit(
                    "search",
                    None,
                    f"Testing width {width}px",
                    {"attempt": attempts, "width": width},
                )
                return self._build_candidate(
                    source, info, width, temp_dir, settings, attempts
                )

            try:
                outcome = _adaptive_search(
                    widths,
                    evaluate,
                    target_limit=safe_limit,
                    hard_limit=settings.limit_bytes,
                )
            except _NoFitError as exc:
                raise ConversionError(
                    "The video cannot fit within "
                    f"{settings.limit_bytes:,} bytes while preserving every frame "
                    f"at the minimum width of {minimum_width}px. "
                    f"The minimum candidate was {exc.smallest_size:,} bytes."
                ) from exc

            selected = self._validate_candidate(outcome.best, info, settings)
            self._check_cancelled()
            output_path = self._publish(selected.path, destination_dir, source)
            result = ConversionResult(
                input_path=source,
                output_path=output_path,
                width=selected.width,
                height=selected.height,
                size_bytes=selected.size_bytes,
                frames=selected.frames,
                duration=selected.duration,
                attempts=attempts,
            )

        self._emit(
            "complete",
            1.0,
            "GIF conversion complete",
            {
                "input_path": str(result.input_path),
                "output_path": str(result.output_path),
                "width": result.width,
                "height": result.height,
                "size_bytes": result.size_bytes,
                "frames": result.frames,
                "duration": result.duration,
                "attempts": result.attempts,
            },
        )
        return result


__all__ = [
    "CancelledError",
    "ConversionEngine",
    "ConversionError",
    "ConversionResult",
    "ConversionSettings",
    "ProbeInfo",
]
