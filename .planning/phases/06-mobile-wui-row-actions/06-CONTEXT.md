# Phase 6 Context: Mobile WUI Row Actions

## Goal

Make WUI tables usable on mobile without relying on cramped horizontal scrolling for critical row
actions.

## Canonical References

- `.planning/ROADMAP.md`
- `.planning/REQUIREMENTS.md`
- `.planning/ROADMAP-ANALYSIS.md`
- `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`
- `whilly/api/templates/index.html.j2`
- `whilly/api/templates/_tasks_table.html`
- `whilly/api/templates/_workers_table.html`
- `tests/integration/test_htmx_dashboard.py`

## Requirements

- OPUI-06: WUI mobile table layouts expose row details/actions without cramped horizontal scrolling.

## Success Criteria

1. Mobile review rows expose details and actions in a touch-friendly layout.
2. Task, worker, and event rows remain scannable below 900px width.
3. Critical action buttons do not overlap row text or require precise horizontal scrolling.
4. Existing desktop table behavior remains unchanged.
5. Phase 5 table-contract labels are preserved or intentionally adapted for mobile display.

## Current Design Direction

Use the Phase 5 table contract to drive labels, then add responsive row-detail/action layout in CSS
and templates. Prefer template and CSS changes over new server endpoints unless the existing
partials cannot express the layout cleanly.

## Out of Scope

- Changing backend task/review/worker APIs.
- Changing TUI layout beyond preserving the shared contract.
- Changing pause/resume or review-decision semantics.
