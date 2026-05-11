---
phase: 09
slug: profile-native-verification-wiring
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-05-08
revised: 2026-05-08
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
- **After Plan 09-01:** Run `.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py --maxfail=1`.
- **After Plan 09-02:** Run `.venv/bin/python -m pytest -q tests/integration/test_plan_io.py tests/unit/test_transport_schemas.py tests/unit/test_remote_client.py --maxfail=1`.
- **After Plan 09-03:** Run `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py tests/unit/test_verification_runner.py --maxfail=1`.
- **After Plan 09-04:** Run `.venv/bin/python -m pytest -q tests/unit/test_cli_worker.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_compliance_report.py --maxfail=1`.
- **Before `$gsd-verify-work`:** Run `make lint`, `.venv/bin/lint-imports --config .importlinter`, and `make test` when practical.
- **Max feedback latency:** 180 seconds for focused suites.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 09-01-01 | 01 | 1 | VER-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_project_config.py --maxfail=1` | Extend existing | pending |
| 09-01-02 | 01 | 1 | VER-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_plan_io.py --maxfail=1` | Extend existing | pending |
| 09-02-01 | 02 | 2 | VER-01 | integration | `.venv/bin/python -m pytest -q tests/integration/test_plan_io.py tests/integration/test_alembic_015_plan_verification_commands.py tests/integration/test_alembic_full_chain.py tests/integration/test_alembic_013_work_intents.py --maxfail=1` | Create/extend existing | pending |
| 09-02-02 | 02 | 2 | VER-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_transport_schemas.py tests/unit/test_remote_client.py --maxfail=1` | Extend existing | pending |
| 09-03-01 | 03 | 3 | VER-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_cli_run.py tests/unit/test_verification_runner.py --maxfail=1` | Extend existing | pending |
| 09-04-01 | 04 | 4 | VER-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_cli_worker.py --maxfail=1` | Extend existing | pending |
| 09-04-02 | 04 | 4 | VER-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_compliance_report.py --maxfail=1` | Extend existing | pending |

*Status: pending, green, red, flaky.*

---

## Wave 0 Requirements

- [ ] `whilly/core/models.py` has typed plan-level verification command metadata.
- [ ] `whilly/project_config/plan_builder.py` emits top-level profile verification metadata.
- [ ] `whilly/adapters/filesystem/plan_io.py` preserves top-level `verification_commands`.
- [ ] Plan persistence can store/export profile verification metadata.
- [ ] Transport schema/server/client can expose plan verification metadata without sibling tasks.
- [ ] Local runtime uses source-aware profile-vs-CLI command resolution.
- [ ] Remote worker composition can fetch plan verification metadata without server-only imports.
- [ ] Worker tests cover source-aware profile-vs-CLI failure evidence.
- [ ] Compliance tests cover profile-native evidence separately from explicit CLI verification evidence.

---

## Plan Dependency Map

| Plan | Wave | Depends On | Scope |
|------|------|------------|-------|
| 09-01 | 1 | none | Core model, project-config generation, filesystem plan_io round-trip |
| 09-02 | 2 | 09-01 | DB persistence/import-export and transport server/schema/client metadata exposure |
| 09-03 | 3 | 09-02 | Source-aware verification helper and local CLI/runtime wiring |
| 09-04 | 4 | 09-03 | Remote worker wiring, local/remote failure detail, and compliance evidence |

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

**Approval:** approved 2026-05-08 for Phase 9 planning; revised 2026-05-08 for four-plan scope split.
