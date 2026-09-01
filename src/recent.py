"""
recent.py — the recently-opened lists behind the toolbar's Open menu.

Two lists are kept: drawings the user actually opened, and folders they
browsed to. Both live in the same QSettings store as the theme and the
updater, newest first, capped so the menu stays a shortlist rather than a
history.

One deliberate omission: nothing here touches the filesystem. Drawings in
this shop live on network shares, and probing a dozen UNC paths every time
the menu drops would stall the button whenever a share is asleep — worse,
it would quietly erase the whole list any time the VPN was down. So a path
is only checked when the user actually picks it, and `remove_file` /
`remove_folder` let the caller drop an entry that turned out to be gone.
"""
from __future__ import annotations

import os
from typing import Iterable

from PyQt6.QtCore import QSettings

_ORG = "DWGViewer"
_APP = "DWGViewer"

_KEY_FILES = "recent/files"
_KEY_FOLDERS = "recent/folders"

MAX_FILES = 12
MAX_FOLDERS = 10


# ------------------------------------------------------------------ #
#  Storage helpers
# ------------------------------------------------------------------ #

def _settings() -> QSettings:
    return QSettings(_ORG, _APP)


def _norm(path) -> str:
    """One spelling per path, so the same drawing opened from the browser
    and from the command line does not appear twice."""
    return os.path.normpath(str(path))


def _key(path: str) -> str:
    # Windows paths differ only in case far more often than they differ
    # in meaning; matching case-insensitively is what stops duplicates.
    return _norm(path).casefold()


def _read(key: str) -> list[str]:
    raw = _settings().value(key, [])
    # QSettings hands back a bare string when the stored list had one
    # entry, and None when the key was written empty.
    if raw is None:
        raw = []
    elif isinstance(raw, str):
        raw = [raw] if raw.strip() else []
    elif not isinstance(raw, Iterable):
        raw = []

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        path = _norm(item)
        if _key(path) in seen:
            continue
        seen.add(_key(path))
        out.append(path)
    return out


def _write(key: str, values: list[str], limit: int) -> None:
    _settings().setValue(key, values[:limit])


def _add(key: str, path, limit: int) -> None:
    entry = _norm(path)
    if not entry or entry == ".":
        return
    kept = [e for e in _read(key) if _key(e) != _key(entry)]
    _write(key, [entry] + kept, limit)


def _remove(key: str, path, limit: int) -> None:
    entry = _key(_norm(path))
    kept = [e for e in _read(key) if _key(e) != entry]
    _write(key, kept, limit)


# ------------------------------------------------------------------ #
#  Public API
# ------------------------------------------------------------------ #

def add_file(path) -> None:
    """Record a drawing as the most recently opened one."""
    _add(_KEY_FILES, path, MAX_FILES)


def add_folder(path) -> None:
    """Record a folder as the most recently browsed one."""
    _add(_KEY_FOLDERS, path, MAX_FOLDERS)


def files() -> list[str]:
    """Recently opened drawings, newest first. Not checked for existence —
    see the module docstring."""
    return _read(_KEY_FILES)[:MAX_FILES]


def folders() -> list[str]:
    """Recently browsed folders, newest first."""
    return _read(_KEY_FOLDERS)[:MAX_FOLDERS]


def remove_file(path) -> None:
    """Drop a drawing from the list — used when opening it fails."""
    _remove(_KEY_FILES, path, MAX_FILES)


def remove_folder(path) -> None:
    """Drop a folder from the list — used when it no longer exists."""
    _remove(_KEY_FOLDERS, path, MAX_FOLDERS)


def elide(text: str, limit: int = 46) -> str:
    """Trim the middle of a long path — the drive and the leaf folder are
    what identify it, the levels in between rarely are."""
    if len(text) <= limit:
        return text
    keep = limit - 1
    head = keep // 3
    return f"{text[:head]}\u2026{text[-(keep - head):]}"


def menu_label(path: str, number: int | None = None, *,
               is_file: bool = True) -> str:
    """One line of the Open menu.

    A drawing is named by its file and placed by its folder, because a
    dozen jobs all contain an A-101; a folder is just its path, shortened.
    """
    p = os.path.normpath(str(path))
    if is_file:
        name = os.path.basename(p)
        parent = os.path.dirname(p)
        text = f"{name}   \u2014   {elide(parent)}" if parent else name
    else:
        text = elide(p, 64)
    # Qt reads & as a mnemonic marker and paths on a share are full of
    # them; doubling is what keeps the name the user actually sees.
    text = text.replace("&", "&&")
    if number is not None and number <= 9:
        text = f"&{number}   {text}"
    return text


def is_empty() -> bool:
    return not (files() or folders())


def clear() -> None:
    """Forget both lists."""
    s = _settings()
    s.remove(_KEY_FILES)
    s.remove(_KEY_FOLDERS)
