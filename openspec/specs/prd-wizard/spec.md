## Purpose

The prd-wizard capability governs interactive PRD authoring driven through the
Claude CLI and run alongside the orchestrator. It covers two delivery surfaces:
`PrdWizard` (background daemon thread launched from the running TUI, defined in
`whilly/prd_wizard.py`) and `run_prd_wizard` (foreground interactive Claude CLI
launched from the command line, defined in `whilly/prd_launcher.py`). Both
conduct a conversational requirements interview, produce a `PRD-<slug>.md`
document, then derive a tasks file via `prd_generator.generate_tasks`. This
capability also governs `merge_tasks_into_plan`, which folds wizard-generated
tasks into an already-running plan with conflict-free re-IDing.

## Requirements

### Requirement: Background wizard session lifecycle
The `PrdWizard.start` method SHALL launch the PRD session on a background daemon
thread and SHALL be guarded against concurrent double-start via the `is_running`
property, returning early without starting a second thread when a session is
already running.

#### Scenario: Start launches a daemon thread
- **WHEN** `PrdWizard.start(idea)` is called and `is_running` is False
- **THEN** the system SHALL set `is_running` to True
- **AND** the system SHALL launch `_run` on a daemon `threading.Thread` so the
  orchestrator's main loop continues uninterrupted

#### Scenario: Double-start is refused
- **WHEN** `PrdWizard.start` is called while `is_running` is already True
- **THEN** the system SHALL log a warning and return without starting a second
  thread

### Requirement: Interactive then non-interactive PRD authoring
The `PrdWizard` session SHALL first attempt interactive PRD authoring via
`_run_claude_interactive` (a Claude CLI process inside a `whilly-prd-wizard`
tmux session loaded with the PRD system prompt), and SHALL fall back to
`_run_claude_noninteractive` (a single headless `claude --print` call) when the
tmux session cannot be created.

#### Scenario: Interactive session produces the PRD file
- **WHEN** the interactive tmux session runs to completion and the target
  `PRD-<slug>.md` path exists
- **THEN** the system SHALL treat PRD authoring as successful and proceed to
  task generation

#### Scenario: Fallback to non-interactive on tmux failure
- **WHEN** creating the `whilly-prd-wizard` tmux session returns a non-zero exit
  code
- **THEN** the system SHALL invoke `_run_claude_noninteractive` to generate the
  PRD without tmux

#### Scenario: Missing PRD file fails the session
- **WHEN** the Claude session ends but the target PRD path does not exist
- **THEN** the system SHALL set `WizardResult.error` to indicate no PRD file was
  created and SHALL NOT attempt task generation

### Requirement: WizardResult reporting and completion callback
The `PrdWizard` session SHALL record its outcome in a `WizardResult` carrying
`success`, `prd_path`, `tasks_path`, `task_count`, `error`, `idea`, and
`elapsed_sec`, store it on `self.result`, clear `is_running`, and invoke the
`on_complete` callback when one was supplied.

#### Scenario: Successful run populates the result
- **WHEN** the PRD is authored and `_generate_tasks` returns a tasks path
- **THEN** the system SHALL set `WizardResult.success` True, populate
  `prd_path`, `tasks_path`, and `task_count` from the generated tasks file
- **AND** the system SHALL set `elapsed_sec` to the measured session duration

#### Scenario: Completion callback always fires
- **WHEN** the background `_run` finishes, whether it succeeded or raised
- **THEN** the system SHALL clear `is_running` and SHALL invoke the
  `on_complete` callback with the `WizardResult` if a callback was supplied

### Requirement: Foreground CLI wizard with system prompt
The `run_prd_wizard` function SHALL launch the Claude CLI interactively in the
current terminal with the PRD master prompt built by `_build_system_prompt`
(which forces requirements-interview mode and pins the Write path to the
resolved `PRD-<slug>.md`), and SHALL run `generate_tasks` after the CLI exits
when `generate_tasks_after` is True and the PRD file was created.

#### Scenario: Slug derivation and prompt assembly
- **WHEN** `run_prd_wizard` is invoked with a slug or description
- **THEN** the system SHALL sanitize or shorten the slug into a `PRD-<slug>.md`
  path and SHALL pass `_build_system_prompt(prd_path)` to Claude via
  `--append-system-prompt`

#### Scenario: Missing PRD after exit returns failure
- **WHEN** the Claude CLI exits and the resolved PRD path does not exist
- **THEN** the system SHALL print a not-created message and SHALL return exit
  code 1

#### Scenario: Task generation after a created PRD
- **WHEN** the PRD file exists after the CLI exits and `generate_tasks_after` is
  True
- **THEN** the system SHALL call `prd_generator.generate_tasks` on the PRD and
  SHALL return exit code 0

### Requirement: Merge generated tasks into a running plan
The `merge_tasks_into_plan` function SHALL append source tasks into the target
plan, re-assigning each a fresh `TASK-NNN` ID above the target's current maximum,
forcing status to `pending`, dropping dependencies that do not resolve to an
existing target ID, tagging `_origin`, and SHALL return the number of tasks
added.

#### Scenario: Tasks merged with conflict-free IDs
- **WHEN** `merge_tasks_into_plan` is called with a source tasks file and a
  target plan file
- **THEN** the system SHALL assign each merged task a new `TASK-NNN` ID strictly
  above the target's highest existing numeric ID
- **AND** the system SHALL set each merged task's status to `pending` and tag
  its `_origin` with the source file name

#### Scenario: Unresolvable dependencies are dropped
- **WHEN** a source task lists a dependency ID that is not present among the
  target plan's existing task IDs
- **THEN** the system SHALL remove that dependency from the merged task before
  appending it to the target plan
