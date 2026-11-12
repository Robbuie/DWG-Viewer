"""What a 3D snapshot includes — remembered per user.

Kept apart from model_view so the main window can show these options in
its menus without importing the OpenGL widget module at startup.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings

_CLEAR = "model/snapshotTransparent"
_TRIAD = "model/snapshotAxes"
_CROP = "model/snapshotCrop"


def transparent() -> bool:
    """Everything not covered by the model is transparent (default on)."""
    return QSettings().value(_CLEAR, True, type=bool)


def triad() -> bool:
    """Draw the XYZ indicator into the picture (default off)."""
    return QSettings().value(_TRIAD, False, type=bool)


def set_transparent(on: bool) -> None:
    QSettings().setValue(_CLEAR, bool(on))


def set_triad(on: bool) -> None:
    QSettings().setValue(_TRIAD, bool(on))


def crop() -> bool:
    """Frame the picture tightly around the model (default on)."""
    return QSettings().value(_CROP, True, type=bool)


def set_crop(on: bool) -> None:
    QSettings().setValue(_CROP, bool(on))
