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
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette, QColor

from src.main_window import MainWindow


def _apply_dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor("#2d2d2d"))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor("#dcdcdc"))
    pal.setColor(QPalette.ColorRole.Base,            QColor("#1e1e1e"))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor("#2a2a2a"))
    pal.setColor(QPalette.ColorRole.ToolTipBase,     QColor("#3a3a3a"))
    pal.setColor(QPalette.ColorRole.ToolTipText,     QColor("#dcdcdc"))
    pal.setColor(QPalette.ColorRole.Text,            QColor("#dcdcdc"))
    pal.setColor(QPalette.ColorRole.Button,          QColor("#3a3a3a"))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor("#dcdcdc"))
    pal.setColor(QPalette.ColorRole.BrightText,      Qt.GlobalColor.red)
    pal.setColor(QPalette.ColorRole.Link,            QColor("#4a9ee8"))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor("#3a6ea8"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    # Disabled state
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,       QColor("#666"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#666"))
    app.setPalette(pal)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("DWG Viewer")
    app.setOrganizationName("DWGViewer")
    _apply_dark_palette(app)

    window = MainWindow()
    window.show()

    # If a folder was passed on the command line, open it immediately.
    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
        if folder.is_dir():
            window._browser.open_folder(folder)
        elif folder.is_file() and folder.suffix.lower() in {".dwg", ".dxf"}:
            window._browser.open_folder(folder.parent)
            window._open_file(str(folder))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
