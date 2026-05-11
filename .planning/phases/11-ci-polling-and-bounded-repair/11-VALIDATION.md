# Phase 11: CI polling and bounded repair - Validation

**Created:** 2026-05-08
**Phase Goal:** Model `execute -> verify/CI -> repair attempt N -> verify/CI -> escalate` as an auditable loop.
**Requirements:** CI-01, CI-02

## Validation Scope

Phase 11 is complete only when CI polling is explicit and auditable, and repair behavior is bounded with visible escalation. The verifier must not accept a hidden background loop, same-task retry, or compliance wording that implies continuous autonomous remediation.

## Requirement Map

| Requirement | Required Evidence | Minimum Automated Coverage |
|-------------|-------------------|----------------------------|
| CI-01: CI status polling can be used as a configured verification or sink stage. | CI polling models produce structured `ci.poll.started` / `ci.poll.result` evidence and map required/optional CI outcomes into `verification.*` results with `source="ci"`. Configured metadata can express CI verification/sink intent. | `tests/unit/test_ci_polling.py`; worker/transport tests proving `ci.*` diagnostic events are accepted; project-config tests for configured CI metadata. |
| CI-02: Repair attempts are bounded, auditable, and stop with escalation when budgets are exhausted. | Repair policy creates at most one next repair task while budget remains, emits repair attempt evidence, and emits `repair.escalated` when exhausted or disabled. Worker failure handling must not release/reclaim the same task as repair. | `tests/unit/test_repair_loop.py`; local/remote worker tests for repair request and escalation; compliance tests for bounded wording. |

## Required Test Files

- `tests/unit/test_ci_polling.py`
- `tests/unit/test_repair_loop.py`
- `tests/unit/test_local_worker.py`
- `tests/unit/test_remote_worker.py`
- `tests/unit/test_remote_client.py` and/or `tests/integration/test_transport_tasks.py`
- `tests/unit/test_configured_sinks.py` or project-config plan-builder tests
- `tests/unit/test_compliance_report.py`

## Verification Commands

Run the smallest new primitive tests after each implementation slice:

```bash
.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_repair_loop.py --maxfail=1
```

Run the phase integration set before verification:

```bash
.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_repair_loop.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_remote_client.py tests/integration/test_transport_tasks.py tests/unit/test_configured_sinks.py tests/unit/test_compliance_report.py --maxfail=1
```

Run the phase gate:

```bash
make lint
.venv/bin/lint-imports --config .importlinter
make test
```

If any full-suite failure is unrelated, capture the exact failing test and keep the focused Phase 11 command green.

## Safety Invariants

- CI polling must be explicit: no new unconditional background poller or daemon.
- A CI poll that is unavailable, unauthenticated, timed out, or missing data must not be treated as success.
- `source="ci"` verification must not be executed through shell command runners.
- Required CI failure blocks normal `DONE`; optional CI failure emits warning evidence without blocking.
- Repair defaults to disabled or zero attempts unless explicitly configured.
- Repair must never call `release_task()` to retry the same failed task.
- Repair task IDs must be deterministic, such as `<orig-task-id>-repair-<N>`.
- Repair tasks must not depend on a failed original task unless that dependency is known terminal-successful.
- Exhausted budgets must emit `repair.escalated` with attempts, max attempts, trigger, and reason.
- Compliance wording must avoid "continuous", "auto-merge", "production recovery", and unbounded/autonomous repair claims.

## Acceptance Checklist

- [ ] `whilly/ci/` exists with typed CI poll specs/results, event builders, and CI-to-verification mapping.
- [ ] `whilly/repair/` exists with typed budgets/triggers/decisions, deterministic task/event builders, and budget exhaustion behavior.
- [ ] Local and remote diagnostic event paths can carry `ci.*` and `repair.*` evidence.
- [ ] Project-config/profile metadata can express CI verification or sink stage intent.
- [ ] Worker verification failure can request bounded repair or escalate without same-task retry.
- [ ] Compliance reports bounded CI/repair primitives only when service, event, worker, and tests exist.
- [ ] `make lint`, import-linter, focused Phase 11 tests, and `make test` pass or have documented unrelated failures.
