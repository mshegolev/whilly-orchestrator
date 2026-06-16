## Purpose

The task-generation capability governs the PRD-to-tasks.json generation
contract: given an existing PRD markdown file, produce a tasks payload that the
orchestrator can run. It covers `whilly/cli/init.py` `run_init_command` (the
`whilly init` PRD-to-plan pipeline) and the generator functions in
`whilly/prd_generator.py` — the v3 file flow `generate_tasks` (writes
`<slug>_tasks.json`), the v4 dict flow `generate_tasks_dict` (returns the
same-shape dict consumed by the plan import), and the shared
`_build_tasks_payload` that reads the PRD, calls Claude, and validates the JSON
response. This capability references the plan-json-contract capability for the
individual task field schema and scopes that field schema OUT — it specifies
how tasks are generated, not what each task field means.

## Requirements

### Requirement: PRD-to-tasks payload construction
The system SHALL build a tasks payload from an existing PRD file by reading the
PRD, calling the Claude CLI through `_call_claude` with the tasks-generation
prompt, and parsing the response into a dict carrying a `project` string and a
non-empty `tasks` list via the shared `_build_tasks_payload` helper.

#### Scenario: PRD drives a tasks payload
- **WHEN** `_build_tasks_payload` is called with a path to an existing PRD file
- **THEN** the system SHALL read the PRD text, prompt Claude for a tasks JSON
  document, and return a dict with `project` and a non-empty `tasks` list

#### Scenario: Missing PRD file rejected
- **WHEN** `_build_tasks_payload` is called with a path that does not exist
- **THEN** the system SHALL raise a `FileNotFoundError` before any Claude call

#### Scenario: No tasks generated is an error
- **WHEN** the parsed Claude response contains an empty or absent `tasks` list
- **THEN** the system SHALL raise a `RuntimeError` stating no tasks were
  generated

### Requirement: Robust JSON parsing with forensics fallback
The system SHALL strip markdown fences from the model output and parse it as
JSON, MUST attempt a `json_repair` fallback when strict parsing fails, and
SHALL persist the raw output to the supplied `raw_dump_path` (when provided)
before raising on unrecoverable parse failure.

#### Scenario: Repairable JSON is salvaged
- **WHEN** the model output is not strict JSON but `json_repair` can parse it
- **THEN** the system SHALL use the repaired result rather than raising

#### Scenario: Unparseable output saved for forensics
- **WHEN** both strict parsing and `json_repair` fail and a `raw_dump_path`
  was supplied
- **THEN** the system SHALL write the raw output to that path and raise a
  `RuntimeError` naming the dump location

### Requirement: Task field defaults applied at generation
The system SHALL apply default values to every generated task — defaulting
`status` to `pending` and `dependencies`, `key_files`, `acceptance_criteria`,
and `test_steps` to empty lists — and MUST assign a sequential `TASK-NNN` id to
any task missing one; the meaning of each field is governed by the
plan-json-contract capability and is not duplicated here.

#### Scenario: Missing collection fields defaulted
- **WHEN** a generated task omits `status`, `dependencies`, `key_files`,
  `acceptance_criteria`, or `test_steps`
- **THEN** the system SHALL set `status` to `pending` and each absent
  collection field to an empty list

#### Scenario: Missing id auto-numbered
- **WHEN** a generated task has no `id`
- **THEN** the system SHALL assign a zero-padded sequential id of the form
  `TASK-NNN` based on its position in the list

### Requirement: Dual generation flows over one payload builder
The system SHALL expose two generation entry points over the shared
`_build_tasks_payload`: `generate_tasks`, which writes the payload to
`<slug>_tasks.json` and returns its path, and `generate_tasks_dict`, which
stamps the caller-supplied `plan_id` onto the same-shape payload and returns it
in-memory without a persistent disk write.

#### Scenario: File flow writes tasks json
- **WHEN** `generate_tasks` is called with a PRD path
- **THEN** the system SHALL write the payload to `<slug>_tasks.json` and return
  the path to that file

#### Scenario: Dict flow stamps plan id and skips disk
- **WHEN** `generate_tasks_dict` is called with a PRD path and a `plan_id`
- **THEN** the system SHALL set the payload's `plan_id` to the supplied value
  and return the dict, removing any temporary forensics file on success

### Requirement: Init pipeline slugifies, refuses overwrite, then imports
The system SHALL, in `run_init_command`, derive a kebab-case slug from the
idea (or validate an explicit `--slug`), resolve the PRD path to
`PRD-<slug>.md`, refuse to overwrite an existing PRD unless `--force` is given,
and then drive `generate_tasks_dict` to build the plan before importing it.

#### Scenario: Existing PRD refused without --force
- **WHEN** `run_init_command` resolves a `PRD-<slug>.md` path that already
  exists and `--force` was not passed
- **THEN** the system SHALL print an error hinting at `--force` or a different
  slug and return the user-error exit code without regenerating

#### Scenario: Empty idea rejected
- **WHEN** `run_init_command` receives idea text that is empty after joining
  and stripping
- **THEN** the system SHALL print an error and return the user-error exit code

#### Scenario: Generated plan imported to Postgres
- **WHEN** the PRD exists, `--no-import` is not set, and the database URL env
  var is set
- **THEN** the system SHALL build the payload via the tasks builder, validate
  it, insert the plan and tasks, and print the imported task count
