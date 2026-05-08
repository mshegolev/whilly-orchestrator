# WUI State-Preserving Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve WUI operator state across full-body HTMX refreshes and SSE-driven fragment swaps so the web dashboard behaves closer to the browserless TUI.

**Architecture:** Store only local view state in `sessionStorage`: active surface, filter text, selected review row, and focused operator input. Save before manual refresh / HTMX requests and restore on script initialization and after swaps. Keep worker pause/resume and review decisions backed by the existing APIs; this phase does not add new server state.

**Tech Stack:** Jinja2 dashboard template, HTMX/SSE, browser `sessionStorage`, pytest integration contract tests.

---

## Contract

| State | Before | After |
| --- | --- | --- |
| Active surface | Full refresh returns to Overview | Restores last selected surface. |
| Filter text | Full refresh clears input | Restores filter text and reapplies row visibility. |
| Review selection | Full refresh resets selected row | Restores selected actionable review row index when still present. |
| Input focus | Refresh can move focus away | Restores focus to dashboard filter/admin/reviewer input when applicable. |
| Worker pause state | Backend-driven | Still backend-driven; not stored in browser state. |

## Tasks

### Task 1: Static Contract Tests

**Files:**
- Modify: `tests/integration/test_htmx_dashboard.py`

- [ ] Add assertions that the dashboard script defines state save/restore helpers.
- [ ] Assert manual refresh and HTMX lifecycle events call the helpers.
- [ ] Assert the initial page boot restores state before applying filter and selection.

### Task 2: WUI State Preservation

**Files:**
- Modify: `whilly/api/templates/index.html.j2`

- [ ] Add `sessionStorage` helpers with fail-soft `try/catch`.
- [ ] Persist state from surface switches, filter input, review row selection, and before HTMX requests.
- [ ] Restore state on boot and after HTMX swaps.
- [ ] Keep dashboard quit behavior local to the current page session.

### Task 3: Review Evidence

**Files:**
- Modify: `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`

- [ ] Mark WUI state-preserving refresh as resolved.
- [ ] Keep remaining findings focused on identity panel, mobile table layout, table contract, and action affordances.
