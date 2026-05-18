---
gsd_state_version: 1.0
milestone: v1.1
milestone_name: UI parity completion
status: completed
last_updated: "2026-05-18T13:10:00Z"
last_activity: 2026-05-18
progress:
  total_phases: 7
  completed_phases: 7
  total_plans: 12
  completed_plans: 12
  percent: 100
active_out_of_band:
  plan: post-auth-hardening
  plan_file: .planning/post-auth-hardening-tasks.json
  prd_file: docs/PRD-post-auth-hardening.md
  latest_handoff: .planning/SESSION-HANDOFF-2026-05-18.md
  status_counts:
    done: 3
    skipped: 2
    human_loop: 1
    pending: 23
    total: 29
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-05-11)

**Core value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.
**Current focus:** Out-of-band follow-up plan `post-auth-hardening` is active (no formal v1.2
milestone declared yet). See [`SESSION-HANDOFF-2026-05-18.md`](SESSION-HANDOFF-2026-05-18.md)
for the latest handoff. Promote to a v1.2 milestone with `$gsd-new-milestone` when the scope
firms up.

## Current Position

Current Milestone: v1.1 (archived) — no v1.2 declared yet
Phase: Out-of-band follow-up (`post-auth-hardening`)
Plan: `.planning/post-auth-hardening-tasks.json`
Status: 3 done, 2 skipped, 1 human_loop, 23 pending (of 29 total)
Last Activity: 2026-05-18
Last Activity Description: Shipped PRs #271 (CI restoration), #272 (C7 docs), #275 (F18a
migration 023). Filed issues #273 (Whilly v4 single-task mode) and #274 (CLAUDE.md stale).
Wrote session handoff at [`SESSION-HANDOFF-2026-05-18.md`](SESSION-HANDOFF-2026-05-18.md).

Progress (v1.1 milestone): [##########] 100%
Progress (post-auth-hardening, by count): 3 done + 2 skipped = 5/29 cleared (~17%)

## Active Scope

**Out-of-band:** [`post-auth-hardening`](post-auth-hardening-tasks.json) plan, scoped by
[`docs/PRD-post-auth-hardening.md`](../docs/PRD-post-auth-hardening.md). Latest handoff with
ready-task list and recommended next step lives at
[`SESSION-HANDOFF-2026-05-18.md`](SESSION-HANDOFF-2026-05-18.md). Handoff files are
date-stamped so the history accumulates rather than being overwritten — start the next
session by reading the most recent one.

**Archived v1.1 evidence:**

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
