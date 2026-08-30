"""
textsearch.py — finding a tag on the sheet.

On a 36x24 plan, hunting for one balloon or one equipment tag by
scrolling at magnification is the slowest thing anybody does in a
viewer. Every format here can say where its text is; they just say it
differently, so each gets its own extractor and they all hand back the
same thing: hits in sheet-normalised coordinates (0..1 across the
drawing, y downward), which is the same convention markup uses and maps
onto the canvas with no format knowledge at all.

  * **DWFx** — the translated SVG carries real `<text>` elements, so the
    hits are read back out of it with the nested `<g transform>` chain
    applied. Parsing the output rather than the XPS source means the
    positions are exactly where the drawing was actually drawn.
  * **DXF / DWG** — ezdxf's SVG backend converts text to filled paths,
    so there is nothing to read back; the strings come from the document
    instead, and are mapped through the same fit-and-centre transform
    the backend uses to place the drawing on the page.
  * **Classic DWF** — most lettering on an ePlot sheet is stroked
    geometry with no string attached, so only genuine text opcodes can
    be found. Those are collected during the geometry pass that already
    runs for sharp zoom, so searching costs nothing extra.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# A rough mean glyph advance as a fraction of font size, used only when
# the renderer did not record an advance. Good enough to place a
# highlight box; nothing is measured from it.
_MEAN_ADVANCE = 0.55


@dataclass(frozen=True)
class TextHit:
    """One string, in sheet fractions: (x, y) is its top-left corner."""
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2.0, self.y + self.h / 2.0


class TextIndex:
    """Every string on one sheet, searchable."""

    def __init__(self, hits: list[TextHit] | None = None):
        self.hits: list[TextHit] = list(hits or ())
        self._folded = [h.text.casefold() for h in self.hits]

    def __len__(self) -> int:
        return len(self.hits)

    def __bool__(self) -> bool:
        return bool(self.hits)

    def search(self, query: str) -> list[TextHit]:
        """Case-insensitive substring match, in reading order.

        Sorted top-to-bottom then left-to-right, so pressing Next walks
        the sheet the way a person reads it rather than the arbitrary
        order the file happened to store.
        """
        needle = (query or "").strip().casefold()
        if not needle:
            return []
        found = [h for h, folded in zip(self.hits, self._folded)
                 if needle in folded]
        return sorted(found, key=lambda h: (round(h.y, 3), h.x))


# ------------------------------------------------------------------ #
#  SVG (DWFx)
# ------------------------------------------------------------------ #

_NUM = r"[-+]?[\d.]+(?:[eE][-+]?\d+)?"


def _parse_transform(value: str) -> tuple[float, float, float, float, float, float]:
    """Collapse an SVG transform list to one matrix (a, b, c, d, e, f)."""
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", value or ""):
        nums = [float(n) for n in re.findall(_NUM, args)]
        if name == "matrix" and len(nums) >= 6:
            m = tuple(nums[:6])
        elif name == "translate" and nums:
            m = (1.0, 0.0, 0.0, 1.0, nums[0], nums[1] if len(nums) > 1 else 0.0)
        elif name == "scale" and nums:
            sx = nums[0]
            sy = nums[1] if len(nums) > 1 else sx
            m = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        else:
            continue            # rotate/skew: rare here, and only the
                                # anchor would shift slightly
        ctm = _multiply(ctm, m)
    return ctm


def _multiply(p, q):
    a1, b1, c1, d1, e1, f1 = p
    a2, b2, c2, d2, e2, f2 = q
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def _apply(m, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def from_svg(svg: str) -> TextIndex:
    if not svg or "<text" not in svg:
        return TextIndex()
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return TextIndex()

    box = (root.get("viewBox") or "").split()
    if len(box) != 4:
        return TextIndex()
    try:
        vx, vy, vw, vh = (float(v) for v in box)
    except ValueError:
        return TextIndex()
    if vw <= 0 or vh <= 0:
        return TextIndex()

    hits: list[TextHit] = []

    def walk(node, ctm):
        local = node.get("transform")
        if local:
            ctm = _multiply(ctm, _parse_transform(local))
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "text":
            text = "".join(node.itertext()).strip()
            if text:
                try:
                    x = float(node.get("x", 0.0))
                    y = float(node.get("y", 0.0))
                    size = float(node.get("font-size", 12.0))
                except ValueError:
                    size = 12.0
                    x = y = 0.0
                try:
                    advance = float(node.get("textLength"))
                except (TypeError, ValueError):
                    advance = len(text) * size * _MEAN_ADVANCE
                # y is the baseline; the box wants the top.
                px, py = _apply(ctm, x, y - size)
                scale = abs(ctm[0]) or 1.0
                hits.append(TextHit(
                    text=text,
                    x=(px - vx) / vw, y=(py - vy) / vh,
                    w=max(1e-4, advance * scale / vw),
                    h=max(1e-4, size * abs(ctm[3] or 1.0) / vh)))
        for child in node:
            walk(child, ctm)

    walk(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0))
    return TextIndex(hits)


# ------------------------------------------------------------------ #
#  DXF / DWG
# ------------------------------------------------------------------ #

def from_dxf(doc, page_w: float = 1600.0, page_h: float = 1200.0) -> TextIndex:
    """Strings from the document, placed the way the SVG backend places
    the drawing: the model extents fitted into the page, centred, with y
    flipped."""
    try:
        from ezdxf import bbox
    except Exception:
        return TextIndex()
    try:
        msp = doc.modelspace()
        extents = bbox.extents(msp, fast=True)
    except Exception:
        return TextIndex()
    if extents is None or not extents.has_data:
        return TextIndex()

    min_x, min_y = float(extents.extmin.x), float(extents.extmin.y)
    span_x = float(extents.extmax.x) - min_x
    span_y = float(extents.extmax.y) - min_y
    if span_x <= 0 or span_y <= 0 or page_w <= 0 or page_h <= 0:
        return TextIndex()

    scale = min(page_w / span_x, page_h / span_y)
    off_x = (page_w - span_x * scale) / 2.0
    off_y = (page_h - span_y * scale) / 2.0

    def place(x: float, y: float) -> tuple[float, float]:
        return ((off_x + (x - min_x) * scale) / page_w,
                (page_h - off_y - (y - min_y) * scale) / page_h)

    hits: list[TextHit] = []
    for entity in msp:
        kind = entity.dxftype()
        if kind not in ("TEXT", "MTEXT", "ATTRIB"):
            continue
        try:
            if kind == "MTEXT":
                text = entity.plain_text()
                height = float(entity.dxf.char_height)
                anchor = entity.dxf.insert
            else:
                text = str(entity.dxf.text)
                height = float(entity.dxf.height)
                anchor = entity.dxf.insert
                align = getattr(entity.dxf, "align_point", None)
                if align is not None and (anchor.x == 0 and anchor.y == 0):
                    anchor = align         # aligned text parks insert at 0,0
        except Exception:
            continue
        text = (text or "").strip()
        if not text:
            continue
        nx, ny = place(float(anchor.x), float(anchor.y) + height)
        hits.append(TextHit(
            text=text, x=nx, y=ny,
            w=max(1e-4, len(text) * height * _MEAN_ADVANCE * scale / page_w),
            h=max(1e-4, height * scale / page_h)))
    return TextIndex(hits)


# ------------------------------------------------------------------ #
#  Classic DWF
# ------------------------------------------------------------------ #

def from_classic_geometry(geometry) -> TextIndex:
    """Text opcodes captured while decoding a classic DWF sheet."""
    texts = getattr(geometry, "texts", None)
    view = getattr(geometry, "view", None)
    if not texts or not view:
        return TextIndex()
    x0, y0, x1, y1 = view
    span_x, span_y = float(x1 - x0), float(y1 - y0)
    if span_x <= 0 or span_y <= 0:
        return TextIndex()

    # A W2D text opcode carries no height, so the box is nominal — big
    # enough to see, small enough not to swamp what it is marking.
    nominal = 0.012
    hits = []
    for x, y, s in texts:
        s = (s or "").strip()
        if not s:
            continue
        nx = (float(x) - x0) / span_x
        ny = 1.0 - (float(y) - y0) / span_y
        hits.append(TextHit(text=s, x=nx, y=ny - nominal,
                            w=max(1e-4, len(s) * nominal * _MEAN_ADVANCE),
                            h=nominal))
    return TextIndex(hits)
