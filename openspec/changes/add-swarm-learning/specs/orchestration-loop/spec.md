## ADDED Requirements

### Requirement: Shared knowledge retains scope and provenance
The system SHALL filter knowledge by authorized product, project and classification before composing bounded agent context, and SHALL retain explicit freshness and revision evidence.

#### Scenario: Source becomes unavailable
- **WHEN** a source cannot be checked
- **THEN** its claim is not represented as freshly verified

#### Scenario: Unrelated principal requests knowledge
- **WHEN** the principal lacks scope or classification access
- **THEN** knowledge content and hidden reference identifiers are not disclosed

### Requirement: Memory cannot grant execution authority
The system SHALL treat knowledge as untrusted evidence and SHALL revalidate approval-bound material context before execution.

#### Scenario: Bound knowledge is redacted or superseded
- **WHEN** an approved task references a no-longer-valid knowledge revision
- **THEN** execution is blocked pending fresh planning and approval

### Requirement: Durable scoped collaboration messages
The system SHALL persist bounded host-authenticated messages and delivery receipts atomically, deduplicate retries before delivery side effects, and authorize product/project/recipient scope independently of message content.

#### Scenario: Retry after delivery without acknowledgment
- **WHEN** delivery is retried after a crash before acknowledgment
- **THEN** only one logical inbox entry exists and receipt state survives restart
- **AND** acknowledgment never completes or dispatches a task

#### Scenario: Missing policy or forged scope
- **WHEN** a sender lacks explicit delivery policy, attempts a foreign product or exceeds hop/payload bounds
- **THEN** sending fails with a named blocker before persistence

#### Scenario: Offline and expired recipients
- **WHEN** an authorized recipient is offline
- **THEN** the message remains persisted until delivery or expiry and its state is visible
- **AND** expired payloads cannot execute or grant authority

### Requirement: Cross-project proposals preserve approval authority
The system SHALL persist evidence-backed cross-project proposals with product-scoped deduplication and immutable lifecycle events. Submission or acceptance for planning SHALL NOT execute work or confer approval.

#### Scenario: Specialist discovers neighboring work
- **WHEN** an authorized specialist submits a proposal for a registered project
- **THEN** its evidence, origin, target, acceptance criteria and dependencies are retained as proposed work
- **AND** retries yield one logical proposal without dispatch

#### Scenario: Accept only for planning
- **WHEN** an authorized owner accepts an eligible proposal at the current feature revision
- **THEN** proposal history and a new specification revision are committed atomically
- **AND** previous approval digests are cleared without dispatching tasks or calling a model

#### Scenario: Contract verification is unavailable
- **WHEN** a proposal claims a contract change without host-bound producer and consumer verification
- **THEN** admission remains blocked and an editable specification cannot substitute for that proof

#### Scenario: Withdraw already planned work
- **WHEN** an operator tries to reject a proposal already accepted into a plan
- **THEN** the proposal-only rejection is blocked and the originating feature workflow is required

#### Scenario: Scope changes after planning
- **WHEN** accepted proposed work changes a canonical plan
- **THEN** the existing revision-bound approval is invalidated and fresh approval is required
- **AND** ownership, dependency cycles, protected paths, budget and consumer contract checks remain mandatory
