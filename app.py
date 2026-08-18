"""GIFFit desktop application.

A compact, local-only Windows GUI for converting video files to GIF files under
a strict byte limit.  The Tk event loop is never used for media work: probing
and conversion run on daemon worker threads and communicate through a queue.

The source module also exposes a small JSON CLI for integration diagnostics.
The distributed Windows executable is intentionally a windowed GUI build.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:
    from converter import (
        CancelledError,
        ConversionEngine,
        ConversionError,
        ConversionSettings,
        ProbeInfo,
    )
except ImportError:  # pragma: no cover - supports package-style execution
    from .converter import (  # type: ignore[no-redef]
        CancelledError,
        ConversionEngine,
        ConversionError,
        ConversionSettings,
        ProbeInfo,
    )


APP_NAME = "GIFFit"
DEFAULT_LIMIT_BYTES = 5_000_000
SAFE_MARGIN_RATIO = 0.98
VIDEO_TYPES = (
    ("지원 영상", "*.mp4 *.mov *.m4v *.avi *.mkv *.webm *.wmv"),
    ("MP4 영상", "*.mp4"),
    ("모든 파일", "*.*"),
)
QUALITY_LABELS: Mapping[str, str] = {
    "균형 · 선명도와 화면 크기 조화 (balanced)": "balanced",
    "화질 우선 · 손실 최소화 (quality)": "quality",
    "해상도 우선 · 더 큰 화면 크기 (resolution)": "resolution",
}
QUALITY_TO_LABEL = {value: label for label, value in QUALITY_LABELS.items()}
STAGE_LABELS: Mapping[str, str] = {
    "probe": "영상 정보 분석",
    "analyze": "영상 정보 분석",
    "analysis": "영상 정보 분석",
    "prepare": "변환 준비",
    "palette": "색상 팔레트 최적화",
    "encode": "GIF 생성",
    "encoding": "GIF 생성",
    "optimize": "용량 최적화",
    "optimise": "용량 최적화",
    "search": "최대 해상도 탐색",
    "verify": "파일 크기 검증",
    "validate": "파일 크기 검증",
    "complete": "완료",
    "done": "완료",
}


class MissingToolsError(RuntimeError):
    """Raised when the bundled/system media tools cannot be resolved."""


@dataclass(frozen=True)
class ToolPaths:
    ffmpeg: Path
    ffprobe: Path
    gifsicle: Path


@dataclass
class FileItem:
    key: str
    path: Path
    state: str = "waiting"
    probe: Any | None = None
    output_path: Path | None = None
    result: Any | None = None
    error: str = ""
    status: str = "분석 대기"
    progress: float = 0.0


def _base_tool_dirs() -> list[Path]:
    """Return tool directories in the required bundle/app lookup order."""

    directories: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        directories.append(Path(bundle_root) / "tools")

    if getattr(sys, "frozen", False):
        directories.append(Path(sys.executable).resolve().parent / "tools")
    else:
        directories.append(Path(__file__).resolve().parent / "tools")

    # Keep the order stable while avoiding duplicate lookups in source builds.
    unique: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        marker = os.path.normcase(str(directory.resolve()))
        if marker not in seen:
            seen.add(marker)
            unique.append(directory)
    return unique


def resolve_tool(name: str) -> Path | None:
    """Resolve a bundled executable, falling back to the process PATH."""

    executable_names = [f"{name}.exe", name] if os.name == "nt" else [name, f"{name}.exe"]
    for directory in _base_tool_dirs():
        for executable_name in executable_names:
            candidate = directory / executable_name
            if candidate.is_file():
                return candidate.resolve()

    found = shutil.which(name)
    return Path(found).resolve() if found else None


def resolve_tools() -> ToolPaths:
    resolved = {
        "ffmpeg": resolve_tool("ffmpeg"),
        "ffprobe": resolve_tool("ffprobe"),
        "gifsicle": resolve_tool("gifsicle"),
    }
    missing = [name for name, path in resolved.items() if path is None]
    if missing:
        display = ", ".join(missing)
        raise MissingToolsError(
            f"필수 변환 도구를 찾을 수 없습니다: {display}. "
            "앱의 tools 폴더에 실행 파일을 넣거나 시스템 PATH에 추가해 주세요."
        )
    return ToolPaths(
        ffmpeg=resolved["ffmpeg"],  # type: ignore[arg-type]
        ffprobe=resolved["ffprobe"],  # type: ignore[arg-type]
        gifsicle=resolved["gifsicle"],  # type: ignore[arg-type]
    )


def create_engine(
    tools: ToolPaths,
    *,
    progress_callback: Callable[..., None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ConversionEngine:
    """Create an engine using the converter module's public API contract."""

    return ConversionEngine(
        ffmpeg_path=tools.ffmpeg,
        ffprobe_path=tools.ffprobe,
        gifsicle_path=tools.gifsicle,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )


def make_settings(
    limit_bytes: int,
    quality: str,
    *,
    safe_margin: bool,
    output_dir: Path | None = None,
) -> ConversionSettings:
    """Build ConversionSettings from the public semantic options."""

    try:
        return ConversionSettings(
            limit_bytes=int(limit_bytes),
            safe_ratio=SAFE_MARGIN_RATIO if safe_margin else 1.0,
            quality_mode=quality,
        )
    except (TypeError, ValueError) as exc:
        raise ConversionError(f"변환 설정을 만들 수 없습니다: {exc}") from exc


def engine_probe(engine: ConversionEngine, input_path: Path, cancel_event: threading.Event | None = None) -> ProbeInfo:
    if cancel_event is not None:
        engine.cancel_event = cancel_event
    return engine.probe(input_path)


def engine_convert(
    engine: ConversionEngine,
    input_path: Path,
    output_path: Path,
    settings: ConversionSettings,
    progress_callback: Callable[..., None],
    cancel_event: threading.Event,
) -> Any:
    engine.progress_callback = progress_callback
    engine.cancel_event = cancel_event
    return engine.convert(input_path, output_path.parent, settings)


def value_from(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def serialise_result(obj: Any) -> Any:
    if obj is None:
        return None
    if dataclasses.is_dataclass(obj):
        return {key: serialise_result(value) for key, value in dataclasses.asdict(obj).items()}
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Mapping):
        return {str(key): serialise_result(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialise_result(value) for value in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "__dict__"):
        return {
            key: serialise_result(value)
            for key, value in vars(obj).items()
            if not key.startswith("_")
        }
    return str(obj)


def parse_progress(args: Sequence[Any], kwargs: Mapping[str, Any]) -> tuple[str, float, str]:
    """Normalise common converter callback forms to stage/fraction/message."""

    stage: Any = kwargs.get("stage", "")
    fraction: Any = kwargs.get("progress", kwargs.get("fraction", kwargs.get("percent", 0.0)))
    message: Any = kwargs.get("message", kwargs.get("detail", ""))
    details: Any = kwargs.get("details", {})

    if len(args) == 1 and not isinstance(args[0], (str, int, float)):
        event = args[0]
        stage = value_from(event, "stage", "phase", default=stage)
        fraction = value_from(event, "progress", "fraction", "percent", default=fraction)
        message = value_from(event, "message", "detail", "status", default=message)
        details = value_from(event, "details", "data", default=details)
    else:
        if len(args) >= 1:
            stage = args[0]
        if len(args) >= 2:
            fraction = args[1]
        if len(args) >= 3:
            message = args[2]
        if len(args) >= 4:
            details = args[3]

    try:
        numeric_fraction = float(fraction)
    except (TypeError, ValueError):
        numeric_fraction = 0.0
    if numeric_fraction > 1.0:
        numeric_fraction /= 100.0
    numeric_fraction = max(0.0, min(1.0, numeric_fraction))
    raw_stage = str(stage or "").lower()
    # Converter progress is local to a stage (and may restart for each tested
    # resolution).  Translate it into a coarse per-file scale; the UI also
    # keeps this value monotonic so iterative searches never look stalled.
    if raw_stage in {"probe", "analyze", "analysis"}:
        numeric_fraction = 0.02 + numeric_fraction * 0.04
    elif raw_stage == "search":
        numeric_fraction = 0.08
    elif raw_stage in {"palette", "encode", "encoding"}:
        numeric_fraction = 0.14 + numeric_fraction * 0.56
    elif raw_stage in {"optimize", "optimise"}:
        numeric_fraction = 0.76
    elif raw_stage in {"verify", "validate"}:
        numeric_fraction = 0.88 + numeric_fraction * 0.10
    elif raw_stage in {"complete", "done"}:
        numeric_fraction = 1.0
    display_stage = STAGE_LABELS.get(raw_stage, str(stage or "변환 중"))
    display_message = ""
    if isinstance(details, Mapping):
        width = details.get("width")
        height = details.get("height")
        attempt = details.get("attempt")
        size_bytes = details.get("size_bytes")
        if raw_stage == "search" and width:
            display_message = f"{width}px 후보 확인 중"
            if attempt:
                display_message += f" · {attempt}차"
        elif raw_stage in {"encode", "encoding", "palette"} and width and height:
            display_message = f"{width}×{height} 처리 중"
        elif raw_stage in {"optimize", "optimise"} and width and height:
            display_message = f"{width}×{height} 압축 중"
        elif raw_stage in {"verify", "validate"} and size_bytes:
            display_message = format_bytes(size_bytes)
    return display_stage, numeric_fraction, display_message


def unique_output_path(output_dir: Path, input_path: Path) -> Path:
    candidate = output_dir / f"{input_path.stem}_5MB.gif"
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{input_path.stem}_5MB_{index}.gif"
        index += 1
    return candidate


def format_bytes(size: int | float | None) -> str:
    if size is None:
        return "—"
    numeric = float(size)
    if numeric >= 1_000_000:
        return f"{numeric / 1_000_000:.2f}MB"
    if numeric >= 1_000:
        return f"{numeric / 1_000:.0f}KB"
    return f"{int(numeric)}B"


def format_duration(seconds: Any) -> str:
    try:
        numeric = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if numeric >= 60:
        minutes, remainder = divmod(numeric, 60)
        return f"{int(minutes)}:{remainder:04.1f}"
    return f"{numeric:.2f}초"


def probe_summary(probe: Any) -> str:
    width = value_from(probe, "width", default="—")
    height = value_from(probe, "height", default="—")
    fps = value_from(probe, "fps", "frame_rate", default=None)
    frames = value_from(probe, "frame_count", "frames", "nb_frames", default=None)
    duration = value_from(probe, "duration", "duration_seconds", default=None)
    pieces = [format_duration(duration), f"{width}×{height}"]
    if fps is not None:
        try:
            pieces.append(f"{float(fps):.2f}fps")
        except (TypeError, ValueError):
            pieces.append(f"{fps}fps")
    if frames is not None:
        pieces.append(f"{frames}프레임")
    return " · ".join(pieces)


def result_summary(result: Any, output_path: Path) -> str:
    width = value_from(result, "width", "output_width", default=None)
    height = value_from(result, "height", "output_height", default=None)
    frames = value_from(result, "frame_count", "frames", "output_frames", default=None)
    size = value_from(result, "size_bytes", "output_size", "file_size", default=None)
    if size is None and output_path.exists():
        size = output_path.stat().st_size
    pieces: list[str] = []
    if width and height:
        pieces.append(f"{width}×{height}")
    if frames is not None:
        pieces.append(f"{frames}프레임")
    pieces.append(format_bytes(size))
    return " · ".join(pieces)


def open_path(path: Path) -> None:
    path = path.resolve()
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _set_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class GifFitApp:
    """Tk application controller."""

    BG = "#F3F2ED"
    PANEL = "#FFFFFF"
    INK = "#17221F"
    MUTED = "#697570"
    LINE = "#DDE2DC"
    ACCENT = "#087965"
    ACCENT_DARK = "#075E50"
    ACCENT_SOFT = "#E2F2EC"
    WARN = "#A96112"
    ERROR = "#A83931"
    SELECT = "#EAF5F1"

    def __init__(self, root: Any) -> None:
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.root.title(f"{APP_NAME} · 5MB GIF 변환기")
        self.root.geometry("1040x720")
        self.root.minsize(860, 600)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        available_fonts = set(tkfont.families(root))
        self.font_family = next(
            (name for name in ("SUIT Variable", "Pretendard", "Segoe UI Variable", "맑은 고딕") if name in available_fonts),
            "TkDefaultFont",
        )
        self.mono_family = "Cascadia Mono" if "Cascadia Mono" in available_fonts else self.font_family

        self.items: dict[str, FileItem] = {}
        self.path_keys: dict[str, str] = {}
        self.ui_events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.probe_threads: set[threading.Thread] = set()
        self.busy = False
        self.closing = False
        self.shutdown_deadline = 0.0
        self.tools: ToolPaths | None = None
        self.engine: ConversionEngine | None = None
        self.tools_error = ""

        self.limit_var = tk.StringVar(value="5.00")
        self.safe_margin_var = tk.BooleanVar(value=True)
        self.quality_var = tk.StringVar(value=QUALITY_TO_LABEL["quality"])
        self.output_var = tk.StringVar(value=str(Path.home() / "Downloads" / "GIFFit"))
        self.status_var = tk.StringVar(value="영상을 추가하면 파일 정보를 먼저 확인합니다.")
        self.summary_var = tk.StringVar(value="추가된 영상 없음")
        self.target_var = tk.StringVar(value="실제 목표 4.90MB · 상한 5.00MB")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._configure_styles()
        self._build_ui()
        self._bind_shortcuts()
        self.limit_var.trace_add("write", self._on_limit_changed)
        self.safe_margin_var.trace_add("write", self._on_limit_changed)
        self.root.after(80, self._drain_events)
        self.root.after(20, self._initialise_engine_async)

    def _configure_styles(self) -> None:
        style = self.ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure(
            "Treeview",
            background=self.PANEL,
            fieldbackground=self.PANEL,
            foreground=self.INK,
            rowheight=52,
            borderwidth=0,
            relief="flat",
            font=(self.font_family, 10),
        )
        style.map("Treeview", background=[("selected", self.SELECT)], foreground=[("selected", self.INK)])
        style.configure(
            "Treeview.Heading",
            background="#F7F8F5",
            foreground=self.MUTED,
            borderwidth=0,
            relief="flat",
            padding=(10, 10),
            font=(self.font_family, 9, "bold"),
        )
        style.map("Treeview.Heading", background=[("active", "#F7F8F5")])
        style.configure(
            "Warm.Horizontal.TProgressbar",
            troughcolor="#DCE3DE",
            background=self.ACCENT,
            bordercolor="#DCE3DE",
            lightcolor=self.ACCENT,
            darkcolor=self.ACCENT,
            thickness=8,
        )
        style.configure(
            "TCombobox",
            padding=8,
            fieldbackground=self.PANEL,
            background=self.PANEL,
            foreground=self.INK,
            arrowcolor=self.INK,
            bordercolor=self.LINE,
            lightcolor=self.LINE,
            darkcolor=self.LINE,
            font=(self.font_family, 9),
        )
        style.map("TCombobox", fieldbackground=[("readonly", self.PANEL)], foreground=[("readonly", self.INK)])
        style.configure(
            "TCheckbutton",
            background=self.PANEL,
            foreground=self.INK,
            font=(self.font_family, 9),
        )
        style.map("TCheckbutton", background=[("active", self.PANEL)])

    def _build_ui(self) -> None:
        tk = self.tk

        outer = tk.Frame(self.root, bg=self.BG)
        outer.pack(fill="both", expand=True, padx=24, pady=(20, 18))

        header = tk.Frame(outer, bg=self.BG, height=58)
        header.pack(fill="x", pady=(0, 16))
        header.pack_propagate(False)

        title_block = tk.Frame(header, bg=self.BG)
        title_block.pack(side="left", fill="y")
        tk.Label(
            title_block,
            text=APP_NAME,
            bg=self.BG,
            fg=self.INK,
            font=(self.font_family, 24, "bold"),
            anchor="w",
        ).pack(side="left", anchor="s")
        tk.Label(
            title_block,
            text="5MB 안에서 가장 선명하게",
            bg=self.BG,
            fg=self.MUTED,
            font=(self.font_family, 11),
            padx=12,
        ).pack(side="left", anchor="s", pady=(0, 5))

        badge = tk.Frame(header, bg=self.ACCENT_SOFT, highlightthickness=1, highlightbackground="#B8DBCF")
        badge.pack(side="right", anchor="n", pady=5)
        tk.Label(
            badge,
            text="●  로컬 처리 · 외부 업로드 없음",
            bg=self.ACCENT_SOFT,
            fg=self.ACCENT_DARK,
            font=(self.font_family, 9, "bold"),
            padx=12,
            pady=7,
        ).pack()

        add_panel = tk.Frame(
            outer,
            bg=self.PANEL,
            height=98,
            highlightthickness=1,
            highlightbackground=self.LINE,
            cursor="hand2",
        )
        add_panel.pack(fill="x", pady=(0, 14))
        add_panel.pack_propagate(False)
        add_panel.bind("<Button-1>", lambda _event: self.add_files())

        add_copy = tk.Frame(add_panel, bg=self.PANEL)
        add_copy.pack(side="left", fill="y", padx=22)
        add_title = tk.Label(
            add_copy,
            text="영상을 추가하세요",
            bg=self.PANEL,
            fg=self.INK,
            font=(self.font_family, 13, "bold"),
            anchor="w",
        )
        add_title.pack(anchor="w", pady=(19, 0))
        add_hint = tk.Label(
            add_copy,
            text="MP4, MOV, AVI, MKV, WebM · 여러 파일을 한 번에 선택할 수 있어요",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 9),
            anchor="w",
        )
        add_hint.pack(anchor="w", pady=(3, 0))
        for widget in (add_title, add_hint):
            widget.bind("<Button-1>", lambda _event: self.add_files())

        self.add_button = self._button(
            add_panel,
            "＋  영상 선택",
            self.add_files,
            primary=True,
            width=14,
        )
        self.add_button.pack(side="right", padx=22)

        # Reserve the action footer before the expanding content area.  Tk's
        # packer allocates parcels in packing order; placing an oversized
        # content request first can otherwise leave the start/cancel controls
        # unmapped on the default 720px-high window.
        footer = tk.Frame(outer, bg=self.BG)
        footer.pack(side="bottom", fill="x", pady=(14, 0))

        content = tk.Frame(outer, bg=self.BG)
        content.pack(fill="both", expand=True)
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, minsize=292)
        content.grid_rowconfigure(0, weight=1)

        list_panel = tk.Frame(content, bg=self.PANEL, highlightthickness=1, highlightbackground=self.LINE)
        list_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 14))

        list_header = tk.Frame(list_panel, bg=self.PANEL, height=48)
        list_header.pack(fill="x")
        list_header.pack_propagate(False)
        tk.Label(
            list_header,
            text="변환 목록",
            bg=self.PANEL,
            fg=self.INK,
            font=(self.font_family, 11, "bold"),
        ).pack(side="left", padx=16)
        tk.Label(
            list_header,
            textvariable=self.summary_var,
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 9),
        ).pack(side="left")

        self.clear_button = self._button(list_header, "전체 비우기", self.clear_files, compact=True)
        self.clear_button.pack(side="right", padx=(4, 12), pady=9)
        self.remove_button = self._button(list_header, "선택 제거", self.remove_selected, compact=True)
        self.remove_button.pack(side="right", padx=4, pady=9)

        tree_frame = tk.Frame(list_panel, bg=self.PANEL)
        tree_frame.pack(fill="both", expand=True)
        columns = ("name", "source", "result", "size", "status")
        self.tree = self.ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "name": "파일",
            "source": "원본",
            "result": "결과",
            "size": "용량",
            "status": "상태",
        }
        widths = {"name": 188, "source": 150, "result": 150, "size": 82, "status": 120}
        anchors = {"name": "w", "source": "w", "result": "w", "size": "e", "status": "w"}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=70 if column != "name" else 140,
                anchor=anchors[column],
                stretch=column in {"name", "source", "result", "status"},
            )
        self.tree_scrollbar = self.ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=self.tree_scrollbar.set)
        self.tree_scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("done", foreground=self.ACCENT_DARK)
        self.tree.tag_configure("failed", foreground=self.ERROR)
        self.tree.tag_configure("active", background="#F0F8F5")
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_controls())

        settings_panel = tk.Frame(
            content,
            bg=self.PANEL,
            highlightthickness=1,
            highlightbackground=self.LINE,
        )
        settings_panel.grid(row=0, column=1, sticky="nsew")
        tk.Label(
            settings_panel,
            text="변환 설정",
            bg=self.PANEL,
            fg=self.INK,
            font=(self.font_family, 12, "bold"),
        ).pack(anchor="w", padx=18, pady=(17, 14))

        settings_body = tk.Frame(settings_panel, bg=self.PANEL)
        settings_body.pack(fill="both", expand=True)
        self.settings_scrollbar = self.ttk.Scrollbar(settings_body, orient="vertical")
        self.settings_scrollbar.pack(side="right", fill="y")
        self.settings_canvas = tk.Canvas(
            settings_body,
            bg=self.PANEL,
            width=272,
            bd=0,
            highlightthickness=0,
            takefocus=True,
            yscrollcommand=self.settings_scrollbar.set,
        )
        self.settings_canvas.pack(side="left", fill="both", expand=True)
        self.settings_scrollbar.configure(command=self.settings_canvas.yview)
        settings = tk.Frame(self.settings_canvas, bg=self.PANEL)
        self.settings_inner = settings
        self.settings_window = self.settings_canvas.create_window(
            (0, 0), window=settings, anchor="nw"
        )
        settings.bind(
            "<Configure>",
            lambda _event: self.settings_canvas.configure(
                scrollregion=self.settings_canvas.bbox("all")
            ),
        )
        self.settings_canvas.bind(
            "<Configure>",
            lambda event: self.settings_canvas.itemconfigure(
                self.settings_window, width=event.width
            ),
        )
        self.settings_canvas.bind("<Prior>", lambda event: self._scroll_settings(event, -1, "pages"))
        self.settings_canvas.bind("<Next>", lambda event: self._scroll_settings(event, 1, "pages"))
        self.settings_canvas.bind("<Home>", lambda event: self._scroll_settings_to(event, 0.0))
        self.settings_canvas.bind("<End>", lambda event: self._scroll_settings_to(event, 1.0))
        self.root.bind("<MouseWheel>", self._on_settings_mousewheel, add="+")

        self._field_label(settings, "파일당 최대 용량")
        size_row = tk.Frame(settings, bg=self.PANEL)
        size_row.pack(fill="x", padx=18)
        self.limit_entry = tk.Entry(
            size_row,
            textvariable=self.limit_var,
            bg="#FAFBF9",
            fg=self.INK,
            insertbackground=self.INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.LINE,
            highlightcolor=self.ACCENT,
            font=(self.mono_family, 11, "bold"),
            justify="right",
        )
        self.limit_entry.pack(side="left", fill="x", expand=True, ipady=8)
        tk.Label(size_row, text="MB", bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 10)).pack(
            side="left", padx=(8, 0)
        )

        self.safe_check = self.ttk.Checkbutton(
            settings,
            text="업로드 호환 안전 여유 2%",
            variable=self.safe_margin_var,
        )
        self.safe_check.pack(anchor="w", padx=18, pady=(9, 1))
        self.target_label = tk.Label(
            settings,
            textvariable=self.target_var,
            bg=self.PANEL,
            fg=self.ACCENT_DARK,
            font=(self.font_family, 8, "bold"),
        )
        self.target_label.pack(anchor="w", padx=20, pady=(0, 15))

        self._separator(settings)
        self._field_label(settings, "품질 기준", top=14)
        self.quality_combo = self.ttk.Combobox(
            settings,
            textvariable=self.quality_var,
            values=list(QUALITY_LABELS),
            state="readonly",
        )
        self.quality_combo.pack(fill="x", padx=18)
        tk.Label(
            settings,
            text="모든 모드가 프레임 수와 재생 길이를 유지합니다. 모드에 따라 색상 손실과 해상도 비중만 달라져요.",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 8),
            justify="left",
            wraplength=246,
        ).pack(anchor="w", padx=19, pady=(7, 15))

        self._separator(settings)
        self._field_label(settings, "저장 위치", top=14)
        output_box = tk.Frame(settings, bg="#F7F8F5", highlightthickness=1, highlightbackground=self.LINE)
        output_box.pack(fill="x", padx=18)
        self.output_label = tk.Label(
            output_box,
            textvariable=self.output_var,
            bg="#F7F8F5",
            fg=self.INK,
            font=(self.font_family, 8),
            anchor="w",
            justify="left",
            wraplength=212,
            padx=9,
            pady=9,
        )
        self.output_label.pack(fill="x")
        output_actions = tk.Frame(settings, bg=self.PANEL)
        output_actions.pack(fill="x", padx=18, pady=(7, 12))
        self.pick_output_button = self._button(output_actions, "위치 변경", self.pick_output, compact=True)
        self.pick_output_button.pack(side="left")
        self.open_output_button = self._button(output_actions, "폴더 열기", self.open_output_folder, compact=True)
        self.open_output_button.pack(side="right")

        note = tk.Frame(settings, bg="#F7F4EA", highlightthickness=1, highlightbackground="#E7DFC6")
        note.pack(fill="x", padx=18, pady=18)
        tk.Label(
            note,
            text="GIF 안내",
            bg="#F7F4EA",
            fg="#65542F",
            font=(self.font_family, 8, "bold"),
        ).pack(anchor="w", padx=10, pady=(9, 3))
        tk.Label(
            note,
            text="최대 256색 · 소리 제외 · 프레임 시간은 10ms 단위로 맞춰집니다. 원본 영상은 변경하지 않아요.",
            bg="#F7F4EA",
            fg="#786A48",
            font=(self.font_family, 8),
            justify="left",
            wraplength=238,
        ).pack(anchor="w", padx=10, pady=(0, 9))

        progress_block = tk.Frame(footer, bg=self.BG)
        tk.Label(
            progress_block,
            textvariable=self.status_var,
            bg=self.BG,
            fg=self.INK,
            font=(self.font_family, 9),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))
        self.progress = self.ttk.Progressbar(
            progress_block,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
            style="Warm.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x")

        self.cancel_button = self._button(footer, "중지", self.cancel_conversion, width=8)
        self.cancel_button.pack(side="right", padx=(8, 0), pady=4)
        self.start_button = self._button(
            footer,
            "5MB 아래로 변환",
            self.start_conversion,
            primary=True,
            width=18,
        )
        self.start_button.pack(side="right", pady=4)
        progress_block.pack(side="left", fill="x", expand=True, padx=(0, 18))

        for widget in (
            self.limit_entry,
            self.safe_check,
            self.quality_combo,
            self.pick_output_button,
            self.open_output_button,
        ):
            widget.bind(
                "<FocusIn>",
                lambda event: self.root.after_idle(
                    lambda target=event.widget: self._reveal_settings_widget(target)
                ),
                add="+",
            )

        self._refresh_controls()

    def _scroll_settings(self, _event: Any, amount: int, units: str) -> str:
        self.settings_canvas.yview_scroll(amount, units)
        return "break"

    def _scroll_settings_to(self, _event: Any, fraction: float) -> str:
        self.settings_canvas.yview_moveto(fraction)
        return "break"

    def _on_settings_mousewheel(self, event: Any) -> str | None:
        canvas = self.settings_canvas
        pointer_x, pointer_y = canvas.winfo_pointerxy()
        left = canvas.winfo_rootx()
        top = canvas.winfo_rooty()
        if not (
            left <= pointer_x < left + canvas.winfo_width()
            and top <= pointer_y < top + canvas.winfo_height()
        ):
            return None
        delta = int(getattr(event, "delta", 0))
        if delta == 0:
            return None
        canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def _reveal_settings_widget(self, widget: Any) -> None:
        """Scroll a keyboard-focused setting fully into the visible viewport."""
        if not widget.winfo_exists() or not self.settings_canvas.winfo_exists():
            return
        self.root.update_idletasks()
        canvas = self.settings_canvas
        viewport_height = canvas.winfo_height()
        if viewport_height <= 1:
            return
        widget_top = widget.winfo_rooty() - self.settings_inner.winfo_rooty()
        widget_bottom = widget_top + widget.winfo_height()
        view_top = float(canvas.canvasy(0))
        view_bottom = view_top + viewport_height
        if widget_top >= view_top and widget_bottom <= view_bottom:
            return
        scrollregion = canvas.bbox("all")
        if not scrollregion:
            return
        content_height = max(1, scrollregion[3] - scrollregion[1])
        target_top = widget_top if widget_top < view_top else widget_bottom - viewport_height
        canvas.yview_moveto(max(0.0, min(1.0, target_top / content_height)))

    def _button(
        self,
        parent: Any,
        text: str,
        command: Callable[[], None],
        *,
        primary: bool = False,
        compact: bool = False,
        width: int = 0,
    ) -> Any:
        background = self.ACCENT if primary else self.PANEL
        foreground = "#FFFFFF" if primary else self.INK
        active_background = self.ACCENT_DARK if primary else "#EEF1ED"
        button = self.tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg=foreground,
            activebackground=active_background,
            activeforeground=foreground,
            disabledforeground="#A5ADA9",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.ACCENT if primary else self.LINE,
            highlightcolor=self.ACCENT_DARK,
            cursor="hand2",
            font=(self.font_family, 9, "bold" if primary else "normal"),
            padx=10 if compact else 15,
            pady=5 if compact else 10,
            width=width,
            takefocus=True,
        )
        return button

    def _field_label(self, parent: Any, text: str, *, top: int = 0) -> None:
        self.tk.Label(
            parent,
            text=text,
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 8, "bold"),
        ).pack(anchor="w", padx=18, pady=(top, 6))

    def _separator(self, parent: Any) -> None:
        self.tk.Frame(parent, bg=self.LINE, height=1).pack(fill="x", padx=18)

    def _bind_shortcuts(self) -> None:
        self.root.bind_all("<Control-o>", lambda _event: self.add_files())
        self.root.bind_all("<Control-O>", lambda _event: self.add_files())
        self.root.bind_all("<Control-Return>", lambda _event: self.start_conversion())
        self.root.bind_all("<Delete>", lambda _event: self.remove_selected())

    def _initialise_engine_async(self) -> None:
        def worker() -> None:
            try:
                tools = resolve_tools()
                engine = create_engine(tools)
                self.ui_events.put(("engine_ready", tools, engine))
            except Exception as exc:
                self.ui_events.put(("engine_error", str(exc)))

        threading.Thread(target=worker, name="giffit-tools", daemon=True).start()

    def add_files(self) -> None:
        if self.busy:
            return
        from tkinter import filedialog

        filenames = filedialog.askopenfilenames(title="변환할 영상 선택", filetypes=VIDEO_TYPES)
        if not filenames:
            return
        self._enqueue_files(Path(filename) for filename in filenames)

    def _enqueue_files(self, paths: Iterable[Path]) -> None:
        new_items: list[FileItem] = []
        for raw_path in paths:
            path = raw_path.expanduser().resolve()
            path_marker = os.path.normcase(str(path))
            if path_marker in self.path_keys:
                continue
            key = uuid.uuid4().hex
            item = FileItem(key=key, path=path)
            self.items[key] = item
            self.path_keys[path_marker] = key
            self.tree.insert(
                "",
                "end",
                iid=key,
                values=(path.name, "분석 대기", "—", "—", item.status),
            )
            new_items.append(item)

        if not new_items:
            self.status_var.set("이미 목록에 있는 파일은 다시 추가하지 않았습니다.")
            return

        self._update_summary()
        self._refresh_controls()
        self.status_var.set(f"새 영상 {len(new_items)}개의 정보를 확인하고 있습니다.")

        def worker(snapshot: list[FileItem]) -> None:
            try:
                if self.engine is None:
                    self.ui_events.put(("probe_deferred", snapshot))
                    return
                for item in snapshot:
                    if self.cancel_event.is_set():
                        return
                    self.ui_events.put(("probe_started", item.key))
                    try:
                        if not item.path.is_file():
                            raise ConversionError("파일을 찾을 수 없습니다.")
                        probe = engine_probe(self.engine, item.path, self.cancel_event)
                        self.ui_events.put(("probe_done", item.key, probe))
                    except CancelledError:
                        return
                    except Exception as exc:
                        self.ui_events.put(("probe_failed", item.key, self._friendly_error(exc)))
            finally:
                self.ui_events.put(("probe_thread_done", threading.current_thread()))

        probe_thread = threading.Thread(
            target=worker,
            args=(new_items,),
            name="giffit-probe",
            daemon=True,
        )
        self.probe_threads.add(probe_thread)
        probe_thread.start()

    def _probe_deferred(self, items: list[FileItem]) -> None:
        if self.engine is None:
            if not self.tools_error:
                self.root.after(250, lambda: self._probe_deferred(items))
                return
            for item in items:
                self._mark_failed(item.key, self.tools_error or "변환 엔진을 준비하지 못했습니다.")
            return
        # Engine is now available; launch the same sequential probing path.
        paths = [item.path for item in items if item.key in self.items]
        for item in items:
            if item.key in self.items:
                self.path_keys.pop(os.path.normcase(str(item.path)), None)
                self.tree.delete(item.key)
                self.items.pop(item.key, None)
        self._enqueue_files(paths)

    def remove_selected(self) -> None:
        if self.busy:
            return
        for key in self.tree.selection():
            item = self.items.pop(key, None)
            if item is not None:
                self.path_keys.pop(os.path.normcase(str(item.path)), None)
            if self.tree.exists(key):
                self.tree.delete(key)
        self._update_summary()
        self._refresh_controls()

    def clear_files(self) -> None:
        if self.busy:
            return
        for key in list(self.items):
            if self.tree.exists(key):
                self.tree.delete(key)
        self.items.clear()
        self.path_keys.clear()
        self.progress_var.set(0)
        self.status_var.set("영상을 추가하면 파일 정보를 먼저 확인합니다.")
        self._update_summary()
        self._refresh_controls()

    def pick_output(self) -> None:
        if self.busy:
            return
        from tkinter import filedialog

        selected = filedialog.askdirectory(title="GIF 저장 폴더", initialdir=self.output_var.get())
        if selected:
            self.output_var.set(str(Path(selected).resolve()))

    def open_output_folder(self) -> None:
        from tkinter import messagebox

        path = Path(self.output_var.get()).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            open_path(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"저장 폴더를 열 수 없습니다.\n\n{exc}")

    def _on_tree_double_click(self, event: Any) -> None:
        row = self.tree.identify_row(event.y)
        item = self.items.get(row)
        if item is None:
            return
        target = item.output_path if item.output_path and item.output_path.exists() else item.path.parent
        try:
            open_path(target)
        except OSError:
            pass

    def _on_limit_changed(self, *_args: Any) -> None:
        try:
            hard_mb = float(self.limit_var.get().replace(",", "."))
            if not (0.1 <= hard_mb <= 5.0):
                raise ValueError
            target_mb = hard_mb * (SAFE_MARGIN_RATIO if self.safe_margin_var.get() else 1.0)
            self.target_var.set(f"실제 목표 {target_mb:.2f}MB · 상한 {hard_mb:.2f}MB")
            self.target_label.configure(fg=self.ACCENT_DARK)
        except ValueError:
            self.target_var.set("0.10~5.00MB 사이로 입력해 주세요")
            self.target_label.configure(fg=self.ERROR)
        self._refresh_controls()

    def _validated_options(self) -> tuple[int, str, bool, Path] | None:
        from tkinter import messagebox

        try:
            mb_value = float(self.limit_var.get().replace(",", "."))
            if not (0.1 <= mb_value <= 5.0):
                raise ValueError
        except ValueError:
            messagebox.showwarning(APP_NAME, "최대 용량을 0.10~5.00MB 사이 숫자로 입력해 주세요.")
            self.limit_entry.focus_set()
            return None
        quality = QUALITY_LABELS.get(self.quality_var.get())
        if quality is None:
            messagebox.showwarning(APP_NAME, "품질 기준을 선택해 주세요.")
            return None
        output_dir = Path(self.output_var.get()).expanduser().resolve()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"저장 폴더를 만들 수 없습니다.\n\n{exc}")
            return None
        return int(mb_value * 1_000_000), quality, self.safe_margin_var.get(), output_dir

    def start_conversion(self) -> None:
        if self.busy:
            return
        if self.engine is None:
            from tkinter import messagebox

            messagebox.showerror(APP_NAME, self.tools_error or "변환 엔진을 아직 준비하고 있습니다.")
            return

        options = self._validated_options()
        if options is None:
            return
        limit_bytes, quality, safe_margin, output_dir = options
        ready_items = [
            item
            for item in self.items.values()
            if item.probe is not None and item.state in {"ready", "done", "failed"}
        ]
        if not ready_items:
            return

        self.busy = True
        self.cancel_event.clear()
        self.progress_var.set(0)
        self._refresh_controls()
        settings = make_settings(
            limit_bytes,
            quality,
            safe_margin=safe_margin,
            output_dir=output_dir,
        )

        def worker(snapshot: list[FileItem]) -> None:
            completed = 0
            failed = 0
            cancelled = False
            total = len(snapshot)
            for index, item in enumerate(snapshot):
                if self.cancel_event.is_set():
                    cancelled = True
                    break
                output_path = unique_output_path(output_dir, item.path)
                self.ui_events.put(("convert_started", item.key, index, total, output_path))

                def progress_callback(*args: Any, _key: str = item.key, _index: int = index, **kwargs: Any) -> None:
                    stage, fraction, detail = parse_progress(args, kwargs)
                    self.ui_events.put(("convert_progress", _key, _index, total, stage, fraction, detail))

                try:
                    if self.tools is None:
                        raise MissingToolsError("변환 도구 경로를 준비하지 못했습니다.")
                    conversion_engine = create_engine(
                        self.tools,
                        progress_callback=progress_callback,
                        cancel_event=self.cancel_event,
                    )
                    result = engine_convert(
                        conversion_engine,
                        item.path,
                        output_path,
                        settings,
                        progress_callback,
                        self.cancel_event,
                    )
                    resolved_output = value_from(result, "output_path", "path", "gif_path", default=output_path)
                    resolved_output = Path(resolved_output)
                    if not resolved_output.is_file():
                        raise ConversionError("GIF 파일이 생성되지 않았습니다.")
                    actual_size = resolved_output.stat().st_size
                    if actual_size > limit_bytes:
                        raise ConversionError(
                            f"결과가 상한을 초과했습니다 ({format_bytes(actual_size)} / {format_bytes(limit_bytes)})."
                        )
                    completed += 1
                    self.ui_events.put(("convert_done", item.key, result, resolved_output, index, total))
                except CancelledError:
                    cancelled = True
                    self.ui_events.put(("convert_cancelled", item.key))
                    break
                except Exception as exc:
                    failed += 1
                    self.ui_events.put(("convert_failed", item.key, self._friendly_error(exc), index, total))

            self.ui_events.put(("batch_done", completed, failed, cancelled, total))

        self.worker_thread = threading.Thread(
            target=worker,
            args=(ready_items,),
            name="giffit-convert",
            daemon=True,
        )
        self.worker_thread.start()

    def cancel_conversion(self) -> None:
        if not self.busy:
            return
        self.cancel_event.set()
        self.status_var.set("현재 변환을 안전하게 중지하고 있습니다…")
        self.cancel_button.configure(state="disabled", text="중지 중…")

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.ui_events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(80, self._drain_events)

    def _handle_event(self, event: tuple[Any, ...]) -> None:
        kind = event[0]
        if kind == "engine_ready":
            self.tools, self.engine = event[1], event[2]
            self.tools_error = ""
            self.status_var.set("준비 완료 · 영상을 추가해 주세요.")
        elif kind == "engine_error":
            self.tools_error = str(event[1])
            self.status_var.set(self.tools_error)
        elif kind == "probe_deferred":
            self.root.after(200, lambda items=event[1]: self._probe_deferred(items))
        elif kind == "probe_started":
            item = self.items.get(event[1])
            if item:
                item.state = "probing"
                item.status = "분석 중…"
                self._update_tree_item(item)
        elif kind == "probe_done":
            item = self.items.get(event[1])
            if item:
                item.probe = event[2]
                item.state = "ready"
                item.status = "변환 준비"
                self._update_tree_item(item)
                self.status_var.set(f"{item.path.name} 분석 완료")
        elif kind == "probe_failed":
            self._mark_failed(event[1], event[2])
        elif kind == "probe_thread_done":
            self.probe_threads.discard(event[1])
        elif kind == "convert_started":
            item = self.items.get(event[1])
            if item:
                item.state = "converting"
                item.output_path = event[4]
                item.status = "변환 준비"
                item.progress = 0.0
                self._update_tree_item(item)
                self.status_var.set(f"{event[2] + 1}/{event[3]} · {item.path.name} 변환 중")
        elif kind == "convert_progress":
            item = self.items.get(event[1])
            if item:
                _key, index, total, stage, fraction, detail = event[1:]
                item.progress = max(item.progress, fraction)
                item.status = stage
                self._update_tree_item(item)
                overall = ((index + item.progress) / total) * 100 if total else 0
                self.progress_var.set(overall)
                suffix = f" · {detail}" if detail else ""
                self.status_var.set(f"{index + 1}/{total} · {stage}{suffix}")
        elif kind == "convert_done":
            item = self.items.get(event[1])
            if item:
                item.result = event[2]
                item.output_path = event[3]
                item.state = "done"
                item.status = "용량 상한 통과"
                item.progress = 1.0
                self._update_tree_item(item)
                self.progress_var.set(((event[4] + 1) / event[5]) * 100)
        elif kind == "convert_failed":
            item = self.items.get(event[1])
            if item:
                item.state = "failed"
                item.error = event[2]
                item.status = "실패"
                self._update_tree_item(item)
            self.progress_var.set(((event[3] + 1) / event[4]) * 100)
        elif kind == "convert_cancelled":
            item = self.items.get(event[1])
            if item:
                item.state = "ready"
                item.status = "중지됨"
                self._update_tree_item(item)
        elif kind == "batch_done":
            completed, failed, cancelled, _total = event[1:]
            self.busy = False
            self.worker_thread = None
            if not self.closing:
                self.cancel_event.clear()
            if cancelled:
                self.status_var.set(f"변환을 중지했습니다 · 완료 {completed}개 · 실패 {failed}개")
            elif failed:
                self.status_var.set(f"변환 완료 · 성공 {completed}개 · 실패 {failed}개")
            else:
                self.status_var.set(f"모두 완료 · {completed}개 파일이 용량 상한을 통과했습니다.")
                self.progress_var.set(100)
            self._refresh_controls()
        self._update_summary()
        self._refresh_controls()

    def _mark_failed(self, key: str, message: str) -> None:
        item = self.items.get(key)
        if item:
            item.state = "failed"
            item.error = message
            item.status = "읽기 실패"
            self._update_tree_item(item)
            self.status_var.set(f"{item.path.name}: {message}")

    def _update_tree_item(self, item: FileItem) -> None:
        if not self.tree.exists(item.key):
            return
        source = probe_summary(item.probe) if item.probe is not None else (item.error or "분석 중…")
        if item.state == "done" and item.output_path:
            result = result_summary(item.result, item.output_path)
            size = format_bytes(item.output_path.stat().st_size if item.output_path.exists() else None)
        elif item.state == "failed":
            result = item.error or "변환 실패"
            size = "—"
        else:
            result = "—"
            size = "—"
        tag = "done" if item.state == "done" else "failed" if item.state == "failed" else "active" if item.state == "converting" else ""
        values = (item.path.name, source, result, size, item.status)
        self.tree.item(item.key, values=values, tags=(tag,) if tag else ())

    def _update_summary(self) -> None:
        total = len(self.items)
        if total == 0:
            self.summary_var.set("추가된 영상 없음")
            return
        done = sum(item.state == "done" for item in self.items.values())
        failed = sum(item.state == "failed" for item in self.items.values())
        suffix: list[str] = []
        if done:
            suffix.append(f"완료 {done}")
        if failed:
            suffix.append(f"확인 필요 {failed}")
        detail = " · " + " · ".join(suffix) if suffix else ""
        self.summary_var.set(f"{total}개{detail}")

    def _refresh_controls(self) -> None:
        has_items = bool(self.items)
        has_selection = bool(getattr(self, "tree", None) and self.tree.selection())
        ready = any(
            item.probe is not None and item.state in {"ready", "done", "failed"}
            for item in self.items.values()
        )
        probing = any(item.state in {"waiting", "probing"} for item in self.items.values())
        invalid_limit = False
        try:
            parsed_limit = float(self.limit_var.get().replace(",", "."))
            invalid_limit = not (0.1 <= parsed_limit <= 5.0)
        except ValueError:
            invalid_limit = True

        normal_state = "disabled" if self.busy else "normal"
        self.add_button.configure(state=normal_state)
        self.clear_button.configure(state="normal" if has_items and not self.busy else "disabled")
        self.remove_button.configure(state="normal" if has_selection and not self.busy else "disabled")
        self.pick_output_button.configure(state=normal_state)
        self.limit_entry.configure(state=normal_state)
        self.safe_check.configure(state=normal_state)
        self.quality_combo.configure(state="disabled" if self.busy else "readonly")
        start_ready = ready and not probing and self.engine is not None and not self.busy and not invalid_limit
        self.start_button.configure(
            state="normal" if start_ready else "disabled",
            text=f"{len(self.items)}개 변환 시작" if self.items else "5MB 아래로 변환",
        )
        self.cancel_button.configure(state="normal" if self.busy else "disabled", text="중지")

    @staticmethod
    def _friendly_error(exc: BaseException) -> str:
        text = str(exc).strip()
        if isinstance(exc, FileNotFoundError):
            return "필요한 파일이나 변환 도구를 찾을 수 없습니다."
        if isinstance(exc, PermissionError):
            return "파일을 읽거나 저장할 권한이 없습니다."
        if isinstance(exc, CancelledError):
            return "변환이 중지되었습니다."
        return text or exc.__class__.__name__

    def _on_close(self) -> None:
        active_probes = any(thread.is_alive() for thread in self.probe_threads)
        if self.busy or active_probes:
            from tkinter import messagebox

            if not messagebox.askyesno(
                APP_NAME,
                "진행 중인 작업을 안전하게 중지하고 앱을 닫을까요?\n"
                "완료된 GIF는 그대로 유지됩니다.",
            ):
                return
            self.closing = True
            self.cancel_event.set()
            self.shutdown_deadline = time.monotonic() + 10.0
            self.status_var.set("변환 도구를 종료하고 임시 파일을 정리하고 있습니다…")
            self.root.protocol("WM_DELETE_WINDOW", lambda: None)
            self.root.after(50, self._wait_for_shutdown)
            return
        self.closing = True
        self._destroy_root()

    def _destroy_root(self) -> None:
        """Cancel queued Tk callbacks before destroying the interpreter."""
        try:
            pending_jobs = self.root.tk.splitlist(self.root.tk.call("after", "info"))
        except self.tk.TclError:
            pending_jobs = ()
        for job in pending_jobs:
            try:
                self.root.after_cancel(job)
            except self.tk.TclError:
                pass
        self.root.destroy()

    def _wait_for_shutdown(self) -> None:
        threads = [thread for thread in self.probe_threads if thread.is_alive()]
        if self.worker_thread is not None and self.worker_thread.is_alive():
            threads.append(self.worker_thread)
        if not threads or time.monotonic() >= self.shutdown_deadline:
            self._destroy_root()
            return
        self.root.after(100, self._wait_for_shutdown)


def run_gui() -> int:
    _set_windows_dpi_awareness()
    import tkinter as tk
    from tkinter import messagebox

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"{APP_NAME} GUI를 시작할 수 없습니다: {exc}", file=sys.stderr)
        return 2
    try:
        GifFitApp(root)
        root.mainloop()
        return 0
    except Exception as exc:  # pragma: no cover - last-resort GUI guard
        traceback.print_exc()
        try:
            messagebox.showerror(APP_NAME, f"앱을 시작하지 못했습니다.\n\n{exc}")
        except Exception:
            pass
        return 2


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GIFFit 5MB GIF 변환기")
    parser.add_argument("--cli", action="store_true", help="GUI 대신 JSON CLI 실행")
    parser.add_argument("inputs", nargs="*", metavar="INPUT", help="입력 영상 파일")
    parser.add_argument("--output-dir", type=Path, help="GIF 저장 폴더")
    parser.add_argument(
        "--limit-bytes",
        type=int,
        default=DEFAULT_LIMIT_BYTES,
        help="파일당 최대 바이트 (최대 5,000,000)",
    )
    parser.add_argument(
        "--quality",
        choices=("balanced", "quality", "resolution"),
        default="quality",
        help="압축 우선순위",
    )
    return parser


def run_cli(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "ok": False,
        "limit_bytes": args.limit_bytes,
        "quality": args.quality,
        "results": [],
        "errors": [],
    }

    if not args.inputs:
        payload["errors"].append({"error": "입력 영상이 없습니다."})
        print(json.dumps(payload, ensure_ascii=True))
        return 2
    if args.output_dir is None:
        payload["errors"].append({"error": "--output-dir를 지정해 주세요."})
        print(json.dumps(payload, ensure_ascii=True))
        return 2
    if not (0 < args.limit_bytes <= DEFAULT_LIMIT_BYTES):
        payload["errors"].append(
            {"error": "--limit-bytes는 1~5,000,000 사이여야 합니다."}
        )
        print(json.dumps(payload, ensure_ascii=True))
        return 2

    output_dir = args.output_dir.expanduser().resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        tools = resolve_tools()
        engine = create_engine(tools)
        settings = make_settings(
            args.limit_bytes,
            args.quality,
            safe_margin=False,
            output_dir=output_dir,
        )
    except Exception as exc:
        payload["errors"].append({"error": str(exc)})
        print(json.dumps(payload, ensure_ascii=True))
        return 2

    cancel_event = threading.Event()
    for raw_input in args.inputs:
        input_path = Path(raw_input).expanduser().resolve()
        record: dict[str, Any] = {"input": str(input_path)}
        try:
            if not input_path.is_file():
                raise ConversionError("입력 파일을 찾을 수 없습니다.")
            probe = engine_probe(engine, input_path, cancel_event)
            output_path = unique_output_path(output_dir, input_path)
            result = engine_convert(
                engine,
                input_path,
                output_path,
                settings,
                lambda *_args, **_kwargs: None,
                cancel_event,
            )
            resolved_output = Path(value_from(result, "output_path", "path", "gif_path", default=output_path))
            if not resolved_output.is_file():
                raise ConversionError("GIF 파일이 생성되지 않았습니다.")
            size_bytes = resolved_output.stat().st_size
            if size_bytes > args.limit_bytes:
                raise ConversionError(
                    f"결과가 상한을 초과했습니다 ({size_bytes} > {args.limit_bytes}바이트)."
                )
            record.update(
                {
                    "ok": True,
                    "output": str(resolved_output.resolve()),
                    "size_bytes": size_bytes,
                    "probe": serialise_result(probe),
                    "conversion": serialise_result(result),
                }
            )
            payload["results"].append(record)
        except Exception as exc:
            record.update({"ok": False, "error": str(exc) or exc.__class__.__name__})
            payload["errors"].append(record)

    payload["ok"] = not payload["errors"]
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if payload["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    if args.cli:
        return run_cli(args)
    if args.inputs or args.output_dir is not None:
        parser.error("자동 변환에는 --cli를 함께 지정해 주세요.")
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
