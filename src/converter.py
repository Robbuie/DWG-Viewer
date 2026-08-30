"""
converter.py — drawing loading and SVG rendering.

Three input paths, all ending in an SVG string:
  * DXF   — read directly by ezdxf
  * DWG   — converted to DXF by the free ODA File Converter, then ezdxf
  * DWFx  — an XPS/OPC package, translated to SVG by src/dwfx.py with no
            external converter at all

Classic DWF (version 6 and earlier) stores its graphics as binary
WHIP!/W2D streams and is detected but not yet rendered.

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
        self._sheet: int = 0

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
            raise DrawingError(
                "This is a classic DWF file (DWF 6 or earlier).\n\n"
                "Its geometry is stored as binary WHIP!/W2D streams, which this "
                "viewer cannot read yet — only the newer XPS-based DWFx format "
                "is supported.\n\n"
                "Re-publish the sheet as DWFx from AutoCAD (PUBLISH, then choose "
                "DWFx), or export the drawing as DWG/DXF."
            )

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

    @property
    def sheet_count(self) -> int:
        return self._dwfx.sheet_count if self._dwfx is not None else 1

    def sheet_names(self) -> list[str]:
        return self._dwfx.sheet_names() if self._dwfx is not None else []

    def set_sheet(self, index: int) -> None:
        if self._dwfx is None or not 0 <= index < self._dwfx.sheet_count:
            return
        self._sheet = index
        self._layer_vis = {name: True for name in self._dwfx.layers(index)}

    @property
    def doc(self): return self._doc
