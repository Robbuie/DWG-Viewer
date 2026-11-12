# DWG Viewer

A fast drawing viewer for Windows: folder thumbnails, pan/zoom, layer
toggles, point-to-point measurement, redline markup, text search,
printing to scale, PDF export of whole drawing sets, and snapshots to
the clipboard — plus a shaded 3D view for STEP models.

## Supported formats

| Format | How it is read | Needs |
|--------|----------------|-------|
| `.dxf` | ezdxf, directly | — |
| `.dwg` | ODA File Converter → DXF → ezdxf | ODA File Converter |
| `.dwfx` | XPS markup translated to SVG in-process | — |
| `.dwf` | WHIP!/W2D opcodes decoded in-process | — |
| `.step` / `.stp` | OpenCascade, tessellated in-process | — (bundled) |

Nothing here needs a converter except DWG.

**DWFx** is an XPS/OPC package, so its sheets are read straight out of
the ZIP with the standard library. Named Canvas groups appear in the
layer panel (DWFx has no CAD layer table, so that is the nearest
equivalent).

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

## STEP models

A STEP file is a 3D solid model rather than a sheet, so it opens in a
shaded 3D view in place of the drawing canvas. Reading one needs a real
geometry kernel; the viewer bundles OpenCascade through the
`cadquery-ocp-novtk` wheel (about 150 MB installed — the plain
`cadquery-ocp` wheel would add another ~300 MB of VTK that is never
used). `src/step.py` is the only module that touches it, and hands
back plain numpy triangle meshes.

* **Moving around** — left-drag rotates (turntable: it never rolls the
  model onto its side), middle-drag or `Shift`/`Alt`+drag pans, the
  wheel zooms about the cursor, double-click or `F` fits.
* **Standard views** — Iso, Front, Back, Top, Bottom, Left and Right on
  the strip above the model (`0` iso, then `1`–`6` for front, back,
  right, left, top, bottom). CAD packages disagree about which axis is up — Creo,
  NX and CATIA are Z-up, SolidWorks, Inventor and Fusion Y-up — so the
  **Z up / Y up** button picks, and the choice is remembered.
* **Parts** — the side panel lists the assembly's parts in place of
  layers, with their colours. Repeated parts are one row (`M8 bolt ×40`),
  and unticking hides them at once — there is nothing to re-render.
  Click a part in the view to highlight it and see its name; `Esc`
  clears it. `E` toggles the edge lines.
* **Snapshots** — `S` or `Ctrl`+`Shift`+`C` copies the view,
  `Ctrl`+`Shift`+`S` saves it as PNG or JPG; the **Snapshot** button on
  the view's strip does both. Snapshots are re-rendered, not grabbed
  off the screen, and leave out the selection highlight. They are
  **cropped to the model** with a small margin — or to the part of it on
  screen, when zoomed in on a detail — and rendered at that framing with
  the long side at least 2000 px (up to 4096), so a model that is small
  in the window still exports crisply. Turn cropping off to get the
  whole window at twice its size. With cropping on, the XYZ indicator
  gets its own corner, widening the picture if the model fills it. By default everything not
  covered by the model is transparent — in a PNG, and on the clipboard
  (a PNG copy goes on alongside the bitmap, which is what Office and
  image editors paste) — and the XYZ indicator is left out. Both are
  switches (with Crop to Model) under **Snapshot** or **View ▸ 3D Model**, and are
  remembered. JPEG cannot hold transparency, so a JPG is flattened onto
  white. Printing, PDF export, measuring,
  markup and text search are sheet tools and are switched off while a
  model is open.
* **Speed** — the first open tessellates the model (roughly 10 s per
  million triangles) and caches the mesh under
  `%LOCALAPPDATA%\DWGViewer\cache\mesh`, capped at 2 GB; every later
  open is a file read. Folder thumbnails come from the same pass.
* **Graphics** — the view uses OpenGL 1.x-level calls, so it runs on
  anything down to Windows' own software GL over Remote Desktop. If no
  OpenGL context can be had at all it falls back to a software renderer
  and says so in the status bar; set `DWGV_SOFTWARE_3D=1` to force that.
  The OpenGL widget is not created until the first STEP file is
  opened, so nothing changes for anyone who only opens drawings.

Units are whatever OpenCascade converts the file to — millimetres — and
the status bar shows the model's overall size.

## Sheets

Both DWF flavours can hold a whole published set rather than one
drawing, so a file with more than one sheet gets a picker in the
toolbar. The sheets come from the container's manifest, which is cheap
to read, so the list is there the moment the file opens and only the
sheet being looked at is ever decoded.

Everything that belongs to a sheet is kept per sheet: its layers, its
searchable text, its redlines, and — for classic DWF — its cached
raster, so going back to a sheet already seen is instant rather than
another minute of decoding. The first sheet of a classic DWF keeps
redlines drawn before the viewer could read sheet names.

Layers come from the stream's layer opcodes — `(Layer 3 'WALLS')` to
declare one and either `(Layer 3)` or the binary `0xAC` to re-select it
— which are decoded and applied. But most classic DWFs do not have any. AutoCAD only writes
them when a sheet is published with layer information included, and both
of the sample sheets this decoder was built against were published
without it: 430 MB of opcodes between them and not one layer. When a
sheet has none the layer panel says so rather than sitting empty.

Hiding a layer on a classic DWF means decoding the whole stream again —
the minute-long job the raster cache exists to avoid — so `R` applies
layer changes rather than every click doing it, and each combination of
hidden layers is cached separately. Going back to a combination already
seen is instant. Deep zoom, which redraws from retained geometry rather
than the raster, filters layers with no re-decode at all.

## Layers

| Format | Where layers come from |
|--------|------------------------|
| `.dxf` / `.dwg` | the CAD layer table, with each layer's colour |
| `.dwfx` | named Canvas groups — XPS has no layer table |
| `.dwf` | layer opcodes, when the sheet was published with them |

An empty layer panel always says why it is empty: a DWFx with no named
groups, a drawing that defines no layers, a classic DWF still decoding,
or one published without layer information.

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
python tests\test_printing.py
python tests\test_w2d_layers.py
python tests\test_converter_dwf.py
python tests\test_model_camera.py
python tests\test_step.py
python tests\test_menus_and_updates.py
```

Or `pytest tests`, which every file also works as. CI runs the suite on
every push to `main`; see `.github/workflows/tests.yml`.

The first two run on the standard library alone (they stub ezdxf if it is
missing) and build their own synthetic DWFx packages, so no sample
drawings are needed. `test_navigator.py`, `test_markup.py` and
`test_printing.py` drive the navigator, the panel toggles, the redline
tools, the snapshot capture, the page-tiling maths and on-sheet search
through real Qt events on the offscreen platform — printing goes to a
PDF, so no printer is needed — and they skip themselves if Qt cannot be
loaded, naming the reason (on a headless Linux box it is usually a
missing `libEGL`, not a missing PyQt6).

`test_w2d_layers.py` builds W2D opcode streams by hand to exercise the
classic-DWF layer path, because no sample drawing here has layers in it.
Its layer-opcode layouts come from the DWF Toolkit source rather than
from a real stream, which is the one place in this decoder that is true
— everything else was recovered from files. That makes these tests the
only thing standing behind the layer path until a layered DWF turns up.
Its decoding tests need only the standard library; the rendering ones
need numpy and Pillow and skip without them.
`test_converter_dwf.py` goes the rest of the way: it assembles a real
DWF 6 container around one of those streams and drives the converter
through decoding, hiding a layer, and a second open that reads the cache
instead of decoding again.

`test_model_camera.py` covers the 3D view's camera maths with numpy
alone. `test_step.py` builds its STEP models with OpenCascade itself
(`tests/step_fixtures.py` — run it directly to write a sample assembly to
try the viewer with) and checks names, instancing, colours (per part and
per face), the mesh cache, the software renderer's depth handling, part
picking and the main window's switch into and out of 3D. It skips if
OpenCascade is not installed. On the offscreen platform it exercises the
software renderer; the OpenGL path was checked under Xvfb with Mesa.

## Diagnostics

`tools/dwf_inspect.py` dumps a DWF's container: version, sections,
resources, and the head of each graphics stream. `tools/w2d_scan.py`
walks a W2D opcode stream and stops at the first opcode it does not
understand, printing the offset and a hexdump — which is how the decoder
was built. Drop files in `samples/` (gitignored) and run either with no
arguments.

## Menus

**File**, **Edit**, **View**, **Tools** and **Help** across the top hold
every command, with its key beside it, so nothing is only reachable by
knowing the shortcut. They are the same commands as the toolbar buttons
(one action each, so one shortcut and one enabled state): sheet-only
commands grey out in both places while a STEP model is open, and
**View ▸ 3D Model** is live only then. **Help ▸ Keyboard Shortcuts**
(`F1`) lists every key, read off the menus themselves. Check for
Updates, Appearance and About moved off the toolbar into Help and View.

## Keys

| Key | Action |
|-----|--------|
| `F` | Fit drawing to window (or the model to the 3D view) |
| `M` | Measure mode |
| `P` | Pan mode |
| `R` | Re-render after toggling layers |
| `N` | Show / hide the navigator |
| `F1` | Keyboard shortcuts |
| `Ctrl`+`Q` | Exit |
| `Ctrl`+`O` | Open a folder of drawings |
| `Ctrl`+`P` | Print — to scale or fitted |
| `Ctrl`+`Shift`+`P` | Save as PDF — this sheet or the whole set |
| `Ctrl`+`F` | Find text on the sheet |
| `F3` | Find the next match |
| `S` | Snapshot — drag a region to the clipboard |
| `Ctrl`+`Shift`+`C` | Copy the whole visible drawing |
| `Ctrl`+`Shift`+`S` | Save the visible drawing (or model) as PNG or JPG |
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
| `0` – `6` | 3D: Iso, Front, Back, Right, Left, Top, Bottom view |
| `E` | 3D: Show / hide edges |
| `Esc` | 3D: Clear the part selection |

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

## Printing

`Ctrl`+`P` asks what to print and at what size, then opens the normal
print preview, where the printer and paper are chosen.

**Scale is real or it is not offered.** DWFx carries a true page size
(XPS units are 1/96 inch) and classic DWF records inches per drawing
unit, so for those "Actual size (1:1)" really is 1:1 and half size
really is half. DXF and DWG open here as model space with no plot
layout, so there is no honest inches-per-unit to quote — those get
fit-to-page, and the dialog says why rather than offering a scale that
would be a guess.

A D-size sheet at 1:1 does not fit on letter paper, so it is **tiled**
across as many pages as it needs (a 34x22 sheet becomes 4 across by 3
down) rather than being silently shrunk. Turning tiling off prints the
middle of the drawing at the requested scale instead. Either way you can
print the whole sheet or only what is on screen, with or without markup.

For a drawing set (a multi-sheet DWF or DWFx) the dialog also offers
**all sheets**. That goes to the printer dialog rather than the preview —
a preview of eighteen sheets would hold all eighteen at printer
resolution in memory — and prints each sheet on its own page, or tiled
when printing at a scale.

### Save as PDF

`Ctrl`+`Shift`+`P`, or the arrow beside **Print**, writes the open sheet
— or, for a set, every sheet — into one PDF, with each sheet's redlines
on its own page. Nothing on screen changes while it runs; sheets are
decoded one at a time in the background, and Cancel stops it without
leaving a half-written file (or replacing an existing one).

- **Page size.** "Same size as each sheet" gives every page the size of
  its sheet, so the PDF prints to scale from any reader. Picking a paper
  instead (Letter, Tabloid, ANSI C/D/E, A4–A0) fits each sheet onto it,
  turned to suit its shape, or scales and tiles it as printing does.
- **Classic DWF sheets are vectors**, drawn from the decoded geometry,
  not a picture of the raster: crisp at any zoom, and no sheet has to be
  rasterised first just to be exported. A busy sheet of a few million
  segments takes a few seconds and adds roughly 10 MB.
- DWG and DXF have one sheet (model space) and no paper size, so they
  go onto the chosen paper, fitted.

## Text search

`Ctrl`+`F` opens a find bar along the bottom; `F3` steps to the next
match. Matches are walked in reading order — top to bottom, then left to
right — and each one is centred and highlighted, keeping your zoom if
the text is already a readable size and reframing if it would be a
speck.

Where the text comes from depends on the format:

* **DWFx** keeps real text in the sheet, so every label is searchable.
* **DXF and DWG** have their text converted to outlines by the renderer,
  so the strings are read from the document instead and placed through
  the same fit-and-centre transform the renderer uses.
* **Classic DWF** draws most of its lettering as stroked line work with
  no string attached, so only genuine text opcodes can be found. Those
  are collected during the geometry pass that already runs for sharp
  zoom, so searching costs nothing extra — but expect a sheet to have
  fewer searchable labels than it has visible words. The find bar says
  how many it has.

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
drawing and `Ctrl`+`Shift`+`S` writes it to a PNG or JPG.

Snapshots are re-rendered from the drawing at roughly twice screen
resolution on a white background, with any markup included — a snapshot
pasted into a report should not look like a photograph of a monitor.

## Recent files

The arrow beside **Open Folder** drops a list of the drawings and folders
opened before, newest first — up to twelve files and ten folders. Picking
a drawing opens its folder in the browser and loads the sheet; the first
nine are numbered, so `Alt`-releasing the menu and pressing `1` reopens the
last drawing.

The list is stored per user alongside the theme settings, and nothing is
checked against disk until you pick it. That is deliberate: probing a dozen
paths every time the menu opens would stall on any share that happens to be
asleep, and would empty the list whenever the VPN was down. An entry that
really has gone says so once and removes itself. **Clear Recent** forgets
both lists.

## Thumbnails

Thumbnails are generated once and cached as PNG under
`%LOCALAPPDATA%\DWGViewer\cache`. Delete that folder to force a rebuild.
Loading is driven by the viewport, so opening a folder of a thousand
drawings only does work for the tiles you can see.

## Building an installer

See [RELEASING.md](RELEASING.md).
