# Mission Handoff: 9dab9b74-6487-4d7a-92f7-b08ddb382f57

**Title:** Whilly v4 — Prompt-injection hardening + PR review feedback loop
**Closed:** 2026-05-05
**Base when started:** upstream `main` at v4.6.0
**Base after rebase:** upstream `d67514b` at v4.6.1
**Final HEAD:** `36cf431` (10 commits above `d67514b`)

This document captures the exact state of the mission at close so the next droid (or human) can pick up cleanly. Do **not** assume the validation contract or `features.json` from this mission are still active state — they live in `.factory/missions/9dab9b74-…/` and are mission-local. This file is the only state that survives in the repo.

---

## TL;DR

| Track | Status | Detail |
| --- | --- | --- |
| **M1 — security hardening** | ✅ **DONE & SEALED** | 5 commits, all 36 VAL-SEC-* assertions PASSED end-to-end |
| **M2 — PR review feedback** | ⚠️ **CODE COMPLETE, MILESTONE NOT SEALED** | 5 commits, 35 contract assertions still `pending` |
| **Misc fixes** | ⏭️ Deferred | 2 follow-ups documented below |

The codebase compiles, lints clean, type-checks clean, import-linter green, **1469 unit tests pass** (was 1376 pre-mission), all M2 integration tests pass when run with the documented invocation. Nothing is broken. The only thing the mission did not finish is the **automated milestone validation pass** for M2.

---

## Commit Manifest (10 commits, all pushed local-only until handoff)

```
36cf431 feat(m2-pr-fix-prompt-and-cli): build_pr_fix_prompt + pr-feedback CLI subcommand
c9b25d9 feat(m2-pr-iterate): pr_iterate followup spawner + cap + completion event
6882051 feat(m2-pr-feedback): github_pr_feedback poller (m2-pr-feedback-poller)
a3e277c feat(m2-pr-feedback): post-COMPLETE PR opener hook + pr.open_failed event
de67210 feat(m2-pr-feedback): alembic 012 pull_requests + PR event types
3fd9c9b feat(m1-security): verifier '--' argv separator + author/timestamp guard on revert
80c19a7 feat(m1-security): tmux wrapper shlex.quote + Task.id regex validator
1fc1a8d feat(m1-security)!: default-deny worker Claude agent across both dispatch paths
75d14d9 feat(m1-security): wire prompt sanitizer into every external-content prompt site
cb344d4 feat(m1-security): add whilly.security.prompt_sanitizer
```

`1fc1a8d` is the only **BREAKING** commit (default-deny worker Claude agent). CHANGELOG entry under `## [Unreleased] — v4.7.0 prep` documents the opt-in env var `WHILLY_AGENT_ALLOW_SHELL=1` for restoring the legacy posture. The project version remains at `4.6.1` (inherited from upstream's release cut) — this mission did not own a version bump.

---

## M1 — Security Hardening (SEALED)

### What changed

**New module: `whilly/security/prompt_sanitizer.py`** — `sanitize_external_text(text, *, scope, max_chars=8000)` wraps untrusted input in `<UNTRUSTED kind={scope}>…</UNTRUSTED>` fences, redacts AWS / GitHub PAT / Slack / OpenAI tokens, strips C0 control bytes, neutralises embedded `</UNTRUSTED>` close-tags, enforces a length cap with a single `[truncated]` indicator, and is idempotent.

**Wiring (commit `75d14d9`)** — sanitizer applied at every site that interpolates external content into prompts or PR bodies:
- `whilly/core/prompts.py::build_task_prompt` (description, acceptance_criteria, test_steps, prd_requirement)
- `whilly/forge/intake.py::_issue_to_description`
- `whilly/sources/github_issues.py::issue_to_task`
- `whilly/sources/jira.py::issue_to_task_dict`
- `whilly/triz_analyzer.py` + `whilly/core/triz.py::_build_prompt`
- `whilly/decision_gate.py::build_prompt`
- `whilly/sinks/github_pr.py::render_pr_body` and `_short_title` (PR title control-byte strip + 60-char cap)
- `whilly/prd_generator.py::_build_tasks_payload`

**Default-deny worker agent (commit `1fc1a8d`, BREAKING)** — both dispatch paths (`whilly/agents/claude.py::ClaudeBackend` and `whilly/adapters/runner/claude_cli.py::run_task`) now build the Claude argv with `--disallowedTools Write,Edit,MultiEdit,NotebookEdit,Bash` and OMIT `--dangerously-skip-permissions`. Restore legacy with `WHILLY_AGENT_ALLOW_SHELL=1`. `WHILLY_CLAUDE_SAFE=1` continues to add `--permission-mode acceptEdits` and stacks on top of the denylist.

**Task.id validator (commit `80c19a7`, Part A)** — new module `whilly/core/task_id.py` exposes `validate_task_id` + `VALID_TASK_ID_RE = ^[A-Za-z0-9._:/-]+$` (rejects `..` traversal). Wired into `Task.from_dict`, `whilly/adapters/filesystem/plan_io.py::parse_plan_dict`, and `whilly/cli/validate_schema`. Loading a task with shell metacharacters or `..` raises a structured `ValueError` naming the offending id.

**Tmux wrapper hardening (commit `80c19a7`, Part B)** — every interpolated field in `whilly/tmux_runner.py::launch_agent` (`task_id`, `cwd`, `log_file`, `model`, `backend_name`, `session_name`) is wrapped via `shlex.quote`.

**Verifier hardening (commit `3fd9c9b`)**:
- `_run_lint` and `_run_tests` now insert a literal `--` element between flags and file args — `git diff` files named `--exclude.py` are treated as positionals, not flags.
- `_revert_last_commit` reads HEAD's author email + commit timestamp via `git log -1 --format=%ae%n%ct HEAD` and refuses to `git reset --soft HEAD~1` unless author matches the orchestrator's expected email AND `commit_ts >= task_start_ts`. Refusal logs a structured warning with substring `refus` + the offending field; no subprocess fires.

### Validation status

All **36 VAL-SEC-* assertions** verified PASS by `user-testing-validator-m1` (run `93a9faec…`, 2026-05-05T16:10Z). Six flow-validator subagents executed targeted pytest invocations covering each assertion group. The scrutiny-validator-m1 was **overridden by user direction** (synthesis at `.factory/missions/9dab9b74-…/validation/m1-security-hardening/scrutiny/synthesis.json`) after three rounds surfaced only environmental and pre-existing flakes; the validator-gate evidence itself was clean (lint + format + typecheck + import-linter all green; 1389 unit tests passed).

**M1 milestone is sealed.** Do not re-validate it.

---

## M2 — PR Review Feedback (CODE COMPLETE, NOT SEALED)

### What changed

**Alembic migration `012_pull_requests_and_pr_events.py` (commit `de67210`)** — new `pull_requests` table:
- `id BIGSERIAL PK`, `plan_id TEXT NOT NULL` FK plans.id ON DELETE CASCADE (note: TEXT, not UUID — the spec text was off; existing schema convention preserved)
- `task_id TEXT NOT NULL`, `pr_number INT NOT NULL`, `pr_url TEXT NOT NULL`, `branch TEXT NOT NULL`, `head_sha TEXT`
- `state TEXT NOT NULL DEFAULT 'open'` CHECK in `('open','merged','closed','failed')`
- `review_decision TEXT` CHECK in `('APPROVED','CHANGES_REQUESTED','REVIEW_REQUIRED', NULL)`
- `last_seen_review_id BIGINT`, `last_seen_check_run_id BIGINT`, `last_synced_at TIMESTAMPTZ`
- composite UNIQUE on `(plan_id, pr_number)`
- `schema.sql` mirrored

Six PR event-type literals + `PR_EVENT_TYPES` tuple in `whilly/adapters/db/repository.py`: `pr.opened`, `pr.review.changes_requested`, `pr.review.approved`, `pr.iteration.requested`, `pr.iteration.completed`, `pr.merged`. Plus `pr.open_failed` (warning literal added in commit `a3e277c`). `TaskRepository.emit_pr_event(event_type, *, plan_id, task_id, payload)` round-trips through Postgres `events` and the JSONL mirror; ValueError on unknown event_type before any I/O.

**Post-COMPLETE PR opener hook (commit `a3e277c`)** — `whilly/sinks/post_complete_pr_hook.py` exposes `make_post_complete_hook(...)`. Plumbed through `run_local_worker` and `run_worker`; fires inside try/except after `complete_task` returns so failures never disturb the COMPLETE transition. Wired in `whilly/cli/run.py::_async_run` only when `WHILLY_AUTO_OPEN_PR=1` AND `plans.github_issue_ref` is non-NULL. Failure paths (push fail, gh fail, gh timeout) emit a single `pr.open_failed` warning event with `task_id` + exit code + failure_mode in detail; happy path persists `pull_requests` row + `pr.opened` event in PG and JSONL with payload parity.

**Remote-worker NOT wired** — see Open Tech Debt below.

**Feedback poller (commit `6882051`)** — `whilly/sources/github_pr_feedback.py::poll_pr_feedback(repo, plan_id)`. For each open `pull_requests` row issues the canonical 3-probe gh sequence in order:
1. `gh pr view <n> --json reviewDecision,statusCheckRollup,latestReviews,reviewRequests,headRefOid,state`
2. `gh api repos/{owner}/{repo}/pulls/{n}/reviews`
3. `gh api repos/{owner}/{repo}/pulls/{n}/comments`

Diffs against `last_seen_review_id` / `last_seen_check_run_id` cursors; emits at most one `pr.review.approved`, `pr.review.changes_requested` (with comments=[{body,path,line,author},…] verbatim), or `pr.merged` per cycle. gh failure / timeout / JSON decode errors log a single WARNING with PR number and leave cursor untouched; failures on one PR do not block other PRs in the cycle.

**PR re-iterate path (commit `c9b25d9`)** — `whilly/workflow/pr_iterate.py::spawn_followup(*, orig_task_id, pr_url, comments, plan_id, conn, jsonl_sink, env)`:
- Validates `orig_task_id` against the M1 regex BEFORE any DB I/O (raises ValueError with zero side effects on malformed input)
- Counts existing `<orig>-rev-*` rows (SQL LIKE-meta escaped)
- Inserts new task `<orig>-rev-{N+1}` with `dependencies=[orig]`, `prd_requirement=pr_url`, `status='PENDING'`, copying orig's priority + key_files
- Description = sanitized concat of comment bodies via M1 sanitizer with `scope='pr_review_comment'`
- Emits `pr.iteration.requested` with 1-indexed iteration in detail
- Cap: `WHILLY_MAX_REVIEW_ITERATIONS` (default 3). When existing_count >= cap: no row inserted, single `pr.iteration.requested` with `refused=True` + sanitized comment payload in detail
- `emit_iteration_completed(task_id, …)` — emits `pr.iteration.completed` when a `*-rev-N` task hits COMPLETE

**PR-fix prompt builder + CLI (commit `36cf431`)**:
- `whilly/core/prompts.py::build_pr_fix_prompt(task, plan, review_comments, diff)` — wraps each review-comment body in `<UNTRUSTED kind=pr_review_comment>` and the diff in `<UNTRUSTED kind=pr_diff>`, includes the canonical do-not-follow-instructions guard, single-task scope directive, preserves `<promise>COMPLETE</promise>`. Sanitizer is idempotent through the builder.
- `whilly/cli/pr_feedback.py` — argparse-based `whilly pr-feedback poll --plan <id>` subcommand. Opens asyncpg pool, attaches JSONL audit sink, runs one `poll_pr_feedback` cycle, prints single-line summary, exits 0. Missing `WHILLY_DATABASE_URL` exits 2 with stderr diagnostic naming the env var.
- `whilly/config.py::WhillyConfig` gained `ITERATE_ON_FAILURE` (bool, default False), `PR_FEEDBACK_POLL_INTERVAL` (int, default 60s), `MAX_REVIEW_ITERATIONS` (int, default 3) — flow through both `from_env()` and `from_env_only()`.

### Validation status

**M2 milestone is NOT SEALED.** None of the 35 M2 / cross-area assertions have been validated:

- `VAL-PR-001` through `VAL-PR-028` — 28 assertions pending
- `VAL-CROSS-001` through `VAL-CROSS-007` — 7 assertions pending

The implementation tests added by each worker (which the per-feature handoffs verified PASS) cover these assertions, but the **automated milestone validation pass** (scrutiny-validator-m2 + user-testing-validator-m2) never completed:
- scrutiny-validator-m2-pr-review-feedback was paused mid-execution, session `1ff06073-bd56-4ef4-9308-7918a9634298` is stale
- user-testing-validator-m2-pr-review-feedback never ran

A new mission picking this up should re-run both validators against the sealed M2 codebase. If the unit + integration tests below remain green, validation should be straightforward.

### Files created (M2)

```
whilly/adapters/db/migrations/versions/012_pull_requests_and_pr_events.py
whilly/sinks/post_complete_pr_hook.py
whilly/sources/github_pr_feedback.py
whilly/workflow/pr_iterate.py
whilly/workflow/__init__.py        (if it didn't exist — check)
whilly/cli/pr_feedback.py
tests/integration/test_alembic_012_pull_requests.py        (15 cases)
tests/integration/test_pr_event_types_round_trip.py        (10 cases)
tests/integration/test_pr_events_concurrent_ordering.py    (2 cases)
tests/integration/test_post_complete_pr_hook.py            (3 cases)
tests/integration/test_pr_feedback_poller.py               (5 cases)
tests/integration/test_pr_iterate_followup.py              (7 cases)
tests/integration/test_pr_iterate_completion_event.py      (8 cases)
tests/integration/test_pr_feedback_e2e.py                  (1 case — VAL-PR-021 + VAL-CROSS-003)
tests/unit/test_pr_title_argv_sanitization.py              (6 cases)
tests/unit/test_pr_hook_failure_events.py                  (7 cases)
tests/unit/test_pr_feedback_argv_shape.py                  (8 cases)
tests/unit/test_pr_feedback_failure_handling.py            (6 cases)
tests/unit/test_pr_iterate_sanitization.py                 (7 cases)
tests/unit/test_pr_iterate_cap.py                          (17 parametrized cases)
tests/unit/test_build_pr_fix_prompt.py                     (13 cases)
tests/unit/test_pr_feedback_cli.py                         (10 cases)
tests/unit/test_whilly_config_pr_envs.py                   (6 cases)
```

---

## Open Tech Debt

### 1. `misc-followup-server-side-pr-hook` — remote-worker PR opener hook

**Why it didn't land in this mission:** the worker-entry-purity import-linter contract forbids `asyncpg`/repository imports inside `whilly/cli/worker.py`'s closure, and remote workers do not hold direct DB access. The post-COMPLETE PR hook in commit `a3e277c` is wired only into the **local** worker path. Remote-worker COMPLETE flows do not currently fire `gh pr create`, so end-to-end PR automation only works in local-worker mode today.

**How to fix:** add a server-side handler that fires the PR opener after `server.complete_task` succeeds (cleanest — the COMPLETE write already happens server-side over HTTP for remote workers), OR introduce a control-plane HTTP endpoint `POST /plans/{plan_id}/tasks/{task_id}/pull-request` that the remote worker calls after COMPLETE. Reuse `whilly/sinks/post_complete_pr_hook.py::run_post_complete_pr_hook` + `insert_pull_request` + `get_plan_github_issue_ref`.

Verification: end-to-end test in `tests/integration/test_post_complete_pr_hook_remote.py` that drives a remote worker through COMPLETE and asserts the same VAL-PR-005..008 invariants surface (pull_requests row + pr.opened event) without violating import-linter.

### 2. `misc-fix-distributed-audit-docs-mirror-flake`

**Status as of this handoff:** the chore commit that closes this mission restores the docs/distributed-audit/ mirror to its canonical content. That makes `tests/unit/test_m1_readiness_baseline.py::test_distributed_audit_docs_mirror_canonical_source` pass on first run after a clean checkout.

**However**, the underlying **root cause** is not fixed. There is a pytest fixture or session-autouse hook somewhere in the suite that rewrites `docs/distributed-audit/research-findings.md` (and its sibling) to match the canonical source as a side-effect. On every full-suite run the same mirror drift will re-appear in the working tree. Today the test passes because the mirror == canonical at HEAD, but anything that legitimately edits the canonical source under `library/distributed-audit/` will silently re-introduce the drift.

**Proper fix** (deferred — out of scope for this closing chore): identify the side-effect-producing fixture (likely under `tests/unit/test_m1_readiness_baseline.py` or in `conftest.py`) and either (a) make it not rewrite the mirror, or (b) make `test_distributed_audit_docs_mirror_canonical_source` order-independent by writing the expected mirror content in its own setup. The tracked-as-feature description preferred option (b) for low touch.

### 3. Additional non-blocking suggestions surfaced during M2

- **`tests/conftest.py::db_pool`** TRUNCATE list — should append `pull_requests` for clarity. Today CASCADE handles it via FK chain so behaviour is correct, but explicit mention survives any future migration that severs the chain. Pure additive change. (M2F2 worker raised this; orchestrator dismissed as low-value at the time.)

- **Spec text correction** (informational only — code is correct): the M2F1 feature description text said `plan_id UUID` but the existing `plans.id` column is `TEXT`. The migration correctly used TEXT. Future spec authors should write `plan_id TEXT NOT NULL` in any PR-feedback specification.

---

## How to Resume

If the next mission picks this up:

1. **Branch state:** local main is `36cf431` + the closing chore commit, all 11 commits ahead of origin (or 0 ahead post-push). All upstream M3 work (testcontainers retry, /health fix, compose tag bump, v4.6.1 release) is integrated via the rebase performed mid-mission on 2026-05-05.

2. **Test invocation policy** (CRITICAL — was the source of M1 scrutiny round-2 cascade):
   ```bash
   # Unit suite — fast, parallel-safe
   sudo -E /home/factory-user/whilly-orchestrator/.venv/bin/python -m pytest tests/unit -n 2 -q

   # Integration suite — heavy, MUST run serially + deselect known flake
   sudo -E /home/factory-user/whilly-orchestrator/.venv/bin/python -m pytest tests/integration \
     -p no:xdist -q \
     --deselect tests/integration/test_phase6_cross_host.py::test_phase6_two_workers_drain_five_task_plan_and_shutdown_cleanly
   ```
   Do **NOT** run `pytest -q -n 2` over root. AGENTS.md (mission-local) explicitly forbade it; integration tests under xdist saturate the Docker daemon and produce cascading `asyncpg.create_pool failed after 6 attempts` errors.

3. **Pre-existing flake to keep deselecting:** `tests/integration/test_phase6_cross_host.py::test_phase6_two_workers_drain_five_task_plan_and_shutdown_cleanly`. This was confirmed pre-existing on upstream `d67514b` (without our M1 commits) on 2026-05-05 — fails identically. NOT caused by this mission.

4. **Environment:** `docker-compose-v2` plugin (v2.40.3) was installed mid-mission on 2026-05-05 to satisfy `tests/integration/test_demo_compose_default_env.py::test_demo_compose_config_validates`. The legacy `docker-compose` v1 binary is intentionally absent (broken on Python 3.12). Tests must use `docker compose` (v2 plugin) only.

5. **Validation contract** lived in `.factory/missions/9dab9b74-…/validation-contract.md` (71 assertions: 36 VAL-SEC + 28 VAL-PR + 7 VAL-CROSS). For a follow-up mission to seal M2, copy or recreate the M2 + cross subset and re-run the validation surface. The implementation tests already prove behaviour; the milestone-level user-testing run is what was missed.

6. **CLI surface added by M2** — `whilly pr-feedback poll --plan <id>`. Required env: `WHILLY_DATABASE_URL`. Optional: `WHILLY_AUTO_OPEN_PR`, `WHILLY_ITERATE_ON_FAILURE`, `WHILLY_PR_FEEDBACK_POLL_INTERVAL`, `WHILLY_MAX_REVIEW_ITERATIONS`, `WHILLY_AGENT_ALLOW_SHELL` (M1 opt-in for legacy worker permissions).

---

## Audit Trail

- Initial audits: `.tmp-audit/prompt-injection-audit.md` (38 vulnerabilities mapped) and `.tmp-audit/pr-feedback-audit.md` (PR flow gap analysis). These are intentionally **untracked** (`.tmp-audit/` is in `.gitignore` or just not added). They informed the mission plan and remain as orchestrator-side reference if you need to pick up the same work.
- Mission artifacts (validation-contract, features.json, AGENTS.md, library/, skills/, handoffs/, validation/) live in `.factory/missions/9dab9b74-6487-4d7a-92f7-b08ddb382f57/` outside the repo.
- Per-feature worker handoffs at `.factory/missions/9dab9b74-…/handoffs/*.json` capture every implementation step's evidence.
- M1 scrutiny override synthesis: `.factory/missions/9dab9b74-…/validation/m1-security-hardening/scrutiny/synthesis.json`.
- M1 user-testing synthesis: `.factory/missions/9dab9b74-…/validation/m1-security-hardening/user-testing/synthesis.json`.
