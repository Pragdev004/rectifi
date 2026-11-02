# WS-AI handoff (branch ws/ai)

Built from `docs/MULTIUSER_PLAN.md` section 6 (AI, owner-only: D7 and D8). Tests: `tests/test_ai_mu.py` (about 60 cases, most of them the
parametrised "friend gets 403" walk over every AI route). Laptop mode behaves as before.

## What changed

- **Threads.** `api/ask.py`, `api/routes/charts.py`, `api/routes/knowledge.py` and `agent/jobs.py` start threads with
  `userctx.spawn`. `KNOWN_BARE_THREADS` in `tests/test_userctx.py` is now empty (the only edit there).
- **`agent/jobs.py`.** The queue still holds job ids (the existing test reads them). `_owners` maps each id to the `UserContext` it was
  queued under, and the single `rectifi-bg` worker binds that person around `run_job`. A job with no recorded person runs as the laptop
  user in laptop mode and fails closed (`NoUser`, swallowed by the loop) in multi-user mode.
  `requeue_unfinished()` returns 0 and keeps its once-per-process turn when multi-user mode has nobody bound.
- **`hooks.py` WS-AI block.** `ai_after_sign_in`: multi-user mode, owner (`uid == 1`), key present, otherwise nothing. It calls
  `jobs.requeue_unfinished()` and `knowledge.auto_reindex(("guide", "note", "upload"))` (a spawned thread). Imports are inside the function.
- **`api/routes/knowledge.py`.** `start_reindex` (so `auto_reindex`) returns False in multi-user mode with nobody bound, so the
  start-up call in `api/app.py` `_start_background_work` is already harmless there.
- **No ledger text in the library (D8).** In multi-user mode `rag/index.py` never indexes `txn` and deletes any `txn` chunks left from
  before (`reindex` output `txn.removed`); `tools.rag_note_rows()` returns `[]` (weekly commitments and transaction notes come from sealed
  columns, and the note titles carry the payee); `tools.rag_status` reports 0 transactions. Guides and the owner's uploaded `.md`/`.txt`
  files are indexed as before.
- **`tools.py` WS-AI block.** `apply_classifications` and `apply_category_to_similar` write `category_reason` through `vault.seal`
  (laptop mode: unchanged text).
- **Chat memory.** `routes/chat.py::uid_of` maps the int `1` (multi-user owner) to `"owner"`, so memories stay where they were.

## Requests for files I do not own

1. **`rectifi/api/app.py`, `_start_background_work` (optional clean-up).** Nothing is required: in multi-user mode both calls are now
   no-ops, and the owner's sign-in hook does the work. When convenient, wrap them as `if not config.multiuser():` so the intent shows.
2. **`rectifi/agent/memory.py`.** No change needed now. When AI opens to friends the namespace `("users", uid, "memories")` will use
   `str(uid)` for them; the store has no `user_id` column and no RLS (D8), so that is a later-phase seal/RLS job.
3. **`rectifi/agent/explain.py`, insights and alerts explain routes.** No change needed: `FRIEND_ROUTES` denies both explain routes
   (`tests/test_ai_mu.py` proves 403 for a friend on `POST /api/insights/explain` and `POST /api/alerts/{id}/explain`).
4. **`rectifi/seal_sweep.py` (WS-ACCOUNT).** It already deletes `rag_chunks` of corpus `txn`. It could also run
   `DELETE FROM rag_documents WHERE corpus = 'note'` once the owner is encrypted, to remove old notes that copied sealed text
   (`rag_note_rows`). My next re-index does this too, through `rag_delete_missing("note", [])`, but only when the owner signs in.

## Decisions (plan or task unclear, safest option taken)

- **Notes corpus is empty in multi-user mode.** The task said "guides and the owner's notes are fine". The `note` corpus is built from
  the sealed `note` and `commitment` columns, and titles include the payee, so indexing it would copy sealed text into plain tables.
  Only the owner's uploaded files (the `upload` corpus) stay. If Prag wants transaction notes searchable, they must be sealed in
  `rag_documents` first (later phase).
- **Queue keeps plain ids.** Putting `(id, ctx)` tuples on `jobs._queue` would break `tests/test_classify_llm.py` (it reads ids), and I
  may not loosen it. So the person lives in a side dict.
- **`_requeued` is not consumed by a no-user call**, so the later owner sign-in still requeues. After that it runs once per process.
- **Hook requires `ctx.key`.** No key means no classify job could read payees, so nothing is queued.
- **`apply-similar` stays allowed for friends** (WS-AUTH's choice), and its `category_reason` is sealed now.
- **`GET /api/imports/{batch}/rows` is not an AI route.** The import page uses it, so a friend may call it; RLS makes another person's
  batch a 404 (tested).

## Not verified

- Real Ollama, real embeddings, real LangGraph under load. All flows ran with scripted models and `BowEmbeddings`.
- The LangGraph checkpoint and store tables have no `user_id` (D8). Owner chat text and answers sit there readable.
- Decrypted payees reach the chart child process and the classify prompt (masked) while the owner is signed in; a queued classify job
  keeps the owner's data key in memory until it finishes (`jobs._owners`).
- Two sign-ins racing the once-per-process requeue were not tested.
