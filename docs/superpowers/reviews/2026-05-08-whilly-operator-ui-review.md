# Whilly Operator UI Review

Date: 2026-05-08
Scope: operator WUI dashboard, operator TUI, and shared pause/resume semantics.

## Method

This audit uses the `gsd-ui-review` style as a code-and-test review of the two operator
surfaces. Browser-plugin rendering was not available in this environment, so the review is
grounded in template/TUI code, parity tests, and worker-control behavior tests.

## Current Score

Overall: 22 / 24

- Visual hierarchy: 4 / 4
- Workflow clarity: 4 / 4
- Control parity: 4 / 4
- State feedback: 4 / 4
- Responsive resilience: 3 / 4
- Risk and recovery: 3 / 4

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
- WUI admin bearer and reviewer inputs now live in a compact `Operator identity` panel, while
  pause/resume and filter controls stay visible in the primary topbar.
- Phase 5 shared table contracts now centralize table labels and field order for tasks, workers,
  review gaps, and events, with intentional medium-specific omissions documented in tests.
- Phase 6 mobile stacked rows now replace horizontal scrolling for the dense operator tables, and
  review action targets retain the 44 px mobile tap-area contract.
- Phase 7 Review action affordances now make WUI buttons read `A Approve`, `X Reject`, and
  `C Changes`; reject and request-changes prompts cancel or reject blank input before posting; the
  existing review endpoint, decision literals, and Compliance-surface hotkeys are preserved; and
  TUI help now spells out `a=Approve review`, `x=Reject review`, and `c=Changes`.

## Remaining Residual Risks

1. True undo remains deferred.
   Reject/request-changes actions now require clearer intent and recover from cancel or blank
   prompt input before posting, but reversing a recorded decision still requires backend/audit
   reversal semantics that are outside this UI-only pass.

2. Browser and assistive-technology QA still need a live pass.
   This review is grounded in source and automated tests. A future verification pass should capture
   live browser behavior and screen-reader/keyboard traversal evidence for the WUI dashboard.

## Recommended Next Tasks

1. Add a backend/audit-reversal phase if operators need true undo for recorded human-review
   decisions.
2. Run live browser and screen-reader QA for the WUI dashboard once the environment supports it.
