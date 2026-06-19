---
phase: 20
slug: jira-watcher-daemon
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-06-12
---

# Phase 20 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (existing) |
| **Config file** | pyproject.toml / tests/conftest.py |
| **Quick run command** | `.venv/bin/python -m pytest tests/unit/cli -q` |
| **Full suite command** | `.venv/bin/python -m pytest tests/unit -q` |
| **Estimated runtime** | quick ~15s, full ~120s |

---

## Sampling Rate

- **After every task commit:** Run the quick command above
- **After every plan wave:** Run the full unit suite
- **Before `/gsd-verify-work`:** Full unit suite green; live watcher run is manual UAT
- **Max feedback latency:** 150 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01-T1 | 20-01 | 1 | WATCH-01, WATCH-02 | T-20-03, T-20-05 | status file secret-free; atomic write | unit | `.venv/bin/python -m pytest tests/unit/cli/test_jira_watch_loop.py -q` | ⬜ new | ⬜ pending |
| 01-T2 | 20-01 | 1 | WATCH-02 | T-20-02, T-20-03 | PID guard refuses (no kill); audit payload secret-free | unit | `.venv/bin/python -m pytest tests/unit/cli/test_jira_watch_loop.py -q` | ⬜ new | ⬜ pending |
| 02-T1 | 20-02 | 2 | WATCH-03 | T-20-07 | pause → poll-no-dispatch; reason payload secret-free | unit | `.venv/bin/python -m pytest tests/unit/cli/test_jira_watch_loop.py -k pause -q` | ⬜ new | ⬜ pending |
| 02-T2 | 20-02 | 2 | WATCH-03 | T-20-06, T-20-08 | dispatch default-off; gated; no circular import | unit | `.venv/bin/python -m pytest tests/unit/cli/test_jira_watch_loop.py -k "dispatch or readiness or block" -q` | ⬜ new | ⬜ pending |
| 03-T1 | 20-03 | 3 | WATCH-01, WATCH-02, WATCH-03 | T-20-10, T-20-12 | --interactive-config present; dispatch gated through run path | unit | `.venv/bin/python -m pytest tests/unit/test_jira_cli.py -k watch -q` | ⬜ new | ⬜ pending |
| 03-T2 | 20-03 | 3 | WATCH-01, WATCH-02, WATCH-03 | T-20-11 | docs pinned; status output secret-free | unit | `.venv/bin/python -m pytest tests/unit/test_docs_live_smoke.py tests/unit/test_docs_bash_blocks_parse.py -q` | ⬜ new | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers the harness: injectable-collector pattern (test_jira_smoke.py),
injectable stop event (threading.Event), _FakeRepo pattern (test_jira_watch.py), atomic status
write pattern (state_store.py). New test modules are created by the plans:

- [ ] `tests/unit/cli/test_jira_watch_loop.py` — created by Plan 20-01 Task 1 (covers WATCH-01/02 loop)
- [ ] `whilly/cli/jira_watch_loop.py` — created by Plan 20-01 Task 1 (implementation)
- [ ] watch/watch-status CLI tests in `tests/unit/test_jira_cli.py` — Plan 20-03 Task 1
- [ ] docs regression assertions in `tests/unit/test_docs_live_smoke.py` — Plan 20-03 Task 2

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live watcher against real Jira | WATCH-01 | Real credentials + wall-clock intervals | Run `whilly jira watch --issue KEY --interval 30`, observe ≥2 cycles, Ctrl-C, check status file + watch-status |
| Pause honored live | WATCH-03 | Requires touching .whilly_pause during a live run | Pause via TUI/file mid-run, confirm status shows paused and no dispatch |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags in test commands
- [x] Feedback latency < 150s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
