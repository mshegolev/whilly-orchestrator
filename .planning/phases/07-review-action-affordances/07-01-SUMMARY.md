---
phase: 07-review-action-affordances
plan: 01
subsystem: ui
tags: [operator-ui, wui, tui, human-review, accessibility, pytest, ruff]

requires:
  - phase: 02-shared-review-decision-path
    provides: Shared TUI/WUI review-decision service path and decision literals.
  - phase: 05-shared-operator-table-contract
    provides: Shared operator table labels and field order.
  - phase: 06-mobile-wui-row-actions
    provides: Mobile stacked-row table layout and 44 px review action targets.
provides:
  - Clear WUI review action labels, titles, prompt recovery, and status copy.
  - Clear TUI review hotkey help while preserving compact a/x/c controls.
  - Updated operator UI audit resolving Phase 7 review action affordance findings.
affects: [operator-ui, dashboard, tui, human-review, OPUI-08]

tech-stack:
  added: []
  patterns:
    - TDD RED/GREEN commits for UI copy and static contract coverage.
    - Source-level WUI JavaScript assertions for pre-fetch prompt recovery branches.

key-files:
  created:
    - .planning/phases/07-review-action-affordances/07-01-SUMMARY.md
  modified:
    - whilly/api/templates/index.html.j2
    - whilly/cli/tui.py
    - tests/integration/test_htmx_dashboard.py
    - tests/unit/test_tui.py
    - docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md

key-decisions:
  - "Review action affordance work remains UI-only: backend review endpoint, decision literals, and shared service path were preserved."
  - "Reject and request-changes WUI prompts require non-empty trimmed input and return before fetch on cancel or blank input."

patterns-established:
  - "WUI review action tests assert visible labels, titles, aria labels, decision literals, and prompt recovery source order."
  - "TUI review help can become more explicit without changing expert a/x/c action cells or key handling."

requirements-completed: [OPUI-08]

duration: 5min
completed: 2026-05-08
---

# Phase 7 Plan 01: Review Action Affordances Summary

**WUI and TUI human-review actions now expose clear approve/reject/changes affordances while preserving the existing review endpoint, decision literals, hotkeys, and shared service path.**

## Performance

- **Duration:** 5 min
- **Started:** 2026-05-08T12:29:17Z
- **Completed:** 2026-05-08T12:34:07Z
- **Tasks:** 3
- **Files modified:** 5 implementation/test/doc files, plus this summary

## Accomplishments

- WUI review buttons now read `A Approve`, `X Reject`, and `C Changes` with exact titles, aria labels, and unchanged `data-review-decision` literals.
- WUI reject/request-changes prompts now cancel or reject blank input before `fetch`, and successful status copy names the recorded decision.
- TUI help now spells out `a=Approve review`, `x=Reject review`, and `c=Changes` while the compact table action cell remains `a/x/c`.
- The operator UI review audit now records Phase 5, Phase 6, and Phase 7 as resolved and narrows residual risk to true undo semantics and live browser/screen-reader QA.

## Task Commits

1. **Task 1: Pin and implement WUI review action affordances**
   - `950186f` test: add failing WUI affordance tests
   - `13ffde1` feat: clarify WUI review action affordances
2. **Task 2: Pin and implement TUI review action copy without key-path changes**
   - `886e0d2` test: add failing TUI review copy tests
   - `5b3d3f1` feat: clarify TUI review action help
3. **Task 3: Update the operator UI review audit and run final verification**
   - `8759207` docs: resolve review affordance audit findings

## Files Created/Modified

- `whilly/api/templates/index.html.j2` - WUI review labels, titles, prompt recovery branches, and status copy.
- `whilly/cli/tui.py` - TUI review-action help text.
- `tests/integration/test_htmx_dashboard.py` - WUI rendered/static contract tests for labels, prompt recovery, endpoint, click dispatch, hotkeys, and CSS.
- `tests/unit/test_tui.py` - TUI rendered help, validation, selected-gap, key handling, and shared service path tests.
- `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md` - Audit update resolving stale OPUI-08 affordance findings and narrowing residual risks.
- `.planning/phases/07-review-action-affordances/07-01-SUMMARY.md` - Execution summary.

## Decisions Made

- Kept the work UI-only. No backend route semantics, shared review service path, decision literals, pause/resume behavior, or mobile table layout semantics were changed.
- Used source-order assertions around `submitReviewDecision` to prove cancel/blank reject and changes branches update status and return before the first review `fetch`.

## Deviations from Plan

None - plan executed exactly as written.

## Auth Gates

None.

## Issues Encountered

None. Docker-backed dashboard integration tests were available and were not skipped.

## Verification

- `.venv/bin/python -m pytest -q tests/integration/test_htmx_dashboard.py::test_dashboard_review_actions_use_clear_affordance_contract tests/integration/test_htmx_dashboard.py::test_dashboard_review_prompt_recovery_copy_and_hotkey_contract tests/integration/test_htmx_dashboard.py::test_dashboard_mobile_labels_match_table_contract_and_review_actions_are_accessible` - 3 passed.
- `.venv/bin/python -m pytest -q tests/unit/test_tui.py::test_render_tui_overview_includes_surfaces_and_hotkeys tests/unit/test_tui.py::test_render_tui_compliance_shows_clear_review_action_help tests/unit/test_tui.py::test_handle_tui_key_switches_views_filter_pause_refresh_review_actions_and_quit tests/unit/test_tui.py::test_apply_pending_review_action_requires_reviewer tests/unit/test_tui.py::test_apply_pending_review_action_requires_selected_actionable_gap tests/unit/test_tui.py::test_record_human_review_decision_uses_shared_service` - 6 passed.
- `.venv/bin/python -m pytest -q tests/integration/test_htmx_dashboard.py tests/unit/test_tui.py` - 46 passed.
- `.venv/bin/python -m ruff check whilly/cli/tui.py tests/integration/test_htmx_dashboard.py tests/unit/test_tui.py` - All checks passed.
- `.venv/bin/python -c 'from pathlib import Path; text = Path("docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md").read_text(); assert "Review actions need stronger affordance." not in text; assert "Add clearer review-action affordances for reject/request-changes paths." not in text'` - passed.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

OPUI-08 is complete from the UI/test/audit perspective. Remaining documented risks are outside this plan: true undo requires backend/audit reversal semantics, and live browser/screen-reader QA still needs a suitable environment.

## Self-Check: PASSED

- Summary file exists at `.planning/phases/07-review-action-affordances/07-01-SUMMARY.md`.
- Recorded task commits exist: `950186f`, `13ffde1`, `886e0d2`, `5b3d3f1`, `8759207`.

---
*Phase: 07-review-action-affordances*
*Completed: 2026-05-08*
