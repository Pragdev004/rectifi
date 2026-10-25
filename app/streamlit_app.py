"""Rectifi — Streamlit front end.   Run:  streamlit run app/streamlit_app.py

Pages: Overview · Review · Insights · Ask · Add · Ledger · Budgets · Accounts · Phone
The UI only calls rectifi.tools / rectifi.importer / the agent. It never does money arithmetic itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import psycopg  # noqa: E402
import streamlit as st  # noqa: E402
from psycopg_pool import PoolTimeout  # noqa: E402
from theme import chip, esc, inject_css, tok  # noqa: E402

from rectifi.categorize import FALLBACK_EXPENSE  # noqa: E402
from rectifi import config, forwarder_config, insights, sms_gateway, tailscale, tools  # noqa: E402
from rectifi.db import DatabaseUnavailable, init_db  # noqa: E402
from rectifi.importer import assess_statement, import_rows, normalise, read_table  # noqa: E402
from rectifi.money import fmt_inr, parse_amount  # noqa: E402
from rectifi.parsing import parse_sms  # noqa: E402
from rectifi.tools import RectifiError  # noqa: E402

LEDGER_LIMIT = 1000

st.set_page_config(page_title="Rectifi", page_icon=":material/receipt_long:", layout="wide")
inject_css()


# ───────────────────────── setup & shared helpers ─────────────────────────

@st.cache_resource(show_spinner=False)
def _boot(url: str) -> bool:
    """Create the tables once per database URL. If Postgres is down this raises, and Streamlit does not
    cache a raised error, so the next page load simply tries again (no restart needed)."""
    init_db()
    return True


def db_down(exc: Exception) -> None:
    """A friendly page instead of a traceback when the database can't be reached."""
    st.title("Rectifi can't reach its database")
    st.error(str(exc), icon=":material/database:")
    empty(
        "Your data is safe. Postgres just isn't running.",
        "Start it with <code>python scripts/dev.py db</code> (Docker), or check <code>DATABASE_URL</code> in "
        "<code>.env</code>. Then reload this page. <code>python scripts/dev.py doctor</code> shows what is wrong.",
    )


def months_back(n: int = 12) -> list[date]:
    first = date.today().replace(day=1)
    out = []
    for _ in range(n):
        out.append(first)
        first = (first - timedelta(days=1)).replace(day=1)
    return out


def month_picker(key: str, label: str = "Month") -> date:
    options = months_back()
    return st.selectbox(label, options, format_func=lambda d: d.strftime("%B %Y"), key=key)


def inr(x, paise: bool = True, sign: bool = False) -> str:
    return fmt_inr(x, paise=paise, sign=sign)


def ledger_html(rows: list[dict]) -> str:
    body = []
    for t in rows:
        credit = t["direction"] == "credit"
        who = t.get("payee") or t.get("description") or "—"
        sign = "+" if credit else "−"
        body.append(
            f'<tr><td class="date">{t["txn_date"]:%d %b}</td>'
            f'<td class="who">{esc(who)}<span class="cat">{esc(t["category"])} · {esc(t["source"])}</span></td>'
            f'<td class="amt {"credit" if credit else ""}">{sign}{inr(t["amount"])}</td></tr>'
        )
    return f'<table class="ledger">{"".join(body)}</table>'


def recon_html(r: dict) -> str:
    verdict = (
        f'{chip("ok").replace("On track", "Balanced")} &nbsp;{esc(r["explanation"])}'
        if r["balanced"]
        else f'{chip("over").replace("Over budget", "Gap " + inr(r["gap"]))} &nbsp;{esc(r["explanation"])}'
    )
    return f"""<div class="recon"><table>
<tr><td>Opening balance · {r['opening_date']:%d %b %Y}</td><td>{inr(r['opening'])}</td></tr>
<tr><td>+ credits Rectifi recorded</td><td>{inr(r['credits'])}</td></tr>
<tr><td>− debits Rectifi recorded</td><td>{inr(r['debits'])}</td></tr>
<tr class="total"><td>= expected closing</td><td>{inr(r['expected_closing'])}</td></tr>
<tr><td>Bank's closing · {r['closing_date']:%d %b %Y}</td><td>{inr(r['closing'])}</td></tr>
</table><div class="verdict">{verdict}</div></div>"""


def budget_rows_html(status: dict) -> str:
    rows = []
    for b in status["categories"]:
        width = min(float(b["percent"]), 100.0)
        pace = min(float(status["pace"]), 100.0)
        rows.append(
            f'<div class="brow"><div class="name">{esc(b["category"])}<br>{chip(b["state"])}</div>'
            f'<div class="bar" role="img" aria-label="{esc(b["category"])}: {b["percent"]}% of budget used, '
            f'{status["pace"]}% of month gone"><div class="fill {b["state"]}" style="width:{width}%"></div>'
            f'<div class="pace" style="left:{pace}%"></div></div>'
            f'<div class="figs">{inr(b["spent"], False)} / {inr(b["limit"], False)}<br>'
            f'{"over by " + inr(-b["remaining"], False) if b["remaining"] < 0 else inr(b["remaining"], False) + " left"}</div></div>'
        )
    return "".join(rows)


def hero_html(big: str, caption: str, cells: list[tuple[str, str, bool]], eyebrow: str, big_style: str = "") -> str:
    """The big-number block at the top of a page. `big` is already-safe HTML (the headline figure);
    `cells` are (label, value, is_negative) triples shown in the ledger on the right."""
    cell_html = "".join(
        f'<div><div class="label">{esc(label)}</div><div class="value {"neg" if negative else ""}">{esc(value)}</div></div>'
        for label, value, negative in cells
    )
    return (
        f"""<div class="hero"><div><p class="eyebrow">{esc(eyebrow)}</p><div class="hero-rate" style="{big_style}">{big}</div>
        <div class="hero-caption">{esc(caption)}</div></div>
        <div class="hero-ledger">{cell_html}</div></div>"""
    )


def empty(title: str, body: str) -> None:
    st.markdown(f'<div class="empty"><strong>{esc(title)}</strong><br>{body}</div>', unsafe_allow_html=True)


SEVERITY_CHIP = {"act": ("over", "Act"), "watch": ("near", "Watch"), "info": ("ok", "Good to know")}


def insight_html(item, compact: bool = False) -> str:
    """One insight row. The chip always carries a word, so colour is never the only signal."""
    state, word = SEVERITY_CHIP[item.severity]
    detail = "" if compact else f'<div class="d">{esc(item.detail)}</div>'
    return (f'<div class="ins{" compact" if compact else ""}"><div>{chip(state, word)}</div>'
            f'<div><div class="t">{esc(item.title)}</div>{detail}</div></div>')


def insight_actions(item, key_prefix: str, explain_ok: bool = False) -> None:
    """The buttons under one insight: go to the page that fixes it, apply a suggested budget, ask the model to explain."""
    cols = st.columns([1.4, 1.4, 1.2, 2], vertical_alignment="center")
    if item.action_page and item.action_label and item.apply is None and item.action_page in PAGES:
        with cols[0]:
            st.page_link(PAGES[item.action_page], label=item.action_label, icon=":material/arrow_forward:")
    if item.apply and item.apply.get("tool") == "set_budget":
        if cols[0].button(item.action_label or "Set this budget", key=f"{key_prefix}_apply_{item.key}", icon=":material/savings:",
                          help="Sets this month's budget for the category. You can change it on the Budgets page."):
            try:
                tools.set_budget(item.apply["category"], item.apply["limit"], item.apply["month"])
            except RectifiError as exc:
                st.error(str(exc), icon=":material/error:")
            else:
                st.toast(f"Budget set for {item.apply['category']}.", icon=":material/check_circle:")
                st.rerun()
    if explain_ok and cols[1].button("Explain this", key=f"{key_prefix}_explain_{item.key}", icon=":material/lightbulb:"):
        from rectifi.agent.explain import explain

        with st.spinner("Thinking…"):
            st.session_state.setdefault("explained", {})[item.key] = explain(item)
    result = st.session_state.get("explained", {}).get(item.key)
    if result:
        if result["text"]:
            st.caption(f":material/lightbulb: {result['text']}")
            st.caption("Advice written by the assistant. Every figure in it was checked against Rectifi's own numbers above.")
        elif result["dropped"]:
            shown = ", ".join(inr(v, False) for v in result["dropped"])
            st.caption(f":material/warning: Not shown: the assistant used {shown}, which Rectifi did not calculate. "
                       "Its advice was thrown away so no unchecked figure reaches you.")
        else:
            st.caption(f":material/cloud_off: {result['error']}")


def assistant_ready() -> bool:
    from rectifi.agent.llm import llm_status

    try:
        return bool(llm_status()[0])
    except Exception:                                  # a broken model setting must never break the Insights page
        return False


# ───────────────────────── pages ─────────────────────────

def suggestions_strip() -> None:
    """The three most useful insights, compact, with a link to the page that has them all. Hidden when there are none."""
    try:
        top = insights.top_insights(3)
    except (DatabaseUnavailable, psycopg.OperationalError, PoolTimeout):
        raise
    except Exception:                                  # a surprise in one rule must not take the Overview down
        logging.getLogger("rectifi.app").exception("insights failed")
        st.caption("Suggestions could not be worked out this time. The rest of the page is fine.")
        return
    if not top:
        return
    st.markdown('<p class="eyebrow">Top suggestions</p>' + "".join(insight_html(i, compact=True) for i in top), unsafe_allow_html=True)
    st.page_link(PAGES["insights"], label="See all insights", icon=":material/lightbulb:")
    st.write("")


def page_overview():
    head_l, head_r = st.columns([3, 1], vertical_alignment="bottom")
    with head_r:
        month = month_picker("ov_month")
    start, end = tools.month_bounds(month)
    with head_l:
        st.title(month.strftime("%B %Y"))

    summary = tools.spending_summary(start, end)
    if summary["count"] == 0:
        empty(
            "Nothing recorded for this month yet.",
            "Start in <b>Add</b>: type an expense, paste a bank SMS, or import your bank statement (CSV/Excel). "
            "Or open <b>Ask</b> and say “spent 250 on swiggy”.",
        )
        return

    rate = summary["savings_rate"]
    caption = (
        "of your income saved this month" if rate is not None and rate >= 0
        else "spending is above income this month" if rate is not None
        else "no income recorded yet — add your stipend or salary to see a savings rate"
    )
    big = f"{rate}<small>%</small>" if rate is not None else "—"
    cells = [("Money in", inr(summary["income"], False), False), ("Money out", inr(summary["spending"], False), False),
             ("Saved", inr(summary["saved"], False), summary["saved"] < 0)]
    st.markdown(hero_html(big, caption, cells, "Savings rate"), unsafe_allow_html=True)
    suggestions_strip()

    left, right = st.columns([3, 2], gap="large")
    with left:
        status = tools.budget_status(month)
        st.markdown('<p class="eyebrow">Budgets · the line marks how far through the month you are</p>', unsafe_allow_html=True)
        if status["categories"]:
            st.markdown(budget_rows_html(status), unsafe_allow_html=True)
        else:
            empty("No budgets for this month.", "Set limits on the <b>Budgets</b> page — that's what turns tracking into a coach.")

        st.markdown('<p class="eyebrow" style="margin-top:1.6rem">Spending by category</p>', unsafe_allow_html=True)
        if summary["by_category"]:
            cat = pd.DataFrame([{"Category": r["category"], "Spent": float(r["amount"]), "Share": f'{r["share"]}%'} for r in summary["by_category"]])
            chart = (
                alt.Chart(cat).mark_bar(cornerRadiusEnd=3, height=16, color=tok("accent"))
                .encode(
                    x=alt.X("Spent:Q", title=None, axis=alt.Axis(format=",.0f", grid=False)),
                    y=alt.Y("Category:N", sort="-x", title=None, axis=alt.Axis(labelOverlap=False, labelLimit=160)),
                    tooltip=["Category", alt.Tooltip("Spent:Q", format=",.2f"), "Share"],
                ).properties(height=max(120, 34 * len(cat)))
            )
            st.altair_chart(chart, width="stretch")
        else:
            empty("No spending this month.", "Only money coming in so far. Spending shows up here as soon as you record some.")

        daily = tools.daily_spending(start, min(end, date.today()))
        if daily and status["total_limit"] > 0:
            days = (end - start).days + 1
            frame = pd.DataFrame([
                {"Day": d["day"], "Series": "You", "Amount": float(d["cumulative"])} for d in daily
            ] + [
                {"Day": d["day"], "Series": "Budget pace",
                 "Amount": float(status["total_limit"]) * ((d["day"] - start).days + 1) / days} for d in daily
            ])
            st.markdown('<p class="eyebrow" style="margin-top:1.2rem">Cumulative spending vs budget pace</p>', unsafe_allow_html=True)
            line = (
                alt.Chart(frame).mark_line(strokeWidth=2)
                .encode(
                    x=alt.X("Day:T", title=None, axis=alt.Axis(format="%d %b", grid=False)),
                    y=alt.Y("Amount:Q", title=None, axis=alt.Axis(format=",.0f")),
                    color=alt.Color("Series:N", scale=alt.Scale(domain=["You", "Budget pace"], range=[tok("accent"), tok("ink-2")]),
                                    legend=alt.Legend(orient="top", title=None)),
                    strokeDash=alt.StrokeDash("Series:N", scale=alt.Scale(domain=["You", "Budget pace"], range=[[1, 0], [4, 4]]), legend=None),
                    tooltip=[alt.Tooltip("Day:T", format="%d %b"), "Series", alt.Tooltip("Amount:Q", format=",.0f")],
                ).properties(height=220)
            )
            st.altair_chart(line, width="stretch")

    with right:
        st.markdown('<p class="eyebrow">Latest entries</p>', unsafe_allow_html=True)
        recent = tools.list_transactions(start, end, limit=8)
        st.markdown(ledger_html(recent), unsafe_allow_html=True)
        st.page_link(PAGES["ledger"], label="Open full ledger", icon=":material/arrow_forward:")

        st.markdown('<p class="eyebrow" style="margin-top:1.6rem">Completeness check</p>', unsafe_allow_html=True)
        accounts = tools.list_accounts()
        shown = False
        for acct in accounts:
            try:
                r = tools.reconcile(acct["id"])
            except RectifiError:
                continue
            shown = True
            st.markdown(f"**{esc(acct['name'])}**")
            st.markdown(recon_html(r), unsafe_allow_html=True)
        if not shown:
            empty("Not proven complete yet.",
                  "Import a bank statement with a balance column (or record two balances on <b>Accounts</b>) "
                  "and Rectifi will prove — with arithmetic — that no transaction is missing.")


def weeks_back(n: int = 12) -> list[date]:
    """Mondays of this week and the weeks before it, newest first."""
    monday, _ = tools.week_bounds(None)
    return [monday - timedelta(days=7 * i) for i in range(n)]


def week_label(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    span = f"{monday:%d %b} – {sunday:%d %b}"
    this_monday = weeks_back(1)[0]
    if monday == this_monday:
        return f"This week · {span}"
    if monday == this_monday - timedelta(days=7):
        return f"Last week · {span}"
    return span


def compare_html(r: dict) -> str:
    """Spending this week vs the same days last week, printed like the passbook sum on the Accounts page."""
    same_days = f"{r['prev_start']:%d %b} – {r['prev_end']:%d %b}"
    comparable = r["prev_spending"] > 0
    if not comparable:
        # No spending in the earlier period: a "change" or a percentage would be meaningless, so show neither.
        verdict = f'{chip("near", "Nothing to compare")} &nbsp;No spending was recorded for {same_days}.'
    elif r["trend"] == "flat":
        verdict = f'{chip("ok", "Same")} &nbsp;Spending is the same as {same_days}.'
    else:
        word, state = ("Up", "over") if r["trend"] == "up" else ("Down", "ok")
        verdict = (f'{chip(state, word + " " + str(abs(r["spending_change_pct"])) + "%")} &nbsp;'
                   f'{"More" if r["trend"] == "up" else "Less"} than {same_days} by {inr(abs(r["spending_change"]))}.')
    change_row = (f'<tr class="total"><td>= change</td><td>{inr(r["spending_change"], sign=True)}</td></tr>\n'
                  if comparable else "")
    return f"""<div class="recon"><table>
<tr><td>Money out · {r['week_start']:%d %b} – {r['through']:%d %b}</td><td>{inr(r['spending'])}</td></tr>
<tr><td>Money out · {same_days}</td><td>{inr(r['prev_spending'])}</td></tr>
{change_row}</table><div class="verdict">{verdict}</div></div>"""


def category_list_html(rows: list[dict]) -> str:
    body = "".join(
        f'<tr><td class="who">{esc(c["category"])}<span class="cat">{c["share"]}% of money out</span></td>'
        f'<td class="amt">{inr(c["amount"])}</td></tr>'
        for c in rows
    )
    return f'<table class="ledger">{body}</table>'


def page_review():
    head_l, head_r = st.columns([5, 2], vertical_alignment="bottom")
    with head_r:
        monday = st.selectbox("Week", weeks_back(), format_func=week_label, key="rev_week")
    r = tools.weekly_review(monday)
    with head_l:
        st.title("Weekly review")
        st.caption(f"{week_label(monday)} · counted through {r['through']:%A %d %B}"
                   + ("" if not r["is_current"] else " · the week isn't over yet"))

    if r["count"] == 0:
        empty("Nothing recorded this week yet.",
              "Add what you spent on the <b>Add</b> page, or import your statement. Then come back on Sunday.")
    else:
        rate = r["savings_rate"]
        cells = [("Money in", inr(r["income"], False), False), ("Saved", inr(r["saved"], False), r["saved"] < 0),
                 ("Savings rate", f"{rate}%" if rate is not None else "—", rate is not None and rate < 0)]
        caption = f"money out · {r['week_start']:%a %d %b} to {r['through']:%a %d %b}" + (
            "" if rate is not None else " · no income this week, so no savings rate")
        st.markdown(hero_html(esc(inr(r["spending"], False)), caption, cells, "Spent this week",
                              big_style="font-size:clamp(2.8rem,6vw,4.4rem)"), unsafe_allow_html=True)

        left, right = st.columns([3, 2], gap="large")
        with left:
            st.markdown('<p class="eyebrow">Compared with the same days last week</p>' if r["is_current"]
                        else '<p class="eyebrow">Compared with the week before</p>', unsafe_allow_html=True)
            st.markdown(compare_html(r), unsafe_allow_html=True)
            if r["top_categories"]:
                st.markdown('<p class="eyebrow" style="margin-top:1.6rem">Where it went</p>', unsafe_allow_html=True)
                st.markdown(category_list_html(r["top_categories"]), unsafe_allow_html=True)
            if r["biggest_debits"]:
                st.markdown('<p class="eyebrow" style="margin-top:1.6rem">Biggest payments</p>', unsafe_allow_html=True)
                st.markdown(ledger_html(r["biggest_debits"]), unsafe_allow_html=True)

        with right:
            st.markdown('<p class="eyebrow">Needs attention</p>', unsafe_allow_html=True)
            items = []
            if r["uncategorised_count"]:
                n = r["uncategorised_count"]
                items.append(f'<div class="brow"><div class="name">{chip("near", "Check")}</div>'
                             f'<div>{n} {"entry is" if n == 1 else "entries are"} still in “{FALLBACK_EXPENSE}”. '
                             f'Give {"it" if n == 1 else "them"} a real category in the Ledger.</div></div>')
            for b in r["budgets_needing_attention"]:
                over = b["state"] == "over"
                label = "Over budget" if over else "Near limit"
                items.append(f'<div class="brow"><div class="name">{esc(b["category"])}<br>{chip(b["state"], label)}</div>'
                             f'<div>{inr(b["spent"], False)} of {inr(b["limit"], False)} used this month ({b["percent"]}%).</div></div>')
            if r["unlinked_count"]:
                n = r["unlinked_count"]
                lead = "1 entry this week isn’t" if n == 1 else f"{n} entries this week aren’t"
                items.append(f'<div class="brow"><div class="name">{chip("near", "Check")}</div>'
                             f'<div>{lead} linked to an account, so no balance check covers {"it" if n == 1 else "them"}.</div></div>')
            for a in r["accounts_needing_attention"]:
                reasons = "<br>".join(esc(x) for x in a["reasons"])
                items.append(f'<div class="brow"><div class="name">{esc(a["name"])}<br>{chip("near", "Check")}</div>'
                             f'<div>{reasons}</div></div>')
            if items:
                st.markdown("".join(items), unsafe_allow_html=True)
                if r["uncategorised_count"]:
                    st.page_link(PAGES["ledger"], label="Fix categories in the Ledger", icon=":material/receipt_long:")
                if r["budgets_needing_attention"]:
                    st.page_link(PAGES["budgets"], label="Review your Budgets", icon=":material/savings:")
                if r["unlinked_count"]:
                    st.page_link(PAGES["ledger"], label="See unlinked entries in the Ledger", icon=":material/receipt_long:")
                if r["accounts_needing_attention"]:
                    st.page_link(PAGES["accounts"], label="Check your Accounts", icon=":material/account_balance:")
            elif r["all_clear"]:
                st.markdown(f'<div class="brow"><div class="name">{chip("ok", "All clear")}</div>'
                            '<div>No budget is near its limit, nothing is uncategorised, every entry is linked to an account, and every account checks out.</div></div>',
                            unsafe_allow_html=True)

    worth = [i for i in insights.insights() if i.severity == "act"]
    if worth:
        st.markdown('<p class="eyebrow" style="margin-top:1.8rem">Insights worth acting on</p>'
                    + "".join(insight_html(i) for i in worth), unsafe_allow_html=True)
        st.page_link(PAGES["insights"], label="See all insights", icon=":material/lightbulb:")

    st.markdown('<p class="eyebrow" style="margin-top:1.8rem">One commitment</p>', unsafe_allow_html=True)
    if r["last_commitment"]:
        st.markdown(f'<div class="recon"><div class="eyebrow">What you committed to last week</div>'
                    f'<div style="font-size:1.05rem">{esc(r["last_commitment"])}</div>'
                    f'<div class="verdict">Did you keep it? Look at the numbers above, then pick your next one.</div></div>',
                    unsafe_allow_html=True)
    with st.form("commitment", border=False):
        text = st.text_input(
            "Your commitment for next week",
            value=r["commitment"] or "",
            max_chars=tools.COMMITMENT_MAX,
            placeholder="No more than 3 delivery orders",
            help="One small, specific promise. Next week this page will show it back to you.",
        )
        if st.form_submit_button("Save commitment", type="primary", icon=":material/check:"):
            try:
                tools.save_commitment(text, monday)
                st.success("Saved. You will see it here next week.", icon=":material/check_circle:")
            except RectifiError as exc:
                st.error(str(exc), icon=":material/error:")


def page_insights():
    st.title("Insights")
    st.caption("What Rectifi noticed in your own numbers, and what to do about it. Fixed rules worked out every figure: "
               "no AI decides a number, and nothing here changes your data unless you press a button.")
    span = insights.data_span()
    if span["count"] == 0:
        empty("Insights need about a month of data.",
              "Import last month's bank statement to get started: it fills in a whole month at once. "
              "You can also add what you spend on the <b>Add</b> page.")
        st.page_link(PAGES["add"], label="Go to Add", icon=":material/add_circle:")
        return
    found = insights.insights()
    if span["days"] < 28:
        st.info(f"Rectifi has {span['days']} day{'s' if span['days'] != 1 else ''} of data. Spikes, subscriptions and the savings "
                "trend need about a month, so those checks stay quiet for now. Import last month's statement to unlock them.",
                icon=":material/hourglass_top:")
    if not found:
        empty("Nothing to flag right now.",
              "No budget is heading over, nothing looks unusual, and your records look complete. Check back after a few more days of entries.")
        return
    explain_ok = assistant_ready()
    groups = [("Fix these first: they make every other number less trustworthy", [i for i in found if i.kind in insights.DATA_QUALITY_KINDS]),
              ("Act now", [i for i in found if i.kind not in insights.DATA_QUALITY_KINDS and i.severity == "act"]),
              ("Keep an eye on", [i for i in found if i.kind not in insights.DATA_QUALITY_KINDS and i.severity == "watch"]),
              ("Good to know", [i for i in found if i.kind not in insights.DATA_QUALITY_KINDS and i.severity == "info"])]
    for heading, items in groups:
        if not items:
            continue
        st.markdown(f'<p class="eyebrow" style="margin-top:1.4rem">{esc(heading)}</p>', unsafe_allow_html=True)
        for item in items:
            st.markdown(insight_html(item), unsafe_allow_html=True)
            insight_actions(item, "ins", explain_ok)
    if not explain_ok:
        st.caption("“Explain this” appears when the Ask model is on (see the Ask page). Everything above works without it.")


def _graph():
    from rectifi.agent.graph import build_graph, postgres_checkpointer

    @st.cache_resource(show_spinner=False)
    def make(provider: str):
        return build_graph(checkpointer=postgres_checkpointer())

    return make(config.llm_provider())


def page_ask():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from rectifi.agent.graph import chat, unverified_amounts
    from rectifi.agent.llm import describe, explain_error, llm_notice, llm_status

    top_l, top_r = st.columns([4, 1], vertical_alignment="bottom")
    top_l.title("Ask Rectifi")
    if top_r.button("New conversation", icon=":material/refresh:", width="stretch"):
        st.session_state.thread_id = str(uuid.uuid4())
    st.caption(f"Model: {describe()} · amounts are checked against what you type before anything is saved")

    ok, why = llm_status()
    if not ok:
        st.warning(why, icon=":material/smart_toy:")
        empty("The assistant is off — the rest of Rectifi works.",
              "You can still add entries, import statements and set budgets by hand.")
        st.page_link(PAGES["add"], label="Go to Add", icon=":material/add_circle:")
        return
    notice = llm_notice()
    if notice:
        st.info(notice, icon=":material/info:")

    thread = st.session_state.setdefault("thread_id", str(uuid.uuid4()))
    try:
        graph = _graph()
    except Exception as exc:
        st.error(f"Couldn't start the agent: {exc}", icon=":material/error:")
        return
    cfg = {"configurable": {"thread_id": thread}}
    history = graph.get_state(cfg).values.get("messages", [])

    def render(messages, context=()):
        """Draw messages. `context` is whatever came before them in the chat, so a figure in a reply can be
        checked against what the user typed and what the tools returned."""
        steps = []
        seen = list(context)
        for m in messages:
            seen.append(m)
            if isinstance(m, HumanMessage):
                with st.chat_message("user"):
                    st.markdown(m.content)
            elif isinstance(m, ToolMessage):
                steps.append(m)
            elif isinstance(m, AIMessage) and m.content:
                with st.chat_message("assistant", avatar=":material/receipt_long:"):
                    st.markdown(m.content)
                    odd = unverified_amounts(seen)
                    if odd:
                        figures = ", ".join(inr(v) for v in odd)
                        st.caption(f":material/warning: Not verified: {figures}. I can't match this to anything you typed "
                                   "or to a number Rectifi worked out, so check it before you rely on it.")
                    if steps:
                        with st.expander(f"What Rectifi did · {len(steps)} step{'s' if len(steps) > 1 else ''}"):
                            for s_ in steps:
                                prefix = "⛔ " if getattr(s_, "status", "") == "error" else ""
                                st.code(f"{prefix}{s_.name} → {s_.content}", language=None, wrap_lines=True)
                steps = []

    if not history:
        st.markdown('<p class="eyebrow">Try</p>', unsafe_allow_html=True)
        examples = ["spent 250 on swiggy", "got 8000 stipend today", "how much did I spend on food this month?",
                    "set a 3000 budget for food", "am I over budget anywhere?"]
        cols = st.columns(len(examples))
        for col, ex in zip(cols, examples):
            if col.button(ex, width="stretch"):
                st.session_state.pending_prompt = ex
                st.rerun()
    render(history)

    prompt = st.chat_input("Spent, received, paste an SMS, or ask a question…") or st.session_state.pop("pending_prompt", None)
    if prompt:
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.spinner("Thinking…"):
            try:
                new = chat(graph, prompt, thread)
            except Exception as exc:
                st.error(f"The model call failed: {explain_error(exc)}", icon=":material/cloud_off:")
                return
        render([m for m in new if not isinstance(m, HumanMessage)], context=history + [HumanMessage(prompt)])


NO_ACCOUNT = "No account"


def statement_table(upload) -> pd.DataFrame | None:
    """The uploaded statement as a table, or None (after showing why).

    CSV and Excel are read at once. A PDF is tried without a password first. If it needs one, the password goes
    into a form that empties itself when it is submitted, and only the resulting table is kept in the session,
    never the password."""
    data = upload.getvalue()
    if not upload.name.lower().endswith(".pdf"):
        try:
            return read_table(data, upload.name)
        except RectifiError as exc:
            st.error(str(exc), icon=":material/error:")
            return None

    key = f"{upload.name}:{hashlib.sha256(data).hexdigest()}"
    state = st.session_state.get("pdf_read")
    if state is None or state["key"] != key:
        state = {"key": key, "table": None, "error": None}
        try:
            state["table"] = read_table(data, upload.name)          # most bank PDFs open without a password
        except RectifiError as exc:
            state["error"] = str(exc)
        st.session_state["pdf_read"] = state
    message = st.container()
    box = st.expander("PDF password", expanded=state["table"] is None, icon=":material/key:")
    with box:
        with st.form("pdf_password", clear_on_submit=True, border=False):
            typed = st.text_input("PDF password (optional)", type="password",
                                  help="If your bank's email gave a password for this PDF, enter it.")
            submitted = st.form_submit_button("Read the PDF with this password", icon=":material/lock_open:")
        if submitted:
            try:
                state["table"], state["error"] = read_table(data, upload.name, typed or None), None
            except RectifiError as exc:
                state["table"], state["error"] = None, str(exc)
        typed = None                                                  # the password is not kept anywhere
    if state["table"] is None:
        message.error(state["error"], icon=":material/error:")
    return state["table"]


def account_choices(accounts: list[dict]) -> tuple[list[str], int]:
    """Options and starting index for an Account picker.

    With at least one account, one is already selected (see tools.default_account), so an entry is never
    unlinked by accident. "No account" stays in the list as an explicit choice, last.
    """
    names = [a["name"] for a in accounts]
    default = tools.default_account()
    return names + [NO_ACCOUNT], (names.index(default["name"]) if default and default["name"] in names else len(names))


IMPORT_HELP = ("Download a CSV, Excel or PDF statement from net banking. Rectifi finds the columns and, when they are clear, "
               "imports at once: it skips rows it already has, merges entries you logged by hand, and uses the balance column "
               "to prove nothing is missing. If anything is unclear it shows you the columns first instead of guessing. "
               "PDFs are read on this laptop only; a PDF that is a scan or a photo can't be read.")


def import_card(entry: dict, file_hash: str) -> None:
    """The result of one statement import: what happened to each row, where it went, the balance check, and Undo."""
    batch = tools.get_import_batch(entry["batch_id"])
    done = st.session_state.setdefault("auto_imports", {})
    if batch is None:
        done.pop(file_hash, None)
        return
    if batch["undone_at"] is not None:
        st.markdown(f'<div class="empty"><strong>Import undone.</strong><br>The {batch["added"]} entries this import added were removed. '
                    "Entries you made by hand, and rows that were already recorded, were not touched.</div>", unsafe_allow_html=True)
        if st.button("Import this file again", key=f"again_{file_hash[:12]}", icon=":material/upload:"):
            done.pop(file_hash, None)
            st.rerun()
        return
    how = ("matched by the account digits in the statement" if entry.get("by_digits")
           else "chosen by you" if entry.get("chosen") else
           "the statement does not say which account it is, so this is your most recently used account" if entry.get("guessed")
           else "your only account")
    needs = batch["needs_review"]
    st.markdown(
        f"""<div class="recon"><div class="eyebrow">{esc(batch["filename"] or "Statement")}</div><table>
<tr><td>Added as new entries</td><td>{batch["added"]}</td></tr>
<tr><td>Merged with entries you made by hand</td><td>{batch["merged"]}</td></tr>
<tr><td>Already recorded (skipped)</td><td>{batch["duplicates"]}</td></tr>
<tr><td>Needs review (not imported)</td><td>{needs}</td></tr>
</table><div class="verdict">{chip("ok", "Imported")} &nbsp;Went into <b>{esc(batch["account"])}</b> ({esc(how)}).</div></div>""",
        unsafe_allow_html=True,
    )
    if needs:
        st.warning(f"{needs} row(s) were NOT imported: Rectifi can't prove from the balance (or a Dr/Cr mark) whether that money "
                   "went out or came in, and it does not guess. The balance check below shows them as a gap until you add "
                   "them by hand under Quick entry, or use the CSV or Excel statement.", icon=":material/rule:")
        st.markdown("\n".join(f"- {line}" for line in entry.get("needs_review", [])))
    for line in entry.get("skipped", [])[:5]:
        st.caption(line)
    st.markdown('<p class="eyebrow" style="margin-top:1rem">Balance check</p>', unsafe_allow_html=True)
    try:
        st.markdown(recon_html(tools.reconcile(batch["account_id"])), unsafe_allow_html=True)
    except RectifiError as exc:
        st.caption(str(exc))
    if batch["added"] == 0:
        st.caption("This import added nothing new, so there is nothing to undo.")
    elif st.button("Undo this import", key=f"undo_{batch['id']}", icon=":material/undo:",
                   help="Removes exactly the entries this import added. Merged and already-recorded rows are not touched."):
        undone = tools.undo_import(batch["id"])
        st.toast(f"Removed {undone['removed']} entries.", icon=":material/undo:")
        st.rerun()


def run_import(rows, account, upload, file_hash: str, **extra) -> None:
    """Import, remember it for this session (so a Streamlit rerun can't import the same file twice), show the card."""
    try:
        res = import_rows(rows, account, upload.name)
    except RectifiError as exc:
        st.error(str(exc), icon=":material/error:")
        return
    st.session_state.setdefault("auto_imports", {})[file_hash] = {
        "batch_id": res.batch_id, "needs_review": res.needs_review, "skipped": res.skipped, **extra}
    if res.batch_id is None:
        st.session_state["auto_imports"].pop(file_hash)
        st.warning("Nothing could be imported: every row needs review.", icon=":material/rule:")
        return
    import_card(st.session_state["auto_imports"][file_hash], file_hash)


def statement_tab(accounts: list[dict]) -> None:
    st.caption(IMPORT_HELP)
    if not accounts:
        st.info("Add the bank account first (Accounts page) so imported rows know where they belong.", icon=":material/account_balance:")
    manual = st.checkbox("Let me check the columns and choose the account myself (don't import automatically)", key="stmt_manual")
    upload = st.file_uploader("Statement file", type=["csv", "xlsx", "xls", "pdf"])
    if upload is None:
        return
    table = statement_table(upload)
    if table is None:
        return
    file_hash = hashlib.sha256(upload.getvalue()).hexdigest()
    done = st.session_state.setdefault("auto_imports", {})
    if file_hash in done:                                   # this file was already imported in this session: show that, do nothing
        import_card(done[file_hash], file_hash)
        return

    check = assess_statement(table)
    if accounts and check.confident and not manual:
        pick = tools.account_for_statement(check.account_last4)
        if pick["account"] is not None:
            multi = len(accounts) > 1
            run_import(check.rows, pick["account"]["id"], upload, file_hash, by_digits=pick["by_digits"],
                       guessed=multi and not pick["by_digits"])
            return
        st.info(pick["reason"], icon=":material/account_balance:")
    elif not check.confident:
        st.info("Rectifi is not sure it read this file right, so nothing was imported yet. " + " ".join(check.reasons)
                + " Check the columns below, then import.", icon=":material/help:")
    manual_preview(table, upload, accounts, file_hash, check)


def manual_preview(table: pd.DataFrame, upload, accounts: list[dict], file_hash: str, check) -> None:
    """The step-by-step import: you see the detected columns and a preview, pick the account, and press Import."""
    detected = check.mapping
    cols = ["—"] + [c for c in table.columns if not str(c).startswith("__")]
    needs = not detected.get("date") or not detected.get("narration") or not (
        (detected.get("debit") and detected.get("credit")) or detected.get("amount"))
    with st.expander("Columns Rectifi detected", expanded=needs or not check.confident, icon=":material/view_column:"):
        grid = st.columns(4)
        mapping = {}
        for i, role in enumerate(["date", "narration", "debit", "credit", "amount", "type", "balance", "ref"]):
            current = detected.get(role)
            mapping[role] = grid[i % 4].selectbox(role.capitalize(), cols, index=cols.index(current) if current in cols else 0, key=f"map_{role}")
        mapping = {k: (None if v == "—" else v) for k, v in mapping.items()}
    try:
        rows, problems = normalise(table, mapping)
    except RectifiError as exc:
        st.error(str(exc), icon=":material/error:")
        return
    if not rows:
        st.warning("No transactions found with these column choices.", icon=":material/help:")
        return
    review = [r for r in rows if r.review]
    rows = [r for r in rows if not r.review]
    if review:
        st.warning(f"{len(review)} row(s) need your review and will NOT be imported: Rectifi can't prove from the balance "
                   "(or a Dr/Cr mark) whether that money went out or came in, and it does not guess. "
                   "Add them by hand under Quick entry, or use the CSV or Excel statement.", icon=":material/rule:")
        st.dataframe(pd.DataFrame([{"Date": r.txn_date, "Narration": r.narration, "Amount": inr(r.amount), "Why": r.review}
                                   for r in review]), hide_index=True, width="stretch")
    if not rows:
        return
    out = sum((r.amount for r in rows if r.direction == "debit"), Decimal(0))
    inn = sum((r.amount for r in rows if r.direction == "credit"), Decimal(0))
    m = st.columns(4)
    m[0].metric("Rows", len(rows))
    m[1].metric("Period", f"{rows[0].txn_date:%d %b} – {rows[-1].txn_date:%d %b}")
    m[2].metric("Money out", inr(out, False))
    m[3].metric("Money in", inr(inn, False))
    st.dataframe(
        pd.DataFrame([{"Date": r.txn_date, "Narration": r.narration, "Out": inr(r.amount) if r.direction == "debit" else "",
                       "In": inr(r.amount) if r.direction == "credit" else "", "Balance": inr(r.balance) if r.balance is not None else ""}
                      for r in rows[:12]]),
        hide_index=True, width="stretch",
    )
    for p in problems[:5]:
        st.caption(p)
    if accounts:
        pick = tools.account_for_statement(check.account_last4)
        names = [a["name"] for a in accounts]
        start = names.index(pick["account"]["name"]) if pick["account"] and pick["account"]["name"] in names else 0
        target = st.selectbox("Import into account", names, index=start)
        if st.button(f"Import {len(rows)} rows", type="primary", icon=":material/upload:"):
            run_import(rows + review, target, upload, file_hash, chosen=True)


def page_add():
    st.title("Add")
    pre = st.session_state.get("_prefill")           # an SMS from the Phone page's "Needs review" list, if one was sent here
    if pre:
        st.info("Filled in from a bank SMS your phone sent. Rectifi's fixed rules could not read all of it, "
                "so check every field, then save.", icon=":material/smartphone:")
        st.code(pre["raw_text"], language=None, wrap_lines=True)
        if st.button("Forget this SMS", icon=":material/close:"):
            del st.session_state["_prefill"]
            st.rerun()
    quick, sms_tab, statement = st.tabs(["Quick entry", "Paste bank SMS", "Import statement"])
    categories = tools.list_categories()
    accounts = tools.list_accounts()

    with quick:
        with st.form("quick", clear_on_submit=True, border=False):
            c1, c2, c3 = st.columns([1.2, 1, 1])
            amount = c1.text_input("Amount (₹)", value=str(pre["amount"]) if pre and pre.get("amount") else "", placeholder="250")
            direction = c2.segmented_control("Type", ["Money out", "Money in"],
                                             default="Money in" if pre and pre.get("direction") == "credit" else "Money out")
            when = c3.date_input("Date", value=pre["txn_date"] if pre and pre.get("txn_date") else date.today(),
                                 max_value=date.today(), format="DD/MM/YYYY")
            c4, c5, c6 = st.columns([1.2, 1, 1])
            payee = c4.text_input("Paid to / received from", value=(pre or {}).get("payee") or "", placeholder="Swiggy")
            kind = "income" if direction == "Money in" else "expense"
            cat = c5.selectbox("Category", ["Auto (from payee)"] + [c["name"] for c in categories if c["kind"] in (kind, "transfer")])
            acct_options, acct_index = account_choices(accounts)
            if pre and pre.get("account_id"):
                wanted = next((a["name"] for a in accounts if a["id"] == pre["account_id"]), None)
                acct_index = acct_options.index(wanted) if wanted in acct_options else acct_index
            acct = c6.selectbox("Account", acct_options, index=acct_index,
                                help="Entries with an account are covered by the balance check. “No account” entries are not.")
            note = st.text_input("Note (optional)")
            if st.form_submit_button("Save entry", type="primary", icon=":material/check:"):
                try:
                    r = tools.add_transaction(
                        amount, "credit" if direction == "Money in" else "debit", when, payee=payee or None,
                        category=None if cat.startswith("Auto") else cat, account=None if acct == NO_ACCOUNT else acct,
                        note=note or None, source="sms" if pre else "manual",
                        upi_ref=pre.get("upi_ref") if pre else None, raw_text=pre["raw_text"] if pre else None,
                    )
                    t = r["transaction"]
                    if r["status"] == "duplicate":
                        st.info("Already recorded (same UPI reference). Nothing was added.", icon=":material/content_copy:")
                    else:
                        st.success(f"Saved {inr(t['amount'])} · {t['payee'] or '—'} · {t['category']}", icon=":material/check_circle:")
                    if pre:
                        tools.mark_inbox_added(pre["inbox_id"])
                        del st.session_state["_prefill"]
                except RectifiError as exc:
                    st.error(str(exc), icon=":material/error:")

    with sms_tab:
        text = st.text_area("Bank SMS", height=110, placeholder="Rs.250.00 debited from A/c XX1234 on 21-10-26 to VPA swiggy@icici UPI Ref No 429183746521")
        if text.strip():
            parsed = parse_sms(text)
            if parsed is None:
                st.warning("That doesn't look like a transaction SMS — no amount, or no debited/credited wording.", icon=":material/help:")
            else:
                cols = st.columns(5)
                cols[0].metric("Amount", inr(parsed.amount))
                cols[1].metric("Type", "Money out" if parsed.direction == "debit" else "Money in")
                cols[2].metric("Date", parsed.txn_date.strftime("%d %b %Y") if parsed.txn_date else "today")
                cols[3].metric("Payee", parsed.payee or "—")
                cols[4].metric("UPI ref", parsed.upi_ref or "—")
                for w in parsed.warnings:
                    st.caption(w)
                if st.button("Save from SMS", type="primary", icon=":material/check:"):
                    match = [a for a in accounts if a["last4"] and parsed.account_last4 and a["last4"][-4:] == parsed.account_last4]
                    # an SMS that names no account, with exactly one account on file, can only belong to that one
                    sms_account = match[0]["id"] if match else (tools.only_account_id() if not parsed.account_last4 else None)
                    try:
                        r = tools.add_transaction(parsed.amount, parsed.direction, parsed.txn_date, payee=parsed.payee,
                                                  account=sms_account, source="sms",
                                                  upi_ref=parsed.upi_ref, raw_text=text)
                        if r["status"] == "duplicate":
                            st.info("Already recorded — same UPI reference. Nothing was added.", icon=":material/content_copy:")
                        else:
                            st.success(f"Saved {inr(parsed.amount)} under {r['transaction']['category']}.", icon=":material/check_circle:")
                    except RectifiError as exc:
                        st.error(str(exc), icon=":material/error:")

    with statement:
        statement_tab(accounts)


def page_ledger():
    st.title("Ledger")
    f1, f2, f3 = st.columns([1, 1, 1.4])
    period = f1.selectbox("Period", ["All time"] + months_back(), format_func=lambda d: d if isinstance(d, str) else d.strftime("%B %Y"))
    categories = [c["name"] for c in tools.list_categories()]
    cat = f2.selectbox("Category", ["All"] + categories)
    search = f3.text_input("Search payee, narration or note", placeholder="swiggy")
    start, end = (None, None) if isinstance(period, str) else tools.month_bounds(period)
    rows = tools.list_transactions(start, end, category=None if cat == "All" else cat, search=search or None, limit=LEDGER_LIMIT)
    if not rows:
        empty("No transactions match.", "Change the filters, or add entries on the <b>Add</b> page.")
        return
    frame = pd.DataFrame([{
        "id": t["id"], "Date": t["txn_date"], "Payee": t["payee"] or t["description"] or "—", "Category": t["category"],
        "Amount": ("+" if t["direction"] == "credit" else "−") + inr(t["amount"]), "Source": t["source"],
        "Account": t["account"] or "", "Note": t["note"] or "", "Delete": False,
    } for t in rows])
    st.caption(f"{len(rows)} transactions · edit Category or Note in place, then save. Changing a category teaches Rectifi that payee.")
    if len(rows) >= LEDGER_LIMIT:
        st.warning(f"Showing only the newest {LEDGER_LIMIT} transactions. Pick a month or type a search to see older ones.",
                   icon=":material/info:")
    edited = st.data_editor(
        frame, hide_index=True, width="stretch", key="ledger_editor",
        disabled=["id", "Date", "Payee", "Amount", "Source", "Account"],
        column_config={
            "id": None,
            "Date": st.column_config.DateColumn(format="DD MMM YYYY"),
            "Category": st.column_config.SelectboxColumn(options=categories, required=True),
            "Amount": st.column_config.TextColumn(width="small"),
            "Delete": st.column_config.CheckboxColumn(help="Tick, then Save, to delete"),
        },
    )
    if st.button("Save changes", type="primary", icon=":material/save:"):
        changed = deleted = 0
        problems = []
        for before, after in zip(frame.to_dict("records"), edited.to_dict("records")):
            label = f"{before['Date']:%d %b} · {before['Payee']} · {before['Amount']}"
            if after["Delete"]:
                tools.delete_transaction(int(after["id"]))
                deleted += 1
                continue
            if after["Category"] != before["Category"]:
                try:
                    tools.set_category(int(after["id"]), after["Category"], remember=True)
                    changed += 1
                except RectifiError as exc:
                    problems.append(f"{label}: {exc}")
            if after["Note"] != before["Note"]:
                tools.update_note(int(after["id"]), after["Note"])
                changed += 1
        if problems:
            st.error("Some rows were not changed:\n\n" + "\n".join(f"- {p}" for p in problems), icon=":material/error:")
            st.toast(f"Saved {changed} change(s), {len(problems)} row(s) need another look.", icon=":material/warning:")
        else:
            st.toast(f"Saved {changed} change(s), deleted {deleted}.", icon=":material/check_circle:")
            st.rerun()


def page_budgets():
    head_l, head_r = st.columns([3, 1], vertical_alignment="bottom")
    head_l.title("Budgets")
    with head_r:
        month = month_picker("bud_month")
    status = tools.budget_status(month)
    current = {b["category"]: b["limit"] for b in status["categories"]}    # Decimals
    expense = [c["name"] for c in tools.list_categories("expense")]

    if status["categories"]:
        st.markdown(f'<p class="eyebrow">{month:%B} · {status["pace"]}% of the month gone · '
                    f'{inr(status["total_spent"], False)} of {inr(status["total_limit"], False)} used</p>', unsafe_allow_html=True)
        st.markdown(budget_rows_html(status), unsafe_allow_html=True)
    else:
        empty("No budgets for this month yet.", "Set limits below. Start with the 3–4 categories where your money actually goes.")
        prev = (month - timedelta(days=1)).replace(day=1)
        if st.button(f"Copy budgets from {prev:%B}", icon=":material/content_copy:"):
            n = tools.copy_budgets(prev, month)
            st.toast(f"Copied {n} budget(s)." if n else f"No budgets in {prev:%B} to copy.")
            st.rerun()

    st.markdown('<p class="eyebrow" style="margin-top:1.6rem">Monthly limits</p>', unsafe_allow_html=True)
    frame = pd.DataFrame([{"Category": c, "Limit (₹)": float(current[c]) if c in current else None} for c in expense])
    edited = st.data_editor(
        frame, hide_index=True, width="stretch", disabled=["Category"], key=f"budget_editor_{month}",
        column_config={"Limit (₹)": st.column_config.NumberColumn(min_value=0, step=100, format="₹ %d", help="Leave empty for no budget")},
    )
    if st.button("Save budgets", type="primary", icon=":material/save:"):
        for row in edited.to_dict("records"):
            value = row["Limit (₹)"]
            had = row["Category"] in current
            if value is None or pd.isna(value) or value <= 0:
                if had:
                    tools.delete_budget(row["Category"], month)
            else:
                new_limit = parse_amount(str(int(value)) if float(value).is_integer() else str(value))
                if not had or new_limit != current[row["Category"]]:      # compare as Decimals, never floats
                    tools.set_budget(row["Category"], new_limit, month)
        st.toast("Budgets saved.", icon=":material/check_circle:")
        st.rerun()


def page_accounts():
    st.title("Accounts")
    st.caption("Reconciliation needs the bank's balance on two dates. A statement import records them automatically; "
               "you can also type them in from your passbook or banking app.")
    accounts = tools.list_accounts()
    left, right = st.columns([3, 2], gap="large")
    with right:
        st.markdown('<p class="eyebrow">Add an account</p>', unsafe_allow_html=True)
        with st.form("acct", clear_on_submit=True, border=False):
            name = st.text_input("Name", placeholder="SBI Savings")
            last4 = st.text_input("Last 4 digits (matches SMS)", max_chars=4, placeholder="1234")
            if st.form_submit_button("Add account", icon=":material/add:"):
                try:
                    tools.add_account(name, last4)
                    st.rerun()
                except RectifiError as exc:
                    st.error(str(exc))
        if accounts:
            st.markdown('<p class="eyebrow" style="margin-top:1.4rem">Record a balance</p>', unsafe_allow_html=True)
            with st.form("bal", clear_on_submit=True, border=False):
                acct = st.selectbox("Account", [a["name"] for a in accounts])
                when = st.date_input("End of day", value=date.today(), max_value=date.today(), format="DD/MM/YYYY")
                balance = st.text_input("Balance (₹)", placeholder="12,000")
                if st.form_submit_button("Save balance", icon=":material/save:"):
                    try:
                        tools.record_balance(acct, when, balance)
                        st.rerun()
                    except RectifiError as exc:
                        st.error(str(exc))
    with left:
        if not accounts:
            empty("No accounts yet.", "Add the bank account you spend from. Rectifi uses it to match SMS and statements.")
            return
        for a in accounts:
            st.markdown(f"#### {esc(a['name'])}" + (f" <span class='mono' style='color:var(--ink-2)'>··{esc(a['last4'])}</span>" if a["last4"] else ""),
                        unsafe_allow_html=True)
            try:
                st.markdown(recon_html(tools.reconcile(a["id"])), unsafe_allow_html=True)
            except RectifiError as exc:
                st.caption(f"{a['transactions']} transactions · {exc}")
            st.write("")


# ───────────────────────── phone SMS capture ─────────────────────────

SENDER_REGEX = forwarder_config.SENDER_REGEX      # the by-hand values live in rectifi/forwarder_config.py,
TEXT_REGEX = forwarder_config.TEXT_REGEX          # next to the import file that uses the same ones
JSON_TEMPLATE = forwarder_config.JSON_TEMPLATE


def seen_label(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    local = moment.astimezone()
    minutes = int((datetime.now().astimezone() - local).total_seconds() // 60)
    if minutes < 1:
        ago = "just now"
    elif minutes < 60:
        ago = f"{minutes} min ago"
    elif minutes < 24 * 60:
        ago = f"{minutes // 60} h ago"
    else:
        ago = f"{minutes // (24 * 60)} days ago"
    return f"{ago} ({local:%d %b, %H:%M})"


def tailscale_info() -> dict:
    """What Tailscale says about this laptop right now: installed? running? its MagicDNS name? which https ports are published?
    Two quick commands, nothing cached, so the page is always current. Without Tailscale nothing is run at all."""
    cli = tailscale.find_cli()
    name = tailscale.dns_name(tailscale.read_status(cli)) if cli else None
    ports = tailscale.read_served_ports(cli) if name else set()
    return {"installed": cli is not None, "name": name, "app_published": tailscale.HTTPS_APP_PORT in ports,
            "gateway_published": tailscale.HTTPS_GATEWAY_PORT in ports}


def phone_card(info: dict) -> None:
    """"Open Rectifi on your phone": the app's own address on your tailnet, and what to do if there is none yet."""
    st.markdown('<p class="eyebrow">Open Rectifi on your phone</p>', unsafe_allow_html=True)
    if info["name"]:
        url = tailscale.app_url(info["name"])
        if info["app_published"]:
            st.markdown(f'{chip("ok", "Published")} &nbsp;On your phone (Tailscale switched on, same account), open this in Chrome:',
                        unsafe_allow_html=True)
        else:
            st.markdown(f'{chip("near", "Not published yet")} &nbsp;Run <code>python scripts/dev.py tailscale</code> on this laptop once. '
                        "Then this address works on your phone:", unsafe_allow_html=True)
        st.code(url, language=None)
        st.caption("Only devices signed in to your own Tailscale account can open it. To keep it like an app: in Chrome press the "
                   "⋮ menu, then “Add to Home screen”.")
    elif info["installed"]:
        st.markdown(f'{chip("near", "Tailscale is off")} &nbsp;Tailscale is installed on this laptop but not running or not signed in. '
                    "Open the Tailscale app and sign in, then reload this page.", unsafe_allow_html=True)
    else:
        st.markdown(f'{chip("near", "Not set up")} &nbsp;Install <b>Tailscale</b> (free) on this laptop and on your phone, sign in to the '
                    "same account on both, then run <code>python scripts/dev.py tailscale</code>. This page then shows the address to open. "
                    "Full steps: README, “Use Rectifi on your phone”.", unsafe_allow_html=True)


def pairing_panel(device: dict, port: int, gateway_up: bool, info: dict) -> None:
    """Set up a phone: one downloaded file for the Android app, or the same values to type by hand.
    The token is on screen (and inside the in-memory file) only while this panel is open."""
    token = device["token"]
    st.markdown(f'<p class="eyebrow">Set up “{esc(device["name"])}”</p>', unsafe_allow_html=True)
    st.warning("Rectifi keeps only a fingerprint (a hash) of the token, so it can't be shown again. Download the setup file "
               "now (or copy the values under “Set it up by hand instead”). Lose it and you pair the phone again.",
               icon=":material/key:")
    open_lan = config.sms_host_is_open()
    addresses = sms_gateway.private_ipv4s() if open_lan else []
    secure_url = tailscale.gateway_url(info["name"]) if info["name"] else None
    # The address the phone will post to: the Tailscale HTTPS one, else (only when the host was opened on purpose) the first LAN one.
    gateway_url = secure_url or (f"http://{addresses[0]['ip']}:{port}/sms" if addresses else None)

    st.markdown("**First, on your phone:** install **SMS to URL Forwarder** from F-Droid or IzzyOnDroid (package "
                "`tech.bogomolov.incomingsmsgateway`). Then do the steps below **on the phone**: open Rectifi there "
                "(the address at the top of this page) and come to this page.")
    if gateway_url:
        # In memory only: the bytes go to the browser and are never written to this laptop's disk.
        setup_file = forwarder_config.build_forwarder_rules(
            gateway_url, token, device_id=device["id"], text_filter=TEXT_REGEX).encode("utf-8")
        st.download_button("Download setup file for SMS to URL Forwarder", setup_file,
                           file_name=forwarder_config.FILE_NAME, mime="application/json", type="primary",
                           width="stretch", icon=":material/download:", on_click="ignore", key="download_setup_file")
        if secure_url and not info["gateway_published"]:
            st.warning("The gateway is not published on your tailnet yet. Run `python scripts/dev.py tailscale` on this laptop "
                       "(with `RECTIFI_SMS_GATEWAY=1` in `.env`), then reload this page.", icon=":material/lan:")
    else:
        st.download_button("Download setup file for SMS to URL Forwarder", b"", file_name=forwarder_config.FILE_NAME,
                           mime="application/json", type="primary", width="stretch", icon=":material/download:",
                           disabled=True, on_click="ignore", key="download_setup_file")
        st.info("The setup file needs the address your phone will post to, and there is none yet. The gateway listens on this "
                "laptop only, so the phone reaches it through Tailscale: install Tailscale on the laptop and the phone (same "
                "account), run `python scripts/dev.py tailscale`, then reload this page. "
                "Steps: README, “Use Rectifi on your phone”.", icon=":material/lan:")
    st.markdown(
        "1. Tap **Download setup file** above.\n"
        "2. Open **SMS to URL Forwarder**, tap the ⋮ menu (or **Settings**), then **Import**, and choose "
        "`rectifi-sms-setup.json` from **Downloads**.\n"
        "3. Allow the SMS permission when the app asks. In Android's battery settings, set the app to **Unrestricted** "
        "so the system does not stop it.\n"
        "4. Tap **Send a test message** below."
    )
    st.caption("Delete rectifi-sms-setup.json from Downloads afterwards. It contains your key.")

    health = (f"{secure_url.rsplit('/sms', 1)[0]}/health" if secure_url
              else f"http://{addresses[0]['ip']}:{port}/health" if addresses else None)
    st.markdown("**Test it.** With the gateway on, press the button below. It sends a harmless message from this laptop "
                "using the token, so it proves the gateway and the token work. It does not prove your phone can reach "
                "the laptop: for that, open this address in the phone's browser"
                + (f": `{health}`. It should show `{{\"ok\": true}}`." if health else " once you have an address."))
    if st.button("Send a test message", icon=":material/send:", disabled=not gateway_up,
                 help=None if gateway_up else "Turn the gateway on first (see the top of this page)."):
        status, said = sms_gateway.send_test_sms(token, port)
        if status == 200 and said == "ignored":
            st.success("It works. The gateway accepted the token and answered “ignored”, which is right: a test message "
                       "is not a transaction, so nothing was stored.", icon=":material/check_circle:")
        elif status == 200:
            st.info(f"The gateway answered “{said}”.", icon=":material/info:")
        else:
            st.error(f"Test failed: {said}.", icon=":material/error:")

    with st.expander("Set it up by hand instead", expanded=False, icon=":material/edit_note:"):
        st.markdown("In the app, add a forwarding rule with these values:")
        if secure_url:
            st.markdown("Webhook URL (HTTPS, through Tailscale: only your own devices can use it):")
            st.code(secure_url, language=None)
            if not info["gateway_published"]:
                st.warning("The gateway is not published on your tailnet yet. Run `python scripts/dev.py tailscale` on this laptop "
                           "(with `RECTIFI_SMS_GATEWAY=1` in `.env`), then reload this page.", icon=":material/lan:")
        elif not open_lan:
            st.info("The gateway listens on this laptop only, so your phone needs Tailscale to reach it. Install Tailscale on both "
                    "devices (same account), run `python scripts/dev.py tailscale`, and the HTTPS address appears here. "
                    "Steps: README, “Use Rectifi on your phone”.", icon=":material/lan:")
        if open_lan:
            st.warning(f"Advanced: RECTIFI_SMS_HOST is {config.sms_host()}, so the gateway is open to everyone on the same network and "
                       "the token is the only lock. The addresses below are plain HTTP. Prefer the default (this laptop only) with Tailscale.",
                       icon=":material/warning:")
            if addresses:
                st.markdown("Local network addresses (the phone must be on the same Wi-Fi; if the first does not work, try the next):")
                for a in addresses:
                    st.code(f"http://{a['ip']}:{port}/sms", language=None)
                    st.caption(a["label"])
            else:
                st.info("Rectifi could not find this laptop's network address. On Windows run `ipconfig` and use the IPv4 Address, "
                        f"like `http://192.168.x.x:{port}/sms`.", icon=":material/lan:")
        st.markdown("Custom header (the app asks for headers as JSON):")
        st.code(json.dumps({"Authorization": f"Bearer {token}"}), language="json")
        st.caption(f"That is the header `Authorization: Bearer {token}`")
        st.markdown("JSON body template:")
        st.code(JSON_TEMPLATE, language="json")
        st.markdown("Sender filter (regex). Indian bank SMS come from IDs like `AX-HDFCBK` or `VM-SBIUPI`. "
                    "This may need adjusting for your bank (the setup file accepts any sender and relies on the text filter):")
        st.code(SENDER_REGEX, language=None)
        st.markdown("Text filter (regex):")
        st.code(TEXT_REGEX, language=None)
        st.markdown("If the app has a “Local network mode” option, tick it: it is for addresses on your own network. "
                    "Leave retries on (the app tries again for a while if the laptop is off).")
    with st.expander("iPhone (Shortcuts): untested", expanded=False, icon=":material/phone_iphone:"):
        st.warning("These iPhone steps are written from how Shortcuts works. Nobody has run them against Rectifi yet, "
                   "so expect to adjust them.", icon=":material/science:")
        st.markdown(
            "1. Open **Shortcuts**, go to **Automation**, tap **+**, choose **Message**.\n"
            "2. Set **Message Contains** to a word such as `debited`. Make one automation per word: `debited`, `credited`.\n"
            "3. Choose **Run Immediately** (not “Run after confirmation”).\n"
            "4. Add the action **Get Contents of URL**. Set the URL to the first address above.\n"
            "5. Open **Show More**: Method **POST**, add a header `Authorization` with the value `Bearer <your token>`, "
            "set Request Body to **JSON**, and add two fields: `text` = the **Shortcut Input** (the message), `from` = **Sender** of the message.\n"
            "6. Save. Send yourself a test message to check."
        )
    if st.button("Done: hide the token", icon=":material/visibility_off:"):
        del st.session_state["new_device"]
        st.rerun()


def page_phone():
    st.title("Phone")
    info = tailscale_info()
    phone_card(info)
    st.write("")
    st.markdown(
        '<div class="empty"><strong>How this works, honestly.</strong><br>Nothing can read SMS from a phone number. '
        'Instead, your phone <em>forwards</em> each bank SMS to this laptop, over Tailscale (HTTPS, only your own devices). Rectifi reads it with fixed '
        'rules (no AI), adds it, and skips duplicates. If the laptop was off, anything missed is caught later by '
        'importing a statement and the balance check.</div>',
        unsafe_allow_html=True,
    )
    port = config.sms_port()
    gateway_up = sms_gateway.gateway_reachable(port)
    st.write("")
    st.markdown('<p class="eyebrow">Gateway</p>', unsafe_allow_html=True)
    if gateway_up:
        st.markdown(f'{chip("ok", "On")} &nbsp;The gateway answers on this laptop at '
                    f'<code>http://127.0.0.1:{port}/health</code>.', unsafe_allow_html=True)
        if config.sms_host_is_open():
            st.warning(f"It listens on {config.sms_host()}: open to everyone on the same network, with the device token as the only lock. "
                       "Remove RECTIFI_SMS_HOST from .env to go back to this laptop only (the default).", icon=":material/warning:")
        else:
            st.caption("It listens on this laptop only (127.0.0.1). Your phone reaches it through Tailscale, which only your own "
                       "devices can use. Switch the gateway off when you do not need it.")
    else:
        st.markdown(f'{chip("near", "Off")} &nbsp;Nothing answers at <code>http://127.0.0.1:{port}/health</code>. '
                    "This is the default: the gateway is off until you turn it on.", unsafe_allow_html=True)
        st.markdown("To turn it on for good, add these lines to your `.env` file, then restart with `python scripts/dev.py up`:")
        st.code(f"RECTIFI_SMS_GATEWAY=1\nRECTIFI_SMS_PORT={port}", language="ini")
        st.markdown("Or, just for now, run this in a second terminal:")
        st.code("python scripts/dev.py phone", language="bash")
        st.caption("Then run `python scripts/dev.py tailscale` so your phone can reach it over HTTPS.")

    st.write("")
    st.markdown('<p class="eyebrow">Pair a phone</p>', unsafe_allow_html=True)
    with st.form("pair", clear_on_submit=True, border=False):
        name = st.text_input("Phone name", placeholder="My Android", max_chars=tools.DEVICE_NAME_MAX)
        if st.form_submit_button("Create a pairing token", icon=":material/add_link:"):
            try:
                st.session_state["new_device"] = tools.create_device(name)
            except RectifiError as exc:
                st.error(str(exc), icon=":material/error:")
    device = st.session_state.get("new_device")
    if device:
        pairing_panel(device, port, gateway_up, info)

    devices = tools.list_devices()
    st.write("")
    st.markdown('<p class="eyebrow">Paired phones</p>', unsafe_allow_html=True)
    if not devices:
        empty("No phone paired yet.", "Create a pairing token above.")
    for d in devices:
        left, mid, right = st.columns([2, 2.4, 1])
        revoked = d["revoked_at"] is not None
        left.markdown(f"**{esc(d['name'])}** &nbsp;{chip('over', 'Revoked') if revoked else chip('ok', 'Active')}", unsafe_allow_html=True)
        mid.caption(f"Last message: {seen_label(d['last_seen_at'])}")
        if not revoked and right.button("Revoke", key=f"revoke_{d['id']}", icon=":material/block:",
                                        help="This phone's token stops working at once."):
            tools.revoke_device(d["id"])
            if st.session_state.get("new_device", {}).get("id") == d["id"]:
                del st.session_state["new_device"]
            st.rerun()

    inbox = tools.list_inbox("pending")
    st.write("")
    st.markdown(f'<p class="eyebrow">Needs review ({len(inbox)})</p>', unsafe_allow_html=True)
    if not inbox:
        empty("Nothing waiting.", "A bank-looking SMS that Rectifi's fixed rules can't read for sure waits here for you. "
                                  "OTPs and offers are thrown away, not kept.")
    for item in inbox:
        with st.container(border=True):
            st.code(item["raw_text"], language=None, wrap_lines=True)
            when = (item["sent_at"] or item["received_at"]).astimezone()
            st.caption(f"From {item['sender'] or 'unknown'} via {item['device'] or 'a removed phone'} · {when:%d %b, %H:%M}"
                       + (f" · {item['reason']}" if item["reason"] else ""))
            b1, b2, _ = st.columns([1.1, 1.5, 3])
            if b1.button("Add", key=f"inbox_add_{item['id']}", icon=":material/edit_note:", help="Open the Add page with what Rectifi could read filled in."):
                st.session_state["_prefill"] = tools.inbox_prefill(item["id"])
                st.switch_page(PAGES["add"])
            if b2.button("Dismiss", key=f"inbox_dismiss_{item['id']}", icon=":material/close:"):
                tools.dismiss_inbox_item(item["id"])
                st.rerun()

    st.write("")
    st.markdown('<p class="eyebrow">Good to know</p>', unsafe_allow_html=True)
    st.markdown(
        "- The laptop must be on and Rectifi running, or the phone can't deliver. The Android app keeps retrying for a while. "
        "Anything still missed is caught when you import a statement, and the balance check shows the gap.\n"
        "- **Campus Wi-Fi often blocks one device from reaching another** (client isolation), which is why Rectifi uses **Tailscale**: "
        "both devices connect out to Tailscale, so it works on any network, hostel Wi-Fi included. No router settings, no open ports.\n"
        "- Another way, if you cannot use Tailscale: turn on the phone's hotspot and connect the laptop to it, then set "
        "`RECTIFI_SMS_HOST=0.0.0.0` in `.env` (advanced: it opens the gateway to that network, and Windows Firewall asks the first time; "
        "allow it on **Private networks only**).\n"
        "- Offers, OTPs and chats are ignored and never stored. An SMS from a plain phone number is ignored too.\n"
        "- If you lose a phone, press Revoke. Its token stops working immediately."
    )


# ───────────────────────── navigation ─────────────────────────

PAGES = {
    "overview": st.Page(page_overview, title="Overview", icon=":material/space_dashboard:", url_path="overview", default=True),
    "review": st.Page(page_review, title="Review", icon=":material/event_available:", url_path="review"),
    "insights": st.Page(page_insights, title="Insights", icon=":material/lightbulb:", url_path="insights"),
    "ask": st.Page(page_ask, title="Ask", icon=":material/forum:", url_path="ask"),
    "add": st.Page(page_add, title="Add", icon=":material/add_circle:", url_path="add"),
    "ledger": st.Page(page_ledger, title="Ledger", icon=":material/receipt_long:", url_path="ledger"),
    "budgets": st.Page(page_budgets, title="Budgets", icon=":material/savings:", url_path="budgets"),
    "accounts": st.Page(page_accounts, title="Accounts", icon=":material/account_balance:", url_path="accounts"),
    "phone": st.Page(page_phone, title="Phone", icon=":material/smartphone:", url_path="phone"),
}

with st.sidebar:
    st.markdown("<div style='font-family:Fraunces,serif;font-size:1.6rem;font-weight:600;line-height:1'>Rectifi</div>"
                "<div style='color:var(--ink-2);font-size:.82rem;margin:.2rem 0 1rem'>every rupee, accounted for</div>",
                unsafe_allow_html=True)

try:
    _boot(config.database_url())
    st.navigation(list(PAGES.values()), position="sidebar").run()
except (DatabaseUnavailable, psycopg.OperationalError, PoolTimeout) as exc:
    db_down(exc)
