# Proposal: TUI dual transport — read-only HTTP operator snapshot

## Why

`whilly tui` currently requires a direct Postgres connection
(`WHILLY_DATABASE_URL`). This makes it inaccessible to operators who can reach
the control-plane HTTP endpoint but cannot reach Postgres directly — a common
constraint for SREs inspecting a remote cluster through the `whilly server`
control plane.

Adding a read-only HTTP transport lets the TUI consume the operator snapshot over
the same authenticated connection that remote workers use, without adding a
mutation surface. Control actions (pause/resume) and human-review decisions stay
DB-only so the read-only path cannot silently lose control intent.

## What Changes

- **ADDED** `operator-views-logs` → "TUI read-only HTTP transport": when
  `--connect URL` (or `WHILLY_CONTROL_URL`) is supplied, `whilly tui` SHALL poll
  `GET /api/v1/operator/snapshot` with a bearer token (`--token` /
  `WHILLY_WORKER_TOKEN`) in place of the direct Postgres transport. Without a
  connect URL the command defaults to Postgres (unchanged behavior, full
  capability). In HTTP mode control actions (pause/resume) and human-review
  decisions are disabled and surfaced as unavailable in the TUI footer. A plain
  `http://` URL to a non-loopback host is rejected unless `--insecure`
  (`WHILLY_INSECURE=1`) is set. The operator snapshot is serialized via a single
  shared codec used by both the HTTP client and the server endpoint.
- **ADDED** `web-status-ui` → "Operator snapshot read-only endpoint": the
  control-plane SHALL expose `GET /api/v1/operator/snapshot` (optional `?plan=`
  filter) returning the operator snapshot as JSON encoded by the shared codec,
  gated by the same bearer auth as `GET /events/stream` (a registered per-worker
  bearer, an active bootstrap token, or the configured legacy fallback). The
  endpoint is read-only — no task, plan, or worker state is mutated.

## Impact

- Specs: `operator-views-logs` (1 requirement added), `web-status-ui`
  (1 requirement added).
- Code: new `whilly/adapters/transport/operator_snapshot_codec.py` (shared
  serializer/deserializer), new route in `whilly/adapters/transport/server.py`,
  new `--connect`, `--token`, `--insecure` CLI flags on `whilly tui` wired
  through `whilly/cli/tui.py`.
- Coverage matrix: add `whilly/adapters/transport/operator_snapshot_codec.py`
  to both `operator-views-logs` (client) and `web-status-ui` (server).
- Backward compatible — no connect URL ⇒ unchanged Postgres-direct behavior.
- No schema change required.
