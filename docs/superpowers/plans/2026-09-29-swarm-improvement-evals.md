# Evaluated Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Compare proposed improvements against frozen baselines without allowing self-approval.

**Architecture:** Extend existing Whilly product workflow through pure policies and explicit adapter ports. PostgreSQL is canonical; derived search and external observability never authorize work.

**Tech Stack:** Existing Python 3.12 environment, asyncpg/PostgreSQL, Alembic, FastAPI, pytest and existing vanilla JS UI. No new model or vector database required.

**Spec:** ../specs/2026-09-29-swarm-learning-design.md

## Global Constraints

- Initial release is observe-and-propose. No autonomous policy changes, protected configuration edits, merges, deployments or production-data access.
- All model calls share the existing global cap of five. Missing profiles fail closed; no expensive fallback.
- Private product knowledge stays in private configured storage. Public source code contains neutral schemas/examples only.
- Preserve existing uncommitted changes in /opt/develop/whilly-swarm. No mixed commits; task checkpoints are reports/diffs until the owner approves a clean integration boundary.
- Use cheap explicitly selected subagent models only; at most five simultaneous subagents. Re-check available model identifiers at execution time.
- This is increment 5/5; requires acceptance of increment 4 before integration.
- Additive migrations numbered against the current head at execution time; numbers below assume head035 and must be adjusted if another agent added a revision.

## Review Focus

- Access-filtered data must never leak through indexes or diagnostics (L1).
- Crash/retry and timezone boundaries must not duplicate side effects (L2/L4).
- Changed plans or policies must invalidate previously valid authority (L3/L5).
- Malicious sources or DNS changes must not turn research into privileged execution (L4).
- Missing evidence or costs must remain unknown rather than passing evaluation (L1/L5).

## File ownership

Proposed files: whilly/swarm/learning/experiments.py; whilly/adapters/db/learning_experiments.py; whilly/adapters/observability/learning_eval.py; whilly/api/swarm_experiments.py; migrations/040_learning_experiments.py. Shared runtime/API/UI integration belongs to the controller; do not dispatch simultaneous edits to those files. Store adapters implement domain ports, never the reverse. Cross-increment result DTOs are frozen dataclasses with explicit fields named below; use strings only for documented finite states.

### Task 5.1: Experiment manifest and evaluation

**Files:** experiments.py; learning_experiments.py; 040_learning_experiments.py. Test: tests/unit/test_learning_improvement_evals_1.py; real DB cases: tests/integration/test_learning_improvement_evals.py.

**Interfaces:** ExperimentManifest (id, hypothesis, baseline_sha, candidate_sha, dataset_hash, heldout_hash, rubric_version, policy_version, model_versions, acceptance_limits, triz_record). TRIZ record names contradiction, ideal outcome, existing resources, alternatives and falsifying measurement. ExperimentService.compare(manifest: ExperimentManifest, results: list[dict]) -> EvaluationReport. No automatic thresholds; explicit owner-approved acceptance limits are required before evaluation is promotable.

- [x] Write failing tests: test_changed_dataset_invalidates_comparison; test_tuning_heldout_overlap_blocks; test_unknown_cost_not_zero; test_regression_blocks_recommendation. Required assertions: Compare correctness, missed escalations, regressions, repeated failures, latency and total known cost; report per-metric sample sizes and unavailable values. Model judgment alone cannot produce authorization.
- [x] Run the new unit file with `.venv/bin/python -m pytest -q <exact test file above>`; confirm the named missing behavior fails, not imports/dependency setup unrelated to the task.
- [x] Implement only this task's interfaces and minimum supporting DTOs; expose no alternate authorization path.
- [x] Run the same tests to green, then affected existing tests. DB tests use the private check.py wrapper against whilly_swarm_test only, serially; never reset the operational database.
- [x] Fresh reviewer checks spec compliance, failure cases and diff scope. Record changed files, commands/results and blockers; preserve unrelated work without committing it.

### Task 5.2: Optional export, decision and rollout records

**Files:** learning_eval.py; swarm_experiments.py; adapters/transport/server.py; api/static/product-swarm.js; api/templates/product_swarm.html.j2. Test: tests/unit/test_learning_improvement_evals_2.py; real DB cases: tests/integration/test_learning_improvement_evals.py.

**Interfaces:** EvalSink.write(report: EvaluationReport, export_policy: ExportPolicy) -> ExportReceipt; local persistence precedes optional authorized Langfuse export. Export is disabled by default and excludes raw secrets/transcripts. ExperimentService.record_decision(principal: Principal, experiment_id: str, decision: str, reason: str) -> DecisionReceipt records owner acceptance/rejection only; proposed code changes still use existing feature workflow and fresh approval. Rollback references last accepted artifact and requires normal execution authorization.

- [x] Write failing tests: test_export_disabled_no_network; test_agent_cannot_accept_own_change; test_policy_change_requires_owner; test_failed_export_preserves_local_result; test_rollback_does_not_bypass_permissions. Required assertions: UI exposes baseline/candidate versions, evidence, decision and rollback reference. No automatic retraining, prompt installation, merge, deployment or Jev dependency.
- [x] Run the new unit file with `.venv/bin/python -m pytest -q <exact test file above>`; confirm the named missing behavior fails, not imports/dependency setup unrelated to the task.
- [x] Implement only this task's interfaces and minimum supporting DTOs; expose no alternate authorization path.
- [x] Run the same tests to green, then affected existing tests. DB tests use the private check.py wrapper against whilly_swarm_test only, serially; never reset the operational database.
- [x] Fresh reviewer checks spec compliance, failure cases and diff scope. Record changed files, commands/results and blockers; preserve unrelated work without committing it.

## Acceptance and handoff

- [x] Run `.venv/bin/lint-imports --no-cache`, focused Ruff and `git diff --check`.
- [x] Verify named boundary failures with isolated negative/mutation fixtures; never mutate a live repository to test a guard.
- [x] If UI changed, verify authenticated browser flows, CSRF, desktop/mobile, persistence after safe restart and no model calls from view-only actions.
- [x] Update docs/Product-Swarm.md and OpenSpec capability delta with the actual supported increment and explicit limitations.
- [x] Controller self-review maps results to the spec; hand off working code with schedule/export disabled. Report mocked versus live evidence separately.
