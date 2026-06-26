## MODIFIED Requirements

### Requirement: Runner selection between tmux and subprocess
The system SHALL dispatch every agent in the live run path (`whilly/cli/run.py`
and `whilly/cli/worker.py`) through the subprocess runner
(`whilly.adapters.runner.run_task`, the Claude CLI wrapper). The tmux-session
runner (`tmux_runner.launch_agent`) and the `USE_TMUX` flag (`WHILLY_USE_TMUX`)
are **legacy and unwired**: no live dispatch path imports the tmux runner for
runner selection or reads `WHILLY_USE_TMUX`, and `WHILLY_USE_TMUX` is a
parsed-but-inert no-op (see configuration). The system SHALL NOT select the
tmux runner in the live path.

#### Scenario: Live dispatch always uses the subprocess runner
- **WHEN** the local worker (`whilly/cli/run.py`) or remote worker
  (`whilly/cli/worker.py`) dispatches a task to its runner
- **THEN** the system SHALL invoke `whilly.adapters.runner.run_task` and SHALL
  NOT launch a tmux session

#### Scenario: USE_TMUX does not select a tmux runner
- **WHEN** `WHILLY_USE_TMUX` is set in the environment
- **THEN** the system SHALL dispatch agents identically to when it is unset,
  because `WHILLY_USE_TMUX` is a no-op and no live path consults it for runner
  selection

### Requirement: One tmux session per task
The legacy `tmux_runner` module SHALL name each tmux agent session
`whilly-{task_id}` using the flattened safe task id and MUST run exactly one
agent session per task, killing any pre-existing session of the same name
before launch. This behavior is **not wired into the live run path** — neither
the local nor the remote worker invokes `tmux_runner.launch_agent` — and it
applies only when `tmux_runner.launch_agent` is called directly.

#### Scenario: Session name derived from task id
- **WHEN** `tmux_runner.launch_agent` is invoked directly for a task
- **THEN** the session name SHALL be `whilly-` followed by the safe-flattened
  task id produced by `safe_task_id_filename`

#### Scenario: Stale session replaced before launch
- **WHEN** a tmux session named `whilly-{task_id}` already exists at launch
- **THEN** the legacy runner SHALL kill that session before starting the new one
  so exactly one session per task remains
