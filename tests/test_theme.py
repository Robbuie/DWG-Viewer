"""
test_theme.py — the appearance system's invariants.

These are the rules that are easy to break by hand and impossible to notice
by eye, because breaking one only shows up in the fourth theme somebody tries
six months from now:

  * every combination of the three axes resolves to a complete stylesheet;
  * no literal colour is left in the stylesheet, so nothing can silently stop
    tracking the theme or the accent picker;
  * every tint of the accent actually moves when the accent does;
  * the settings normalisers never hand back junk, whatever is in the file.
"""
from __future__ import annotations

import re
import sys

import pytest

pytest.importorskip("PyQt6.QtGui")

from src import theme  # noqa: E402


ALL = [(t, a, d) for t in theme.THEMES for a in theme.ACCENTS
       for d in theme.DENSITIES]


def test_every_combination_resolves():
    """90 combinations, no unknown token and nothing left unsubstituted."""
    for t, a, d in ALL:
        sheet = theme.stylesheet(t, a, d)
        assert "var(--" not in sheet, f"unresolved token in {t}/{a}/{d}"
        assert sheet.strip()


def test_no_literal_colour_in_the_stylesheet():
    """A hardcoded colour is a spot that stops following the theme.

    The sheet is allowed exactly one literal — #ffffff, the text on the
    accent-filled primary button, which has to stay white whatever the accent
    is because the fill is always dark enough to carry it.
    """
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", theme._QSS)
    assert set(literals) <= {"#ffffff"}, f"literal colours in sheet: {literals}"
    assert "rgba(" not in theme._QSS and "rgb(" not in theme._QSS


def test_every_accent_tint_tracks_the_picker():
    """Change the accent and every derived tint has to move with it."""
    names = [k for k in theme._accent_tokens("redline") if k.startswith("accent")]
    assert len(names) >= 8
    for name in names:
        seen = {theme._accent_tokens(a)[name] for a in theme.ACCENTS}
        assert len(seen) == len(theme.ACCENTS), f"--{name} does not track the accent"


def test_accent_text_is_readable_on_the_chrome_it_sits_on():
    """`accent-on-chrome` has to contrast with bg-1 in every theme.

    This is the one that regressed: the white-lifted accent is fine on the
    dark chrome and unreadable on the light one.
    """
    from PyQt6.QtGui import QColor

    for t in theme.THEMES:
        for a in theme.ACCENTS:
            v = theme.tokens(t, a)
            fg = QColor(v["accent-on-chrome"]).lightness()
            bg = QColor(v["bg-1"]).lightness()
            assert abs(fg - bg) > 40, f"{t}/{a}: accent text too close to chrome"


def test_the_drawing_backdrop_never_goes_light():
    """The renderer maps ACI 7 and ByLayer to white, so a light backdrop
    hides most of a typical sheet. `drawing-bg` is the one surface that is
    allowed to ignore the theme, and it has to stay dark in all five."""
    from PyQt6.QtGui import QColor

    for t in theme.THEMES:
        bg = QColor(theme.tokens(t)["drawing-bg"])
        assert bg.lightness() < 60, f"{t}: drawing backdrop would hide white ink"


def test_densities_change_every_metric_they_claim_to():
    base = set(theme._BASE_METRICS)
    for name, spec in theme.DENSITIES.items():
        if name == "normal":
            assert spec["metrics"] == {}, "normal is the base set and restates nothing"
        else:
            assert set(spec["metrics"]) == base, f"{name} is missing a metric"


def test_themes_only_restate_colours():
    """A theme may not carry an accent or a density — that is the whole point
    of keeping the three axes independent."""
    metrics = set(theme._BASE_METRICS)
    for name, spec in theme.THEMES.items():
        keys = set(spec["tokens"])
        assert not keys & metrics, f"{name} sets a density metric"
        assert not any(k.startswith("accent") for k in keys), f"{name} sets an accent"
        assert keys <= set(theme._BASE), f"{name} invents a token"


@pytest.mark.parametrize("junk", [None, "", "nope", 7, [], {"a": 1}, "DARK"])
def test_normalisers_never_return_junk(junk):
    """Settings from a later build, hand-edited, or from a version where the
    option did not exist must not be able to produce unreadable chrome."""
    assert theme.theme_of(junk) in theme.THEMES
    assert theme.accent_of(junk) in theme.ACCENTS
    assert theme.density_of(junk) in theme.DENSITIES
    # And a bad value must not swallow the app's own default either.
    assert theme.accent_of(junk, "blue") == "blue"


def test_unknown_token_in_a_sheet_is_a_loud_failure():
    with pytest.raises(KeyError):
        theme._resolve("QWidget { color: var(--not-a-token); }",
                       theme.tokens())
