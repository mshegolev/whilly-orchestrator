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

### Requirement: Anonymizer map loaded from environment
The system SHALL load the anonymizer's redaction map from the
`WHILLY_ANONYMIZER_MAP` environment variable (a JSON object string) at
`Anonymizer` construction time via `_load_company_mappings()`, and MUST
default to an empty map — performing no redaction — when the variable is
unset, empty, or contains invalid JSON.  No company name SHALL be hardcoded
in source; all real company names MUST be supplied through local,
gitignored environment configuration.

#### Scenario: Map loaded when env var is set
- **WHEN** `WHILLY_ANONYMIZER_MAP` is set to a valid JSON object string
- **THEN** `Anonymizer().company_mappings` SHALL equal the parsed dict
- **AND** anonymization SHALL replace occurrences of keys with their values

#### Scenario: Empty map when env var is absent
- **WHEN** `WHILLY_ANONYMIZER_MAP` is unset or empty
- **THEN** `Anonymizer().company_mappings` SHALL be an empty dict
- **AND** `anonymize_text` SHALL return the input unchanged with an empty mapping

#### Scenario: Empty map on invalid JSON
- **WHEN** `WHILLY_ANONYMIZER_MAP` contains a string that is not valid JSON
- **THEN** the system SHALL log a WARNING and `Anonymizer().company_mappings` SHALL be an empty dict

### Requirement: Explicit swarm child environment boundary
The system SHALL expose `build_swarm_environment` that constructs a child environment from explicit host inputs without copying ambient environment entries.

#### Scenario: Host and identity allowlists are enforced
- **WHEN** the helper receives base, host path, home, temporary, and identity inputs
- **THEN** it SHALL use the explicit `PATH`, `HOME`, and `TMPDIR` values
- **AND** it SHALL copy only `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, and the four named `WHILLY_SWARM_*` identity keys

#### Scenario: Provider credential is scoped
- **WHEN** an online phase selects Claude or Codex
- **THEN** the helper SHALL include only that provider's host-supplied credential
- **AND** it SHALL reject unknown provider keys

#### Scenario: Offline phases are credential-free
- **WHEN** phase is `verify`, `git`, or `host_script`
- **THEN** the returned environment SHALL omit provider credentials

#### Scenario: Invalid contract keys are rejected
- **WHEN** the phase, provider, or identity key is unknown
- **THEN** the helper SHALL raise `ValueError` without exposing input values

### Requirement: Fail-closed local execution
The swarm execution adapter MUST run supported local verification commands inside a host-enforced macOS sandbox with explicit policy roots and no direct-host fallback.

#### Scenario: Unsupported isolation
- **WHEN** macOS `sandbox-exec` is unavailable or the host is not macOS
- **THEN** execution returns the named `execution_isolation_unavailable` blocker
- **AND** the requested argv is not executed directly

### Requirement: Bounded sandbox transport
The swarm execution adapter MUST bound stdout and stderr independently, terminate the complete process group on timeout or cancellation, and return a named result for output overflow.

#### Scenario: Child output exceeds the policy cap
- **WHEN** a sandboxed child exceeds the configured output limit
- **THEN** the adapter terminates the process group
- **AND** returns `output_limit_exceeded` without waiting indefinitely for descendants

### Requirement: Real isolation evidence
The local acceptance probe MUST verify allowed file access and denial of synthetic outside-file, descendant-write, and localhost-network attempts using successful unsandboxed positive controls.

#### Scenario: Offline verification probe
- **WHEN** the disposable fixture contains readable and writable outside sentinels and a reachable localhost listener
- **THEN** unsandboxed controls succeed
- **AND** the sandboxed policy allows the approved read while denying outside reads, writes, descendant writes, and network access

### Requirement: Explicit execution roots
The sandbox profile MUST derive every read, write, executable and protected-write grant from canonicalized roots explicitly present in the execution policy.

#### Scenario: Relative executable resolution
- **WHEN** a caller supplies a relative executable and an explicit child `PATH`
- **THEN** the adapter resolves it against that `PATH` and `cwd`
- **AND** rejects execution when `PATH` is absent or the resolved executable is outside policy read roots

### Requirement: Fail-closed probe evidence
The local probe MUST use only fresh disposable fixture targets and MUST report `execution_isolation_unavailable` when any control or denial probe is not a completed concrete result.

#### Scenario: Backend failure during a negative probe
- **WHEN** a denial attempt has no exit code, a named spawn failure, a timeout, or cancellation
- **THEN** the probe reports unavailable
- **AND** it does not count the attempt as isolation denial

### Requirement: Independent candidate repositories

Candidate-consuming swarm Git operations SHALL materialize an ordinary or bare
source at the approved base commit into a repository with independent Git
objects, no imported publication remote, and no writes to the source checkout,
refs, or worktree metadata.

#### Scenario: Dirty source checkout

- **WHEN** the source checkout has ordinary dirty tracked and untracked files
- **THEN** materialization preserves those files in the source
- **AND** the candidate starts at the approved base SHA without importing the
  dirty files

### Requirement: Guarded Git and explicit hooks

Candidate-consuming Git and coordinator commits SHALL use the explicit offline
Git execution policy and environment. A required executable hook SHALL have
pinned bytes, source-relative dependency digests, `git` phase, and expected exit
zero in the approved hook policy before it can run.

#### Scenario: Hook approval or failure

- **WHEN** a required hook has no valid approval, fails, or writes outside the
  candidate policy roots
- **THEN** the operation blocks with a named policy or Git failure
- **AND** it does not silently skip the hook or publish the candidate

### Requirement: Candidate identity

The candidate identity SHALL expose its exact head SHA and deterministic tracked
tree digest through the guarded Git seam for later evidence binding.

#### Scenario: Exact candidate identity

- **WHEN** the coordinator captures a candidate identity
- **THEN** it receives the candidate HEAD SHA and tracked-tree digest from
  guarded Git reads

### Requirement: Host-owned project verification
The swarm SHALL require every project entering guarded execution to have nonempty host-owned test, lint, and architecture argv policies with an explicit toolchain identifier.

#### Scenario: Task commands cannot replace absent policy
- **WHEN** a plan supplies verification commands for a project without a mandatory project policy
- **THEN** discussion and plan proposal remain available
- **AND** guarded application is rejected before worker admission

#### Scenario: Policy-backed plan omits duplicate commands
- **WHEN** a trusted project policy has all three categories
- **THEN** a plan may omit task-authored verification commands
- **AND** execution uses the canonical project policy

### Requirement: Approval binding includes verification identity
The swarm SHALL bind each guarded revision to its resolved base SHA, verification policy digest, and pinned hook-policy digest.

#### Scenario: Binding is missing or stale
- **WHEN** an applied snapshot or approved product specification lacks the binding or any bound identity changes
- **THEN** guarded admission is rejected with a named blocker
- **AND** the system does not invent a legacy default

### Requirement: Trusted gate evidence is fail-closed
The host SHALL accept only enrolled pytest JUnit, Ruff JSON, or architecture JSON parser schemas and SHALL reject malformed, missing, oversized, unsupported, zero-discovery, or required-skipped evidence.

#### Scenario: Exit zero is insufficient
- **WHEN** a JUnit report contains no testcase, contains a required skip, or architecture JSON does not prove evaluated rules
- **THEN** the gate is not passed
- **AND** worker-supplied report text is not treated as host proof

