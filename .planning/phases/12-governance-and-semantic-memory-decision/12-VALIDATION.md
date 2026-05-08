---
phase: 12
slug: governance-and-semantic-memory-decision
status: draft
nyquist_compliant: true
wave_0_complete: true
created: 2026-05-08
---

# Phase 12 — Validation Strategy

> Per-phase validation contract for governance policy, semantic-memory scope, and docs/compliance alignment.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest |
| **Config file** | `pytest.ini`, `pyproject.toml` |
| **Quick run command** | `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py tests/unit/test_compliance_report.py --maxfail=1` |
| **Full suite command** | `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py tests/unit/test_compliance_report.py --maxfail=1 && make lint && .venv/bin/lint-imports --config .importlinter` |
| **Estimated runtime** | ~25 seconds for focused tests, longer for lint/import-linter |

---

## Sampling Rate

- **After every task commit:** Run the smallest touched test file.
- **After every plan wave:** Run `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py tests/unit/test_compliance_report.py --maxfail=1`
- **Before `$gsd-verify-work`:** Focused suite, `make lint`, and import-linter must be green.
- **Max feedback latency:** 60 seconds for focused tests.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 12-01-01 | 01 | 1 | GOV-01 | unit | `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py --maxfail=1` | W0 | pending |
| 12-01-02 | 01 | 1 | GOV-02 | unit | `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` | W0 | pending |
| 12-01-03 | 01 | 1 | DOC-04 | unit/docs | `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` | W0 | pending |

*Status: pending until execution writes the tests and code.*

---

## Wave 0 Requirements

Existing infrastructure covers the phase:

- `tests/unit/test_compliance_report.py` already validates compliance report output and doc mismatch rules.
- `tests/unit/core/` already exists for pure core-policy tests.
- `make lint` and `.venv/bin/lint-imports --config .importlinter` cover formatting and core import purity.

---

## Manual-Only Verifications

All Phase 12 behaviors have automated verification. Manual review is optional for wording preference only.

---

## Required Automated Coverage

- Governance policy test must cover all required categories: `migration`, `auth`, `infrastructure`, `dependencies`, `release`, and `external_pr`.
- Governance policy test must prove scoring is deterministic and returns stable reasons/approval boundaries.
- Compliance report test must prove governance policy is reported from concrete code/test evidence.
- Compliance report test must prove semantic memory is explicitly deferred unless deterministic runtime evidence exists.
- Docs/compliance test must prove current-vs-target wording is synchronized and does not claim semantic long-term memory as implemented.
- Compliance markdown/JSON command must still run.

---

## Validation Sign-Off

- [x] All tasks have planned automated verification.
- [x] Sampling continuity: no 3 consecutive tasks without automated verify.
- [x] Wave 0 covers all missing references.
- [x] No watch-mode flags.
- [x] Feedback latency target under 60 seconds for focused tests.
- [x] `nyquist_compliant: true` set in frontmatter.

**Approval:** approved 2026-05-08
