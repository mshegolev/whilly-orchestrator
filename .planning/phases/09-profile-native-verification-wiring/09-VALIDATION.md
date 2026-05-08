---
phase: 09
slug: profile-native-verification-wiring
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-05-08
---

# Phase 09 - Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8+, pytest-asyncio >=0.23 |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`, testpaths=`tests`, asyncio_mode=`auto`) |
| **Quick run command** | `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py tests/unit/test_cli_run.py tests/unit/test_verification_runner.py --maxfail=1` |
| **Full suite command** | `make test` |
| **Architecture guard** | `.venv/bin/lint-imports --config .importlinter` |
| **Estimated runtime** | ~30-180 seconds for focused suites; full suite varies with Docker availability |

---

## Sampling Rate

- **After every task commit:** Run the focused pytest file for the touched subsystem.
- **After every plan wave:** Run `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py tests/unit/test_cli_run.py tests/unit/test_verification_runner.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py --maxfail=1`.
- **Before `$gsd-verify-work`:** Run `make lint`, `.venv/bin/lint-imports --config .importlinter`, and `make test` when practical.
- **Max feedback latency:** 180 seconds for focused suites.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 09-01-01 | 01 | 1 | VER-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py --maxfail=1` | Extend existing | pending |
| 09-01-02 | 01 | 1 | VER-01 | integration/unit | `.venv/bin/python -m pytest -q tests/integration/test_plan_io.py tests/unit/test_transport_schemas.py --maxfail=1` | Extend existing | pending |
| 09-01-03 | 01 | 1 | VER-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py tests/unit/test_worker_cli.py tests/unit/test_verification_runner.py --maxfail=1` | Extend existing | pending |
| 09-01-04 | 01 | 1 | VER-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_compliance_report.py --maxfail=1` | Extend existing | pending |

*Status: pending, green, red, flaky.*

---

## Wave 0 Requirements

- [ ] `whilly/core/models.py` has typed plan-level verification command metadata.
- [ ] `whilly/adapters/filesystem/plan_io.py` preserves top-level `verification_commands`.
- [ ] Plan persistence can store/export profile verification metadata.
- [ ] Remote worker composition can fetch plan verification metadata without server-only imports.
- [ ] Worker tests cover source-aware profile-vs-CLI verification evidence.

---

## Manual-Only Verifications

All Phase 9 behavior should have automated verification. No visual, real-time, or external-service manual testing is required.

---

## Validation Sign-Off

- [x] All tasks have automated verify commands or Wave 0 dependencies.
- [x] Sampling continuity: no 3 consecutive tasks without automated verify.
- [x] Wave 0 covers all missing references.
- [x] No watch-mode flags.
- [x] Feedback latency target under 180 seconds for focused suites.
- [x] `nyquist_compliant: true` set in frontmatter.

**Approval:** approved 2026-05-08 for Phase 9 planning.
