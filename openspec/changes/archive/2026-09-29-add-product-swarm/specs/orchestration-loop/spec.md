## ADDED Requirements

### Requirement: BMAD specification artifacts remain host controlled
The system SHALL keep planners read-only and persist validated BMAD specification artifacts in private revision workspaces through the host executor.

#### Scenario: Script-dependent skill without host executor
- **WHEN** a configured skill requires helper scripts but host execution is unavailable
- **THEN** planning stops with a named setup blocker before invoking a model

#### Scenario: Artifact escapes or failed preservation
- **WHEN** a returned artifact escapes its workspace or reports failed preservation
- **THEN** the specification cannot become approvable

### Requirement: Product features bind execution approval
The system SHALL bind approval to a server-computed digest of the feature specification, plan revision, registry hash, base SHAs, execution profiles and budget.

#### Scenario: Approved metadata changes
- **WHEN** specification, budget, registry or base commit changes
- **THEN** execution is blocked pending renewed planning and approval

### Requirement: Model roles and global capacity are explicit
The system SHALL use configured strong profiles for planning and cheap profiles for implementation and independent review, with at most five admitted model calls globally.

#### Scenario: Missing profile or exhausted capacity
- **WHEN** a required profile is missing or five calls are active
- **THEN** the call is blocked with a named reason and no fallback model is invoked

### Requirement: Publication requires trusted evidence
The system SHALL restrict publication to explicitly allowlisted destinations and SHALL NOT merge or deploy changes.

#### Scenario: Pipeline evidence absent
- **WHEN** exact-SHA successful CI evidence is absent
- **THEN** publication cannot be reported ready
