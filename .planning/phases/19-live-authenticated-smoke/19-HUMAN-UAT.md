---
status: partial
phase: 19-live-authenticated-smoke
source: [19-VERIFICATION.md]
started: 2026-06-12T12:00:00Z
updated: 2026-06-12T12:00:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. Jira authenticated smoke (LIVE-01)
expected: With real JIRA_* credentials, `whilly jira smoke --issue KEY` exits 0, writes whilly_logs/smoke/jira-smoke-*.json, no tracebacks.
result: [pending]

### 2. GitLab authenticated smoke (LIVE-02)
expected: With real GitLab token, `whilly gitlab smoke --repo-url URL` exits 0, writes report with measured durations.
result: [pending]

### 3. Jira failure hint
expected: Wrong token → exit 1 with actionable hint, no "Traceback" in output.
result: [pending]

### 4. GitLab failure hint + no credential leak
expected: Wrong token → exit 1; token value never appears in report or stdout.
result: [pending]

## Summary

total: 4
passed: 0
issues: 0
pending: 4
skipped: 0
blocked: 0

## Gaps
