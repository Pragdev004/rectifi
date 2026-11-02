# WS-CORE handoff (ws/core)

Built from `docs/MULTIUSER_PLAN.md` section 6. Tests: `tests/test_core_sealed.py` (21) plus the existing suites for the modules touched.

## What changed

- **`tools.py`** (outside the WS-AI block): `add_transaction` seals payee, description, raw_text, note, category_reason and the plain
  UPI reference (`upi_ref_sealed`); `upi_ref` is `vault.blind_ingest(...)`, `fingerprint` is `vault.blind(...)`; `merchant_key` is
  `merchant_allowlist.plain_key(payee, description)`; `unlocked = false` when multi-user and no data key (an SMS that arrived signed out).
  `set_category` / `update_note` / `save_commitment` / `create_device` / `_to_inbox` seal. A learned `merchant_rules` row gets
  `pattern = seal(...)`, `pattern_blind = blind(...)`. `TXN_SELECT.upi_ref` is `COALESCE(upi_ref_sealed, plain upi_ref)`. Ledger search
  is Python over the newest `SEARCH_WINDOW` (20,000) rows. `authenticate_device` runs on `connect_system` and returns
  `{"id", "name", "uid", "ctx"}`; `ctx` is the owner's `UserContext` with no data key (strict False) plus public key and ingest salt.
  `ingest_sms` refuses a device id that is not the bound person's. `sms_seen` / `sms_inbox.text_hash` = `blind_ingest(sha256 digest)`.
- **`importer.py`**: same sealing, `fingerprint = blind(sha)`, `upi_ref = blind_ingest(ref)`, `merchant_key`, merge step also sets
  `upi_ref_sealed` and fills `merchant_key` when empty. A friend's rows never get `classify_state = 'pending'` (no model job, D7).
- **`categorize.py`**: rules sorted in Python (learned first, longest first); a rule whose pattern reads `None` (sealed, no key) is skipped;
  `_index_cache` is keyed `(uid, stamp)`, one entry per person.
- **`sms_gateway.py`**: binds `device["ctx"]` around `tools.ingest_sms` and around the alerts sweep. In multi-user mode the start-up
  `alerts.ensure_watermark()` is skipped (it would raise `NoUser`); laptop mode unchanged.
- **`api/uploads.py`**: `pending` and `recent` carry the person. Another person's upload id answers **404** `not_found`; an unknown or
  expired id still answers 409 `upload_expired` (existing tests). `PENDING_MAX` is per person. `recent` is keyed `(uid, digest)`.
- **`api/routes/imports.py`**: `start_sorting` returns at once unless the person is the owner (no `agent/jobs`, no `txn` reindex).
- **`hooks.py` WS-CORE block**: opens `rs1` in `sms_inbox`, re-seals as `rx1`; for `transactions WHERE NOT unlocked` re-seals every sealed
  column, re-runs layers 1 to 4 with a full context (learned rules and look-alike index) unless `category_source = 'user'`, sets
  `unlocked = true`. Idempotent. Does nothing when `ctx.key is None`.

## For WS-ACCOUNT (the sweep must match these keys)

- `upi_ref` blind = `vault.blind_ingest(plain_ref)` (the stripped reference, as `parsing` returns it).
- `fingerprint` blind = `vault.blind(plain_fingerprint)` where the plain fingerprint is `importer._fingerprint(...)` (sha256 hex, first 32).
- `upi_ref_sealed` = `vault.seal(plain_ref)`. `merchant_key` for old rows = `merchant_allowlist.plain_key(payee, description)`.
- `sms_seen.text_hash` / `sms_inbox.text_hash` = `blind_ingest(sha256(f"{sender}|{stamp or 'day:YYYY-MM-DD'}|{text}"))`.
- `merchant_rules.pattern_blind` = `vault.blind(pattern)`.

## For WS-AUTH and the integrator

- Call `hooks.after_sign_in(ctx)` with the context that has `key` AND `private_key` (the row factory needs the private key for `rs1`).
- `routes/imports.py`, `uploads.py` and `categorize.py` use `userctx.current()`; the request must be bound by `SessionLock`. An upload id
  from one person used by another is 404 only once SessionLock binds real people (the unit test binds with `as_user`).
- `sms_gateway.create_app()` lifespan: in multi-user mode WS-ALERTS should create each person's watermark lazily (first sweep) or in
  `alerts.tick`; the gateway no longer calls `ensure_watermark` there.
- `_sweep_as(device)` calls `alerts.sweep_and_deliver("sms")` with the device owner bound. If WS-ALERTS renames or re-scopes that call,
  change only that function.

## Requests for files I do not own

- `rectifi/api/wire.py`: none needed. `txn()` already leaves `upi_ref` and `raw_text` off the wire.
- `rectifi/agent/tools.py` calls `tools.add_transaction(... upi_ref=..., raw_text=...)`: works unchanged (sealing happens inside).
- WS-AI block in `tools.py` (`apply_classifications` writes `category_reason`): the owner is not encrypted until the sweep, so it works
  today; after the sweep it must pass `vault.seal(reason)` or `enforce_sealed` will refuse. Not edited here.

## Decisions

1. **Other person's upload id is 404, unknown id stays 409.** The plan says 404 for cross-person use; existing tests need 409 for an
   expired id. The ids are 128 random bits, so the difference tells nobody anything.
2. **`unlocked` is false only when multi-user mode and no data key.** Laptop mode never sets it (nothing to unlock).
3. **Hashes are taken over the old digest, not the raw text** (`blind(sha256(...))`), so laptop mode stores exactly what it did before.
4. **The sign-in hook reads `rs1` through the row factory** (needs `ctx.private_key`), then re-seals; it checks the inbox with
   `left(col, 4) = 'rs1.'` (an envelope prefix, not content).
5. **Ledger search is Python in laptop mode too** (one code path). Difference: a `%` or `_` in the search text is now literal.
6. **`add_transaction(fingerprint=...)` blinds its argument**, so callers pass the plain fingerprint.
7. A friend's rows never get `classify_state = 'pending'`, so they are not picked up if the owner's job later runs.

## Not verified

- Real SessionLock binding (WS-AUTH is parallel): all tests bind with `tests/support/tenants.as_user` or the device context.
- `hooks.after_sign_in` ordering with the other blocks; only the CORE block is exercised.
- 20,000-row search speed with real AES-GCM (only a window of 2 rows is tested for the cut-off).
