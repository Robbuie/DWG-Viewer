"""
canvas.py — DWG drawing canvas with pan, zoom, and measure tool.

Uses QGraphicsView + QGraphicsSvgItem for smooth, vector-quality rendering.
"""
from __future__ import annotations

import math
import re

from PyQt6.QtCore import (Qt, QRect, QRectF, QPoint, QPointF, QSize,
                          pyqtSignal, QTimer, QThread, QObject)
from PyQt6.QtGui import (
    QWheelEvent, QMouseEvent, QKeyEvent, QPainter,
    QPen, QColor, QCursor, QTransform, QPixmap, QImage, QImageReader,
    QGuiApplication,
)
from PyQt6.QtSvgWidgets import QGraphicsSvgItem
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (QGraphicsView, QGraphicsScene, QGraphicsLineItem,
                             QGraphicsEllipseItem, QGraphicsPixmapItem,
                             QGraphicsItem, QRubberBand, QInputDialog)
from PyQt6.QtCore import QByteArray, QBuffer, QIODevice

from src.navigator import NavigatorOverlay
from src import markup as mk


class _DetailWorker(QThread):
    """Renders one detail tile off the UI thread.

    Tiles take a couple of hundred milliseconds, which is fine in the
    background and a visible stutter on the UI thread. Results carry a
    token so a tile that finishes after the view has moved is discarded
    rather than drawn in the wrong place.
    """

    done = pyqtSignal(int, bytes, float, float, float, float)

    def __init__(self, token, provider, scene_rect, base_size, out_size):
        super().__init__()
        self._token = token
        self._provider = provider
        self._rect = scene_rect
        self._base = base_size
        self._out = out_size

    def run(self):
        try:
            png = self._provider(self._rect, self._base, self._out)
        except Exception:
            return
        if png:
            self.done.emit(self._token, png, *self._rect)


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
    navigatorVisibilityChanged = pyqtSignal(bool)
    markupChanged = pyqtSignal()             # something was added/moved/removed
    markupToolFinished = pyqtSignal()        # a one-shot tool used itself up
    snapshotTaken = pyqtSignal(int, int)     # width, height in pixels
    statusMessage = pyqtSignal(str)

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

        # Detail rendering: a cached raster is fixed at one resolution, so
        # magnifying past it can only interpolate. When a provider is set,
        # the visible region is redrawn at the resolution it is actually
        # being displayed at, and laid over the raster.
        self._detail_provider = None
        self._detail_item: QGraphicsPixmapItem | None = None
        self._detail_worker: "_DetailWorker | None" = None
        self._detail_token = 0
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(220)     # let a burst of zooming settle
        self._detail_timer.timeout.connect(self._request_detail)

        # Overview map. It is a child of the view rather than of the
        # viewport, which puts it above the drawing and gives it its own
        # mouse events instead of the pan handler's.
        self._nav = NavigatorOverlay(self)
        self._nav.centerRequested.connect(self._nav_center_on)
        self._nav.rectRequested.connect(self._nav_fit_rect)
        self._nav.zoomRequested.connect(self._nav_zoom)
        self._nav.closeRequested.connect(
            lambda: self.set_navigator_visible(False))
        self._nav.hide()
        self._nav_wanted = True          # user preference, independent of
                                         # whether a drawing is loaded
        self.horizontalScrollBar().valueChanged.connect(self._update_navigator_view)
        self.verticalScrollBar().valueChanged.connect(self._update_navigator_view)

        # Markup (redlines)
        self._mk_store: "mk.MarkupStore | None" = None
        self._mk_key = "0"
        self._mk_items: dict[str, QGraphicsItem] = {}
        self._mk_origin: dict[str, QPointF] = {}
        self._mk_tool: str | None = None      # None | 'select' | a mk.KIND
        self._mk_color = mk.DEFAULT_COLOR
        self._mk_visible = True
        self._mk_undo: list[tuple] = []
        self._draft_item: QGraphicsItem | None = None
        self._draft_points: list[QPointF] = []

        # Snapshot to clipboard
        self._snap_mode = False
        self._snap_origin: QPointF | None = None
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())

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
        self._refresh_navigator()
        self._rebuild_markup()

    @staticmethod
    def _load_downscaled(png_data: bytes) -> QPixmap | None:
        """Second attempt at half resolution.

        A full-size sheet raster is a few hundred megabytes expanded, and
        a machine short on memory can refuse it. Half the width is a
        quarter of the pixels and still sharper than the old default —
        much better than showing nothing.
        """
        try:
            buf = QBuffer(QByteArray(png_data))
            buf.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(buf, b"PNG")
            size = reader.size()
            if not size.isValid():
                return None
            reader.setScaledSize(size / 2)
            img = reader.read()
            if img.isNull():
                return None
            pix = QPixmap.fromImage(img)
            return None if pix.isNull() else pix
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    #  Detail rendering
    # ------------------------------------------------------------------ #

    def set_detail_provider(self, provider) -> None:
        """Supply a callable (scene_rect, base_size, out_size) -> PNG bytes.

        Passing None disables detail rendering and drops any overlay.
        """
        self._detail_provider = provider
        self._clear_detail()
        if provider is not None:
            self._schedule_detail()

    def _clear_detail(self) -> None:
        if self._detail_item is not None:
            try:
                self._scene.removeItem(self._detail_item)
            except Exception:
                pass
            self._detail_item = None

    def _schedule_detail(self) -> None:
        if self._detail_provider is not None and self._svg_item is not None:
            self._detail_timer.start()

    def _request_detail(self) -> None:
        provider = self._detail_provider
        base = self._svg_item
        if provider is None or not isinstance(base, QGraphicsPixmapItem):
            return

        scale = self.transform().m11()
        if scale <= 1.02:
            # At or below the raster's own resolution it is already as
            # sharp as a redraw would be.
            self._clear_detail()
            return

        base_rect = base.boundingRect()
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        rect = visible.intersected(base_rect)
        if rect.width() < 1 or rect.height() < 1:
            return

        out_w = min(4096, max(2, int(rect.width() * scale)))
        out_h = min(4096, max(2, int(rect.height() * scale)))

        self._detail_token += 1
        worker = _DetailWorker(
            self._detail_token, provider,
            (rect.left(), rect.top(), rect.right(), rect.bottom()),
            (base_rect.width(), base_rect.height()), (out_w, out_h))
        worker.done.connect(self._on_detail_ready)
        worker.finished.connect(worker.deleteLater)
        self._detail_worker = worker
        worker.start()

    def _on_detail_ready(self, token: int, png: bytes,
                         x0: float, y0: float, x1: float, y1: float) -> None:
        if token != self._detail_token or not png:
            return          # the view moved on while this was rendering
        pix = QPixmap()
        if not pix.loadFromData(QByteArray(png), "PNG") or pix.isNull():
            return

        self._clear_detail()
        item = QGraphicsPixmapItem(pix)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        # The tile was rendered for this scene rectangle; scale it to fit
        # exactly, so it lines up with the raster underneath.
        item.setScale(1.0)
        w = max(1e-6, x1 - x0)
        h = max(1e-6, y1 - y0)
        item.setTransform(QTransform().translate(x0, y0).scale(
            w / pix.width(), h / pix.height()))
        item.setZValue(1.0)
        self._scene.addItem(item)
        self._detail_item = item

    def _update_sampling(self) -> None:
        """Rasterised drawings are smoothed while shrunk and sampled
        sharply once magnified past their native resolution.

        Smoothing a magnified bitmap turns thin CAD linework and small
        text into a blur; nearest-neighbour keeps the strokes hard, which
        stays readable a good deal further in.
        """
        item = self._svg_item
        if not isinstance(item, QGraphicsPixmapItem):
            return
        magnified = self.transform().m11() > 1.0
        item.setTransformationMode(
            Qt.TransformationMode.FastTransformation if magnified
            else Qt.TransformationMode.SmoothTransformation)

    def load_image(self, png_data: bytes,
                   extents: tuple[float, float, float, float] | None = None) -> bool:
        """Display a rasterised drawing.

        Classic DWF arrives this way: its sheets carry millions of
        primitives, so they are decoded once into a high-resolution
        bitmap rather than an SVG. `extents` plays the part the SVG
        viewBox plays elsewhere — it maps cursor position back to sheet
        inches, so the measure tool keeps working.
        """
        self._scene.clear()
        self._svg_item = None
        self._svg_renderer = None
        self._measure_line = None
        self._measure_dot1 = None
        self._measure_dot2 = None
        self._measure_point1 = None

        if not png_data:
            return False

        self._detail_item = None
        self._detail_token += 1

        pix = QPixmap()
        if not pix.loadFromData(QByteArray(png_data), "PNG") or pix.isNull():
            pix = self._load_downscaled(png_data)
            if pix is None:
                # Say so rather than leaving an empty canvas with no
                # explanation, which is indistinguishable from a bug.
                return False

        item = QGraphicsPixmapItem(pix)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(item)
        self._svg_item = item
        self._svg_viewbox = extents

        self._scene.setSceneRect(item.boundingRect())
        self.fit_to_view()
        self._refresh_navigator()
        self._rebuild_markup()
        return True

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
        self._update_sampling()
        self._schedule_detail()
        self._update_navigator_view()

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
        self._nav.set_drawing(None, QRectF())
        self._nav.hide()
        self._mk_items.clear()
        self._mk_origin.clear()
        self._draft_item = None
        self._draft_points = []
        self._measure_point1 = None
        self._measure_line = None
        self._measure_dot1 = None
        self._measure_dot2 = None

    # ------------------------------------------------------------------ #
    #  Overview map
    # ------------------------------------------------------------------ #

    def set_navigator_visible(self, visible: bool) -> None:
        """Show or hide the overview map (remembered across drawings)."""
        self._nav_wanted = bool(visible)
        if visible and self._nav.has_drawing():
            self._nav.show()
            self._nav.raise_()
            self._nav.anchor_to_parent()
            self._update_navigator_view()
        else:
            self._nav.hide()
        self.navigatorVisibilityChanged.emit(self._nav_wanted)

    def navigator_visible(self) -> bool:
        return self._nav.isVisible()

    def _content_rect(self) -> QRectF:
        if self._svg_item is None:
            return QRectF()
        return self._svg_item.sceneBoundingRect()

    def _build_navigator_thumb(self) -> QPixmap | None:
        """A miniature of the whole sheet, drawn once per load.

        Raster drawings are simply scaled down; SVG is re-rendered small,
        which is both sharper and cheaper than rasterising it full size
        and throwing the pixels away.
        """
        max_w, max_h = NavigatorOverlay.thumb_bounds()
        item = self._svg_item
        if item is None:
            return None

        if isinstance(item, QGraphicsPixmapItem):
            pix = item.pixmap()
            if pix.isNull():
                return None
            return pix.scaled(max_w, max_h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)

        renderer = self._svg_renderer
        if renderer is None or not renderer.isValid():
            return None
        box = item.boundingRect()
        if box.width() <= 0 or box.height() <= 0:
            return None
        scale = min(max_w / box.width(), max_h / box.height())
        w = max(1, int(round(box.width() * scale)))
        h = max(1, int(round(box.height() * scale)))
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor("#ffffff"))
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            renderer.render(painter, QRectF(0, 0, w, h))
        finally:
            painter.end()
        return QPixmap.fromImage(img)

    def _refresh_navigator(self) -> None:
        thumb = self._build_navigator_thumb()
        self._nav.set_drawing(thumb, self._content_rect())
        if thumb is not None and self._nav_wanted:
            self._nav.show()
            self._nav.raise_()
            self._nav.anchor_to_parent()
        else:
            self._nav.hide()
        self._update_navigator_view()
        self.navigatorVisibilityChanged.emit(self._nav_wanted)

    def _update_navigator_view(self) -> None:
        if not self._nav.isVisible():
            return
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        self._nav.set_view_rect(visible)

    # -- requests coming back from the map ---------------------------- #

    def _nav_center_on(self, scene_pt: QPointF) -> None:
        self.centerOn(scene_pt)
        self._update_navigator_view()
        self._schedule_detail()

    def _nav_fit_rect(self, rect: QRectF) -> None:
        if rect.width() <= 0 or rect.height() <= 0:
            return
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        zoom = self.transform().m11()
        if zoom > _MAX_ZOOM or zoom < _MIN_ZOOM:
            # Snap back inside the allowed range rather than leaving the
            # view somewhere the wheel cannot get out of.
            clamped = min(_MAX_ZOOM, max(_MIN_ZOOM, zoom))
            self.scale(clamped / zoom, clamped / zoom)
            zoom = clamped
        self._current_zoom = zoom
        self._update_sampling()
        self._schedule_detail()
        self._update_navigator_view()

    def _nav_zoom(self, steps: int) -> None:
        factor = _ZOOM_FACTOR if steps > 0 else 1.0 / _ZOOM_FACTOR
        new_zoom = self._current_zoom * factor
        if not (_MIN_ZOOM <= new_zoom <= _MAX_ZOOM):
            return
        anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.scale(factor, factor)
        self.setTransformationAnchor(anchor)
        self._current_zoom = new_zoom
        self._update_sampling()
        self._schedule_detail()
        self._update_navigator_view()

    # ------------------------------------------------------------------ #
    #  Mouse events
    # ------------------------------------------------------------------ #

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._nav.isVisible():
            self._nav.anchor_to_parent()
        self._update_navigator_view()

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = _ZOOM_FACTOR if delta > 0 else 1.0 / _ZOOM_FACTOR
        new_zoom = self._current_zoom * factor
        if _MIN_ZOOM <= new_zoom <= _MAX_ZOOM:
            self.scale(factor, factor)
            self._current_zoom = new_zoom
            self._update_sampling()
            self._schedule_detail()
            self._update_navigator_view()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and self._snap_mode):
            self._snap_press(event)
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and self._mk_tool in mk.KINDS):
            self._markup_press(event)
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and self._mk_tool == 'select'):
            super().mousePressEvent(event)
            return
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

        if self._snap_origin is not None:
            self._snap_move(event)
            return
        if self._draft_points:
            self._markup_move(event)
            return
        if self._pan_origin is not None:
            delta = event.position() - self._pan_origin
            self._pan_origin = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            self._update_navigator_view()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._snap_origin is not None:
            self._snap_release(event)
            return
        if event.button() == Qt.MouseButton.LeftButton and self._draft_points:
            self._markup_release(event)
            return
        if event.button() == Qt.MouseButton.LeftButton and self._mk_tool == 'select':
            super().mouseReleaseEvent(event)
            self._sync_moved_markup()
            return
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            if self._pan_origin is not None:
                self._pan_origin = None
                self._schedule_detail()      # redraw detail where we landed
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
            self._cancel_draft()
            if self._snap_mode:
                self.set_snapshot_mode(False)
            if self._mk_tool is not None:
                self.set_markup_tool(None)
                self.markupToolFinished.emit()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected_markup()
        elif (event.key() == Qt.Key.Key_Z
              and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.undo_markup()
        elif event.key() == Qt.Key.Key_F:
            self.fit_to_view()
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.pageNavigateRequested.emit(1)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.pageNavigateRequested.emit(-1)
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    #  Markup (redlines)
    # ------------------------------------------------------------------ #

    def set_markup_context(self, store, sheet_key: str) -> None:
        """Attach the store this drawing's redlines live in.

        Called on every open and on every sheet change; passing None
        detaches, which is what happens when no file is loaded.
        """
        self._mk_store = store
        self._mk_key = sheet_key or "0"
        self._rebuild_markup()

    def _clear_markup_items(self) -> None:
        for item in self._mk_items.values():
            try:
                if item.scene() is self._scene:
                    self._scene.removeItem(item)
            except RuntimeError:
                pass            # the scene was cleared out from under it
        self._mk_items.clear()
        self._mk_origin.clear()

    def _rebuild_markup(self) -> None:
        """Recreate every markup item from the store.

        Redlines are stored as fractions of the sheet, so this is also
        what keeps them in place when a drawing is re-rendered at a
        different size after a layer toggle.
        """
        self._clear_markup_items()
        self._cancel_draft()
        store = self._mk_store
        if store is None or self._svg_item is None:
            return
        content = self._content_rect()
        if content.isEmpty():
            return
        for m in store.sheet(self._mk_key):
            item = mk.build_item(m, content)
            if item is None:
                continue
            item.setZValue(20)
            item.setData(0, m.id)
            item.setVisible(self._mk_visible)
            self._scene.addItem(item)
            self._mk_items[m.id] = item
            self._mk_origin[m.id] = item.pos()
        self._apply_selectable(self._mk_tool == 'select')

    def _apply_selectable(self, on: bool) -> None:
        for item in self._mk_items.values():
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, on)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, on)
            if not on:
                item.setSelected(False)

    # -- tool state --------------------------------------------------- #

    def set_markup_tool(self, tool: str | None) -> None:
        self._cancel_draft()
        self._mk_tool = tool
        self._apply_selectable(tool == 'select')
        if tool in mk.KINDS:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            if not self._mk_visible:
                self.set_markup_visible(True)   # drawing into hidden
                                                # markup would baffle
        elif tool == 'select':
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        elif self._pan_mode:
            self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        else:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def markup_tool(self) -> str | None:
        return self._mk_tool

    def set_markup_color(self, color: str) -> None:
        self._mk_color = color or mk.DEFAULT_COLOR

    def markup_color(self) -> str:
        return self._mk_color

    def set_markup_visible(self, visible: bool) -> None:
        self._mk_visible = bool(visible)
        for item in self._mk_items.values():
            item.setVisible(self._mk_visible)

    def markup_visible(self) -> bool:
        return self._mk_visible

    def markup_count(self) -> int:
        return len(self._mk_items)

    def can_undo_markup(self) -> bool:
        return bool(self._mk_undo)

    # -- editing ------------------------------------------------------ #

    def _records(self) -> list:
        if self._mk_store is None:
            return []
        return self._mk_store.sheet(self._mk_key)

    def _add_markup(self, m) -> None:
        if self._mk_store is None:
            return
        self._records().append(m)
        self._mk_undo.append(('add', m, None))
        self._rebuild_markup()
        self.markupChanged.emit()

    def delete_selected_markup(self) -> int:
        if self._mk_store is None:
            return 0
        ids = {item.data(0) for item in self._mk_items.values()
               if item.isSelected()}
        if not ids:
            return 0
        records = self._records()
        removed = [(i, m) for i, m in enumerate(records) if m.id in ids]
        for _, m in reversed(removed):
            records.remove(m)
        self._mk_undo.append(('delete', [m for _, m in removed],
                              [i for i, _ in removed]))
        self._rebuild_markup()
        self.markupChanged.emit()
        return len(removed)

    def clear_markup_on_sheet(self) -> int:
        if self._mk_store is None:
            return 0
        records = self._records()
        if not records:
            return 0
        gone = list(records)
        self._mk_undo.append(('delete', gone, list(range(len(gone)))))
        records.clear()
        self._rebuild_markup()
        self.markupChanged.emit()
        return len(gone)

    def undo_markup(self) -> bool:
        if not self._mk_undo or self._mk_store is None:
            return False
        action, payload, extra = self._mk_undo.pop()
        records = self._records()
        if action == 'add':
            if payload in records:
                records.remove(payload)
        elif action == 'delete':
            for m, index in zip(payload, extra):
                records.insert(min(index, len(records)), m)
        elif action == 'move':
            for m, pts in zip(payload, extra):
                m.points = pts
        self._rebuild_markup()
        self.markupChanged.emit()
        return True

    def _sync_moved_markup(self) -> None:
        """Write dragged items back into the store.

        Items are built at absolute scene positions, so a drag shows up
        as a non-zero item position; that offset is converted back to
        sheet fractions and folded into the record.
        """
        if self._mk_store is None:
            return
        content = self._content_rect()
        if content.isEmpty():
            return
        by_id = {m.id: m for m in self._records()}
        moved, before = [], []
        for mid, item in self._mk_items.items():
            origin = self._mk_origin.get(mid, QPointF())
            delta = item.pos() - origin
            if abs(delta.x()) < 1e-9 and abs(delta.y()) < 1e-9:
                continue
            record = by_id.get(mid)
            if record is None:
                continue
            dx = delta.x() / content.width()
            dy = delta.y() / content.height()
            moved.append(record)
            before.append(list(record.points))
            record.points = [(x + dx, y + dy) for x, y in record.points]
        if not moved:
            return
        self._mk_undo.append(('move', moved, before))
        self._rebuild_markup()
        self.markupChanged.emit()

    # -- drawing a new one -------------------------------------------- #

    def _cancel_draft(self) -> None:
        if self._draft_item is not None:
            try:
                if self._draft_item.scene() is self._scene:
                    self._scene.removeItem(self._draft_item)
            except RuntimeError:
                pass
        self._draft_item = None
        self._draft_points = []

    def _draft_markup(self):
        pts = [mk.to_norm(p, self._content_rect()) for p in self._draft_points]
        return mk.Markup(kind=self._mk_tool, points=pts, color=self._mk_color,
                         author=mk.current_author())

    def _show_draft(self) -> None:
        content = self._content_rect()
        if content.isEmpty() or len(self._draft_points) < 2:
            return
        item = mk.build_item(self._draft_markup(), content)
        if item is None:
            return
        if self._draft_item is not None and self._draft_item.scene() is self._scene:
            self._scene.removeItem(self._draft_item)
        item.setZValue(21)
        item.setOpacity(0.85)
        self._scene.addItem(item)
        self._draft_item = item

    def _markup_press(self, event: QMouseEvent) -> None:
        if self._mk_store is None or self._svg_item is None:
            self.statusMessage.emit("Open a drawing before adding markup.")
            return
        pt = self.mapToScene(event.position().toPoint())

        if self._mk_tool == mk.TEXT:
            text, ok = QInputDialog.getText(self, "Markup text", "Note:")
            if ok and text.strip():
                content = self._content_rect()
                self._add_markup(mk.Markup(
                    kind=mk.TEXT, points=[mk.to_norm(pt, content)],
                    color=self._mk_color, text=text.strip(),
                    author=mk.current_author()))
            self.markupToolFinished.emit()
            return

        self._draft_points = [pt, pt]
        event.accept()

    def _markup_move(self, event: QMouseEvent) -> None:
        pt = self.mapToScene(event.position().toPoint())
        if self._mk_tool == mk.PEN:
            # Thin the trail: a point every few pixels is plenty, and
            # keeps the sidecar from growing to megabytes.
            last = self._draft_points[-1]
            step = 3.0 / max(1e-6, self.transform().m11())
            if (abs(pt.x() - last.x()) > step or abs(pt.y() - last.y()) > step):
                self._draft_points.append(pt)
        else:
            self._draft_points[-1] = pt
        self._show_draft()
        event.accept()

    def _markup_release(self, event: QMouseEvent) -> None:
        pt = self.mapToScene(event.position().toPoint())
        if self._mk_tool == mk.PEN:
            self._draft_points.append(pt)
        else:
            self._draft_points[-1] = pt

        points = list(self._draft_points)
        tool = self._mk_tool
        self._cancel_draft()

        if len(points) >= 2:
            span = (points[0] - points[-1])
            scale = max(1e-9, self.transform().m11())
            if tool == mk.PEN:
                enough = len(points) > 2
            else:
                enough = (abs(span.x()) * scale > 4 or abs(span.y()) * scale > 4)
            if enough:
                content = self._content_rect()
                self._add_markup(mk.Markup(
                    kind=tool,
                    points=[mk.to_norm(p, content) for p in points],
                    color=self._mk_color, author=mk.current_author()))
        self.markupToolFinished.emit()
        event.accept()

    # ------------------------------------------------------------------ #
    #  Snapshot to clipboard
    # ------------------------------------------------------------------ #

    def set_snapshot_mode(self, enabled: bool) -> None:
        self._snap_mode = bool(enabled)
        if not enabled:
            self._snap_origin = None
            self._rubber.hide()
            if self._pan_mode:
                self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
            else:
                self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        else:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def snapshot_mode(self) -> bool:
        return self._snap_mode

    def _snap_press(self, event: QMouseEvent) -> None:
        self._snap_origin = event.position()
        self._rubber.setGeometry(QRect(self._snap_origin.toPoint(), QSize()))
        self._rubber.show()
        event.accept()

    def _snap_move(self, event: QMouseEvent) -> None:
        if self._snap_origin is None:
            return
        rect = QRect(self._snap_origin.toPoint(),
                     event.position().toPoint()).normalized()
        self._rubber.setGeometry(rect)
        event.accept()

    def _snap_release(self, event: QMouseEvent) -> None:
        origin = self._snap_origin
        self._snap_origin = None
        self._rubber.hide()
        self.set_snapshot_mode(False)
        if origin is None:
            return
        rect = QRect(origin.toPoint(), event.position().toPoint()).normalized()
        if rect.width() < 4 or rect.height() < 4:
            self.statusMessage.emit("Snapshot cancelled — drag a region to copy.")
            return
        scene_rect = self.mapToScene(rect).boundingRect()
        self.copy_region_to_clipboard(scene_rect)
        event.accept()

    def copy_view_to_clipboard(self) -> bool:
        """Copy everything currently on screen."""
        if self._svg_item is None:
            return False
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        return self.copy_region_to_clipboard(
            visible.intersected(self._content_rect()))

    def copy_region_to_clipboard(self, scene_rect: QRectF) -> bool:
        image = self.render_region(scene_rect)
        if image is None:
            return False
        QGuiApplication.clipboard().setImage(image)
        self.snapshotTaken.emit(image.width(), image.height())
        return True

    def render_region(self, scene_rect: QRectF, oversample: float = 2.0,
                      max_px: int = 8000) -> QImage | None:
        """Render part of the drawing to an image, redlines included.

        Rendered from the scene rather than grabbed from the widget, so
        the result is at print resolution instead of at whatever the
        monitor happens to be — a snapshot pasted into a report should
        not be a photograph of a screen.
        """
        if self._svg_item is None or scene_rect.width() <= 0 or scene_rect.height() <= 0:
            return None
        scale = max(1e-6, self.transform().m11()) * max(1.0, oversample)
        w = int(round(scene_rect.width() * scale))
        h = int(round(scene_rect.height() * scale))
        if w < 2 or h < 2:
            return None
        if max(w, h) > max_px:
            shrink = max_px / max(w, h)
            w = max(2, int(w * shrink))
            h = max(2, int(h * shrink))

        image = QImage(w, h, QImage.Format.Format_ARGB32)
        if image.isNull():
            return None
        # White, not the viewer's dark background: this is going into an
        # email or a report, next to white paper.
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        try:
            self._scene.render(painter, QRectF(0, 0, w, h), scene_rect,
                               Qt.AspectRatioMode.IgnoreAspectRatio)
        finally:
            painter.end()
        return image

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
