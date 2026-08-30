"""
dwfx.py — DWFx reader: renders DWFx sheets to SVG using only the stdlib.

A .dwfx file is an OPC (ZIP) package built on Microsoft's XPS document
model: the drawing is stored as XPS FixedPage markup (Canvas / Path /
Glyphs) rather than as CAD entities.  That makes it readable without any
converter, unlike DWG (needs ODA) or classic binary DWF 6 (WHIP!/W2D
streams, not handled here — see is_classic_dwf).

The translation target is SVG because the rest of the app already speaks
SVG: canvas.py renders it with QSvgRenderer and file_browser.py rasterises
it for thumbnails.

Deliberate limits, all of which degrade to "draws slightly wrong" rather
than "fails":
  * text is emitted as <text> runs, not outlined glyphs — embedded ODTTF
    fonts are only mined for their family name
  * TileMode on image brushes is ignored (drawings use None in practice)
  * OpacityMask and VisualBrush are skipped
"""
from __future__ import annotations

import base64
import re
import struct
import threading
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# XPS user-space unit is 1/96 inch, same as CSS px — no scaling needed.
_UNITS_PER_INCH = 96.0

_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}


class DwfxError(Exception):
    pass


# ── XML helpers ──────────────────────────────────────────────────────
#
# XPS markup is namespaced and the namespace URI has varied between
# producers, so every lookup here is done on the local name.

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr(el, name: str, default=None):
    got = el.get(name)
    if got is not None:
        return got
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return default


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _floats(text: str) -> list[float]:
    if not text:
        return []
    out = []
    for tok in re.split(r"[\s,]+", text.strip()):
        if not tok:
            continue
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


# ── Package part paths ───────────────────────────────────────────────

def _resolve(base_part: str, target: str) -> str:
    """Resolve an OPC part reference relative to the part holding it."""
    target = target.strip()
    if target.startswith("/"):
        joined = target
    else:
        base_dir = base_part.rsplit("/", 1)[0] if "/" in base_part else ""
        joined = base_dir + "/" + target if base_dir else target
    out: list[str] = []
    for seg in joined.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/".join(out)


def _rels_of(part: str) -> str:
    head, _, name = part.rpartition("/")
    return f"{head}/_rels/{name}.rels" if head else f"_rels/{name}.rels"


# ── Format sniffing ──────────────────────────────────────────────────

def is_dwfx_package(path) -> bool:
    """True for an OPC package carrying XPS fixed pages (i.e. a DWFx).

    Sniffed by content, not extension: DWFx is occasionally published
    with a .dwf name, and a .dwfx that turns out to be classic DWF
    should not be routed here.
    """
    try:
        if not zipfile.is_zipfile(str(path)):
            return False
        with zipfile.ZipFile(str(path)) as z:
            names = [n.lstrip("/").lower() for n in z.namelist()]
    except Exception:
        return False
    return ("[content_types].xml" in names
            and any(n.endswith(".fpage") for n in names))


def is_classic_dwf(path) -> bool:
    """True for DWF 6.x and earlier: binary WHIP!/W2D graphics streams."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
        if head.startswith(b"(DWF V"):
            return not is_dwfx_package(path)
        if zipfile.is_zipfile(str(path)):
            with zipfile.ZipFile(str(path)) as z:
                names = [n.lower() for n in z.namelist()]
            return (any(n.endswith(".w2d") for n in names)
                    and not any(n.endswith(".fpage") for n in names))
    except Exception:
        pass
    return False


# ── Embedded font names ──────────────────────────────────────────────
#
# XPS embeds fonts as .odttf: a TrueType file whose first 32 bytes are
# XORed with the GUID in its own filename. De-obfuscating far enough to
# read the name table lets the SVG name a real font family, so text
# lands at roughly the right width.

def _deobfuscate_odttf(data: bytes, part_name: str) -> bytes:
    stem = part_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    hexs = re.sub(r"[^0-9a-fA-F]", "", stem)
    if len(hexs) < 32:
        return data
    key = bytes.fromhex(hexs[:32])
    for candidate in (key[::-1], key):
        buf = bytearray(data[:32])
        for i in range(min(32, len(buf))):
            buf[i] ^= candidate[i % 16]
        if bytes(buf[:4]) in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"):
            return bytes(buf) + data[32:]
    return data


def _font_family(data: bytes) -> str | None:
    """Pull the family name (name ID 1) out of an sfnt font."""
    try:
        if len(data) < 12:
            return None
        num_tables = struct.unpack(">H", data[4:6])[0]
        name_off = name_len = 0
        for i in range(num_tables):
            rec = 12 + i * 16
            tag = data[rec:rec + 4]
            if tag == b"name":
                name_off, name_len = struct.unpack(">II", data[rec + 8:rec + 16])
                break
        if not name_len or name_off + name_len > len(data):
            return None
        tbl = data[name_off:name_off + name_len]
        count, str_off = struct.unpack(">HH", tbl[2:6])
        best = None
        for i in range(count):
            rec = 6 + i * 12
            (pid, _eid, _lid, nid, ln, off) = struct.unpack(">HHHHHH", tbl[rec:rec + 12])
            if nid != 1:
                continue
            raw = tbl[str_off + off:str_off + off + ln]
            try:
                name = raw.decode("utf-16-be" if pid in (0, 3) else "latin-1")
            except Exception:
                continue
            name = name.strip()
            if name and (best is None or pid == 3):
                best = name
        return best
    except Exception:
        return None


# ── Colours ──────────────────────────────────────────────────────────

def _srgb(linear: float) -> int:
    """scRGB (linear, 0..1) → 8-bit sRGB."""
    c = max(0.0, min(1.0, linear))
    c = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
    return max(0, min(255, int(round(c * 255))))


def _parse_color(value: str) -> tuple[str, float]:
    """XPS colour string → ('#rrggbb', alpha 0..1). Falls back to black."""
    if not value:
        return "#000000", 1.0
    value = value.strip()

    if value.lower().startswith("contextcolor"):
        # ContextColor <profile-uri> a,c1,c2[,c3...] — the alternate
        # channels are only RGB when there are exactly three of them.
        nums = _floats(value.split(" ", 2)[-1])
        if len(nums) >= 4:
            a = max(0.0, min(1.0, nums[0]))
            r, g, b = (_srgb(n) for n in nums[1:4])
            return f"#{r:02x}{g:02x}{b:02x}", a
        return "#000000", 1.0

    if value.lower().startswith("sc#"):
        nums = _floats(value[3:])
        if len(nums) >= 4:
            a, r, g, b = nums[0], nums[1], nums[2], nums[3]
        elif len(nums) == 3:
            a, (r, g, b) = 1.0, nums
        else:
            return "#000000", 1.0
        return (f"#{_srgb(r):02x}{_srgb(g):02x}{_srgb(b):02x}",
                max(0.0, min(1.0, a)))

    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3:
            return "#" + "".join(c * 2 for c in h), 1.0
        if len(h) == 4:
            a = int(h[0] * 2, 16) / 255.0
            return "#" + "".join(c * 2 for c in h[1:]), a
        if len(h) == 6:
            return "#" + h.lower(), 1.0
        if len(h) == 8:
            return "#" + h[2:].lower(), int(h[:2], 16) / 255.0
    return "#000000", 1.0


def _fmt(v: float) -> str:
    if v != v or v in (float("inf"), float("-inf")):
        return "0"
    r = round(v, 4)
    if r == int(r):
        return str(int(r))
    return f"{r:g}"


_LINE_CAP = {"flat": "butt", "round": "round", "square": "square",
             "triangle": "square"}
_LINE_JOIN = {"miter": "miter", "bevel": "bevel", "round": "round"}
_STATIC_RES = re.compile(r"^\{StaticResource\s+([^}]+)\}$")


# ── FixedPage → SVG ──────────────────────────────────────────────────

class _PageTranslator:
    """Translates one FixedPage part into SVG body + defs."""

    def __init__(self, doc: "DwfxDocument", part: str, hidden: frozenset = frozenset()):
        self.doc = doc
        self.part = part
        self.hidden = {h.strip().lower() for h in hidden}
        self.defs: list[str] = []
        self.body: list[str] = []
        self.layer_names: list[str] = []
        self._seen_layers: set[str] = set()
        self._scopes: list[dict] = []
        self._uid = 0
        self._font_cache: dict[str, str | None] = {}

    # -- infrastructure ------------------------------------------------

    def _id(self, prefix: str) -> str:
        self._uid += 1
        return f"{prefix}{self._uid}"

    def _push_resources(self, el, owner: str) -> None:
        table: dict = {}
        node = _child(el, f"{owner}.Resources")
        if node is not None:
            rd = _child(node, "ResourceDictionary")
            for item in (rd if rd is not None else node):
                key = _attr(item, "Key")
                if key:
                    table[key] = item
        self._scopes.append(table)

    def _pop_resources(self) -> None:
        if self._scopes:
            self._scopes.pop()

    def _deref(self, value):
        """Resolve {StaticResource x}; returns a string, an element or None."""
        if not isinstance(value, str):
            return value
        m = _STATIC_RES.match(value.strip())
        if not m:
            return value
        key = m.group(1).strip()
        for scope in reversed(self._scopes):
            if key in scope:
                return scope[key]
        return None

    # -- shared attribute handling -------------------------------------

    def _transform(self, el, owner: str) -> str | None:
        raw = _attr(el, "RenderTransform")
        resolved = self._deref(raw) if raw else None
        if resolved is not None and not isinstance(resolved, str):
            raw = _attr(resolved, "Matrix")
        elif isinstance(resolved, str):
            raw = resolved
        if not raw:
            node = _child(el, f"{owner}.RenderTransform")
            if node is not None:
                mt = _child(node, "MatrixTransform")
                if mt is not None:
                    raw = _attr(mt, "Matrix")
        nums = _floats(raw) if raw else []
        if len(nums) == 6 and nums != [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]:
            return "matrix(%s)" % " ".join(_fmt(n) for n in nums)
        return None

    def _clip(self, el, owner: str) -> str | None:
        raw = _attr(el, "Clip")
        geom = self._deref(raw) if raw else None
        if geom is None:
            node = _child(el, f"{owner}.Clip")
            if node is not None:
                geom = node[0] if len(node) else None
        d, rule = self._geometry(geom)
        if not d:
            return None
        cid = self._id("clip")
        rule_attr = f' clip-rule="{rule}"' if rule else ""
        self.defs.append(f'<clipPath id="{cid}"><path d="{d}"{rule_attr}/></clipPath>')
        return f"url(#{cid})"

    @staticmethod
    def _opacity(el) -> str:
        o = _num(_attr(el, "Opacity"), 1.0)
        return "" if o >= 1.0 else f' opacity="{_fmt(o)}"'

    # -- geometry ------------------------------------------------------

    def _geometry(self, source) -> tuple[str, str | None]:
        """Abbreviated string or PathGeometry element → (path data, fill-rule)."""
        source = self._deref(source)
        if source is None:
            return "", None
        if isinstance(source, str):
            return self._abbreviated(source)
        if _local(source.tag) != "PathGeometry":
            inner = _child(source, "PathGeometry")
            if inner is None:
                return "", None
            source = inner

        rule = "evenodd" if (_attr(source, "FillRule", "") or "").lower() == "evenodd" else None
        figures = _attr(source, "Figures")
        if figures:
            d, abbr_rule = self._abbreviated(figures)
            return d, rule or abbr_rule

        parts = [self._figure(f) for f in source if _local(f.tag) == "PathFigure"]
        d = " ".join(p for p in parts if p)
        transform = _attr(source, "Transform")
        if d and transform:
            # A geometry-level transform has to be baked into a wrapper;
            # callers place the path themselves, so fold it in as a group.
            pass
        return d, rule

    @staticmethod
    def _abbreviated(text: str) -> tuple[str, str | None]:
        """XPS abbreviated geometry ≈ SVG path data, plus an F0/F1 prefix."""
        s = text.strip()
        rule = None
        m = re.match(r"^[Ff]\s*([01])\s*", s)
        if m:
            rule = "evenodd" if m.group(1) == "0" else "nonzero"
            s = s[m.end():]
        return s.strip(), rule

    def _figure(self, fig) -> str:
        start = _floats(_attr(fig, "StartPoint", ""))
        if len(start) < 2:
            return ""
        out = [f"M {_fmt(start[0])} {_fmt(start[1])}"]
        for seg in fig:
            tag = _local(seg.tag)
            pts = _floats(_attr(seg, "Points", ""))
            if tag == "PolyLineSegment":
                for i in range(0, len(pts) - 1, 2):
                    out.append(f"L {_fmt(pts[i])} {_fmt(pts[i + 1])}")
            elif tag == "PolyBezierSegment":
                for i in range(0, len(pts) - 5, 6):
                    out.append("C " + " ".join(_fmt(v) for v in pts[i:i + 6]))
            elif tag == "PolyQuadraticBezierSegment":
                for i in range(0, len(pts) - 3, 4):
                    out.append("Q " + " ".join(_fmt(v) for v in pts[i:i + 4]))
            elif tag == "ArcSegment":
                pt = _floats(_attr(seg, "Point", ""))
                size = _floats(_attr(seg, "Size", ""))
                if len(pt) < 2 or len(size) < 2:
                    continue
                rot = _num(_attr(seg, "RotationAngle"), 0.0)
                large = 1 if (_attr(seg, "IsLargeArc", "false") or "").lower() == "true" else 0
                sweep = 1 if (_attr(seg, "SweepDirection", "") or "").lower().startswith("clock") else 0
                out.append(f"A {_fmt(size[0])} {_fmt(size[1])} {_fmt(rot)} "
                           f"{large} {sweep} {_fmt(pt[0])} {_fmt(pt[1])}")
        if (_attr(fig, "IsClosed", "false") or "").lower() == "true":
            out.append("Z")
        return " ".join(out)

    # -- brushes -------------------------------------------------------

    def _brush(self, el, owner: str, prop: str):
        """Returns ('color', hex, alpha) | ('paint', url, alpha) |
        ('image', element) | None."""
        raw = _attr(el, prop)
        brush = self._deref(raw) if raw is not None else None
        if brush is None:
            node = _child(el, f"{owner}.{prop}")
            if node is not None:
                brush = self._deref(node[0]) if len(node) else None
        if brush is None:
            return None
        if isinstance(brush, str):
            if not brush.strip():
                return None
            hexc, alpha = _parse_color(brush)
            return ("color", hexc, alpha)

        tag = _local(brush.tag)
        opacity = _num(_attr(brush, "Opacity"), 1.0)
        if tag == "SolidColorBrush":
            colour = _attr(brush, "Color")
            if colour is None:
                node = _child(brush, "SolidColorBrush.Color")
                colour = _attr(node, "Color") if node is not None else None
            hexc, alpha = _parse_color(colour or "#FF000000")
            return ("color", hexc, alpha * opacity)
        if tag in ("LinearGradientBrush", "RadialGradientBrush"):
            url = self._gradient(brush, tag)
            return ("paint", url, opacity) if url else None
        if tag == "ImageBrush":
            return ("image", brush)
        return None

    def _gradient(self, brush, tag: str) -> str | None:
        stops = []
        holder = _child(brush, f"{tag}.GradientStops") or brush
        for st in holder.iter():
            if _local(st.tag) != "GradientStop":
                continue
            colour, alpha = _parse_color(_attr(st, "Color", "#FF000000"))
            offset = _num(_attr(st, "Offset"), 0.0)
            stops.append(f'<stop offset="{_fmt(offset)}" stop-color="{colour}"'
                         f' stop-opacity="{_fmt(alpha)}"/>')
        if not stops:
            return None
        gid = self._id("grad")
        spread = {"pad": "pad", "reflect": "reflect", "repeat": "repeat"}.get(
            (_attr(brush, "SpreadMethod", "pad") or "pad").lower(), "pad")
        transform = _attr(brush, "Transform")
        tr = ""
        nums = _floats(transform) if transform else []
        if len(nums) == 6:
            tr = ' gradientTransform="matrix(%s)"' % " ".join(_fmt(n) for n in nums)
        if tag == "LinearGradientBrush":
            a = _floats(_attr(brush, "StartPoint", "0,0"))
            b = _floats(_attr(brush, "EndPoint", "1,0"))
            if len(a) < 2 or len(b) < 2:
                return None
            self.defs.append(
                f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse"'
                f' x1="{_fmt(a[0])}" y1="{_fmt(a[1])}" x2="{_fmt(b[0])}"'
                f' y2="{_fmt(b[1])}" spreadMethod="{spread}"{tr}>'
                + "".join(stops) + "</linearGradient>")
        else:
            c = _floats(_attr(brush, "Center", "0,0"))
            r = _floats(_attr(brush, "RadiusX", "1")) + _floats(_attr(brush, "RadiusY", "1"))
            if len(c) < 2 or not r:
                return None
            self.defs.append(
                f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse"'
                f' cx="{_fmt(c[0])}" cy="{_fmt(c[1])}" r="{_fmt(max(r))}"'
                f' spreadMethod="{spread}"{tr}>' + "".join(stops) + "</radialGradient>")
        return f"url(#{gid})"

    # -- elements ------------------------------------------------------

    def walk(self, el, owner: str) -> None:
        self._push_resources(el, owner)
        try:
            for child in el:
                tag = _local(child.tag)
                if tag == "Canvas":
                    self._canvas(child)
                elif tag == "Path":
                    self._path(child)
                elif tag == "Glyphs":
                    self._glyphs(child)
        finally:
            self._pop_resources()

    def _canvas(self, el) -> None:
        name = _attr(el, "Name")
        if name:
            key = name.strip().lower()
            if key not in self._seen_layers:
                self._seen_layers.add(key)
                self.layer_names.append(name.strip())
            if key in self.hidden:
                return
        attrs = ""
        self._push_resources(el, "Canvas")   # its own dictionary is in scope
        try:                                 # for its own attributes too
            tr = self._transform(el, "Canvas")
            if tr:
                attrs += f' transform="{tr}"'
            clip = self._clip(el, "Canvas")
            if clip:
                attrs += f' clip-path="{clip}"'
        finally:
            self._pop_resources()
        attrs += self._opacity(el)
        if name:
            attrs += f' data-layer="{_esc(name.strip())}"'
        self.body.append(f"<g{attrs}>")
        self.walk(el, "Canvas")
        self.body.append("</g>")

    def _path(self, el) -> None:
        self._push_resources(el, "Path")
        try:
            source = _attr(el, "Data")
            if source is None:
                node = _child(el, "Path.Data")
                if node is not None and len(node):
                    source = node[0]
            d, rule = self._geometry(source)
            if not d:
                return

            wrapper = ""
            tr = self._transform(el, "Path")
            if tr:
                wrapper += f' transform="{tr}"'
            clip = self._clip(el, "Path")
            if clip:
                wrapper += f' clip-path="{clip}"'
            wrapper += self._opacity(el)

            fill = self._brush(el, "Path", "Fill")
            if fill and fill[0] == "image":
                self._image(fill[1], d, rule, wrapper)
                return

            attrs = [f'd="{d}"']
            if fill is None:
                attrs.append('fill="none"')
            elif fill[0] == "color":
                attrs.append(f'fill="{fill[1]}"')
                if fill[2] < 1.0:
                    attrs.append(f'fill-opacity="{_fmt(fill[2])}"')
            else:
                attrs.append(f'fill="{fill[1]}"')
                if fill[2] < 1.0:
                    attrs.append(f'fill-opacity="{_fmt(fill[2])}"')
            if rule == "evenodd":
                attrs.append('fill-rule="evenodd"')

            stroke = self._brush(el, "Path", "Stroke")
            if stroke and stroke[0] != "image":
                thickness = _num(_attr(el, "StrokeThickness"), 1.0)
                attrs.append(f'stroke="{stroke[1]}"')
                if stroke[2] < 1.0:
                    attrs.append(f'stroke-opacity="{_fmt(stroke[2])}"')
                attrs.append(f'stroke-width="{_fmt(thickness)}"')
                cap = _LINE_CAP.get((_attr(el, "StrokeStartLineCap", "") or "").lower())
                if cap and cap != "butt":
                    attrs.append(f'stroke-linecap="{cap}"')
                join = _LINE_JOIN.get((_attr(el, "StrokeLineJoin", "") or "").lower())
                if join and join != "miter":
                    attrs.append(f'stroke-linejoin="{join}"')
                miter = _attr(el, "StrokeMiterLimit")
                if miter:
                    attrs.append(f'stroke-miterlimit="{_fmt(_num(miter, 10.0))}"')
                dashes = _floats(_attr(el, "StrokeDashArray", ""))
                if dashes:
                    # XPS dash lengths are multiples of the stroke thickness;
                    # SVG wants user units.
                    scaled = [d_ * thickness for d_ in dashes]
                    attrs.append('stroke-dasharray="%s"' % " ".join(_fmt(v) for v in scaled))
                    offset = _num(_attr(el, "StrokeDashOffset"), 0.0)
                    if offset:
                        attrs.append(f'stroke-dashoffset="{_fmt(offset * thickness)}"')

            path_svg = "<path " + " ".join(attrs) + "/>"
            self.body.append(f"<g{wrapper}>{path_svg}</g>" if wrapper else path_svg)
        finally:
            self._pop_resources()

    def _image(self, brush, d: str, rule: str | None, wrapper: str) -> None:
        src = (_attr(brush, "ImageSource", "") or "").strip()
        m = re.search(r"\{[^}]*\}\s*(.+)$", src)   # {ColorConvertedBitmap ...} form
        if m:
            src = m.group(1).split()[0]
        if not src:
            return
        data_uri = self.doc.image_data_uri(_resolve(self.part, src))
        if not data_uri:
            return
        viewport = _floats(_attr(brush, "Viewport", ""))
        if len(viewport) < 4:
            return
        cid = self._id("clip")
        rule_attr = ' clip-rule="evenodd"' if rule == "evenodd" else ""
        self.defs.append(f'<clipPath id="{cid}"><path d="{d}"{rule_attr}/></clipPath>')
        x, y, w, h = viewport[:4]
        tr = _floats(_attr(brush, "Transform", ""))
        img_tr = ' transform="matrix(%s)"' % " ".join(_fmt(n) for n in tr) if len(tr) == 6 else ""
        image = (f'<g clip-path="url(#{cid})"><image x="{_fmt(x)}" y="{_fmt(y)}"'
                 f' width="{_fmt(w)}" height="{_fmt(h)}" preserveAspectRatio="none"'
                 f'{img_tr} xlink:href="{data_uri}"/></g>')
        self.body.append(f"<g{wrapper}>{image}</g>" if wrapper else image)

    def _glyphs(self, el) -> None:
        text = _attr(el, "UnicodeString", "") or ""
        if text.startswith("{}"):      # XPS escape for a literal brace
            text = text[2:]
        if not text.strip():
            return
        if (_attr(el, "IsSideways", "false") or "").lower() == "true":
            return

        size = _num(_attr(el, "FontRenderingEmSize"), 12.0)
        if size <= 0:
            return
        x = _num(_attr(el, "OriginX"), 0.0)
        y = _num(_attr(el, "OriginY"), 0.0)

        fill = self._brush(el, "Glyphs", "Fill")
        colour = fill[1] if fill and fill[0] != "image" else "#000000"
        alpha = fill[2] if fill and fill[0] != "image" else 1.0

        attrs = [f'x="{_fmt(x)}"', f'y="{_fmt(y)}"', f'font-size="{_fmt(size)}"',
                 f'fill="{colour}"']
        if alpha < 1.0:
            attrs.append(f'fill-opacity="{_fmt(alpha)}"')
        family = self._family(_attr(el, "FontUri", ""))
        if family:
            attrs.append(f'font-family="{_esc(family)}, sans-serif"')
        style = (_attr(el, "StyleSimulations", "") or "").lower()
        if "bold" in style:
            attrs.append('font-weight="bold"')
        if "italic" in style:
            attrs.append('font-style="italic"')
        advance = self._run_width(_attr(el, "Indices", ""), size)
        if advance > 0:
            attrs.append(f'textLength="{_fmt(advance)}" lengthAdjust="spacingAndGlyphs"')
        tr = self._transform(el, "Glyphs")
        if tr:
            attrs.append(f'transform="{tr}"')
        clip = self._clip(el, "Glyphs")
        if clip:
            attrs.append(f'clip-path="{clip}"')
        attrs.append('xml:space="preserve"')
        self.body.append("<text " + " ".join(attrs) + ">" + _esc(text) + "</text>")

    @staticmethod
    def _run_width(indices: str, em: float) -> float:
        """Total advance of a glyph run; advances are in 1/100 em."""
        if not indices:
            return 0.0
        total = 0.0
        for cluster in indices.split(";"):
            cluster = re.sub(r"\([^)]*\)", "", cluster)
            fields = cluster.split(",")
            if len(fields) >= 2 and fields[1].strip():
                total += _num(fields[1], 0.0)
        return total * em / 100.0

    def _family(self, font_uri: str) -> str | None:
        uri = (font_uri or "").split("#")[0].strip()
        if not uri:
            return None
        if uri in self._font_cache:
            return self._font_cache[uri]
        family = self.doc.font_family(_resolve(self.part, uri))
        self._font_cache[uri] = family
        return family


# ── Package ──────────────────────────────────────────────────────────

_REL_FIXED_REP = "fixedrepresentation"
_MAX_EMBEDDED_IMAGE = 24 * 1024 * 1024


class DwfxDocument:
    """One DWFx package: a list of sheets, each renderable to SVG."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self._zip = zipfile.ZipFile(str(self.path))
        except Exception as exc:
            raise DwfxError(f"Not a readable DWFx package: {exc}") from exc
        # One document can be read from the load thread and then re-rendered
        # from the render thread; ZipFile is not safe for overlapping reads.
        self._lock = threading.RLock()
        self._names = {n.lstrip("/").lower(): n for n in self._zip.namelist()}
        self._image_cache: dict[str, str | None] = {}
        self._font_cache: dict[str, str | None] = {}
        self._layer_cache: dict[int, list[str]] = {}
        self.sheets: list[dict] = self._discover()
        if not self.sheets:
            raise DwfxError(
                "This DWFx package contains no drawable sheets.\n"
                "It may hold only 3D content, which this viewer cannot show yet."
            )

    # -- package access ------------------------------------------------

    def _read(self, part: str) -> bytes | None:
        real = self._names.get(part.lstrip("/").lower())
        if real is None:
            return None
        try:
            with self._lock:
                with self._zip.open(real) as fh:
                    return fh.read()
        except Exception:
            return None

    def _read_xml(self, part: str):
        raw = self._read(part)
        if raw is None:
            return None
        try:
            return ET.fromstring(raw)
        except ET.ParseError:
            return None

    def _rel_targets(self, part: str, type_filter=None) -> list[str]:
        rels = self._read_xml(_rels_of(part))
        if rels is None:
            return []
        out = []
        for rel in rels:
            if _local(rel.tag) != "Relationship":
                continue
            rtype = (_attr(rel, "Type", "") or "").lower()
            if type_filter and type_filter not in rtype:
                continue
            target = _attr(rel, "Target")
            if target:
                out.append(_resolve(part, target))
        return out

    # -- sheet discovery -----------------------------------------------

    def _discover(self) -> list[dict]:
        pages: list[str] = []
        for seq in self._rel_targets("", _REL_FIXED_REP):
            for doc_part in self._sources(seq, "DocumentReference"):
                for page_part in self._sources(doc_part, "PageContent"):
                    if page_part not in pages:
                        pages.append(page_part)

        if not pages:   # no usable relationships — fall back to a scan
            found = [real for low, real in self._names.items() if low.endswith(".fpage")]
            pages = sorted(found, key=self._natural_key)

        sheets = []
        for i, part in enumerate(pages):
            width, height = self._page_size(part)
            sheets.append({"part": part, "name": f"Sheet {i + 1}",
                           "width": width, "height": height})
        return sheets

    def _sources(self, part: str, tag: str) -> list[str]:
        root = self._read_xml(part)
        if root is None:
            return []
        out = []
        for el in root.iter():
            if _local(el.tag) != tag:
                continue
            src = _attr(el, "Source")
            if src:
                out.append(_resolve(part, src))
        return out

    @staticmethod
    def _natural_key(name: str):
        return [int(t) if t.isdigit() else t.lower()
                for t in re.split(r"(\d+)", name)]

    def _page_size(self, part: str) -> tuple[float, float]:
        raw = self._read(part)
        head = (raw[:16384].decode("utf-8", "ignore") if raw else "")
        w = re.search(r'\bWidth\s*=\s*"([\d.eE+-]+)"', head)
        h = re.search(r'\bHeight\s*=\s*"([\d.eE+-]+)"', head)
        width = _num(w.group(1) if w else None, 11 * _UNITS_PER_INCH)
        height = _num(h.group(1) if h else None, 8.5 * _UNITS_PER_INCH)
        return (width if width > 0 else 1056.0, height if height > 0 else 816.0)

    # -- resources used by the translator ------------------------------

    def image_data_uri(self, part: str) -> str | None:
        if part in self._image_cache:
            return self._image_cache[part]
        result = None
        raw = self._read(part)
        if raw is not None and len(raw) <= _MAX_EMBEDDED_IMAGE:
            ext = "." + part.rsplit(".", 1)[-1].lower() if "." in part else ""
            mime = _IMAGE_MIME.get(ext)
            if mime is None and raw[:8] == b"\x89PNG\r\n\x1a\n":
                mime = "image/png"
            elif mime is None and raw[:2] == b"\xff\xd8":
                mime = "image/jpeg"
            if mime:   # TIFF and friends are skipped: QSvgRenderer can't read them
                result = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        self._image_cache[part] = result
        return result

    def font_family(self, part: str) -> str | None:
        if part in self._font_cache:
            return self._font_cache[part]
        family = None
        raw = self._read(part)
        if raw:
            if part.lower().endswith(".odttf"):
                raw = _deobfuscate_odttf(raw, part)
            family = _font_family(raw)
        self._font_cache[part] = family
        return family

    # -- public API ----------------------------------------------------

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)

    def sheet_names(self) -> list[str]:
        return [s["name"] for s in self.sheets]

    def layers(self, index: int = 0) -> list[str]:
        """Named Canvas groups on a sheet — DWFx's nearest thing to layers."""
        if index not in self._layer_cache:
            self.render_svg(index)
        return list(self._layer_cache.get(index, []))

    def render_svg(self, index: int = 0, width_px: float | None = None,
                   height_px: float | None = None,
                   hidden_layers=()) -> str:
        with self._lock:
            return self._render_svg(index, width_px, height_px, hidden_layers)

    def _render_svg(self, index, width_px, height_px, hidden_layers) -> str:
        if not 0 <= index < len(self.sheets):
            raise DwfxError(f"Sheet {index + 1} does not exist.")
        sheet = self.sheets[index]
        root = self._read_xml(sheet["part"])
        if root is None:
            raise DwfxError("This DWFx sheet is corrupt or unreadable.")

        tr = _PageTranslator(self, sheet["part"], frozenset(hidden_layers))
        tr.walk(root, "FixedPage")
        self._layer_cache[index] = tr.layer_names

        pw, ph = sheet["width"], sheet["height"]
        # Fit into the requested box while keeping the sheet's aspect ratio:
        # canvas.py maps item coordinates onto the viewBox linearly, so any
        # letterboxing would put the measure tool out by that margin.
        if width_px and height_px:
            scale = min(width_px / pw, height_px / ph)
        elif width_px:
            scale = width_px / pw
        elif height_px:
            scale = height_px / ph
        else:
            scale = 1.0
        w, h = pw * scale, ph * scale
        defs = f"<defs>{''.join(tr.defs)}</defs>" if tr.defs else ""
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{_fmt(w)}" height="{_fmt(h)}" '
            f'viewBox="0 0 {_fmt(pw)} {_fmt(ph)}">'
            f'{defs}'
            f'<rect x="0" y="0" width="{_fmt(pw)}" height="{_fmt(ph)}" fill="#ffffff"/>'
            f'{"".join(tr.body)}'
            f'</svg>'
        )

    def close(self) -> None:
        try:
            with self._lock:
                self._zip.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def render_sheet_svg(path, index: int = 0, width_px: float | None = None,
                     height_px: float | None = None) -> str:
    """One-shot render — used by the thumbnail workers."""
    with DwfxDocument(path) as doc:
        return doc.render_svg(index, width_px, height_px)
