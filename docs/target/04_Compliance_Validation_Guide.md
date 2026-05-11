# Whilly Compliance Validation Guide

## Purpose

This guide defines how an AI validation agent should evaluate the current Whilly Orchestrator repository.

The agent must not assume a capability is implemented just because a helper module exists. It must verify that the capability is wired into the active runtime path.

## Capability Matrix

| Capability | Required for target | Expected current state | Agent action |
|---|---:|---|---|
| JSON plan import | Yes | Implemented | Validate no regression |
| GitHub issue source | Yes | Implemented | Validate integration boundaries |
| Jira source | Yes | Implemented | Validate integration boundaries |
| Forge/PRD intake | Yes | Implemented with LLM involvement | Validate docs and nondeterminism warning |
| Postgres task state | Yes | Implemented | Validate transactional behavior |
| Dependency/cycle checks | Yes | Implemented | Validate edge cases |
| Decision gates | Yes | Implemented | Validate strict/default behavior |
| Governance risk policy | Yes | Implemented: deterministic governance risk policy covers `migration`, `auth`, `infrastructure`, `dependencies`, `release`, and `external_pr` with inspectable reasons and operator approval boundaries | Validate pure policy evidence and do not claim autonomous production release or default auto-merge |
| Worker claim with SKIP LOCKED | Yes | Implemented | Validate ordering and concurrency safety |
| Prompt injection guard | Yes | Implemented basic guard | Validate coverage and false positives |
| Dangerous command guard | Yes | Implemented basic deny-list | Validate placement before runner |
| Runner abstraction | Yes | Implemented | Validate result contract |
| Completion marker parsing | Yes | Implemented | Validate marker semantics |
| Required verification before DONE | Yes for target | Implemented when required verification commands are configured | Validate marker-only fallback wording |
| Profile-native verification commands | Yes for target | Implemented: profile-native verification commands feed runtime verification | Validate plan metadata and worker runner wiring |
| Project profiles | Yes for target | Implemented | Validate presets and generated plan metadata |
| Configurable pipeline stages | Yes for target | Implemented as audit-event stage lifecycle | Validate runtime event evidence |
| Human review checkpoint model | Yes for target | Implemented dashboard/TUI operator decisions and release-hold enforcement | Validate admin-token handling and audit trail |
| Automatic PR creation after DONE | Optional | Helper exists, not core loop | Do not claim automatic behavior |
| PR review feedback loop | Future | Missing | Document out of scope |
| Bounded CI polling and repair | Yes for target | Implemented: explicit configured CI polling, bounded repair attempts, and `repair.escalated`; No continuous polling, auto-merge, production recovery, or unbounded repair is claimed. | Validate local/remote runtime evidence and retry budgets |
| Multi-repo task execution | Future | Missing | Document out of scope |
| Sandbox/VM isolation | Future/hardening | Partial: prompt, shell, secret, and runner-env guards exist; no full per-task VM/container isolation | Document residual risk |
| Semantic memory | Future target | Semantic memory is explicitly deferred from current scope; deterministic events, task history, PR evidence, and verification logs remain authoritative. | Keep semantic recall out of current-capability claims until runtime wiring exists |
| Git rollback | Future/hardening | Implemented as operator-triggered rollback; no autonomous recovery | Document limitation |
| Observability | Yes | Implemented | Validate events, SSE, metrics |

## Validation Report Format

```markdown
# Whilly Compliance Validation Report

## Summary
- Overall status: PASS / PARTIAL / FAIL
- Target spec version:
- Repository commit:
- Date:

## Capability Matrix
| Capability | Status | Evidence | Gap | Recommended action |
|---|---|---|---|---|

## Critical Findings
1. ...

## Documentation Mismatches
1. ...

## Implementation Gaps
1. ...

## Security and Safety Risks
1. ...

## Recommended Implementation Tasks
1. ...

## Acceptance Criteria for Remediation
- ...
```

## Status Semantics

- **PASS:** Capability exists and is wired into the relevant runtime path.
- **PARTIAL:** Code exists but is not wired into the main path, or behavior is limited.
- **FAIL:** Capability is missing or contradicted by implementation.
- **UNKNOWN:** Agent could not validate due to missing access, missing dependencies or ambiguous code.

## Required Inspection Areas

The agent should inspect at least:

- `README.md`
- `docs/Whilly-v4-Architecture.md`
- `docs/Whilly-Usage.md`
- `docs/CODEX-MISSION.md`
- `whilly/core`
- `whilly/worker`
- `whilly/adapters`
- `whilly/api`
- `whilly/sources`
- `whilly/sinks`
- `whilly/forge`

## Documentation Mismatch Rules

Flag documentation as inaccurate if it claims:

- Whilly is already a fully autonomous developer.
- DONE always means verified code.
- DONE automatically creates PRs without `WHILLY_AUTO_OPEN_PR=1` and an
  explicit configured GitHub PR sink or legacy PR context.
- Whilly supports full multi-repo execution.
- Whilly has full sandbox/VM isolation.
- Whilly has semantic long-term memory.
- Whilly has robust smart rollback.
- Whilly automatically processes PR review feedback and fixes requested changes.
