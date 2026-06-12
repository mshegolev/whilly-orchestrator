---
status: complete
phase: 19-live-authenticated-smoke
source: [19-VERIFICATION.md]
started: 2026-06-12T12:00:00Z
updated: 2026-06-12T05:15:00Z
---

## Current Test

All 4 tests executed live on 2026-06-12 (operator machine, real credentials).

## Tests

### 1. Jira authenticated smoke (LIVE-01)
expected: With real JIRA_* credentials, `whilly jira smoke --issue KEY` exits 0, writes whilly_logs/smoke/jira-smoke-*.json, no tracebacks.
result: passed — `whilly jira smoke --issue EORD-9855` vs jira.mts.ru (Server/DC 9.12): 6/6 checks, exit 0, report whilly_logs/smoke/jira-smoke-2026-06-12T05:09:27Z.json. Required JIRA_AUTH_SCHEME=bearer + JIRA_API_VERSION=2 (documented in Whilly-Usage.md).

### 2. GitLab authenticated smoke (LIVE-02)
expected: With real GitLab token, `whilly gitlab smoke --repo-url URL` exits 0, writes report with measured durations.
result: passed — `whilly gitlab smoke --repo-url https://gitlab.services.mts.ru/aiqa/aiqa-core`: 3/3 checks, exit 0, measured durations (auth 1.098s, project_access 1.027s), report persisted.

### 3. Jira failure hint
expected: Wrong token → exit 1 with actionable hint, no "Traceback" in output.
result: passed — stale Jira token run: exit 1, per-check actionable hints, report still written, no Traceback in output.

### 4. GitLab failure hint + no credential leak
expected: Wrong token → exit 1; token value never appears in report or stdout.
result: passed — expired GitLab token run: exit 1, hints reference GITLAB_TOKEN by name only; grep confirmed token value absent from report JSON.

## Summary

total: 4
passed: 4
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps
