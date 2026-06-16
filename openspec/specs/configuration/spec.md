## Purpose

The configuration capability governs how Whilly resolves its runtime
configuration: the `WHILLY_`-prefixed environment-variable contract read by
`WhillyConfig.from_env`, the five-layer precedence pipeline in
`load_layered` (defaults → user TOML → repo `whilly.toml` → `.env` → shell
`WHILLY_*`, with CLI flags applied last), string→typed coercion via `_coerce`,
the secret-reference schemes (`env:`, `keyring:`, `file:`) resolved by
`WhillyConfig.resolved`, and the universal project-config surface
(`whilly/project_config/*` plus the `project-config`, `project-map`, and
`quick-setup` CLI commands). This capability covers where values come from and
how they are typed and resolved; it references the auth-security capability for
the *handling* of secret material (keyring storage, redaction, lint) rather
than duplicating it. Several legacy state fields are retained for backward
compatibility but are no-ops in v4 — this spec documents them truthfully.

## Requirements

### Requirement: WHILLY_ env-var contract and documented defaults
The system SHALL expose configuration as the `WhillyConfig` dataclass whose
fields are populated from `WHILLY_<FIELD>` environment variables, and SHALL
apply the documented defaults when a field is unset: `MODEL` =
`claude-opus-4-6[1m]`, `MAX_PARALLEL` = 3, `HEARTBEAT_INTERVAL` = 1,
`MAX_ITERATIONS` = 0, `LOG_DIR` = `whilly_logs`, `BUDGET_USD` = 0.0
(unlimited), `TIMEOUT` = 0, and `MAX_TASK_RETRIES` = 5.

#### Scenario: Defaults applied when no env vars set
- **WHEN** `WhillyConfig.from_env` is called and no `WHILLY_*` variables, TOML
  files, or `.env` are present
- **THEN** the returned config SHALL have `MODEL` = `claude-opus-4-6[1m]`,
  `MAX_PARALLEL` = 3, and `LOG_DIR` = `whilly_logs`
- **AND** `BUDGET_USD` SHALL be 0.0 to mean unlimited budget

#### Scenario: Env var overrides the default
- **WHEN** `WHILLY_MAX_PARALLEL=8` is set in the shell environment and
  `WhillyConfig.from_env` is called
- **THEN** the returned config SHALL have `MAX_PARALLEL` equal to 8 rather than
  the default 3

#### Scenario: Bare SLACK_ACCESS_TOKEN accepted without prefix
- **WHEN** `SLACK_ACCESS_TOKEN` is set without the `WHILLY_` prefix and the
  prefixed form was not resolved by the layered loader
- **THEN** `WhillyConfig.from_env` SHALL populate `SLACK_ACCESS_TOKEN` from the
  bare environment variable

### Requirement: Layered precedence ordering (last layer wins)
The system SHALL resolve configuration through `load_layered` by overlaying
sources in strict order so that a higher layer overrides a lower one:
(1) dataclass defaults, (2) user TOML at `user_config_path()`, (3) repo
`whilly.toml`, (4) `.env` loaded into the environment, (5) shell `WHILLY_*`
variables; CLI flags applied by `cli.py` after `load_layered` returns SHALL be
the effective highest layer.

#### Scenario: Repo TOML overrides user TOML
- **WHEN** the user TOML sets `MAX_PARALLEL = 2` and the repo `whilly.toml`
  sets `MAX_PARALLEL = 4`
- **THEN** `load_layered` SHALL return a config with `MAX_PARALLEL` equal to 4

#### Scenario: Shell env overrides TOML
- **WHEN** the repo `whilly.toml` sets `MODEL` to one value and the shell sets
  `WHILLY_MODEL` to a different value
- **THEN** `load_layered` SHALL return the `WHILLY_MODEL` value, because the
  shell env layer is applied after both TOML layers

#### Scenario: Shell env only overrides fields explicitly set
- **WHEN** a `WHILLY_<FIELD>` variable is absent from the environment
- **THEN** `load_layered` SHALL leave that field at the value resolved by the
  TOML/defaults layers and SHALL NOT overwrite it with the dataclass default

### Requirement: Type coercion of string values to typed fields
The system SHALL coerce string values from TOML and environment variables to
each dataclass field's declared type via `_coerce`: `int` and `float` fields
parsed numerically, and `bool` fields treated as false only for the tokens
`0`, `false`, `no`, `off`, or empty string (case-insensitive) and true
otherwise.

#### Scenario: Integer field coerced from string
- **WHEN** `WHILLY_MAX_PARALLEL=5` is read as the string `"5"`
- **THEN** the resolved config field `MAX_PARALLEL` SHALL be the integer 5

#### Scenario: Boolean falsey tokens coerced to False
- **WHEN** a boolean field such as `USE_TMUX` is supplied as `0`, `false`,
  `no`, `off`, or the empty string
- **THEN** `_coerce` SHALL resolve that field to `False`

#### Scenario: Boolean truthy token coerced to True
- **WHEN** a boolean field is supplied as `1`, `true`, or any other non-falsey
  token
- **THEN** `_coerce` SHALL resolve that field to `True`

### Requirement: Secret-reference scheme resolution at access time
The system SHALL, when `WhillyConfig.resolved` is called, replace every
string-typed field carrying a recognised secret-reference scheme with its
resolved plaintext: `env:NAME` read from `os.environ[NAME]`,
`keyring:service[/user]` read from the OS keyring, and `file:/path` read and
stripped from the file; a missing secret SHALL resolve to an empty string
rather than raising, and non-string fields SHALL pass through unchanged.

#### Scenario: env: reference resolved from environment
- **WHEN** a string field holds `env:MY_TOKEN` and `MY_TOKEN` is set in the
  environment
- **THEN** `resolved()` SHALL return a config whose field equals the value of
  `MY_TOKEN`

#### Scenario: file: reference resolved and stripped
- **WHEN** a string field holds `file:/path/to/secret` pointing at a readable
  file
- **THEN** `resolved()` SHALL return the file contents with surrounding
  whitespace stripped

#### Scenario: Missing secret resolves to empty string
- **WHEN** a string field holds a `keyring:` or `env:` reference whose target
  does not exist
- **THEN** `resolved()` SHALL set that field to an empty string and SHALL NOT
  raise

### Requirement: Project-config surface for universal plans
The system SHALL provide a project-config surface that loads and validates
universal project configuration files (`load_project_config`,
`project_config_from_dict`), builds plans from project-type presets
(`preset_pipeline`), resolves Jira project keys to repositories
(`project_config.resolver`), and exposes the `whilly project-config`,
`whilly project-map`, and `whilly quick-setup` CLI commands; project configs
containing plaintext secret-like values SHALL be rejected in favour of
`env:`, `keyring:`, or `file:` references.

#### Scenario: Unsupported project_type rejected
- **WHEN** `load_project_config` parses a config whose `project_type` is not in
  the supported set (`python_backend`, `etl_pipeline`, `documentation`,
  `graphql_api`, `generic`, or an alias)
- **THEN** the loader SHALL raise `ProjectConfigError`

#### Scenario: Plaintext secret in project config blocked
- **WHEN** `project_config_from_dict` encounters a plaintext value that the
  secret lint matches as secret-like
- **THEN** the loader SHALL raise `ProjectConfigError` directing the author to
  use an `env:`, `keyring:`, or `file:` reference

#### Scenario: Missing project type falls back to preset pipeline
- **WHEN** a valid project config declares no explicit pipeline
- **THEN** the loader SHALL populate the pipeline from `preset_pipeline` for
  the declared project type

### Requirement: Legacy state fields accepted as no-ops
The system SHALL accept the legacy state/workspace toggles `WHILLY_WORKTREE`,
`WHILLY_USE_WORKSPACE`, `WHILLY_USE_TMUX`, `WHILLY_STATE_FILE`, and
`WHILLY_ORCHESTRATOR` so legacy `.env` files and shell exports keep parsing
without error, but setting them SHALL have no behavioural effect on the v4 CLI
— they are retained as no-ops, not live behaviour.

#### Scenario: WHILLY_WORKTREE parsed but inert
- **WHEN** `WHILLY_WORKTREE=1` is set and `WhillyConfig.from_env` is called
- **THEN** the config SHALL parse the field without error
- **AND** the v4 CLI SHALL NOT change its execution behaviour as a result of
  the field being set

#### Scenario: WHILLY_STATE_FILE has no live effect
- **WHEN** `WHILLY_STATE_FILE` is set to a custom path
- **THEN** the config SHALL accept the value
- **AND** the v4 worker-claim execution path SHALL NOT read or write state at
  that path because the field is a no-op
