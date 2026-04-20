# Contributing to Whilly Orchestrator

Thanks for your interest! Small fixes, bug reports and feature ideas are all welcome.

## Dev setup

```bash
git clone https://github.com/mshegolev/whilly-orchestrator
cd whilly-orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Requires Python 3.10+ and [Claude CLI](https://docs.claude.com/en/docs/claude-code) on PATH for any end-to-end runs (tests that need it are skipped if it isn't present).

## Running checks

```bash
ruff check whilly/ tests/
ruff format --check whilly/ tests/
pytest -q
```

These are the same commands CI runs on every PR — if they pass locally, CI should be green.

## Pull request flow

1. Open an issue first for anything non-trivial — easier to align on scope before coding.
2. Branch off `main`, keep changes focused (one concern per PR).
3. Add tests for new behaviour where practical.
4. Ensure `ruff check`, `ruff format --check` and `pytest` all pass.
5. Push and open a PR against `main`. Fill in the PR template.

## Code style

- Line length: 120
- Import order and formatting handled by `ruff format`
- Type hints encouraged but not strictly required (project is not fully typed yet)

## Reporting bugs

Use the bug report template. Include:
- `whilly --version`
- Python version
- A minimal reproduction (a short `tasks.log` + the exact command you ran is usually perfect)

## Questions

Open a GitHub Discussion or an issue with the `question` label.
