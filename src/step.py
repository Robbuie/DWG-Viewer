"""STEP (ISO 10303-21) solid models: read, tessellate, cache.

Everything else this viewer opens is a flat sheet. A STEP file is a 3D
boundary-representation model, so it needs a real geometry kernel to
turn into something drawable: OpenCascade, through the ``OCP`` bindings
(``cadquery-ocp`` on PyPI). This module is the only place that touches
it, and it hands back plain numpy triangle soups so that nothing else
in the app has to know OpenCascade exists.

What comes back is a :class:`StepModel` — a list of :class:`Part`, one
per distinct part name in the assembly. Forty instances of the same
bolt are one part with forty copies of its mesh, so the side panel
lists "M8 bolt  ×40" once rather than forty rows that all say the same.

Tessellating a large assembly can take a while, so the result is cached
as an ``.npz`` under the app cache, keyed like every other cache entry
by path + mtime + size. Opening it again is a file read.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

STEP_SUFFIXES = (".step", ".stp")

# Bump when the mesh layout or tessellation settings change, so stale
# cache entries are ignored rather than misread.
MESH_GENERATION = 1

# OpenCascade's STEP reader keeps global state (unit and schema
# parameters); two threads reading at once can corrupt each other. The
# file browser's thumbnail pool and the main window's loader both read
# STEP, so every read goes through this lock.
_OCC_LOCK = threading.Lock()

_DEFAULT_COLOR = (0.70, 0.72, 0.75)


class StepError(Exception):
    """A STEP file that could not be read; the message is for the user."""


class Cancelled(Exception):
    pass


def is_step(filepath) -> bool:
    return Path(filepath).suffix.lower() in STEP_SUFFIXES


def kernel_available() -> bool:
    try:
        import OCP.STEPCAFControl  # noqa: F401
        return True
    except Exception:
        return False


# ── Data ──────────────────────────────────────────────────────────────

@dataclass
class Part:
    name: str
    color: tuple[float, float, float]
    vertices: np.ndarray          # (3N, 3) float32 — triangle soup
    normals: np.ndarray           # (3N, 3) float32
    colors: np.ndarray | None     # (3N, 3) float32 per-vertex, or None
    edges: np.ndarray             # (2M, 3) float32 — line segments
    instances: int = 1
    visible: bool = True

    @property
    def label(self) -> str:
        """What the side panel shows, and the key it toggles by."""
        return f"{self.name}  ×{self.instances}" if self.instances > 1 \
            else self.name

    @property
    def triangle_count(self) -> int:
        return len(self.vertices) // 3


@dataclass
class StepModel:
    path: Path
    parts: list[Part] = field(default_factory=list)
    unit: str = "mm"

    @property
    def triangle_count(self) -> int:
        return sum(p.triangle_count for p in self.parts)

    def bounds(self, visible_only: bool = False) -> tuple[np.ndarray, np.ndarray]:
        lo = np.array([np.inf] * 3)
        hi = np.array([-np.inf] * 3)
        for p in self.parts:
            if visible_only and not p.visible:
                continue
            for arr in (p.vertices, p.edges):
                if len(arr):
                    lo = np.minimum(lo, arr.min(axis=0))
                    hi = np.maximum(hi, arr.max(axis=0))
        if not np.isfinite(lo).all():
            return np.zeros(3), np.zeros(3)
        return lo, hi

    def part(self, label: str) -> Part | None:
        for p in self.parts:
            if p.label == label:
                return p
        return None

    def layers(self) -> list[dict]:
        """Parts in the shape LayerPanel.populate() takes."""
        return [{"name": p.label, "color_hex": _hex(p.color),
                 "visible": p.visible} for p in self.parts]

    def size_text(self) -> str:
        lo, hi = self.bounds()
        d = hi - lo
        return " × ".join(_fmt(v) for v in d) + f" {self.unit}"


def _fmt(v: float) -> str:
    return f"{v:,.1f}" if v < 1000 else f"{v:,.0f}"


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(round(max(0, min(1, c)) * 255)) for c in rgb)


# ── Loading ───────────────────────────────────────────────────────────

def load(filepath, cancel: threading.Event | None = None,
         use_cache: bool = True) -> StepModel:
    """Read a STEP file, from the mesh cache when it is there."""
    path = Path(filepath)
    if use_cache:
        cached = _read_cache(path)
        if cached is not None:
            return cached
    model = _tessellate(path, cancel)
    if use_cache:
        _write_cache(model)
    return model


_JUNK_NAME = re.compile(r"^(NAUO\d*|SOLID|=>\[.*\]|\s*|Open CASCADE STEP translator.*)$",
                        re.IGNORECASE)


def _tessellate(path: Path, cancel) -> StepModel:
    try:
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TDocStd import TDocStd_Document
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.TDF import TDF_LabelSequence
        from OCP.IFSelect import IFSelect_RetDone
    except Exception as exc:     # pragma: no cover - depends on the install
        raise StepError(
            "STEP support is not installed in this copy of the viewer "
            f"(OpenCascade could not be loaded: {exc}).") from exc

    if not path.is_file():
        raise StepError(f"File not found: {path}")

    with _OCC_LOCK:
        reader = STEPCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)
        reader.SetLayerMode(False)
        try:
            status = reader.ReadFile(str(path))
        except Exception as exc:
            raise StepError(f"Could not read {path.name}: {exc}") from exc
        if status != IFSelect_RetDone:
            raise StepError(
                f"{path.name} is not a readable STEP file (it may be damaged, "
                "or saved by a program using an unsupported STEP schema).")
        _check(cancel)
        doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
        if not reader.Transfer(doc):
            raise StepError(f"{path.name} was read but holds no usable geometry.")
        _check(cancel)

        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
        walker = _Walker(shape_tool, color_tool, cancel)
        roots = TDF_LabelSequence()
        shape_tool.GetFreeShapes(roots)
        for i in range(1, roots.Length() + 1):
            walker.visit(roots.Value(i), np.eye(4), None, None)

    model = StepModel(path=path, parts=walker.parts())
    if not model.parts:
        raise StepError(f"{path.name} contains no solids or surfaces to show.")
    return model


def _check(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


class _Walker:
    """Walks the XCAF assembly tree, meshing each prototype once."""

    def __init__(self, shape_tool, color_tool, cancel):
        self.st, self.ct, self.cancel = shape_tool, color_tool, cancel
        self._proto: dict[str, tuple] = {}     # label entry -> mesh
        self._groups: dict[str, dict] = {}     # display name -> buffers

    # The tree: free shapes are assemblies or simple shapes; an assembly
    # holds components, each a reference (with a placement) to another
    # label, which is again an assembly or a simple shape.
    def visit(self, label, xform, name, color):
        from OCP.XCAFDoc import XCAFDoc_ShapeTool
        from OCP.TDF import TDF_Label, TDF_LabelSequence
        _check(self.cancel)
        own_color = _label_color(self.ct, label) or color
        own_name = _label_name(label)

        if XCAFDoc_ShapeTool.IsReference_s(label):
            ref = TDF_Label()
            XCAFDoc_ShapeTool.GetReferredShape_s(label, ref)
            loc = XCAFDoc_ShapeTool.GetLocation_s(label)
            child_x = xform @ _trsf_matrix(loc.Transformation())
            # The instance's own name is usually a generated "NAUO12";
            # the part it points at has the real one.
            ref_name = _label_name(ref)
            pick = own_name if own_name and not _JUNK_NAME.match(own_name) else None
            self.visit(ref, child_x, pick or ref_name or name, own_color)
            return

        if XCAFDoc_ShapeTool.IsAssembly_s(label):
            comps = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(label, comps, False)
            for i in range(1, comps.Length() + 1):
                self.visit(comps.Value(i), xform, None, own_color)
            return

        shape = XCAFDoc_ShapeTool.GetShape_s(label)
        if shape.IsNull():
            return
        nm = name if name and not _JUNK_NAME.match(name) else None
        nm = nm or (own_name if own_name and not _JUNK_NAME.match(own_name) else None)
        nm = nm or "Unnamed part"
        key = _entry(label)
        mesh = self._proto.get(key)
        if mesh is None:
            mesh = _mesh_shape(shape, self.ct, own_color, self.cancel)
            self._proto[key] = mesh
        self._add(nm, own_color, mesh, xform)

    def _add(self, name, color, mesh, xform):
        verts, norms, cols, edges = mesh
        if not len(verts) and not len(edges):
            return
        g = self._groups.setdefault(name, {
            "color": color or _DEFAULT_COLOR, "v": [], "n": [], "c": [],
            "e": [], "count": 0, "any_col": False})
        rot = xform[:3, :3]
        g["v"].append(_apply(verts, xform))
        # Placements are rigid, so the rotation part turns normals too.
        g["n"].append((norms @ rot.T).astype(np.float32))
        g["c"].append(cols)
        g["any_col"] |= cols is not None
        g["e"].append(_apply(edges, xform))
        g["count"] += 1

    def parts(self) -> list[Part]:
        out = []
        for name, g in self._groups.items():
            color = g["color"]
            cols = None
            if g["any_col"]:
                base = np.array(color, np.float32)
                cols = np.concatenate([
                    c if c is not None else np.tile(base, (len(v), 1))
                    for c, v in zip(g["c"], g["v"])]).astype(np.float32)
            out.append(Part(
                name=name, color=tuple(color),
                vertices=_cat(g["v"]), normals=_cat(g["n"]), colors=cols,
                edges=_cat(g["e"]), instances=g["count"]))
        out.sort(key=lambda p: p.name.lower())
        return out


def _cat(arrs) -> np.ndarray:
    arrs = [a for a in arrs if len(a)]
    return np.concatenate(arrs).astype(np.float32) if arrs \
        else np.zeros((0, 3), np.float32)


def _apply(pts: np.ndarray, m: np.ndarray) -> np.ndarray:
    if not len(pts):
        return pts
    return (pts @ m[:3, :3].T + m[:3, 3]).astype(np.float32)


def _trsf_matrix(trsf) -> np.ndarray:
    m = np.eye(4)
    for r in range(3):
        for c in range(4):
            m[r, c] = trsf.Value(r + 1, c + 1)
    return m


def _entry(label) -> str:
    """The label's stable "0:1:1:3" address. (Not id(): the Python
    wrappers are temporaries, and a recycled id would hand one part
    another part's mesh.)"""
    from OCP.TDF import TDF_Tool
    from OCP.TCollection import TCollection_AsciiString
    text = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, text)
    return text.ToCString()


def _label_name(label) -> str | None:
    from OCP.TDataStd import TDataStd_Name
    attr = TDataStd_Name()
    try:
        if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
            text = attr.Get().ToExtString().strip()
            return text or None
    except Exception:
        pass
    return None


def _to_srgb(qc) -> tuple[float, float, float]:
    from OCP.Quantity import Quantity_Color
    conv = Quantity_Color.Convert_LinearRGB_To_sRGB_s
    return (conv(qc.Red()), conv(qc.Green()), conv(qc.Blue()))


def _label_color(ct, label):
    from OCP.Quantity import Quantity_Color
    from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_ColorTool
    for kind in (XCAFDoc_ColorType.XCAFDoc_ColorSurf,
                 XCAFDoc_ColorType.XCAFDoc_ColorGen):
        qc = Quantity_Color()
        try:
            # The label overload is static in OCP; the instance method
            # only takes shapes.
            if XCAFDoc_ColorTool.GetColor_s(label, kind, qc):
                return _to_srgb(qc)
        except Exception:
            pass
    return None


def _shape_color(ct, shape):
    from OCP.Quantity import Quantity_Color
    from OCP.XCAFDoc import XCAFDoc_ColorType
    for kind in (XCAFDoc_ColorType.XCAFDoc_ColorSurf,
                 XCAFDoc_ColorType.XCAFDoc_ColorGen):
        qc = Quantity_Color()
        try:
            if ct.GetColor(shape, kind, qc):
                return _to_srgb(qc)
        except Exception:
            pass
    return None


def _vertex_normals(pts: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Area-weighted vertex normals for one face's triangulation.

    Computed here rather than asked of OpenCascade node by node: a BRep
    face is one smooth surface, so averaging within it is exactly right,
    and creases only ever fall between faces, which do not share nodes.
    """
    tri = pts[idx]
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    vn = np.zeros_like(pts)
    for k in range(3):
        np.add.at(vn, idx[:, k], fn)
    ln = np.linalg.norm(vn, axis=1, keepdims=True)
    return vn / np.where(ln > 0, ln, 1)


def _mesh_shape(shape, ct, part_color, cancel):
    """Triangles, normals, per-face colours and boundary edges of one
    prototype shape, in its own coordinates."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_REVERSED
    from OCP.TopoDS import TopoDS
    from OCP.TopLoc import TopLoc_Location

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        empty = np.zeros((0, 3), np.float32)
        return empty, empty, None, empty
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = float(np.linalg.norm([x1 - x0, y1 - y0, z1 - z0])) or 1.0
    # Deflection relative to the part: a washer and a frame both come
    # out smooth without the frame turning into a million triangles.
    BRepMesh_IncrementalMesh(shape, max(diag * 0.0015, 1e-4), False, 0.35, True)

    verts, norms, cols, edges = [], [], [], []
    have_face_colors = False
    seen_edges = set()
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        _check(cancel)
        face = TopoDS.Face_s(exp.Current())
        exp.Next()
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None or tri.NbTriangles() == 0:
            continue
        m = _trsf_matrix(loc.Transformation())
        # One wrapper call per node and per triangle is the floor with
        # these bindings; Coord()/Get() return tuples, which is about
        # twice as fast as X(), Y(), Z() one at a time.
        pts = np.array([tri.Node(i).Coord() for i in range(1, tri.NbNodes() + 1)],
                       dtype=np.float64)
        idx = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)],
                       dtype=np.int64) - 1
        reversed_ = face.Orientation() == TopAbs_REVERSED
        if reversed_:
            idx = idx[:, ::-1]
        nrm = _vertex_normals(pts, idx)
        pts = pts @ m[:3, :3].T + m[:3, 3]
        nrm = nrm @ m[:3, :3].T
        flat = idx.reshape(-1)
        verts.append(pts[flat])
        norms.append(nrm[flat])
        fc = _shape_color(ct, face)
        if fc is not None:
            have_face_colors = True
        cols.append((fc, len(flat)))

        # Boundary edges, from the polygons the mesher left on this
        # face's triangulation. Seams (the line down a cylinder where
        # its surface wraps) are not real edges and are left out.
        eexp = TopExp_Explorer(face, TopAbs_EDGE)
        while eexp.More():
            edge = TopoDS.Edge_s(eexp.Current())
            eexp.Next()
            try:
                if BRep_Tool.IsClosed_s(edge, face):
                    continue
                key = hash(edge)
            except Exception:
                key = None
            if key is not None and key in seen_edges:
                continue
            poly = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
            if poly is None:
                continue
            nodes = poly.Nodes()
            ids = [nodes.Value(j) - 1 for j in range(nodes.Lower(), nodes.Upper() + 1)]
            if len(ids) < 2:
                continue
            if key is not None:
                seen_edges.add(key)
            line = pts[ids]
            seg = np.empty((2 * (len(ids) - 1), 3))
            seg[0::2] = line[:-1]
            seg[1::2] = line[1:]
            edges.append(seg)

    V = _cat(verts)
    N = _cat(norms)
    lens = np.linalg.norm(N, axis=1, keepdims=True) if len(N) else None
    if lens is not None:
        N = (N / np.where(lens > 0, lens, 1)).astype(np.float32)
    C = None
    if have_face_colors:
        base = part_color or _DEFAULT_COLOR
        C = np.concatenate([np.tile(np.array(fc or base, np.float32), (n, 1))
                            for fc, n in cols]).astype(np.float32)
    return V, N, C, _cat(edges)


# ── Cache ─────────────────────────────────────────────────────────────

def _cache_path(path: Path) -> Path | None:
    try:
        from src import cache
        d = cache.CACHE_ROOT / "mesh"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{cache.cache_key(path)}.g{MESH_GENERATION}.npz"
    except Exception:
        return None


def has_cached_mesh(filepath) -> bool:
    target = _cache_path(Path(filepath))
    return target is not None and target.is_file()


def _write_cache(model: StepModel) -> None:
    target = _cache_path(model.path)
    if target is None:
        return
    arrays = {}
    meta = []
    for i, p in enumerate(model.parts):
        arrays[f"v{i}"] = p.vertices
        arrays[f"n{i}"] = p.normals.astype(np.float16)   # unit vectors: half is plenty
        arrays[f"e{i}"] = p.edges
        if p.colors is not None:
            arrays[f"c{i}"] = (p.colors * 255).round().astype(np.uint8)
        meta.append([p.name, *p.color, p.instances])
    arrays["meta"] = np.array(meta, dtype=object)
    arrays["unit"] = np.array(model.unit)
    tmp = target.with_suffix(".part.npz")
    try:
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, **arrays)
        tmp.replace(target)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_cache(path: Path) -> StepModel | None:
    target = _cache_path(path)
    if target is None or not target.is_file():
        return None
    try:
        # Pruning goes oldest-first by mtime, so a model in use stays.
        os.utime(target)
    except OSError:
        pass
    try:
        with np.load(target, allow_pickle=True) as z:
            parts = []
            for i, (name, r, g, b, count) in enumerate(z["meta"]):
                cols = z[f"c{i}"].astype(np.float32) / 255 if f"c{i}" in z else None
                parts.append(Part(
                    name=str(name), color=(float(r), float(g), float(b)),
                    vertices=z[f"v{i}"], normals=z[f"n{i}"].astype(np.float32),
                    colors=cols, edges=z[f"e{i}"], instances=int(count)))
            return StepModel(path=path, parts=parts, unit=str(z["unit"]))
    except Exception:
        return None


# ── Thumbnails ────────────────────────────────────────────────────────

def render_thumbnail(model: StepModel, width: int, height: int,
                     background: str = "#ffffff", up: str = "z"):
    """Shaded isometric picture for the file browser (no OpenGL needed)."""
    from src.model_view import render_thumbnail as _render
    return _render(model, width, height, background, up)
