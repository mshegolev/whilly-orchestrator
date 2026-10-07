## ADDED Requirements

### Requirement: Collaboration CLI cannot bypass coordinator scope
The collaboration CLI SHALL exchange only bounded send/ack data through a coordinator-created local mailbox and SHALL NOT fall back to direct database access.

#### Scenario: No coordinator mailbox
- **WHEN** a worker invokes collaborate without an assigned mailbox
- **THEN** the command reports a setup blocker without network or database access

#### Scenario: Forged message authority
- **WHEN** a request includes a sender or product override or an execution operation
- **THEN** the coordinator rejects it without dispatching a task
