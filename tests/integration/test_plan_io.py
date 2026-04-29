"""Round-trip integration tests for ``whilly plan import`` / ``export`` (TASK-010c).

Acceptance criteria from TASK-010c:

* ``whilly plan export <id>`` prints JSON to stdout and exits ``0``.
* A non-existent ``plan_id`` exits ``2`` with a helpful stderr message.
* ``import → export → import`` is idempotent (the round-trip diff is empty).

The round-trip is the load-bearing test. It proves the
:func:`~whilly.adapters.filesystem.plan_io.parse_plan` /
:func:`~whilly.adapters.filesystem.plan_io.serialize_plan` pair is *symmetric*
when fed through Postgres in between, which is the contract every later
"snapshot a plan, replay it elsewhere" workflow (TASK-015 ``plan show``,
TASK-029 backups) will rely on.

Why integration, not unit?
--------------------------
``parse_plan`` / ``serialize_plan`` already have a pure unit test
(:mod:`tests.unit.test_plan_io`). The new surface in TASK-010c is the SQL
SELECT path in :mod:`whilly.cli.plan` plus the CLI entry point itself —
both of those need a real Postgres to exercise meaningfully. Mocking
asyncpg here would only assert that we *call* the right methods, not that
the JSONB columns round-trip cleanly through the database (which is
exactly the failure mode the test is designed to catch — every previous
v3 attempt at "save the plan in SQL" stumbled on JSON encoding).

Fixture strategy
----------------
We re-use ``db_pool`` from :mod:`tests.conftest` (per-test asyncpg pool
against a session-scoped testcontainers Postgres with migrations applied).
That fixture also TRUNCATEs every table at setup, so each test starts from
an empty DB. The round-trip test populates the DB itself by calling
:func:`whilly.cli.plan.run_plan_command` against a temp-file plan, so it
exercises the same surface an operator would touch.

We deliberately do **not** use :class:`subprocess.Popen` to spawn a real
``whilly`` process. The handler is plain-Python and ``capsys`` /
``capfd`` capture its stdout / stderr cleanly; spawning a subprocess
would force us to install the package first (``pip install -e .``)
and brittle-ifies CI. The :func:`run_plan_command` import path mirrors
``whilly plan ...`` exactly because that's how :func:`whilly.cli.main`
dispatches itself.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.filesystem.plan_io import parse_plan
from whilly.cli.plan import (
    DATABASE_URL_ENV,
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    run_plan_command,
)

# Module-level skip — every test in this file boots a Postgres container via
# the session-scoped ``postgres_dsn`` fixture, so a Docker-less CI runner
# should skip collection rather than fail per-test.
pytestmark = DOCKER_REQUIRED


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def database_url(postgres_dsn: str) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` for the duration of one test.

    :func:`run_plan_command` reads the DSN from the environment (it is a
    one-shot CLI; the alternative would be threading the DSN through every
    function signature). The ``postgres_dsn`` fixture already exports it for
    its own bootstrap, but we restore the prior value on teardown so an
    earlier test that sets a custom DSN doesn't leak into a later one.
    """
    prior = os.environ.get(DATABASE_URL_ENV)
    os.environ[DATABASE_URL_ENV] = postgres_dsn
    try:
        yield postgres_dsn
    finally:
        if prior is None:
            os.environ.pop(DATABASE_URL_ENV, None)
        else:
            os.environ[DATABASE_URL_ENV] = prior


@pytest.fixture
def sample_plan_payload() -> dict[str, Any]:
    """Return a v4 plan dict with multiple tasks, dependencies, and priorities.

    Picked to exercise every field :func:`serialize_plan` emits:

    * ``plan_id`` distinct from ``project`` (so the export must not collapse
      them);
    * tasks at three priority levels (``critical``, ``high``, ``low``) — the
      schema's CHECK constraint accepts these and the export ORDER BY ``id``
      preserves a deterministic listing across reruns;
    * a non-empty ``dependencies`` array (catches JSONB encoding bugs);
    * empty ``key_files`` / ``acceptance_criteria`` (catches the ``[]``
      default path);
    * ``prd_requirement`` with non-ASCII (catches UTF-8 round-trip).
    """
    return {
        "plan_id": "plan-roundtrip-001",
        "project": "Roundtrip Workshop",
        "tasks": [
            {
                "id": "T-001",
                "status": "PENDING",
                "priority": "critical",
                "description": "Bootstrap the workshop.",
                "dependencies": [],
                "key_files": ["README.md", "src/main.py"],
                "acceptance_criteria": ["docs render", "main runs"],
                "test_steps": ["pytest -q"],
                "prd_requirement": "FR-1.1",
            },
            {
                "id": "T-002",
                "status": "PENDING",
                "priority": "high",
                "description": "Зависит от T-001 — нужна для проверки JSONB UTF-8.",
                "dependencies": ["T-001"],
                "key_files": [],
                "acceptance_criteria": [],
                "test_steps": [],
                "prd_requirement": "FR-2.2",
            },
            {
                "id": "T-003",
                "status": "PENDING",
                "priority": "low",
                "description": "Trailing low-priority task.",
                "dependencies": ["T-001", "T-002"],
                "key_files": ["scripts/deploy.sh"],
                "acceptance_criteria": ["deploy.sh is executable"],
                "test_steps": ["bash scripts/deploy.sh --dry-run"],
                "prd_requirement": "",
            },
        ],
    }


@pytest.fixture
def sample_plan_file(tmp_path: Path, sample_plan_payload: dict[str, Any]) -> Path:
    """Write ``sample_plan_payload`` to a file and yield its path.

    Materialising the JSON on disk (rather than calling :func:`parse_plan`
    on a dict directly) is what makes this an *integration* test of the
    full ``import`` pipeline — the file-read step in :func:`parse_plan` is
    part of the surface we're exercising.
    """
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(sample_plan_payload), encoding="utf-8")
    return target


# ─── round-trip tests ────────────────────────────────────────────────────


def test_export_prints_canonical_json_to_stdout(
    db_pool: asyncpg.Pool,  # noqa: ARG001  — implicit DB readiness via fixture chain
    database_url: str,  # noqa: ARG001  — sets WHILLY_DATABASE_URL for run_plan_command
    sample_plan_file: Path,
    sample_plan_payload: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Import a plan, then ``export <plan_id>`` to capture stdout.

    Asserts:
      * exit code is :data:`EXIT_OK`;
      * stdout parses as JSON;
      * the parsed JSON has ``plan_id`` / ``project`` matching the input;
      * the task list size + ids match the input;
      * the output is the canonical shape (only the keys
        :func:`serialize_plan` emits — extra keys from the input are
        dropped, as the unit test in :mod:`tests.unit.test_plan_io`
        already proves for the in-memory case).
    """
    # Step 1: import — succeeds, populates the DB.
    rc = run_plan_command(["import", str(sample_plan_file)])
    assert rc == EXIT_OK, f"import returned {rc} (expected {EXIT_OK})"
    capsys.readouterr()  # discard import's stdout banner so export's output is clean.

    # Step 2: export — captures stdout.
    rc = run_plan_command(["export", "plan-roundtrip-001"])
    assert rc == EXIT_OK, f"export returned {rc} (expected {EXIT_OK})"

    captured = capsys.readouterr()
    # The success message lives on stderr (so stdout is pipeable into a
    # file). Asserting on this protects against a future refactor that
    # accidentally swaps the streams.
    assert "exported plan" in captured.err
    assert captured.out.strip(), "export must print non-empty JSON to stdout"

    payload = json.loads(captured.out)
    assert payload["plan_id"] == "plan-roundtrip-001"
    assert payload["project"] == "Roundtrip Workshop"
    assert isinstance(payload["tasks"], list)
    assert [t["id"] for t in payload["tasks"]] == ["T-001", "T-002", "T-003"]

    # Canonical shape: the export must contain *exactly* the keys
    # serialize_plan emits at the top level — no leakage of internal
    # columns (``created_at``, ``claimed_by``, ...) and no surface
    # leftovers from the input (``prd_file``, ``agent_instructions``).
    assert set(payload.keys()) == {"plan_id", "project", "tasks"}


def test_round_trip_import_export_import_is_idempotent(
    db_pool: asyncpg.Pool,  # noqa: ARG001
    database_url: str,  # noqa: ARG001
    sample_plan_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``import → export → import`` round-trips to ``==``-equal core models.

    The strongest form of the AC ("Round-trip import → export → import
    идемпотентен (diff пустой)"). We compare at the *model* level rather
    than byte-comparing JSON because:

    * JSON byte-equality is over-strict (key ordering, whitespace) — the
      contract we actually want is "the second import sees the same
      :class:`Plan` + tasks the first did";
    * model equality piggybacks on the frozen-dataclass ``__eq__`` from
      :mod:`whilly.core.models`, which is the equality semantics every
      consumer downstream of :func:`parse_plan` already uses.

    The test also asserts the second import does not raise — i.e. the
    canonical exported JSON is itself a valid v4 plan, closing the loop.
    """
    # First import: original file → DB.
    assert run_plan_command(["import", str(sample_plan_file)]) == EXIT_OK
    capsys.readouterr()

    # Export to a captured string.
    assert run_plan_command(["export", "plan-roundtrip-001"]) == EXIT_OK
    exported_json = capsys.readouterr().out

    # Stage the export back to a file so the parse path is identical to
    # the first leg.
    exported_file = tmp_path / "exported.json"
    exported_file.write_text(exported_json, encoding="utf-8")

    # Parse both files into core models. ``parse_plan`` is the canonical
    # consumer of v4 JSON — if it accepts both, an importer downstream
    # cannot tell them apart.
    original_plan, original_tasks = parse_plan(sample_plan_file)
    exported_plan, exported_tasks = parse_plan(exported_file)

    # Plan equality (id, name, tasks tuple) — frozen dataclass ``__eq__``.
    assert original_plan == exported_plan, (
        "round-trip altered the Plan: original vs exported differ. "
        f"original={original_plan!r}, exported={exported_plan!r}"
    )
    # Tasks list equality — same-order, same-content.
    assert original_tasks == exported_tasks, (
        f"round-trip altered the Task list. original={original_tasks!r}, exported={exported_tasks!r}"
    )

    # Re-import the exported file. Idempotent on the DB side because of
    # ``ON CONFLICT (id) DO NOTHING``; we only need the import command
    # itself to *succeed* (no validation errors, no SQL errors).
    assert run_plan_command(["import", str(exported_file)]) == EXIT_OK


def test_two_consecutive_exports_are_byte_identical(
    db_pool: asyncpg.Pool,  # noqa: ARG001
    database_url: str,  # noqa: ARG001
    sample_plan_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two ``export`` runs against the same plan produce identical stdout.

    This is the determinism corollary of the round-trip property: if
    two consecutive exports diverge (different key order, different task
    ordering, randomised whitespace), the round-trip JSON file would not
    be ``diff``-clean across snapshots — operationally important for any
    "git-track the plan snapshot" workflow.

    The export uses ``json.dump(..., sort_keys=True, indent=2)``, so a
    regression here surfaces as a literal byte diff in the assertion.
    """
    assert run_plan_command(["import", str(sample_plan_file)]) == EXIT_OK
    capsys.readouterr()

    assert run_plan_command(["export", "plan-roundtrip-001"]) == EXIT_OK
    first = capsys.readouterr().out

    assert run_plan_command(["export", "plan-roundtrip-001"]) == EXIT_OK
    second = capsys.readouterr().out

    assert first == second, (
        "two consecutive exports diverged — the export is not deterministic, "
        "round-tripped snapshots will produce noisy diffs"
    )


# ─── error paths ─────────────────────────────────────────────────────────


def test_export_missing_plan_id_returns_exit_2_with_helpful_message(
    db_pool: asyncpg.Pool,  # noqa: ARG001  — TRUNCATE makes the DB empty for this test.
    database_url: str,  # noqa: ARG001
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exporting a non-existent ``plan_id`` exits ``2`` with a message.

    AC verbatim: "Несуществующий plan_id → exit 2 с понятным сообщением".
    The message must (a) name the missing id and (b) tell the operator
    what to check, otherwise it's an opaque error from the user's POV.
    """
    rc = run_plan_command(["export", "no-such-plan-12345"])
    assert rc == EXIT_ENVIRONMENT_ERROR, f"expected exit code {EXIT_ENVIRONMENT_ERROR} for missing plan, got {rc}"

    captured = capsys.readouterr()
    # Stdout should be empty — no half-rendered JSON for a missing plan.
    assert captured.out == "", f"expected empty stdout on missing plan, got {captured.out!r}"
    # Stderr should mention the missing id and suggest the cause.
    assert "no-such-plan-12345" in captured.err, f"error message must name the missing plan id; got: {captured.err!r}"
    assert "not found" in captured.err.lower()


def test_export_without_database_url_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing ``WHILLY_DATABASE_URL`` exits ``2`` (same scheme as import).

    Distinguishable from the "plan not found" exit ``2`` only by the
    stderr message, which is fine — both are environment failures. The
    test does *not* depend on Docker (no DB connection attempted), so we
    skip the ``database_url`` fixture; this also doubles as a guard that
    the env-var check happens *before* the pool open.
    """
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)

    rc = run_plan_command(["export", "any-plan-id"])
    assert rc == EXIT_ENVIRONMENT_ERROR

    captured = capsys.readouterr()
    assert DATABASE_URL_ENV in captured.err, f"error message must name the missing env var; got: {captured.err!r}"
    assert captured.out == ""
