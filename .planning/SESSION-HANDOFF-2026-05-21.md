# Session Handoff — 2026-05-21

PRD scope: [`docs/PRD-post-auth-hardening.md`](../docs/PRD-post-auth-hardening.md)
Plan file: [`.planning/post-auth-hardening-tasks.json`](post-auth-hardening-tasks.json)
ADR: [`docs/adr/ADR-001-auth-hardening-p1.md`](../docs/adr/ADR-001-auth-hardening-p1.md)
Prev handoff: [`SESSION-HANDOFF-2026-05-19.md`](SESSION-HANDOFF-2026-05-19.md)

## TL;DR

Started from the 2026-05-19 handoff's leftovers (the "3 pre-existing flakes,
not blocking" + the dirty `docs/` files). Those turned out to be **two real,
local-only test-hygiene bugs**, both fixed. Then closed the deferred
`{% raw %}` divergence, and — under explicit security review — implemented
**E17 (OIDC reverse-proxy header-trust)**. Plan is now `done=26, skipped=3`
(only A1a/A1b/E15). `main` is clean, no open PRs.

## What shipped this session (5 PRs merged)

| PR | What |
|---|---|
| [#303](https://github.com/mshegolev/whilly-orchestrator/pull/303) | Killed the "3 flakes" (a local `.env`→`os.environ` leak flipping `JiraAuth.verify_ssl`) via an autouse env snapshot/restore in `tests/unit/conftest.py`; stopped `test_m1_baseline_fixtures_script_is_idempotent_on_rerun` from corrupting `docs/` by routing it through the `WHILLY_M1_BASELINE_ROOT` synthetic-repo isolation. |
| [#304](https://github.com/mshegolev/whilly-orchestrator/pull/304) | `m1_baseline_fixtures.py` now Liquid-escapes markdown when mirroring into the Jekyll-published `docs/distributed-audit/` (`escape_liquid_for_jekyll`), so a manual script run no longer strips the `{% raw %}` escapes. Reproduces f6071f4 exactly → `escape(library) == committed docs` byte-for-byte. |
| [#305](https://github.com/mshegolev/whilly-orchestrator/pull/305) | E15/E17 security-design plan: [`.planning/E15-E17-auth-security-design.md`](E15-E17-auth-security-design.md) (doc-only review artifact). |
| [#306](https://github.com/mshegolev/whilly-orchestrator/pull/306) | **E17 — OIDC header-trust** (flag-gated, default OFF). New `whilly/api/oidc_header_auth.py`; `_authenticate_session` honours `request.state.proxy_principal`; conditional innermost mount in `create_app`; 15 tests. Reviewer decisions recorded in ADR-001 §P1.6. |

(Plus the ADR §P1.6 addendum + design-doc §3.5 resolution were squashed into #306.)

## E17 — what to know before touching it

- **Default OFF.** When `WHILLY_TRUST_PROXY_AUTH` is unset/`0`, the middleware
  is **not mounted**, so `request.state.proxy_principal` is never set and the
  new branch in `_authenticate_session` is a provable no-op.
- **Real app factory is `whilly/adapters/transport/server.py::create_app`**,
  not `whilly/api/main.py` (the PRD/plan said `main.py` — stale). Config is
  resolved there with `ProxyHeaderAuthConfig.from_env()` at construction time
  (fail-closed: empty/invalid allowlist → app refuses to start).
- **Trust is on the DIRECT peer IP** (`request.client.host`), never
  `X-Forwarded-For`. Allowlist is `WHILLY_TRUSTED_PROXY_IPS` (CIDR list).
- **⚠️ Operational gate (separate from the merge gate):** do NOT set
  `WHILLY_TRUST_PROXY_AUTH=1` in any deployment until (a) the reverse proxy is
  confirmed to strip any client-supplied `X-Forwarded-User`, and (b)
  `WHILLY_TRUSTED_PROXY_IPS` is set to the proxy's CIDR(s). See
  [ADR-001 §P1.6](../docs/adr/ADR-001-auth-hardening-p1.md) +
  [`.env.example`](../.env.example).
- **Resolved review questions** (ADR-001 §P1.6): proxy identity keeps full role
  from the `users` row (not read-only); `must_change_password` is bypassed by
  design (SSO password lifecycle is the proxy's); no conflict with
  `WHILLY_ENABLE_ROUTE_AUDIT=1` (route audit walks routes, header-trust adds
  none). **Still open:** trusted-hop count (single proxy assumed; chained
  proxies would need a documented `num_trusted_hops`).

## What's left

- **E15 (WebAuthn / passkeys)** — `skipped`, **held for a dedicated sprint**.
  Blocker on *this* machine: the `webauthn` PyPI package can't be installed
  (no network / PyPI access), so the ceremony code can't be run or verified —
  do it where PyPI + CI are available. Full plan in
  [`E15-E17-auth-security-design.md`](E15-E17-auth-security-design.md) §2; it
  reuses the E14b TOTP pending-cookie state machine. New migration would be
  `026_webauthn_credentials` (head is `025_auth_audit`; the PRD's "025" is
  stale). Gate tests with `pytest.importorskip("webauthn")`.
- **A1a / A1b** — `skipped`; original `build_auth_router` defect never
  reproduced. Nothing to do.

## How to start the next session

```bash
cd /opt/develop/whilly-orchestrator
git checkout main && git pull --ff-only origin main
cat .planning/SESSION-HANDOFF-2026-05-21.md   # this file

# Plan state
cat .planning/post-auth-hardening-tasks.json | python3 -c "
import json,sys; from collections import Counter
d=json.load(sys.stdin); print('STATE:', dict(Counter(t['status'] for t in d['tasks'])))
print('skipped:', [t['id'] for t in d['tasks'] if t['status']=='skipped'])"
```

### If picking up E15 (only where webauthn installs)
```bash
grep -B2 -A30 "## 2. E15" .planning/E15-E17-auth-security-design.md
pip install '.[webauthn]'   # add the extra to pyproject first
```

## Verification commands

```bash
.venv/bin/python -m pytest tests/unit/ -q          # 2318 passed, 3 skipped (deterministic)
.venv/bin/python -m pytest tests/unit/test_oidc_header_auth.py -v   # 15 E17 tests
.venv/bin/python -m ruff check whilly/ tests/
.venv/bin/python -m ruff format --check whilly/ tests/
.venv/bin/lint-imports                              # 2 contracts kept
.venv/bin/alembic -c alembic.ini heads              # 025_auth_audit (head)
```

## Sharp edges / notes for the next operator

- **`tests/unit/conftest.py` now snapshots/restores `os.environ` per test.**
  This is the guard that keeps a local `.env` (which `run_run_command` loads
  via `load_dotenv`) from leaking `JIRA_VERIFY_SSL=false` (and a real Jira
  token) into later tests. Scoped to `tests/unit/` only — `tests/conftest.py`
  has session-scoped fixtures that set `DOCKER_HOST`/`WHILLY_DATABASE_URL`, so
  a per-test restore there would wipe them mid-session. Don't move it up.
- **Don't run `m1_baseline_fixtures.py` expecting it to be a no-op on an old
  checkout** — pre-#304 it strips the Jekyll escapes from `docs/`. On `main`
  it's now idempotent (escapes preserved).
- **CI is green-but-blind to local-only issues**: both bugs fixed this session
  were invisible on CI (no `.env`, ephemeral checkout). Run the full
  `pytest tests/unit/` locally before trusting "CI passed".
