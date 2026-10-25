"""Fill a DEMO database with a believable five weeks of fake money, so every screen has something to show.

    python scripts/dev.py demo            # the easy way: uses the rectifi_demo database on port 8502

Safety rules (the fake rows would pollute your real ledger):
  * It refuses to run unless the database name ends in "_demo". Use --force only if you really mean it.
  * Running it twice does nothing the second time. It never doubles the data.
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from rectifi import config, tools  # noqa: E402
from rectifi.db import describe_target, init_db  # noqa: E402

ACCOUNT = "Demo Savings"
HOW_MANY_DAYS = 35


def database_name(url: str) -> str:
    return psycopg.conninfo.conninfo_to_dict(url).get("dbname", "")


def already_seeded() -> bool:
    return any(a["name"] == ACCOUNT for a in tools.list_accounts())


def seed(today: date | None = None) -> None:
    today = today or tools.today()
    random.seed(7)
    first_day = today - timedelta(days=HOW_MANY_DAYS - 1)
    this_month = today.replace(day=1)

    tools.add_account(ACCOUNT, "4417")
    opening = 6200
    tools.record_balance(ACCOUNT, first_day - timedelta(days=1), str(opening))
    moved = 0

    # a stipend on the 1st of each month that falls inside the window
    day = first_day
    while day <= today:
        if day.day == 1:
            tools.add_transaction("9000", "credit", day, payee="IITM stipend", category="Salary & Allowance",
                                  account=ACCOUNT, source="manual")
            moved += 9000
        day += timedelta(days=1)

    spends = [
        ("swiggy@icici", 180, 420), ("zomato@hdfcbank", 150, 380), ("blinkit@ybl", 90, 650),
        ("uber@axisbank", 70, 310), ("rapido@ybl", 40, 120), ("amazon@apl", 300, 1400),
        ("Chai point", 20, 60), ("Campus canteen", 30, 140), ("Corner shop", 40, 250),   # no rule: stays in "Miscellaneous"
    ]
    day = first_day
    while day <= today:
        for _ in range(random.randint(0, 2)):
            payee, low, high = random.choice(spends)
            amount = random.randint(low, high)
            tools.add_transaction(str(amount), "debit", day, payee=payee, account=ACCOUNT, source="chat")
            moved -= amount
        day += timedelta(days=1)

    for offset, payee, amount in [(4, "netflix@hdfcbank", 199), (6, "airtel@icici", 649)]:
        when = min(first_day + timedelta(days=offset), today)
        tools.add_transaction(str(amount), "debit", when, payee=payee, account=ACCOUNT, source="sms")
        moved -= amount

    for category, limit in [("Food & Dining", "3500"), ("Groceries", "2000"), ("Transport", "1500"), ("Shopping", "2500")]:
        tools.set_budget(category, limit, this_month)

    # closing balance is 120 lower than the arithmetic says, so the Accounts page shows a gap to hunt down
    tools.record_balance(ACCOUNT, today, str(opening + moved - 120))


def main(argv: list[str]) -> int:
    force = "--force" in argv
    url = config.database_url()
    name = database_name(url)
    if not name.endswith("_demo") and not force:
        print(f"Refusing to add fake data to the database {name!r} at {describe_target(url)}.\n"
              "Demo data only goes into a database whose name ends in _demo.\n"
              "Use `python scripts/dev.py demo`, or pass --force if you are sure.", file=sys.stderr)
        return 2
    init_db()
    if already_seeded():
        print(f"{describe_target(url)} already has the demo data. Nothing to do.")
        return 0
    seed()
    print(f"Demo data added to {describe_target(url)}. The Accounts page will show a 120 rupee gap to find.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
