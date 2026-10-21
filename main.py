"""
DWG Viewer — entry point.

Usage
-----
    python main.py [folder]

Press F to fit the drawing to the window.
Press M to enter measure mode, P to return to pan mode.
Press R to re-render after toggling layers.
Middle-mouse drag (or Alt + left-drag) pans the view.
Scroll wheel zooms.
"""
import sys
from pathlib import Path

# Make sure `src/` is on the import path when running from the project folder.
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImageReader

from src import theme
from src.main_window import MainWindow


_error_dialog_open = False


def _tell_user(exc_type, exc) -> None:
    """Report an error that escaped a Qt slot, and keep running.

    With sys.excepthook replaced PyQt no longer aborts the process on an
    unhandled exception in a slot; the slot is simply abandoned. Every
    such slot in this app redraws or repopulates one piece of UI, so the
    rest of the window keeps working and re-opening the drawing puts it
    right — which is better than the window vanishing. The user is told
    once (not once per repaint), and pointed at the log.
    """
    global _error_dialog_open
    import threading
    if _error_dialog_open or issubclass(exc_type, KeyboardInterrupt):
        return
    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except Exception:
        return
    app = QApplication.instance()
    if app is None:
        return

    def show():
        global _error_dialog_open
        if _error_dialog_open:
            return
        _error_dialog_open = True
        try:
            QMessageBox.warning(
                app.activeWindow(), "Something went wrong",
                f"{exc_type.__name__}: {exc}\n\n"
                "The viewer is still running. If the drawing looks wrong, "
                "open it again.\n\nDetails were written to crash.log in "
                "%LOCALAPPDATA%\\DWGViewer.")
        finally:
            _error_dialog_open = False

    if threading.current_thread() is threading.main_thread():
        QTimer.singleShot(0, show)
    # From a worker thread the dialog cannot be shown directly, and every
    # worker already reports its own failures through signals, so the log
    # is enough there.


def _install_crash_log() -> None:
    """Leave a traceback behind when the app dies.

    The packaged build has no console, so a native crash or a Python
    exception escaping a Qt slot (which PyQt turns into an abort) just
    makes the window vanish. faulthandler also dumps every thread's stack
    if the process is killed or hangs, which is what a stalled decode
    looks like from outside.
    """
    import faulthandler, os, threading, traceback
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DWGViewer"
        base.mkdir(parents=True, exist_ok=True)
        log = open(base / "crash.log", "a", buffering=1, encoding="utf-8")
    except OSError:
        return
    import datetime
    log.write(f"\n==== started {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ====\n")
    faulthandler.enable(file=log, all_threads=True)

    def _hook(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb, file=log)
        if sys.__stderr__:
            sys.__excepthook__(exc_type, exc, tb)
        _tell_user(exc_type, exc)

    sys.excepthook = _hook
    threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value,
                                           a.exc_traceback)
    main._crash_log = log          # keep the file object alive


def main() -> None:
    _install_crash_log()
    # Qt 6 refuses to decode any image larger than 256 MB by default.
    # A rasterised DWF sheet is 16000 px wide and lands around 375 MB
    # once expanded, so without this the drawing silently fails to load
    # and the canvas just shows nothing. 0 removes the ceiling.
    QImageReader.setAllocationLimit(0)

    app = QApplication(sys.argv)
    app.setApplicationName("DWG Viewer")
    app.setOrganizationName("DWGViewer")
    # Themes, accent and chrome density live in src/theme.py, the appearance
    # system shared with the Redline PDF app. The viewer defaults to Drafting
    # blue rather than that app's Redline red: same system, one colour apart,
    # so the two read as a pair without the viewer looking like a markup tool.
    theme.apply_saved(app, defaults={"accent": "blue"})

    window = MainWindow()
    window.show()

    # If a folder was passed on the command line, open it immediately.
    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
        if folder.is_dir():
            window._browser.open_folder(folder)
        elif folder.is_file() and folder.suffix.lower() in {".dwg", ".dxf",
                                                           ".dwf", ".dwfx"}:
            window._browser.open_folder(folder.parent)
            window._open_file(str(folder))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
