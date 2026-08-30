"""
dwf_inspect.py — dump the structure of a DWF file.

Diagnostic only: nothing in the app imports this. It answers the
questions that decide how a classic DWF has to be decoded —
which container, which version, which sections, and whether the
graphics stream is compressed.

    python tools\\dwf_inspect.py samples\\drawing.dwf
    python tools\\dwf_inspect.py samples\\*.dwf
"""
from __future__ import annotations

import re
import sys
import zipfile
import zlib
from pathlib import Path

HEADER_RE = re.compile(rb"^\((DWF|W2D|W3D) V(\d\d)\.(\d\d)\)")


def _printable(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def _hexdump(data: bytes, limit: int = 256) -> str:
    lines = []
    for off in range(0, min(len(data), limit), 16):
        chunk = data[off:off + 16]
        hexes = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        lines.append(f"    {off:04x}  {hexes}  {_printable(chunk)}")
    return "\n".join(lines)


def _header_of(data: bytes) -> str | None:
    m = HEADER_RE.match(data)
    return f"{m.group(1).decode()} V{m.group(2).decode()}.{m.group(3).decode()}" if m else None


def _describe_stream(name: str, data: bytes) -> None:
    print(f"\n  ── {name}  ({len(data):,} bytes)")
    header = _header_of(data)
    print(f"     header: {header or 'none — not a WHIP stream?'}")

    body = data
    if header:
        end = data.find(b")") + 1
        body = data[end:]

    # WHIP streams may carry a zlib-deflated payload after the header.
    for label, probe in (("raw", body), ("zlib", None)):
        if probe is None:
            for skip in range(0, 8):
                try:
                    inflated = zlib.decompressobj().decompress(body[skip:], 4096)
                except zlib.error:
                    continue
                if len(inflated) > 32:
                    print(f"     zlib payload found at +{skip}, first bytes inflate to:")
                    print(_hexdump(inflated, 128))
                    return
            print("     no zlib payload detected — opcodes appear to be stored plain")
        else:
            print(f"     first bytes ({label}):")
            print(_hexdump(probe, 192))

    # Which opcodes appear? ASCII ones are '(' + keyword, and are the
    # cheapest signal of what the stream actually contains.
    keywords = sorted({m.decode("latin-1") for m in
                       re.findall(rb"\(([A-Za-z][A-Za-z0-9_ ]{1,24})", body[:200000])})
    if keywords:
        print(f"     ASCII opcodes seen ({len(keywords)}): {', '.join(keywords[:40])}")


def inspect(path: Path) -> None:
    print("=" * 72)
    print(f"{path.name}   ({path.stat().st_size:,} bytes)")
    print("=" * 72)

    head = path.read_bytes()[:64]
    print(f"file header: {_header_of(head) or 'none'}")
    print(f"first bytes:\n{_hexdump(head, 64)}")

    if not zipfile.is_zipfile(path):
        print("\nNOT a zip container — this is a pre-6.0 DWF: one continuous "
              "WHIP stream in the file itself.")
        _describe_stream(path.name, path.read_bytes())
        return

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        print(f"\nzip container with {len(names)} entries:")
        for info in z.infolist():
            print(f"    {info.file_size:>10,}  {info.filename}")

        for manifest in [n for n in names if n.lower().endswith(".xml")][:4]:
            text = z.read(manifest).decode("utf-8", "ignore")
            print(f"\n  ── {manifest} (first 1500 chars)")
            print("     " + text[:1500].replace("\n", "\n     "))

        for graphics in [n for n in names if n.lower().endswith((".w2d", ".w3d"))]:
            _describe_stream(graphics, z.read(graphics))


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv[1:]]
    if not paths:
        paths = sorted(Path("samples").glob("*.dwf*"))
    if not paths:
        print("usage: python tools/dwf_inspect.py <file.dwf> [...]")
        print("       (or drop files into samples/ and run with no arguments)")
        return 1
    for p in paths:
        if p.is_file():
            inspect(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
