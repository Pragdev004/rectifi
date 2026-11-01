# Next steps (handoff to Claude Code on the laptop)

State on 2026-11-01. `master` = all AI workstreams merged (1574 passed in the cloud). Two branches are UNFINISHED:

## 1. `ws/f4fix` — review fixes (almost done, suite never run)
Fixes from the Opus review of master. Code is written; tests not yet run.
- MED 1 charts/_child.py: audit hook blocks os.kill / os.killpg / signal.pthread_kill.
- MED 2 charts/validate.py: FORBIDDEN_ATTRS (os, sys, subprocess, importlib, modules, builtins, …) + FORBIDDEN_ATTR_PREFIXES ("f_","gi_","cr_","ag_","tb_") so chart code can't reach modules or walk frames.
- MED 3 api/ask.py `_finish_stop`: stopped answers must get a real `unverified` list (rule 16).
- MED 4 agent/supervisor.py `verify_amounts`: on the knowledge (RAG) route use RAG's own plain-code unverified list.
- MED 5 scripts/dev.py + rectifi/tailscale.py: `up` refuses / `doctor` fails when Tailscale Funnel is on and no login is set (RECTIFI_PUBLIC=1 + set-password). HOSTING.md paragraph.
- LOWs: 44 px buttons in ImportCategories, forget_all loop cap, fork_point limit, redact error strings.
Todo: check each fix + its test exists, run suite, fix failures, merge into master.

## 2. `ws/alerts` — real-time spending-leak alerts (P1–P5 done, P6 half, P7 not started)
Spec: `docs/ALERTS_PLAN.md` (on the branch). Idea: ₹1 lakh salary vanishing in small Blinkit/Swiggy/Amazon UPI spends → detect merchant patterns, daily/weekly budgets, alerts in-app + Web Push to the installed PWA. All numbers deterministic; no model on the alert path.
- Done + tested: P1 schema, P2 merchants/patterns, P3 Web Push (VAPID, RFC 8291), P4 sweep/delivery/hooks, P5 API routes.
- P6 WIP (untested): web/public/sw.js, lib/push.ts, TodayStrip, pages/Alerts.tsx, pages/Leaks.tsx, Waterfall. Needs: routes in App.tsx + nav, vitest tests, typecheck, 390/1280 light/dark screenshots.
- P7: docs (CLAUDE.md rule 14 text, README, PROGRESS, CONCEPTS, .env.example RECTIFI_VAPID_PRIVATE_KEY / RECTIFI_VAPID_SUB, `dev.py` key generation if the plan says so).
Then: build-web, commit web/dist, full suite, merge into master (after ws/f4fix).

## Rules
CLAUDE.md is binding (LLM never decides a number, Decimal money, masking, thin API, no money maths in the browser, one worker, web/dist committed). docs/CONSTRAINTS.md lists every user requirement.

## Not verified anywhere
Real Ollama quality/latency, Windows chart sandbox (Job Object), Docker build, Oracle VM, Tailscale Funnel, real bank statements, a real phone push notification.
