"""
test_sheet_export.py — every sheet of a drawing set into one PDF.

Builds real classic-DWF and DWFx sets, exports them, and reads the PDF
back with nothing but zlib and regular expressions: page count, page
sizes, that classic sheets arrive as vector linework rather than as a
JPEG, and that each sheet's redlines land on that sheet's page.

Runs on Qt's offscreen platform; skips itself when PyQt6 is unavailable.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QRectF
    from PyQt6.QtWidgets import QApplication
except ImportError as exc:                            # pragma: no cover
    import unittest
    raise unittest.SkipTest(f"Qt unavailable: {exc}")

from tests.test_converter_dwf import (                # noqa: E402
    build_set, _sandbox, TWO_LAYERS, ONE_LAYER, HAVE_RENDER,
)
from tests.test_converter_dwfx import DWGConverter    # noqa: E402
from tests.test_dwfx import build_package             # noqa: E402
from src import printing, sheet_export                # noqa: E402
from src import markup as mk                          # noqa: E402
from src.canvas import DrawingCanvas                  # noqa: E402

_app = QApplication.instance() or QApplication([])

# The test streams declare no PlotInfo, so a drawing unit is 1/1200 in
# and their 1000-unit view is a 0.833 in square.
SHEET_IN = 1000 / 1200


# ── reading a PDF back without a PDF library ─────────────────────────

def _pages(data: bytes) -> list[tuple[float, float]]:
    """Page sizes in inches, in page order."""
    boxes = re.findall(rb"/MediaBox \[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]",
                       data)
    return [((float(x1) - float(x0)) / 72.0, (float(y1) - float(y0)) / 72.0)
            for x0, y0, x1, y1 in boxes]


def _streams(data: bytes) -> list[bytes]:
    out = []
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        try:
            out.append(zlib.decompress(data[start:end]))
        except zlib.error:
            continue
    return out


def _page_contents(data: bytes) -> list[bytes]:
    """Decompressed content streams that draw something, in order."""
    return [s for s in _streams(data) if re.search(rb"\b(re|l|m)\b", s)]


# ── page planning for a PDF ──────────────────────────────────────────

SOURCE = QRectF(0, 0, 3264, 2112)          # 34 x 22 in at 96 / in
IPS = 34.0 / 3264


def test_same_size_gives_one_true_size_page():
    pages = printing.plan_pdf_pages(SOURCE, IPS, printing.PrintOptions(), 600)
    assert len(pages) == 1
    (w, h), layout = pages[0]
    assert abs(w - 34) < 1e-6 and abs(h - 22) < 1e-6
    assert abs(layout.target.width() - 34 * 600) < 0.5


def test_same_size_at_half_scale_is_a_half_size_page():
    opts = printing.PrintOptions(mode=printing.SCALE, ratio=0.5)
    (w, h), _ = printing.plan_pdf_pages(SOURCE, IPS, opts, 600)[0]
    assert abs(w - 17) < 1e-6 and abs(h - 11) < 1e-6


def test_fixed_paper_turns_to_the_sheet_and_keeps_a_margin():
    opts = printing.PrintOptions(paper=(8.5, 11.0))
    pages = printing.plan_pdf_pages(SOURCE, IPS, opts, 600)
    assert len(pages) == 1
    (w, h), layout = pages[0]
    assert (w, h) == (11.0, 8.5)                # landscape, like the sheet
    m = printing.PDF_MARGIN_IN * 600
    assert layout.target.left() >= m - 0.5
    assert layout.target.top() >= m - 0.5
    assert layout.target.right() <= 11 * 600 - m + 0.5
    assert layout.target.bottom() <= 8.5 * 600 - m + 0.5

    tall = printing.plan_pdf_pages(QRectF(0, 0, 100, 300), None, opts, 600)
    assert tall[0][0] == (8.5, 11.0)            # portrait for a tall one


def test_fixed_paper_at_scale_tiles():
    opts = printing.PrintOptions(paper=(11.0, 17.0), mode=printing.SCALE,
                                 ratio=1.0, tile=True)
    pages = printing.plan_pdf_pages(SOURCE, IPS, opts, 300)
    # 34 x 22 in on 16.5 x 10.5 in of printable tabloid: 3 x 3.
    assert len(pages) == 9
    assert all(size == (17.0, 11.0) for size, _ in pages)


def test_no_sheet_size_falls_back_to_tabloid():
    pages = printing.plan_pdf_pages(SOURCE, None, printing.PrintOptions(), 600)
    assert pages[0][0] == (17.0, 11.0)


def test_default_name_names_the_sheet_only_when_saving_one():
    class Conv:
        filepath = Path("C:/plans/Line 4.dwf")
        current_sheet = 1

        def sheet_names(self):
            return ["E-100 Cover", "E-101 Power/Controls"]

    one = printing.PrintOptions(what=printing.WHOLE_SHEET)
    every = printing.PrintOptions(what=printing.ALL_SHEETS)
    assert printing.default_pdf_name(Conv(), every) == "Line 4.pdf"
    assert printing.default_pdf_name(Conv(), one) == \
        "Line 4 - E-101 Power_Controls.pdf"


# ── the dialog ───────────────────────────────────────────────────────

def test_pdf_dialog_defaults_to_the_whole_set():
    d = printing.PrintOptionsDialog(None, (34.0, 22.0), sheet_count=18,
                                    for_pdf=True)
    opts = d.options()
    assert opts.what == printing.ALL_SHEETS
    assert opts.paper is None                    # same size as each sheet
    assert opts.mode == printing.FIT


def test_print_dialog_still_defaults_to_this_sheet():
    d = printing.PrintOptionsDialog(None, (34.0, 22.0), sheet_count=18)
    assert d.options().what == printing.WHOLE_SHEET
    d._rb_all.setChecked(True)
    assert d.options().what == printing.ALL_SHEETS
    assert d._ok.text() == "Print…"              # no preview for a set


def test_single_sheet_dialog_offers_no_set_option():
    d = printing.PrintOptionsDialog(None, (34.0, 22.0), sheet_count=1,
                                    for_pdf=True)
    d._rb_all.setChecked(True)                   # not on the dialog
    assert d.options().what == printing.WHOLE_SHEET


def test_pdf_dialog_without_a_sheet_size_offers_paper_only():
    d = printing.PrintOptionsDialog(None, None, for_pdf=True)
    values = [d._paper_box.itemData(i) for i in range(d._paper_box.count())]
    assert None not in values
    assert d.options().paper == printing.FALLBACK_PAPER


# ── exporting real sets ──────────────────────────────────────────────

class _Progress:
    """QProgressDialog's surface, recorded; can cancel after n sheets."""

    def __init__(self, cancel_after: int | None = None):
        self.labels = []
        self.value = 0
        self.maximum = 0
        self._cancel_after = cancel_after

    def setLabelText(self, text):
        self.labels.append(text)

    def setValue(self, v):
        self.value = v

    def setMaximum(self, v):
        self.maximum = v

    def wasCanceled(self):
        return self._cancel_after is not None and self.value >= self._cancel_after


def _classic_set(tmp: Path) -> DWGConverter:
    _sandbox(tmp)
    path = build_set(tmp / "set.dwf", [("Plan", TWO_LAYERS),
                                       ("Detail", ONE_LAYER),
                                       ("Grid", ONE_LAYER)])
    conv = DWGConverter(path)
    conv.load()
    return conv


def test_a_classic_set_becomes_one_vector_pdf(tmp_path=None):
    if not HAVE_RENDER:
        return
    tmp = Path(tmp_path or tempfile.mkdtemp())
    conv = _classic_set(tmp)
    out = tmp / "set.pdf"
    progress = _Progress()
    job = sheet_export.SheetExport(
        conv, DrawingCanvas(), None,
        printing.PrintOptions(what=printing.ALL_SHEETS), progress=progress)
    result = job.write_pdf(out)

    assert result.error is None and not result.failed
    assert result.pages == 3 and result.sheets == 3
    data = out.read_bytes()
    sizes = _pages(data)
    assert len(sizes) == 3
    for w, h in sizes:                           # true sheet size
        assert abs(w - SHEET_IN) < 0.01 and abs(h - SHEET_IN) < 0.01
    # Linework, not a picture of linework.
    assert b"/DCTDecode" not in data
    assert b"/Subtype /Image" not in data
    assert progress.maximum == 3 and progress.value == 3
    assert any("Detail" in label for label in progress.labels)
    # Nothing left behind but the PDF itself.
    assert sorted(p.name for p in tmp.glob("*.pdf")) == ["set.pdf"]
    assert not list(tmp.glob(".*part*"))


def test_each_sheets_redlines_land_on_its_own_page(tmp_path=None):
    if not HAVE_RENDER:
        return
    tmp = Path(tmp_path or tempfile.mkdtemp())
    conv = _classic_set(tmp)
    store = mk.MarkupStore(conv.filepath)
    # A green box on the second sheet only.
    store.set_sheet("Detail", [mk.Markup(kind=mk.BOX, points=[(0.2, 0.2), (0.8, 0.8)],
                                         color="#00ff00")])
    out = tmp / "marked.pdf"
    job = sheet_export.SheetExport(
        conv, DrawingCanvas(), store,
        printing.PrintOptions(what=printing.ALL_SHEETS))
    assert job.write_pdf(out).pages == 3

    contents = _page_contents(out.read_bytes())
    green = [b"0 1 0 SCN" in c for c in contents]
    assert green.count(True) == 1, green
    assert green.index(True) == 1

    # And without markup, nowhere.
    job = sheet_export.SheetExport(
        conv, DrawingCanvas(), store,
        printing.PrintOptions(what=printing.ALL_SHEETS, markup=False))
    job.write_pdf(out)
    assert not any(b"0 1 0 SCN" in c for c in _page_contents(out.read_bytes()))


def test_this_sheet_only_uses_the_sheet_on_screen(tmp_path=None):
    if not HAVE_RENDER:
        return
    tmp = Path(tmp_path or tempfile.mkdtemp())
    conv = _classic_set(tmp)
    conv.set_sheet(2)
    out = tmp / "one.pdf"
    job = sheet_export.SheetExport(conv, DrawingCanvas(), None,
                                   printing.PrintOptions())
    result = job.write_pdf(out)
    assert result.pages == 1 and job.indices == [2]
    assert len(_pages(out.read_bytes())) == 1


def test_fixed_paper_puts_every_sheet_on_it(tmp_path=None):
    if not HAVE_RENDER:
        return
    tmp = Path(tmp_path or tempfile.mkdtemp())
    conv = _classic_set(tmp)
    out = tmp / "letter.pdf"
    job = sheet_export.SheetExport(
        conv, DrawingCanvas(), None,
        printing.PrintOptions(what=printing.ALL_SHEETS, paper=(8.5, 11.0)))
    assert job.write_pdf(out).pages == 3
    for w, h in _pages(out.read_bytes()):
        assert {round(w, 2), round(h, 2)} == {8.5, 11.0}


def test_cancelling_leaves_no_file(tmp_path=None):
    if not HAVE_RENDER:
        return
    tmp = Path(tmp_path or tempfile.mkdtemp())
    conv = _classic_set(tmp)
    out = tmp / "cancelled.pdf"
    job = sheet_export.SheetExport(
        conv, DrawingCanvas(), None,
        printing.PrintOptions(what=printing.ALL_SHEETS),
        progress=_Progress(cancel_after=1))
    result = job.write_pdf(out)
    assert result.cancelled
    assert not out.exists()
    assert not list(tmp.glob(".*part*"))


def test_an_existing_pdf_survives_a_failed_export(tmp_path=None):
    if not HAVE_RENDER:
        return
    tmp = Path(tmp_path or tempfile.mkdtemp())
    conv = _classic_set(tmp)
    out = tmp / "keep.pdf"
    out.write_bytes(b"%PDF-1.4 the good one")
    job = sheet_export.SheetExport(
        conv, DrawingCanvas(), None,
        printing.PrintOptions(what=printing.ALL_SHEETS),
        progress=_Progress(cancel_after=2))
    assert job.write_pdf(out).cancelled
    assert out.read_bytes() == b"%PDF-1.4 the good one"


def test_a_damaged_sheet_is_left_out_not_fatal(tmp_path=None):
    if not HAVE_RENDER:
        return
    tmp = Path(tmp_path or tempfile.mkdtemp())
    _sandbox(tmp)
    path = build_set(tmp / "bad.dwf", [("Good", ONE_LAYER),
                                       ("Broken", b"not a w2d stream"),
                                       ("Also good", TWO_LAYERS)])
    conv = DWGConverter(path)
    conv.load()
    out = tmp / "bad.pdf"
    job = sheet_export.SheetExport(
        conv, DrawingCanvas(), None,
        printing.PrintOptions(what=printing.ALL_SHEETS))
    result = job.write_pdf(out)
    assert result.failed == ["Broken"]
    assert result.pages == 2
    assert len(_pages(out.read_bytes())) == 2


def test_a_dwfx_set_exports_every_page_at_its_size(tmp_path=None):
    tmp = Path(tmp_path or tempfile.mkdtemp())
    path = build_package(tmp / "set.dwfx")
    conv = DWGConverter(path)
    conv.load()
    assert conv.sheet_count == 2
    expected = []
    for i in range(2):
        conv.set_sheet(i)
        expected.append(conv.paper_size_inches())
    conv.set_sheet(0)

    out = tmp / "set.pdf"
    job = sheet_export.SheetExport(
        conv, DrawingCanvas(), None,
        printing.PrintOptions(what=printing.ALL_SHEETS))
    result = job.write_pdf(out)
    assert result.error is None and not result.failed, result
    sizes = _pages(out.read_bytes())
    assert len(sizes) == 2
    for (w, h), (ew, eh) in zip(sizes, expected):
        assert abs(w - ew) < 0.01 and abs(h - eh) < 0.01


def test_live_sheet_for_a_single_drawing():
    """DWG, DXF and single-sheet DWFx print what is on screen."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="960" height="480" '
           'viewBox="0 0 960 480"><line x1="0" y1="0" x2="960" y2="480" '
           'stroke="#000"/></svg>')
    live = DrawingCanvas()
    live.resize(800, 600)
    live.load_svg(svg)
    live.set_paper_inches((10.0, 5.0))

    class Conv:
        filepath = Path(tempfile.mkdtemp()) / "plan.dxf"
        sheet_count = 1
        is_raster = False
        is_dwfx = False
        current_sheet = 0
        geometry = None

        def sheet_names(self):
            return []

        def hidden_layers(self):
            return frozenset()

    out = Conv.filepath.with_suffix(".pdf")
    job = sheet_export.SheetExport(Conv(), live, None, printing.PrintOptions())
    assert job.write_pdf(out).pages == 1
    (w, h), = _pages(out.read_bytes())
    assert abs(w - 10) < 0.01 and abs(h - 5) < 0.01


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))


# ── the vector painter agrees with the raster ────────────────────────

def _geometry():
    from src.w2d import GeometryCollector
    from src.w2d_render import SheetGeometry
    c = GeometryCollector()
    c.set_color((255, 0, 0, 255))
    c.polyline([(100, 100), (900, 100), (900, 900)])     # red L
    c.set_color((0, 0, 0, 255))
    c.arc(500, 500, 300, 0.0, 90.0)                      # upper-right quarter
    c.set_color((0, 0, 255, 255))
    c.polytriangle([(150, 700), (350, 700), (150, 850)])  # blue, upper-left
    return SheetGeometry(c, (0, 0, 1000, 1000), 1 / 1200)


def _ink_by_quadrant(img):
    import numpy as np
    a = np.asarray(img.convert("RGB")).astype(int)
    dark = a.min(axis=2) < 128
    h, w = dark.shape
    return [int(dark[:h // 2, :w // 2].sum()), int(dark[:h // 2, w // 2:].sum()),
            int(dark[h // 2:, :w // 2].sum()), int(dark[h // 2:, w // 2:].sum())]


def test_vectors_draw_what_the_raster_draws():
    if not HAVE_RENDER:
        return
    import io
    import numpy as np
    from PIL import Image
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtGui import QImage, QPainter, QColor
    from src import w2d_vector

    geom = _geometry()
    raster = geom.render_region((0, 0, 1000, 1000), 400, 400)

    img = QImage(400, 400, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    assert w2d_vector.paint_geometry(p, geom, (0, 0, 1000, 1000),
                                     QRectF(0, 0, 400, 400), line_width=3.0)
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    vec = Image.open(io.BytesIO(bytes(buf.data())))

    # The same things in the same places: the arc in the upper right
    # (counter-clockwise from east, as on the raster), the triangle in
    # the upper left, the L along the bottom and right.
    rq, vq = _ink_by_quadrant(raster), _ink_by_quadrant(vec)
    for r, v in zip(rq, vq):
        assert (r == 0) == (v == 0), (rq, vq)
    assert vq[1] > 0 and vq[0] > 0
    a = np.asarray(vec.convert("RGB")).astype(int)
    red = (a[:, :, 0] > 200) & (a[:, :, 1] < 80) & (a[:, :, 2] < 80)
    blue = (a[:, :, 2] > 200) & (a[:, :, 0] < 80) & (a[:, :, 1] < 80)
    assert red.sum() > 100 and blue.sum() > 100
    # Red L: along the bottom edge (y=100 is near the bottom of the page).
    assert red[350:, :].sum() > red[:50, :].sum()


def test_hidden_layers_stay_hidden_in_vectors():
    if not HAVE_RENDER:
        return
    from PyQt6.QtGui import QImage, QPainter, QColor
    from src.w2d import GeometryCollector
    from src.w2d_render import SheetGeometry
    from src import w2d_vector
    c = GeometryCollector()
    c.set_layer(1, "KEEP")
    c.polyline([(100, 100), (900, 100)])
    c.set_layer(2, "HIDE")
    c.polyline([(100, 900), (900, 900)])
    geom = SheetGeometry(c, (0, 0, 1000, 1000), 1 / 1200)

    def ink(hidden):
        img = QImage(200, 200, QImage.Format.Format_RGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        w2d_vector.paint_geometry(p, geom, (0, 0, 1000, 1000),
                                  QRectF(0, 0, 200, 200), hidden, 2.0)
        p.end()
        top = sum(QColor(img.pixel(x, 20)).value() < 128 for x in range(200))
        bottom = sum(QColor(img.pixel(x, 180)).value() < 128 for x in range(200))
        return top, bottom

    top, bottom = ink(frozenset())
    assert top > 50 and bottom > 50
    top, bottom = ink(frozenset({"HIDE"}))
    assert top == 0 and bottom > 50


def test_a_set_prints_every_sheet_to_a_printer(tmp_path=None):
    """The printer path, with Qt's PDF printer standing in for paper."""
    if not HAVE_RENDER:
        return
    from PyQt6.QtPrintSupport import QPrinter
    tmp = Path(tmp_path or tempfile.mkdtemp())
    conv = _classic_set(tmp)
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(tmp / "printed.pdf"))
    job = sheet_export.SheetExport(
        conv, DrawingCanvas(), None,
        printing.PrintOptions(what=printing.ALL_SHEETS))
    result = job.print_to(printer)
    assert result.pages == 3 and result.error is None
    assert len(_pages((tmp / "printed.pdf").read_bytes())) == 3


def test_the_window_offers_save_as_pdf():
    from src.main_window import MainWindow
    w = MainWindow()
    assert w._act_pdf.shortcut().toString() == "Ctrl+Shift+P"
    menu = w._print_btn.menu()
    # The same actions as File ▸ Print / Save as PDF, so the same labels.
    assert [a.text().replace("&", "") for a in menu.actions()] == \
        ["Print…", "Save as PDF…"]
    assert w._print_btn.text() == "🖨  Print"
    w.close()
