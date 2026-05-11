---
gsd_state_version: 1.0
milestone: v1.1
milestone_name: UI parity completion
current_phase: null
current_phase_name: null
current_plan: null
status: milestone_complete
last_updated: "2026-05-11T17:20:00Z"
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
**Current focus:** v1.1 UI parity completion, with Jira-driven work routing captured as the next
product-flow follow-up.

## Current Position

Current Milestone: v1.1
Phase: Complete
Plan: None
Status: Milestone complete
Last Activity: 2026-05-11
Last Activity Description: Completed Phase 17 and v1.1 milestone audit.

Progress: [##########] 100%

## Active Scope

- Canonical surface, hotkey, action, selector, and route contract for TUI and WUI.
- WUI static/template cleanup so active code uses current DOM selectors and supported API paths.
- Logs/admin/PRD fragment decisions: fully wire with backend methods and TUI parity, or quarantine
  from active UI scope.
- Focused tests that prevent orphan WUI files or stale routes from drifting back in.
- Version lifecycle controls so operators can check for a newer Whilly release, apply a manual
  update, or opt into automatic update behavior without hidden environment mutation.
- A fast GitHub feedback channel so operators testing Whilly on another computer can report bugs
  or ideas directly from the CLI.
- Jira-driven work routing that classifies incoming tasks, persists task history, rereads GitLab
  links, and checks code/test readiness before autonomous worker execution.

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

## Previous Milestone

v1.0 shipped and archived on 2026-05-08.

Archives:
- `.planning/milestones/v1.0-ROADMAP.md`
- `.planning/milestones/v1.0-REQUIREMENTS.md`
- `.planning/milestones/v1.0-MILESTONE-AUDIT.md`

## Deferred Items

- Browser/screen-reader QA for the complete WUI operator workflow.
- New operator modules beyond the pulled logs/admin/PRD artifacts.
- Replacement of the current Jinja/HTMX dashboard or Rich TUI architecture.

## Next Step

Archive v1.1 with `$gsd-complete-milestone` after operator approval to commit/tag.
