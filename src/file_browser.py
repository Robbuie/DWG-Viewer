"""
file_browser.py — left panel: folder selector + drawing icon grid.

Thumbnail pipeline
──────────────────
Work is driven by the *viewport*, not by the folder. Opening a folder of
900 drawings no longer queues 900 file reads, 900 Shell calls and one
900-file ODA conversion before the first thumbnail appears; it queues
work only for the ~15 tiles you can actually see, and queues more as you
scroll. Everything else is unchanged in behaviour, just deferred.

For each requested file, in order of cost:

1. Cached PNG  (src/cache.py)          — sub-millisecond, no decoding
2. Fast worker (I/O pool)
     • embedded DWG preview bitmap, read via the header's image-seeker
     • Windows Shell / eDrawings thumbnail handler
     • extension-badge placeholder so a tile is never blank
3. DXF/DWFx render worker (render pool) — native .dxf and .dwfx, no ODA
4. Batch ODA worker (1 at a time)      — uncached .dwg, in chunks, with
   results streamed to the render pool as ODA writes each file

Rendered results are rasterised once and cached as PNG, so a second
visit to a folder is pure disk reads.

Workers hand back QImage, never QPixmap — QPixmap is not safe to touch
off the GUI thread. Every result carries the generation counter it was
queued under, so switching folders mid-load discards stale work instead
of painting it into the new grid.
"""
from __future__ import annotations
import glob
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PyQt6.QtCore    import (Qt, QRunnable, QThreadPool, pyqtSignal, QObject,
                             QSize, QByteArray, QBuffer, QIODevice, QTimer, QRect,
                             QRectF)
from PyQt6.QtGui     import QPixmap, QImage, QColor, QIcon, QPainter, QFont
from PyQt6.QtSvg     import QSvgRenderer
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QListWidget,
                             QListWidgetItem, QLabel, QPushButton,
                             QFileDialog, QSizePolicy, QLineEdit)

_THUMB_W, _THUMB_H = 160, 110
_RENDER_SCALE      = 2               # cache at 2x for crisp hi-DPI display
_SUPPORTED         = {".dwg", ".dxf", ".dwf", ".dwfx"}

# How far beyond the visible area to pre-load, as a multiple of one
# viewport height. 1.0 means "one screen above and one below", which is
# enough that normal scrolling never outruns the loader.
_PRELOAD_SCREENS   = 1.0

# Uncached DWGs per ODA invocation. One call for the whole folder made
# the first thumbnail wait for the last file; small batches let results
# stream in while later batches are still queued.
_ODA_BATCH_SIZE    = 24


# ── Startup cleanup ──────────────────────────────────────────────────

def _cleanup_orphaned_temps() -> None:
    tmp = tempfile.gettempdir()
    for pattern in ("dwgv_*.dxf", "dwgv_in_*", "dwgv_out_*"):
        for item in glob.glob(os.path.join(tmp, pattern)):
            try:
                if os.path.isdir(item):
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    os.remove(item)
            except OSError:
                pass


_cleanup_orphaned_temps()

try:
    from src import cache as _cache
    _cache.prune_async()
except Exception:
    _cache = None  # type: ignore[assignment]


# ── Shared signal carrier ────────────────────────────────────────────

class _Signals(QObject):
    """One instance per FileBrowser, shared by every worker.

    The old code built a QObject per file and connected it per file;
    with a few hundred files that is a few hundred allocations and
    cross-thread connections before any drawing is read.
    """
    done = pyqtSignal(int, str, QImage)      # generation, path, image


# ── Helpers ──────────────────────────────────────────────────────────

def _badge_image(filepath: str) -> QImage:
    img = QImage(_THUMB_W, _THUMB_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("#2a2a2a"))
    p = QPainter(img)
    p.setPen(QColor("#444"))
    p.drawRect(0, 0, _THUMB_W - 1, _THUMB_H - 1)
    font = QFont()
    font.setPointSize(18)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor("#555"))
    p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, Path(filepath).suffix.upper())
    p.end()
    return img


def _plain_pixmap() -> QPixmap:
    img = QImage(_THUMB_W, _THUMB_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("#2a2a2a"))
    return QPixmap.fromImage(img)


def _image_to_png_bytes(img: QImage) -> bytes:
    ba  = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def _svg_to_image(svg_string: str, w: int, h: int) -> QImage | None:
    try:
        rnd = QSvgRenderer(QByteArray(svg_string.encode("utf-8")))
        if not rnd.isValid():
            return None
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(QColor("#1e1e1e"))
        p = QPainter(img)
        # Fit rather than stretch: sheet drawings are rarely the same
        # aspect ratio as the thumbnail tile.
        native = rnd.defaultSize()
        if native.width() > 0 and native.height() > 0:
            scale = min(w / native.width(), h / native.height())
            tw, th = native.width() * scale, native.height() * scale
            rnd.render(p, QRectF((w - tw) / 2, (h - th) / 2, tw, th))
        else:
            rnd.render(p)
        p.end()
        return img
    except Exception:
        return None


def _render_dxf_to_svg(dxf_path: str, w: int, h: int) -> str | None:
    """Render a DXF file to an SVG string using ezdxf."""
    try:
        from ezdxf import recover as ezdxf_recover
        from ezdxf.addons.drawing import RenderContext, Frontend
        from ezdxf.addons.drawing.svg import SVGBackend
        from ezdxf.addons.drawing import layout as drawing_layout

        doc, _ = ezdxf_recover.readfile(dxf_path)
        for layer in doc.layers:
            layer.on()
        msp  = doc.modelspace()
        ctx  = RenderContext(doc)
        back = SVGBackend()
        Frontend(ctx, back).draw_layout(msp)
        page = drawing_layout.Page(w, h, drawing_layout.Units.px)
        return back.get_string(page)
    except Exception:
        return None


def _render_dwfx_to_svg(path: str, w: int, h: int) -> str | None:
    """Render sheet 1 of a DWFx package to an SVG string.

    Classic DWF lands here too and returns None, which leaves the file
    showing its extension badge instead of a thumbnail.
    """
    try:
        from src import dwfx
        if not dwfx.is_dwfx_package(path):
            return None
        return dwfx.render_sheet_svg(path, 0, w, h)
    except Exception:
        return None


# ── Worker 0: cached PNG (the common case after first visit) ─────────

class _CacheLoadWorker(QRunnable):
    def __init__(self, gen: int, filepath: str, signals: _Signals):
        super().__init__()
        self.gen, self.filepath, self.signals = gen, filepath, signals
        self.setAutoDelete(True)

    def run(self):
        try:
            data = _cache.get_cached_png(Path(self.filepath)) if _cache else None
            if data:
                img = QImage()
                if img.loadFromData(QByteArray(data), "PNG") and not img.isNull():
                    self.signals.done.emit(self.gen, self.filepath, img)
                    return
        except Exception:
            pass
        self.signals.done.emit(self.gen, self.filepath, _badge_image(self.filepath))


# ── Worker 1: fast per-file (embedded preview / Shell) ───────────────

class _FastThumbWorker(QRunnable):
    """Embedded DWG preview, then the Windows Shell handler, then a badge.

    Always emits something quickly so the grid is never blank while the
    slower ODA path runs.
    """
    def __init__(self, gen: int, filepath: str, signals: _Signals):
        super().__init__()
        self.gen, self.filepath, self.signals = gen, filepath, signals
        self.setAutoDelete(True)

    def run(self):
        fp = self.filepath
        try:
            from src.preview import get_thumbnail_image
            img = get_thumbnail_image(fp, _THUMB_W, _THUMB_H)
            if img is not None and not img.isNull():
                self.signals.done.emit(self.gen, fp, img)
                return
        except Exception:
            pass
        self.signals.done.emit(self.gen, fp, _badge_image(fp))


# ── Worker 2: batch ODA ──────────────────────────────────────────────

class _BatchOdaThumbWorker(QRunnable):
    """One ODA File Converter call for a chunk of uncached DWGs.

    Uses Popen + polling so thumbnails appear progressively as ODA
    finishes each file. Each converted DXF is stored in the DXF cache,
    so later opening that drawing in the viewer skips ODA entirely.
    """
    def __init__(self, gen: int, dwg_paths: list[str],
                 render_pool: QThreadPool, signals: _Signals,
                 is_current):
        super().__init__()
        self.gen          = gen
        self.dwg_paths    = dwg_paths
        self._render_pool = render_pool
        self.signals      = signals
        self._is_current  = is_current      # callable -> bool
        self.setAutoDelete(True)

    def run(self):
        if not self.dwg_paths or not self._is_current(self.gen):
            return

        from src.converter import (oda_path, _oda_lock, hidden_popen_kwargs,
                                   store_cached_dxf, mark_failure)
        exe = oda_path()
        if not exe:
            return

        tmp_in  = Path(tempfile.mkdtemp(prefix="dwgv_in_"))
        tmp_out = Path(tempfile.mkdtemp(prefix="dwgv_out_"))
        stem_to_orig: dict[str, str] = {}

        try:
            for fp in self.dwg_paths:
                p = Path(fp)
                staged = tmp_in / p.name
                try:
                    os.link(p, staged)
                except OSError:
                    try:
                        shutil.copy2(p, staged)
                    except OSError:
                        continue
                stem_to_orig[p.stem.lower()] = fp

            cmd = [str(exe), str(tmp_in), str(tmp_out), "ACAD2018", "DXF", "0", "1"]
            seen: set[str] = set()

            with _oda_lock:
                if not self._is_current(self.gen):
                    return
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        **hidden_popen_kwargs())
                while proc.poll() is None:
                    self._enqueue_new(tmp_out, stem_to_orig, seen, store_cached_dxf)
                    time.sleep(0.4)
                proc.wait(timeout=600)

            self._enqueue_new(tmp_out, stem_to_orig, seen, store_cached_dxf)

            # Anything ODA never produced is a drawing we cannot convert.
            # Record it so the next visit to this folder skips it instead
            # of paying the conversion cost again.
            converted = {Path(n).stem.lower() for n in seen}
            for stem, orig in stem_to_orig.items():
                if stem not in converted:
                    try:
                        mark_failure(Path(orig))
                    except Exception:
                        pass

        except Exception:
            pass
        finally:
            shutil.rmtree(tmp_in,  ignore_errors=True)
            shutil.rmtree(tmp_out, ignore_errors=True)

    def _enqueue_new(self, tmp_out: Path, stem_to_orig: dict,
                     seen: set, store_cached_dxf) -> None:
        try:
            candidates = list(tmp_out.glob("*.dxf"))
        except OSError:
            return
        for dxf_file in candidates:
            if dxf_file.name in seen:
                continue
            orig_fp = stem_to_orig.get(dxf_file.stem.lower())
            if not orig_fp:
                continue
            seen.add(dxf_file.name)
            try:
                render_path = str(store_cached_dxf(Path(orig_fp), dxf_file))
            except Exception:
                render_path = str(dxf_file)
            self._render_pool.start(
                _DxfRenderWorker(self.gen, orig_fp, render_path,
                                 self.signals, self._is_current))


# ── Worker 3: DXF render ─────────────────────────────────────────────

class _DxfRenderWorker(QRunnable):
    """Render one drawing to a thumbnail and cache it as PNG.

    Defaults to the ezdxf DXF renderer; pass render_fn to use another
    (DWFx packages render themselves, with no converter in the middle).

    Only emits on success, so the badge placed by the fast worker stays
    put when a drawing cannot be rendered.
    """
    def __init__(self, gen: int, orig_fp: str, dxf_path: str,
                 signals: _Signals, is_current, render_fn=None):
        super().__init__()
        self.gen         = gen
        self.orig_fp     = orig_fp
        self.dxf_path    = dxf_path
        self.signals     = signals
        self._is_current = is_current
        self._render_fn  = render_fn or _render_dxf_to_svg
        self.setAutoDelete(True)

    def run(self):
        if not self._is_current(self.gen):
            return
        try:
            svg = self._render_fn(self.dxf_path,
                                  _THUMB_W * _RENDER_SCALE,
                                  _THUMB_H * _RENDER_SCALE)
            if not svg:
                if _cache:
                    _cache.mark_failure(Path(self.orig_fp))
                return

            img = _svg_to_image(svg, _THUMB_W * _RENDER_SCALE, _THUMB_H * _RENDER_SCALE)
            if img is None or img.isNull():
                if _cache:
                    _cache.mark_failure(Path(self.orig_fp))
                return

            # Rasterise once, cache as PNG. Re-parsing a multi-megabyte
            # SVG on every visit was the single biggest repeat-visit cost.
            if _cache:
                try:
                    _cache.store_png(Path(self.orig_fp), _image_to_png_bytes(img))
                except Exception:
                    pass

            self.signals.done.emit(self.gen, self.orig_fp, img)
        except Exception:
            pass


# ── FileBrowser widget ───────────────────────────────────────────────

class FileBrowser(QWidget):
    """Left panel: folder path bar + scrollable drawing icon grid."""

    fileSelected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._folder: Path | None = None
        self._gen = 0

        cpu = os.cpu_count() or 4

        # I/O-bound: cache reads, embedded previews, Shell handler calls.
        self._io_pool = QThreadPool(self)
        self._io_pool.setMaxThreadCount(max(4, min(16, cpu * 2)))

        # One ODA batch at a time.
        self._oda_pool = QThreadPool(self)
        self._oda_pool.setMaxThreadCount(1)

        # CPU-bound: ezdxf rendering.
        self._render_pool = QThreadPool(self)
        self._render_pool.setMaxThreadCount(max(2, min(8, cpu)))

        self._items: dict[str, QListWidgetItem] = {}
        self._requested: set[str] = set()
        self._pending_dwg: list[str] = []

        self._signals = _Signals()
        self._signals.done.connect(self._on_done)

        self._build_ui()

        # Debounce viewport scans so a scroll gesture triggers one pass,
        # not one per pixel of movement.
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(90)
        self._scan_timer.timeout.connect(self._load_visible)

        # Let a burst of newly-visible DWGs accumulate into one ODA call.
        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.setInterval(250)
        self._batch_timer.timeout.connect(self._dispatch_oda_batch)

        self._list.verticalScrollBar().valueChanged.connect(self._schedule_scan)

    # ── UI ───────────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(4)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Select a folder…")
        self._path_edit.setReadOnly(True)
        self._path_edit.setStyleSheet(
            "QLineEdit{background:#2a2a2a;border:1px solid #444;"
            "border-radius:3px;padding:3px 6px;color:#ccc;}")
        row.addWidget(self._path_edit)

        btn = QPushButton("…")
        btn.setFixedWidth(32)
        btn.setToolTip("Browse for folder")
        btn.clicked.connect(self._browse)
        row.addWidget(btn)
        lay.addLayout(row)

        self._count = QLabel("No folder selected")
        self._count.setStyleSheet("color:#888;font-size:11px;")
        lay.addWidget(self._count)

        self._list = QListWidget()
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setIconSize(QSize(_THUMB_W, _THUMB_H))
        self._list.setGridSize(QSize(_THUMB_W + 20, _THUMB_H + 36))
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setUniformItemSizes(True)
        # SinglePass (the default) is required here: the viewport scan
        # relies on visualItemRect() being valid for every item, and in
        # Batched mode off-screen items have no geometry yet.
        self._list.setLayoutMode(QListWidget.LayoutMode.SinglePass)
        self._list.setSpacing(6)
        self._list.setStyleSheet(
            "QListWidget{background:#252525;border:none;}"
            "QListWidget::item{color:#ccc;border-radius:4px;padding:2px;}"
            "QListWidget::item:selected{background:#3a5a8a;}"
            "QListWidget::item:hover{background:#333;}")
        self._list.itemActivated.connect(self._on_activated)
        lay.addWidget(self._list)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_scan()

    # ── Public API ───────────────────────────────────────────────────

    def open_folder(self, folder) -> None:
        self._folder = Path(folder)
        self._path_edit.setText(str(self._folder))
        self._refresh()

    def _browse(self):
        start = str(self._folder) if self._folder else ""
        f = QFileDialog.getExistingDirectory(self, "Select drawing folder", start)
        if f:
            self.open_folder(f)

    def _on_activated(self, item: QListWidgetItem):
        fp = item.data(Qt.ItemDataRole.UserRole)
        if fp:
            self.fileSelected.emit(fp)

    def select_next(self, delta: int) -> None:
        """Move selection by delta (+1 = next, -1 = previous) and open the file."""
        count = self._list.count()
        if count == 0:
            return
        current = self._list.currentRow()
        if current < 0:
            current = 0 if delta > 0 else count
        new_row = max(0, min(count - 1, current + delta))
        if new_row == current and 0 <= current < count:
            return
        self._list.setCurrentRow(new_row)
        item = self._list.item(new_row)
        if item:
            self._list.scrollToItem(item)
            fp = item.data(Qt.ItemDataRole.UserRole)
            if fp:
                self.fileSelected.emit(fp)

    # ── Folder population ────────────────────────────────────────────

    def _refresh(self):
        # Invalidate in-flight work: anything still running will see a
        # stale generation and drop its result on the floor.
        self._gen += 1
        self._oda_pool.clear()
        self._render_pool.clear()
        self._io_pool.clear()
        self._batch_timer.stop()

        self._list.clear()
        self._items.clear()
        self._requested.clear()
        self._pending_dwg.clear()

        if not (self._folder and self._folder.is_dir()):
            self._count.setText("Folder not found")
            return

        try:
            files = sorted(
                (f for f in self._folder.iterdir()
                 if f.is_file() and f.suffix.lower() in _SUPPORTED),
                key=lambda p: p.name.lower())
        except OSError:
            self._count.setText("Folder not readable")
            return

        if not files:
            self._count.setText("No drawing files found")
            return

        n = len(files)
        self._count.setText(f"{n} file{'s' if n != 1 else ''}")

        # Populate the grid with placeholders only. This is pure UI work
        # and completes immediately even for very large folders; no file
        # is touched until it scrolls into view.
        ph  = _plain_pixmap()
        icon = QIcon(ph)
        hint = QSize(_THUMB_W + 16, _THUMB_H + 32)
        for f in files:
            fp = str(f)
            item = QListWidgetItem(icon, f.name)
            item.setData(Qt.ItemDataRole.UserRole, fp)
            item.setSizeHint(hint)
            item.setToolTip(fp)
            self._list.addItem(item)
            self._items[fp] = item

        self._schedule_scan()

    # ── Viewport-driven loading ──────────────────────────────────────

    def _schedule_scan(self):
        self._scan_timer.start()

    def _is_current(self, gen: int) -> bool:
        return gen == self._gen

    def _load_visible(self):
        count = self._list.count()
        if count == 0:
            return

        vp     = self._list.viewport().rect()
        margin = int(vp.height() * _PRELOAD_SCREENS)
        window = QRect(vp.left(), vp.top() - margin,
                       vp.width(), vp.height() + 2 * margin)

        gen = self._gen
        queued_dwg = False

        for i in range(count):
            item = self._list.item(i)
            if item is None:
                continue
            r = self._list.visualItemRect(item)
            if r.bottom() < window.top():
                continue
            if r.top() > window.bottom():
                break                      # laid out in order — nothing further is near
            fp = item.data(Qt.ItemDataRole.UserRole)
            if not fp or fp in self._requested:
                continue
            self._requested.add(fp)
            if self._dispatch(gen, fp):
                queued_dwg = True

        if queued_dwg:
            self._batch_timer.start()

    def _dispatch(self, gen: int, fp: str) -> bool:
        """Start work for one file. Returns True if it needs ODA."""
        path = Path(fp)

        # 1. Cached PNG — by far the common case after the first visit.
        if _cache is not None and _cache.has_cached_png(path):
            self._io_pool.start(_CacheLoadWorker(gen, fp, self._signals))
            return False

        # 2. Something on screen straight away.
        self._io_pool.start(_FastThumbWorker(gen, fp, self._signals))

        suffix = path.suffix.lower()

        # 3. Native DXF renders directly — no converter needed.
        if suffix == ".dxf":
            self._render_pool.start(
                _DxfRenderWorker(gen, fp, fp, self._signals, self._is_current))
            return False

        # 3b. DWFx carries its own drawable markup — no converter either.
        if suffix in (".dwf", ".dwfx"):
            if _cache is not None and _cache.is_known_failure(path):
                return False
            self._render_pool.start(
                _DxfRenderWorker(gen, fp, fp, self._signals, self._is_current,
                                 render_fn=_render_dwfx_to_svg))
            return False

        # 4. DWG needs ODA, unless we already know it cannot be converted.
        if suffix == ".dwg":
            if _cache is not None and _cache.is_known_failure(path):
                return False
            self._pending_dwg.append(fp)
            return True

        return False

    def _dispatch_oda_batch(self):
        if not self._pending_dwg:
            return
        gen = self._gen
        while self._pending_dwg:
            chunk = self._pending_dwg[:_ODA_BATCH_SIZE]
            del self._pending_dwg[:_ODA_BATCH_SIZE]
            self._oda_pool.start(
                _BatchOdaThumbWorker(gen, chunk, self._render_pool,
                                     self._signals, self._is_current))

    # ── Result handling ──────────────────────────────────────────────

    def _on_done(self, gen: int, fp: str, img: QImage):
        if gen != self._gen or img.isNull():
            return
        item = self._items.get(fp)
        if item is None:
            return
        if img.width() > _THUMB_W or img.height() > _THUMB_H:
            img = img.scaled(_THUMB_W, _THUMB_H,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        item.setIcon(QIcon(QPixmap.fromImage(img)))
