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

## Requirements

### Validated

- [x] WUI and TUI expose the same global worker pause/resume semantics.
- [x] Local and remote workers honor global pause at safe checkpoints.
- [x] WUI and TUI human-review decisions use one shared review-decision command path.
- [x] WUI preserves local operator state across refresh/SSE swaps.
- [x] WUI hides admin bearer and reviewer fields in a compact operator identity panel.
- [x] Documentation distinguishes current control-plane capabilities from future autonomous-developer targets.

### Active

- [ ] WUI mobile table layouts provide row-detail/action ergonomics instead of cramped horizontal scroll.
- [ ] WUI and TUI share an explicit operator table-column contract.
- [ ] Review actions provide clearer affordances for reject/request-changes paths.
- [ ] Project-profile verification commands are wired into runtime worker verification.
- [ ] Sandbox/secrets hardening closes the `a3-a4` v6 mission scope without overclaiming VM isolation.
- [ ] Rollback and branch-protection tooling gives operators an explicit safety net.
- [ ] CI polling and bounded repair loops are auditable and budgeted.
- [ ] Governance and semantic-memory scope are explicit, deterministic, and documented.

### Out of Scope

- Fully autonomous production release without human approval - too risky for current control-plane scope.
- Full VM/container isolation claims until a real per-task isolation backend is implemented.
- Opaque semantic memory as an authority source - deterministic event/task/PR history must remain primary.
- Auto-merge by default - externally visible repository mutation must stay opt-in and auditable.

## Context

- Python 3.12 package with domain code in `whilly/core`, adapters in `whilly/adapters`, workers in
  `whilly/worker`, and operator interfaces in `whilly/api/templates/index.html.j2` and
  `whilly/cli/tui.py`.
- Superpowers artifacts remain as detailed evidence in `docs/superpowers/plans/` and
  `docs/superpowers/reviews/`.
- `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md` is the source backlog for
  doc-pack alignment and v6 hardening.
- `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md` is the source backlog for
  remaining operator UI quality work.

## Constraints

- **Control-plane framing**: Do not describe Whilly as a fully autonomous AI developer unless code
  evidence supports that claim.
- **Compatibility**: Preserve existing API payloads, TUI hotkeys, worker flows, Docker demo paths,
  and dashboard SSE/HTMX behavior.
- **Security**: Do not commit secrets. Treat bootstrap tokens, worker bearers, Slack tokens, model
  provider keys, and database URLs as sensitive.
- **Verification**: Phase completion needs focused tests first; broaden when behavior touches
  workers, transport, migrations, or operator workflows.
- **Planning**: GSD is canonical for current roadmap state; superpowers plans remain detailed
  implementation evidence and archive.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Treat Whilly as a control plane, not a fully autonomous developer | Matches current implementation and avoids overclaiming target-pack features | Good |
| Keep superpowers artifacts as evidence instead of copying every detail into GSD | GSD stays readable while detailed plans remain linked | Good |
| Start the GSD roadmap at current UI backlog, then continue into doc-pack hardening | Matches the user's active work stream while preserving the larger roadmap | Pending |
| Store only local WUI view state in browser storage | Worker pause/resume and review decisions must remain backend/audit state | Good |

---
*Last updated: 2026-05-08 after migrating superpowers artifacts into GSD*
