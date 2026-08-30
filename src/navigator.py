"""
navigator.py — overview minimap that floats over the drawing canvas.

Shows the whole sheet in miniature with a bright rectangle marking the
part of it currently on screen. The rectangle can be dragged to pan, and
dragging on empty space draws a new box that the main view zooms to — the
usual CAD navigator idiom, which beats hunting for a region by scrolling
at high magnification.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, pyqtSignal
from PyQt6.QtGui import QPainter, QPixmap, QColor, QPen, QBrush, QCursor, QFont
from PyQt6.QtWidgets import QWidget


_MAX_W, _MAX_H = 240, 190      # largest the map area may grow
_MIN_W, _MIN_H = 110, 80       # smallest, for very elongated sheets
_PAD = 6                       # frame padding around the map
_HEADER_H = 18                 # title strip with the close button
_DRAG_SLOP = 5                 # px below which a drag counts as a click


class NavigatorOverlay(QWidget):
    """Floating overview map.

    Signals
    -------
    centerRequested(QPointF)  — centre the view on this scene point
    rectRequested(QRectF)     — fit the view to this scene rectangle
    zoomRequested(int)        — wheel steps over the map (+in / -out)
    closeRequested()          — the ✕ was clicked
    """

    centerRequested = pyqtSignal(QPointF)
    rectRequested = pyqtSignal(QRectF)
    zoomRequested = pyqtSignal(int)
    closeRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)

        self._thumb: QPixmap | None = None
        self._content = QRectF()        # drawing extents, scene coords
        self._view = QRectF()           # visible region, scene coords

        # Interaction state
        self._mode = None               # 'pan' | 'band' | 'move' | None
        self._press_pos = QPoint()
        self._grab_offset = QPointF()   # cursor → box centre, scene units
        self._band: QRect | None = None
        self._moved_by_user = False     # user dragged the overlay somewhere

        self.resize(_MAX_W + 2 * _PAD, _MAX_H + 2 * _PAD + _HEADER_H)

    # ------------------------------------------------------------------ #
    #  Content
    # ------------------------------------------------------------------ #

    @property
    def moved_by_user(self) -> bool:
        return self._moved_by_user

    def set_drawing(self, thumb: QPixmap | None, content: QRectF) -> None:
        """Install a miniature of the whole sheet and its scene extents."""
        self._thumb = thumb
        self._content = QRectF(content)
        self._band = None
        self._mode = None
        if thumb is None or thumb.isNull() or content.isEmpty():
            self.hide()
            return
        w = max(_MIN_W, thumb.width())
        h = max(_MIN_H, thumb.height())
        self.resize(w + 2 * _PAD, h + 2 * _PAD + _HEADER_H)
        self.update()

    def set_view_rect(self, rect: QRectF) -> None:
        if rect == self._view:
            return
        self._view = QRectF(rect)
        self.update()

    def has_drawing(self) -> bool:
        return self._thumb is not None and not self._content.isEmpty()

    @staticmethod
    def thumb_bounds() -> tuple[int, int]:
        return _MAX_W, _MAX_H

    # ------------------------------------------------------------------ #
    #  Geometry helpers
    # ------------------------------------------------------------------ #

    def _map_rect(self) -> QRect:
        return QRect(_PAD, _HEADER_H + _PAD,
                     self.width() - 2 * _PAD,
                     self.height() - _HEADER_H - 2 * _PAD)

    def _close_rect(self) -> QRect:
        return QRect(self.width() - _HEADER_H - 2, 1, _HEADER_H, _HEADER_H - 2)

    def _scene_to_map(self, pt: QPointF) -> QPointF:
        m, c = self._map_rect(), self._content
        if c.width() <= 0 or c.height() <= 0:
            return QPointF(m.center())
        fx = (pt.x() - c.left()) / c.width()
        fy = (pt.y() - c.top()) / c.height()
        return QPointF(m.left() + fx * m.width(), m.top() + fy * m.height())

    def _map_to_scene(self, pt: QPointF) -> QPointF:
        m, c = self._map_rect(), self._content
        if m.width() <= 0 or m.height() <= 0:
            return QPointF(c.center())
        fx = (pt.x() - m.left()) / m.width()
        fy = (pt.y() - m.top()) / m.height()
        return QPointF(c.left() + fx * c.width(), c.top() + fy * c.height())

    def _is_pan_grab(self, pos: QPoint, modifiers) -> bool:
        """True when a press here should drag the existing box.

        Zoomed all the way out the box covers the whole map, and treating
        that as a pan would make it impossible to draw a box at all — so
        in that case, and whenever Shift is held, a drag always draws a
        new region instead.
        """
        if not self.has_drawing() or not self._map_rect().contains(pos):
            return False
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return False
        box = self._view_box()
        if box.isEmpty():
            return False
        m = QRectF(self._map_rect())
        if box.width() * box.height() >= 0.9 * m.width() * m.height():
            return False        # nothing left to pan towards
        return box.contains(QPointF(pos))

    def _view_box(self) -> QRectF:
        """The visible region as a rectangle in widget coordinates."""
        if self._view.isEmpty():
            return QRectF()
        tl = self._scene_to_map(self._view.topLeft())
        br = self._scene_to_map(self._view.bottomRight())
        return QRectF(tl, br).normalized()

    # ------------------------------------------------------------------ #
    #  Painting
    # ------------------------------------------------------------------ #

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Frame
        frame = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setBrush(QBrush(QColor(35, 35, 38, 235)))
        p.setPen(QPen(QColor("#555"), 1))
        p.drawRoundedRect(frame, 5, 5)

        # Header
        p.setPen(QColor("#999"))
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        p.setFont(f)
        p.drawText(QRect(_PAD, 0, self.width() - 2 * _PAD - _HEADER_H, _HEADER_H),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   "Navigator")
        cr = self._close_rect()
        p.drawText(cr, int(Qt.AlignmentFlag.AlignCenter), "✕")

        m = self._map_rect()
        if self._thumb is None or m.width() <= 0 or m.height() <= 0:
            p.end()
            return

        p.drawPixmap(m, self._thumb, self._thumb.rect())

        # Dim everything outside the visible region so the box reads as
        # "you are here" rather than as one more rectangle in a drawing
        # already full of them.
        box = self._view_box().intersected(QRectF(m))
        if not box.isEmpty():
            shade = QColor(10, 10, 12, 120)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(shade)
            mf = QRectF(m)
            p.drawRect(QRectF(mf.left(), mf.top(), mf.width(), box.top() - mf.top()))
            p.drawRect(QRectF(mf.left(), box.bottom(), mf.width(), mf.bottom() - box.bottom()))
            p.drawRect(QRectF(mf.left(), box.top(), box.left() - mf.left(), box.height()))
            p.drawRect(QRectF(box.right(), box.top(), mf.right() - box.right(), box.height()))

            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, 160), 3))
            p.drawRect(box)
            p.setPen(QPen(QColor("#ffb454"), 1.5))
            p.drawRect(box)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(10, 10, 12, 120))
            p.drawRect(QRectF(m))

        # Rubber band being dragged out right now
        if self._band is not None:
            p.setBrush(QColor(90, 160, 255, 60))
            p.setPen(QPen(QColor("#5aa9ff"), 1, Qt.PenStyle.DashLine))
            p.drawRect(QRectF(self._band).normalized())

        # Map border last, so nothing paints over it
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#444"), 1))
        p.drawRect(QRectF(m).adjusted(-0.5, -0.5, 0.5, 0.5))
        p.end()

    # ------------------------------------------------------------------ #
    #  Mouse
    # ------------------------------------------------------------------ #

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        self._press_pos = pos

        if self._close_rect().contains(pos):
            self._mode = 'close'
            return

        if pos.y() < _HEADER_H:
            self._mode = 'move'
            self._grab_offset = QPointF(pos)
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return

        if not self.has_drawing() or not self._map_rect().contains(pos):
            self._mode = None
            return

        if self._is_pan_grab(pos, event.modifiers()):
            self._mode = 'pan'
            scene_pt = self._map_to_scene(QPointF(pos))
            self._grab_offset = self._view.center() - scene_pt
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
        else:
            self._mode = 'band'
            self._band = QRect(pos, pos)
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()

        if self._mode == 'move':
            delta = QPointF(pos) - self._grab_offset
            self._reposition(self.pos() + delta.toPoint())
            self._moved_by_user = True
            return

        if self._mode == 'pan':
            scene_pt = self._map_to_scene(QPointF(pos))
            self.centerRequested.emit(scene_pt + self._grab_offset)
            return

        if self._mode == 'band':
            self._band = QRect(self._press_pos, pos)
            self.update()
            return

        # Idle: hint what a click here would do
        if pos.y() < _HEADER_H:
            self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        elif self._is_pan_grab(pos, event.modifiers()):
            self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        elif self._map_rect().contains(pos):
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        else:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        mode, self._mode = self._mode, None
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

        if mode == 'close':
            if self._close_rect().contains(pos):
                self.closeRequested.emit()
            return

        if mode == 'band' and self._band is not None:
            band = self._band.normalized()
            self._band = None
            if band.width() < _DRAG_SLOP and band.height() < _DRAG_SLOP:
                # A plain click: jump the view to that spot.
                self.centerRequested.emit(self._map_to_scene(QPointF(pos)))
            else:
                band = band.intersected(self._map_rect())
                if band.width() >= 2 and band.height() >= 2:
                    tl = self._map_to_scene(QPointF(band.topLeft()))
                    br = self._map_to_scene(QPointF(band.bottomRight()))
                    self.rectRequested.emit(QRectF(tl, br).normalized())
            self.update()

    def wheelEvent(self, event):
        steps = event.angleDelta().y()
        if steps:
            self.zoomRequested.emit(1 if steps > 0 else -1)
        event.accept()

    def leaveEvent(self, event):
        if self._mode is None:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        super().leaveEvent(event)

    # ------------------------------------------------------------------ #
    #  Placement
    # ------------------------------------------------------------------ #

    def _reposition(self, top_left: QPoint) -> None:
        parent = self.parentWidget()
        if parent is None:
            self.move(top_left)
            return
        x = max(0, min(parent.width() - self.width(), top_left.x()))
        y = max(0, min(parent.height() - self.height(), top_left.y()))
        self.move(x, y)

    def anchor_to_parent(self, margin: int = 12) -> None:
        """Sit in the bottom-right corner, unless the user dragged it
        elsewhere — then just keep it on screen."""
        parent = self.parentWidget()
        if parent is None:
            return
        if self._moved_by_user:
            self._reposition(self.pos())
        else:
            self._reposition(QPoint(parent.width() - self.width() - margin,
                                    parent.height() - self.height() - margin))
