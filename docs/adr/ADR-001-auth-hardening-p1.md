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

## P1.7 — Second-factor brute-force lockout (post-E15 security review)

A review of the merged E15 surface surfaced a **pre-existing** weakness (E14b
code that E15 relocated into `whilly/api/second_factor.py`): the 2FA failed-
attempt lockout lived **only** in the client-held, HMAC-signed pending cookie.
HMAC prevents *editing* the counter but not *replaying* an older `a=0` cookie
before each guess, so the 5-try lockout was bypassable. The password account-
lockout did not backstop it — once the password is correct, no further password
failures accrue while the attacker brute-forces the (rotating, but small) TOTP
code. The verify endpoints also had no IP rate-limit (the password endpoint did).

**Decision — defense in depth, two layers:**

1. **IP rate-limit** (`rate_limit.allow`) on `POST /auth/totp` and the WebAuthn
   `begin`/`verify` endpoints — the same edge cap `submit_login` already had.
2. **Server-side per-user lockout** — `users_repo.is_account_locked` /
   `register_failed_second_factor` reuse the existing `users.failed_attempts` /
   `locked_until` columns, so a wrong **TOTP** code counts toward the same
   15-minute account lock the password path uses, and a cookie replay cannot
   reset it. Cleared on success via `update_last_login`. The per-cookie counter
   is retained for the "N attempts remaining" UX only.

**Scope decision — WebAuthn does not bump the lockout.** A WebAuthn assertion
cannot be brute-forced (it needs the private key), so a failed assertion does
*not* increment the shared counter — that would let a fumbled passkey lock the
account (including password login) for no security gain. WebAuthn still *respects*
an existing lock and is IP-rate-limited.

---

## P1.8 — E17 chained-proxy trust (`num_trusted_hops`) — IMPLEMENTED

**Status:** Implemented. Closes the open question from §P1.6 ("single proxy
assumed; chained proxies would need a documented `num_trusted_hops`").

**Problem.** E17 (`whilly/api/oidc_header_auth.py`) trusts the `X-Forwarded-User`
header only when the **direct TCP peer** (`request.client.host`) is in
`WHILLY_TRUSTED_PROXY_IPS`, and it deliberately ignores `X-Forwarded-For`. That
is exactly correct for the **single-hop** topology it was designed for
(`client → reverse-proxy → Whilly`). It does **not** cover multi-hop chains
(`client → CDN / L4 load-balancer → auth-proxy → Whilly`):

- If Whilly's direct peer is an L4 LB that is *not* the auth-injecting proxy,
  `peer_is_trusted` is checked against the LB. Putting the LB in the allowlist
  would trust a hop that did not necessarily strip a client-supplied
  `X-Forwarded-User` at the right boundary.
- If the auth-proxy is not the direct peer at all, the feature simply fails to
  authenticate (fail-closed — not a vulnerability, but a deployment blocker).

**Current behaviour is safe by default.** With the single-hop model, the worst
case behind an unanticipated LB is *no* proxy auth (401 → cookie login), never a
bypass. So this is a capability gap, not an open vulnerability.

**Design (as built).** `WHILLY_TRUSTED_PROXY_HOP_COUNT` (default `1` = the
peer-IP-only behaviour). When `> 1`, `ProxyHeaderAuthConfig.chain_is_trusted`
builds the nearest-first chain `[direct peer, *reversed(X-Forwarded-For)]` and
requires the first `N` entries to **all** be in `WHILLY_TRUSTED_PROXY_IPS`.
`X-Forwarded-For` is never trusted blindly — only allowlisted entries count, and
`N` bounds how far the walk goes. `from_env` fail-closes on a non-integer or
out-of-range (`1..16`) hop count.

**Guarantees (verified by `test_oidc_header_auth.py`).**
- Default `1` is byte-equivalent to the peer-IP-only path (XFF ignored), so a
  forged XFF cannot widen trust.
- With `N`, a request is trusted iff the `N` nearest hops are all allowlisted;
  any untrusted hop within the first `N` → reject (fail-closed).
- A short/empty/missing `X-Forwarded-For` under `N>1` → reject.
- The `(N+1)`-th entry (the purported client) is never required to be trusted, so
  spoofing it changes nothing.

**Operational note.** Set `N` to the exact number of trusted proxies in front of
Whilly, and put every one of them in `WHILLY_TRUSTED_PROXY_IPS`. An off-by-one
(too low) fails closed (no auth); too high also fails closed (XFF too short).

---

## P1.9 — Server-side single-use WebAuthn challenge (post-E15 review, Finding 2)

The E15 WebAuthn challenge was carried inside the HMAC-signed pending/registration
cookie. HMAC gives integrity but not **freshness**: single-use rested on the
cookie being cleared on success plus the sign-count regression check — and that
check is a no-op for **counter-less synced passkeys** (iCloud/Google), which
report `sign_count = 0` forever. A captured `(cookie, assertion)` pair could
therefore be replayed within the 5-minute TTL.

**Decision.** Move the challenge **server-side** (migration `027_webauthn_challenges`,
`whilly/api/webauthn_challenge_repo.py`). `begin` inserts a row keyed by a random
`challenge_id`; the cookie now carries only that id. `verify`/`finish` redeem the
challenge with an atomic `DELETE … RETURNING`, **before** verifying the assertion —
so even a failed verify burns it and any replay finds nothing. The challenge is
bound to its ceremony via a `purpose` CHECK (`register` / `authenticate`) so a
registration challenge can't be redeemed by the auth path. Applies to **both**
ceremonies for one auditable mechanism. DB-backed (not in-process) is forced by
correctness: a `begin` on one uvicorn worker must be redeemable by `verify` on
another.

---

## P1.10 — Opaque WebAuthn user handle (post-E15 review, Finding 3)

Registration used the username as the WebAuthn user handle
(`user_id=username.encode("utf-8")`). The spec recommends an **opaque, random**
handle (≤64 bytes): it is stored on the authenticator / passkey provider, so a
username leaks identity to that provider and is not stable across a rename.

**Decision.** A random 32-byte handle per user, stored in
`webauthn_user_handles` (migration `028`) and created on first enrolment via
`webauthn_repo.get_or_create_user_handle` (atomic upsert → stable across calls,
so a user's multiple passkeys share one handle). `register/begin` uses it as
`user.id`. Verify is unchanged — it still looks credentials up by `credential_id`,
so the handle is a registration-time privacy/stability improvement, not part of
the assertion check.

---

## P1.11 — Auth URL safety (broader auth-surface review)

A review of the rest of the auth surface (login / magic-link / change-password /
sessions, beyond E15/E17) found the token, session and CSRF primitives sound
(`hmac.compare_digest`, explicit `alg=HS256`, `typ`-claim separation, 256-bit
session ids, atomic single-use magic-link consume, email-scoped session list,
IDOR-guarded session revoke). Two URL-handling weaknesses were fixed:

- **Finding 5 (Medium) — host-header injection in magic links.** `_build_magic_url`
  built the link from `request.base_url` (the client-controlled `Host`). An
  attacker could request a link for a *victim's* email while spoofing
  `Host: attacker.test`; the victim's email would point at the attacker, who
  harvests the valid single-use token on click → account takeover. Fixed by
  `_public_base_url`: prefer `WHILLY_PUBLIC_ORIGIN` (already used by E15), falling
  back to `request.base_url` only when unset (dev/loopback).
- **Finding 7 (Low) — open-redirect via backslash in `?next=`.** Browsers fold
  `\` to `/`, so `/\evil.com` becomes protocol-relative `//evil.com`.
  `_sanitise_next_path` now rejects any path containing a backslash.

**Finding 6 (Low) — FIXED.** `POST /auth/change-password` (the forced first-login
flow) sets a new password WITHOUT the current one. It required an authenticated
session but neither the current password nor `must_change_password=True`, so a
session that was hijacked (cookie theft) or left open could rotate the password
without the current one — the check `POST /me/password` enforces. (Not CSRF-
exploitable: SameSite=Strict cookie + Origin allowlist; the path is not CSRF-
exempt.) The handler now reads the user row and proceeds only when
`must_change_password=True`; any other session (or a missing user row) is
redirected to `/me/password`, which requires the current password. The forced
first-login flow is unchanged (bootstrap admin has the flag set). Verified by
`tests/unit/test_change_password_gate.py`.

---

## P1.12 — Session-identity resolution (`<username>@local` vs real email, Finding 8)

**Severity: Medium — found while making the post-auth smoke test pass.** Several
auth call sites recovered the username from `sessions.email` by
`removesuffix("@local")`. That works for password users with no email (the
synthetic `<username>@local`), but **the seeded admin's email is the real
`admin@whilly.local`** (migration 020). For it, the strip is a no-op, leaving
`admin@whilly.local`, which has no `users` row — so:

- the **must-change gate fail-opened** → the default `admin/admin` account was
  never actually forced to change its password (it could navigate past the
  one-time login redirect to any page); and
- **both change-password endpoints broke** for the admin (`set_password` /
  `verify_credentials` got a non-existent username) — the seeded admin literally
  could not change its password through either flow.

`submit_login` works because it has the `User` object directly; only the
email→username *reconstruction* sites were affected.

**Fix.** One canonical resolver, `users_repo.get_user_by_session_email`:
`<username>@local` → strip + `get_user_by_username`; any other value →
`get_user_by_email` (defensive `LIMIT 2`, exactly-one-match else `None`; a
magic-link-only user with no row still → `None` → gate fail-open, which is
correct). Routed through it: the must-change gate, `POST /auth/change-password`,
and `POST /me/password`. Verified by the now-passing
`tests/integration/test_post_auth_smoke.py` plus
`tests/integration/test_user_email_resolution.py` (against the real seeded admin).

> Note: `tests/integration/test_post_auth_smoke.py` also needed an explicit
> `Origin` header on its change-password POST — a *test* gap (a browser sends
> `Origin` on a same-origin form POST; the CSRF middleware is correct), not a
> product issue.

---

## References

- PRD: [`docs/PRD-post-auth-hardening.md`](../PRD-post-auth-hardening.md)
- Plan + realisation notes: [`.planning/post-auth-hardening-tasks.json`](../../.planning/post-auth-hardening-tasks.json)
- Session handoff (sprint start): [`.planning/SESSION-HANDOFF-2026-05-18.md`](../../.planning/SESSION-HANDOFF-2026-05-18.md)
- Authentication configuration reference: [`docs/Whilly-Usage.md`](../Whilly-Usage.md) §Authentication Configuration
