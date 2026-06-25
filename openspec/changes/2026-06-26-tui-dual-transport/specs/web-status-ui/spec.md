## ADDED Requirements

### Requirement: Operator snapshot read-only endpoint
The system SHALL expose `GET /api/v1/operator/snapshot` returning the current
operator snapshot as JSON encoded by the shared operator-snapshot codec,
optionally filtered to a single plan via `?plan=<plan_id>`, gated by the same
bearer-auth dependency used by `GET /events/stream` — a registered per-worker
bearer, an active bootstrap token, or the configured legacy fallback token —
and SHALL NOT mutate any task, plan, or worker state in response to this
endpoint.

#### Scenario: Snapshot returned for authenticated caller
- **WHEN** an authenticated client (registered worker bearer, bootstrap token,
  or configured legacy fallback) calls `GET /api/v1/operator/snapshot`
- **THEN** the system SHALL return `200 OK` with the full operator snapshot as
  JSON encoded by the shared operator-snapshot codec

#### Scenario: Optional plan filter narrows the snapshot
- **WHEN** the client calls `GET /api/v1/operator/snapshot?plan=<plan_id>`
- **THEN** the system SHALL return a snapshot scoped to that plan's tasks and
  workers rather than the full cluster view

#### Scenario: Unauthenticated request is rejected
- **WHEN** a client calls `GET /api/v1/operator/snapshot` with no accepted
  credential
- **THEN** the system SHALL respond `401 Unauthorized` with
  `WWW-Authenticate: Bearer realm="whilly"`

#### Scenario: Endpoint is read-only
- **WHEN** `GET /api/v1/operator/snapshot` is called by any authenticated client
- **THEN** the system SHALL return data without altering any task, plan, or
  worker state
