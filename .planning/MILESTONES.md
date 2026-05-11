# Milestones

## v1.1 UI parity completion (Shipped: 2026-05-11)

**Phases completed:** 7 phases, 12 plans, 0 tasks

**Key accomplishments:**
- Shared TUI/WUI operator surface, hotkey, route, and action contracts now drive the active
  dashboard and TUI behavior.
- Stale WUI hotkeys, selectors, and worker routes were brought onto the canonical five-surface
  contract with focused regression coverage.
- Logs/admin/PRD fragments were classified so inactive UI artifacts are not silently treated as
  active operator surfaces.
- Version checks, manual update, and explicit automatic-update policy were added for classic
  operator-controlled package lifecycle behavior.
- `whilly feedback` now creates explicit GitHub bug/idea reports with runtime context and secret
  redaction.
- Jira intake can classify feature/bug/task/devops work, persist issue history, reread comments and
  links, derive repo hints, and gate autonomous execution on code/test readiness.

**Archives:**
- `.planning/milestones/v1.1-ROADMAP.md`
- `.planning/milestones/v1.1-REQUIREMENTS.md`
- `.planning/milestones/v1.1-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.1-RETROSPECTIVE.md`

**Known deferred validation:**
- Live authenticated Jira/GitLab smoke on a real operator machine.
- Full Docker-backed Alembic chain run outside the focused static migration coverage.
- Long-running Jira watcher/daemon wrapper around one-shot `whilly jira poll`.

---

## v1.0 milestone (Shipped: 2026-05-08)

**Phases completed:** 12 phases, 25 plans, 0 tasks

**Key accomplishments:**
- WUI/TUI operator pause, review decisions, refresh behavior, identity controls, table metadata,
  mobile rows, and review actions were aligned for operator workflows.
- Secret linting, runner environment allowlists, guard audits, verification redaction, and
  residual-risk docs closed the scoped `a3-a4` hardening work.
- Project-profile verification commands now flow through generated plans, persistence, local worker
  execution, remote transport, and compliance reporting.
- Rollback points, branch/push preflight, and confirmation-gated restore provide an operator safety
  net for risky repository mutation.
- Explicit configured CI polling and bounded repair attempts create auditable escalation instead of
  unbounded retry or auto-merge behavior.
- Governance policy is deterministic and semantic memory is explicitly deferred from current scope.

**Archives:**
- `.planning/milestones/v1.0-ROADMAP.md`
- `.planning/milestones/v1.0-REQUIREMENTS.md`
- `.planning/milestones/v1.0-MILESTONE-AUDIT.md`

**Known deferred validation:**
- Live authenticated GitHub CI provider smoke against a real PR with checks.
- Browser and assistive-technology QA for mobile WUI/review affordance polish.

---
