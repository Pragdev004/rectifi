"""The registry of every column that holds sealed (encrypted) text, and every column that holds a keyed hash.

One list, read by: the database trigger `enforce_sealed` (db.py builds it from here), the owner sweep, the Privacy page
test and the "no plain text in a dump" test. Add a column here and all of them see it. Standard library only.

  sealed  columns that must hold an envelope ('rx1.' or 'rs1.') once the person is encrypted
  blind   columns that must hold a keyed hash ('b1:') once the person is encrypted (dedupe keys that would leak text)
  when    (column, value): the table is sealed only for rows where that column has that value
          (shipped default merchant rules stay plain; only rules the person taught are sealed)
  enforce False when the rule depends on more than one column, so the trigger cannot check it (the API layer does)
  hidden  columns (and settings) that are readable text with ONE part replaced by a keyed hash: the name of a merchant that is not on
          the allow-list, inside an alert's `dedupe_key` / `scope_key` or an alert preference's scope list. The trigger cannot check
          them (the rest of the text is plain on purpose); `alerts._name_part` and `seal_sweep._hide_key` make them, both with `blind()`
          (a key from the person's data key, so no dump or backup can guess the name).

Amounts, dates, categories, account names, last 4 digits and caps stay plain on purpose (decision D4).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    sealed: tuple[str, ...] = ()
    blind: tuple[str, ...] = ()
    when: tuple[str, str] | None = None
    enforce: bool = True
    hidden: tuple[str, ...] = ()


SEALED: dict[str, Table] = {
    "transactions": Table(
        sealed=("payee", "description", "raw_text", "note", "category_reason", "upi_ref_sealed"),
        blind=("upi_ref", "fingerprint"),
    ),
    "merchant_rules": Table(sealed=("pattern",), blind=("pattern_blind",), when=("source", "learned")),
    "weekly_reviews": Table(sealed=("commitment",)),
    "phone_devices": Table(sealed=("name",)),
    "sms_inbox": Table(sealed=("sender", "raw_text", "reason"), blind=("text_hash",)),
    # digest_sealed: the plain digest behind text_hash, sealed, so the keyed hash can be made again if the server's blind key changes
    "sms_seen": Table(sealed=("digest_sealed",), blind=("text_hash",)),
    "import_batches": Table(sealed=("filename",)),
    "merchant_patterns": Table(sealed=("pattern", "merchant"), blind=("pattern_blind",)),
    # a cap on an allow-listed brand or a category stays plain; only a custom merchant is sealed, which depends on the target
    "alert_rules": Table(sealed=("target",), blind=("target_blind",), enforce=False),
    "alerts": Table(sealed=("title", "detail"), hidden=("dedupe_key", "scope_key")),
    "push_subscriptions": Table(sealed=("label",)),
}

# Settings (app_settings key, field) that hold such scope keys; see `hidden` above.
HIDDEN_SETTINGS: tuple[tuple[str, str], ...] = (("alerts.prefs", "muted_scopes"), ("alerts.prefs", "snoozed_scopes"))

# Owner-only AI tables (decision D8): not sealed this phase. They hold only the owner's data.
OWNER_ONLY_TABLES: tuple[str, ...] = ("classify_jobs", "charts", "conversations", "rag_documents", "rag_chunks", "trace_spans")

# Tables that belong to a library (LangGraph) and have no user_id. Owner-only by route gate. Names are matched as
# prefixes: the library's own migration tables vary.
LIBRARY_TABLES: tuple[str, ...] = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations", "store",
                                   "store_migrations", "store_vectors", "vector_migrations")

# Columns that stay plain on purpose, as the Privacy page lists them (decision D3, D4).
PLAIN_ON_PURPOSE: tuple[str, ...] = (
    "amount", "txn_date", "direction", "category", "account name", "account last 4 digits", "budget and alert caps",
    "times", "merchant_key (allow-listed brands only)",
)


def all_sealed() -> list[tuple[str, str]]:
    """Every (table, column) that can hold a sealed envelope."""
    return [(t, c) for t, spec in SEALED.items() for c in spec.sealed]


def all_blind() -> list[tuple[str, str]]:
    return [(t, c) for t, spec in SEALED.items() for c in spec.blind]


def all_hidden() -> list[str]:
    """Every place a custom merchant's name is kept only as a keyed hash inside a larger text, as 'table.column' or 'setting.field'."""
    return [f"{t}.{c}" for t, spec in SEALED.items() for c in spec.hidden] + [f"{key}.{field}" for key, field in HIDDEN_SETTINGS]
