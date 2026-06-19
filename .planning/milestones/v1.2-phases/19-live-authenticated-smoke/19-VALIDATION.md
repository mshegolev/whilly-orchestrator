---
phase: 19
slug: live-authenticated-smoke
status: planned
nyquist_compliant: true
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
| **Quick run command** | `.venv/bin/python -m pytest tests/unit/cli -q` |
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
| 01-T1 | 19-01 | 1 | LIVE-03 | T-19-01 | Report redaction strips userinfo; no token/DSN written | import + unit | `.venv/bin/python -m pytest tests/unit/cli/test_smoke.py -q` | ⬜ new | ⬜ pending |
| 01-T2 | 19-01 | 1 | LIVE-03 | T-19-01 | No secret leak in serialized report | unit | `.venv/bin/python -m pytest tests/unit/cli/test_smoke.py -q` | ⬜ new | ⬜ pending |
| 02-T1 | 19-02 | 2 | LIVE-01, LIVE-03 | T-19-03/04/05 | Config gate before HTTP; key validated; redacted report; no traceback | cli help + unit | `.venv/bin/python -m whilly jira smoke --help` | ⬜ new | ⬜ pending |
| 02-T2 | 19-02 | 2 | LIVE-01, LIVE-03 | T-19-04/05 | Pass/fail/missing-config/classify-readonly/no-secrets | unit (mocked) | `.venv/bin/python -m pytest tests/unit/cli/test_jira_smoke.py -q` | ⬜ new | ⬜ pending |
| 03-T1 | 19-03 | 2 | LIVE-02, LIVE-03 | T-19-06/08/09 | Injectable read-only GitLab ping; path normalized; redacted report | import + unit | `.venv/bin/python -m pytest tests/unit/cli/test_gitlab_smoke.py -q` | ⬜ new | ⬜ pending |
| 03-T2 | 19-03 | 2 | LIVE-02 | — | gitlab registered + in --help | cli help | `.venv/bin/python -m whilly gitlab smoke --help` | ⬜ new | ⬜ pending |
| 03-T3 | 19-03 | 2 | LIVE-02, LIVE-03 | T-19-07/08/09 | auth/missing-config/repo-hint/no-secrets/token-precedence | unit (mocked) | `.venv/bin/python -m pytest tests/unit/cli/test_gitlab_smoke.py -q` | ⬜ new | ⬜ pending |
| 04-T1 | 19-04 | 3 | LIVE-01, LIVE-02, LIVE-03 | T-19-10 | Documented setup; no secrets in examples; docs regression green | unit | `.venv/bin/python -m pytest tests/unit/test_ui_parity_docs.py -q` | ✅ existing | ⬜ pending |
| 04-T2 | 19-04 | 3 | LIVE-01, LIVE-02, LIVE-03 | T-19-10 | Live smoke section pinned | unit | `.venv/bin/python -m pytest tests/unit/test_docs_live_smoke.py -q` | ⬜ new | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers the harness (pytest, mock-injection patterns from
tests/unit/test_jira_cli.py `snapshot_collector` lambda). New package + test
modules are created by the plans:

- [ ] `tests/unit/cli/__init__.py` — package marker (Plan 19-01 Task 2)
- [ ] `tests/unit/cli/test_smoke.py` — shared helper tests (Plan 19-01 Task 2)
- [ ] `tests/unit/cli/test_jira_smoke.py` — jira smoke tests (Plan 19-02 Task 2)
- [ ] `tests/unit/cli/test_gitlab_smoke.py` — gitlab smoke tests (Plan 19-03 Task 3)
- [ ] `tests/unit/test_docs_live_smoke.py` — docs regression (Plan 19-04 Task 2)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live Jira smoke against real project | LIVE-01 | Real credentials cannot run in CI | Set JIRA_* env vars, run `whilly jira smoke --issue KEY`, confirm pass + report file |
| Live GitLab smoke against real repo | LIVE-02 | Real credentials cannot run in CI | Set GITLAB_URL/GITLAB_TOKEN, run `whilly gitlab smoke --repo-url URL`, confirm pass + report file |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 150s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** planned 2026-06-12
