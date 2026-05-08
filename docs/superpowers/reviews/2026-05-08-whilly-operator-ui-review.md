# Whilly Operator UI Review

Date: 2026-05-08
Scope: operator WUI dashboard, operator TUI, and shared pause/resume semantics.

## Method

This audit uses the `gsd-ui-review` style as a code-and-test review of the two operator
surfaces. Browser-plugin rendering was not available in this environment, so the review is
grounded in template/TUI code, parity tests, and worker-control behavior tests.

## Current Score

Overall: 18 / 24

- Visual hierarchy: 3 / 4
- Workflow clarity: 3 / 4
- Control parity: 4 / 4
- State feedback: 4 / 4
- Responsive resilience: 2 / 4
- Risk and recovery: 2 / 4

## Resolved In This Pass

- `p` now means `pause workers` in both TUI and WUI.
- `R` now means `resume workers` in both TUI and WUI.
- Lowercase `r` remains manual refresh in the TUI; WUI keeps its refresh button and live polling.
- The old WUI-only `pause refresh` behavior is removed. Pausing workers no longer freezes the
  dashboard; the interface keeps updating while workers are paused.
- Local and remote workers check the shared control state at safe checkpoints, stop claiming new
  work while paused, and release active tasks with `operator_pause`.
- WUI review hotkeys `j/k/a/x/c` now operate only on the Compliance surface, matching the TUI.
- WUI/API and TUI human-review decisions now use a shared review-decision service, so event type
  mapping and payload construction stay aligned across both operator surfaces.
- WUI refresh now preserves local operator state across manual refresh, HTMX refresh, and SSE-driven
  fragment swaps: active surface, filter text, selected review row, and dashboard input focus.

## Remaining Findings

1. Operator identity controls consume prime screen space.
   Admin bearer and reviewer inputs are always visible in the top bar. Collapse them into an
   operator identity panel with clear current identity state and validation feedback.

2. Mobile tables are functional but cramped.
   The WUI relies heavily on horizontal scrolling and `nowrap`. Convert dense action columns into
   a row detail/action drawer or stacked mobile layout so controls remain easy to hit.

3. Table contracts are not fully identical.
   Tasks show `Claimed by` in WUI and `Worker` in TUI; WUI also exposes `Updated`. Worker table
   order differs. Define a shared operator column contract and let both surfaces intentionally
   diverge only when the medium requires it.

4. Review actions need stronger affordance.
   `A/X/C` are efficient for expert operators but weak for first-time use and risky when
   destructive. Add tooltips, clearer labels in constrained spaces, and confirmation or undo
   affordances for reject/request-changes paths.

## Recommended Next Tasks

1. Replace always-visible admin token inputs with a compact operator identity panel.
2. Add a mobile row-detail/action layout for the WUI dashboard tables.
3. Define a shared TUI/WUI table-column contract for tasks, workers, review queue, and events.
4. Add clearer review-action affordances for reject/request-changes paths.
