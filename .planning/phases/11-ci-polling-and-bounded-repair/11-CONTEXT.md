# Phase 11: CI polling and bounded repair - Context

**Gathered:** 2026-05-08
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 11 adds explicit, auditable primitives for CI/status polling and bounded repair loops. The target model is `execute -> verify/CI -> repair attempt N -> verify/CI -> escalate`, with clear retry budgets, stop conditions, and escalation evidence.

This phase should make CI/PR feedback usable as configured verification or sink evidence without creating hidden always-on autonomous repair behavior.
</domain>

<decisions>
## Implementation Decisions

### CI Polling
- CI polling should be an explicit configured stage or verification primitive, not an unconditional background daemon.
- CI polling should return structured evidence: target, provider/status source, conclusion/state, URL/details when available, timeout/attempt counts, and whether the result blocks progression.
- Missing, unavailable, or unauthenticated CI data should be represented explicitly instead of being treated as success.
- GitHub PR feedback polling already exists as a manual one-shot command; reuse its ideas and data where appropriate, but do not convert it into an always-on loop in this phase.

### Bounded Repair
- Repair attempts must have explicit retry budgets and stop conditions.
- A failed verification/CI/PR feedback result may create repair evidence or a repair task only within the configured budget.
- Exhausted repair budgets must emit escalation evidence that operators can inspect.
- Repair loops must remain auditable and deterministic; no silent infinite retry, no hidden auto-merge, and no production release behavior.

### Runtime Integration
- Prefer small dedicated modules such as `whilly/ci/` and `whilly/repair/` over burying policy in `cli/run.py` or worker loops.
- The worker verification path in `whilly/worker/local.py` and the profile verification wiring from Phase 9 are key integration points.
- Configured pipeline stages and sinks from project-config should be able to describe CI or repair behavior without changing existing task/plan import contracts more than necessary.
- Existing manual `whilly pr-feedback poll --plan <id>` behavior should keep working as a one-shot poll command.

### Compliance And Documentation
- Compliance may report CI polling/bounded repair as implemented only when concrete code and tests exist.
- Wording must not claim a continuous autonomous developer loop, automatic production recovery, auto-merge, or unbounded PR review remediation.
- Documentation or compliance updates should state that repair is bounded and escalates when exhausted.

### Claude's Discretion
- Exact provider support is at Claude's discretion. A local/provider-neutral polling contract with GitHub-compatible adapters is acceptable if it satisfies CI-01 and CI-02.
- The implementation may focus on primitives and wiring rather than a complete hosted CI integration, as long as the configured stage can run and the repair budget/escalation semantics are tested.
</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `whilly/pipeline/verification.py` runs configured verification commands and emits `verification.started`, `verification.succeeded`, `verification.failed`, and `verification.warning` events.
- `whilly/worker/local.py` records verification events, fails tasks on required verification failure, and emits pipeline stage failure events.
- `whilly/cli/run.py` and `whilly/cli/worker.py` build verification runners from profile-native and explicit CLI verification commands.
- `whilly/cli/pr_feedback.py` exposes manual one-shot PR feedback polling.
- `whilly/sources/github_pr_feedback.py` polls GitHub PR review/status data and emits review/merge events without running continuously.
- `whilly/pipeline/sinks.py` contains pure policy helpers for configured external PR sink stages and approval guards.
- `whilly/project_config/models.py` has `PipelineStepConfig`, `VerificationCommandConfig`, and `SinkConfig` as likely extension points.
- `whilly/adapters/db/repository.py` has `record_task_event()` and PR event emission surfaces for auditable runtime events.

### Established Patterns
- Keep policy helpers pure where possible, and keep subprocess/network adapters outside `whilly.core`.
- Use structured dataclasses and JSON-ready evidence objects for runtime contracts.
- Emit explicit audit events for guard, verification, pipeline stage, PR feedback, and rollback safety decisions.
- Preserve opt-in behavior for external mutation and repair-like flows.
- Tests should isolate network/CLI providers with fakes and monkeypatching.

### Integration Points
- New CI primitives: likely `whilly/ci/` plus `tests/unit/test_ci_polling.py`.
- New repair policy/primitives: likely `whilly/repair/` plus `tests/unit/test_repair_loop.py`.
- Runtime integration: `whilly/worker/local.py`, `whilly/cli/run.py`, `whilly/cli/worker.py`, and project-config models/plan generation if needed.
- PR feedback reuse: `whilly/cli/pr_feedback.py`, `whilly/sources/github_pr_feedback.py`.
- Compliance evidence: `whilly/compliance/__init__.py`, `tests/unit/test_compliance_report.py`.
</code_context>

<specifics>
## Specific Ideas

- Canonical backlog source: `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`, Task 7.
- Roadmap success criteria:
  1. CI polling can run as a configured verification or sink stage.
  2. Repair attempts have explicit retry budgets and stop conditions.
  3. Escalation events make exhausted repair loops visible to operators.
- Requirements:
  - CI-01: CI status polling can be used as a configured verification or sink stage.
  - CI-02: Repair attempts are bounded, auditable, and stop with escalation when budgets are exhausted.
- Suggested tests from backlog:
  - `tests/unit/test_ci_polling.py`
  - `tests/unit/test_repair_loop.py`
- Existing validation gates should still include `make lint`, import-linter, and focused tests.
</specifics>

<deferred>
## Deferred Ideas

- Governance risk scoring belongs to Phase 12.
- Semantic-memory decisions belong to Phase 12.
- Full automatic PR review feedback repair loop, default auto-merge, and production release remain out of current scope.
- Continuous background polling can be left to operator scheduling or a future service if this phase provides explicit one-shot/configured primitives.
</deferred>
