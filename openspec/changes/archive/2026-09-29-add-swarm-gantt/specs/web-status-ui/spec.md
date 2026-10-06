## ADDED Requirements

### Requirement: Swarm execution Gantt
The swarm UI SHALL offer a revision-scoped Gantt view grouped by project and role, showing actual attempt intervals, statuses, retries and named dependencies without inventing dates for unstarted tasks.

#### Scenario: Inspect executed and proposed work
- **WHEN** an administrator selects a session and opens Gantt
- **THEN** recorded attempts appear on a time scale and running attempts extend to the current time
- **AND** unstarted tasks and missing timestamps are explicitly labeled without fabricated bars
- **AND** choosing another revision or session removes unrelated execution data

#### Scenario: Refresh and safe rendering
- **WHEN** task data changes while the Gantt view is open
- **THEN** the view refreshes without starting model calls or tasks
- **AND** task labels are rendered as text, including on mobile layouts
