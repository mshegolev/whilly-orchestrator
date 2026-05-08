# Compliance Documentation Mismatch False Positives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the compliance report from flagging explicit non-goal language as positive capability claims.

**Architecture:** Keep compliance reporting deterministic and conservative. Extend the local documentation-claim classifier so it still flags positive claims like "Whilly provides full sandbox isolation", but ignores negative boundary sentences such as "does not claim ... full sandbox or VM isolation". The change is limited to `whilly/compliance/__init__.py` plus focused unit tests.

**Tech Stack:** Python 3.12, pytest, existing `whilly.compliance` deterministic report builder.

---

## File Structure

- `tests/unit/test_compliance_report.py`: regression tests for negative current-boundary wording and positive claim detection.
- `whilly/compliance/__init__.py`: `_contains_positive_claim()` and `_has_negative_boundary()` claim classifier helpers.
- `out/compliance-report.md`: ignored verification artifact generated after the fix.
- `out/compliance-report.json`: ignored verification artifact generated after the fix.

## Task 1: Reproduce README Boundary False Positive

**Files:**
- Modify: `tests/unit/test_compliance_report.py`
- Test: `tests/unit/test_compliance_report.py`

- [ ] **Step 1: Add a regression test for the current README boundary sentence**

Add this test after `test_doc_mismatch_scan_ignores_negative_boundary_claims`:

```python
def test_doc_mismatch_scan_ignores_long_negative_boundary_list(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "README.md").write_text(
        "The core worker loop does **not** claim all of the following as complete product "
        "guarantees: full multi-repo execution, automatic PR review feedback loops, "
        "mandatory CI/lint verification unless verification commands are configured, "
        "full sandbox or VM isolation, semantic long-term memory, reliable git rollback, "
        "or autonomous production release without human review.\n",
        encoding="utf-8",
    )
    for relative in ("Whilly-v4-Architecture.md", "Whilly-Usage.md", "CODEX-MISSION.md"):
        (docs / relative).write_text("Current capability boundaries are documented here.\n", encoding="utf-8")

    report = build_compliance_report(repo_root=repo, doc_root=docs)

    assert not any(item.startswith("README.md:") for item in report.doc_mismatches)
```

- [ ] **Step 2: Run the regression and verify it fails before implementation**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py::test_doc_mismatch_scan_ignores_long_negative_boundary_list
```

Expected before implementation: failure showing `README.md:` still appears in `report.doc_mismatches`.

## Task 2: Preserve Positive Claim Detection

**Files:**
- Modify: `tests/unit/test_compliance_report.py`
- Test: `tests/unit/test_compliance_report.py`

- [ ] **Step 1: Add a positive-claim regression test**

Add this test after the long negative-boundary test:

```python
def test_doc_mismatch_scan_still_flags_positive_claims(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "README.md").write_text(
        "Whilly provides full sandbox or VM isolation and semantic long-term memory.\n",
        encoding="utf-8",
    )
    for relative in ("Whilly-v4-Architecture.md", "Whilly-Usage.md", "CODEX-MISSION.md"):
        (docs / relative).write_text("Current capability boundaries are documented here.\n", encoding="utf-8")

    report = build_compliance_report(repo_root=repo, doc_root=docs)

    assert any("claims full sandbox/VM isolation" in item for item in report.doc_mismatches)
    assert any("claims semantic long-term memory" in item for item in report.doc_mismatches)
```

- [ ] **Step 2: Run the positive test**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py::test_doc_mismatch_scan_still_flags_positive_claims
```

Expected before and after implementation: pass.

## Task 3: Implement Context-Aware Negative Boundary Detection

**Files:**
- Modify: `whilly/compliance/__init__.py`
- Test: `tests/unit/test_compliance_report.py`

- [ ] **Step 1: Replace `_contains_positive_claim()` and `_has_negative_boundary()`**

Use a broader sentence-level prefix so long non-goal lists keep the initial negative marker in scope. Replace the existing helper bodies with:

```python
def _contains_positive_claim(text: str, needle: str) -> bool:
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return False
        sentence_start = max(
            text.rfind(".", 0, index),
            text.rfind(";", 0, index),
            text.rfind("\n", 0, index),
        ) + 1
        sentence_end_candidates = [pos for pos in (text.find(".", index), text.find(";", index)) if pos >= 0]
        sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(text)
        sentence = text[sentence_start:sentence_end].strip()
        prefix = sentence[: max(0, sentence.find(needle))]
        if not _has_negative_boundary(prefix):
            return True
        start = index + len(needle)


def _has_negative_boundary(prefix: str) -> bool:
    normalized = prefix.replace("*", "").replace("_", "").replace("`", "")
    markers = (
        "does not",
        "do not",
        "should not",
        "not ",
        "not yet",
        "no ",
        "cannot",
        "do n't",
        "does n't",
        "не ",
        "нельзя",
    )
    return any(marker in normalized for marker in markers)
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py
```

Expected after implementation: all compliance unit tests pass.

## Task 4: Regenerate Compliance Evidence

**Files:**
- Generated only: `out/compliance-report.md`
- Generated only: `out/compliance-report.json`

- [ ] **Step 1: Regenerate markdown and JSON reports**

Run:

```bash
.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md
.venv/bin/python -m whilly compliance report --format json --out out/compliance-report.json
```

Expected: both commands print `whilly compliance report: wrote ...`.

- [ ] **Step 2: Verify README false positives are gone**

Run:

```bash
rg -n "README.md: claims full sandbox|README.md: claims semantic" out/compliance-report.md
```

Expected: exit code `1` and no output.

- [ ] **Step 3: Verify report still contains real gaps**

Run:

```bash
rg -n "Sandbox/VM isolation|Semantic memory|Git rollback|Human review checkpoint model" out/compliance-report.md
```

Expected: matching rows remain in the capability matrix or critical findings.

## Task 5: Phase Commit And Main Integration

**Files:**
- Modify: `tests/unit/test_compliance_report.py`
- Modify: `whilly/compliance/__init__.py`

- [ ] **Step 1: Run final checks**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py
git diff --check
```

Expected: tests pass and `git diff --check` exits `0`.

- [ ] **Step 2: Commit phase**

Run:

```bash
git add whilly/compliance/__init__.py tests/unit/test_compliance_report.py
git commit -m "fix(compliance): ignore boundary non-goal claims"
```

Expected: commit created on `main`.

- [ ] **Step 3: Merge to main**

If already on `main`, verify merge state instead of creating a no-op merge:

```bash
git status --short --branch
```

Expected: `## main...origin/main` with no unstaged compliance-code changes.
