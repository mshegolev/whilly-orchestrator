# Codebase Concerns

**Analysis Date:** 2026-06-10

## Tech Debt

### Cost Tracking Store Not Implemented

**Issue:** Dashboard cost display is stubbed
**Files:** `whilly/api/dashboard.py:106`
**Impact:** Cost tracking appears as dummy values (0 usd, 40000 tokens budget); users see incorrect spend metrics
**Fix approach:** Implement real cost-tracking lookup in `FileLogStore.usage()`. Currently returns hardcoded `(0, LOG_TOKENS_BUDGET_DEFAULT, 0.0)`. Requires building/wiring a cost store (database or cache) to fetch actual usage from `whilly_logs/{task_id}.jsonl` event stream, aggregate by task, and expose it for dashboard consumption.

### Route Audit Disabled by Default

**Issue:** Auth route surface audit only runs when `WHILLY_ENABLE_ROUTE_AUDIT=1`
**Files:** `whilly/api/route_audit.py`, `docs/adr/ADR-001-auth-hardening-p1.md` (§P1.5)
**Impact:** Unknown routes may be reachable without auth in production. Default OFF because existing routes use inline `_authenticate_session()` calls (not Depends-style) which the audit's dependant-walk cannot see
**Fix approach:** Refactor all inline-auth routes to use Depends-style auth dependencies, then flip default to ON. ~40+ routes affected. This is a refactoring pass that should be scoped as a focused sprint to avoid spreading the change across multiple PRs.

### 30-Second TTL Cache on Password-Change Gate

**Issue:** `MustChangePasswordGateMiddleware` caches the `must_change_password` verdict for 30s
**Files:** `whilly/api/must_change_gate.py`
**Impact:** Password-change enforcement can lag by up to 30s if explicit invalidation is ever skipped. Mitigated by cache invalidation in the change-password handler, but a missed call could allow a user to bypass the gate temporarily
**Fix approach:** Add a schema-level `password_version` column to `users` table and check it on every request instead of caching. This removes the time-window risk entirely and is simpler than the current TTL approach.

## Known Bugs

### Session Cookie Revoke Window (24h)

**Issue:** After `/me/sessions/{session_id}/revoke`, the cookie is marked revoked but remains usable for 24h if leaked
**Files:** `whilly/api/auth_routes.py` (session revocation), `whilly/api/auth_tokens.py` (token validation)
**Trigger:** User clicks revoke, then someone finds the old cookie before expiry
**Workaround:** Session cookies have 24h TTL by default (`WHILLY_SESSION_COOKIE_TTL`). The `/me/sessions` UI shows all active sessions and allows per-device revoke, but revocation only marks the row; it doesn't invalidate the cookie immediately
**Why it's a trade-off:** Cookie-based sessions inherently have this window in every implementation. JWTs signed at issue time remain valid for their TTL. Mitigation is the revoke UI + short default TTL (24h is conservative for a dev tool; could be lowered to 1-4h in prod)

### VersionConflictError on Remote Worker Complete (Protocol Gap)

**Issue:** Remote worker `complete_task` filter expects `IN_PROGRESS` status, but worker skips the `start_task` RPC step
**Files:** `whilly/worker/remote.py:60-75`, `whilly/adapters/transport/server.py:58-82`
**Trigger:** Real run against production server where `/tasks/{id}/start` endpoint does not exist
**Workaround:** Logs and continues on 409 conflict; the status is still updated by a subsequent release_stale_tasks sweep
**Context:** TASK-022a2 / 022a3 shipped only four worker RPCs (register, heartbeat, claim, complete, fail). A future task must either add `/tasks/{id}/start` or relax the complete filter to accept `CLAIMED` too. Unit tests use a stub client that masks the issue by returning the correct post-update state.

## Security Considerations

### Path Traversal Risk in Task Log Reading (Recently Fixed)

**Issue:** Task IDs with `/` or `..` could bypass `whilly_logs/` directory containment
**Files:** `whilly/api/dashboard.py::_resolve_task_log_path`, `whilly/dashboard.py`, `whilly/cli/dashboard.py`
**Current mitigation:** Fixed in PR #318 + follow-up 5fb63e3. Task ID is now flattened to a safe filename via `safe_task_id_filename()` before path construction (e.g., `task-001a/subtask` → `task-001a__subtask`). Write sinks (`tmux_runner`, `verifier`) and read sink (`dashboard`) both use the same flattening function.
**Recommendations:** 
- Keep read/write flattening in lockstep (test regression for asymmetry)
- Consider adding a second layer: reject task IDs containing `/` or `..` at task creation time rather than relying on safe filename conversion

### OIDC Reverse-Proxy Header Trust (E17, Flag-Gated)

**Issue:** `E17` feature allows proxies to inject `X-Remote-User` header; if proxy is compromised, any user can be spoofed
**Files:** `whilly/adapters/transport/auth.py`, `whilly/api/auth_routes.py` (OIDC integration points), `docs/adr/ADR-001-auth-hardening-p1.md` (§P1.6)
**Current state:** Flag-gated with `WHILLY_OIDC_REVERSE_PROXY_TRUST_ENABLED` (default OFF). Implemented after explicit security review on 2026-05-21.
**Recommendations:**
- Do not enable in production unless the reverse proxy is infrastructure you control and can guarantee integrity
- Document the threat model clearly in operator guides (header-injection risk scales with proxy surface)
- Consider adding `X-Forwarded-For` IP-allowlist checks to reduce the blast radius if a peer proxy on the same network is compromised

### WebAuthn User Handle Exposure (E15, Fixed)

**Issue:** User handles were hashed via `sha256(username)`, making them reversible given a username list
**Files:** `whilly/api/webauthn_*`, PR #313
**Current state:** Fixed. Handles are now opaque random 64-byte secrets (`user_webauthn_user_handles` table). Old handles are migrated in migration 028.
**Context:** Fixes E15 review Finding 3. Already resolved in code.

### WebAuthn Challenge Reuse (E15, Fixed)

**Issue:** Challenges were server-side stored but could be replayed if the storage was lost
**Files:** `whilly/api/webauthn_*`, PR #312
**Current state:** Fixed. Challenges are now single-use (stored in `webauthn_challenges` table, deleted after verification). Each login attempt generates a fresh challenge; old challenges expire after 10 minutes.
**Context:** Fixes E15 review Finding 2. Already resolved in code.

### 2FA Brute-Force Lockout Bypass (E16, Fixed)

**Issue:** Rate-limit on IP could be exhausted to trigger global fallback, allowing brute-force on TOTP
**Files:** `whilly/api/rate_limit.py`, `whilly/api/users_repo.py`, PR #310
**Current state:** Fixed. Added server-side TOTP attempt counter (locked after 5 failures) + IP rate-limiting with Redis cluster awareness.
**Context:** Already resolved in code.

## Performance Bottlenecks

### Repository File Size (4549 Lines)

**Problem:** `whilly/adapters/db/repository.py` is the largest module (4549 lines)
**Files:** `whilly/adapters/db/repository.py`
**Cause:** Consolidates all task-state-machine SQL and atomic transaction logic in one class to keep isolation invariants tight. Every state transition (claim, complete, fail, release_stale) owns its own SQL with optimistic locking or row-level locks.
**Improvement path:** Split by domain (e.g., `TaskRepository` → `TaskClaimRepo`, `TaskCompleteRepo`, `TaskEventRepo`, `PlanRepo`). Risk: concurrency contracts are fragile; refactor only after comprehensive integration test coverage of all race conditions (visibility timeout vs. peer release, concurrent complete attempts, etc.).

### Transport Server Size (3557 Lines)

**Problem:** `whilly/adapters/transport/server.py` concentrates all FastAPI routes and middleware plumbing
**Files:** `whilly/adapters/transport/server.py`
**Cause:** Routes are co-located with lifespan, auth dependencies, and state setup so contract is visible in one place
**Improvement path:** Extract route groups into sub-routers (`APIRouter` for `/tasks/*`, `/workers/*`, `/auth/*`). Current structure is acceptable for a 50-endpoint API; refactor becomes valuable at >100 endpoints.

### Plan Serialization Round-Trips

**Problem:** Plans are serialized/deserialized on every state transition (claim, complete, release_stale)
**Files:** `whilly/adapters/db/repository.py`, `whilly/core/models.py`, `whilly/adapters/transport/schemas.py`
**Cause:** `TaskRepository` fetches rows, reconstructs domain `Task` objects, then returns them to the caller for deserialization into wire schemas
**Improvement path:** Consider a view-projection layer that skips domain reconstruction for read-only dashboard queries. Current path is fine for control-plane RPCs but adds latency to dashboard polling loops that re-fetch all tasks every 5s.

## Fragile Areas

### Task Visibility-Timeout Sweep

**Files:** `whilly/adapters/db/repository.py::release_stale_tasks` (line ~3014), `whilly/adapters/transport/server.py` (sweep loop), `whilly/cli/worker.py` (visibility_timeout_seconds config)
**Why fragile:** Two concurrent writers race: this sweep and a peer worker's complete/fail RPC. Lost-update detection relies on optimistic locking (version counter). If the sweep releases a task but the owner-worker's complete lands out-of-order (network reordering), the worker will see 409 conflict and log a WARNING instead of failing the task. This is intentional fail-open behavior, but if sweep interval is misconfigured (too tight), it can cascade: released task → picked by peer → original worker completes (409) → sweep releases again → loop.
**Safe modification:** 
- Tests: Add a chaos-test that simulates network delay and validates the version counter prevents double-completion
- Config: Document `VISIBILITY_TIMEOUT_SECONDS` (default 15m) as "must be ≥ max-expected-agent-runtime + sweep-interval". Warn at startup if interval > timeout/2.
- Test coverage: `tests/test_visibility_timeout.py` covers basic sweep, but lacks concurrent-writer scenarios. Add a test that spawns two workers with synthetic delays.

### Session Cache Invalidation on Password Change

**Files:** `whilly/api/must_change_gate.py`, `whilly/api/auth_routes.py::POST /me/password`
**Why fragile:** Cache invalidation on 30s TTL requires the change-password handler to call `invalidate_session()`. If a future code path adds a second place where password changes (e.g., admin force-reset), and that path forgets the invalidation, users will not be forced to re-login.
**Safe modification:**
- Extract invalidation into a `@dataclass` decorator or context manager: `@invalidate_session_on_password_change` or `with invalidate_on_change():`
- Add a test that patches the invalidation hook to fail and verifies the TTL boundary (should fail after 31s)

### Circular Dependency Risk Between Core and Adapters

**Files:** `whilly/core/`, `whilly/adapters/`, `whilly/worker/`, import-linter config `.importlinter`
**Why fragile:** `.importlinter` enforces `whilly.core` stays pure (no asyncpg, httpx, fastapi imports). If a future refactor in one of the adapters imports from `core` and that `core` function transitively imports from the adapter, the whole architecture collapses. The linter catches it at CI, but the fix is non-local.
**Safe modification:**
- Keep adapters out of `whilly/core/` imports entirely. Always pass dependencies in (Dependency Injection).
- When refactoring core functions, grep for `import whilly.core` in adapters and verify no reverse dependency.
- Consider splitting `core` into `core.models` (pure dataclasses) and `core.logic` (functions) to make the boundary clearer.

### Auth Token Lifetime and Revocation Asynchrony

**Files:** `whilly/api/auth_tokens.py`, `whilly/api/dashboard_token.py`, session/auth routes
**Why fragile:** Tokens have fixed TTLs (dashboard token ~30m, session cookie 24h). Revocation marks a row but doesn't invalidate the token itself. If a token is leaked and used repeatedly before revocation, the attacker has full access for that duration. The `/me/sessions` revoke UI is the escape hatch but requires user awareness.
**Safe modification:**
- Add `token_issued_version` column to users table and bump it on password change / session revoke. Validate on every request.
- Short-term: Lower session TTL to 1h in production (`.env.example` already defaults to 24h; document the trade-off).
- Tests: Add a test that verifies revoked sessions fail within 1 second of revocation (not 24h).

## Scaling Limits

### Claim Long-Poll Budget (30s Default)

**Current capacity:** Default 30s holds idle worker's connection open. At 1s poll interval, ~30 queries per idle worker to the database.
**Limit:** With 100 idle workers, that's ~3 qps baseline just from polling. At 10 idle workers, negligible. At 1000 idle workers, this becomes a problem (300 qps idle baseline).
**Scaling path:** 
- Implement a pub/sub notification system (database notifications via `LISTEN/NOTIFY` or Redis Pub/Sub) to wake workers when a task arrives instead of polling.
- Keep the 30s timeout as a fallback for when notifications fail, but the primary path becomes event-driven.

### Repository Size on Large Plans

**Current capacity:** Repository holds all tasks in memory during a plan run. `list_tasks` pagination defaults to 200 rows per page.
**Limit:** A plan with 10,000 tasks will have 50 pages. Dashboard polls `/tasks` every 5s; at 50 pages, that's 50 queries + serialization overhead per cycle.
**Scaling path:**
- Implement a `JSONL` event stream projection: append-only log of task status changes instead of full-table scan.
- Dashboard subscribes to the stream (SSE or WebSocket) instead of polling.
- Tests: Add a perf test with 10k tasks, measure page-load time and ensure it stays <500ms.

### Event Table Growth

**Current capacity:** Every task state transition (claim, complete, fail, release_stale) appends a row to `events`. Plan with 100 tasks → ~300-500 event rows. No automatic cleanup; events are append-only.
**Limit:** Over 1 year, a busy orchestrator could accumulate millions of event rows. Queries like "last 200 tasks" start scanning deeper tables. Dashboard's /tasks endpoint becomes slower.
**Scaling path:**
- Implement event archival: Move events older than 90 days to a separate `events_archive` table (partitioned by month or year).
- Queries transparently scan both tables via `UNION` or a partition-aware view.
- Add a `whilly admin archive-events` command to trigger backfill.

## Dependencies at Risk

### asyncpg Connection Pool Initialization

**Risk:** If the pool fails to initialize at startup, the server silently skips health checks and starts unhealthy
**Files:** `whilly/adapters/db/pool.py::create_pool`, `whilly/adapters/transport/server.py::lifespan`
**Impact:** Workers register and start claiming tasks before the database is actually reachable; claims timeout, tasks fail
**Mitigation:** `create_pool` now includes a health check (SELECT 1) before returning the pool. Server lifespan ensures `pool` is initialized before handlers run. But the initialization is not synchronized; if pool creation is slow, handlers are registered before the pool is ready (though the async lifespan context manager should prevent this, it's worth a regression test).
**Recommendation:** Add a test that starts the server with an unreachable database and verifies it fails loudly at startup rather than silently.

### Alembic Migration Order Sensitivity

**Risk:** Migrations 020–028 added user auth infrastructure (users, TOTP, WebAuthn, auth_audit). If an existing deployment skips a migration or applies them out of order, the schema will be incomplete
**Files:** `whilly/adapters/db/migrations/versions/020_users.py` through `028_webauthn_user_handles.py`
**Impact:** User creation fails if `users` table is missing; auth_audit logging fails if `auth_audit` table is missing
**Mitigation:** Alembic enforces linear order; applying migrations out of order will fail. But if someone manually deletes a row from `alembic_version`, the check is bypassed.
**Recommendation:** Add a startup validator that checks `alembic_version` table for gaps (e.g., version 019 present but 020 missing) and fails loud if found.

## Missing Critical Features

### Cost Tracking Integration

**Problem:** Dashboard shows dummy cost metrics; operators cannot track spend
**Blocks:** Budget-aware planning workflows, cost optimization feedback, compliance reporting
**Context:** Tracked in CLAUDE.md as a TODO comment (line 106 of `whilly/api/dashboard.py`).

### Multi-Plan Coordination

**Problem:** Whilly today runs one plan at a time sequentially. Multiple operators cannot work in parallel.
**Blocks:** Large orgs with many concurrent projects
**Status:** WUI supports "multi-plan" browsing (`.planning/wui-multi-plan_tasks.json` was a trial), but the orchestrator doesn't distribute work across plans dynamically.

### OIDC / External IdP Integration

**Problem:** Auth is still local users + magic links. No integration with corporate OIDC (Okta, Azure AD, etc.)
**Blocks:** Enterprise deployments that require SSO
**Status:** E17 (header-trust for reverse proxies) is a workaround, not a solution.

## Test Coverage Gaps

### Visibility-Timeout Sweep Under Contention

**What's not tested:** A task that is claimed, released by the sweep, claimed again by a peer, and then the original worker tries to complete it
**Files:** `whilly/adapters/db/repository.py::release_stale_tasks`, test suite at `tests/`
**Risk:** The version counter should prevent double-completion, but the code path is exercised only in synthetic unit tests, not in a realistic async scenario with three concurrent writers
**Priority:** High — this is a core correctness invariant

### Route Audit with Real Routes

**What's not tested:** The route audit (`whilly/api/route_audit.py`) only runs when `WHILLY_ENABLE_ROUTE_AUDIT=1`. The audit is not part of the standard CI because it fails on inline-auth routes.
**Files:** `whilly/api/route_audit.py`
**Risk:** A developer adds a public endpoint and forgets to add it to the auth whitelist or a Depends dependency. The audit would catch it, but since it's off by default, the route ships unvetted.
**Priority:** Medium — Enable the audit as a soft warning (log but don't fail) by default, upgrade to hard fail after inline-auth refactor.

### E2E Workflow with External Services

**What's not tested:** A complete workflow: (1) operator submits a plan, (2) worker registers, (3) task is claimed and run via Claude CLI, (4) result is posted back. Tests mock the Claude CLI; no real-subprocess test.
**Files:** `tests/test_whilly_e2e_triz_prd.py` (closest thing), `whilly/worker/remote.py`, `whilly/adapters/runner.py`
**Risk:** A regression in subprocess handling or Claude CLI invocation goes undetected until production.
**Priority:** Medium — Requires Docker + claude binary in CI. Currently blocked by infrastructure. See `.planning/STATE.md` "A2 (E2E smoke test)".

### Concurrent Session Revocation

**What's not tested:** Two revoke requests for the same session arriving simultaneously, or a session being revoked while a request is in flight
**Files:** `whilly/api/auth_routes.py::POST /me/sessions/{session_id}/revoke`
**Risk:** Race condition could cause a partial revoke or duplicate REVOKE event in the audit log
**Priority:** Low — Low likelihood (users don't typically double-click revoke), but a test would be cheap.

---

*Concerns audit: 2026-06-10*
