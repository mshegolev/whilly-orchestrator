# Session Handoff — 2026-05-19

PRD scope: [`docs/PRD-post-auth-hardening.md`](../docs/PRD-post-auth-hardening.md)
Plan file: [`.planning/post-auth-hardening-tasks.json`](post-auth-hardening-tasks.json)
ADR: [`docs/adr/ADR-001-auth-hardening-p1.md`](../docs/adr/ADR-001-auth-hardening-p1.md)

## TL;DR

Post-auth-hardening sprint **functionally complete**. 24 PRs shipped across
2 sessions (2026-05-18 → 2026-05-19). Plan state: `done=23, skipped=4,
human_loop=1, pending=1`. Only operator-blocking work remains.

## Plan state at handoff

```
done       = 23
skipped    =  4   (A1a, A1b, E15, E17)
human_loop =  1   (G19 — needs operator)
pending    =  1   (G20 — hard-blocked on G19)
total      = 29
```

## What shipped today (2026-05-19, 22 PRs)

Continued from the 2026-05-18 handoff (which left `done=3, pending=23`).
Autopilot pass took the sprint to functional completion.

### Wave 1 — pre-autopilot warmup (4 PRs)

| PR | Branch | What |
|---|---|---|
| [#276](https://github.com/mshegolev/whilly-orchestrator/pull/276) | `feat-e14a-totp-migration` | E14a: Alembic **024** `user_totp_secrets` + `pyotp` optional extras |
| [#277](https://github.com/mshegolev/whilly-orchestrator/pull/277) | `feat-d11-auth-audit-migration-and-repo` | D11: Alembic **025** `auth_audit` table + async `insert_attempt` repo |
| [#278](https://github.com/mshegolev/whilly-orchestrator/pull/278) | `feat-c6-must-change-gate-middleware` | C6: per-request `must_change_password` gate middleware + 12 tests |
| [#279](https://github.com/mshegolev/whilly-orchestrator/pull/279) | `feat-d9-self-service-password-change` | D9: voluntary `/me/password` self-service routes + 8 tests |

### Wave 2 — autopilot parallels (5 PRs)

| PR | Branch | What |
|---|---|---|
| [#280](https://github.com/mshegolev/whilly-orchestrator/pull/280) | `feat-b4-worker-launch-unit-tests` | B4: 22 unit tests for `whilly worker launch/list/remove` |
| [#281](https://github.com/mshegolev/whilly-orchestrator/pull/281) | `feat-b3-reset-endpoint-tests` | B3: 9 unit tests for `tasks_api_crud` reset endpoints |
| [#282](https://github.com/mshegolev/whilly-orchestrator/pull/282) | `feat-b5-session-persistence-test` | B5: integration test for `WHILLY_DASHBOARD_TOKEN_SECRET` survives restart |
| [#283](https://github.com/mshegolev/whilly-orchestrator/pull/283) | `feat-c8-cluster-rate-limit-warning` | C8: cluster-aware rate limiter (NullRateLimiter + RedisRateLimiter) |
| [#284](https://github.com/mshegolev/whilly-orchestrator/pull/284) | `feat-c12-smtp-mailer` | C12: SMTP magic-link transport with event-log fallback (`aiosmtplib`) |

### Wave 3 — D-chain (4 PRs)

| PR | Branch | What |
|---|---|---|
| [#285](https://github.com/mshegolev/whilly-orchestrator/pull/285) | `feat-d10-admin-users-ui` | D10: admin user-management UI + `auth_audit` paginated browse |
| [#286](https://github.com/mshegolev/whilly-orchestrator/pull/286) | `feat-d10b-auth-audit-instrumentation` | D10b: instrument `submit_login` with `auth_audit.insert_attempt` |
| [#287](https://github.com/mshegolev/whilly-orchestrator/pull/287) | `feat-d13-startup-route-audit` | D13: opt-in startup route audit (`WHILLY_ENABLE_ROUTE_AUDIT=1`) |
| [#288](https://github.com/mshegolev/whilly-orchestrator/pull/288) | `chore-plan-skip-e-chain-stretch` | Skip E14b/E15/E17 (later un-skipped E14b after re-scope) |

### Wave 4 — E16 + first-round skips (3 PRs)

| PR | Branch | What |
|---|---|---|
| [#289](https://github.com/mshegolev/whilly-orchestrator/pull/289) | `feat-e16-active-sessions-ui` | E16: `/me/sessions` active-sessions UI with per-device revoke |
| [#290](https://github.com/mshegolev/whilly-orchestrator/pull/290) | `feat-f18b-worker-tag-claim` (skip-only) | Skip F18b + I23 (later un-skipped after re-scope) |
| [#291](https://github.com/mshegolev/whilly-orchestrator/pull/291) | `feat-h21-worker-launch-model-connect-override` | H21: `whilly worker launch --model`/`--connect` override fix |

### Wave 5 — second-round skips + ADR (2 PRs)

| PR | Branch | What |
|---|---|---|
| [#292](https://github.com/mshegolev/whilly-orchestrator/pull/292) | `chore-final-skip-a2-h22` (skip-only) | Skip A2 + H22 (later un-skipped after re-scope) |
| [#293](https://github.com/mshegolev/whilly-orchestrator/pull/293) | `feat-i24-adr-auth-hardening` | I24: `docs/adr/ADR-001-auth-hardening-p1.md` Nygard-format ADR |

### Wave 6 — re-scoped extension (operator authorised quick-wins) (6 PRs)

| PR | Branch | What |
|---|---|---|
| [#294](https://github.com/mshegolev/whilly-orchestrator/pull/294) | `feat-f18b-tag-filter-sql` | F18b SQL slice: `<@` worker-tag filter in `_CLAIM_SQL` + 3 tests |
| [#295](https://github.com/mshegolev/whilly-orchestrator/pull/295) | `feat-h22-worker-bootstrap` | H22: `whilly worker bootstrap` convenience command + 7 tests |
| [#296](https://github.com/mshegolev/whilly-orchestrator/pull/296) | `feat-a2-post-auth-smoke-test` | A2: post-auth-journey integration smoke test |
| [#297](https://github.com/mshegolev/whilly-orchestrator/pull/297) | `feat-i23-usage-docs-refresh` | I23: `Whilly-Usage.md` §Remote-worker setup |
| [#298](https://github.com/mshegolev/whilly-orchestrator/pull/298) | `feat-e14b-totp-routes` | E14b: TOTP second-factor routes (flag-gated, default OFF) + 14 tests |
| [#299](https://github.com/mshegolev/whilly-orchestrator/pull/299) | `fix-e14b-pyotp-importorskip` | Hotfix: `pytest.importorskip('pyotp')` for CI |

All 22 PRs: merged via `--squash --delete-branch`. CI green on each
(except PR #298 Tests-collection-error → hotfixed in #299).

## Architectural deltas

What changed in `whilly/api/`:

- **New modules:**
  - `must_change_gate.py` — Starlette middleware (C6)
  - `auth_audit_repo.py` — async repo for the audit ledger (D11)
  - `admin_users_routes.py` — admin CRUD + audit browse routes (D10)
  - `route_audit.py` — opt-in startup route audit (D13)
  - `mailer.py` — SMTP magic-link with event-log fallback (C12)
  - `totp_routes.py` — TOTP enrolment + verify routes (E14b)
  - `totp_repo.py` — `user_totp_secrets` CRUD (E14b)

- **Extended modules:**
  - `auth_routes.py` — new `/me/password`, `/me/sessions`, change-password
    cache invalidation hook, audit instrumentation, single-line TOTP intercept
  - `sessions.py` — new `list_active_sessions_for_email` helper
  - `users_repo.py` — new `list_users`, `create_user`, `set_role`,
    `delete_user`, `reset_password_to_random`
  - `rate_limit.py` — `NullRateLimiter`, `RedisRateLimiter`, `build_rate_limiter`
    factory, `install_rate_limiter` swap helper
  - `csrf.py` — unchanged; new gate registered BEFORE it so CSRF stays outermost

- **Templates added** (all registered in `OPERATOR_WUI_ARTIFACTS`):
  - `me_password.html.j2`, `me_sessions.html.j2`
  - `admin_users.html.j2`, `admin_auth_audit.html.j2`
  - `totp_setup.html.j2`, `totp_verify.html.j2`

- **Migrations** on main, head = `025_auth_audit`:
  - 023 `worker_tags` (F18a, pre-session)
  - 024 `user_totp_secrets` (E14a)
  - 025 `auth_audit` (D11)

- **`pyproject.toml` extras added:**
  - `[totp] = ["pyotp>=2.9"]` (E14a)
  - `server` gained `aiosmtplib>=3.0` (C12)

- **Env vars introduced/documented this sprint:**
  - `WHILLY_TOTP_ENABLED` (E14b, default OFF — instant rollback)
  - `WHILLY_ENABLE_ROUTE_AUDIT` (D13, default OFF)
  - `WHILLY_SKIP_ROUTE_AUDIT` (D13, override)
  - `WHILLY_NUM_WORKERS` (C8)
  - `WHILLY_REDIS_URL` (C8)
  - `WHILLY_SMTP_HOST`/`PORT`/`USER`/`PASSWORD`/`FROM` (C12)

## What's left

### `human_loop` — needs operator (1 task)

**G19** — publish `mshegolev/claude-anonymizer` as a public GitHub repo
from `/opt/develop/qa-team/claude-anonymizer/`. Autopilot can't do this:
needs operator's GitHub credentials + a "yes, this is OK to publish"
decision. See the task description in
[`.planning/post-auth-hardening-tasks.json`](post-auth-hardening-tasks.json)
for the publishing checklist.

### `pending` — hard-blocked (1 task)

**G20** — `.github/workflows/ci.yml` for the published anonymizer repo.
Can't be done until G19 lands (the workflow lives in a different repo
that doesn't exist yet).

### `skipped` — deferred follow-up work (4 tasks)

These were intentionally not attempted in the autopilot run. Pick them
up in focused PRs when there's time + the right context:

- **A1a / A1b** — original `build_auth_router` `RuntimeError` defect
  didn't reproduce. Nothing to do; status stays skipped per
  truthful-update policy from the 2026-05-18 handoff.
- **E15** — WebAuthn / passkeys. PRD §Risk Register R3 + Item 15 prose
  both explicitly recommend "save for dedicated sprint". Complex
  protocol, depends on E14b's session state machine (now built); future
  sprint inherits the pending-cookie + intercept pattern.
- **E17** — OIDC header trust. PRD R3 "Critical impact" header-injection
  attack surface. Mandates security review per PRD R3 mitigation;
  beyond safe autopilot scope. Future sprint: implement with
  `WHILLY_TRUSTED_PROXY_IPS` enforcement + explicit security review.

### Known issues / sharp edges

- **3 pre-existing test-order flakes** still present on local full
  `pytest tests/unit/`:
  - `test_prompt_sanitizer_wiring.py::test_jira_full_pipeline_produces_fenced_task_description`
  - `test_qa_release_collector.py::test_collect_release_context_fetches_linked_issues_and_artifacts`
  - `test_qa_release_collector.py::test_collect_release_context_warns_when_remote_links_fail`

  All three pass cleanly in isolation. The 2026-05-18 handoff diagnosed
  these as `langsmith` / `pydantic_core` / `typing_extensions` collision
  when system Python leaks in. **CI is unaffected** — the fresh runner
  has a clean Python install. Worth re-triaging when a maintainer next
  touches those modules, but not blocking anything.

- **`docs/distributed-audit/readiness-validation.md` and
  `research-findings.md`** were dirty in the working tree at session
  start and remained untouched. Pre-session work that needs a
  maintainer decision (commit / discard / amend).

- **F18b register-side plumbing** still pending. The `<@` SQL filter
  (PR #294) is live in `_CLAIM_SQL` but every worker today has
  `tags=[]` so the filter is a no-op until a future PR adds:
  - `tags` field on `RegisterRequest` schema
  - server-side `register_worker` to persist tags
  - client `register()` signature
  - CLI `--tags` persistence in `worker_launch`
  Tracked in the F18b realisation note.

- **D13 route audit is opt-in** because many existing routes use inline
  `_authenticate_session` calls that the dependant-walk can't see. Flip
  to default-on after the route layer is refactored to Depends-style
  auth — listed as future direction in
  [`docs/adr/ADR-001-auth-hardening-p1.md`](../docs/adr/ADR-001-auth-hardening-p1.md).

## How to start the next session

```bash
cd /opt/develop/whilly-orchestrator
git checkout main && git pull --ff-only origin main
cat .planning/SESSION-HANDOFF-2026-05-19.md   # this file
cat docs/adr/ADR-001-auth-hardening-p1.md     # sprint summary in Nygard format

# Plan introspection — what's ready vs skipped vs human-blocked
cat .planning/post-auth-hardening-tasks.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
from collections import Counter
print('STATE:', dict(Counter(t['status'] for t in d['tasks'])))
for s in ('pending', 'human_loop'):
    print(f'\n{s}:')
    for t in d['tasks']:
        if t['status'] == s:
            print(f'  {t[\"id\"]}')
"
```

### If picking up G19 (publish anonymizer)

```bash
# Inspect what would be published — confirm no secrets, no internal-only refs.
cd /opt/develop/qa-team/claude-anonymizer
git log --oneline
ls -la

# When ready, create the public GitHub repo via gh CLI:
gh repo create mshegolev/claude-anonymizer --public --source=. --remote=origin --push
```

After G19 lands, G20 (the GitHub Actions CI yml) is a ~15-minute follow-up
in the new repo.

### If picking up E15 (WebAuthn) or E17 (OIDC)

Both need explicit security review per PRD R3. Start with the PRD prose:

```bash
grep -B 2 -A 25 "Item 15\|Item 17\|R3" docs/PRD-post-auth-hardening.md
```

E15 can build on E14b's pending-cookie pattern in
[`whilly/api/totp_routes.py`](../whilly/api/totp_routes.py) — the
state-machine extension is the same shape.

E17 must NOT ship without `WHILLY_TRUSTED_PROXY_IPS` enforcement +
mandatory security-review PR comment per R3 mitigation.

## Verification commands

```bash
# Full unit suite (expect 3 pre-existing flakes in full sweep; isolation passes)
.venv/bin/python -m pytest tests/unit/ -q

# Auth-related tests in isolation
.venv/bin/python -m pytest tests/unit/test_must_change_gate.py \
                            tests/unit/test_me_password_routes.py \
                            tests/unit/test_me_sessions_routes.py \
                            tests/unit/test_admin_users_routes.py \
                            tests/unit/test_auth_audit_instrumentation.py \
                            tests/unit/test_route_audit.py \
                            tests/unit/test_totp_routes.py \
                            tests/unit/test_mailer.py \
                            tests/unit/test_rate_limit_cluster.py \
                            tests/unit/test_cli_worker_launch.py \
                            tests/unit/test_cli_worker_bootstrap.py -v

# Lint + type-check + arch
.venv/bin/python -m ruff check whilly/ tests/
.venv/bin/python -m ruff format --check whilly/ tests/
.venv/bin/python -m mypy --strict whilly/core
.venv/bin/lint-imports

# Alembic head
.venv/bin/alembic -c alembic.ini heads   # → 025_auth_audit (head)

# Integration tests (require Docker; auto-skip otherwise)
.venv/bin/python -m pytest tests/integration/test_session_persistence.py \
                            tests/integration/test_post_auth_smoke.py -v
```

## Risks / sharp edges for the next operator

- **`WHILLY_TOTP_ENABLED=1` flip is the riskiest env-var change.** It
  extends the session state machine for every login. Test in staging
  first; flipping back to off is an instant rollback (the router
  doesn't load and `submit_login`'s intercept becomes a no-op).
- **`WHILLY_ENABLE_ROUTE_AUDIT=1` will refuse to start** if any new
  route ships without Depends-style auth or a whitelist entry. Keep
  this in mind when reviewing PRs that add new routes.
- **`WHILLY_NUM_WORKERS > 1` without `WHILLY_REDIS_URL`** is fail-open
  with a WARNING — monitor startup logs.

## Files touched in the wider planning surface

- [`.planning/post-auth-hardening-tasks.json`](post-auth-hardening-tasks.json) —
  every task carries a `DONE 2026-05-{18,19}: ...` realisation note or
  `SKIPPED 2026-05-{18,19}: ...` rationale block.
- [`docs/adr/ADR-001-auth-hardening-p1.md`](../docs/adr/ADR-001-auth-hardening-p1.md) —
  the canonical sprint summary; reference it from any future PR that
  revisits one of the P1.X decisions.
- [`docs/PRD-post-auth-hardening.md`](../docs/PRD-post-auth-hardening.md) —
  unchanged from session start. The ADR captures the realised state;
  the PRD remains the historical pre-sprint scoping doc.
