"""
w2d_scan.py — exploratory scanner for a WHIP!/W2D opcode stream.

Not app code. This walks the stream counting opcodes and stops dead at
the first thing it does not understand, printing the offset and a
hexdump around it. That failure is the point: it's how the decoder gets
built, one opcode at a time, against real files instead of guesses.

    python tools\\w2d_scan.py samples\\drawing.dwf
"""
from __future__ import annotations

import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import dwf as classic   # noqa: E402

WS = b" \t\r\n"


class Stop(Exception):
    def __init__(self, pos: int, why: str):
        super().__init__(why)
        self.pos, self.why = pos, why


def _dump(data: bytes, pos: int, before: int = 32, after: int = 48) -> str:
    lo, hi = max(0, pos - before), min(len(data), pos + after)
    out = []
    for off in range(lo, hi, 16):
        chunk = data[off:off + 16]
        hexes = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        mark = " <<<" if off <= pos < off + 16 else ""
        out.append(f"    {off:08x}  {hexes}  {text}{mark}")
    return "\n".join(out)


def _skip_ascii(data: bytes, i: int) -> tuple[int, str]:
    """'(' Token ... ')' — nesting and single-quoted strings both occur."""
    start = i
    depth = 0
    name = []
    reading_name = True
    n = len(data)
    while i < n:
        c = data[i]
        if c == 0x27:                      # single-quoted string
            i += 1
            while i < n and data[i] != 0x27:
                i += 1
            i += 1
            reading_name = False
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1, "".join(name)
        elif reading_name:
            if 33 <= c < 127 and c not in (0x28, 0x29):
                name.append(chr(c))
            else:
                reading_name = False
        i += 1
    raise Stop(start, "unterminated ASCII opcode")


def _skip_ext_binary(data: bytes, i: int) -> tuple[int, int]:
    """'{' + 4-byte LE length + 2-byte opcode id ... '}'.

    The length's exact basis is the thing to pin down, so try the
    plausible readings and keep whichever lands on the '}' terminator.
    """
    start = i
    if i + 8 > len(data):
        raise Stop(i, "truncated extended-binary opcode")
    size = int.from_bytes(data[i + 1:i + 5], "little")
    opcode = data[i + 5] | (data[i + 6] << 8)
    for basis, end in (("after-length", i + 5 + size),
                       ("from-brace", i + 1 + size),
                       ("after-header", i + 7 + size)):
        if 0 < end <= len(data) and data[end - 1:end] == b"}":
            _skip_ext_binary.basis[basis] += 1
            return end, opcode
    raise Stop(start, f"extended-binary length {size} lands on no '}}' "
                      f"(opcode 0x{opcode:04x})")


_skip_ext_binary.basis = Counter()


# ── Single-byte opcodes ──────────────────────────────────────────────
#
# Each entry says how many operand bytes follow the opcode byte, or
# gives a function returning the position just past the operands.
# Everything here was confirmed against real streams, not guessed:
# an entry with the wrong length derails the stream within a few
# opcodes and the scanner stops somewhere obviously wrong.

def _fixed(n: int):
    def consume(data: bytes, i: int) -> int:
        return i + 1 + n
    return consume


def _count(data: bytes, i: int) -> tuple[int, int]:
    """Point-set count: one byte, or 0 then a 2-byte count plus 256."""
    c = data[i]
    if c:
        return i + 1, c
    return i + 3, 256 + int.from_bytes(data[i + 1:i + 3], "little")


def _quoted(data: bytes, i: int) -> int:
    """A WHIP string: either single-quoted ASCII (backslash escapes the
    next byte), or a Unicode form of '{', a 32-bit CHARACTER count,
    UTF-16LE data, and a closing '}'."""
    n = len(data)
    while i < n and data[i] in WS:
        i += 1
    if i < n and data[i] == 0x7B:                    # '{' Unicode form
        chars = int.from_bytes(data[i + 1:i + 5], "little")
        j = i + 5 + 2 * chars
        if data[j:j + 1] != b"}":
            raise Stop(i, f"unicode string of {chars} chars has no '}}'")
        return j + 1
    if i >= n or data[i] != 0x27:
        raise Stop(i, "expected a quoted string")
    i += 1
    while i < n:
        if data[i] == 0x5C:          # backslash escape
            i += 2
            continue
        if data[i] == 0x27:
            return i + 1
        i += 1
    raise Stop(i, "unterminated quoted string")


def _text_32(data: bytes, i: int) -> int:
    """'x': 32-bit relative position, then the string."""
    return _quoted(data, i + 1 + 8)


# WT_Font: a 2-byte field mask, then only the fields whose bit is set,
# in this order. The sizes were solved, not guessed: three real font
# opcodes with masks 0x006b, 0x04b0, 0x04fb and 0x04e0 give four
# equations in the field widths, and this is the only assignment that
# satisfies all four and lands each one on a valid following opcode.
_FONT_FIELDS = [
    ("name", None),        # quoted string
    ("charset", 1),
    ("pitch", 1),
    ("family", 1),
    ("style", 1),
    ("height", 4),
    ("rotation", 2),
    ("width_scale", 2),
    ("spacing", 2),
    ("oblique", 2),
    ("flags", 4),
]


def _font(data: bytes, i: int) -> int:
    mask = int.from_bytes(data[i + 1:i + 3], "little")
    j = i + 3
    for bit, (_name, size) in enumerate(_FONT_FIELDS):
        if not mask & (1 << bit):
            continue
        j = _quoted(data, j) if size is None else j + size
    return j


def _text_18(data: bytes, i: int) -> int:
    """Ctrl-X text: 32-bit position, quoted string, then a flag byte
    that (when set) is followed by a four-point bounding quad, and two
    trailing bytes."""
    j = _quoted(data, i + 1 + 8)
    flag = data[j]
    j += 1
    if flag:
        j += 32
    return j + 2


_NUM = set(b"0123456789+-.,")


def _ascii_int(data: bytes, i: int) -> tuple[int, int]:
    n = len(data)
    while i < n and data[i] in WS:
        i += 1
    start = i
    while i < n and data[i] in _NUM:
        i += 1
    if i == start:
        raise Stop(start, "expected an ASCII integer")
    return i, int(data[start:i])


def _ascii_points(data: bytes, i: int) -> int:
    """'M': ASCII macro-draw — a count, then that many 'x,y' tokens."""
    n = len(data)
    j, count = _ascii_int(data, i + 1)
    for _ in range(count):
        while j < n and data[j] in WS:
            j += 1
        start = j
        while j < n and data[j] in _NUM:
            j += 1
        if j == start:
            raise Stop(start, "expected an ASCII point")
    return j


def _points(width: int):
    """count, then `count` points of `width` bytes per ordinate pair."""
    def consume(data: bytes, i: int) -> int:
        j, n = _count(data, i + 1)
        return j + n * width * 2
    return consume


SINGLE = {
    # visibility is an on/off pair with no operands
    0x56: ("visibility-on", _fixed(0)),      # 'V'
    0x76: ("visibility-off", _fixed(0)),     # 'v'
    # Ctrl-C: RGBA colour
    0x03: ("color-rgba", _fixed(4)),
    # 'O': origin — two 32-bit ordinates, the anchor for later deltas
    0x4F: ("origin", _fixed(8)),
    # Ctrl-P: binary polygon, count then 16-bit relative points
    0x10: ("polygon-16", _points(2)),
    # Ctrl-L: a TWO-POINT LINE with 16-bit relative coords — no count
    # byte. The counted forms are the polygon/polyline opcodes.
    0x0C: ("line-16", _fixed(8)),
    # 'l': the same line with 32-bit relative coords
    0x6C: ("line-32", _fixed(16)),
    # 0x8D: macro-draw / polymarker, 16-bit relative. The toolkit notes
    # this "SHOULD have been 0x0D", but 0x0D is carriage return.
    0x8D: ("macro-draw-16", _points(2)),
    # 'N': object node — a 32-bit id tying geometry back to a DWG object
    0x4E: ("object-node", _fixed(4)),
    # 'c': colour by palette index
    0x63: ("color-index", _fixed(1)),
    # 0x92: circular arc, 32-bit relative — centre x, centre y, radius,
    # then 16-bit start and end angles.
    0x92: ("arc-32", _fixed(16)),
    # 0x0E: object node with an implied (auto-incrementing) id
    0x0E: ("object-node-auto", _fixed(0)),
    # 'x': text — position then a quoted string
    0x78: ("text-32", _text_32),
    # 'n': object node with a 16-bit id
    0x6E: ("object-node-16", _fixed(2)),
    # Ctrl-T: polytriangle strip, 16-bit relative
    0x14: ("polytriangle-16", _points(2)),
    # 'e': ellipse — 32-bit centre, major, minor, then 16-bit start
    # angle, end angle and tilt.
    0x65: ("ellipse-32", _fixed(22)),
    # Ctrl-F: font, with a field mask
    0x06: ("font", _font),
    # Ctrl-X: text with a bounding quad
    0x18: ("text-bounded", _text_18),
    # 'M': the ASCII form of macro-draw, coordinates written as text
    0x4D: ("macro-draw-ascii", _ascii_points),
    # 'm': macro-draw with 32-bit relative points
    0x6D: ("macro-draw-32", _points(4)),
    # 0xAC: set layer by number, the binary form a stream uses for every
    # layer switch after the declaring (Layer n 'name'). The operand is a
    # count, not a fixed width. From the DWF Toolkit (opcode.cpp maps
    # WD_SBBO_SET_LAYER to WT_Layer) rather than from a file: no sample
    # here has a layer opcode in it.
    0xAC: ("set-layer", lambda data, i: _count(data, i + 1)[0]),
}


def scan(data: bytes, budget_bytes: int | None = None) -> None:
    # skip the "(W2D V06.00)" header
    i = data.find(b")") + 1 if data[:1] == b"(" else 0
    n = len(data) if budget_bytes is None else min(len(data), budget_bytes)

    counts: Counter = Counter()
    ascii_names: Counter = Counter()
    ext_ids: Counter = Counter()
    t0 = time.time()
    try:
        while i < n:
            b = data[i]
            if b in WS:
                i += 1
                continue
            if b == 0x28:                     # '('
                i, name = _skip_ascii(data, i)
                counts["ascii"] += 1
                ascii_names[name[:24]] += 1
                continue
            if b == 0x7B:                     # '{'
                i, opcode = _skip_ext_binary(data, i)
                counts["ext-binary"] += 1
                ext_ids[f"0x{opcode:04x}"] += 1
                continue
            entry = SINGLE.get(b)
            if entry is not None:
                name, consume = entry
                j = consume(data, i)
                if not i < j <= n:
                    raise Stop(i, f"{name} consumed a bad range ({i} -> {j})")
                i = j
                counts[name] += 1
                continue
            raise Stop(i, f"single-byte opcode 0x{b:02x} "
                          f"({chr(b) if 32 <= b < 127 else '?'}) not implemented")
    except Stop as stop:
        dt = time.time() - t0
        print(f"\n  stopped at offset {stop.pos:,} of {len(data):,} "
              f"({stop.pos / len(data):.2%}) after {dt:.1f}s")
        print(f"  reason: {stop.why}")
        print(_dump(data, stop.pos))
    else:
        print(f"\n  scanned {n:,} bytes cleanly in {time.time() - t0:.1f}s")

    print(f"\n  opcode classes: {dict(counts)}")
    if _skip_ext_binary.basis:
        print(f"  extended-binary length basis: {dict(_skip_ext_binary.basis)}")
    if ext_ids:
        print(f"  extended-binary ids: {dict(ext_ids.most_common(15))}")
    if ascii_names:
        print(f"  ascii tokens: {dict(ascii_names.most_common(15))}")


def main(argv):
    paths = [Path(a) for a in argv[1:]] or sorted(Path("samples").glob("*.dwf"))
    for path in paths:
        print("=" * 70)
        print(path.name)
        with classic.ClassicDwf(path) as doc:
            stream = doc.graphics_stream(0)
        if not stream:
            print("  no W2D stream")
            continue
        print(f"  {len(stream):,} bytes of opcodes")
        scan(stream)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
