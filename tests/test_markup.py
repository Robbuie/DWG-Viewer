"""
test_markup.py — redlines and clipboard snapshots.

Runs standalone (`python tests/test_markup.py`) or under pytest, on
Qt's offscreen platform. Skips itself when PyQt6 is not installed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt, QEvent, QPoint, QPointF, QRectF
    from PyQt6.QtGui import QMouseEvent, QGuiApplication
    from PyQt6.QtWidgets import QApplication
except ImportError:                                   # pragma: no cover
    import unittest
    raise unittest.SkipTest("PyQt6 not installed")

from src import markup as mk                          # noqa: E402
from src.canvas import DrawingCanvas                  # noqa: E402


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" '
       'viewBox="0 0 800 600"><rect width="800" height="600" fill="#fff"/>'
       '<line x1="0" y1="0" x2="800" y2="600" stroke="#000" stroke-width="3"/>'
       '</svg>')

_app = QApplication.instance() or QApplication([])


def _pump():
    _app.processEvents()


def _send(widget, kind, pos):
    btn = Qt.MouseButton.LeftButton
    held = (Qt.MouseButton.NoButton
            if kind == QEvent.Type.MouseButtonRelease else btn)
    _app.sendEvent(widget, QMouseEvent(kind, QPointF(pos), QPointF(pos),
                                       btn, held, Qt.KeyboardModifier.NoModifier))


def _drag(canvas, a, b):
    """A left-drag across the canvas viewport."""
    vp = canvas.viewport()
    _send(vp, QEvent.Type.MouseButtonPress, a)
    _send(vp, QEvent.Type.MouseMove, QPoint((a.x() + b.x()) // 2,
                                            (a.y() + b.y()) // 2))
    _send(vp, QEvent.Type.MouseMove, b)
    _send(vp, QEvent.Type.MouseButtonRelease, b)
    _pump()


def _canvas(tmp: Path, name: str = "sheet.dxf"):
    drawing = tmp / name
    drawing.write_bytes(b"not a real drawing, only a name to hang markup on")
    store = mk.MarkupStore(drawing)
    c = DrawingCanvas()
    c.resize(900, 700)
    c.show()
    _pump()
    c.load_svg(SVG)
    _pump()
    c.set_markup_context(store, "0")
    return c, store, drawing


# ── the model ────────────────────────────────────────────────────────

def test_markup_survives_a_json_round_trip():
    m = mk.Markup(kind=mk.CLOUD, points=[(0.1, 0.2), (0.4, 0.5)],
                  color="#0a84ff", author="tester")
    back = mk.Markup.from_dict(json.loads(json.dumps(m.to_dict())))
    assert back is not None
    assert back.kind == mk.CLOUD and back.color == "#0a84ff"
    assert back.points == [(0.1, 0.2), (0.4, 0.5)]
    assert back.id == m.id


def test_a_corrupt_record_is_dropped_not_fatal():
    assert mk.Markup.from_dict({"kind": "nonsense", "points": [[0, 0]]}) is None
    assert mk.Markup.from_dict({"kind": mk.BOX}) is None
    assert mk.Markup.from_dict({"kind": mk.BOX, "points": [[0.1, 0.2],
                                                           [0.3, 0.4]]})


def test_normalised_coordinates_map_both_ways():
    content = QRectF(100, 50, 800, 600)
    pt = mk.to_scene((0.25, 0.5), content)
    assert abs(pt.x() - 300) < 1e-6 and abs(pt.y() - 350) < 1e-6
    assert mk.to_norm(pt, content) == (0.25, 0.5)


def test_every_kind_builds_an_item():
    content = QRectF(0, 0, 800, 600)
    for kind in mk.KINDS:
        m = mk.Markup(kind=kind, points=[(0.2, 0.2), (0.6, 0.7)], text="note")
        item = mk.build_item(m, content)
        assert item is not None, kind
        assert item.boundingRect().width() > 0, kind


# ── the sidecar ──────────────────────────────────────────────────────

def test_sidecar_is_written_beside_the_drawing_and_read_back():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        drawing = tmp / "plan.dwfx"
        drawing.write_bytes(b"x")
        store = mk.MarkupStore(drawing)
        store.sheet("Sheet 1").append(
            mk.Markup(kind=mk.BOX, points=[(0.1, 0.1), (0.2, 0.2)]))
        assert store.save()

        sidecar = tmp / "plan.dwfx.markup.json"
        assert sidecar.is_file()
        assert not store.is_fallback

        again = mk.MarkupStore(drawing)
        assert len(again.sheet("Sheet 1")) == 1
        assert again.sheet("Sheet 1")[0].kind == mk.BOX
        # Sheets are independent.
        assert again.sheet("Sheet 2") == []


def test_sheets_are_keyed_by_name_not_index():
    with tempfile.TemporaryDirectory() as tmp:
        drawing = Path(tmp) / "set.dwfx"
        drawing.write_bytes(b"x")
        store = mk.MarkupStore(drawing)
        store.sheet("E-101").append(
            mk.Markup(kind=mk.CLOUD, points=[(0.1, 0.1), (0.3, 0.3)]))
        store.sheet("E-102").append(
            mk.Markup(kind=mk.ARROW, points=[(0.5, 0.5), (0.7, 0.7)]))
        store.save()

        reopened = mk.MarkupStore(drawing)
        assert reopened.sheet("E-101")[0].kind == mk.CLOUD
        assert reopened.sheet("E-102")[0].kind == mk.ARROW
        assert reopened.total() == 2


def test_emptying_the_store_removes_the_sidecar():
    with tempfile.TemporaryDirectory() as tmp:
        drawing = Path(tmp) / "plan.dxf"
        drawing.write_bytes(b"x")
        store = mk.MarkupStore(drawing)
        store.sheet("0").append(mk.Markup(kind=mk.BOX, points=[(0.1, 0.1),
                                                              (0.2, 0.2)]))
        store.save()
        sidecar = Path(str(drawing) + ".markup.json")
        assert sidecar.is_file()
        store.sheet("0").clear()
        store.save()
        assert not sidecar.exists()


def test_an_unwritable_folder_falls_back_to_the_local_cache():
    """A drawing on a read-only share must not cost the user their work.

    The sidecar path is blocked here by putting a directory in its
    place, which fails the same way a read-only share does and works
    regardless of what the test runs as.
    """
    with tempfile.TemporaryDirectory() as tmp:
        drawing = Path(tmp) / "locked.dwfx"
        drawing.write_bytes(b"x")
        blocker = Path(str(drawing) + ".markup.json")
        blocker.mkdir()

        store = mk.MarkupStore(drawing)
        store.sheet("0").append(
            mk.Markup(kind=mk.BOX, points=[(0.1, 0.1), (0.3, 0.3)]))
        assert store.save()
        assert store.is_fallback
        assert blocker.is_dir()          # untouched

        again = mk.MarkupStore(drawing)
        assert again.is_fallback
        assert len(again.sheet("0")) == 1

        # Leave no litter in the shared cache for the next run.
        again.sheet("0").clear()
        again.save()


# ── drawing on the canvas ────────────────────────────────────────────

def test_dragging_with_the_cloud_tool_adds_one_markup():
    with tempfile.TemporaryDirectory() as tmp:
        c, store, _ = _canvas(Path(tmp))
        c.set_markup_tool(mk.CLOUD)
        _drag(c, QPoint(200, 200), QPoint(400, 350))
        assert len(store.sheet("0")) == 1
        m = store.sheet("0")[0]
        assert m.kind == mk.CLOUD and m.author
        # Stored as fractions of the sheet, inside it.
        for x, y in m.points:
            assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
        assert c.markup_count() == 1


def test_a_stray_click_does_not_leave_a_speck():
    with tempfile.TemporaryDirectory() as tmp:
        c, store, _ = _canvas(Path(tmp))
        c.set_markup_tool(mk.BOX)
        _drag(c, QPoint(300, 300), QPoint(301, 301))
        assert store.sheet("0") == []


def test_freehand_keeps_its_shape_but_not_every_pixel():
    with tempfile.TemporaryDirectory() as tmp:
        c, store, _ = _canvas(Path(tmp))
        c.set_markup_tool(mk.PEN)
        vp = c.viewport()
        _send(vp, QEvent.Type.MouseButtonPress, QPoint(100, 100))
        for i in range(60):
            _send(vp, QEvent.Type.MouseMove, QPoint(100 + i * 4, 100 + i))
        _send(vp, QEvent.Type.MouseButtonRelease, QPoint(340, 160))
        _pump()
        assert len(store.sheet("0")) == 1
        pts = store.sheet("0")[0].points
        assert 3 <= len(pts) <= 62


def test_markup_lands_in_the_same_place_after_a_re_render():
    with tempfile.TemporaryDirectory() as tmp:
        c, store, _ = _canvas(Path(tmp))
        c.set_markup_tool(mk.BOX)
        _drag(c, QPoint(250, 220), QPoint(450, 380))
        item = next(iter(c._mk_items.values()))
        before = item.sceneBoundingRect()
        stored = list(store.sheet("0")[0].points)

        # Re-render at a different size, as "Apply Layers" does.
        c.load_svg(SVG.replace('width="800" height="600"',
                               'width="1600" height="1200"'))
        c.set_markup_context(store, "0")
        _pump()

        assert c.markup_count() == 1
        assert store.sheet("0")[0].points == stored     # untouched
        item2 = next(iter(c._mk_items.values()))
        after = item2.sceneBoundingRect()
        content = c._content_rect()
        # Same fraction of the sheet, even though the sheet is 2x bigger.
        assert abs(after.left() / content.width()
                   - before.left() / (content.width() / 2)) < 0.01


def test_moving_a_selected_markup_writes_the_new_position():
    with tempfile.TemporaryDirectory() as tmp:
        c, store, _ = _canvas(Path(tmp))
        c.set_markup_tool(mk.BOX)
        _drag(c, QPoint(250, 220), QPoint(450, 380))
        original = list(store.sheet("0")[0].points)

        c.set_markup_tool('select')
        item = next(iter(c._mk_items.values()))
        item.setSelected(True)
        item.setPos(QPointF(40, 25))         # as a drag would leave it
        c._sync_moved_markup()
        _pump()

        moved = store.sheet("0")[0].points
        assert moved != original
        content = c._content_rect()
        assert abs((moved[0][0] - original[0][0]) - 40 / content.width()) < 1e-6
        assert abs((moved[0][1] - original[0][1]) - 25 / content.height()) < 1e-6


def test_delete_and_undo():
    with tempfile.TemporaryDirectory() as tmp:
        c, store, _ = _canvas(Path(tmp))
        c.set_markup_tool(mk.BOX)
        _drag(c, QPoint(200, 200), QPoint(300, 300))
        _drag(c, QPoint(400, 400), QPoint(500, 500))
        assert len(store.sheet("0")) == 2

        c.set_markup_tool('select')
        next(iter(c._mk_items.values())).setSelected(True)
        assert c.delete_selected_markup() == 1
        assert len(store.sheet("0")) == 1

        assert c.undo_markup()
        assert len(store.sheet("0")) == 2
        assert c.markup_count() == 2

        assert c.undo_markup()               # undoes the second add
        assert len(store.sheet("0")) == 1


def test_clear_sheet_is_undoable():
    with tempfile.TemporaryDirectory() as tmp:
        c, store, _ = _canvas(Path(tmp))
        c.set_markup_tool(mk.ARROW)
        _drag(c, QPoint(200, 200), QPoint(300, 300))
        _drag(c, QPoint(400, 400), QPoint(500, 500))
        assert c.clear_markup_on_sheet() == 2
        assert store.sheet("0") == []
        assert c.undo_markup()
        assert len(store.sheet("0")) == 2


def test_hiding_markup_leaves_the_records_alone():
    with tempfile.TemporaryDirectory() as tmp:
        c, store, _ = _canvas(Path(tmp))
        c.set_markup_tool(mk.BOX)
        _drag(c, QPoint(200, 200), QPoint(300, 300))
        c.set_markup_visible(False)
        assert not any(i.isVisible() for i in c._mk_items.values())
        assert len(store.sheet("0")) == 1
        c.set_markup_visible(True)
        assert all(i.isVisible() for i in c._mk_items.values())


def test_drawing_needs_a_store():
    c = DrawingCanvas()
    c.resize(900, 700)
    c.show()
    _pump()
    c.load_svg(SVG)
    _pump()
    c.set_markup_tool(mk.BOX)
    _drag(c, QPoint(200, 200), QPoint(300, 300))
    assert c.markup_count() == 0          # nothing to attach it to


# ── snapshots ────────────────────────────────────────────────────────

def test_region_render_is_high_resolution_and_white_backed():
    with tempfile.TemporaryDirectory() as tmp:
        c, _, _ = _canvas(Path(tmp))
        content = c._content_rect()
        region = QRectF(content.left(), content.top(),
                        content.width() / 4, content.height() / 4)
        img = c.render_region(region)
        assert img is not None
        on_screen = region.width() * c.transform().m11()
        assert img.width() > on_screen * 1.5      # oversampled, not a grab
        corner = img.pixelColor(1, img.height() - 2)
        assert corner.red() > 240 and corner.green() > 240


def test_snapshot_drag_copies_to_the_clipboard():
    with tempfile.TemporaryDirectory() as tmp:
        c, _, _ = _canvas(Path(tmp))
        QGuiApplication.clipboard().clear()
        seen = []
        c.snapshotTaken.connect(lambda w, h: seen.append((w, h)))

        c.set_snapshot_mode(True)
        assert c.snapshot_mode()
        _drag(c, QPoint(150, 150), QPoint(450, 400))

        assert seen, "no snapshot was taken"
        assert not c.snapshot_mode()        # one-shot, as the toolbar expects
        img = QGuiApplication.clipboard().image()
        assert not img.isNull()
        assert (img.width(), img.height()) == seen[-1]


def test_a_tiny_snapshot_drag_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        c, _, _ = _canvas(Path(tmp))
        seen = []
        c.snapshotTaken.connect(lambda w, h: seen.append((w, h)))
        c.set_snapshot_mode(True)
        _drag(c, QPoint(300, 300), QPoint(302, 301))
        assert not seen


def test_snapshots_include_the_markup():
    with tempfile.TemporaryDirectory() as tmp:
        c, store, _ = _canvas(Path(tmp))
        c.set_markup_color("#ff3b30")
        c.set_markup_tool(mk.BOX)
        _drag(c, QPoint(200, 200), QPoint(500, 450))
        c.set_markup_tool(None)
        _pump()

        content = c._content_rect()
        img = c.render_region(content)
        assert img is not None
        reds = 0
        for y in range(0, img.height(), 3):
            for x in range(0, img.width(), 3):
                px = img.pixelColor(x, y)
                if px.red() > 180 and px.green() < 90 and px.blue() < 90:
                    reds += 1
        assert reds > 20, "the redline is missing from the snapshot"


def test_copy_view_needs_a_drawing():
    c = DrawingCanvas()
    c.resize(400, 300)
    c.show()
    _pump()
    assert c.copy_view_to_clipboard() is False


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
