---
phase: 18
slug: migration-chain-validation
status: draft
nyquist_compliant: false
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
- **After every plan wave:** Run `pytest tests/integration/test_alembic_full_chain.py -q`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 180 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| (filled by planner) | | | MIG-01, MIG-02 | — | N/A | integration | `pytest tests/integration/test_alembic_full_chain.py -q` | ✅ (stale at 016, extend) | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements — `tests/integration/test_alembic_full_chain.py`,
`tests/conftest.py` (`empty_postgres_dsn`, `DOCKER_REQUIRED`, `_build_alembic_config`) already exist;
this phase extends them.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| CI job runs green on ubuntu-latest | MIG-02 | Requires a pushed pipeline run | Push branch, observe migration-chain CI job result |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 180s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
