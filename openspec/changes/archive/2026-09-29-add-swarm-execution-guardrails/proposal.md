# Fail-closed local swarm execution

## Why

Local swarm workers and verification commands need a host-enforced execution
boundary. A policy-only or argv-only guard is insufficient because child
processes can otherwise read coordinator files, write outside disposable roots,
or access host sockets; unsupported isolation must block rather than fall back.

## What Changes

Add a pure immutable execution-policy contract and a macOS `sandbox-exec`
adapter. The adapter canonicalizes trusted roots, rejects allowed/denied
overlap, denies network by default, runs descendants in the same process group,
and returns bounded transport outcomes for timeout, cancellation, spawn and
output-limit failures. Add executable unit and real local acceptance probes with
positive controls for file and localhost behavior. Task6 will integrate these
results with `ProcessOutcome`; this change does not alter the swarm runtime.
