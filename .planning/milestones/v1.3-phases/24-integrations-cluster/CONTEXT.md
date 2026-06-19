---
phase: 24-integrations-cluster
type: context
requirements: [INT-01, INT-02, INT-03, INT-04, INT-05, INT-06]
source: orchestrator-authored (autonomous run)
---

# Phase 24 Context — Integrations Cluster

## Goal

Capture the 6 external-integration contracts as **normative, machine-checkable** OpenSpec
specs under `openspec/specs/<slug>/spec.md`, each **reverse-spec'd from the real v4.7.0
code**, stating **auth expectations** and the **read-only vs mutating boundary**, and passing
`openspec validate <slug> --strict`.

## Grounding discipline

READ the modules; spec observed behavior. State wiring/auth truthfully. If a module is
legacy/unwired in the v4 worker-claim run path, say so. The plan-checker and verifier
adversarially check every requirement against source.

## 6 specs to write (one per slug)

| Req | Slug | Reverse-spec from | Altitude note |
|-----|------|-------------------|---------------|
| INT-01 | `jira-integration` | `whilly/jira_board.py`, `whilly/jira_work.py`, `whilly/sources/jira.py`, `whilly/cli/jira.py`, `whilly/cli/jira_tui.py` | Jira read / work-snapshot + auth expectations; read-only vs mutating boundary |
| INT-02 | `gitlab-integration` | `whilly/cli/gitlab.py`, `whilly/sinks/gitlab_mr.py` | GitLab CLI surface + MR sink |
| INT-03 | `github-integration` | **32 modules** — key: `whilly/github_pr.py`, `whilly/github_projects.py`, `whilly/github_converter.py`, `whilly/gh_utils.py`, `whilly/sinks/github_pr.py`, `whilly/sources/github_issues*.py`, `whilly/forge/*`, `whilly/hierarchy/*`, `whilly/workflow/*`, `whilly/ci/github.py` | **Spec at SUBSYSTEM altitude** — capture the guaranteed contracts (PR creation, projects, issue→plan conversion/intake, issue/PR sources, the workflow engine, CI), NOT one requirement per module. Group by sub-surface. |
| INT-04 | `jira-watcher-daemon` | `whilly/jira_watch.py`, `whilly/cli/jira_watch_loop.py` | **Phase 20 shipped behavior** — lifecycle, pause/readiness gates, fail-closed behavior. See `.planning/phases/20-jira-watcher-daemon/` for shipped intent. |
| INT-05 | `notifications` | `whilly/notifications.py`, `whilly/slack_task_notify.py`, `whilly/core/notifications.py`, `whilly/adapters/notifications/*` (factory/null/slack), `whilly/api/mailer.py`, `whilly/adapters/confluence/*` (publisher) | Slack/sink notification dispatch + email + confluence publishing |
| INT-06 | `mcp-integration` | `whilly/mcp/__init__.py`, `whilly/mcp/profiles.py`, `whilly/mcp/registry.py` | MCP server/client integration surface |

(Authoritative module→capability assignments: `openspec/COVERAGE-MATRIX.md`.)

## Boundaries

- State **auth expectations** explicitly per integration (tokens/env vars/credential policy) and the **read-only vs mutating** split (e.g. Jira read/work-snapshot vs ticket writes; GitHub PR creation is mutating).
- `notifications` covers outbound dispatch (Slack, email, confluence) — reference, don't duplicate, the `events`/audit layer.
- `github-integration` is broad; keep requirements at the contract level (what the subsystem guarantees) and let the coverage matrix carry per-module accounting.
- Reference (don't re-spec) earlier capabilities (orchestration-loop, plan-json-contract, result-collection) where integrations feed them.

## Spec format

Mirror `openspec/specs/task-model-fsm/spec.md`; follow `openspec/AUTHORING.md`. `## Purpose`
(≥50 chars) → `## Requirements` with `### Requirement:` blocks (SHALL/MUST body ≤500 chars)
each ≥1 `#### Scenario:` (WHEN/THEN).

## Out of scope

Phases 25–27 capabilities; any `whilly/` Python changes. **Documentation only.**

## Success criteria (ROADMAP)

1. 6 capabilities specced.
2. `jira-watcher-daemon` captures lifecycle, pause/readiness gates, fail-closed behavior.
3. Each integration spec states auth expectations + read-only vs mutating boundary.
4. Each spec ≥1 scenario; all 6 pass `openspec validate --strict`.
5. Covered modules accounted for in the coverage matrix.
