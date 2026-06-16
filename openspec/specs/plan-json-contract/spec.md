## Purpose

The plan-json-contract capability defines the on-disk JSON shape that Whilly
plans use as the source of truth for orchestration. It governs the top-level
plan envelope, the required and optional fields on every task, the v4
provenance and routing extensions, how parse errors are surfaced, how plans
are written atomically, how unknown keys round-trip, and how every task id is
validated before a plan is accepted.

## Requirements

### Requirement: Plan envelope shape
The system SHALL require a plan JSON document to be an object carrying a
non-empty string `project` and a `tasks` JSON array, and SHALL raise a
`PlanParseError` naming the offending field when either is missing or wrongly
typed.

#### Scenario: Missing required plan field
- **WHEN** `parse_plan` reads a document that omits `project` or `tasks`
- **THEN** the system SHALL raise a `PlanParseError` whose message names the
  missing field and the source path or `<dict>`

#### Scenario: Non-array tasks rejected
- **WHEN** a plan document has `tasks` that is not a JSON array
- **THEN** the system SHALL raise a `PlanParseError` stating `'tasks' must be a
  JSON array`

#### Scenario: Non-object top level rejected
- **WHEN** `parse_plan` decodes JSON whose top-level value is not an object
- **THEN** the system SHALL raise a `PlanParseError` naming the source path

### Requirement: Required task fields
The system SHALL model a managed task with the no-default fields `id`, `phase`,
`category`, `priority`, `description`, and `status`, and the defaulted fields
`dependencies`, `key_files`, `acceptance_criteria`, `test_steps`, and
`prd_requirement` per the `Task` dataclass in `whilly/task_manager.py`.

#### Scenario: Task constructed from a complete dict
- **WHEN** `Task.from_dict` receives a dict containing all six no-default
  fields plus any defaulted fields
- **THEN** the system SHALL construct a `Task` populating those fields

#### Scenario: Defaulted collection fields absent
- **WHEN** a task dict omits `dependencies`, `key_files`,
  `acceptance_criteria`, or `test_steps`
- **THEN** the system SHALL default each absent collection field to empty and
  `prd_requirement` to the empty string

### Requirement: Parser per-task minimum and priority range
The system SHALL require every task entry to carry a non-empty `id` plus
`status`, `priority`, and `description`, and SHALL restrict `priority` to one
of critical, high, medium, or low; a violation MUST raise a `PlanParseError`
naming the offending `task.id`.

#### Scenario: Task missing a parser-required field
- **WHEN** `parse_plan` reads a task that omits `status`, `priority`, or
  `description`
- **THEN** the system SHALL raise a `PlanParseError` naming the task id and the
  missing field

#### Scenario: Invalid priority value
- **WHEN** a task declares a `priority` outside critical, high, medium, low
- **THEN** the system SHALL raise a `PlanParseError` listing the valid priority
  values

#### Scenario: Status value delegated to the FSM
- **WHEN** a task declares a `status` value
- **THEN** the system SHALL accept only the legal statuses defined by the
  task-model-fsm capability and raise a `PlanParseError` otherwise

### Requirement: Optional v4 plan extensions
The system SHALL accept the optional top-level extensions `plan_id`, `origin`
(PlanOrigin), `repo_targets` (a list of RepoTarget), and
`verification_commands` (a list of VerificationCommand), and MUST raise a
`PlanParseError` when any present extension has the wrong shape.

#### Scenario: Plan id falls back to project
- **WHEN** a plan document omits `plan_id`
- **THEN** the system SHALL use `project` as the plan identifier

#### Scenario: Origin provenance parsed
- **WHEN** a plan document carries an `origin` object with non-empty `system`
  and `ref`
- **THEN** the system SHALL parse it into a PlanOrigin and SHALL raise a
  `PlanParseError` when `system` or `ref` is missing or empty

#### Scenario: Repo target referenced by a task must be declared
- **WHEN** a task sets `repo_target_id` to a value not present in the top-level
  `repo_targets`
- **THEN** the system SHALL raise a `PlanParseError` reporting the undeclared
  repo target id

### Requirement: Atomic plan writes
The system SHALL persist plan changes by writing to a temporary file in the
plan's directory and then `os.replace`-ing it into place, so a crash never
leaves a half-written plan on disk.

#### Scenario: Successful save replaces atomically
- **WHEN** `TaskManager.save` writes the current tasks back to disk
- **THEN** the system SHALL write a temp file then atomically replace the plan
  file with it

#### Scenario: Failure cleans up the temp file
- **WHEN** writing the temp file raises before the replace completes
- **THEN** the system SHALL remove the temp file and re-raise rather than leave
  a partial artifact

### Requirement: Round-trip tolerance of unknown keys
The system SHALL ignore unknown on-disk keys when constructing a task, so extra
keys are tolerated on read but MUST be dropped on the `to_dict` / serialize
round-trip rather than re-emitted.

#### Scenario: Unknown key tolerated on read
- **WHEN** `Task.from_dict` receives a dict containing keys not present on the
  dataclass
- **THEN** the system SHALL filter the dict to known fields and construct the
  task without error

#### Scenario: Extra key dropped on round-trip
- **WHEN** a task is read from disk and then serialized again
- **THEN** the system SHALL emit only the canonical fields and SHALL NOT
  re-emit the unknown key

### Requirement: Every task id is validated
The system SHALL validate the id of every task in the plan array via
`validate_task_id` when `validate_schema` (in `whilly/cli/__init__.py`) runs,
and SHALL NOT limit validation to only the first three tasks.

#### Scenario: All task ids validated
- **WHEN** `validate_schema` is given a plan with any number of tasks
- **THEN** the system SHALL validate the `id` of every task that carries one,
  regardless of position in the array

#### Scenario: Malformed id rejected before side effects
- **WHEN** any task in the array carries an id that fails `validate_task_id`
- **THEN** the system SHALL raise a `ValueError` naming the offending id before
  any downstream side effect
