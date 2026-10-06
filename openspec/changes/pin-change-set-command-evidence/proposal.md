# Pin change-set command evidence

## Why

Native JSONB evidence constraints must enforce the same command element contract
as domain rehydration so immutable audit records remain readable after restart.

## What Changes

- Require each supplied command-array element to be a nonempty string.
- Validate argv independently of job identity and named-absence alternatives.
- Verify native rejection in every evidence-bearing table and schema roundtrip.

## Impact

Capability: state-persistence. Correction to unreleased migration 041; no domain
state, runtime, dependency or external-effect changes.
