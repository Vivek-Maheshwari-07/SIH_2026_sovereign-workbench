"""
Theme checks: every text/background pair the UI uses meets WCAG AA, the Streamlit config repeats
the tokens from ui/components/theme.py (the single source), and the CSS loads nothing remote.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from ui.components import theme as t

CONFIG = Path(__file__).resolve().parents[2] / ".streamlit" / "config.toml"
WHITE = "#FFFFFF"


def luminance(hex_colour: str) -> float:
    rgb = [int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# (text, background, where); all normal-size text, so 4.5:1
TEXT_PAIRS = [
    (WHITE, t.HEADER, "title on the header band"),
    (t.HEADER_SUB, t.HEADER, "tagline on the header band"),
    (WHITE, t.HEADER_CHIP, "NET-001 / firewall chips"),
    (t.DONE_TEXT, WHITE, "Air-gap sealed chip"),
    (t.WARN_TEXT, WHITE, "Not sealed chip"),
    (WHITE, t.ALARM, "red NET-001 chip"),
    (t.INK, t.CANVAS, "body text on the canvas"),
    (t.INK_2, t.CANVAS, "secondary text on the canvas"),
    (t.MUTED, t.CANVAS, "muted text on the canvas"),
    (t.MUTED, t.PANEL, "muted text on panels"),
    (t.ACTIVE_TEXT, t.CANVAS, "active step label"),
    (WHITE, t.ACTIVE, "Running tag"),
    (t.DONE_TEXT, t.CANVAS, "green text"),
    (t.ALARM_TEXT, t.PANEL, "red text"),
    (t.ALARM_TEXT, t.ALARM_BG, "core leak rows"),
    (t.WARN_TEXT, t.WARN_BG, "warnings, platform rows"),
    (t.DELIVER_INK, t.DELIVER, "Download button"),
    (t.DELIVER_TAG, t.DELIVER_TAG_BG, "file-type tag"),
    (t.INK_2, t.PANEL, "waiting bubble code"),
    (t.ACTIVE_TEXT, t.PANEL, "open bubble code"),
]
# bubble codes are 18 px bold = large text, so 3:1
LARGE_PAIRS = [(WHITE, t.DONE, "finished bubble"), (WHITE, t.ACTIVE, "active bubble"), (WHITE, t.ALARM, "failed bubble")]


@pytest.mark.parametrize("fg, bg, where", TEXT_PAIRS, ids=[p[2] for p in TEXT_PAIRS])
def test_text_contrast_aa(fg, bg, where):
    assert contrast(fg, bg) >= 4.5, f"{where}: {contrast(fg, bg):.2f}"


@pytest.mark.parametrize("fg, bg, where", LARGE_PAIRS, ids=[p[2] for p in LARGE_PAIRS])
def test_large_text_contrast_aa(fg, bg, where):
    assert contrast(fg, bg) >= 3.0, f"{where}: {contrast(fg, bg):.2f}"


def test_config_repeats_theme_tokens():
    cfg = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["theme"]
    assert cfg["primaryColor"] == t.ACTIVE and cfg["backgroundColor"] == t.CANVAS
    assert cfg["textColor"] == t.INK and cfg["borderColor"] == t.BORDER and cfg["linkColor"] == t.ACTIVE_TEXT
    assert cfg["redTextColor"] == t.ALARM_TEXT and cfg["redBackgroundColor"] == t.ALARM_BG
    assert cfg["yellowTextColor"] == t.WARN_TEXT and cfg["yellowBackgroundColor"] == t.WARN_BG
    assert cfg["orangeTextColor"] == t.WARN_TEXT      # Streamlit's "orange" alerts are warnings: amber, never orange


def test_orange_only_for_deliverables():
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", t.CSS.split(":root", 1)[1].split("}", 1)[1])
    orange = [sel.strip() for sel, body in rules if re.search(r"var\(--(deliver|tag)", body)]
    assert orange and all(".wb-file" in sel or "st-key-dl_" in sel for sel in orange), orange
    assert t.DELIVER not in CONFIG.read_text(encoding="utf-8")


def test_css_loads_nothing_remote():
    assert not re.search(r"https?://|@import|url\(", t.CSS)
    assert ":material/" not in t.CSS


def test_selected_css_uses_the_accent():
    assert t.selected_css("code_calc") == ('<style>.st-key-wocard_code_calc { border-left: 3px solid #1673E6 '
                                           '!important; border-radius: 0 8px 8px 0 !important; }</style>')
