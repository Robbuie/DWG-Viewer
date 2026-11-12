"""Orthographic camera for the 3D model view.

Pure numpy, no Qt and no OpenGL, so the view maths can be tested on a
machine with neither. Both renderers — the OpenGL one and the software
fallback — drive the same :class:`Camera`, so a model looks the same
whichever one is drawing it.

Conventions: ``rotation`` is world -> view, rows are the view's right,
up and toward-the-viewer axes. ``half_height`` is half the height of
the visible region in world units (orthographic, so that is the whole
of "zoom").
"""
from __future__ import annotations

import math

import numpy as np

# Mechanical CAD is split on which way is up: Creo, NX, CATIA and most
# STEP from them are Z-up; SolidWorks, Inventor and Fusion keep Y-up.
UP_AXES = ("z", "y")

PRESETS = ("iso", "front", "back", "top", "bottom", "left", "right")


def _norm(v) -> np.ndarray:
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    return v / n if n else v


def look_rotation(look, up) -> np.ndarray:
    """Rotation for a camera looking along ``look`` with ``up`` roughly up."""
    f = _norm(look)
    r = np.cross(f, up)
    if np.linalg.norm(r) < 1e-9:              # looking straight along up
        r = np.cross(f, [0, 1, 0] if abs(f[1]) < 0.9 else [1, 0, 0])
    r = _norm(r)
    u = np.cross(r, f)
    return np.array([r, u, -f])


# (look direction, up) for each preset, Z-up then Y-up.
_PRESET_DIRS = {
    "z": {
        "front":  ((0, 1, 0), (0, 0, 1)),
        "back":   ((0, -1, 0), (0, 0, 1)),
        "right":  ((-1, 0, 0), (0, 0, 1)),
        "left":   ((1, 0, 0), (0, 0, 1)),
        "top":    ((0, 0, -1), (0, 1, 0)),
        "bottom": ((0, 0, 1), (0, -1, 0)),
        "iso":    ((-1, 1, -1), (0, 0, 1)),
    },
    "y": {
        "front":  ((0, 0, -1), (0, 1, 0)),
        "back":   ((0, 0, 1), (0, 1, 0)),
        "right":  ((-1, 0, 0), (0, 1, 0)),
        "left":   ((1, 0, 0), (0, 1, 0)),
        "top":    ((0, -1, 0), (0, 0, -1)),
        "bottom": ((0, 1, 0), (0, 0, 1)),
        "iso":    ((-1, -1, -1), (0, 1, 0)),
    },
}


def up_vector(up: str) -> np.ndarray:
    return np.array([0.0, 0.0, 1.0]) if up == "z" else np.array([0.0, 1.0, 0.0])


def preset_rotation(name: str, up: str = "z") -> np.ndarray:
    look, upv = _PRESET_DIRS[up if up in _PRESET_DIRS else "z"][name]
    return look_rotation(look, upv)


def axis_rotation(axis, angle: float) -> np.ndarray:
    """Rodrigues: rotation of ``angle`` radians about unit ``axis``."""
    x, y, z = _norm(axis)
    c, s = math.cos(angle), math.sin(angle)
    C = 1 - c
    return np.array([
        [c + x * x * C,     x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C,     y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


class Camera:
    MIN_HALF = 1e-6

    def __init__(self, up: str = "z"):
        self.up = up
        self.rotation = preset_rotation("iso", up)
        self.target = np.zeros(3)
        self.half_height = 1.0
        self.width = 800
        self.height = 600
        self._lo = np.full(3, -1.0)
        self._hi = np.full(3, 1.0)

    def copy(self) -> "Camera":
        c = Camera(self.up)
        c.rotation = self.rotation.copy()
        c.target = self.target.copy()
        c.half_height = self.half_height
        c.width, c.height = self.width, self.height
        c._lo, c._hi = self._lo.copy(), self._hi.copy()
        return c

    # ── viewport ─────────────────────────────────────────────────────
    def resize(self, width: int, height: int) -> None:
        self.width, self.height = max(1, int(width)), max(1, int(height))

    @property
    def aspect(self) -> float:
        return self.width / self.height

    @property
    def half_width(self) -> float:
        return self.half_height * self.aspect

    @property
    def pixels_per_unit(self) -> float:
        return self.height / (2 * self.half_height)

    # ── framing ──────────────────────────────────────────────────────
    def set_bounds(self, lo, hi) -> None:
        self._lo, self._hi = np.asarray(lo, float), np.asarray(hi, float)

    def corners(self) -> np.ndarray:
        lo, hi = self._lo, self._hi
        return np.array([[x, y, z] for x in (lo[0], hi[0])
                         for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])

    def fit(self, margin: float = 0.06) -> None:
        """Frame the bounds as they project in the current direction."""
        pts = self.corners() @ self.rotation.T
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        centre_view = (lo + hi) / 2
        # Back to world: only x and y of the centre matter, depth is
        # handled by the clip range.
        self.target = self.rotation.T @ np.array([centre_view[0], centre_view[1],
                                                  0.0]) + \
            self.rotation[2] * ((self.rotation[2] @ (self._lo + self._hi) / 2))
        span = np.maximum(hi - lo, 1e-9)
        half_h = max(span[1] / 2, span[0] / 2 / self.aspect)
        self.half_height = max(half_h * (1 + 2 * margin), self.MIN_HALF)

    def set_preset(self, name: str) -> None:
        self.rotation = preset_rotation(name, self.up)
        self.fit()

    def set_up(self, up: str) -> None:
        self.up = up if up in UP_AXES else "z"

    def depth_range(self) -> tuple[float, float]:
        """Near and far planes (view z) that hold the whole model with room
        to spare, whatever it is rotated to."""
        centre = (self._lo + self._hi) / 2
        radius = max(np.linalg.norm(self._hi - self._lo) / 2, 1e-6)
        dz = self.rotation[2] @ (centre - self.target)
        return dz - radius * 1.5, dz + radius * 1.5

    # ── interaction ──────────────────────────────────────────────────
    def orbit(self, dx_px: float, dy_px: float) -> None:
        """Turntable: horizontal drag spins about the world up axis,
        vertical drag tilts about the screen's horizontal. Never rolls,
        which is what keeps a model from ending up on its side."""
        k = math.pi / max(self.height, 1)      # half a turn per view height
        spin = axis_rotation(up_vector(self.up), dx_px * k)
        tilt = axis_rotation([1, 0, 0], dy_px * k)
        r = tilt @ self.rotation @ spin
        # Re-orthonormalise so a long drag does not drift.
        u, _, vt = np.linalg.svd(r)
        self.rotation = u @ vt

    def pan(self, dx_px: float, dy_px: float) -> None:
        s = 1 / self.pixels_per_unit
        self.target = self.target - self.rotation.T @ np.array([dx_px * s,
                                                                -dy_px * s, 0])

    def zoom(self, factor: float, at_px: tuple[float, float] | None = None) -> None:
        """Zoom by ``factor`` (>1 is closer), keeping the point under the
        cursor where it is."""
        if at_px is not None:
            before = self.unproject(*at_px)
        self.half_height = max(self.half_height / factor, self.MIN_HALF)
        if at_px is not None:
            after = self.unproject(*at_px)
            self.target = self.target + (before - after)

    # ── projection ───────────────────────────────────────────────────
    def unproject(self, x_px: float, y_px: float) -> np.ndarray:
        """World point on the plane through the target under a pixel."""
        s = 1 / self.pixels_per_unit
        vx = (x_px - self.width / 2) * s
        vy = (self.height / 2 - y_px) * s
        return self.target + self.rotation.T @ np.array([vx, vy, 0.0])

    def project(self, pts) -> np.ndarray:
        """World points -> (x_px, y_px, view depth); depth grows toward
        the viewer."""
        v = (np.asarray(pts, float) - self.target) @ self.rotation.T
        s = self.pixels_per_unit
        out = np.empty_like(v)
        out[..., 0] = self.width / 2 + v[..., 0] * s
        out[..., 1] = self.height / 2 - v[..., 1] * s
        out[..., 2] = v[..., 2]
        return out

    def modelview(self) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = self.rotation
        m[:3, 3] = -self.rotation @ self.target
        return m

    def view_name(self) -> str | None:
        """The preset the camera is exactly at, if any."""
        for name in PRESETS:
            if np.allclose(self.rotation, preset_rotation(name, self.up), atol=1e-6):
                return name
        return None
