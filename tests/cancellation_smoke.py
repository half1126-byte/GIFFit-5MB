"""Manual smoke test that cancellation terminates tools and cleans temp files."""

from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import app  # noqa: E402
from converter import CancelledError, ConversionSettings  # noqa: E402


parser = argparse.ArgumentParser()
parser.add_argument("video", type=Path, help="영상 파일 하나")
args = parser.parse_args()
source = args.video.expanduser().resolve()
if not source.is_file():
    parser.error(f"영상 파일을 찾을 수 없습니다: {source}")

with tempfile.TemporaryDirectory(prefix="giffit-cancel-") as temp_name:
    output_dir = Path(temp_name)
    cancel_event = threading.Event()
    engine = app.create_engine(app.resolve_tools(), cancel_event=cancel_event)
    errors: list[BaseException] = []

    def convert() -> None:
        try:
            engine.convert(source, output_dir, ConversionSettings())
        except BaseException as exc:  # the concrete exception is asserted below
            errors.append(exc)

    worker = threading.Thread(target=convert, name="cancellation-smoke")
    worker.start()
    deadline = time.monotonic() + 5.0
    while worker.is_alive() and time.monotonic() < deadline:
        if any(output_dir.glob(".giffit-*")):
            break
        time.sleep(0.05)
    time.sleep(0.25)
    cancel_event.set()
    worker.join(timeout=12.0)

    temp_dirs = list(output_dir.glob(".giffit-*"))
    published = list(output_dir.glob("*.gif"))
    result = {
        "thread_alive": worker.is_alive(),
        "exception": type(errors[0]).__name__ if errors else None,
        "temp_dirs": [str(path) for path in temp_dirs],
        "published": [str(path) for path in published],
    }
    print(result)
    assert not worker.is_alive()
    assert errors and isinstance(errors[0], CancelledError)
    assert not temp_dirs
    assert not published
