"""
main_window.py — application shell: toolbar, split layout, wiring.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer, QSize
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QPixmap, QColor, QActionGroup
from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QStatusBar, QLabel,
    QToolBar, QWidget, QProgressBar, QMessageBox,
    QFileDialog, QSizePolicy, QComboBox, QLineEdit,
)

from src.file_browser import FileBrowser
from src.canvas import DrawingCanvas
from src.layer_panel import LayerPanel
from src.converter import DWGConverter, DrawingError
from src.version import __version__, APP_NAME
from src.update_ui import UpdateChecker
from src import markup as mk
from src import printing


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
    # converter, layers, svg, png bytes, extents, text index — a drawing
    # arrives as one or the other: vector formats as SVG, classic DWF as
    # a raster.
    finished = pyqtSignal(object, list, str, object, object, object)
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
                # Classic DWF has no text until the geometry pass runs,
                # which happens after the drawing is already on screen.
                self.signals.finished.emit(conv, layers, "", png, extents, None)
            else:
                svg = conv.render_svg()
                index = conv.build_text_index(svg)
                self.signals.finished.emit(conv, layers, svg, None, None, index)
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

        self._markup_store: mk.MarkupStore | None = None
        self._markup_warned_fallback = False
        # Redlines save themselves; a short delay coalesces a burst of
        # edits into one write instead of hitting a network share on
        # every stroke.
        self._markup_save_timer = QTimer(self)
        self._markup_save_timer.setSingleShot(True)
        self._markup_save_timer.setInterval(400)
        self._markup_save_timer.timeout.connect(self._save_markup)

        self._updater = UpdateChecker(self)

        self._build_ui()
        self._build_toolbar()
        self._build_markup_toolbar()
        self._build_find_bar()
        self._build_statusbar()

        # Quiet daily check: says nothing unless there is something to say.
        QTimer.singleShot(4000, self._updater.check_silently)
        # Pan is checked by default — sync canvas state
        self._canvas.set_pan_mode(True)

    # ------------------------------------------------------------------ #
    #  UI construction
    # ------------------------------------------------------------------ #

    _PANEL_MIN = 140          # narrowest a side panel is still useful at

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
        self._canvas.markupChanged.connect(self._on_markup_changed)
        self._canvas.markupToolFinished.connect(self._sync_markup_actions)
        self._canvas.snapshotTaken.connect(self._on_snapshot_taken)
        self._canvas.statusMessage.connect(
            lambda msg: self.statusBar().showMessage(msg, 4000))
        splitter.addWidget(self._canvas)

        # Right: layer panel
        self._layers = LayerPanel()
        self._layers.layerToggled.connect(self._on_layer_toggled)
        self._layers.allToggled.connect(self._on_all_layers_toggled)
        splitter.addWidget(self._layers)

        # Proportions: browser 18%, canvas 64%, layers 18%
        splitter.setSizes([252, 896, 252])
        splitter.setCollapsible(1, False)     # never lose the drawing itself
        self._splitter = splitter
        # Widths to restore a panel to after it has been hidden.
        self._panel_widths = [252, 252]
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

        act_print = QAction("🖨  Print", self)
        act_print.setToolTip("Print the drawing, to scale or fitted (Ctrl+P)")
        act_print.setShortcut(QKeySequence("Ctrl+P"))
        act_print.triggered.connect(self._print)
        tb.addAction(act_print)

        act_find = QAction("🔍  Find", self)
        act_find.setToolTip("Find text on the sheet (Ctrl+F)")
        act_find.setShortcut(QKeySequence("Ctrl+F"))
        act_find.triggered.connect(self._show_find)
        tb.addAction(act_find)

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

        self._act_snap = QAction("📷  Snapshot", self)
        self._act_snap.setToolTip(
            "Drag a region to copy it to the clipboard (S)")
        self._act_snap.setShortcut(QKeySequence("S"))
        self._act_snap.setCheckable(True)
        self._act_snap.toggled.connect(self._set_snapshot_mode)
        tb.addAction(self._act_snap)

        act_copy_view = QAction("Copy view", self)
        act_copy_view.setToolTip("Copy the whole visible drawing (Ctrl+Shift+C)")
        act_copy_view.setShortcut(QKeySequence("Ctrl+Shift+C"))
        act_copy_view.triggered.connect(self._copy_view)
        self.addAction(act_copy_view)

        act_save_img = QAction("Save image…", self)
        act_save_img.setShortcut(QKeySequence("Ctrl+Shift+S"))
        act_save_img.triggered.connect(self._save_view_image)
        self.addAction(act_save_img)

        tb.addSeparator()

        # View controls: overview map and the two side panels
        self._act_nav = QAction("🗺  Navigator", self)
        self._act_nav.setToolTip(
            "Overview map — drag the box to pan, drag a new box to zoom (N)")
        self._act_nav.setShortcut(QKeySequence("N"))
        self._act_nav.setCheckable(True)
        self._act_nav.setChecked(True)
        self._act_nav.toggled.connect(self._canvas.set_navigator_visible)
        self._canvas.navigatorVisibilityChanged.connect(self._sync_nav_action)
        tb.addAction(self._act_nav)

        self._act_files = QAction("📁  Files", self)
        self._act_files.setToolTip("Show/hide the file browser (Ctrl+1)")
        self._act_files.setShortcut(QKeySequence("Ctrl+1"))
        self._act_files.setCheckable(True)
        self._act_files.setChecked(True)
        self._act_files.toggled.connect(
            lambda on: self._set_panel_visible(0, on))
        tb.addAction(self._act_files)

        self._act_layers_panel = QAction("🗂  Layers", self)
        self._act_layers_panel.setToolTip("Show/hide the layer panel (Ctrl+2)")
        self._act_layers_panel.setShortcut(QKeySequence("Ctrl+2"))
        self._act_layers_panel.setCheckable(True)
        self._act_layers_panel.setChecked(True)
        self._act_layers_panel.toggled.connect(
            lambda on: self._set_panel_visible(2, on))
        tb.addAction(self._act_layers_panel)

        # Both at once — hidden from the toolbar, it is just a shortcut.
        act_both = QAction("Toggle both side panels", self)
        act_both.setShortcut(QKeySequence("Ctrl+\\"))
        act_both.triggered.connect(self._toggle_both_panels)
        self.addAction(act_both)

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
    #  Find bar
    # ------------------------------------------------------------------ #

    def _build_find_bar(self):
        """A strip at the bottom, hidden until asked for.

        At the bottom rather than in the toolbar because it appears and
        disappears; a control that shuffles the toolbar every time it is
        used moves everything else out from under the cursor.
        """
        bar = QToolBar("Find")
        bar.setMovable(False)
        bar.setStyleSheet(
            "QToolBar { background: #262626; border-top: 1px solid #444;"
            " spacing: 6px; padding: 2px 6px; }"
            "QToolButton { color: #ccc; padding: 3px 7px; border-radius: 3px; }"
            "QToolButton:hover { background: #3a3a3a; }")
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, bar)

        label = QLabel("Find on sheet:")
        label.setStyleSheet("color:#888; font-size:11px;")
        bar.addWidget(label)

        self._find_edit = QLineEdit()
        self._find_edit.setPlaceholderText("Tag, label or note…")
        self._find_edit.setMaximumWidth(280)
        self._find_edit.setStyleSheet(
            "QLineEdit { background:#2a2a2a; border:1px solid #444;"
            " border-radius:3px; padding:3px 6px; color:#ddd; }")
        self._find_edit.returnPressed.connect(lambda: self._find_step(True))
        self._find_edit.textChanged.connect(self._on_find_text_changed)
        bar.addWidget(self._find_edit)

        act_prev = QAction("\u25c0  Previous", self)
        act_prev.triggered.connect(lambda: self._find_step(False))
        bar.addAction(act_prev)

        act_next = QAction("\u25b6  Next", self)
        act_next.triggered.connect(lambda: self._find_step(True))
        bar.addAction(act_next)

        self._find_label = QLabel("")
        self._find_label.setStyleSheet("color:#888; font-size:11px;")
        bar.addWidget(self._find_label)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        act_close = QAction("\u2715", self)
        act_close.setToolTip("Close the find bar (Esc)")
        act_close.triggered.connect(self._hide_find)
        bar.addAction(act_close)

        self._find_bar = bar
        bar.hide()

        # Scoped to the find field: a window-wide Escape here would
        # steal the key from the canvas, where it cancels a half-drawn
        # markup or backs out of snapshot mode.
        act_esc = QAction("Close find", self._find_edit)
        act_esc.setShortcut(QKeySequence("Esc"))
        act_esc.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        act_esc.triggered.connect(self._hide_find)
        self._find_edit.addAction(act_esc)

        act_next_key = QAction("Find next", self)
        act_next_key.setShortcut(QKeySequence("F3"))
        act_next_key.triggered.connect(lambda: self._find_step(True))
        self.addAction(act_next_key)

    def _show_find(self) -> None:
        self._find_bar.show()
        self._find_edit.setFocus()
        self._find_edit.selectAll()
        self._update_find_status()

    def _hide_find(self) -> None:
        self._find_bar.hide()
        self._canvas.clear_text_search()
        self._canvas.setFocus()

    def _on_find_text_changed(self, _text: str) -> None:
        # Typing restarts the walk; nothing moves until Enter or Next, so
        # the drawing does not lurch around under a half-typed tag.
        self._canvas.clear_text_search()
        self._update_find_status()

    def _find_step(self, forward: bool) -> None:
        query = self._find_edit.text().strip()
        if not query:
            self._update_find_status()
            return
        position, total = self._canvas.find_text(query, forward)
        if not total:
            self._find_label.setText("No match")
            available = self._canvas.text_index_size()
            if not available:
                self.statusBar().showMessage(
                    "This sheet has no searchable text — its lettering is "
                    "drawn as line work.", 5000)
        else:
            self._find_label.setText(f"{position} of {total}")

    def _update_find_status(self) -> None:
        if not hasattr(self, "_find_label"):
            return
        available = self._canvas.text_index_size()
        if not self._find_edit.text().strip():
            self._find_label.setText(
                f"{available} labels on this sheet" if available
                else "No searchable text")
        self._find_edit.setEnabled(True)

    # ------------------------------------------------------------------ #
    #  Printing
    # ------------------------------------------------------------------ #

    def _print(self) -> None:
        printing.print_drawing(self, self._canvas)

    # ------------------------------------------------------------------ #
    #  Markup toolbar
    # ------------------------------------------------------------------ #

    _MARKUP_COLORS = [
        ("Red", "#ff3b30"), ("Yellow", "#ffcc00"), ("Green", "#34c759"),
        ("Blue", "#0a84ff"), ("Magenta", "#ff2d95"), ("Black", "#101010"),
    ]

    _MARKUP_TOOLS = [
        ("select", "\u2196", "Select", "Select, move or delete markup (V)", "V"),
        (mk.CLOUD, "\u2601", "Cloud", "Revision cloud — drag a region (C)", "C"),
        (mk.BOX, "\u25ad", "Box", "Rectangle — drag a region (B)", "B"),
        (mk.ELLIPSE, "\u2b2d", "Ellipse", "Ellipse — drag a region (E)", "E"),
        (mk.ARROW, "\u2197", "Arrow", "Arrow — drag from tail to head (A)", "A"),
        (mk.PEN, "\u270e", "Pen", "Freehand line (D)", "D"),
        (mk.TEXT, "T", "Note", "Text note — click where it goes (T)", "T"),
    ]

    def _build_markup_toolbar(self):
        self.addToolBarBreak()
        tb = QToolBar("Markup")
        tb.setMovable(False)
        tb.setStyleSheet(
            "QToolBar { background: #262626; border-bottom: 1px solid #444;"
            " spacing: 4px; }"
            "QToolButton { color: #ccc; padding: 3px 7px; border-radius: 3px; }"
            "QToolButton:hover { background: #3a3a3a; }"
            "QToolButton:checked { background: #a33; color: #fff; }")
        self.addToolBar(tb)

        label = QLabel("  Markup ")
        label.setStyleSheet("color:#888; font-size:11px;")
        tb.addWidget(label)

        group = QActionGroup(self)
        group.setExclusive(True)
        self._markup_actions = {}
        for tool, glyph, name, tip, key in self._MARKUP_TOOLS:
            act = QAction(f"{glyph}  {name}", self)
            act.setToolTip(tip)
            act.setShortcut(QKeySequence(key))
            act.setCheckable(True)
            act.triggered.connect(
                lambda checked, t=tool: self._pick_markup_tool(t, checked))
            group.addAction(act)
            tb.addAction(act)
            self._markup_actions[tool] = act

        tb.addSeparator()

        self._color_box = QComboBox()
        self._color_box.setToolTip("Markup colour")
        self._color_box.setIconSize(QSize(12, 12))
        self._color_box.setStyleSheet(
            "QComboBox { background: #3a3a3a; color: #ccc; border: 1px solid #555;"
            " border-radius: 3px; padding: 2px 6px; }"
            "QComboBox QAbstractItemView { background: #2d2d2d; color: #ccc;"
            " selection-background-color: #3a6ea8; }")
        self._markup_bar = tb
        for name, hex_code in self._MARKUP_COLORS:
            pix = QPixmap(12, 12)
            pix.fill(QColor(hex_code))
            self._color_box.addItem(QIcon(pix), name, hex_code)
        self._color_box.currentIndexChanged.connect(
            lambda i: self._canvas.set_markup_color(
                self._color_box.itemData(i) or mk.DEFAULT_COLOR))
        tb.addWidget(self._color_box)

        tb.addSeparator()

        self._act_mk_undo = QAction("\u21b6  Undo", self)
        self._act_mk_undo.setToolTip("Undo the last markup change (Ctrl+Z)")
        self._act_mk_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self._act_mk_undo.triggered.connect(self._undo_markup)
        tb.addAction(self._act_mk_undo)

        self._act_mk_delete = QAction("\u2715  Delete", self)
        self._act_mk_delete.setToolTip("Delete the selected markup (Del)")
        self._act_mk_delete.triggered.connect(self._delete_markup)
        tb.addAction(self._act_mk_delete)

        act_clear = QAction("Clear sheet", self)
        act_clear.setToolTip("Remove every markup on this sheet")
        act_clear.triggered.connect(self._clear_markup)
        tb.addAction(act_clear)

        tb.addSeparator()

        self._act_mk_show = QAction("\U0001f441  Show markup", self)
        self._act_mk_show.setToolTip("Show or hide all markup (H)")
        self._act_mk_show.setShortcut(QKeySequence("H"))
        self._act_mk_show.setCheckable(True)
        self._act_mk_show.setChecked(True)
        self._act_mk_show.toggled.connect(self._canvas.set_markup_visible)
        tb.addAction(self._act_mk_show)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self._markup_label = QLabel("No markup")
        self._markup_label.setStyleSheet("color:#888; font-size:11px; padding-right:8px;")
        tb.addWidget(self._markup_label)

        act_bar = QAction("Show/hide the markup toolbar", self)
        act_bar.setShortcut(QKeySequence("Ctrl+3"))
        act_bar.triggered.connect(
            lambda: self._markup_bar.setVisible(not self._markup_bar.isVisible()))
        self.addAction(act_bar)

        self._sync_markup_actions()

    # -- markup slots -------------------------------------------------- #

    def _pick_markup_tool(self, tool: str, checked: bool) -> None:
        self._canvas.set_markup_tool(tool if checked else None)
        if checked and self._act_snap.isChecked():
            self._act_snap.setChecked(False)
        self._sync_markup_actions()

    def _sync_markup_actions(self) -> None:
        active = self._canvas.markup_tool()
        for tool, act in self._markup_actions.items():
            act.blockSignals(True)
            act.setChecked(tool == active)
            act.blockSignals(False)
        self._act_mk_undo.setEnabled(self._canvas.can_undo_markup())
        self._act_mk_show.blockSignals(True)
        self._act_mk_show.setChecked(self._canvas.markup_visible())
        self._act_mk_show.blockSignals(False)
        self._update_markup_label()

    def _update_markup_label(self) -> None:
        n = self._canvas.markup_count()
        if not n:
            self._markup_label.setText("No markup")
            return
        where = ""
        if self._markup_store is not None and self._markup_store.is_fallback:
            where = " · saved locally"
        self._markup_label.setText(
            f"{n} markup item{'s' if n != 1 else ''}{where}")

    def _undo_markup(self) -> None:
        if not self._canvas.undo_markup():
            self.statusBar().showMessage("Nothing to undo.", 2500)
        self._sync_markup_actions()

    def _delete_markup(self) -> None:
        n = self._canvas.delete_selected_markup()
        if not n:
            self.statusBar().showMessage(
                "Select markup first — press V, then click it.", 3500)
        self._sync_markup_actions()

    def _clear_markup(self) -> None:
        if self._canvas.markup_count() == 0:
            return
        if QMessageBox.question(
                self, "Clear markup",
                "Remove every markup item on this sheet?") != \
                QMessageBox.StandardButton.Yes:
            return
        self._canvas.clear_markup_on_sheet()
        self._sync_markup_actions()

    def _on_markup_changed(self) -> None:
        self._markup_save_timer.start()
        self._sync_markup_actions()

    def _save_markup(self) -> None:
        store = self._markup_store
        if store is None:
            return
        if not store.save():
            self.statusBar().showMessage(
                "Markup could not be saved — the folder is not writable.", 6000)
            return
        if store.is_fallback and not self._markup_warned_fallback:
            self._markup_warned_fallback = True
            self.statusBar().showMessage(
                f"This folder is read-only, so markup is being kept on this "
                f"PC instead ({store.path}).", 9000)
        self._update_markup_label()

    def _sheet_key(self) -> str:
        conv = self._current_conv
        if conv is None:
            return "0"
        names = conv.sheet_names()
        index = self._sheet_box.currentIndex() if len(names) > 1 else 0
        if 0 <= index < len(names):
            return names[index]
        return "0"

    def _attach_markup(self) -> None:
        self._canvas.set_markup_context(self._markup_store, self._sheet_key())
        self._sync_markup_actions()

    # ------------------------------------------------------------------ #
    #  Snapshot
    # ------------------------------------------------------------------ #

    def _set_snapshot_mode(self, on: bool) -> None:
        self._canvas.set_snapshot_mode(on)
        if on:
            for act in self._markup_actions.values():
                act.setChecked(False)
            self._canvas.set_markup_tool(None)
            self.statusBar().showMessage(
                "Drag a region to copy it to the clipboard — Esc cancels.", 5000)

    def _copy_view(self) -> None:
        if not self._canvas.copy_view_to_clipboard():
            self.statusBar().showMessage("Nothing to copy — open a drawing first.",
                                         3000)

    def _on_snapshot_taken(self, w: int, h: int) -> None:
        self._act_snap.setChecked(False)
        self.statusBar().showMessage(
            f"Copied to the clipboard at {w} \u00d7 {h} px — paste it anywhere.",
            5000)

    def _save_view_image(self) -> None:
        if self._current_conv is None:
            self.statusBar().showMessage("Open a drawing first.", 3000)
            return
        default = f"{self._current_conv.filepath.stem}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save image", default, "PNG image (*.png)")
        if not path:
            return
        visible = self._canvas.mapToScene(
            self._canvas.viewport().rect()).boundingRect()
        image = self._canvas.render_region(visible)
        if image is None or not image.save(path, "PNG"):
            self.statusBar().showMessage("Could not write that image.", 4000)
        else:
            self.statusBar().showMessage(f"Saved {path}", 5000)

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

    # ------------------------------------------------------------------ #
    #  Panel visibility
    # ------------------------------------------------------------------ #

    def _set_panel_visible(self, index: int, visible: bool) -> None:
        """Collapse or restore a side panel.

        The width it had is kept, so bringing a panel back does not leave
        it as a sliver or eat half the drawing area.
        """
        widget = self._splitter.widget(index)
        if widget is None or widget.isVisible() == visible:
            return
        slot = 0 if index == 0 else 1
        if not visible:
            current = self._splitter.sizes()[index]
            if current >= self._PANEL_MIN:
                self._panel_widths[slot] = current
            widget.hide()
        else:
            widget.show()
        self._apply_panel_widths()

    def _apply_panel_widths(self) -> None:
        total = max(1, self._splitter.width() - 2 * self._splitter.handleWidth())
        left = self._panel_widths[0] if self._splitter.widget(0).isVisible() else 0
        right = self._panel_widths[1] if self._splitter.widget(2).isVisible() else 0
        centre = total - left - right
        if centre < 200:            # never squeeze the drawing out
            left = right = 0
            centre = total
        self._splitter.setSizes([left, centre, right])

    def _toggle_both_panels(self) -> None:
        show = not (self._act_files.isChecked() or self._act_layers_panel.isChecked())
        self._act_files.setChecked(show)
        self._act_layers_panel.setChecked(show)

    def _sync_nav_action(self, visible: bool) -> None:
        """Keep the toolbar button honest when the map closes itself."""
        if self._act_nav.isChecked() != visible:
            self._act_nav.blockSignals(True)
            self._act_nav.setChecked(visible)
            self._act_nav.blockSignals(False)

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

        # Anything still pending for the previous drawing goes to disk
        # before its store is replaced.
        self._flush_markup()

        self._current_conv = None
        self._canvas.set_markup_context(None, "0")
        self._markup_store = mk.MarkupStore(filepath)
        self._markup_warned_fallback = False
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
                          png=None, extents=None, text_index=None):
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
        self._attach_markup()
        self._canvas.set_paper_inches(conv.paper_size_inches())
        self._canvas.set_text_index(text_index)
        self._update_find_status()
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
        self._canvas.set_text_index(conv.build_text_index())
        self._update_find_status()
        found = self._canvas.text_index_size()
        extra = f" {found} searchable labels." if found else ""
        self.statusBar().showMessage(
            "Sharp zoom ready — the drawing is now redrawn at full detail "
            "as you zoom in." + extra, 5000)

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
        self._flush_markup()
        conv.set_sheet(index)
        self._layers.populate(conv.get_layers())
        self._start_render()
        self._attach_markup()

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
        w.done.connect(lambda svg: (self._canvas.load_svg(svg),
                                    self._attach_markup(),
                                    self._canvas.set_paper_inches(
                                        self._current_conv.paper_size_inches()),
                                    self._canvas.set_text_index(
                                        self._current_conv.build_text_index(svg)),
                                    self._update_find_status(),
                                    self._progress.setVisible(False)))
        w.err.connect(lambda msg: (self._progress.setVisible(False),
                                   self.statusBar().showMessage(f"Render error: {msg}", 4000)))
        w.finished.connect(w.deleteLater)
        self._render_worker = w
        w.start()

    def _flush_markup(self) -> None:
        """Write pending redlines now rather than on the timer."""
        if self._markup_save_timer.isActive():
            self._markup_save_timer.stop()
            self._save_markup()

    def closeEvent(self, event):
        self._flush_markup()
        super().closeEvent(event)

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
