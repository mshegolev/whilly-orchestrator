# Phase 7 Context: Review Action Affordances

## Goal

Make reject and request-changes review actions clearer and safer without slowing expert operators
who already use `a`, `x`, and `c` hotkeys in the TUI and WUI.

## Canonical References

- `.planning/ROADMAP.md`
- `.planning/REQUIREMENTS.md`
- `.planning/ROADMAP-ANALYSIS.md`
- `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`
- `.planning/phases/06-mobile-wui-row-actions/06-UI-SPEC.md`
- `.planning/phases/06-mobile-wui-row-actions/06-VERIFICATION.md`
- `whilly/api/templates/index.html.j2`
- `whilly/cli/tui.py`
- `tests/integration/test_htmx_dashboard.py`
- `tests/unit/test_tui.py`

## Requirements

- OPUI-08: Reject and request-changes actions have clearer labels, tooltips, and recovery
  affordances.

## Success Criteria

1. Review action controls communicate approve, reject, and request-changes clearly.
2. Destructive or blocking decisions have confirmation, undo, or stronger comment affordances.
3. TUI and WUI hotkeys remain efficient for expert operators.

## Implementation Decisions

### Shared action language

- Keep the hotkey mapping unchanged: `a` approves, `x` rejects, and `c` requests changes.
- Use the same visible action names across WUI and TUI: Approve, Reject, and Changes.
- WUI buttons may grow beyond the Phase 6 `A/X/C` labels, but must keep hotkey cues in visible text,
  title text, or adjacent action help.
- TUI should keep a compact action column and hotkey footer, but must spell out the meaning of
  `a/x/c` where operators can see it without external documentation.

### Recovery and confirmation

- Prefer stronger comment affordances over adding new backend undo semantics in this phase.
- WUI reject and request-changes paths should not submit silently after an empty prompt; cancellation
  or blank required text must leave the task unchanged and show operator feedback.
- TUI remains browserless and hotkey-first; do not add blocking multi-step text entry unless the
  existing key loop can support it safely. A clear built-in requested-changes reason is acceptable
  for TUI as long as the action labels and status feedback are explicit.

### Scope boundaries

- Do not change the shared `record_review_decision` backend path, API route, event type mapping, or
  audit payload shape except for already-supported `comment` and `requested_changes` fields.
- Do not change pause/resume semantics, mobile stacked table layout, dashboard refresh behavior, or
  task/worker/event table contracts.
- Do not introduce new JavaScript frameworks, modal libraries, server endpoints, or persistent
  browser storage for review recovery.

## Existing Code Insights

### WUI

- Review controls are rendered in `whilly/api/templates/index.html.j2` inside the Compliance table
  as three buttons with `data-review-decision="approved|rejected|changes_requested"`.
- Phase 6 added task-specific `aria-label` values and mobile 44px touch-target CSS for those buttons.
- `submitReviewDecision(row, decision)` prompts for `Review comment` on reject and `Requested
  changes` on request-changes before posting to `/api/v1/tasks/{task_id}/human-review`.
- WUI keyboard shortcuts already submit `a`, `x`, and `c` only on the Compliance surface.
- The existing hotkey legend uses compact copy (`a=approve`, `x=reject`, `c=changes`); Phase 7 can
  clarify this without changing the dispatch code.

### TUI

- `whilly/cli/tui.py` maps `a`, `x`, and `c` to the same decision strings through
  `handle_tui_key`.
- `_record_human_review_decision` already uses the shared pipeline service and sends a fixed
  requested-changes note for TUI `changes_requested`.
- The Compliance table currently shows `a/x/c` for actionable rows and the footer says
  `a=approve  x=reject  c=changes`.
- TUI should remain single-key and browserless; clearer copy belongs in the footer/caption/action
  cell rather than a new blocking text-entry flow.

### Tests

- `tests/integration/test_htmx_dashboard.py` already verifies review button aria labels, decision
  data attributes, visible Phase 6 labels, mobile CSS, and keyboard state-preservation behavior.
- `tests/unit/test_tui.py` already verifies review hotkey mappings, Compliance table rendering,
  and the shared TUI review-decision service call.
- Phase 7 tests should pin clearer WUI visible labels, titles/tooltips, prompt copy, unchanged POST
  path, and TUI rendered action language while preserving hotkey efficiency.

### Documentation

- `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md` currently lists review action
  affordances as a remaining finding. If Phase 7 resolves the finding, update that artifact so the
  audit trail does not stay stale.

## Deferred Ideas

- True undo for submitted review decisions belongs in a later backend/audit phase because it needs
  persisted reversal semantics.
- Rich browser dialogs or modal components are deferred until the dashboard has a real component
  system.

---
*Phase: 07-review-action-affordances*
*Context gathered: 2026-05-08*
