"""
converter.py — DWG/DXF loading and SVG rendering.

ezdxf only reads DXF. DWG files are converted to DXF first via the
free ODA File Converter, then rendered with ezdxf.

ODA File Converter (free):  https://www.opendesign.com/guestfiles/oda_file_converter
"""
from __future__ import annotations
import os, shutil, subprocess, tempfile
from pathlib import Path

import ezdxf
from ezdxf import recover as ezdxf_recover
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.svg import SVGBackend
from ezdxf.addons.drawing import layout as drawing_layout

# ── ODA File Converter detection ────────────────────────────────────

def find_oda_converter() -> str | None:
    """Search common locations for ODAFileConverter.exe."""
    # 1. Check PATH first
    found = shutil.which("ODAFileConverter") or shutil.which("ODAFileConverter.exe")
    if found:
        return found

    # 2. Scan all subdirectories under Program Files\ODA (handles any version folder)
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
            # Also check bin/ subfolder
            deeper = os.path.join(entry.path, "bin", "ODAFileConverter.exe")
            if os.path.isfile(deeper):
                return deeper

    return None


_oda_cache: str | None | bool = False

def oda_path() -> str | None:
    global _oda_cache
    if _oda_cache is False:
        _oda_cache = find_oda_converter()
    return _oda_cache  # type: ignore[return-value]

def oda_is_available() -> bool:
    return oda_path() is not None

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
    """Convert a single DWG to a temp DXF file via ODA File Converter."""
    exe = oda_path()
    if not exe:
        raise NeedODAConverter(
            "ODA File Converter is not installed.\n\n"
            "DWG is a proprietary binary format that requires a free converter.\n"
            "Download it from:\n"
            "  https://www.opendesign.com/guestfiles/oda_file_converter\n\n"
            "After installing, restart DWG Viewer."
        )

    tmp_in  = Path(tempfile.mkdtemp(prefix="dwgv_in_"))
    tmp_out = Path(tempfile.mkdtemp(prefix="dwgv_out_"))

    try:
        staged = tmp_in / dwg_path.name
        try:
            os.link(dwg_path, staged)
        except OSError:
            shutil.copy2(dwg_path, staged)

        # Run without file filter — most reliable across ODA FC versions
        cmd = [
            str(exe),
            str(tmp_in),
            str(tmp_out),
            "ACAD2018",   # output DXF version
            "DXF",        # output format
            "0",          # don't recurse
            "1",          # audit
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        hits = list(tmp_out.glob("*.dxf")) + list(tmp_out.glob("*.DXF"))
        if not hits:
            details = []
            if result.stderr.strip():
                details.append(f"stderr: {result.stderr.strip()}")
            if result.returncode != 0:
                details.append(f"return code: {result.returncode}")
            raise DrawingError(
                "ODA File Converter ran but produced no DXF output.\n"
                "This usually means the DWG version is not supported, or the\n"
                "file is password-protected.\n\n"
                + ("\n".join(details) if details else "")
            )

        final = Path(tempfile.mktemp(suffix=".dxf", prefix="dwgv_"))
        shutil.move(str(hits[0]), final)
        return final

    finally:
        shutil.rmtree(tmp_in,  ignore_errors=True)
        shutil.rmtree(tmp_out, ignore_errors=True)


# ── Main converter class ─────────────────────────────────────────────

class DWGConverter:
    def __init__(self, filepath: str | Path):
        self.filepath   = Path(filepath)
        self._doc: ezdxf.document.Drawing | None = None
        self._tmp_dxf: Path | None = None
        self._layer_vis: dict[str, bool] = {}

    def load(self) -> None:
        suffix = self.filepath.suffix.lower()
        if suffix == ".dwg":
            self._load_dwg()
        elif suffix == ".dxf":
            self._load_dxf(self.filepath)
        else:
            raise DrawingError(f"Unsupported file type: {suffix}")
        self._init_layers()

    def _load_dwg(self) -> None:
        dxf_path = _dwg_to_dxf(self.filepath)
        self._tmp_dxf = dxf_path
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
        if self._tmp_dxf and self._tmp_dxf.exists():
            try:   self._tmp_dxf.unlink()
            except OSError: pass
        self._tmp_dxf = None

    def __del__(self): self.close()

    def _init_layers(self) -> None:
        assert self._doc is not None
        self._layer_vis = {l.dxf.name: l.is_on() for l in self._doc.layers}

    def get_layers(self) -> list[dict]:
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
        if not self._doc: raise DrawingError("No document loaded.")
        for layer in self._doc.layers:
            name = layer.dxf.name
            layer.on() if self._layer_vis.get(name, True) else layer.off()
        return self._render(width_px, height_px)

    def render_thumbnail_svg(self, width_px: int = 300, height_px: int = 200) -> str:
        if not self._doc: raise DrawingError("No document loaded.")
        for layer in self._doc.layers: layer.on()
        return self._render(width_px, height_px)

    @property
    def doc(self): return self._doc
