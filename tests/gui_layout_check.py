"""Small manual smoke check for the Tk layout at the default window size."""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import app  # noqa: E402


def walk(widget: tk.Misc):
    for child in widget.winfo_children():
        yield child
        yield from walk(child)


root = tk.Tk()
ui = app.GifFitApp(root)
root.update()
root_width = root.winfo_width()
root_height = root.winfo_height()
clipped = []
for widget in walk(root):
    if not widget.winfo_ismapped():
        continue
    x = widget.winfo_rootx() - root.winfo_rootx()
    y = widget.winfo_rooty() - root.winfo_rooty()
    width = widget.winfo_width()
    height = widget.winfo_height()
    if x < 0 or y < 0 or x + width > root_width or y + height > root_height:
        clipped.append((widget.winfo_class(), x, y, width, height))

print(
    {
        "window": (root_width, root_height),
        "requested": (root.winfo_reqwidth(), root.winfo_reqheight()),
        "clipped": clipped,
    }
)
root.destroy()
