"""
startup_diag.py - find out where DWG Viewer stalls before its window appears.

Run from this folder in a Command Prompt:
    python startup_diag.py

It prints each startup stage as it happens, and if the app goes quiet for
15 seconds it dumps every thread's stack to the console AND to
startup_diag.log next to this file. Paste that output back.
"""
import faulthandler, os, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
log = open(HERE / "startup_diag.log", "w", buffering=1, encoding="utf-8")
t0 = time.time()

def stage(msg):
    line = f"[{time.time() - t0:6.2f}s] {msg}"
    print(line, flush=True)
    log.write(line + "\n")

faulthandler.enable(file=log, all_threads=True)
faulthandler.dump_traceback_later(15, repeat=True, file=log)
stage(f"python {sys.version.split()[0]} on {sys.platform}")
for k in ("QT_QPA_PLATFORM", "QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS",
          "QT_OPENGL", "QT_PLUGIN_PATH", "QT_DEBUG_PLUGINS"):
    if os.environ.get(k):
        stage(f"env {k}={os.environ[k]}")

stage("import PyQt6");               from PyQt6.QtCore import QTimer, QSettings
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImageReader, QGuiApplication
stage("import src.theme");           from src import theme
stage("import src.main_window");     from src.main_window import MainWindow

s = QSettings("DWGViewer", "DWGViewer")
stage(f"saved settings: { {k: s.value(k) for k in s.allKeys()} }")

QImageReader.setAllocationLimit(0)
stage("QApplication()");             app = QApplication(sys.argv)
app.setApplicationName("DWG Viewer"); app.setOrganizationName("DWGViewer")
for sc in QGuiApplication.screens():
    stage(f"screen {sc.name()!r} geom={sc.geometry()} dpr={sc.devicePixelRatio()}")
stage("theme.apply_saved");          theme.apply_saved(app, defaults={"accent": "blue"})
stage("MainWindow()");               w = MainWindow()
stage("window.show()");              w.show()

def report():
    stage(f"event loop alive: visible={w.isVisible()} geom={w.geometry()} "
          f"state={w.windowState()} screen={w.screen().name() if w.screen() else None}")
QTimer.singleShot(1000, report)
QTimer.singleShot(6000, lambda: (report(), faulthandler.cancel_dump_traceback_later(),
                                  stage("startup looks healthy; watchdog off")))
stage("app.exec()")
rc = app.exec()
stage(f"exited rc={rc}")
