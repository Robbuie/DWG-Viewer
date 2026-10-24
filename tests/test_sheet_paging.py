"""
test_sheet_paging.py — paging through a drawing set, and keeping sparse
classic-DWF sheets legible when shrunk to fit the window.

Runs on Qt's offscreen platform; skips itself when PyQt6 is unavailable.
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt, QByteArray
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtWidgets import QApplication, QGraphicsPixmapItem
    from PIL import Image, ImageDraw
except ImportError as exc:                            # pragma: no cover
    import unittest
    raise unittest.SkipTest(f"Qt unavailable: {exc}")

from src.canvas import DrawingCanvas                  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _pump(seconds: float = 0.0, until=None) -> None:
    end = time.time() + seconds
    while True:
        _app.processEvents()
        if until is not None and until():
            return
        if time.time() >= end:
            return
        time.sleep(0.01)


def _sparse_raster_png(width=8000, height=5000) -> bytes:
    """A big white sheet with a few one-pixel lines — what a schematic
    looks like at the classic-DWF raster resolution."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for y in range(500, height, 500):
        d.line((0, y, width, y), fill=(0, 0, 0), width=1)
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


class _Provider:
    """Stands in for DWGConverter.render_detail_png."""

    def __init__(self):
        self.calls = []

    def __call__(self, rect, base, out):
        self.calls.append((rect, base, out))
        img = Image.new("RGB", (int(out[0]), int(out[1])), (255, 255, 255))
        ImageDraw.Draw(img).line((0, 0, out[0], out[1]), fill=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()


def test_shrunk_raster_gets_a_screen_resolution_tile():
    c = DrawingCanvas()
    c.resize(1000, 700)
    c.show()
    assert c.load_image(_sparse_raster_png(), (0, 5, 8, -5))
    assert c.transform().m11() < 0.5          # fitted, i.e. shrunk

    provider = _Provider()
    c.set_detail_provider(provider)
    _pump(3, until=lambda: c._detail_item is not None)

    assert c._detail_item is not None, "no tile while zoomed out"
    # The tile is drawn at roughly the size it is shown at, not at the
    # raster's own 8000 px.
    tile_calls = [o for _r, _b, o in provider.calls if o[0] > 400]
    assert tile_calls and max(w for w, _h in tile_calls) <= 1000 * 2 + 2


def test_navigator_miniature_is_redrawn_from_geometry():
    c = DrawingCanvas()
    c.resize(1000, 700)
    c.show()
    assert c.load_image(_sparse_raster_png(), (0, 5, 8, -5))
    before = c._nav._thumb
    provider = _Provider()
    c.set_detail_provider(provider)
    _pump(3, until=lambda: c._nav._thumb is not before)
    assert c._nav._thumb is not before


def test_native_resolution_needs_no_tile():
    c = DrawingCanvas()
    c.resize(1000, 700)
    c.show()
    assert c.load_image(_sparse_raster_png(800, 500), (0, 5, 8, -5))
    c.resetTransform()
    provider = _Provider()
    c.set_detail_provider(provider)
    _pump(0.6)
    assert c._detail_item is None


# ── sheet paging ─────────────────────────────────────────────────────

def _window():
    from src.main_window import MainWindow
    w = MainWindow()
    w.resize(1400, 850)
    w.show()
    _pump(0.05)
    return w


def test_sheet_label_numbers_the_set():
    from src.main_window import MainWindow
    assert MainWindow.sheet_label(0, 18, "A-Model") == " 1 / 18   A-Model"
    assert MainWindow.sheet_label(17, 18, "B") == "18 / 18   B"
    assert MainWindow.sheet_label(2, 3, "C") == "3 / 3   C"


def test_previous_and_next_step_through_the_picker():
    w = _window()
    seen = []
    w._on_sheet_changed = lambda i: (seen.append(i), w._update_sheet_steps())
    w._sheet_box.currentIndexChanged.disconnect()
    w._sheet_box.currentIndexChanged.connect(w._on_sheet_changed)

    w._sheet_box.addItems(["a", "b", "c"])
    w._sheet_box.setCurrentIndex(0)
    w._show_sheet_controls(True)
    assert not w._act_prev_sheet.isEnabled()
    assert w._act_next_sheet.isEnabled()

    w._act_next_sheet.trigger()
    w._act_next_sheet.trigger()
    assert w._sheet_box.currentIndex() == 2
    assert not w._act_next_sheet.isEnabled()
    w._step_sheet(1)                         # past the end: no-op
    assert w._sheet_box.currentIndex() == 2

    w._act_prev_sheet.trigger()
    assert w._sheet_box.currentIndex() == 1
    assert w._act_prev_sheet.isEnabled() and w._act_next_sheet.isEnabled()
    assert seen[-3:] == [1, 2, 1]


def test_page_keys_are_bound():
    w = _window()
    assert w._act_prev_sheet.shortcut().toString() == "PgUp"
    assert w._act_next_sheet.shortcut().toString() == "PgDown"


def test_controls_hidden_for_a_single_sheet():
    w = _window()
    w._populate_sheets(None)
    assert not w._act_prev_sheet.isVisible()
    assert not w._act_next_sheet.isVisible()
    assert not w._sheet_action.isVisible()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
