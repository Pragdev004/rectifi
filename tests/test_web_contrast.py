"""Colour contrast of the web app's design tokens (web/src/styles/tokens.css), computed with the WCAG formula.

Text needs 4.5:1, borders and graphics that carry meaning need 3:1. Both themes are checked, and the two ways of getting dark
(the OS setting and the manual switch) must define the same colours."""

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent / "web" / "src" / "styles" / "tokens.css"


def blocks() -> dict[str, dict[str, str]]:
    text = CSS.read_text()
    found = {}
    for name, start in (("light", ":root {"), ("dark_media", ":root:not([data-theme=\"light\"]) {"), ("dark_switch", ":root[data-theme=\"dark\"] {")):
        body = text[text.index(start) + len(start):]
        body = body[:body.index("\n  }" if name == "dark_media" else "\n}")]
        found[name] = dict(re.findall(r"--(c-[a-z-]+):\s*(#[0-9a-fA-F]{6})", body))
    return found


def luminance(hex_colour: str) -> float:
    rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_PAIRS = [
    ("c-text", "c-bg"), ("c-text", "c-raised"), ("c-text", "c-sunk"),
    ("c-muted", "c-bg"), ("c-muted", "c-raised"), ("c-muted", "c-sunk"),
    ("c-subtle", "c-bg"), ("c-subtle", "c-sunk"),          # placeholders and disabled text
    ("c-accent", "c-bg"), ("c-accent", "c-raised"), ("c-accent", "c-accent-tint"),
    ("c-on-accent", "c-accent"),
    ("c-ok", "c-ok-bg"), ("c-ok", "c-bg"), ("c-near", "c-near-bg"), ("c-near", "c-bg"), ("c-over", "c-over-bg"), ("c-over", "c-bg"),
    ("c-text", "c-near-bg"), ("c-text", "c-over-bg"), ("c-text", "c-accent-tint"),
]
GRAPHIC_PAIRS = [("c-rule-strong", "c-bg"), ("c-rule-strong", "c-raised"), ("c-accent", "c-bg"), ("c-over", "c-bg"), ("c-near", "c-bg")]


@pytest.mark.parametrize("theme", ["light", "dark_media"])
def test_text_pairs_reach_4_5(theme):
    colours = blocks()[theme]
    low = {f"{fg} on {bg}": round(ratio(colours[fg], colours[bg]), 2) for fg, bg in TEXT_PAIRS if ratio(colours[fg], colours[bg]) < 4.5}
    assert not low, low


@pytest.mark.parametrize("theme", ["light", "dark_media"])
def test_borders_and_graphics_reach_3(theme):
    colours = blocks()[theme]
    low = {f"{fg} on {bg}": round(ratio(colours[fg], colours[bg]), 2) for fg, bg in GRAPHIC_PAIRS if ratio(colours[fg], colours[bg]) < 3}
    assert not low, low


def test_the_two_dark_definitions_are_identical():
    b = blocks()
    assert b["dark_media"] == b["dark_switch"] and len(b["dark_media"]) >= 17
