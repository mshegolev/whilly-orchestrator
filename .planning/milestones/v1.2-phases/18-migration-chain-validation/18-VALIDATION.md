---
phase: 18
slug: migration-chain-validation
status: planned
nyquist_compliant: true
wave_0_complete: false
created: 2026-06-11
---

# Phase 18 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (existing) |
| **Config file** | pyproject.toml / tests/conftest.py |
| **Quick run command** | `pytest tests/unit -q` |
| **Full suite command** | `pytest tests/integration/test_alembic_full_chain.py -q` (Docker required) |
| **Estimated runtime** | ~120 seconds (full chain in Docker) |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/unit -q`
- **After every plan wave:** Run `make migrate-chain` (full chain in Docker)
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 180 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 18-01-T1 | 01 | 1 | MIG-01 | — | N/A | static+integration | `pytest tests/integration/test_alembic_full_chain.py -q` | ✅ (extend to 028) | ⬜ pending |
| 18-01-T2 | 01 | 1 | MIG-01 | — | N/A | integration | `pytest tests/integration/test_alembic_full_chain.py::test_full_chain_upgrade_then_full_downgrade -q` | ✅ (extend) | ⬜ pending |
| 18-01-T3 | 01 | 1 | MIG-01 | T-18-01,T-18-02 | No DSN/secret in evidence or stdout | integration | `pytest tests/integration/test_alembic_full_chain.py::test_full_chain_then_re_upgrade_idempotent -q` | ✅ (extend) | ⬜ pending |
| 18-02-T1 | 02 | 2 | MIG-02 | — | N/A | smoke | `make migrate-chain` | ⬜ (Makefile target — Wave 0 gap) | ⬜ pending |
| 18-02-T2 | 02 | 2 | MIG-02 | T-18-04,T-18-05,T-18-06,T-18-SC | No secrets in CI logs/artifact | CI | `migration-chain` job in ci.yml | ⬜ (CI job — Wave 0 gap) | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers MIG-01 test execution — `tests/integration/test_alembic_full_chain.py`,
`tests/conftest.py` (`empty_postgres_dsn`, `DOCKER_REQUIRED`, `_build_alembic_config`) already exist;
Plan 01 extends them. MIG-02 entry points (`make migrate-chain`, `migration-chain` CI job) are new and
created by Plan 02 before they can be relied on.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| CI job runs green on ubuntu-latest | MIG-02 | Requires a pushed pipeline run | Push branch, observe migration-chain CI job result + evidence artifact |
| Full chain runs green in Docker locally | MIG-01 | Requires a running Docker daemon (skips otherwise) | Start Docker, run `make migrate-chain`, confirm green + migration-chain-evidence.json written |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 180s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** planned 2026-06-11
