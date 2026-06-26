# Tasks

- [x] Author ADDED delta specs for `operator-views-logs` and `web-status-ui`
- [ ] Implement shared operator-snapshot codec
      (`whilly/adapters/transport/operator_snapshot_codec.py`) with unit tests
- [ ] Add `GET /api/v1/operator/snapshot` route to control-plane server
      (`whilly/adapters/transport/server.py`) gated by bearer auth, with unit tests
- [ ] Implement DB backend for TUI snapshot (thin wrapper around existing
      `fetch_operator_snapshot` Postgres call)
- [ ] Implement HTTP backend for TUI snapshot (polls `/api/v1/operator/snapshot`
      with httpx, decodes via shared codec)
- [ ] Wire `--connect`, `--token`, `--insecure` flags into `whilly tui`; select
      DB or HTTP backend at startup; reject non-loopback `http://` without
      `--insecure`
- [ ] Disable control and review hotkeys in HTTP mode; add read-only footer hint
- [ ] Update `docs/Whilly-Usage.md` with new `whilly tui` flags and
      HTTP transport notes
- [ ] `pytest -k tui` green (all transport-related tests pass)
- [ ] `openspec validate tui-dual-transport --strict` passes
- [ ] `make spec-check` green
- [ ] Archive: `openspec archive tui-dual-transport`
- [ ] Confirm `openspec validate --all --strict` still passes after archive
