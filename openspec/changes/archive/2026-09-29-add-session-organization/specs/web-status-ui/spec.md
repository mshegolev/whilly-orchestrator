## ADDED Requirements

### Requirement: Reversible swarm session organization
The UI SHALL hide explicitly marked test sessions and archived sessions by default, offer independent filters to reveal them, and allow administrators to change those flags without deleting history or starting or stopping execution.

#### Scenario: Archive and restore
- **WHEN** an administrator archives a session and enables the archived filter
- **THEN** the session remains accessible with its history and can be restored

#### Scenario: Explicit test classification
- **WHEN** a session is marked as test data
- **THEN** it is hidden unless the test filter is enabled
- **AND** titles alone do not determine test classification
