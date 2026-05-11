---
gsd_state_version: 1.0
milestone: v1.1
milestone_name: UI parity completion
status: completed
last_updated: "2026-05-11T16:47:42Z"
last_activity: 2026-05-11
progress:
  total_phases: 7
  completed_phases: 7
  total_plans: 12
  completed_plans: 12
  percent: 100
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-05-11)

**Core value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.
**Current focus:** No active milestone. Start the next product slice with `$gsd-new-milestone`.

## Current Position

Current Milestone: none
Phase: Complete
Plan: None
Status: Milestone complete
Last Activity: 2026-05-11
Last Activity Description: Archived v1.1 milestone artifacts after a passing milestone audit.

Progress: [##########] 100%

## Active Scope

No active milestone scope. v1.1 evidence is archived in:

- `.planning/milestones/v1.1-ROADMAP.md`
- `.planning/milestones/v1.1-REQUIREMENTS.md`
- `.planning/milestones/v1.1-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.1-RETROSPECTIVE.md`

## Recent Decisions

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

- Browser/screen-reader QA for the complete WUI operator workflow.
- New operator modules beyond the pulled logs/admin/PRD artifacts.
- Replacement of the current Jinja/HTMX dashboard or Rich TUI architecture.

## Next Step

Start the next milestone with `$gsd-new-milestone`.
