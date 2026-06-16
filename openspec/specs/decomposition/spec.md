## Purpose

The decomposition capability governs mid-run splitting of oversized or unclear
pending tasks into smaller subtasks via an LLM agent, as implemented in
`whilly/decomposer.py`. It covers the size heuristic (`needs_decompose`), the
LLM split prompt (`build_decompose_prompt`), the cached run entry point
(`run_decompose`), and the `WHILLY_DECOMPOSE_EVERY` cadence (default 5,
`whilly/config.py`). It references `task-model-fsm` for the `pending` status and
`plan-json-contract` for task fields without re-specifying them. This capability
also normatively records its true v4 wiring status: the decomposer consumes the
legacy in-memory `TaskManager` plus a `use_tmux` flag and is NOT invoked by the
v4 worker-claim run path.

## Requirements

### Requirement: Pending-only decomposition heuristic
The `needs_decompose` function SHALL return True when, and only when, at least
one task whose status is `pending` satisfies any of: six or more
`acceptance_criteria` entries, a `description` containing two or more `" и "`
substrings, or a `description` containing one or more `" + "` substring; tasks
in any non-pending status SHALL be ignored.

#### Scenario: Pending task with six acceptance criteria triggers decompose
- **WHEN** `needs_decompose(tm)` is evaluated and a `pending` task has six or
  more `acceptance_criteria`
- **THEN** the system SHALL return True

#### Scenario: Pending task description with conjunctions triggers decompose
- **WHEN** a `pending` task's `description` contains two or more `" и "`
  substrings, or one or more `" + "` substring
- **THEN** the system SHALL return True

#### Scenario: Non-pending tasks are ignored
- **WHEN** every oversized task has a status other than `pending`
- **THEN** the system SHALL return False

### Requirement: LLM split prompt contract
The `build_decompose_prompt` function SHALL emit a prompt that references the
tasks file, instructs the agent to split each oversized pending task into two to
five subtasks with `TASK-XXXa`/`TASK-XXXb` style IDs inheriting the parent's
phase, category, and priority, forbids touching tasks in `done`, `in_progress`,
or `failed` status, and requires a `<promise>DECOMPOSED N</promise>` or
`<promise>NO_DECOMPOSE</promise>` completion marker.

#### Scenario: Prompt instructs subtask creation and protects terminal tasks
- **WHEN** `build_decompose_prompt(tasks_file)` is called
- **THEN** the returned prompt SHALL instruct the agent to produce two to five
  inheriting subtasks per oversized task
- **AND** the prompt SHALL forbid modifying `done`, `in_progress`, or `failed`
  tasks and SHALL require the DECOMPOSED/NO_DECOMPOSE completion marker

### Requirement: Cached, idempotent decomposition run
The `run_decompose` function SHALL compute `_tasks_hash` (a SHA256 over the
sorted `pending` task IDs and descriptions), short-circuit to a zero result when
that hash matches the last run that resulted in `NO_DECOMPOSE`, otherwise run the
agent, reload the `TaskManager`, and return the non-negative task-count delta
between the post-reload and pre-run totals.

#### Scenario: Cache hit short-circuits the run
- **WHEN** `run_decompose` computes a `_tasks_hash` equal to the cached hash and
  the cached result was `NO_DECOMPOSE`
- **THEN** the system SHALL skip dispatching the agent and SHALL return 0

#### Scenario: NO_DECOMPOSE response returns zero
- **WHEN** the agent result text contains `<promise>NO_DECOMPOSE</promise>`
- **THEN** the system SHALL record `NO_DECOMPOSE` in the cache and SHALL return 0

#### Scenario: Successful split returns the count delta
- **WHEN** the agent modifies the plan file and the reloaded `TaskManager` total
  count exceeds the pre-run total
- **THEN** the system SHALL return the positive delta and SHALL record
  `DECOMPOSED` in the cache

### Requirement: Decomposition cadence configuration
The system SHALL expose a `DECOMPOSE_EVERY` configuration value (env
`WHILLY_DECOMPOSE_EVERY`) defaulting to 5, representing the iteration cadence at
which a decomposition pass is intended to run.

#### Scenario: Default cadence is five
- **WHEN** no `WHILLY_DECOMPOSE_EVERY` override is supplied
- **THEN** the configured `DECOMPOSE_EVERY` value SHALL be 5

### Requirement: Legacy unwired status in the v4 run path
The system SHALL treat decomposition as a legacy capability that operates on the
in-memory `TaskManager` and a `use_tmux` flag, and SHALL NOT invoke
`needs_decompose` or `run_decompose` from the v4 worker-claim run path
(`whilly/cli/run.py`, `whilly/cli/worker.py`, `whilly/worker/main.py`); these
functions are unreferenced by the active run loop and are retained as legacy
code rather than live behavior.

#### Scenario: Worker-claim run path never calls the decomposer
- **WHEN** the v4 worker-claim run path executes a plan
- **THEN** the system SHALL NOT import or call `needs_decompose` or
  `run_decompose`
- **AND** the decomposer SHALL remain coupled only to the legacy in-memory
  `TaskManager` and the `use_tmux` flag of `run_decompose`

#### Scenario: Specification reflects legacy status, not aspirational wiring
- **WHEN** this capability is consulted to understand v4 runtime behavior
- **THEN** the specification SHALL present decomposition as legacy and unwired in
  the worker-claim path rather than asserting it runs on the configured cadence
