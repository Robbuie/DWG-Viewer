# DWG Viewer

A fast drawing viewer for Windows: folder thumbnails, pan/zoom, layer
toggles, and point-to-point measurement.

## Supported formats

| Format | How it is read | Needs |
|--------|----------------|-------|
| `.dxf` | ezdxf, directly | — |
| `.dwg` | ODA File Converter → DXF → ezdxf | ODA File Converter |
| `.dwfx` | XPS markup translated to SVG in-process | — |
| `.dwf` | only if the file is really a DWFx package | — |

DWFx is an XPS/OPC package, so its sheets are read straight out of the
ZIP with the standard library — no converter, no extra dependency. A
multi-sheet DWFx shows a sheet picker in the toolbar, and named Canvas
groups appear in the layer panel (DWFx has no CAD layer table, so that
is the nearest equivalent).

Classic DWF (version 6 and earlier) stores geometry as binary
WHIP!/W2D streams and is *not* readable yet; those files are detected
and reported clearly rather than failing oddly. Re-publish them as DWFx,
or export DWG/DXF.

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
