"""
preview.py — Fast DWG thumbnail extraction, no conversion required.

Strategy (fastest to slowest):
  1. Embedded preview bitmap in the DWG file header (~5-20 ms, no tools)
  2. Windows Shell IShellItemImageFactory — uses whatever thumbnail handler
     is registered for .dwg (e.g. eDrawings, AutoCAD TrueView)
  3. Return None → caller shows a generic placeholder
"""
from __future__ import annotations
import os
import struct
from PyQt6.QtCore import Qt, QByteArray
from PyQt6.QtGui import QImage, QPixmap

# ── DWG embedded preview ─────────────────────────────────────────────
#
# DWG files (R13 / AC1012 through R2018 / AC1032) embed a preview image
# in the file body.  The block is preceded by a fixed 16-byte sentinel.

_SENTINEL = bytes([
    0x1F, 0x25, 0x6D, 0x07, 0xD4, 0x36, 0x28, 0x28,
    0x9D, 0x57, 0xCA, 0x3F, 0x9D, 0x44, 0x10, 0xDB,
])


def _dib_to_bmp(dib: bytes) -> bytes | None:
    """Prepend a standard BMP file-header to a raw DIB payload."""
    if len(dib) < 40:
        return None
    try:
        header_size = struct.unpack_from("<I", dib, 0)[0]
        bit_count   = struct.unpack_from("<H", dib, 14)[0]
        clr_used    = struct.unpack_from("<I", dib, 32)[0]
        compression = struct.unpack_from("<I", dib, 16)[0]

        if bit_count <= 8:
            n_colors = clr_used if clr_used else (1 << bit_count)
        elif compression == 3:          # BI_BITFIELDS
            n_colors = 3
        else:
            n_colors = 0

        pixel_off  = 14 + header_size + n_colors * 4
        file_hdr   = struct.pack("<2sIHHI", b"BM", 14 + len(dib), 0, 0, pixel_off)
        return file_hdr + dib
    except Exception:
        return None


def _parse_preview_directory(fh, base: int, filesize: int) -> bytes | None:
    """Read the preview-image directory at *base* and return PNG/BMP bytes."""
    fh.seek(base)
    head = fh.read(21)
    if len(head) < 21 or head[:16] != _SENTINEL:
        return None

    # 16 bytes sentinel, 4 bytes overall size, 1 byte entry count
    count = head[20]
    dir_bytes = fh.read(9 * min(count, 8))

    png_bytes = bmp_bytes = None
    off = 0
    for _ in range(min(count, 8)):
        if off + 9 > len(dir_bytes):
            break
        code  = dir_bytes[off]
        start = struct.unpack_from("<I", dir_bytes, off + 1)[0]
        size  = struct.unpack_from("<I", dir_bytes, off + 5)[0]
        off  += 9

        if not (0 < size < 20_000_000 and 0 < start and start + size <= filesize):
            continue
        if code not in (2, 6):          # 1/3 = WMF, no pure-Python decoder
            continue

        fh.seek(start)
        chunk = fh.read(size)
        if len(chunk) != size:
            continue

        if code == 6 and chunk[:4] == b"\x89PNG":
            png_bytes = chunk
        elif code == 2 and len(chunk) >= 40:
            bmp_bytes = _dib_to_bmp(chunk)

    return png_bytes or bmp_bytes


def extract_embedded_preview(filepath: str) -> bytes | None:
    """
    Read the embedded preview image from a DWG file.
    Returns raw PNG or BMP bytes, or None if not found.

    The DWG header carries an "image seeker" pointer at offset 0x0D that
    gives the absolute address of the preview block. Following it costs
    two small reads. The previous implementation instead pulled the first
    8 MB of every file into memory and searched it for the sentinel —
    on a network share, with a folder of large drawings, that alone was
    hundreds of megabytes of reads before a single thumbnail appeared.
    A bounded scan is kept only as a fallback for files whose pointer is
    missing or wrong.
    """
    try:
        filesize = os.path.getsize(filepath)
        with open(filepath, "rb") as fh:
            head = fh.read(0x20)
            if len(head) < 0x20 or head[:2] != b"AC":   # not a DWG
                return None

            # Preferred path: follow the image-seeker pointer.
            addr = struct.unpack_from("<I", head, 0x0D)[0]
            if 0 < addr < filesize:
                result = _parse_preview_directory(fh, addr, filesize)
                if result:
                    return result

            # Fallback: bounded scan of the first 2 MB.
            fh.seek(0)
            window = fh.read(min(filesize, 2 * 1024 * 1024))
            pos = window.find(_SENTINEL)
            if pos == -1:
                return None
            return _parse_preview_directory(fh, pos, filesize)

    except Exception:
        return None


# ── Windows Shell thumbnail ──────────────────────────────────────────
#
# Uses IShellItemImageFactory::GetImage, which delegates to whichever
# COM thumbnail provider is registered for .dwg — typically eDrawings.

import threading

_com_state = threading.local()

# Circuit breaker: if no DWG thumbnail handler is registered on this
# machine (no eDrawings, no TrueView), every Shell call is a guaranteed
# miss that still costs a COM round-trip. After a run of failures with
# zero successes we stop asking.
_shell_failures = 0
_shell_successes = 0
_SHELL_GIVE_UP_AFTER = 12


def shell_thumbnails_worth_trying() -> bool:
    return _shell_successes > 0 or _shell_failures < _SHELL_GIVE_UP_AFTER


def _windows_shell_thumbnail(filepath: str, size: int) -> QImage | None:
    """Query the Windows Shell for a thumbnail (eDrawings, AutoCAD, etc.)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import (
            windll, POINTER, byref,
            c_int, c_long, c_void_p, c_wchar_p,
            c_ulong, c_ushort, c_ubyte, c_uint32,
            Structure, WINFUNCTYPE, HRESULT,
        )

        ole32   = windll.ole32
        shell32 = windll.shell32
        gdi32   = windll.gdi32
        user32  = windll.user32

        # ── COM helpers ─────────────────────────────────────────────
        class GUID(Structure):
            _fields_ = [
                ("Data1", c_ulong), ("Data2", c_ushort),
                ("Data3", c_ushort), ("Data4", c_ubyte * 8),
            ]

        class SIZE(Structure):
            _fields_ = [("cx", c_long), ("cy", c_long)]

        def make_guid(s: str) -> GUID:
            import uuid
            b = uuid.UUID(s).bytes_le
            g = GUID()
            g.Data1 = int.from_bytes(b[0:4], "little")
            g.Data2 = int.from_bytes(b[4:6], "little")
            g.Data3 = int.from_bytes(b[6:8], "little")
            g.Data4 = (c_ubyte * 8)(*b[8:16])
            return g

        # ── Get IShellItemImageFactory ───────────────────────────────
        # CoInitializeEx must happen once per thread, not once per file.
        # The old code called it on every single thumbnail, which bumps
        # COM's per-thread reference count without a matching uninit.
        if not getattr(_com_state, "ready", False):
            ole32.CoInitializeEx(None, 0x2)     # COINIT_APARTMENTTHREADED
            _com_state.ready = True

        iid = make_guid("BCC18B79-BA16-442F-80C4-8A59C30C463B")
        ppv = c_void_p()
        hr  = shell32.SHCreateItemFromParsingName(
            c_wchar_p(filepath), None, byref(iid), byref(ppv))
        if hr != 0:
            return None

        vtable   = ctypes.cast(ppv, POINTER(POINTER(c_void_p)))[0]
        GetImage = WINFUNCTYPE(HRESULT, c_void_p, SIZE, c_int, POINTER(c_void_p))(vtable[3])
        Release  = WINFUNCTYPE(c_ulong,  c_void_p)(vtable[2])

        sz  = SIZE(size, size)
        hbm = c_void_p()
        hr  = GetImage(ppv, sz, 0, byref(hbm))
        Release(ppv)

        if hr != 0 or not hbm.value:
            return None

        # ── HBITMAP → QPixmap ────────────────────────────────────────
        class BITMAP(Structure):
            _fields_ = [
                ("bmType",       c_long), ("bmWidth",      c_long),
                ("bmHeight",     c_long), ("bmWidthBytes", c_long),
                ("bmPlanes",     c_ushort), ("bmBitsPixel", c_ushort),
                ("bmBits",       c_void_p),
            ]

        class BITMAPINFOHEADER(Structure):
            _fields_ = [
                ("biSize",          c_uint32), ("biWidth",         c_long),
                ("biHeight",        c_long),   ("biPlanes",        c_ushort),
                ("biBitCount",      c_ushort), ("biCompression",   c_uint32),
                ("biSizeImage",     c_uint32), ("biXPelsPerMeter", c_long),
                ("biYPelsPerMeter", c_long),   ("biClrUsed",       c_uint32),
                ("biClrImportant",  c_uint32),
            ]

        bm = BITMAP()
        gdi32.GetObjectW(hbm, ctypes.sizeof(bm), byref(bm))
        w, h = bm.bmWidth, abs(bm.bmHeight)
        if w <= 0 or h <= 0:
            gdi32.DeleteObject(hbm)
            return None

        hdc = user32.GetDC(None)
        bih = BITMAPINFOHEADER()
        bih.biSize        = ctypes.sizeof(bih)
        bih.biWidth       = w
        bih.biHeight      = -h          # negative = top-down scan
        bih.biPlanes      = 1
        bih.biBitCount    = 32
        bih.biCompression = 0           # BI_RGB

        buf = (ctypes.c_byte * (w * h * 4))()
        gdi32.GetDIBits(hdc, hbm, 0, h, buf, byref(bih), 0)
        user32.ReleaseDC(None, hdc)
        gdi32.DeleteObject(hbm)

        # .copy() detaches the QImage from the ctypes buffer, which is
        # about to go out of scope.
        img = QImage(bytes(buf), w, h, w * 4, QImage.Format.Format_BGRA8888)
        return img.copy() if not img.isNull() else None

    except Exception:
        return None


# ── Public entry point ───────────────────────────────────────────────

def get_thumbnail_image(filepath: str, width: int, height: int) -> QImage | None:
    """
    Return a thumbnail QImage for *filepath*, or None on failure.
    Scaled to fit within (width x height), aspect ratio preserved.

    QImage rather than QPixmap: this runs on a worker thread, and QPixmap
    is only safe to touch on the GUI thread.
    """
    global _shell_failures, _shell_successes
    img: QImage | None = None

    # Stage 1: embedded DWG preview
    raw = extract_embedded_preview(filepath)
    if raw:
        candidate = QImage.fromData(QByteArray(raw))
        if not candidate.isNull():
            img = candidate

    # Stage 2: Windows Shell / eDrawings
    if img is None and shell_thumbnails_worth_trying():
        img = _windows_shell_thumbnail(filepath, max(width, height))
        if img is not None and not img.isNull():
            _shell_successes += 1
        else:
            _shell_failures += 1
            img = None

    if img is not None and not img.isNull():
        return img.scaled(
            width, height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return None


def get_thumbnail(filepath: str, width: int, height: int) -> QPixmap | None:
    """QPixmap wrapper — GUI thread only."""
    img = get_thumbnail_image(filepath, width, height)
    return QPixmap.fromImage(img) if img is not None else None
