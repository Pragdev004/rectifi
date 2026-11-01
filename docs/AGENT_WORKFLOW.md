# Agent workflow (how every Rectifi task is run)

Binding for Claude Code on this repo. Goal: max output quality, min tokens.

## Models
- Opus 5.5, high effort: planning, architecture, creative thinking, review, integration decisions.
- Sonnet 5.5, high effort: all coding (subagents).
- Switch models with Prag's model-change mod (find it in ~/.claude or this project's .claude: commands/, plugins/, skills/) instead of asking him. If you can't find it, ask once, then use `/model` or the Agent tool's model parameter.

## Pipeline (sequential stages; parallel, conditional and iterative inside them)
1. PLAN (Opus, one agent): read CLAUDE.md, docs/CONSTRAINTS.md, PROGRESS.md and the task's plan doc. Write docs/<TASK>_PLAN.md:
   contracts/interfaces first, workstreams with STRICT file ownership (no two streams edit the same file),
   shared files edited only inside `# ═══ WS-<NAME>: begin/end ═══` blocks, acceptance tests per stream,
   and a "Not verified" list.
2. FOUNDATION (Sonnet, one agent, sequential): shared schema, contracts, stubs, test fakes, markers. Commit on master.
   Full suite green before fan-out.
3. BUILD (Sonnet, PARALLEL): one subagent per workstream, each in its own git worktree + branch (ws/<name>)
   and its own test DB `rectifi_<ws>_test`. Narrow brief: plan section + owned files only.
   Run targeted tests while working, full suite once at the end. Commit WIP often (rate limits).
   Report <=15 lines: commit, test tail, done/skipped, integrator requests (docs/handoffs/integrator-requests.md).
   Stagger launches if rate-limited; resume a stopped agent with SendMessage, never restart it.
4. INTEGRATE (Sonnet, one agent): merge branches in dependency order, apply integrator requests,
   wire across streams, run real subgraphs/routes end-to-end, build-web, full suite, screenshots 390/1280 light/dark.
5. REVIEW (Opus, read-only, fresh agent that did not write the code): diff vs CLAUDE.md rules,
   CONSTRAINTS.md, security (SQL injection, XSS, auth bypass, sandbox escape, secrets in logs),
   accessibility, design system. Findings tagged HIGH/MED/LOW + file:line + concrete fix.
6. FIX LOOP (CONDITIONAL + ITERATIVE): only if HIGH/MED exist -> Sonnet fixer on ws/fix branch -> Opus re-review
   of just those findings. Max 3 rounds. LOWs go to PROGRESS.md backlog.
7. VERIFY + SHIP: full suite, live checks where possible (`pytest -m ollama`, `evals --live`), update PROGRESS.md
   (real test count, Not verified list), merge to master, one short message to Prag.

## Small tasks
Skip 2-4: one Sonnet agent on a branch -> Opus review -> fix loop.

## Rules for every agent
- CLAUDE.md is binding. Never print secrets or .env values. No claims without running it.
- Main session stays thin: delegate, don't read whole files yourself; relay only stage completions to Prag, in few words.
- Deleting files on Prag's disk outside the repo needs his OK.
