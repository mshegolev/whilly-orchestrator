# Swarm execution Gantt

## Why
Operators need to see concurrent execution, retries and dependencies without inventing future dates.

## What Changes
- Add a Gantt tab to the existing swarm conversation with revision selection, project/role grouping and automatic refresh.
- Display actual attempt intervals and explicit untimed proposed/queued tasks.

## Impact
- web-status-ui and additive timing/identity fields in the existing swarm report;
  no new endpoint, migrations, model calls or scheduling behavior.
