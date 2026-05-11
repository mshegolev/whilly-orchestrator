---
phase: 11-ci-polling-and-bounded-repair
verified: 2026-05-08T19:18:14Z
status: human_needed
score: "7/7 must-haves verified by automated checks"
re_verification:
  previous_status: human_needed
  previous_score: "7/7 must-haves verified by automated checks"
  gaps_closed:
    - "Remote invalid diagnostic event error detail now mentions ci.* and repair.* alongside the accepted diagnostic prefixes."
  gaps_remaining: []
  regressions: []
human_verification:
  - test: "Live GitHub CI poll smoke"
    expected: "A source=ci verification target for an authenticated GitHub PR records ci.poll.started and ci.poll.result with provider github and an explicit success or non-success conclusion, without shell execution."
    why_human: "Requires real GitHub credentials, network access, and a PR with CI checks; automated tests monkeypatch provider calls."
---

# Phase 11: CI Polling And Bounded Repair Verification Report

**Phase Goal:** Model `execute -> verify/CI -> repair attempt N -> verify/CI -> escalate` as an auditable loop.
**Verified:** 2026-05-08T19:18:14Z
**Status:** human_needed
**Re-verification:** Yes - after cleanup commit `999a128`

## Goal Achievement

Phase 11 still has the required code, wiring, compliance wording, and focused automated tests for the auditable CI and bounded repair loop. Cleanup commit `999a128` resolves the previous non-blocking warning: the invalid diagnostic event error detail now names `ci.*` and `repair.*`, and the integration test file pins those strings.

The only remaining verification item is a live GitHub provider smoke test, because the concrete GitHub API/CLI interaction requires credentials, network access, and an actual PR with checks.

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `source="ci"` verification is dispatched as CI evidence, not shell execution. | VERIFIED | `run_verification_commands()` branches on `spec.source == CI_VERIFICATION_SOURCE` before `_run_one()` in `whilly/pipeline/verification.py:165`; `tests/unit/test_verification_runner.py:265` monkeypatches `asyncio.create_subprocess_shell` to prove CI sources do not reach the shell. |
| 2 | CI polling emits bounded `ci.poll.started` and `ci.poll.result` evidence and treats unavailable, unauthenticated, timed-out, or unknown data as non-success. | VERIFIED | `CIPollResult.succeeded` rejects unavailable/unauthenticated/timed-out results in `whilly/ci/models.py:56`; event builders are in `whilly/ci/events.py:14` and `whilly/ci/events.py:32`; mapping tests are in `tests/unit/test_ci_polling.py:64`, `tests/unit/test_ci_polling.py:86`, and `tests/unit/test_ci_polling.py:115`. |
| 3 | Project config, plan IO, DB/import export, and remote payloads preserve `source` and `repair_max_attempts`; `ci_status` creates concrete `source="ci"` `ci://...` verification commands. | VERIFIED | CI verification config skips shell scanning and requires `ci://` in `whilly/project_config/loader.py:331`; `ci_status` verification commands are generated in `whilly/project_config/plan_builder.py:134`; metadata round trips through `whilly/adapters/filesystem/plan_io.py:380` and `whilly/adapters/transport/schemas.py:230`. |
| 4 | Repair policy is bounded, deterministic, disabled by default, and escalates on disabled or exhausted budgets. | VERIFIED | `RepairBudget(max_attempts=0)` is the default in `whilly/repair/models.py:9`; `decide_repair()` requests the next deterministic attempt or escalates in `whilly/repair/policy.py:35`; escalation and completion builders are in `whilly/repair/events.py:66` and `whilly/repair/events.py:37`. |
| 5 | Repair attempts create new dependency-free tasks and do not retry the failed task via release. | VERIFIED | `build_repair_task()` returns `dependencies=()` in `whilly/repair/tasks.py:37`; local tests assert the repair path does not use release retry in `tests/unit/test_local_worker.py:1003`; remote tests assert `client.release_calls == []` in the repair path in `tests/unit/test_remote_worker.py:821`. |
| 6 | Local runtime records ordered CI evidence, requests or escalates repair, and records terminal repair completion. | VERIFIED | Local worker records CI evidence before verification result events in `whilly/worker/local.py:308`; repair request/escalation wiring is in `whilly/worker/local.py:352`; terminal completion is in `whilly/worker/local.py:413`; transactional repair insertion is in `whilly/adapters/db/repository.py:1872`. |
| 7 | Remote runtime and transport accept CI/repair diagnostics, preserve human-review approval protections, request repair through a dedicated endpoint, and record terminal repair completion. | VERIFIED | Transport allowlist includes `ci.` and `repair.` in `whilly/adapters/transport/server.py:367`; invalid diagnostic error text now includes `ci.*` and `repair.*` in `whilly/adapters/transport/server.py:1808`; tests pin those strings in `tests/integration/test_transport_tasks.py:672`; worker-forged human-review approval remains rejected in `tests/integration/test_transport_tasks.py:768`; repair endpoint inserts via repository in `whilly/adapters/transport/server.py:1857`; remote worker CI/repair wiring is in `whilly/worker/remote.py:383`, `whilly/worker/remote.py:452`, and `whilly/worker/remote.py:533`. |

**Score:** 7/7 automated truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `whilly/ci/models.py` | CI poll spec, result, evidence contracts | VERIFIED | `CIPollSpec`, `CIPollResult`, and `CIPollEvidence` exist with success/blocking semantics. |
| `whilly/ci/events.py` | `ci.poll.started` and `ci.poll.result` event builders | VERIFIED | Event payloads include provider, target, budget, result state, blocking flags, and bounded check detail. |
| `whilly/ci/github.py` | One-shot GitHub-compatible adapter | VERIFIED | Uses `gh pr view` via subprocess exec and classifies auth/unavailable/timeout failures as explicit non-success evidence. Live provider behavior still needs human smoke testing. |
| `whilly/ci/verification.py` and `whilly/pipeline/verification.py` | CI-to-verification mapping and shell-bypass dispatch | VERIFIED | `run_ci_verification()` returns `CIPollEvidence` plus mapped `VerificationCommandResult`; CI source dispatch happens before shell execution. |
| `whilly/repair/models.py`, `whilly/repair/policy.py`, `whilly/repair/events.py`, `whilly/repair/tasks.py` | Bounded repair primitives, audit events, deterministic task builder | VERIFIED | Budget decisions, request/completion/escalation events, deterministic IDs, and empty repair dependencies are implemented and tested. |
| `whilly/project_config/*`, `whilly/adapters/filesystem/plan_io.py`, `whilly/adapters/transport/schemas.py` | Durable CI source and repair budget metadata | VERIFIED | Config validation, plan file IO, and transport payloads preserve `source` and `repair_max_attempts`; `ci_status` produces executable CI verification metadata. |
| `whilly/worker/local.py`, `whilly/adapters/db/repository.py`, `whilly/cli/run.py` | Local runtime CI evidence and bounded repair | VERIFIED | Local CI runner injection, ordered CI evidence, repair insertion, escalation, completion events, and no repair release retry are covered. |
| `whilly/adapters/transport/server.py`, `whilly/adapters/transport/client.py`, `whilly/worker/remote.py`, `whilly/cli/worker.py` | Remote CI/repair transport and runtime | VERIFIED | Diagnostics, repair request endpoint/client, remote CI evidence, repair request/escalation, and completion are implemented. Cleanup commit `999a128` also corrected the invalid diagnostic detail text. |
| `whilly/compliance/__init__.py` | Scoped compliance capability wording | VERIFIED | Capability `Bounded CI polling and repair` reports PASS only with concrete CI/repair local, remote, transport, and test signals. |
| Focused tests | Regression coverage for CI, repair, metadata, local/remote runtime, transport, and compliance | VERIFIED | Focused Phase 11 suite passed locally: `314 passed, 43 skipped`. Docker-backed transport tests were skipped because Docker was not reachable; the cleanup code path and regression assertions were also verified statically. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `whilly/pipeline/verification.py` | `whilly/ci/verification.py` | `spec.source == CI_VERIFICATION_SOURCE` branch | WIRED | CI verification uses `run_ci_verification()` and appends `CIPollEvidence` before returning `VerificationRunOutcome`. |
| `whilly/project_config/plan_builder.py` | `whilly/pipeline/verification.py` | Generated `source="ci"` verification command | WIRED | `ci_status` appends plan-level and task-level CI verification commands with `ci://` targets. |
| `whilly/cli/run.py` | `whilly/ci/github.py` | `GitHubCIPollAdapter()` injection | WIRED | Local runner passes `ci_poll_runner` only when resolved specs include `source="ci"`. |
| `whilly/cli/worker.py` | `whilly/ci/github.py` | `GitHubCIPollAdapter()` injection | WIRED | Remote runner mirrors local CI runner injection. |
| `whilly/worker/local.py` | `whilly/ci/events.py` | `VerificationRunOutcome.ci_polls` | WIRED | Local worker records `ci.poll.started` then `ci.poll.result` before mapped verification events. |
| `whilly/worker/remote.py` | `whilly/ci/events.py` | `VerificationRunOutcome.ci_polls` | WIRED | Remote worker records ordered CI evidence before mapped verification events. |
| `whilly/worker/local.py` | `whilly/repair/tasks.py` and `whilly/adapters/db/repository.py` | `build_repair_task()` then `insert_repair_task()` | WIRED | Local failure path creates one deterministic repair task or escalates, then fails the original task. |
| `whilly/worker/remote.py` | `whilly/adapters/transport/client.py` | `RemoteWorkerClient.request_repair()` | WIRED | Remote failure path requests a new repair task through dedicated transport, not `client.release()`. |
| `whilly/adapters/transport/server.py` | `whilly/adapters/db/repository.py` | `/tasks/{task_id}/repair` endpoint | WIRED | Server validates token/version/dependencies and calls `insert_repair_task()`. |
| `whilly/adapters/transport/server.py` | `tests/integration/test_transport_tasks.py` | Invalid diagnostic rejection detail assertions | WIRED | Error detail includes `verification.*, ci.*, repair.*, and human_review.*`; tests assert `ci.*` and `repair.*` appear in the rejection message. |
| `whilly/compliance/__init__.py` | Local/remote/runtime/tests | File-content capability signals | WIRED | Compliance requires CI primitive, repair primitive, local worker, remote worker, transport, and focused test evidence before PASS. |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| CI-01 | 11-01, 11-03, 11-04, 11-05, 11-06 | CI status polling can be used as a configured verification or sink stage. | SATISFIED, live provider smoke pending | `source="ci"` dispatch bypasses shell, CI evidence is structured, `ci_status` generates `ci://` verification metadata, local/remote workers emit CI evidence, remote diagnostics accept `ci.*`, and compliance reports scoped PASS. |
| CI-02 | 11-02, 11-03, 11-04, 11-05, 11-06 | Repair attempts are bounded, auditable, and stop with escalation when budgets are exhausted. | SATISFIED | Repair policy creates one deterministic repair task while budget remains, emits request/completion/escalation events, local and remote runtimes do not release failed tasks for repair retry, remote diagnostics accept `repair.*`, and tests cover exhausted budgets. |

No orphaned Phase 11 requirements were found in `.planning/REQUIREMENTS.md`; Phase 11 maps only `CI-01` and `CI-02`, and both appear in plan frontmatter.

### Anti-Patterns Found

No blocker stubs, TODO/FIXME placeholders, hidden CI daemon loops, release-based repair retry paths, or diagnostic-message mismatches were found in the Phase 11 code paths.

The previous warning about `whilly/adapters/transport/server.py` invalid diagnostic error text omitting `ci.*` and `repair.*` is closed by cleanup commit `999a128`. Current code includes both families in the detail string, and `tests/integration/test_transport_tasks.py` asserts both substrings.

### Human Verification Required

### 1. Live GitHub CI Poll Smoke

**Test:** Configure a `source="ci"` verification command against an authenticated GitHub PR with CI checks, using a supported target such as `ci://github/{owner}/{repo}#pr-{number}` or `ci://github/{owner}/{repo}/pull/{number}`, and run it through local or remote worker verification.
**Expected:** The task records `ci.poll.started` and `ci.poll.result` with `provider="github"` and the configured `ci://...` target. Success, failure, unavailable, unauthenticated, and timeout outcomes are explicit CI evidence, and the target is not executed through shell.
**Why human:** This requires live GitHub credentials, network access, and an actual PR/check suite. Automated tests monkeypatch `gh` and runtime verification runners.

### Gaps Summary

No code gaps block Phase 11 goal achievement. Automated verification confirms the auditable loop primitives and runtime wiring. Status is `human_needed` only because live external GitHub provider behavior cannot be fully verified with local static checks and monkeypatched tests.

## Verification Commands Run

```bash
.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_repair_loop.py tests/unit/test_project_config.py tests/unit/test_plan_io.py tests/integration/test_plan_io.py tests/unit/test_transport_schemas.py tests/unit/test_remote_client.py tests/unit/test_cli_run.py tests/unit/test_cli_worker.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/integration/test_transport_tasks.py tests/unit/test_configured_sinks.py tests/unit/test_compliance_report.py --maxfail=1
# 314 passed, 43 skipped in 34.90s

make lint
# All checks passed; 447 files already formatted

.venv/bin/lint-imports --config .importlinter
# 2 contracts kept, 0 broken

.venv/bin/python -m whilly compliance report --format markdown --out /private/tmp/phase11-compliance-report.md
rg -n "Bounded CI polling and repair|explicit configured CI polling|bounded repair attempts|repair\\.escalated|No continuous polling" /private/tmp/phase11-compliance-report.md
# Required bounded CI/repair PASS row and negative scope wording found

.venv/bin/python -m pytest -q -rs tests/integration/test_transport_tasks.py::test_record_task_event_accepts_pipeline_verification_and_human_review_events tests/integration/test_transport_tasks.py::test_record_task_event_accepts_ci_and_repair_diagnostics tests/integration/test_transport_tasks.py::test_record_task_event_still_rejects_worker_forged_human_review_approval --maxfail=1
# 3 skipped: Docker daemon not reachable; testcontainers cannot boot Postgres
```

---

_Verified: 2026-05-08T19:18:14Z_
_Verifier: Claude (gsd-verifier)_
