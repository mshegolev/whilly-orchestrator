# Whilly Orchestrator

## What This Is

Whilly is an issue-driven, Postgres-backed AI engineering control plane. It coordinates tasks,
workers, task validation, runner execution, audit events, dashboards, health checks, and human
review points for controlled AI-assisted engineering workflows.

It is not positioned as a fully autonomous AI developer. The product goal is a reliable operator
control plane with explicit verification gates, safe worker controls, honest documentation, and
clear current-vs-target boundaries.

## Core Value

Operators can safely coordinate AI-assisted engineering work with auditable state, human control,
and verification before claiming success.

## Current State

v1.0 shipped on 2026-05-08 with 12 completed phases, 25 completed plans, and 23/23 v1
requirements covered. v1.1 is complete and audited: 7 phases, 12 plans, and 23/23 v1.1
requirements covered. It closes UI parity gaps and adds the operator-adoption controls needed to
run Whilly on another machine against real Jira/GitLab work.

After v1.1, the out-of-band `post-auth-hardening` plan shipped the authentication stack (session
auth, gated password change, flag-gated OIDC header trust, flag-gated WebAuthn second factor) and
a security-review loop that closed the task-id path-traversal sink class (ADR-001 §P1.x, PRs
#303–#318). It is functionally complete: 27 done, 2 skipped as non-issues.

The shipped v1.0 scope includes:

- WUI/TUI operator pause parity and a shared review-decision command path.
- WUI state preservation, compact operator identity, shared table metadata, mobile row actions, and
  clearer review action affordances.
- Secret linting, runner environment allowlists, guard audit evidence, and honest residual-risk
  documentation.
- Profile-native verification command metadata through plan generation, persistence, local
  execution, remote transport, and compliance reporting.
- Operator-triggered rollback points, branch/push preflight, and confirmation-gated restore.
- Explicit configured CI polling and bounded repair attempts with escalation.
- Deterministic governance policy and explicit semantic-memory deferral.

The shipped v1.1 scope includes:

- One canonical TUI/WUI surface, hotkey, action, and route contract.
- Active WUI static/template artifacts use current DOM selectors and supported API paths only.
- Logs, admin, and PRD UI fragments are either fully wired with backend methods and TUI parity or
  explicitly quarantined from active UI scope.
- Focused parity tests fail when a WUI/TUI surface, route, or hotkey drifts.
- Operator-controlled version checks, manual update, and explicit automatic-update policy.
- A GitHub feedback reporter for bug and idea issues.
- Jira-driven work intake can classify incoming issues, persist task history, reread Jira/GitLab
  links, and gate autonomous execution on code/test readiness.

## Current Milestone: v1.2 Adoption & live-ops

**Goal:** Take Whilly from "functionally complete on the dev machine" to "operable against real
Jira/GitLab work on an operator machine" by closing the deferred live-validation and ops backlog.

**Target features:**
- Long-running Jira watcher/daemon wrapping the one-shot `whilly jira poll` so intake is
  continuous instead of manual.
- Live authenticated Jira/GitLab smoke validation on a real operator machine (v1.1 deferred
  validation).
- Full Docker-backed Alembic migration chain run beyond the focused static migration coverage.

**Progress:** Phase 18 complete — full Alembic chain (001→028) validated live in Docker with honest evidence flags, `make migrate-chain` entry point, and a `migration-chain` CI job (first live CI run pending push). Phase 19 complete — `whilly jira smoke` and `whilly gitlab smoke` shipped and validated LIVE against jira.mts.ru (6/6) and gitlab.services.mts.ru (3/3) with redacted persisted reports and verified no-token-leak failure paths.

**Key context:** The out-of-band `post-auth-hardening` plan is functionally complete (27 done,
2 skipped as non-issues) — its auth stack (sessions, flag-gated OIDC header trust, flag-gated
WebAuthn second factor) and the ADR-001 path-sink fixes are prerequisites this milestone builds
on. A1a/A1b are excluded: the defect never reproduced.

## Requirements

### Validated

- [x] WUI and TUI expose the same global worker pause/resume semantics - v1.0.
- [x] Local and remote workers honor global pause at safe checkpoints - v1.0.
- [x] WUI and TUI human-review decisions use one shared review-decision command path - v1.0.
- [x] WUI preserves local operator state across refresh/SSE swaps - v1.0.
- [x] WUI hides admin bearer and reviewer fields in a compact operator identity panel - v1.0.
- [x] Documentation distinguishes current control-plane capabilities from future autonomous-developer
  targets - v1.0.
- [x] WUI and TUI share an explicit operator table-column contract - v1.0.
- [x] WUI mobile table layouts provide row-detail/action ergonomics instead of cramped horizontal
  scroll - v1.0.
- [x] Review actions provide clearer affordances for reject/request-changes paths - v1.0.
- [x] Sandbox/secrets hardening closes the `a3-a4` v6 mission scope without overclaiming VM
  isolation - v1.0.
- [x] Project-profile verification commands are wired into runtime worker verification - v1.0.
- [x] Rollback and branch-protection tooling gives operators an explicit safety net - v1.0.
- [x] CI polling and bounded repair loops are auditable and budgeted - v1.0.
- [x] Governance and semantic-memory scope are explicit, deterministic, and documented - v1.0.

### Validated in v1.1

- [x] Close stale WUI hotkey and route drift from the `feat(wui): adopt 90s/TUI design system`
  commit.
- [x] Ensure every active WUI fragment/control has a supported server method and corresponding
  TUI capability or an explicit documented exclusion.
- [x] Add tests that prevent orphan WUI partials/static files from drifting away from the canonical
  operator UI contract.
- [x] Add Jira work classification, comment-driven approval, GitLab link refresh, and code/test
  readiness gates before autonomous Jira polling.

### Out of Scope

- Fully autonomous production release without human approval - too risky for current control-plane
  scope.
- Full VM/container isolation claims until a real per-task isolation backend is implemented.
- Opaque semantic memory as an authority source - deterministic event/task/PR history must remain
  primary.
- Auto-merge by default - externally visible repository mutation must stay opt-in and auditable.

## Context

- Python 3.12 package with domain code in `whilly/core`, adapters in `whilly/adapters`, workers in
  `whilly/worker`, and operator interfaces in `whilly/api/templates/index.html.j2` and
  `whilly/cli/tui.py`.
- Superpowers artifacts remain as detailed evidence in `docs/superpowers/plans/` and
  `docs/superpowers/reviews/`.
- v1.0 and v1.1 milestone archives live in `.planning/milestones/`.
- Phase execution evidence remains in `.planning/phases/`.
- `docs/CODEX-MISSION.md` remains the current Factory mission and boundary reference.

## Constraints

- **Control-plane framing**: Do not describe Whilly as a fully autonomous AI developer unless code
  evidence supports that claim.
- **Compatibility**: Preserve existing API payloads, TUI hotkeys, worker flows, Docker demo paths,
  and dashboard SSE/HTMX behavior.
- **Security**: Do not commit secrets. Treat bootstrap tokens, worker bearers, Slack tokens, model
  provider keys, and database URLs as sensitive.
- **Verification**: Phase completion needs focused tests first; broaden when behavior touches
  workers, transport, migrations, or operator workflows.
- **Planning**: GSD is canonical for roadmap state; superpowers plans remain detailed
  implementation evidence and archive.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Treat Whilly as a control plane, not a fully autonomous developer | Matches current implementation and avoids overclaiming target-pack features | Good |
| Keep superpowers artifacts as evidence instead of copying every detail into GSD | GSD stays readable while detailed plans remain linked | Good |
| Start the GSD roadmap at current UI backlog, then continue into doc-pack hardening | Matches the active work stream while preserving the larger roadmap | Good |
| Put the shared table contract before mobile row actions | Mobile layout should use stable shared labels and field mapping | Good |
| Put `a3-a4` sandbox/secrets before profile-native verification wiring | Hardens command/env handling before more commands flow from profiles | Good |
| Store only local WUI view state in browser storage | Worker pause/resume and review decisions must remain backend/audit state | Good |
| Keep rollback restore operator-triggered and confirmation-gated | Prevents silent destructive branch mutation | Good |
| Make CI polling explicit and bounded repair budgeted | Avoids claims of continuous polling, auto-merge, production recovery, or unbounded repair | Good |
| Defer semantic memory from current scope | Deterministic events, task history, PR evidence, and verification logs remain authoritative | Good |
| Treat hotfix as urgency, not a primary Jira work kind | Hotfix can apply to bugs, tasks, or DevOps changes and should add safety gates instead of changing the whole taxonomy | Good |
| Gate autonomous Jira work on code/test readiness | Linked repos, GitLab refs, unit tests, and verification commands must be known before workers mutate code | Good |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-06-11 after starting milestone v1.2*
