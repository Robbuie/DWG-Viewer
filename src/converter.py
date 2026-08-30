"""
converter.py — drawing loading and SVG rendering.

Three input paths, all ending in an SVG string:
  * DXF   — read directly by ezdxf
  * DWG   — converted to DXF by the free ODA File Converter, then ezdxf
  * DWFx  — an XPS/OPC package, translated to SVG by src/dwfx.py with no
            external converter at all

Classic DWF (version 6 and earlier) stores its graphics as binary
WHIP!/W2D streams. Those are decoded by src/w2d.py and rasterised by
src/w2d_render.py — a sheet holds millions of primitives, far too many
for the SVG route the other formats take, so it becomes a cached
bitmap instead.

ODA File Converter (free):  https://www.opendesign.com/guestfiles/oda_file_converter
"""
from __future__ import annotations
import os, shutil, subprocess, tempfile, hashlib, json, threading
from pathlib import Path

import ezdxf
from ezdxf import recover as ezdxf_recover
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.svg import SVGBackend
from ezdxf.addons.drawing import layout as drawing_layout

from src import dwfx

# ── Global ODA lock ───────────────────────────────────────────────────
#
# Ensures only one ODA File Converter process runs at a time, whether
# triggered by thumbnail generation or by opening a file.

_oda_lock = threading.Lock()


# ── ODA File Converter detection ─────────────────────────────────────

def find_oda_converter() -> str | None:
    found = shutil.which("ODAFileConverter") or shutil.which("ODAFileConverter.exe")
    if found:
        return found
    for base in [r"C:\Program Files\ODA", r"C:\Program Files (x86)\ODA"]:
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name, reverse=True)
        except PermissionError:
            continue
        for entry in entries:
            candidate = os.path.join(entry.path, "ODAFileConverter.exe")
            if os.path.isfile(candidate):
                return candidate
            deeper = os.path.join(entry.path, "bin", "ODAFileConverter.exe")
            if os.path.isfile(deeper):
                return deeper
    return None


_oda_exe_cache: str | None | bool = False

def oda_path() -> str | None:
    global _oda_exe_cache
    if _oda_exe_cache is False:
        _oda_exe_cache = find_oda_converter()
    return _oda_exe_cache  # type: ignore[return-value]

def oda_is_available() -> bool:
    return oda_path() is not None


# ── Hidden subprocess helper ─────────────────────────────────────────

def hidden_popen_kwargs() -> dict:
    """Extra kwargs to hide the ODA window on Windows."""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0   # SW_HIDE
    return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}


# ── Thumbnail / DXF cache ────────────────────────────────────────────
#
# All cache logic now lives in src/cache.py: a persistent, index-free,
# thread-safe store under %LOCALAPPDATA%. The wrappers below keep the
# names the rest of the app already imports.

from src import cache as _cache

get_cached_dxf   = _cache.get_cached_dxf
store_cached_dxf = _cache.store_cached_dxf


def get_cached_thumbnail_png(filepath: Path) -> bytes | None:
    return _cache.get_cached_png(filepath)


def store_thumbnail_png(filepath: Path, data: bytes) -> None:
    _cache.store_png(filepath, data)


def has_cached_thumbnail(filepath: Path) -> bool:
    return _cache.has_cached_png(filepath)


def is_known_failure(filepath: Path) -> bool:
    return _cache.is_known_failure(filepath)


def mark_failure(filepath: Path) -> None:
    _cache.mark_failure(filepath)


# ── ACI colour map ───────────────────────────────────────────────────

_ACI: dict[int, str] = {
    0:"#000000",1:"#ff0000",2:"#ffff00",3:"#00ff00",
    4:"#00ffff",5:"#0000ff",6:"#ff00ff",7:"#ffffff",
    8:"#808080",9:"#c0c0c0",256:"#ffffff",
}
def aci_to_hex(i: int) -> str:
    return _ACI.get(i, "#ffffff")


# ── Exceptions ───────────────────────────────────────────────────────

# Above this many segments in view, a detail redraw costs seconds and
# improves little — the cached raster is already adequate at that zoom.
MAX_DETAIL_SEGMENTS = 250_000


class DrawingError(Exception):
    pass

class NeedODAConverter(DrawingError):
    DOWNLOAD_URL = "https://www.opendesign.com/guestfiles/oda_file_converter"


# ── DWG → DXF conversion ─────────────────────────────────────────────

def _dwg_to_dxf(dwg_path: Path) -> Path:
    """Convert a single DWG to a temp DXF file via ODA File Converter.
    Acquires _oda_lock so this never runs concurrently with the batch
    thumbnail converter."""
    exe = oda_path()
    if not exe:
        raise NeedODAConverter(
            "ODA File Converter is not installed.\n\n"
            "DWG/DWF is a proprietary binary format that requires a free converter.\n"
            "Download it from:\n"
            "  https://www.opendesign.com/guestfiles/oda_file_converter\n\n"
            "After installing, restart DWG Viewer."
        )

    # Check DXF cache first — thumbnail batch may have already converted this
    cached = get_cached_dxf(dwg_path)
    if cached:
        return cached

    tmp_in  = Path(tempfile.mkdtemp(prefix="dwgv_in_"))
    tmp_out = Path(tempfile.mkdtemp(prefix="dwgv_out_"))

    try:
        staged = tmp_in / dwg_path.name
        try:    os.link(dwg_path, staged)
        except OSError: shutil.copy2(dwg_path, staged)

        # ODA defaults input filter to *.DWG; DWF files need *.DWF explicitly.
        suffix = dwg_path.suffix.upper().lstrip(".")
        input_filter = f"*.{suffix}"
        cmd = [str(exe), str(tmp_in), str(tmp_out), "ACAD2018", "DXF", "0", "1", input_filter]

        with _oda_lock:
            subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           **hidden_popen_kwargs())

        hits = [h for h in tmp_out.glob("*.dxf") if h.suffix.lower() == ".dxf"]
        if not hits:
            err_files = list(tmp_out.glob("*.err"))
            err_detail = ""
            if err_files:
                try:
                    err_detail = "\n\nODA error details:\n" + err_files[0].read_text(errors="replace")[:500]
                except Exception:
                    pass
            raise DrawingError(
                "ODA File Converter could not convert this DWG file.\n"
                "The file may be an unsupported version or password-protected."
                + err_detail
            )

        # Keep the converted DXF so re-opening this drawing — or generating
        # its thumbnail — skips ODA entirely.
        try:
            return store_cached_dxf(dwg_path, hits[0])
        except Exception:
            final = Path(tempfile.mktemp(suffix=".dxf", prefix="dwgv_"))
            shutil.move(str(hits[0]), final)
            return final

    finally:
        shutil.rmtree(tmp_in,  ignore_errors=True)
        shutil.rmtree(tmp_out, ignore_errors=True)


# ── Main converter class ─────────────────────────────────────────────

class DWGConverter:
    def __init__(self, filepath: str | Path):
        self.filepath      = Path(filepath)
        self._doc: ezdxf.document.Drawing | None = None
        self._tmp_dxf: Path | None = None
        self._owns_tmp: bool = False   # False when using cached DXF
        self._layer_vis: dict[str, bool] = {}
        self._dwfx: dwfx.DwfxDocument | None = None
        self._classic: bool = False
        self._geometry = None          # decoded W2D geometry, once asked for
        self._sheet: int = 0
        # Classic DWF names its layers only inside the opcode stream, so
        # they are not known until a decode pass has run. None means "not
        # decoded yet" and an empty list means "this sheet declares none"
        # — the panel says something different for each.
        self._classic_layers: list[str] | None = None
        # Sheet titles from the DWF manifest. A classic DWF is a whole
        # published set as often as it is one drawing, and the sheets
        # are known from the container before anything is decoded.
        self._classic_sheets: list[str] = []

    def load(self) -> None:
        suffix = self.filepath.suffix.lower()
        if suffix in (".dwf", ".dwfx"):
            self._load_dwfx()      # keeps its own layer state
            return
        if suffix == ".dwg":
            self._load_dwg()
        elif suffix == ".dxf":
            self._load_dxf(self.filepath)
        else:
            raise DrawingError(f"Unsupported file type: {suffix}")
        self._init_layers()

    # -- DWFx ---------------------------------------------------------

    def _load_dwfx(self) -> None:
        """Read a DWFx package directly. The extension is not trusted:
        DWFx is occasionally published as .dwf, and .dwfx files that turn
        out to be classic DWF should get the classic-DWF message."""
        if dwfx.is_dwfx_package(self.filepath):
            try:
                self._dwfx = dwfx.DwfxDocument(self.filepath)
            except dwfx.DwfxError as exc:
                raise DrawingError(str(exc)) from exc
            self._sheet = 0
            self._layer_vis = {name: True for name in self._dwfx.layers(0)}
            return

        if dwfx.is_classic_dwf(self.filepath):
            self._classic = True
            self._layer_vis = {}
            self._sheet = 0
            self._classic_sheets = self._read_classic_sheets()
            return

        raise DrawingError(
            "This file is not a readable DWF or DWFx package.\n\n"
            "It may be damaged, or it may be a renamed file of another type."
        )

    def _load_dwg(self) -> None:
        dxf_path = _dwg_to_dxf(self.filepath)
        # _dwg_to_dxf returns cached path (no ownership) or a fresh temp (owned)
        cached = get_cached_dxf(self.filepath)
        if cached and cached == dxf_path:
            self._tmp_dxf  = None   # cached — don't delete on close
            self._owns_tmp = False
        else:
            self._tmp_dxf  = dxf_path
            self._owns_tmp = True
        self._load_dxf(dxf_path)

    def _load_dxf(self, path: Path) -> None:
        try:
            doc, _ = ezdxf_recover.readfile(str(path))
            self._doc = doc
        except ezdxf.DXFStructureError as exc:
            raise DrawingError(f"Corrupt or unreadable file: {exc}") from exc
        except IOError as exc:
            raise DrawingError(f"Cannot open file: {exc}") from exc
        except Exception as exc:
            raise DrawingError(f"Unexpected error reading file: {exc}") from exc

    def close(self) -> None:
        self._geometry = None
        if self._dwfx is not None:
            self._dwfx.close()
            self._dwfx = None
        if self._owns_tmp and self._tmp_dxf and self._tmp_dxf.exists():
            try:   self._tmp_dxf.unlink()
            except OSError: pass
        self._tmp_dxf = None

    def __del__(self): self.close()

    def _init_layers(self) -> None:
        assert self._doc is not None
        self._layer_vis = {l.dxf.name: l.is_on() for l in self._doc.layers}

    def get_layers(self) -> list[dict]:
        if self._classic:
            # Classic DWF carries no colour per layer, only per primitive.
            return [{"name": name, "color_index": 7, "color_hex": "#c8c8c8",
                     "visible": self._layer_vis.get(name, True)}
                    for name in (self._classic_layers or ())]
        if self._dwfx is not None:
            # DWFx has no CAD layer table; named Canvas groups are the
            # nearest equivalent, and they carry no colour of their own.
            return [{"name": name, "color_index": 7, "color_hex": "#c8c8c8",
                     "visible": self._layer_vis.get(name, True)}
                    for name in self._dwfx.layers(self._sheet)]
        if not self._doc: return []
        result = []
        for layer in self._doc.layers:
            name = layer.dxf.name
            ci   = layer.dxf.get("color", 7)
            result.append({"name": name, "color_index": ci,
                           "color_hex": aci_to_hex(abs(ci)),
                           "visible": self._layer_vis.get(name, True)})
        return sorted(result, key=lambda x: x["name"].lower())

    def set_layer_visible(self, name: str, visible: bool) -> None:
        self._layer_vis[name] = visible

    def set_all_layers_visible(self, visible: bool) -> None:
        for n in self._layer_vis: self._layer_vis[n] = visible

    def hidden_layers(self) -> frozenset[str]:
        return frozenset(n for n, on in self._layer_vis.items() if not on)

    def layer_note(self) -> str | None:
        """What to tell the user when the layer panel is empty.

        An empty panel with no explanation reads as a broken viewer. Most
        of the time it means the file simply has no layers in it, and
        that is worth saying plainly — along with what to do about it.
        """
        if self._classic:
            if self._classic_layers is None:
                return ("Layers appear once this sheet has finished "
                        "decoding.")
            if not self._classic_layers:
                return ("This DWF was published without layer information, "
                        "so there are no layers to toggle. Republishing "
                        "from AutoCAD with layer information included "
                        "brings them across.")
            return None
        if self._dwfx is not None and not self._dwfx.layers(self._sheet):
            return ("This DWFx has no named groups, which is the nearest "
                    "thing it has to layers — DWFx keeps no CAD layer "
                    "table.")
        if self._doc is not None and not len(self._doc.layers):
            return "This drawing defines no layers."
        return None

    def _read_classic_sheets(self) -> list[str]:
        """Sheet titles from the DWF manifest.

        Reading the container is cheap — it is the opcode streams that
        are not — so the whole set is listed at open time and only the
        sheet being looked at is ever decoded. A sheet published without
        a title still needs something in the picker, so it gets its
        number.
        """
        from src import dwf as classic
        try:
            with classic.ClassicDwf(self.filepath) as doc:
                titles = [(sh.title or sh.name or "").strip()
                          for sh in doc.sheets]
        except Exception:
            return []
        # Markup and the sheet picker both key off these, so two sheets
        # published under the same title have to be told apart.
        names: list[str] = []
        for i, title in enumerate(titles):
            name = title or f"Sheet {i + 1}"
            if name in names:
                n = 2
                while f"{name} ({n})" in names:
                    n += 1
                name = f"{name} ({n})"
            names.append(name)
        return names

    def _note_classic_layers(self, layers: list[str]) -> None:
        """Record the layer names a decode pass turned up, keeping any
        visibility the user has already set."""
        self._classic_layers = list(layers)
        self._layer_vis = {name: self._layer_vis.get(name, True)
                           for name in layers}

    def _render(self, w: int, h: int) -> str:
        assert self._doc
        msp  = self._doc.modelspace()
        ctx  = RenderContext(self._doc)
        back = SVGBackend()
        Frontend(ctx, back).draw_layout(msp)
        page = drawing_layout.Page(w, h, drawing_layout.Units.px)
        return back.get_string(page)

    def render_svg(self, width_px: int = 1600, height_px: int = 1200) -> str:
        if self._dwfx is not None:
            hidden = {n for n, on in self._layer_vis.items() if not on}
            return self._dwfx.render_svg(self._sheet, width_px, height_px, hidden)
        if not self._doc: raise DrawingError("No document loaded.")
        for layer in self._doc.layers:
            name = layer.dxf.name
            layer.on() if self._layer_vis.get(name, True) else layer.off()
        return self._render(width_px, height_px)

    def render_thumbnail_svg(self, width_px: int = 300, height_px: int = 200) -> str:
        if self._dwfx is not None:
            return self._dwfx.render_svg(self._sheet, width_px, height_px)
        if not self._doc: raise DrawingError("No document loaded.")
        for layer in self._doc.layers: layer.on()
        return self._render(width_px, height_px)

    # -- sheets (DWFx packages hold a whole drawing set) ---------------

    # -- classic DWF: decoded to a cached raster -----------------------

    @property
    def is_raster(self) -> bool:
        return self._classic

    def has_cached_raster(self, width_px: int | None = None) -> bool:
        """True when the current layer combination is already on disk, so
        applying it costs nothing rather than a minute of decoding."""
        if not self._classic:
            return False
        from src import w2d_render
        width = width_px or w2d_render.DEFAULT_WIDTH
        variant = _cache.raster_variant(self.hidden_layers())
        try:
            return _cache.raster_path(self.filepath, width, variant,
                                      self._sheet).is_file()
        except OSError:
            return False

    def render_raster(self, width_px: int | None = None) -> tuple[bytes, tuple]:
        """PNG bytes plus the drawing's extents in sheet inches.

        Decoding is expensive enough — tens of seconds for a busy
        sheet — that the result is cached at full resolution and reused
        on every later open.
        """
        from src import w2d_render

        width = width_px or w2d_render.DEFAULT_WIDTH
        hidden = self.hidden_layers()
        variant = _cache.raster_variant(hidden)
        cached = _cache.get_cached_raster(self.filepath, width, variant,
                                          self._sheet)
        if cached:
            try:
                from PIL import Image
                import io as _io
                with Image.open(_io.BytesIO(cached)) as probe:
                    w, h = probe.size
                # The stored box has to be recovered from the sheet, not
                # from the PNG, so read it back off the stream cheaply.
                box = self._inches_box(w, h)
                if box is not None:
                    known = _cache.get_cached_raster_layers(
                        self.filepath, width, self._sheet)
                    if known is not None:
                        self._note_classic_layers(known)
                    return cached, box
            except Exception:
                pass

        try:
            png, box, layers = w2d_render.render_sheet_png(
                self.filepath, self._sheet, width, hidden)
        except Exception as exc:
            raise DrawingError(f"Could not decode this DWF sheet: {exc}") from exc
        # A pass with layers hidden only reports the layers it drew, so
        # the full list is recorded from the unfiltered pass alone.
        if not hidden:
            self._note_classic_layers(layers)
            try:
                _cache.store_raster_layers(self.filepath, width, layers,
                                           self._sheet)
            except Exception:
                pass
        try:
            _cache.store_raster(self.filepath, width, png, variant, self._sheet)
        except Exception:
            pass
        return png, box

    # -- sharp zoom: redraw a region from retained geometry -------------

    def ensure_geometry(self):
        """Decode the sheet into retained geometry.

        Costs a full pass over the opcode stream, so callers do this once,
        off the UI thread, and keep the result for as long as the drawing
        is open.
        """
        if self._geometry is None and self._classic:
            from src import w2d_render
            # A decode takes long enough that the user can switch sheets
            # while it runs. The sheet it was started for is remembered
            # so a late arrival is dropped rather than handing the new
            # sheet the old one's geometry and layer names.
            sheet = self._sheet
            geometry = w2d_render.decode_geometry(self.filepath, sheet)
            if sheet != self._sheet:
                return self._geometry
            self._geometry = geometry
            # A cached raster skips the decode that would have found the
            # layer names, so this pass is the backstop that fills them in.
            if self._classic_layers is None or (
                    geometry.layers and not self._classic_layers):
                self._note_classic_layers(geometry.layers)
                try:
                    from src import cache as _c
                    _c.store_raster_layers(self.filepath,
                                           w2d_render.DEFAULT_WIDTH,
                                           geometry.layers,
                                           sheet)
                except Exception:
                    pass
        return self._geometry

    @property
    def has_geometry(self) -> bool:
        return self._geometry is not None

    def render_detail_png(self, scene_rect, base_size, out_size) -> bytes:
        """Redraw one rectangle of the sheet at an arbitrary resolution.

        `scene_rect` is in the coordinates of the cached raster, which is
        what the canvas works in; it is mapped back to drawing coordinates
        here so the canvas needs to know nothing about the format.
        """
        geom = self._geometry
        if geom is None:
            return b""
        import io as _io

        sx0, sy0, sx1, sy1 = scene_rect
        base_w, base_h = base_size
        if base_w <= 0 or base_h <= 0:
            return b""

        vx0, vy0, vx1, vy1 = geom.view
        span_x, span_y = vx1 - vx0, vy1 - vy0
        # Raster rows run downward, drawing coordinates run up.
        region = (vx0 + (sx0 / base_w) * span_x,
                  vy0 + (1.0 - sy1 / base_h) * span_y,
                  vx0 + (sx1 / base_w) * span_x,
                  vy0 + (1.0 - sy0 / base_h) * span_y)

        out_w, out_h = int(out_size[0]), int(out_size[1])
        if out_w < 2 or out_h < 2:
            return b""
        img = geom.render_region(region, out_w, out_h,
                                 max_segments=MAX_DETAIL_SEGMENTS,
                                 hidden=self.hidden_layers())
        if img is None:
            return b""      # too much in view to be worth redrawing
        buf = _io.BytesIO()
        img.save(buf, "PNG", compress_level=1)   # speed over size; it is transient
        return buf.getvalue()

    def _inches_box(self, width_px: int, height_px: int):
        """Recover the inches box for a cached raster without decoding
        the whole graphics stream again."""
        try:
            from src import dwf as classic
            from src import w2d_render
            with classic.ClassicDwf(self.filepath) as doc:
                href = doc.sheets[self._sheet].first("2d streaming graphics")
                if not href:
                    return None
                real = doc._names.get(href.replace("\\", "/").lstrip("/").lower())
                if real is None:
                    return None
                with doc._zip.open(real) as fh:
                    head = fh.read(8192)
            view = w2d_render.read_view(head)
            if view is None:
                return None
            x0, y0, x1, y1 = view
            per_unit = w2d_render.read_inches_per_unit(head)
            return (0.0, (y1 - y0) * per_unit, (x1 - x0) * per_unit,
                    -(y1 - y0) * per_unit)
        except Exception:
            return None

    def build_text_index(self, svg: str | None = None):
        """Searchable text for the current sheet.

        The SVG is passed in rather than re-rendered because the loader
        has just produced it; DWFx keeps its strings as real <text>
        there, DXF and DWG do not (the backend strokes them), and classic
        DWF only has text once the geometry pass has run.
        """
        from src import textsearch
        if svg and "<text" in svg:
            return textsearch.from_svg(svg)
        if self._doc is not None:
            return textsearch.from_dxf(self._doc)
        if self._geometry is not None:
            return textsearch.from_classic_geometry(self._geometry)
        return textsearch.TextIndex()

    def paper_size_inches(self) -> tuple[float, float] | None:
        """The sheet's real paper size, when the format records one.

        DWFx is an XPS package, so its FixedPage carries a true page size
        in 1/96 inch; classic DWF stores inches per drawing unit in its
        graphics stream. DXF and DWG are model space with no plot layout
        attached, so there is no honest answer for them — callers get
        None and offer fit-to-page instead of pretending to a scale.
        """
        if self._dwfx is not None:
            try:
                sheet = self._dwfx.sheets[self._sheet]
                w = float(sheet["width"]) / 96.0
                h = float(sheet["height"]) / 96.0
                return (w, h) if w > 0 and h > 0 else None
            except Exception:
                return None
        if self._classic:
            box = self._inches_box(0, 0)
            if box is None:
                return None
            w, h = abs(box[2]), abs(box[3])
            return (w, h) if w > 0 and h > 0 else None
        return None

    @property
    def sheet_count(self) -> int:
        if self._dwfx is not None:
            return self._dwfx.sheet_count
        if self._classic:
            return max(1, len(self._classic_sheets))
        return 1

    @property
    def current_sheet(self) -> int:
        return self._sheet

    def sheet_names(self) -> list[str]:
        if self._dwfx is not None:
            return self._dwfx.sheet_names()
        return list(self._classic_sheets) if self._classic else []

    def set_sheet(self, index: int) -> None:
        if self._dwfx is not None:
            if not 0 <= index < self._dwfx.sheet_count:
                return
            self._sheet = index
            self._layer_vis = {name: True for name in self._dwfx.layers(index)}
            return
        if self._classic:
            if not 0 <= index < len(self._classic_sheets) or index == self._sheet:
                return
            self._sheet = index
            # Everything known about the old sheet came out of its opcode
            # stream — geometry, layer names, which layers were hidden —
            # and none of it describes the new one. Clearing it puts the
            # layer panel back to "still decoding" rather than showing
            # the previous sheet's layers over this one's drawing.
            self._geometry = None
            self._classic_layers = None
            self._layer_vis = {}

    @property
    def doc(self): return self._doc
