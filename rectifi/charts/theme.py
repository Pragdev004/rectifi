"""Passbook colours and matplotlib settings for chart PNGs (one light, one dark). Pure data, stdlib only.

The same tokens as the web app: paper background, one accent, rules for gridlines, IBM Plex type. B9 in WEB_PLAN: at most
two series, data in the accent, reference marks muted, no pie unless asked.
"""

from __future__ import annotations

from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_FILES = ("IBMPlexSans-Regular.ttf", "IBMPlexSans-SemiBold.ttf", "IBMPlexMono-Regular.ttf")
SANS = "IBM Plex Sans"
MONO = "IBM Plex Mono"

PALETTES = {
    "light": {"paper": "#F6F2E9", "ink": "#1B1A17", "muted": "#6B6557", "rule": "#D9D0BE", "accent": "#1F4E5F", "sunk": "#EAE3D2", "alert": "#9A3B2A"},
    "dark": {"paper": "#17150F", "ink": "#ECE7DA", "muted": "#A39C8A", "rule": "#3A352C", "accent": "#7FBACB", "sunk": "#24211A", "alert": "#E08A76"},
}
THEMES = ("light", "dark")


def palette(theme: str) -> dict:
    return dict(PALETTES[theme])


def rc_params(theme: str) -> dict:
    """matplotlib rcParams for one theme."""
    p = PALETTES[theme]
    return {
        "figure.facecolor": p["paper"], "axes.facecolor": p["paper"], "savefig.facecolor": p["paper"],
        "axes.edgecolor": p["rule"], "axes.labelcolor": p["muted"], "axes.titlecolor": p["ink"],
        "text.color": p["ink"], "xtick.color": p["muted"], "ytick.color": p["muted"],
        "grid.color": p["rule"], "grid.linewidth": 0.8, "axes.grid": True, "axes.grid.axis": "y", "axes.axisbelow": True,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.prop_cycle": f"cycler('color', ['{p['accent']}', '{p['muted']}'])",
        "legend.frameon": False, "legend.labelcolor": p["ink"],
        "font.family": [SANS, "DejaVu Sans"], "font.size": 11, "axes.titlesize": 14, "axes.titleweight": "semibold",
        "axes.titlelocation": "left", "axes.labelsize": 11, "figure.dpi": 100, "savefig.dpi": 144,
        "axes.unicode_minus": True, "figure.autolayout": False, "figure.constrained_layout.use": True,
    }
