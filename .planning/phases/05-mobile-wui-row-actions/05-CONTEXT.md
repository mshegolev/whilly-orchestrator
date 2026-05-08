# Phase 5 Context: Mobile WUI Row Actions

## Goal

Make WUI tables usable on mobile without relying on cramped horizontal scrolling for critical row
actions.

## Canonical References

- `.planning/ROADMAP.md`
- `.planning/REQUIREMENTS.md`
- `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`
- `whilly/api/templates/index.html.j2`
- `tests/integration/test_htmx_dashboard.py`

## Requirements

- OPUI-06: WUI mobile table layouts expose row details/actions without cramped horizontal scrolling.

## Success Criteria

1. Mobile review rows expose details and actions in a touch-friendly layout.
2. Task, worker, and event rows remain scannable below 900px width.
3. Critical action buttons do not overlap row text or require precise horizontal scrolling.
4. Existing desktop table behavior remains unchanged.

## Current Design Direction

Use responsive row-detail/action layout for WUI tables. Keep desktop table behavior stable. Prefer
CSS and template changes over new server endpoints unless the existing partials cannot express the
layout cleanly.

## Out of Scope

- Changing backend task/review/worker APIs.
- Changing TUI layout in this phase.
- Changing pause/resume or review-decision semantics.
