## ADDED Requirements

### Requirement: Dashboard swarm navigation
The dashboard SHALL display a link labeled Swarm to `/swarm` in its authenticated header when the swarm route is enabled, without changing destination authorization.

#### Scenario: Open the swarm conversation
- **WHEN** a signed-in user opens a swarm-enabled dashboard
- **THEN** the header includes a visible Swarm navigation link
- **AND** following it opens the existing swarm page subject to existing access checks

#### Scenario: Swarm disabled or shared view
- **WHEN** the swarm route is absent or the dashboard is an anonymous shared view
- **THEN** the Swarm navigation link is omitted
