"""
test_converter_dwf.py — DWGConverter's classic-DWF path, end to end.

Builds a real DWF 6 container — the "(DWF V06.00)" header in front of a
ZIP, with a hand-assembled W2D opcode stream inside — and drives the
converter through it: decode, layer discovery, hiding a layer, the
per-combination raster cache, and a second open that reads the cache
without decoding anything.

The raster cache is redirected to a temporary directory, so running this
never touches the user's real cache.

ezdxf is stubbed when it isn't installed (via test_converter_dwfx, which
installs the stand-ins); the classic-DWF path never touches it. Needs
numpy and Pillow, and skips without them.
"""
from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_converter_dwfx import DWGConverter          # noqa: E402
from tests.test_w2d_layers import (                          # noqa: E402
    line, origin, layer, skip, stream, view,
)

try:
    import numpy as np
    from PIL import Image
    HAVE_RENDER = True
except ImportError:                                   # pragma: no cover
    HAVE_RENDER = False

from src import cache, dwfx                                  # noqa: E402


TWO_LAYERS = stream(
    view(0, 0, 1000, 1000),
    layer(1, "WALLS"), origin(100, 100), line(0, 0, 800, 0),
    layer(2, "TEXT"), origin(100, 300), line(0, 0, 800, 0),
)
NO_LAYERS = stream(view(0, 0, 1000, 1000), origin(100, 100), line(0, 0, 800, 0))

MANIFEST = (
    '<?xml version="1.0"?><Manifest>'
    '<Section type="com.autodesk.dwf.eplot" name="s1" title="Sheet 1">'
    '<Resources><Resource role="2d streaming graphics" href="s1/graphics.w2d"/>'
    '</Resources></Section></Manifest>'
)


def build_dwf(path: Path, body: bytes) -> Path:
    """A DWF 6 package: the version header, then an ordinary ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.xml", MANIFEST)
        z.writestr("s1/graphics.w2d", body)
    path.write_bytes(b"(DWF V06.00)" + buf.getvalue())
    return path


def _sandbox(tmp: Path) -> None:
    """Point the raster cache at a temporary directory for this run."""
    cache.RASTER_DIR = tmp / "raster"
    cache.RASTER_DIR.mkdir(parents=True, exist_ok=True)


def _ink(png: bytes) -> int:
    """Dark pixels — how much of the drawing actually got drawn."""
    with Image.open(io.BytesIO(png)) as img:
        return int(np.sum(np.asarray(img.convert("RGB")).min(axis=2) < 128))


def test_layers_are_found_hidden_and_cached(tmp_path=None):
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_dwf(tmp / "sheet.dwf", TWO_LAYERS)
    assert dwfx.is_classic_dwf(path)

    conv = DWGConverter(path)
    conv.load()
    assert conv.is_raster
    # Nothing is known before the decode, and the panel says so rather
    # than claiming the sheet has no layers.
    assert "finished decoding" in conv.layer_note()

    png, box = conv.render_raster(width_px=400)
    assert [l["name"] for l in conv.get_layers()] == ["WALLS", "TEXT"]
    assert conv.layer_note() is None
    assert box[2] > 0
    full = _ink(png)
    assert full > 0

    # Hiding a layer is a different raster, so it is a different cache
    # entry — not yet on disk, and on disk once drawn.
    conv.set_layer_visible("TEXT", False)
    assert conv.hidden_layers() == frozenset({"TEXT"})
    assert not conv.has_cached_raster(400)
    half = _ink(conv.render_raster(width_px=400)[0])
    assert 0 < half < full
    assert conv.has_cached_raster(400)

    conv.set_all_layers_visible(False)
    assert _ink(conv.render_raster(width_px=400)[0]) == 0

    # And back: the unfiltered raster is the entry written on first open.
    conv.set_all_layers_visible(True)
    assert _ink(conv.render_raster(width_px=400)[0]) == full


def test_a_cached_open_still_knows_its_layers(tmp_path=None):
    """The decode is what finds the layer names, and a cached open skips
    it — so the names have to come back off the sidecar instead."""
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_dwf(tmp / "sheet.dwf", TWO_LAYERS)

    first = DWGConverter(path)
    first.load()
    first.render_raster(width_px=400)

    second = DWGConverter(path)
    second.load()
    assert second.has_cached_raster(400)
    second.render_raster(width_px=400)
    assert [l["name"] for l in second.get_layers()] == ["WALLS", "TEXT"]
    assert second.layer_note() is None


def test_a_sheet_without_layers_says_why(tmp_path=None):
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_dwf(tmp / "plain.dwf", NO_LAYERS)

    conv = DWGConverter(path)
    conv.load()
    conv.render_raster(width_px=400)
    assert conv.get_layers() == []
    note = conv.layer_note()
    assert "published without layer information" in note
    # An empty list is not the same as "not decoded yet", and the note is
    # where that difference has to show up.
    assert "finished decoding" not in note


# ── standalone runner ────────────────────────────────────────────────

def _main() -> int:
    import unittest
    failed = skipped = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except unittest.SkipTest as why:
            skipped += 1
            print(f"  skip {name}: {why}")
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {name}")
    tail = f" ({skipped} skipped)" if skipped else ""
    print(("all passed" if not failed else f"{failed} failed") + tail)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
