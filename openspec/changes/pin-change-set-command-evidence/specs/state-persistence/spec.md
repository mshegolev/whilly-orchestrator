## MODIFIED Requirements

### Requirement: Change-set transitions and evidence are durable and atomic

The system SHALL persist product/repository changes with optimistic versions and
append-only transition events in the same transaction; each event SHALL have a
unique idempotency key and concrete command/job/SHA evidence or named absence.
Every supplied command SHALL be an array whose elements are nonempty strings,
independently of any job identity or named-absence alternative. Immutable definitions
and audit/receipt history SHALL reject destructive mutation.

#### Scenario: Concurrent transition at one expected version

- **WHEN** two callers attempt to advance the same version
- **THEN** exactly one state/event update succeeds and the other reports a version conflict

#### Scenario: Event append fails

- **WHEN** a uniqueness or evidence constraint rejects the event
- **THEN** the accompanying state change is rolled back atomically

#### Scenario: Command arguments cannot be rehydrated

- **WHEN** an evidence command contains null, boolean, empty-string or other non-string elements
- **THEN** native persistence rejects it before modifying state or immutable audit
- **AND** a valid job identity or named absence cannot bypass that validation
