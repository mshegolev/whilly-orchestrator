## Purpose

The decision-gate capability governs whether a task is worth executing before
Whilly spends a full agent run on it. It covers two complementary surfaces: the
per-task refuse/accept gate in `whilly/decision_gate.py` (`evaluate`,
`build_prompt`, `parse_decision`, `label_flip_for_gh_task`), which filters
nonsense or under-specified tasks ahead of dispatch, and the deterministic
plan-level TRIZ contradiction analysis in `whilly/core/triz.py`
(`analyze_plan_triz`, `PlanTrizReport`, `PlanTrizFinding`), which inspects a
whole imported plan for structural contradictions, duplicate work, and resource
conflicts. This capability also defines the fail-open posture shared by both
surfaces and references `task-model-fsm` for the terminal SKIPPED outcome of a
refused task rather than re-specifying the state machine.

## Requirements

### Requirement: Short-description auto-refuse without an LLM call
The `decision_gate.evaluate` function SHALL return a `REFUSE` Decision with
`cost_usd` of `0.0` and SHALL NOT invoke the gate runner when the task's
trimmed `description` is shorter than `MIN_DESCRIPTION_LEN` (20 characters).

#### Scenario: Description below the minimum length is auto-refused
- **WHEN** `evaluate` is called with a task whose stripped `description` is
  fewer than 20 characters
- **THEN** the system SHALL return a `Decision` with `decision` equal to
  `REFUSE`
- **AND** the system SHALL set `cost_usd` to `0.0` because no runner was called
- **AND** the `reason` SHALL report the observed length against the minimum

#### Scenario: Sufficiently long description proceeds to the gate runner
- **WHEN** `evaluate` is called with a task whose stripped `description` is at
  least 20 characters
- **THEN** the system SHALL build a prompt via `build_prompt` and invoke the
  injected runner rather than auto-refusing

### Requirement: Fail-open on runner exception or non-zero exit
The `decision_gate.evaluate` function SHALL return a `PROCEED` Decision when the
gate runner raises an exception or returns a non-zero `exit_code`, because a
false-refuse is costlier than a false-proceed.

#### Scenario: Runner raises an exception
- **WHEN** the gate runner raises any exception during `evaluate`
- **THEN** the system SHALL catch it and return a `Decision` with `decision`
  equal to `PROCEED`
- **AND** the `reason` SHALL identify the outcome as fail-open from a runner
  exception

#### Scenario: Runner returns a non-zero exit code
- **WHEN** the gate runner returns an `AgentResult` whose `exit_code` is not
  zero
- **THEN** the system SHALL return a `Decision` with `decision` equal to
  `PROCEED`
- **AND** the system SHALL carry the runner's reported `cost_usd` and
  `raw_text` onto the returned Decision

### Requirement: Tolerant decision parsing with fail-open default
The `decision_gate.parse_decision` function SHALL extract a `(decision, reason)`
pair from bare JSON, an embedded JSON blob, or a bare keyword, and SHALL default
to `PROCEED` whenever no valid decision can be parsed.

#### Scenario: Bare JSON object is parsed directly
- **WHEN** `parse_decision` receives a string that is a single JSON object with
  a `decision` field of `proceed` or `refuse`
- **THEN** the system SHALL return that decision together with its `reason`

#### Scenario: JSON blob embedded in surrounding text is recovered
- **WHEN** the raw text contains a JSON object with a `decision` field
  surrounded by other prose
- **THEN** the system SHALL locate and parse the embedded blob and return its
  decision and reason

#### Scenario: Empty or unparseable text fails open to proceed
- **WHEN** `parse_decision` receives empty text, or text from which no valid
  `proceed` or `refuse` decision can be extracted
- **THEN** the system SHALL return `PROCEED` with a fail-open reason

### Requirement: Decision payload shape
The `decision_gate.Decision` dataclass SHALL carry the fields `decision`,
`reason`, `cost_usd`, and `raw_text`, where `decision` MUST be one of `PROCEED`
or `REFUSE`.

#### Scenario: Decision exposes the documented fields
- **WHEN** any `evaluate` path constructs a `Decision`
- **THEN** the returned object SHALL expose `decision`, `reason`, `cost_usd`,
  and `raw_text`
- **AND** the `decision` value SHALL be either `PROCEED` or `REFUSE`

### Requirement: GitHub label flip on a refused GitHub-sourced task
The `decision_gate.label_flip_for_gh_task` function SHALL attempt a GitHub label
flip (add `needs-clarification`, remove `whilly:ready`) only when the Decision is
a `REFUSE` and the task originates from GitHub Issues — its `id` begins with
`GH-` and its `prd_requirement` holds a parseable issue URL.

#### Scenario: Refused GitHub task triggers a label flip
- **WHEN** `label_flip_for_gh_task` is called with a `REFUSE` Decision for a
  task whose `id` starts with `GH-` and whose `prd_requirement` contains a
  GitHub issue URL with an owner/repo and issue number
- **THEN** the system SHALL attempt to flip the issue labels and SHALL return
  `True`

#### Scenario: Proceed decision performs no label flip
- **WHEN** `label_flip_for_gh_task` is called with a Decision whose `decision`
  is `PROCEED`
- **THEN** the system SHALL perform no GitHub action and SHALL return `False`

#### Scenario: Non-GitHub task performs no label flip
- **WHEN** the Decision is `REFUSE` but the task `id` does not start with `GH-`
  or `prd_requirement` is empty or lacks an issue URL
- **THEN** the system SHALL perform no GitHub action and SHALL return `False`

### Requirement: Deterministic plan-level TRIZ preflight report
The `core.triz.analyze_plan_triz` function SHALL inspect an imported `Plan`
deterministically — without any LLM call, subprocess, network, or Postgres
access — and SHALL return a `PlanTrizReport` carrying `plan_id`, `task_count`,
`verdict`, `ideality_score`, `findings`, `mergeable_groups`, `removable_tasks`,
and `summary`.

#### Scenario: Plan preflight returns a populated report
- **WHEN** `analyze_plan_triz` is called with an imported `Plan`
- **THEN** the system SHALL return a `PlanTrizReport` whose `plan_id` and
  `task_count` match the plan
- **AND** the system SHALL aggregate findings from the pure detectors, including
  the Decision Gate check, dependency analysis, duplicate-description grouping,
  shared-file grouping, and over-engineering detection

#### Scenario: Cyclic dependencies yield a reject verdict
- **WHEN** the analyzed plan contains a dependency cycle among its tasks
- **THEN** the system SHALL emit a `critical` `dependency_contradiction`
  `PlanTrizFinding` for the cycle
- **AND** the resulting report `verdict` SHALL be `reject`

#### Scenario: A clean plan is approved
- **WHEN** `analyze_plan_triz` finds no structural contradictions in the plan
- **THEN** the report `verdict` SHALL be `approve` with an empty `findings`
  tuple

### Requirement: Per-task TRIZ contradiction analysis is fail-open
The `core.triz.analyze_contradiction` function SHALL return a `TrizFinding` on a
positive contradiction verdict and SHALL return `None` for every other outcome —
including no contradiction and every soft-fail mode (claude absent, timeout,
malformed JSON, claude non-zero exit) — never re-raising into its caller.

#### Scenario: Positive verdict returns a TrizFinding
- **WHEN** `analyze_contradiction` runs the claude CLI within the hard
  `TIMEOUT_SECONDS` (25 s) and parses a positive contradiction verdict
- **THEN** the system SHALL return a `TrizFinding` carrying
  `contradiction_type` and `reason`

#### Scenario: Soft-fail modes return None without raising
- **WHEN** the claude binary is absent, the subprocess times out, the output is
  malformed JSON, or claude exits non-zero
- **THEN** the system SHALL return `None`
- **AND** the system SHALL NOT propagate an exception to the caller

### Requirement: Refused tasks reach the SKIPPED terminal outcome via the FSM
The decision-gate capability SHALL delegate the terminal handling of a refused
task to the `task-model-fsm` capability, which owns the SKIPPED status, and MUST
NOT redefine the task state machine within this capability.

#### Scenario: Refuse outcome defers terminal state to the FSM
- **WHEN** the gate returns a `REFUSE` Decision for a task
- **THEN** the task's terminal status transition to SKIPPED SHALL be governed by
  the `task-model-fsm` capability rather than by decision-gate
- **AND** decision-gate SHALL NOT re-specify the legal status values or
  transitions defined by `task-model-fsm`
