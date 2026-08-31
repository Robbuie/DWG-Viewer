"""
layer_panel.py — right-side panel for toggling layer visibility.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea,
    QCheckBox, QSizePolicy, QFrame,
)


class _LayerRow(QWidget):
    """Single row: colour swatch + checkbox with layer name."""

    toggled = pyqtSignal(str, bool)   # layer_name, visible

    def __init__(self, name: str, color_hex: str, visible: bool, parent=None):
        super().__init__(parent)
        self._name = name

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        # Colour swatch
        swatch = QFrame()
        swatch.setFixedSize(12, 12)
        # The one colour in the app that is not the theme's to choose: it is
        # the layer's own colour out of the drawing. Only the frame is themed.
        swatch.setStyleSheet(
            f"background-color: {color_hex};"
            " border: 1px solid palette(mid); border-radius: 2px;"
        )
        layout.addWidget(swatch)

        # Checkbox
        self._cb = QCheckBox(name)
        self._cb.setChecked(visible)
        self._cb.toggled.connect(lambda checked: self.toggled.emit(self._name, checked))
        layout.addWidget(self._cb)
        layout.addStretch()

    def set_checked(self, checked: bool):
        self._cb.blockSignals(True)
        self._cb.setChecked(checked)
        self._cb.blockSignals(False)


class LayerPanel(QWidget):
    """
    Right-side dock widget listing all layers with visibility checkboxes.

    Signals
    -------
    layerToggled(name: str, visible: bool)   — single layer toggled
    allToggled(visible: bool)                — show/hide all clicked
    """

    layerToggled = pyqtSignal(str, bool)
    allToggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: dict[str, _LayerRow] = {}
        self._build_ui()

    # ------------------------------------------------------------------ #
    #  UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        self.setObjectName("sidePanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 6, 4, 4)
        outer.setSpacing(4)

        # Header
        header = QLabel("Layers")
        header.setObjectName("panelTitle")
        outer.addWidget(header)

        # Show all / Hide all buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        self._show_all = QPushButton("Show All")
        self._show_all.clicked.connect(lambda: self._toggle_all(True))
        btn_row.addWidget(self._show_all)

        self._hide_all = QPushButton("Hide All")
        self._hide_all.clicked.connect(lambda: self._toggle_all(False))
        btn_row.addWidget(self._hide_all)

        outer.addLayout(btn_row)

        # Divider
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("divider")
        outer.addWidget(line)

        # Scrollable layer list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(1)
        # Shown in place of the list when there are no layers. An empty
        # panel on its own reads as a bug; the usual cause is a file that
        # simply has no layers in it, which is worth saying.
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setObjectName("hint")
        self._note.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._note.hide()
        self._container_layout.addWidget(self._note)

        self._container_layout.addStretch()

        scroll.setWidget(self._container)
        outer.addWidget(scroll)

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(180)

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def populate(self, layers: list[dict], note: str | None = None) -> None:
        """
        Replace the layer list.

        Parameters
        ----------
        layers : list of {name, color_hex, visible} dicts
                 (as returned by DWGConverter.get_layers())
        note   : what to show instead when the list is empty — why this
                 drawing has no layers, rather than a blank panel.
        """
        # Clear existing rows
        for row in self._rows.values():
            self._container_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

        has_layers = bool(layers)
        self._show_all.setEnabled(has_layers)
        self._hide_all.setEnabled(has_layers)
        shown = "" if has_layers else (note or "")
        self._note.setText(shown)
        self._note.setVisible(bool(shown))

        if not has_layers:
            return

        # Re-insert rows before the stretch
        stretch_item = self._container_layout.takeAt(
            self._container_layout.count() - 1
        )

        for layer in layers:
            row = _LayerRow(
                layer["name"],
                layer["color_hex"],
                layer["visible"],
            )
            row.toggled.connect(self.layerToggled)
            self._container_layout.addWidget(row)
            self._rows[layer["name"]] = row

        self._container_layout.addStretch()

    def set_layer_visible(self, name: str, visible: bool) -> None:
        row = self._rows.get(name)
        if row:
            row.set_checked(visible)

    def clear(self) -> None:
        self.populate([])

    @property
    def note(self) -> str:
        """The empty-state message currently shown, or "".

        Read off the text rather than the widget's visibility: a panel
        that has never been shown reports every child as invisible, so
        isVisible() here would only ever be a test of whether the window
        is open.
        """
        return self._note.text()

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _toggle_all(self, visible: bool) -> None:
        for row in self._rows.values():
            row.set_checked(visible)
        self.allToggled.emit(visible)

