"""Manual regression check for fixed controls and scrollable settings access."""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import app  # noqa: E402


# Tool discovery is unrelated to geometry and changes status text asynchronously.
app.GifFitApp._initialise_engine_async = lambda self: None


def walk(widget: tk.Misc):
    for child in widget.winfo_children():
        yield child
        yield from walk(child)


def is_descendant(widget: tk.Misc, ancestor: tk.Misc) -> bool:
    current: tk.Misc | None = widget
    while current is not None:
        if current == ancestor:
            return True
        current = getattr(current, "master", None)
    return False


def fully_visible(widget: tk.Misc, viewport: tk.Misc) -> bool:
    left = widget.winfo_rootx()
    top = widget.winfo_rooty()
    right = left + widget.winfo_width()
    bottom = top + widget.winfo_height()
    view_left = viewport.winfo_rootx()
    view_top = viewport.winfo_rooty()
    view_right = view_left + viewport.winfo_width()
    view_bottom = view_top + viewport.winfo_height()
    return (
        left >= view_left
        and top >= view_top
        and right <= view_right
        and bottom <= view_bottom
    )


for dpi in (96, 120, 144, 192):
    root = tk.Tk()
    root.tk.call("tk", "scaling", dpi / 72)
    ui = app.GifFitApp(root)
    for geometry in ("1040x720", "860x600"):
        root.geometry(geometry)
        ui.settings_canvas.yview_moveto(0)
        root.update()
        root_width = root.winfo_width()
        root_height = root.winfo_height()
        clipped = []
        for widget in walk(root):
            if not widget.winfo_ismapped() or is_descendant(widget, ui.settings_inner):
                continue
            x = widget.winfo_rootx() - root.winfo_rootx()
            y = widget.winfo_rooty() - root.winfo_rooty()
            width = widget.winfo_width()
            height = widget.winfo_height()
            if x < 0 or y < 0 or x + width > root_width or y + height > root_height:
                clipped.append((widget.winfo_class(), x, y, width, height))

        fixed = {
            "add": ui.add_button,
            "start": ui.start_button,
            "cancel": ui.cancel_button,
            "progress": ui.progress,
            "tree_scrollbar": ui.tree_scrollbar,
            "settings_scrollbar": ui.settings_scrollbar,
        }
        unmapped_fixed = [
            name for name, widget in fixed.items() if not widget.winfo_ismapped()
        ]

        inaccessible_settings = []
        settings = {
            "limit": ui.limit_entry,
            "safe_margin": ui.safe_check,
            "quality": ui.quality_combo,
            "output": ui.output_label,
            "pick_output": ui.pick_output_button,
            "open_output": ui.open_output_button,
        }
        for name, widget in settings.items():
            ui._reveal_settings_widget(widget)
            root.update()
            if not widget.winfo_ismapped() or not fully_visible(widget, ui.settings_canvas):
                inaccessible_settings.append(name)

        inner_width = ui.settings_canvas.itemcget(ui.settings_window, "width")
        width_matches = abs(float(inner_width) - ui.settings_canvas.winfo_width()) <= 1
        result = {
            "dpi": dpi,
            "window": (root_width, root_height),
            "requested": (root.winfo_reqwidth(), root.winfo_reqheight()),
            "clipped": clipped,
            "unmapped_fixed": unmapped_fixed,
            "inaccessible_settings": inaccessible_settings,
            "settings_width_matches": width_matches,
        }
        print(result)
        assert not clipped, result
        assert not unmapped_fixed, result
        assert not inaccessible_settings, result
        assert width_matches, result
    ui._on_close()
