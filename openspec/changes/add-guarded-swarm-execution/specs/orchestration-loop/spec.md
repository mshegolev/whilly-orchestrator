## ADDED Requirements

### Requirement: Candidate execution uses the guarded host capability
The system SHALL route every candidate-consuming model, verification, Git and
BMAD host-script launch through an explicit execution policy and a
host-provisioned toolchain identified by `toolchain_id`.

#### Scenario: Capability is unavailable
- **WHEN** isolation, named provider authorization or toolchain provisioning is unavailable
- **THEN** the launch fails with a named blocker before a legacy process, Git or transport is constructed

#### Scenario: Explicit fixture execution
- **WHEN** a test supplies synthetic provider values, executable roots and an isolated HOME/TMPDIR
- **THEN** the executor uses only those values and redacts synthetic secrets from durable logs

### Requirement: Candidate evidence remains approval-bound and structurally verifiable
Candidate acceptance MUST preserve the boolean `passed` wrapper while storing
structured gate evidence, protected-change results, baseline/candidate
provenance and unchanged head/tree checks after verification and review.

#### Scenario: Gate counters are present
- **WHEN** a parser reports collected, passed and skipped counts
- **THEN** the Store receives `passed=True` only for an accepted outcome and retains the integer counters under nested evidence

#### Scenario: Verification or review changes the candidate
- **WHEN** candidate head, tree or protected files differ after the gate or review
- **THEN** acceptance fails with a named candidate/policy blocker

### Requirement: Unsupported publication is fail closed
The product workflow MUST reject publication with `publication_unavailable`
before reading legacy publication settings or constructing candidate Git or
network transport objects, while preserving the local result packet.

#### Scenario: Legacy publication settings exist
- **WHEN** guarded publication is not provisioned
- **THEN** publication is blocked before any legacy Git or transport side effect
