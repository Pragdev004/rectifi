"""Shared helpers for the chart tests (not a test file): seeded data and scripted model replies."""

from datetime import date, timedelta

from langchain_core.messages import AIMessage

from rectifi import tools


def seed_spending():
    """Three months of fake spending, a salary credit and a budget, so a chart has something to draw."""
    today = date.today()
    first = today.replace(day=1)
    months = [first]
    for _ in range(2):
        months.append((months[-1] - timedelta(days=1)).replace(day=1))
    for i, month in enumerate(reversed(months)):
        for day, amount, payee, category in ((2, "1250.50", "Swiggy", "Food & Dining"), (5, "320", "Uber", "Transport"),
                                             (9, "2400.25", "Cafe Coffee", "Food & Dining"), (12, "999", "Amazon", "Shopping")):
            d = min(month.replace(day=day), today)
            tools.add_transaction(amount, "debit", d.isoformat(), payee=payee, category=category, source="manual")
        tools.add_transaction("50000", "credit", min(month.replace(day=1), today).isoformat(), payee="Employer", category="Salary & Allowance", source="manual")
    tools.set_budget("Food & Dining", "6000", first)


def reply(code: str) -> AIMessage:
    return AIMessage(content=f"Here is the chart.\n```python\n{code}```")


GOOD = '''import matplotlib.pyplot as plt

food = df[(df.direction == "debit") & (df.category == "Food & Dining")]
paise = food.groupby("month")["amount_paise"].sum()
fig, ax = plt.subplots(figsize=(10, 6))
ax.bar(paise.index, paise.values / 100, color=PALETTE["accent"])
ax.bar_label(ax.containers[0], labels=[inr(p) for p in paise.values], padding=3)
ax.set_title("Food & Dining spend by month")
ax.set_xlabel("Month")
ax.set_ylabel("Rupees")
'''
INVALID = 'import os\nfig = 1\n'
CRASHES = '''import matplotlib.pyplot as plt
fig, ax = plt.subplots()
ax.bar(df["no_such_column"], 1)
'''
MADE_UP_TITLE = '''import matplotlib.pyplot as plt

paise = df[df.direction == "debit"].groupby("month")["amount_paise"].sum()
fig, ax = plt.subplots(figsize=(10, 6))
ax.bar(paise.index, paise.values / 100, color=PALETTE["accent"])
ax.set_title("You spent ₹" + "99" + ",999 in total")
ax.set_xlabel("Month")
ax.set_ylabel("Rupees")
'''
