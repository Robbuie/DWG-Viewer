# DWG Viewer

A fast drawing viewer for Windows: folder thumbnails, pan/zoom, layer
toggles, and point-to-point measurement.

## Supported formats

| Format | How it is read | Needs |
|--------|----------------|-------|
| `.dxf` | ezdxf, directly | — |
| `.dwg` | ODA File Converter → DXF → ezdxf | ODA File Converter |
| `.dwfx` | XPS markup translated to SVG in-process | — |
| `.dwf` | WHIP!/W2D opcodes decoded in-process | — |

Nothing here needs a converter except DWG.

**DWFx** is an XPS/OPC package, so its sheets are read straight out of
the ZIP with the standard library. A multi-sheet DWFx shows a sheet
picker in the toolbar, and named Canvas groups appear in the layer panel
(DWFx has no CAD layer table, so that is the nearest equivalent).

**Classic DWF** (version 6 and earlier) stores its geometry as binary
WHIP!/W2D opcode streams. `src/w2d.py` decodes those and
`src/w2d_render.py` draws them. A busy plant sheet holds 8 million
primitives in 300 MB of opcodes, far too many for the SVG path the other
formats use, so a classic DWF is handled differently:

* on first open it is decoded and rasterised at 16000 px wide (~460 dpi
  on a 36x24 sheet), which takes 30-60 seconds and is then cached — every
  later open is instant
* in the background the geometry is retained, after which zooming past
  the raster's resolution redraws the visible region at screen
  resolution instead of magnifying pixels
* the folder grid shows the plot preview AutoCAD embeds when publishing,
  so tiles appear immediately without decoding anything

Layer opcodes are decoded but not yet acted on, so the layer panel is
empty for classic DWF.

## Running from source

```bat
pip install -r requirements.txt
python main.py
```

Or double-click `DWG Viewer.pyw`.

DWG files additionally need the free
[ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter).
DXF and DWFx work without it.

## Tests

```bat
python tests\test_dwfx.py
python tests\test_converter_dwfx.py
```

Both run on the standard library alone (they stub ezdxf if it is
missing) and build their own synthetic DWFx packages, so no sample
drawings are needed.

## Diagnostics

`tools/dwf_inspect.py` dumps a DWF's container: version, sections,
resources, and the head of each graphics stream. `tools/w2d_scan.py`
walks a W2D opcode stream and stops at the first opcode it does not
understand, printing the offset and a hexdump — which is how the decoder
was built. Drop files in `samples/` (gitignored) and run either with no
arguments.

## Keys

| Key | Action |
|-----|--------|
| `F` | Fit drawing to window |
| `M` | Measure mode |
| `P` | Pan mode |
| `R` | Re-render after toggling layers |
| `←` `→` | Previous / next file |

Middle-drag (or Alt+left-drag) pans; the scroll wheel zooms.

## Thumbnails

Thumbnails are generated once and cached as PNG under
`%LOCALAPPDATA%\DWGViewer\cache`. Delete that folder to force a rebuild.
Loading is driven by the viewport, so opening a folder of a thousand
drawings only does work for the tiles you can see.

## Building an installer

See [RELEASING.md](RELEASING.md).
