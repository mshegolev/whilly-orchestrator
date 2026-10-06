## ADDED Requirements

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
