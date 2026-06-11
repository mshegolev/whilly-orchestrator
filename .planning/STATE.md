---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: Adoption & live-ops
status: executing
last_updated: "2026-06-11T07:51:54.494Z"
last_activity: 2026-06-11 -- Phase 18 planning complete
progress:
  total_phases: 3
  completed_phases: 0
  total_plans: 2
  completed_plans: 0
  percent: 0
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-06-11)

**Core value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.
**Current focus:** Milestone v1.2 Adoption & live-ops — roadmap defined, ready to plan Phase 18.

## Current Position

Phase: 18 (next to start)
Plan: —
Status: Ready to execute
Last activity: 2026-06-11 -- Phase 18 planning complete

## Active Roadmap

See: `.planning/ROADMAP.md`

| Phase | Name | Requirements | Status |
|-------|------|--------------|--------|
| 18 | Migration Chain Validation | MIG-01, MIG-02 | Not started |
| 19 | Live Authenticated Smoke | LIVE-01, LIVE-02, LIVE-03 | Not started |
| 20 | Jira Watcher Daemon | WATCH-01, WATCH-02, WATCH-03 | Not started |

## Active Scope

**Out-of-band complete:** `post-auth-hardening` plan is functionally complete (27 done, 2 skipped).
Auth stack (sessions, flag-gated OIDC header trust, flag-gated WebAuthn second factor) and
ADR-001 path-sink fixes are prerequisites this milestone builds on.

**Archived v1.1 evidence:**

- `.planning/milestones/v1.1-ROADMAP.md`
- `.planning/milestones/v1.1-REQUIREMENTS.md`
- `.planning/milestones/v1.1-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.1-RETROSPECTIVE.md`

## Recent Decisions

- Phase 18 (MIG) is sequenced first: Docker-backed Alembic chain validation is standalone
  infrastructure with no credential dependencies. Running it first gives confidence in the data
  layer before live sessions write to Postgres.

- Phase 19 (LIVE) is sequenced second: live smoke validates the existing poll/link-refresh code
  paths that the watcher will wrap. Must pass before Phase 20 can be trusted.

- Phase 20 (WATCH) is sequenced last: the daemon wraps a validated poll cycle (Phase 19) and
  writes to a validated data layer (Phase 18).

- Phase 13 Plan 01 kept UI-01 metadata in `whilly/operator_views.py` to extend the existing pure
  operator contract pattern.

- TUI surface key handling now derives from `operator_surface_hotkeys()` instead of a duplicate
  literal map.

- Active WUI dashboard JavaScript receives surface order, hotkey copy, and route prefixes through
  dashboard context.

- Phase 13 Plan 02 classifies templates and static JavaScript only; CSS, fonts, and images remain
  outside the UI-02 artifact scope.

- `_logs.html` remains routeable but noncanonical with Phase 14 follow-up, while `_admin.html` and
  `_prd.html` remain inactive quarantined artifacts.

- `whilly/api/static/whilly-hotkeys.js` is now active after replacing stale `1-7` selectors and
  `/admin/workers/*` routes with the canonical five-surface API contract.

- Phase 13.1 was inserted after Phase 13 because update checks/manual update/automatic update
  policy are product lifecycle controls that should be available before continuing lower-priority
  WUI/TUI parity work.

- Phase 13.1 keeps automatic updates explicit: default mode is off, and no unrelated command
  silently upgrades Whilly.

- Update tests mock PyPI and subprocess boundaries so verification does not mutate the local
  environment.

- Phase 13.2 keeps feedback explicit and single-channel: GitHub Issues via `gh`, no email/GitLab,
  and no automatic crash reporting.

- Phase 17 treats `hotfix` as urgency over `feature`, `bug`, `task`, or `devops` instead of a fifth
  work kind, because urgent production fixes can exist in more than one work category.

- Phase 17 makes code readiness a gate: missing repo context, inaccessible GitLab links, or missing
  unit-test strategy should ask the operator before workers run.

- Phase 14 keeps `_logs.html` routeable but noncanonical with backend coverage, and keeps
  `_admin.html`/`_prd.html` quarantined because their routes are not active supported WUI routes.

- Phase 15 keeps TUI scoped to active WUI navigation only; logs/admin/PRD are explicit exclusions
  until a future phase wires them as canonical capabilities.

- Phase 16 updates operator docs to the current shared TUI/WUI hotkeys and pins the fragment
  boundary with docs regression tests.

- Phase 17 stores Jira routing metadata in `jira_work` plan JSON and Postgres session/event tables,
  keeping classification, context hashes, and readiness verdicts available for later watch flows.

- Phase 17 adds one-shot `whilly jira poll` for rereading Jira issue fields, comments, changelog,
  linked issues, remote links, and repo hints; long-running watch can wrap that command.

- Phase 17 keeps autonomous Jira `run` gated only when the operator provides a local
  `--readiness-repo-path`; the override is explicit through `--allow-unready-run`.

## Accumulated Context

### Roadmap Evolution

- Phase 13.1 inserted after Phase 13: Version update checks and manual/automatic update modes
  (URGENT).

- Phase 13.2 inserted after Phase 13.1: GitHub feedback issue reporter (URGENT).
- Phase 17 added after Phase 16: Jira work classification and code readiness routing.
- Phases 18-20 defined for milestone v1.2: migration validation, live smoke, watcher daemon.

## Performance Metrics

| Phase | Plan | Duration | Tasks | Files |
|-------|------|----------|-------|-------|
| 13 | 01 | 9 min | 3 | 7 |
| 13 | 02 | 8 min | 3 | 4 |
| 13.1 | 01 | focused | 3 | 6 |
| 13.2 | 01 | focused | 3 | 6 |
| 14 | 01 | focused | 2 | 2 |
| 15 | 01 | focused | 2 | 1 |
| 16 | 01 | focused | 2 | 4 |
| 17 | 01-05 | focused | 5 | 12 |

## Previous Milestones

- v1.0 shipped and archived on 2026-05-08.
- v1.1 shipped and archived on 2026-05-11.

Archives:

- `.planning/milestones/v1.0-ROADMAP.md`
- `.planning/milestones/v1.0-REQUIREMENTS.md`
- `.planning/milestones/v1.0-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.1-ROADMAP.md`
- `.planning/milestones/v1.1-REQUIREMENTS.md`
- `.planning/milestones/v1.1-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.1-RETROSPECTIVE.md`

## Deferred Items

- Browser/screen-reader QA for the complete WUI operator workflow (OPQA-01).
- New operator modules beyond the pulled logs/admin/PRD artifacts.
- Replacement of the current Jinja/HTMX dashboard or Rich TUI architecture.

## Next Step

Plan Phase 18 with `/gsd-plan-phase 18`.
