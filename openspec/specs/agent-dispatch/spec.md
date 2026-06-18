## Purpose

The agent-dispatch capability governs how Whilly hands a single task to an
external coding-agent process and which dispatch mechanism it uses. It covers
runner selection between the tmux-session runner and the subprocess (Claude
CLI) runner, the one-session-per-task tmux naming convention, the prompt and
working-directory contract a dispatched agent receives, the deny-by-default
permission posture of the spawned agent, and the retry/auth handling around a
single agent invocation. It does NOT cover the workspace/worktree directory
layout (see worktree-isolation), result parsing (see result-collection), or
the task state machine (see task-model-fsm).
## Requirements
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

### Requirement: Dispatched agent receives the built prompt
The system SHALL pass each dispatched agent the prompt produced by
`whilly.core.prompts.build_task_prompt`, and MUST hand that prompt to the
agent process without shell interpretation of its contents.

#### Scenario: Prompt built before dispatch
- **WHEN** the worker dispatches a task to the runner
- **THEN** the runner SHALL receive the `build_task_prompt` output as the
  agent prompt

#### Scenario: tmux prompt passed via file
- **WHEN** the tmux runner launches an agent
- **THEN** the prompt SHALL be written to a `{task_id}_prompt.txt` file and
  supplied to the backend command as a single literal argument, not inlined as
  shell text

### Requirement: Dispatched agent runs in the prepared workspace cwd
The system SHALL run a dispatched agent in the working directory of the task's
prepared workspace, injected at dispatch time via the `workspace_runner`
closure in `whilly/cli/run.py`, and MUST defer the workspace directory layout
and lifecycle to the worktree-isolation capability rather than constructing it
in the dispatch layer.

#### Scenario: cwd injected from prepared workspace
- **WHEN** the production runner is `run_task` and a task's workspace has been
  prepared
- **THEN** the system SHALL invoke `run_task` with `cwd` set to the prepared
  workspace path

#### Scenario: Workspace preparation failure surfaces as task failure
- **WHEN** preparing the task's workspace raises before dispatch
- **THEN** the system SHALL return a failing `AgentResult` with
  `is_complete=False` rather than dispatching the agent or crashing the worker

### Requirement: No removed env-flag gates dispatch
The system SHALL NOT gate agent dispatch or per-task workspace selection on
`WHILLY_WORKTREE` or `WHILLY_USE_WORKSPACE`, which are removed no-ops retained
only for backward `.env` compatibility.

#### Scenario: WHILLY_WORKTREE has no dispatch effect
- **WHEN** `WHILLY_WORKTREE` is set in the environment
- **THEN** the system SHALL dispatch agents identically to when it is unset

#### Scenario: WHILLY_USE_WORKSPACE has no dispatch effect
- **WHEN** `WHILLY_USE_WORKSPACE` is set in the environment
- **THEN** the system SHALL dispatch agents identically to when it is unset

### Requirement: Deny-by-default permission posture
The system SHALL build the Claude CLI argv with `--output-format json` and a
deny-by-default tool denylist (`--disallowedTools Write,Edit,MultiEdit,
NotebookEdit,Bash`), and MUST only drop that denylist and re-emit
`--dangerously-skip-permissions` when `WHILLY_AGENT_ALLOW_SHELL` is enabled.

#### Scenario: Default deny posture
- **WHEN** `build_command` builds the agent argv and `WHILLY_AGENT_ALLOW_SHELL`
  is unset
- **THEN** the argv SHALL contain `--disallowedTools` with the default-deny
  tool list and SHALL NOT contain `--dangerously-skip-permissions`

#### Scenario: Shell override enabled
- **WHEN** `WHILLY_AGENT_ALLOW_SHELL` is enabled
- **THEN** the argv SHALL drop the denylist and emit
  `--dangerously-skip-permissions`

### Requirement: Retry on transient errors, fail fast on auth
The system SHALL retry a single agent invocation on transient API errors using
the backoff schedule 5/10/20/40/60 seconds, and MUST NOT retry permanent
authentication failures (`failed to authenticate` or `403 Forbidden`),
returning them immediately.

#### Scenario: Transient API error is retried
- **WHEN** an agent invocation returns a retriable error and the backoff
  schedule is not exhausted
- **THEN** the system SHALL sleep the next schedule interval and re-invoke the
  agent

#### Scenario: Auth failure returns immediately
- **WHEN** an agent invocation returns output indicating an authentication
  failure
- **THEN** the system SHALL return that result immediately without consuming
  any retry attempt

