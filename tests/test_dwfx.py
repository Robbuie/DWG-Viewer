"""
test_dwfx.py — exercises the DWFx reader against synthetic packages.

Runs standalone (`python tests/test_dwfx.py`) or under pytest, and needs
nothing but the stdlib, so it works without PyQt6/ezdxf installed.
"""
from __future__ import annotations

import io
import os
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import dwfx   # noqa: E402


# ── Fixture construction ─────────────────────────────────────────────

FONT_PART = "Documents/1/Resources/Fonts/5b3f8a1c-2d4e-4f6a-8b9c-0d1e2f3a4b5c.odttf"

PAGE_1 = """<?xml version="1.0" encoding="utf-8"?>
<FixedPage xmlns="http://schemas.microsoft.com/xps/2005/06"
           xmlns:x="http://schemas.microsoft.com/xps/2005/06/resourcedictionary-key"
           Width="1056" Height="816" xml:lang="en-us">
  <FixedPage.Resources>
    <ResourceDictionary>
      <PathGeometry x:Key="g1" Figures="M 0,0 L 100,0 L 100,50 Z"/>
      <SolidColorBrush x:Key="b1" Color="#FF00FF00"/>
    </ResourceDictionary>
  </FixedPage.Resources>

  <Canvas Name="Walls" RenderTransform="1,0,0,1,10,20">
    <Path Data="M 0,0 L 200,0" Stroke="#FF0000FF" StrokeThickness="2"
          StrokeDashArray="3 1" StrokeStartLineCap="Round"/>
    <Path Data="{StaticResource g1}" Fill="{StaticResource b1}"/>
  </Canvas>

  <Canvas Name="Nested">
    <Canvas.Resources>
      <ResourceDictionary>
        <MatrixTransform x:Key="t1" Matrix="2,0,0,2,5,5"/>
      </ResourceDictionary>
    </Canvas.Resources>
    <Canvas RenderTransform="{StaticResource t1}">
      <Path Data="M 1,1 L 2,2" Stroke="#FF123456"/>
    </Canvas>
  </Canvas>

  <Canvas Name="Annotation">
    <Glyphs OriginX="50" OriginY="60" FontRenderingEmSize="12"
            FontUri="/%s" UnicodeString="Hello" Indices="43,60;72,55"
            Fill="#FF102030"/>
  </Canvas>

  <Path Fill="#80FF0000">
    <Path.Data>
      <PathGeometry FillRule="EvenOdd">
        <PathFigure StartPoint="10,10" IsClosed="true">
          <PolyLineSegment Points="60,10 60,40"/>
          <ArcSegment Point="10,10" Size="25,25" IsLargeArc="false"
                      SweepDirection="Clockwise"/>
        </PathFigure>
      </PathGeometry>
    </Path.Data>
  </Path>

  <Path Data="M 0,0 L 100,0 L 100,100 L 0,100 Z" Clip="M 0,0 L 50,0 L 50,50 Z">
    <Path.Fill>
      <ImageBrush ImageSource="/Documents/1/Resources/Images/pic.png"
                  Viewbox="0,0,2,2" ViewboxUnits="Absolute"
                  Viewport="5,6,100,100" ViewportUnits="Absolute" TileMode="None"/>
    </Path.Fill>
  </Path>
</FixedPage>
""" % FONT_PART

PAGE_2 = """<?xml version="1.0" encoding="utf-8"?>
<FixedPage xmlns="http://schemas.microsoft.com/xps/2005/06"
           Width="612" Height="792">
  <Path Data="M 0,0 L 10,10" Stroke="sc#1,0,0,0"/>
</FixedPage>
"""

RELS = """<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Target="FixedDocSeq.fdseq"
    Type="http://schemas.microsoft.com/xps/2005/06/fixedrepresentation"/>
</Relationships>
"""

FDSEQ = """<?xml version="1.0" encoding="utf-8"?>
<FixedDocumentSequence xmlns="http://schemas.microsoft.com/xps/2005/06">
  <DocumentReference Source="Documents/1/FixedDoc.fdoc"/>
</FixedDocumentSequence>
"""

FDOC = """<?xml version="1.0" encoding="utf-8"?>
<FixedDocument xmlns="http://schemas.microsoft.com/xps/2005/06">
  <PageContent Source="Pages/1.fpage"/>
  <PageContent Source="Pages/2.fpage"/>
</FixedDocument>
"""

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="fpage" ContentType="application/vnd.ms-package.xps-fixedpage+xml"/>
</Types>
"""

# 1x1 transparent PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082")


def _fake_odttf(family: str = "TestFont") -> bytes:
    """A minimal sfnt carrying one name record, then XPS-obfuscated."""
    name_bytes = family.encode("utf-16-be")
    records = struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(name_bytes), 0)
    name_table = struct.pack(">HHH", 0, 1, 6 + 12) + records + name_bytes
    header = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    record = b"name" + struct.pack(">III", 0, 12 + 16, len(name_table))
    font = bytearray(header + record + name_table)

    guid = FONT_PART.rsplit("/", 1)[-1].split(".")[0].replace("-", "")
    key = bytes.fromhex(guid)[::-1]
    for i in range(min(32, len(font))):
        font[i] ^= key[i % 16]
    return bytes(font)


def build_package(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("FixedDocSeq.fdseq", FDSEQ)
        z.writestr("Documents/1/FixedDoc.fdoc", FDOC)
        z.writestr("Documents/1/_rels/FixedDoc.fdoc.rels", RELS)
        z.writestr("Documents/1/Pages/1.fpage", PAGE_1)
        z.writestr("Documents/1/Pages/2.fpage", PAGE_2)
        z.writestr("Documents/1/Resources/Images/pic.png", PNG)
        z.writestr(FONT_PART, _fake_odttf())
    return path


# ── Tests ────────────────────────────────────────────────────────────

def _pkg(tmp: Path) -> Path:
    return build_package(tmp / "sample.dwfx")


def test_sniffing(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    pkg = _pkg(tmp)
    assert dwfx.is_dwfx_package(pkg)
    assert not dwfx.is_classic_dwf(pkg)

    classic = tmp / "old.dwf"
    classic.write_bytes(b"(DWF V06.00)PK\x03\x04" + b"\x00" * 64)
    assert dwfx.is_classic_dwf(classic)
    assert not dwfx.is_dwfx_package(classic)

    plain = tmp / "notes.txt"
    plain.write_bytes(b"hello")
    assert not dwfx.is_dwfx_package(plain)
    assert not dwfx.is_classic_dwf(plain)


def test_sheets_and_sizes(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    with dwfx.DwfxDocument(_pkg(tmp)) as doc:
        assert doc.sheet_count == 2
        assert doc.sheet_names() == ["Sheet 1", "Sheet 2"]
        assert doc.sheets[0]["width"] == 1056 and doc.sheets[0]["height"] == 816
        assert doc.sheets[1]["width"] == 612


def test_render_is_well_formed(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    with dwfx.DwfxDocument(_pkg(tmp)) as doc:
        svg = doc.render_svg(0, 1600, 1200)
    root = ET.fromstring(svg)                      # must parse
    assert root.get("viewBox") == "0 0 1056 816"
    # fitted into 1600x1200 with the sheet aspect kept (no letterboxing,
    # so canvas.py's viewBox mapping stays uniform)
    w, h = float(root.get("width")), float(root.get("height"))
    assert w <= 1600 and h <= 1200
    assert abs(w / h - 1056 / 816) < 1e-6
    assert abs(h - 1200) < 1e-3
    body = svg

    # stroke properties: dash lengths scale by thickness (3,1 * 2)
    assert 'stroke-dasharray="6 2"' in body
    assert 'stroke-linecap="round"' in body
    assert 'stroke="#0000ff"' in body

    # canvas transform survives
    assert 'transform="matrix(1 0 0 1 10 20)"' in body

    # resource dictionary lookups resolved (geometry + brush)
    assert "M 0,0 L 100,0 L 100,50 Z" in body
    assert 'fill="#00ff00"' in body

    # PathFigure/ArcSegment translation
    assert "A 25 25 0 0 1 10 10" in body
    assert 'fill-rule="evenodd"' in body

    # #AARRGGBB alpha split out
    assert 'fill="#ff0000"' in body and 'fill-opacity="0.5' in body

    # text run, with the family mined out of the obfuscated font
    assert ">Hello</text>" in body
    assert 'font-family="TestFont, sans-serif"' in body

    # image brush became a clipped <image> at the viewport; the path's own
    # Clip nests instead of colliding with the image's clip attribute
    assert "data:image/png;base64," in body
    assert '<image x="5" y="6" width="100" height="100"' in body
    assert body.count('clip-path="url(#clip') >= 2

    # a canvas transform pulled from its own resource dictionary
    assert 'transform="matrix(2 0 0 2 5 5)"' in body

    # sheet 2 uses scRGB black
    with dwfx.DwfxDocument(_pkg(tmp)) as doc:
        svg2 = doc.render_svg(1)
    ET.fromstring(svg2)
    assert 'stroke="#000000"' in svg2


def test_layers_and_hiding(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    with dwfx.DwfxDocument(_pkg(tmp)) as doc:
        assert doc.layers(0) == ["Walls", "Nested", "Annotation"]
        shown = doc.render_svg(0)
        hidden = doc.render_svg(0, hidden_layers={"walls"})
    assert 'stroke="#0000ff"' in shown
    assert 'stroke="#0000ff"' not in hidden      # layer content dropped
    assert ">Hello</text>" in hidden             # other layers untouched
    ET.fromstring(hidden)


def test_scan_fallback_without_relationships(tmp_path=None):
    """A package with broken rels still renders via the .fpage scan."""
    tmp = Path(tmp_path or tempfile.mkdtemp())
    path = tmp / "norels.dwfx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("Documents/1/Pages/10.fpage", PAGE_2)
        z.writestr("Documents/1/Pages/2.fpage", PAGE_2)
    with dwfx.DwfxDocument(path) as doc:
        assert doc.sheet_count == 2
        # natural sort: 2 before 10
        assert doc.sheets[0]["part"].endswith("2.fpage")
        ET.fromstring(doc.render_svg(0))


def test_bad_input(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    junk = tmp / "junk.dwfx"
    junk.write_bytes(b"not a zip")
    try:
        dwfx.DwfxDocument(junk)
    except dwfx.DwfxError:
        pass
    else:
        raise AssertionError("expected DwfxError")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            tmp = tempfile.mkdtemp()
            try:
                fn(tmp)
                print(f"  ok   {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print("all passed" if not failures else f"{failures} failing")
    sys.exit(1 if failures else 0)
