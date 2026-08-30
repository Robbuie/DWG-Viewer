"""
cache.py — persistent, thread-safe thumbnail / DXF cache.

Replaces the old temp-directory + index.json cache, which had three
problems:

  1. It lived in %TEMP%, so Windows Disk Cleanup / Storage Sense wiped
     it and every folder had to be re-converted from scratch.
  2. index.json was rewritten (non-atomically, without a lock) by four
     render threads at once, so entries were routinely lost and files
     were re-converted even when a good thumbnail was already on disk.
  3. It stored SVG, which has to be re-parsed and re-rasterised by
     QSvgRenderer on every single visit.

The new cache is keyed by a deterministic hash of (path, mtime, size),
so no index file is needed — the filename *is* the lookup. Thumbnails
are stored as PNG at final display resolution, which loads in well
under a millisecond. Failures are recorded with a ".none" marker so a
drawing that cannot be converted is not retried on every visit.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

# ── Cache location ────────────────────────────────────────────────────
#
# %LOCALAPPDATA%\DWGViewer\cache on Windows — survives reboots and
# temp-file cleanup, and is per-user so there are no permission clashes
# on a shared machine.

def _base_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    else:
        root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(root) / "DWGViewer" / "cache"


CACHE_ROOT = _base_dir()
THUMB_DIR  = CACHE_ROOT / "thumbs"
DXF_DIR    = CACHE_ROOT / "dxf"
# Full-resolution rasters of formats that cannot be drawn as vectors
# fast enough to do it on every open (classic DWF).
RASTER_DIR = CACHE_ROOT / "raster"

# Bumped whenever the app learns to render something it previously
# could not. Failure markers are keyed by it, so adding a format never
# leaves users staring at "we already tried this" for files that would
# now work — without making them delete their cache by hand.
RENDERER_GENERATION = 4

# Cache size ceilings — pruned oldest-first on startup.
MAX_THUMB_FILES = 20_000
MAX_DXF_BYTES   = 4 * 1024 ** 3      # 4 GB of converted DXFs
MAX_RASTER_BYTES = 2 * 1024 ** 3     # 2 GB of decoded DWF sheets

_dirs_ready = False
_dirs_lock  = threading.Lock()


def _ensure_dirs() -> None:
    global _dirs_ready
    if _dirs_ready:
        return
    with _dirs_lock:
        if _dirs_ready:
            return
        try:
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            DXF_DIR.mkdir(parents=True, exist_ok=True)
            RASTER_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        _dirs_ready = True


# ── Key ───────────────────────────────────────────────────────────────

def cache_key(filepath: Path) -> str:
    """Identity of a drawing: path + mtime + size.

    mtime is truncated to whole seconds so that sub-second differences
    introduced by copying files between filesystems don't invalidate an
    otherwise-good thumbnail.
    """
    try:
        st  = filepath.stat()
        raw = f"{str(filepath).lower()}|{int(st.st_mtime)}|{st.st_size}"
    except OSError:
        raw = str(filepath).lower()
    return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()


def _atomic_write(target: Path, data: bytes) -> None:
    """Write via a temp file + os.replace so a reader never sees a
    half-written thumbnail, and two threads racing produce one good
    file rather than a corrupt one."""
    _ensure_dirs()
    tmp = target.with_suffix(target.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ── Thumbnail PNG cache ───────────────────────────────────────────────

def thumb_png_path(filepath: Path) -> Path:
    return THUMB_DIR / (cache_key(filepath) + ".png")


def fail_marker_path(filepath: Path) -> Path:
    return THUMB_DIR / f"{cache_key(filepath)}.g{RENDERER_GENERATION}.none"


def get_cached_png(filepath: Path) -> bytes | None:
    p = thumb_png_path(filepath)
    try:
        if p.is_file():
            os.utime(p, None)          # touch: keeps hot entries out of the prune
            return p.read_bytes()
    except OSError:
        pass
    return None


def has_cached_png(filepath: Path) -> bool:
    try:
        return thumb_png_path(filepath).is_file()
    except OSError:
        return False


def store_png(filepath: Path, data: bytes) -> None:
    if data:
        _atomic_write(thumb_png_path(filepath), data)


# ── Full-resolution raster cache ──────────────────────────────────────

def raster_variant(hidden) -> str:
    """A short key for one combination of hidden layers.

    Hiding a layer on a classic DWF means decoding the whole sheet again,
    which is the expensive thing this cache exists to avoid — so each
    combination gets its own entry and going back to one already seen is
    instant. The empty set keeps the unsuffixed name so rasters cached by
    earlier versions stay valid.
    """
    if not hidden:
        return ""
    digest = hashlib.sha1("\u0000".join(sorted(hidden)).encode("utf-8"))
    return "." + digest.hexdigest()[:10]


def raster_sheet(sheet: int) -> str:
    """Which sheet of a multi-sheet DWF a raster belongs to.

    A DWF set holds several sheets in one file, each decoding to a
    different picture, so the sheet has to be part of the key or the
    second sheet opened would be served the first one's raster. Sheet 0
    keeps the unsuffixed name, so rasters cached by earlier versions —
    which only ever decoded the first sheet — stay valid.
    """
    return f".s{sheet}" if sheet else ""


def raster_path(filepath: Path, width_px: int, variant: str = "",
                sheet: int = 0) -> Path:
    return (RASTER_DIR /
            f"{cache_key(filepath)}.w{width_px}{raster_sheet(sheet)}{variant}"
            f".g{RENDERER_GENERATION}.png")


def raster_layers_path(filepath: Path, width_px: int, sheet: int = 0) -> Path:
    """The sheet's layer names, cached beside the raster.

    Without this a cached open would show no layers at all: the names
    only exist as a by-product of decoding, which is exactly what the
    cache is there to skip. Layers are per sheet, so this is keyed by
    sheet the same way the raster is.
    """
    return (RASTER_DIR /
            f"{cache_key(filepath)}.w{width_px}{raster_sheet(sheet)}"
            f".g{RENDERER_GENERATION}.layers.json")


def get_cached_raster(filepath: Path, width_px: int,
                      variant: str = "", sheet: int = 0) -> bytes | None:
    p = raster_path(filepath, width_px, variant, sheet)
    try:
        if p.is_file():
            return p.read_bytes()
    except OSError:
        pass
    return None


def store_raster(filepath: Path, width_px: int, data: bytes,
                 variant: str = "", sheet: int = 0) -> None:
    if data:
        _ensure_dirs()
        _atomic_write(raster_path(filepath, width_px, variant, sheet), data)


def get_cached_raster_layers(filepath: Path, width_px: int,
                             sheet: int = 0) -> list[str] | None:
    """Layer names for a cached raster, or None if none were recorded.

    None means unknown — an empty list means the sheet genuinely declared
    no layers, and the two must not be confused: one is a reason to wait,
    the other a reason to tell the user why the panel is empty.
    """
    try:
        raw = raster_layers_path(filepath, width_px, sheet).read_bytes()
        got = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return [str(x) for x in got] if isinstance(got, list) else None


def store_raster_layers(filepath: Path, width_px: int,
                        layers: list[str], sheet: int = 0) -> None:
    _ensure_dirs()
    try:
        _atomic_write(raster_layers_path(filepath, width_px, sheet),
                      json.dumps(list(layers)).encode("utf-8"))
    except OSError:
        pass


def is_known_failure(filepath: Path) -> bool:
    """True if we already tried and failed to render this drawing.

    Prevents a folder containing a handful of broken or password-protected
    DWGs from paying the full ODA conversion cost on every single visit.
    The marker expires after 7 days in case the tooling changes.
    """
    p = fail_marker_path(filepath)
    try:
        if p.is_file():
            if time.time() - p.stat().st_mtime < 7 * 86400:
                return True
            p.unlink()
    except OSError:
        pass
    return False


def mark_failure(filepath: Path) -> None:
    _atomic_write(fail_marker_path(filepath), b"")


# ── Converted-DXF cache ───────────────────────────────────────────────

def cached_dxf_path(filepath: Path) -> Path:
    return DXF_DIR / (cache_key(filepath) + ".dxf")


def get_cached_dxf(filepath: Path) -> Path | None:
    p = cached_dxf_path(filepath)
    try:
        if p.is_file() and p.stat().st_size > 0:
            os.utime(p, None)
            return p
    except OSError:
        pass
    return None


def store_cached_dxf(filepath: Path, dxf_path: Path) -> Path:
    """Move (not copy) the converted DXF into the cache when possible —
    the source is a temp file we are about to delete anyway, so a rename
    avoids writing hundreds of megabytes twice."""
    _ensure_dirs()
    target = cached_dxf_path(filepath)
    tmp    = target.with_suffix(f".dxf.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        try:
            shutil.move(str(dxf_path), str(tmp))
        except OSError:
            shutil.copy2(str(dxf_path), str(tmp))
        os.replace(tmp, target)
        return target
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return Path(dxf_path)


# ── Pruning ───────────────────────────────────────────────────────────

def prune() -> None:
    """Trim the cache to its ceilings, oldest-accessed first.

    Safe to call from a background thread at startup; failures are
    ignored because a too-large cache is never worth crashing over.
    """
    _ensure_dirs()
    try:
        # The .layers.json sidecars are a few hundred bytes and are the
        # only record of a sheet's layer names once its raster is cached,
        # so they are not worth pruning and are skipped here.
        rasters = [(p.stat().st_mtime, p.stat().st_size, p)
                   for p in RASTER_DIR.iterdir()
                   if p.is_file() and p.suffix == ".png"]
        total = sum(size for _, size, _ in rasters)
        if total > MAX_RASTER_BYTES:
            rasters.sort()
            for _, size, p in rasters:
                if total <= MAX_RASTER_BYTES:
                    break
                try:
                    p.unlink()
                    total -= size
                except OSError:
                    pass
    except OSError:
        pass

    try:
        thumbs = [(p.stat().st_mtime, p) for p in THUMB_DIR.iterdir() if p.is_file()]
        if len(thumbs) > MAX_THUMB_FILES:
            thumbs.sort()
            for _, p in thumbs[: len(thumbs) - MAX_THUMB_FILES]:
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass

    try:
        dxfs  = [(p.stat().st_mtime, p.stat().st_size, p)
                 for p in DXF_DIR.iterdir() if p.is_file()]
        total = sum(s for _, s, _ in dxfs)
        if total > MAX_DXF_BYTES:
            dxfs.sort()
            for _, size, p in dxfs:
                if total <= MAX_DXF_BYTES:
                    break
                try:
                    p.unlink()
                    total -= size
                except OSError:
                    pass
    except OSError:
        pass


def prune_async() -> None:
    threading.Thread(target=prune, name="cache-prune", daemon=True).start()


def clear_all() -> None:
    """Wipe the cache — exposed for a future 'rebuild thumbnails' command."""
    for d in (THUMB_DIR, DXF_DIR):
        shutil.rmtree(d, ignore_errors=True)
    global _dirs_ready
    _dirs_ready = False
    _ensure_dirs()
