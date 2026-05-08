# Compact Operator Identity Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move admin bearer and reviewer inputs out of the always-visible topbar into a compact operator identity panel without changing review or worker-control APIs.

**Architecture:** Keep existing input ids and JS consumers so behavior stays compatible. Wrap the credentials in a closed-by-default `<details>` panel with a concise summary, preserve keyboard/focus behavior, and make state restoration open the panel before focusing a restored hidden identity input.

**Tech Stack:** Jinja2 dashboard template, native HTML `<details>`, CSS, pytest integration contract tests.

---

## Contract

| Area | Before | After |
| --- | --- | --- |
| Admin bearer input | Always visible in topbar | Inside compact identity panel. |
| Reviewer input | Always visible in topbar | Inside compact identity panel. |
| Pause/resume controls | Visible | Still visible. |
| Review actions | Read same input ids | Still read same input ids. |
| State restore | Can focus identity inputs | Opens identity panel before focusing hidden input. |

## Tasks

### Task 1: Static Contract Tests

**Files:**
- Modify: `tests/integration/test_htmx_dashboard.py`

- [ ] Assert `operator-identity-panel` uses `<details>`.
- [ ] Assert admin/reviewer inputs remain present but live inside the panel.
- [ ] Assert worker pause/resume buttons remain outside the identity panel.
- [ ] Assert focus restore can open the identity panel before focusing credentials.

### Task 2: Template And CSS

**Files:**
- Modify: `whilly/api/templates/index.html.j2`

- [ ] Add compact identity panel markup.
- [ ] Add restrained topbar/panel styling.
- [ ] Preserve existing input ids and JS selectors.
- [ ] Update `restoreDashboardFocus` to open containing details panel.

### Task 3: Review Evidence

**Files:**
- Modify: `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`

- [ ] Mark identity panel finding as resolved.
- [ ] Keep remaining findings focused on mobile table layout, table contract, and action affordances.
