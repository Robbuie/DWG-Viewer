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

ONE_LAYER = stream(
    view(0, 0, 1000, 1000),
    layer(1, "GRID"), origin(100, 500), line(0, 0, 800, 0),
)

MANIFEST = (
    '<?xml version="1.0"?><Manifest>'
    '<Section type="com.autodesk.dwf.eplot" name="s1" title="Sheet 1">'
    '<Resources><Resource role="2d streaming graphics" href="s1/graphics.w2d"/>'
    '</Resources></Section></Manifest>'
)


def _section(n: int, title: str) -> str:
    return (f'<Section type="com.autodesk.dwf.eplot" name="s{n}" '
            f'title="{title}">'
            f'<Resources><Resource role="2d streaming graphics" '
            f'href="s{n}/graphics.w2d"/></Resources></Section>')


def build_dwf(path: Path, body: bytes) -> Path:
    """A DWF 6 package: the version header, then an ordinary ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.xml", MANIFEST)
        z.writestr("s1/graphics.w2d", body)
    path.write_bytes(b"(DWF V06.00)" + buf.getvalue())
    return path


def build_set(path: Path, sheets: list[tuple[str, bytes]]) -> Path:
    """A published DWF set: several ePlot sections in one container."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.xml",
                   '<?xml version="1.0"?><Manifest>'
                   + "".join(_section(i + 1, title)
                             for i, (title, _) in enumerate(sheets))
                   + "</Manifest>")
        for i, (_, body) in enumerate(sheets):
            z.writestr(f"s{i + 1}/graphics.w2d", body)
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


# ── multi-sheet sets ─────────────────────────────────────────────────

def test_a_set_lists_its_sheets(tmp_path=None):
    """A classic DWF is as often a whole published set as one drawing,
    and the sheets are known from the manifest without decoding any of
    them."""
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_set(tmp / "set.dwf", [("Plan", TWO_LAYERS),
                                       ("Detail", ONE_LAYER)])

    conv = DWGConverter(path)
    conv.load()
    assert conv.is_raster
    assert conv.sheet_names() == ["Plan", "Detail"]
    assert conv.sheet_count == 2
    assert conv.current_sheet == 0


def test_untitled_and_repeated_sheets_still_get_distinct_names(tmp_path=None):
    """Markup is keyed by sheet name, so two sheets can never share
    one — including two that were published with no title at all."""
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_set(tmp / "dupes.dwf", [("Plan", ONE_LAYER),
                                         ("Plan", ONE_LAYER),
                                         ("", ONE_LAYER)])

    conv = DWGConverter(path)
    conv.load()
    names = conv.sheet_names()
    assert len(set(names)) == 3
    assert names[0] == "Plan" and names[1] == "Plan (2)"


def test_each_sheet_gets_its_own_raster(tmp_path=None):
    """The expensive thing here is decoding, so rasters are cached — and
    a cache keyed by file alone would hand the second sheet the first
    sheet's picture."""
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_set(tmp / "set.dwf", [("Plan", TWO_LAYERS),
                                       ("Detail", ONE_LAYER)])

    conv = DWGConverter(path)
    conv.load()
    first = _ink(conv.render_raster(width_px=400)[0])

    conv.set_sheet(1)
    assert conv.current_sheet == 1
    # Sheet 1's raster is on disk; sheet 2 has never been decoded.
    assert not conv.has_cached_raster(400)
    second = _ink(conv.render_raster(width_px=400)[0])
    assert first > second > 0          # two lines against one
    assert conv.has_cached_raster(400)

    # Both are now cached, each under its own key, and going back is
    # instant rather than a second decode.
    conv.set_sheet(0)
    assert conv.has_cached_raster(400)
    assert _ink(conv.render_raster(width_px=400)[0]) == first


def test_switching_sheets_forgets_the_old_sheets_layers(tmp_path=None):
    """Layer names come out of the opcode stream, so they describe the
    sheet that was decoded and nothing else."""
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_set(tmp / "set.dwf", [("Plan", TWO_LAYERS),
                                       ("Detail", ONE_LAYER)])

    conv = DWGConverter(path)
    conv.load()
    conv.render_raster(width_px=400)
    assert [l["name"] for l in conv.get_layers()] == ["WALLS", "TEXT"]
    conv.set_layer_visible("TEXT", False)

    conv.set_sheet(1)
    # Nothing is claimed about the new sheet before it has been decoded,
    # and the layer hidden on the old one does not follow it across.
    assert conv.get_layers() == []
    assert "finished decoding" in conv.layer_note()
    assert conv.hidden_layers() == frozenset()

    conv.render_raster(width_px=400)
    assert [l["name"] for l in conv.get_layers()] == ["GRID"]


def test_a_cached_sheet_knows_its_own_layers(tmp_path=None):
    """The layer sidecar is keyed by sheet too, or a cached open of one
    sheet would show another's layers."""
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_set(tmp / "set.dwf", [("Plan", TWO_LAYERS),
                                       ("Detail", ONE_LAYER)])

    first = DWGConverter(path)
    first.load()
    first.render_raster(width_px=400)
    first.set_sheet(1)
    first.render_raster(width_px=400)

    second = DWGConverter(path)
    second.load()
    second.set_sheet(1)
    assert second.has_cached_raster(400)
    second.render_raster(width_px=400)
    assert [l["name"] for l in second.get_layers()] == ["GRID"]


def test_a_decode_that_lands_after_a_sheet_change_is_dropped(tmp_path=None):
    """Geometry is decoded in the background and a sheet takes a minute,
    so the user can be looking at another one by the time it finishes.
    What comes back then describes the sheet that has gone."""
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_set(tmp / "set.dwf", [("Plan", TWO_LAYERS),
                                       ("Detail", ONE_LAYER)])

    conv = DWGConverter(path)
    conv.load()

    from src import w2d_render
    real = w2d_render.decode_geometry

    def switch_sheet_mid_decode(p, index=0):
        got = real(p, index)
        conv.set_sheet(1)          # the user moves on while this runs
        return got

    w2d_render.decode_geometry = switch_sheet_mid_decode
    try:
        assert conv.ensure_geometry() is None
    finally:
        w2d_render.decode_geometry = real
    assert not conv.has_geometry
    assert conv.get_layers() == []       # nor the old sheet's layers


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
