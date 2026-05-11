# Roadmap: Whilly Orchestrator

## Overview

GSD is the canonical high-level execution plan for Whilly. Completed milestone evidence is archived
under `.planning/milestones/`; `.planning/ROADMAP.md` stays small and describes only the active or
next milestone state.

## Milestones

| Milestone | Status | Shipped | Evidence |
|-----------|--------|---------|----------|
| v1.0 | Shipped | 2026-05-08 | `.planning/milestones/v1.0-ROADMAP.md`, `.planning/milestones/v1.0-REQUIREMENTS.md`, `.planning/milestones/v1.0-MILESTONE-AUDIT.md` |
| v1.1 UI parity completion | Shipped | 2026-05-11 | `.planning/milestones/v1.1-ROADMAP.md`, `.planning/milestones/v1.1-REQUIREMENTS.md`, `.planning/milestones/v1.1-MILESTONE-AUDIT.md`, `.planning/milestones/v1.1-RETROSPECTIVE.md` |

## Current Milestone

No active milestone.

## Completed Milestone Summary

v1.1 closed the post-v1.0 WUI/TUI interface gap, added explicit version update controls, added a
GitHub feedback reporter, and introduced Jira-driven intake with classification, history refresh,
repo hints, and code/test readiness gates.

Completed phases:
- Phase 13: Canonical UI parity contract.
- Phase 13.1: Version update checks and manual/automatic update modes.
- Phase 13.2: GitHub feedback issue reporter.
- Phase 14: WUI method and fragment wiring.
- Phase 15: TUI capability parity.
- Phase 16: UI parity verification and docs.
- Phase 17: Jira work classification and code readiness routing.

## Deferred Scope

- Browser and assistive-technology QA for the full WUI operator workflow.
- Live authenticated Jira/GitLab smoke on a real operator machine.
- Full Docker-backed Alembic chain run outside the focused static migration coverage.
- Long-running Jira watcher/daemon wrapper around the current one-shot `whilly jira poll`.
- New operator modules beyond the pulled logs/admin/PRD artifacts.
- Replacement of the current Jinja/HTMX WUI or Rich TUI architecture.

## Next Step

Start the next milestone with `$gsd-new-milestone`.
