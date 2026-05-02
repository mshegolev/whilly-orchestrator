"""Integration test for VAL-CROSS-BACKCOMPAT-005 — `bash workshop-demo.sh
--cli stub` drains exactly 5 seeded tasks to DONE within the contracted
5-minute budget.

Round-4 finding (fix-m1-workshop-demo-5-tasks): even after the terminal-
state guard (fix-m1-workshop-demo-exit-code) ensured the demo exited 0
only when every task was DONE/FAILED/SKIPPED, the seeded plan still
contained only 2 tasks instead of 5. VAL-CROSS-BACKCOMPAT-005 explicitly
requires 5 DONE within 5 minutes for the stub demo. This test is the
end-to-end backstop: it spins up the real docker-compose demo with the
shim Claude CLI (no API keys required) and asserts the helper's summary
line reports `DONE=5`.

The test is heavy (docker build + multi-container start + 5x stub-Claude
sleeps); it skips cleanly when the Docker daemon is unavailable so
unit-test sweeps and laptop-no-docker contributors are not penalised.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SCRIPT = REPO_ROOT / "workshop-demo.sh"
PARALLEL_PLAN = REPO_ROOT / "examples" / "demo" / "parallel.json"
HELPER = REPO_ROOT / "scripts" / "check_demo_tasks_terminal.sh"

DEMO_TIMEOUT_SECONDS = 300

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="workshop-demo.sh is bash-only; no bash on this host",
)


def _docker_daemon_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        result = subprocess.run(
            [docker, "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


@pytest.fixture(scope="module")
def docker_available() -> bool:
    if not _docker_daemon_available():
        pytest.skip("Docker daemon unavailable — VAL-CROSS-BACKCOMPAT-005 cannot run end-to-end here")
    return True


def test_seed_plan_has_5_tasks() -> None:
    """Static assertion that the seeded parallel.json defines exactly 5 tasks.

    Even when Docker is unavailable, this guards against a future regression
    that re-shrinks the plan back to 2 (which is what triggered round-4 of
    user-testing in the first place).
    """
    import json

    plan = json.loads(PARALLEL_PLAN.read_text(encoding="utf-8"))
    assert plan["plan_id"] == "parallel"
    tasks = plan["tasks"]
    assert len(tasks) == 5, f"VAL-CROSS-BACKCOMPAT-005 requires 5 seeded tasks; got {len(tasks)}"
    statuses = {t["status"] for t in tasks}
    assert statuses == {"pending"}, f"all seeded tasks must start as PENDING; got {statuses}"
    ids = [t["id"] for t in tasks]
    assert len(set(ids)) == 5, f"task IDs must be unique; got {ids}"
    for task in tasks:
        assert task["dependencies"] == [], (
            f"all seeded tasks must be independent so two workers can drain them in parallel; "
            f"task {task['id']} has dependencies {task['dependencies']}"
        )


def test_demo_invokes_helper_with_min_done_5() -> None:
    """Static check that workshop-demo.sh wires the new --min-done 5 guard."""
    body = DEMO_SCRIPT.read_text(encoding="utf-8")
    assert "--min-done 5" in body, (
        "workshop-demo.sh does not pass --min-done 5 to check_demo_tasks_terminal.sh; "
        "VAL-CROSS-BACKCOMPAT-005 will silently regress"
    )
    assert "exit 5" in body, "workshop-demo.sh must propagate exit code 5 from the DONE-count guard"


def test_helper_min_done_mode_prints_summary() -> None:
    """The helper's --min-done mode prints both shapes the contract relies on:
    `DONE=N PENDING=N ...` (key=value, used by this integration test) AND
    `<n> DONE <n> PENDING` (used by VAL-CROSS-BACKCOMPAT-005)."""
    fixture = "PAR-001|DONE\nPAR-002|DONE\nPAR-003|DONE\nPAR-004|DONE\nPAR-005|DONE\n"
    result = subprocess.run(
        ["bash", str(HELPER), "--min-done", "5", "--plan", "parallel"],
        input=fixture,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "DONE=5" in result.stdout
    assert "PENDING=0" in result.stdout
    assert "5 DONE 0 PENDING" in result.stdout


def test_helper_min_done_mode_fails_when_done_below_threshold() -> None:
    """If every task is terminal but DONE < N, helper exits 5 with breakdown."""
    fixture = "PAR-001|DONE\nPAR-002|DONE\nPAR-003|FAILED\nPAR-004|FAILED\nPAR-005|SKIPPED\n"
    result = subprocess.run(
        ["bash", str(HELPER), "--min-done", "5"],
        input=fixture,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 5, result.stderr
    assert "DONE=2" in result.stdout
    assert "FAILED=2" in result.stdout
    assert "SKIPPED=1" in result.stdout


def test_workshop_demo_drains_5_tasks_done(docker_available: bool, tmp_path: Path) -> None:
    """End-to-end: `bash workshop-demo.sh --cli stub` exits 0 within 5 minutes
    AND the helper's summary line reports `DONE=5 PENDING=0`.

    This is the canonical assertion behind VAL-CROSS-BACKCOMPAT-005.
    """
    log_file = tmp_path / "workshop-demo.log"
    env = os.environ.copy()
    env["NO_COLOR"] = "1"

    cmd = ["bash", str(DEMO_SCRIPT), "--cli", "stub", "--no-color"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=DEMO_TIMEOUT_SECONDS,
            cwd=str(REPO_ROOT),
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log_file.write_text(
            f"=== TIMEOUT after {DEMO_TIMEOUT_SECONDS}s ===\n"
            f"=== stdout ===\n{exc.stdout or ''}\n"
            f"=== stderr ===\n{exc.stderr or ''}\n",
            encoding="utf-8",
        )
        pytest.fail(
            f"workshop-demo.sh did not finish within {DEMO_TIMEOUT_SECONDS}s; "
            f"see {log_file}. VAL-CROSS-BACKCOMPAT-005 contract violated."
        )

    log_file.write_text(
        f"=== exit code: {result.returncode} ===\n=== stdout ===\n{result.stdout}\n=== stderr ===\n{result.stderr}\n",
        encoding="utf-8",
    )

    assert result.returncode == 0, (
        f"workshop-demo.sh exit={result.returncode} (expected 0). See {log_file}.\nLast stderr: {result.stderr[-2000:]}"
    )

    combined = result.stdout + result.stderr

    assert "DONE=5" in combined, f"workshop-demo.sh did not report DONE=5 (VAL-CROSS-BACKCOMPAT-005). See {log_file}."
    assert "PENDING=0" in combined, (
        f"workshop-demo.sh did not report PENDING=0 (VAL-CROSS-BACKCOMPAT-005). See {log_file}."
    )

    short_form = re.search(r"\b5 DONE 0 PENDING\b", combined)
    assert short_form, (
        "workshop-demo.sh did not emit the contract-shaped `5 DONE 0 PENDING` line "
        f"required by VAL-CROSS-BACKCOMPAT-005. See {log_file}."
    )
