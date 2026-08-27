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
