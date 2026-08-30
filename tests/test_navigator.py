"""
test_navigator.py — drives the overview map and the panel toggles.

Runs standalone (`python tests/test_navigator.py`) or under pytest, on
Qt's offscreen platform so it needs no display. Skips itself when PyQt6
is not installed, which keeps `python tests/test_dwfx.py` style runs on
a bare checkout working.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt, QEvent, QPoint, QPointF, QBuffer, QIODevice
    from PyQt6.QtGui import QMouseEvent, QPixmap, QPainter, QColor
    from PyQt6.QtWidgets import QApplication
except ImportError as exc:                            # pragma: no cover
    import unittest
    # Not always a missing PyQt6: on a headless box QtGui needs libEGL,
    # and saying so is the difference between a skip someone fixes and a
    # skip everyone reads as "no Qt here, nothing to do".
    raise unittest.SkipTest(f"Qt unavailable: {exc}")

from src.canvas import DrawingCanvas                  # noqa: E402


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" '
       'viewBox="0 0 800 600"><rect width="800" height="600" fill="#fff"/>'
       '<line x1="0" y1="0" x2="800" y2="600" stroke="#000" stroke-width="3"/>'
       '<circle cx="600" cy="150" r="80" fill="none" stroke="red"/></svg>')

_app = QApplication.instance() or QApplication([])


def _pump():
    _app.processEvents()


def _send(widget, kind, pos, modifiers=Qt.KeyboardModifier.NoModifier):
    btn = Qt.MouseButton.LeftButton
    held = (Qt.MouseButton.NoButton
            if kind == QEvent.Type.MouseButtonRelease else btn)
    _app.sendEvent(widget, QMouseEvent(kind, QPointF(pos), QPointF(pos),
                                       btn, held, modifiers))


def _canvas():
    c = DrawingCanvas()
    c.resize(900, 700)
    c.show()
    _pump()
    c.load_svg(SVG)
    _pump()
    return c


def _view_centre(c):
    return c.mapToScene(c.viewport().rect()).boundingRect().center()


# ── the map itself ───────────────────────────────────────────────────

def test_appears_with_the_drawing_and_covers_the_sheet_when_fitted():
    c = _canvas()
    nav = c._nav
    assert nav.isVisible() and nav.has_drawing()
    assert nav._content.width() == 800 and nav._content.height() == 600
    # Fitted, the box is the whole map — there is nothing off screen.
    assert nav._view_box().width() >= nav._map_rect().width() - 2


def test_box_shrinks_as_the_view_zooms_in():
    c = _canvas()
    nav = c._nav
    wide = nav._view_box().width()
    for _ in range(6):
        c._nav_zoom(1)
    _pump()
    assert nav._view_box().width() < wide * 0.6


def test_dragging_the_box_pans_the_view():
    c = _canvas()
    nav = c._nav
    for _ in range(6):
        c._nav_zoom(1)
    _pump()

    before = _view_centre(c)
    start = nav._view_box().center().toPoint()
    _send(nav, QEvent.Type.MouseButtonPress, start)
    _send(nav, QEvent.Type.MouseMove, start + QPoint(25, 18))
    _send(nav, QEvent.Type.MouseButtonRelease, start + QPoint(25, 18))
    _pump()
    after = _view_centre(c)
    assert (after - before).manhattanLength() > 1
    # Dragging right and down moves the view right and down.
    assert after.x() > before.x() and after.y() > before.y()


def test_dragging_a_new_box_zooms_to_that_region():
    c = _canvas()
    nav = c._nav
    m = nav._map_rect()
    zoom = c._current_zoom

    p1 = QPoint(m.left() + 8, m.top() + 8)
    p2 = QPoint(m.left() + int(m.width() * 0.35),
                m.top() + int(m.height() * 0.35))
    _send(nav, QEvent.Type.MouseButtonPress, p1)
    _send(nav, QEvent.Type.MouseMove, p2)
    _send(nav, QEvent.Type.MouseButtonRelease, p2)
    _pump()
    assert c._current_zoom > zoom * 1.5
    # And it landed on the top-left quarter that was dragged out.
    view = c.mapToScene(c.viewport().rect()).boundingRect()
    assert view.center().x() < 400 and view.center().y() < 300


def test_shift_draws_a_box_inside_the_current_one():
    c = _canvas()
    nav = c._nav
    for _ in range(4):
        c._nav_zoom(1)
    _pump()
    box = nav._view_box()
    zoom = c._current_zoom
    p1 = QPoint(int(box.left()) + 3, int(box.top()) + 3)
    p2 = QPoint(int(box.center().x()), int(box.center().y()))
    mod = Qt.KeyboardModifier.ShiftModifier
    _send(nav, QEvent.Type.MouseButtonPress, p1, mod)
    _send(nav, QEvent.Type.MouseMove, p2, mod)
    _send(nav, QEvent.Type.MouseButtonRelease, p2, mod)
    _pump()
    assert c._current_zoom > zoom          # zoomed, not panned


def test_a_plain_click_jumps_the_view():
    c = _canvas()
    nav = c._nav
    for _ in range(6):
        c._nav_zoom(1)
    _pump()
    m = nav._map_rect()
    target = QPoint(m.right() - 10, m.bottom() - 10)
    _send(nav, QEvent.Type.MouseButtonPress, target)
    _send(nav, QEvent.Type.MouseButtonRelease, target)
    _pump()
    centre = _view_centre(c)
    assert centre.x() > 500 and centre.y() > 400


def test_close_button_hides_it_and_reports_the_preference():
    c = _canvas()
    nav = c._nav
    seen = []
    c.navigatorVisibilityChanged.connect(seen.append)
    cr = nav._close_rect().center()
    _send(nav, QEvent.Type.MouseButtonPress, cr)
    _send(nav, QEvent.Type.MouseButtonRelease, cr)
    _pump()
    assert not nav.isVisible()
    assert seen and seen[-1] is False
    c.set_navigator_visible(True)
    assert nav.isVisible()


def test_survives_clear_reload_and_resize():
    c = _canvas()
    nav = c._nav
    c.clear()
    _pump()
    assert not nav.isVisible()

    c.load_svg(SVG)
    _pump()
    assert nav.isVisible()

    c.resize(420, 360)
    _pump()
    assert nav.x() >= 0 and nav.y() >= 0
    assert nav.x() + nav.width() <= c.width()
    assert nav.y() + nav.height() <= c.height()


def test_raster_drawings_get_a_map_too():
    c = DrawingCanvas()
    c.resize(900, 700)
    c.show()
    _pump()

    pix = QPixmap(1200, 400)
    pix.fill(QColor("#ffffff"))
    painter = QPainter(pix)
    painter.setPen(QColor("#000000"))
    painter.drawLine(0, 0, 1200, 400)
    painter.end()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pix.save(buf, "PNG")

    assert c.load_image(bytes(buf.data()), (0.0, 0.0, 120.0, 40.0))
    _pump()
    nav = c._nav
    assert nav.isVisible()
    # The map keeps the sheet's 3:1 shape rather than a fixed rectangle.
    assert abs(nav._thumb.width() / nav._thumb.height() - 3.0) < 0.1
    nav.grab()          # paintEvent must not raise


# ── panel toggles ────────────────────────────────────────────────────

def _window():
    from src.main_window import MainWindow
    w = MainWindow()
    w.resize(1400, 850)
    w.show()
    _pump()
    return w


def test_side_panels_hide_and_come_back_at_their_old_width():
    w = _window()
    sp = w._splitter
    base = sp.sizes()
    assert base[0] > 100 and base[2] > 100

    w._act_files.setChecked(False)
    _pump()
    assert not sp.widget(0).isVisible() and sp.sizes()[0] == 0
    assert sp.sizes()[1] > base[1]

    w._act_layers_panel.setChecked(False)
    _pump()
    assert sp.sizes()[0] == 0 and sp.sizes()[2] == 0
    assert sp.sizes()[1] > base[1] + 200          # canvas took it all

    w._toggle_both_panels()
    _pump()
    assert sp.widget(0).isVisible() and sp.widget(2).isVisible()
    assert abs(sp.sizes()[0] - base[0]) < 12
    assert abs(sp.sizes()[2] - base[2]) < 12


def test_a_resized_panel_returns_to_the_width_the_user_gave_it():
    w = _window()
    sp = w._splitter
    sp.setSizes([400, 600, 200])
    _pump()
    custom = sp.sizes()[0]
    w._act_files.setChecked(False)
    _pump()
    w._act_files.setChecked(True)
    _pump()
    assert abs(sp.sizes()[0] - custom) < 15


def test_shortcuts_are_wired():
    w = _window()
    assert w._act_files.shortcut().toString() == "Ctrl+1"
    assert w._act_layers_panel.shortcut().toString() == "Ctrl+2"
    assert w._act_nav.shortcut().toString() == "N"
    assert any(a.shortcut().toString() == "Ctrl+\\" for a in w.actions())


# ── standalone runner ────────────────────────────────────────────────

def _main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {exc}")
        else:
            print(f"  ok   {name}")
    print("all passed" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
