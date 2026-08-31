"""
Appearance dialog — theme, accent and chrome density.

The counterpart to the Redline PDF app's settings modal, and deliberately the
same shape: three independent axes, one control each, a one-line note under
every choice saying what it is for, and a live preview.

Live preview rather than an OK-then-look: the whole point of five themes is
that the right one is obvious on sight and unguessable from a name. Cancel
puts back whatever was running when the dialog opened, so trying all five
costs nothing.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                             QGridLayout, QLabel, QVBoxLayout)

from src import theme


class AppearanceDialog(QDialog):
    """Picks one combination of the three axes and applies it live."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Appearance")
        self.setMinimumWidth(430)

        self._start = theme.load({"accent": "blue"})
        self._build_ui()
        self._load_current()

    # ------------------------------------------------------------------ #
    #  UI
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)

        self._theme_box, self._theme_note = self._axis(
            grid, 0, "Theme", theme.THEMES,
            lambda k, v: (v["label"], v["note"], None))

        # The accent swatch is the control's own preview: six colour names in
        # a list say much less than six colours do.
        self._accent_box, self._accent_note = self._axis(
            grid, 2, "Accent", theme.ACCENTS,
            lambda k, v: (v["label"], "", _swatch(v["rgb"])))

        self._density_box, self._density_note = self._axis(
            grid, 4, "Density", theme.DENSITIES,
            lambda k, v: (v["label"], v["note"], None))

        outer.addLayout(grid)

        hint = QLabel("Every app in this family shares these settings' "
                      "design system, so a theme you pick here will look "
                      "familiar in the others.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        outer.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self._reject)
        outer.addWidget(buttons)

    def _axis(self, grid, row, title, catalog, describe):
        """One labelled combo plus the note that changes under it."""
        label = QLabel(title)
        label.setObjectName("panelHead")
        grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignRight)

        box = QComboBox()
        for key, value in catalog.items():
            text, _note, icon = describe(key, value)
            if icon is not None:
                box.addItem(icon, text, key)
            else:
                box.addItem(text, key)
        grid.addWidget(box, row, 1)

        note = QLabel("")
        note.setObjectName("hint")
        note.setWordWrap(True)
        grid.addWidget(note, row + 1, 1)

        box.currentIndexChanged.connect(self._on_changed)
        return box, note

    # ------------------------------------------------------------------ #
    #  State
    # ------------------------------------------------------------------ #

    def _load_current(self):
        for box, key in ((self._theme_box, "theme"),
                         (self._accent_box, "accent"),
                         (self._density_box, "density")):
            index = box.findData(self._start[key])
            box.blockSignals(True)
            box.setCurrentIndex(max(0, index))
            box.blockSignals(False)
        self._refresh_notes()

    def choice(self) -> dict[str, str]:
        return {
            "theme": self._theme_box.currentData(),
            "accent": self._accent_box.currentData(),
            "density": self._density_box.currentData(),
        }

    def _refresh_notes(self):
        c = self.choice()
        self._theme_note.setText(theme.THEMES[c["theme"]]["note"])
        self._density_note.setText(theme.DENSITIES[c["density"]]["note"])
        self._accent_note.setText(
            "Used for the armed tool, the active panel and the progress bar.")

    def _on_changed(self, _index=None):
        self._refresh_notes()
        self._apply(self.choice())

    def _apply(self, choice: dict[str, str]):
        values = theme.apply(QApplication.instance(), **choice)
        # Widgets that paint their own chrome are out of the stylesheet's
        # reach and have to be told. Walking the window tree for an
        # `apply_theme` beats a signal every one of them has to remember to
        # connect to.
        window = self.parent()
        if window is not None:
            for widget in window.findChildren(object):
                hook = getattr(widget, "apply_theme", None)
                if callable(hook):
                    hook(values)

    # ------------------------------------------------------------------ #
    #  Result
    # ------------------------------------------------------------------ #

    def _accept(self):
        c = self.choice()
        theme.save(**c)
        self.accept()

    def _reject(self):
        # Put back what was running when the dialog opened — a cancelled
        # dialog that leaves the fourth theme you tried on screen is not a
        # cancel.
        self._apply(self._start)
        self.reject()


def _swatch(rgb, size: int = 12) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(QColor(*rgb))
    return QIcon(pix)
