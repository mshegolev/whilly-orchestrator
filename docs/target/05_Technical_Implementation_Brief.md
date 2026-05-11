# Technical Implementation Brief

## Goal

Validate the current Whilly Orchestrator repository against the target orchestration criteria and implement the minimum project-aware orchestration layer needed to support configurable project types, verification stages and human review checkpoints.

## Background

Whilly currently provides a strong foundation: task intake, Postgres-backed task queue, deterministic task states, worker claiming, runner abstraction, prompt/shell guards, events, metrics and dashboard/SSE observability.

The current implementation appears to lack first-class project profiles, configurable pipeline stages, mandatory verification before final success, and integrated human-review checkpoint semantics.

PR creation, multi-repo execution, sandboxing, rollback, PR review feedback loops and semantic memory should not be claimed as current core capabilities unless implemented and wired.

## Scope

### In scope

- Repository compliance validation.
- Project profile schema and loader.
- Built-in project profiles.
- CLI profile validation.
- Profile-aware post-agent verification.
- Verification event emission.
- Human review checkpoint event/metadata.
- Documentation updates.
- Backward compatibility with existing behavior.

### Out of scope

- Full multi-repo execution.
- Full sandbox/VM isolation.
- Automatic PR review feedback processing.
- Automatic merge/release.
- Semantic vector memory.
- Smart git rollback.
- Large UI redesign.

## Requirements

### Functional requirements

- Load and validate YAML/JSON project profiles.
- Provide default generic profile.
- Provide built-in profiles for generic, Python backend, GraphQL API, ETL pipeline and documentation.
- Run configured verification commands after successful agent execution.
- Block full success on required verification failure.
- Emit audit events for pipeline/verification/human review stages.
- Generate or support generation of compliance validation report.
- Preserve existing plan import/apply/run flows.

### Non-functional requirements

- Do not weaken existing safety guards.
- Do not break existing task state transitions.
- Keep profile support opt-in or default-compatible.
- Keep implementation modular.
- Avoid hardcoding project-type logic in the worker loop.
- Ensure errors are actionable.

## Architecture Direction

Use a profile-aware orchestration extension around the existing worker/state-machine design.

Do not replace the core state machine. Add:

```text
whilly/
  profiles/
    __init__.py
    models.py
    loader.py
    builtins.py
    validator.py
  pipeline/
    __init__.py
    stages.py
    executor.py
    verification.py
    human_review.py
    sinks.py
  cli/
    profile.py
    validate.py
```

## Proposed Workflow

1. Inspect current repository implementation and produce compliance report.
2. Add profile models and built-in profile templates.
3. Add profile loader and validator.
4. Add CLI command to validate/show profiles.
5. Add verification runner using configured commands.
6. Integrate verification after successful agent execution.
7. Add audit events for verification and human review checkpoints.
8. Add tests for profile loading, validation and verification behavior.
9. Update documentation to match implemented behavior.
10. Re-run tests and produce final compliance report.

## Agent Instructions

1. Start by validating the repository against the capability matrix.
2. Do not assume a capability is implemented just because a helper module exists.
3. Preserve existing behavior unless explicitly required.
4. Prefer minimal, modular additions over rewriting worker architecture.
5. If adding a new task status requires risky migrations, represent review/verification state through events or metadata for MVP.
6. Do not implement auto-merge, auto-release, multi-repo execution, sandboxing, semantic memory or smart rollback unless explicitly requested later.
7. Update documentation to remove or qualify unsupported claims.
8. Add tests before considering implementation complete.

## Acceptance Criteria

- Profile validation works for valid and invalid profiles.
- Generic default profile preserves existing behavior.
- At least one test demonstrates required verification failure blocking full success.
- At least one test demonstrates optional verification failure as warning.
- Human review requirement emits auditable signal.
- Compliance report identifies implemented/partial/missing capabilities.
- Documentation accurately describes current limitations.

## Suggested Implementation Steps

1. Add profile Pydantic models.
2. Add YAML/JSON loader.
3. Add built-in profiles.
4. Add validator using existing safety guard where possible.
5. Add CLI profile commands.
6. Add verification command runner.
7. Add event emission helpers for verification/human review.
8. Wire verification into local worker after successful agent result.
9. Add tests.
10. Update docs.
11. Run full test suite.
12. Produce compliance validation report.

## Testing Strategy

- Unit tests: profile models, loader, validator, verification command policy.
- Integration tests: worker with fake runner and verification success/failure.
- E2E tests: demo plan import/apply/run with default profile.
- Evaluation: compliance report must match known current-state gaps.
- Observability: verify events are emitted and persisted.

## Definition of Done

- Code compiles and tests pass.
- Existing workflows are backward compatible.
- New profile and verification features are documented and tested.
- Safety guards cover configured commands.
- Compliance report can be generated.
- Unsupported capabilities are not claimed as current behavior.
