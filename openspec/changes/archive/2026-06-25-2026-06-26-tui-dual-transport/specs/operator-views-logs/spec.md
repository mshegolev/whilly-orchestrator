## ADDED Requirements

### Requirement: TUI read-only HTTP transport
The `whilly tui` command SHALL support a read-only HTTP transport when
`--connect URL` (or `WHILLY_CONTROL_URL`) is supplied, polling the
control-plane's `GET /api/v1/operator/snapshot` endpoint with a bearer token
from `--token` or `WHILLY_WORKER_TOKEN` in place of the direct Postgres
transport; when no connect URL is configured the command SHALL default to the
direct Postgres transport (`WHILLY_DATABASE_URL`, unchanged full-capability
behavior). In HTTP transport mode the TUI SHALL be read-only: control actions
(pause/resume) and human-review decisions SHALL be disabled and the TUI footer
SHALL surface a read-only indicator showing they are unavailable. A plain
`http://` URL targeting a non-loopback host SHALL be rejected with an error
unless `--insecure` (`WHILLY_INSECURE=1`) is set. The operator snapshot wire
schema SHALL be a single shared codec used by both the HTTP client and the server
endpoint.

#### Scenario: HTTP transport selected by --connect
- **WHEN** `whilly tui` is invoked with `--connect <URL>` (or
  `WHILLY_CONTROL_URL` set) and a bearer token from `--token` or
  `WHILLY_WORKER_TOKEN`
- **THEN** the TUI SHALL poll `GET /api/v1/operator/snapshot` on the configured
  URL using `Authorization: Bearer <token>`
- **AND** the TUI SHALL NOT require `WHILLY_DATABASE_URL` to be set

#### Scenario: Postgres transport remains the default
- **WHEN** `whilly tui` is invoked with no `--connect` argument and
  `WHILLY_CONTROL_URL` is unset
- **THEN** the TUI SHALL behave exactly as before — requiring `WHILLY_DATABASE_URL`
  and polling the operator snapshot directly from Postgres

#### Scenario: HTTP mode disables control and review actions
- **WHEN** the TUI is running in HTTP transport mode and the operator presses a
  control hotkey (pause `p`/`P`, resume `R`) or a review-decision hotkey
  (approve, reject, changes_requested)
- **THEN** the TUI SHALL NOT dispatch the action to any backend
- **AND** the TUI footer SHALL display a read-only indicator communicating that
  control and review actions are unavailable in HTTP mode

#### Scenario: Non-loopback http:// rejected without --insecure
- **WHEN** `whilly tui` is invoked with `--connect http://<non-loopback-host>`
  and `--insecure` is not set and `WHILLY_INSECURE` is not `1`
- **THEN** the command SHALL exit with an error stating that plain HTTP to a
  non-loopback host requires `--insecure` or `WHILLY_INSECURE=1`

#### Scenario: --insecure permits non-loopback http
- **WHEN** `whilly tui` is invoked with `--connect http://<non-loopback-host>`
  and `--insecure` is set (or `WHILLY_INSECURE=1`)
- **THEN** the TUI SHALL proceed with the HTTP transport without raising an error
