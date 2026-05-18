# Session Handoff — 2026-05-18

PRD scope: [`docs/PRD-post-auth-hardening.md`](../docs/PRD-post-auth-hardening.md)
Plan file: [`.planning/post-auth-hardening-tasks.json`](post-auth-hardening-tasks.json)

## What shipped today

| PR | Branch | Squash SHA | What |
|---|---|---|---|
| [#271](https://github.com/mshegolev/whilly-orchestrator/pull/271) | `fix-ci-4-failures-on-main` | `01a5b24` | Restore green CI on main: version drift (`__init__.py`, `pyproject.toml`), `BASE_RUNNER_ENV_ALLOWLIST` contract, WUI artifact classification for 5 auth templates |
| [#272](https://github.com/mshegolev/whilly-orchestrator/pull/272) | `feat-c7-document-dashboard-token-secret` | (merged) | C7 docs: `WHILLY_DASHBOARD_TOKEN_SECRET` in `.env.example` + new `## Authentication Configuration` section (325 words) in `docs/Whilly-Usage.md` |
| [#275](https://github.com/mshegolev/whilly-orchestrator/pull/275) | `feat-f18a-worker-tags-migration` | (merged) | F18a: Alembic migration **023** (NOT 026 — see note) adds `workers.tags TEXT[]` and `tasks.required_tags TEXT[]` |

All three: merged via `--squash`, local + remote branches deleted, CI green on each.

## Issues filed (open)

- [#273](https://github.com/mshegolev/whilly-orchestrator/issues/273) — Whilly v4: add in-process / no-DB single-task mode.
- [#274](https://github.com/mshegolev/whilly-orchestrator/issues/274) — `CLAUDE.md` describes v3 architecture; v4 has been shipped.

## Plan state at handoff

```
done       = 3   (A0, C7, F18a)
skipped    = 2   (A1a, A1b — stale, defect did not reproduce)
human_loop = 1   (G19 — claude-anonymizer extraction, needs operator)
pending    = 23
total      = 29
```

### Important re-routes done this session

The originating PRD A1a/A1b assumed a FastAPI `RuntimeError` in `build_auth_router`. The defect did not reproduce. Truthful update applied:

- A1a, A1b → `skipped`, descriptions prefixed with explanation.
- New `A0-ci-restoration` (status=done) records the actual work in PR #271.
- 17 task dependency lists were re-pointed from `A1b` → `A0` (because Whilly treats `skipped` as a permanent block; only `done` satisfies a dep).

### F18a numbering deviation (worth noting for next migration)

PRD pre-assigned `026_worker_tags.py` assuming E14a (024 TOTP) and E15 (025 WebAuthn) would land first. They didn't. F18a took the next sequential `023`. When E14a/E15 land they will renumber against the then-current head.

## Ready right now (9 tasks, no blockers)

| Prio | ID | Quick win? | Notes |
|---|---|---|---|
| critical | `A2-smoke-test-post-auth-journey` | no — E2E pytest, 30-60 min | Auth-flow assertions skip cleanly pre-D11 (auth_audit table) |
| high | `B3-unit-tests-reset-endpoint` | medium — single test file | Pure unit tests on `tasks_api_crud.reset_preview_endpoint` |
| high | `B4-unit-tests-worker-launch` | medium — single test file | Pure unit tests on `whilly/cli/worker_launch.py`. Unblocks F18b + H21 |
| high | `B5-integration-test-session-persistence` | no — integration test | Restart server, session must survive |
| high | `C6-must-change-gate-middleware` | medium-large | Middleware that gates routes when `must_change_password=true`. Unblocks D9 |
| high | `C8-cluster-rate-limit-multi-worker-warning` | medium — but has Redis stub | Detect multi-worker w/o Redis → WARN |
| high | `C12-smtp-mailer` | medium — needs SMTP client | Magic-link delivery. Unblocks D13 |
| medium | `D11-auth-audit-migration-and-repo` | medium — migration **024** + repo | Auth audit table; unblocks A2 auth_audit assertions + D10 |
| low | `E14a-totp-migration-024` | **yes — same shape as F18a (#275)** | Schema only. **Take next free number `024`** (not PRD's 024 literal). Unblocks E14b/E15/E16 |

## Suggested next session start

**Open with `E14a` first.** Rationale:
- Same shape as F18a we just shipped — Alembic migration, single file, ~30 lines, no logic, ruff + `alembic heads` verify. ~10 min wall-clock.
- It's the cheapest task that unblocks downstream (E14b → E15 → E16).
- Reinforces the workflow pattern (branch → write migration → ruff → `alembic heads` → commit → push → PR → merge).

**Then `D11-auth-audit-migration-and-repo`.** Rationale:
- Also starts with a migration (would be `025_auth_audit.py` after E14a takes 024).
- The repo layer is mechanical: see `whilly/api/users_repo.py` for the existing repo pattern.
- Unblocks `A2`'s `auth_audit` assertions (currently behind a `pytest.skip` guard) and `D10`.

**`A2-smoke-test-post-auth-journey` after both D11 and E14a land.** Reasoning: A2 has a conditional assertion block on `auth_audit` that becomes live only when `D11` lands. If you tackle A2 before D11, you write a test that skips most of its useful assertions.

## How to start the next session

```bash
cd /opt/develop/whilly-orchestrator
git checkout main && git pull --ff-only origin main
cat .planning/SESSION-HANDOFF-2026-05-18.md   # this file
cat .planning/post-auth-hardening-tasks.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
done = {t['id'] for t in d['tasks'] if t['status'] == 'done'}
ready = [t for t in d['tasks']
         if t['status'] == 'pending'
         and (not t.get('dependencies') or all(dep in done for dep in t['dependencies']))]
for t in sorted(ready, key=lambda x: (['critical','high','medium','low'].index(x['priority']), x['id'])):
    print(f'  [{t[\"priority\"]:8}] {t[\"id\"]}')
"
```

### For E14a specifically

```bash
# Current Alembic head (should be 023_worker_tags after PR #275 merge)
.venv/bin/alembic -c alembic.ini heads

git checkout -b feat-e14a-totp-migration
# Create whilly/adapters/db/migrations/versions/024_totp.py
# Pattern: copy 023_worker_tags.py, change revision = "024_totp",
#          down_revision = "023_worker_tags", and the column DDL.
# Per PRD §Epic E Item 14a: users.totp_secret TEXT NULL +
#                            users.totp_enabled BOOLEAN NOT NULL DEFAULT FALSE
# Both columns reversible.

.venv/bin/python -m ruff check whilly/adapters/db/migrations/versions/024_totp.py
.venv/bin/alembic -c alembic.ini heads   # must show "024_totp (head)"

# Update plan: E14a → done with realisation note (see how F18a was marked)
# Commit, push, gh pr create, merge after CI green.
```

## Env reminders (for live runs)

If you ever want to actually launch Whilly v4 against the plan (vs do tasks by hand), the prereqs are:

```bash
bash scripts/db-up.sh                                    # boot Postgres
.venv/bin/alembic -c alembic.ini upgrade head            # apply schema
# Plus: WHILLY_DATABASE_URL, CLAUDE_BIN, HTTP_PROXY for tunnel, etc.
# See issue #273 — single-task mode without DB would change this.
```

For manual `claude` invocations from this terminal, the binary lives at:

```
/Users/mshegolev/.reflex/.nvm/versions/node/v20.19.6/bin/claude
```

The shell alias `claude` → `claudeproxy` (function in `~/.claude/shell-snapshots/...`) wraps it with SSH tunnel + `--dangerously-skip-permissions`. Python subprocess won't see the alias.

## Files touched in the wider planning surface

- [`.planning/post-auth-hardening-tasks.json`](post-auth-hardening-tasks.json) — three status mutations + dep re-routes (commits in #272, #275 and one direct-to-main `67c2785` for the truthful A0/A1a/A1b update).
- [`docs/PRD-post-auth-hardening.md`](../docs/PRD-post-auth-hardening.md) — **not refreshed**. §Epic A still describes the original A1a/A1b hypothesis. Refreshing is in scope for a future PR but was deliberately left out of every PR this session to keep diffs narrow. Worth doing as part of `I-25` / `I24-adr-auth-hardening`.

## Risks / sharp edges noted

- 48 unrelated test failures appeared on local full `pytest tests/unit` once during this session; on the second run they were 3, then 0. Looks like environment / test-order flake (suspected `langsmith`/`pydantic_core` `typing_extensions` collision when system Python leaks in). CI is clean; the local flake hasn't been reproduced reliably enough to triage.
- `gh pr checks --json` is not supported on this machine's `gh` version; do not build polling loops around it. Use `gh pr checks --watch` or `gh api .../check-runs --jq ...`.
- Pre-commit hook is wired and works (`.git/hooks/pre-commit`, runs ruff via `python3 -m ruff`).
