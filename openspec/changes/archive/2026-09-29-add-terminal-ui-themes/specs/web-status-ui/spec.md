## ADDED Requirements

### Requirement: Persistent terminal UI themes
The UI SHALL support light, dark and system preferences while retaining monospace typography and the terminal-style line-based layout.

#### Scenario: Explicit preference persists across navigation
- **WHEN** a user selects light or dark on the dashboard, swarm, product cockpit or sign-in page
- **THEN** that palette is applied immediately and persisted locally for subsequent pages and reloads before content paint

#### Scenario: System preference follows device changes
- **WHEN** no valid explicit preference exists or the user selects system
- **THEN** the palette follows the device color scheme, including changes while the page is open

#### Scenario: Storage is unavailable
- **WHEN** browser storage cannot be read or written
- **THEN** theme controls remain usable for the current page without breaking other UI actions

#### Scenario: Accessible terminal rendering
- **WHEN** either palette is active
- **THEN** text, statuses, controls and the Gantt chart remain legible on desktop and mobile, with keyboard-accessible theme selection
