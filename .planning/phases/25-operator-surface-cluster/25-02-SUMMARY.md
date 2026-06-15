---
phase: 25-operator-surface-cluster
plan: 02
subsystem: web-status-ui
tags: [openspec, reverse-spec, transport, fastapi, sse, auth, documentation]
requires:
  - openspec/AUTHORING.md
  - whilly/adapters/transport/{server,client,auth,schemas}.py
  - whilly/api/{main,plans_api,tasks_api,tasks_api_crud,metrics,sse,sse_endpoint,event_flusher,static_mount}.py
  - whilly/cli/server.py
  - whilly/web_status.py
provides:
  - openspec/specs/web-status-ui/spec.md
affects:
  - .planning/REQUIREMENTS.md
  - .planning/STATE.md
tech-stack:
  added: []
  patterns: [reverse-spec-from-source, openspec-strict, subsystem-altitude]
key-files:
  created:
    - openspec/specs/web-status-ui/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
  removed:
    - openspec/specs/web-status-ui/.gitkeep
decisions:
  - "Captured the transport two-token auth split (per-worker bearer vs bootstrap) without re-speccing the full session/OIDC/WebAuthn model — that boundary is referenced as auth-security (Phase 26)."
  - "Specced observed v4 behavior only: 204 on idle claim, 409 optimistic-lock conflict tuple, 401 + WWW-Authenticate envelope, startup fast-fail, constant-time compare."
  - "Added an explicit read-only vs mutating boundary requirement to anchor which endpoints require a credential."
  - "Removed the placeholder .gitkeep — sibling capability dirs hold only spec.md."
metrics:
  duration: ~12m
  completed: 2026-06-15
---

# Phase 25 Plan 02: web-status-ui Spec Summary

Reverse-spec'd Whilly's HTTP control-plane / web-status surface (OPS-02) into one
normative, subsystem-altitude OpenSpec capability spec at
`openspec/specs/web-status-ui/spec.md`, passing `openspec validate web-status-ui
--strict` with zero errors and zero warnings (exit 0).

## What was built

A single capability spec with a ≥50-char `## Purpose` and 16 `### Requirement:`
blocks (each with a SHALL/MUST first body line and ≥1 `#### Scenario:`), grounded
in the real v4 code:

**Worker HTTP transport (Task 1 — `whilly/adapters/transport/*.py`)**
- Per-worker bearer auth on steady-state RPCs (`WHILLY_WORKER_TOKEN` legacy /
  `workers.token_hash`); unknown bearer → 401 `invalid token`.
- Bootstrap-token auth on `POST /workers/register` (`WHILLY_WORKER_BOOTSTRAP_TOKEN`
  / `bootstrap_tokens`), distinct secret so either rotates independently; plaintext
  bearer returned exactly once, only the SHA-256 hash persisted.
- 401 envelope carries `WWW-Authenticate: Bearer realm="whilly"` (missing /
  non-Bearer / empty / mismatched).
- Auth dependency fast-fails at `create_app` time on missing/whitespace-only env;
  constant-time `secrets.compare_digest`.

**FastAPI control plane + SSE + status (Task 2 — `whilly/api/*`, `cli/server.py`, `web_status.py`)**
- Register + heartbeat (200 `{"ok": false}` for a deregistered worker).
- Long-polled `POST /tasks/claim` → 200 with payload or 204 on idle timeout.
- Terminal RPCs `complete`/`fail` with optimistic concurrency → 200 or 409 with
  the `(task_id, expected_version, actual_version, actual_status)` conflict tuple.
- Unauthenticated `GET /health` → 200/503 on DB round-trip.
- Plans API: `GET /api/v1/plans` list, `POST` → 201 + ETag, `PATCH /{plan_id}`
  with `If-Match` (428 missing, 412 stale).
- Tasks API `GET /api/v1/tasks`: paginated, status-filterable, per-row `version`
  + human-review annotation.
- `GET /metrics` Prometheus text exposition.
- `GET /events/stream` SSE with `event_id`/`event_type` framing, `Last-Event-ID`
  replay (cap + `replay_truncated` frame), per-subscriber broker fan-out.
- Read-only vs mutating boundary requirement.
- Dashboard static mount + explicit delegation of session/OIDC/WebAuthn to
  `auth-security`.
- `whilly server` boot (env-error exit when `WHILLY_DATABASE_URL` unset).
- Standalone localhost:9191 `GET /api/status` (JSON) + `GET /` (HTML) + 404.

## Verification

- `openspec validate web-status-ui --strict` → "Specification 'web-status-ui' is
  valid", exit 0.
- Endpoints enumerated match real route strings in `server.py`,
  `plans_api.py`, `sse_endpoint.py`, `metrics.py`, and `web_status.py` (no
  invented routes).
- No delta headers; auth-security referenced, not duplicated.

## Deviations from Plan

None — plan executed as written. The only filesystem side effect beyond the spec
itself was removing the now-redundant `openspec/specs/web-status-ui/.gitkeep`
(consistent with every other capability directory, which holds only `spec.md`).

## Known Stubs

None. The spec is a complete normative document; no placeholder content.

## Self-Check: PASSED

- FOUND: openspec/specs/web-status-ui/spec.md
- FOUND commit: dd6a24f (docs(25-02): reverse-spec web-status-ui ...)
- `openspec validate web-status-ui --strict` → valid, exit 0
