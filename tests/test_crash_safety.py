"""
test_crash_safety.py — the ways a classic DWF used to take the app down.

* Pillow stalls on arcs with a very large radius while holding the GIL,
  freezing the whole app mid-decode.
* Opening another file (or sheet) while a decode ran called
  QThread.terminate(), and dropped the last reference to a running
  QThread, which makes Qt abort the process.
* A decode that finished after the user moved on could file its raster
  in the cache under the wrong sheet.
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_converter_dwf import (                       # noqa: E402
    DWGConverter, build_set, build_dwf, _sandbox, TWO_LAYERS, ONE_LAYER,
    HAVE_RENDER,
)
from tests.test_w2d_layers import line, origin, skip, stream, view  # noqa: E402

from src import cache                                          # noqa: E402


def _big_stream(lines: int = 200_000) -> bytes:
    """Several megabytes of opcodes: long enough to cross the decoder's
    cancellation checks."""
    body = [view(0, 0, 100_000, 100_000), origin(10, 10)]
    body += [line(0, 0, 5, 5)] * lines
    return stream(*body)


# ── Huge arcs ────────────────────────────────────────────────────────

def test_huge_arcs_draw_quickly():
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    from PIL import Image, ImageDraw
    from src.w2d_render import draw_arc
    img = Image.new("RGB", (4000, 3000), "white")
    d = ImageDraw.Draw(img)
    start = time.monotonic()
    for r in (50_000, 1_000_000, 100_000_000, 2_000_000_000):
        draw_arc(d, 2000, 1500, r, r, 10, 80, (0, 0, 0))
        draw_arc(d, 2000, 1500 - r + 500, r, r, 0, 360, (0, 0, 0))
    # Pillow alone takes minutes on the first of these.
    assert time.monotonic() - start < 2.0


def test_a_flattened_arc_lands_where_pillow_draws_it():
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    import numpy as np
    from PIL import Image, ImageDraw
    from src import w2d_render
    for start, end in ((10, 80), (300, 45), (0, 360)):
        a = Image.new("L", (1200, 1200), 255)
        b = a.copy()
        ImageDraw.Draw(a).arc([100, 100, 1100, 1100], -end, -start, fill=0)
        ImageDraw.Draw(b).line(
            w2d_render._arc_points(600, 600, 500, 500, start, end), fill=0)
        pa = np.argwhere(np.asarray(a) < 128)
        pb = np.argwhere(np.asarray(b) < 128)
        # every flattened pixel within 2 px of one Pillow drew
        for y, x in pb[:: max(1, len(pb) // 200)]:
            assert np.min(np.abs(pa - (y, x)).max(axis=1)) <= 2


# ── Cancellation ─────────────────────────────────────────────────────

def test_a_cancelled_decode_stops():
    from src.w2d import W2dDecoder, GeometryCollector, Cancelled
    cancel = threading.Event()
    cancel.set()
    try:
        W2dDecoder(_big_stream()).run(GeometryCollector(), cancel)
    except Cancelled:
        return
    raise AssertionError("decode ran to the end despite being cancelled")


def test_a_cancelled_raster_leaves_the_cache_alone(tmp_path=None):
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    from src.w2d import Cancelled
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_dwf(tmp / "big.dwf", _big_stream())
    conv = DWGConverter(path)
    conv.load()
    cancel = threading.Event()
    cancel.set()
    try:
        conv.render_raster(width_px=400, cancel=cancel)
    except Cancelled:
        pass
    else:
        raise AssertionError("expected Cancelled")
    assert not conv.has_cached_raster(400)
    # and nothing half-decoded is claimed about the sheet's layers
    assert conv.get_layers() == []


def test_a_raster_finished_after_a_sheet_change_is_filed_under_its_own_sheet(
        tmp_path=None):
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_set(tmp / "set.dwf", [("Plan", TWO_LAYERS),
                                       ("Detail", ONE_LAYER)])
    conv = DWGConverter(path)
    conv.load()

    from src import w2d_render
    real = w2d_render.render_sheet_png

    def switch_mid_decode(p, index=0, *a, **k):
        got = real(p, index, *a, **k)
        conv.set_sheet(1)
        return got

    w2d_render.render_sheet_png = switch_mid_decode
    try:
        conv.render_raster(width_px=400)
    finally:
        w2d_render.render_sheet_png = real

    assert cache.raster_path(path, 400, "", 0).is_file()
    assert not cache.raster_path(path, 400, "", 1).is_file()
    # sheet 1 is on screen; sheet 0's layers must not be shown for it
    assert "GRID" not in [l["name"] for l in conv.get_layers()]
    assert "WALLS" not in [l["name"] for l in conv.get_layers()]


# ── The window ───────────────────────────────────────────────────────

_app = None


def _qt():
    global _app
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError as exc:                        # pragma: no cover
        skip(f"Qt unavailable: {exc}")
    _app = QApplication.instance() or QApplication([])
    return _app


def _pump(app, until, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return False


def test_an_unreferenced_running_worker_does_not_abort():
    app = _qt()
    from PyQt6.QtCore import QThread
    from src import main_window as mw

    class Slow(QThread):
        def __init__(self):
            super().__init__()
            self.cancel = threading.Event()

        def run(self):
            while not self.cancel.wait(0.01):
                pass

    w = Slow()
    mw._keep_alive(w)
    w.start()
    mw._retire(w)
    del w
    gc.collect()                  # used to be "QThread: Destroyed while running"
    assert _pump(app, lambda: not mw._LIVE_WORKERS, 5.0)


def test_opening_another_file_mid_decode_is_safe(tmp_path=None):
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    app = _qt()
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    big = build_dwf(tmp / "big.dwf", _big_stream(1_500_000))
    small = build_dwf(tmp / "small.dwf", TWO_LAYERS)

    from src import main_window as mw, w2d_render
    old_width = w2d_render.DEFAULT_WIDTH
    w2d_render.DEFAULT_WIDTH = 800
    try:
        win = mw.MainWindow()
        win._open_file(str(big))
        first = win._load_worker
        assert _pump(app, lambda: first.isRunning(), 5.0)
        time.sleep(0.2)                        # well into the decode
        win._open_file(str(small))
        gc.collect()

        # The big decode was asked to stop, not killed, and stops quickly.
        assert first.cancel.is_set()
        assert _pump(app, lambda: not mw._LIVE_WORKERS
                     and win._current_conv is not None, 30.0)
        assert win._current_conv.filepath == small
        # Its raster was never cached, so the next open decodes cleanly.
        again = DWGConverter(big)
        again.load()
        assert not again.has_cached_raster()
        win.close()
    finally:
        w2d_render.DEFAULT_WIDTH = old_width
