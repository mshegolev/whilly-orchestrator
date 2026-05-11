---
phase: 13
slug: canonical-ui-parity-contract
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-05-11
---

# Phase 13 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest `9.0.3` local / `>=8.0` floor; pytest-asyncio `1.3.0` local / `>=0.23` floor |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py` |
| **Full suite command** | `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py tests/integration/test_htmx_dashboard.py tests/integration/test_control_state_admin_api.py` |
| **Estimated runtime** | ~8 seconds without Docker startup; Docker-backed integration time depends on local provider |

---

## Sampling Rate

- **After every task commit:** Run `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py`.
- **After every plan wave:** Run `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py tests/integration/test_htmx_dashboard.py tests/integration/test_control_state_admin_api.py`.
- **Before `$gsd-verify-work`:** Run the full focused suite above plus `python3 -m ruff check whilly/operator_views.py whilly/cli/tui.py tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py`.
- **Max feedback latency:** ~30 seconds for unit/static feedback; Docker-backed integration may be skipped with an explicit provider remediation message when Docker is unavailable.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 13-01-01 | 01 | 1 | UI-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py` | yes | pending |
| 13-01-02 | 01 | 1 | UI-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_tui.py` | yes | pending |
| 13-01-03 | 01 | 1 | UI-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_operator_ui_contract.py` | no - Wave 0 creates or folds into `test_operator_views.py` | pending |
| 13-02-01 | 02 | 2 | UI-02 | unit static | `.venv/bin/python -m pytest -q tests/unit/test_wui_contract_static.py` | no - Wave 0 creates | pending |
| 13-02-02 | 02 | 2 | UI-02 | integration/static | `.venv/bin/python -m pytest -q tests/integration/test_htmx_dashboard.py::test_dashboard_mirrors_operator_surfaces_and_hotkeys` | yes | pending |
| 13-02-03 | 02 | 2 | UI-02 | integration | `.venv/bin/python -m pytest -q tests/integration/test_control_state_admin_api.py` | yes | pending |

*Status: pending · green · red · flaky*

---

## Wave 0 Requirements

- [ ] `tests/unit/test_wui_contract_static.py` — static artifact classification and stale-pattern regression tests for UI-02.
- [ ] `tests/unit/test_operator_ui_contract.py` or extended `tests/unit/test_operator_views.py` — contract helper tests for UI-01.
- [ ] `whilly/operator_views.py` — pure contract helpers for surface hotkeys, shared operator actions, WUI selectors/routes, and WUI artifact status.
- [ ] Optional `tests/integration/test_htmx_dashboard.py` update — rendered WUI tabs/actions compared against contract-derived expectations.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| None | UI-01, UI-02 | All Phase 13 behaviors can be verified by unit/static/integration tests. | Not applicable. |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies.
- [x] Sampling continuity: no 3 consecutive tasks without automated verify.
- [x] Wave 0 covers all MISSING references.
- [x] No watch-mode flags.
- [x] Feedback latency < 30s for unit/static loop.
- [x] `nyquist_compliant: true` set in frontmatter.

**Approval:** approved 2026-05-11
