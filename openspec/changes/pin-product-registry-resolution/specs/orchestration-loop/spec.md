## ADDED Requirements

### Requirement: Canonical product registry identity and storage
The system SHALL normalize explicit default remote ports and reject escaped hostname syntax before repository identity checks, and SHALL bind resolved declared state_dir into canonical registry and approval digests without replacing it with a runtime environment override.

#### Scenario: Equivalent default port or escaped hostname
- **WHEN** a declared HTTPS or SSH remote uses its explicit default port
- **THEN** it has the same canonical remote and identity as the implicit default
- **AND** an escaped hostname is rejected before it can hide a duplicate

#### Scenario: State storage changes with unchanged JSON
- **WHEN** the same JSON resolves declared state storage differently because of its source directory or a symlink retarget
- **THEN** registry and approval bindings change and snapshot revalidation blocks continuation
- **AND** the resolved directory survives serialization roundtrip

### Requirement: Registry shell command option boundaries
The system SHALL reject inline shell command options in fast, full and compensation argv, including combined POSIX flags containing c and case-insensitive PowerShell command or encoded-command forms, while permitting trusted file-script invocation.

#### Scenario: Inline shell option spelling varies
- **WHEN** a registry check uses a combined command flag or a case variant of a PowerShell command option
- **THEN** policy parsing fails before any command can be launched

#### Scenario: Script file invocation
- **WHEN** a registry check invokes a script file without an inline-command option
- **THEN** the inline-shell boundary permits the argv subject to the other policy checks
