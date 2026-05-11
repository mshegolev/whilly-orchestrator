"""Verify VAL-CROSS-READY-905: resource-aware pytest parallelism cap.

The mission validator (VAL-CROSS-READY-905) greps `.github/workflows/`
for `pytest -n` and the `Makefile` for `WHILLY_PYTEST_PARALLEL` to
confirm a literal cap is configured — see
``mission.md`` §5 ("`pytest -n auto` capped at `cpus / 2` for
testcontainers RAM headroom").

These tests assert the same invariants from the source tree so a
regression that drops the flag or env var is caught locally before
the validator runs. They skip cleanly when the repo layout isn't
present (e.g. running from a packaged sdist install with no
``.github/`` directory).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
MAKEFILE_PATH = REPO_ROOT / "Makefile"


def _read_or_skip(path: Path, what: str) -> str:
    if not path.exists():
        pytest.skip(f"{what} not present at {path} — running outside a checkout")
    return path.read_text(encoding="utf-8")


def test_workflows_dir_exists_or_skip() -> None:
    if not WORKFLOWS_DIR.is_dir():
        pytest.skip(f".github/workflows/ not present at {WORKFLOWS_DIR}")
    assert any(WORKFLOWS_DIR.glob("*.yml")), ".github/workflows/ has no YAML workflow files"


def test_ci_workflow_documents_parallelism_cap() -> None:
    """At least one workflow must contain the literal cap pattern."""
    if not WORKFLOWS_DIR.is_dir():
        pytest.skip(f".github/workflows/ not present at {WORKFLOWS_DIR}")

    yamls = list(WORKFLOWS_DIR.glob("*.yml")) + list(WORKFLOWS_DIR.glob("*.yaml"))
    if not yamls:
        pytest.skip("no workflow YAML files to inspect")

    combined = "\n".join(p.read_text(encoding="utf-8") for p in yamls)
    assert "pytest -n" in combined, (
        "VAL-CROSS-READY-905: expected literal `pytest -n` cap in at least one "
        "workflow under .github/workflows/, found none"
    )
    assert "--maxprocesses=4" in combined, (
        "VAL-CROSS-READY-905: expected literal `--maxprocesses=4` cap in at least one "
        "workflow under .github/workflows/, found none"
    )


def test_ci_workflow_has_oom_rationale_comment() -> None:
    """The cap must be documented with a comment so future readers understand the why."""
    if not WORKFLOWS_DIR.is_dir():
        pytest.skip(f".github/workflows/ not present at {WORKFLOWS_DIR}")

    ci_yml = WORKFLOWS_DIR / "ci.yml"
    if not ci_yml.exists():
        pytest.skip("ci.yml not present")

    text = ci_yml.read_text(encoding="utf-8").lower()
    assert "oom" in text or "memory" in text or "ram" in text, (
        "expected ci.yml to comment on the OOM/memory rationale for the parallelism cap"
    )


def test_makefile_honors_whilly_pytest_parallel_env_var() -> None:
    text = _read_or_skip(MAKEFILE_PATH, "Makefile")
    assert "WHILLY_PYTEST_PARALLEL" in text, (
        "VAL-CROSS-READY-905: Makefile must reference WHILLY_PYTEST_PARALLEL (env var consumed by the `test` target)"
    )


def test_makefile_default_parallelism_is_four() -> None:
    text = _read_or_skip(MAKEFILE_PATH, "Makefile")
    assert "WHILLY_PYTEST_PARALLEL ?= 4" in text or "WHILLY_PYTEST_PARALLEL?=4" in text, (
        "VAL-CROSS-READY-905: Makefile must default WHILLY_PYTEST_PARALLEL to 4 "
        "(conservative resource-aware default per AGENTS.md / mission §5)"
    )


def test_makefile_test_target_uses_parallelism_cap() -> None:
    text = _read_or_skip(MAKEFILE_PATH, "Makefile")
    assert "--maxprocesses=$(WHILLY_PYTEST_PARALLEL)" in text, (
        "VAL-CROSS-READY-905: Makefile `test` target must pass "
        "`--maxprocesses=$(WHILLY_PYTEST_PARALLEL)` to pytest so the env var "
        "actually caps xdist worker count"
    )
