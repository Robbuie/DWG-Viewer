"""
test_step.py — STEP models: reading, the mesh cache, rendering, and the
main window's 3D mode.

The models are built here with OpenCascade itself (tests/step_fixtures.py),
so no sample files are needed. Skips itself when OpenCascade (the
cadquery-ocp-novtk wheel) or Qt is missing, naming which.

Runs standalone (`python tests/test_step.py`) or under pytest, on Qt's
offscreen platform — which has no OpenGL, so the 3D view is exercised
through its software renderer here. The OpenGL path was checked under
Xvfb + Mesa; on Windows it is the default.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest  # noqa: E402

try:
    import OCP.STEPCAFControl  # noqa: F401
except Exception as exc:                              # pragma: no cover
    raise unittest.SkipTest(f"OpenCascade (cadquery-ocp-novtk) unavailable: {exc}")

try:
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtWidgets import QApplication
except ImportError as exc:                            # pragma: no cover
    raise unittest.SkipTest(f"Qt unavailable: {exc}")

import numpy as np  # noqa: E402

from src import step, cache  # noqa: E402
from src.model_camera import Camera  # noqa: E402
from tests.step_fixtures import make_assembly, make_single_solid  # noqa: E402

_app = QApplication.instance() or QApplication([])
_TMP = Path(tempfile.mkdtemp(prefix="dwgv_step_"))
ASM = make_assembly(_TMP / "fixture.step")
SOLID = make_single_solid(_TMP / "torus.stp")


@contextlib.contextmanager
def _private_cache():
    """Mesh cache in a temp folder, not the user's."""
    old = cache.CACHE_ROOT
    cache.CACHE_ROOT = Path(tempfile.mkdtemp(prefix="dwgv_cache_"))
    try:
        yield cache.CACHE_ROOT
    finally:
        cache.CACHE_ROOT = old


def _pump(seconds=0.0):
    end = time.time() + seconds
    while True:
        _app.processEvents()
        if time.time() >= end:
            break
        time.sleep(0.01)


# ── reading ──────────────────────────────────────────────────────────

def test_suffixes():
    assert step.is_step("a.STEP") and step.is_step("b.stp")
    assert not step.is_step("c.dwg")


def test_assembly_parts_names_and_instances():
    m = step.load(ASM, use_cache=False)
    labels = [p.label for p in m.parts]
    assert labels == ["Base plate", "Bracket", "Knob", "M8 bolt  ×4"], labels
    bolt = m.part("M8 bolt  ×4")
    assert bolt.instances == 4


def test_instances_are_placed_not_stacked():
    m = step.load(ASM, use_cache=False)
    v = m.part("M8 bolt  \u00d74").vertices
    # One prototype placed four times: each quarter of the mesh is the
    # same shape, centred on its own hole.
    quarters = v.reshape(4, -1, 3)
    centres = sorted(tuple(int(c) for c in np.round((q.min(0) + q.max(0))[:2] / 2))
                     for q in quarters)
    assert centres == [(15, 15), (15, 45), (85, 15), (85, 45)], centres


def test_colours_come_through_in_srgb():
    m = step.load(ASM, use_cache=False)
    assert np.allclose(m.part("Base plate").color, (0.55, 0.60, 0.68), atol=0.01)
    assert np.allclose(m.part("M8 bolt  ×4").color, (0.85, 0.70, 0.20), atol=0.01)
    # No colour in the file: the neutral default, not black.
    assert min(m.part("Knob").color) > 0.5


def test_face_colour_overrides_part_colour():
    m = step.load(ASM, use_cache=False)
    br = m.part("Bracket")
    assert br.colors is not None
    found = np.unique(np.round(br.colors.astype(np.float64), 2), axis=0)

    def has(rgb):
        return np.isclose(found, rgb, atol=0.011).all(axis=1).any()
    assert has((0.85, 0.15, 0.15))              # the red face
    assert has((0.20, 0.45, 0.80))              # the rest of it


def test_bounds_and_size():
    m = step.load(ASM, use_cache=False)
    lo, hi = m.bounds()
    # Plate 100 x 60 from z=0; bolts reach z=-12; knob tops out at 68.
    assert np.allclose(lo, [0, 0, -12], atol=0.5)
    assert np.allclose(hi, [100, 60, 68], atol=0.5)
    assert m.size_text().endswith(" mm")


def test_normals_are_unit_and_match_triangles():
    m = step.load(ASM, use_cache=False)
    for p in m.parts:
        n = np.linalg.norm(p.normals, axis=1)
        assert np.allclose(n, 1, atol=1e-3), p.name
        assert len(p.normals) == len(p.vertices) and len(p.vertices) % 3 == 0


def test_box_has_only_its_twelve_edges():
    """Edges shared by two faces are drawn once, not twice."""
    m = step.load(ASM, use_cache=False)
    assert len(m.part("Bracket").edges) == 12 * 2


def test_unnamed_single_solid():
    m = step.load(SOLID, use_cache=False)
    assert [p.name for p in m.parts] == ["Unnamed part"]
    assert m.triangle_count > 100


def test_garbage_file_is_a_step_error():
    bad = _TMP / "bad.step"
    bad.write_text("ISO-10303-21;\nthis is not a model\n")
    try:
        step.load(bad, use_cache=False)
    except step.StepError as exc:
        assert "bad.step" in str(exc)
    else:
        raise AssertionError("expected StepError")


def test_missing_file_is_a_step_error():
    try:
        step.load(_TMP / "nope.step", use_cache=False)
    except step.StepError:
        pass
    else:
        raise AssertionError("expected StepError")


def test_cancel_stops_the_load():
    ev = threading.Event()
    ev.set()
    try:
        step.load(ASM, cancel=ev, use_cache=False)
    except step.Cancelled:
        pass
    else:
        raise AssertionError("expected Cancelled")


def test_cache_round_trip():
    with _private_cache():
        assert not step.has_cached_mesh(ASM)
        a = step.load(ASM)
        assert step.has_cached_mesh(ASM)
        b = step.load(ASM)
        assert [p.label for p in a.parts] == [p.label for p in b.parts]
        for pa, pb in zip(a.parts, b.parts):
            assert np.allclose(pa.vertices, pb.vertices)
            assert np.allclose(pa.normals, pb.normals, atol=2e-3)
            assert np.allclose(pa.edges, pb.edges)
            assert np.allclose(pa.color, pb.color)
            assert (pa.colors is None) == (pb.colors is None)
            if pa.colors is not None:
                assert np.allclose(pa.colors, pb.colors, atol=1 / 255)


def test_corrupt_cache_entry_is_ignored():
    with _private_cache():
        step.load(ASM)
        step._cache_path(ASM).write_bytes(b"truncated")
        m = step.load(ASM)                      # re-reads the STEP
        assert len(m.parts) == 4


# ── rendering ────────────────────────────────────────────────────────

def _px(img, x, y):
    c = img.pixelColor(x, y)
    return c.red(), c.green(), c.blue()


def test_software_render_top_view():
    from src.model_view import render_image
    m = step.load(ASM, use_cache=False)
    c = Camera("z")
    c.resize(400, 300)
    c.set_bounds(*m.bounds())
    c.set_preset("top")
    img = render_image(m, c, 400, 300, background="#ffffff")
    # A bolt head sits at (15, 15): yellow, and seen from above, in
    # front of the plate it stands on.
    x, y, _ = c.project([[15, 15, 13]])[0]
    r, g, b = _px(img, int(x), int(y))
    assert r > 120 and g > 100 and b < 90, (r, g, b)
    # Plate between the bolts: blue-grey.
    x, y, _ = c.project([[50, 5, 8]])[0]
    r, g, b = _px(img, int(x), int(y))
    assert b >= r and abs(r - g) < 40, (r, g, b)
    # Outside the model: the background.
    assert _px(img, 2, 2) == (255, 255, 255)


def test_hidden_parts_are_not_drawn():
    from src.model_view import render_image
    m = step.load(ASM, use_cache=False)
    m.part("Base plate").visible = False
    c = Camera("z")
    c.resize(400, 300)
    c.set_bounds(*m.bounds())
    c.set_preset("top")
    img = render_image(m, c, 400, 300, background="#ffffff")
    x, y, _ = c.project([[50, 5, 8]])[0]
    assert _px(img, int(x), int(y)) == (255, 255, 255)


def test_hidden_edges_stay_hidden():
    """Edges are depth-tested: the bracket's back edges, seen from the
    front, are behind its front face and must not show through it."""
    from src.model_view import render_image
    m = step.load(ASM, use_cache=False)
    for p in m.parts:
        p.visible = p.name == "Bracket"
    c = Camera("z")
    c.resize(400, 400)
    c.set_bounds(*m.bounds())
    c.set_preset("iso")
    plain = render_image(m, c, 400, 400, edges=False)
    edged = render_image(m, c, 400, 400, edges=True)
    a = np.frombuffer(plain.bits().asstring(plain.sizeInBytes()), np.uint8)
    b = np.frombuffer(edged.bits().asstring(edged.sizeInBytes()), np.uint8)
    changed = (a.reshape(400, 400, 4) != b.reshape(400, 400, 4)).any(axis=2)
    # Iso shows 9 of the box's 12 edges; the 3 at the hidden corner
    # should not be drawn. Count changed pixels near that corner.
    hidden_corner = c.project([[35, 40, 8]])[0]      # back-bottom-left
    x, y = int(hidden_corner[0]), int(hidden_corner[1])
    assert not changed[max(0, y - 2):y + 3, max(0, x - 2):x + 3].any()
    assert changed.sum() > 200                       # visible edges are there


def test_thumbnail():
    m = step.load(SOLID, use_cache=False)
    img = step.render_thumbnail(m, 160, 110)
    assert img.width() == 160 and img.height() == 110
    assert _px(img, 80, 55) == (255, 255, 255)       # the torus's hole
    assert _px(img, 1, 1) == (255, 255, 255)


# ── the widget ───────────────────────────────────────────────────────

def test_model_view_basics():
    from src.model_view import ModelView
    m = step.load(ASM, use_cache=False)
    v = ModelView()
    v.resize(600, 450)
    v.show()
    v.set_model(m)
    _pump(0.2)
    assert v.renderer in ("opengl", "software")
    img = v.grab_image()
    assert img is not None and not img.isNull()

    picked = []
    v.partSelected.connect(picked.append)
    v.select("Bracket")
    assert picked == ["Bracket"]
    v.set_part_visible("Bracket", False)    # hiding the selection clears it
    assert v.selected is None and picked[-1] == ""
    assert not m.part("Bracket").visible
    v.set_all_visible(True)
    assert all(p.visible for p in m.parts)

    v.set_view("front")
    assert v.camera.view_name() == "front"
    v.set_up_axis("y")
    assert v.camera.up == "y" and v.camera.view_name() == "iso"
    v.set_up_axis("z")
    v.close()


def test_gl_failure_falls_back_to_software():
    """Whatever the GL driver does, the panel is never left blank."""
    from src.model_view import ModelView
    m = step.load(SOLID, use_cache=False)
    v = ModelView()
    v.resize(400, 300)
    v.show()
    v.set_model(m)
    _pump(0.1)
    v._fall_back("simulated driver failure")
    _pump(0.1)
    assert v.renderer == "software"
    assert v._software_reason
    img = v.grab_image()
    assert img is not None and not img.isNull()
    v.close()


def test_snapshot_transparency_and_xyz_indicator():
    from src.model_view import ModelView
    m = step.load(ASM, use_cache=False)
    v = ModelView()
    v.resize(400, 300)
    v.show()
    v.set_model(m)
    v.select("Bracket")
    _pump(0.1)
    lw, lh = v._view.width(), v._view.height()

    clear = v.snapshot(transparent=True, triad=False)
    assert (clear.width(), clear.height()) == (lw * 2, lh * 2)   # 2x screen
    assert clear.pixelColor(2, 2).alpha() == 0                   # background
    cx, cy, _ = v.camera.project([[50, 30, 30]])[0]              # the bracket
    mid = clear.pixelColor(int(cx * 2), int(cy * 2))
    assert mid.alpha() == 255
    assert mid.blue() > mid.red(), "selection highlight leaked into the snapshot"

    backdrop = v.snapshot(transparent=False, triad=False)
    assert backdrop.pixelColor(2, 2).alpha() == 255

    # The indicator sits bottom-left; with it off that corner is empty.
    corner = (int(40 * 2), int((lh - 40) * 2))

    def ink(img):
        x0, y0 = corner
        return sum(img.pixelColor(x, y).alpha() > 0
                   for x in range(x0 - 10, x0 + 60, 2)
                   for y in range(y0 - 60, y0 + 10, 2))
    assert ink(clear) == 0
    assert ink(v.snapshot(transparent=True, triad=True)) > 20

    # Clipboard keeps the alpha: a PNG goes on alongside the bitmap.
    v.copy_to_clipboard()
    assert QApplication.clipboard().mimeData().hasFormat("image/png")
    v.close()


def test_jpeg_export_is_flattened_onto_white():
    from src.main_window import flatten_for_jpeg
    from src.model_view import ModelView
    m = step.load(ASM, use_cache=False)
    v = ModelView()
    v.resize(300, 200)
    v.show()
    v.set_model(m)
    _pump(0.1)
    img, flattened = flatten_for_jpeg(v.snapshot(transparent=True))
    assert flattened and not img.hasAlphaChannel()
    c = img.pixelColor(2, 2)
    assert (c.red(), c.green(), c.blue()) == (255, 255, 255)     # not black
    out = _TMP / "snap.jpg"
    assert img.save(str(out), "JPG", 92) and out.stat().st_size > 0
    _, flattened = flatten_for_jpeg(v.snapshot(transparent=False))
    assert not flattened
    v.close()


def test_snapshot_options_are_remembered():
    from src import snapshot_prefs
    t0, a0 = snapshot_prefs.transparent(), snapshot_prefs.triad()
    try:
        snapshot_prefs.set_transparent(False)
        snapshot_prefs.set_triad(True)
        assert not snapshot_prefs.transparent() and snapshot_prefs.triad()
    finally:
        snapshot_prefs.set_transparent(t0)
        snapshot_prefs.set_triad(a0)


def test_software_pick_finds_the_part_under_the_cursor():
    from src.model_view import ModelView, _SoftView
    m = step.load(ASM, use_cache=False)
    v = ModelView()
    v.resize(600, 450)
    v.show()
    v.set_model(m)
    v.set_view("top")
    _pump(0.1)
    soft = _SoftView(v, v.camera)
    x, y, _ = v.camera.project([[50, 30, 70]])[0]      # the knob, from above
    soft._pick(x, y)
    assert v.selected == "Knob"
    soft._pick(2, 2)                                   # empty space
    assert v.selected is None
    v.close()


# ── the main window ──────────────────────────────────────────────────

def test_main_window_switches_to_3d_and_back():
    QCoreApplication.setOrganizationName("DWGViewerTests")
    QCoreApplication.setApplicationName("test_step")
    from src.main_window import MainWindow
    import ezdxf
    dxf = _TMP / "plain.dxf"
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (10, 10))
    doc.saveas(dxf)

    with _private_cache():
        w = MainWindow()
        w.resize(1200, 800)
        w.show()
        _pump(0.1)
        w._open_file(str(ASM))
        for _ in range(300):
            _pump(0.02)
            if w._model is not None:
                break
        assert w._model is not None, w._file_label.text()
        assert w._center.currentWidget() is w._model_view
        assert w._layers.title == "Parts"
        assert not w._print_btn.isEnabled()
        assert not w._act_measure.isEnabled()
        assert not any(a.isEnabled() for a in w._markup_actions.values())
        assert "4 parts" in w._file_label.text()

        # View keys reach the 3D view, and do not collide with the
        # window's own shortcuts (Ctrl+1 / Ctrl+3 are panel toggles).
        from PyQt6.QtTest import QTest
        from PyQt6.QtCore import Qt
        w.activateWindow()
        QTest.qWaitForWindowActive(w, 1000)
        w._model_view._view.setFocus()
        _pump(0.05)
        QTest.keyClick(w._model_view._view, Qt.Key.Key_5)
        _pump(0.05)
        assert w._model_view.camera.view_name() == "top"
        QTest.keyClick(w._model_view._view, Qt.Key.Key_F)
        _pump(0.05)
        # E is Ellipse on the (now disabled) markup bar and Edges here.
        edges = w._model_view.show_edges
        QTest.keyClick(w._model_view._view, Qt.Key.Key_E)
        _pump(0.05)
        assert w._model_view.show_edges is not edges
        w._model_view.set_edges(edges)

        w._on_layer_toggled("Knob", False)
        assert not w._model.part("Knob").visible

        w._copy_view()
        assert not QApplication.clipboard().image().isNull()

        w._open_file(str(dxf))
        for _ in range(300):
            _pump(0.02)
            if w._current_conv is not None:
                break
        assert w._model is None
        assert w._center.currentWidget() is w._canvas
        assert w._layers.title == "Layers"
        assert w._print_btn.isEnabled() and w._act_measure.isEnabled()
        assert any(a.isEnabled() for a in w._markup_actions.values())
        w.close()
        _pump(0.1)


def _main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {exc!r}")
        else:
            print(f"  ok   {name}")
    print("all passed" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
