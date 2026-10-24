"""
sheet_export.py — every sheet of a drawing set to PDF, or to the printer.

Printing used to mean "the sheet on screen". A published DWF set is
eighteen sheets, and paging through them one print preview at a time is
not how anyone wants to hand a set to a field tech. This puts any number
of sheets into one job without touching what is on screen:

* A background thread (SheetPreparer) gets each sheet ready — SVG for a
  DWFx sheet, decoded geometry for a classic DWF sheet — no more than
  one sheet ahead of the page being drawn, so an eighteen-sheet set
  never holds eighteen sheets' geometry at once.
* The UI thread loads each one into an offscreen DrawingCanvas, with
  that sheet's redlines, and paints it with the same render_scene and
  page planning that ordinary printing uses.

Classic DWF sheets are painted as vectors (see w2d_vector) rather than
from their raster. For a PDF that is the difference between crisp
linework and a JPEG of linework; it also means no sheet has to be
rasterised just to be exported, which for a busy sheet that has never
been opened would be a minute each.

DWG and DXF have one sheet (model space) and come straight off the
canvas on screen, as does a single-sheet DWFx.
"""
from __future__ import annotations

import io
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QThread, QRectF, QSizeF, QMarginsF
from PyQt6.QtGui import QPainter, QPageLayout, QPageSize
from PyQt6.QtWidgets import QApplication

from src import printing

# PDF device resolution. Only sets the precision of coordinates: what
# is drawn is vectors, so this costs nothing in file size.
PDF_DPI = 600


@dataclass
class PreparedSheet:
    """One sheet, decoded and ready to load into a canvas."""
    index: int
    name: str
    svg: str | None = None
    geometry: object | None = None
    extents: tuple | None = None
    paper: tuple[float, float] | None = None
    hidden: frozenset = frozenset()
    live: bool = False          # use the canvas on screen as it is
    error: str | None = None


@dataclass
class ExportResult:
    pages: int = 0
    sheets: int = 0
    failed: list[str] = field(default_factory=list)
    cancelled: bool = False
    error: str | None = None


def markup_key(names: list[str], index: int) -> str:
    """The key a sheet's redlines are stored under — the same rule the
    main window uses when it attaches markup to the sheet on screen."""
    if 0 <= index < len(names):
        return names[index]
    return "0"


# ------------------------------------------------------------------ #
#  Getting sheets ready (any thread)
# ------------------------------------------------------------------ #

def _classic_paper(geom) -> tuple[tuple, tuple[float, float]]:
    x0, y0, x1, y1 = geom.view
    per_unit = geom.inches_per_unit
    w_in = (x1 - x0) * per_unit
    h_in = (y1 - y0) * per_unit
    return (0.0, h_in, w_in, -h_in), (w_in, h_in)


class SheetSource:
    """What an export needs to know about the drawing that is open.

    Captured on the UI thread when the job starts, so the background
    thread never reads the live converter's changing state.
    """

    def __init__(self, conv, live_index: int | None = None):
        self.filepath = Path(conv.filepath)
        self.names = list(conv.sheet_names())
        self.count = max(1, conv.sheet_count)
        self.classic = bool(conv.is_raster)
        self.dwfx = conv.is_dwfx
        self.live_index = conv.current_sheet if live_index is None else live_index
        self.live_hidden = frozenset(conv.hidden_layers())
        # Geometry the screen already decoded for the sheet it shows —
        # reused rather than decoding the same sheet twice.
        self.live_geometry = conv.geometry if self.classic else None
        self._conv = None       # the export's own converter, made lazily

    def name(self, index: int) -> str:
        return self.names[index] if 0 <= index < len(self.names) else \
            self.filepath.stem

    def prepare(self, index: int, cancel=None) -> PreparedSheet:
        sheet = PreparedSheet(index, self.name(index))
        current = index == self.live_index
        try:
            if self.classic:
                from src import w2d_render
                geom = self.live_geometry if current else None
                if geom is None:
                    geom = w2d_render.decode_geometry(self.filepath, index,
                                                      cancel=cancel)
                sheet.geometry = geom
                sheet.extents, sheet.paper = _classic_paper(geom)
                sheet.hidden = self.live_hidden if current else frozenset()
            elif self.dwfx and self.count > 1:
                conv = self._own_converter()
                conv.set_sheet(index)
                if current:
                    for name in self.live_hidden:
                        conv.set_layer_visible(name, False)
                sheet.svg = conv.render_svg()
                sheet.paper = conv.paper_size_inches()
                sheet.hidden = self.live_hidden if current else frozenset()
            else:
                sheet.live = True
        except Exception as exc:
            from src.w2d import Cancelled
            if isinstance(exc, Cancelled):
                raise
            sheet.error = str(exc) or type(exc).__name__
        return sheet

    def _own_converter(self):
        if self._conv is None:
            from src.converter import DWGConverter
            conv = DWGConverter(self.filepath)
            conv.load()
            self._conv = conv
        return self._conv

    def close(self) -> None:
        if self._conv is not None:
            try:
                self._conv.close()
            except Exception:
                pass
            self._conv = None


class SheetPreparer(QThread):
    """Prepares sheets in order, at most `ahead` in front of the painter.

    Results arrive on `results` (a queue.Queue); None marks the end. The
    painter calls done_with() after each sheet to let the next one start.
    """

    def __init__(self, source: SheetSource, indices: list[int], ahead: int = 1):
        super().__init__()
        self._source = source
        self._indices = list(indices)
        self._slots = threading.Semaphore(max(1, ahead))
        self.results: queue.Queue = queue.Queue()
        self.cancel = threading.Event()

    def done_with(self) -> None:
        self._slots.release()

    def stop(self) -> None:
        self.cancel.set()
        self._slots.release()

    def run(self):
        try:
            for index in self._indices:
                while not self._slots.acquire(timeout=0.1):
                    if self.cancel.is_set():
                        return
                if self.cancel.is_set():
                    return
                try:
                    sheet = self._source.prepare(index, cancel=self.cancel)
                except BaseException:
                    return          # cancelled mid-decode
                self.results.put(sheet)
        finally:
            self.results.put(None)
            self._source.close()


# ------------------------------------------------------------------ #
#  Turning a prepared sheet into something render_scene can paint
# ------------------------------------------------------------------ #

def _blank_png(aspect: float, width: int = 2000) -> bytes:
    """A white stand-in for the raster. The canvas wants a base picture
    to size the scene and place redlines by; the vectors replace it."""
    from PIL import Image
    height = max(1, int(round(width * aspect)))
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(
        buf, "PNG", compress_level=1)
    return buf.getvalue()


def build_canvas(sheet: PreparedSheet, store, key: str):
    """An offscreen canvas holding one prepared sheet, or None."""
    from src.canvas import DrawingCanvas
    from src import w2d_vector

    canvas = DrawingCanvas()
    canvas.resize(1200, 900)
    if sheet.svg:
        canvas.load_svg(sheet.svg)
    elif sheet.geometry is not None:
        x0, y0, x1, y1 = sheet.geometry.view
        if x1 <= x0 or y1 <= y0:
            return None
        if not canvas.load_image(_blank_png((y1 - y0) / (x1 - x0)),
                                 sheet.extents):
            return None
        canvas.set_vector_painter(
            w2d_vector.vector_painter(sheet.geometry, sheet.hidden))
    else:
        return None
    if canvas.content_rect().isEmpty():
        return None
    canvas.set_paper_inches(sheet.paper)
    canvas.set_markup_context(store, key)
    return canvas


def _dispose(canvas) -> None:
    try:
        canvas.set_vector_painter(None)
        canvas.set_markup_context(None, "0")
        canvas.clear()
        canvas.deleteLater()
    except RuntimeError:
        pass


def view_source(live_canvas, canvas, options) -> QRectF:
    """What part of `canvas` to put on paper.

    "What is on screen now" is a rectangle in the live canvas's scene,
    which for a classic DWF sheet is not the scene of the canvas being
    exported (that holds a stand-in of a different pixel size), so it is
    carried across as a fraction of the sheet.
    """
    content = canvas.content_rect()
    if options.what != printing.CURRENT_VIEW or live_canvas is None:
        return content
    live = live_canvas.content_rect()
    visible = live_canvas.visible_scene_rect().intersected(live)
    if live.isEmpty() or visible.isEmpty():
        return content
    fx0 = (visible.left() - live.left()) / live.width()
    fy0 = (visible.top() - live.top()) / live.height()
    fx1 = (visible.right() - live.left()) / live.width()
    fy1 = (visible.bottom() - live.top()) / live.height()
    return QRectF(content.left() + fx0 * content.width(),
                  content.top() + fy0 * content.height(),
                  (fx1 - fx0) * content.width(),
                  (fy1 - fy0) * content.height())


# ------------------------------------------------------------------ #
#  The job
# ------------------------------------------------------------------ #

class SheetExport:
    """Runs one export: prepares sheets in the background and paints
    them on this thread, keeping the UI responsive in between.

    `progress` is optional and duck-typed (QProgressDialog fits):
    setLabelText, setValue, setMaximum, wasCanceled.
    """

    def __init__(self, conv, live_canvas, store, options,
                 indices: list[int] | None = None, progress=None):
        self.source = SheetSource(conv)
        self.live_canvas = live_canvas
        self.store = store
        self.options = options
        if indices is None:
            indices = (list(range(self.source.count))
                       if options.what == printing.ALL_SHEETS
                       else [self.source.live_index])
        self.indices = indices
        self.progress = progress

    # -- the loop both outputs share ----------------------------------

    def _sheets(self, result: ExportResult):
        """Yield (sheet, canvas) in order; cleans up after each."""
        preparer = SheetPreparer(self.source, self.indices)
        total = len(self.indices)
        if self.progress is not None:
            self.progress.setMaximum(total)
            self.progress.setValue(0)
        preparer.start()
        done = 0
        try:
            while True:
                if self.progress is not None:
                    index = self.indices[min(done, total - 1)]
                    self.progress.setLabelText(
                        f"Sheet {done + 1} of {total} — "
                        f"{self.source.name(index)}")
                sheet = self._next(preparer, result)
                if sheet is None:
                    return
                canvas = None
                try:
                    if sheet.error:
                        result.failed.append(sheet.name)
                        continue
                    if sheet.live:
                        canvas = self.live_canvas
                    else:
                        canvas = build_canvas(
                            sheet, self.store,
                            markup_key(self.source.names, sheet.index))
                    if canvas is None:
                        result.failed.append(sheet.name)
                        continue
                    yield sheet, canvas
                    result.sheets += 1
                finally:
                    if canvas is not None and canvas is not self.live_canvas:
                        _dispose(canvas)
                    sheet.geometry = None
                    done += 1
                    if self.progress is not None:
                        self.progress.setValue(done)
                    preparer.done_with()
        finally:
            preparer.stop()
            # Never leave a decode running behind a closed dialog; it
            # notices the cancel within milliseconds.
            preparer.wait(15000)

    def _next(self, preparer: SheetPreparer, result: ExportResult):
        app = QApplication.instance()
        while True:
            # Let the dialog repaint and its Cancel button be pressed,
            # between pages as well as while waiting on a decode.
            if app is not None:
                app.processEvents()
            if self.progress is not None and self.progress.wasCanceled():
                result.cancelled = True
                return None
            try:
                return preparer.results.get(timeout=0.03)
            except queue.Empty:
                pass

    # -- PDF -----------------------------------------------------------

    def write_pdf(self, path: str | Path, title: str | None = None
                  ) -> ExportResult:
        """Write the sheets to a PDF file. The file is written beside the
        target under a temporary name and moved into place at the end,
        so a cancelled or failed export never leaves half a PDF behind
        — or replaces a good one."""
        from PyQt6.QtGui import QPdfWriter
        from src.version import APP_NAME, __version__

        result = ExportResult()
        path = Path(path)
        tmp = path.with_name(f".{path.stem}.{os.getpid()}.part.pdf")
        writer = QPdfWriter(str(tmp))
        writer.setResolution(PDF_DPI)
        writer.setTitle(title or path.stem)
        writer.setCreator(f"{APP_NAME} {__version__}")
        painter = QPainter()
        started = False
        try:
            for sheet, canvas in self._sheets(result):
                source = view_source(self.live_canvas, canvas, self.options)
                ips = printing.inches_per_scene_unit(canvas)
                for size_in, page in printing.plan_pdf_pages(
                        source, ips, self.options, PDF_DPI):
                    layout = QPageLayout(
                        QPageSize(QSizeF(*size_in), QPageSize.Unit.Inch),
                        QPageLayout.Orientation.Portrait,
                        QMarginsF(0, 0, 0, 0))
                    if not started:
                        writer.setPageLayout(layout)
                        if not painter.begin(writer):
                            result.error = (
                                "The PDF could not be created. Check that "
                                "the folder is writable.")
                            return result
                        started = True
                    else:
                        writer.setPageLayout(layout)
                        writer.newPage()
                    canvas.render_scene(painter, page.target, page.source,
                                        include_markup=self.options.markup)
                    result.pages += 1
        except Exception as exc:
            result.error = str(exc) or type(exc).__name__
        finally:
            if painter.isActive():
                painter.end()
            del writer

        if result.cancelled or result.error or result.pages == 0:
            _remove(tmp)
            if not result.error and not result.cancelled:
                result.error = "None of the sheets could be drawn."
            return result
        try:
            os.replace(tmp, path)
        except OSError as exc:
            _remove(tmp)
            result.error = (f"The PDF could not be saved as {path.name}. "
                            f"If it is open in a PDF reader, close it and "
                            f"try again.\n\n({exc.strerror or exc})")
        return result

    # -- printer -------------------------------------------------------

    def print_to(self, printer) -> ExportResult:
        """Send the sheets to a printer that has already been set up,
        each on its own page (or tiled across pages, at scale)."""
        result = ExportResult()
        painter = QPainter()
        try:
            dpi = float(printer.resolution())
            paint = printer.pageLayout().paintRectPixels(int(dpi))
            page_px = QSizeF(float(paint.width()), float(paint.height()))
            for sheet, canvas in self._sheets(result):
                source = view_source(self.live_canvas, canvas, self.options)
                pages = printing.plan_pages(
                    source, page_px, dpi,
                    printing.inches_per_scene_unit(canvas), self.options)
                for page in pages:
                    if not painter.isActive():
                        if not painter.begin(printer):
                            result.error = "The printer could not be started."
                            return result
                    else:
                        printer.newPage()
                    canvas.render_scene(painter, page.target, page.source,
                                        include_markup=self.options.markup)
                    result.pages += 1
        except Exception as exc:
            result.error = str(exc) or type(exc).__name__
        finally:
            if painter.isActive():
                if result.cancelled:
                    printer.abort()
                painter.end()
        return result


def _remove(path: Path) -> None:
    for _ in range(3):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)     # Windows: a scanner may have it open
