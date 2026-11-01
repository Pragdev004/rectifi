-- Rectifi schema. Safe to run repeatedly (everything is IF NOT EXISTS or checks first).
--
-- Two parts, split at the line "@@ PART B @@" by db.init_db():
--   Part A  creates tables and adds columns. Every table is created in its v1 shape; later columns come from the
--           ALTER lines. `user_id` is added as a plain nullable column, so an OLD database and a NEW one take the same path.
--   (between) db._migrate_tenancy() drops the old one-person unique indexes and single-column foreign keys. It runs only
--           on a database made before multi-user support, and only after the owner agrees (decision D10).
--   Part B  fills user_id (user 1 for old rows), then adds everything that makes a database multi-user: composite
--           keys, unique indexes that start with user_id, row level security, and the triggers.
-- A fresh database and an upgraded one end in the same shape, because both run Part B (tests/test_tenancy_migration.py).

CREATE OR REPLACE FUNCTION app_uid() RETURNS int LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.user_id', true), '')::int $$;

-- 'system' only inside db.connect_system(): sign-up, login, invites, the SMS device lookup, the alert ticker.
CREATE OR REPLACE FUNCTION app_scope() RETURNS text LANGUAGE sql STABLE AS
$$ SELECT COALESCE(current_setting('app.scope', true), '') $$;

-- ───────────── people ─────────────

-- Row 1 is the owner ("owner", no credentials until claimed). In laptop mode it is the only person.
CREATE TABLE IF NOT EXISTS users (
    id                  SERIAL PRIMARY KEY,
    username            TEXT,
    scrypt_salt         BYTEA,
    verifier            BYTEA,          -- sha256 of the scrypt verifier half
    dek_by_password     BYTEA,          -- the data key, wrapped by the password
    dek_by_recovery     BYTEA,          -- a second copy, wrapped by the recovery code
    recovery_salt       BYTEA,
    public_key          BYTEA,          -- X25519, lets a signed-out job seal text for this person
    private_key_wrapped BYTEA,          -- wrapped by the data key
    ingest_salt         BYTEA,          -- keyed hashes that must work while signed out
    session_epoch       INT NOT NULL DEFAULT 0,
    encrypted           BOOLEAN NOT NULL DEFAULT false,
    confirmed_at        TIMESTAMPTZ,
    sweep_state         TEXT,
    failures            JSONB NOT NULL DEFAULT '[]'::jsonb,
    locked_until        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_username ON users (lower(username));

CREATE TABLE IF NOT EXISTS invites (
    id          SERIAL PRIMARY KEY,
    code_hash   TEXT UNIQUE NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('friend', 'owner')),
    expires_at  TIMESTAMPTZ NOT NULL,
    reserved_by INT REFERENCES users(id) ON DELETE SET NULL,
    used_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id           UUID PRIMARY KEY,
    user_id      INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    dek_wrapped  BYTEA,
    pending      BOOLEAN NOT NULL DEFAULT false,
    expires_at   TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ
);

-- The fixed list of shops whose name may be stored in plain text (rectifi/merchant_allowlist.py). Public, no user data.
CREATE TABLE IF NOT EXISTS allowed_merchants (
    key   TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);

-- ───────────── the data ─────────────

CREATE TABLE IF NOT EXISTS accounts (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,                 -- "SBI Savings"
    last4       TEXT,                          -- matches "A/c XX1234" in SMS
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS categories (
    id    SERIAL PRIMARY KEY,
    name  TEXT NOT NULL,
    kind  TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'transfer'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id           BIGSERIAL PRIMARY KEY,
    account_id   INT,
    amount       NUMERIC(12,2) NOT NULL CHECK (amount > 0),     -- never FLOAT for money
    direction    TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    txn_date     DATE NOT NULL,
    payee        TEXT,                                          -- "swiggy@icici" or "SWIGGY"
    description  TEXT,                                          -- statement narration
    upi_ref      TEXT,                                          -- 12-digit UPI reference / RRN
    fingerprint  TEXT,                                          -- hash of a statement row without a ref
    category_id  INT,
    source       TEXT NOT NULL CHECK (source IN ('sms', 'statement', 'chat', 'manual')),
    raw_text     TEXT,                                          -- original SMS / row, for audit
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS budgets (
    category_id   INT  NOT NULL,
    month         DATE NOT NULL CHECK (EXTRACT(DAY FROM month) = 1),   -- first day of the month
    limit_amount  NUMERIC(12,2) NOT NULL CHECK (limit_amount > 0),
    PRIMARY KEY (category_id, month)
);

-- "The bank says my balance was X at the end of day D." Two of these make a reconciliation.
CREATE TABLE IF NOT EXISTS balance_snapshots (
    account_id  INT  NOT NULL,
    as_of       DATE NOT NULL,
    balance     NUMERIC(14,2) NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (account_id, as_of)
);

-- Learned categorisation: "anything with 'swiggy' in it is Food & Dining".
CREATE TABLE IF NOT EXISTS merchant_rules (
    id           SERIAL PRIMARY KEY,
    pattern      TEXT NOT NULL,              -- lowercase substring
    category_id  INT NOT NULL
);

-- The weekly review: one small promise per week ("no more than 3 delivery orders"). week_start is always a Monday.
-- (The primary key becomes (user_id, week_start) in Part B.)
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
    token_hash    TEXT UNIQUE NOT NULL,      -- global on purpose: the token is how the gateway finds the person
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);

-- Bank-looking SMS that Rectifi could not read with its fixed rules. They wait here for a human ("Needs review").
-- The raw text stays on this laptop. text_hash makes a re-sent SMS land here only once (the database enforces it).
CREATE TABLE IF NOT EXISTS sms_inbox (
    id           BIGSERIAL PRIMARY KEY,
    device_id    INT,
    sender       TEXT,
    raw_text     TEXT NOT NULL,
    sent_at      TIMESTAMPTZ,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'added', 'dismissed')),
    reason       TEXT,
    text_hash    TEXT NOT NULL
);

-- Hashes (never text) of SMS that were already turned into a transaction. If the phone sends the same SMS again
-- (its retry after a network drop), the second copy is recognised even when the SMS has no UPI reference.
-- (The primary key becomes (user_id, text_hash) in Part B.)
CREATE TABLE IF NOT EXISTS sms_seen (
    text_hash  TEXT PRIMARY KEY,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per statement import, so "Undo this import" can remove exactly the rows that import added.
-- snapshot_changes remembers what the import did to the balance snapshots (so undo can put them back).
CREATE TABLE IF NOT EXISTS import_batches (
    id                SERIAL PRIMARY KEY,
    account_id        INT,
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
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS import_batch_id INT;

-- ───────────── next phase: classifier, charts, knowledge Q&A, chat, traces (all additive, safe to run again) ─────────────

-- Why a row has its category. confidence is a PERCENT in a SMALLINT on purpose: api/wire.to_wire turns every Decimal into a money string.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_source TEXT
    CHECK (category_source IN ('user', 'learned', 'rule', 'heuristic', 'retrieval', 'llm', 'fallback'));
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_confidence SMALLINT CHECK (category_confidence BETWEEN 0 AND 100);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_reason TEXT;             -- "rule 'swiggy'", "like #812 SWIGGY BLR", "qwen2.5:3b"
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS suggested_category_id INT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS suggested_confidence SMALLINT CHECK (suggested_confidence BETWEEN 0 AND 100);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS classify_state TEXT CHECK (classify_state IN ('pending', 'done'));

-- Multi-user: the sealed UPI reference, a plain brand key from the allow-list (decision D3), and "arrived while signed out".
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS upi_ref_sealed TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS merchant_key TEXT REFERENCES allowed_merchants(key);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS unlocked BOOLEAN NOT NULL DEFAULT true;

-- 'default' rules ship with Rectifi; 'learned' rules were taught by the person (a correction in the Ledger).
ALTER TABLE merchant_rules ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'default' CHECK (source IN ('default', 'learned'));
ALTER TABLE merchant_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- What makes two rules "the same pattern". Equal to pattern in laptop mode; a keyed hash when the pattern is sealed.
-- A trigger fills it from pattern when an INSERT leaves it out (Part B).
ALTER TABLE merchant_rules ADD COLUMN IF NOT EXISTS pattern_blind TEXT;

CREATE TABLE IF NOT EXISTS classify_jobs (
    id           SERIAL PRIMARY KEY,
    batch_id     INT,
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
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rag_chunks (
    id           BIGSERIAL PRIMARY KEY,
    document_id  INT,
    txn_id       BIGINT,                                                         -- corpus 'txn' rows (undo_import cascades)
    corpus       TEXT NOT NULL CHECK (corpus IN ('guide', 'note', 'upload', 'txn')),
    chunk_index  INT NOT NULL DEFAULT 0,
    content      TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash TEXT NOT NULL,
    embed_model  TEXT NOT NULL,
    embedding    REAL[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
-- (The primary key becomes (user_id, key) in Part B: every person has their own.)
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ───────────── leak alerts / real-time budget alerts (additive, safe to run again) ─────────────

-- Merchant names you taught Rectifi, on top of the built-in list in rectifi/merchants.py.
CREATE TABLE IF NOT EXISTS merchant_patterns (
    id         SERIAL PRIMARY KEY,
    pattern    TEXT NOT NULL CHECK (length(pattern) >= 3),
    merchant   TEXT NOT NULL,
    grp        TEXT NOT NULL CHECK (grp IN ('Quick commerce', 'Food delivery', 'Shopping', 'Other')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE merchant_patterns ADD COLUMN IF NOT EXISTS pattern_blind TEXT;

-- A spending cap: "Swiggy at most 1,500 a week". scope says what target means (a merchant, a group, a category, or all spending).
CREATE TABLE IF NOT EXISTS alert_rules (
    id            SERIAL PRIMARY KEY,
    scope         TEXT NOT NULL CHECK (scope IN ('merchant', 'group', 'category', 'total')),
    target        TEXT NOT NULL,
    period        TEXT NOT NULL CHECK (period IN ('day', 'week', 'month')),
    cap           NUMERIC(12,2) NOT NULL CHECK (cap > 0),
    muted         BOOLEAN NOT NULL DEFAULT false,
    snoozed_until TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS target_blind TEXT;     -- target, or a keyed hash of it for a custom merchant

-- One row per alert. (user_id, dedupe_key) UNIQUE + ON CONFLICT DO NOTHING means the same alert is never stored (or pushed) twice.
CREATE TABLE IF NOT EXISTS alerts (
    id            SERIAL PRIMARY KEY,
    dedupe_key    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('act', 'watch', 'info')),
    title         TEXT NOT NULL,
    detail        TEXT NOT NULL,
    push_title    TEXT NOT NULL,
    push_body     TEXT NOT NULL,
    numbers       JSONB NOT NULL,
    rule_id       INT,
    txn_id        BIGINT,
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
    endpoint   TEXT NOT NULL,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    label      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ok_at TIMESTAMPTZ,
    fail_count INT NOT NULL DEFAULT 0
);

-- ───────────── multi-user: whose row is it? ─────────────

-- user_id on every table that holds a person's data. Plain and nullable here; Part B fills it and locks it down.
-- (Checks first, so a second run takes no table locks.)
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['accounts', 'categories', 'transactions', 'budgets', 'balance_snapshots', 'merchant_rules',
        'weekly_reviews', 'phone_devices', 'sms_inbox', 'sms_seen', 'import_batches', 'classify_jobs', 'charts',
        'rag_documents', 'rag_chunks', 'conversations', 'trace_spans', 'app_settings', 'merchant_patterns', 'alert_rules',
        'alerts', 'push_subscriptions']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = format('public.%I', t)::regclass AND attname = 'user_id' AND NOT attisdropped) THEN
            EXECUTE format('ALTER TABLE %I ADD COLUMN user_id INT', t);
        END IF;
    END LOOP;
END
$$;

-- Row 1 is the owner. It must exist before any old row can be given to it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users WHERE id = 1) THEN
        INSERT INTO users (id, username) VALUES (1, 'owner');
        PERFORM setval('users_id_seq', GREATEST(1, (SELECT max(id) FROM users)));
    END IF;
END
$$;

-- @@ PART B @@

-- Give every old row to user 1, then make user_id required, default to the current person, and point at users(id).
-- (Checks first, so a second run takes no locks and changes nothing.)
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['accounts', 'categories', 'transactions', 'budgets', 'balance_snapshots', 'merchant_rules',
        'weekly_reviews', 'phone_devices', 'sms_inbox', 'sms_seen', 'import_batches', 'classify_jobs', 'charts',
        'rag_documents', 'rag_chunks', 'conversations', 'trace_spans', 'app_settings', 'merchant_patterns', 'alert_rules',
        'alerts', 'push_subscriptions']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = format('public.%I', t)::regclass AND attname = 'user_id' AND NOT attnotnull) THEN
            EXECUTE format('UPDATE %I SET user_id = 1 WHERE user_id IS NULL', t);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN user_id SET DEFAULT app_uid(), ALTER COLUMN user_id SET NOT NULL', t);
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
                           t, t || '_user_id_fkey');
        END IF;
    END LOOP;
END
$$;

-- "The same pattern" is decided by the blind column. Old rows: it equals the plain column.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM (VALUES ('merchant_rules', 'pattern_blind', 'pattern'), ('merchant_patterns', 'pattern_blind', 'pattern'),
                                   ('alert_rules', 'target_blind', 'target')) AS v(tbl, blind, src)
    LOOP
        IF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = format('public.%I', r.tbl)::regclass AND attname = r.blind AND NOT attnotnull) THEN
            EXECUTE format('UPDATE %I SET %I = %I WHERE %I IS NULL', r.tbl, r.blind, r.src, r.blind);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN %I SET NOT NULL', r.tbl, r.blind);
        END IF;
    END LOOP;
END
$$;

-- Primary keys that become per-person.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM (VALUES ('sms_seen', 'user_id, text_hash'), ('weekly_reviews', 'user_id, week_start'),
                                   ('app_settings', 'user_id, key')) AS v(tbl, cols)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = format('public.%I', r.tbl)::regclass AND contype = 'p'
                       AND pg_get_constraintdef(oid) LIKE 'PRIMARY KEY (user_id,%') THEN
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', r.tbl, r.tbl || '_pkey');
            EXECUTE format('ALTER TABLE %I ADD PRIMARY KEY (%s)', r.tbl, r.cols);
        END IF;
    END LOOP;
END
$$;

-- Unique keys. Each starts with user_id, so two people can have an account called "SBI" and the same UPI reference.
-- The (user_id, id) keys are what the composite foreign keys below point at.
CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_id         ON accounts (user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_name       ON accounts (user_id, name);
CREATE UNIQUE INDEX IF NOT EXISTS uq_categories_id       ON categories (user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_categories_name     ON categories (user_id, name);
CREATE UNIQUE INDEX IF NOT EXISTS uq_txn_id              ON transactions (user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_import_batches_id   ON import_batches (user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_phone_devices_id    ON phone_devices (user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_rules_id      ON alert_rules (user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_documents_id    ON rag_documents (user_id, id);

-- Dedupe lives in the database, not in Python: the same UPI payment can never be stored twice,
-- even if the app has a bug. (direction is included because a reversal can reuse the reference.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_txn_upi_ref         ON transactions (user_id, upi_ref, direction) WHERE upi_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_txn_fingerprint     ON transactions (user_id, fingerprint)        WHERE fingerprint IS NOT NULL;
CREATE INDEX        IF NOT EXISTS idx_txn_date           ON transactions (txn_date);
CREATE INDEX        IF NOT EXISTS idx_txn_account        ON transactions (account_id, txn_date);
CREATE INDEX        IF NOT EXISTS idx_txn_batch          ON transactions (import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE INDEX        IF NOT EXISTS idx_txn_classify_pending ON transactions (import_batch_id) WHERE classify_state = 'pending';

CREATE UNIQUE INDEX IF NOT EXISTS uq_merchant_rules_pattern    ON merchant_rules (user_id, pattern_blind);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_inbox_hash            ON sms_inbox (user_id, text_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_documents_source      ON rag_documents (user_id, corpus, source);
CREATE UNIQUE INDEX IF NOT EXISTS uq_chunk                     ON rag_chunks (user_id, corpus, COALESCE(document_id, 0), COALESCE(txn_id, 0), chunk_index, embed_model);
CREATE UNIQUE INDEX IF NOT EXISTS uq_merchant_patterns_pattern ON merchant_patterns (user_id, pattern_blind);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_rules_target        ON alert_rules (user_id, scope, target_blind, period);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_dedupe             ON alerts (user_id, dedupe_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_push_endpoint             ON push_subscriptions (user_id, endpoint);

-- Composite foreign keys: a row can only point at a parent that belongs to the SAME person, and a foreign key error
-- cannot reveal that another person's id exists. "ON DELETE SET NULL (col)" nulls only that column, never user_id.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('transactions',      'fk_txn_account',      'FOREIGN KEY (user_id, account_id) REFERENCES accounts (user_id, id) ON DELETE SET NULL (account_id)'),
        ('transactions',      'fk_txn_category',     'FOREIGN KEY (user_id, category_id) REFERENCES categories (user_id, id)'),
        ('transactions',      'fk_txn_suggested',    'FOREIGN KEY (user_id, suggested_category_id) REFERENCES categories (user_id, id) ON DELETE SET NULL (suggested_category_id)'),
        ('transactions',      'fk_txn_batch',        'FOREIGN KEY (user_id, import_batch_id) REFERENCES import_batches (user_id, id) ON DELETE SET NULL (import_batch_id)'),
        ('budgets',           'fk_budgets_category', 'FOREIGN KEY (user_id, category_id) REFERENCES categories (user_id, id) ON DELETE CASCADE'),
        ('balance_snapshots', 'fk_snap_account',     'FOREIGN KEY (user_id, account_id) REFERENCES accounts (user_id, id) ON DELETE CASCADE'),
        ('merchant_rules',    'fk_rules_category',   'FOREIGN KEY (user_id, category_id) REFERENCES categories (user_id, id) ON DELETE CASCADE'),
        ('sms_inbox',         'fk_inbox_device',     'FOREIGN KEY (user_id, device_id) REFERENCES phone_devices (user_id, id) ON DELETE SET NULL (device_id)'),
        ('import_batches',    'fk_batch_account',    'FOREIGN KEY (user_id, account_id) REFERENCES accounts (user_id, id) ON DELETE SET NULL (account_id)'),
        ('classify_jobs',     'fk_jobs_batch',       'FOREIGN KEY (user_id, batch_id) REFERENCES import_batches (user_id, id) ON DELETE CASCADE'),
        ('rag_chunks',        'fk_chunks_document',  'FOREIGN KEY (user_id, document_id) REFERENCES rag_documents (user_id, id) ON DELETE CASCADE'),
        ('rag_chunks',        'fk_chunks_txn',       'FOREIGN KEY (user_id, txn_id) REFERENCES transactions (user_id, id) ON DELETE CASCADE'),
        ('alerts',            'fk_alerts_rule',      'FOREIGN KEY (user_id, rule_id) REFERENCES alert_rules (user_id, id) ON DELETE SET NULL (rule_id)'),
        ('alerts',            'fk_alerts_txn',       'FOREIGN KEY (user_id, txn_id) REFERENCES transactions (user_id, id) ON DELETE SET NULL (txn_id)')
    ) AS v(tbl, cname, ddl)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = r.cname AND conrelid = format('public.%I', r.tbl)::regclass) THEN
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s', r.tbl, r.cname, r.ddl);
        END IF;
    END LOOP;
END
$$;

-- Fills pattern_blind / target_blind from the plain column when an INSERT leaves it out (laptop mode, seeds, old code).
-- It runs before enforce_sealed (triggers fire in name order), so a sealed person who forgets blind() is refused there.
CREATE OR REPLACE FUNCTION fill_blind() RETURNS trigger LANGUAGE plpgsql AS
$$
DECLARE j jsonb := to_jsonb(NEW);
BEGIN
    IF (j ->> TG_ARGV[0]) IS NULL AND (j ->> TG_ARGV[1]) IS NOT NULL THEN
        NEW := jsonb_populate_record(NEW, jsonb_build_object(TG_ARGV[0], j ->> TG_ARGV[1]));
    END IF;
    RETURN NEW;
END
$$;

-- The database is the real guard for sealing (like dedupe, rule 3): once a person is encrypted, a sealed column that
-- gets plain text, or a blind column that gets something that is not a keyed hash, is an error, not a leak.
-- Args: sealed columns, blind columns, then an optional column and value that say which rows are sealed.
-- On UPDATE only columns that CHANGED are checked, so a row the owner sweep has not reached yet can still be touched.
CREATE OR REPLACE FUNCTION enforce_sealed() RETURNS trigger LANGUAGE plpgsql AS
$$
DECLARE
    newj jsonb;
    oldj jsonb;
    col  text;
    val  text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id AND encrypted) THEN
        RETURN NEW;
    END IF;
    newj := to_jsonb(NEW);
    IF NULLIF(TG_ARGV[2], '') IS NOT NULL AND (newj ->> TG_ARGV[2]) IS DISTINCT FROM TG_ARGV[3] THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        oldj := to_jsonb(OLD);
    END IF;
    FOREACH col IN ARRAY COALESCE(string_to_array(NULLIF(TG_ARGV[0], ''), ','), ARRAY[]::text[]) LOOP
        val := newj ->> col;
        IF val IS NULL OR val = '' OR (oldj IS NOT NULL AND (oldj ->> col) IS NOT DISTINCT FROM val) THEN
            CONTINUE;
        END IF;
        IF val NOT LIKE 'rx1.%' AND val NOT LIKE 'rs1.%' THEN
            RAISE EXCEPTION 'Plain text in %.%: this person''s text must be sealed before it is stored.', TG_TABLE_NAME, col
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    FOREACH col IN ARRAY COALESCE(string_to_array(NULLIF(TG_ARGV[1], ''), ','), ARRAY[]::text[]) LOOP
        val := newj ->> col;
        IF val IS NULL OR (oldj IS NOT NULL AND (oldj ->> col) IS NOT DISTINCT FROM val) THEN
            CONTINUE;
        END IF;
        IF val NOT LIKE 'b1:%' THEN
            RAISE EXCEPTION 'Unhashed value in %.%: this person''s dedupe keys must be keyed hashes.', TG_TABLE_NAME, col
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END
$$;

DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM (VALUES ('merchant_rules', 'pattern_blind', 'pattern'), ('merchant_patterns', 'pattern_blind', 'pattern'),
                                   ('alert_rules', 'target_blind', 'target')) AS v(tbl, blind, src)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = format('public.%I', r.tbl)::regclass AND tgname = 'a_fill_blind') THEN
            EXECUTE format('CREATE TRIGGER a_fill_blind BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION fill_blind(%L, %L)',
                           r.tbl, r.blind, r.src);
        END IF;
    END LOOP;
END
$$;
-- (The b_enforce_sealed triggers are made by db.init_db() from rectifi/sealed_fields.py, so that list lives in one place.)

-- Row level security: a person sees and writes only their own rows. FORCE makes it apply to the table owner too (the role
-- the app logs in as). With no person set, every query sees zero rows and every INSERT fails. This is a seatbelt against
-- a forgotten WHERE; it does not stop someone who can run SQL as a superuser (see db.assert_app_role).
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['accounts', 'categories', 'transactions', 'budgets', 'balance_snapshots', 'merchant_rules',
        'weekly_reviews', 'phone_devices', 'sms_inbox', 'sms_seen', 'import_batches', 'classify_jobs', 'charts',
        'rag_documents', 'rag_chunks', 'conversations', 'trace_spans', 'app_settings', 'merchant_patterns', 'alert_rules',
        'alerts', 'push_subscriptions', 'users', 'invites', 'user_sessions', 'allowed_merchants']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = format('public.%I', t)::regclass AND relrowsecurity AND relforcerowsecurity) THEN
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        END IF;
    END LOOP;
END
$$;

DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT u.t AS tbl, 'tenant_isolation' AS pname,
                    'USING (user_id = (SELECT app_uid())) WITH CHECK (user_id = (SELECT app_uid()))' AS body
             FROM unnest(ARRAY['accounts', 'categories', 'transactions', 'budgets', 'balance_snapshots', 'merchant_rules',
                  'weekly_reviews', 'phone_devices', 'sms_inbox', 'sms_seen', 'import_batches', 'classify_jobs', 'charts',
                  'rag_documents', 'rag_chunks', 'conversations', 'trace_spans', 'app_settings', 'merchant_patterns',
                  'alert_rules', 'alerts', 'push_subscriptions']) AS u(t)
             UNION ALL SELECT * FROM (VALUES
                -- the token is how the gateway finds the person, so a system connection may READ devices to find the owner of a token
                ('phone_devices', 'device_lookup', 'FOR SELECT USING (app_scope() = ''system'')'),
                -- a person reads their own row; only a system connection writes people, invites and sessions
                ('users', 'own_row', 'FOR SELECT USING (app_scope() = ''system'' OR id = (SELECT app_uid()))'),
                ('users', 'system_writes', 'USING (app_scope() = ''system'') WITH CHECK (app_scope() = ''system'')'),
                ('invites', 'system_only', 'USING (app_scope() = ''system'') WITH CHECK (app_scope() = ''system'')'),
                ('user_sessions', 'system_only', 'USING (app_scope() = ''system'') WITH CHECK (app_scope() = ''system'')'),
                -- the allow-list is public to read, and changed only by init_db
                ('allowed_merchants', 'public_read', 'FOR SELECT USING (true)'),
                ('allowed_merchants', 'system_writes', 'USING (app_scope() = ''system'') WITH CHECK (app_scope() = ''system'')')
             ) AS v(tbl, pname, body)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = r.tbl AND policyname = r.pname) THEN
            EXECUTE format('CREATE POLICY %I ON %I %s', r.pname, r.tbl, r.body);
        END IF;
    END LOOP;
END
$$;
