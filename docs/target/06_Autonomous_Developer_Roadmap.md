# Roadmap: From Whilly Orchestrator to Autonomous Developer

## Executive Summary

The next step toward an autonomous developer is not simply “let the agent code more.” The system needs additional control loops, verification layers, repository understanding, review handling, rollback, sandboxing, and decision governance.

Current Whilly is best understood as an AI-assisted task orchestrator. To become an autonomous developer, it must evolve from executing isolated tasks to managing the full engineering lifecycle:

```text
Understand → Plan → Implement → Verify → Review → Repair → Integrate → Release candidate → Learn
```

The most important shift is adding closed-loop autonomy with safety boundaries.

## Maturity Levels

### Level 0 — Manual AI Assistant

Human gives prompt. AI suggests code. Human applies changes.

### Level 1 — Task Runner

System runs an AI agent on a structured task.

Current Whilly is roughly here, with stronger orchestration features: queue, state, workers, events, guards.

### Level 2 — Verified Task Executor

System executes a task and runs configured verification before considering it successful.

Required next step:

- project profiles;
- verification stages;
- human review checkpoints;
- compliance validation;
- documentation alignment.

### Level 3 — PR-Capable Autonomous Contributor

System can create branches/PRs, run CI, react to CI failures, update PRs, and request human review.

Needed:

- branch management;
- PR sink wired into pipeline;
- CI status polling;
- PR review comment ingestion;
- repair loop;
- reviewer assignment;
- safe force-push policy;
- branch protection awareness.

### Level 4 — Multi-Task Feature Developer

System can decompose features into multiple dependent tasks, execute them, coordinate changes, and maintain context across tasks.

Needed:

- feature-level plan object;
- task graph refinement;
- cross-task memory;
- dependency-aware execution;
- incremental verification;
- architectural consistency checks;
- human approval at plan boundaries.

### Level 5 — Multi-Repo Autonomous Developer

System can coordinate changes across multiple repositories or services.

Needed:

- per-task repo/workspace mapping;
- multi-repo workspace manager;
- cross-repo dependency graph;
- integration test environment;
- versioning/release coordination;
- distributed rollback plan;
- service ownership rules.

### Level 6 — Release-Candidate Autonomous Engineer

System can prepare release candidates but still requires human approval for production.

Needed:

- release notes generation;
- changelog validation;
- deployment plan generation;
- environment-specific checks;
- canary/staging verification;
- risk scoring;
- final human approval gate.

## What Is Missing for Autonomous Developer

## 1. Strong Repository Understanding

The system needs persistent, queryable understanding of the codebase.

Required capabilities:

- repository indexing;
- symbols/classes/functions map;
- dependency graph;
- test ownership map;
- service/module ownership map;
- API/schema map;
- architectural decision memory;
- previous task/PR history.

MVP implementation:

- static file index;
- dependency graph for supported languages;
- test discovery;
- changed-file to test mapping;
- lightweight project memory from previous events and PRs.

Risks:

- stale repo memory causing bad changes;
- over-reliance on embeddings without structural analysis;
- memory leaking secrets or sensitive code.

## 2. Planning and Decomposition Loop

An autonomous developer must convert a feature or issue into an executable plan.

Required capabilities:

- feature intake;
- ambiguity detection;
- plan generation;
- plan critique;
- dependency ordering;
- risk scoring;
- approval gate before execution.

MVP implementation:

- `FeaturePlan` object;
- `PlanReviewGate`;
- generated task DAG;
- human approval before applying generated plans.

Acceptance criteria:

- vague feature requests do not go straight to execution;
- generated plan includes acceptance criteria and test strategy;
- high-risk plans require human approval.

## 3. Verification as a First-Class System

Autonomy without verification is dangerous.

Required capabilities:

- project-specific verification;
- test selection;
- lint/type checks;
- contract tests;
- CI integration;
- flaky test detection;
- verification result storage;
- failure classification.

MVP implementation:

- configured verification commands;
- CI status polling;
- required vs optional checks;
- verification artifacts linked to task events.

Acceptance criteria:

- task cannot be treated as verified if required checks fail;
- CI failures trigger repair loop or human escalation;
- verification results are auditable.

## 4. Closed-Loop Repair

The system must react to failures instead of stopping at first failed attempt.

Required capabilities:

- classify failure type;
- summarize failure;
- generate repair prompt;
- re-run agent with context;
- limit retry budget;
- detect repeated failure;
- escalate to human.

MVP implementation:

```text
execute → verify → if fail: repair attempt 1 → verify → repair attempt 2 → escalate
```

Acceptance criteria:

- retry budget is explicit;
- repeated failures do not loop forever;
- repair attempts are linked to original task;
- human sees concise failure history.

## 5. PR Review Feedback Loop

To act as an autonomous contributor, the system must respond to review comments.

Required capabilities:

- read PR review comments;
- distinguish blocking vs non-blocking comments;
- map comments to files/lines;
- generate fix tasks;
- update branch;
- reply to comments;
- request re-review.

MVP implementation:

- GitHub PR review poller;
- review comment classifier;
- repair task generator;
- branch update runner.

Acceptance criteria:

- requested changes produce follow-up tasks;
- resolved comments are acknowledged;
- human can stop the loop.

## 6. Safe Workspace and Sandbox

Autonomous development requires stronger isolation.

Required capabilities:

- per-task worktree or clone;
- container/VM sandbox;
- network policy;
- secret isolation;
- filesystem allowlist;
- command allowlist/denylist;
- resource limits;
- artifact capture.

MVP implementation:

- per-task git worktree;
- containerized execution option;
- restricted environment variables;
- command policy engine.

Acceptance criteria:

- no task runs directly in an uncontrolled shared workspace by default;
- dangerous commands are blocked;
- secrets are not exposed unless explicitly allowed;
- task artifacts are preserved for audit.

## 7. Branch, Commit and Rollback Management

Autonomous developer must manage code changes as reversible units.

Required capabilities:

- branch per task;
- structured commits;
- diff summary;
- backup tag;
- soft/hard rollback policy;
- conflict detection;
- rebase/update strategy.

MVP implementation:

- branch naming convention;
- pre-execution clean working tree check;
- post-execution diff capture;
- commit only after verification;
- rollback command for failed tasks.

Acceptance criteria:

- failed task does not pollute main workspace;
- every change is attributable to a task;
- rollback path is documented and tested.

## 8. Multi-Repo / Multi-Service Orchestration

Autonomous developer needs to coordinate across services.

Required capabilities:

- repo registry;
- service ownership map;
- task-to-repo routing;
- cross-repo task graph;
- integration environment;
- release dependency management.

MVP implementation:

- `repositories` section in project profile;
- per-task `repo_id`;
- worker workspace per repo;
- explicit multi-repo feature plan.

Acceptance criteria:

- multi-repo work is not hidden inside one vague task;
- each repo change has its own verification;
- integration verification runs before final review.

## 9. Human Governance and Approval Policy

Autonomy needs clear governance.

Required capabilities:

- policy engine;
- risk scoring;
- approval gates;
- escalation rules;
- audit trail;
- role-based approvals.

Example policy:

```yaml
approval_policy:
  require_human_for:
    - production_release
    - database_migration
    - security_sensitive_code
    - authz_authn_changes
    - multi_repo_changes
    - destructive_commands
    - dependency_upgrades
```

Acceptance criteria:

- high-risk tasks cannot bypass approval;
- approval events are auditable;
- policy is configurable per project.

## 10. Evaluation and Quality Scoring

The system must measure autonomous performance.

Required metrics:

- task success rate;
- verification pass rate;
- repair success rate;
- PR acceptance rate;
- human intervention rate;
- regression rate;
- escaped defect rate;
- cost per successful task;
- time to verified PR.

MVP implementation:

- metrics for stage outcomes;
- task evaluation record;
- dashboard for autonomous loops.

## Recommended Next Step

Do not jump directly to full autonomous developer.

Recommended sequence:

### Step 1 — Verified Task Executor

Implement:

- project profiles;
- verification stages;
- human review checkpoints;
- compliance validation.

### Step 2 — PR-Capable Contributor

Implement:

- branch per task;
- PR creation as configured sink;
- CI polling;
- PR review feedback ingestion;
- repair loop.

### Step 3 — Feature-Level Developer

Implement:

- feature plan object;
- task DAG generation;
- plan approval gate;
- cross-task memory;
- feature-level verification.

### Step 4 — Multi-Repo Developer

Implement:

- repo registry;
- per-task repo mapping;
- multi-repo workspace manager;
- integration verification.

### Step 5 — Release Candidate Automation

Implement:

- release candidate preparation;
- release notes;
- staging/canary validation;
- human approval for production.

## Concrete Next Architecture Additions

### New modules

```text
whilly/
  profiles/
  pipeline/
  verification/
  review/
  ci/
  workspace/
  memory/
  planning/
  governance/
  evaluation/
```

### New domain objects

```text
ProjectProfile
PipelineStage
VerificationRun
HumanReviewCheckpoint
FeaturePlan
RepairAttempt
PullRequestRun
CIStatus
WorkspaceSession
RiskAssessment
ApprovalPolicy
EvaluationRecord
```

### New state concepts

Avoid replacing the current task state machine immediately. Add higher-level execution states:

```text
EXECUTED
VERIFICATION_FAILED
VERIFIED
NEEDS_REVIEW
REVIEW_CHANGES_REQUESTED
REPAIRING
READY_FOR_PR
PR_OPENED
READY_FOR_HUMAN_MERGE
```

These can initially be modeled as events/metadata, then promoted to first-class DB state if stable.

## Critical Risks

1. **False autonomy**
   Claiming autonomous developer status before verification, review and rollback loops exist.

2. **Unsafe execution**
   Running agent commands in uncontrolled workspaces.

3. **Silent failure**
   Treating completion markers as correctness.

4. **Unbounded loops**
   Letting repair or review cycles continue without budget limits.

5. **Bad memory**
   Persisting stale or incorrect assumptions.

6. **Multi-repo complexity**
   Coordinating changes without integration verification.

7. **Governance gaps**
   Allowing agents to touch auth, migrations, infra or release paths without approval.

## MVP Definition: Autonomous Developer v0

A realistic first autonomous developer milestone:

- accepts a well-scoped issue;
- creates a branch/workspace;
- implements change;
- runs configured verification;
- if verification fails, attempts bounded repair;
- creates draft PR;
- monitors CI;
- if CI fails, attempts bounded repair;
- asks human for review;
- responds to requested changes within retry budget;
- never merges without approval;
- records all events and artifacts.

This is a safe and defensible autonomous developer v0.
