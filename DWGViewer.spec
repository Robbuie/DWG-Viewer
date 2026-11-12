# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for DWG Viewer.

onedir, not onefile: a onefile build unpacks the whole app to a temp
directory on every launch, which for a PyQt6 + ezdxf app costs several
seconds of startup every single time. The Inno Setup installer bundles
the directory just as happily, and the user never sees it.
"""
import os
from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                     collect_submodules)

datas, binaries, hiddenimports = [], [], []

# ezdxf ships font metrics and drawing resources it loads at runtime.
datas    += collect_data_files("ezdxf")
binaries += collect_dynamic_libs("ezdxf")

# Sweep ezdxf's submodules, but skip its optional rendering backends.
# collect_all() would drag in ezdxf.addons.drawing.qtviewer, which imports
# PySide6 and makes PyInstaller abort with "multiple Qt bindings packages".
_BACKEND_MARKERS = ("qtviewer", "pyqt", "pyside", "matplotlib", "mupdf",
                    "pymupdf", "dxf2code", "browser")
hiddenimports += [m for m in collect_submodules("ezdxf")
                  if not any(x in m.lower() for x in _BACKEND_MARKERS)]

hiddenimports += [
    "ezdxf.addons.drawing.svg",
    "ezdxf.addons.drawing.layout",
    "ezdxf.addons.drawing.frontend",
    "ezdxf.addons.drawing.recorder",
    "PyQt6.QtSvg",
]

# STEP models: the OpenCascade kernel, from cadquery-ocp-novtk. The
# whole of OpenCascade is one extension module (OCP.OCP) that registers
# every OCP.* submodule itself, so only the package and that module need
# naming. Its DLLs were moved into cadquery_ocp_novtk.libs by delvewheel,
# and OCP/__init__.py adds that folder — beside the package, one level
# up — to the DLL search path at import, so they go to the same place
# here. The vtk-linked "cadquery-ocp" wheel must NOT be used: it needs
# ~300 MB of VTK this app never calls.
import glob, importlib.util
_ocp = importlib.util.find_spec("OCP")
if _ocp is None or not _ocp.origin:
    raise SystemExit("cadquery-ocp-novtk is not installed — STEP support "
                     "would be missing from this build. "
                     "pip install -r requirements.txt -c constraints-build.txt")
_ocp_dir = os.path.dirname(_ocp.origin)
_ocp_libs = os.path.join(os.path.dirname(_ocp_dir), "cadquery_ocp_novtk.libs")
binaries += collect_dynamic_libs("OCP")
if os.path.isdir(_ocp_libs):
    binaries += [(f, "cadquery_ocp_novtk.libs")
                 for f in glob.glob(os.path.join(_ocp_libs, "*")) if os.path.isfile(f)]
hiddenimports += ["OCP", "OCP.OCP"]

# The 3D view: QOpenGLWidget plus PyOpenGL for the GL calls. PyOpenGL
# picks its platform module and array handlers at runtime, so they are
# named explicitly in case the contrib hook misses one.
hiddenimports += [
    "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets",
    "OpenGL.platform.win32", "OpenGL.arrays.numpymodule",
    "OpenGL.arrays.ctypesarrays", "OpenGL.arrays.ctypespointers",
    "OpenGL.arrays.numbers", "OpenGL.arrays.strings", "OpenGL.arrays.lists",
    "OpenGL.arrays.nones", "OpenGL.arrays.buffers",
]

# Qt modules this app never touches. Dropping them takes roughly
# 150-250 MB off the installed size.
excludes = [
    # Other Qt bindings. PyInstaller refuses to build if it sees two, and
    # ezdxf's optional extras are how a second one sneaks in.
    "PySide6", "PySide2", "PyQt5", "shiboken6", "shiboken2",
    # Qt modules this app never touches — worth roughly 150-250 MB.
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineQuick",
    "PyQt6.QtQuick", "PyQt6.QtQuick3D", "PyQt6.QtQml", "PyQt6.Qt3DCore",
    "PyQt6.Qt3DRender", "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtBluetooth", "PyQt6.QtNfc", "PyQt6.QtPositioning", "PyQt6.QtSql",
    "PyQt6.QtTest", "PyQt6.QtCharts", "PyQt6.QtDataVisualization",
    # VTK, in case the vtk-linked OCP wheel is what got installed.
    "vtk", "vtkmodules",
    # Optional ezdxf backends.
    "matplotlib", "fitz", "pymupdf", "tkinter",
    # NOTE: do NOT exclude stdlib modules like unittest / pydoc, however
    # unused they look. pyparsing (an ezdxf dependency) imports
    # pyparsing.testing at package import time, which imports unittest —
    # excluding it builds fine and then dies on the very first launch.
    # NOTE: do NOT exclude PIL — ezdxf.addons.drawing.frontend imports
    # PIL.Image at module level, so excluding it builds cleanly and then
    # crashes the moment a drawing is rendered.
]

icon_path = os.path.join("installer", "app.ico")
icon = icon_path if os.path.exists(icon_path) else None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DWG Viewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI app: no console window
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DWG Viewer",
)
