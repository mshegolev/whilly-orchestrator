# Durable product change-set state

## Why

Product-wide changes need an immutable registry/baseline definition, durable
state boundaries and auditable receipts before publication or merge can be added.

## What Changes

- Add product/repository state models, explicit outcomes and complete registry coverage.
- Persist state with optimistic versions and atomic append-only transition evidence.
- Retain immutable scoped external-effect receipts with idempotent key reuse.
- Prevent completion without passed stage acceptance and required artifacts.
- Add migration 041 after the existing learning schema, preserving legacy workflows.

## Impact

Capabilities: orchestration-loop and state-persistence. No GitLab transport,
merge, deployment, UI activation, registry onboarding or external credentials.
