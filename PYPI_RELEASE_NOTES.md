# Whilly Orchestrator v4.6.1 — M3 user-facing fix bundle 🩹

## What's New

**v4.6.1 is a strictly-additive patch release** that gets five M3
user-facing fixes — already on `main` since the day after v4.6.0 was
cut — into the published artefacts (PyPI sdist + wheel and the
multi-arch `mshegolev/whilly:4.6.1` Docker Hub tag). Operators
running the default `WHILLY_IMAGE` now get the fixed live-dashboard /
tasks-API / metrics / SSE / health surfaces without a manual
rebuild. No schema migration, no env-var changes, no breaking
changes.

## Why upgrade

If you pulled `mshegolev/whilly:4.6.0` (or installed
`whilly-orchestrator==4.6.0`) and saw any of these symptoms, v4.6.1
is the fix:

| Symptom on 4.6.0 | Fixed in 4.6.1 |
|---|---|
| HTMX dashboard rows didn't live-swap on task state changes | `sse:task.*` triggers realigned to the broker's UPPERCASE event names (`TASK.CLAIM`, `TASK.COMPLETE`, …) |
| `GET /api/v1/tasks?status=typo` returned an empty list with HTTP 200 (typo masked as empty queue) | now `400 Bad Request` with allowed-values enumerated in the body |
| `whilly_plan_budget_remaining_usd{plan_id="…"}` series for deleted plans stuck around in `/metrics` forever | refresh loop diffs label sets and drops stale ones |
| `GET /events/stream` with a bigint-overflow `Last-Event-ID` header 500-ed via `asyncpg.OutOfRangeError` | overflow is treated as malformed → start-fresh path |
| `GET /health` reported `listener_connected:true` even while the listener task was in its exponential-backoff reconnect window | probe reads `_ListenerState.connected` instead of `task.done()`, aligned with `GET /health/ready` |

## Quick upgrade

```bash
# PyPI (orchestrator + worker meta-package, lockstep pin)
pip install --upgrade whilly-orchestrator==4.6.1 whilly-worker==4.6.1

# Docker Hub (multi-arch: linux/amd64 + linux/arm64)
docker pull mshegolev/whilly:4.6.1
```

The worker meta-package keeps its `==X.Y.Z` pin to the orchestrator
— always upgrade both together. No `alembic upgrade head` is needed
(head stays at migration `011`).

## Compatibility

- **Backwards-compatible.** No breaking changes; no migrations; no
  env-var changes. `bash workshop-demo.sh --cli stub` is
  byte-equivalent to v4.6.0; the existing single-host
  `docker-compose.demo.yml` stack continues to work unchanged.
- **Default `WHILLY_IMAGE` lifted to `mshegolev/whilly:4.6.1`** in
  `docker-compose.control-plane.yml`, `docker-compose.worker.yml`,
  and `.env.worker.example`. Operators with `WHILLY_IMAGE` already
  pinned in their environment are unaffected.
- **No new optional-extras packages.** `[server]` and `[worker]`
  closures are identical to v4.6.0.

## Internal Quality

- `ruff check whilly/ tests/` clean.
- `ruff format --check whilly/ tests/` clean.
- `lint-imports --config .importlinter` 1 contract kept / 0 broken
  (worker import-path purity preserved).
- `tests/integration/test_compose_default_image_tag.py` passes with
  the bumped 4.6.1 default.

## Migration from 4.6.0

```bash
pip install --upgrade whilly-orchestrator==4.6.1
docker pull mshegolev/whilly:4.6.1
```

That's it. No data migration, no config rewrite, no runtime
behaviour difference outside the five fixes listed above.

---

*Whilly Orchestrator — Ralph Wiggum's smarter brother. v4.6.1 picks
up the M3 user-facing bugs that slipped past the v4.6.0 cutoff and
ships them on the default `:4.6.1` tag.*
