## ADDED Requirements

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
