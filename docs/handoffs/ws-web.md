# WS-WEB handoff (branch ws/web)

Built (all under `web/`, plus `tests/test_web_e2e_account.py`; nothing in `rectifi/`):

- Pages: `Signup` and `Recover` (one component, `components/AccountEntry.tsx`), `Settings` (change password, new recovery code,
  export ZIP, delete account), public `Privacy`, Login with a username in multi-user mode (laptop and shared-password login unchanged).
- `components/RecoveryGate.tsx`: shows a code once. Finish stays off until the code is downloaded (.txt blob) or "I saved it" is
  ticked (`lib/recovery.ts::canFinish`). Used by sign-up, recover and Settings "new recovery code".
- `api/secret.ts::useSecretCall`: every call that carries a password, code or token goes through it, never through
  `useMutation`/`useQuery` (a mutation keeps its variables and answer in the cache for minutes). `api/account.ts` has the calls.
- Owner-only pages (Ask, Charts, Library, Memory, Traces) are hidden from friends: sidebar, More sheet, bottom bar (a friend gets
  Alerts where the owner gets Ask). A friend who types `/ask` sees "This page is for the owner." "Explain this" and the Ask
  model status are never requested for a friend (no 403s; the e2e test asserts zero 403 responses).
- `api/auth.ts`: `AuthStatus` now carries `multiuser`, `username`, `is_owner`; helpers `isOwnerView`, `hasAccount`, `useIsOwner`.
- `api/client.ts`: `requestBlob` (ZIP download with the same headers and error shape), `delWith` (DELETE with a body).
  `request` was split into a private `send` plus `request`; behaviour is unchanged (client tests pass).

Tests: vitest (196, 20 files; new: `pages/accountPages.test.tsx`, `components/RecoveryGate.test.tsx`, `components/shell/nav.test.ts`,
`lib/recovery.test.ts`), e2e `tests/test_web_e2e_account.py` (15 cases: sign-up gate, sign out, recover, data readable, download opens
the gate, wrong invite/code, password mismatch, friend sees no owner pages and gets no 403, owner sees everything, Settings
password/code/export/delete, Privacy signed out at 390 and 1280 in light and dark, 44 px targets, no sideways scroll).

## Requests for the integrator

1. **JS budget (tests/test_web_dist.py, `JS_BUDGET`).** The new pages cost about 5.8 KB gzipped. I trimmed prose and merged
   components until the sum fits (168,939 of 168,960 bytes), so there are about 20 bytes of headroom. Any later change under
   `web/src` will break `test_javascript_and_css_stay_within_budget`. I did not touch the test (not mine, and I may not loosen
   one). Suggest raising `JS_BUDGET` to 175 KB when the integrator rebuilds `web/dist` last. Compression note: duplicated code is
   almost free under gzip, only unique text and logic cost bytes, so further trimming means removing features.
2. **Server log noise (already your request 3 in ws-auth.md).** Starting a multi-user server logs
   `NoUser` from `api/app.py::_start_background_work` -> `jobs.requeue_unfinished()` ("Could not resume the unfinished sorting jobs").
   It is caught and logged, the app works, but it shows in every e2e run until `lifespan` loops users.
3. Nothing else is needed from the server. The routes and shapes in ws-auth.md item 5 and ws-account.md item 3 matched the code.

## Decisions

- **Secrets never touch the query cache.** Not through TanStack at all (see `useSecretCall`). The e2e test proves the address, every
  request URL, localStorage, sessionStorage and `document.cookie` hold no code or password. The query cache itself cannot be read
  from outside the page, so that part is proven by construction: a vitest greps the secret-handling files for `useMutation`,
  storage, `console.` and `setQueryData`.
- **The invite code is not read from the URL** (no `?invite=`): it is a secret, and the contract says no secret in the URL.
- **Repeat-password boxes** on sign-up, recover and change-password: there is no reset, so a typo must not lock someone out.
- **No "off" screens.** `/signup` and `/recover` on a laptop show the form and the server's plain error (400 `signup_off` /
  `not_multiuser`) if submitted. Settings on a laptop says there are no accounts, and its menu item only appears for a signed-in
  multi-user person.
- **Sign-up step 2 failures.** If confirm answers `signup_expired`, the gate shows the server's sentence and a "Start again" link
  (the account was never made). A reload at step 2 loses the code on purpose; the unconfirmed account is cleared by the server.
- **Download file name is fixed** (`rectifi-recovery-code.txt`); the account name is inside the file. The file explains what the code is.
- **Settings has no "sign out everywhere" button** (route exists, `useSignOutEverywhere` exists in `api/auth.ts`); it was cut for the
  JS budget and was not in the brief. Delete is typed-username plus password; the button stays off until the typed name matches.
- **Privacy** is outside the app shell (no sidebar, no private requests). The page text is the registry text: it prints every
  `sealed_fields`, `keyed_hash_fields`, `plain_fields`, `unsealed_owner_tables`, allow-listed brand name, `who_can_read` row,
  `limits` line, `recovery`, and `backup_days`. The e2e test compares the rendered text with `account.privacy_facts()` and with
  `GET /api/privacy`. "Back to Rectifi" goes to `/`, which sends a signed-out visitor on to sign in.
- **Sidebar/More for a friend** drop the five owner pages; the Ask status line is not requested. In the e2e test the sign-out
  button is reached through More on a phone.
- **Login** now keeps the username in component state only, clears the password after a send, and no longer uses a TanStack mutation
  (same reason as above). `signInProblem` and `safeNext` are unchanged.

## Not verified

- Real Safari/iOS download behaviour of a blob link (Chromium only). Clipboard copy of the code uses the existing `CodeValue`.
- The owner claim flow through the browser beyond "owner sees all pages" (the sweep thread runs; its result is not checked here).
- Screenshots were looked at for sign-up code (390), friend page (1280) and privacy (390, dark); the others were only checked by
  the layout assertions (no sideways scroll, 44 px targets).
