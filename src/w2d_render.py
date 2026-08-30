"""
w2d_render.py — rasterise a decoded W2D stream.

Classic DWF sheets carry millions of primitives (15 million on a busy
36x24 plant layout), which rules out the SVG path the other formats
use: no SVG document of that size would parse, let alone render. So
these are drawn straight into a bitmap once, at high resolution, and
cached. Panning and zooming then costs nothing, at the price of going
soft past the raster's resolution — the same trade a scanned drawing
makes.

Pillow does the drawing rather than Qt: it is already a dependency, it
works off the GUI thread without ceremony, and its primitives are C
speed.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# Pillow warns above ~89 megapixels in case a hostile file is trying to
# exhaust memory. These rasters are ours and their size is something we
# chose, so the guard is only noise here.
Image.MAX_IMAGE_PIXELS = None

from src import dwf as classic
from src.w2d import W2dDecoder

# Resolution of the cached raster. On a 36x24 sheet this is ~460 dpi,
# which puts 0.1-inch drawing text at 46 pixels tall — comfortably
# readable before magnification starts to soften it.
#
# It costs far less than it looks: decoding dominates, so doubling the
# width added about three seconds, and the PNG is under 2 MB. Peak
# memory while rendering (~500 MB) is what keeps this from going higher.
DEFAULT_WIDTH = 16000

_VIEW_RE = re.compile(rb"\(View\s+(-?\d+),(-?\d+)\s+(-?\d+),(-?\d+)\)")


def read_view(data: bytes) -> tuple[int, int, int, int] | None:
    """The sheet's logical extents, declared near the top of the stream."""
    m = _VIEW_RE.search(data[:8192])
    return tuple(int(g) for g in m.groups()) if m else None


_PLOT_RE = re.compile(rb"\(PlotInfo\s[^(]*\(\(([-\d.eE+]+)\s")

# Fallback if a sheet declares no plot matrix: WHIP's usual 1200 logical
# units per inch.
_DEFAULT_INCHES_PER_UNIT = 1.0 / 1200.0


def read_inches_per_unit(data: bytes) -> float:
    """Sheet inches per logical unit, from the PlotInfo transform."""
    m = _PLOT_RE.search(data[:8192])
    if not m:
        return _DEFAULT_INCHES_PER_UNIT
    try:
        value = float(m.group(1))
    except ValueError:
        return _DEFAULT_INCHES_PER_UNIT
    return value if value > 0 else _DEFAULT_INCHES_PER_UNIT


# AutoCAD's colour-index table, the palette DWF's indexed colours refer
# to. Taken verbatim from ezdxf rather than approximated with an HSV
# formula: a lookalike gets the basic colours right and everything else
# visibly wrong — rails that should be green came out orange.
_ACI_PACKED = (
    0x000000, 0xFF0000, 0xFFFF00, 0x00FF00, 0x00FFFF, 0x0000FF, 0xFF00FF, 0xFFFFFF,
    0x808080, 0xC0C0C0, 0xFF0000, 0xFF7F7F, 0xA50000, 0xA55252, 0x7F0000, 0x7F3F3F,
    0x4C0000, 0x4C2626, 0x260000, 0x261313, 0xFF3F00, 0xFF9F7F, 0xA52900, 0xA56752,
    0x7F1F00, 0x7F4F3F, 0x4C1300, 0x4C2F26, 0x260900, 0x261713, 0xFF7F00, 0xFFBF7F,
    0xA55200, 0xA57C52, 0x7F3F00, 0x7F5F3F, 0x4C2600, 0x4C3926, 0x261300, 0x261C13,
    0xFFBF00, 0xFFDF7F, 0xA57C00, 0xA59152, 0x7F5F00, 0x7F6F3F, 0x4C3900, 0x4C4226,
    0x261C00, 0x262113, 0xFFFF00, 0xFFFF7F, 0xA5A500, 0xA5A552, 0x7F7F00, 0x7F7F3F,
    0x4C4C00, 0x4C4C26, 0x262600, 0x262613, 0xBFFF00, 0xDFFF7F, 0x7CA500, 0x91A552,
    0x5F7F00, 0x6F7F3F, 0x394C00, 0x424C26, 0x1C2600, 0x212613, 0x7FFF00, 0xBFFF7F,
    0x52A500, 0x7CA552, 0x3F7F00, 0x5F7F3F, 0x264C00, 0x394C26, 0x132600, 0x1C2613,
    0x3FFF00, 0x9FFF7F, 0x29A500, 0x67A552, 0x1F7F00, 0x4F7F3F, 0x134C00, 0x2F4C26,
    0x092600, 0x172613, 0x00FF00, 0x7FFF7F, 0x00A500, 0x52A552, 0x007F00, 0x3F7F3F,
    0x004C00, 0x264C26, 0x002600, 0x132613, 0x00FF3F, 0x7FFF9F, 0x00A529, 0x52A567,
    0x007F1F, 0x3F7F4F, 0x004C13, 0x264C2F, 0x002609, 0x135817, 0x00FF7F, 0x7FFFBF,
    0x00A552, 0x52A57C, 0x007F3F, 0x3F7F5F, 0x004C26, 0x264C39, 0x002613, 0x13581C,
    0x00FFBF, 0x7FFFDF, 0x00A57C, 0x52A591, 0x007F5F, 0x3F7F6F, 0x004C39, 0x264C42,
    0x00261C, 0x135858, 0x00FFFF, 0x7FFFFF, 0x00A5A5, 0x52A5A5, 0x007F7F, 0x3F7F7F,
    0x004C4C, 0x264C4C, 0x002626, 0x135858, 0x00BFFF, 0x7FDFFF, 0x007CA5, 0x5291A5,
    0x005F7F, 0x3F6F7F, 0x00394C, 0x26427E, 0x001C26, 0x135858, 0x007FFF, 0x7FBFFF,
    0x0052A5, 0x527CA5, 0x003F7F, 0x3F5F7F, 0x00264C, 0x26397E, 0x001326, 0x131C58,
    0x003FFF, 0x7F9FFF, 0x0029A5, 0x5267A5, 0x001F7F, 0x3F4F7F, 0x00134C, 0x262F7E,
    0x000926, 0x131758, 0x0000FF, 0x7F7FFF, 0x0000A5, 0x5252A5, 0x00007F, 0x3F3F7F,
    0x00004C, 0x26267E, 0x000026, 0x131358, 0x3F00FF, 0x9F7FFF, 0x2900A5, 0x6752A5,
    0x1F007F, 0x4F3F7F, 0x13004C, 0x2F267E, 0x090026, 0x171358, 0x7F00FF, 0xBF7FFF,
    0x5200A5, 0x7C52A5, 0x3F007F, 0x5F3F7F, 0x26004C, 0x39267E, 0x130026, 0x1C1358,
    0xBF00FF, 0xDF7FFF, 0x7C00A5, 0x9152A5, 0x5F007F, 0x6F3F7F, 0x39004C, 0x42264C,
    0x1C0026, 0x581358, 0xFF00FF, 0xFF7FFF, 0xA500A5, 0xA552A5, 0x7F007F, 0x7F3F7F,
    0x4C004C, 0x4C264C, 0x260026, 0x581358, 0xFF00BF, 0xFF7FDF, 0xA5007C, 0xA55291,
    0x7F005F, 0x7F3F6F, 0x4C0039, 0x4C2642, 0x26001C, 0x581358, 0xFF007F, 0xFF7FBF,
    0xA50052, 0xA5527C, 0x7F003F, 0x7F3F5F, 0x4C0026, 0x4C2639, 0x260013, 0x58131C,
    0xFF003F, 0xFF7F9F, 0xA50029, 0xA55267, 0x7F001F, 0x7F3F4F, 0x4C0013, 0x4C262F,
    0x260009, 0x581317, 0x000000, 0x656565, 0x666666, 0x999999, 0xCCCCCC, 0xFFFFFF,
)

ACI = [((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF) for v in _ACI_PACKED]


class RasterSink:
    """Draws decoded primitives into a Pillow image."""

    def __init__(self, draw: ImageDraw.ImageDraw, x0: int, y0: int,
                 scale: float, height_px: int):
        self.d = draw
        self.x0, self.y0, self.s, self.h = x0, y0, scale, height_px
        self.colour: tuple[int, int, int] = (0, 0, 0)
        self.drawn = 0

    def _map(self, pts):
        s, x0, y0, h = self.s, self.x0, self.y0, self.h
        return [(int((x - x0) * s), h - int((y - y0) * s)) for x, y in pts]

    def polyline(self, pts):
        p = self._map(pts)
        if len(p) > 1:
            self.d.line(p, fill=self.colour, width=1)
            self.drawn += 1

    def polygon(self, pts):
        # "Polygon or polyline" depending on the fill state, which these
        # sheets leave off — so an OPEN polyline. Neither filling it nor
        # closing it is right: filling produced black blobs, and closing
        # drew a spurious stroke across every character of every label.
        p = self._map(pts)
        if len(p) > 1:
            self.d.line(p, fill=self.colour, width=1)
            self.drawn += 1

    def polytriangle(self, pts):
        p = self._map(pts)
        for k in range(len(p) - 2):
            self.d.polygon(p[k:k + 3], fill=self.colour)
        self.drawn += 1

    def markers(self, pts):
        for xy in self._map(pts):
            self.d.point(xy, fill=self.colour)
        self.drawn += 1

    def arc(self, cx, cy, r, start, end):
        (px, py), = self._map([(cx, cy)])
        rp = max(1, int(r * self.s))
        try:
            self.d.arc([px - rp, py - rp, px + rp, py + rp], -end, -start,
                       fill=self.colour)
            self.drawn += 1
        except ValueError:
            pass          # degenerate box — nothing to draw

    def ellipse(self, cx, cy, major, minor, start, end, tilt):
        (px, py), = self._map([(cx, cy)])
        a = max(1, int(abs(major) * self.s))
        b = max(1, int(abs(minor) * self.s))
        try:
            self.d.arc([px - a, py - b, px + a, py + b], -end, -start,
                       fill=self.colour)
            self.drawn += 1
        except ValueError:
            pass

    def text(self, x, y, s):
        # Sheet text is also present as stroked geometry, so it already
        # renders; drawing it again with a substitute font would only
        # double it up.
        pass

    def set_color(self, rgba):
        self.colour = rgba[:3]

    def set_index(self, index):
        self.colour = ACI[index & 0xFF]

    def set_visible(self, on):
        pass


def render_sheet(path, index: int = 0, width_px: int = DEFAULT_WIDTH):
    """Decode one sheet of a classic DWF and return (image, inches_box).

    inches_box is (0, height_in, width_in, -height_in): the drawing's
    extents in sheet inches, with a negative height because raster rows
    run downward while drawing coordinates run up. The canvas maps
    cursor position through it, so measurements come out in inches.
    """
    with classic.ClassicDwf(path) as doc:
        stream = doc.graphics_stream(index)
    if not stream:
        raise classic.ClassicDwfError("This DWF sheet has no graphics stream.")

    view = read_view(stream)
    if view is None:
        raise classic.ClassicDwfError(
            "This DWF sheet declares no view extents; it may be damaged.")

    x0, y0, x1, y1 = view
    if x1 <= x0 or y1 <= y0:
        raise classic.ClassicDwfError("This DWF sheet has empty view extents.")

    scale = width_px / (x1 - x0)
    height_px = max(1, int((y1 - y0) * scale))
    img = Image.new("RGB", (width_px, height_px), (255, 255, 255))
    sink = RasterSink(ImageDraw.Draw(img), x0, y0, scale, height_px)
    W2dDecoder(stream).run(sink)

    per_unit = read_inches_per_unit(stream)
    width_in = (x1 - x0) * per_unit
    height_in = (y1 - y0) * per_unit
    return img, (0.0, height_in, width_in, -height_in)


# ── On-demand rendering from retained geometry ───────────────────────
#
# The cached raster is fixed at one resolution, so magnifying past it can
# only interpolate — which is what makes deep zoom look soft or blocky.
# Keeping the decoded geometry lets any region be redrawn at exactly the
# resolution it is being displayed at, so it stays sharp however far in
# you go.


class SheetGeometry:
    """Decoded geometry for one sheet, ready to redraw at any zoom."""

    def __init__(self, collector, view, inches_per_unit: float):
        self.view = view
        self.inches_per_unit = inches_per_unit
        self.palette = collector.palette
        self.tri = collector.tri

        self.seg = np.frombuffer(collector.seg, dtype=np.int32).reshape(-1, 4)
        self.seg_col = np.frombuffer(collector.seg_col, dtype=np.uint16)
        self.pt = np.frombuffer(collector.pt, dtype=np.int32).reshape(-1, 2)
        self.pt_col = np.frombuffer(collector.pt_col, dtype=np.uint16)
        self.arc = np.frombuffer(collector.arc_xyz, dtype=np.int32).reshape(-1, 3)
        self.arc_ang = np.frombuffer(collector.arc_ang, dtype=np.float32).reshape(-1, 2)
        self.arc_col = np.frombuffer(collector.arc_col, dtype=np.uint16)

    @property
    def nbytes(self) -> int:
        return (self.seg.nbytes + self.seg_col.nbytes + self.pt.nbytes
                + self.pt_col.nbytes + self.arc.nbytes + self.arc_ang.nbytes)

    def render_region(self, region, width_px: int, height_px: int,
                      max_segments: int | None = None) -> Image.Image | None:
        """Draw the logical rectangle `region` into an image of the given
        pixel size. Only primitives overlapping the region are touched.

        Returns None if the region holds more than `max_segments`. Drawing
        time scales with what is in view, and a wide view can hold
        millions of segments — several seconds of work to produce
        something the cached raster already renders acceptably. The
        payoff is at deep zoom, where the raster runs out and the segment
        count is small.
        """
        rx0, ry0, rx1, ry1 = region
        if rx1 <= rx0 or ry1 <= ry0:
            raise ValueError("empty region")

        sx = width_px / (rx1 - rx0)
        sy = height_px / (ry1 - ry0)
        img = Image.new("RGB", (width_px, height_px), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        palette = self.palette

        # Segment bounds are derived per query rather than kept: caching
        # them costs 165 MB and saves under 100 ms, which is the wrong
        # trade for a drawing this size.
        ax, ay = self.seg[:, 0], self.seg[:, 1]     # not sx/sy: those are
        bx, by = self.seg[:, 2], self.seg[:, 3]     # the scale factors above
        hit = np.flatnonzero((np.minimum(ax, bx) <= rx1)
                             & (np.maximum(ax, bx) >= rx0)
                             & (np.minimum(ay, by) <= ry1)
                             & (np.maximum(ay, by) >= ry0))
        if max_segments is not None and hit.size > max_segments:
            return None

        if hit.size:
            chosen = self.seg[hit]
            px = ((chosen[:, [0, 2]] - rx0) * sx).astype(np.int32)
            py = (height_px - (chosen[:, [1, 3]] - ry0) * sy).astype(np.int32)
            cols = self.seg_col[hit]
            line = draw.line
            for k in range(hit.size):
                line((int(px[k, 0]), int(py[k, 0]),
                      int(px[k, 1]), int(py[k, 1])),
                     fill=palette[cols[k]], width=1)

        if self.pt.size:
            pts = self.pt
            keep = np.flatnonzero((pts[:, 0] >= rx0) & (pts[:, 0] <= rx1)
                                  & (pts[:, 1] >= ry0) & (pts[:, 1] <= ry1))
            if keep.size:
                chosen = pts[keep]
                px = ((chosen[:, 0] - rx0) * sx).astype(np.int32)
                py = (height_px - (chosen[:, 1] - ry0) * sy).astype(np.int32)
                cols = self.pt_col[keep]
                point = draw.point
                for k in range(keep.size):
                    point((int(px[k]), int(py[k])), fill=palette[cols[k]])

        if self.arc.size:
            arcs = self.arc
            keep = np.flatnonzero(
                (arcs[:, 0] + arcs[:, 2] >= rx0) & (arcs[:, 0] - arcs[:, 2] <= rx1)
                & (arcs[:, 1] + arcs[:, 2] >= ry0) & (arcs[:, 1] - arcs[:, 2] <= ry1))
            for k in keep:
                cx, cy, r = arcs[k]
                start, end = self.arc_ang[k]
                cpx = (cx - rx0) * sx
                cpy = height_px - (cy - ry0) * sy
                rpx = max(1.0, r * sx)
                try:
                    draw.arc([cpx - rpx, cpy - rpx, cpx + rpx, cpy + rpx],
                             -float(end), -float(start),
                             fill=palette[self.arc_col[k]])
                except ValueError:
                    pass

        for pts, colour in self.tri:
            mapped = [(int((x - rx0) * sx), int(height_px - (y - ry0) * sy))
                      for x, y in pts]
            for k in range(len(mapped) - 2):
                draw.polygon(mapped[k:k + 3], fill=palette[colour])

        return img


def decode_geometry(path, index: int = 0) -> SheetGeometry:
    """Decode a sheet into retained geometry. Costs one full pass over
    the opcode stream — tens of seconds — so callers keep the result."""
    from src.w2d import GeometryCollector

    with classic.ClassicDwf(path) as doc:
        stream = doc.graphics_stream(index)
    if not stream:
        raise classic.ClassicDwfError("This DWF sheet has no graphics stream.")
    view = read_view(stream)
    if view is None:
        raise classic.ClassicDwfError("This DWF sheet declares no view extents.")

    collector = GeometryCollector()
    W2dDecoder(stream).run(collector)
    return SheetGeometry(collector, view, read_inches_per_unit(stream))


def render_sheet_png(path, index: int = 0,
                     width_px: int = DEFAULT_WIDTH) -> tuple[bytes, tuple]:
    img, inches_box = render_sheet(path, index, width_px)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue(), inches_box
