# Harden swarm mailbox transport

## Why

Task-local mailbox files must remain safe when workers can create special files
or replace mailbox path components while the coordinator awaits a service call.

## Scope

This change hardens legacy messaging, collaboration messaging, and
proposal-only mailbox consumers with one descriptor-anchored filesystem
adapter. Public enqueue and inbox APIs remain unchanged.

## Outcome

Mailbox reads are no-follow, nonblocking, bounded, and regular-file checked;
receipts, state, requests, and removals are descriptor-relative and atomic.
