## ADDED Requirements

### Requirement: Product workflow keeps admin and CSRF boundaries
The system SHALL require an administrator session for all product swarm routes and same-origin CSRF checks for mutations.

#### Scenario: Worker credential used on cockpit
- **WHEN** a caller presents only worker bearer authentication
- **THEN** product messages, feature state and workflow actions are inaccessible

#### Scenario: Browser attempts to supply execution metadata
- **WHEN** a specification request includes plan revision, trusted profiles or base SHAs
- **THEN** the request is rejected rather than binding caller-supplied execution metadata
