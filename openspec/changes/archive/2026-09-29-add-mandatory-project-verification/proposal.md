# Mandatory project verification bindings

## Why

Guarded swarm execution must use host-selected project verification rather than
commands supplied by a task or worker. Approval must also remain tied to the
resolved base, verification policy, and pinned hook policy.

## What Changes

Add canonical test, lint, and architecture policies with fail-closed parsers;
persist their digests and host-owned execution bindings in existing JSON
columns. Legacy verification remains readable for discussion and planning, but
cannot make a revision ready for guarded execution by itself.
