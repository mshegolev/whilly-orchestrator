---
phase: 24-integrations-cluster
plan: "03"
subsystem: openspec-integration-specs
tags: [openspec, documentation, gitlab, notifications, mcp, integrations]
requirements: [INT-02, INT-05, INT-06]
dependency_graph:
  requires: []
  provides:
    - "openspec/specs/gitlab-integration/spec.md (INT-02)"
    - "openspec/specs/notifications/spec.md (INT-05)"
    - "openspec/specs/mcp-integration/spec.md (INT-06)"
  affects:
    - "openspec/COVERAGE-MATRIX.md (capability accounting)"
tech_stack:
  added: []
  patterns:
    - "Reverse-spec from real v4 code; observed behavior only"
    - "OpenSpec 1.4.1 normative spec format (SHALL/MUST first body line + #### Scenario WHEN/THEN)"
key_files:
  created:
    - openspec/specs/gitlab-integration/spec.md
    - openspec/specs/notifications/spec.md
    - openspec/specs/mcp-integration/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "gitlab MR sink uses plain `git push --force` (not --force-with-lease) — specced as observed; the worker owns the whilly/<task-id> branch namespace"
  - "notifications scoped to OUTBOUND dispatch only; events/audit layer referenced, not duplicated"
  - "mcp-integration framed as registration/discovery surface — explicitly NOT a mutating external caller"
metrics:
  duration: ~12m
  completed: 2026-06-16
---

# Phase 24 Plan 03: GitLab / Notifications / MCP Integration Specs Summary

Wrote three independent normative OpenSpec capability specs reverse-spec'd from
real v4 code — `gitlab-integration` (INT-02), `notifications` (INT-05), and
`mcp-integration` (INT-06) — each stating auth/config expectations and the
read-only-vs-mutating (or best-effort-dispatch / registration) boundary, all
passing `openspec validate <slug> --strict` with 0 errors and 0 warnings.

## What was built

### Task 1 — gitlab-integration (INT-02), commit c3cbd95
Five requirements from `whilly/cli/gitlab.py` + `whilly/sinks/gitlab_mr.py`:
token resolution (`GITLAB_TOKEN` → `GITLAB_API_TOKEN` →
`WHILLY_GITLAB_API_TOKEN` → `glab config get token -h <host>`) with host derived
from the repo URL; credential redaction across reports/errors/stdout; the
strictly read-only smoke verb (Bearer GET `/api/v4/user` + `/api/v4/projects`,
redacted JSON report, exit 0/1/2, no state change); and `open_mr_for_task` as
the single mutating path (push `--force` then `glab mr create`), treating
up-to-date pushes as `no_diff` and returning structured `failure_mode` results
rather than raising into the loop.

### Task 2 — notifications (INT-05), commit daaa128
Six requirements from the notification adapters, `slack_task_notify.py`,
`api/mailer.py`, `adapters/confluence/publisher.py`, and `notifications.py`:
Slack factory gating (`SLACK_ENABLED` + `SLACK_ACCESS_TOKEN` + `SLACK_CHANNEL`
else `NullNotifier`; adapters perform no env reads); best-effort Slack dispatch
(transport/API errors logged at WARNING, never raised); the `Mailer` SMTP host
gate with `whilly_events.jsonl` event-log fallback that never raises; Confluence
publisher credential requirement + Basic/Bearer auth scheme; local `say` voice
no-op when disabled or the binary is absent; and the cross-cutting rule that no
channel ever blocks or gates orchestration.

### Task 3 — mcp-integration (INT-06), commit 6591e55
Five requirements from `whilly/mcp/registry.py` + `whilly/mcp/profiles.py`: tool
registry registration/lookup with category indexing and a process-global
`get_registry()` singleton; tool JSON round-trip
(name/description/category/parameters/url/provider/api_key_env); profile registry
grouping tool-name references with JSON round-trip and a `get_profile_registry()`
singleton; the credential expectation that each tool names its `api_key_env`
env var (registry holds no secrets); and the registration/discovery boundary
(the registries do not themselves perform mutating external calls).

## Deviations from Plan

None — plan executed exactly as written. The gitlab_mr.py module docstring
describes the push step as `--force-with-lease` in prose, but the actual code
uses plain `git push --force` (with an inline comment explaining why a fresh
worktree lacks the tracking ref `--force-with-lease` requires). The spec
documents the observed `--force` behavior, consistent with the plan's grounding.

## Authentication Gates

None.

## Verification

- `openspec validate gitlab-integration --strict` → is valid (0/0), exit 0
- `openspec validate notifications --strict` → is valid (0/0), exit 0
- `openspec validate mcp-integration --strict` → is valid (0/0), exit 0
- Each spec: `## Purpose` (≥50 chars), `## Requirements` with ≥1 `### Requirement:`,
  every requirement has SHALL/MUST on its first body line and ≥1 `#### Scenario:`
  with WHEN/THEN; no delta headers.

## Known Stubs

None — documentation-only, no code stubs.

## Self-Check: PASSED

- FOUND: openspec/specs/gitlab-integration/spec.md
- FOUND: openspec/specs/notifications/spec.md
- FOUND: openspec/specs/mcp-integration/spec.md
- FOUND commit c3cbd95 (gitlab-integration)
- FOUND commit daaa128 (notifications)
- FOUND commit 6591e55 (mcp-integration)
