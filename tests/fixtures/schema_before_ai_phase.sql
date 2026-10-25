-- Rectifi schema. Safe to run repeatedly (everything is IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS accounts (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,          -- "SBI Savings"
    last4       TEXT,                          -- matches "A/c XX1234" in SMS
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS categories (
    id    SERIAL PRIMARY KEY,
    name  TEXT UNIQUE NOT NULL,
    kind  TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'transfer'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id           BIGSERIAL PRIMARY KEY,
    account_id   INT REFERENCES accounts(id) ON DELETE SET NULL,
    amount       NUMERIC(12,2) NOT NULL CHECK (amount > 0),     -- never FLOAT for money
    direction    TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    txn_date     DATE NOT NULL,
    payee        TEXT,                                          -- "swiggy@icici" or "SWIGGY"
    description  TEXT,                                          -- statement narration
    upi_ref      TEXT,                                          -- 12-digit UPI reference / RRN
    fingerprint  TEXT,                                          -- hash of a statement row without a ref
    category_id  INT REFERENCES categories(id),
    source       TEXT NOT NULL CHECK (source IN ('sms', 'statement', 'chat', 'manual')),
    raw_text     TEXT,                                          -- original SMS / row, for audit
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Dedupe lives in the database, not in Python: the same UPI payment can never be stored twice,
-- even if the app has a bug. (direction is included because a reversal can reuse the reference.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_txn_upi_ref     ON transactions (upi_ref, direction) WHERE upi_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_txn_fingerprint ON transactions (fingerprint)        WHERE fingerprint IS NOT NULL;
CREATE INDEX        IF NOT EXISTS idx_txn_date       ON transactions (txn_date);
CREATE INDEX        IF NOT EXISTS idx_txn_account    ON transactions (account_id, txn_date);

CREATE TABLE IF NOT EXISTS budgets (
    category_id   INT  NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    month         DATE NOT NULL CHECK (EXTRACT(DAY FROM month) = 1),   -- first day of the month
    limit_amount  NUMERIC(12,2) NOT NULL CHECK (limit_amount > 0),
    PRIMARY KEY (category_id, month)
);

-- "The bank says my balance was X at the end of day D." Two of these make a reconciliation.
CREATE TABLE IF NOT EXISTS balance_snapshots (
    account_id  INT  NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    as_of       DATE NOT NULL,
    balance     NUMERIC(14,2) NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (account_id, as_of)
);

-- Learned categorisation: "anything with 'swiggy' in it is Food & Dining".
CREATE TABLE IF NOT EXISTS merchant_rules (
    id           SERIAL PRIMARY KEY,
    pattern      TEXT UNIQUE NOT NULL,       -- lowercase substring
    category_id  INT NOT NULL REFERENCES categories(id) ON DELETE CASCADE
);

-- The weekly review: one small promise per week ("no more than 3 delivery orders"). week_start is always a Monday.
CREATE TABLE IF NOT EXISTS weekly_reviews (
    week_start  DATE PRIMARY KEY CHECK (EXTRACT(ISODOW FROM week_start) = 1),
    commitment  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phones that forward bank SMS to this laptop (the "Phone" page). Only a SHA-256 hash of the token is stored:
-- the token itself is shown once, when the phone is paired, and can never be read back.
CREATE TABLE IF NOT EXISTS phone_devices (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT UNIQUE NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);

-- Bank-looking SMS that Rectifi could not read with its fixed rules. They wait here for a human ("Needs review").
-- The raw text stays on this laptop. text_hash makes a re-sent SMS land here only once (the database enforces it).
CREATE TABLE IF NOT EXISTS sms_inbox (
    id           BIGSERIAL PRIMARY KEY,
    device_id    INT REFERENCES phone_devices(id) ON DELETE SET NULL,
    sender       TEXT,
    raw_text     TEXT NOT NULL,
    sent_at      TIMESTAMPTZ,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'added', 'dismissed')),
    reason       TEXT,
    text_hash    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_inbox_hash ON sms_inbox (text_hash);

-- Hashes (never text) of SMS that were already turned into a transaction. If the phone sends the same SMS again
-- (its retry after a network drop), the second copy is recognised even when the SMS has no UPI reference.
CREATE TABLE IF NOT EXISTS sms_seen (
    text_hash  TEXT PRIMARY KEY,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per statement import, so "Undo this import" can remove exactly the rows that import added.
-- snapshot_changes remembers what the import did to the balance snapshots (so undo can put them back).
CREATE TABLE IF NOT EXISTS import_batches (
    id                SERIAL PRIMARY KEY,
    account_id        INT REFERENCES accounts(id) ON DELETE SET NULL,
    filename          TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    added             INT NOT NULL DEFAULT 0,
    merged            INT NOT NULL DEFAULT 0,
    duplicates        INT NOT NULL DEFAULT 0,
    needs_review      INT NOT NULL DEFAULT 0,
    snapshot_changes  JSONB NOT NULL DEFAULT '[]'::jsonb,
    undone_at         TIMESTAMPTZ
);

-- Which import added this row. NULL for hand entries, SMS and anything imported before this column existed.
-- Rows that an import only merged into (or found already there) keep their old value, so undo never touches them.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS import_batch_id INT REFERENCES import_batches(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_txn_batch ON transactions (import_batch_id) WHERE import_batch_id IS NOT NULL;
