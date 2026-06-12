---
phase: 20
slug: jira-watcher-daemon
status: draft
nyquist_compliant: false
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
| (filled by planner) | | | WATCH-01..03 | no secrets in status/log/events | deterministic loop tests via injected collector + stop event | unit | `.venv/bin/python -m pytest tests/unit/cli -q` | ⬜ new tests | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers the harness: injectable-collector pattern (test_jira_smoke.py),
injectable stop event (threading.Event), _FakeRepo pattern (test_jira_watch.py), atomic status
write pattern (state_store.py). New test modules are created by the plans.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live watcher against real Jira | WATCH-01 | Real credentials + wall-clock intervals | Run `whilly jira watch --issue KEY --interval 30`, observe ≥2 cycles, Ctrl-C, check status file + watch-status |
| Pause honored live | WATCH-03 | Requires touching .whilly_pause during a live run | Pause via TUI/file mid-run, confirm status shows paused and no dispatch |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags in test commands
- [ ] Feedback latency < 150s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
