"""Design tokens + the few custom HTML pieces Streamlit can't draw (hero number, ledger rows, state chips).

Every colour has a light and a dark value. Colour never carries meaning alone: states always have a word too.
"""

from __future__ import annotations

import html

import streamlit as st

TOKENS = {
    "light": {
        "paper": "#F6F2E9", "paper-2": "#ECE5D6", "ink": "#1D1B16", "ink-2": "#5C5547", "rule": "#D9D0BE",
        "accent": "#1F4E5F", "ok": "#3F6B4F", "near": "#8A5A12", "over": "#9B2C2C",
        "ok-bg": "#E3EADF", "near-bg": "#F3E6CC", "over-bg": "#F2DCD7", "credit": "#3F6B4F",
    },
    "dark": {
        "paper": "#17150F", "paper-2": "#221F18", "ink": "#ECE6D8", "ink-2": "#A79F8E", "rule": "#3A352C",
        "accent": "#7FBACB", "ok": "#8DBA98", "near": "#E0A64A", "over": "#E07A6B",
        "ok-bg": "#22301F", "near-bg": "#3A2C14", "over-bg": "#3B1F1B", "credit": "#8DBA98",
    },
}

STATE_LABEL = {"ok": "On track", "near": "Near limit", "over": "Over budget"}


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def tok(name: str) -> str:
    return TOKENS[mode()][name]


def inject_css() -> None:
    t = TOKENS[mode()]
    variables = ";".join(f"--{k}:{v}" for k, v in t.items())
    st.html(f"""
<style>
:root {{ {variables}; }}
.block-container {{ padding-top: 2.2rem; max-width: 1180px; }}
h1, h2, h3 {{ letter-spacing: -0.01em; }}
.mono, [data-testid="stMetricValue"] {{ font-family: "IBM Plex Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; }}

/* eyebrow labels above sections, like a passbook column heading */
.eyebrow {{ font-size: .72rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--ink-2); margin: 0 0 .35rem; }}

/* hero: savings rate is the biggest thing on the page */
.hero {{ display: grid; grid-template-columns: minmax(220px, 1.1fr) 2fr; gap: 2rem; align-items: end;
         border-bottom: 1px solid var(--rule); padding-bottom: 1.4rem; margin-bottom: 1.6rem; }}
.hero-rate {{ font-family: Fraunces, Georgia, serif; font-size: clamp(3.4rem, 8vw, 5.6rem); line-height: .95; font-weight: 600; color: var(--ink); }}
.hero-rate small {{ font-size: .38em; font-weight: 500; color: var(--ink-2); margin-left: .15em; }}
.hero-caption {{ color: var(--ink-2); font-size: .95rem; margin-top: .5rem; }}
.hero-ledger {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 0; border-left: 1px solid var(--rule); }}
.hero-ledger div {{ padding: .2rem 1.1rem; border-right: 1px solid var(--rule); }}
.hero-ledger .label {{ font-size: .78rem; color: var(--ink-2); }}
.hero-ledger .value {{ font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; font-size: 1.35rem; font-weight: 500; }}
.hero-ledger .value.neg {{ color: var(--over); }}
@media (max-width: 720px) {{
  .hero {{ grid-template-columns: 1fr; gap: 1rem; }}
  .hero-ledger {{ grid-template-columns: 1fr; border-left: 0; }}
  .hero-ledger div {{ border-right: 0; border-top: 1px solid var(--rule); padding: .6rem 0; }}
}}

/* ledger rows */
.ledger {{ width: 100%; border-collapse: collapse; font-size: .92rem; }}
.ledger td {{ padding: .55rem .4rem; border-bottom: 1px dashed var(--rule); vertical-align: top; }}
.ledger td.date {{ color: var(--ink-2); white-space: nowrap; font-family: "IBM Plex Mono", monospace; font-size: .82rem; width: 5.5rem; }}
.ledger td.who {{ overflow-wrap: anywhere; }}
.ledger td.who .cat {{ display: block; color: var(--ink-2); font-size: .78rem; }}
.ledger td.amt {{ text-align: right; white-space: nowrap; font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; }}
.ledger td.amt.credit {{ color: var(--credit); }}

/* state chips: colour + word, never colour alone */
.chip {{ display: inline-block; font-size: .72rem; font-weight: 600; padding: .12rem .5rem; border-radius: 999px; letter-spacing: .02em; }}
.chip.ok {{ background: var(--ok-bg); color: var(--ok); }}
.chip.near {{ background: var(--near-bg); color: var(--near); }}
.chip.over {{ background: var(--over-bg); color: var(--over); }}

/* budget rows */
.brow {{ display: grid; grid-template-columns: minmax(120px, 1.2fr) 3fr auto; gap: .9rem; align-items: center; padding: .55rem 0; border-bottom: 1px solid var(--rule); }}
.brow .name {{ font-weight: 500; }}
.brow .figs {{ font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; font-size: .82rem; color: var(--ink-2); text-align: right; white-space: nowrap; }}
.bar {{ position: relative; height: 10px; background: var(--paper-2); border-radius: 999px; overflow: visible; }}
.bar .fill {{ position: absolute; inset: 0 auto 0 0; border-radius: 999px; transition: width .5s cubic-bezier(.2,.8,.2,1); }}
.bar .fill.ok {{ background: var(--ok); }} .bar .fill.near {{ background: var(--near); }} .bar .fill.over {{ background: var(--over); }}
.bar .pace {{ position: absolute; top: -4px; bottom: -4px; width: 2px; background: var(--ink); opacity: .55; }}
@media (max-width: 720px) {{ .brow {{ grid-template-columns: 1fr; gap: .35rem; }} .brow .figs {{ text-align: left; }} }}

/* reconciliation card: printed like a passbook sum */
.recon {{ border: 1px solid var(--rule); border-radius: 8px; padding: 1rem 1.1rem; background: color-mix(in srgb, var(--paper-2) 45%, transparent); }}
.recon table {{ width: 100%; font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; font-size: .86rem; border-collapse: collapse; }}
.recon td {{ padding: .18rem 0; }} .recon td:last-child {{ text-align: right; }}
.recon tr.total td {{ border-top: 1px solid var(--ink); font-weight: 600; padding-top: .35rem; }}
.recon .verdict {{ margin-top: .6rem; font-size: .9rem; }}

/* insight rows: a state chip on the left, the finding on the right (stacked on a phone) */
.ins {{ display: grid; grid-template-columns: 6.2rem minmax(0, 1fr); gap: .9rem; padding: .85rem 0; border-bottom: 1px solid var(--rule); }}
.ins .t {{ font-weight: 600; overflow-wrap: anywhere; }}
.ins .d {{ color: var(--ink-2); font-size: .92rem; margin-top: .2rem; overflow-wrap: anywhere; }}
.ins.compact {{ padding: .55rem 0; }}
@media (max-width: 720px) {{ .ins {{ grid-template-columns: minmax(0, 1fr); gap: .35rem; }} }}

/* phone: every thing you tap is at least 44px, and the page keeps a comfortable edge */
@media (max-width: 720px) {{
  .block-container {{ padding-left: 1rem; padding-right: 1rem; padding-bottom: 3rem; }}
  [data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapseButton"] button {{ min-width: 44px; min-height: 44px; }}
  [data-testid="stSidebarNav"] a, [data-testid="stSidebarNavLink"] {{ min-height: 44px; align-items: center; }}
  [data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-tertiary"],
  [data-testid="stBaseButton-secondaryFormSubmit"], [data-testid="stBaseButton-primaryFormSubmit"],
  [data-testid="stDownloadButton"] button {{ min-height: 44px; }}
  [data-testid="stPageLink"] a, [data-testid="stPageLink-NavLink"] {{ min-height: 44px; align-items: center; }}
  [role="tab"] {{ min-height: 44px; }}
  [data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"],
  [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input, [data-testid="stDateInput"] input,
  [data-testid="stTextArea"] textarea {{ min-height: 44px; }}
  [data-testid="stCheckbox"] label, [data-testid="stRadio"] label {{ min-height: 44px; align-items: center; }}
  [data-testid="stSelectbox"] input, [data-testid="stMultiSelect"] input {{ min-height: 44px; }}
  [data-testid="stFileChipDeleteBtn"], [data-testid="stBaseButton-borderlessIcon"], [data-testid="stBaseButton-minimal"] {{ min-width: 44px; min-height: 44px; }}
}}

.empty {{ border: 1px dashed var(--rule); border-radius: 8px; padding: 1.4rem; color: var(--ink-2); }}
.empty strong {{ color: var(--ink); }}

@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>""")


def esc(text) -> str:
    return html.escape("" if text is None else str(text))


def chip(state: str, label: str | None = None) -> str:
    """A coloured pill that always carries a word. Pass `label` to say something other than the default."""
    return f'<span class="chip {state}">{esc(label or STATE_LABEL.get(state, state))}</span>'
