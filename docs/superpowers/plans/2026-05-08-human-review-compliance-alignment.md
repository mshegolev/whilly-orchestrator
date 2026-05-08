# Human Review Compliance Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the compliance report describe the current human-review implementation accurately without overstating it as complete.

**Architecture:** Keep the capability status `PARTIAL` because dashboard/TUI operator controls are still incomplete. Update deterministic evidence and gap text so the report recognizes the existing admin human-review decision API and release-hold enforcement, while still pointing to the remaining operator-control gap.

**Tech Stack:** Python 3.12, pytest, existing `whilly.compliance` deterministic report builder.

---

## File Structure

- `tests/unit/test_compliance_report.py`: regression test for the human-review capability finding.
- `whilly/compliance/__init__.py`: human-review compliance evidence and gap text.
- `out/compliance-report.md`: ignored verification artifact generated after the fix.
- `out/compliance-report.json`: ignored verification artifact generated after the fix.

## Task 1: Lock Current Human-Review Compliance Wording

**Files:**
- Modify: `tests/unit/test_compliance_report.py`
- Test: `tests/unit/test_compliance_report.py`

- [ ] **Step 1: Add a regression test for current implemented controls**

Add this test after `test_report_model_classifies_capabilities_and_partial_helper_evidence`:

```python
def test_human_review_compliance_reports_admin_controls_and_remaining_ui_gap() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    finding = report.capability("Human review checkpoint model")

    assert finding.status is CapabilityStatus.PARTIAL
    assert "admin human-review decision endpoint" in finding.evidence.lower()
    assert "release-hold enforcement" in finding.evidence.lower()
    assert "dashboard/tui operator controls" in finding.gap.lower()
    assert "approval capture/enforcement is not yet" not in finding.gap.lower()
```

- [ ] **Step 2: Run the regression and verify it fails before implementation**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py::test_human_review_compliance_reports_admin_controls_and_remaining_ui_gap
```

Expected before implementation: failure showing the current evidence or gap does not mention admin controls and release-hold enforcement.

## Task 2: Update Human-Review Evidence And Gap

**Files:**
- Modify: `whilly/compliance/__init__.py`
- Test: `tests/unit/test_compliance_report.py`

- [ ] **Step 1: Update `_human_review_evidence()`**

Replace the existing helper body with logic that distinguishes the current implementation level:

```python
def _human_review_evidence(files: _RepoFiles) -> str:
    if not files.exists("whilly/pipeline/human_review.py"):
        return "HumanLoopConfig and PipelineStepConfig.human_gate model review requirements."
    if files.contains("whilly/adapters/transport/server.py", "/api/v1/tasks/{task_id}/human-review") and files.contains(
        "tests/integration/test_transport_tasks.py", "test_human_review_release_holds_task_until_admin_approval"
    ):
        return (
            "HumanLoopConfig and PipelineStepConfig.human_gate model review requirements; "
            "whilly/pipeline/human_review.py defines checkpoint events; "
            "the admin human-review decision endpoint records approval/rejection events; "
            "integration tests cover release-hold enforcement until admin approval."
        )
    return (
        "HumanLoopConfig and PipelineStepConfig.human_gate model review requirements; "
        "whilly/pipeline/human_review.py defines checkpoint events and workers emit human_review.required."
    )
```

- [ ] **Step 2: Update `_human_review_gap()`**

Replace the existing helper body with:

```python
def _human_review_gap(files: _RepoFiles) -> str:
    if files.exists("whilly/pipeline/human_review.py") and files.contains(
        "whilly/adapters/transport/server.py", "/api/v1/tasks/{task_id}/human-review"
    ):
        return (
            "Admin approval capture and release-hold enforcement exist; remaining gap is first-class "
            "dashboard/TUI operator controls for approve, reject, and request-changes decisions."
        )
    if files.exists("whilly/pipeline/human_review.py"):
        return "Checkpoint events exist, but approval capture and release enforcement are still incomplete."
    return "Review gates are represented in generated tasks, not enforced as a separate runtime approval state."
```

- [ ] **Step 3: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py
```

Expected after implementation: all compliance unit tests pass.

## Task 3: Regenerate And Inspect Compliance Report

**Files:**
- Generated only: `out/compliance-report.md`
- Generated only: `out/compliance-report.json`

- [ ] **Step 1: Regenerate reports**

Run:

```bash
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
.venv/bin/python -m whilly compliance report --format json --out out/compliance-report.json
```

Expected: both commands print `whilly compliance report: wrote ...`.

- [ ] **Step 2: Verify human-review report wording**

Run:

```bash
rg -n "admin human-review decision endpoint|release-hold enforcement|dashboard/TUI operator controls|approval capture/enforcement is not yet" out/compliance-report.md
```

Expected: first three phrases appear and the stale `approval capture/enforcement is not yet` phrase does not appear.

## Task 4: Phase Commit And Main Integration

**Files:**
- Modify: `tests/unit/test_compliance_report.py`
- Modify: `whilly/compliance/__init__.py`
- Create: `docs/superpowers/plans/2026-05-08-human-review-compliance-alignment.md`

- [ ] **Step 1: Run final checks**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py tests/integration/test_transport_tasks.py::test_human_review_release_holds_task_until_admin_approval tests/integration/test_htmx_dashboard.py::test_dashboard_renders_compliance_gaps_and_events
.venv/bin/python -m ruff check whilly/compliance/__init__.py tests/unit/test_compliance_report.py
.venv/bin/python -m ruff format --check whilly/compliance/__init__.py tests/unit/test_compliance_report.py
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 2: Commit phase**

Run:

```bash
git add docs/superpowers/plans/2026-05-08-human-review-compliance-alignment.md whilly/compliance/__init__.py tests/unit/test_compliance_report.py
git commit -m "fix(compliance): align human review evidence"
```

Expected: commit created on `main`.

- [ ] **Step 3: Push main**

Run:

```bash
git push
```

Expected: `main` pushed to `origin/main`.
