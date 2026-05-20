# ADR-001 — Auth Hardening (P1)

**Status:** Accepted (2026-05-19)
**Author:** Sprint participants; ADR finalised in PR series #271–#291.
**PRD:** [`docs/PRD-post-auth-hardening.md`](../PRD-post-auth-hardening.md)
**Plan:** [`.planning/post-auth-hardening-tasks.json`](../../.planning/post-auth-hardening-tasks.json)

---

## Context

The v3.x Whilly control plane shipped an authentication layer built around a single
operator (`admin/admin` bootstrap, magic-link email, per-process session secret).
That model held up for the homelab deployments it was designed for, but two
classes of operational issue emerged once larger teams adopted Whilly:

1. **Security posture gaps.** Failed-login attempts were rate-limited in-process
   but not cluster-wide, the session-signing secret was regenerated on every
   server restart (forcing operators to re-login after every deploy), the
   "change your default password" prompt could be bypassed by directly
   navigating to `/`, and there was no audit trail of authentication events.
2. **Operator UX gaps.** No self-service password change without admin
   intervention, no admin UI for managing users (every CRUD operation was a
   manual `psql` session), no email transport for magic links (operators
   recovered links from log files), and no visibility into the active
   sessions a user had open.

This sprint (the "post-auth-hardening" PRD) was scoped to close these gaps
while staying within the existing architectural shape — no migration to an
external IdP, no full RBAC matrix, no replacement of Postgres. The work is
organised under the P1.1–P1.5 decisions below.

---

## Decision

### P1.1 — Forced-password-change gate

**Decision:** A user with `users.must_change_password = TRUE` is redirected
to `/auth/change-password` on **every** authenticated request, not just the
one immediately after login. Enforced by a Starlette `BaseHTTPMiddleware`
([`whilly/api/must_change_gate.py`](../../whilly/api/must_change_gate.py))
registered before the CSRF middleware so CSRF stays outermost.

**Voluntary path:** A second endpoint, `GET/POST /me/password`
([`whilly/api/auth_routes.py`](../../whilly/api/auth_routes.py)), lets a
signed-in user change their own password without admin intervention. Unlike
the forced flow, it requires the current password — protecting against
session-cookie theft.

**Cache:** 30s TTL keyed on `session_id`, with explicit invalidation from
the change-password handler so the next request after a successful change
doesn't loop on the cached `True` verdict.

**Shipped in:** PR #278 (C6 middleware), PR #279 (D9 self-service).

### P1.2 — IP rate-limiting + account lockout + cluster awareness

**Decision (existing):** In-process sliding-window IP rate limiter
([`whilly/api/rate_limit.py`](../../whilly/api/rate_limit.py)) gates the
auth endpoints. Account lockout
([`whilly/api/users_repo.py::verify_credentials`](../../whilly/api/users_repo.py))
adds a per-account counter that locks the row for 15 minutes after 5
consecutive failures, with constant-time-ish dummy verifies on the
"no such user" path so the latency doesn't leak account existence.

**Decision (this sprint):** Cluster awareness. `build_rate_limiter()`
detects `WHILLY_NUM_WORKERS > 1` at startup; with no `WHILLY_REDIS_URL`
it emits a WARNING and installs `NullRateLimiter` (fail-open — preferable
to bricking auth on a misconfigured cluster). With Redis configured, a
`RedisRateLimiter` (INCR/EXPIRE counter) takes over.

**Shipped in:** PR #283 (C8 cluster-aware selection).

### P1.3 — `WHILLY_PROD_MODE` umbrella switch

**Decision (existing):** Single `WHILLY_PROD_MODE=true` flag flips multiple
defaults to prod-safe values at once:
- Session cookie `Secure` flag on (HTTPS-only)
- `__Host-` prefix on the session cookie name (browsers enforce
  `Secure; Path=/; no Domain`)
- Validators (P1.5) fail-closed instead of fail-warn

See [`whilly/api/prod_mode.py`](../../whilly/api/prod_mode.py).

**Decision (this sprint):** Persistent session secret. PR #272 documented
`WHILLY_DASHBOARD_TOKEN_SECRET` in `.env.example` and `docs/Whilly-Usage.md`;
PR #282 added an integration test that verifies the cookie survives a
server restart when the secret is set, and is rejected when it isn't.

### P1.4 — Cookie-authenticated state-mutating requests (CSRF)

**Decision (existing):** `WhillySessionCSRFMiddleware`
([`whilly/api/csrf.py`](../../whilly/api/csrf.py)) gates every state-mutating
request that carries the session cookie with an `Origin` allowlist
(`WHILLY_CSRF_ORIGIN_ALLOWLIST`). Default `SameSite=Strict` on the cookie
itself is the first line of defence; the middleware is defence-in-depth.

**This sprint:** Verified that the new `MustChangePasswordGateMiddleware`
registers BEFORE CSRF so CSRF stays outermost on the request path. A
bad-Origin POST gets 403 from CSRF before the gate ever queries the DB.
Test in [`tests/unit/test_must_change_gate.py::test_csrf_still_blocks_bad_origin_post_with_session`](../../tests/unit/test_must_change_gate.py).

### P1.5 — Prod-mode validators

**Decision (existing):** A set of startup validators fail-closed in
`WHILLY_PROD_MODE=true` and fail-warn otherwise, covering things like
"the bootstrap admin row is still using the default password" and
"`WHILLY_DASHBOARD_TOKEN_SECRET` is unset". See
[`whilly/api/prod_mode.py`](../../whilly/api/prod_mode.py) and the
operator-views invariants tracked by
[`whilly/operator_views.py::OPERATOR_WUI_ARTIFACTS`](../../whilly/operator_views.py).

**This sprint:** Two new validators (opt-in by env var):
- `WHILLY_ENABLE_ROUTE_AUDIT=1` runs the startup route audit
  ([`whilly/api/route_audit.py`](../../whilly/api/route_audit.py), PR #287) —
  refuses to start if any `APIRoute` is reachable without either a
  Depends-style auth dependency or an entry in the public whitelist.
  Default OFF because many existing routes use inline
  `_authenticate_session` (not Depends), which the dependant walk can't
  see. Future direction: migrate inline-auth routes to Depends, then
  flip the default to on.

---

## Sprint additions beyond the original P1.1–P1.5 frame

These items extended P1 with capabilities the original spec assumed but
didn't ship:

| Item | PR | What |
|---|---|---|
| D11 | [#277](https://github.com/mshegolev/whilly-orchestrator/pull/277) | `auth_audit` ledger table + `insert_attempt` repo for compliance review |
| D10b | [#286](https://github.com/mshegolev/whilly-orchestrator/pull/286) | Instrumentation of `submit_login` branches with the audit repo |
| D10 | [#285](https://github.com/mshegolev/whilly-orchestrator/pull/285) | Admin user-management UI + paginated `auth_audit` browse |
| E14a | [#276](https://github.com/mshegolev/whilly-orchestrator/pull/276) | Schema groundwork for TOTP (`user_totp_secrets` table + `pyotp` extras) |
| E16 | [#289](https://github.com/mshegolev/whilly-orchestrator/pull/289) | `/me/sessions` active-sessions UI with per-device revoke |
| C12 | [#284](https://github.com/mshegolev/whilly-orchestrator/pull/284) | SMTP magic-link transport with event-log fallback |
| B3 | [#281](https://github.com/mshegolev/whilly-orchestrator/pull/281) | Unit tests for `tasks_api_crud` reset endpoints |
| B4 | [#280](https://github.com/mshegolev/whilly-orchestrator/pull/280) | Unit tests for `whilly worker launch/list/remove` |
| B5 | [#282](https://github.com/mshegolev/whilly-orchestrator/pull/282) | Integration test: session cookie survives server restart with fixed secret |
| F18a | (prior session) | Migration adds `workers.tags` + `tasks.required_tags` |
| H21 | [#291](https://github.com/mshegolev/whilly-orchestrator/pull/291) | Worker launch `--model`/`--connect` override fix |

---

## Consequences

### Positive
- The "navigate-away-after-login" bypass is closed (P1.1). A misconfigured
  bootstrap admin password can no longer leak via a curious operator who
  knows the default URL layout.
- Cookie persistence across restart (P1.3 + `WHILLY_DASHBOARD_TOKEN_SECRET`)
  eliminates the "every deploy logs everyone out" footgun, which was the
  single biggest operator complaint pre-sprint.
- The audit ledger (D11 + D10b) makes compliance reviews answerable from
  SQL instead of grepping log files. The admin browse UI (D10) gives the
  admin a paginated view without needing `psql`.
- The cluster-aware rate-limit warning (C8) replaces a silent
  under-counting failure mode with a loud startup WARNING.
- A new admin can manage users (create / role / reset / delete) without
  touching the DB.

### Negative / Trade-offs
- The forced-password-change gate's 30s TTL cache can delay enforcement
  by up to 30 seconds if the explicit `invalidate_session()` hook is
  ever skipped. Mitigated by the change-password handler always calling
  it; future schema-level `password_version` column would remove the
  need entirely.
- The route audit (D13) ships default-OFF because the existing route
  surface uses inline `_authenticate_session` calls that the dependant
  walk can't see. Enabling-by-default is deferred until the route layer
  is refactored to Depends-style auth — tracked as future work.
- Cookie-based session model means revoking a session cookie still allows
  the leaked cookie to be used until it expires (default 24h) — true of
  every cookie-based scheme. The `/me/sessions` revoke (E16) is the
  user-facing escape hatch.
- The Mailer module (C12) hard-depends `aiosmtplib` in the `server`
  extras. Operators who don't use SMTP still install the dep — the
  alternative (separate `mailer` extra) would have meant a runtime
  import-error if SMTP was enabled without the install, judged less
  ergonomic than the slightly-larger base footprint.

### Skipped / Deferred (with rationale)
- **E14b (TOTP routes):** Session state machine extension carries
  regression risk across every auth path. The schema is ready (E14a),
  but the routes are paused for a focused sprint per PRD's High-risk
  classification.
- **E15 (WebAuthn / passkeys):** PRD R3 explicitly recommends a
  dedicated sprint. Complex protocol, depends on E14b.
- **E17 (OIDC header trust):** PRD R3 flags Critical impact from
  header-injection attack surface. Deferred from the main sprint, then
  implemented under explicit security review on 2026-05-21 (flag-gated,
  default OFF) — see the P1.6 addendum below.
- **F18b (worker-tag claim filter + CLI):** Full plumbing touches the
  API contract (RegisterRequest schema, server handler, client method,
  CLI flag). F18a's schema is in place; the claim-side logic is paused.
- **A2 (E2E smoke test):** Auth slice is already covered by
  `test_auth_matrix` + B5; the worker/task slice needs Docker + claude
  binary infrastructure better served by a focused integration PR.
- **H22 (worker bootstrap convenience command):** Non-critical wrapper
  around `whilly worker launch`.
- **I23 (Whilly-Usage docs refresh):** Mostly content for skipped items
  (F18b tag-pool, H22 bootstrap); the per-PR docs updates from C7, C8,
  and C12 covered the live-feature surface.

### Open process item
- The AC for this ADR includes "Reviewed and acknowledged by at least
  one other contributor (comment in PR)" — this autopilot-authored ADR
  will be merged via the same `--squash` workflow as the rest of the
  sprint; the human acknowledgement happens at merge approval time. A
  future-author can amend this ADR via a follow-up PR if any of the
  decisions above need revisiting.

---

## P1.6 — E17 OIDC header-trust (addendum, 2026-05-21)

Item 17 (reverse-proxy header trust) was implemented after the main sprint —
flag-gated, default OFF. See [`whilly/api/oidc_header_auth.py`](../../whilly/api/oidc_header_auth.py)
and the design in
[`.planning/E15-E17-auth-security-design.md`](../../.planning/E15-E17-auth-security-design.md).
The three review questions left open in the design were resolved as follows:

- **Proxy-authed identity gets its full role from the `users` row (not
  read-only).** Header trust already places full trust in the proxy to
  authenticate users; downgrading a trusted identity to read-only adds
  branching through every mutation path for marginal benefit. If you do not
  trust the proxy enough to grant admin, do not enable the feature.
- **The `must_change_password` gate is bypassed for proxy-authed users — by
  design.** In an SSO deployment the password lifecycle is the proxy's
  responsibility; the user may hold no usable local password, so routing them
  through Whilly's change-password flow is incoherent.
- **No conflict with `WHILLY_ENABLE_ROUTE_AUDIT=1`.** The route audit walks
  `app.routes`; header trust adds no routes (it is middleware that feeds the
  already-recognised `_authenticate_session`), so the two flags coexist.

**Operational gate (separate from the merge gate).** `WHILLY_TRUST_PROXY_AUTH=1`
must NOT be enabled in any deployment until (a) the reverse proxy is confirmed
to strip any client-supplied `X-Forwarded-User`, and (b) `WHILLY_TRUSTED_PROXY_IPS`
is set to the proxy's CIDR(s). With the flag off (the default) the header is
ignored entirely and the middleware is not mounted.

---

## References

- PRD: [`docs/PRD-post-auth-hardening.md`](../PRD-post-auth-hardening.md)
- Plan + realisation notes: [`.planning/post-auth-hardening-tasks.json`](../../.planning/post-auth-hardening-tasks.json)
- Session handoff (sprint start): [`.planning/SESSION-HANDOFF-2026-05-18.md`](../../.planning/SESSION-HANDOFF-2026-05-18.md)
- Authentication configuration reference: [`docs/Whilly-Usage.md`](../Whilly-Usage.md) §Authentication Configuration
