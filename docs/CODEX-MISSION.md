# Codex Mission Migration: Whilly v6.0 Hardening

Source Factory mission:
`/Users/m.v.shchegolev/.factory/missions/75d95174-16a0-4392-a6c8-c5508a381918`

State at migration: paused, working directory `/opt/develop/whilly-orchestrator`,
Factory id `mis_b0836c2a`, last updated `2026-05-06T07:44:23.977Z`.

## Mission Goal

Continue Whilly v6.0 Security & Rollback Hardening over the v4.6.1/v5.0 baseline.
Scope is additive only:

- Block A: security and isolation.
- Block D: rollback and safety net.
- Preserve v5.0 backcompat, especially `bash workshop-demo.sh --cli stub`.

## Last Closed Feature

`publish-whilly-worker-4-6-1-and-fix-dashboard-sse-401`

Purpose:

- Keep `whilly_worker` PyPI publishing in lockstep with the main package.
- Fix dashboard live updates where anonymous `GET /` rendered a page whose
  `/events/stream` connection failed with 401/403.

Codex close-out:

- Release workflow side appears already committed in `2f64ad1`.
- Dashboard fix implemented with `whilly/api/dashboard_token.py`, `index.html.j2`,
  package-data wiring, and dashboard-token auth for `/events/stream` and read-only
  `/api/v1/tasks`.
- `whilly-worker==4.6.1` manually published to PyPI on 2026-05-06 because the
  original `v4.6.1` tag predated the lockstep worker publish workflow.
- `bash workshop-demo.sh --cli stub` passed on 2026-05-06.

## Current Active Feature

`user-testing-validator-v6-baseline`

Purpose:

- Re-run v6 baseline user testing against the hardened v4.6.1/v5.0 baseline.
- Treat the VPS doctor as the pre-flight gate before any cross-host validation.

Status:

- Blocked on 2026-05-06 by the required VPS doctor tunnel-stability gate:
  `doctor: tunnel-flapping`.
- Latest evidence:
  `out/v6-baseline-vps-doctor/20260506T120315Z/state.json`.
- Healthy checks before the gate: SSH ok, stack running, `/health` ok,
  `/metrics` fail-closed ok, image `mshegolev/whilly:4.6.1`, and
  `openclaw-gateway` running.
- The temporary v6-baseline VPS stack was torn down afterward with
  `bash scripts/v6-baseline-vps-down.sh` without `--volumes`; evidence is in
  `out/v6-baseline-vps-down/`.
- Do not run cross-host validation until
  `bash scripts/v6-baseline-vps-doctor.sh --require-stable` passes.

## Pending Feature Queue

1. `a1-prompt-injection-guard`
2. `a2-shell-deny-list`
3. `a3-a4-sandbox-and-secrets-lint`
4. `d1-d3-backup-tag-and-auto-restore`
5. `d2-branch-protection-preflight`
6. `d4-smart-rollback-cli`
7. `misc-v6-htmx-cdn-inline-or-route-stub`

Completed baseline/support features include VPS topology bringup, v6 fixture harness,
VPS doctor, paid localhost.run funnel migration, funnel resilience, tunnel-stability
doctor gate, changelog breaking-section fix, frozen-file byte-equality follow-up,
and the dashboard SSE / `whilly-worker` publish close-out.

## Hard Boundaries

- Do not touch the off-limits VPS `openclaw-gateway` service or port `18789`.
- Tailscale remains removed; do not introduce `TAILSCALE_*` paths.
- Preserve Docker memory caps and worker import-path purity.
- Keep `Dockerfile` production `CMD ["control-plane"]` invariant.
- Treat `docker-compose.demo.yml`, `workshop-demo.sh`, `Dockerfile.demo`, v3 legacy
  CLI flags, HTMX/SSE/metrics surfaces, and migrations as backcompat-sensitive.
- Do not clean or delete pre-existing untracked analysis artifacts unless explicitly
  requested.

## Validation Gates

Prefer focused tests first, then broaden:

```bash
.venv/bin/python -m ruff check whilly/ tests/
.venv/bin/python -m ruff format --check whilly/ tests/
.venv/bin/lint-imports --config .importlinter
.venv/bin/python -m pytest -q tests/unit --maxfail=3
bash workshop-demo.sh --cli stub
```

For v6-baseline user-testing, run the VPS doctor first:

```bash
bash scripts/v6-baseline-vps-doctor.sh --require-stable
```

Abort cross-host validation if the doctor reports `tunnel-flapping`.
