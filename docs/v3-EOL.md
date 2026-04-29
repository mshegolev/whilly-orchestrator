# Whilly v3.x — End of Life Announcement

**Effective date:** 2026-04-27
**Frozen at tag:** [`v3-final`](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3-final)
**Last release on the v3.x line:** v3.3.0 (PyPI)
**Successor line:** v4.0 (in development — see [PRD](PRD-refactoring-1.md))

---

## What this means

The v3.x line of `whilly-orchestrator` is in **maintenance mode**:

- ✅ Critical bugfixes (security, data loss, crashes that block existing users) — will be backported on a best-effort basis until v4.0 ships.
- ❌ New features — land only in v4.0.
- ❌ Breaking refactors of v3.x APIs — none planned. The v3 surface (`whilly --tasks tasks.json`, `WHILLY_*` env vars, `tasks.json` schema with embedded statuses) is frozen as of `v3-final`.

If you are a current v3.x user: **nothing breaks today.** Your `tasks.json` keeps working, your CI pipelines keep working. You have a clear "do nothing" option until v4.0 is released and you choose to migrate.

---

## Why v4.0 is incompatible (no in-place upgrade)

v4.0 is a distributed rewrite around three load-bearing changes that cannot be retrofitted into v3.x without making the codebase worse for both audiences:

1. **State of the world moves from `tasks.json` (file on disk) to PostgreSQL.** Optimistic locking via `SELECT ... FOR UPDATE SKIP LOCKED` is the only honest answer to concurrent claim races. File-based locking on top of `tasks.json` was a known sharp edge in v3.x (see "ghost plans" in `docs/status/STATUS-2026-04-27.md`).
2. **Workers become network clients.** v4.0 separates a FastAPI control plane from `whilly-worker` processes that connect over HTTPS with a bootstrap token. v3.x always assumed orchestrator + agents share a filesystem.
3. **Hexagonal architecture.** `whilly/core/` is pure (no `asyncpg`, no `httpx`, no `subprocess`, no `os.chdir`), enforced by `import-linter`. The v3.x `whilly/cli.py` (~1000 LOC of mixed I/O and orchestration) cannot be unwound incrementally — it would need a parallel `core/` regardless, so we do it as a rewrite, not a refactor.

These three together imply: new schema, new CLI surface, new dependencies (Postgres 15+, Python 3.12+). Backwards compat would mean shipping two storage backends, two CLIs, and two architectures inside one package — which is the opposite of why we're doing this.

> Full reasoning: [PRD-refactoring-1.md, Appendix C](PRD-refactoring-1.md#appendix-c-why-not-functional-programming-rejected-approach) — and the design discussion that produced it.

---

## Migration story (v3.x → v4.0)

When v4.0 ships, migrating an existing v3.x project takes three steps:

```bash
# 1. Install v4.0 (replaces v3.x)
pipx install --force whilly-orchestrator==4.0.0

# 2. Bring up Postgres (one-time)
docker compose -f docker-compose.yml up -d
alembic upgrade head

# 3. Re-import your existing plan
whilly plan import tasks.json
whilly run                   # was: whilly --tasks tasks.json
```

What changes for you:

| v3.x                                         | v4.0                                                        |
| -------------------------------------------- | ----------------------------------------------------------- |
| `whilly --tasks tasks.json`                  | `whilly plan import tasks.json && whilly run`               |
| `tasks.json` is the source of truth          | `tasks.json` is an import format; Postgres holds live state |
| `.whilly_state.json` (per-machine resume)    | gone — state is in DB, `whilly run --resume` reads from DB  |
| `tmux_runner` / `worktree_runner` per task   | local worker (`whilly run`) or remote (`whilly-worker`)     |
| File locking + atomic writes for concurrency | `SELECT FOR UPDATE SKIP LOCKED` + optimistic locking        |
| 1 machine                                    | 1 control plane + N worker machines                         |

Your `PRD-*.md` files are **unchanged** — the PRD wizard and `whilly --init` flow stays.

The legacy entry point `whilly --tasks tasks.json` will print a clear error in v4.0:

> `v3.x CLI is gone. Use 'whilly plan import tasks.json && whilly run'. See docs/v3-EOL.md.`

— so existing CI pipelines fail loudly, not silently.

---

## What if I don't want to migrate?

That's a supported answer. `pip install 'whilly-orchestrator==3.3.0'` (or pin to `<4`) keeps you on v3.x indefinitely. We will:

- Keep the `v3-final` tag immutable.
- Cut v3.3.x bugfix releases for security or data-loss bugs (best-effort, **only on user report** — we do not run continuous security scans on the v3 line).
- Not add new features to the v3.x line. Not even small ones. Not even good ones.

If a v3.x bugfix is needed, file an issue with the `v3-eol` label.

---

## Why now, not "after v4.0 ships"

Freezing v3.x **before** v4.0 ships (rather than after) is intentional:

- It removes the implicit promise that v3.x will keep evolving in parallel to v4.0 — which would either dilute v4.0 effort or set up a maintenance trap.
- It gives users a known anchor (`v3-final`) before the rewrite churn begins. If you need to fork v3.x for a downstream project, do it from this tag.
- It makes the v4.0 PRD's risk R-2 ("Big-bang rewrite oставляет v3.x пользователей без апгрейд-пути") concrete: there *is* an apgrade path — re-import — and there *is* an opt-out — pin to v3.

---

## Timeline

| Date           | Event                                                                  |
| -------------- | ---------------------------------------------------------------------- |
| **2026-04-27** | v3-final tag created. v3.x enters maintenance mode. (this announcement) |
| **2026-05-04** | v4.0 development begins per [PRD-refactoring-1.md](PRD-refactoring-1.md) — 1-week sprint. |
| **2026-05-11** | v4.0.0 target release (PRD Day 7 deliverable; may slip — see PRD R-1).  |
| **+30 days**   | v3.x backports require explicit user issue with `v3-eol` label.         |
| **2026-12-31** | v3.x best-effort backport window closes. v3.3.x stays on PyPI but is unmaintained. |

---

## Pointers

- v3.x final state: <https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3-final>
- v4.0 PRD: [`docs/PRD-refactoring-1.md`](PRD-refactoring-1.md)
- v3.x weekly status (last full report on this line): [`docs/status/STATUS-2026-04-27.md`](status/STATUS-2026-04-27.md)
- Architecture (ArchiMate Open Exchange XML, importable into Archi): [`docs/status/whilly-archimate.xml`](status/whilly-archimate.xml)
