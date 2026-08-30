"""
test_converter_dwfx.py — DWGConverter's DWFx path, end to end.

ezdxf is stubbed when it isn't installed so this runs anywhere; the DWFx
path never touches it.
"""
from __future__ import annotations

import sys
import tempfile
import types
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:                       # pragma: no cover - depends on the environment
    import ezdxf           # noqa: F401
except ImportError:        # minimal stand-ins for the imports converter.py makes
    ezdxf = types.ModuleType("ezdxf")
    ezdxf.DXFStructureError = type("DXFStructureError", (Exception,), {})
    ezdxf.document = types.ModuleType("ezdxf.document")
    ezdxf.document.Drawing = object
    recover = types.ModuleType("ezdxf.recover")
    recover.readfile = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
    drawing = types.ModuleType("ezdxf.addons.drawing")
    drawing.RenderContext = drawing.Frontend = object
    svg_mod = types.ModuleType("ezdxf.addons.drawing.svg")
    svg_mod.SVGBackend = object
    layout_mod = types.ModuleType("ezdxf.addons.drawing.layout")
    layout_mod.Page = object
    layout_mod.Units = types.SimpleNamespace(px=0)
    drawing.layout = layout_mod
    for name, mod in {
        "ezdxf": ezdxf, "ezdxf.recover": recover, "ezdxf.document": ezdxf.document,
        "ezdxf.addons": types.ModuleType("ezdxf.addons"),
        "ezdxf.addons.drawing": drawing,
        "ezdxf.addons.drawing.svg": svg_mod,
        "ezdxf.addons.drawing.layout": layout_mod,
    }.items():
        sys.modules.setdefault(name, mod)

from src.converter import DWGConverter, DrawingError   # noqa: E402
from tests.test_dwfx import build_package               # noqa: E402


def test_dwfx_round_trip(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    pkg = build_package(tmp / "set.dwfx")

    conv = DWGConverter(pkg)
    conv.load()
    try:
        assert conv.sheet_count == 2
        assert conv.sheet_names() == ["Sheet 1", "Sheet 2"]

        layers = conv.get_layers()
        assert [l["name"] for l in layers] == ["Walls", "Nested", "Annotation"]
        assert all(l["visible"] for l in layers)

        svg = conv.render_svg(1600, 1200)
        ET.fromstring(svg)
        assert 'stroke="#0000ff"' in svg

        conv.set_layer_visible("Walls", False)
        assert 'stroke="#0000ff"' not in conv.render_svg(1600, 1200)

        # thumbnails ignore layer state, like the DXF path does
        assert 'stroke="#0000ff"' in conv.render_thumbnail_svg(300, 200)

        conv.set_sheet(1)
        assert conv.get_layers() == []
        ET.fromstring(conv.render_svg(1600, 1200))
    finally:
        conv.close()


def test_extension_is_not_trusted(tmp_path=None):
    """A .dwfx that is really classic DWF takes the raster path, not the
    DWFx one — the extension is never trusted over the content."""
    tmp = Path(tmp_path or tempfile.mkdtemp())
    fake = tmp / "old.dwfx"
    fake.write_bytes(b"(DWF V06.00)PK\x03\x04" + b"\x00" * 64)
    conv = DWGConverter(fake)
    conv.load()
    assert conv.is_raster
    assert conv.get_layers() == []
    # ...and this one is a stub with no graphics, so rendering must fail
    # with a clear error rather than a traceback.
    try:
        conv.render_raster(64)
    except DrawingError:
        pass
    else:
        raise AssertionError("expected DrawingError from a bodyless DWF")


def test_unknown_type(tmp_path=None):
    conv = DWGConverter(Path(tmp_path or tempfile.mkdtemp()) / "x.png")
    try:
        conv.load()
    except DrawingError as exc:
        assert "Unsupported file type" in str(exc)
    else:
        raise AssertionError("expected DrawingError")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(tempfile.mkdtemp())
                print(f"  ok   {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print("all passed" if not failures else f"{failures} failing")
    sys.exit(1 if failures else 0)
