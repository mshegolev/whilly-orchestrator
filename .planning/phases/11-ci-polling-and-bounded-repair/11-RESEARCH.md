# Phase 11: CI polling and bounded repair - Research

**Researched:** 2026-05-08
**Domain:** Worker verification, GitHub PR/CI polling, bounded repair policy
**Confidence:** HIGH

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
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

### Deferred Ideas (OUT OF SCOPE)
## Deferred Ideas

- Governance risk scoring belongs to Phase 12.
- Semantic-memory decisions belong to Phase 12.
- Full automatic PR review feedback repair loop, default auto-merge, and production release remain out of current scope.
- Continuous background polling can be left to operator scheduling or a future service if this phase provides explicit one-shot/configured primitives.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CI-01 | CI status polling can be used as a configured verification or sink stage. | Use `source="ci"` verification specs and `whilly/ci/` poll results that emit `ci.poll.*` plus mapped `verification.*` outcomes; keep sink integration explicit and one-shot. |
| CI-02 | Repair attempts are bounded, auditable, and stop with escalation when budgets are exhausted. | Use `whilly/repair/` pure policy and task/event service: at most one next repair task when budget remains, otherwise `repair.escalated`; no same-task automatic retry. |
</phase_requirements>

## Summary

Phase 11 should be implemented as small primitives around the existing worker verification path, not as a new scheduler or daemon. The current worker loop already has the correct insertion point: after an agent reports `<promise>COMPLETE</promise>`, local and remote workers emit `verification.started`, emit each result event, and block `DONE` on required verification failure. CI polling should plug into that path as a configured verification source, with extra `ci.poll.*` evidence events carried through `VerificationRunOutcome` or an adjacent event list.

Bounded repair should be policy-first. The repair module should decide whether a failed verification/CI/PR feedback result creates a new repair task or escalates, based on explicit `max_attempts`. It must not release and reclaim the same failed task, because that is how an infinite retry loop starts. Use generated repair tasks plus events as the audit trail.

**Primary recommendation:** Add `whilly/ci/` and `whilly/repair/` primitives; wire CI as `VerificationCommand.source == "ci"` and repair as a bounded post-failure decision that creates `*-repair-N` tasks or emits `repair.escalated`.

## Standard Stack

### Core
| Component | Version/Source | Purpose | Why Standard |
|-----------|----------------|---------|--------------|
| Python | `>=3.12` from `pyproject.toml` | Runtime and dataclass typing | Existing package baseline; no new runtime dependency needed. |
| `pytest` | installed `9.0.3`; spec `>=8.0` | Unit/integration tests | Existing test runner. |
| `pytest-asyncio` | installed `1.3.0`; spec `>=0.23` | Async worker/poller tests | Existing async test support. |
| Ruff | installed/spec `0.11.5` | Formatting/lint | Existing enforced formatter/linter. |
| import-linter | installed `2.11`; spec `>=2.0` | Import boundary guard | Protects `whilly.core` and remote-worker closure purity. |
| `gh` CLI | Existing local adapter contract | GitHub PR/status polling | `github_pr_feedback.py` already uses `gh pr view` and `gh api`; reuse this shape. |

### Supporting
| Component | Version/Source | Purpose | When to Use |
|-----------|----------------|---------|-------------|
| `asyncpg` | installed `0.31.0`; spec `>=0.29` | DB-backed repair task/event insertion | Only in repository/service paths, never in `whilly.core`. |
| `httpx` | installed `0.28.1`; spec `>=0.27` | Remote worker client | Remote diagnostic event propagation. |
| Pydantic | installed `2.13.3`; spec `>=2.6` | Worker/control-plane schemas | Extend only if plan/verification wire payload shape changes. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `source="ci"` verification primitive | Background poller service | Background service violates phase boundary and creates hidden always-on behavior. |
| Event-backed repair tasks | Retrying the same task row | Same-row retries are simpler but risk unbounded loops and unclear audit history. |
| Existing `gh`-based PR polling shape | Direct GitHub REST/GraphQL client | Direct API may be cleaner later, but adds auth/API surface not needed for CI-01/CI-02. |

**Installation:** No new package installation should be planned for the first cut. Use the existing dev environment:

```bash
pip install -e '.[dev]'
```

**Version verification:** Local installed versions were verified with `.venv/bin/python -c "import importlib.metadata..."` on 2026-05-08.

## Architecture Patterns

### Recommended Project Structure

```text
whilly/
├── ci/
│   ├── __init__.py          # exports event constants, specs, result helpers
│   ├── models.py            # CIPollSpec, CIPollResult, CICheckSummary
│   ├── events.py            # make_ci_poll_started_event/result_event
│   ├── github.py            # GitHub-compatible gh adapter, one-shot only
│   └── verification.py      # convert CI poll specs/results to verification outcomes
├── repair/
│   ├── __init__.py          # exports repair event constants and policy API
│   ├── models.py            # RepairBudget, RepairTrigger, RepairDecision
│   ├── policy.py            # pure budget/attempt decision helpers
│   ├── events.py            # make_repair_attempt_requested/completed/escalated
│   └── tasks.py             # repair task construction/DB protocol adapter
```

### Pattern 1: CI As Explicit Verification Source

**What:** Treat CI polling as a verification source, not a shell command. A plan/profile verification item with `source="ci"` is dispatched to `whilly.ci.verification`; normal commands stay in `run_verification_commands`.

**When to use:** Required CI should block `DONE`; optional CI should emit warning evidence but allow completion, matching existing required/optional verification semantics.

**Contract:**

```python
CI_POLL_STARTED_EVENT = "ci.poll.started"
CI_POLL_RESULT_EVENT = "ci.poll.result"
CI_VERIFICATION_SOURCE = "ci"
```

`ci.poll.started` payload:

```python
{
    "task_id": task_id,
    "plan_id": plan_id,
    "name": spec.name,
    "provider": spec.provider,          # e.g. "github"
    "target": spec.target,              # e.g. "plan_open_prs" or "github:owner/repo#123"
    "required": spec.required,
    "timeout_s": spec.timeout_s,
    "poll_interval_s": spec.poll_interval_s,
    "max_attempts": spec.max_attempts,
}
```

`ci.poll.result` payload:

```python
{
    "task_id": task_id,
    "plan_id": plan_id,
    "name": result.name,
    "provider": result.provider,
    "target": result.target,
    "state": result.state,              # queued|in_progress|completed|unavailable|unknown
    "conclusion": result.conclusion,    # success|failure|cancelled|timed_out|unavailable|unknown
    "succeeded": result.succeeded,
    "required": result.required,
    "blocking": result.required and not result.succeeded,
    "attempts": result.attempts,
    "timed_out": result.timed_out,
    "unavailable": result.unavailable,
    "unauthenticated": result.unauthenticated,
    "duration_s": result.duration_s,
    "details_url": result.details_url,
}
```

`ci.poll.result` detail should carry bounded check data:

```python
{"checks": [{"name": "...", "state": "...", "conclusion": "...", "details_url": "..."}]}
```

### Pattern 2: Bounded Repair Decision Before Final Failure

**What:** On required verification/CI failure, build a `RepairTrigger`, ask pure policy for the next action, then either create one repair task and emit `repair.attempt.requested`, or emit `repair.escalated`.

**When to use:** Only when a configured repair budget exists. Budget default should be `0` unless explicitly configured, so existing verification failure behavior remains unchanged.

**Repair event names and payloads:**

```python
REPAIR_ATTEMPT_REQUESTED_EVENT = "repair.attempt.requested"
REPAIR_ATTEMPT_COMPLETED_EVENT = "repair.attempt.completed"
REPAIR_ESCALATED_EVENT = "repair.escalated"
```

`repair.attempt.requested` payload:

```python
{
    "task_id": orig_task_id,
    "plan_id": plan_id,
    "repair_task_id": repair_task_id,
    "attempt": attempt,
    "max_attempts": max_attempts,
    "trigger_type": "verification" | "ci" | "pr_feedback",
    "trigger_event_type": trigger_event_type,
    "reason": reason,
}
```

`repair.attempt.completed` payload:

```python
{
    "task_id": repair_task_id,
    "plan_id": plan_id,
    "orig_task_id": orig_task_id,
    "attempt": attempt,
    "max_attempts": max_attempts,
    "terminal_status": "DONE" | "FAILED",
}
```

`repair.escalated` payload:

```python
{
    "task_id": orig_task_id,
    "plan_id": plan_id,
    "attempts": attempts,
    "max_attempts": max_attempts,
    "trigger_type": "verification" | "ci" | "pr_feedback",
    "last_failure_event_type": last_failure_event_type,
    "last_repair_task_id": last_repair_task_id,
    "reason": "repair_budget_exhausted" | "repair_disabled" | "repair_not_configured",
}
```

Details may include redacted failed command names, failed check summaries, PR review comments, and URLs. Do not put full raw CI payloads into event payload.

### Pattern 3: Remote Diagnostic Event Allowlist

**What:** Remote workers can only record diagnostic prefixes accepted by `DIAGNOSTIC_EVENT_PREFIXES` in `whilly/adapters/transport/server.py`.

**Required change:** Add `"ci."` and `"repair."` to that tuple, and update the 400 diagnostic message and remote transport tests.

### Anti-Patterns to Avoid

- **Hidden polling loop:** Do not add a background CI daemon or long-running PR-feedback loop in this phase.
- **Same-task auto retry:** Do not release the failed task back to `PENDING` as repair. Create a bounded repair task or escalate.
- **New task states:** Do not add `REPAIRING`, `ESCALATED`, or `BLOCKED` to `TaskStatus` for this phase. Events are the MVP state.
- **CI URI through shell:** A `source="ci"` verification item must never be sent to `asyncio.create_subprocess_shell`.
- **Dependency on failed task:** Repair tasks should not depend on the failed original/previous repair task; failed dependencies are not safe for future scheduler semantics.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Worker retry orchestration | New scheduler loop | Existing local/remote worker verification insertion point | Already handles shutdown, pause, stage events, and terminal transitions. |
| Audit event shape | Ad hoc DB inserts from workers | `PipelineTaskEvent`, `TaskRepository.record_task_event`, `RemoteWorkerClient.record_event` | Keeps local/remote audit paths consistent. |
| GitHub PR data collection | New API client first | Existing `github_pr_feedback.py` `gh pr view`/`gh api` pattern | Existing tests pin argv shape and cursor behavior. |
| Repair cap parsing | Unbounded counters in worker locals | Pure `whilly.repair.policy` plus `*-repair-N` parsing | Testable without Postgres and deterministic across workers. |
| Secret handling | Raw command/check payload persistence | `redact_secrets` and bounded detail fields | Existing verification path redacts command/output and should remain the model. |

**Key insight:** Phase 11 is mostly control-flow and evidence design. The highest-risk bug is not "CI cannot be polled"; it is accidentally creating an autonomous retry loop that operators cannot audit or stop.

## Common Pitfalls

### Pitfall 1: Remote Events Rejected
**What goes wrong:** Local tests pass, but remote workers fail to emit `ci.*` or `repair.*` events with HTTP 400.
**Why it happens:** `DIAGNOSTIC_EVENT_PREFIXES` currently allows only `llm.`, `pipeline.stage.`, `verification.`, and `human_review.`.
**How to avoid:** Add `ci.` and `repair.` prefixes and update server/client tests before wiring remote worker emission.
**Warning signs:** Remote worker logs show `invalid_event_type`; tests only cover local worker.

### Pitfall 2: Missing CI Treated As Green
**What goes wrong:** A missing PR, unauthenticated `gh`, empty status rollup, or timeout becomes success.
**Why it happens:** Boolean `ok` is too coarse for CI evidence.
**How to avoid:** Use explicit `state`, `conclusion`, `unavailable`, `unauthenticated`, `timed_out`, and `blocking` fields.
**Warning signs:** Tests assert only `succeeded is False` and do not inspect unavailable/timeout payload fields.

### Pitfall 3: Infinite Repair Through Release/Reclaim
**What goes wrong:** Required verification fails, worker releases the same task, then claims it again forever.
**Why it happens:** Reuse of shutdown/operator-pause release mechanisms for repair.
**How to avoid:** Never repair by release. On budget remaining, create one new repair task; on exhaustion, emit `repair.escalated`.
**Warning signs:** Repair code calls `release_task()` or increments no explicit attempt count.

### Pitfall 4: Repair Task Cannot Run Under Future Scheduler
**What goes wrong:** A repair task depends on a FAILED task and becomes permanently not-ready when dependency enforcement is applied.
**Why it happens:** PR iteration follow-ups depend on original tasks that already reached `DONE`; verification repair originates from failed tasks.
**How to avoid:** Carry `orig_task_id` in payload/task id, but leave dependencies empty for repair tasks unless the dependency is known `DONE`.
**Warning signs:** `dependencies=[orig_task_id]` in generic repair task builder.

### Pitfall 5: Compliance Overclaims
**What goes wrong:** Compliance/docs say Whilly now automatically repairs PRs continuously.
**Why it happens:** CI polling and bounded repair are easy to phrase as autonomous development.
**How to avoid:** Report only explicit configured one-shot polling and bounded repair/escalation evidence.
**Warning signs:** Wording contains "continuous", "auto-merge", "production recovery", or "unbounded".

## Code Examples

Verified local patterns from the existing codebase:

### CI Result To Verification Result

```python
from whilly.pipeline.verification import (
    VERIFICATION_FAILED_EVENT,
    VERIFICATION_SUCCEEDED_EVENT,
    VERIFICATION_WARNING_EVENT,
    VerificationCommandResult,
)


def ci_result_to_verification_result(result: CIPollResult) -> VerificationCommandResult:
    warning = (not result.required) and (not result.succeeded)
    return VerificationCommandResult(
        name=result.name,
        command=f"ci://{result.provider}/{result.target}",
        required=result.required,
        succeeded=result.succeeded,
        warning=warning,
        event_name=(
            VERIFICATION_SUCCEEDED_EVENT
            if result.succeeded
            else VERIFICATION_WARNING_EVENT
            if warning
            else VERIFICATION_FAILED_EVENT
        ),
        returncode=None,
        stdout="",
        stderr=result.reason,
        duration_s=result.duration_s,
        source="ci",
        timed_out=result.timed_out,
    )
```

### Bounded Repair Policy

```python
def decide_repair(trigger: RepairTrigger, budget: RepairBudget) -> RepairDecision:
    if budget.max_attempts <= 0:
        return RepairDecision(action="escalate", reason="repair_disabled")
    if trigger.current_attempt >= budget.max_attempts:
        return RepairDecision(action="escalate", reason="repair_budget_exhausted")
    return RepairDecision(
        action="request_repair",
        attempt=trigger.current_attempt + 1,
        repair_task_id=f"{trigger.orig_task_id}-repair-{trigger.current_attempt + 1}",
    )
```

### Worker Integration Shape

```python
verification_outcome = await verification_runner(running)
for event in verification_outcome.events:
    await _record_pipeline_event(repo, event)
for verification_result in verification_outcome.results:
    await _record_pipeline_event(repo, make_verification_result_event(running.id, verification_result, plan_id=plan.id))

if verification_outcome.required_failed:
    repair_decision = await repair_handler(running, verification_outcome)
    if repair_decision is not None:
        await _record_pipeline_event(repo, repair_decision.event)
    await repo.fail_task(running.id, running.version, "verification_failed", detail=_verification_failure_detail(...))
```

## State of the Art

| Old/Current Approach | Current Phase Approach | When Changed | Impact |
|----------------------|------------------------|--------------|--------|
| Manual `whilly pr-feedback poll --plan <id>` only | Keep one-shot PR feedback; add one-shot/configured CI evidence | Phase 11 | No hidden daemon. |
| Verification commands are shell commands | Verification sources include command and CI poll sources | Phase 11 | CI can block `DONE` with structured evidence. |
| PR re-iteration cap exists in `whilly.workflow.pr_iterate` | General repair policy uses same bounded pattern without PR-specific assumptions | Phase 11 | CI/verification repair becomes testable and auditable. |
| Compliance reports PR review feedback loop as partial | Compliance can report bounded CI/repair primitives when tests land | Phase 11 | Still must not claim continuous autonomous remediation. |

**Deprecated/outdated:**
- Treating `verification_commands.command` as always shell-executable becomes unsafe once `source="ci"` exists.
- Treating PR feedback polling as a background process remains out of scope.

## Open Questions

1. **Exact project-config surface for CI**
   - What we know: Existing plan metadata already carries `verification_commands` with `name`, `command`, `required`, and `source`.
   - What's unclear: Whether the implementation should add a richer `ci_checks` config now or encode CI target URIs in `command` with `source="ci"`.
   - Recommendation: Use `source="ci"` plus a documented `ci://...` target string for Phase 11; defer richer config until there is a second provider or UI need.

2. **Repair task DB insertion API**
   - What we know: `TaskRepository` lacks a generic insert-task method; PR iteration uses raw `asyncpg.Connection`.
   - What's unclear: Whether to add a generic repository method or keep a repair-specific insertion helper.
   - Recommendation: Add a narrow repository method/protocol for repair task insertion so local worker integration does not reach into `repo._pool`.

3. **PR feedback repair unification**
   - What we know: `whilly.workflow.pr_iterate` already creates bounded PR review follow-ups with `WHILLY_MAX_REVIEW_ITERATIONS`.
   - What's unclear: Whether Phase 11 should migrate that path into generic `whilly.repair`.
   - Recommendation: Do not migrate PR iteration wholesale. Reuse the cap/event ideas and leave PR-specific behavior stable.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest `9.0.3`, pytest-asyncio `1.3.0` |
| Config file | `pyproject.toml` |
| Quick run command | `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_repair_loop.py --maxfail=1` |
| Full suite command | `make test` |
| Lint/import command | `make lint` plus `.venv/bin/lint-imports --config .importlinter` |

### Phase Requirements -> Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| CI-01 | CI poll result produces `ci.poll.started`, `ci.poll.result`, and mapped `verification.*` result with source `ci`. | unit | `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py::test_ci_poll_result_maps_to_required_verification_failure -x` | No - Wave 0 |
| CI-01 | Missing/unavailable/unauthenticated CI data is explicit and blocks only when required. | unit | `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py::test_unavailable_required_ci_is_not_success -x` | No - Wave 0 |
| CI-01 | Remote diagnostic endpoint accepts `ci.*` events. | unit/integration | `.venv/bin/python -m pytest -q tests/unit/test_remote_client.py tests/integration/test_transport_tasks.py --maxfail=1` | Existing files need updates |
| CI-02 | Repair budget creates attempts 1..N and escalates at N+1. | unit | `.venv/bin/python -m pytest -q tests/unit/test_repair_loop.py::test_repair_budget_escalates_when_exhausted -x` | No - Wave 0 |
| CI-02 | Worker verification failure records repair request/escalation without same-task infinite retry. | unit | `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py --maxfail=1` | Existing files need updates |
| CI-02 | Compliance reports bounded repair without continuous/autonomous overclaim. | unit | `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py -x` | Existing file needs updates |

### Sampling Rate

- **Per task commit:** `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_repair_loop.py --maxfail=1`
- **Per wave merge:** `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_repair_loop.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_configured_sinks.py tests/unit/test_compliance_report.py --maxfail=1`
- **Phase gate:** `make lint`, `.venv/bin/lint-imports --config .importlinter`, focused unit suite above, then `make test` when practical.

### Wave 0 Gaps

- [ ] `tests/unit/test_ci_polling.py` - covers CI-01 event/result contracts and CI-to-verification mapping.
- [ ] `tests/unit/test_repair_loop.py` - covers CI-02 budget, task id parsing, request/escalation events.
- [ ] `tests/unit/test_local_worker.py` - add verification failure repair-request and budget-exhausted escalation cases.
- [ ] `tests/unit/test_remote_worker.py` - same repair/CI event path for remote worker.
- [ ] `tests/unit/test_configured_sinks.py` or `tests/unit/test_project_config.py` - confirm configured CI verification/sink metadata generation.
- [ ] `tests/unit/test_compliance_report.py` - update capability evidence without overclaiming.

## Sources

### Primary (HIGH confidence)
- `.planning/phases/11-ci-polling-and-bounded-repair/11-CONTEXT.md` - locked decisions and scope.
- `.planning/REQUIREMENTS.md` - CI-01 and CI-02 requirement text.
- `.planning/ROADMAP.md` - Phase 11 success criteria and dependency on Phase 10.
- `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md` - Task 7 backlog.
- `docs/CODEX-MISSION.md` and `AGENTS.md` - hard boundaries, validation gates, repo conventions.
- `whilly/pipeline/verification.py` - current verification result/events contract.
- `whilly/worker/local.py`, `whilly/worker/remote.py`, `whilly/cli/run.py`, `whilly/cli/worker.py` - local/remote verification wiring.
- `whilly/cli/pr_feedback.py`, `whilly/sources/github_pr_feedback.py` - existing one-shot GitHub PR feedback polling.
- `whilly/pipeline/events.py`, `whilly/pipeline/sinks.py`, `whilly/project_config/models.py`, `whilly/project_config/loader.py`, `whilly/project_config/plan_builder.py` - configured stage/sink and event patterns.
- `whilly/workflow/pr_iterate.py`, `tests/unit/test_pr_iterate_cap.py` - existing bounded PR iteration pattern.
- `whilly/adapters/transport/server.py`, `whilly/adapters/transport/client.py`, `whilly/adapters/transport/schemas.py` - remote event allowlist and diagnostic event transport.
- `whilly/compliance/__init__.py`, `tests/unit/test_compliance_report.py` - compliance report capability wording.

### Secondary (MEDIUM confidence)
- `pyproject.toml`, `.importlinter` - local dependency specs and import constraints.

### Tertiary (LOW confidence)
- None. No network research was used.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Verified from `pyproject.toml` and installed venv metadata.
- Architecture: HIGH - Based on current worker, verification, project-config, transport, and PR feedback code paths.
- Pitfalls: HIGH - Derived from existing tests and explicit code constraints.
- GitHub CI provider specifics: MEDIUM - Existing `gh` PR feedback shape is verified locally; richer GitHub Actions semantics were not researched because Phase 11 can stay provider-neutral.

**Research date:** 2026-05-08
**Valid until:** 2026-06-07 for local architecture; 2026-05-15 for GitHub/`gh` provider behavior if expanded beyond existing local adapter shape.
