"""
dwf.py — classic DWF (version 6 and earlier) container reader.

A DWF 6 file is "(DWF V06.00)" followed by a ZIP package. manifest.xml
lists sections; an ePlot section is one printable sheet and carries:

    role="2d streaming graphics"  → the .w2d WHIP! opcode stream
    role="thumbnail"              → a PNG of the plotted sheet
    role="font"                   → embedded .ef_ fonts
    role="descriptor"             → paper size, units, layer properties

This module reads the container only. Decoding the W2D opcode stream is
a separate job; until that exists, the thumbnail is what lets classic
DWF files show something useful in the folder grid.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

_EPLOT = "com.autodesk.dwf.eplot"


class ClassicDwfError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _norm(href: str) -> str:
    """Manifest hrefs and zip entry names both use backslashes here."""
    return href.replace("\\", "/").lstrip("/").lower()


class Sheet:
    """One ePlot section: a single plotted sheet."""

    def __init__(self, name: str, title: str):
        self.name = name
        self.title = title or name
        self.resources: dict[str, list[str]] = {}
        self.paper: tuple[float, float, str] | None = None   # w, h, units

    def first(self, role: str) -> str | None:
        got = self.resources.get(role.lower())
        return got[0] if got else None

    def __repr__(self) -> str:
        return f"<Sheet {self.title!r} paper={self.paper}>"


class ClassicDwf:
    def __init__(self, path):
        self.path = Path(path)
        try:
            self._zip = zipfile.ZipFile(str(self.path))
        except Exception as exc:
            raise ClassicDwfError(f"Not a readable DWF package: {exc}") from exc
        self._names = {_norm(n): n for n in self._zip.namelist()}
        self.version = self._version()
        self.sheets = self._read_manifest()

    # -- container -----------------------------------------------------

    def _version(self) -> str | None:
        try:
            with open(self.path, "rb") as fh:
                m = re.match(rb"\(DWF V(\d\d)\.(\d\d)\)", fh.read(16))
            return f"{int(m.group(1))}.{int(m.group(2))}" if m else None
        except Exception:
            return None

    def read(self, href: str) -> bytes | None:
        real = self._names.get(_norm(href))
        if real is None:
            return None
        try:
            with self._zip.open(real) as fh:
                return fh.read()
        except Exception:
            return None

    def _read_manifest(self) -> list[Sheet]:
        raw = self.read("manifest.xml")
        if raw is None:
            raise ClassicDwfError("This DWF package has no manifest.")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ClassicDwfError(f"Corrupt DWF manifest: {exc}") from exc

        sheets = []
        for el in root.iter():
            if _local(el.tag) != "Section":
                continue
            if not (el.get("type", "").lower().startswith(_EPLOT)):
                continue
            if el.get("type", "").lower().endswith("global"):
                continue      # the set-wide section, not a sheet
            sheet = Sheet(el.get("name", ""), el.get("title", ""))
            for res in el.iter():
                if _local(res.tag) != "Resource":
                    continue
                role = (res.get("role") or "").lower()
                href = res.get("href")
                if role and href:
                    sheet.resources.setdefault(role, []).append(href)
            sheet.paper = self._paper(sheet)
            sheets.append(sheet)
        return sheets

    def _paper(self, sheet: Sheet) -> tuple[float, float, str] | None:
        href = sheet.first("descriptor")
        if not href:
            return None
        raw = self.read(href)
        if raw is None:
            return None
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return None
        for el in root.iter():
            if _local(el.tag) != "Paper":
                continue
            try:
                return (float(el.get("width", 0)), float(el.get("height", 0)),
                        el.get("units", "in"))
            except ValueError:
                return None
        return None

    # -- public --------------------------------------------------------

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)

    def thumbnail_png(self, index: int = 0) -> bytes | None:
        """The plotted-sheet preview AutoCAD embeds when publishing."""
        if not 0 <= index < len(self.sheets):
            return None
        href = self.sheets[index].first("thumbnail")
        if not href:
            return None
        data = self.read(href)
        return data if data and data[:8] == b"\x89PNG\r\n\x1a\n" else None

    def graphics_stream(self, index: int = 0) -> bytes | None:
        """The raw W2D opcode stream — inflated, which for a busy sheet
        can be hundreds of megabytes."""
        if not 0 <= index < len(self.sheets):
            return None
        href = self.sheets[index].first("2d streaming graphics")
        return self.read(href) if href else None

    def close(self) -> None:
        try:
            self._zip.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def thumbnail_png(path) -> bytes | None:
    """One-shot preview extraction for the thumbnail workers."""
    try:
        with ClassicDwf(path) as doc:
            return doc.thumbnail_png(0)
    except Exception:
        return None
