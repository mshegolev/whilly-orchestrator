# Coding Conventions

**Analysis Date:** 2026-06-10

## Naming Patterns

**Files:**
- Lowercase with underscores: `decision_gate.py`, `external_integrations.py`, `agent_runner.py`
- Test files: `test_<module>.py` (e.g., `test_decision_gate.py`, `test_config_layered.py`)
- Entry point scripts: `cli.py`, `main.py` (for module-level entry points like `whilly.api:main`, `whilly.worker:main`)

**Functions:**
- Lowercase with underscores: `load_dotenv()`, `build_prompt()`, `parse_decision()`, `active_backend_from_env()`
- Private functions: prefix with underscore: `_default_runner()`, `_retry_colima_flake()`, `_cleanup_orphan_testcontainers()`
- Test helpers: prefix with underscore: `_make_task()`, `_write()`, `_stop_once()`

**Variables:**
- Lowercase with underscores for local and module-level variables: `desc`, `log`, `min_size`, `backoffs`
- Constants: UPPERCASE with underscores: `DEFAULT_MODEL`, `DEFAULT_TIMEOUT_S`, `MIN_DESCRIPTION_LEN`, `PROCEED`, `REFUSE`
- Configuration keys: UPPERCASE in dataclass fields: `MAX_PARALLEL`, `MAX_ITERATIONS`, `AGENT_BACKEND`
- Abbreviations expanded when possible: `exc` is acceptable (exception context), `msg` is acceptable (message)

**Types:**
- Type aliases in UPPERCASE: `TaskId: TypeAlias = str`, `PlanId: TypeAlias = str`
- Dataclass names: PascalCase: `WhillyConfig`, `Decision`, `AgentResult`, `ExternalTaskRef`
- Enum classes: PascalCase: `TaskStatus`, `Priority`
- Protocol classes (for backends): PascalCase: `AgentBackend`

**Classes:**
- PascalCase: `WhillyConfig`, `Decision`, `GitHubIntegration`, `ExternalIntegration`
- Abstract base classes: PascalCase with `ABC` suffix pattern: `ExternalIntegration(ABC)`

## Code Style

**Formatting:**
- Tool: `ruff format`
- Line length: 120 characters (set in `pyproject.toml` under `[tool.ruff]`)
- Commands: `ruff format whilly/ tests/` (apply), `ruff format --check whilly/ tests/` (verify)

**Linting:**
- Tool: `ruff check`
- Configuration: `pyproject.toml` under `[tool.ruff]`, target Python 3.12+
- All checks must pass before code is accepted: `ruff check whilly/` must show "All checks passed!"

**Import Organization:**

Order (strictly observed):
1. `from __future__ import annotations` (always first in every module)
2. Standard library imports (alphabetically): `import json`, `import logging`, `import os`, `import re`
3. Third-party imports (alphabetically): `import asyncpg`, `import pytest`, `from fastapi import ...`
4. Local whilly imports: `from whilly.config import ...`, `from whilly.decision_gate import ...`

**Path Aliases:**
- None in use. Full absolute imports from package root: `from whilly.agents import ...`, `from whilly.api.auth_routes import ...`

## Error Handling

**Patterns:**
- Fail-open semantics where appropriate: catch broad exceptions and default to safe behavior
  - Example in `decision_gate.py::parse_decision()`: unparseable JSON defaults to `PROCEED` (proceed with caution)
  - Example in `decision_gate.py::evaluate()`: runner exception returns `Decision(decision=PROCEED, reason="fail-open: ...", cost_usd=0.0)`
- Specific exception catching in subprocess/integration code: catch `json.JSONDecodeError`, `AttributeError`, `FileNotFoundError`, `subprocess.TimeoutExpired`
- Broad `except Exception as e:` only in final fallback paths or integration boundaries (not in pure logic)
- Log at `WARNING` level for recoverable errors that were automatically handled
- Log at `ERROR` level only for unhandled failures or operations that failed permanently

**Error recovery in tests:**
- Use `with pytest.raises(ExceptionType):` for expected exceptions
- Use `with caplog.at_level("WARNING", logger="whilly"):` to assert logging output without swallowing the exception

**Subprocess error handling:**
- Always capture stderr: `subprocess.run(..., capture_output=True, text=True, ...)`
- Check `result.returncode == 0` before assuming success
- Log both `returncode` and stderr on failure

## Logging

**Framework:** `logging` module (stdlib)

**Pattern:**
```python
log = logging.getLogger("whilly")  # or "whilly.module_name"
```

**Usage:**
- `log.warning()` for issues that were auto-recovered (fail-open, retries succeeded, non-blocking)
- `log.error()` for failures that cascade (operation failed, task will retry or be marked failed)
- `log.debug()` for verbose diagnostics (only if explicitly enabled via `WHILLY_VERBOSE=1`)
- `log.info()` sparingly — avoid chatty info logs in loops

**Format:**
- Use `%` formatting, not f-strings, in log calls (allows lazy evaluation and is convention in Python logging)
- Include context: task ID, file path, external system name
- Example: `log.warning("gh issue edit (label flip) failed for %s: %s", task.id, proc.stderr.strip())`

## Comments

**When to Comment:**
- Complex algorithmic steps: numbered comments explaining the 2-3 step fallback logic in `parse_decision()`
  ```python
  # 1) Try direct JSON parse first.
  # 2) Search for an embedded JSON blob.
  # 3) Bare keyword fallback.
  ```
- Non-obvious design decisions: why certain exceptions are caught broadly
  ```python
  except (json.JSONDecodeError, AttributeError):
      pass  # Both can occur when parsing untrusted external output
  ```
- Reason for defensive checks: why a condition is tested
  ```python
  if not override and key in os.environ:
      continue  # Existing env vars win unless override=True
  ```

**Avoid comments for:**
- Obvious code: `if not line:` does not need "skip empty lines" above it
- Dense docstrings already explain it: if a function docstring covers the logic, no inline comments needed

**Docstrings (PEP 257):**
- Module docstring at top (always present): summarizes module purpose and key exports
- Function/method docstring (always present): one-line summary, then blank line, then implementation notes
- Docstring style: Google/NumPy-like (descriptive prose, not param-by-param)

Example from `whilly/decision_gate.py::parse_decision()`:
```python
def parse_decision(raw_text: str) -> tuple[str, str]:
    """Extract (decision, reason) from raw LLM output.

    Tolerant: accepts bare JSON, fenced JSON, JSON with extra text around.
    Returns (PROCEED, "...") on parse failure (fail-open).
    """
```

**Type hints:**
- Always present on function signatures (enforced by strict mypy on `whilly.core.*`)
- Always present on dataclass fields
- Use `|` syntax for unions (Python 3.10+): `str | None`, `int | float`
- Use `TypeAlias` for semantic type aliases: `type TaskId = str` or `TaskId: TypeAlias = str` (PEP 695)

## Function Design

**Size:** 
- Typical function: 20–50 lines
- Keep single-purpose: `build_prompt()` only formats the template, `parse_decision()` only parses the result
- Extract numbered step sequences into comments and keep them in one function if they form a coherent fallback chain

**Parameters:**
- Use explicit keyword arguments for optional/contextual parameters: `def evaluate(task: Task, model: str = DEFAULT_MODEL, timeout_s: int = DEFAULT_TIMEOUT_S, runner: RunnerFn = _default_runner) -> Decision:`
- Dataclass instances over loose parameters: pass a `Task` object, not `task_id, description, priority, ...`
- Callable injection for testability: `runner: RunnerFn` parameter allows tests to inject a mock

**Return Values:**
- Return dataclass instances for structured results: `Decision(decision=..., reason=..., cost_usd=...)`
- Use `tuple` for simple fixed-length returns: `tuple[str, str]` for `(decision, reason)`
- Never return `None` for "not found" — use sentinel values or raise explicitly

## Module Design

**Exports:**
- Explicit `__all__` list only when the module is a public API boundary
- Example: `whilly/agent_runner.py` re-exports the legacy compat API with `__all__ = ["AgentResult", "AgentUsage", "run_agent", ...]`
- No `__all__` in private modules — rely on underscore prefix for privacy

**Barrel Files:**
- `__init__.py` files kept minimal; prefer explicit imports in consuming code
- Exception: `whilly/adapters/transport/__init__.py` may re-export the public server creation function if it's a key surface

**Frozen Dataclasses:**
- Core domain models use `@dataclass(frozen=True)` for value-object semantics and immutability
- Collections inside frozen dataclasses default to `tuple` instead of `list`
- Example: `whilly/core/models.py` has all models frozen per PRD NFR-4

## PEP 695 Type Aliases

For semantic type aliases (e.g., distinct IDs that happen to be strings), use PEP 695 syntax when possible:
```python
type TaskId = str
type PlanId = str
```

Fallback (Python 3.9 compat if needed):
```python
TaskId: TypeAlias = str
```

The whilly codebase uses the latter with explicit imports: `from typing import TypeAlias`.

## Mypy Configuration

**Strict mode for `whilly.core.*`:**
- Applied via `[[tool.mypy.overrides]]` in `pyproject.toml`
- Enforces: `disallow_untyped_defs`, `disallow_any_generics`, `disallow_incomplete_defs`, etc.
- All function signatures in `whilly/core/models.py` and `whilly/core/` must have complete type hints

**Relaxed mode elsewhere:**
- Adapter and API layers: full annotations preferred but not enforced
- Tests: type hints on fixtures and test helpers appreciated but not required

## Async/Await

**Pattern:**
- Async functions marked with `async def`
- Async test functions inherit pytest-asyncio's automode (configured via `asyncio_mode = "auto"` in `pyproject.toml`)
- Explicit `await` for all async calls; never fire-and-forget without `await`
- Use `async with` for context managers: `async with pool.acquire() as conn:`

**Tests:**
- Async tests optionally decorated with `@pytest.mark.asyncio` (but not required due to automode)
- Fixtures that are async: `async def fixture() -> AsyncIterator[Type]:`

---

*Convention analysis: 2026-06-10*
