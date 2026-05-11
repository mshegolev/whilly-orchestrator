# Phase 5 Context: Shared Operator Table Contract

## Goal

Define and apply a shared operator table-column contract for the TUI and WUI so both interfaces show
the same task, worker, review, and event concepts unless a medium-specific difference is explicitly
documented.

## Canonical References

- `.planning/ROADMAP.md`
- `.planning/REQUIREMENTS.md`
- `.planning/ROADMAP-ANALYSIS.md`
- `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`
- `whilly/operator_views.py`
- `whilly/cli/tui.py`
- `whilly/api/templates/index.html.j2`
- `whilly/api/templates/_tasks_table.html`
- `whilly/api/templates/_workers_table.html`
- `tests/unit/test_tui.py`
- `tests/integration/test_htmx_dashboard.py`

## Requirements

- OPUI-07: TUI and WUI table columns follow a shared operator contract or document intentional
  differences.

## Success Criteria

1. Task table columns use consistent names for shared concepts such as status, worker, update time,
   and review state.
2. Worker table order and labels are consistent or explicitly documented as medium-specific.
3. Review queue and event fields map to a shared operator contract.
4. Tests pin the shared contract so future UI work can build on stable labels and fields.

## Current Design Direction

Create the smallest shared contract that fits existing code. Prefer a pure data/metadata helper in
`whilly/operator_views.py` or a nearby module, then make TUI and WUI consume that contract where it
does not create unnecessary rendering complexity.

## Out of Scope

- Mobile row-detail layout; that is Phase 6.
- New backend APIs.
- Changing review-decision semantics.
- Changing pause/resume semantics.
