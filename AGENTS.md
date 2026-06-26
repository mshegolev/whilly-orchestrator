# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.12 package. Main source lives in `whilly/`: CLI entry points in `whilly/cli/`, pure domain code in `whilly/core/`, adapters in `whilly/adapters/`, and worker runtime code in `whilly/worker/`. Docker support lives in `Dockerfile*`, `docker/`, and `docker-compose*.yml`. Tests are in `tests/unit/`, `tests/integration/`, and selected top-level regression files. Docs and planning artifacts live in `docs/`, `library/`, `.planning/`, and `examples/`.

## Build, Test, and Development Commands

- `python3 -m venv .venv && source .venv/bin/activate`: create a local virtualenv.
- `pip install -e '.[dev]'`: install Whilly with server, worker, and developer dependencies.
- `make lint`: run `ruff check` and `ruff format --check` over `whilly/` and `tests/`.
- `make format`: apply Ruff formatting and safe lint fixes.
- `make test`: run pytest with capped xdist parallelism.
- `pytest -q tests/unit`: run the faster unit subset.
- `docker-compose up -d`: start the local Postgres development service.
- `docker build -t whilly:dev .`: build the production multi-role image.

## Coding Style & Naming Conventions

Use 4-space indentation, Python type hints where practical, and a 120-character line length. Ruff is the formatter and linter. Keep `whilly.core` pure: no network, database, subprocess, or framework imports except documented exceptions in `.importlinter`. Prefer explicit module names and test names that describe behavior, for example `test_concurrent_claims_audit_log_consistent`.

## Testing Guidelines

Pytest is the test runner; async tests use `pytest-asyncio`. Name test files `test_*.py` and keep integration tests under `tests/integration/`. Docker-backed tests are skipped when Docker is unavailable, but run them before changing Postgres, workers, compose, or Dockerfiles. For focused changes, run the smallest relevant file first, then `make test` when practical.

## Commit & Pull Request Guidelines

Git history follows Conventional Commit style with scopes, such as `feat(v6-baseline): ...`, `fix(m2): ...`, `test(m1): ...`, and `docs(misc): ...`. Keep commits focused and include validation IDs when relevant. Pull requests should describe the behavior change, list tests run, link issues or planning tasks, and call out Docker, migration, or configuration impacts.

Any change to `whilly/` behavior MUST ship with an `opsx` spec delta (propose → apply → archive) that updates the relevant capability spec at `openspec/specs/<slug>/spec.md` — required, not optional. OpenSpec is the living WHAT; do not let the spec and code drift. See `openspec/FORWARD-PROCESS.md` for the workflow. Pure docs/test/refactor with no behavior change is exempt.

## Security & Configuration Tips

Do not commit real secrets. Use `.env.example`, `.env.worker.example`, `whilly.example.toml`, or environment variables for configuration. Treat `ANTHROPIC_API_KEY`, `GH_TOKEN`, `GROQ_API_KEY`, database URLs, worker bootstrap tokens, and Slack tokens as sensitive.

**This is a PUBLIC repo — do not commit company or user data either.** Keep company-internal identifiers out of tracked files (source, tests, docs, fixtures): real internal hostnames (your corporate GitLab/Jira/registry hosts), Jira/GitLab project/issue keys, team/namespace names, repo paths, and personal names/emails. Use neutral placeholders in tracked code (`gitlab.example.com`, `jira.example.com`, `DEMO-123`, `ACME`, `example-group/repo`, `you@example.com`); configure real values locally via gitignored `.env`/`whilly.toml`, and read them at runtime with a neutral default in code (`os.environ.get("WHILLY_GITLAB_SSH_HOST", "gitlab.example.com")`). The redaction anonymizer (`whilly/adapters/runner/anonymizer.py`) is the sole intentional exception.

## Active Codex Mission

The Factory mission is migrated in `docs/CODEX-MISSION.md`. Use it for v6.0 hardening scope, feature order, validation gates, and boundaries. Do not delete untracked `out/`, `.planning/distributed-audit/`, or analysis artifacts unless explicitly asked.
