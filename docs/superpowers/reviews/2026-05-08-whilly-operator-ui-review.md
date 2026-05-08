# Whilly Operator UI Review

Date: 2026-05-08
Scope: operator WUI dashboard, operator TUI, and shared pause/resume semantics.

## Method

This audit uses the `gsd-ui-review` style as a code-and-test review of the two operator
surfaces. Browser-plugin rendering was not available in this environment, so the review is
grounded in template/TUI code, parity tests, and worker-control behavior tests.

## Current Score

Overall: 17 / 24

- Visual hierarchy: 3 / 4
- Workflow clarity: 3 / 4
- Control parity: 4 / 4
- State feedback: 3 / 4
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

## Remaining Findings

1. WUI refresh can still reset local page state.
   Manual refresh swaps the whole dashboard body, while the TUI preserves in-memory selection and
   view state. Move WUI refresh toward fragment updates or explicit state preservation for selected
   tab, filters, and form inputs.

2. Operator identity controls consume prime screen space.
   Admin bearer and reviewer inputs are always visible in the top bar. Collapse them into an
   operator identity panel with clear current identity state and validation feedback.

3. Mobile tables are functional but cramped.
   The WUI relies heavily on horizontal scrolling and `nowrap`. Convert dense action columns into
   a row detail/action drawer or stacked mobile layout so controls remain easy to hit.

4. Table contracts are not fully identical.
   Tasks show `Claimed by` in WUI and `Worker` in TUI; WUI also exposes `Updated`. Worker table
   order differs. Define a shared operator column contract and let both surfaces intentionally
   diverge only when the medium requires it.

5. Review actions need stronger affordance.
   `A/X/C` are efficient for expert operators but weak for first-time use and risky when
   destructive. Add tooltips, clearer labels in constrained spaces, and confirmation or undo
   affordances for reject/request-changes paths.

## Recommended Next Tasks

1. Preserve WUI tab/filter/form state across refresh and SSE updates.
2. Define a shared TUI/WUI table-column contract for tasks, workers, review queue, and events.
3. Replace always-visible admin token inputs with a compact operator identity panel.
4. Add a mobile row-detail/action layout for the WUI dashboard tables.
