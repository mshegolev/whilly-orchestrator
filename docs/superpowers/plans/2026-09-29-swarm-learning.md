# Swarm Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Deliver current shared knowledge, inter-agent collaboration and evidence-based improvement without expanding autonomous authority.

**Architecture:** Five independently accepted increments extend existing product sessions, mailbox, approval and admission mechanisms. Pure policies depend on ports; storage, research and evaluation are adapters.

**Tech Stack:** Existing Whilly Python/PostgreSQL/FastAPI/vanilla JS stack.

**Spec:** ../specs/2026-09-29-swarm-learning-design.md (approved).

## Global Constraints

- Observe-and-propose first; schedules and exports disabled until explicitly configured and authorized.
- Up to five cheap subagents; strong paid runtime calls require configured profiles and budget.
- No mixed commits, production writes, merge/deploy, or replacement of existing approval checks.
- Preserve dirty worktree. Re-read shared board, repository instructions and current migration head before implementation.

## Review Focus

Access leakage, duplicate side effects, stale authority, untrusted research content,
and missing evaluation evidence are mapped to negative tests in the increment plans.

## Ordered increments

- [x] [L1: Shared memory](2026-09-29-swarm-memory.md) — knowledge revisions, freshness, permissions, context packages, redaction.
- [x] [L2: Durable messages](2026-09-29-swarm-messages.md) — delivery receipts, retries, expiry, authenticated sender; live inspector verified, sending intentionally requires explicit policy.
- [x] [L3: Cross-project proposals](2026-09-29-swarm-proposals.md) — safe-mode deduplication and approval-bound admission; contract changes remain blocked without host proof.
- [ ] [L4: Daily research](2026-09-29-swarm-daily-research.md) — disabled scheduler, restricted fetch, retrospective, reports.
- [ ] [L5: Improvement evaluation](2026-09-29-swarm-improvement-evals.md) — frozen comparisons, TRIZ records, owner decisions, optional Langfuse export.

## Execution coordination

Controller owns existing runtime, routes, UI and all real-DB acceptance runs.
After domain contracts freeze, agents may work on disjoint adapter/test files in
parallel. No simultaneous shared test-DB runs. Each task receives a fresh cheap
reviewer; no nested fan-out. Do not use a stronger model silently when a cheap
model cannot finish: record the blocker and request direction.

Read each increment's Interfaces before assignment. Proposed result DTOs belong
to that increment's pure domain module; dependent adapters import them. Before
parallel assignment, define any supporting DTO's exact field types in tests so
implementers cannot invent conflicting interfaces independently.

## Scope mapping / self-review

Spec L1 and retention: plan1; L2 and message trust: plan2; L3 approval and contract
changes: plan3; L4 public-source retrieval and resource limits: plan4; L5 evaluation,
TRIZ and optional Jev boundary: plan5. Chief UI is updated in each relevant plan.
Architecture checks begin in plan1 and run for every increment. Verification
separates fixtures, disposable DB, authenticated browser and authorized live work.

No claim that a complete security boundary is supplied by prompt rules or by a
worktree: execution-environment hardening remains an activation dependency from
the guardrails initiative. Until verified, do not run untrusted code autonomously.

## Final acceptance / live activation

- [ ] Run focused unit and serial disposable-DB suites, architecture checks and browser checks.
- [ ] Verify existing product approvals, cap5 and legacy Gantt remain compatible.
- [ ] Retain evidence and a rollback procedure; deploy additive migrations only after idle preflight and review.
- [ ] Before live research, require timezone/window, providers, source/egress policy, budgets and retention/export settings. Missing settings produce setup blockers.
- [ ] Run an explicitly authorized bounded canary; report real versus fake-adapter evidence. No background schedule is enabled merely because tests pass.

Implementation is not started by saving these plans. Owner review of the plans
precedes execution using the already requested cheap-subagent method.
