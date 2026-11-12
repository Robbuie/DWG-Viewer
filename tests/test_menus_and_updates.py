"""
test_menus_and_updates.py — the menu bar, and the update check.

Menus: every action in them is the same QAction the toolbar uses, so
there is exactly one shortcut per key — Qt silently fires *nothing* for
a key bound twice, which is the bug this mostly guards against.

Updates: a check that fails must not count as "checked", and the silent
check must not wait a day.

Runs standalone or under pytest, on Qt's offscreen platform.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtGui import QKeySequence
    from PyQt6.QtWidgets import QApplication, QMessageBox
except ImportError as exc:                            # pragma: no cover
    import unittest
    raise unittest.SkipTest(f"Qt unavailable: {exc}")

from src import updater, update_ui  # noqa: E402

_app = QApplication.instance() or QApplication([])
QCoreApplication.setOrganizationName("DWGViewerTests")
QCoreApplication.setApplicationName("test_menus")

_INI = Path(tempfile.mkdtemp(prefix="dwgv_upd_")) / "settings.ini"


def _isolate():
    """Update settings in a temp ini, not the user's registry."""
    if _INI.exists():
        _INI.unlink()
    update_ui._settings = lambda: QSettings(str(_INI), QSettings.Format.IniFormat)


def _window():
    from src.main_window import MainWindow
    w = MainWindow()
    w._update_timer.stop()
    return w


# ── menus ────────────────────────────────────────────────────────────

def test_menu_bar_has_the_usual_menus():
    w = _window()
    titles = [a.text().replace("&", "") for a in w.menuBar().actions()]
    assert titles == ["File", "Edit", "View", "Tools", "Help"]
    w.close()


def test_no_key_is_bound_twice():
    from PyQt6.QtGui import QAction
    w = _window()
    seen = {}
    for act in w.findChildren(QAction):
        for seq in act.shortcuts():
            key = seq.toString()
            if not key:
                continue
            # Actions on child widgets with a narrower context (the find
            # field's Esc, the 3D view's own keys) do not compete.
            assert key not in seen, f"{key}: {seen.get(key)!r} and {act.text()!r}"
            if act.parent() is w:
                seen[key] = act.text()
    w.close()


def test_menus_and_toolbar_share_actions():
    w = _window()
    file_actions = w._menu_file.actions()
    assert w._act_open in file_actions and w._act_print in file_actions
    # Menu shows a plain label; the toolbar button keeps its glyph.
    assert w._act_open.text() == "&Open Folder…"
    assert "Open Folder" in w._open_btn.text() and w._open_btn.text() != w._act_open.text()
    # Toggling from the menu is the same toggle as the toolbar.
    w._act_layers_panel.trigger()
    assert not w._act_layers_panel.isChecked()
    w._act_layers_panel.trigger()
    w.close()


def test_shortcut_list_comes_from_the_menus():
    w = _window()
    rows = w._shortcut_rows()
    keys = {label: key for _, label, key in rows}
    assert keys["Open Folder"] == QKeySequence("Ctrl+O").toString(
        QKeySequence.SequenceFormat.NativeText)
    assert keys["Find Next"] == "F3"
    assert keys["Front"] == "1"            # a hint from the 3D menu
    w.close()


def test_3d_menu_only_with_a_model():
    w = _window()
    assert not w._menu_3d.isEnabled()
    w._set_3d_mode(True)
    assert w._menu_3d.isEnabled()
    assert not w._act_print.isEnabled()
    w._set_3d_mode(False)
    assert not w._menu_3d.isEnabled()
    assert w._act_print.isEnabled()
    w.close()


def test_toolbar_lost_the_help_items():
    """They live in Help and View now."""
    w = _window()
    texts = [a.iconText() for tb in (w.findChildren(type(w._markup_bar)))
             for a in tb.actions()]
    assert not any("Check for Updates" in t or "About" == t for t in texts)
    assert w._act_update in w._menu_help.actions()
    w.close()


# ── update check outcomes ────────────────────────────────────────────

def _release(tag, assets=("DWGViewer-Setup-9.9.9.exe",), draft=False):
    return {"tag_name": tag, "draft": draft, "body": "notes",
            "assets": [{"name": n, "browser_download_url": f"https://x/{n}",
                        "size": 1} for n in assets]}


def _with_release(release):
    updater.fetch_latest_release = lambda: release


_REAL_FETCH = updater.fetch_latest_release


def test_check_outcomes():
    try:
        _with_release(None)
        assert updater.check() == (updater.FAILED, None)
        _with_release(_release("v0.0.1"))
        assert updater.check()[0] == updater.LATEST
        _with_release(_release("v99.0.0"))
        outcome, info = updater.check()
        assert outcome == updater.UPDATE and info["version"] == "99.0.0"
        _with_release(_release("v99.0.0", assets=()))   # installer not up yet
        assert updater.check()[0] == updater.FAILED
        _with_release(_release("v99.0.0", draft=True))
        assert updater.check()[0] == updater.LATEST
    finally:
        updater.fetch_latest_release = _REAL_FETCH


def _run_check(checker, interactive):
    checker._interactive = interactive
    checker._on_check_done(*updater.check())


def test_failed_check_is_not_remembered():
    _isolate()
    shown = []
    real = QMessageBox.exec
    QMessageBox.exec = lambda self: shown.append(self.windowTitle()) or 0
    real_warn = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: shown.append("warning"))
    try:
        c = update_ui.UpdateChecker(None)
        _with_release(None)
        _run_check(c, interactive=False)
        assert update_ui._settings().value("updates/last_check") is None
        assert shown == []                    # silent stays silent
        _run_check(c, interactive=True)
        assert shown == ["warning"]           # and asking says it failed
        _with_release(_release("v0.0.1"))
        _run_check(c, interactive=False)
        assert float(update_ui._settings().value("updates/last_check")) > time.time() - 5
    finally:
        updater.fetch_latest_release = _REAL_FETCH
        QMessageBox.exec = real
        QMessageBox.warning = real_warn


def test_silent_check_runs_again_within_the_day():
    _isolate()
    c = update_ui.UpdateChecker(None)
    started = []
    c._start = lambda interactive: started.append(interactive)
    update_ui._settings().setValue("updates/last_check", time.time() - 3 * 3600)
    c.check_silently()
    assert started == [False]
    update_ui._settings().setValue("updates/last_check", time.time() - 60)
    c.check_silently()
    assert started == [False]                 # but not every minute


def test_skip_this_version_quiets_only_the_silent_check():
    _isolate()
    prompts = []

    def fake_exec(box):
        prompts.append(box.text())
        skip = [b for b in box.buttons() if b.text() == "Skip this version"][0]
        box.clickedButton = lambda: skip
        return 0
    real = QMessageBox.exec
    QMessageBox.exec = fake_exec
    try:
        _with_release(_release("v99.0.0"))
        c = update_ui.UpdateChecker(None)
        _run_check(c, interactive=False)
        assert len(prompts) == 1
        assert update_ui._settings().value("updates/skipped_version") == "99.0.0"
        _run_check(c, interactive=False)
        assert len(prompts) == 1              # skipped
        _run_check(c, interactive=True)
        assert len(prompts) == 2              # asked for: always offered
        _with_release(_release("v99.0.1"))
        _run_check(c, interactive=False)
        assert len(prompts) == 3              # a newer one is offered again
    finally:
        updater.fetch_latest_release = _REAL_FETCH
        QMessageBox.exec = real


def _main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {exc!r}")
        else:
            print(f"  ok   {name}")
    print("all passed" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
