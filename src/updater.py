"""
updater.py — check GitHub Releases for a newer build and install it.

How it works
────────────
1. GET https://api.github.com/repos/{owner}/{repo}/releases/latest
2. Compare the release's tag (v1.2.3) against src/version.__version__
3. If newer, download the .exe installer asset attached to that release
4. Run it silently; Inno Setup closes the running app, replaces it,
   and relaunches

Only the standard library is used, so this adds no dependency to the
frozen build. Everything is best-effort: no network, a rate limit, a
malformed release — the app carries on without complaint.

Note on install location: the installer is per-user
(%LOCALAPPDATA%\\Programs), which means the silent update runs without
a UAC prompt. A Program Files install would prompt for admin rights on
every update and defeat the point.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from src.version import __version__, GITHUB_OWNER, GITHUB_REPO, APP_NAME

API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
_TIMEOUT = 12
_UA = f"{APP_NAME}/{__version__}"


class UpdateError(Exception):
    pass


# ── Version comparison ────────────────────────────────────────────────

def parse_version(text: str) -> tuple:
    """'v1.10.2' -> (1, 10, 2). Unparseable input sorts lowest.

    Numeric comparison matters: a plain string compare would rank
    '1.9.0' above '1.10.0'.
    """
    nums = re.findall(r"\d+", (text or "").strip().lstrip("vV"))
    if not nums:
        return (0,)
    return tuple(int(n) for n in nums[:4])


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


# ── Release lookup ────────────────────────────────────────────────────

def fetch_latest_release() -> dict | None:
    """Return the latest release as a dict, or None if unavailable."""
    req = urllib.request.Request(
        API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": _UA},
    )
    token = os.environ.get("DWGVIEWER_GITHUB_TOKEN")
    if token:                      # only needed if the repo is private
        req.add_header("Authorization", f"Bearer {token}")
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def installer_asset(release: dict) -> dict | None:
    """Pick the installer .exe from a release's assets."""
    for asset in release.get("assets") or []:
        name = (asset.get("name") or "").lower()
        if name.endswith(".exe") and "setup" in name:
            return asset
    for asset in release.get("assets") or []:
        if (asset.get("name") or "").lower().endswith(".exe"):
            return asset
    return None


def check_for_update() -> dict | None:
    """
    Return {'version', 'tag', 'notes', 'url', 'asset_name', 'size'} when a
    newer release exists, else None. Never raises.
    """
    release = fetch_latest_release()
    if not release or release.get("draft"):
        return None
    tag = release.get("tag_name") or ""
    if not is_newer(tag):
        return None
    asset = installer_asset(release)
    if not asset:
        return None
    return {
        "version":    tag.lstrip("vV"),
        "tag":        tag,
        "notes":      release.get("body") or "",
        "url":        asset.get("browser_download_url"),
        "asset_name": asset.get("name"),
        "size":       asset.get("size") or 0,
    }


# ── Download + install ────────────────────────────────────────────────

def download_installer(url: str, filename: str, progress=None) -> Path:
    """Download the installer to a temp file. progress(done, total) is
    called as bytes arrive; return False from it to cancel."""
    target = Path(tempfile.gettempdir()) / f"dwgviewer_update_{filename}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    token = os.environ.get("DWGVIEWER_GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    ctx = ssl.create_default_context()
    tmp = target.with_suffix(".part")
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress and progress(done, total) is False:
                        raise UpdateError("Update cancelled.")
        os.replace(tmp, target)
        return target
    except UpdateError:
        tmp.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"Could not download the update: {exc}") from exc


def run_installer_and_exit(installer: Path) -> None:
    """Launch the installer silently and quit so it can replace our files.

    /CLOSEAPPLICATIONS + /RESTARTAPPLICATIONS let Inno Setup shut this
    process down cleanly and start the new version afterwards.
    """
    if os.name != "nt":
        raise UpdateError("Automatic update is only supported on Windows.")
    flags = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
             "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
    creationflags = 0x00000008 | 0x00000200      # DETACHED | NEW_PROCESS_GROUP
    subprocess.Popen([str(installer), *flags], close_fds=True,
                     creationflags=creationflags)
    sys.exit(0)


def is_frozen() -> bool:
    """True when running from the PyInstaller build rather than source."""
    return getattr(sys, "frozen", False)
