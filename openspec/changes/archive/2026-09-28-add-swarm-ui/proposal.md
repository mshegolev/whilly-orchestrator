# Proposal: admin swarm conversation UI

## Why

Operators need one persistent conversation for cross-project planning and a
visible approval boundary before local agents execute a selected revision.

## What Changes

Add a bounded, admin-only browser surface for discussing swarm plans, previewing
validated proposed revisions, and explicitly running or stopping selected work.
The router is integrated by the main runtime and receives the trusted registry
path from server configuration.
