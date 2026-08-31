"""
printing.py — putting a drawing on paper, at a scale you can trust.

A viewer that cannot print is not a replacement for one that can: the
drawing still has to reach a field tech's hands. Two things make this
more than "render the widget to a printer".

**Scale has to be real or absent.** DWFx carries a true page size (XPS
units are 1/96 inch) and classic DWF records inches per drawing unit, so
for those a 1:1 print really is 1:1 and 50% really is half. DXF and DWG
as opened here are model space with no plot layout, so there is no
honest inches-per-unit to quote — those get fit-to-page only, and the
dialog says why rather than offering a scale that would be a guess.

**A sheet at scale rarely fits one page.** A D-size sheet at 1:1 on
letter paper is 8 pages, and the answer that helps is to tile it, not to
silently shrink it. `plan_pages` works that out as plain geometry, with
no printer involved, which is also what makes it testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PyQt6.QtCore import Qt, QRectF, QSizeF
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QCheckBox,
    QPushButton, QGroupBox, QRadioButton, QButtonGroup, QMessageBox,
)

# What to put on the paper.
WHOLE_SHEET = "sheet"
CURRENT_VIEW = "view"

FIT = "fit"
SCALE = "scale"

# Offered as "print at this fraction of true size". Anything finer is
# better served by the fit option.
RATIOS = [
    ("Actual size (1:1)", 1.0),
    ("Half size (1:2)", 0.5),
    ("Quarter size (1:4)", 0.25),
    ("Eighth size (1:8)", 0.125),
    ("Double size (2:1)", 2.0),
]


@dataclass(frozen=True)
class PrintOptions:
    what: str = WHOLE_SHEET
    mode: str = FIT
    ratio: float = 1.0
    tile: bool = True
    markup: bool = True


@dataclass(frozen=True)
class PageLayout:
    """One sheet of paper: which part of the drawing, drawn where."""
    source: QRectF          # scene coordinates
    target: QRectF          # device pixels on the page
    row: int = 0
    column: int = 0


def plan_pages(source: QRectF, page: QSizeF, dpi: float,
               inches_per_scene: float | None, options: PrintOptions
               ) -> list[PageLayout]:
    """Work out the page grid. Pure geometry — no printer, no Qt paint.

    `inches_per_scene` converts scene units to real inches; None means
    the format did not tell us, and only fit-to-page is possible.
    """
    if (source.width() <= 0 or source.height() <= 0
            or page.width() <= 0 or page.height() <= 0):
        return []

    fit_scale = min(page.width() / source.width(),
                    page.height() / source.height())

    if options.mode == FIT or not inches_per_scene or dpi <= 0:
        px = fit_scale
    else:
        px = inches_per_scene * options.ratio * dpi

    total_w = source.width() * px
    total_h = source.height() * px

    # Comfortably within one page: centre it and stop.
    if total_w <= page.width() + 0.5 and total_h <= page.height() + 0.5:
        left = (page.width() - total_w) / 2.0
        top = (page.height() - total_h) / 2.0
        return [PageLayout(QRectF(source), QRectF(left, top, total_w, total_h))]

    if not options.tile:
        # Honour the scale and print the middle of the drawing, which is
        # at least predictable; the caller warns that it was cropped.
        span_x = page.width() / px
        span_y = page.height() / px
        left = source.left() + (source.width() - span_x) / 2.0
        top = source.top() + (source.height() - span_y) / 2.0
        return [PageLayout(QRectF(left, top, span_x, span_y),
                           QRectF(0, 0, page.width(), page.height()))]

    cols = max(1, math.ceil(total_w / page.width() - 1e-6))
    rows = max(1, math.ceil(total_h / page.height() - 1e-6))
    span_x = page.width() / px
    span_y = page.height() / px

    pages: list[PageLayout] = []
    for r in range(rows):
        for c in range(cols):
            x = source.left() + c * span_x
            y = source.top() + r * span_y
            w = min(span_x, source.right() - x)
            h = min(span_y, source.bottom() - y)
            if w <= 0 or h <= 0:
                continue
            pages.append(PageLayout(
                QRectF(x, y, w, h),
                QRectF(0, 0, w * px, h * px), row=r, column=c))
    return pages


def describe_plan(pages: list[PageLayout], options: PrintOptions,
                  inches_per_scene: float | None) -> str:
    """One line for the dialog, so nobody is surprised at the printer."""
    if not pages:
        return "Nothing to print."
    n = len(pages)
    if options.mode == FIT or not inches_per_scene:
        return "Fitted to one page."
    label = next((name for name, r in RATIOS
                  if abs(r - options.ratio) < 1e-9), f"{options.ratio:g}x")
    if n == 1:
        return f"{label} — one page."
    cols = max(p.column for p in pages) + 1
    rows = max(p.row for p in pages) + 1
    if not options.tile:
        return f"{label} — one page, cropped to the middle of the drawing."
    return f"{label} — {n} pages, {cols} across by {rows} down."


# ------------------------------------------------------------------ #
#  Options dialog
# ------------------------------------------------------------------ #

class PrintOptionsDialog(QDialog):
    """What to print and at what scale. Paper and printer come after,
    from the preview window's own page setup."""

    def __init__(self, parent, paper_inches: tuple[float, float] | None,
                 has_view: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Print drawing")
        self._paper = paper_inches

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # -- what ----------------------------------------------------- #
        what_box = QGroupBox("Print")
        wl = QVBoxLayout(what_box)
        self._rb_sheet = QRadioButton("The whole sheet")
        self._rb_view = QRadioButton("Only what is on screen now")
        self._rb_sheet.setChecked(True)
        self._rb_view.setEnabled(has_view)
        wl.addWidget(self._rb_sheet)
        wl.addWidget(self._rb_view)
        lay.addWidget(what_box)

        # -- scale ---------------------------------------------------- #
        scale_box = QGroupBox("Size")
        sl = QVBoxLayout(scale_box)
        self._rb_fit = QRadioButton("Fit to the page")
        self._rb_scale = QRadioButton("Print at a set scale")
        self._rb_fit.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self._rb_fit)
        group.addButton(self._rb_scale)
        sl.addWidget(self._rb_fit)

        row = QHBoxLayout()
        row.addWidget(self._rb_scale)
        self._ratio_box = QComboBox()
        for name, value in RATIOS:
            self._ratio_box.addItem(name, value)
        row.addWidget(self._ratio_box, 1)
        sl.addLayout(row)

        self._tile = QCheckBox("Tile across several pages when it does not fit")
        self._tile.setChecked(True)
        sl.addWidget(self._tile)

        if paper_inches is None:
            # Say why rather than greying a control with no explanation.
            self._rb_scale.setEnabled(False)
            self._ratio_box.setEnabled(False)
            self._tile.setEnabled(False)
            note = QLabel("This drawing has no plot layout, so its real "
                          "paper size is unknown — only fit-to-page is "
                          "available. DWF and DWFx sheets can be printed "
                          "to scale.")
            note.setWordWrap(True)
            note.setObjectName("hint")
            sl.addWidget(note)
        else:
            w, h = paper_inches
            size = QLabel(f"Sheet is {w:.2f} × {h:.2f} in "
                          f"({w * 25.4:.0f} × {h * 25.4:.0f} mm).")
            size.setObjectName("hint")
            sl.addWidget(size)
        lay.addWidget(scale_box)

        self._markup = QCheckBox("Include markup")
        self._markup.setChecked(True)
        lay.addWidget(self._markup)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("Preview…")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        lay.addLayout(buttons)

    def options(self) -> PrintOptions:
        return PrintOptions(
            what=CURRENT_VIEW if self._rb_view.isChecked() else WHOLE_SHEET,
            mode=SCALE if (self._rb_scale.isChecked()
                           and self._rb_scale.isEnabled()) else FIT,
            ratio=float(self._ratio_box.currentData() or 1.0),
            tile=self._tile.isChecked(),
            markup=self._markup.isChecked(),
        )


# ------------------------------------------------------------------ #
#  Driving the printer
# ------------------------------------------------------------------ #

def source_rect(canvas, options: PrintOptions) -> QRectF:
    content = canvas.content_rect()
    if options.what == CURRENT_VIEW:
        visible = canvas.visible_scene_rect().intersected(content)
        if visible.width() > 0 and visible.height() > 0:
            return visible
    return content


def inches_per_scene_unit(canvas) -> float | None:
    paper = canvas.paper_inches()
    content = canvas.content_rect()
    if not paper or content.width() <= 0:
        return None
    return paper[0] / content.width()


def render_pages(canvas, printer, options: PrintOptions) -> int:
    """Paint every page. Returns the number of pages printed."""
    source = source_rect(canvas, options)
    dpi = float(printer.resolution())
    # paintRectPixels is the printable area in the painter's own device
    # pixels, which is the only unit the page maths needs — asking for
    # points and converting would just add a rounding step.
    paint = printer.pageLayout().paintRectPixels(int(dpi))
    page_px = QSizeF(float(paint.width()), float(paint.height()))

    pages = plan_pages(source, page_px, dpi,
                       inches_per_scene_unit(canvas), options)
    if not pages:
        return 0

    painter = QPainter()
    if not painter.begin(printer):
        return 0
    try:
        for i, page in enumerate(pages):
            if i:
                printer.newPage()
            canvas.render_scene(painter, page.target, page.source,
                                include_markup=options.markup)
    finally:
        painter.end()
    return len(pages)


def print_drawing(parent, canvas) -> None:
    """Ask what to print, then hand it to the print preview."""
    from PyQt6.QtPrintSupport import QPrinter, QPrintPreviewDialog
    from PyQt6.QtGui import QPageLayout, QPageSize

    if canvas.content_rect().isEmpty():
        QMessageBox.information(parent, "Nothing to print",
                                "Open a drawing first.")
        return

    dialog = PrintOptionsDialog(parent, canvas.paper_inches())
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    options = dialog.options()

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    source = source_rect(canvas, options)
    # Start in the orientation the drawing actually is; a landscape sheet
    # defaulting to portrait wastes half the paper and everyone's time.
    printer.setPageOrientation(
        QPageLayout.Orientation.Landscape if source.width() >= source.height()
        else QPageLayout.Orientation.Portrait)

    preview = QPrintPreviewDialog(printer, parent)
    preview.setWindowTitle("Print preview")
    preview.resize(1000, 750)
    preview.paintRequested.connect(
        lambda p: render_pages(canvas, p, options))
    preview.exec()
