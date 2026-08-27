"""
canvas.py — DWG drawing canvas with pan, zoom, and measure tool.

Uses QGraphicsView + QGraphicsSvgItem for smooth, vector-quality rendering.
"""
from __future__ import annotations

import math
import re

from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import (
    QWheelEvent, QMouseEvent, QKeyEvent, QPainter,
    QPen, QColor, QCursor, QTransform,
)
from PyQt6.QtSvgWidgets import QGraphicsSvgItem
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsLineItem, QGraphicsEllipseItem
from PyQt6.QtCore import QByteArray


_ZOOM_FACTOR = 1.15   # per scroll step
_MIN_ZOOM = 0.01
_MAX_ZOOM = 200.0


class DrawingCanvas(QGraphicsView):
    """
    A QGraphicsView that displays a DWG/DXF drawing rendered as SVG.

    Signals
    -------
    coordChanged(x, y)      — emitted as the mouse moves (drawing coords)
    measurementDone(dist, x1, y1, x2, y2)
                             — emitted when a measurement is completed
    """

    coordChanged = pyqtSignal(float, float)
    measurementDone = pyqtSignal(float, float, float, float, float)
    pageNavigateRequested = pyqtSignal(int)   # +1 = next file, -1 = previous file

    def __init__(self, parent=None):
        super().__init__(parent)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        # Rendering settings
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setBackgroundBrush(QColor("#1e1e1e"))
        self.setFrameShape(self.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # State
        self._svg_item: QGraphicsSvgItem | None = None
        self._svg_renderer: QSvgRenderer | None = None
        self._current_zoom = 1.0
        self._pan_origin: QPointF | None = None   # set while panning
        self._pan_mode = False                     # True when toolbar Pan is active

        # Measure tool state
        self._measure_mode = False
        self._measure_point1: QPointF | None = None
        self._measure_line: QGraphicsLineItem | None = None
        self._measure_dot1: QGraphicsEllipseItem | None = None
        self._measure_dot2: QGraphicsEllipseItem | None = None

        # SVG viewBox (drawing coordinates → scene coordinates mapping)
        self._svg_viewbox: tuple[float, float, float, float] | None = None  # x, y, w, h

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def load_svg(self, svg_string: str) -> None:
        """Replace the displayed drawing with a new SVG string."""
        self._scene.clear()
        self._svg_item = None
        self._svg_renderer = None
        self._measure_line = None
        self._measure_dot1 = None
        self._measure_dot2 = None
        self._measure_point1 = None

        if not svg_string:
            return

        data = QByteArray(svg_string.encode("utf-8"))
        renderer = QSvgRenderer(data)
        if not renderer.isValid():
            return

        self._svg_renderer = renderer
        self._svg_viewbox = self._parse_viewbox(svg_string)

        item = QGraphicsSvgItem()
        item.setSharedRenderer(renderer)
        item.setFlags(item.GraphicsItemFlag.ItemClipsToShape)
        item.setCacheMode(item.CacheMode.NoCache)
        self._scene.addItem(item)
        self._svg_item = item

        self._scene.setSceneRect(item.boundingRect())
        self.fit_to_view()

    def fit_to_view(self) -> None:
        """Scale so the entire drawing fits in the viewport."""
        if self._svg_item is None:
            return
        self.resetTransform()
        self._current_zoom = 1.0
        rect = self._svg_item.boundingRect()
        if rect.isNull():
            return
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        # Record actual zoom factor after fitInView
        self._current_zoom = self.transform().m11()

    def set_pan_mode(self, enabled: bool) -> None:
        """Called by the toolbar Pan button to enable/disable left-click panning."""
        self._pan_mode = enabled
        if not self._measure_mode:
            if enabled:
                self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
            else:
                self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def set_measure_mode(self, enabled: bool) -> None:
        self._measure_mode = enabled
        self._measure_point1 = None
        self._clear_measure_graphics()
        if enabled:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        elif self._pan_mode:
            self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        else:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def clear(self) -> None:
        self._scene.clear()
        self._svg_item = None
        self._svg_renderer = None
        self._svg_viewbox = None
        self._measure_point1 = None
        self._measure_line = None
        self._measure_dot1 = None
        self._measure_dot2 = None

    # ------------------------------------------------------------------ #
    #  Mouse events
    # ------------------------------------------------------------------ #

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = _ZOOM_FACTOR if delta > 0 else 1.0 / _ZOOM_FACTOR
        new_zoom = self._current_zoom * factor
        if _MIN_ZOOM <= new_zoom <= _MAX_ZOOM:
            self.scale(factor, factor)
            self._current_zoom = new_zoom

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            # Start pan with middle button
            self._pan_origin = event.position()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            event.accept()
        elif (
            event.button() == Qt.MouseButton.LeftButton
            and not self._measure_mode
            and (self._pan_mode or event.modifiers() & Qt.KeyboardModifier.AltModifier)
        ):
            # Pan mode active (toolbar) or Alt+left-drag
            self._pan_origin = event.position()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton and self._measure_mode:
            self._handle_measure_click(event)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        # Emit drawing coordinates
        scene_pt = self.mapToScene(event.position().toPoint())
        dx, dy = self._scene_to_drawing(scene_pt)
        self.coordChanged.emit(dx, dy)

        if self._pan_origin is not None:
            delta = event.position() - self._pan_origin
            self._pan_origin = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            if self._pan_origin is not None:
                self._pan_origin = None
                if self._measure_mode:
                    self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
                elif self._pan_mode:
                    self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
                else:
                    self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._measure_point1 = None
            self._clear_measure_graphics()
        elif event.key() == Qt.Key.Key_F:
            self.fit_to_view()
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.pageNavigateRequested.emit(1)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.pageNavigateRequested.emit(-1)
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    #  Measure tool helpers
    # ------------------------------------------------------------------ #

    def _handle_measure_click(self, event: QMouseEvent) -> None:
        scene_pt = self.mapToScene(event.position().toPoint())

        if self._measure_point1 is None:
            self._measure_point1 = scene_pt
            self._clear_measure_graphics()
            self._measure_dot1 = self._add_dot(scene_pt)
        else:
            p1 = self._measure_point1
            p2 = scene_pt
            self._measure_point1 = None

            # Draw line + second dot
            self._add_measure_line(p1, p2)
            self._measure_dot2 = self._add_dot(p2)

            # Convert to drawing units and emit
            d1 = self._scene_to_drawing(p1)
            d2 = self._scene_to_drawing(p2)
            dist = math.hypot(d2[0] - d1[0], d2[1] - d1[1])
            self.measurementDone.emit(dist, d1[0], d1[1], d2[0], d2[1])

    def _add_dot(self, pt: QPointF) -> QGraphicsEllipseItem:
        r = 4 / self._current_zoom  # keep dot size constant in screen space
        dot = self._scene.addEllipse(
            QRectF(pt.x() - r, pt.y() - r, 2 * r, 2 * r),
            QPen(QColor("#ff5555"), 0),
            QColor("#ff5555"),
        )
        dot.setZValue(10)
        return dot

    def _add_measure_line(self, p1: QPointF, p2: QPointF) -> None:
        pen = QPen(QColor("#ffaa00"), 0)
        pen.setStyle(Qt.PenStyle.DashLine)
        line = self._scene.addLine(p1.x(), p1.y(), p2.x(), p2.y(), pen)
        line.setZValue(9)
        self._measure_line = line

    def _clear_measure_graphics(self) -> None:
        for item in (self._measure_line, self._measure_dot1, self._measure_dot2):
            if item is not None and item.scene() == self._scene:
                self._scene.removeItem(item)
        self._measure_line = None
        self._measure_dot1 = None
        self._measure_dot2 = None

    # ------------------------------------------------------------------ #
    #  Coordinate helpers
    # ------------------------------------------------------------------ #

    def _scene_to_drawing(self, pt: QPointF) -> tuple[float, float]:
        """
        Map a scene coordinate to DWG drawing units.
        The SVG viewBox encodes the drawing extents.
        """
        if self._svg_item is None:
            return pt.x(), pt.y()

        br = self._svg_item.boundingRect()  # scene size of the SVG
        if br.width() == 0 or br.height() == 0:
            return pt.x(), pt.y()

        if self._svg_viewbox:
            vx, vy, vw, vh = self._svg_viewbox
            # Fraction across the scene rect
            fx = (pt.x() - br.x()) / br.width()
            fy = (pt.y() - br.y()) / br.height()
            dx = vx + fx * vw
            dy = vy + fy * vh
            return dx, dy

        return pt.x(), pt.y()

    # ------------------------------------------------------------------ #
    #  SVG parsing helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_viewbox(svg_string: str) -> tuple[float, float, float, float] | None:
        """Extract viewBox attribute values from SVG string."""
        m = re.search(r'viewBox=["\']([^"\']+)["\']', svg_string)
        if not m:
            return None
        parts = m.group(1).split()
        if len(parts) != 4:
            return None
        try:
            return tuple(float(p) for p in parts)  # type: ignore[return-value]
        except ValueError:
            return None
