"""
test_printing.py — page planning, text extraction and on-sheet search.

Runs standalone (`python tests/test_printing.py`) or under pytest, on
Qt's offscreen platform. Skips itself when PyQt6 is not installed; the
printing tests print to a PDF file, so no printer is needed either.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QRectF, QSizeF
    from PyQt6.QtWidgets import QApplication
except ImportError as exc:                            # pragma: no cover
    import unittest
    # Not always a missing PyQt6: on a headless box QtGui needs libEGL,
    # and saying so is the difference between a skip someone fixes and a
    # skip everyone reads as "no Qt here, nothing to do".
    raise unittest.SkipTest(f"Qt unavailable: {exc}")

from src import printing, textsearch                  # noqa: E402
from src.canvas import DrawingCanvas                  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _pump():
    _app.processEvents()


# A 34 x 22 inch sheet at 96 units per inch, the shape a DWFx arrives in.
SHEET_W_IN, SHEET_H_IN = 34.0, 22.0
SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="3264" height="2112" '
    'viewBox="0 0 3264 2112">'
    '<rect width="3264" height="2112" fill="#fff"/>'
    '<line x1="0" y1="0" x2="3264" y2="2112" stroke="#000"/>'
    '<text x="300" y="400" font-size="48">ZONE 4 CONVEYOR</text>'
    '<g transform="translate(1000 500)">'
    '  <text x="120" y="60" font-size="36">PRB-MA-V7</text>'
    '  <g transform="translate(200 300)">'
    '    <text x="0" y="0" font-size="24" textLength="300">TAKE-UP ASSEMBLY</text>'
    '  </g>'
    '</g>'
    '<text x="2600" y="2000" font-size="30">SHEET E-101</text>'
    '</svg>'
)


def _canvas(paper=(SHEET_W_IN, SHEET_H_IN)):
    c = DrawingCanvas()
    c.resize(1000, 700)
    c.show()
    _pump()
    c.load_svg(SVG)
    c.set_paper_inches(paper)
    c.set_text_index(textsearch.from_svg(SVG))
    _pump()
    return c


# ── page planning ────────────────────────────────────────────────────

def test_fit_puts_everything_on_one_centred_page():
    source = QRectF(0, 0, 3264, 2112)
    page = QSizeF(3300, 2550)            # letter landscape at 300 dpi
    pages = printing.plan_pages(source, page, 300.0, None,
                                printing.PrintOptions(mode=printing.FIT))
    assert len(pages) == 1
    target = pages[0].target
    assert target.width() <= page.width() + 0.5
    assert target.height() <= page.height() + 0.5
    # Centred: equal margins on the axis that did not fill.
    assert abs(target.left() - (page.width() - target.width()) / 2) < 0.5
    assert pages[0].source == source


def test_actual_size_means_actual_inches():
    source = QRectF(0, 0, 3264, 2112)                 # 34 x 22 in
    inches_per_scene = SHEET_W_IN / source.width()
    page = QSizeF(3400 * 300 / 300, 2400)             # big enough for the test
    page = QSizeF(34 * 300, 22 * 300)                 # exactly D size at 300dpi
    pages = printing.plan_pages(
        source, page, 300.0, inches_per_scene,
        printing.PrintOptions(mode=printing.SCALE, ratio=1.0))
    assert len(pages) == 1
    # 34 inches at 300 dpi is 10200 device pixels.
    assert abs(pages[0].target.width() - 34 * 300) < 1.0
    assert abs(pages[0].target.height() - 22 * 300) < 1.0


def test_half_size_is_half():
    source = QRectF(0, 0, 3264, 2112)
    ips = SHEET_W_IN / source.width()
    page = QSizeF(34 * 300, 22 * 300)
    full = printing.plan_pages(source, page, 300.0, ips,
                               printing.PrintOptions(mode=printing.SCALE,
                                                     ratio=1.0))
    half = printing.plan_pages(source, page, 300.0, ips,
                               printing.PrintOptions(mode=printing.SCALE,
                                                     ratio=0.5))
    assert abs(half[0].target.width() * 2 - full[0].target.width()) < 1.0


def test_a_d_sheet_at_full_size_tiles_across_letter_paper():
    source = QRectF(0, 0, 3264, 2112)
    ips = SHEET_W_IN / source.width()
    page = QSizeF(11 * 300, 8.5 * 300)                # letter landscape
    pages = printing.plan_pages(
        source, page, 300.0, ips,
        printing.PrintOptions(mode=printing.SCALE, ratio=1.0, tile=True))
    cols = max(p.column for p in pages) + 1
    rows = max(p.row for p in pages) + 1
    assert cols == 4 and rows == 3          # 34/11 -> 4, 22/8.5 -> 3
    assert len(pages) == 12
    # The tiles cover the sheet without overlapping.
    assert abs(min(p.source.left() for p in pages) - source.left()) < 1e-6
    assert abs(max(p.source.right() for p in pages) - source.right()) < 1e-6
    first, second = pages[0], pages[1]
    assert abs(first.source.right() - second.source.left()) < 1e-6
    # Edge tiles are short, and their target shrinks to match.
    last = pages[-1]
    assert last.target.width() <= page.width() + 0.5
    assert last.target.height() <= page.height() + 0.5


def test_tiling_off_crops_to_the_middle():
    source = QRectF(0, 0, 3264, 2112)
    ips = SHEET_W_IN / source.width()
    page = QSizeF(11 * 300, 8.5 * 300)
    pages = printing.plan_pages(
        source, page, 300.0, ips,
        printing.PrintOptions(mode=printing.SCALE, ratio=1.0, tile=False))
    assert len(pages) == 1
    got = pages[0].source
    assert got.width() < source.width()
    assert abs(got.center().x() - source.center().x()) < 1e-6
    assert abs(got.center().y() - source.center().y()) < 1e-6


def test_no_paper_size_means_fit_whatever_was_asked_for():
    source = QRectF(0, 0, 3264, 2112)
    page = QSizeF(3300, 2550)
    pages = printing.plan_pages(
        source, page, 300.0, None,
        printing.PrintOptions(mode=printing.SCALE, ratio=1.0))
    assert len(pages) == 1
    assert pages[0].target.width() <= page.width() + 0.5


def test_degenerate_input_plans_nothing():
    assert printing.plan_pages(QRectF(), QSizeF(100, 100), 300.0, None,
                               printing.PrintOptions()) == []
    assert printing.plan_pages(QRectF(0, 0, 10, 10), QSizeF(0, 0), 300.0,
                               None, printing.PrintOptions()) == []


def test_the_plan_describes_itself():
    source = QRectF(0, 0, 3264, 2112)
    ips = SHEET_W_IN / source.width()
    page = QSizeF(11 * 300, 8.5 * 300)
    options = printing.PrintOptions(mode=printing.SCALE, ratio=1.0)
    pages = printing.plan_pages(source, page, 300.0, ips, options)
    text = printing.describe_plan(pages, options, ips)
    assert "12 pages" in text and "4 across by 3 down" in text


# ── the printed page ─────────────────────────────────────────────────

def test_printing_produces_a_pdf_with_the_expected_page_count():
    from PyQt6.QtPrintSupport import QPrinter
    from PyQt6.QtGui import QPageSize, QPageLayout

    c = _canvas()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sheet.pdf"
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(out))
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
        printer.setPageOrientation(QPageLayout.Orientation.Landscape)

        n = printing.render_pages(
            c, printer, printing.PrintOptions(mode=printing.FIT))
        assert n == 1
        assert out.is_file() and out.stat().st_size > 1000

        out2 = Path(tmp) / "tiled.pdf"
        printer2 = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer2.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer2.setOutputFileName(str(out2))
        printer2.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
        printer2.setPageOrientation(QPageLayout.Orientation.Landscape)
        tiled = printing.render_pages(
            c, printer2,
            printing.PrintOptions(mode=printing.SCALE, ratio=1.0, tile=True))
        assert tiled > 1
        assert out2.stat().st_size > out.stat().st_size


def test_current_view_prints_less_than_the_whole_sheet():
    c = _canvas()
    whole = printing.source_rect(c, printing.PrintOptions(
        what=printing.WHOLE_SHEET))
    for _ in range(6):
        c._nav_zoom(1)
    _pump()
    view = printing.source_rect(c, printing.PrintOptions(
        what=printing.CURRENT_VIEW))
    assert view.width() < whole.width()


def test_inches_per_scene_unit_reflects_the_sheet():
    c = _canvas()
    ips = printing.inches_per_scene_unit(c)
    assert ips is not None
    assert abs(c.content_rect().width() * ips - SHEET_W_IN) < 0.01
    c.set_paper_inches(None)
    assert printing.inches_per_scene_unit(c) is None


def test_markup_can_be_left_off_the_print():
    from src import markup as mk
    c = _canvas()
    with tempfile.TemporaryDirectory() as tmp:
        drawing = Path(tmp) / "x.dwfx"
        drawing.write_bytes(b"x")
        store = mk.MarkupStore(drawing)
        c.set_markup_context(store, "0")
        c._add_markup(mk.Markup(kind=mk.BOX, points=[(0.2, 0.2), (0.6, 0.6)],
                                color="#ff3b30"))
        _pump()
        assert c.markup_count() == 1

        with_markup = c.render_region(c.content_rect())
        item = next(iter(c._mk_items.values()))
        assert item.isVisible()

        # render_scene puts it back the way it found it.
        from PyQt6.QtGui import QImage, QPainter
        img = QImage(400, 300, QImage.Format.Format_ARGB32)
        img.fill(0xFFFFFFFF)
        painter = QPainter(img)
        c.render_scene(painter, QRectF(0, 0, 400, 300), c.content_rect(),
                       include_markup=False)
        painter.end()
        assert item.isVisible(), "markup was left hidden after printing"

        def reds(image):
            n = 0
            for y in range(0, image.height(), 2):
                for x in range(0, image.width(), 2):
                    p = image.pixelColor(x, y)
                    if p.red() > 180 and p.green() < 90 and p.blue() < 90:
                        n += 1
            return n

        assert reds(with_markup) > 20
        assert reds(img) == 0


# ── text extraction ──────────────────────────────────────────────────

def test_svg_text_is_found_with_nested_transforms():
    index = textsearch.from_svg(SVG)
    assert len(index) == 4
    strings = {h.text for h in index.hits}
    assert strings == {"ZONE 4 CONVEYOR", "PRB-MA-V7", "TAKE-UP ASSEMBLY",
                       "SHEET E-101"}
    for hit in index.hits:
        assert 0.0 <= hit.x <= 1.0 and 0.0 <= hit.y <= 1.0
        assert hit.w > 0 and hit.h > 0

    # The nested one is offset by both transforms: x 1000+200+0, y 500+300+0.
    nested = next(h for h in index.hits if h.text == "TAKE-UP ASSEMBLY")
    assert abs(nested.x - 1200 / 3264) < 0.01
    assert abs(nested.y - (800 - 24) / 2112) < 0.01


def test_search_is_case_insensitive_and_in_reading_order():
    index = textsearch.from_svg(SVG)
    assert [h.text for h in index.search("zone")] == ["ZONE 4 CONVEYOR"]
    assert index.search("prb-ma-v7")[0].text == "PRB-MA-V7"
    assert index.search("") == []
    assert index.search("nothing here") == []

    both = index.search("e")           # several hits, top to bottom
    ys = [h.y for h in both]
    assert ys == sorted(ys)


def test_svg_without_text_yields_an_empty_index():
    plain = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
             '<line x1="0" y1="0" x2="10" y2="10"/></svg>')
    assert len(textsearch.from_svg(plain)) == 0
    assert len(textsearch.from_svg("")) == 0
    assert len(textsearch.from_svg("<svg><text>no viewbox</text></svg>")) == 0


def test_classic_dwf_text_maps_through_the_view_box():
    class Fake:
        view = (0, 0, 1000, 500)
        texts = [(0, 500, "TOP LEFT"), (1000, 0, "BOTTOM RIGHT"),
                 (500, 250, "MIDDLE"), (10, 10, "   ")]

    index = textsearch.from_classic_geometry(Fake())
    assert len(index) == 3               # the blank one is dropped
    top = next(h for h in index.hits if h.text == "TOP LEFT")
    assert abs(top.x) < 1e-6 and top.y < 0.05
    mid = next(h for h in index.hits if h.text == "MIDDLE")
    assert abs(mid.x - 0.5) < 1e-6 and abs(mid.y + mid.h - 0.5) < 1e-6


def test_no_geometry_means_no_index():
    class Empty:
        view = None
        texts = []
    assert len(textsearch.from_classic_geometry(Empty())) == 0


# ── searching on the canvas ──────────────────────────────────────────

def test_find_walks_the_matches_and_wraps():
    c = _canvas()
    assert c.text_index_size() == 4
    pos, total = c.find_text("e")
    assert total >= 2 and pos == 1
    seen = [pos]
    for _ in range(total - 1):
        pos, _ = c.find_text("e")
        seen.append(pos)
    assert seen == list(range(1, total + 1))
    pos, _ = c.find_text("e")
    assert pos == 1                        # wrapped


def test_find_moves_the_view_onto_the_hit():
    c = _canvas()
    c.fit_to_view()
    _pump()
    pos, total = c.find_text("SHEET E-101")
    assert (pos, total) == (1, 1)
    hit = c._text_matches[0]
    rect = c._hit_rect(hit)
    visible = c.visible_scene_rect()
    assert visible.contains(rect.center())
    assert c._text_marker is not None      # highlighted


def test_find_backwards():
    c = _canvas()
    _, total = c.find_text("e")
    back, _ = c.find_text("e", forward=False)
    assert back == total                   # stepped back past the start


def test_a_miss_reports_nothing_and_clears_the_marker():
    c = _canvas()
    c.find_text("ZONE")
    assert c._text_marker is not None
    assert c.find_text("no such tag") == (0, 0)
    assert c._text_marker is None


def test_clearing_the_search_removes_the_highlight():
    c = _canvas()
    c.find_text("ZONE")
    c.clear_text_search()
    assert c._text_marker is None
    assert c._text_pos == -1


def test_a_drawing_with_no_index_searches_safely():
    c = DrawingCanvas()
    c.resize(400, 300)
    c.show()
    _pump()
    c.load_svg(SVG)
    _pump()
    assert c.text_index_size() == 0
    assert c.find_text("anything") == (0, 0)


# ── the window puts it together ──────────────────────────────────────

def _window():
    from src.main_window import MainWindow
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    _pump()
    w._canvas.load_svg(SVG)
    w._canvas.set_paper_inches((SHEET_W_IN, SHEET_H_IN))
    w._canvas.set_text_index(textsearch.from_svg(SVG))
    w._update_find_status()
    _pump()
    return w


def test_the_find_bar_walks_and_reports():
    w = _window()
    assert not w._find_bar.isVisible()
    w._show_find()
    _pump()
    assert w._find_bar.isVisible()
    assert "4 labels" in w._find_label.text()

    w._find_edit.setText("prb-ma")
    w._find_step(True)
    assert w._find_label.text() == "1 of 1"

    w._find_edit.setText("nothing here")
    w._find_step(True)
    assert w._find_label.text() == "No match"

    w._hide_find()
    _pump()
    assert not w._find_bar.isVisible()
    assert w._canvas._text_marker is None


def test_escape_in_the_find_field_does_not_steal_the_canvas_key():
    """Escape must still cancel a markup tool when the drawing has focus."""
    from PyQt6.QtCore import Qt as _Qt
    w = _window()
    w._build_find_bar.__self__     # sanity: the bar exists
    actions = [a for a in w._find_edit.actions()
               if a.shortcut().toString() == "Esc"]
    assert actions, "no Escape action on the find field"
    assert (actions[0].shortcutContext()
            == _Qt.ShortcutContext.WidgetWithChildrenShortcut)
    # And no window-level Escape was registered.
    assert not any(a.shortcut().toString() == "Esc" for a in w.actions())


def test_print_needs_a_drawing_but_never_raises():
    from src.main_window import MainWindow
    w = MainWindow()
    w.resize(800, 600)
    w.show()
    _pump()
    assert w._canvas.content_rect().isEmpty()
    # print_drawing bails out with a message box rather than a traceback;
    # calling the pieces it would use must be safe on an empty canvas.
    assert printing.inches_per_scene_unit(w._canvas) is None
    assert printing.plan_pages(printing.source_rect(
        w._canvas, printing.PrintOptions()), QSizeF(100, 100), 300.0,
        None, printing.PrintOptions()) == []


# ── standalone runner ────────────────────────────────────────────────

def _main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {exc}")
        else:
            print(f"  ok   {name}")
    print("all passed" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
