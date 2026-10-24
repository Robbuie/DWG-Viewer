"""
w2d_vector.py — draw decoded classic-DWF geometry as real vectors.

The screen never needs this: there, a sheet is a cached raster with a
sharp tile redrawn on top, and both are bitmaps. A PDF is different.
Qt's PDF writer stores every image as JPEG, so a raster of a drawing
comes out soft-edged with ringing around every line, and at 2–3 MB a
page even when the sheet is nearly blank. Painting the same segments
through QPainter instead gives a PDF that stays crisp at any zoom in
any reader, prints at the printer's own resolution, and is a fraction
of the size.

The drawing order and primitives follow SheetGeometry.render_region so
the PDF looks like the screen; only the destination differs.
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF

# Classic DWF plots at one pixel whatever the zoom, which on paper would
# be a hairline: thinner than a printer can resolve, and some readers
# drop it altogether at fit-to-window. 0.13 mm is the thinnest standard
# plot pen, which is what a one-pixel line was standing in for.
LINE_WIDTH_INCHES = 0.13 / 25.4

# Lines are handed to Qt in batches so a sheet of a few million segments
# never builds one enormous Python list.
_BATCH = 50_000


def _pen(rgb, width: float) -> QPen:
    pen = QPen(QColor(*rgb))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def paint_geometry(painter: QPainter, geom, region, target: QRectF,
                   hidden: frozenset[str] = frozenset(),
                   line_width: float | None = None,
                   snap: bool = True) -> bool:
    """Draw the drawing-coordinate rectangle `region` of `geom` into
    `target` (painter device units). Returns False if there was nothing
    usable to draw with, so the caller can fall back to the raster.

    `line_width` is in device units; by default it is worked out from
    the painter's resolution so lines come out LINE_WIDTH_INCHES wide.

    `snap` rounds segment ends to whole device units. At PDF_DPI that is
    1/600 in, far below the pen width, and it roughly halves the size of
    every coordinate written to the file.
    """
    rx0, ry0, rx1, ry1 = region
    if rx1 <= rx0 or ry1 <= ry0 or target.width() <= 0 or target.height() <= 0:
        return False

    sx = target.width() / (rx1 - rx0)
    sy = target.height() / (ry1 - ry0)
    ox, oy = target.left(), target.top()
    if line_width is None:
        dpi = painter.device().logicalDpiX() if painter.device() else 96
        line_width = max(0.5, float(dpi) * LINE_WIDTH_INCHES)

    drop = geom._hidden_indices(hidden)
    palette = geom.palette

    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # -- segments, one pen per colour -------------------------------
        seg = geom.seg
        if seg.size:
            ax, ay, bx, by = seg[:, 0], seg[:, 1], seg[:, 2], seg[:, 3]
            keep = ((np.minimum(ax, bx) <= rx1) & (np.maximum(ax, bx) >= rx0)
                    & (np.minimum(ay, by) <= ry1) & (np.maximum(ay, by) >= ry0))
            if drop is not None and geom.seg_layer.size == seg.shape[0]:
                keep &= ~np.isin(geom.seg_layer, drop)
            hit = np.flatnonzero(keep)
            if hit.size:
                cols = geom.seg_col[hit]
                order = np.argsort(cols, kind="stable")
                hit, cols = hit[order], cols[order]
                chosen = seg[hit].astype(np.float64)
                chosen[:, [0, 2]] = ox + (chosen[:, [0, 2]] - rx0) * sx
                chosen[:, [1, 3]] = oy + (ry1 - chosen[:, [1, 3]]) * sy
                if snap:
                    chosen = np.rint(chosen)
                # Polylines arrive as runs of segments, each starting where
                # the last ended. Drawn as one path, a run costs one "l" per
                # vertex in the PDF instead of an "m" and an "l" per
                # segment — half the size for the same picture.
                joined = np.zeros(hit.size, dtype=bool)
                if hit.size > 1:
                    joined[1:] = ((chosen[1:, 0] == chosen[:-1, 2])
                                  & (chosen[1:, 1] == chosen[:-1, 3])
                                  & (cols[1:] == cols[:-1]))
                bounds = np.flatnonzero(np.diff(cols)) + 1
                starts = np.concatenate(([0], bounds))
                ends = np.concatenate((bounds, [cols.size]))
                for s, e in zip(starts.tolist(), ends.tolist()):
                    painter.setPen(_pen(palette[int(cols[s])], line_width))
                    for b in range(s, e, _BATCH):
                        stop = min(e, b + _BATCH)
                        rows = chosen[b:stop].tolist()
                        cont = joined[b:stop].tolist()
                        cont[0] = False
                        path = QPainterPath()
                        move, line_to = path.moveTo, path.lineTo
                        for (x0, y0, x1, y1), c in zip(rows, cont):
                            if not c:
                                move(x0, y0)
                            line_to(x1, y1)
                        painter.drawPath(path)

        # -- markers ------------------------------------------------------
        pts = geom.pt
        if pts.size:
            keep = ((pts[:, 0] >= rx0) & (pts[:, 0] <= rx1)
                    & (pts[:, 1] >= ry0) & (pts[:, 1] <= ry1))
            if drop is not None and geom.pt_layer.size == pts.shape[0]:
                keep &= ~np.isin(geom.pt_layer, drop)
            idx = np.flatnonzero(keep)
            if idx.size:
                cols = geom.pt_col[idx]
                xy = pts[idx].astype(np.float64)
                xs = (ox + (xy[:, 0] - rx0) * sx).tolist()
                ys = (oy + (ry1 - xy[:, 1]) * sy).tolist()
                for c in np.unique(cols).tolist():
                    painter.setPen(_pen(palette[c], line_width))
                    sel = np.flatnonzero(cols == c).tolist()
                    painter.drawPoints([QPointF(xs[k], ys[k]) for k in sel])

        # -- arcs -------------------------------------------------------
        arcs = geom.arc
        if arcs.size:
            keep = ((arcs[:, 0] + arcs[:, 2] >= rx0)
                    & (arcs[:, 0] - arcs[:, 2] <= rx1)
                    & (arcs[:, 1] + arcs[:, 2] >= ry0)
                    & (arcs[:, 1] - arcs[:, 2] <= ry1))
            if drop is not None and geom.arc_layer.size == arcs.shape[0]:
                keep &= ~np.isin(geom.arc_layer, drop)
            current = None
            for k in np.flatnonzero(keep).tolist():
                cx, cy, r = (float(v) for v in arcs[k])
                start, end = (float(v) for v in geom.arc_ang[k])
                colour = int(geom.arc_col[k])
                if colour != current:
                    painter.setPen(_pen(palette[colour], line_width))
                    current = colour
                px = ox + (cx - rx0) * sx
                py = oy + (ry1 - cy) * sy
                rx, ry = abs(r) * sx, abs(r) * sy
                span = (end - start) % 360.0 or 360.0
                # Counter-clockwise in drawing coordinates is
                # counter-clockwise on the page too, which is Qt's sense.
                painter.drawArc(QRectF(px - rx, py - ry, 2 * rx, 2 * ry),
                                int(round(start * 16)), int(round(span * 16)))

        # -- filled triangle strips ---------------------------------------
        if geom.tri:
            painter.setPen(Qt.PenStyle.NoPen)
            for strip, colour, layer in geom.tri:
                if drop is not None and layer in drop:
                    continue
                painter.setBrush(QBrush(QColor(*palette[colour])))
                mapped = [QPointF(ox + (x - rx0) * sx, oy + (ry1 - y) * sy)
                          for x, y in strip]
                for k in range(len(mapped) - 2):
                    painter.drawPolygon(QPolygonF(mapped[k:k + 3]))
    finally:
        painter.restore()
    return True


def scene_region(geom, scene_rect, base_size):
    """Map a rectangle in raster/scene coordinates back to drawing
    coordinates — the same mapping DWGConverter.render_detail_png uses."""
    sx0, sy0, sx1, sy1 = scene_rect
    base_w, base_h = base_size
    vx0, vy0, vx1, vy1 = geom.view
    span_x, span_y = vx1 - vx0, vy1 - vy0
    return (vx0 + (sx0 / base_w) * span_x,
            vy0 + (1.0 - sy1 / base_h) * span_y,
            vx0 + (sx1 / base_w) * span_x,
            vy0 + (1.0 - sy0 / base_h) * span_y)


def vector_painter(geom, hidden: frozenset[str] = frozenset()):
    """A callable for DrawingCanvas.set_vector_painter."""
    def paint(painter, scene_rect, base_size, target) -> bool:
        if base_size[0] <= 0 or base_size[1] <= 0:
            return False
        region = scene_region(geom, scene_rect, base_size)
        return paint_geometry(painter, geom, region, target, hidden)
    return paint
