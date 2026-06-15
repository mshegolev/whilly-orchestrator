## Purpose

The cli-surface capability governs the `whilly` console entry point declared in
`pyproject.toml` (`[project.scripts] whilly = "whilly.cli:main"`) and the two
module-mode shims (`python -m whilly`, `python -m whilly.cli`). It covers how the
single `main(argv)` dispatcher in `whilly/cli/__init__.py` routes the first
positional token to a v4 sub-CLI, the help and version fast paths, the v3
backwards-compatibility flag shim (`_apply_legacy_shim`), and — most importantly
for headless callers — the real per-command process exit-code contract grounded
in the `EXIT_*` constants in `whilly/cli/plan.py`, `whilly/cli/run.py`, and
`whilly/workspaces.py`. This capability deliberately documents observed v4
behavior, not the legacy v3 "0=ok / 1=some failed / 2=budget / 3=timeout"
narrative.

## Requirements

### Requirement: Real v4 exit-code contract
The `whilly` CLI MUST surface process exit codes drawn from the real v4 `EXIT_*`
constants — `EXIT_OK = 0`, `EXIT_VALIDATION_ERROR = 1`, `EXIT_ENVIRONMENT_ERROR = 2`,
and `WORKSPACE_FAILED_EXIT_CODE = -4` — and SHALL NOT use the legacy v3 "budget
exceeded = 2 / timeout = 3" wording. The set of codes a command can return is
command-specific (see the per-command scenarios below).

#### Scenario: Success returns EXIT_OK
- **WHEN** any v4 subcommand completes its work successfully (e.g. `whilly plan import`
  commits a plan, `whilly plan export` prints JSON, or `whilly run` reaches its
  termination condition)
- **THEN** the process SHALL exit with `EXIT_OK` (0)

#### Scenario: plan validation failure returns EXIT_VALIDATION_ERROR
- **WHEN** `whilly plan import` (or `whilly plan apply`) parses a plan whose JSON
  shape is malformed, is missing a required field, or contains a dependency cycle
- **THEN** the process SHALL exit with `EXIT_VALIDATION_ERROR` (1)
- **AND** a diagnostic naming the offending input (including a `Cycle detected: A → B → A`
  chain for cycles) SHALL be written to stderr

#### Scenario: environment failure returns EXIT_ENVIRONMENT_ERROR
- **WHEN** a database-backed subcommand (`plan import`/`export`/`show`/`create`,
  or `run`) runs with `WHILLY_DATABASE_URL` unset, the named plan absent from the
  database, or the plan file not found
- **THEN** the process SHALL exit with `EXIT_ENVIRONMENT_ERROR` (2)
- **AND** a diagnostic explaining the missing environment precondition SHALL be
  written to stderr

#### Scenario: workspace preparation failure surfaces WORKSPACE_FAILED_EXIT_CODE
- **WHEN** `whilly run` prepares a per-task repository-target workspace and the
  preparation raises
- **THEN** the task's `AgentResult.exit_code` SHALL be `WORKSPACE_FAILED_EXIT_CODE`
  (-4) as defined in `whilly/workspaces.py`
- **AND** a `workspace.prepare_failed` event SHALL be recorded for the task

### Requirement: run command has no validation-error path
The `whilly run` subcommand SHALL expose only `EXIT_OK` (0) and
`EXIT_ENVIRONMENT_ERROR` (2) as its own returned codes and SHALL NOT return
`EXIT_VALIDATION_ERROR` (1), because argparse raises `SystemExit` on malformed
arguments before the handler runs.

#### Scenario: bad run arguments exit via argparse before the handler
- **WHEN** `whilly run` is invoked with malformed or missing required arguments
  (e.g. no `--plan`)
- **THEN** argparse SHALL emit its usage error and `SystemExit` (conventional code 2)
  before `run_run_command` returns any `EXIT_*` value

#### Scenario: run reaches its termination condition
- **WHEN** `whilly run` loads the plan and the worker loop returns normally
  (max iterations reached, stop signalled, or the plan is exhausted)
- **THEN** the process SHALL exit with `EXIT_OK` (0)

### Requirement: Subcommand dispatch table
The `main(argv)` dispatcher MUST route the first positional token to its matching
v4 sub-CLI handler — including `plan`, `run`, `dashboard`, `server`, `init`,
`admin`, `logs`, `forge`, `jira`, `gitlab`, `qa-release`, `project-config`,
`project-map`, `github-projects`, `compliance`, `tui`, `pr-feedback`, `rollback`,
`update`, `feedback`, `quick-setup`, `scheduler`, `skill`, and `worker` — and
SHALL import each handler lazily so that non-database invocations do not pull in
asyncpg or the agent stack.

#### Scenario: known command routes to its handler
- **WHEN** `main` is called with a recognized command token as the first argument
  (e.g. `plan`, `run`, `logs`, `tui`)
- **THEN** `main` SHALL import that command's handler lazily and return the handler's
  exit code

#### Scenario: worker sub-dispatch routes register/connect/launch/etc.
- **WHEN** `main` is called with `worker` followed by `register`, `connect`,
  `launch`, `bootstrap`, `list`, or `remove`
- **THEN** `main` SHALL dispatch to the corresponding worker handler
- **AND** a bare `worker` (or any other tail) SHALL fall through to the main
  worker-loop entry point

### Requirement: No-args and help print HELP and return 0
The CLI MUST print the v4 help text via `_print_help` and return 0 when invoked
with no arguments or with `-h`/`--help`, and SHALL NOT launch an interactive menu.

#### Scenario: no arguments prints help
- **WHEN** `main` is called with an empty argument list
- **THEN** the v4 help block SHALL be written to stdout via `_print_help`
- **AND** the process SHALL return 0

#### Scenario: -h/--help prints help
- **WHEN** `main` is called with `-h` or `--help` as the first argument
- **THEN** the v4 help block SHALL be written to stdout
- **AND** the process SHALL return 0

### Requirement: Version fast path
The CLI MUST print `whilly <__version__>` (reading `whilly.__version__`) to stdout
and return 0 when invoked with `-V` or `--version`.

#### Scenario: -V/--version prints the package version
- **WHEN** `main` is called with `-V` or `--version` as the first argument
- **THEN** the line `whilly <version>` (where `<version>` is `whilly.__version__`)
  SHALL be written to stdout
- **AND** the process SHALL return 0

### Requirement: Unknown command returns 2
The CLI MUST write a diagnostic to stderr, print the help text to stderr, and
return 2 when the first positional token is not a recognized command and is not a
handled legacy flag form.

#### Scenario: unrecognized command is rejected
- **WHEN** `main` is called with a first token that is neither a known subcommand,
  a help/version flag, nor a legacy flag handled by the shim
- **THEN** `whilly: unknown command '<token>'` SHALL be written to stderr
- **AND** the help text SHALL be written to stderr
- **AND** the process SHALL return 2

### Requirement: v3 legacy flag shim rewrites top-level flags
The CLI MUST run `_apply_legacy_shim` before help/version handling and the
unknown-command path, rewriting recognized v3 top-level flags into the equivalent
v4 subcommand invocation. `--tasks PATH` SHALL become `run --plan PATH`;
`--init "desc"` SHALL become `init "desc"` (dropping the v3-only `--plan`/`--go`
modifiers); `--prd-wizard [SLUG]` SHALL become `init --interactive [...]`;
`--from-jira KEY [--go]` SHALL become `jira import KEY [--run]`; and
`--reset PLAN` SHALL become `plan reset PLAN --keep-tasks --yes`.

#### Scenario: --tasks rewrites to run --plan
- **WHEN** `main` is called with `--tasks <path>`
- **THEN** the shim SHALL rewrite the argv to `run --plan <path> ...` before dispatch
- **AND** a missing path argument SHALL write a diagnostic to stderr and return 2

#### Scenario: --reset rewrites to plan reset --keep-tasks --yes
- **WHEN** `main` is called with `--reset <plan_id>`
- **THEN** the shim SHALL rewrite the argv to `plan reset <plan_id> --keep-tasks --yes`

#### Scenario: --resume and --all are no-ops returning 0
- **WHEN** `main` is called with `--resume` or `--all`
- **THEN** the shim SHALL write a one-line breadcrumb to stderr and return 0
  without dispatching a subcommand

### Requirement: Legacy modifier flags are consumed without breaking dispatch
The CLI MUST treat `--workspace`, `--worktree`, `--no-workspace`, and
`--no-worktree` as silently-consumed no-ops, and MUST consume `--headless` by
setting `WHILLY_HEADLESS=1` in the environment and stripping it from argv before
dispatch. The `--headless` flag is retained for v3 backwards compatibility; v4
subcommands SHALL determine headless/interactive mode from their own flags and
TTY detection rather than from the `WHILLY_HEADLESS` variable.

#### Scenario: --headless exports WHILLY_HEADLESS and is stripped
- **WHEN** `main` is called with `--headless` anywhere in argv
- **THEN** the shim SHALL set `os.environ["WHILLY_HEADLESS"] = "1"`
- **AND** the `--headless` token SHALL be removed from argv before dispatch

#### Scenario: workspace/worktree toggles are stripped before dispatch
- **WHEN** `main` is called with `--workspace`, `--worktree`, `--no-workspace`, or
  `--no-worktree` mixed into the arguments
- **THEN** the shim SHALL strip those tokens from argv so v4 dispatch never sees
  them as unrecognized arguments

#### Scenario: only modifiers present falls back to help
- **WHEN** the only legacy tokens are modifiers (e.g. `--headless --no-workspace`)
  with no legacy verb in head position
- **THEN** the shim SHALL leave an empty argv
- **AND** `main` SHALL print help and return 0

### Requirement: Module-mode entry points delegate to main
The `python -m whilly` and `python -m whilly.cli` entry shims MUST delegate to
`whilly.cli.main` and exit with its returned code.

#### Scenario: python -m whilly runs the dispatcher
- **WHEN** the interpreter executes `whilly/__main__.py` or `whilly/cli/__main__.py`
- **THEN** it SHALL call `whilly.cli.main()` and pass its return value to `sys.exit`
