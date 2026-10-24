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
ALL_SHEETS = "all"          # every sheet of a drawing set

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


# Paper for a PDF. None means "each page the size of its own sheet",
# which is what makes a PDF of a drawing set print to scale from any
# reader; the rest are for sending a set to an office printer or a
# print shop on one paper size. Sizes are (short, long) in inches.
_MM = 1 / 25.4
PDF_PAPERS = [
    ("Same size as each sheet", None),
    ("Letter (8.5 × 11 in)", (8.5, 11.0)),
    ("Legal (8.5 × 14 in)", (8.5, 14.0)),
    ("Tabloid / ANSI B (11 × 17 in)", (11.0, 17.0)),
    ("ANSI C (17 × 22 in)", (17.0, 22.0)),
    ("ANSI D (22 × 34 in)", (22.0, 34.0)),
    ("ANSI E (34 × 44 in)", (34.0, 44.0)),
    ("A4 (210 × 297 mm)", (210 * _MM, 297 * _MM)),
    ("A3 (297 × 420 mm)", (297 * _MM, 420 * _MM)),
    ("A2 (420 × 594 mm)", (420 * _MM, 594 * _MM)),
    ("A1 (594 × 841 mm)", (594 * _MM, 841 * _MM)),
    ("A0 (841 × 1189 mm)", (841 * _MM, 1189 * _MM)),
]
# For a drawing with no paper size of its own (DWG, DXF) when "same
# size as each sheet" has nothing to go on.
FALLBACK_PAPER = (11.0, 17.0)
# Printers cannot reach the edge of the paper, and neither should a PDF
# that is going to one. Sheet-sized pages have none: the sheet already
# carries its own border.
PDF_MARGIN_IN = 0.25


@dataclass(frozen=True)
class PrintOptions:
    what: str = WHOLE_SHEET
    mode: str = FIT
    ratio: float = 1.0
    tile: bool = True
    markup: bool = True
    paper: tuple[float, float] | None = None    # PDF only; see PDF_PAPERS


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


def plan_pdf_pages(source: QRectF, inches_per_scene: float | None,
                   options: PrintOptions, dpi: float
                   ) -> list[tuple[tuple[float, float], PageLayout]]:
    """Pages for one sheet of a PDF: [((width_in, height_in), layout)].

    Unlike a printer, a PDF can make every page whatever size it likes.
    With no paper chosen each sheet gets one page of its own size — or
    that size times the ratio, when printing at a set scale — so the
    PDF is a true-size copy of the set. With a paper chosen, each sheet
    is fitted (or scaled and tiled) onto it, turned to match the sheet.
    """
    if source.width() <= 0 or source.height() <= 0 or dpi <= 0:
        return []
    if options.paper is None and inches_per_scene:
        k = options.ratio if options.mode == SCALE else 1.0
        w_in = source.width() * inches_per_scene * k
        h_in = source.height() * inches_per_scene * k
        return [((w_in, h_in),
                 PageLayout(QRectF(source), QRectF(0, 0, w_in * dpi, h_in * dpi)))]

    short, long_ = sorted(options.paper or FALLBACK_PAPER)
    landscape = source.width() >= source.height()
    w_in, h_in = (long_, short) if landscape else (short, long_)
    m = PDF_MARGIN_IN
    printable = QSizeF((w_in - 2 * m) * dpi, (h_in - 2 * m) * dpi)
    out = []
    for page in plan_pages(source, printable, dpi, inches_per_scene, options):
        target = page.target.translated(m * dpi, m * dpi)
        out.append(((w_in, h_in),
                    PageLayout(page.source, target, page.row, page.column)))
    return out


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
    from the preview window's own page setup — except for a PDF, which
    has no printer to ask, so the paper is chosen here."""

    def __init__(self, parent, paper_inches: tuple[float, float] | None,
                 has_view: bool = True, sheet_count: int = 1,
                 for_pdf: bool = False):
        super().__init__(parent)
        self._for_pdf = for_pdf
        self.setWindowTitle("Save as PDF" if for_pdf else "Print drawing")
        self._paper = paper_inches

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # -- what ----------------------------------------------------- #
        what_box = QGroupBox("Sheets" if sheet_count > 1 else
                             ("Save" if for_pdf else "Print"))
        wl = QVBoxLayout(what_box)
        self._rb_sheet = QRadioButton(
            "This sheet" if sheet_count > 1 else "The whole sheet")
        self._rb_view = QRadioButton("Only what is on screen now")
        self._rb_all = QRadioButton(f"All {sheet_count} sheets in this set")
        self._rb_sheet.setChecked(True)
        self._rb_view.setEnabled(has_view)
        if sheet_count > 1:
            wl.addWidget(self._rb_all)
        wl.addWidget(self._rb_sheet)
        wl.addWidget(self._rb_view)
        self._has_all = sheet_count > 1
        if sheet_count > 1 and for_pdf:
            # Saving a set as a PDF is nearly always the whole set.
            self._rb_all.setChecked(True)
        self._all_note = QLabel("A set prints straight to the printer, "
                                "without a preview.")
        self._all_note.setWordWrap(True)
        self._all_note.setObjectName("hint")
        wl.addWidget(self._all_note)
        lay.addWidget(what_box)

        # -- paper (PDF only) ---------------------------------------- #
        self._paper_box = QComboBox()
        if for_pdf:
            paper_box = QGroupBox("Page size")
            pl = QVBoxLayout(paper_box)
            for name, value in PDF_PAPERS:
                if value is None and paper_inches is None:
                    continue        # no sheet size to match
                self._paper_box.addItem(name, value)
            if paper_inches is None:
                self._paper_box.setCurrentIndex(
                    next(i for i in range(self._paper_box.count())
                         if self._paper_box.itemData(i) == FALLBACK_PAPER))
            pl.addWidget(self._paper_box)
            self._paper_note = QLabel()
            self._paper_note.setWordWrap(True)
            self._paper_note.setObjectName("hint")
            pl.addWidget(self._paper_note)
            lay.addWidget(paper_box)
            self._paper_box.currentIndexChanged.connect(self._sync)

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
        self._ok = QPushButton("Save…" if for_pdf else "Preview…")
        self._ok.setDefault(True)
        self._ok.clicked.connect(self.accept)
        buttons.addWidget(self._ok)
        lay.addLayout(buttons)

        for rb in (self._rb_all, self._rb_sheet, self._rb_view):
            rb.toggled.connect(self._sync)
        self._rb_fit.toggled.connect(self._sync)
        self._sync()

    def _sync(self, *_):
        """Keep the controls honest about what they will do."""
        all_sheets = self._rb_all.isChecked()
        self._all_note.setVisible(all_sheets and not self._for_pdf)
        if not self._for_pdf:
            self._ok.setText("Print…" if all_sheets else "Preview…")
            return
        same_size = self._paper_box.currentData() is None
        # One page per sheet at its own size never needs tiling.
        self._tile.setEnabled(self._paper is not None and not same_size)
        self._rb_fit.setText("Actual size" if same_size else "Fit to the page")
        if same_size:
            self._paper_note.setText(
                "Each page is the size of its sheet, so the PDF prints to "
                "scale from any PDF reader.")
        else:
            self._paper_note.setText(
                "Each sheet is turned to suit its shape. "
                "Pages have a ¼ in margin.")

    def options(self) -> PrintOptions:
        if self._has_all and self._rb_all.isChecked():
            what = ALL_SHEETS
        elif self._rb_view.isChecked():
            what = CURRENT_VIEW
        else:
            what = WHOLE_SHEET
        return PrintOptions(
            what=what,
            mode=SCALE if (self._rb_scale.isChecked()
                           and self._rb_scale.isEnabled()) else FIT,
            ratio=float(self._ratio_box.currentData() or 1.0),
            tile=self._tile.isChecked(),
            markup=self._markup.isChecked(),
            paper=self._paper_box.currentData() if self._for_pdf else None,
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


def _sheet_count(conv) -> int:
    try:
        return max(1, int(conv.sheet_count)) if conv is not None else 1
    except Exception:
        return 1


def print_drawing(parent, canvas, conv=None, store=None) -> None:
    """Ask what to print, then hand it to the print preview — or, for a
    whole drawing set, straight to the printer."""
    from PyQt6.QtPrintSupport import QPrinter, QPrintPreviewDialog, QPrintDialog
    from PyQt6.QtGui import QPageLayout, QPageSize

    if canvas.content_rect().isEmpty():
        QMessageBox.information(parent, "Nothing to print",
                                "Open a drawing first.")
        return

    dialog = PrintOptionsDialog(parent, canvas.paper_inches(),
                                sheet_count=_sheet_count(conv))
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

    if options.what == ALL_SHEETS and conv is not None:
        # A preview of eighteen sheets would hold all eighteen, rendered
        # at printer resolution, in memory at once. The printer dialog
        # (which also offers Microsoft Print to PDF) is the check instead.
        printer.setDocName(conv.filepath.stem)
        chooser = QPrintDialog(printer, parent)
        chooser.setWindowTitle("Print all sheets")
        if chooser.exec() != QDialog.DialogCode.Accepted:
            return
        from src import sheet_export
        progress = _progress(parent, "Printing…")
        try:
            job = sheet_export.SheetExport(conv, canvas, store, options,
                                           progress=progress)
            result = job.print_to(printer)
        finally:
            progress.close()
        _report(parent, result, "printed", None)
        return

    preview = QPrintPreviewDialog(printer, parent)
    preview.setWindowTitle("Print preview")
    preview.resize(1000, 750)
    preview.paintRequested.connect(
        lambda p: render_pages(canvas, p, options))
    preview.exec()


def default_pdf_name(conv, options: PrintOptions) -> str:
    """The file name the save dialog offers."""
    stem = conv.filepath.stem
    names = conv.sheet_names()
    if options.what != ALL_SHEETS and len(names) > 1:
        index = conv.current_sheet
        if 0 <= index < len(names):
            safe = "".join("_" if ch in '<>:"/\\|?*' else ch
                           for ch in names[index]).strip()
            stem = f"{stem} - {safe}" if safe else stem
    return stem + ".pdf"


def save_pdf(parent, canvas, conv, store=None) -> None:
    """Ask what to save, where, and write the PDF."""
    from PyQt6.QtCore import QSettings, QUrl
    from PyQt6.QtGui import QDesktopServices
    from PyQt6.QtWidgets import QFileDialog
    from pathlib import Path

    if conv is None or canvas.content_rect().isEmpty():
        QMessageBox.information(parent, "Nothing to save",
                                "Open a drawing first.")
        return

    dialog = PrintOptionsDialog(parent, canvas.paper_inches(),
                                sheet_count=_sheet_count(conv), for_pdf=True)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    options = dialog.options()

    settings = QSettings()
    folder = settings.value("export/pdfDir", "", type=str)
    if not folder or not Path(folder).is_dir():
        folder = str(conv.filepath.parent)
    suggested = str(Path(folder) / default_pdf_name(conv, options))
    path, _ = QFileDialog.getSaveFileName(parent, "Save as PDF", suggested,
                                          "PDF files (*.pdf)")
    if not path:
        return
    if not path.lower().endswith(".pdf"):
        path += ".pdf"
    settings.setValue("export/pdfDir", str(Path(path).parent))

    from src import sheet_export
    progress = _progress(parent, "Saving PDF…")
    try:
        job = sheet_export.SheetExport(conv, canvas, store, options,
                                       progress=progress)
        result = job.write_pdf(path, title=conv.filepath.stem)
    finally:
        progress.close()

    if _report(parent, result, "saved", path):
        box = QMessageBox(parent)
        box.setWindowTitle("PDF saved")
        pages = f"{result.pages} page{'s' if result.pages != 1 else ''}"
        box.setText(f"Saved {pages} to {Path(path).name}.")
        open_btn = box.addButton("Open PDF", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def _progress(parent, title: str):
    from PyQt6.QtWidgets import QProgressDialog
    progress = QProgressDialog("Preparing…", "Cancel", 0, 1, parent)
    progress.setWindowTitle(title)
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    progress.setMinimumWidth(380)
    progress.show()
    return progress


def _report(parent, result, verb: str, path) -> bool:
    """Tell the user about anything that went wrong. Returns True when
    the job finished and something was produced."""
    if result.cancelled:
        return False
    if result.error:
        QMessageBox.warning(parent, "Could not finish", result.error)
        return False
    if result.failed:
        names = ", ".join(result.failed[:6])
        more = len(result.failed) - 6
        if more > 0:
            names += f" and {more} more"
        QMessageBox.warning(
            parent, "Some sheets were left out",
            f"{len(result.failed)} sheet{'s' if len(result.failed) != 1 else ''} "
            f"could not be drawn and {'were' if len(result.failed) != 1 else 'was'} "
            f"left out: {names}.\n\nThe rest were {verb}.")
    return True
