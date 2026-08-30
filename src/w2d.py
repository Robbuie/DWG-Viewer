"""
w2d.py — decoder for WHIP!/W2D opcode streams (classic DWF 6 and earlier).

The stream is a flat sequence of opcodes carrying both graphics and the
state they draw with. Nearly all coordinates are *relative*: each point
is a delta from a running current point, which the Origin opcode resets.
De-relativizing correctly is the whole game — one wrong operand length
and every coordinate after it is garbage.

Opcode operand layouts were recovered from real files (see
tools/w2d_scan.py, which walks a stream and stops at anything it does
not understand). The font opcode's field widths were solved from four
real masks rather than guessed.

Decoded geometry is handed to a sink one primitive at a time instead of
being accumulated: a busy sheet holds 15 million primitives, and a list
of those would cost more memory than the raster we are building.
"""
from __future__ import annotations

import re
import struct
from array import array
from typing import Protocol

WS = b" \t\r\n"
# A single number: the comma is a separator between ordinates, not part
# of one, so it is deliberately absent here.
_NUM = set(b"0123456789+-.")

# Angles are 1/65536 of a full turn.
_ANGLE_SCALE = 360.0 / 65536.0


class W2dError(Exception):
    pass


def _load_aci():
    from src.w2d_render import ACI as _aci
    return _aci


class _LazyACI:
    """The colour table lives in w2d_render; importing it at module load
    would make the two modules import each other."""
    _table = None

    def __getitem__(self, index):
        if _LazyACI._table is None:
            _LazyACI._table = _load_aci()
        return _LazyACI._table[index]


ACI = _LazyACI()


# (Layer 3 'WALLS') declares a layer and makes it current; every later
# reference to the same layer is the bare number, so the name is carried
# forward. A sheet published without layer information has none of these
# at all, which is the usual case for AutoCAD's ePlot output.
_LAYER_RE = re.compile(rb"\(Layer\s+(-?\d+)\s*(?:'((?:[^'\\]|\\.)*)')?", re.I)


def layer_label(index: int, name: str) -> str:
    """What to call a layer in the UI. Only the declaring opcode carries
    a name, and some producers give none at all."""
    return name or f"Layer {index}"


class UnsupportedOpcode(W2dError):
    def __init__(self, pos: int, byte: int):
        super().__init__(f"unsupported W2D opcode 0x{byte:02x} at offset {pos}")
        self.pos, self.byte = pos, byte


class Sink(Protocol):
    """Receives decoded primitives in draw order."""

    def polyline(self, pts: list[tuple[int, int]]) -> None: ...
    def polygon(self, pts: list[tuple[int, int]]) -> None: ...
    def polytriangle(self, pts: list[tuple[int, int]]) -> None: ...
    def arc(self, cx: int, cy: int, r: int, start: float, end: float) -> None: ...
    def ellipse(self, cx: int, cy: int, major: int, minor: int,
                start: float, end: float, tilt: float) -> None: ...
    def text(self, x: int, y: int, s: str) -> None: ...
    def set_color(self, rgba: tuple[int, int, int, int]) -> None: ...
    def set_visible(self, on: bool) -> None: ...
    def set_layer(self, index: int, label: str) -> None: ...


class GeometryCollector:
    """Sink that keeps every primitive in flat arrays.

    This is what makes sharp zooming possible. A cached raster is fixed
    at one resolution, so magnifying past it can only interpolate; with
    the geometry retained, any region can be redrawn at whatever
    resolution the screen is actually showing.

    Everything is packed into `array` buffers rather than Python objects:
    a busy sheet has ten million segments, and tuples of ints would cost
    well over a gigabyte. Packed, the same data is a few hundred MB and
    converts to numpy views for free.
    """

    def __init__(self):
        self.seg = array("i")          # x1, y1, x2, y2 per segment
        self.seg_col = array("H")
        self.pt = array("i")           # x, y per marker
        self.pt_col = array("H")
        self.arc_xyz = array("i")      # cx, cy, r per arc
        self.arc_ang = array("f")      # start, end per arc
        self.arc_col = array("H")
        # Layer index per primitive, parallel to the colour arrays. These
        # stay empty for a sheet published without layer information,
        # which is most of them, so nothing pays for the feature until a
        # layer opcode actually turns up.
        self.seg_layer = array("H")
        self.pt_layer = array("H")
        self.arc_layer = array("H")
        self.layer_names: dict[int, str] = {}
        self.layer_order: list[int] = []
        self._layer = 0
        self._track_layers = False
        self.tri: list[tuple[list, int, int]] = []   # rare; kept as-is
        # Text opcodes, kept for search. Most lettering on a published
        # ePlot sheet arrives as stroked geometry rather than text, so
        # this is usually a short list — but a title block or a tag that
        # did survive as text is exactly what someone searches for.
        self.texts: list[tuple[int, int, str]] = []
        self.palette: list[tuple[int, int, int]] = [(0, 0, 0)]
        self._pal_index = {(0, 0, 0): 0}
        self._colour = 0

    # -- colour interning ----------------------------------------------

    def _intern(self, rgb: tuple[int, int, int]) -> int:
        got = self._pal_index.get(rgb)
        if got is None:
            got = len(self.palette)
            self.palette.append(rgb)
            self._pal_index[rgb] = got
        return got

    def set_color(self, rgba):
        self._colour = self._intern((rgba[0], rgba[1], rgba[2]))

    def set_index(self, index):
        self._colour = self._intern(ACI[index & 0xFF])

    def set_visible(self, on):
        pass

    # -- layers --------------------------------------------------------

    def set_layer(self, index, label):
        if index not in self.layer_names:
            self.layer_order.append(index)
        self.layer_names[index] = label
        self._layer = index & 0xFFFF
        if not self._track_layers:
            # Everything drawn before the first layer opcode belongs to
            # no declared layer; backfill it as index 0 so the parallel
            # arrays stay in step from here on.
            self._track_layers = True
            for arr, count in ((self.seg_layer, len(self.seg) // 4),
                               (self.pt_layer, len(self.pt) // 2),
                               (self.arc_layer, len(self.arc_xyz) // 3)):
                arr.frombytes(bytes(arr.itemsize * count))

    @property
    def has_layers(self) -> bool:
        return bool(self.layer_names)

    def layers(self) -> list[str]:
        """Layer labels in the order the sheet declares them."""
        return [self.layer_names[i] for i in self.layer_order]

    # -- geometry ------------------------------------------------------

    def _chain(self, pts, close: bool) -> None:
        seg, col = self.seg, self._colour
        # Bound to a local rather than tested per segment: this loop runs
        # ten million times on a busy sheet.
        lay = self.seg_layer if self._track_layers else None
        layer = self._layer
        prev_x, prev_y = pts[0]
        for x, y in pts[1:]:
            seg.append(prev_x); seg.append(prev_y)
            seg.append(x); seg.append(y)
            self.seg_col.append(col)
            if lay is not None:
                lay.append(layer)
            prev_x, prev_y = x, y
        if close and len(pts) > 2:
            fx, fy = pts[0]
            seg.append(prev_x); seg.append(prev_y)
            seg.append(fx); seg.append(fy)
            self.seg_col.append(col)
            if lay is not None:
                lay.append(layer)

    def polyline(self, pts):
        if len(pts) > 1:
            self._chain(pts, False)

    def polygon(self, pts):
        # Opcode 0x10 is "polygon or polyline" depending on the fill
        # state, and published ePlot sheets leave fill off — so it is an
        # OPEN polyline. Closing it draws a spurious stroke across every
        # glyph: text rendered as 'PRB-MA-V' instead of 'PRB-NA-7', which
        # is how this was caught.
        if len(pts) > 1:
            self._chain(pts, False)

    def polytriangle(self, pts):
        if len(pts) > 2:
            self.tri.append((list(pts), self._colour, self._layer))

    def markers(self, pts):
        pt, col = self.pt, self._colour
        lay = self.pt_layer if self._track_layers else None
        layer = self._layer
        for x, y in pts:
            pt.append(x); pt.append(y)
            self.pt_col.append(col)
            if lay is not None:
                lay.append(layer)

    def arc(self, cx, cy, r, start, end):
        self.arc_xyz.append(cx); self.arc_xyz.append(cy); self.arc_xyz.append(r)
        self.arc_ang.append(start); self.arc_ang.append(end)
        self.arc_col.append(self._colour)
        if self._track_layers:
            self.arc_layer.append(self._layer)

    def ellipse(self, cx, cy, major, minor, start, end, tilt):
        # Stored as a circle of the larger radius; tilt and eccentricity
        # are lost, which for the handful per sheet is not worth a
        # separate array.
        self.arc_xyz.append(cx); self.arc_xyz.append(cy)
        self.arc_xyz.append(max(abs(major), abs(minor)))
        self.arc_ang.append(start); self.arc_ang.append(end)
        self.arc_col.append(self._colour)
        if self._track_layers:
            self.arc_layer.append(self._layer)

    def text(self, x, y, s):
        # Drawn lettering is already stroked geometry, so this adds
        # nothing to the picture — but the string is worth keeping so it
        # can be searched for.
        if s and s.strip() and len(self.texts) < 200_000:
            self.texts.append((x, y, s))

    # -- summary -------------------------------------------------------

    @property
    def segment_count(self) -> int:
        return len(self.seg) // 4

    @property
    def marker_count(self) -> int:
        return len(self.pt) // 2

    @property
    def arc_count(self) -> int:
        return len(self.arc_xyz) // 3

    def nbytes(self) -> int:
        return (self.seg.itemsize * len(self.seg)
                + self.seg_col.itemsize * len(self.seg_col)
                + self.pt.itemsize * len(self.pt)
                + self.pt_col.itemsize * len(self.pt_col)
                + self.arc_xyz.itemsize * len(self.arc_xyz)
                + self.arc_ang.itemsize * len(self.arc_ang))


class BoundsSink:
    """Sink that only measures — used to check a decode is sane before
    anything is drawn."""

    def __init__(self):
        self.min_x = self.min_y = 2 ** 62
        self.max_x = self.max_y = -2 ** 62
        self.counts: dict[str, int] = {}

    def _tally(self, kind: str, n: int = 1) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + n

    def _pts(self, pts) -> None:
        for x, y in pts:
            if x < self.min_x: self.min_x = x
            if x > self.max_x: self.max_x = x
            if y < self.min_y: self.min_y = y
            if y > self.max_y: self.max_y = y

    def polyline(self, pts):     self._tally("polyline"); self._pts(pts)
    def polygon(self, pts):      self._tally("polygon"); self._pts(pts)
    def polytriangle(self, pts): self._tally("polytriangle"); self._pts(pts)

    def arc(self, cx, cy, r, start, end):
        self._tally("arc")
        self._pts([(cx - r, cy - r), (cx + r, cy + r)])

    def ellipse(self, cx, cy, major, minor, start, end, tilt):
        self._tally("ellipse")
        rad = max(abs(major), abs(minor))
        self._pts([(cx - rad, cy - rad), (cx + rad, cy + rad)])

    def text(self, x, y, s):
        self._tally("text")
        self._pts([(x, y)])

    def set_color(self, rgba): self._tally("color")
    def set_index(self, index): self._tally("color")
    def set_visible(self, on): self._tally("visibility")
    def set_layer(self, index, label): self._tally("layer")


# ── Font field table ─────────────────────────────────────────────────
#
# A 2-byte mask selects which fields follow, in this order. The widths
# were solved from four real opcodes (masks 0x006b, 0x04b0, 0x04fb,
# 0x04e0): those give four independent equations, and this is the only
# assignment satisfying all of them.

_FONT_FIELDS = (
    ("name", None), ("charset", 1), ("pitch", 1), ("family", 1),
    ("style", 1), ("height", 4), ("rotation", 2), ("width_scale", 2),
    ("spacing", 2), ("oblique", 2), ("flags", 4),
)


class W2dDecoder:
    def __init__(self, data: bytes):
        self.data = data
        self.n = len(data)
        self.x = 0          # running current point
        self.y = 0
        self.visible = True
        self.color = (0, 0, 0, 255)
        self.layer = 0
        self.layer_names: dict[int, str] = {}
        # Every extended-ASCII token the stream used, counted. Cheap to
        # keep and the first thing worth looking at when a producer
        # spells an opcode differently than the files this was built on.
        self.ascii_tokens: dict[str, int] = {}

    # -- primitive readers ---------------------------------------------

    def _count(self, i: int) -> tuple[int, int]:
        c = self.data[i]
        if c:
            return i + 1, c
        return i + 3, 256 + int.from_bytes(self.data[i + 1:i + 3], "little")

    def _rel_points(self, i: int, n: int, wide: bool):
        """Read n relative points and de-relativize against the current
        point, which then advances to the last of them."""
        data = self.data
        if wide:
            vals = struct.unpack_from(f"<{2 * n}i", data, i)
            size = n * 8
        else:
            vals = struct.unpack_from(f"<{2 * n}h", data, i)
            size = n * 4
        x, y = self.x, self.y
        pts = []
        append = pts.append
        for k in range(0, 2 * n, 2):
            x += vals[k]
            y += vals[k + 1]
            append((x, y))
        self.x, self.y = x, y
        return i + size, pts

    def _string(self, i: int) -> tuple[int, str]:
        data, n = self.data, self.n
        while i < n and data[i] in WS:
            i += 1
        if i < n and data[i] == 0x7B:               # '{' UTF-16 form
            chars = int.from_bytes(data[i + 1:i + 5], "little")
            end = i + 5 + 2 * chars
            if data[end:end + 1] != b"}":
                raise W2dError(f"bad unicode string at {i}")
            return end + 1, data[i + 5:end].decode("utf-16-le", "replace")
        if i >= n or data[i] != 0x27:
            raise W2dError(f"expected a string at {i}")
        i += 1
        out = bytearray()
        while i < n:
            if data[i] == 0x5C:
                out.append(data[i + 1])
                i += 2
                continue
            if data[i] == 0x27:
                return i + 1, out.decode("latin-1")
            out.append(data[i])
            i += 1
        raise W2dError("unterminated string")

    def _font(self, i: int) -> int:
        mask = int.from_bytes(self.data[i:i + 2], "little")
        j = i + 2
        for bit, (_name, size) in enumerate(_FONT_FIELDS):
            if not mask & (1 << bit):
                continue
            if size is None:
                j, _ = self._string(j)
            else:
                j += size
        return j

    def _ascii_int(self, i: int) -> tuple[int, int]:
        data, n = self.data, self.n
        while i < n and data[i] in WS:
            i += 1
        start = i
        while i < n and data[i] in _NUM:
            i += 1
        if i == start:
            raise W2dError(f"expected an integer at {start}")
        return i, int(data[start:i])

    def _ascii_opcode(self, i: int) -> tuple[int, str]:
        """'(' Token ... ')' — returns the end and the leading token.

        Most of these are metadata this viewer has no use for, but the
        token has to be read rather than skipped past: Layer is one of
        them, and it is the only place a classic DWF names its layers.
        """
        data, n = self.data, self.n
        depth = 0
        name: list[str] = []
        reading = True
        while i < n:
            c = data[i]
            if c == 0x27:                       # single-quoted string
                i += 1
                while i < n and data[i] != 0x27:
                    i += 2 if data[i] == 0x5C else 1
                i += 1
                reading = False
                continue
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    return i + 1, "".join(name)
            elif reading:
                if 33 <= c < 127:
                    name.append(chr(c))
                else:
                    reading = False
            i += 1
        raise W2dError("unterminated ASCII opcode")

    def _layer_opcode(self, body: bytes, sink) -> None:
        """(Layer <number> ['name']) — declares a layer and makes it
        current. The name comes with the first mention only; later
        references are the bare number, so names are carried forward.

        A name given in the Unicode '{...}' string form is not read back
        here; that layer keeps its numeric label rather than being
        dropped.
        """
        m = _LAYER_RE.match(body)
        if m is None:
            return
        index = int(m.group(1))
        raw = m.group(2)
        if raw is not None:
            out = bytearray()
            k = 0
            while k < len(raw):
                if raw[k] == 0x5C and k + 1 < len(raw):
                    k += 1
                out.append(raw[k])
                k += 1
            got = out.decode("latin-1").strip()
            if got:
                self.layer_names[index] = got
        self.layer = index
        setter = getattr(sink, "set_layer", None)
        if setter is not None:
            setter(index, layer_label(index, self.layer_names.get(index, "")))

    def _skip_ext_binary(self, i: int) -> int:
        size = int.from_bytes(self.data[i + 1:i + 5], "little")
        for end in (i + 5 + size, i + 1 + size, i + 7 + size):
            if 0 < end <= self.n and self.data[end - 1:end] == b"}":
                return end
        raise W2dError(f"bad extended-binary opcode at {i}")

    # -- main loop -----------------------------------------------------

    def run(self, sink) -> None:
        data, n = self.data, self.n
        unpack = struct.unpack_from

        # skip the "(W2D V06.00)" header
        i = data.find(b")") + 1 if data[:1] == b"(" else 0

        while i < n:
            b = data[i]

            if b in WS:
                i += 1
                continue

            # ---- the hot three: polygons, lines, markers --------------
            if b == 0x10:                       # polygon, 16-bit relative
                i, count = self._count(i + 1)
                i, pts = self._rel_points(i, count, False)
                sink.polygon(pts)
                continue

            if b == 0x0C:                       # two-point line, 16-bit
                i, pts = self._rel_points(i + 1, 2, False)
                sink.polyline(pts)
                continue

            if b == 0x8D:                       # marker / macro draw, 16-bit
                i, count = self._count(i + 1)
                i, pts = self._rel_points(i, count, False)
                sink.markers(pts)
                continue

            if b == 0x92:                       # circular arc, 32-bit relative
                cx, cy, r = unpack("<iii", data, i + 1)
                start, end = unpack("<HH", data, i + 13)
                self.x += cx
                self.y += cy
                sink.arc(self.x, self.y, r,
                         start * _ANGLE_SCALE, end * _ANGLE_SCALE)
                i += 17
                continue

            if b == 0x28:                       # '(' ASCII opcode
                j, token = self._ascii_opcode(i)
                self.ascii_tokens[token] = self.ascii_tokens.get(token, 0) + 1
                if token.lower() == "layer":
                    self._layer_opcode(data[i:j], sink)
                i = j
                continue

            if b == 0x7B:                       # '{' extended binary
                i = self._skip_ext_binary(i)
                continue

            # ---- everything else -------------------------------------
            if b == 0x14:                       # polytriangle, 16-bit
                i, count = self._count(i + 1)
                i, pts = self._rel_points(i, count, False)
                sink.polytriangle(pts)
                continue

            if b == 0x65:                       # ellipse, 32-bit relative
                cx, cy, major, minor = unpack("<iiii", data, i + 1)
                start, end, tilt = unpack("<HHH", data, i + 17)
                self.x += cx
                self.y += cy
                sink.ellipse(self.x, self.y, major, minor,
                             start * _ANGLE_SCALE, end * _ANGLE_SCALE,
                             tilt * _ANGLE_SCALE)
                i += 23
                continue

            if b == 0x78:                       # text, position then string
                dx, dy = unpack("<ii", data, i + 1)
                self.x += dx
                self.y += dy
                i, s = self._string(i + 9)
                sink.text(self.x, self.y, s)
                continue

            if b == 0x18:                       # text with a bounding quad
                dx, dy = unpack("<ii", data, i + 1)
                self.x += dx
                self.y += dy
                j, s = self._string(i + 9)
                flag = data[j]
                j += 1
                if flag:
                    j += 32
                i = j + 2
                sink.text(self.x, self.y, s)
                continue

            if b == 0x56:                       # 'V' visible
                self.visible = True
                sink.set_visible(True)
                i += 1
                continue

            if b == 0x76:                       # 'v' invisible
                self.visible = False
                sink.set_visible(False)
                i += 1
                continue

            if b == 0x03:                       # RGBA colour
                self.color = (data[i + 1], data[i + 2], data[i + 3], data[i + 4])
                sink.set_color(self.color)
                i += 5
                continue

            if b == 0x63:                       # 'c' colour by index
                sink.set_index(data[i + 1])
                i += 2
                continue

            if b == 0x4F:                       # 'O' origin — absolute
                self.x, self.y = unpack("<ii", data, i + 1)
                i += 9
                continue

            if b == 0x6C:                       # 'l' two-point line, 32-bit
                i, pts = self._rel_points(i + 1, 2, True)
                sink.polyline(pts)
                continue

            if b == 0x6D:                       # 'm' markers, 32-bit
                i, count = self._count(i + 1)
                i, pts = self._rel_points(i, count, True)
                sink.markers(pts)
                continue

            if b == 0x4D:                       # 'M' markers, ASCII absolute
                i, count = self._ascii_int(i + 1)
                pts = []
                for _ in range(count):
                    i, x = self._ascii_int(i)
                    while i < n and data[i] == 0x2C:   # ','
                        i += 1
                    i, y = self._ascii_int(i)
                    pts.append((x, y))
                if pts:
                    self.x, self.y = pts[-1]
                sink.markers(pts)
                continue

            if b == 0x06:                       # font
                i = self._font(i + 1)
                continue

            if b == 0x4E:                       # 'N' object node, 32-bit id
                i += 5
                continue
            if b == 0x6E:                       # 'n' object node, 16-bit id
                i += 3
                continue
            if b == 0x0E:                       # object node, implied id
                i += 1
                continue

            raise UnsupportedOpcode(i, b)
