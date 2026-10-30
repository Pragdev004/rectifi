"""Deterministic checks on a finished chart (AI_PLAN B2.6). Pure: no model, no database, no langchain.

Each failed check becomes plain-words feedback for the repair step. The most important one is provenance (CLAUDE.md rule 1):
every rupee figure the chart's own text states must equal something that was plotted, the sum of something plotted, or a
total Rectifi worked out in SQL (`tools.chart_reference_totals`). Tick labels are scale marks, not claims, so they are skipped.
"""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

MIN_PNG = 5 * 1024
MAX_PNG = 2 * 1024 * 1024
PAISE = Decimal("0.01")

_RUPEE = re.compile(r"(?i)(?:₹|\brs\.?|\binr)\s*([\d,]+(?:\.\d+)?)\s*(k\b|l\b|lakhs?\b|cr\b|crores?\b)?")
_GROUPED = re.compile(r"(?<![\d,.])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?(?![\d,])")
_PIE_ASKED = re.compile(r"(?i)\b(pie|donut|doughnut)\b")


def figures_in(text: str) -> list[tuple[str, Decimal | None]]:
    """Every rupee figure in a text, as (text as written, value). The value is None for an abbreviation ('₹5k', '₹2L'), which can't
    be checked: the chart must write the full figure with inr()."""
    found: list[tuple[str, Decimal | None]] = []
    rest = text or ""
    for match in _RUPEE.finditer(text or ""):
        written = match.group(0).strip()
        if match.group(2):
            found.append((written, None))
            continue
        try:
            found.append((written, Decimal(match.group(1).replace(",", "")).quantize(PAISE, rounding=ROUND_HALF_UP)))
        except InvalidOperation:
            found.append((written, None))
    rest = _RUPEE.sub(" ", rest)
    for match in _GROUPED.finditer(rest):
        try:
            value = Decimal(match.group(0).replace(",", ""))
        except InvalidOperation:
            continue
        if value >= 100:
            found.append((match.group(0), value.quantize(PAISE, rounding=ROUND_HALF_UP)))
    return found


def _dec(x) -> Decimal | None:
    try:
        d = Decimal(repr(float(x)))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None
    return d if d.is_finite() else None


def reference_values(reference: dict | None) -> list[Decimal]:
    """Flatten tools.chart_reference_totals into one list of allowed figures."""
    out: list[Decimal] = []

    def walk(node):
        if isinstance(node, Decimal):
            out.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, int) and not isinstance(node, bool):
            out.append(Decimal(node))

    walk(reference or {})
    return out


def plotted_values(info: dict) -> list[Decimal]:
    """Everything drawn, plus the sum of each series and of each axis's bars. Both the rupee reading and the paise reading
    (value / 100) are kept, because a chart may plot amount_paise."""
    values: list[Decimal] = []
    for ax in info.get("axes", []):
        axis_sum = Decimal(0)
        for artist in ax.get("artists", []):
            for key in ("values", "values_x", "tops", "rights"):
                series = [d for d in (_dec(v) for v in artist.get(key, [])) if d is not None]
                values.extend(series)
                if series and key in ("values", "values_x", "tops"):
                    total = sum(series, Decimal(0))
                    values.append(total)
                    if artist.get("type") == "bar" and key in ("values", "values_x"):
                        axis_sum += total
            values.append(Decimal(artist.get("n", 0)))
        values.append(axis_sum)
    values.extend(v / 100 for v in list(values))
    return values


def _matches(figure: Decimal, candidate: Decimal) -> bool:
    if figure == candidate.quantize(PAISE, rounding=ROUND_HALF_UP):
        return True
    if figure == figure.to_integral_value():            # written without paise: truncated or rounded to whole rupees
        return figure in (candidate.to_integral_value(ROUND_DOWN), candidate.to_integral_value(ROUND_HALF_UP))
    return False


def claimed_texts(info: dict) -> list[tuple[str, str]]:
    """(where, text) for every piece of the chart's own wording that may state a figure. Tick labels are left out."""
    out: list[tuple[str, str]] = []
    if info.get("suptitle"):
        out.append(("the overall title", info["suptitle"]))
    for ax in info.get("axes", []):
        for key, where in (("title", "the title"), ("title_left", "the title"), ("title_right", "the title"), ("xlabel", "the x label"), ("ylabel", "the y label")):
            if ax.get(key):
                out.append((where, ax[key]))
        out.extend(("a label on the chart", t) for t in ax.get("texts", []))
        out.extend(("the legend", t) for t in ax.get("legend", []))
    return out


def check_provenance(info: dict, reference: dict | None, *, entries: int | None = None) -> dict:
    allowed = reference_values(reference) + plotted_values(info)
    if entries is not None:
        allowed.append(Decimal(entries))
    bad: list[str] = []
    seen: set[str] = set()
    for where, text in claimed_texts(info):
        for written, value in figures_in(text):
            if written in seen:
                continue
            seen.add(written)
            if value is None:
                bad.append(f"{where} says {written}: write the full figure with inr(), never an abbreviation")
            elif not any(_matches(value, c) for c in allowed):
                bad.append(f"{where} says {written}, which isn't in your data: compute it from df and build the text with inr()")
    return {"name": "figures_match", "ok": not bad, "detail": "; ".join(bad[:4]) if bad else "every rupee figure traces to your records"}


def run_checks(info: dict | None, *, png: dict | None = None, reference: dict | None = None, request: str = "",
               warnings: list | None = None, entries: int | None = None) -> dict:
    """Run all checks. Returns {"ok": bool, "checks": [{"name", "ok", "detail"}], "failures": [str]}."""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    info = info or {}
    axes = [a for a in info.get("axes", []) if a.get("visible", True)]
    with_data = [a for a in axes if any(art["type"] in ("image", "wedge", "collection") and art["n"] > 0
                                        or art["type"] in ("bar", "line", "scatter") and art["n"] > 0 and (art["values"] or art["tops"] or art["values_x"])
                                        for art in a.get("artists", []))]
    add("has_data", bool(with_data), "the chart holds data" if with_data else "no axis holds any data points (or they are all NaN)")

    title_ok = bool(info.get("suptitle") or any(a.get("title") or a.get("title_left") or a.get("title_right") for a in axes))
    missing = [("x" if not a.get("xlabel") else "y") for a in with_data if not a.get("is_pie") and not (a.get("xlabel") and a.get("ylabel"))]
    add("labelled", title_ok and not missing,
        "title and axis labels are present" if title_ok and not missing else
        ("the chart has no title" if not title_ok else f"an axis has no {missing[0]} label (set_xlabel / set_ylabel)"))

    one_point = entries is not None and entries <= 1
    lines_and_bars = [art for a in with_data for art in a["artists"] if art["type"] in ("bar", "line")]
    thin = bool(lines_and_bars) and all(art["n"] < 2 for art in lines_and_bars)
    add("enough_points", one_point or not thin, "more than one position is drawn" if one_point or not thin else "a line or bar chart needs at least 2 x positions")

    pie = any(a.get("is_pie") for a in axes)
    add("no_unasked_pie", not pie or bool(_PIE_ASKED.search(request or "")),
        "no pie chart" if not pie else ("a pie was asked for" if _PIE_ASKED.search(request or "") else "a pie or donut chart wasn't asked for: use bars"))

    if info:
        checks.append(check_provenance(info, reference, entries=entries))

    glyph = [w for w in (warnings or []) if "missing from font" in w.lower() or "glyph" in w.lower()]
    add("glyphs", not glyph, "every character was drawn" if not glyph else "some characters (such as ₹) were missing from the font")

    if png is not None:
        sizes = {k: len(v) for k, v in png.items()}
        bad = [k for k, n in sizes.items() if not MIN_PNG <= n <= MAX_PNG]
        add("image_size", not bad, "the image size is normal" if not bad else "the image is nearly empty or far too large")

    failures = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
    return {"ok": not failures, "checks": checks, "failures": failures}


def alt_text(info: dict | None, checks: dict, title: str | None = None) -> str:
    """A plain-words description for the image's alt text, built only from what the harness saw."""
    ax = (info or {}).get("axes", [{}])[0] if (info or {}).get("axes") else {}
    title = title or (info or {}).get("suptitle") or ax.get("title") or "Chart"
    kinds = sorted({a["type"] for a in ax.get("artists", [])})
    names = {"bar": "bar chart", "line": "line chart", "scatter": "scatter plot", "wedge": "pie chart", "image": "heat map", "collection": "chart"}
    kind = ", ".join(names.get(k, k) for k in kinds) or "chart"
    if not kinds:
        return f"{title}. Checks passed: {', '.join(c['name'].replace('_', ' ') for c in checks.get('checks', []) if c['ok']) or 'none'}."
    passed = [c["name"].replace("_", " ") for c in checks.get("checks", []) if c["ok"]]
    return f"{title}. A {kind} of your spending. Checks passed: {', '.join(passed) or 'none'}."
