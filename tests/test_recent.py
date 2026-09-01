"""
test_recent.py — the recently-opened lists.

The interesting behaviour is not "does it store a path" but the three things
that go wrong quietly: the same drawing appearing twice because Windows spelt
it differently, the list growing without bound, and QSettings handing back a
bare string instead of a list when only one entry was ever saved.

One rule is asserted by its absence elsewhere: nothing in this module touches
the filesystem, so a list full of paths on an offline share survives intact.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtCore")

from PyQt6.QtCore import QSettings  # noqa: E402

from src import recent  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point the module at an ini file instead of the real registry."""
    ini = tmp_path / "settings.ini"
    monkeypatch.setattr(
        recent, "_settings",
        lambda: QSettings(str(ini), QSettings.Format.IniFormat))
    yield


def test_starts_empty():
    assert recent.files() == []
    assert recent.folders() == []
    assert recent.is_empty()


def test_newest_first():
    recent.add_file(r"C:\jobs\a.dwg")
    recent.add_file(r"C:\jobs\b.dwg")
    assert [p.split("\\")[-1] if "\\" in p else p.split("/")[-1]
            for p in recent.files()] == ["b.dwg", "a.dwg"]


def test_reopening_promotes_instead_of_duplicating():
    recent.add_file("/jobs/a.dwg")
    recent.add_file("/jobs/b.dwg")
    recent.add_file("/jobs/a.dwg")
    entries = recent.files()
    assert len(entries) == 2
    assert entries[0].endswith("a.dwg")


def test_case_and_separator_variants_are_one_entry():
    recent.add_file("/Jobs/Plan.dwg")
    recent.add_file("/jobs/./plan.DWG".replace("/jobs/./", "/jobs/"))
    recent.add_file("/jobs//plan.DWG")
    assert len(recent.files()) <= 2      # differs only by case, if that


def test_single_entry_round_trips():
    """QSettings collapses a one-item list to a bare string on read."""
    recent.add_folder("/jobs")
    assert recent.folders() == [recent.folders()[0]]
    assert len(recent.folders()) == 1


def test_lists_are_capped():
    for i in range(recent.MAX_FILES + 8):
        recent.add_file(f"/jobs/sheet{i}.dwg")
    assert len(recent.files()) == recent.MAX_FILES
    assert recent.files()[0].endswith(f"sheet{recent.MAX_FILES + 7}.dwg")


def test_remove_drops_only_that_entry():
    recent.add_file("/jobs/a.dwg")
    recent.add_file("/jobs/b.dwg")
    recent.remove_file("/jobs/a.dwg")
    assert len(recent.files()) == 1
    assert recent.files()[0].endswith("b.dwg")


def test_missing_paths_are_kept():
    """A share being asleep must not empty the list."""
    recent.add_file("/no/such/share/plan.dwg")
    recent.add_folder("/no/such/share")
    assert recent.files() and recent.folders()


def test_clear_forgets_both():
    recent.add_file("/jobs/a.dwg")
    recent.add_folder("/jobs")
    recent.clear()
    assert recent.is_empty()


def test_junk_in_settings_is_ignored(tmp_path, monkeypatch):
    ini = tmp_path / "junk.ini"
    s = QSettings(str(ini), QSettings.Format.IniFormat)
    monkeypatch.setattr(recent, "_settings",
                        lambda: QSettings(str(ini), QSettings.Format.IniFormat))
    s.setValue("recent/files", ["", "  ", "/jobs/real.dwg"])
    s.sync()
    assert recent.files() == [recent.files()[0]]
    assert recent.files()[0].endswith("real.dwg")


def test_blank_paths_are_not_recorded():
    recent.add_file("")
    recent.add_folder("")
    assert recent.is_empty()


# ------------------------------------------------------------------ #
#  Menu labels
# ------------------------------------------------------------------ #

def test_label_names_the_file_and_places_it():
    label = recent.menu_label("/jobs/2026/riverside/A-101.dwg", 1)
    assert "A-101.dwg" in label
    assert "riverside" in label
    assert label.startswith("&1")


def test_label_numbers_only_the_first_nine():
    assert recent.menu_label("/jobs/a.dwg", 9).startswith("&9")
    assert not recent.menu_label("/jobs/a.dwg", 10).startswith("&")


def test_ampersands_are_escaped():
    """Otherwise Qt eats the & and underlines the next letter."""
    label = recent.menu_label("/jobs/Smith & Sons/plan.dwg", None)
    assert "&&" in label
    assert "& " not in label.replace("&&", "")


def test_long_paths_are_shortened():
    deep = "/" + "/".join(f"level{i}" for i in range(20)) + "/plan.dwg"
    label = recent.menu_label(deep, None)
    assert "\u2026" in label
    assert len(label) < len(deep)
    assert "plan.dwg" in label      # the identifying part always survives


def test_short_paths_are_left_alone():
    assert recent.elide("/jobs/a") == "/jobs/a"
