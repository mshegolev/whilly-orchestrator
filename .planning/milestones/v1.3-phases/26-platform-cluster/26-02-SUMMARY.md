---
phase: 26-platform-cluster
plan: 02
subsystem: auth-security
tags: [openspec, spec, auth, security, documentation-only]
requires:
  - openspec/AUTHORING.md (format rules)
  - openspec/specs/task-model-fsm/spec.md (exemplar shape)
  - whilly/api/* auth set + security/* + secrets.py + core/task_id.py (reverse-spec source)
provides:
  - openspec/specs/auth-security/spec.md (normative auth-security capability, PLAT-02)
affects:
  - openspec/COVERAGE-MATRIX.md auth-security capability is now realized as a spec
tech-stack:
  added: []
  patterns: [reverse-spec from shipped v4 code, subsystem-altitude security spec]
key-files:
  created:
    - openspec/specs/auth-security/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - Specced at subsystem altitude (one requirement per concern), not per-module.
  - Flag-gated features (OIDC/WebAuthn/TOTP) each carry a default-OFF scenario.
  - web-status-ui transport tokens referenced as a boundary, not re-specified.
metrics:
  duration: ~12m
  completed: 2026-06-16
---

# Phase 26 Plan 02: auth-security Spec Summary

Normative subsystem-altitude OpenSpec capability spec for `auth-security`
(PLAT-02), reverse-spec'd from the real v4 auth/security subsystem and passing
`openspec validate auth-security --strict` (exit 0).

## What was built

`openspec/specs/auth-security/spec.md` — a single capability spec with a
`## Purpose` (well over 50 chars, names the reverse-spec sources and the
web-status-ui boundary) and `## Requirements` with 16 `### Requirement:` blocks,
each whose first body line carries SHALL/MUST and each with ≥1 `#### Scenario:`
(WHEN/THEN). Coverage, one requirement per concern:

1. Session authentication + signed `SameSite=Strict` cookie (sessions.py,
   auth_routes.py, auth_tokens.py).
2. Password hashing (PBKDF2-HMAC-SHA256 ≥100k iters) + 5-strike/15-min account
   lockout with uniform failure shape (passwords.py, users_repo.py).
3. Forced password-change gate redirecting flagged users to
   `/auth/change-password` with the exact whitelist (must_change_gate.py).
4. Forced-change verdict invalidation on `set_password` (auth_routes.py +
   must_change_gate.invalidate_session).
5. Flag-gated OIDC reverse-proxy header trust — default OFF, fail-closed on
   empty/invalid allowlist, peer-IP trust, forged XFF cannot widen
   (oidc_header_auth.py, E17 design, ADR-001 §P1.6/§P1.8).
6. Flag-gated WebAuthn — default OFF, admin-session enrollment, single-use
   challenge, sign-count-regression reject (webauthn_routes.py + repos).
7. Flag-gated TOTP second factor — default OFF, pending-cookie two-phase login
   (totp_routes.py).
8. Two-phase login coordinator dispatch (second_factor.py).
9. CSRF origin protection on cookie-authenticated mutations, bearer paths exempt
   (csrf.py).
10. Login rate limiting (10/60s sliding window, fail-open cluster degrade)
    (rate_limit.py).
11. Route auth audit — opt-in `WHILLY_ENABLE_ROUTE_AUDIT=1` startup check
    (route_audit.py).
12. Auth audit trail — best-effort `auth_audit` inserts (auth_audit_repo.py).
13. Production-mode config hardening (prod_mode.validate_prod_config).
14. Dashboard SSE bearer token (dashboard_token.py).
15. Secret reference resolution (env:/keyring:/file:) + per-worker credential
    storage with chmod-600 file fallback (secrets.py).
16. Secret linting + prompt sanitization for untrusted text
    (security/secret_lint.py, security/prompt_sanitizer.py).
17. **ADR-001 path-traversal sink-class mitigation** — `validate_task_id`
    rejecting non-string/empty/`..`/out-of-charset ids before shell/path/tmux/
    log-filename sinks, plus `safe_task_id_filename` flattening
    (core/task_id.py).

The web-status-ui bootstrap + per-worker bearer transport tokens are referenced
as a boundary and explicitly NOT re-specified.

## Verification

- `openspec validate auth-security --strict` → "Specification 'auth-security' is
  valid", exit 0 (0 errors, 0 warnings).
- `validate_task_id` requirement present (the word `validate_task_id` appears).
- No delta headers; mirrors the task-model-fsm exemplar shape.

## Deviations from Plan

None — plan executed exactly as written. Documentation-only; zero `whilly/`
changes.

## Threat Model Realization

All `mitigate`-disposition threats from the plan's STRIDE register are realized
as normative requirements: T-26-01 (OIDC fail-closed header trust), T-26-02
(forced password-change gate), T-26-03 (task-id path-sink), T-26-04
(secrets/secret-lint/prompt-sanitizer), T-26-05 (auth audit trail). T-26-SC
(accept) holds — no package installs in a documentation-only phase.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: openspec/specs/auth-security/spec.md
- `openspec validate auth-security --strict` exit 0
