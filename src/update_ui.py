"""
update_ui.py — Qt glue for the GitHub updater.

Two entry points:
  • check_silently(parent)  — run at startup and every few hours while
    the app stays open; says nothing unless an update actually exists
  • check_interactively(parent) — the Help ▸ Check for Updates action,
    which always reports its result

Network work happens on a QThread so the window never freezes, which
matters on a corporate connection where api.github.com may be slow or
blocked outright.
"""
from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal, QSettings, Qt
from PyQt6.QtWidgets import QMessageBox, QProgressDialog, QApplication

from src import updater
from src.version import __version__, APP_NAME

# The silent check runs at every launch unless one *succeeded* within
# this window. It used to be once a day, stamped before the check ran:
# open the viewer in the morning, publish a release at lunch, and nobody
# heard about it until the next day — and a check that failed (VPN down,
# proxy) still counted as done.
_CHECK_INTERVAL = 2 * 3600
# While the app stays open — people leave it running for days.
RECHECK_MS = 6 * 3600 * 1000

_KEY_LAST = "updates/last_check"
_KEY_SKIP = "updates/skipped_version"


class _CheckWorker(QThread):
    finished_check = pyqtSignal(str, object)      # outcome, dict | None

    def run(self):
        try:
            self.finished_check.emit(*updater.check())
        except Exception:
            self.finished_check.emit(updater.FAILED, None)


class _DownloadWorker(QThread):
    progress = pyqtSignal(int, int)
    done     = pyqtSignal(object)            # Path | None
    failed   = pyqtSignal(str)

    def __init__(self, url: str, name: str, parent=None):
        super().__init__(parent)
        self._url, self._name = url, name
        self.cancelled = False

    def run(self):
        def on_progress(d, t):
            self.progress.emit(d, t)
            return not self.cancelled
        try:
            self.done.emit(updater.download_installer(self._url, self._name, on_progress))
        except Exception as exc:
            self.failed.emit(str(exc))


def _settings() -> QSettings:
    return QSettings("DWGViewer", "DWGViewer")


class UpdateChecker(QObject):
    """Owns the worker threads so they are not garbage-collected mid-run."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parent = parent
        self._check: _CheckWorker | None = None
        self._dl: _DownloadWorker | None = None
        self._dlg: QProgressDialog | None = None
        self._interactive = False

    # ── Entry points ─────────────────────────────────────────────────

    def check_silently(self) -> None:
        last = float(_settings().value(_KEY_LAST, 0) or 0)
        if time.time() - last < _CHECK_INTERVAL:
            return
        self._start(interactive=False)

    def check_interactively(self) -> None:
        self._start(interactive=True)

    # ── Check ────────────────────────────────────────────────────────

    def _start(self, interactive: bool) -> None:
        if self._check is not None and self._check.isRunning():
            return
        self._interactive = interactive
        self._check = _CheckWorker(self)
        self._check.finished_check.connect(self._on_check_done)
        self._check.start()

    def _on_check_done(self, outcome: str, info) -> None:
        settings = _settings()
        if outcome != updater.FAILED:
            # Only a check that got an answer counts as done.
            settings.setValue(_KEY_LAST, time.time())
        if outcome == updater.FAILED:
            if self._interactive:
                QMessageBox.warning(
                    self._parent, "Could not check for updates",
                    "GitHub could not be reached to check for a newer "
                    f"version. You are running {APP_NAME} {__version__}.\n\n"
                    "Check the network or VPN and try again.")
            return
        if outcome == updater.LATEST:
            if self._interactive:
                QMessageBox.information(
                    self._parent, "No updates",
                    f"{APP_NAME} {__version__} is the latest version.")
            return
        # "Skip this version" only quiets the automatic check; asking
        # from the Help menu always offers it.
        if not self._interactive and settings.value(_KEY_SKIP, "") == info["version"]:
            return

        notes = (info.get("notes") or "").strip()
        if len(notes) > 900:
            notes = notes[:900].rstrip() + "…"

        box = QMessageBox(self._parent)
        box.setWindowTitle("Update available")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"{APP_NAME} {info['version']} is available.\n"
                    f"You are running {__version__}.")
        if notes:
            box.setDetailedText(notes)
        install = box.addButton("Install now", QMessageBox.ButtonRole.AcceptRole)
        skip = box.addButton("Skip this version", QMessageBox.ButtonRole.DestructiveRole)
        later = box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(install)
        box.setEscapeButton(later)
        box.exec()
        if box.clickedButton() is install:
            settings.remove(_KEY_SKIP)
            self._download(info)
        elif box.clickedButton() is skip:
            settings.setValue(_KEY_SKIP, info["version"])

    # ── Download + install ───────────────────────────────────────────

    def _download(self, info: dict) -> None:
        self._dlg = QProgressDialog("Downloading update…", "Cancel", 0, 100, self._parent)
        self._dlg.setWindowTitle("Updating")
        self._dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._dlg.setAutoClose(False)
        self._dlg.setMinimumDuration(0)

        self._dl = _DownloadWorker(info["url"], info["asset_name"], self)
        self._dl.progress.connect(self._on_progress)
        self._dl.done.connect(self._on_downloaded)
        self._dl.failed.connect(self._on_failed)
        self._dlg.canceled.connect(self._cancel)
        self._dl.start()
        self._dlg.show()

    def _cancel(self) -> None:
        if self._dl is not None:
            self._dl.cancelled = True

    def _on_progress(self, done: int, total: int) -> None:
        if self._dlg is None:
            return
        if total > 0:
            self._dlg.setMaximum(100)
            self._dlg.setValue(int(done * 100 / total))
            self._dlg.setLabelText(
                f"Downloading update…  {done/1048576:.1f} / {total/1048576:.1f} MB")
        else:
            self._dlg.setMaximum(0)

    def _on_downloaded(self, path) -> None:
        if self._dlg is not None:
            self._dlg.close()
        if not path:
            return
        QMessageBox.information(
            self._parent, "Installing",
            f"{APP_NAME} will now close and reopen with the new version.")
        QApplication.processEvents()
        try:
            updater.run_installer_and_exit(Path(path))
        except Exception as exc:
            QMessageBox.warning(self._parent, "Update failed", str(exc))

    def _on_failed(self, message: str) -> None:
        if self._dlg is not None:
            self._dlg.close()
        if self._interactive:
            QMessageBox.warning(self._parent, "Update failed", message)
