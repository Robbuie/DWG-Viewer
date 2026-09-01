"""
Shared appearance system — themes, accent and chrome density.

This is a port of the design system in the Redline PDF app
(`src/css/app.css` + `src/js/appearance.js`) to Qt stylesheets, so the two
applications read as one product. It holds nothing specific to this app and
is meant to be copied verbatim into the next one: import it, call
`apply_saved(app)` at startup, and give widgets the object names listed at
the bottom of this file.

THE THREE AXES ARE INDEPENDENT, AND THAT IS DELIBERATE
------------------------------------------------------
A theme sets the greys. The accent sets one colour. The density sets the
chrome metrics. Anything that mixes them — a theme that hardcodes a red, a
density that also shifts a colour — means the combinations multiply and half
of them look wrong. There are 5 x 6 x 3 = 90 combinations here and none of
them needs its own block.

THE ACCENT IS ONE CHANNEL TRIPLE, NOT A HEX
-------------------------------------------
Every tint of it — the fill behind a checked tool button, the border on an
active chip, the progress chunk — is derived from `--accent-rgb` by the
helpers in `_accent_tokens`. Writing a literal accent-looking colour into
the stylesheet below re-pins that one spot to whatever the default happens
to be and it stops tracking the picker, which reads as a picker that half
works. `tests/test_theme.py` fails on a literal accent in the sheet.

Token names are the CSS custom-property names, unchanged, and the stylesheet
is written with `var(--bg-1)` syntax that `_resolve` substitutes. That is on
purpose: a change to app.css can be diffed straight against this file.
"""
from __future__ import annotations

import re
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QColor, QPalette

# --------------------------------------------------------------------------
#  Catalogs
# --------------------------------------------------------------------------

# `dark` is the base set and every other theme restates only the greys it
# changes; the accent and the density come from their own axes. It is listed
# here anyway — the settings dialog is built from this catalog, and a catalog
# that omits the default is one the UI cannot offer.
_BASE = {
    "bg-0": "#101216",          # app backdrop
    "bg-1": "#171a20",          # chrome
    "bg-2": "#1d2128",          # raised chrome
    "bg-3": "#252a33",          # hover
    "bg-4": "#2f3540",          # active / borders-strong
    "canvas-bg": "#0b0d10",     # panel behind the drawing area
    "line": "#2a2f38",
    "line-soft": "#22262d",
    "txt-0": "#e7ebf2",
    "txt-1": "#aab3c0",
    "txt-2": "#79828f",
    "info": "#4aa8ff",
    "good": "#46c98b",
    "warn": "#f2c14e",
    "sel": "#4aa8ff",
    "page-ring": "rgba(0, 0, 0, 140)",
    "radius": "7px",
    "radius-sm": "5px",
    "font": '"Segoe UI", Inter, system-ui, sans-serif',
    "mono": '"Cascadia Mono", Consolas, monospace',
}

THEMES: dict[str, dict] = {
    "dark": {
        "label": "Dark (CAD pro)",
        "note": "The default. Neutral greys, drawing forward.",
        "tokens": {},
    },
    "light": {
        "label": "Light",
        "note": "For bright rooms and shared screens.",
        "tokens": {
            "bg-0": "#eceef2", "bg-1": "#f7f8fa", "bg-2": "#ffffff",
            "bg-3": "#e8ebf0", "bg-4": "#d7dbe2", "canvas-bg": "#c9ced6",
            "line": "#d5d9e0", "line-soft": "#e3e6eb",
            "txt-0": "#14181f", "txt-1": "#4a525e", "txt-2": "#7b838f",
            "page-ring": "rgba(0, 0, 0, 30)",
        },
    },
    "paper": {
        "label": "Warm paper",
        "note": "Light, off-white. Easier over a long review.",
        # The canvas behind the sheet is a desk rather than a screen, and the
        # chrome is knocked off pure white — for people who review on a bright
        # monitor all day.
        "tokens": {
            "bg-0": "#ece7dd", "bg-1": "#f7f3ea", "bg-2": "#fffdf8",
            "bg-3": "#eae4d7", "bg-4": "#d9d1c0", "canvas-bg": "#c8bfae",
            "line": "#d8d0c0", "line-soft": "#e6e0d3",
            "txt-0": "#211d17", "txt-1": "#564f43", "txt-2": "#857c6c",
            "page-ring": "rgba(60, 48, 30, 40)",
        },
    },
    "blueprint": {
        "label": "Blueprint",
        "note": "Deep blue chrome; the sheet is the only warm thing on screen.",
        "tokens": {
            "bg-0": "#0a1220", "bg-1": "#0f1b2d", "bg-2": "#142339",
            "bg-3": "#1b2f4a", "bg-4": "#26405f", "canvas-bg": "#060d17",
            "line": "#21374f", "line-soft": "#182b40",
            "txt-0": "#e2ecf8", "txt-1": "#9fb4cc", "txt-2": "#6d8299",
            "info": "#6cc0ff", "sel": "#6cc0ff",
            "page-ring": "rgba(0, 0, 0, 153)",
        },
    },
    "contrast": {
        "label": "High contrast",
        "note": "Maximum separation — an accessibility target, not a style.",
        # Not a style. Text goes to pure white on near-black, every border is a
        # visible line rather than a hint, and the muted grey is lifted until it
        # passes as body text, because in this theme it is being read rather
        # than skimmed.
        "tokens": {
            "bg-0": "#000000", "bg-1": "#0a0a0c", "bg-2": "#141418",
            "bg-3": "#23232a", "bg-4": "#3a3a45", "canvas-bg": "#000000",
            "line": "#55555f", "line-soft": "#3d3d46",
            "txt-0": "#ffffff", "txt-1": "#e4e4ea", "txt-2": "#b6b6c0",
            "info": "#7cc4ff", "good": "#5ee0a0", "warn": "#ffd75e",
            "sel": "#7cc4ff",
            "page-ring": "rgba(255, 255, 255, 89)",
        },
    },
}

# Channel triples, not hex, because every tint is derived with an alpha or a
# mix. See the note at the top of this file.
ACCENTS: dict[str, dict] = {
    "redline": {"label": "Redline red", "rgb": (255, 91, 74)},
    "amber": {"label": "Amber", "rgb": (242, 165, 60)},
    "green": {"label": "Field green", "rgb": (70, 201, 139)},
    "cyan": {"label": "Cyan", "rgb": (54, 191, 210)},
    "blue": {"label": "Drafting blue", "rgb": (74, 145, 255)},
    "violet": {"label": "Violet", "rgb": (154, 122, 255)},
}

# One drawing on a laptop wants the chrome out of the way; the same app on a
# 4K panel wants it legible. Both are the same handful of numbers. `normal` is
# the base set and restates nothing.
_BASE_METRICS = {
    "ui-font": "13px",
    "tb-h": "34px",
    "row-h": "40px",
    "status-h": "26px",
    "side-w": "268px",
    "tbtn-h": "30px",
    "icon": "18px",
    "field-h": "28px",
    "small-font": "11px",
}

DENSITIES: dict[str, dict] = {
    "compact": {
        "label": "Compact",
        "note": "Least chrome — more sheet on a laptop.",
        "metrics": {
            "ui-font": "12px", "tb-h": "30px", "row-h": "34px",
            "status-h": "22px", "side-w": "238px", "tbtn-h": "26px",
            "icon": "16px", "field-h": "24px", "small-font": "10px",
        },
    },
    "normal": {"label": "Normal", "note": "The default.", "metrics": {}},
    "large": {
        "label": "Large",
        "note": "Bigger targets and type for high-DPI panels.",
        "metrics": {
            "ui-font": "15px", "tb-h": "38px", "row-h": "46px",
            "status-h": "30px", "side-w": "304px", "tbtn-h": "34px",
            "icon": "21px", "field-h": "32px", "small-font": "12px",
        },
    },
}

# The module's own fallbacks. An app that wants different ones passes them to
# `apply_saved` rather than editing this — the file is shared.
DEFAULTS = {"theme": "dark", "accent": "redline", "density": "normal"}


# --------------------------------------------------------------------------
#  Normalisers
#
#  Everything here takes whatever was in the settings file and hands back
#  something the stylesheet can use. Settings written by a later build, or
#  hand-edited, or left over from a version where the option did not exist,
#  must not be able to put the app into a state with no readable chrome. That
#  is why nothing below trusts its input, and why the applying functions call
#  the normalisers rather than the other way round.
# --------------------------------------------------------------------------

def _pick(catalog: dict, value, fallback: str, default: str) -> str:
    """The one place a stored value is turned into a key of `catalog`.

    `value` is whatever came out of the settings file, and QSettings hands
    back more shapes than it looks like it can: a string with a comma in it
    comes back as a *list*, an unset key as None, and a hand-edited file can
    hold anything at all. `x in catalog` raises TypeError on an unhashable
    one, which crashed the app during startup — before any window existed to
    show the error in. Hence the isinstance guard rather than a try/except:
    the rule is that nothing here can raise, ever.
    """
    if isinstance(value, str) and value in catalog:
        return value
    return fallback if fallback in catalog else default


def theme_of(value, fallback: str | None = None) -> str:
    return _pick(THEMES, value, fallback, DEFAULTS["theme"])


def accent_of(value, fallback: str | None = None) -> str:
    return _pick(ACCENTS, value, fallback, DEFAULTS["accent"])


def density_of(value, fallback: str | None = None) -> str:
    return _pick(DENSITIES, value, fallback, DEFAULTS["density"])


# --------------------------------------------------------------------------
#  Colour maths
#
#  The CSS does this with color-mix() and rgba(). Qt stylesheets have neither,
#  so the mixing happens here and the sheet only ever sees a finished colour.
# --------------------------------------------------------------------------

def _rgb(value) -> tuple[int, int, int]:
    """Accept '#rrggbb' or an (r, g, b) triple."""
    if isinstance(value, (tuple, list)):
        return tuple(int(c) for c in value[:3])  # type: ignore[return-value]
    c = QColor(value)
    return (c.red(), c.green(), c.blue())


def _mix(a, b, weight: float) -> str:
    """`weight` of colour `a`, the remainder of `b` — color-mix(in srgb, …)."""
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    w = max(0.0, min(1.0, weight))
    return "#%02x%02x%02x" % (
        round(ar * w + br * (1 - w)),
        round(ag * w + bg * (1 - w)),
        round(ab * w + bb * (1 - w)),
    )


def _rgba(rgb: tuple[int, int, int], alpha: float) -> str:
    return "rgba(%d, %d, %d, %.3f)" % (rgb[0], rgb[1], rgb[2], alpha)


def _accent_tokens(accent_id: str) -> dict[str, str]:
    """Every tint the sheet is allowed to use, derived from the one triple."""
    rgb = ACCENTS[accent_id]["rgb"]
    return {
        "accent": "rgb(%d, %d, %d)" % rgb,
        "accent-dim": _mix(rgb, "#000000", 0.70),
        "accent-text": _mix(rgb, "#ffffff", 0.74),
        "accent-lift": _mix(rgb, "#ffb066", 0.62),
        # The gradient top on a primary button. The CSS pins this to a literal
        # #ff6a58, which only works while the accent is the red; derived here
        # so the button tracks the picker like everything else.
        "accent-hi": _mix(rgb, "#ffffff", 0.86),
        "accent-soft": _rgba(rgb, 0.14),
        "accent-wash": _rgba(rgb, 0.17),
        "accent-line": _rgba(rgb, 0.45),
        "accent-glow": _rgba(rgb, 0.80),
    }


def tokens(theme: str = "dark", accent: str = "redline",
           density: str = "normal") -> dict[str, str]:
    """The complete resolved token set for one combination of the three axes.

    Code that needs a colour outside the stylesheet — the canvas backdrop, a
    painted rubber-band, a chart series — reads it from here rather than
    hardcoding one, which is what keeps those surfaces on the theme too.
    """
    theme, accent, density = theme_of(theme), accent_of(accent), density_of(density)
    out = dict(_BASE)
    out.update(THEMES[theme]["tokens"])
    out.update(_BASE_METRICS)
    out.update(DENSITIES[density]["metrics"])
    out.update(_accent_tokens(accent))
    # Themes are light or dark, and a few rules need to know which — a hover
    # lift that works on #101216 is invisible on #f7f8fa. One flag beats
    # per-theme special cases scattered through the sheet.
    out["is-light"] = "1" if QColor(out["bg-1"]).lightness() > 127 else "0"
    # `accent-text` is the accent lifted towards white, which reads well on the
    # dark chrome and turns into pale-blue-on-pale-blue the moment the chrome
    # is white. app.css solves this with a `body.theme-light` carve-out; doing
    # it as a token instead means the rules that use it never have to know
    # which theme is running, and the warm-paper theme gets the fix for free.
    out["accent-on-chrome"] = (out["accent-dim"] if out["is-light"] == "1"
                               else out["accent-text"])

    # THE DRAWING BACKDROP DOES NOT FOLLOW THE THEME, AND THAT IS NOT AN
    # OVERSIGHT. converter.py maps ACI 7 and 256 — "white" and ByLayer, which
    # between them are most of the entities in a typical sheet — to #ffffff.
    # Put that on a light backdrop and the drawing disappears: not a theme
    # that looks wrong, a viewer that shows nothing. The PDF app can tint its
    # canvas because a PDF page carries its own white; a DWG does not.
    #
    # It is a token rather than a literal so it is still stated in one place,
    # and so the day the renderer learns to invert its ink for a light
    # backdrop, this is the line that changes.
    out["drawing-bg"] = "#0b0d10" if out["is-light"] == "0" else "#14171c"
    return out


_VAR_RE = re.compile(r"var\(--([a-z0-9-]+)\)")


def _resolve(sheet: str, values: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in values:
            raise KeyError(f"theme.py: stylesheet uses unknown token --{name}")
        return values[name]
    return _VAR_RE.sub(sub, sheet)


# --------------------------------------------------------------------------
#  The stylesheet
#
#  Written with `var(--token)` so it can be read side by side with app.css.
#  Nothing below may contain a literal grey or a literal accent-looking
#  colour: a hardcoded value is a spot that silently stops following the
#  theme, and those are only ever found by someone switching to the light
#  theme and seeing one panel stay black.
#
#  Two Qt-specific carve-outs, both deliberate:
#    * Check and radio indicators are left to the Fusion style and the
#      palette. Styling `::indicator` without supplying an image loses the
#      tick mark entirely, which is worse than an indicator half a shade off.
#    * The combo box arrow is likewise left alone for the same reason; only
#      the frame around it is themed. Styling `::drop-down`
#      at all — even only its border — replaces the subcontrol and the arrow
#      vanishes, leaving a combo box with no sign that it opens.
# --------------------------------------------------------------------------

_QSS = """
/* --- base ------------------------------------------------------------- */
QWidget {
    background: var(--bg-0);
    color: var(--txt-0);
    font-family: var(--font);
    font-size: var(--ui-font);
}
QMainWindow, QDialog { background: var(--bg-0); }
QMainWindow::separator { background: var(--line); width: 1px; height: 1px; }

QToolTip {
    background: var(--bg-2);
    color: var(--txt-0);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    padding: 4px 7px;
}

/* --- toolbars ---------------------------------------------------------- */
/* The action row is flat chrome; the tool row is lifted off it with a
   gradient, the same two-row split as the PDF app. */
QToolBar {
    background: var(--bg-1);
    border: 0px;
    border-bottom: 1px solid var(--line);
    spacing: 4px;
    padding: 0px 6px;
    min-height: var(--row-h);
}
QToolBar#toolsBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 var(--bg-2), stop:1 var(--bg-1));
}
QToolBar#findBar {
    background: var(--bg-1);
    border-bottom: 0px;
    border-top: 1px solid var(--line);
    padding: 2px 6px;
}
QToolBar::separator {
    background: var(--line);
    width: 1px;
    margin: 0px 8px;
}

QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    color: var(--txt-1);
    padding: 0px 10px;
    min-height: var(--tbtn-h);
}
QToolButton:hover { background: var(--bg-3); color: var(--txt-0); }
QToolButton:pressed { background: var(--bg-4); }
QToolButton:disabled { color: var(--txt-2); background: transparent; }
/* An armed tool. Accent fill, accent hairline, accent text — the three
   together are what make it read as latched rather than merely hovered. */
QToolButton:checked {
    background: var(--accent-soft);
    border-color: var(--accent-line);
    color: var(--accent-on-chrome);
}
/* Keyboard focus is not the same state as armed, and until this rule existed
   the two were drawn identically — the first button in the toolbar came up
   looking like the active tool on every launch. Focus gets the hairline
   only; the fill stays the exclusive mark of a latched tool. */
QToolButton:focus:!checked {
    background: transparent;
    border-color: var(--accent-line);
}
QToolButton:focus:!checked:hover { background: var(--bg-3); }
QToolButton:checked:hover { background: var(--accent-wash); }
QToolButton::menu-indicator { image: none; width: 0px; }
/* Split buttons (Open ▾). The rule above hides the arrow, which is right
   for a plain tool but would leave a split button with no sign that half
   of it opens a menu; the hairline divider is what makes the two halves
   legible, arrow or no arrow. */
/* The arrow half is drawn inside the button's own rect, so without
   this the label loses its last few characters. */
QToolButton[popupMode="1"] { padding-right: 26px; }
QToolButton::menu-button {
    width: 18px;
    border-left: 1px solid var(--line);
    border-top-right-radius: var(--radius-sm);
    border-bottom-right-radius: var(--radius-sm);
}
QToolButton::menu-button:hover { background: var(--bg-4); }

/* --- menus ------------------------------------------------------------- */
QMenuBar { background: var(--bg-1); color: var(--txt-1); border-bottom: 1px solid var(--line); }
QMenuBar::item { background: transparent; padding: 5px 10px; border-radius: var(--radius-sm); }
QMenuBar::item:selected { background: var(--bg-3); color: var(--txt-0); }
QMenu {
    background: var(--bg-2);
    color: var(--txt-1);
    border: 1px solid var(--bg-4);
    border-radius: var(--radius);
    padding: 5px;
}
QMenu::item { padding: 6px 26px 6px 22px; border-radius: var(--radius-sm); }
QMenu::item:selected { background: var(--bg-3); color: var(--txt-0); }
QMenu::item:disabled { color: var(--txt-2); }
QMenu::separator { height: 1px; background: var(--line-soft); margin: 5px 8px; }

/* --- text and fields --------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {
    background: var(--bg-2);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    color: var(--txt-0);
    padding: 0px 8px;
    min-height: var(--field-h);
    selection-background-color: var(--sel);
    selection-color: var(--bg-0);
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus { border-color: var(--accent); }
QLineEdit:read-only { color: var(--txt-1); }
QLineEdit:disabled, QSpinBox:disabled { color: var(--txt-2); background: var(--bg-1); }

QComboBox {
    background: var(--bg-2);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    color: var(--txt-0);
    padding: 0px 6px;
    min-height: var(--field-h);
}
QComboBox:hover { border-color: var(--bg-4); }
QComboBox:focus, QComboBox:on { border-color: var(--accent); }
QComboBox QAbstractItemView {
    background: var(--bg-2);
    color: var(--txt-0);
    border: 1px solid var(--bg-4);
    border-radius: var(--radius-sm);
    padding: 3px;
    outline: none;
    selection-background-color: var(--accent-soft);
    selection-color: var(--accent-on-chrome);
}

/* --- buttons ----------------------------------------------------------- */
/* The default is the PDF app's ghost button: quiet, and it stays quiet when
   there are six of them in a panel. */
QPushButton {
    background: var(--bg-2);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    color: var(--txt-1);
    padding: 0px 12px;
    min-height: var(--field-h);
}
QPushButton:hover { background: var(--bg-3); color: var(--txt-0); }
QPushButton:pressed { background: var(--bg-4); }
QPushButton:disabled { color: var(--txt-2); background: var(--bg-1); }
QPushButton:default, QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 var(--accent-hi), stop:1 var(--accent));
    border: 0px;
    color: #ffffff;
    font-weight: 600;
}
QPushButton:default:hover, QPushButton#primary:hover { background: var(--accent-lift); }
QPushButton:default:disabled, QPushButton#primary:disabled {
    background: var(--bg-3); color: var(--txt-2);
}
/* A destructive action earns the accent as an outline, never as a fill it
   would share with the primary button. */
QPushButton#danger { color: var(--accent); border-color: var(--accent-line); }
QPushButton#danger:hover { background: var(--accent); border-color: var(--accent); color: #ffffff; }

QCheckBox, QRadioButton { background: transparent; color: var(--txt-1); spacing: 7px; }
QCheckBox:hover, QRadioButton:hover { color: var(--txt-0); }
QCheckBox:disabled, QRadioButton:disabled { color: var(--txt-2); }

/* --- containers -------------------------------------------------------- */
QSplitter::handle { background: var(--line); }
QSplitter::handle:hover { background: var(--accent-line); }
QSplitter::handle:pressed { background: var(--accent); }

QScrollArea { border: 0px; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }

QGroupBox {
    background: transparent;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    margin-top: 12px;
    padding: 10px 10px 8px;
}
/* The section heading style from the PDF app's dialogs: small, spaced,
   uppercase, muted — a label that organises without competing. */
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0px 5px;
    color: var(--txt-2);
    font-size: var(--small-font);
    font-weight: 650;
}

QListWidget, QTreeWidget, QTableWidget, QListView, QTreeView {
    background: var(--bg-2);
    border: 0px;
    color: var(--txt-1);
    outline: none;
}
QListWidget::item, QTreeWidget::item {
    color: var(--txt-1);
    border-radius: var(--radius-sm);
    padding: 2px;
}
QListWidget::item:hover, QTreeWidget::item:hover { background: var(--bg-3); color: var(--txt-0); }
QListWidget::item:selected, QTreeWidget::item:selected {
    background: var(--accent-soft);
    color: var(--accent-on-chrome);
}

QStatusBar {
    background: var(--bg-1);
    border-top: 1px solid var(--line);
    color: var(--txt-2);
    font-size: var(--small-font);
    min-height: var(--status-h);
}
QStatusBar::item { border: 0px; }
QStatusBar QLabel { color: var(--txt-2); font-size: var(--small-font); background: transparent; }

QProgressBar {
    background: var(--bg-2);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    max-height: 14px;
    text-align: center;
    color: var(--txt-2);
}
QProgressBar::chunk { background: var(--accent); border-radius: 4px; }

/* --- named helpers ----------------------------------------------------- */
/* Widgets opt into these with setObjectName; they are the only styling any
   view file should need, which is what keeps the greys in this one file. */
#hint { color: var(--txt-2); font-size: var(--small-font); background: transparent; }
QLabel#panelHead {
    color: var(--txt-2);
    font-size: var(--small-font);
    font-weight: 650;
    background: transparent;
    padding-bottom: 2px;
}
QLabel#panelTitle { color: var(--txt-0); font-weight: 600; background: transparent; }
#divider { background: var(--line-soft); border: 0px; max-height: 1px; }
#vsep { background: var(--line); border: 0px; max-width: 1px; }
#sidePanel { background: var(--bg-1); }
/* A stretch widget pushing toolbar actions apart. It inherits the base
   QWidget background otherwise, and paints a flat rectangle straight over
   the tool row's gradient — a dark patch in the middle of the toolbar. */
#tspacer { background: transparent; }
#panelList { background: var(--bg-1); }

/* --- scrollbars -------------------------------------------------------- */
/* 12px with a 3px transparent gutter, matching the PDF app's
   background-clip: content-box trick. */
QScrollBar:vertical { background: transparent; width: 12px; margin: 0px; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0px; }
QScrollBar::handle:vertical {
    background: var(--bg-4); border-radius: 3px; margin: 3px; min-height: 28px;
}
QScrollBar::handle:horizontal {
    background: var(--bg-4); border-radius: 3px; margin: 3px; min-width: 28px;
}
QScrollBar::handle:hover { background: var(--txt-2); }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; width: 0px; border: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


# --------------------------------------------------------------------------
#  Applying
# --------------------------------------------------------------------------

def stylesheet(theme: str = "dark", accent: str = "redline",
               density: str = "normal") -> str:
    """The resolved Qt stylesheet for one combination of the three axes."""
    return _resolve(_QSS, tokens(theme, accent, density))


def palette(values: dict[str, str]) -> QPalette:
    """A QPalette matching the token set.

    The stylesheet cannot reach everything: the tick inside a check box, the
    spin box arrows, the frame Fusion draws around a modal. Those come from
    the palette, so it has to agree with the sheet or the light themes end up
    with black ticks on white boxes and dark ones with the reverse.
    """
    pal = QPalette()
    C = QColor
    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    pal.setColor(role.Window, C(values["bg-1"]))
    pal.setColor(role.WindowText, C(values["txt-0"]))
    pal.setColor(role.Base, C(values["bg-2"]))
    pal.setColor(role.AlternateBase, C(values["bg-1"]))
    pal.setColor(role.ToolTipBase, C(values["bg-2"]))
    pal.setColor(role.ToolTipText, C(values["txt-0"]))
    pal.setColor(role.Text, C(values["txt-0"]))
    pal.setColor(role.Button, C(values["bg-2"]))
    pal.setColor(role.ButtonText, C(values["txt-0"]))
    pal.setColor(role.BrightText, C(values["warn"]))
    pal.setColor(role.Link, C(values["info"]))
    pal.setColor(role.LinkVisited, C(values["info"]))
    pal.setColor(role.Highlight, C(values["sel"]))
    pal.setColor(role.HighlightedText,
                 C(values["bg-0"] if values["is-light"] == "0" else "#ffffff"))
    pal.setColor(role.PlaceholderText, C(values["txt-2"]))
    pal.setColor(role.Light, C(values["bg-3"]))
    pal.setColor(role.Midlight, C(values["bg-3"]))
    pal.setColor(role.Mid, C(values["bg-4"]))
    pal.setColor(role.Dark, C(values["line"]))
    pal.setColor(role.Shadow, C(values["bg-0"]))

    for r in (role.Text, role.ButtonText, role.WindowText):
        pal.setColor(group.Disabled, r, C(values["txt-2"]))
    pal.setColor(group.Disabled, role.Base, C(values["bg-1"]))
    pal.setColor(group.Disabled, role.Button, C(values["bg-1"]))
    return pal


_current: dict[str, str] | None = None


def current() -> dict[str, str]:
    """The token set live on screen right now.

    Widgets that paint their own chrome — a QGraphicsView backdrop, a custom
    overlay — cannot be reached by a stylesheet and have to look their colours
    up. Asking here rather than calling `tokens()` with guessed arguments is
    what keeps them on the same theme as everything else before the first
    `apply` has run and after every one since.
    """
    return dict(_current) if _current else tokens(**load())


def apply(app, theme: str = "dark", accent: str = "redline",
          density: str = "normal") -> dict[str, str]:
    """Put one combination on the running application. Returns its tokens.

    Safe to call again at any time — this is how the settings dialog gives a
    live preview, and it is why nothing here caches: a second call has to be
    able to fully replace the first.
    """
    theme, accent, density = theme_of(theme), accent_of(accent), density_of(density)
    values = tokens(theme, accent, density)
    # Fusion, because the native Windows style ignores most of the sheet and
    # draws its own chrome — which is the whole thing this file exists to stop.
    app.setStyle("Fusion")
    app.setPalette(palette(values))
    app.setStyleSheet(_resolve(_QSS, values))
    global _current
    _current = values
    return values


# --------------------------------------------------------------------------
#  Persistence
# --------------------------------------------------------------------------

_ORG = "DWGViewer"
_APP = "DWGViewer"
_KEY = "appearance"


def load(defaults: dict | None = None) -> dict[str, str]:
    """The saved choice, normalised. Never raises and never returns junk."""
    d = {**DEFAULTS, **(defaults or {})}
    s = QSettings(_ORG, _APP)
    return {
        "theme": theme_of(s.value(f"{_KEY}/theme"), d["theme"]),
        "accent": accent_of(s.value(f"{_KEY}/accent"), d["accent"]),
        "density": density_of(s.value(f"{_KEY}/density"), d["density"]),
    }


def save(theme: str, accent: str, density: str) -> None:
    s = QSettings(_ORG, _APP)
    s.setValue(f"{_KEY}/theme", theme_of(theme))
    s.setValue(f"{_KEY}/accent", accent_of(accent))
    s.setValue(f"{_KEY}/density", density_of(density))


def apply_saved(app, defaults: dict | None = None) -> dict[str, str]:
    """Startup entry point. `defaults` is how each app picks its own accent.

    The viewer defaults to Drafting blue and the PDF app to Redline red: same
    system, one colour apart, so they are recognisably a pair without the
    viewer pretending to be a markup tool.
    """
    choice = load(defaults)
    values = apply(app, **choice)
    values["_choice"] = choice  # type: ignore[assignment]
    return values


# --------------------------------------------------------------------------
#  Object names the stylesheet knows about
#
#  Set these with setObjectName instead of writing a setStyleSheet call. If a
#  widget needs something that is not here, add it here — a local sheet is a
#  grey that will be wrong in four of the five themes.
#
#      toolsBar     QToolBar   the lifted second row (markup tools)
#      findBar      QToolBar   a strip docked at the bottom
#      sidePanel    QWidget    a docked panel body
#      panelList    QListWidget  a list that sits on panel chrome, not on Base
#      tspacer      QWidget    a stretch that pushes toolbar actions apart
#      panelHead    QLabel     small uppercase section heading
#      panelTitle   QLabel     a panel's own name
#      hint         QLabel     muted 11px explanatory text
#      divider      QFrame     1px horizontal rule
#      vsep         QFrame     1px vertical rule (status bar, toolbars)
#      primary      QPushButton   the accent-filled confirming action
#      danger       QPushButton   destructive; accent outline, not fill
# --------------------------------------------------------------------------
