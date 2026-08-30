"""
markup.py — redlines: the data model, the sidecar file, and the
QGraphicsItems that draw them.

Markup is what keeps Autodesk Design Review installed at firms that
stopped using anything else about it: draw a cloud round a mistake, type
what is wrong, send the file back. Everything here serves that loop.

Two decisions worth stating up front.

**Coordinates are stored normalised.** A markup is kept as fractions of
the sheet's own extents, not as scene pixels. The same drawing is a
1600-px SVG one minute and a 16000-px raster the next, and a re-render
after a layer toggle rebuilds the scene from scratch; anything stored in
scene units would drift or land in the wrong place. Fractions survive
all of it.

**The sidecar sits next to the drawing.** Redlines are only useful if
the next person to open the file sees them, and on a shared engineering
folder that means `<drawing>.markup.json` beside the drawing rather than
buried in one user's profile. When the folder is read-only the store
falls back to the local cache so work is never lost, and says so.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QColor, QPen, QBrush, QPainterPath, QFont, QPolygonF
from PyQt6.QtWidgets import (
    QGraphicsItem, QGraphicsPathItem, QGraphicsSimpleTextItem,
    QGraphicsItemGroup,
)

SCHEMA = 1

# Tool identifiers, also the "kind" stored in the file.
CLOUD = "cloud"
BOX = "box"
ELLIPSE = "ellipse"
ARROW = "arrow"
PEN = "pen"
TEXT = "text"

KINDS = (CLOUD, BOX, ELLIPSE, ARROW, PEN, TEXT)

DEFAULT_COLOR = "#ff3b30"

# Sizes are a fraction of the sheet diagonal, so a redline looks the
# same weight on a 36x24 plan as on a title-block detail, and stays
# proportionate however the sheet was rasterised.
_STROKE_FRACTION = 0.0016
_TEXT_FRACTION = 0.016
_CLOUD_BUMP_FRACTION = 0.012
_ARROW_HEAD_FRACTION = 0.014


@dataclass
class Markup:
    """One redline, in sheet-normalised coordinates (0..1)."""

    kind: str
    points: list[tuple[float, float]]
    color: str = DEFAULT_COLOR
    text: str = ""
    author: str = ""
    created: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict:
        d = asdict(self)
        d["points"] = [[round(x, 6), round(y, 6)] for x, y in self.points]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Markup | None":
        try:
            kind = str(d["kind"])
            if kind not in KINDS:
                return None
            pts = [(float(p[0]), float(p[1])) for p in d.get("points", [])]
            if not pts:
                return None
            return cls(
                kind=kind,
                points=pts,
                color=str(d.get("color") or DEFAULT_COLOR),
                text=str(d.get("text") or ""),
                author=str(d.get("author") or ""),
                created=float(d.get("created") or time.time()),
                id=str(d.get("id") or uuid.uuid4().hex[:12]),
            )
        except Exception:
            # One malformed record must not cost the user the rest of
            # their redlines.
            return None


def current_author() -> str:
    return (os.environ.get("USERNAME") or os.environ.get("USER")
            or "unknown")


# ------------------------------------------------------------------ #
#  Sidecar store
# ------------------------------------------------------------------ #

def _fallback_dir() -> Path:
    from src import cache
    return cache.CACHE_ROOT / "markup"


class MarkupStore:
    """Markup for one drawing file, grouped by sheet.

    Sheets are keyed by name rather than index: a DWFx package can be
    republished with a sheet added, and index-keyed redlines would then
    silently attach to the wrong drawing.
    """

    def __init__(self, drawing_path: str | Path):
        self.drawing_path = Path(drawing_path)
        self._sheets: dict[str, list[Markup]] = {}
        self._path = self.drawing_path.with_suffix(
            self.drawing_path.suffix + ".markup.json")
        self._fallback = False
        self.load()

    # -- location ---------------------------------------------------- #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_fallback(self) -> bool:
        """True when redlines are being kept in the local cache because
        the drawing's own folder could not be written to."""
        return self._fallback

    def _fallback_path(self) -> Path:
        from src import cache
        key = cache.digest_for(self.drawing_path) if hasattr(cache, "digest_for") \
            else None
        if not key:
            import hashlib
            key = hashlib.sha1(
                str(self.drawing_path.resolve()).lower().encode("utf-8")
            ).hexdigest()[:16]
        return _fallback_dir() / f"{key}.markup.json"

    # -- contents ---------------------------------------------------- #

    def sheet(self, key: str) -> list[Markup]:
        return self._sheets.setdefault(key or "0", [])

    def set_sheet(self, key: str, items: list[Markup]) -> None:
        self._sheets[key or "0"] = list(items)

    def rekey_legacy(self, key: str) -> None:
        """Move redlines saved under the old index key onto a named sheet.

        Before the viewer could name a classic DWF's sheets it opened the
        first one and saved every redline under "0". Those redlines are
        the first sheet's, so the first time it is opened by name they
        are carried across rather than vanishing. Only ever done when the
        named sheet has nothing of its own, so it cannot overwrite work.
        """
        if not key or key == "0":
            return
        legacy = self._sheets.get("0")
        if not legacy or self._sheets.get(key):
            return
        self._sheets[key] = legacy
        del self._sheets["0"]

    def total(self) -> int:
        return sum(len(v) for v in self._sheets.values())

    def sheets_with_markup(self) -> list[str]:
        return [k for k, v in self._sheets.items() if v]

    # -- persistence ------------------------------------------------- #

    def load(self) -> None:
        self._sheets = {}
        for candidate, is_fb in ((self._path, False),
                                 (self._fallback_path(), True)):
            try:
                if not candidate.is_file():
                    continue
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            sheets = data.get("sheets") or {}
            if not isinstance(sheets, dict):
                continue
            for key, records in sheets.items():
                items = [m for m in (Markup.from_dict(r) for r in records)
                         if m is not None]
                if items:
                    self._sheets[str(key)] = items
            self._fallback = is_fb
            if self._sheets:
                return

    def save(self) -> bool:
        """Write the sidecar. Returns False if nothing could be written.

        An empty store deletes the sidecar rather than leaving an empty
        file behind, so a folder is not littered with markup files for
        drawings nobody redlined.
        """
        payload = {
            "schema": SCHEMA,
            "drawing": self.drawing_path.name,
            "updated": time.time(),
            "sheets": {k: [m.to_dict() for m in v]
                       for k, v in self._sheets.items() if v},
        }
        empty = not payload["sheets"]

        target = self._fallback_path() if self._fallback else self._path
        if self._write(target, payload, empty):
            return True
        if not self._fallback:
            # Read-only share, most likely. Keep the work locally.
            self._fallback = True
            return self._write(self._fallback_path(), payload, empty)
        return False

    @staticmethod
    def _write(target: Path, payload: dict, empty: bool) -> bool:
        try:
            if empty:
                if target.is_file():
                    target.unlink()
                return True
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write beside the target and replace, so an interrupted
            # save cannot truncate redlines that were already on disk.
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=1)
                os.replace(tmp, target)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except Exception:
            return False


# ------------------------------------------------------------------ #
#  Drawing
# ------------------------------------------------------------------ #

def stroke_width(content: QRectF) -> float:
    diag = (content.width() ** 2 + content.height() ** 2) ** 0.5
    return max(1e-6, diag * _STROKE_FRACTION)


def to_scene(pt: tuple[float, float], content: QRectF) -> QPointF:
    return QPointF(content.left() + pt[0] * content.width(),
                   content.top() + pt[1] * content.height())


def to_norm(pt: QPointF, content: QRectF) -> tuple[float, float]:
    if content.width() <= 0 or content.height() <= 0:
        return 0.0, 0.0
    return ((pt.x() - content.left()) / content.width(),
            (pt.y() - content.top()) / content.height())


def _cloud_path(rect: QRectF, bump: float) -> QPainterPath:
    """A revision cloud: semicircular bumps round a rectangle."""
    rect = rect.normalized()
    bump = max(bump, 1e-6)
    path = QPainterPath()
    path.moveTo(rect.topLeft())

    def run(length: float, start_angle: float, step_x: float, step_y: float,
            origin: QPointF) -> None:
        n = max(1, int(round(length / bump)))
        seg = length / n
        r = seg / 2.0
        for i in range(n):
            cx = origin.x() + step_x * (seg * i + r)
            cy = origin.y() + step_y * (seg * i + r)
            path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), start_angle, -180)

    run(rect.width(), 180, 1, 0, rect.topLeft())
    run(rect.height(), 90, 0, 1, rect.topRight())
    run(rect.width(), 0, -1, 0, rect.bottomRight())
    run(rect.height(), 270, 0, -1, rect.bottomLeft())
    path.closeSubpath()
    return path


def _arrow_path(p1: QPointF, p2: QPointF, head: float) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(p1)
    path.lineTo(p2)
    import math
    dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return path
    ux, uy = dx / length, dy / length
    head = min(head, length * 0.6)
    base = QPointF(p2.x() - ux * head, p2.y() - uy * head)
    half = head * 0.42
    left = QPointF(base.x() - uy * half, base.y() + ux * half)
    right = QPointF(base.x() + uy * half, base.y() - ux * half)
    path.addPolygon(QPolygonF([p2, left, right, p2]))
    return path


def build_item(m: Markup, content: QRectF) -> QGraphicsItem | None:
    """Turn a stored markup into a scene item positioned on the sheet."""
    if content.isEmpty() or not m.points:
        return None

    pts = [to_scene(p, content) for p in m.points]
    width = stroke_width(content)
    diag = (content.width() ** 2 + content.height() ** 2) ** 0.5
    color = QColor(m.color)
    if not color.isValid():
        color = QColor(DEFAULT_COLOR)

    if m.kind == TEXT:
        item = QGraphicsSimpleTextItem(m.text or " ")
        font = QFont()
        font.setPixelSize(100)          # scaled below; pixel size keeps
        font.setBold(True)              # the metrics predictable
        item.setFont(font)
        item.setBrush(QBrush(color))
        pen = QPen(QColor(255, 255, 255, 200), 100 * 0.06)
        item.setPen(pen)                # halo, so text reads on linework
        target = diag * _TEXT_FRACTION
        scale = target / 100.0
        item.setScale(scale)
        item.setPos(pts[0])
        return item

    path = QPainterPath()
    if m.kind == CLOUD and len(pts) >= 2:
        path = _cloud_path(QRectF(pts[0], pts[1]), diag * _CLOUD_BUMP_FRACTION)
    elif m.kind == BOX and len(pts) >= 2:
        path.addRect(QRectF(pts[0], pts[1]).normalized())
    elif m.kind == ELLIPSE and len(pts) >= 2:
        path.addEllipse(QRectF(pts[0], pts[1]).normalized())
    elif m.kind == ARROW and len(pts) >= 2:
        path = _arrow_path(pts[0], pts[1], diag * _ARROW_HEAD_FRACTION)
    elif m.kind == PEN and len(pts) >= 2:
        path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)
    else:
        return None

    item = QGraphicsPathItem(path)
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    item.setPen(pen)
    if m.kind == ARROW:
        item.setBrush(QBrush(color))    # solid arrow head
    else:
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
    return item
