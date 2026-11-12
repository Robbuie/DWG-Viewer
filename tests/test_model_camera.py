"""
test_model_camera.py — the 3D view's camera maths.

Pure numpy: no Qt, no OpenGL, no OpenCascade, so this runs anywhere.
Runs standalone (`python tests/test_model_camera.py`) or under pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.model_camera import (Camera, PRESETS, UP_AXES,  # noqa: E402
                              preset_rotation)

LO, HI = np.array([0.0, 0.0, 0.0]), np.array([100.0, 60.0, 80.0])


def _cam(up="z", view="iso", w=800, h=600):
    c = Camera(up)
    c.resize(w, h)
    c.set_bounds(LO, HI)
    c.set_preset(view)
    return c


def test_presets_are_proper_rotations():
    for up in UP_AXES:
        for name in PRESETS:
            r = preset_rotation(name, up)
            assert np.allclose(r @ r.T, np.eye(3), atol=1e-9), (up, name)
            assert np.isclose(np.linalg.det(r), 1.0), (up, name)


def test_front_view_z_up_looks_along_plus_y():
    c = _cam("z", "front")
    p = c.project([[0, 0, 0], [100, 0, 0], [0, 0, 80]])
    assert p[1, 0] > p[0, 0]          # +X to the right
    assert p[2, 1] < p[0, 1]          # +Z up the screen
    assert np.isclose(p[0, 1], p[1, 1])


def test_front_view_y_up_looks_down_minus_z():
    c = _cam("y", "front")
    p = c.project([[0, 0, 0], [100, 0, 0], [0, 60, 0], [0, 0, 80]])
    assert p[1, 0] > p[0, 0]          # +X right
    assert p[2, 1] < p[0, 1]          # +Y up
    assert p[3, 2] > p[0, 2]          # +Z toward the viewer


def test_top_view_sees_the_top():
    c = _cam("z", "top")
    p = c.project([[0, 0, 0], [0, 0, 80]])
    assert p[1, 2] > p[0, 2]          # higher points are nearer


def test_fit_puts_every_corner_on_screen():
    for view in PRESETS:
        c = _cam("z", view, 640, 480)
        pts = c.project(c.corners())
        assert pts[:, 0].min() >= -0.5 and pts[:, 0].max() <= 640.5, view
        assert pts[:, 1].min() >= -0.5 and pts[:, 1].max() <= 480.5, view
        # ...and fills it in at least one direction.
        fill = max(np.ptp(pts[:, 0]) / 640, np.ptp(pts[:, 1]) / 480)
        assert fill > 0.8, (view, fill)


def test_zoom_keeps_the_point_under_the_cursor():
    c = _cam()
    before = c.unproject(123, 456)
    c.zoom(2.5, (123, 456))
    after = c.unproject(123, 456)
    assert np.allclose(before, after, atol=1e-9)
    assert c.half_height < 44


def test_pan_moves_the_picture_with_the_mouse():
    c = _cam("z", "front")
    p0 = c.project([[50, 30, 40]])[0]
    c.pan(30, -20)
    p1 = c.project([[50, 30, 40]])[0]
    assert np.allclose(p1[:2] - p0[:2], [30, -20], atol=1e-6)


def test_orbit_never_rolls():
    """Turntable rotation: world up stays in the screen's vertical plane."""
    c = _cam()
    rng = np.random.default_rng(1)
    for _ in range(200):
        c.orbit(*rng.uniform(-40, 40, 2))
        r = c.rotation
        assert np.allclose(r @ r.T, np.eye(3), atol=1e-9)
        up_on_screen = r @ np.array([0, 0, 1.0])
        assert abs(up_on_screen[0]) < 1e-6


def test_horizontal_drag_spins_about_up():
    c = _cam("z", "front")
    c.orbit(c.height / 2, 0)          # a quarter turn
    # Spinning about Z leaves Z where it was on screen.
    assert np.allclose(c.rotation @ [0, 0, 1], [0, 1, 0], atol=1e-9)


def test_depth_range_holds_the_model_at_any_angle():
    c = _cam()
    for _ in range(50):
        c.orbit(17, 9)
        near, far = c.depth_range()
        z = (c.corners() - c.target) @ c.rotation[2]
        assert z.min() > near and z.max() < far


def test_view_name_round_trips():
    c = _cam("y", "right")
    assert c.view_name() == "right"
    c.orbit(5, 0)
    assert c.view_name() is None


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
