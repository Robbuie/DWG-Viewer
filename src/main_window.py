"""
main_window.py — application shell: toolbar, split layout, wiring.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QStatusBar, QLabel,
    QToolBar, QWidget, QProgressBar, QMessageBox,
    QFileDialog, QSizePolicy, QComboBox,
)

from src.file_browser import FileBrowser
from src.canvas import DrawingCanvas
from src.layer_panel import LayerPanel
from src.converter import DWGConverter, DrawingError
from src.version import __version__, APP_NAME
from src.update_ui import UpdateChecker


# ------------------------------------------------------------------ #
#  Background loader thread
# ------------------------------------------------------------------ #

class _GeometryWorker(QThread):
    """Decodes a classic DWF sheet into retained geometry in the
    background, so sharp zooming becomes available a little after the
    drawing itself appears rather than holding it up."""

    ready = pyqtSignal(object)

    def __init__(self, conv):
        super().__init__()
        self._conv = conv

    def run(self):
        try:
            self._conv.ensure_geometry()
        except Exception:
            return
        self.ready.emit(self._conv)


class _LoadSignals(QObject):
    # converter, layers, svg, png bytes, extents — a drawing arrives as
    # one or the other: vector formats as SVG, classic DWF as a raster.
    finished = pyqtSignal(object, list, str, object, object)
    error = pyqtSignal(str)


class _LoadWorker(QThread):
    def __init__(self, filepath: str):
        super().__init__()
        self.filepath = filepath
        self.signals = _LoadSignals()
        self._conv: DWGConverter | None = None

    def run(self):
        try:
            conv = DWGConverter(self.filepath)
            conv.load()
            layers = conv.get_layers()
            if conv.is_raster:
                png, extents = conv.render_raster()
                self.signals.finished.emit(conv, layers, "", png, extents)
            else:
                svg = conv.render_svg()
                self.signals.finished.emit(conv, layers, svg, None, None)
        except DrawingError as exc:
            self.signals.error.emit(str(exc))
        except Exception as exc:
            self.signals.error.emit(f"Unexpected error: {exc}")


# ------------------------------------------------------------------ #
#  Main window
# ------------------------------------------------------------------ #

class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1400, 850)

        self._current_conv: DWGConverter | None = None
        self._load_worker: _LoadWorker | None = None
        self._geometry_worker: _GeometryWorker | None = None

        self._updater = UpdateChecker(self)

        self._build_ui()
        self._build_toolbar()
        self._build_statusbar()

        # Quiet daily check: says nothing unless there is something to say.
        QTimer.singleShot(4000, self._updater.check_silently)
        # Pan is checked by default — sync canvas state
        self._canvas.set_pan_mode(True)

    # ------------------------------------------------------------------ #
    #  UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        # Outer splitter: file browser | canvas | layers
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(4)
        splitter.setStyleSheet(
            "QSplitter::handle { background: #3a3a3a; }"
            "QSplitter::handle:hover { background: #555; }"
        )

        # Left: file browser
        self._browser = FileBrowser()
        self._browser.fileSelected.connect(self._open_file)
        splitter.addWidget(self._browser)

        # Centre: drawing canvas
        self._canvas = DrawingCanvas()
        self._canvas.coordChanged.connect(self._on_coord_changed)
        self._canvas.measurementDone.connect(self._on_measurement_done)
        self._canvas.pageNavigateRequested.connect(self._navigate_file)
        splitter.addWidget(self._canvas)

        # Right: layer panel
        self._layers = LayerPanel()
        self._layers.layerToggled.connect(self._on_layer_toggled)
        self._layers.allToggled.connect(self._on_all_layers_toggled)
        splitter.addWidget(self._layers)

        # Proportions: browser 18%, canvas 64%, layers 18%
        splitter.setSizes([252, 896, 252])
        self.setCentralWidget(splitter)

    def _build_toolbar(self):
        tb = QToolBar("Main Toolbar")
        tb.setMovable(False)
        tb.setStyleSheet(
            "QToolBar { background: #2d2d2d; border-bottom: 1px solid #444; spacing: 4px; }"
            "QToolButton { color: #ccc; padding: 4px 8px; border-radius: 3px; }"
            "QToolButton:hover { background: #3a3a3a; }"
            "QToolButton:checked { background: #3a6ea8; color: #fff; }"
        )
        self.addToolBar(tb)

        # Open Folder
        act_open = QAction("📂  Open Folder", self)
        act_open.setToolTip("Open a folder of DWG files (Ctrl+O)")
        act_open.setShortcut(QKeySequence("Ctrl+O"))
        act_open.triggered.connect(self._browse_folder)
        tb.addAction(act_open)

        tb.addSeparator()

        # Fit to View
        act_fit = QAction("⛶  Fit View", self)
        act_fit.setToolTip("Fit drawing to window (F)")
        act_fit.setShortcut(QKeySequence("F"))
        act_fit.triggered.connect(self._canvas.fit_to_view)
        tb.addAction(act_fit)

        tb.addSeparator()

        # Prev / Next file navigation
        act_prev = QAction("◀  Prev", self)
        act_prev.setToolTip("Previous file (Left arrow)")
        act_prev.triggered.connect(lambda: self._navigate_file(-1))
        tb.addAction(act_prev)

        act_next = QAction("▶  Next", self)
        act_next.setToolTip("Next file (Right arrow)")
        act_next.triggered.connect(lambda: self._navigate_file(1))
        tb.addAction(act_next)

        tb.addSeparator()

        # Pan / Measure toggle
        self._act_pan = QAction("✋  Pan", self)
        self._act_pan.setToolTip("Pan mode — left-click drag to pan (P)")
        self._act_pan.setShortcut(QKeySequence("P"))
        self._act_pan.setCheckable(True)
        self._act_pan.setChecked(True)
        self._act_pan.triggered.connect(self._set_pan_mode)
        tb.addAction(self._act_pan)

        self._act_measure = QAction("📏  Measure", self)
        self._act_measure.setToolTip("Measure distance between two clicks (M)")
        self._act_measure.setShortcut(QKeySequence("M"))
        self._act_measure.setCheckable(True)
        self._act_measure.triggered.connect(self._set_measure_mode)
        tb.addAction(self._act_measure)

        tb.addSeparator()

        # Sheet picker — hidden unless the file holds a drawing set,
        # which today means a multi-sheet DWFx package.
        self._sheet_box = QComboBox()
        self._sheet_box.setToolTip("Sheet within this DWFx package")
        self._sheet_box.setMinimumWidth(140)
        self._sheet_box.setStyleSheet(
            "QComboBox { background: #3a3a3a; color: #ccc; border: 1px solid #555;"
            " border-radius: 3px; padding: 2px 6px; }"
            "QComboBox QAbstractItemView { background: #2d2d2d; color: #ccc;"
            " selection-background-color: #3a6ea8; }"
        )
        self._sheet_box.currentIndexChanged.connect(self._on_sheet_changed)
        self._sheet_action = tb.addWidget(self._sheet_box)
        self._sheet_action.setVisible(False)

        tb.addSeparator()

        # Re-render (apply layer changes)
        act_render = QAction("🔄  Apply Layers", self)
        act_render.setToolTip("Re-render with current layer visibility (R)")
        act_render.setShortcut(QKeySequence("R"))
        act_render.triggered.connect(self._rerender)
        tb.addAction(act_render)

        # Push the update controls to the right-hand end of the toolbar.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        act_update = QAction("⭳  Check for Updates", self)
        act_update.setToolTip("Check GitHub for a newer version")
        act_update.triggered.connect(self._check_updates)
        tb.addAction(act_update)

        act_about = QAction("About", self)
        act_about.triggered.connect(self._about)
        tb.addAction(act_about)

    # ------------------------------------------------------------------ #
    #  Updates
    # ------------------------------------------------------------------ #

    def _check_updates(self):
        self._updater.check_interactively()

    def _about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {__version__}<br><br>"
            "DWG, DXF and DWFx drawing viewer.<br>"
            "DWG/DXF are rendered with ezdxf and DWG conversion uses the "
            "free ODA File Converter; DWFx sheets are read directly from "
            "their XPS package.")

    def _build_statusbar(self):
        sb = QStatusBar()
        sb.setStyleSheet(
            "QStatusBar { background: #2d2d2d; color: #888; font-size: 11px; "
            "border-top: 1px solid #444; }"
        )
        self.setStatusBar(sb)

        self._coord_label = QLabel("X: —    Y: —")
        self._coord_label.setMinimumWidth(180)
        sb.addWidget(self._coord_label)

        sb.addWidget(_vline())

        self._measure_label = QLabel("Measure: click two points")
        sb.addWidget(self._measure_label)

        sb.addWidget(_vline())

        self._file_label = QLabel("No file open")
        sb.addWidget(self._file_label, 1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)   # indeterminate
        self._progress.setFixedWidth(120)
        self._progress.setVisible(False)
        self._progress.setStyleSheet(
            "QProgressBar { background: #3a3a3a; border: 1px solid #555; "
            "border-radius: 3px; height: 14px; }"
            "QProgressBar::chunk { background: #3a6ea8; border-radius: 2px; }"
        )
        sb.addPermanentWidget(self._progress)

    # ------------------------------------------------------------------ #
    #  Toolbar actions
    # ------------------------------------------------------------------ #

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select DWG folder")
        if folder:
            self._browser.open_folder(folder)

    def _set_pan_mode(self):
        self._act_pan.setChecked(True)
        self._act_measure.setChecked(False)
        self._canvas.set_pan_mode(True)
        self._canvas.set_measure_mode(False)

    def _set_measure_mode(self):
        self._act_measure.setChecked(True)
        self._act_pan.setChecked(False)
        self._canvas.set_pan_mode(False)
        self._canvas.set_measure_mode(True)

    def _navigate_file(self, delta: int):
        self._browser.select_next(delta)

    def _rerender(self):
        if self._current_conv is None:
            return
        self._start_render()

    # ------------------------------------------------------------------ #
    #  File loading
    # ------------------------------------------------------------------ #

    def _open_file(self, filepath: str):
        if self._load_worker and self._load_worker.isRunning():
            self._load_worker.terminate()

        self._current_conv = None
        self._canvas.clear()
        self._layers.clear()
        self._file_label.setText(self._loading_message(filepath))
        self._progress.setVisible(True)

        worker = _LoadWorker(filepath)
        worker.signals.finished.connect(self._on_load_finished)
        worker.signals.error.connect(self._on_load_error)
        worker.finished.connect(lambda: self._progress.setVisible(False))
        self._load_worker = worker
        worker.start()

    @staticmethod
    def _loading_message(filepath: str) -> str:
        """Classic DWF is decoded from scratch the first time and can take
        a minute, so say so rather than looking hung."""
        name = Path(filepath).name
        try:
            from src import dwfx, cache, w2d_render
            if dwfx.is_classic_dwf(filepath):
                if cache.get_cached_raster(Path(filepath),
                                           w2d_render.DEFAULT_WIDTH) is None:
                    return (f"Decoding {name} — first open only, this can take "
                            f"a minute. Later opens are instant.")
        except Exception:
            pass
        return f"Loading: {name} …"

    def _on_load_finished(self, conv: DWGConverter, layers: list, svg: str,
                          png=None, extents=None):
        self._current_conv = conv
        self._populate_sheets(conv)
        self._layers.populate(layers)
        self._canvas.set_detail_provider(None)
        if png:
            if not self._canvas.load_image(png, extents):
                self._progress.setVisible(False)
                self._on_load_error(
                    "The decoded drawing could not be displayed.\n\n"
                    "This is usually Qt refusing a very large image. Try "
                    "restarting the viewer; if it persists, the sheet may "
                    "need to be rendered at a lower resolution."
                )
                return
        else:
            self._canvas.load_svg(svg)
        name = conv.filepath.name
        detail = "rasterised" if png else f"{len(layers)} layers"
        self._file_label.setText(f"{name}  ({detail})")
        self._progress.setVisible(False)

        if png and conv.is_raster:
            self._start_geometry(conv)

    def _start_geometry(self, conv: DWGConverter) -> None:
        """Decode geometry behind the drawing so deep zoom can be redrawn
        sharply instead of magnifying the raster."""
        if self._geometry_worker is not None and self._geometry_worker.isRunning():
            self._geometry_worker.terminate()
        worker = _GeometryWorker(conv)
        worker.ready.connect(self._on_geometry_ready)
        worker.finished.connect(worker.deleteLater)
        self._geometry_worker = worker
        worker.start()

    def _on_geometry_ready(self, conv: DWGConverter) -> None:
        if conv is not self._current_conv:
            return          # a different drawing was opened meanwhile
        self._canvas.set_detail_provider(conv.render_detail_png)
        self.statusBar().showMessage(
            "Sharp zoom ready — the drawing is now redrawn at full detail "
            "as you zoom in.", 5000)

    def _populate_sheets(self, conv: DWGConverter | None):
        names = conv.sheet_names() if conv is not None else []
        multi = len(names) > 1
        self._sheet_box.blockSignals(True)
        self._sheet_box.clear()
        if multi:
            self._sheet_box.addItems(names)
            self._sheet_box.setCurrentIndex(0)
        self._sheet_box.blockSignals(False)
        self._sheet_action.setVisible(multi)

    def _on_sheet_changed(self, index: int):
        conv = self._current_conv
        if conv is None or index < 0:
            return
        conv.set_sheet(index)
        self._layers.populate(conv.get_layers())
        self._start_render()

    def _on_load_error(self, message: str):
        self._progress.setVisible(False)
        self._file_label.setText("Error loading file")
        QMessageBox.critical(
            self, "Cannot open file",
            f"{message}\n\nSupported files are DWG and DXF (R2000–R2018) "
            f"and DWFx."
        )

    def _start_render(self):
        """Re-render the current drawing (called after layer toggle)."""
        if self._current_conv is None:
            return
        if self._current_conv.is_raster:
            return          # a raster has no layer state to re-apply
        self._progress.setVisible(True)

        class _RenderWorker(QThread):
            done = pyqtSignal(str)
            err = pyqtSignal(str)

            def __init__(self, conv):
                super().__init__()
                self._conv = conv

            def run(self):
                try:
                    svg = self._conv.render_svg()
                    self.done.emit(svg)
                except Exception as exc:
                    self.err.emit(str(exc))

        w = _RenderWorker(self._current_conv)
        w.done.connect(lambda svg: (self._canvas.load_svg(svg), self._progress.setVisible(False)))
        w.err.connect(lambda msg: (self._progress.setVisible(False),
                                   self.statusBar().showMessage(f"Render error: {msg}", 4000)))
        w.finished.connect(w.deleteLater)
        self._render_worker = w
        w.start()

    # ------------------------------------------------------------------ #
    #  Layer panel slots
    # ------------------------------------------------------------------ #

    def _on_layer_toggled(self, name: str, visible: bool):
        if self._current_conv:
            self._current_conv.set_layer_visible(name, visible)
        # Don't auto-rerender on every click — user presses "Apply Layers" (R)
        # This keeps it snappy when toggling many layers at once.

    def _on_all_layers_toggled(self, visible: bool):
        if self._current_conv:
            self._current_conv.set_all_layers_visible(visible)

    # ------------------------------------------------------------------ #
    #  Status bar slots
    # ------------------------------------------------------------------ #

    def _on_coord_changed(self, x: float, y: float):
        self._coord_label.setText(f"X: {x:,.2f}    Y: {y:,.2f}")

    def _on_measurement_done(
        self, dist: float, x1: float, y1: float, x2: float, y2: float
    ):
        self._measure_label.setText(
            f"Distance: {dist:,.4f}  "
            f"({x1:,.2f}, {y1:,.2f}) → ({x2:,.2f}, {y2:,.2f})"
        )


# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #

def _vline() -> QWidget:
    line = QWidget()
    line.setFixedWidth(1)
    line.setFixedHeight(16)
    line.setStyleSheet("background: #555;")
    return line
