# E15 (WebAuthn) + E17 (OIDC header-trust) — Security Design & Implementation Plan

> **STATUS: DESIGN ONLY — NOT IMPLEMENTED.**
> Per [`docs/PRD-post-auth-hardening.md`](../docs/PRD-post-auth-hardening.md) Risk Register **R3**,
> no code for these items ships without a security-review sign-off. This document is
> that review's input artifact: it states the threat model, the concrete plan grounded
> in the code that already exists, and the explicit gates that must be green before merge.
> It deliberately contains **no production auth code**.

Author: autonomous session 2026-05-20. Reviewer: _(unassigned — operator acts as security reviewer)_.

---

## 0. Why these two were deferred

Both were intentionally skipped in the post-auth-hardening autopilot run and flagged in the
[2026-05-19 handoff](SESSION-HANDOFF-2026-05-19.md):

- **E15 (WebAuthn / passkeys)** — complex protocol; PRD §Item 15 + R3 say "save for dedicated sprint."
- **E17 (OIDC header trust)** — PRD R3 rates it **Low likelihood / Critical impact**: a header-injection
  attack surface. "Mandatory security review before merge."

Neither is a bug. This plan exists so that when a dedicated sprint picks them up, the security
analysis is already done and the implementation is a known quantity rather than improvised.

---

## 1. Shared foundation already in place (from E14b)

E15 explicitly "shares the session state machine" (PRD §Item 15 Dependencies → Item 14). The TOTP
work (#298) built a reusable two-phase login pattern that WebAuthn slots into directly:

| Building block | Where | Reuse for E15 |
|---|---|---|
| Signed short-lived "credentials verified, second factor pending" cookie | [`whilly/api/totp_routes.py`](../whilly/api/totp_routes.py) `PENDING_COOKIE_NAME`, `_mint_pending_cookie`, `_verify_pending_cookie` | Same mechanism; mint after password step, redeem after the WebAuthn assertion. |
| Login intercept hook | [`whilly/api/auth_routes.py`](../whilly/api/auth_routes.py) `submit_login` → `maybe_intercept_for_totp` (single flag-gated conditional, ~line 198) | Add a sibling `maybe_intercept_for_webauthn` called from the same point, behind its own flag. |
| Session minting | `whilly/api/sessions.py` `create_session` / `verify_session` (DB-backed) | Unchanged — minted only after the ceremony succeeds. |
| Per-cookie brute-force lockout | `totp_routes.py` (5 attempts → bounce to `/login`) | Pattern reused for failed assertions. |

**Implication:** E15 is additive and isolated — it does not modify the password or TOTP paths, only
adds a third flag-gated branch. E17 is **not** built on this; it is a request-time middleware that
bypasses the interactive login entirely (see §3).

Current migration head is `025_auth_audit` (`whilly/adapters/db/migrations/versions/`), so **new
migrations are `026+`** — note the PRD's "025_webauthn_credentials" filename is stale.

---

## 2. E15 — WebAuthn / passkeys for admins

### 2.1 Scope (PRD §Item 15)
`webauthn` PyPI package; admin-only registration + authentication ceremonies; end-to-end test with a
software authenticator (`py_webauthn` test vectors).

### 2.2 Threat model & review gates
WebAuthn's risk is **implementation correctness**, not a new trust boundary (the protocol is designed
to resist phishing/replay). The review must confirm:

1. **Challenge handling** — challenges are server-generated, single-use, bound to the pending cookie,
   and expire with it. No challenge reuse across ceremonies.
2. **Origin / RP-ID binding** — `expected_origin` and `rp_id` are derived from server config
   (`WHILLY_PUBLIC_ORIGIN`), never from a request header. A misconfigured RP-ID silently disables the
   phishing resistance that is the entire point.
3. **User verification & sign-count** — store and check the authenticator sign-count to detect cloned
   credentials; decide UV policy (required vs. preferred) explicitly.
4. **Credential scoping** — a credential is bound to exactly one user row; registration requires an
   already-authenticated admin session (you cannot enroll a key for someone else).
5. **Fallback** — losing a key must not lock an admin out permanently; document the recovery path
   (admin-reset via existing `admin_users_routes.py`).

### 2.3 Implementation plan (flag-gated, default OFF)
- **Flag:** `WHILLY_WEBAUTHN_ENABLED` (default `0`) — mirrors `WHILLY_TOTP_ENABLED`. Off ⇒ router not
  mounted and `maybe_intercept_for_webauthn` is a no-op (instant rollback, same property as TOTP).
- **Migration `026_webauthn_credentials.py`:** `webauthn_credentials(id, user_id FK, credential_id
  BYTEA UNIQUE, public_key BYTEA, sign_count BIGINT, transports TEXT[], created_at, last_used_at)`.
  Reviewed by db-admin before apply (FK + unique index on `credential_id`).
- **`whilly/api/webauthn_repo.py`:** async CRUD (insert credential, fetch by user, bump sign-count).
- **`whilly/api/webauthn_routes.py`:** four endpoints — `GET/POST /me/webauthn/register`
  (begin/finish registration, requires live admin session) and `POST /auth/webauthn/begin` +
  `/auth/webauthn/verify` (the second-factor ceremony, redeems the pending cookie).
- **`auth_routes.py`:** one conditional after the password check, ordered relative to TOTP (decide:
  WebAuthn-or-TOTP, or WebAuthn-preferred-then-TOTP). Keep it a single line like the TOTP intercept.
- **Templates:** `webauthn_register.html.j2`, `webauthn_verify.html.j2` (+ register in
  `OPERATOR_WUI_ARTIFACTS`).
- **`pyproject.toml`:** `[webauthn] = ["webauthn>=2.0"]` optional extra; gate tests with
  `pytest.importorskip("webauthn")` (the E14b/`pyotp` precedent, #299).

### 2.4 Test strategy
Unit ceremonies against `py_webauthn` software-authenticator vectors (register → verify happy path,
wrong-origin reject, replayed-challenge reject, sign-count-regression reject, lockout after N fails),
all behind `importorskip`. No live browser/hardware needed for CI.

### 2.5 Open questions for the reviewer
- Is WebAuthn a **replacement** for TOTP per-user, or always an **additional** factor?
- UV policy: `required` (PIN/biometric every time) or `preferred`?
- Is passkey usable as a **first** factor (passwordless) for admins, or strictly second factor? PRD
  says "authenticate with hardware keys" — ambiguous; default to **second factor** unless told otherwise.

---

## 3. E17 — OIDC SSO via reverse-proxy header trust

### 3.1 Scope (PRD §Item 17)
New `whilly/api/oidc_header_auth.py`. When `WHILLY_TRUST_PROXY_AUTH=1`, inspect `X-Forwarded-User` on
each request; if the user exists in `users`, create a **transient** (non-persisted) session. Accept the
header **only** from `WHILLY_TRUSTED_PROXY_IPS` (CIDR list). Header ignored entirely when the flag is
unset/`0`. Not full OAuth — header-trust only (PRD non-goals).

### 3.2 Threat model — this is the dangerous one (R3: Critical impact)
The whole feature is a deliberately-trusted identity header. If any client can reach the app directly
(not via the proxy) **and** the app trusts the header, **anyone can become any user by setting
`X-Forwarded-User: admin`.** The review must treat the following as hard, non-negotiable gates:

1. **Fail-closed on misconfig.** `WHILLY_TRUST_PROXY_AUTH=1` with an **empty/unparseable**
   `WHILLY_TRUSTED_PROXY_IPS` must **refuse to start** (or hard-disable the feature with a loud error),
   never fail-open. Mirror the existing fail-closed posture decisions in
   [`ADR-001-auth-hardening-p1.md`](../docs/adr/ADR-001-auth-hardening-p1.md).
2. **Peer IP, not forwarded IP.** Allowlist checks the **direct TCP peer** (`request.client.host` /
   the socket), never `X-Forwarded-For` (which is itself attacker-controlled). Document the trusted
   hop count assumption explicitly.
3. **Strip inbound header at the edge.** The proxy must overwrite/strip any client-supplied
   `X-Forwarded-User`. Whilly cannot enforce this, so it must be in `.env.example` + operator docs as a
   precondition, and the threat-model section must state "Whilly trusts the proxy to strip; if the
   proxy passes client headers through, this feature is a full auth bypass."
4. **Transient session only.** No DB row (`sessions.create_session` is NOT used). Identity is resolved
   per-request from the header; if the header vanishes, the session is gone. Decide how this composes
   with `must_change_password` gate and `auth_audit` logging (proxy logins should still be audited).
5. **No interaction with interactive login.** When proxy-auth resolves a user, the password/TOTP/
   WebAuthn paths are bypassed — confirm that's intended and that a proxy-authed user can't also hold a
   stale cookie session that outlives the proxy trust.

### 3.3 Implementation plan (flag-gated, default OFF, fail-closed)
- **Flags:** `WHILLY_TRUST_PROXY_AUTH` (default `0`), `WHILLY_TRUSTED_PROXY_IPS` (CIDR list, required
  non-empty when the former is `1`).
- **`whilly/api/oidc_header_auth.py`:** a Starlette middleware that (a) checks `WHILLY_TRUST_PROXY_AUTH`,
  (b) validates the peer IP against the parsed CIDR allowlist using `ipaddress`, (c) reads
  `X-Forwarded-User`, (d) looks up the user, (e) attaches a transient identity to `request.state`. Any
  failure ⇒ header ignored (request proceeds to normal auth, ending at 401 if unauthenticated).
- **`whilly/api/main.py`:** mount the middleware **conditionally** and **before** the session/auth
  layer but **after** CSRF stays outermost (match the ordering note in the D10b/C6 work — gate must not
  displace CSRF). Startup must hard-fail if the flag is on and the allowlist is empty/invalid.
- **`.env.example`:** both vars + a prominent comment: "Only enable behind a proxy that strips
  client-supplied X-Forwarded-User. Misconfiguration = full auth bypass."
- **No new migration** (no persisted state).

### 3.4 Test strategy
- Trusted-IP + seeded user + `X-Forwarded-User: alice` → reaches dashboard (PRD AC).
- Same request from an **untrusted** IP → 401 (PRD AC).
- Flag on + **empty** allowlist → startup raises (fail-closed gate #1).
- Client-supplied `X-Forwarded-For` spoof attempt does **not** widen trust (gate #2).
- Flag off → header completely ignored even from a trusted IP.
- Proxy login is recorded in `auth_audit` (gate #4).

### 3.5 Reviewer decisions (resolved 2026-05-21 — see ADR-001 §P1.6)
- **Admin actions:** RESOLVED → proxy-authed identity keeps its full role from the `users` row (not
  read-only). The trust decision is made at the flag; downgrading a trusted identity adds branching for
  marginal benefit.
- **`WHILLY_ENABLE_ROUTE_AUDIT=1` interaction:** RESOLVED → no conflict. The route audit walks
  `app.routes`; header trust adds no routes (it is middleware feeding the already-recognised
  `_authenticate_session`), so the two flags coexist.
- **`must_change_password`:** RESOLVED → bypassed for proxy-authed users by design (password lifecycle
  is the proxy's responsibility in an SSO deployment).
- **Still open (out of scope for this PR):** trusted-hop count — a single proxy is assumed. If chained
  proxies are ever in scope, the peer-IP check needs a documented `num_trusted_hops`.

---

## 4. Recommended sequencing
1. **E17 first design-review** (it's the higher-risk, smaller-surface item) so the threat model is
   signed off while it's fresh; implement only after sign-off.
2. **E15 second** — larger but lower-risk; depends only on the already-shipped E14b state machine.
3. Each ships as its own flag-gated PR with the security-review checklist below pasted into the PR
   description and explicitly ticked by the reviewer.

## 5. Security-review sign-off checklist (paste into the implementing PR)
- [ ] **E17:** fail-closed on empty/invalid `WHILLY_TRUSTED_PROXY_IPS` (startup refuses).
- [ ] **E17:** allowlist checks direct peer IP, never `X-Forwarded-For`.
- [ ] **E17:** `.env.example` + docs state the "proxy must strip client header" precondition.
- [ ] **E17:** transient sessions are not persisted; proxy logins are audited.
- [ ] **E17:** default OFF; flag flip tested in staging; instant rollback confirmed.
- [ ] **E15:** challenges single-use, bound to pending cookie, expiring.
- [ ] **E15:** origin/RP-ID from server config, never from a request header.
- [ ] **E15:** sign-count regression rejected; UV policy decided and documented.
- [ ] **E15:** credential enrollment requires an authenticated admin session.
- [ ] **E15:** default OFF; key-loss recovery path documented.
- [ ] **Both:** new auth paths visible to `route_audit` if `WHILLY_ENABLE_ROUTE_AUDIT=1`.

---

_This plan changes no runtime behavior. It exists to make the eventual E15/E17 sprints a reviewed,
de-risked execution rather than an improvisation._
