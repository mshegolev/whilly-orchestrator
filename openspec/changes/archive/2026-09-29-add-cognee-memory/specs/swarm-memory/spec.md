# Swarm memory

## ADDED Requirements

### Requirement: bounded context assembly

The system SHALL render a deterministic JSON context no larger than the
requested budget, including items, revision manifest, conflicts, and all
omission metadata supplied before or during assembly. Bounding SHALL terminate.

#### Scenario: oversized metadata

- **WHEN** thousands of Unicode omission IDs and long source identifiers are
  supplied with a 256-character budget
- **THEN** assembly terminates and the final rendered context is at most 256
  characters

### Requirement: bound eligibility revalidation

The system SHALL revalidate status, expiry, conflicts, and source availability
for every bound revision immediately before worker prompt construction.

#### Scenario: revision loses eligibility

- **WHEN** a bound revision expires, conflicts, is retracted, or its source is
  unavailable
- **THEN** prompt construction fails closed before returning worker context

#### Scenario: unrelated revision changes

- **WHEN** an unbound revision changes
- **THEN** the existing binding remains valid

#### Scenario: source cannot be verified

- **WHEN** a bound Git revision has no source SHA, or a non-Git revision has no
  expiry
- **THEN** binding validation fails closed before worker context is returned

### Requirement: authoritative scoped retrieval

The system SHALL authorize and verify canonical memory before indexing and SHALL build responses only from revalidated canonical revisions, never from backend-supplied bodies.

#### Scenario: backend returns an unauthorized identity

- **WHEN** a backend returns an unknown or duplicate revision ID
- **THEN** lookup fails with a named error rather than accepting or silently discarding the invalid result

#### Scenario: a source changes during retrieval

- **WHEN** an indexed revision changes, expires, conflicts, is superseded, or loses source verification
- **THEN** it is not supplied as current knowledge or accepted for an approved worker prompt

### Requirement: isolated local Cognee runtime

The system SHALL execute Cognee 1.6.1 in a separate local runtime with OS-enforced network denial, read-only preloaded models, and request-local writable storage, without generative LLM calls.

#### Scenario: missing isolation or model dependencies

- **WHEN** required sandboxing, models, or runtime dependencies are unavailable
- **THEN** selected Cognee retrieval reports unavailable and does not fall back silently

#### Scenario: child attempts unrelated access

- **WHEN** the child attempts a network connection or reads an unrelated file
- **THEN** the operating system denies access

### Requirement: bounded request lifecycle

The system SHALL limit retrieval to 32 records, 128 KiB of serialized input, one active index and four queued requests per installation, and a 90-second operation deadline with explicit cleanup state.

#### Scenario: timeout or caller cancellation

- **WHEN** an active lookup times out or its caller is cancelled
- **THEN** its child process group is terminated and reaped before temporary-copy cleanup is acknowledged

#### Scenario: installation receives excess work

- **WHEN** one lookup is active and four requests are queued
- **THEN** a further request receives a named queue-full outcome

### Requirement: redaction fencing and cleanup

The system SHALL serialize canonical redaction with copy registration and SHALL distinguish logical redaction from pending temporary-copy cleanup.

#### Scenario: redaction races with lookup registration

- **WHEN** a revision is redacted while lookups are being registered or indexed
- **THEN** new copies of that revision are prevented and existing affected lookups are cancelled
- **AND** incomplete cleanup is reported as pending

#### Scenario: orphan recovery

- **WHEN** a process crashes and leaves request storage behind
- **THEN** recovery removes only proven adapter-owned, inactive request storage, without following unrelated symlinks

### Requirement: backend status and engine handoff

The system SHALL expose backend readiness to authenticated administrators and SHALL give each Whilly-launched engine only approved knowledge for its project and declared dependencies.

#### Scenario: backend disabled after approval

- **WHEN** Cognee is disabled after a plan was approved
- **THEN** execution still validates the plan's canonical memory binding

#### Scenario: empty knowledge registry

- **WHEN** the canonical registry has no eligible records
- **THEN** the UI reports empty knowledge distinctly from backend unavailability
- **AND** readiness and errors do not expose queries, bodies, credentials, or private source paths
