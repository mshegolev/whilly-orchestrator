## ADDED Requirements

### Requirement: Product change-set definitions retain complete registry scope

The system SHALL create an immutable product change-set definition with a stable
identity, goal, acceptance criteria, canonical registry digest, every registered
repository baseline and an acyclic dependency graph; execution SHALL require a
recorded approval digest and repository progress SHALL remain within the current
product phase.

#### Scenario: Registered repository omitted or dependency unknown

- **WHEN** baselines omit a registered repository or the graph contains an unknown
  dependency or cycle
- **THEN** creation is rejected before any state or external effect is persisted

#### Scenario: Non-impacted repository identified

- **WHEN** impact evidence excludes a planned repository
- **THEN** its explicit NOT_IMPACTED record is retained instead of silently dropping
  it from the immutable definition

### Requirement: Product change-set outcomes preserve acceptance and compensation boundaries

The system SHALL allow only named product/repository state transitions, preserve
the recorded resume boundary of blocked decisions, prevent generic failure after
an observed merge, and permit DONE only from stage acceptance with passed stage
evidence and artifact-ready mandatory parts; rollback completion SHALL require
observed reverts without unresolved merged parts.

#### Scenario: Local success offered as product completion

- **WHEN** a caller offers local verification, unavailable acceptance or unfinished
  required artifacts as completion evidence
- **THEN** DONE is rejected and prior state/evidence remains unchanged

#### Scenario: Mandatory stage probe fails after merge

- **WHEN** a failed stage observation initiates compensation
- **THEN** ROLLING_BACK can retain that failed observation without claiming success
- **AND** incomplete compensation remains ROLLBACK_FAILED rather than FAILED or DONE
