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

-- ───────────── next phase: classifier, charts, knowledge Q&A, chat, traces (all additive, safe to run again) ─────────────

-- Why a row has its category. confidence is a PERCENT in a SMALLINT on purpose: api/wire.to_wire turns every Decimal into a money string.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_source TEXT
    CHECK (category_source IN ('user', 'learned', 'rule', 'heuristic', 'retrieval', 'llm', 'fallback'));
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_confidence SMALLINT CHECK (category_confidence BETWEEN 0 AND 100);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_reason TEXT;             -- "rule 'swiggy'", "like #812 SWIGGY BLR", "qwen2.5:3b"
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS suggested_category_id INT REFERENCES categories(id) ON DELETE SET NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS suggested_confidence SMALLINT CHECK (suggested_confidence BETWEEN 0 AND 100);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS classify_state TEXT CHECK (classify_state IN ('pending', 'done'));
CREATE INDEX IF NOT EXISTS idx_txn_classify_pending ON transactions (import_batch_id) WHERE classify_state = 'pending';

-- 'default' rules ship with Rectifi; 'learned' rules were taught by the person (a correction in the Ledger).
ALTER TABLE merchant_rules ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'default' CHECK (source IN ('default', 'learned'));
ALTER TABLE merchant_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS classify_jobs (
    id           SERIAL PRIMARY KEY,
    batch_id     INT REFERENCES import_batches(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled', 'no_model')),
    total        INT NOT NULL DEFAULT 0,
    done         INT NOT NULL DEFAULT 0,
    by_source    JSONB NOT NULL DEFAULT '{}'::jsonb,
    model        TEXT,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);

-- Charts the model drew from a request. The code is kept so a chart can be refreshed; the images are what the page shows.
CREATE TABLE IF NOT EXISTS charts (
    id            SERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    code          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('png', 'plotly')),
    image_light   BYTEA,
    image_dark    BYTEA,
    plotly_json   JSONB,
    period_start  DATE,
    period_end    DATE,
    pinned        BOOLEAN NOT NULL DEFAULT false,
    model         TEXT,
    attempts      INT NOT NULL,
    trace         JSONB NOT NULL DEFAULT '[]'::jsonb,
    checks        JSONB NOT NULL DEFAULT '{}'::jsonb,
    thread_id     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    refreshed_at  TIMESTAMPTZ
);

-- The knowledge library: documents, and their chunks with embeddings. REAL[] on purpose: no pgvector needed.
CREATE TABLE IF NOT EXISTS rag_documents (
    id          SERIAL PRIMARY KEY,
    corpus      TEXT NOT NULL CHECK (corpus IN ('guide', 'note', 'upload')),
    title       TEXT NOT NULL,
    source      TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (corpus, source)
);
CREATE TABLE IF NOT EXISTS rag_chunks (
    id           BIGSERIAL PRIMARY KEY,
    document_id  INT REFERENCES rag_documents(id) ON DELETE CASCADE,
    txn_id       BIGINT REFERENCES transactions(id) ON DELETE CASCADE,          -- corpus 'txn' rows (undo_import cascades)
    corpus       TEXT NOT NULL CHECK (corpus IN ('guide', 'note', 'upload', 'txn')),
    chunk_index  INT NOT NULL DEFAULT 0,
    content      TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash TEXT NOT NULL,
    embed_model  TEXT NOT NULL,
    embedding    REAL[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_chunk ON rag_chunks (corpus, COALESCE(document_id, 0), COALESCE(txn_id, 0), chunk_index, embed_model);

-- Chat threads. The messages themselves live in the LangGraph checkpoint tables; this is the list the sidebar shows.
CREATE TABLE IF NOT EXISTS conversations (
    thread_id    TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT 'New conversation',
    preview      TEXT,
    search_text  TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations (updated_at DESC);

-- Local traces: one row per step of a model run. Previews are masked and cut to 2 KB before they get here.
CREATE TABLE IF NOT EXISTS trace_spans (
    id             BIGSERIAL PRIMARY KEY,
    run_id         UUID NOT NULL,
    parent_run_id  UUID,
    root_run_id    UUID NOT NULL,
    thread_id      TEXT,
    name           TEXT NOT NULL,
    run_type       TEXT NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL,
    ended_at       TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'running',
    model          TEXT,
    input_tokens   INT,
    output_tokens  INT,
    input_preview  TEXT,
    output_preview TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_root ON trace_spans (root_run_id);
CREATE INDEX IF NOT EXISTS idx_spans_time ON trace_spans (started_at DESC);

-- Small switches and counters (session_epoch for "sign out everywhere", login_lock, schema markers). Nothing secret is stored here.
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ───────────── leak alerts / real-time budget alerts (additive, safe to run again) ─────────────

-- Merchant names you taught Rectifi, on top of the built-in list in rectifi/merchants.py.
CREATE TABLE IF NOT EXISTS merchant_patterns (
    id         SERIAL PRIMARY KEY,
    pattern    TEXT UNIQUE NOT NULL CHECK (length(pattern) >= 3),
    merchant   TEXT NOT NULL,
    grp        TEXT NOT NULL CHECK (grp IN ('Quick commerce', 'Food delivery', 'Shopping', 'Other')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A spending cap: "Swiggy at most 1,500 a week". scope says what target means (a merchant, a group, a category, or all spending).
CREATE TABLE IF NOT EXISTS alert_rules (
    id            SERIAL PRIMARY KEY,
    scope         TEXT NOT NULL CHECK (scope IN ('merchant', 'group', 'category', 'total')),
    target        TEXT NOT NULL,
    period        TEXT NOT NULL CHECK (period IN ('day', 'week', 'month')),
    cap           NUMERIC(12,2) NOT NULL CHECK (cap > 0),
    muted         BOOLEAN NOT NULL DEFAULT false,
    snoozed_until TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, target, period)
);

-- One row per alert. dedupe_key UNIQUE + ON CONFLICT DO NOTHING means the same alert is never stored (or pushed) twice.
CREATE TABLE IF NOT EXISTS alerts (
    id            SERIAL PRIMARY KEY,
    dedupe_key    TEXT UNIQUE NOT NULL,
    kind          TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('act', 'watch', 'info')),
    title         TEXT NOT NULL,
    detail        TEXT NOT NULL,
    push_title    TEXT NOT NULL,
    push_body     TEXT NOT NULL,
    numbers       JSONB NOT NULL,
    rule_id       INT REFERENCES alert_rules(id) ON DELETE SET NULL,
    txn_id        BIGINT REFERENCES transactions(id) ON DELETE SET NULL,
    scope_key     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at       TIMESTAMPTZ,
    push_state    TEXT NOT NULL DEFAULT 'pending' CHECK (push_state IN ('pending', 'sending', 'sent', 'held', 'skipped', 'failed')),
    push_attempts INT NOT NULL DEFAULT 0,
    pushed_at     TIMESTAMPTZ,
    next_try_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_alerts_unread ON alerts (created_at DESC) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_push   ON alerts (push_state) WHERE push_state IN ('pending', 'held', 'failed');

-- Phones that turned on alerts. endpoint is a capability URL: it is a secret, so it is never logged or returned by the API.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         SERIAL PRIMARY KEY,
    endpoint   TEXT UNIQUE NOT NULL,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    label      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ok_at TIMESTAMPTZ,
    fail_count INT NOT NULL DEFAULT 0
);
