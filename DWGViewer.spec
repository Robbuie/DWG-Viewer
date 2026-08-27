# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for DWG Viewer.

onedir, not onefile: a onefile build unpacks the whole app to a temp
directory on every launch, which for a PyQt6 + ezdxf app costs several
seconds of startup every single time. The Inno Setup installer bundles
the directory just as happily, and the user never sees it.
"""
import os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# ezdxf ships font metrics and drawing resources it loads at runtime;
# collect_all picks up the data files a bare import analysis misses.
for pkg in ("ezdxf",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

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
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineQuick",
    "PyQt6.QtQuick", "PyQt6.QtQuick3D", "PyQt6.QtQml", "PyQt6.Qt3DCore",
    "PyQt6.Qt3DRender", "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtBluetooth", "PyQt6.QtNfc", "PyQt6.QtPositioning", "PyQt6.QtSql",
    "PyQt6.QtTest", "PyQt6.QtCharts", "PyQt6.QtDataVisualization",
    "matplotlib", "PIL", "numpy.testing", "tkinter", "unittest", "pydoc",
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
