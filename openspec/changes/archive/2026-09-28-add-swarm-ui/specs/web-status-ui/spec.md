## ADDED Requirements

### Requirement: Safe swarm conversation rendering
The web status UI SHALL return original message JSON and render untrusted conversation and revision content as text.

#### Scenario: safe conversation rendering
- **WHEN** history or a revision contains `<`, `>`, or `&`
- **THEN** the API returns the original JSON string and the browser inserts it with `textContent`

### Requirement: Explicit swarm execution
The web status UI SHALL preview a proposed plan and execute it only after an explicit revision selection.

#### Scenario: explicit execution
- **WHEN** an admin selects a proposed revision and clicks Run Selected
- **THEN** the validated plan is previewable before the click and only that explicit request applies and executes it

### Requirement: Bounded operation control
The web status UI SHALL cap active in-process jobs and cancel any active job on stop.

#### Scenario: bounded operation control
- **WHEN** the global active-job limit is reached, or an admin stops a chat/run
- **THEN** new work is rejected with a bounded error, and any active async task is cancelled; coordinator stop is requested when one exists

### Requirement: Stale browser state protection
The web status UI SHALL prevent stale polling responses from overwriting the selected session.

#### Scenario: stale browser state
- **WHEN** the admin switches or creates sessions while a poll response is outstanding
- **THEN** the old poll is cleared, selection is reset, and the stale response cannot overwrite the newly selected session
