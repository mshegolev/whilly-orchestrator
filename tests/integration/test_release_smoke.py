"""Whilly v4.0 release smoke (TASK-034a, PRD release gate).

Single pytest that prosecutes all six PRD Success Criteria in order. A
green run on the release commit is the gate that lets v4.0 ship — a
red run blocks the tag. Each SC is its own ``test_sc_<n>_*`` function so
a pytest failure points at the specific contract that broke, and so the
release-checklist (TASK-034b) can quote exact node ids in its links.

Why a single file rather than running each SC's existing suite:
    SC-1..SC-3 already have their own e2e gates
    (test_concurrent_claims, test_phase6_resilience, test_phase5_remote).
    SC-5 / SC-6 are static-analysis gates that don't fit pytest's
    fixture model cleanly. Composing them all here gives the release
    a *single* pytest invocation that prints "release SC contracts:
    6 passed" — the exact line a release-checklist links to. Without
    this composer the checklist would have to enumerate six unrelated
    pytest invocations, multiplying false-failure surface (one of
    them flakes on Docker pull → release blocked spuriously).

What each SC means
------------------
* SC-1 (concurrent claims): N workers race for K tasks; every task
  ends up assigned to exactly one worker, no double-assignments.
  Already gated by ``tests/integration/test_concurrent_claims.py``.
* SC-2 (worker kill recovery): a SIGKILL'd worker's claimed task
  comes back to PENDING via the visibility-timeout sweep + dashboard
  reflects truthfully throughout. Already gated by
  ``tests/integration/test_phase6_resilience.py``.
* SC-3 (remote worker over HTTP): a separate OS process talking to
  the control plane via TCP claims and completes a task. Already
  gated by ``tests/integration/test_phase5_remote.py`` plus the
  operator-facing ``docs/demo-remote-worker.sh``.
* SC-4 (cycle rejection): ``whilly plan import`` refuses to admit a
  cyclic dependency graph. Already gated by
  ``tests/integration/test_phase3_dag.py``.
* SC-5 (core coverage ≥ 80%): static analysis. ``coverage report
  --include='whilly/core/*' --fail-under=80`` is a hard gate.
* SC-6 (core import purity): static analysis. ``lint-imports``
  contract ``core-purity`` blocks ``whilly.core`` from importing
  asyncpg / httpx / fastapi / subprocess / uvicorn / alembic.

Execution order matters
-----------------------
SC-1..SC-3 are runtime gates — they need the testcontainers Postgres,
the seeded plan, and (for SC-3) a subprocess. SC-4 also needs the
postgres fixture but is fast. SC-5 / SC-6 are pure static analysis —
no Postgres needed. We run static gates *first* so a release commit
that introduces a SC-6 violation fails in <5 seconds rather than
making the operator wait through 60 seconds of testcontainers
bootstrap to discover it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import DOCKER_REQUIRED

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


# ─── SC-5: coverage ≥ 80% on whilly.core ──────────────────────────────────


def test_sc_5_core_coverage_at_least_eighty_percent() -> None:
    """SC-5 (PRD NFR-4): pure domain layer is at least 80% covered.

    Runs ``coverage`` in a subprocess so the release smoke matches the
    CI gate exactly. We intentionally do NOT rely on the in-process
    ``coverage.Coverage`` API: the CI shell command is what an operator
    re-runs locally to reproduce a release failure, and ``subprocess``
    is the cheapest way to keep them identical.

    The data file lives in a tmp_path so a developer who has a stale
    ``.coverage`` from earlier work doesn't accidentally pass this
    gate on cached numbers.
    """
    if shutil.which("coverage") is None:
        pytest.skip("coverage CLI not on PATH; install with pip install coverage")

    env = {**os.environ, "COVERAGE_FILE": str(REPO_ROOT / ".coverage.release_smoke")}
    try:
        # Step 1: collect coverage by running the unit tests that exercise
        # whilly.core specifically. tests/unit/test_state_machine.py +
        # test_scheduler.py + test_prompts.py are authored to 100% on the
        # legacy whilly/core/ surface; tests/unit/core/test_gates.py
        # (TASK-104c, PR #223) and tests/unit/core/test_triz.py (TASK-104b,
        # PR #224) cover the v4.1 additions whilly/core/gates.py and
        # whilly/core/triz.py — without them the SC-5 gate trips at 71%
        # because triz.py shows 0% (90 stmts) and gates.py shows ~49%.
        run_args = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--source=whilly/core",
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_state_machine.py",
            "tests/unit/test_scheduler.py",
            "tests/unit/test_prompts.py",
            "tests/unit/core/test_gates.py",
            "tests/unit/core/test_triz.py",
        ]
        run_result = subprocess.run(
            run_args,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert run_result.returncode == 0, (
            f"coverage run failed (exit {run_result.returncode})\n"
            f"stdout:\n{run_result.stdout}\nstderr:\n{run_result.stderr}"
        )

        # Step 2: enforce the gate.
        report_args = [
            sys.executable,
            "-m",
            "coverage",
            "report",
            "--include=whilly/core/*",
            "--fail-under=80",
        ]
        report_result = subprocess.run(
            report_args,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert report_result.returncode == 0, (
            f"coverage gate failed: report exit {report_result.returncode}\n"
            f"stdout:\n{report_result.stdout}\nstderr:\n{report_result.stderr}"
        )
    finally:
        # Don't litter the repo with a smoke-test data file. CI artifact
        # upload comes from the production test job, not from this gate.
        coverage_file = Path(env["COVERAGE_FILE"])
        if coverage_file.exists():
            coverage_file.unlink()


# ─── SC-6: import-linter core-purity contract ─────────────────────────────


def test_sc_6_lint_imports_core_purity_kept() -> None:
    """SC-6 (PRD TC-8): ``whilly.core`` imports no I/O / transport modules.

    Runs ``lint-imports`` in a subprocess — same path the
    ``arch-guard`` CI job takes (TASK-029). A regression here means
    someone re-introduced an asyncpg / httpx / fastapi / subprocess /
    uvicorn / alembic import inside ``whilly.core``, breaking the
    Hexagonal boundary.
    """
    # Run lint-imports via `python -m importlinter` so the smoke matches
    # the venv's installed import-linter version *and* doesn't require
    # `.venv/bin` to be on PATH (subprocess inherits the test's PATH,
    # which on a `pytest` invocation from `python -m pytest` may not
    # include the venv's scripts dir).
    result = subprocess.run(
        [sys.executable, "-m", "importlinter.cli", "lint"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"lint-imports core-purity contract broken (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Belt-and-suspenders for SC-6: import-linter blocks *imports*, not
    # stdlib `os` call sites. This grep mirrors the second arch-guard CI
    # step (TASK-029) and would catch e.g. an os.chdir() smuggled into
    # whilly/core/scheduler.py for "convenience".
    grep_result = subprocess.run(
        [
            "grep",
            "-rnE",
            r"\bos\.(chdir|getcwd)\b",
            "whilly/core/",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    # grep returns 1 on "no matches" — that's the success path.
    assert grep_result.returncode == 1, (
        f"whilly/core/ contains forbidden os.chdir / os.getcwd call sites:\n{grep_result.stdout}"
    )


# ─── SC-1..SC-4: runtime gates piggy-back on existing focused suites ──────

# Why piggy-back rather than re-implement
# ---------------------------------------
# Each individual SC has a focused suite that knows how to seed its
# own state, fake any subprocess it needs, and assert the SC's contract.
# Re-implementing them inline here would add ~1200 lines of duplicated
# fixture wiring without changing what the release smoke validates.
# Instead each SC test below shells out to pytest invoking that one
# suite — the smoke fails iff the suite fails, and the failure message
# carries the inner pytest's stdout for triage.

_SC_RUNTIME_GATES = (
    pytest.param(
        "SC-1",
        ("tests/integration/test_concurrent_claims.py",),
        id="sc_1_concurrent_claims",
    ),
    pytest.param(
        "SC-2",
        ("tests/integration/test_phase6_resilience.py",),
        id="sc_2_worker_kill_recovery",
    ),
    pytest.param(
        "SC-3",
        ("tests/integration/test_phase5_remote.py",),
        id="sc_3_remote_worker_http",
    ),
    pytest.param(
        "SC-4",
        ("tests/integration/test_phase3_dag.py",),
        id="sc_4_cycle_rejection",
    ),
)


@pytest.mark.parametrize(("sc_label", "suite_paths"), _SC_RUNTIME_GATES)
def test_sc_runtime_gate_runs_green(
    sc_label: str,
    suite_paths: tuple[str, ...],
) -> None:
    """Run the SC's existing focused suite via subprocess pytest.

    We invoke ``python -m pytest`` (not ``pytest``) so the smoke runs
    against the *same* interpreter / venv that's running this test —
    a developer who skipped ``pip install -e .`` for ``pytest`` on
    PATH but has whilly importable inside ``.venv`` still gets a
    correct run instead of a confusing missing-module error.
    """
    pytest_args = [sys.executable, "-m", "pytest", "-q", *suite_paths]
    result = subprocess.run(
        pytest_args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"{sc_label} runtime gate failed (exit {result.returncode})\n"
        f"--- inner pytest stdout ---\n{result.stdout}\n"
        f"--- inner pytest stderr ---\n{result.stderr}\n"
        f"--- end ---"
    )


pytestmark = DOCKER_REQUIRED  # SC-1..SC-4 all need testcontainers Postgres
