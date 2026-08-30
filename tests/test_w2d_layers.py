"""
test_w2d_layers.py — layer handling for classic DWF.

The two sample sheets this decoder was built against were published
without layer information, so there is no real file here that exercises
the layer path. These streams are synthetic for exactly that reason:
they are hand-assembled opcode sequences with `(Layer …)` in them, which
is the only way to pin the behaviour down until a layered DWF turns up.

Runs standalone (`python tests/test_w2d_layers.py`) or under pytest.
Needs numpy and Pillow for the rendering tests, which are skipped when
those are missing; the decoding tests need nothing but the stdlib.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.w2d import GeometryCollector, W2dDecoder, layer_label   # noqa: E402

try:
    import numpy as np
    from PIL import Image, ImageDraw
    from src import w2d_render
    HAVE_RENDER = True
except ImportError:                                   # pragma: no cover
    HAVE_RENDER = False


def skip(why: str):
    """Skip loudly. A test that quietly returns still prints 'ok', which
    is how a whole suite comes to look green while testing nothing."""
    import unittest
    raise unittest.SkipTest(why)


# ── Synthetic opcode streams ─────────────────────────────────────────

def stream(*parts: bytes) -> bytes:
    return b"(W2D V06.00)" + b"".join(parts)


def view(x0: int, y0: int, x1: int, y1: int) -> bytes:
    return f"(View {x0},{y0} {x1},{y1})".encode()


def layer(number: int, name: str | None = None) -> bytes:
    """(Layer 3 'WALLS') declares one; (Layer 3) re-selects it."""
    if name is None:
        return f"(Layer {number})".encode()
    return f"(Layer {number} '{name}')".encode()


def layer_unicode(number: int, name: str) -> bytes:
    """The UTF-16 string form a non-ASCII layer name gets."""
    body = name.encode("utf-16-le")
    return (f"(Layer {number} ".encode() + b"{"
            + len(name).to_bytes(4, "little") + body + b"})")


def layer_binary(number: int) -> bytes:
    """0xAC + a count: how a binary-mode stream re-selects a layer once
    it has been declared. This is the form that matters — the toolkit
    writes it for every switch after the first."""
    if 0 < number < 256:
        return b"\xac" + bytes([number])
    return b"\xac" + b"\x00" + (number - 256).to_bytes(2, "little")


def origin(x: int, y: int) -> bytes:
    return b"O" + struct.pack("<ii", x, y)


def line(dx1: int, dy1: int, dx2: int, dy2: int) -> bytes:
    """Ctrl-L: a two-point line in 16-bit relative coordinates."""
    return b"\x0c" + struct.pack("<4h", dx1, dy1, dx2, dy2)


def decode(data: bytes) -> tuple[GeometryCollector, W2dDecoder]:
    collector = GeometryCollector()
    decoder = W2dDecoder(data)
    decoder.run(collector)
    return collector, decoder


# ── Decoding ─────────────────────────────────────────────────────────

def test_layer_names_are_collected_in_declaration_order():
    data = stream(view(0, 0, 1000, 1000),
                  layer(1, "WALLS"), origin(0, 0), line(0, 0, 100, 0),
                  layer(2, "TEXT"), line(0, 100, 100, 0))
    collector, decoder = decode(data)
    assert collector.layers() == ["WALLS", "TEXT"]
    assert decoder.layer_names == {1: "WALLS", 2: "TEXT"}
    assert collector.has_layers


def test_primitives_are_tagged_with_the_current_layer():
    data = stream(view(0, 0, 1000, 1000),
                  layer(1, "WALLS"), origin(0, 0), line(0, 0, 100, 0),
                  layer(2, "TEXT"), line(0, 100, 100, 0),
                  layer(1), line(0, 100, 50, 0))
    collector, _ = decode(data)
    # The third line re-selects layer 1 by number alone, with no name.
    assert list(collector.seg_layer) == [1, 2, 1]
    assert collector.layers() == ["WALLS", "TEXT"]


def test_geometry_before_the_first_layer_opcode_is_backfilled():
    data = stream(view(0, 0, 1000, 1000),
                  origin(0, 0), line(0, 0, 100, 0), line(0, 100, 100, 0),
                  layer(4, "GRID"), line(0, 100, 100, 0))
    collector, _ = decode(data)
    # Two segments drawn before any layer was named, then one after.
    assert list(collector.seg_layer) == [0, 0, 4]
    assert len(collector.seg_layer) == collector.segment_count


def test_a_sheet_without_layers_pays_nothing():
    data = stream(view(0, 0, 1000, 1000),
                  origin(0, 0), line(0, 0, 100, 0), line(0, 100, 100, 0))
    collector, decoder = decode(data)
    assert collector.layers() == []
    assert not collector.has_layers
    assert decoder.layer_names == {}
    # The parallel arrays are never allocated, so a 15-million-primitive
    # sheet with no layers costs exactly what it did before.
    assert len(collector.seg_layer) == 0
    assert len(collector.pt_layer) == 0
    assert len(collector.arc_layer) == 0


def test_escaped_quotes_in_a_layer_name_survive():
    data = stream(view(0, 0, 1000, 1000),
                  layer(1, "A\\'B"), origin(0, 0), line(0, 0, 10, 0))
    collector, _ = decode(data)
    assert collector.layers() == ["A'B"]


def test_an_unnamed_layer_gets_a_numeric_label():
    data = stream(view(0, 0, 1000, 1000),
                  layer(7), origin(0, 0), line(0, 0, 10, 0))
    collector, _ = decode(data)
    assert collector.layers() == ["Layer 7"]
    assert layer_label(7, "") == "Layer 7"
    assert layer_label(7, "WALLS") == "WALLS"


def test_ascii_tokens_are_tallied():
    """The token census is the diagnostic for a producer that spells an
    opcode differently than the files this decoder was built on."""
    data = stream(view(0, 0, 1000, 1000),
                  layer(1, "WALLS"), origin(0, 0), line(0, 0, 10, 0),
                  layer(1))
    _, decoder = decode(data)
    assert decoder.ascii_tokens["Layer"] == 2
    assert decoder.ascii_tokens["View"] == 1


def test_binary_layer_reselection_is_decoded():
    """0xAC is what a real layered DWF uses for every layer switch after
    the declaration. Before this was handled the decoder raised
    UnsupportedOpcode and lost the whole sheet at the first switch."""
    data = stream(view(0, 0, 1000, 1000),
                  layer(1, "WALLS"), origin(0, 0), line(0, 0, 100, 0),
                  layer(2, "TEXT"), line(0, 100, 100, 0),
                  layer_binary(1), line(0, 100, 50, 0),
                  layer_binary(2), line(0, 100, 50, 0))
    collector, decoder = decode(data)
    assert list(collector.seg_layer) == [1, 2, 1, 2]
    # The names came from the declarations; the binary form carries none.
    assert collector.layers() == ["WALLS", "TEXT"]
    assert decoder.layer == 2


def test_a_binary_layer_number_above_255_uses_the_wide_count():
    data = stream(view(0, 0, 1000, 1000),
                  layer(300, "WIDE"), origin(0, 0), line(0, 0, 100, 0),
                  layer(1, "NARROW"), line(0, 100, 100, 0),
                  layer_binary(300), line(0, 100, 50, 0))
    collector, _ = decode(data)
    assert list(collector.seg_layer) == [300, 1, 300]


def test_a_binary_reselection_of_an_undeclared_layer_gets_a_number():
    data = stream(view(0, 0, 1000, 1000),
                  layer_binary(9), origin(0, 0), line(0, 0, 100, 0))
    collector, _ = decode(data)
    assert collector.layers() == ["Layer 9"]
    assert list(collector.seg_layer) == [9]


def test_a_utf16_layer_name_is_read_and_does_not_derail_the_stream():
    """UTF-16 bytes contain 0x28 and 0x29 — '(' and ')' — so a name in
    that form would close the opcode early if it were stepped over byte
    by byte, and every coordinate after it would be garbage."""
    data = stream(view(0, 0, 1000, 1000),
                  layer_unicode(1, "MUR(S)"), origin(0, 0), line(0, 0, 100, 0),
                  layer(2, "TEXT"), line(0, 100, 100, 0))
    collector, _ = decode(data)
    assert collector.layers() == ["MUR(S)", "TEXT"]
    assert list(collector.seg_layer) == [1, 2]


def test_a_declaration_can_be_reselected_by_either_form():
    data = stream(view(0, 0, 1000, 1000),
                  layer(5, "GRID"), origin(0, 0), line(0, 0, 100, 0),
                  layer(6, "TEXT"), line(0, 100, 100, 0),
                  layer(5), line(0, 100, 10, 0),
                  layer_binary(5), line(0, 100, 10, 0))
    collector, _ = decode(data)
    assert list(collector.seg_layer) == [5, 6, 5, 5]
    assert collector.layers() == ["GRID", "TEXT"]


# ── Rendering ────────────────────────────────────────────────────────

TWO_LAYERS = stream(
    view(0, 0, 1000, 1000),
    layer(1, "WALLS"), origin(100, 100), line(0, 0, 800, 0),
    layer(2, "TEXT"), origin(100, 300), line(0, 0, 800, 0),
)


def _raster_ink(hidden: frozenset[str]) -> int:
    img = Image.new("RGB", (200, 200), (255, 255, 255))
    sink = w2d_render.RasterSink(ImageDraw.Draw(img), 0, 0, 0.2, 200, hidden)
    W2dDecoder(TWO_LAYERS).run(sink)
    return int(np.sum(np.asarray(img).min(axis=2) < 128))


def test_raster_skips_hidden_layers():
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    both = _raster_ink(frozenset())
    one = _raster_ink(frozenset({"TEXT"}))
    none = _raster_ink(frozenset({"TEXT", "WALLS"}))
    assert both > one > 0
    assert none == 0


def _region_ink(hidden: frozenset[str]) -> int:
    collector, _ = decode(TWO_LAYERS)
    geom = w2d_render.SheetGeometry(collector, (0, 0, 1000, 1000), 1 / 1200)
    img = geom.render_region((0, 0, 1000, 1000), 200, 200, hidden=hidden)
    return int(np.sum(np.asarray(img).min(axis=2) < 128))


def test_sharp_zoom_redraw_skips_hidden_layers():
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    both = _region_ink(frozenset())
    one = _region_ink(frozenset({"TEXT"}))
    none = _region_ink(frozenset({"TEXT", "WALLS"}))
    assert both > one > 0
    assert none == 0


def test_hiding_an_unknown_layer_changes_nothing():
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    assert _region_ink(frozenset({"NOT A LAYER"})) == _region_ink(frozenset())


def test_geometry_carries_its_layer_names():
    if not HAVE_RENDER:
        skip("numpy/Pillow not installed")
    collector, _ = decode(TWO_LAYERS)
    geom = w2d_render.SheetGeometry(collector, (0, 0, 1000, 1000), 1 / 1200)
    assert geom.layers == ["WALLS", "TEXT"]
    assert geom.seg_layer.size == geom.seg.shape[0]


# ── Cache keys ───────────────────────────────────────────────────────

def test_raster_variant_is_stable_and_order_independent():
    from src import cache
    assert cache.raster_variant(frozenset()) == ""
    a = cache.raster_variant(frozenset({"WALLS", "TEXT"}))
    b = cache.raster_variant(frozenset({"TEXT", "WALLS"}))
    assert a == b and a.startswith(".")
    assert a != cache.raster_variant(frozenset({"WALLS"}))
    # An unfiltered raster keeps the name earlier versions wrote, so
    # caches built before layers existed stay valid.
    root = Path("x.dwf")
    assert cache.raster_path(root, 16000) == cache.raster_path(root, 16000, "")


# ── The empty layer panel says why ───────────────────────────────────

def _panel():
    """A LayerPanel on Qt's offscreen platform.

    Skips rather than returns None when Qt cannot be imported — on a
    headless box that is usually a missing libEGL rather than a missing
    PyQt6, and either way a silent pass would be a lie.
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt6.QtWidgets import QApplication
        from src.layer_panel import LayerPanel
    except ImportError as exc:                        # pragma: no cover
        skip(f"Qt unavailable: {exc}")
    QApplication.instance() or QApplication([])
    return LayerPanel()


def test_empty_panel_explains_itself():
    panel = _panel()
    panel.populate([], "This DWF was published without layer information.")
    assert panel.note == "This DWF was published without layer information."


def test_a_populated_panel_shows_no_note():
    panel = _panel()
    panel.populate([{"name": "WALLS", "color_hex": "#c8c8c8", "visible": True}],
                   "should not be shown")
    assert panel.note == ""
    assert panel._show_all.isEnabled()


def test_show_and_hide_all_are_dead_when_there_is_nothing_to_toggle():
    panel = _panel()
    panel.populate([], "nothing here")
    assert not panel._show_all.isEnabled()
    assert not panel._hide_all.isEnabled()


def test_a_panel_repopulated_with_layers_drops_the_note():
    """A classic DWF opens with 'still decoding' and gets its layers a
    minute later, so the note has to go away on its own."""
    panel = _panel()
    panel.populate([], "Layers appear once this sheet has finished decoding.")
    assert panel.note
    panel.populate([{"name": "GRID", "color_hex": "#c8c8c8", "visible": True}])
    assert panel.note == ""
    assert "GRID" in panel._rows


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
