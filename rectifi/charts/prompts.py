"""Prompts for the chart writer and the repairer. The model sees a schema and five FAKE rows, never a real transaction.

Category names are the one thing taken from the database (the model needs them to filter); they are cleaned to plain words.
"""

from __future__ import annotations

import re

from langchain_core.prompts import ChatPromptTemplate

SCHEMA = """df  (pandas DataFrame, one row per transaction)
  date            datetime64  the day it happened
  month           str         'YYYY-MM'
  weekday         str         'Mon'..'Sun'
  amount_paise    int64       money in paise (1 rupee = 100). Add THIS up for any total.
  amount          float64     the same in rupees, for DRAWING ONLY (axis values). Never format it as money.
  direction       str         'debit' (money out) or 'credit' (money in)
  category        str         one of the category names below
  category_kind   str         'expense', 'income' or 'transfer'
  payee           str         who it was paid to (masked)
  account         str         account name ('' if none)
budgets  (DataFrame): month 'YYYY-MM', category str, limit_paise int64
categories (DataFrame): name, kind"""

SAMPLE_ROWS = [
    ("2026-01-03", "2026-01", "Sat", 45000, 450.0, "debit", "Food & Dining", "expense", "Sample Cafe", "Sample Bank"),
    ("2026-01-04", "2026-01", "Sun", 120000, 1200.0, "debit", "Transport", "expense", "Sample Cab", "Sample Bank"),
    ("2026-01-09", "2026-01", "Fri", 9900000, 99000.0, "credit", "Salary", "income", "Sample Employer", "Sample Bank"),
    ("2026-02-02", "2026-02", "Mon", 30050, 300.5, "debit", "Food & Dining", "expense", "Sample Diner", "Sample Bank"),
    ("2026-02-11", "2026-02", "Wed", 250000, 2500.0, "debit", "Shopping", "expense", "Sample Store", "Sample Bank"),
]
SAMPLE_COLUMNS = ["date", "month", "weekday", "amount_paise", "amount", "direction", "category", "category_kind", "payee", "account"]

RULES = """You write Python that draws ONE chart of the user's spending. Reply with ONE ```python code block and nothing else.

The code runs on the user's own data in a locked room. These names already exist: df, budgets, categories, inr, PALETTE, PERIOD.
  inr(paise) turns a whole number of paise into text such as '₹1,250.50'. It is the ONLY way to put a money figure in any text.
  PALETTE is a dict of colours: PALETTE["accent"] for data, PALETTE["muted"] for reference lines.
  PERIOD is a short text for the period, such as 'last 3 months'.

Hard rules (the code is refused if it breaks one):
1. End with a variable named fig that holds the matplotlib figure: fig, ax = plt.subplots(figsize=(10, 6)).
2. Imports only from: pandas, numpy, matplotlib (pyplot, ticker, dates), seaborn, math, datetime, calendar, collections, itertools, textwrap.
3. Never type a rupee amount or any number above 100. Work totals out from df and write them with inr(). A title may say "Food spend" but never "₹4,200".
4. Do not read or write files, do not call savefig or show, do not use eval, exec, open, getattr or names that start with an underscore.
5. Add a title, an x label and a y label. Sum money with amount_paise; plot amount / or paise / 100 on the axis.
6. Keep to at most 2 series. No pie or donut unless the user asked for one. Use bars for categories and lines for days.
7. Use only "expense" categories for spending unless asked about income: df[(df.direction == "debit") & (df.category_kind == "expense")].
8. If a bar needs its value on it, use ax.bar_label with labels=[inr(p) for p in paise_values].

Category names you can use: {categories}
Period: {period_label}.
"""

EXAMPLES = [
    ("monthly food spend as a bar chart",
     '''import matplotlib.pyplot as plt

food = df[(df.direction == "debit") & (df.category == "Food & Dining")]
paise = food.groupby("month")["amount_paise"].sum()
fig, ax = plt.subplots(figsize=(10, 6))
ax.bar(paise.index, paise.values / 100, color=PALETTE["accent"])
ax.bar_label(ax.containers[0], labels=[inr(p) for p in paise.values], padding=3)
ax.set_title("Food & Dining spend by month")
ax.set_xlabel("Month")
ax.set_ylabel("Rupees")
'''),
    ("daily spending against my budget",
     '''import matplotlib.pyplot as plt

spend = df[(df.direction == "debit") & (df.category_kind == "expense")]
running = spend.groupby("date")["amount_paise"].sum().cumsum()
limit = int(budgets.groupby("month")["limit_paise"].sum().iloc[-1])
fig, ax = plt.subplots(figsize=(10, 6))
ax.plot(running.index, running.values / 100, color=PALETTE["accent"], label="Spent so far")
ax.axhline(limit / 100, color=PALETTE["muted"], linestyle="--", label="Budget " + inr(limit))
ax.legend()
ax.set_title("Spending so far against the budget")
ax.set_xlabel("Day")
ax.set_ylabel("Rupees")
'''),
    ("top 5 payees",
     '''import matplotlib.pyplot as plt

out = df[(df.direction == "debit") & (df.category_kind == "expense")]
top = out.groupby("payee")["amount_paise"].sum().nlargest(5).sort_values()
fig, ax = plt.subplots(figsize=(10, 6))
ax.barh(top.index, top.values / 100, color=PALETTE["accent"])
ax.bar_label(ax.containers[0], labels=[inr(p) for p in top.values], padding=4)
ax.set_title("Top 5 payees")
ax.set_xlabel("Rupees")
ax.set_ylabel("Payee")
'''),
]

_UNSAFE = re.compile(r"[^\w &/,.'+()-]")


def clean_names(names, limit: int = 40) -> str:
    """Category names for the prompt: plain words only, short, at most `limit` of them."""
    out = []
    for name in list(names)[:limit]:
        text = _UNSAFE.sub("", str(name))[:30].strip()
        if text:
            out.append(text)
    return ", ".join(out) or "(none yet)"


def sample_text() -> str:
    lines = [" | ".join(SAMPLE_COLUMNS)] + [" | ".join(str(v) for v in row) for row in SAMPLE_ROWS]
    return "Five EXAMPLE rows (fake, only to show the shape):\n" + "\n".join(lines)


def system_text(categories, period_label: str) -> str:
    shots = "\n\n".join(f"Request: {q}\n```python\n{code}```" for q, code in EXAMPLES)
    return RULES.replace("{categories}", clean_names(categories)).replace("{period_label}", period_label) + "\n" + SCHEMA + "\n\n" + sample_text() + "\n\nExamples:\n\n" + shots


WRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "{system}"),
    ("human", "Request (between the lines):\n-----\n{request}\n-----\n{feedback}Write the code now."),
])

REPAIR_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "{system}"),
    ("human", "Request (between the lines):\n-----\n{request}\n-----\n\nYour last code:\n```python\n{code}\n```\n\n{problem}\n{feedback}"
              "Fix it. Reply with the whole corrected program in ONE ```python code block."),
])
