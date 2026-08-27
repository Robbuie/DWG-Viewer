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
        swatch.setStyleSheet(
            f"background-color: {color_hex}; border: 1px solid #555; border-radius: 2px;"
        )
        layout.addWidget(swatch)

        # Checkbox
        self._cb = QCheckBox(name)
        self._cb.setChecked(visible)
        self._cb.setStyleSheet("QCheckBox { color: #ccc; font-size: 12px; }")
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
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 6, 4, 4)
        outer.setSpacing(4)

        # Header
        header = QLabel("Layers")
        header.setStyleSheet(
            "color: #ddd; font-weight: bold; font-size: 13px; padding-bottom: 2px;"
        )
        outer.addWidget(header)

        # Show all / Hide all buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        show_all = QPushButton("Show All")
        show_all.setStyleSheet(self._btn_style())
        show_all.clicked.connect(lambda: self._toggle_all(True))
        btn_row.addWidget(show_all)

        hide_all = QPushButton("Hide All")
        hide_all.setStyleSheet(self._btn_style())
        hide_all.clicked.connect(lambda: self._toggle_all(False))
        btn_row.addWidget(hide_all)

        outer.addLayout(btn_row)

        # Divider
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #444;")
        outer.addWidget(line)

        # Scrollable layer list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )

        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(1)
        self._container_layout.addStretch()

        scroll.setWidget(self._container)
        outer.addWidget(scroll)

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(180)

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def populate(self, layers: list[dict]) -> None:
        """
        Replace the layer list.

        Parameters
        ----------
        layers : list of {name, color_hex, visible} dicts
                 (as returned by DWGConverter.get_layers())
        """
        # Clear existing rows
        for row in self._rows.values():
            self._container_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

        if not layers:
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

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _toggle_all(self, visible: bool) -> None:
        for row in self._rows.values():
            row.set_checked(visible)
        self.allToggled.emit(visible)

    @staticmethod
    def _btn_style() -> str:
        return (
            "QPushButton { background: #3a3a3a; color: #ccc; border: 1px solid #555; "
            "border-radius: 3px; padding: 3px 8px; font-size: 11px; }"
            "QPushButton:hover { background: #4a4a4a; }"
            "QPushButton:pressed { background: #555; }"
        )
