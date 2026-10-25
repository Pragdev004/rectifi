"""Build a database the way Rectifi's previous version left it (the schema and seeds from before the AI phase)."""

from __future__ import annotations

from pathlib import Path

import psycopg

from rectifi import db

OLD_SCHEMA = Path(__file__).resolve().parent.parent / "fixtures" / "schema_before_ai_phase.sql"

OLD_CATEGORIES = [
    ("Food & Dining", "expense"), ("Groceries", "expense"), ("Transport", "expense"), ("Shopping", "expense"),
    ("Bills & Utilities", "expense"), ("Rent", "expense"), ("Health", "expense"), ("Education", "expense"),
    ("Entertainment", "expense"), ("Subscriptions", "expense"), ("Travel", "expense"), ("Other", "expense"),
    ("Salary & Allowance", "income"), ("Refunds", "income"), ("Other income", "income"), ("Transfers", "transfer"),
]
OLD_RULES = {"Transport": ["uber", "petrol", "fuel"], "Food & Dining": ["swiggy"], "Groceries": ["blinkit"]}


def rebuild_old_database() -> None:
    """Drop every table and recreate the old schema with the old default categories and rules. Pool is closed afterwards."""
    from rectifi import config

    db.close_pool()
    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
        if tables:
            conn.execute("DROP TABLE IF EXISTS " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
        conn.execute(OLD_SCHEMA.read_text(encoding="utf-8"))
        for name, kind in OLD_CATEGORIES:
            conn.execute("INSERT INTO categories (name, kind) VALUES (%s, %s)", (name, kind))
        for category, patterns in OLD_RULES.items():
            for pattern in patterns:
                conn.execute("INSERT INTO merchant_rules (pattern, category_id) SELECT %s, id FROM categories WHERE name = %s",
                             (pattern, category))
