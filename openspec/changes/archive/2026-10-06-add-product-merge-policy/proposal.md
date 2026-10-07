## Why

Full product publication needs explicit policies for every registered repository
and canonical bindings that reject configuration drift before external effects.

## What Changes

- Add optional product_policy blocks to legacy projects and strict explicit product loading.
- Validate identities, protected-target declarations, checks, DAG, artifact, delivery and revert policies.
- Freeze full registry and policy snapshots with separate canonical bytes and digests.
- Bind both digests to approval input and reject changed snapshots.
- Provide a neutral complete thirteen-project example.

## Impact

- Affected specs: orchestration-loop.
- Affected code: swarm registry and product_registry.
- No remote observation, publication, merge, delivery or credentials are activated.
