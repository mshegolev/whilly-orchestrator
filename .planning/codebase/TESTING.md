# Testing Patterns

**Analysis Date:** 2026-06-10

## Test Framework

**Runner:**
- Framework: `pytest` 8.0+
- Config: `pyproject.toml` under `[tool.pytest.ini_options]`
- Key settings:
  - `pythonpath = ["."]` (allows `import whilly` from anywhere)
  - `testpaths = ["tests"]` (only collect from `tests/` directory)
  - `asyncio_mode = "auto"` (auto-detects and runs async tests without explicit decorator)

**Assertion Library:**
- Built-in `assert` statements (stdlib)
- `pytest.approx()` for floating-point comparisons: `assert d.cost_usd == pytest.approx(0.003)`
- `pytest.raises(ExceptionType)` for exception testing

**Run Commands:**
```bash
pytest -q                                    # Run all tests, quiet mode
pytest tests/test_decision_gate.py           # Run single test file
pytest tests/test_decision_gate.py::TestParseDecision::test_clean_proceed  # Single test
pytest -k decision_gate                      # Filter by keyword
pytest -m integration                        # Run tests with @pytest.mark.integration
```

**Coverage:**
```bash
coverage run -m pytest
coverage report
coverage html                                # Generate HTML report
```

## Test File Organization

**Location:**
- Co-located with source (mirrors `whilly/` structure under `tests/`)
- Pattern: `tests/test_<module_name>.py` for unit/functional tests
- Integration tests: `tests/test_<feature>_integration.py` or under `tests/integration/`

**Naming:**
- Test files: `test_*.py` (discovered by pytest automatically)
- Test functions: `test_<behavior>()` (lowercase, underscore-separated)
- Test classes: `Test<Feature>()` (PascalCase, groups related tests)

**Structure:**
```
tests/
├── conftest.py              # Shared fixtures, hooks, skipif markers
├── test_decision_gate.py    # Unit tests for decision_gate module
├── test_config_layered.py   # Unit tests for config with fixtures
├── test_auth_matrix.py      # Integration tests requiring Postgres + FastAPI
├── fixtures/                # Shared fixture data (JSON, YAML, markdown)
│   ├── baselines/
│   └── v4_sample_plans.json
└── integration/
    ├── test_phase1_smoke.py
    └── ...
```

## Test Structure

**Suite Organization:**

A typical unit test file groups tests into classes by behavior:

```python
"""Unit tests for whilly.decision_gate."""

from __future__ import annotations

import pytest
from whilly.decision_gate import PROCEED, REFUSE, parse_decision, Decision, evaluate
from whilly.task_manager import Task


def _make_task(**overrides) -> Task:
    """Helper to build a task with defaults for testing."""
    base = dict(
        id="GH-1",
        phase="GH-Issues",
        description="Add a /health endpoint to the FastAPI server returning ok",
        ...
    )
    base.update(overrides)
    return Task(**base)


class TestParseDecision:
    """Tests for parse_decision() function."""
    def test_clean_proceed(self):
        d, r = parse_decision('{"decision":"proceed","reason":"clear"}')
        assert d == PROCEED
        assert r == "clear"

    def test_invalid_decision_value_falls_open(self):
        d, _ = parse_decision('{"decision":"maybe","reason":"x"}')
        assert d == PROCEED


class TestEvaluateAutoRefuseShortDescription:
    """Tests for evaluate() with short descriptions."""
    def test_short_description_auto_refuses_without_runner_call(self):
        called = []

        def runner(*args, **kwargs):
            called.append(args)
            raise AssertionError("runner should not be called")

        d = evaluate(_make_task(description="x"), runner=runner)
        assert d.decision == REFUSE
        assert "too short" in d.reason
        assert called == []
```

**Patterns:**

1. **Helper factories:** `_make_task()`, `_write()` — private functions that build test objects with defaults
2. **Class grouping:** Tests grouped by the function/feature under test, named `Test<Feature>()`
3. **Numeric prefixes in method names:** None — test methods use full descriptive names (`test_clean_proceed`, not `test_1_clean_proceed`)
4. **Setup/teardown:** Handled by fixtures, not `setUp()`/`tearDown()` methods

## Mocking

**Framework:** `unittest.mock` (stdlib) or pytest fixtures with callable injection

**Patterns:**

For injectable dependencies (preferred):
```python
def test_proceed_returned_with_cost(self):
    result = AgentResult(
        result_text='{"decision":"proceed","reason":"clear"}',
        usage=AgentUsage(cost_usd=0.003),
        exit_code=0,
    )

    def runner(prompt, model, timeout):
        return result

    d = evaluate(_make_task(), runner=runner)
    assert d.decision == PROCEED
    assert d.cost_usd == pytest.approx(0.003)
```

For subprocess/external calls:
```python
def test_flip_runner_failure_returns_false(self):
    d = Decision(decision=REFUSE, reason="x")
    flipped = label_flip_for_gh_task(_make_task(), d, runner=lambda args: 1)
    assert flipped is False
```

For module-level functions (use `monkeypatch` fixture):
```python
def test_secret_keyring_resolution(monkeypatch):
    from whilly.secrets import resolve

    calls: list[tuple[str, str]] = []

    class _FakeKeyring:
        @staticmethod
        def get_password(service, user):
            calls.append((service, user))
            return "from-keyring" if service == "whilly" and user == "github" else None

    monkeypatch.setitem(
        __import__("sys").modules,
        "keyring",
        _FakeKeyring,
    )
    assert secrets_mod.resolve("keyring:whilly/github") == "from-keyring"
    assert calls[0] == ("whilly", "github")
```

**What to Mock:**
- External service calls (LLM, GitHub API, Jira)
- Slow I/O (database queries, file reads — unless testing via testcontainers)
- Subprocess calls (except in integration tests)
- Time-based operations (use `freezegun` for time travel)

**What NOT to Mock:**
- Core business logic (let `Decision`, `Task`, parsing code run for real)
- Data structures (dataclass construction, enum values)
- Standard library (unless testing error paths)
- Code you're testing (only mock its dependencies)

## Fixtures and Factories

**Test Data:**

Fixture files live in `tests/fixtures/`:
```python
def load_fixture(name: str) -> Any:
    """Load a fixture file from ``tests/fixtures/`` by relative name.

    JSON files are parsed; everything else is returned as text.
    """
    path = FIXTURES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"fixture not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return text


@pytest.fixture
def load_fixture_fn() -> Callable[[str], Any]:
    """Pytest fixture wrapper around :func:`load_fixture`."""
    return load_fixture
```

**Usage:**
```python
def test_plan_roundtrip(load_fixture_fn):
    plan_json = load_fixture_fn("v4_sample_plans.json")
    # plan_json is already parsed (dict) because of .json extension
    assert plan_json["project"]
```

**Factories:**
- Inline in test files as `_make_*()` helpers
- Should accept `**overrides` to vary defaults without clutter

Example:
```python
def _make_task(**overrides) -> Task:
    base = dict(
        id="GH-1",
        priority="medium",
        description="Add a /health endpoint",
        status="pending",
        dependencies=[],
        key_files=["app/server.py"],
        acceptance_criteria=["GET /health returns 200"],
        test_steps=["curl localhost"],
    )
    base.update(overrides)
    return Task(**base)

# Usage:
test_task = _make_task(id="CUSTOM-1", priority="critical")
```

**Location:**
- Test-specific factories: inside the test file as module-level functions starting with `_`
- Shared factories: in `conftest.py` as pytest fixtures

## Coverage

**Requirements:** 
- No hard target enforced in CI (set in `pyproject.toml` under `[tool.coverage.report]`)
- Pragmatic: 70%+ for core modules, lower acceptable for adapters and CLI

**Excluded from coverage:**
```python
# pragma: no cover           # Exclude single line
raise NotImplementedError    # Auto-excluded
if TYPE_CHECKING:            # Auto-excluded
```

**View Coverage:**
```bash
coverage run -m pytest
coverage report              # Terminal output
coverage html               # Generates htmlcov/index.html
coverage json              # JSON for CI pipelines
```

## Test Types & Markers

**Unit Tests:**
- Scope: Single function or class, no external I/O
- Location: `tests/test_<module>.py`
- Example: `test_decision_gate.py` (no fixtures, runs in <100ms)
- Run: `pytest tests/test_decision_gate.py`

**Integration Tests:**
- Scope: Multiple components + real Postgres via testcontainers
- Marker: `@pytest.mark.integration`
- Location: `tests/test_*_integration.py` or `tests/integration/`
- Fixture: Uses `db_pool: asyncpg.Pool` from conftest
- Run: `pytest -m integration` (requires Docker)
- Example: `test_auth_matrix.py` (Postgres + FastAPI app + auth flows)

**Docker/Testcontainers:**
- Marker: `@pytest.mark.compose` (docker-compose tests), `DOCKER_REQUIRED` (testcontainers)
- Module-level: `pytestmark = DOCKER_REQUIRED` skips entire file if Docker unavailable
- Per-test: `@pytest.mark.skipif(...)` for conditional skips
- Auto-skip: If Docker not available, tests with `DOCKER_REQUIRED` are silently skipped

**Live LLM Tests:**
- Marker: `@pytest.mark.live_llm`
- Gated by: `WHILLY_RUN_LIVE_LLM=1` environment variable
- Skipped by default in CI
- Purpose: Smoke tests against real Claude API

**Acceptance/E2E:**
- Marker: `@pytest.mark.acceptance`, `@pytest.mark.live_e2e`, `@pytest.mark.wui_e2e`
- Scope: Full workflow from plan to worker to result
- Run manually or in release pipelines
- Example: `test_whilly_e2e_triz_prd.py` (full agent loop)

**Markers defined in pyproject.toml:**
```python
markers = [
    "live_llm: live-LLM smoke test gated by WHILLY_RUN_LIVE_LLM=1 (TASK-104b)",
    "compose: docker-compose-driven integration test",
    "acceptance: full-flow acceptance demo programmatic smoke",
    "wui_e2e: end-to-end WUI workflow tests",
    "ui: browser-driven UI tests via pytest-playwright",
    "live_e2e: full live workflow against real services",
    "integration: integration test requiring real backing services",
]
```

## Async Testing

**Pattern:**
```python
@pytest.mark.asyncio
async def test_post_plans_with_session_and_good_origin_is_not_csrf_blocked(
    auth_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """A session cookie + allowlisted Origin must not be 403'd by CSRF."""
    cookie_value = await _mint_session_cookie(db_pool)
    response = await auth_client.post(
        "/api/v1/plans",
        json={"plan_id": "matrix-good-origin", "name": "Matrix good origin"},
        cookies={COOKIE_NAME: cookie_value},
        headers={"Origin": _GOOD_ORIGIN},
    )
    assert response.status_code in {201, 400, 409, 422}
```

**Automode (`asyncio_mode = "auto"`):**
- Pytest automatically detects async test functions
- `@pytest.mark.asyncio` decorator is optional but still allowed for clarity
- Async fixtures work without decoration

**Error Testing with Async:**
```python
async def test_runner_exception_fails_open_to_proceed(self):
    async def runner(*args, **kwargs):
        raise RuntimeError("network down")

    d = await evaluate(_make_task(), runner=runner)
    assert d.decision == PROCEED
    assert "network down" in d.reason
```

## Fixture Lifecycle & Scope

**Scope Conventions:**

- `scope="session"`: Docker container, expensive one-time setup
  - Example: `PostgresContainer` in conftest (shared across all integration tests)
  - Cleanup: `finally` block in fixture, atexit handler

- `scope="function"` (default): Per-test isolation
  - Example: `db_pool`, database truncation at setup (not teardown)
  - Used for: Most fixtures

- `autouse=True`: Automatically applied to every test in scope
  - Example: `_isolate_env()` in test_config_layered.py (strips WHILLY_* vars)

**Database Fixture Pattern:**
```python
@pytest.fixture
async def db_pool(postgres_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    """Function-scoped asyncpg pool with per-test table TRUNCATE at setup."""
    pool = await create_pool(postgres_dsn, min_size=5, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE events, tasks, plans, magic_links, sessions")
    yield pool
    await close_pool(pool)
```

Why TRUNCATE at setup, not teardown?
- Setup truncation means failing tests leave data for inspection
- Teardown would corrupt the DB for post-mortem debugging

**Isolation Fixture (autouse):**
```python
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Strip WHILLY_* env + stub user config path per test."""
    for key in [k for k in os.environ if k.startswith("WHILLY_")]:
        monkeypatch.delenv(key, raising=False)
    fake_home = tmp_path / "user_home"
    fake_home.mkdir()
    monkeypatch.setattr(config_mod, "user_config_path", lambda: fake_home / "config.toml")
    yield
```

## Common Test Patterns

**Monkeypatch (built-in fixture):**
```python
def test_user_toml_overrides_defaults(tmp_path, monkeypatch):
    _write(tmp_path / "whilly.toml", "MAX_PARALLEL = 2\n")
    monkeypatch.setenv("WHILLY_MAX_PARALLEL", "9")
    cfg = load_layered(cwd=tmp_path)
    assert cfg.MAX_PARALLEL == 9
```

**Caplog (capture logging):**
```python
def test_invalid_toml_is_ignored_with_warning(tmp_path, caplog):
    _write(tmp_path / "whilly.toml", "invalid toml [[[")
    with caplog.at_level("WARNING", logger="whilly"):
        cfg = load_layered(cwd=tmp_path)
    assert cfg.MAX_PARALLEL == 3
    assert any("Invalid TOML" in rec.message for rec in caplog.records)
```

**Parametrize:**
```python
@pytest.mark.parametrize(
    "os_name,expected_fragment",
    [
        ("darwin", "Library/Application Support/whilly"),
        ("linux", "whilly"),
        ("windows", "whilly"),
    ],
)
def test_user_config_path_uses_platformdirs(monkeypatch, os_name, expected_fragment):
    # Test runs 3 times, once per parameter set
    ...
```

**Freezegun (time travel):**
```python
from freezegun import freeze_time

@freeze_time("2026-01-01 12:00:00")
def test_magic_link_expiration():
    # Code sees time as frozen at this moment
    assert datetime.utcnow() == datetime(2026, 1, 1, 12, 0, 0)
```

## Colima/Docker Retry Logic

Integration tests use exponential backoff to work around local Docker port-forwarding flakes:

```python
_TC_RETRY_BACKOFFS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)

def _retry_colima_flake(
    fn: Callable[[], _T],
    *,
    op: str,
    backoffs: tuple[float, ...] = _TC_RETRY_BACKOFFS,
) -> _T:
    """Run ``fn`` with 5-attempt exponential backoff (0.5s, 1.0s, 2.0s, 4.0s, 8.0s)."""
    # 6 attempts total (1 initial + len(backoffs) retries)
```

This is automatically applied to:
1. `PostgresContainer` startup
2. `asyncpg.create_pool()` health check

---

*Testing analysis: 2026-06-10*
