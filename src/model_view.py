"""The 3D view STEP models open in.

Two renderers behind one widget:

* **OpenGL** (``_GLView``) — the normal path. Fixed-function GL 1.x with
  vertex buffers where the driver has them and client arrays where it
  does not, so it runs on anything down to Windows' own software GL
  over Remote Desktop. Click a part to select it.
* **Software** (``_SoftView``) — QPainter, painter's algorithm. Used when
  OpenGL cannot be had at all (no PyOpenGL, a context that will not
  create, or ``DWGV_SOFTWARE_3D=1``). Slower on big assemblies — it
  thins out small triangles while dragging — but it never leaves a
  blank panel.

The file browser's thumbnails come from :func:`render_image` too, which
is why it lives here and not in the widget.

Neither renderer is created until the first STEP file is opened: a
QOpenGLWidget anywhere in a window changes how Qt composites that whole
window, and nobody who only ever opens DWGs should pay for that.
"""
from __future__ import annotations

import os

import numpy as np
from PyQt6.QtCore import Qt, QPointF, QSize, pyqtSignal, QTimer, QSettings
from PyQt6.QtGui import (QColor, QImage, QPainter, QPen, QAction,
                         QActionGroup, QGuiApplication, QFont)
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QToolBar, QStackedLayout,
                             QToolButton, QMenu)

from src.model_camera import Camera, PRESETS
from src import theme
from src import snapshot_prefs

_EDGE_RGB = (0.10, 0.11, 0.13)
_SELECT_RGB = np.array([1.0, 0.55, 0.10], np.float32)
_LIGHT_EYE = np.array([-0.35, 0.45, 0.82])
_LIGHT_EYE = _LIGHT_EYE / np.linalg.norm(_LIGHT_EYE)
_HALF_VEC = _LIGHT_EYE + np.array([0.0, 0.0, 1.0])
_HALF_VEC = _HALF_VEC / np.linalg.norm(_HALF_VEC)

_SETTINGS_UP = "model/upAxis"
_SETTINGS_EDGES = "model/edges"


def set_clipboard_image(img: QImage) -> None:
    """Put an image with an alpha channel on the clipboard so that it
    stays transparent when pasted.

    The bitmap formats Windows apps have always read (CF_DIB) have no
    alpha, so a transparent image pasted from them lands on black. Office,
    browsers and most image editors look for a "PNG" clipboard format
    first and keep the transparency; Qt registers "image/png" under that
    name on Windows. The plain image goes on as well for everything else.
    """
    from PyQt6.QtCore import QMimeData, QByteArray, QBuffer, QIODevice
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    mime = QMimeData()
    mime.setImageData(img)
    mime.setData("image/png", data)
    mime.setData("PNG", data)
    QGuiApplication.clipboard().setMimeData(mime)


# ── Software renderer (thumbnails and the no-GL fallback) ────────────

def render_image(model, camera: Camera, width: int, height: int,
                 background: str | None = "#ffffff",
                 max_triangles: int = 400_000, edges: bool = False,
                 selected: str | None = None) -> QImage:
    """Shaded picture of ``model`` through ``camera``, without OpenGL.

    A small z-buffer rasteriser in numpy: triangles are cut down until no
    edge is longer than a few pixels, sampled on a sub-pixel grid, and
    the nearest sample wins each pixel. Normals are interpolated, so
    curved faces shade smoothly, and edges are depth-tested against the
    result, so hidden edges stay hidden. (Painting whole triangles far
    to near is simpler but wrong whenever a big face and a small one
    overlap — a base plate drawn over the bolts sitting on it.)

    Above ``max_triangles`` the smallest on screen are left out; at that
    density they are sub-pixel anyway.
    """
    camera.resize(width, height)
    rgba = np.zeros((height, width, 4), np.uint8)
    if background:
        bg = QColor(background)
        rgba[:] = (bg.red(), bg.green(), bg.blue(), 255)

    tris, nrms, cols = [], [], []
    for p in model.parts:
        if not p.visible or not len(p.vertices):
            continue
        t = p.vertices.reshape(-1, 3, 3)
        tris.append(t)
        nrms.append(p.normals.reshape(-1, 3, 3))
        if p.label == selected:
            cols.append(np.tile(_SELECT_RGB, (len(t), 1)))
        elif p.colors is not None:
            cols.append(p.colors.reshape(-1, 3, 3)[:, 0, :])
        else:
            cols.append(np.tile(np.array(p.color, np.float32), (len(t), 1)))

    zbuf = np.full(height * width, -np.inf)
    if tris:
        scr = camera.project(np.concatenate(tris).astype(np.float64))
        nv = np.concatenate(nrms).astype(np.float64) @ camera.rotation.T
        col = np.concatenate(cols).astype(np.float64)
        xs, ys = scr[:, :, 0], scr[:, :, 1]
        keep = (xs.max(1) >= 0) & (xs.min(1) <= width) & \
               (ys.max(1) >= 0) & (ys.min(1) <= height)
        scr, nv, col = scr[keep], nv[keep], col[keep]
        if len(scr) > max_triangles:
            e1 = scr[:, 1, :2] - scr[:, 0, :2]
            e2 = scr[:, 2, :2] - scr[:, 0, :2]
            area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])
            top = np.argpartition(area, -max_triangles)[-max_triangles:]
            scr, nv, col = scr[top], nv[top], col[top]
        _raster(scr, nv, col, width, height, rgba, zbuf)

    if edges and tris:
        _raster_edges(model, camera, width, height, rgba, zbuf)

    img = QImage(rgba.data, width, height, 4 * width,
                 QImage.Format.Format_RGBA8888).copy()
    return img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


_MAX_EDGE_PX = 8.0       # triangles are split until no edge is longer
_SPACING_PX = 0.7        # sample spacing — under a pixel, so no pinholes
_ROLL = np.array([[0, 1, 2], [1, 2, 0], [2, 0, 1]])
_GRIDS: dict[int, np.ndarray] = {}


def _bary(k: int) -> np.ndarray:
    g = _GRIDS.get(k)
    if g is None:
        pts = [(i / k, j / k) for i in range(k + 1) for j in range(k + 1 - i)]
        g = np.array([(a, b, 1 - a - b) for a, b in pts])
        _GRIDS[k] = g
    return g


def _edge_lengths(scr):
    d01 = np.hypot(*(scr[:, 1, :2] - scr[:, 0, :2]).T)
    d12 = np.hypot(*(scr[:, 2, :2] - scr[:, 1, :2]).T)
    d20 = np.hypot(*(scr[:, 0, :2] - scr[:, 2, :2]).T)
    return np.stack([d01, d12, d20], axis=1)


def _raster(scr, nv, col, w, h, rgba, zbuf, budget: int = 6_000_000):
    # 1. Split on the longest edge until every edge is short.
    for _ in range(24):
        el = _edge_lengths(scr)
        big = el.max(1) > _MAX_EDGE_PX
        if not big.any() or len(scr) > budget // 10:
            break
        k = np.argmax(el[big], axis=1)
        perm = _ROLL[k][:, :, None]
        sb = np.take_along_axis(scr[big], perm, axis=1)
        nb = np.take_along_axis(nv[big], perm, axis=1)
        sm = (sb[:, 0] + sb[:, 1]) / 2
        nm = (nb[:, 0] + nb[:, 1]) / 2
        a_s = np.stack([sb[:, 0], sm, sb[:, 2]], 1)
        b_s = np.stack([sm, sb[:, 1], sb[:, 2]], 1)
        a_n = np.stack([nb[:, 0], nm, nb[:, 2]], 1)
        b_n = np.stack([nm, nb[:, 1], nb[:, 2]], 1)
        cb = col[big]
        scr = np.concatenate([scr[~big], a_s, b_s])
        nv = np.concatenate([nv[~big], a_n, b_n])
        col = np.concatenate([col[~big], cb, cb])

    # 2. Sample each triangle on a barycentric grid fine enough for its size.
    kk = np.clip(np.ceil(_edge_lengths(scr).max(1) / _SPACING_PX), 1, 12).astype(int)
    lin_all, z_all, rgb_all = [], [], []
    for k in np.unique(kk):
        sel = kk == k
        g = _bary(int(k))
        pts = np.matmul(g, scr[sel]).reshape(-1, 3)
        nrm = np.matmul(g, nv[sel]).reshape(-1, 3)
        c = np.repeat(col[sel], len(g), axis=0)
        px = np.floor(pts[:, 0]).astype(np.int64)
        py = np.floor(pts[:, 1]).astype(np.int64)
        ok = (px >= 0) & (px < w) & (py >= 0) & (py < h)
        ln = np.linalg.norm(nrm[ok], axis=1)
        n = nrm[ok] / np.where(ln > 0, ln, 1)[:, None]
        lam = np.abs(n @ _LIGHT_EYE)                 # two-sided
        shade = 0.28 + 0.57 * lam + 0.15 * np.abs(n[:, 2])
        spec = np.clip(np.abs(n @ _HALF_VEC), 0, 1) ** 40 * 0.18
        lin_all.append(py[ok] * w + px[ok])
        z_all.append(pts[ok, 2])
        rgb_all.append(np.clip(c[ok] * shade[:, None] + spec[:, None], 0, 1))
    if not lin_all:
        return
    lin = np.concatenate(lin_all)
    z = np.concatenate(z_all)
    rgb = np.concatenate(rgb_all)

    # 3. Nearest sample per pixel (view z grows toward the viewer).
    order = np.lexsort((-z, lin))
    lin_s = lin[order]
    first = np.ones(len(lin_s), bool)
    first[1:] = lin_s[1:] != lin_s[:-1]
    win = order[first]
    zbuf[lin[win]] = z[win]
    flat = rgba.reshape(-1, 4)
    flat[lin[win], :3] = (rgb[win] * 255).astype(np.uint8)
    flat[lin[win], 3] = 255


def _raster_edges(model, camera, w, h, rgba, zbuf):
    segs = [p.edges for p in model.parts if p.visible and len(p.edges)]
    if not segs:
        return
    e = camera.project(np.concatenate(segs).astype(np.float64)).reshape(-1, 2, 3)
    a, b = e[:, 0], e[:, 1]
    length = np.hypot(b[:, 0] - a[:, 0], b[:, 1] - a[:, 1])
    n = np.clip(np.ceil(length / _SPACING_PX), 1, 4000).astype(np.int64) + 1
    seg = np.repeat(np.arange(len(e)), n)
    start = np.repeat(np.cumsum(n) - n, n)
    t = (np.arange(n.sum()) - start) / np.repeat(n - 1, n).clip(1)
    pts = a[seg] + (b[seg] - a[seg]) * t[:, None]
    px = np.floor(pts[:, 0]).astype(np.int64)
    py = np.floor(pts[:, 1]).astype(np.int64)
    ok = (px >= 0) & (px < w) & (py >= 0) & (py < h)
    lin = py[ok] * w + px[ok]
    lo, hi = camera.depth_range()
    eps = 2.5 / camera.pixels_per_unit + 0.002 * (hi - lo)
    vis = pts[ok, 2] >= zbuf[lin] - eps
    flat = rgba.reshape(-1, 4)
    hit = lin[vis]
    edge = (np.array(_EDGE_RGB) * 255).astype(np.int32)
    cur = flat[hit, :3].astype(np.int32)
    flat[hit, :3] = ((cur * 35 + edge * 65) // 100).astype(np.uint8)
    flat[hit, 3] = 255


def render_thumbnail(model, width: int, height: int, background: str = "#ffffff",
                     up: str = "z") -> QImage:
    cam = Camera(up)
    cam.resize(width, height)
    lo, hi = model.bounds()
    cam.set_bounds(lo, hi)
    cam.set_preset("iso")
    return render_image(model, cam, width, height, background, edges=False)


def _axis_triad(painter: QPainter, camera: Camera, x0: float, y0: float,
                length: float = 26, scale: float = 1.0) -> None:
    """Little XYZ gizmo in the corner, so a rotated part keeps its bearings.
    ``scale`` sizes it for a snapshot rendered above screen resolution."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    x0, y0, length = x0 * scale, y0 * scale, length * scale
    font = QFont()
    font.setPixelSize(max(1, round(11 * scale)))
    font.setBold(True)
    painter.setFont(font)
    axes = (("X", (1, 0, 0), QColor(220, 70, 60)),
            ("Y", (0, 1, 0), QColor(70, 170, 70)),
            ("Z", (0, 0, 1), QColor(70, 120, 230)))
    items = []
    for name, vec, col in axes:
        v = camera.rotation @ np.array(vec, float)
        items.append((v[2], name, v, col))
    for _, name, v, col in sorted(items, key=lambda t: t[0]):
        end = QPointF(x0 + v[0] * length, y0 - v[1] * length)
        painter.setPen(QPen(col, 2 * scale))
        painter.drawLine(QPointF(x0, y0), end)
        painter.drawText(QPointF(end.x() + (3 * v[0] - 3) * scale,
                                 end.y() + (-3 * v[1] + 4) * scale), name)


def _theme_background() -> tuple[QColor, QColor]:
    t = theme.current()
    top = QColor(t.get("bg-2", "#2a2f38"))
    bottom = QColor(t.get("canvas-bg", "#101216"))
    return top, bottom


# ── Shared mouse handling ─────────────────────────────────────────────

class _Interaction:
    """Mouse and wheel to camera, the same for both renderers.

    Left-drag rotates; middle-drag, Shift+left or Alt+left pans (middle
    and Alt+left are what pan the 2D sheets too); the wheel zooms about
    the cursor; double-click fits. A left click that does not move
    selects the part under it.
    """
    CLICK_SLOP = 4

    def _init_interaction(self):
        self._drag = None          # ("orbit"|"pan", last pos)
        self._press_pos = None
        self._moved = False

    def _pos(self, event):
        p = event.position()
        return p.x(), p.y()

    def mousePressEvent(self, event):
        b = event.button()
        mods = event.modifiers()
        pos = self._pos(event)
        if b == Qt.MouseButton.MiddleButton or (
                b == Qt.MouseButton.LeftButton and
                mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier)):
            self._drag = ("pan", pos)
        elif b == Qt.MouseButton.LeftButton:
            self._drag = ("orbit", pos)
        self._press_pos, self._moved = pos, False
        self.setFocus()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            return
        kind, (lx, ly) = self._drag
        x, y = self._pos(event)
        if self._press_pos and (abs(x - self._press_pos[0]) + abs(y - self._press_pos[1])
                                > self.CLICK_SLOP):
            self._moved = True
        if kind == "orbit":
            self.camera.orbit(x - lx, y - ly)
        else:
            self.camera.pan(x - lx, y - ly)
        self._drag = (kind, (x, y))
        self._camera_changed(interactive=True)

    def mouseReleaseEvent(self, event):
        was = self._drag
        self._drag = None
        if was and was[0] == "orbit" and not self._moved and \
                event.button() == Qt.MouseButton.LeftButton:
            self._pick(*self._pos(event))
        self._camera_changed(interactive=False)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.camera.fit()
            self._camera_changed(interactive=False)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.camera.zoom(1.2 ** steps, self._pos(event))
            self._camera_changed(interactive=False)
        event.accept()

    def keyPressEvent(self, event):
        k = event.key()
        if k in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.owner.pageNavigateRequested.emit(1)
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.owner.pageNavigateRequested.emit(-1)
        elif k == Qt.Key.Key_Escape:
            self.owner.select(None)
        else:
            super().keyPressEvent(event)


# ── OpenGL renderer ───────────────────────────────────────────────────

try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
except Exception:                      # pragma: no cover
    QOpenGLWidget = None


def _gl():
    """PyOpenGL, or None. Imported lazily: it probes the platform's GL
    library on import, which is wasted work for anyone not opening STEP."""
    if os.environ.get("DWGV_SOFTWARE_3D") == "1" or QOpenGLWidget is None:
        return None
    try:
        import OpenGL
        OpenGL.ERROR_CHECKING = False      # per-call glGetError is slow
        from OpenGL import GL
        return GL
    except Exception:
        return None


if QOpenGLWidget is not None:
    class _GLView(_Interaction, QOpenGLWidget):
        failed = pyqtSignal(str)

        def __init__(self, owner, camera):
            super().__init__(owner)
            self.owner, self.camera = owner, camera
            self.GL = _gl()
            self._init_interaction()
            self._buffers: dict[str, list] = {}   # part label -> GL buffer ids
            self._use_vbo = False
            self._ok = False
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setMinimumSize(QSize(100, 100))

        # Camera changes just repaint.
        def _camera_changed(self, interactive):
            self.update()

        def initializeGL(self):
            GL = self.GL
            try:
                ver = GL.glGetString(GL.GL_VERSION)
                self._use_vbo = bool(GL.glGenBuffers) and ver is not None and \
                    not ver.startswith(b"1.1")
                GL.glEnable(GL.GL_DEPTH_TEST)
                GL.glDepthFunc(GL.GL_LEQUAL)
                GL.glEnable(GL.GL_LIGHTING)
                GL.glEnable(GL.GL_LIGHT0)
                GL.glEnable(GL.GL_LIGHT1)
                GL.glLightModeli(GL.GL_LIGHT_MODEL_TWO_SIDE, GL.GL_TRUE)
                GL.glLightModelfv(GL.GL_LIGHT_MODEL_AMBIENT, (0.22, 0.22, 0.24, 1))
                GL.glLightfv(GL.GL_LIGHT0, GL.GL_DIFFUSE, (0.62, 0.62, 0.62, 1))
                GL.glLightfv(GL.GL_LIGHT0, GL.GL_SPECULAR, (0.25, 0.25, 0.25, 1))
                GL.glLightfv(GL.GL_LIGHT1, GL.GL_DIFFUSE, (0.25, 0.25, 0.27, 1))
                GL.glLightfv(GL.GL_LIGHT1, GL.GL_SPECULAR, (0, 0, 0, 1))
                GL.glEnable(GL.GL_COLOR_MATERIAL)
                GL.glColorMaterial(GL.GL_FRONT_AND_BACK, GL.GL_AMBIENT_AND_DIFFUSE)
                GL.glMaterialfv(GL.GL_FRONT_AND_BACK, GL.GL_SPECULAR, (0.3, 0.3, 0.3, 1))
                GL.glMaterialf(GL.GL_FRONT_AND_BACK, GL.GL_SHININESS, 40.0)
                GL.glEnable(GL.GL_NORMALIZE)
                GL.glShadeModel(GL.GL_SMOOTH)
                # Push faces back a hair so edges drawn at the same depth win.
                GL.glEnable(GL.GL_POLYGON_OFFSET_FILL)
                GL.glPolygonOffset(1.0, 1.0)
                self._ok = True
                self.owner._gl_info = ver.decode(errors="replace") if ver else ""
            except Exception as exc:
                self._ok = False
                # Bound now: the name is gone once the except block ends.
                QTimer.singleShot(0, lambda e=str(exc): self.failed.emit(e))

        def resizeGL(self, w, h):
            self.camera.resize(self.width(), self.height())

        # ── buffers ──────────────────────────────────────────────────
        def drop_buffers(self):
            if not self._buffers or not self._use_vbo:
                self._buffers.clear()
                return
            self.makeCurrent()
            try:
                ids = [b for bufs in self._buffers.values() for b in bufs if b]
                if ids:
                    self.GL.glDeleteBuffers(len(ids), ids)
            except Exception:
                pass
            self._buffers.clear()
            self.doneCurrent()

        def _vbo(self, arr):
            GL = self.GL
            buf = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, buf)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, arr.nbytes, arr, GL.GL_STATIC_DRAW)
            return buf

        def _part_buffers(self, part):
            bufs = self._buffers.get(part.label)
            if bufs is None:
                v = np.ascontiguousarray(part.vertices, np.float32)
                n = np.ascontiguousarray(part.normals, np.float32)
                c = None if part.colors is None else \
                    np.ascontiguousarray(part.colors, np.float32)
                e = np.ascontiguousarray(part.edges, np.float32)
                if self._use_vbo:
                    bufs = [self._vbo(v) if len(v) else None,
                            self._vbo(n) if len(n) else None,
                            self._vbo(c) if c is not None and len(c) else None,
                            self._vbo(e) if len(e) else None]
                    self.GL.glBindBuffer(self.GL.GL_ARRAY_BUFFER, 0)
                else:
                    bufs = [v, n, c, e]          # client arrays
                self._buffers[part.label] = bufs
            return bufs

        def _pointer(self, fn, buf, *args):
            GL = self.GL
            if self._use_vbo:
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, buf)
                fn(*args, None)
            else:
                fn(*args, buf)

        # ── drawing ──────────────────────────────────────────────────
        def _matrices(self, cam=None):
            GL = self.GL
            cam = cam or self.camera
            near, far = cam.depth_range()
            GL.glMatrixMode(GL.GL_PROJECTION)
            GL.glLoadIdentity()
            # View z points at the viewer; GL's near/far are distances
            # in front of it, hence the negation.
            GL.glOrtho(-cam.half_width, cam.half_width,
                       -cam.half_height, cam.half_height, -far, -near)
            GL.glMatrixMode(GL.GL_MODELVIEW)
            GL.glLoadIdentity()
            # Lights are given in eye space: a headlight that follows the view.
            GL.glLightfv(GL.GL_LIGHT0, GL.GL_POSITION, (*_LIGHT_EYE, 0.0))
            GL.glLightfv(GL.GL_LIGHT1, GL.GL_POSITION, (0.4, -0.6, 0.5, 0.0))
            GL.glLoadMatrixd(cam.modelview().T.flatten())

        def _draw_parts(self, pick=False, highlight=True):
            GL = self.GL
            model = self.owner.model
            GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
            for i, part in enumerate(model.parts):
                if not part.visible or not len(part.vertices):
                    continue
                v, n, c, _ = self._part_buffers(part)
                self._pointer(GL.glVertexPointer, v, 3, GL.GL_FLOAT, 0)
                if pick:
                    idx = i + 1
                    GL.glColor3ub(idx & 255, (idx >> 8) & 255, (idx >> 16) & 255)
                else:
                    GL.glEnableClientState(GL.GL_NORMAL_ARRAY)
                    self._pointer(GL.glNormalPointer, n, GL.GL_FLOAT, 0)
                    if highlight and part.label == self.owner.selected:
                        GL.glColor3f(*_SELECT_RGB)
                    elif c is not None:
                        GL.glEnableClientState(GL.GL_COLOR_ARRAY)
                        self._pointer(GL.glColorPointer, c, 3, GL.GL_FLOAT, 0)
                    else:
                        GL.glColor3f(*part.color)
                GL.glDrawArrays(GL.GL_TRIANGLES, 0, len(part.vertices))
                GL.glDisableClientState(GL.GL_NORMAL_ARRAY)
                GL.glDisableClientState(GL.GL_COLOR_ARRAY)
            GL.glDisableClientState(GL.GL_VERTEX_ARRAY)
            if self._use_vbo:
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

        def _draw_edges(self):
            GL = self.GL
            GL.glDisable(GL.GL_LIGHTING)
            GL.glColor3f(*_EDGE_RGB)
            GL.glLineWidth(1.0)
            GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
            for part in self.owner.model.parts:
                if not part.visible or not len(part.edges):
                    continue
                e = self._part_buffers(part)[3]
                self._pointer(GL.glVertexPointer, e, 3, GL.GL_FLOAT, 0)
                GL.glDrawArrays(GL.GL_LINES, 0, len(part.edges))
            GL.glDisableClientState(GL.GL_VERTEX_ARRAY)
            if self._use_vbo:
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
            GL.glEnable(GL.GL_LIGHTING)

        def paintGL(self):
            if not self._ok:
                return
            GL = self.GL
            painter = QPainter(self)
            top, bottom = _theme_background()
            from PyQt6.QtGui import QLinearGradient
            grad = QLinearGradient(0, 0, 0, self.height())
            grad.setColorAt(0, top)
            grad.setColorAt(1, bottom)
            painter.fillRect(self.rect(), grad)
            painter.beginNativePainting()
            try:
                self._restore_state()
                GL.glClear(GL.GL_DEPTH_BUFFER_BIT)
                if self.owner.model is not None:
                    self._matrices()
                    self._draw_parts()
                    if self.owner.show_edges:
                        self._draw_edges()
            except Exception as exc:
                self._ok = False
                QTimer.singleShot(0, lambda e=str(exc): self.failed.emit(e))
            finally:
                # Hand QPainter back the state it expects; with depth
                # testing left on its glyph quads vanish.
                for cap in (GL.GL_DEPTH_TEST, GL.GL_LIGHTING,
                            GL.GL_POLYGON_OFFSET_FILL, GL.GL_COLOR_MATERIAL):
                    GL.glDisable(cap)
                painter.endNativePainting()
            if self.owner.model is not None:
                _axis_triad(painter, self.camera, 40, self.height() - 40)
            painter.end()

        def _restore_state(self, size=None):
            """QPainter leaves GL state of its own behind; put back ours."""
            GL = self.GL
            if size is None:
                dpr = self.devicePixelRatioF()
                size = (int(self.width() * dpr), int(self.height() * dpr))
            GL.glViewport(0, 0, *size)
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glDepthMask(GL.GL_TRUE)
            GL.glDisable(GL.GL_BLEND)
            GL.glDisable(GL.GL_SCISSOR_TEST)
            GL.glDisable(GL.GL_STENCIL_TEST)
            GL.glDisable(GL.GL_TEXTURE_2D)
            GL.glDisable(GL.GL_CULL_FACE)
            GL.glEnable(GL.GL_LIGHTING)
            GL.glEnable(GL.GL_COLOR_MATERIAL)
            GL.glEnable(GL.GL_POLYGON_OFFSET_FILL)
            GL.glUseProgram(0)

        def _pick(self, x, y):
            if not self._ok or self.owner.model is None:
                return
            GL = self.GL
            self.makeCurrent()
            hit = None
            try:
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
                self._restore_state()
                GL.glClearColor(0, 0, 0, 1)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
                GL.glDisable(GL.GL_LIGHTING)
                GL.glDisable(GL.GL_DITHER)
                GL.glShadeModel(GL.GL_FLAT)
                self._matrices()
                self._draw_parts(pick=True)
                GL.glFinish()
                dpr = self.devicePixelRatioF()
                px = int(x * dpr)
                py = int((self.height() - y) * dpr) - 1
                data = GL.glReadPixels(px, max(py, 0), 1, 1, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
                b = bytes(data) if not isinstance(data, np.ndarray) else data.tobytes()
                idx = b[0] | (b[1] << 8) | (b[2] << 16)
                parts = self.owner.model.parts
                if 0 < idx <= len(parts):
                    hit = parts[idx - 1].label
            except Exception:
                hit = None
            finally:
                GL.glEnable(GL.GL_DITHER)
                GL.glShadeModel(GL.GL_SMOOTH)
                self.doneCurrent()
            self.owner.select(hit)

        def grab_image(self) -> QImage:
            return self.grabFramebuffer()

        def render_offscreen(self, cam: Camera, width: int, height: int):
            """The model alone on a transparent background, at any size.

            Drawn into a multisampled framebuffer object rather than
            grabbed off the screen, so there is no backdrop to remove,
            edges are antialiased, and the size is not tied to the window.
            Returns None if this driver has no FBOs (GL 1.x); the caller
            falls back to the software renderer.
            """
            if not self._ok or self.owner.model is None:
                return None
            from PyQt6.QtOpenGL import (QOpenGLFramebufferObject,
                                        QOpenGLFramebufferObjectFormat)
            GL = self.GL
            self.makeCurrent()
            fbo = None
            try:
                if not QOpenGLFramebufferObject.hasOpenGLFramebufferObjects():
                    return None
                fmt = QOpenGLFramebufferObjectFormat()
                fmt.setAttachment(
                    QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
                fmt.setSamples(4)
                fbo = QOpenGLFramebufferObject(width, height, fmt)
                if not fbo.isValid():
                    return None
                fbo.bind()
                self._restore_state((width, height))
                GL.glClearColor(0, 0, 0, 0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
                self._matrices(cam)
                self._draw_parts(highlight=False)
                if self.owner.show_edges:
                    self._draw_edges()
                GL.glFinish()
                img = fbo.toImage()
                fbo.release()
                return None if img.isNull() else img.convertToFormat(
                    QImage.Format.Format_ARGB32_Premultiplied)
            except Exception:
                return None
            finally:
                fbo = None                     # freed while still current
                try:
                    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER,
                                         self.defaultFramebufferObject())
                except Exception:
                    pass
                self.doneCurrent()
else:                                  # pragma: no cover
    _GLView = None


# ── Software renderer widget ─────────────────────────────────────────

class _SoftView(_Interaction, QWidget):
    DRAG_TRIANGLES = 30_000
    FULL_TRIANGLES = 1_000_000

    def __init__(self, owner, camera):
        super().__init__(owner)
        self.owner, self.camera = owner, camera
        self._init_interaction()
        self._image: QImage | None = None
        self._interactive = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(QSize(100, 100))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._render)

    def drop_buffers(self):
        self._image = None

    def _camera_changed(self, interactive):
        self._interactive = interactive
        self._timer.start()

    def resizeEvent(self, event):
        self.camera.resize(self.width(), self.height())
        self._camera_changed(False)

    def _render(self):
        if self.owner.model is None:
            self._image = None
        else:
            dpr = self.devicePixelRatioF()
            # Half resolution while dragging keeps rotation responsive;
            # the full picture follows as soon as the mouse lets go.
            if self._interactive:
                dpr *= 0.5
            w, h = max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))
            cam = self.camera
            # Render at device pixels through a copy of the camera, so
            # the shared one keeps its logical-pixel viewport.
            c = Camera(cam.up)
            c.rotation, c.target = cam.rotation, cam.target
            c.half_height = cam.half_height
            c._lo, c._hi = cam._lo, cam._hi
            limit = self.DRAG_TRIANGLES if self._interactive else self.FULL_TRIANGLES
            img = render_image(self.owner.model, c, w, h, background=None,
                               max_triangles=limit,
                               edges=self.owner.show_edges and not self._interactive,
                               selected=self.owner.selected)
            if self._interactive:
                img = img.scaled(int(self.width() * self.devicePixelRatioF()),
                                 int(self.height() * self.devicePixelRatioF()))
                dpr = self.devicePixelRatioF()
            img.setDevicePixelRatio(dpr)
            self._image = img
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        top, bottom = _theme_background()
        from PyQt6.QtGui import QLinearGradient
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0, top)
        grad.setColorAt(1, bottom)
        p.fillRect(self.rect(), grad)
        if self._image is not None:
            p.drawImage(0, 0, self._image)
            _axis_triad(p, self.camera, 40, self.height() - 40)
        p.end()

    def _pick(self, x, y):
        """Nearest triangle under the cursor, by projecting on the CPU."""
        model = self.owner.model
        if model is None:
            return
        best, hit = -np.inf, None
        for p in model.parts:
            if not p.visible or not len(p.vertices):
                continue
            s = self.camera.project(p.vertices.astype(np.float64)).reshape(-1, 3, 3)
            a, b, c = s[:, 0], s[:, 1], s[:, 2]
            def side(p1, p2):
                return (p2[:, 0] - p1[:, 0]) * (y - p1[:, 1]) - \
                       (p2[:, 1] - p1[:, 1]) * (x - p1[:, 0])
            d1, d2, d3 = side(a, b), side(b, c), side(c, a)
            inside = ((d1 >= 0) & (d2 >= 0) & (d3 >= 0)) | \
                     ((d1 <= 0) & (d2 <= 0) & (d3 <= 0))
            if inside.any():
                z = s[inside][:, :, 2].mean(axis=1).max()
                if z > best:
                    best, hit = z, p.label
        self.owner.select(hit)

    def grab_image(self) -> QImage:
        return self.grab().toImage()


# ── The widget the main window holds ─────────────────────────────────

class ModelView(QWidget):
    """Toolbar of standard views over whichever renderer could start."""

    pageNavigateRequested = pyqtSignal(int)
    partSelected = pyqtSignal(str)          # label, or "" for none
    statusMessage = pyqtSignal(str)
    snapshotTaken = pyqtSignal(int, int)    # width, height
    saveImageRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model = None
        self.selected: str | None = None
        s = QSettings()
        self.show_edges = s.value(_SETTINGS_EDGES, True, type=bool)
        self.camera = Camera(str(s.value(_SETTINGS_UP, "z")))
        self._gl_info = ""
        self._software_reason = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._build_toolbar())
        self._stack_host = QWidget()
        self._stack = QStackedLayout(self._stack_host)
        lay.addWidget(self._stack_host, 1)

        self._view = None
        if _gl() is not None and _GLView is not None:
            self._view = _GLView(self, self.camera)
            self._view.failed.connect(self._fall_back)
        else:
            self._software_reason = "OpenGL is not available"
            self._view = _SoftView(self, self.camera)
        self._stack.addWidget(self._view)

    @property
    def renderer(self) -> str:
        return "opengl" if _GLView is not None and isinstance(self._view, _GLView) \
            else "software"

    def _check_gl(self):
        """QOpenGLWidget says nothing to us when it cannot get a context —
        it prints a warning and never calls initializeGL — so look."""
        v = self._view
        if _GLView is None or not isinstance(v, _GLView) or not v.isVisible():
            return
        ctx = v.context()
        if ctx is None or not ctx.isValid():
            self._fall_back("no OpenGL context could be created")

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._check_gl)

    def _fall_back(self, reason: str):
        if isinstance(self._view, _SoftView):
            return
        self._software_reason = reason
        old = self._view
        self._view = _SoftView(self, self.camera)
        self._stack.addWidget(self._view)
        self._stack.setCurrentWidget(self._view)
        old.hide()
        old.deleteLater()
        self._view._camera_changed(False)
        self.statusMessage.emit(
            "OpenGL could not start here, so the model is drawn in software — "
            "rotating big assemblies will be slower.")

    # ── toolbar ──────────────────────────────────────────────────────
    def _build_toolbar(self) -> QToolBar:
        tb = QToolBar()
        tb.setObjectName("modelToolbar")
        tb.setIconSize(QSize(16, 16))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._preset_actions = {}
        labels = {"iso": "Iso", "front": "Front", "back": "Back", "top": "Top",
                  "bottom": "Bottom", "left": "Left", "right": "Right"}
        # Plain digits: Ctrl+1 / Ctrl+3 already toggle the window's panels.
        keys = {"iso": "0", "front": "1", "back": "2", "right": "3",
                "left": "4", "top": "5", "bottom": "6"}
        for name in PRESETS:
            act = QAction(labels[name], self)
            act.setToolTip(f"{labels[name]} view ({keys[name]})")
            act.setShortcut(keys[name])
            act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            act.triggered.connect(lambda _=False, n=name: self.set_view(n))
            tb.addAction(act)
            # Also on the view itself: a WidgetWithChildren shortcut only
            # fires with focus inside a widget the action belongs to, and
            # focus is on the model, not on the toolbar.
            self.addAction(act)
            self._preset_actions[name] = act
        tb.addSeparator()
        self._act_edges = QAction("Edges", self)
        self._act_edges.setCheckable(True)
        self._act_edges.setChecked(self.show_edges)
        self._act_edges.setToolTip("Draw the model's edges (E)")
        self._act_edges.setShortcut("E")
        self._act_edges.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._act_edges.toggled.connect(self.set_edges)
        tb.addAction(self._act_edges)
        self.addAction(self._act_edges)

        up_btn = QToolButton()
        up_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(up_btn)
        group = QActionGroup(self)
        self._up_actions = {}
        for axis, text in (("z", "Z up (Creo, NX, CATIA)"),
                           ("y", "Y up (SolidWorks, Inventor, Fusion)")):
            a = QAction(text, self)
            a.setCheckable(True)
            a.setChecked(self.camera.up == axis)
            a.triggered.connect(lambda _=False, ax=axis: self.set_up_axis(ax))
            group.addAction(a)
            menu.addAction(a)
            self._up_actions[axis] = a
        up_btn.setMenu(menu)
        up_btn.setToolTip("Which axis is up — standard views and rotation follow it")
        self._up_btn = up_btn
        self._sync_up_label()
        tb.addWidget(up_btn)

        tb.addSeparator()
        snap_btn = QToolButton()
        snap_btn.setText("Snapshot")
        snap_btn.setToolTip("Copy or save the view — and choose what goes in it")
        snap_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        smenu = QMenu(snap_btn)
        a = smenu.addAction("Copy to Clipboard")
        a.triggered.connect(self._snapshot_copy)
        a = smenu.addAction("Save Image (PNG or JPG)…")
        a.triggered.connect(self.saveImageRequested.emit)
        smenu.addSeparator()
        self._act_snap_clear = smenu.addAction("Transparent Background")
        self._act_snap_clear.setCheckable(True)
        self._act_snap_clear.toggled.connect(self.set_snapshot_transparent)
        self._act_snap_triad = smenu.addAction("Include XYZ Indicator")
        self._act_snap_triad.setCheckable(True)
        self._act_snap_triad.toggled.connect(self.set_snapshot_triad)

        def sync():
            for act, on in ((self._act_snap_clear, self.snapshot_transparent()),
                            (self._act_snap_triad, self.snapshot_triad())):
                act.blockSignals(True)
                act.setChecked(on)
                act.blockSignals(False)
        smenu.aboutToShow.connect(sync)
        snap_btn.setMenu(smenu)
        tb.addWidget(snap_btn)
        return tb

    def _snapshot_copy(self):
        img = self.copy_to_clipboard()
        if img is not None:
            self.snapshotTaken.emit(img.width(), img.height())

    def _sync_up_label(self):
        self._up_btn.setText(f"{self.camera.up.upper()} up")

    # ── public API ───────────────────────────────────────────────────
    def set_model(self, model) -> None:
        self._view.drop_buffers()
        self.model = model
        self.selected = None
        if model is not None:
            lo, hi = model.bounds()
            self.camera.set_bounds(lo, hi)
            self.camera.set_preset("iso")
        self._check_gl()
        self._refresh()

    def clear(self) -> None:
        self.set_model(None)

    def set_view(self, name: str) -> None:
        self.camera.set_preset(name)
        self._refresh()

    def fit(self) -> None:
        if self.model is not None:
            self.camera.fit()
            self._refresh()

    def set_edges(self, on: bool) -> None:
        self.show_edges = bool(on)
        if self._act_edges.isChecked() != self.show_edges:
            self._act_edges.setChecked(self.show_edges)
        QSettings().setValue(_SETTINGS_EDGES, self.show_edges)
        self._refresh()

    def set_up_axis(self, axis: str) -> None:
        self.camera.set_up(axis)
        QSettings().setValue(_SETTINGS_UP, self.camera.up)
        self._up_actions[self.camera.up].setChecked(True)
        self._sync_up_label()
        self.set_view("iso")

    def set_part_visible(self, label: str, visible: bool) -> None:
        if self.model is None:
            return
        p = self.model.part(label)
        if p is not None:
            p.visible = visible
            if not visible and self.selected == label:
                self.select(None)
            self._refresh()

    def set_all_visible(self, visible: bool) -> None:
        if self.model is None:
            return
        for p in self.model.parts:
            p.visible = visible
        if not visible:
            self.select(None)
        self._refresh()

    def select(self, label: str | None) -> None:
        if label == self.selected:
            return
        self.selected = label
        self.partSelected.emit(label or "")
        self._refresh()

    def grab_image(self) -> QImage | None:
        """What is on screen, backdrop and all."""
        if self.model is None:
            return None
        return self._view.grab_image()

    # ── snapshots ────────────────────────────────────────────────────
    snapshot_transparent = staticmethod(snapshot_prefs.transparent)
    snapshot_triad = staticmethod(snapshot_prefs.triad)
    set_snapshot_transparent = staticmethod(snapshot_prefs.set_transparent)
    set_snapshot_triad = staticmethod(snapshot_prefs.set_triad)

    def snapshot(self, scale: float = 2.0, transparent: bool | None = None,
                 triad: bool | None = None) -> QImage | None:
        """The view re-rendered for export.

        At ``scale`` times the window's size, like the sheet snapshots —
        a picture pasted into a report should not look like a photograph
        of a monitor. Everything not covered by the model is transparent
        unless the backdrop is asked for; the XYZ indicator is included
        only if asked for. The selection highlight is never included:
        it is a state of the screen, not of the model.
        """
        if self.model is None:
            return None
        transparent = self.snapshot_transparent() if transparent is None else transparent
        triad = self.snapshot_triad() if triad is None else triad
        lw, lh = max(1, self._view.width()), max(1, self._view.height())
        w, h = max(1, round(lw * scale)), max(1, round(lh * scale))
        cam = self.camera.copy()
        cam.resize(lw, lh)                 # framing from the window...
        big = cam.copy()
        big.resize(w, h)                   # ...same aspect, more pixels
        img = None
        if _GLView is not None and isinstance(self._view, _GLView):
            img = self._view.render_offscreen(big, w, h)
        if img is None:
            img = render_image(self.model, big, w, h, background=None,
                               max_triangles=2_000_000,
                               edges=self.show_edges, selected=None)
        if not transparent:
            out = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
            p = QPainter(out)
            top, bottom = _theme_background()
            from PyQt6.QtGui import QLinearGradient
            grad = QLinearGradient(0, 0, 0, h)
            grad.setColorAt(0, top)
            grad.setColorAt(1, bottom)
            p.fillRect(out.rect(), grad)
            p.drawImage(0, 0, img)
            p.end()
            img = out
        if triad:
            p = QPainter(img)
            _axis_triad(p, cam, 40, lh - 40, scale=scale)
            p.end()
        return img

    def copy_to_clipboard(self) -> QImage | None:
        img = self.snapshot()
        if img is None or img.isNull():
            return None
        set_clipboard_image(img)
        return img

    def _refresh(self):
        self._view._camera_changed(False)
