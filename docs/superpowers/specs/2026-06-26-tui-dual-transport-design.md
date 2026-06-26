# TUI dual transport — direct DB and WUI-FQDN (read-only)

> **Status:** Design approved 2026-06-26. Implementation routes through an
> `opsx` change proposal (propose → apply → archive) because it changes
> `whilly/` behavior. This document is the design of record; the opsx delta
> captures the normative spec change.

## 1. Overview

`whilly tui` currently talks **only** to Postgres via `WHILLY_DATABASE_URL`
(`whilly/cli/tui.py`). This adds a second, **read-only** transport so an
operator can watch the cluster either:

- **DB mode** (unchanged default): direct Postgres pool — full capability
  (view + control actions + human-review decisions).
- **HTTP mode** (new): over the WUI FQDN with a bearer token — **read-only**
  (monitoring surfaces only). Control and review stay DB-only.

Use case: an operator on a laptop with no Postgres reachability points the TUI
at `https://whilly.<corp-domain>` and watches the cluster fan out.

Non-goals (YAGNI): no control/review over HTTP, no new auth scheme, no SSE in
the TUI (the existing poll loop is retained).

## 2. Mode selection

Explicit flag with DSN fallback (backwards-compatible):

| Inputs present | Mode |
|---|---|
| `--connect URL` (or `WHILLY_CONTROL_URL`) | HTTP (read-only) |
| no connect URL, `WHILLY_DATABASE_URL` set | DB (full) |
| neither | error, exit 2 |

New CLI args on `whilly tui`:
- `--connect URL` — control-plane base URL (env `WHILLY_CONTROL_URL`).
- `--token TOKEN` — bearer (env `WHILLY_WORKER_TOKEN`).
- `--insecure` — allow plain `http://` to a non-loopback host (env
  `WHILLY_INSECURE`), mirroring worker URL-scheme-guard semantics.

## 3. Components and boundaries

### 3.1 `whilly/operator_snapshot_codec.py` (new)
Single source of truth for the wire schema. Pure functions:
`snapshot_to_dict(OperatorSnapshot) -> dict` and
`snapshot_from_dict(dict) -> OperatorSnapshot`, covering every nested type
(`WorkerRow`, `OperatorTaskRow`, `EventRow`, `ReviewGap`, queue-health rows,
`control_state`, `summary`, `rendered_at`). Forward-compatible: unknown keys
ignored on decode; missing required keys raise. Used by **both** the server
endpoint and the HTTP client so the schema can never drift between them.

### 3.2 `whilly/api/operator_api.py` (new router)
`GET /api/v1/operator/snapshot?plan=<id>`:
- Bearer auth via the existing SSE gate helper (`_authenticate_stream_request`
  in `whilly/api/sse_endpoint.py`) — accepts a registered worker bearer, an
  active bootstrap-token row, or the legacy fallback token.
- Calls `fetch_operator_snapshot(pool, plan_id=...)` (the same function the WUI
  HTML dashboard already uses in `whilly/api/dashboard.py`).
- Returns `snapshot_to_dict(...)` as JSON.
- Registered in `create_app` alongside the other routers.

### 3.3 `whilly/cli/tui_backends.py` (new)
```
class OperatorBackend(Protocol):
    read_only: bool
    async def fetch_snapshot(self, plan_id: str | None) -> OperatorSnapshot: ...
    async def close(self) -> None: ...
```
- `DbOperatorBackend(pool)` — wraps `fetch_operator_snapshot` + `TaskRepository`
  for control/review; `read_only = False`. (Today's behavior, extracted.)
- `HttpOperatorBackend(base_url, token, *, insecure=False)` — httpx GET against
  the snapshot endpoint, parses via the codec; `read_only = True`. Control and
  review methods are absent/raise so callers must gate on `read_only`.

### 3.4 `whilly/cli/tui.py` (refactor)
- Add the args above; resolve the backend from inputs (§2).
- **Lazy-import** `whilly.adapters.db` (`create_pool`) — only inside the DB
  branch — so HTTP mode runs on a host without asyncpg / DB access.
- The poll loop calls `backend.fetch_snapshot(plan_id)` regardless of mode.
- Mutating hotkeys (control actions, review decisions) are gated on
  `backend.read_only`: inert in HTTP mode, with a footer hint
  ("read-only (HTTP) — connect to the DB for control").

## 4. Data flow (HTTP mode)

```
TUI poll tick
  → httpx GET {base}/api/v1/operator/snapshot?plan=…  (Authorization: Bearer …)
  → server: fetch_operator_snapshot(pool, plan_id)
  → server: snapshot_to_dict → JSON
  → TUI: snapshot_from_dict → OperatorSnapshot
  → render (identical render path to DB mode)
```

## 5. Authentication and URL guard

`Authorization: Bearer <token>`; the server reuses the SSE bearer gate. The
client applies the same scheme guard as the worker: plain `http://` to a
non-loopback host is rejected before any request unless `--insecure`. HTTPS via
the FQDN is the expected path (TLS terminates at the ingress).

## 6. Error handling

- Neither `--connect` nor `WHILLY_DATABASE_URL` → exit 2, clear message.
- 401/403 → "token rejected", exit 2.
- Network error / 5xx **during the poll loop** → transient "disconnected,
  retrying" banner; the loop keeps polling (no crash). A failure on the **first**
  connect → exit 2 with a clear message.
- Codec decode: unknown fields ignored; missing required field → explicit error.

## 7. Testing

- **Codec**: round-trip `snapshot → dict → snapshot` equality across all nested
  types; unknown-key tolerance; missing-key failure.
- **Endpoint**: 401 (no bearer), 403 (bad bearer), 200 with snapshot JSON, and
  `?plan=` filtering — against the existing API test harness.
- **HttpOperatorBackend**: stub httpx transport returns a snapshot dict → parsed
  `OperatorSnapshot`; `read_only` inertness for control/review.
- **Mode resolution**: connect→HTTP, DSN→DB, neither→exit 2; an import-guard
  test asserting asyncpg is **not** imported on the HTTP path.
- **Backwards-compat**: existing DB-mode TUI tests pass unchanged.

## 8. Docs and deploy follow-ups

- `docs/Whilly-Usage.md`: document `whilly tui --connect/--token/--insecure` and
  that `WHILLY_CONTROL_URL` selects HTTP (read-only) vs `WHILLY_DATABASE_URL`
  selecting DB (full).
- Helm chart README/NOTES: note operators can run the TUI against the WUI FQDN
  read-only with a worker/bootstrap bearer (no DB port-forward needed).

## 9. Process

This changes `whilly/` behavior, so implementation ships with an `opsx` change
proposal updating the relevant capability spec (operator-surface / transport —
confirm against `openspec/COVERAGE-MATRIX.md` at plan time). The change is not
complete until that delta is applied and archived.
