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


def main() -> None:
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
