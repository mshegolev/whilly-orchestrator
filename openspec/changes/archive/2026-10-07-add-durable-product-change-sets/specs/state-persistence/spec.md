## ADDED Requirements

### Requirement: Change-set transitions and evidence are durable and atomic

The system SHALL persist product/repository changes with optimistic versions and
append-only transition events in the same transaction; each event SHALL have a
unique idempotency key and concrete command/job/SHA evidence or named absence.
Immutable definitions and audit/receipt history SHALL reject destructive mutation.

#### Scenario: Concurrent transition at one expected version

- **WHEN** two callers attempt to advance the same version
- **THEN** exactly one state/event update succeeds and the other reports a version conflict

#### Scenario: Event append fails

- **WHEN** a uniqueness or evidence constraint rejects the event
- **THEN** the accompanying state change is rolled back atomically

### Requirement: External-effect receipts cannot be replaced by retries

The system SHALL retain immutable observed external-effect receipts under globally
unique keys, return the original receipt for a same-scope/same-request retry and
reject key reuse across change-set, repository, operation or request digest.

#### Scenario: Result replay after process restart

- **WHEN** a caller retries an observed effect key with a different response body
  but the same scope and request digest
- **THEN** the stored original receipt is returned without replacement

#### Scenario: Reuse attempts to cross authority scope

- **WHEN** a key is reused for another repository, change-set, operation or request
- **THEN** the conflict is named and the original audit/receipt remains unchanged
