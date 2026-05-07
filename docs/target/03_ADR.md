# Architecture Decision Records — Whilly Orchestrator

## ADR-001 — Whilly as Control Plane, Not Autonomous Developer

### Status

Accepted

### Context

AI agents can generate code, run commands and modify repositories, but their outputs are non-deterministic and may be incorrect. A safe enterprise system needs deterministic control over task state, validation, auditability and human review.

### Decision

Whilly is a control plane for AI-assisted engineering workflows, not a fully autonomous developer.

Whilly owns task intake, validation, queueing, worker claiming, prompt construction, runner invocation, state transitions, audit events, observability, verification and review checkpoints.

AI agents own only the bounded execution attempt.

### Consequences

Positive:

- clearer safety boundary;
- better auditability;
- easier enterprise adoption;
- more honest documentation.

Negative:

- more integration work for verification, PR and review loops.

---

## ADR-002 — Keep Deterministic Core State Machine

### Status

Accepted

### Context

The current system has a clear task lifecycle: PENDING, CLAIMED, IN_PROGRESS, DONE, FAILED, SKIPPED, with release paths back to PENDING.

### Decision

Preserve the deterministic task state machine. Represent project-specific stages as events, execution records or profile-aware hooks.

### Consequences

Positive:

- backward compatibility;
- simpler worker logic;
- easier operational reasoning.

Negative:

- states such as NEEDS_REVIEW or VERIFIED may need events/metadata unless a future migration adds statuses.

---

## ADR-003 — Introduce Declarative Project Profiles

### Status

Proposed

### Context

Different project types need different verification and review workflows. Hardcoding this into the worker loop would create brittle conditionals.

### Decision

Introduce YAML/JSON project profiles with typed Pydantic models.

A profile defines project metadata, project type, workspace, sources, pipeline stages, verification commands, human review requirements, sinks and default runner.

### Consequences

Positive:

- extensible project-specific behavior;
- easier onboarding;
- lower need for code changes per project.

Negative:

- schema and validation complexity;
- misconfigured profiles can block execution.

---

## ADR-004 — Verification as Configured Stage

### Status

Proposed

### Context

A task may currently become DONE when the runner exits with code 0 and prints a completion marker. This does not guarantee verification.

### Decision

Verification is an orchestrator-managed stage configured by project profile. Agent completion and verification success are separate concepts.

For MVP, required verification failure prevents the task from being treated as fully successful in reports and sinks.

### Consequences

Positive:

- more honest success criteria;
- better QA integration;
- domain-specific validation.

Negative:

- requires careful backward compatibility with current DONE semantics.

---

## ADR-005 — Human-in-the-Loop as First-Class Checkpoint

### Status

Proposed

### Context

Human review exists indirectly through handoff files, PR review expectations and external tracker comments.

### Decision

Human checkpoints become first-class pipeline stages configured by project profile.

A checkpoint may require human input before execution, after completion, before PR, or before merge/release.

### Consequences

Positive:

- safer workflows;
- clear dashboard visibility;
- better governance.

Negative:

- requires UI/API support for review queues.

---

## ADR-006 — PR Creation is a Configured Sink

### Status

Accepted

### Context

GitHub PR helper exists, but current worker loop should not be assumed to automatically create PRs after DONE.

### Decision

PR creation is an optional configured sink/stage. Documentation must not imply automatic PR creation unless wired.

---

## ADR-007 — Multi-Repo Execution is Future Scope

### Status

Accepted

### Context

The current system is oriented toward one worker workspace/repository at a time.

### Decision

Current Whilly explicitly supports single-workspace execution. Multi-repo orchestration is future architecture and must not be claimed as implemented.

---

## ADR-008 — Security Hardening is Incremental and Fail-Closed

### Status

Accepted

### Context

Whilly has prompt-injection guards, dangerous command deny-lists, auth, audit events and metrics protections, but not full per-task VM/container sandboxing.

### Decision

Document current safety boundaries honestly and add hardening incrementally. Safety-sensitive checks should fail closed.

Priority hardening areas:

- verification command validation;
- secret redaction;
- branch protection preflight;
- sandbox execution design;
- rollback/restore design.
