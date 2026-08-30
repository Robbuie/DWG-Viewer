# DWG Viewer

A fast drawing viewer for Windows: folder thumbnails, pan/zoom, layer
toggles, point-to-point measurement, redline markup, and snapshots to
the clipboard.

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
python tests\test_navigator.py
python tests\test_markup.py
```

The first two run on the standard library alone (they stub ezdxf if it is
missing) and build their own synthetic DWFx packages, so no sample
drawings are needed. `test_navigator.py` and `test_markup.py`
drive the navigator, the panel toggles, the redline tools and the
snapshot capture through real Qt events on the offscreen platform, and
skip themselves if PyQt6 is not installed.

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
| `N` | Show / hide the navigator |
| `S` | Snapshot — drag a region to the clipboard |
| `Ctrl`+`Shift`+`C` | Copy the whole visible drawing |
| `Ctrl`+`Shift`+`S` | Save the visible drawing as a PNG |
| `V` | Select markup |
| `C` `B` `E` `A` `D` `T` | Cloud, box, ellipse, arrow, pen, note |
| `H` | Show / hide all markup |
| `Ctrl`+`Z` | Undo the last markup change |
| `Del` | Delete the selected markup |
| `Ctrl`+`1` | Show / hide the file browser |
| `Ctrl`+`2` | Show / hide the layer panel |
| `Ctrl`+`\` | Show / hide both side panels |
| `Ctrl`+`3` | Show / hide the markup toolbar |
| `←` `→` | Previous / next file |

Middle-drag (or Alt+left-drag) pans; the scroll wheel zooms.

## Navigator

The navigator is a miniature of the whole sheet in the bottom-right
corner of the drawing area, with a box marking the part you are looking
at. Drag the box to pan, drag out a new box on the map to zoom straight
to that region (hold `Shift` to draw one inside the current box), click
anywhere to jump there, and roll the wheel over it to zoom. Drag its
title strip to move it out of the way, or `✕` / `N` to put it away
entirely — the choice sticks for the next drawing.

Because it floats over the canvas rather than living in a panel, it is
still there with both side panels hidden.

## Markup

Redlines are the reason Design Review is still installed at places that
stopped using everything else about it, so they work the same way here:
pick a tool, draw on the sheet, and the next person to open the file
sees it.

* **Tools** — revision cloud, box, ellipse, arrow, freehand pen and text
  note, in six colours. `V` selects, so markup can be dragged to a new
  spot or deleted; `Ctrl`+`Z` undoes; `H` hides the lot when you want a
  clean look at the drawing underneath.
* **Where it is kept** — `<drawing>.markup.json` beside the drawing, so
  redlines travel with the file on a shared engineering folder. Saving
  is automatic and coalesced, so a burst of edits is one write.
* **Read-only folders** — if the drawing's folder cannot be written to,
  markup falls back to `%LOCALAPPDATA%\DWGViewer\cache\markup` and the
  status bar says so. Work is never silently lost.
* **Coordinates** — every markup is stored as a fraction of the sheet's
  extents rather than in pixels, so it stays put when the drawing is
  re-rendered at a different size, when a layer is toggled, or when the
  same DWFx is opened on a different monitor.
* **Sheets** — multi-sheet DWFx keeps markup per sheet, keyed by sheet
  name, so republishing a set with a sheet inserted does not shuffle
  redlines onto the wrong drawing.

## Snapshots

`S`, then drag a region: it lands on the clipboard ready to paste into
an email or a report. `Ctrl`+`Shift`+`C` copies the whole visible
drawing and `Ctrl`+`Shift`+`S` writes it to a PNG.

Snapshots are re-rendered from the drawing at roughly twice screen
resolution on a white background, with any markup included — a snapshot
pasted into a report should not look like a photograph of a monitor.

## Thumbnails

Thumbnails are generated once and cached as PNG under
`%LOCALAPPDATA%\DWGViewer\cache`. Delete that folder to force a rebuild.
Loading is driven by the viewport, so opening a folder of a thousand
drawings only does work for the tiles you can see.

## Building an installer

See [RELEASING.md](RELEASING.md).
