---
phase: 19
slug: live-authenticated-smoke
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-12
---

# Phase 19 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (existing) |
| **Config file** | pyproject.toml / tests/conftest.py |
| **Quick run command** | `.venv/bin/python -m pytest tests/unit -q -k "smoke or jira_cli or gitlab"` |
| **Full suite command** | `.venv/bin/python -m pytest tests/unit -q` |
| **Estimated runtime** | quick ~10s, full ~120s |

---

## Sampling Rate

- **After every task commit:** Run the quick command above
- **After every plan wave:** Run the full unit suite
- **Before `/gsd-verify-work`:** Full unit suite green; live authenticated run is manual UAT
- **Max feedback latency:** 150 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| (filled by planner) | | | LIVE-01..03 | redaction | No token/secret in report or stdout | unit (mocked HTTP) | pytest -k smoke | ⬜ new tests | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers the harness (pytest, mock-injection patterns from
tests/unit/test_jira_cli.py `snapshot_collector` lambda). New unit test modules for the smoke
commands are created by the plans themselves.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live Jira smoke against real project | LIVE-01 | Real credentials cannot run in CI | Set JIRA_* env vars, run `whilly jira smoke --issue KEY`, confirm pass + report file |
| Live GitLab smoke against real repo | LIVE-02 | Real credentials cannot run in CI | Set GitLab token env, run `whilly gitlab smoke --repo-url URL`, confirm pass + report file |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 150s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
