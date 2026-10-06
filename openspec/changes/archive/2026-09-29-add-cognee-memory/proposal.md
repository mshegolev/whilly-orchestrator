# Proposal: Safe Cognee-backed learning-memory context

## Why

Learning-memory context must remain bounded and tied to the revisions approved by
Whilly. Existing metadata compression can loop forever, and worker binding
validation does not reject every revision that became ineligible after approval.

## What changes

- Bound rendered context by a terminating algorithm that counts final JSON,
  including late omissions and conflicts.
- Revalidate expiry, conflicts, status, and Git source availability immediately
  before a bound worker prompt is built, using the same visible snapshot.
- Add optional request-scoped Cognee retrieval in an offline, separate runtime.
- Fence copy registration against redaction, bound cross-process concurrency,
  and expose named availability/cleanup states through the administrator API.
- Supply approved canonical context consistently to chief and worker engines.

## Impact

The default backend remains L1. Local Cognee activation is gated by real SDK,
isolation, lifecycle, database and handoff acceptance. The change does not
activate research schedules, bulk ingestion, global CLI hooks or external LLMs.
