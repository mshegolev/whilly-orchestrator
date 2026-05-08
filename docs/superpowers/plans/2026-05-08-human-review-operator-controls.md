# Human Review Operator Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add real approve, reject, and request-changes controls for pending human-review gaps in both the browser dashboard and the browserless TUI.

**Architecture:** Keep `POST /api/v1/tasks/{task_id}/human-review` as the single HTTP write contract. The web dashboard will call that endpoint from same-origin JavaScript using an operator-entered admin bearer token and reviewer identity, so no admin token is embedded in public HTML. The TUI will use the existing Postgres-backed operator path and record the same audit event shape with a required `--reviewer` or `WHILLY_OPERATOR_EMAIL` identity. Shared `ReviewGap.actionable` metadata prevents controls from writing decisions for inferred/read-only gaps such as missing acceptance criteria.

**Tech Stack:** Python 3.12, FastAPI/Jinja2/HTMX, Rich TUI, pytest, existing `TaskRepository.record_task_event`.

---

## File Structure

- `whilly/operator_views.py`: add `ReviewGap.actionable` and keep human-review event gaps distinguishable from inferred gaps.
- `tests/unit/test_operator_views.py`: pin actionable review gaps and rejected/changes-requested behavior.
- `whilly/cli/tui.py`: add reviewer option, review selection hotkeys, and direct DB-backed audit event recording.
- `tests/unit/test_tui.py`: test hotkey state, rendering, and action recording with monkeypatched recorder.
- `whilly/api/templates/index.html.j2`: add TUI-like web controls, action hotkeys, and admin-token/reviewer fields.
- `tests/integration/test_htmx_dashboard.py`: assert WUI controls render, use the existing admin API, and do not embed admin tokens.
- `whilly/compliance/__init__.py`: promote human-review evidence after dashboard/TUI controls exist.
- `tests/unit/test_compliance_report.py`: update the compliance expectation for the closed dashboard/TUI controls gap.

## Task 1: Shared Actionable Review Gap Model

**Files:**
- Modify: `whilly/operator_views.py`
- Modify: `tests/unit/test_operator_views.py`

- [ ] **Step 1: Add assertions for actionable real human-review gaps**

Extend `test_human_review_required_event_opens_gap_until_approved` so the expected `ReviewGap` includes `actionable=True`.

- [ ] **Step 2: Add rejected and changes-requested regression coverage**

Add one test that builds snapshots with `human_review.rejected` and `human_review.changes_requested`; both must remain open review gaps with `actionable=True` and the correct reason.

- [ ] **Step 3: Add `ReviewGap.actionable`**

Add `actionable: bool = False` to the dataclass. In `_review_gaps()`, set `actionable=True` only for gaps derived from a real `task.human_review.required` event with a non-approved decision.

- [ ] **Step 4: Run model tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_operator_views.py
```

Expected: all operator view tests pass.

## Task 2: Browserless TUI Review Actions

**Files:**
- Modify: `whilly/cli/tui.py`
- Modify: `tests/unit/test_tui.py`

- [ ] **Step 1: Add TUI action state tests**

Add tests for `j/k` selection and `a/x/c` pending decisions on the compliance surface. Keep `r` as refresh.

- [ ] **Step 2: Add renderer assertions**

Update TUI render tests to include `j/k=select`, `a=approve`, `x=reject`, and `c=changes` in the hotkey caption and an `Actions` column for actionable rows.

- [ ] **Step 3: Add action recording test**

Monkeypatch the recording helper and verify `_apply_pending_review_action()` records the selected gap with decision, stage id, and reviewer. Also verify no write happens without reviewer.

- [ ] **Step 4: Implement TUI action path**

Add `--reviewer`, env fallback `WHILLY_OPERATOR_EMAIL`, pending action state, selected gap handling, `_record_human_review_decision()`, and `_apply_pending_review_action()`.

- [ ] **Step 5: Run TUI tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_tui.py
```

Expected: all TUI tests pass.

## Task 3: Web Dashboard Controls With TUI-Like Hotkeys

**Files:**
- Modify: `whilly/api/templates/index.html.j2`
- Modify: `tests/integration/test_htmx_dashboard.py`

- [ ] **Step 1: Add HTML assertions**

Extend `test_dashboard_renders_compliance_gaps_and_events` to assert:

- admin bearer and reviewer fields render,
- review rows carry task/stage/actionable data attributes,
- approve/reject/request-changes buttons render for actionable gaps,
- hotkey labels match TUI (`j/k`, `a`, `x`, `c`),
- no seeded admin token appears in the HTML.

- [ ] **Step 2: Add web controls**

Add compact terminal-like controls to the topbar and an action column in `#review-gaps`. Action buttons call the existing admin endpoint via `fetch` with an operator-entered bearer token.

- [ ] **Step 3: Add keyboard parity**

Add JS selection and hotkeys:

- `j` / `k`: select next/previous actionable review gap,
- `a`: approve selected gap,
- `x`: reject selected gap,
- `c`: request changes for selected gap,
- existing `q`, `r`, `p`, `/`, `1-5` remain unchanged.

- [ ] **Step 4: Run dashboard tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/integration/test_htmx_dashboard.py
```

Expected: dashboard integration tests pass.

## Task 4: Compliance Probe Update

**Files:**
- Modify: `whilly/compliance/__init__.py`
- Modify: `tests/unit/test_compliance_report.py`

- [ ] **Step 1: Update compliance test**

Update `test_human_review_compliance_reports_admin_controls_and_remaining_ui_gap` so it expects `PASS`, dashboard/TUI control evidence, and no remaining dashboard/TUI gap.

- [ ] **Step 2: Update compliance logic**

Teach `_human_review_status()`, `_human_review_evidence()`, and `_human_review_gap()` to recognize:

- the admin API endpoint,
- release-hold integration coverage,
- TUI review action implementation,
- WUI review action controls.

- [ ] **Step 3: Regenerate and inspect compliance report**

Run:

```bash
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
rg -n "Human review checkpoint model|dashboard/TUI operator controls" out/compliance-report.md
```

Expected: human-review row is `PASS`; stale dashboard/TUI gap text does not appear.

## Task 5: Phase Verification, Commit, Push

**Files:**
- All files above plus this plan file.

- [ ] **Step 1: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_compliance_report.py tests/integration/test_htmx_dashboard.py tests/integration/test_transport_tasks.py::test_human_review_release_holds_task_until_admin_approval
.venv/bin/python -m ruff check whilly/operator_views.py whilly/cli/tui.py whilly/compliance/__init__.py tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_compliance_report.py
.venv/bin/python -m ruff format --check whilly/operator_views.py whilly/cli/tui.py whilly/compliance/__init__.py tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_compliance_report.py
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 2: Commit phase**

Run:

```bash
git add docs/superpowers/plans/2026-05-08-human-review-operator-controls.md whilly/operator_views.py whilly/cli/tui.py whilly/api/templates/index.html.j2 whilly/compliance/__init__.py tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_compliance_report.py tests/integration/test_htmx_dashboard.py
git commit -m "feat(operator): add human review controls"
```

Expected: commit created on `main`.

- [ ] **Step 3: Push main**

Run:

```bash
git push
```

Expected: `main` pushed to `origin/main`.
