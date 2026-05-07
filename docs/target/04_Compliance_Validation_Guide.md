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
| Worker claim with SKIP LOCKED | Yes | Implemented | Validate ordering and concurrency safety |
| Prompt injection guard | Yes | Implemented basic guard | Validate coverage and false positives |
| Dangerous command guard | Yes | Implemented basic deny-list | Validate placement before runner |
| Runner abstraction | Yes | Implemented | Validate result contract |
| Completion marker parsing | Yes | Implemented | Validate marker semantics |
| Required verification before DONE | Yes for target | Missing or partial | Implement or model verification state |
| Project profiles | Yes for target | Missing | Implement MVP |
| Configurable pipeline stages | Yes for target | Missing or partial | Implement MVP hooks |
| Human review checkpoint model | Yes for target | Partial | Integrate into profile/pipeline model |
| Automatic PR creation after DONE | Optional | Helper exists, not core loop | Do not claim automatic behavior |
| PR review feedback loop | Future | Missing | Document out of scope |
| Multi-repo task execution | Future | Missing | Document out of scope |
| Sandbox/VM isolation | Future/hardening | Missing or partial | Document risk |
| Semantic memory | Future | Missing | Document out of scope |
| Git rollback | Future/hardening | Partial | Document limitation |
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
