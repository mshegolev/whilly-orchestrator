# PRD: Post-Auth-Hardening Follow-Up Cycle

**Version:** 1.0  
**Date:** 2026-05-16  
**Author:** Mikhail Shchegolev  
**Status:** Draft  
**Whilly version at baseline:** 4.7.0-37fc1f5  

---

## 1. Overview

The auth-hardening Phase 1 sprint (P1.1–P1.5) landed five features on `main`: a forced-password-change gate (`must_change_password`), IP rate-limiting with account lockout, a `WHILLY_PROD_MODE` umbrella switch, SSE cookie authentication, and prod-mode validators. A concurrent hotfix added the task-reset endpoint (`POST /api/v1/tasks/{id}/reset`) and its Reset button in the WUI. These changes left CI red: `test_health_*` in `tests/unit/test_transport_server.py` fails because `build_auth_router` (line 163, `whilly/api/auth_routes.py`) registers `/auth/change-password` with a parameter FastAPI cannot introspect at startup.

This PRD covers the 24 follow-up work items needed to restore CI, close the coverage gap created by the sprint, polish the auth surface to production-ready quality, and deliver Phase 2/3 auth features, architectural extensions, and documentation. The cycle runs in priority order: P0 unblocks the trunk; P1 closes security and test debt; P2 delivers user-facing features; P3 completes stretch goals and governance docs.

---

## 2. Goals

- Restore `main` CI to green within one working session (P0).
- Achieve ≥ 90 % branch coverage on all code added during the auth-hardening sprint (P1).
- Deliver a production-hardened auth layer: per-request gate, persistent session secret, cluster-aware rate limiting, self-service password change, admin user management, login audit, and SMTP magic-link transport (P1/P2).
- Ship the worker tag-pool filter (hybrid pool model) so operators can route tasks to specialist workers (P2).
- Publish the `claude-anonymizer` as a standalone GitHub project with its own CI (P2).
- Fix CLI UX regressions introduced by `worker launch` (`--model` and `--connect` ignored on update paths) (P2).
- Produce accurate end-user and architecture documentation reflecting the hardened state (P3).

---

## 3. Non-Goals

- Redesigning task-claim semantics beyond the tag-filter extension (item 18).
- Replacing Postgres with another datastore.
- Migrating authentication to an external IdP (Keycloak, Okta). OIDC SSO (item 17) trusts a reverse-proxy identity header only — no OAuth flows.
- Building a full RBAC permission matrix. Role='admin' vs. role='user' is the only distinction in scope.
- Redesigning the `whilly` CLI interactive menu (`whilly` with no args).
- Adding multi-tenant plan isolation.

---

## 4. Personas

| Persona | Description | Primary concerns |
|---|---|---|
| **Operator** | Homelab single-user deployment. Manages Postgres, runs `whilly server start`, accesses the WUI from localhost. | CI green, `must_change_password` not blocking their own workflow, low operational overhead. |
| **Admin** | Multi-user shared deployment. Creates/manages user accounts, reviews login audit logs, decides who can access which plans. | User CRUD, audit trail, forced-password policy for new accounts. |
| **Worker-host owner** | Provisions remote agent boxes via `whilly worker launch`. May tag workers for specialisation (GPU, code-signing key, network egress). | `--model` and `--connect` overrides working reliably, `whilly worker bootstrap` automating first-time setup. |

---

## 5. Epic Breakdown

### Epic A — CI Green Again (P0, blocks all other epics)

#### Item 1 — Fix `build_auth_router` FastAPI parameter parse failure

**Goal:** Make `tests/unit/test_transport_server.py::test_health_*` (and all remaining unit tests) pass without skips.

**Scope:** Diagnose the exact line in `whilly/api/auth_routes.py:163+` where FastAPI's `dependencies/utils.py:120` raises `RuntimeError`. Likely candidates: a `Form(...)` parameter missing its `fastapi.Form` import, a missing return-type annotation, or a bare `Optional` not unwrapped under Python 3.10. Apply the minimal fix; do not restructure `build_auth_router`.

**Files to touch:** `whilly/api/auth_routes.py` (fix only); `tests/unit/test_transport_server.py` if a fixture needs updating.

**Acceptance criteria:**
- `pytest tests/unit/test_transport_server.py -q` exits 0 with no skips.
- `ruff check whilly/api/auth_routes.py` reports no errors.
- The fix is ≤ 20 lines changed.

**Risk:** Low. Root cause is a missing annotation or import; fix is mechanical once identified.

**Dependencies:** None — this is the entry point.

---

#### Item 2 — Full smoke test: login → change-password → reset task → worker claims task

**Goal:** Verify the end-to-end user journey introduced by the sprint works as an integrated system.

**Scope:** A single Playwright + httpx script (or pytest integration test) that: (a) POSTs `/auth/login`, (b) follows the redirect to `/auth/change-password`, (c) POSTs `/auth/change-password`, (d) navigates to the tasks table, (e) clicks Reset on a `failed` task, (f) calls `whilly worker launch <plan>` via subprocess, (g) confirms the worker registers and the reset task transitions to `in_progress`.

**Files to touch:** `tests/integration/test_post_auth_smoke.py` (new file).

**Acceptance criteria:**
- Test passes against a live server with `WHILLY_PROD_MODE=false` and a seeded user with `must_change_password=True`.
- CI executes it in the `integration` mark group (not in default `pytest -q`).
- Worker claim confirmed by polling `/api/v1/tasks/{id}` until `status == "in_progress"` with timeout 30s.

**Risk:** Medium. Requires coordinating live server, Postgres seed data, and a real worker subprocess — flaky if timing is tight. Mitigate with generous timeouts and a dedicated fixture that pre-seeds the DB.

**Dependencies:** Item 1 (CI must be green first).

---

### Epic B — Test Coverage for New Code (P1)

#### Item 3 — Unit tests: `tasks_api_crud.reset_preview_endpoint` and `reset_task_endpoint`

**Goal:** Cover the reset endpoint logic introduced by the WUI hotfix.

**Scope:** Tests for: 200 happy path (`pending` task resets to `pending`), 404 unknown task, 409 task currently `in_progress` claimed by a worker, 400 wrong status (e.g., already `pending`), cascade list shape (list of cascaded subtask IDs returned), and no-mapping cascade (task with no dependents).

**Files to touch:** `tests/unit/test_tasks_api_crud.py` (new or extend existing). Mock the DB pool using `AsyncMock` to avoid Postgres dependency in unit tier.

**Acceptance criteria:**
- 6 parametrized test cases, all passing.
- Branch coverage on `tasks_api_crud.py` reaches ≥ 85 % (measured by `pytest --cov=whilly/api/tasks_api_crud`).

**Risk:** Low.

**Dependencies:** Item 1.

---

#### Item 4 — Unit tests: `whilly/cli/worker_launch.py`

**Goal:** Cover the `worker launch`, `worker list`, and `worker remove` sub-commands.

**Scope:** Test cases: `launch` with a new config (registers, writes `~/.config/whilly/worker.json`), `launch` reuse path (existing config, no re-registration), `list` table output, `list --json` output, `remove` single match, `remove` with ambiguous name (prompts or errors), `remove --all`, reading `.env` file from disk, and the three bootstrap-token decode paths (base64url, hex, utf-8 fallback).

**Files to touch:** `tests/unit/test_cli_worker_launch.py` (extend existing or new). Use `tmp_path` fixture for config isolation.

**Acceptance criteria:**
- ≥ 10 test cases covering all branches listed above.
- No real network calls — mock `httpx.AsyncClient` or the underlying `WorkerClient`.

**Risk:** Low.

**Dependencies:** Item 1.

---

#### Item 5 — Integration test: persistent session secret round-trip

**Goal:** Confirm that `WHILLY_DASHBOARD_TOKEN_SECRET` makes session cookies survive a server restart.

**Scope:** Pytest integration test that (a) boots the server with a fixed `WHILLY_DASHBOARD_TOKEN_SECRET`, (b) logs in and captures the `whilly_session` cookie, (c) stops and restarts the server with the same secret, (d) uses the cookie on a `/api/v1/tasks` request, (e) asserts HTTP 200 (not 401).

**Files to touch:** `tests/integration/test_session_persistence.py` (new).

**Acceptance criteria:**
- Test passes with and without the env var set.
- When the secret is absent, the test asserts that the cookie is rejected after restart (ephemeral secret behaviour).

**Risk:** Medium. Requires two server lifetimes in a single test; use `subprocess.Popen` with port-ready polling.

**Dependencies:** Item 1. Item 7 (secret documented in `.env.example` before this test is written).

---

### Epic C — Auth Hardening Polish (P1)

#### Item 6 — Per-request `must_change_password` gate (middleware or Depends)

**Goal:** Prevent a user with `must_change_password=True` from reaching any authenticated route other than the whitelisted set.

**Scope:** Implement as FastAPI middleware (`BaseHTTPMiddleware`) or a `Depends` injected into the session dependency. Whitelist: `/auth/change-password`, `/auth/logout`, `/health`, `/static/*`. Add an in-process TTL cache (30 s) keyed on `(session_id, password_version)` to avoid a DB round-trip on every request. Cache invalidated on password change.

**Files to touch:** `whilly/api/auth_routes.py` or new `whilly/api/must_change_gate.py`, `whilly/api/main.py` (middleware registration).

**Acceptance criteria:**
- Authenticated request to `/` with `must_change_password=True` returns HTTP 303 → `/auth/change-password`.
- Request to `/auth/change-password` with the flag set returns 200.
- After a successful change, the next request to `/` returns 200.
- Cache hit confirmed by asserting DB query count ≤ 1 for 5 rapid successive requests (use `mock.call_count`).

**Risk:** Medium. Middleware ordering with the existing CSRF middleware must be verified — incorrect ordering can break CSRF protection on the gate itself.

**Dependencies:** Item 1, Item 3.

---

#### Item 7 — Document `WHILLY_DASHBOARD_TOKEN_SECRET` in `.env.example` and `Whilly-Usage.md`

**Goal:** Eliminate operator confusion about the ephemeral-vs-persistent session secret split.

**Scope:** Add `WHILLY_DASHBOARD_TOKEN_SECRET` to `.env.example` with comment and the generator one-liner: `python -c "import secrets;print(secrets.token_urlsafe(32))"`. Add a subsection to `docs/Whilly-Usage.md` under a new "Authentication Configuration" heading covering: what the variable does, when to set it (production), rotation procedure (restart required), and security warning (treat as a signing key).

**Files to touch:** `.env.example`, `docs/Whilly-Usage.md`.

**Acceptance criteria:**
- `.env.example` contains the variable with an inline generator comment.
- `docs/Whilly-Usage.md` has ≥ 150 words on the topic with the one-liner visible in a code block.

**Risk:** Low.

**Dependencies:** None.

---

#### Item 8 — Cluster-aware rate-limit: multi-worker warning + Redis stub

**Goal:** Prevent silent under-counting of login attempts when uvicorn spawns multiple workers.

**Scope:** In `whilly/api/rate_limit.py`, detect `WHILLY_NUM_WORKERS > 1` at startup. When detected and `WHILLY_REDIS_URL` is absent, emit a `WARNING` log and disable the in-process limiter entirely (fail-open with a log, not a hard error). When `WHILLY_REDIS_URL` is set, instantiate a `RedisRateLimiter` stub that satisfies the same `RateLimiter` protocol. The stub may call `redis-py` directly; full sliding-window implementation is a stretch goal.

**Files to touch:** `whilly/api/rate_limit.py`, `whilly/api/main.py` (startup warning), `docs/Whilly-Usage.md` (new env vars).

**Acceptance criteria:**
- `WHILLY_NUM_WORKERS=4` without Redis → WARNING logged at startup, rate limiter always returns `allow=True`.
- `WHILLY_NUM_WORKERS=4` with `WHILLY_REDIS_URL=redis://localhost:6379/0` → no warning, `RedisRateLimiter` instantiated.
- Unit test: mock `os.getenv`, verify correct behaviour in both branches.

**Risk:** Low (stub implementation), Medium if sliding-window Redis is attempted immediately.

**Dependencies:** Item 1.

---

### Epic D — Phase 2 Auth Features (P2)

#### Item 9 — Self-service password change: `GET/POST /me/password`

**Goal:** Let any logged-in user change their own password without needing an admin reset.

**Scope:** Two new routes in `whilly/api/auth_routes.py`: `GET /me/password` renders a form with `current_password` + `new_password` + `confirm_new_password` fields. `POST /me/password` validates the current password via `users_repo.verify_credentials`, then calls `users_repo.set_password`. Clears `must_change_password` if set. CSRF protected.

**Files to touch:** `whilly/api/auth_routes.py`, new Jinja2 template `whilly/templates/me_password.html`, `whilly/api/users_repo.py` (if `verify_credentials` needs a username-only variant).

**Acceptance criteria:**
- Correct current password → 303 redirect to `/` with success flash.
- Wrong current password → 422 with form error "Current password is incorrect".
- `new_password != confirm_new_password` → 422 with appropriate error.
- Route requires an authenticated session; unauthenticated request → 303 to `/login`.
- Unit tests covering all four paths above.

**Risk:** Low.

**Dependencies:** Item 1, Item 6.

---

#### Item 10 — Admin user-management UI: `GET/POST /admin/users`

**Goal:** Allow admins to create users, change roles, reset passwords, and delete accounts without touching the DB directly.

**Scope:** New file `whilly/api/admin_users_routes.py`. Routes: `GET /admin/users` (table of all users), `POST /admin/users/create` (username, email, role, initial password → `must_change_password=True`), `POST /admin/users/{id}/role` (set role), `POST /admin/users/{id}/reset-password` (generate random password, set `must_change_password=True`), `POST /admin/users/{id}/delete`. Role guard: `Depends(require_role("admin"))` on all routes. New Jinja2 template `whilly/templates/admin_users.html`.

**Files to touch:** `whilly/api/admin_users_routes.py` (new), `whilly/api/main.py` (router include), `whilly/api/sessions.py` (add `require_role` dependency if absent), `whilly/templates/admin_users.html` (new).

**Acceptance criteria:**
- Admin can create, list, role-change, password-reset, and delete a user in end-to-end Playwright test.
- Non-admin access to any `/admin/*` route returns HTTP 403.
- Delete is idempotent (404 on second delete attempt).

**Risk:** Medium. Role dependency implementation must be consistent with the existing session model; risk of privilege escalation if guard is mis-wired.

**Dependencies:** Item 1, Item 6, Item 9.

---

#### Item 11 — `auth_audit` table and admin browse UI (migration 023)

**Goal:** Provide an auditable record of every login attempt for compliance and security review.

**Scope:** Alembic migration `023_auth_audit.py` creates table `auth_audit (id BIGSERIAL PK, ts TIMESTAMPTZ NOT NULL DEFAULT NOW(), username TEXT, ip TEXT, user_agent TEXT, outcome TEXT CHECK(outcome IN ('ok','bad_password','locked','rate_limited','missing_user')), session_id UUID)`. New `whilly/adapters/db/auth_audit_repo.py` with `insert_attempt` coroutine. Call sites: every branch of `submit_login` and `_authenticate_session`. Admin browse: `GET /admin/auth-audit?page=N&username=X` in `admin_users_routes.py`, paginated table (50 rows/page), no delete route.

**Files to touch:** `whilly/adapters/db/migrations/versions/023_auth_audit.py` (new), `whilly/adapters/db/auth_audit_repo.py` (new), `whilly/api/auth_routes.py` (instrument call sites), `whilly/api/admin_users_routes.py` (browse route + template).

**Acceptance criteria:**
- After a failed login attempt, one row exists in `auth_audit` with `outcome='bad_password'`.
- After a successful login, one row with `outcome='ok'`.
- Admin browse page renders correctly with 200 rows seeded.
- Migration is reversible (downgrade drops the table).

**Risk:** Medium. Instrumenting every login branch risks missing a path; verified by the integration smoke test (item 2).

**Dependencies:** Item 1, Item 10.

---

#### Item 12 — SMTP magic-link transport: `whilly/api/mailer.py`

**Goal:** Deliver magic-link emails via SMTP instead of only the event-log for production deployments.

**Scope:** New `whilly/api/mailer.py` with a `Mailer` class. Constructor reads `WHILLY_SMTP_HOST`, `WHILLY_SMTP_PORT` (default 587), `WHILLY_SMTP_USER`, `WHILLY_SMTP_PASSWORD`, `WHILLY_SMTP_FROM`. If `WHILLY_SMTP_HOST` is empty/unset, falls back to writing a `magic_link.sent` event to the existing event log (current behaviour). Uses `aiosmtplib` for async SMTP; no synchronous SMTP calls in a coroutine. Template: plain-text + HTML multipart.

**Files to touch:** `whilly/api/mailer.py` (new), `whilly/api/auth_routes.py` (swap event-log call with `mailer.send_magic_link`), `.env.example` (SMTP vars), `docs/Whilly-Usage.md`.

**Acceptance criteria:**
- With SMTP configured, `POST /auth/magic` triggers an `aiosmtplib.send` call (verified with `aiosmtplib.testing.SMTPNotImplementedError` mock).
- With SMTP absent, the event-log path is taken (no exception raised).
- Unit test: both paths covered.
- `aiosmtplib` added to `pyproject.toml` `[project.dependencies]`.

**Risk:** Low. Existing magic-link flow is untouched when SMTP is absent.

**Dependencies:** Item 1.

---

#### Item 13 — Startup self-test: enumerate routes and assert auth coverage

**Goal:** Make it impossible to accidentally add a public route without either adding the auth dependency or explicitly whitelisting it.

**Scope:** In `whilly/api/main.py` (or a new `whilly/api/route_audit.py`), after all routers are included, iterate `app.routes`. For each `APIRoute`: if the route's `path` is not in `_PUBLIC_WHITELIST` and none of `_AUTH_DEPENDENCIES` appear in `route.dependencies`, raise `RuntimeError` with the offending path. `_PUBLIC_WHITELIST = {"/login", "/login/magic", "/auth/login", "/auth/magic-login", "/auth/magic", "/health", "/static"}`. Execute at `startup` lifecycle event so the check runs in CI via `create_app`.

**Files to touch:** `whilly/api/main.py` or `whilly/api/route_audit.py` (new), `tests/unit/test_transport_server.py` (add test that intentionally unguarded route triggers RuntimeError).

**Acceptance criteria:**
- Adding a bare `@router.get("/secret")` with no auth dependency causes `create_app` to raise `RuntimeError("Unguarded route: /secret")`.
- All existing routes pass the check (CI green).
- The check is skippable via `WHILLY_SKIP_ROUTE_AUDIT=1` for local development convenience.

**Risk:** High. If the whitelist is incomplete, the check will break `create_app` in production at an inopportune moment. Mitigate by exhaustively testing all registered routes before merging.

**Dependencies:** Items 1, 6, 9, 10, 11, 12 (all routes must exist before audit is enabled).

---

### Epic E — Phase 3 Stretch (P3)

#### Item 14 — TOTP 2FA

**Goal:** Offer time-based one-time password as an opt-in second factor for any user.

**Scope:** `pyotp` as an optional dependency (`pip install whilly[totp]`). New Alembic migration `024_user_totp_secrets.py`: table `user_totp_secrets (user_id FK, secret TEXT, enabled BOOL, created_at TIMESTAMPTZ)`. New routes: `GET/POST /me/totp/setup` (QR code endpoint via `qrcode` lib), `GET/POST /auth/totp` (second-factor verification step after password). Session state machine: `totp_pending` intermediate state.

**Files to touch:** `whilly/api/auth_routes.py`, `whilly/adapters/db/migrations/versions/024_user_totp_secrets.py` (new), `whilly/api/totp_routes.py` (new), `pyproject.toml` (optional dep).

**Acceptance criteria:** QR code renders; TOTP code accepted within ±1 window; brute-force locked after 5 failures.

**Risk:** High. Session state machine extension risks regressions across all auth paths. Recommend feature-flag gated behind `WHILLY_TOTP_ENABLED=1`.

**Dependencies:** Items 1, 6, 9, 13.

---

#### Item 15 — WebAuthn / passkey for admins

**Goal:** Allow admin accounts to authenticate with hardware security keys or platform authenticators.

**Scope:** `webauthn` PyPI package. New routes for registration and authentication ceremonies. Admin-only.

**Files to touch:** `whilly/api/webauthn_routes.py` (new), `whilly/adapters/db/migrations/versions/025_webauthn_credentials.py` (new).

**Acceptance criteria:** End-to-end registration + authentication with a software authenticator (e.g., `py_webauthn` test vector).

**Risk:** High. Complex protocol; save for dedicated sprint.

**Dependencies:** Item 14 (shares session state machine).

---

#### Item 16 — Active-sessions UI + per-device revoke: `/me/sessions`

**Goal:** Let users see all active sessions and revoke any of them individually.

**Scope:** `GET /me/sessions` — list rows from `sessions` table filtered by user. `POST /me/sessions/{session_id}/revoke` — delete the row. Template shows device user-agent, IP, last-seen timestamp, and a Revoke button. Revoke the current session redirects to `/login`.

**Files to touch:** `whilly/api/auth_routes.py` or new `whilly/api/me_routes.py`, `whilly/templates/me_sessions.html` (new).

**Acceptance criteria:** Revoking a session ID makes the corresponding cookie return 401 on next use.

**Risk:** Low.

**Dependencies:** Items 1, 9.

---

#### Item 17 — OIDC SSO via header trust (Authelia / Tailscale)

**Goal:** Support deployments where Whilly sits behind a trusted reverse proxy that sets `X-Forwarded-User`.

**Scope:** New `whilly/api/oidc_header_auth.py`. When `WHILLY_TRUST_PROXY_AUTH=1`, inspect `X-Forwarded-User` header on every request; if present and the user exists in `users`, create a transient session (not persisted to DB). Strict: only accept the header from `WHILLY_TRUSTED_PROXY_IPS` (CIDR list). If `WHILLY_TRUST_PROXY_AUTH` is unset or `0`, the header is ignored entirely.

**Files to touch:** `whilly/api/oidc_header_auth.py` (new), `whilly/api/main.py` (conditional middleware), `.env.example`.

**Acceptance criteria:** With `WHILLY_TRUST_PROXY_AUTH=1` and a seeded user, a request with `X-Forwarded-User: alice` from a trusted IP reaches the dashboard. Same request from an untrusted IP returns 401.

**Risk:** High. Header trust is a significant security surface; incorrect IP allowlisting enables header injection. Mandatory security review before merge.

**Dependencies:** Items 1, 13.

---

### Epic F — Architectural Follow-Ups (P2)

#### Item 18 — Worker hybrid pool model: tag-based task routing

**Goal:** Enable operators to assign tasks to workers with specific capabilities (e.g., GPU, network egress, code-signing certificate).

**Scope:** Alembic migration `025_worker_tags.py` (or next available number): adds `workers.tags TEXT[] NOT NULL DEFAULT '{}'` and `tasks.required_tags TEXT[] NOT NULL DEFAULT '{}'`. Update `/tasks/claim` SQL to filter: `(tasks.required_tags <@ workers.tags OR tasks.required_tags = '{}')`. WUI: chip input for `required_tags` on task detail; chip display for `tags` on worker list. CLI: `whilly worker launch --tags gpu,signing` writes tags to config and sends on registration.

**Files to touch:** `whilly/adapters/db/migrations/versions/025_worker_tags.py` (new), `whilly/api/tasks_api.py` (claim endpoint), `whilly/api/tasks_api_crud.py` (claim query), `whilly/cli/worker_launch.py` (--tags flag), WUI templates, `docs/Whilly-Usage.md`.

**Acceptance criteria:**
- Worker with `tags=['gpu']` claims a task with `required_tags=['gpu']`.
- Worker with `tags=['gpu']` does not claim a task with `required_tags=['signing']`.
- Worker with any tags claims a task with `required_tags=[]`.
- `whilly worker launch --tags gpu,signing` persists tags to config file and sends on registration.
- Unit tests for all three claim-filter scenarios.

**Risk:** Medium. SQL array containment operator (`<@`) must be tested against the actual Postgres version; SQLite (used in some test fixtures) does not support it — ensure test tier targets Postgres or mocks the query.

**Dependencies:** Item 1. Item 4 (worker_launch tests as baseline).

---

### Epic G — claude-anonymizer Standalone (P2)

#### Item 19 — Push `claude-anonymizer` to GitHub `mshegolev/claude-anonymizer`

**Goal:** Make the Acme↔Acme redaction proxy independently usable by teams outside the Whilly project.

**Scope:** Create `mshegolev/claude-anonymizer` GitHub repository from `/opt/develop/qa-team/claude-anonymizer/`. Add `README.md`, `LICENSE` (MIT), and link from `whilly/docs/anonymizer-usage.md` (update the existing file at `docs/anonymizer-usage.md`).

**Files to touch:** `docs/anonymizer-usage.md` (add GitHub link), no files in the Whilly repo itself changed beyond the doc.

**Acceptance criteria:**
- Repository is public on GitHub.
- `docs/anonymizer-usage.md` contains a direct link to the repo.
- `README.md` in the standalone repo documents installation and usage.

**Risk:** Low.

**Dependencies:** None (independent of CI state).

---

#### Item 20 — GitHub Actions CI for `claude-anonymizer`

**Goal:** Give the standalone anonymizer its own CI matrix so contributors can validate changes without a Whilly install.

**Scope:** `.github/workflows/ci.yml` in `mshegolev/claude-anonymizer`: matrix `python-version: ["3.10", "3.11", "3.12"]`, steps: checkout, `pip install -e '.[dev]'`, `ruff check`, `ruff format --check`, `pytest -q`. Trigger: `push` and `pull_request` on `main`.

**Files to touch:** `.github/workflows/ci.yml` in the `mshegolev/claude-anonymizer` repo.

**Acceptance criteria:**
- CI passes on all three Python versions on the initial commit.
- Badge added to `README.md`.

**Risk:** Low.

**Dependencies:** Item 19.

---

### Epic H — CLI UX Polish (P2)

#### Item 21 — Fix `whilly worker launch --model X` and `--connect` ignored on update path

**Goal:** Ensure that repeated `whilly worker launch` calls with new flags actually update the saved config.

**Scope:** In `whilly/cli/worker_launch.py`, the `launch` command uses `dict.setdefault` when merging CLI args into the loaded config, which is a no-op when the key already exists. Replace with explicit overwrite for keys supplied via CLI flags (`--model`, `--connect`, `--tags` once item 18 lands). A CLI flag that is left at its default value (not explicitly supplied by the user) must NOT overwrite the saved value.

**Files to touch:** `whilly/cli/worker_launch.py`.

**Acceptance criteria:**
- `whilly worker launch plan.json --model claude-opus-4-6` on an existing config updates `default_model` in `~/.config/whilly/worker.json`.
- `whilly worker launch plan.json` (no `--model`) does NOT overwrite the existing `default_model`.
- Unit test: assert config after second launch with new `--model` reflects the new value.

**Risk:** Low.

**Dependencies:** Item 4 (worker_launch unit tests must be in place first).

---

#### Item 22 — `whilly worker bootstrap` one-shot first-run script

**Goal:** Reduce new worker-host setup from a multi-step manual process to a single command.

**Scope:** New sub-command `whilly worker bootstrap`. Steps: (1) verify Python 3.10+ and `pip`, (2) `pip install whilly` if not installed, (3) prompt for server URL and bootstrap token, (4) call `whilly worker launch` logic to register and write config, (5) run `claude --version` (or `CLAUDE_BIN --version`) and print result, (6) print a summary with the config file path. Designed to be piped from a remote `curl | bash` style install page but also runnable interactively.

**Files to touch:** `whilly/cli/worker_launch.py` (add `bootstrap` sub-command) or new `whilly/cli/worker_bootstrap.py`.

**Acceptance criteria:**
- Running `whilly worker bootstrap` on a box with no config completes without manual edits to JSON.
- Running it a second time on a configured box detects the existing config and prompts before overwriting.
- `--non-interactive` flag accepts all inputs via env vars (`WHILLY_SERVER_URL`, `WHILLY_BOOTSTRAP_TOKEN`).

**Risk:** Low.

**Dependencies:** Items 1, 21.

---

### Epic I — Documentation (P3)

#### Item 23 — Refresh `docs/Whilly-Usage.md`

**Goal:** Bring the primary operator reference up to date with everything shipped since v4.5.

**Scope:** Add sections (or update existing): `whilly worker launch / list / remove` with all flags; the WUI task Reset button and its API endpoint; `WHILLY_DASHBOARD_TOKEN_SECRET` (cross-reference item 7); `WHILLY_PROD_MODE` semantics; `WHILLY_NUM_WORKERS` and `WHILLY_REDIS_URL`; and the `must_change_password` initial-login flow for new users.

**Files to touch:** `docs/Whilly-Usage.md`.

**Acceptance criteria:**
- Every env var added since v4.5 is documented with type, default, and example value.
- `whilly worker` sub-commands have at least one usage example each.
- No references to features removed in v3→v4 migration remain (grep for `USE_WORKSPACE` in user-facing prose).

**Risk:** Low.

**Dependencies:** Items 7, 8, 18, 21, 22 should be merged first so docs are accurate.

---

#### Item 24 — ADR for auth-hardening design decisions

**Goal:** Capture the rationale behind P1.1–P1.5 for future maintainers, security reviewers, and the compliance record.

**Scope:** Create `docs/adr/` directory. Write `docs/adr/ADR-001-auth-hardening-p1.md` covering: context (single-user homelab growing into multi-user deployments), decisions made (per-request gate vs. token expiry, in-process rate limiter vs. Redis, SSE URL-token deprecation in favour of cookie, `WHILLY_PROD_MODE` umbrella), rejected alternatives, and consequences.

**Files to touch:** `docs/adr/ADR-001-auth-hardening-p1.md` (new), `docs/` index (link the ADR from `index.md` if it exists).

**Acceptance criteria:**
- ADR follows the standard Nygard format (Status, Context, Decision, Consequences).
- All five P1 hardening decisions (P1.1–P1.5) are addressed.
- Reviewed and acknowledged by at least one other contributor (comment in PR).

**Risk:** Low.

**Dependencies:** Items 1–8 merged so the decisions are final and the ADR is accurate.

---

## 6. Acceptance Summary

All items must satisfy: `ruff check` + `ruff format --check` clean; no new `# type: ignore` without inline justification; migration downgrades tested; no hard-coded credentials in test fixtures.

Individual per-item acceptance criteria are listed in Section 5 above.

---

## 7. Dependencies

```
Item 1 (CI fix)
  └── Item 2 (smoke test)
  └── Items 3, 4 (unit tests, can run in parallel)
  └── Item 5 (session persistence — also needs Item 7)
  └── Item 6 (must_change gate — also needs Item 3)
      └── Items 9, 10 (self-service + admin UI, can run in parallel)
          └── Item 11 (audit — also needs Item 10)
          └── Item 13 (route audit — needs Items 9,10,11,12)
              └── Items 14, 17 (TOTP, OIDC — P3 gated on 13)
                  └── Item 15 (WebAuthn — needs 14)
  └── Item 8 (rate-limit — independent of 6, parallel with 3/4)
  └── Item 12 (mailer — independent, parallel with 3/4)
  └── Item 16 (sessions UI — needs Item 9)
Items 19, 20 — independent of CI state (parallel)
Item 21 (--model fix)
  └── Item 22 (bootstrap — needs 21)
Item 4 (worker_launch tests)
  └── Item 18 (tag pool — also needs Item 1)
Items 23, 24 — documentation, run last; Item 24 needs Items 1–8 stable
```

---

## 8. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Route audit (item 13) breaks `create_app`** if whitelist is incomplete at the time of merge | Medium | High (CI + production deploy blocked) | Enable behind `WHILLY_SKIP_ROUTE_AUDIT=1`; merge only after exhaustive route enumeration test passes. |
| R2 | **Parallel auth-route PRs cause merge conflicts** in `auth_routes.py` | High | Medium | Items 6, 9, 10, 11, 12 all touch `auth_routes.py`. Serialize them as a stack: 6 → 9 → 10 → 11 → 12, each rebased on the previous. |
| R3 | **OIDC header trust (item 17) introduces header-injection attack surface** | Low | Critical | Require IP allowlist `WHILLY_TRUSTED_PROXY_IPS` to be non-empty before enabling; mandatory security review in PR. |
| R4 | **Worker tag SQL (`<@` containment) not supported by test DB** | Medium | Medium | Use `pytest.mark.postgres` for tag-filter integration tests; unit tests mock the query layer. |
| R5 | **Smoke test (item 2) is flaky due to timing** | Medium | Low | Use generous timeouts (30 s), retry-with-backoff on task status poll, and seed the DB deterministically in the fixture. |

---

## 9. Rollout

### Phase 0 — Trunk Restoration (this session)
Items 1, 7 (can be done in parallel: fix code + update docs independently).

### Phase 1 — Test Coverage and Auth Polish (next 1–2 sessions)
Items 2, 3, 4, 5, 6, 8, 12 in parallel batches. Gate Phase 2 on `pytest` passing at ≥ 90 % coverage for new code.

### Phase 2 — User-Facing Features (next sprint)
Items 9, 10, 11 serialized (share `auth_routes.py`). Items 18, 19, 20, 21, 22 in parallel (independent modules).

### Phase 3 — Security, Stretch, Documentation (following sprint)
Item 13 (route audit) after all Phase 2 routes are stable. Items 14, 15, 16, 17 as capacity allows. Items 23, 24 close the cycle.

---

## 10. Out of Scope

- Multi-tenant plan isolation or per-plan access control.
- Full OAuth 2.0 / OIDC authorization-code flow (only header-trust proxy is in scope).
- RBAC beyond the `admin` / `user` role distinction.
- Redesigning the Postgres schema for tasks or plans.
- TLS termination (assumed to be handled by the reverse proxy).
- Any changes to the `whilly` interactive menu or PRD wizard.
- Automated database backup and recovery tooling.
- Mobile or native client applications.
