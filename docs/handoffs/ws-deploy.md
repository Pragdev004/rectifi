# WS-DEPLOY handoff (ws/deploy)

Built: Streamlit and MCP refuse in multi-user mode, a `gateway` service in `deploy/compose.oracle.yml`, the env examples,
`deploy-check` for `RECTIFI_DB_PASSWORD` and the session secret, the HOSTING.md runbook, tests (`test_deploy_modes.py`,
`test_deploy_hosting_doc.py`, additions to `test_deploy_files.py` and `test_mcp.py`).

## Decisions

1. **Gateway is a compose profile.** `gateway` has `profiles: ["gateway"]`, so it runs only with `--profile gateway` (the `rc` alias in
   HOSTING.md has it). Reason: rule 8 says the gateway is off by default; D6 only turns it on for the server on purpose.
2. **Gateway listens on 0.0.0.0 inside its container.** A loopback bind inside a container cannot be reached through Docker's published
   port, and the app container already does the same (`RECTIFI_BIND_HOST=0.0.0.0` in the Dockerfile). The host side is
   `127.0.0.1:8765:8765` only. A test fails if any compose port is not on 127.0.0.1 or if `0.0.0.0:` appears in the file.
   Rule 11 says "do not hard-code 0.0.0.0": it is an environment value in compose, like the Dockerfile's, not code.
3. **Multi-user mode ignores `RECTIFI_PASSWORD_HASH`** (ws-auth item 6). `deploy-check` then requires only a 32+ character
   `RECTIFI_SESSION_SECRET`. One-person mode still requires both.
4. **Restore uses `--role=rectifi`** so restored tables belong to the app role. This is my reading of pg_restore, never run. HOSTING.md
   says so and gives a `reassign owned` fallback.
5. **Old Docker volume path** documented as: export with the old `-U rectifi`, `down -v`, `up`, restore as `postgres`. Laptop dumps
   must be migrated with `migrate-tenancy` first.
6. `MULTIUSER` is empty in both env examples (off by default); HOSTING.md says when to set it to 1.
7. The Streamlit refusal sits after `set_page_config`, before the sidebar and the foundation `TenancyMigrationNeeded` handler, and ends
   in `st.stop()`. The MCP refusal is in `main()` (stderr, exit code 2, no stdout); importing the module is unchanged so tests still work.

## Needed elsewhere (integrator)

- **CLAUDE.md** rule 8: "off by default" stays true; add "in multi-user mode the gateway may run on the server (compose profile `gateway`,
  127.0.0.1:8765, Funnel 8443)". Rule 11 hosting addendum: compose also publishes `127.0.0.1:8765:8765`. Rule 13: Streamlit and the MCP
  server are laptop-mode only (this branch enforces it).
- **`rectifi/sms_gateway.py` lifespan (cosmetic, WS-CORE owner):** with `RECTIFI_SMS_HOST=0.0.0.0` inside the container it logs
  "anyone on your network ... can send SMS". The message is wrong on the server. Change: when `RECTIFI_PUBLIC=1` (the image sets it), log
  one plain line instead. No behaviour change.
- **`dev.py` inside the app container:** HOSTING.md tells Prag to run `rc exec app python scripts/dev.py invite` etc. `dev.py` was written
  for a laptop (`project_python()`, `.env`). If it cannot find its Python in the container, a fix in `scripts/dev.py` (owner: integrator)
  or running the command from a machine with the code and a `DATABASE_URL` is the fallback.
- **PROGRESS.md:** add the WS-DEPLOY entry and the unverified list below.

## Not verified

- No Docker here: compose, Dockerfile and the new `gateway` service had static checks only (YAML tests; `docker compose config` tests skip).
- Tailscale Funnel: free on the Personal plan, port 8443 for a second service next to 443, Host header seen by the app and gateway. A free
  tunnel is the fallback (HOSTING.md).
- `rc exec app python scripts/dev.py ...` on a real container; `pg_restore --role=rectifi`; `VACUUM FULL` timing on a real ledger.
- The gateway healthcheck and read-only filesystem with `/tmp` as tmpfs (same pattern as the app).
- No real phone has posted SMS through Funnel.
