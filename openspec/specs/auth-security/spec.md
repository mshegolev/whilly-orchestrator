## Purpose

The auth-security capability governs Whilly's operator authentication and
security-hardening surface: the browser session lifecycle (magic-link and
password login, signed session cookies, DB-backed `sessions`/`magic_links`
rows), the forced password-change gate, the three flag-gated stronger-auth
features (reverse-proxy OIDC header trust, WebAuthn passkeys, TOTP second
factor) with their shared two-phase login coordinator, CSRF origin checking,
login rate limiting and account lockout, the route auth audit and the
`auth_audit` trail, production-mode config hardening, the same-origin dashboard
SSE bearer, secret reference resolution, secret linting/redaction, untrusted-
text prompt sanitization, and the ADR-001 task-id path-traversal sink-class
mitigation. It is reverse-spec'd from `whilly/api/*` (the auth set:
`auth_routes`, `auth_tokens`, `sessions`, `oidc_header_auth`, `webauthn_*`,
`totp_*`, `second_factor`, `passwords`, `must_change_gate`, `csrf`,
`rate_limit`, `route_audit`, `prod_mode`, `dashboard_token`,
`admin_users_routes`, `users_repo`, `auth_audit_repo`), `whilly/cli/admin.py`,
`whilly/secrets.py`, `whilly/security/{secret_lint,prompt_sanitizer}.py`, and
`whilly/core/task_id.py`.

This capability owns the full operator authentication model. The transport-
level auth split owned by the `web-status-ui` capability — the shared cluster
bootstrap token and the per-worker bearer token used by worker RPCs — is
referenced as a boundary here and is NOT re-specified; those machine-to-machine
credentials bypass the cookie session, the must-change gate, and CSRF by
design.

## Requirements

### Requirement: Session authentication and signed session cookie
The system SHALL establish an authenticated operator session only by writing a
`sessions` row (`whilly.api.sessions.create_session`) after a successful
credential check, and SHALL mint a `SameSite=Strict; HttpOnly; Path=/` signed
session cookie carrying that `session_id`; a request is authenticated only when
its cookie signature verifies AND `verify_session` finds a matching row that is
not revoked and not expired.

#### Scenario: Successful login establishes a session
- **WHEN** an operator submits valid credentials to `POST /auth/login` (or
  consumes a valid magic link) and no second-factor or proxy intercept applies
- **THEN** the system SHALL call `create_session` to persist a `sessions` row
- **AND** the system SHALL set the signed session cookie with `SameSite=Strict`,
  `HttpOnly`, and `Path=/` carrying the new `session_id`

#### Scenario: Revoked or expired session is rejected
- **WHEN** a request presents a session cookie whose `session_id` maps to a row
  that is revoked (`revoked_at` set) or past `expires_at`
- **THEN** `verify_session` SHALL return `None` and the request SHALL NOT be
  treated as authenticated

### Requirement: Password hashing and account lockout
The system SHALL hash operator passwords with PBKDF2-HMAC-SHA256 at no fewer
than 100000 iterations using a per-user random salt, verify by constant-time
comparison, and lock an account for 15 minutes after 5 consecutive failed
password attempts without revealing whether the failure was an unknown user, a
wrong password, or a locked account.

#### Scenario: Lockout after repeated failures
- **WHEN** `verify_credentials` records the 5th consecutive password mismatch
  for an account
- **THEN** the system SHALL set `locked_until` to roughly 15 minutes ahead
- **AND** subsequent verification attempts during the lockout window SHALL fail
  with the same indistinguishable mismatch outcome

#### Scenario: Uniform failure shape
- **WHEN** authentication fails for an unknown username, a wrong password, or a
  locked account
- **THEN** the system SHALL return the same failure outcome and SHALL NOT leak
  which case occurred

### Requirement: Forced password-change gate
The system SHALL, via `MustChangePasswordGateMiddleware`, redirect every
cookie-authenticated request from a user whose `must_change_password` flag is
`True` to `/auth/change-password` with HTTP 303, except for the whitelisted
paths (`/auth/change-password`, `/auth/logout`, `/auth/login`, `/auth/magic`,
`/auth/magic-login`, `/health`, and `/static/*`), until the flag is cleared.

#### Scenario: Flagged user is trapped on the change-password page
- **WHEN** a user with `must_change_password=True` requests any non-whitelisted
  path while holding a valid session cookie
- **THEN** the gate SHALL respond with a 303 redirect to `/auth/change-password`

#### Scenario: Machine and whitelist paths pass through
- **WHEN** a request carries no session cookie (worker bearer / dashboard JWT)
  or targets a whitelisted path
- **THEN** the gate SHALL pass the request through without a DB round-trip and
  SHALL NOT redirect

### Requirement: Forced-change verdict invalidation on password change
The system SHALL clear the `must_change_password` flag as a side effect of a
successful `set_password`, and the change-password handler SHALL call
`invalidate_session` to drop the gate's cached verdict so the very next request
is not falsely redirected.

#### Scenario: Verdict refreshed immediately after change
- **WHEN** a flagged user successfully submits the change-password form
- **THEN** the system SHALL clear `must_change_password` for that user
- **AND** the handler SHALL invalidate the gate's cached verdict for the session
  so the next request proceeds to the requested route

### Requirement: Flag-gated OIDC reverse-proxy header trust (default OFF, fail-closed)
The system SHALL ignore the `X-Forwarded-User` header entirely and leave the
header-trust middleware unmounted unless `WHILLY_TRUST_PROXY_AUTH` is truthy,
and when it is truthy SHALL refuse to start (raise at `create_app` time) when
`WHILLY_TRUSTED_PROXY_IPS` is empty or unparseable; when correctly configured it
SHALL grant a transient, non-persisted principal only when the direct TCP peer
(plus, when `WHILLY_TRUSTED_PROXY_HOP_COUNT > 1`, the nearest hops) is in the
CIDR allowlist and the header names an existing user.

#### Scenario: Disabled by default
- **WHEN** `WHILLY_TRUST_PROXY_AUTH` is unset or not truthy
- **THEN** `ProxyHeaderAuthConfig.from_env` SHALL return `enabled=False`
- **AND** the middleware SHALL NOT be mounted and `X-Forwarded-User` SHALL be
  ignored

#### Scenario: Fail-closed on empty allowlist
- **WHEN** `WHILLY_TRUST_PROXY_AUTH=1` but `WHILLY_TRUSTED_PROXY_IPS` is empty
  or contains an invalid CIDR
- **THEN** `from_env` SHALL raise `RuntimeError` so the app refuses to boot
  rather than trusting the identity header from any peer

#### Scenario: Forged X-Forwarded-For cannot widen trust
- **WHEN** the feature is enabled with the default single trusted hop and a
  request arrives from an untrusted direct peer carrying a forged
  `X-Forwarded-For` and `X-Forwarded-User`
- **THEN** trust SHALL be evaluated against the direct peer only and the request
  SHALL receive no proxy principal

### Requirement: Flag-gated WebAuthn passkey enrollment and assertion (default OFF)
The system SHALL leave the WebAuthn routes unmounted and the WebAuthn login
branch a no-op unless `WHILLY_WEBAUTHN_ENABLED` is truthy; when enabled it SHALL
require a live admin session to enroll a credential, bind each authentication
challenge as single-use to the pending cookie, and reject an assertion whose
authenticator sign-count regresses against the stored value.

#### Scenario: Disabled by default
- **WHEN** `WHILLY_WEBAUTHN_ENABLED` is unset or not truthy
- **THEN** the WebAuthn routers SHALL NOT be mounted
- **AND** the second-factor coordinator SHALL behave byte-identically to the
  TOTP-only flow

#### Scenario: Sign-count regression rejected
- **WHEN** the feature is enabled and a passkey assertion presents a sign-count
  not greater than the stored sign-count for that credential
- **THEN** the system SHALL reject the assertion and SHALL NOT mint a session

### Requirement: Flag-gated TOTP second factor (default OFF)
The system SHALL leave the TOTP routes unmounted and the login flow byte-
equivalent to the password-only path unless `WHILLY_TOTP_ENABLED` is truthy and
the user has an enabled TOTP secret; when both hold it SHALL withhold the real
session cookie after the password step, set a short-lived signed pending cookie,
redirect to `/auth/totp`, and mint the session only after the submitted code
verifies.

#### Scenario: Disabled or unenrolled user logs in directly
- **WHEN** `WHILLY_TOTP_ENABLED` is off or the user has no enabled TOTP secret
- **THEN** `POST /auth/login` SHALL complete the session as in the password-only
  flow with no second-factor redirect

#### Scenario: Enrolled user must pass the second factor
- **WHEN** the flag is on and an enrolled user passes the password step
- **THEN** the system SHALL set the pending cookie and redirect to `/auth/totp`
  without minting the real session cookie
- **AND** the real session cookie SHALL be minted only after `POST /auth/totp`
  verifies the pending cookie and the code

### Requirement: Two-phase login coordinator
The system SHALL route the post-password step through the single
`maybe_intercept_for_second_factor` coordinator, which when the WebAuthn flag is
off delegates unchanged to the TOTP intercept, and when it is on dispatches by
what the user has enrolled: none completes login, TOTP-only redirects to
`/auth/totp`, WebAuthn-only to `/auth/webauthn`, and both to the `/auth/2fa`
chooser; every verify route SHALL redeem the same factor-agnostic pending
cookie.

#### Scenario: User with both factors gets the chooser
- **WHEN** the WebAuthn flag is on and a user with both a TOTP secret and a
  passkey passes the password step
- **THEN** the coordinator SHALL redirect to `/auth/2fa` carrying the shared
  pending cookie

#### Scenario: No enrolled factor completes login
- **WHEN** the coordinator runs for a user with no enrolled second factor
- **THEN** it SHALL return no intercept and login SHALL complete normally

### Requirement: CSRF origin protection on cookie-authenticated mutations
The system SHALL reject, via `WhillySessionCSRFMiddleware`, any state-mutating
request (POST/PATCH/PUT/DELETE) that authenticates through the session cookie
when its `Origin` neither matches the request's own scheme+host+port nor appears
in the `WHILLY_CSRF_ORIGIN_ALLOWLIST`, while exempting worker-bearer and
dashboard-JWT requests and the no-cookie `POST /auth/login`.

#### Scenario: Cross-origin cookie POST is blocked
- **WHEN** a cookie-authenticated `POST` arrives with an `Origin` that is
  neither same-origin nor allowlisted
- **THEN** the middleware SHALL reject the request with HTTP 403 before the
  route handler runs

#### Scenario: Bearer mutations are exempt
- **WHEN** a state-mutating request authenticates via a worker bearer or
  dashboard JWT rather than the session cookie
- **THEN** the Origin check SHALL NOT apply

### Requirement: Login rate limiting
The system SHALL apply a per-source-IP sliding-window rate limit to the
authentication endpoints (default 10 requests per 60 seconds) when
`WHILLY_AUTH_RATE_LIMIT_ENABLED` is truthy, selecting an in-process limiter for
single-worker deployments and degrading fail-open to a null (always-allow)
limiter when `WHILLY_NUM_WORKERS > 1` without a configured `WHILLY_REDIS_URL`.

#### Scenario: Excess attempts from one IP are throttled
- **WHEN** a single source IP exceeds the configured request cap within the
  60-second window and the limiter is enabled
- **THEN** `allow()` SHALL return `False` for the over-cap requests

#### Scenario: Disabled limiter always allows
- **WHEN** `WHILLY_AUTH_RATE_LIMIT_ENABLED` is falsy
- **THEN** `allow()` SHALL return `True` for every request

### Requirement: Route auth audit (opt-in startup check)
The system SHALL, only when `WHILLY_ENABLE_ROUTE_AUDIT=1`, walk `app.routes`
after all routers are mounted and refuse to start if any route is reachable
without either a recognized auth dependency in its `Depends` chain or an explicit
entry in the public whitelist; when the flag is unset the audit SHALL be skipped.

#### Scenario: Unprotected route refuses startup
- **WHEN** the audit is enabled and a route has neither a recognized auth
  dependency nor a whitelist entry
- **THEN** `audit_routes` SHALL raise and the server SHALL refuse to start

#### Scenario: Audit off by default
- **WHEN** `WHILLY_ENABLE_ROUTE_AUDIT` is not set to `1`
- **THEN** the route audit SHALL be skipped and SHALL NOT block startup

### Requirement: Auth audit trail
The system SHALL record every login outcome — including probes for non-existent
accounts and trusted-proxy header logins — into the `auth_audit` table via
`auth_audit_repo.insert_attempt`, and that insert SHALL be best-effort: a
failure SHALL be logged at WARNING and swallowed so it never blocks the login
response.

#### Scenario: Proxy login is audited
- **WHEN** a trusted-peer request carries `X-Forwarded-User` naming a known user
- **THEN** the system SHALL insert an `auth_audit` row with outcome `ok`
- **AND** an unknown asserted user SHALL be recorded with outcome `missing_user`

#### Scenario: Audit failure does not break login
- **WHEN** `insert_attempt` raises while recording an outcome
- **THEN** the error SHALL be logged and swallowed and the login response SHALL
  proceed unaffected

### Requirement: Production-mode config hardening
The system SHALL, when `WHILLY_PROD_MODE` is truthy, call `validate_prod_config`
before routing is wired and raise a `RuntimeError` with an actionable message
when the dashboard token secret is missing or shorter than 32 bytes, when the
CSRF origin allowlist is unset, or when the session cookie is explicitly forced
insecure.

#### Scenario: Weak secret refuses startup in prod
- **WHEN** `WHILLY_PROD_MODE=true` and `WHILLY_DASHBOARD_TOKEN_SECRET` decodes
  to fewer than 32 bytes (or is missing)
- **THEN** `validate_prod_config` SHALL raise `RuntimeError` and the app SHALL
  refuse to start

#### Scenario: Dev mode unaffected
- **WHEN** `WHILLY_PROD_MODE` is not truthy
- **THEN** the prod hardening checks SHALL NOT be enforced

### Requirement: Dashboard SSE bearer token
The system SHALL protect the dashboard live-update channel
(`GET /events/stream`) with a short-lived HS256-style signed bearer minted by
`dashboard_token`, embedded in the anonymously-rendered dashboard page, scoped,
and constrained to an expiry no greater than 3600 seconds, signed with a per-
process secret regenerated on restart.

#### Scenario: Expired or unsigned token rejected
- **WHEN** the SSE channel receives a connect request whose token is missing,
  has an invalid signature, or is past its `exp`
- **THEN** the system SHALL reject the connection

#### Scenario: Restart invalidates outstanding tokens
- **WHEN** the control-plane process restarts and regenerates its per-process
  secret
- **THEN** previously minted dashboard tokens SHALL no longer verify

### Requirement: Secret reference resolution and per-worker credential storage
The system SHALL resolve config values bearing the `env:`, `keyring:`, or
`file:` reference schemes through `whilly.secrets` (passing literal and non-
string values through unchanged), and SHALL persist a per-control-plane worker
bearer to the OS keychain, falling back to a chmod-600 atomically-written JSON
file under the XDG config dir when the keychain backend is unavailable.

#### Scenario: Reference scheme resolved
- **WHEN** a config value is `env:NAME`, `keyring:service[/user]`, or
  `file:/path`
- **THEN** the resolver SHALL return the value from the named environment
  variable, OS keyring entry, or stripped file contents respectively

#### Scenario: Keychain failure falls back to a protected file
- **WHEN** `store_worker_credential` cannot write to the OS keychain
- **THEN** it SHALL write the credential to a chmod-600 JSON file under the XDG
  config dir using an atomic temp-then-replace write

### Requirement: Secret linting and prompt sanitization for untrusted text
The system SHALL redact recognized secret patterns before persisting or
forwarding text via `security.secret_lint`, and `security.prompt_sanitizer`
SHALL wrap untrusted text in `<UNTRUSTED kind=...>...</UNTRUSTED>` fences,
redact secrets, strip C0 control bytes while preserving newline and tab,
neutralize embedded close markers, enforce a hard length cap, and be idempotent
before the text reaches a worker prompt or a PR body.

#### Scenario: Untrusted text is fenced and redacted
- **WHEN** `sanitize_external_text` processes untrusted content containing a
  secret-shaped token and a forged `</UNTRUSTED>` marker
- **THEN** the output SHALL be wrapped in the untrusted fence, SHALL have the
  secret redacted, and SHALL neutralize the embedded close marker

#### Scenario: Sanitization is idempotent
- **WHEN** already-sanitized text is passed through `sanitize_external_text`
  again
- **THEN** the result SHALL be unchanged

### Requirement: Task-id path-traversal sink-class mitigation (ADR-001)
The system SHALL route every externally-sourced task id through
`whilly.core.task_id.validate_task_id`, which SHALL raise `ValueError` when the
id is not a string, is empty, contains the `..` path-traversal substring, or
contains any character outside `^[A-Za-z0-9._:/-]+$`, rejecting it before the id
can reach a shell wrapper, branch name, worktree path, tmux session name, or log
filename; ids destined for a filename or tmux target SHALL additionally be
flattened by `safe_task_id_filename`.

#### Scenario: Traversal id rejected before reaching a sink
- **WHEN** a task id containing `..` (e.g. `../../etc/passwd`) reaches
  `validate_task_id` from any loader surface
- **THEN** the function SHALL raise `ValueError` naming the offending id and the
  id SHALL NOT be interpolated into any shell, path, or session target

#### Scenario: Valid id passes unchanged
- **WHEN** `validate_task_id` receives a non-empty id matching
  `^[A-Za-z0-9._:/-]+$` with no `..` substring
- **THEN** the function SHALL return the id unchanged

#### Scenario: Filename target is flattened
- **WHEN** a validated hierarchical or namespaced id (containing `/` or `:`) is
  used as a log filename or tmux session target
- **THEN** `safe_task_id_filename` SHALL replace every character outside
  `[A-Za-z0-9_.-]` with `_` so the id cannot escape its base directory or break
  the tmux target syntax
