# Milestones

## v1.1 UI parity completion (Started: 2026-05-11)

**Phases planned:** 4 phases, 0 plans

**Goal:**
Close the post-v1.0 WUI/TUI interface gap introduced by inactive WUI artifacts, stale routes, and
missing UI methods so every active operator interface path is canonical, reachable, and verified.

**Planned phases:**
- Phase 13: Canonical UI parity contract.
- Phase 14: WUI method and fragment wiring.
- Phase 15: TUI capability parity.
- Phase 16: UI parity verification and docs.

**Active artifacts:**
- `.planning/ROADMAP.md`
- `.planning/REQUIREMENTS.md`

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
