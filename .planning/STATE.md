---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: Adoption & live-ops
status: Awaiting next milestone
last_updated: "2026-06-12T12:54:54.658Z"
last_activity: 2026-06-12 — Milestone v1.2 completed and archived
progress:
  total_phases: 3
  completed_phases: 3
  total_plans: 9
  completed_plans: 9
  percent: 100
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-06-11)

**Core value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.
**Current focus:** Milestone complete

## Current Position

Phase: Milestone v1.2 complete
Plan: —
Status: Awaiting next milestone
Last activity: 2026-06-12 — Milestone v1.2 completed and archived

## Active Roadmap

See: `.planning/ROADMAP.md`

| Phase | Name | Requirements | Status |
|-------|------|--------------|--------|
| 18 | Migration Chain Validation | MIG-01, MIG-02 | Not started |
| 19 | Live Authenticated Smoke | LIVE-01, LIVE-02, LIVE-03 | Not started |
| 20 | Jira Watcher Daemon | WATCH-01, WATCH-02, WATCH-03 | Complete (Plans 01-03 done) |

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
| Phase 18-migration-chain-validation P01 | 19 min | 3 tasks | 2 files |
| 19 | 01 | 16 min | 2 | 2 |
| Phase 19 P02 | 20min | 2 tasks | 2 files |
| 19 | 03 | 23 min | 3 | 3 |
| Phase 19 P04 | 24 | 2 tasks | 2 files |
| 20 | 01 | 24 min | 2 | 2 |
| 20 | 02 | 28 min | 2 | 2 |
| 20 | 03 | 22 min | 2 | 6 |

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

## Decisions

- [Phase ?]: Use EXPECTED_CHAIN[-1] as single source of truth for head revision; no second literal in test
- [Phase ?]: Post-downgrade assertion remains empty-set; all 017-028 migrations have real drop_table/drop_column
- [Phase ?]: Evidence file contains only revision string, count, booleans; no DSN/password
- [Phase 19-01]: Import _log_dir from whilly.llm_ops so WHILLY_LOG_DIR is honored by smoke report dir
- [Phase 19-01]: SmokeReport.add_check never raises on False — per-check accumulation is a hard contract
- [Phase 19-01]: write_smoke_report takes explicit report_dir param for testability; callers use _smoke_report_dir() for production default
- [Phase 19-02]: Import EXIT_CHECK_FAILED as _SMOKE_EXIT_CHECK_FAILED to preserve EXIT_VALIDATION_ERROR constant in jira.py
- [Phase 19-02]: Smoke subparser requires --interactive-config flags because _ensure_jira_config accesses args.interactive_config
- [Phase 19-02]: classify check reads snapshot.classification as pure field access — no extra Jira call, read-only guarantee
- [Phase 19-03]: _resolve_gitlab_config_state exposes GITLAB_TOKEN as highest-priority env var per CONTEXT.md token precedence
- [Phase 19-03]: _resolve_project_path strips '..' components before URL-encoding to satisfy T-19-06 traversal threat
- [Phase 19-03]: gitlab_getter injection defaults to _gitlab_get at call time for clean test injection without circular imports
- [Phase ?]: Phase 19-04: Bash code blocks use env-var references not angle-brackets to satisfy bash -n validation in docs
- [Phase 20-01]: EXIT_VALIDATION_ERROR (1) reused as single-instance refusal code to avoid a new exit constant
- [Phase 20-01]: Backoff applied to NEXT cycle sleep (interval + backoff_seconds), not the current cycle (Pitfall 6)
- [Phase 20-01]: _acquire_pid_lock treats EPERM as stale — conservative choice, overwrite and proceed
- [Phase 20-02]: _read_watch_readiness re-implemented locally in jira_watch_loop.py to avoid circular import from whilly.cli.jira
- [Phase 20-02]: _run_dispatch_if_ready extracted as a named helper so the dispatch call site is isolated and grep-auditable
- [Phase 20-02]: dispatch_runner=None means no dispatch wired yet; plan 03 wires the Phase-17-gated path
- [Phase 20-02]: Pause gate fires after collect and before dispatch — read-only polling continues while paused (CONTEXT.md locked)
- [Phase 20-03]: dispatch_runner=None without --dispatch; production closure wires Phase-17 readiness gate only when --dispatch set (T-20-10)
- [Phase 20-03]: _run_watch_status lives in jira_watch_loop.py to keep no-circular-import invariant; jira.py imports it lazily
- [Phase 20-03]: watch-status returns EXIT_OK for missing status file — status absence is not an error

## Operator Next Steps

- Start the next milestone with /gsd-new-milestone
