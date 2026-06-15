## Purpose

The batch-planning capability defines how Whilly groups a set of ready tasks
into parallel-safe batches based on `key_files` overlap. It governs the two pure
grouping helpers in `whilly/orchestrator.py` — `plan_batches` (deterministic) and
`plan_batches_llm` (LLM-driven with deterministic fallback) — that decide which
ready tasks may run concurrently without writing to the same files. These helpers
are pure functions over the ready set; this capability specifies only the
batches they return, not how a run loop consumes them.

## Requirements

### Requirement: Non-overlapping key_files grouping
The `plan_batches` helper SHALL place two ready tasks in the same batch only when
their `key_files` sets do not intersect, so that any two tasks sharing at least
one key file are never co-batched.

#### Scenario: Tasks sharing a key file are not co-batched
- **WHEN** `plan_batches` is called with `max_parallel` greater than 1 and two
  ready tasks list a common path in their `key_files`
- **THEN** the two tasks SHALL be returned in different batches

#### Scenario: Tasks with disjoint key files may share a batch
- **WHEN** `plan_batches` is called with `max_parallel` greater than 1 and two
  ready tasks have no path in common across their `key_files`
- **THEN** the two tasks MAY be returned in the same batch subject to the batch
  size cap

### Requirement: Empty key_files never blocks a batch
The `plan_batches` helper SHALL treat a task with an empty `key_files` list as
conflicting with no other task, so an empty-key-files task never prevents another
task from joining a batch.

#### Scenario: Empty-key-files task admitted to a batch
- **WHEN** `plan_batches` is called with `max_parallel` greater than 1 and a
  ready task has an empty `key_files` list
- **THEN** that task SHALL be eligible to join a batch regardless of the
  `key_files` of the other tasks already in that batch

### Requirement: Single-task batches when max_parallel is at most one
The `plan_batches` helper SHALL return exactly one task per batch when
`max_parallel` is less than or equal to 1, preserving the order of the ready set.

#### Scenario: Serial planning yields one task per batch
- **WHEN** `plan_batches` is called with `max_parallel` less than or equal to 1
  and a non-empty ready set
- **THEN** the helper SHALL return one batch per ready task, each batch
  containing exactly one task

### Requirement: Batch size capped by max_parallel
The `plan_batches` helper SHALL NOT return any batch containing more tasks than
`max_parallel`.

#### Scenario: Batch never exceeds the parallelism cap
- **WHEN** `plan_batches` is called with a `max_parallel` value of N greater than
  1 and more than N mutually non-conflicting ready tasks
- **THEN** each returned batch SHALL contain at most N tasks

### Requirement: LLM grouping falls back to deterministic planning
The `plan_batches_llm` helper SHALL fall back to `plan_batches` whenever the LLM
agent exits non-zero, returns unparseable output, returns a non-list response, or
yields no usable batches, so its result is never worse than the deterministic
grouping.

#### Scenario: Non-zero agent exit triggers fallback
- **WHEN** `plan_batches_llm` runs the orchestrator agent and the agent result
  has a non-zero exit code
- **THEN** the helper SHALL return the result of `plan_batches` for the same
  ready set and `max_parallel`

#### Scenario: Non-list LLM response triggers fallback
- **WHEN** `plan_batches_llm` parses the agent output and the parsed value is not
  a list
- **THEN** the helper SHALL return the result of `plan_batches` for the same
  ready set and `max_parallel`

#### Scenario: LLM batch ids validated against the ready set
- **WHEN** `plan_batches_llm` builds batches from the LLM response
- **THEN** the helper SHALL include only task ids that belong to the ready set
- **AND** SHALL fall back to `plan_batches` when no valid batch remains

### Requirement: Trivial ready sets return single-task batches
The `plan_batches_llm` helper SHALL return one task per batch without invoking
the LLM when the ready set contains one task or fewer.

#### Scenario: One-or-fewer ready tasks bypass the LLM
- **WHEN** `plan_batches_llm` is called with a ready set of length one or zero
- **THEN** the helper SHALL return one batch per ready task without dispatching
  an orchestrator agent
