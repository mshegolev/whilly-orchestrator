# Phase 12: Governance and semantic-memory decision - Context

**Gathered:** 2026-05-08
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 12 makes governance policy and semantic-memory scope explicit in code and docs. It should close the current roadmap by adding deterministic governance risk classification for high-risk work and either proving semantic memory with evidence-backed runtime behavior or explicitly deferring it from current capability claims.

This phase is not a broad governance platform, not an autonomous release system, and not a semantic-memory implementation unless existing deterministic evidence can support that claim.

</domain>

<decisions>
## Implementation Decisions

### Governance Policy
- Governance risk policy must be deterministic and inspectable, not LLM-scored.
- The minimum risk domains are migrations, authentication/authorization, infrastructure, dependency changes, release actions, and external PR behavior.
- Governance output should identify why a task or plan is high risk and what operator approval or documentation boundary applies.
- Governance must preserve the current control-plane framing: it can recommend or require gates, but it must not claim autonomous production release.

### Semantic Memory
- Semantic memory must remain out of current-capability claims unless it is deterministic, evidence-backed, and wired into task planning or completion.
- If deterministic semantic memory is not implemented in this phase, docs and compliance should explicitly defer it and say deterministic events, task history, PR evidence, and verification logs remain authoritative.
- Semantic recall must never override deterministic audit evidence.
- Future semantic-memory target wording belongs in target/future scope, not implemented capability rows.

### Docs And Compliance Alignment
- `docs/Current-vs-Target.md`, README/docs boundary wording, and compliance report capability rows must describe the same current-vs-target status after Phases 8-11.
- Phase 11 shipped explicit configured CI polling and bounded repair; docs should no longer list those as only future target capabilities.
- Full sandbox or VM isolation, semantic long-term memory, fully autonomous production release, default auto-merge, and continuous PR review repair remain non-goals unless code evidence says otherwise.
- Compliance report tests should guard against positive current-capability claims that contradict docs.

### Claude's Discretion
- Exact module names and risk score shape are at Claude's discretion if they remain pure, typed, and testable.
- Governance can be surfaced through compliance reporting first if that is the smallest coherent runtime/code path.
- Semantic memory may be deferred instead of implemented if implementing it would require a new storage/retrieval subsystem beyond the milestone scope.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Roadmap And Requirements
- `.planning/ROADMAP.md` - Phase 12 goal, success criteria, and single planned plan.
- `.planning/REQUIREMENTS.md` - DOC-04, GOV-01, and GOV-02 acceptance scope.
- `.planning/STATE.md` - Prior phase decisions and current known compliance concerns.

### Current Scope Boundaries
- `docs/Current-vs-Target.md` - Current-vs-target wording that must be synchronized after Phase 12.
- `docs/CODEX-MISSION.md` - v6 hardening boundaries and validation gates.
- `README.md`, `README-RU.md`, `docs/index.md` - User-facing overview docs that must not overclaim current scope.

### Compliance And Tests
- `whilly/compliance/__init__.py` - Current deterministic compliance capability model and doc mismatch rules.
- `tests/unit/test_compliance_report.py` - Existing compliance report tests and overclaim guards.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `whilly/compliance/__init__.py` already builds a deterministic capability matrix with PASS/PARTIAL/FAIL/UNKNOWN statuses, doc mismatch scanning, security risks, implementation tasks, and markdown/JSON rendering.
- `tests/unit/test_compliance_report.py` already validates scoped wording for rollback, bounded CI repair, sandbox risk, semantic-memory mismatch detection, and report CLI output.
- `whilly/project_config/loader.py`, `whilly/pipeline/sinks.py`, and worker paths now expose enough capability signals for compliance to distinguish implemented versus target behavior.

### Established Patterns
- Compliance status is evidence-based by file-content and test-signal checks, not by aspirational docs.
- Negative non-goal wording should not be treated as a positive claim.
- Current docs should say Whilly is a controlled AI engineering control plane, not an autonomous AI developer.

### Integration Points
- Governance policy can live in a small pure module and feed compliance evidence/tests.
- Compliance rows and doc mismatch rules must be updated together with current-vs-target docs.
- Requirement traceability should mark DOC-04, GOV-01, and GOV-02 complete only when docs and tests agree.

</code_context>

<specifics>
## Specific Ideas

- Governance risk categories should include exact labels for migrations, auth, infra, dependencies, release actions, and external PR behavior.
- Semantic-memory deferral wording should be explicit: deterministic event/task/PR/verification evidence remains primary; semantic recall is future target scope.
- `docs/Current-vs-Target.md` should move completed Phase 11 items out of Target/Partial wording where implementation evidence now exists.
- Compliance should stop reporting semantic memory as an ambiguous generic failure if the intended Phase 12 outcome is explicit deferral.

</specifics>

<deferred>
## Deferred Ideas

- A real semantic-memory retrieval subsystem is deferred unless planning finds an existing deterministic event-backed implementation path small enough for this phase.
- Continuous PR review repair loops and autonomous production release remain future capabilities.
- Full per-task VM/container isolation remains future hardening scope.

</deferred>
