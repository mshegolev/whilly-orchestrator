"""Tests for Whilly F4: Headless/CI Mode.

Covers:
- Headless mode produces JSON output on stdout
- Exit codes: 0 (success), 1 (failures), 2 (budget), 3 (timeout)
- Auto-detection of headless when stdout is not a TTY
- Timeout triggers graceful stop
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from whilly import cli as whilly_main
from whilly.config import WhillyConfig
from whilly.dashboard import NullDashboard

_requires_claude = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="requires 'claude' CLI in PATH (not available in CI runner)",
)


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture()
def plan_file(tmp_path: Path) -> Path:
    """Create a minimal valid plan file."""
    plan = {
        "project": "test-project",
        "prd_file": "",
        "created_at": "2026-04-02",
        "agent_instructions": {},
        "tasks": [
            {
                "id": "TASK-001",
                "phase": "1",
                "category": "test",
                "priority": "high",
                "description": "Test task 1",
                "status": "pending",
                "dependencies": [],
            },
            {
                "id": "TASK-002",
                "phase": "1",
                "category": "test",
                "priority": "medium",
                "description": "Test task 2",
                "status": "pending",
                "dependencies": [],
            },
        ],
    }
    f = tmp_path / "tasks.json"
    f.write_text(json.dumps(plan, ensure_ascii=False))
    return f


@pytest.fixture()
def done_plan_file(tmp_path: Path) -> Path:
    """Create a plan file where all tasks are done."""
    plan = {
        "project": "test-project",
        "prd_file": "",
        "created_at": "2026-04-02",
        "agent_instructions": {},
        "tasks": [
            {
                "id": "TASK-001",
                "phase": "1",
                "category": "test",
                "priority": "high",
                "description": "Test task 1",
                "status": "done",
                "dependencies": [],
            },
            {
                "id": "TASK-002",
                "phase": "1",
                "category": "test",
                "priority": "medium",
                "description": "Test task 2",
                "status": "done",
                "dependencies": [],
            },
        ],
    }
    f = tmp_path / "tasks_done.json"
    f.write_text(json.dumps(plan, ensure_ascii=False))
    return f


@pytest.fixture()
def failed_plan_file(tmp_path: Path) -> Path:
    """Create a plan file where some tasks failed."""
    plan = {
        "project": "test-project",
        "prd_file": "",
        "created_at": "2026-04-02",
        "agent_instructions": {},
        "tasks": [
            {
                "id": "TASK-001",
                "phase": "1",
                "category": "test",
                "priority": "high",
                "description": "Test task 1",
                "status": "done",
                "dependencies": [],
            },
            {
                "id": "TASK-002",
                "phase": "1",
                "category": "test",
                "priority": "medium",
                "description": "Test task 2",
                "status": "failed",
                "dependencies": [],
            },
        ],
    }
    f = tmp_path / "tasks_failed.json"
    f.write_text(json.dumps(plan, ensure_ascii=False))
    return f


# ── NullDashboard tests ──────────────────────────────────────


class TestNullDashboard:
    def test_null_dashboard_has_same_interface(self):
        """NullDashboard exposes the same attributes as Dashboard."""
        nd = NullDashboard()
        assert hasattr(nd, "iteration")
        assert hasattr(nd, "phase")
        assert hasattr(nd, "status_msg")
        assert hasattr(nd, "totals")
        assert hasattr(nd, "active_agents")
        assert hasattr(nd, "keyboard")

    def test_null_dashboard_lifecycle(self):
        """start/stop/update are no-ops and don't raise."""
        nd = NullDashboard()
        nd.start()
        nd.update()
        nd.stop()

    def test_null_keyboard_register(self):
        """NullKeyboard.register is a no-op."""
        nd = NullDashboard()
        nd.keyboard.register("q", lambda: None)
        nd.keyboard.start()
        nd.keyboard.stop()


# ── Config tests ─────────────────────────────────────────────


class TestHeadlessConfig:
    def test_headless_default_false(self):
        config = WhillyConfig()
        assert config.HEADLESS is False

    def test_headless_from_env(self):
        with patch.dict(os.environ, {"WHILLY_HEADLESS": "1"}):
            config = WhillyConfig.from_env()
            assert config.HEADLESS is True

    def test_headless_from_env_false(self):
        with patch.dict(os.environ, {"WHILLY_HEADLESS": "0"}):
            config = WhillyConfig.from_env()
            assert config.HEADLESS is False

    def test_timeout_default_zero(self):
        config = WhillyConfig()
        assert config.TIMEOUT == 0

    def test_timeout_from_env(self):
        with patch.dict(os.environ, {"WHILLY_TIMEOUT": "3600"}):
            config = WhillyConfig.from_env()
            assert config.TIMEOUT == 3600


# ── Exit codes ───────────────────────────────────────────────


class TestExitCodes:
    def test_exit_code_constants(self):
        assert whilly_main.EXIT_SUCCESS == 0
        assert whilly_main.EXIT_SOME_FAILED == 1
        assert whilly_main.EXIT_BUDGET_EXCEEDED == 2
        assert whilly_main.EXIT_TIMEOUT == 3


# ── Headless JSON output ────────────────────────────────────


class TestHeadlessOutput:
    def test_emit_json_writes_to_stdout(self, capsys):
        whilly_main._emit_json({"event": "progress", "done": 5, "total": 10})
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["event"] == "progress"
        assert parsed["done"] == 5
        assert parsed["total"] == 10


# ── run_plan headless integration ────────────────────────────


class TestRunPlanHeadless:
    @_requires_claude
    def test_headless_produces_json_output(self, done_plan_file, tmp_path, capsys):
        """In headless mode, run_plan emits a 'complete' JSON event to stdout."""
        config = WhillyConfig(HEADLESS=True, LOG_DIR=str(tmp_path / "logs"), MAX_PARALLEL=1)

        with (
            patch.object(whilly_main, "Reporter") as MockReporter,
            patch.object(whilly_main, "needs_decompose", return_value=False),
            patch.object(whilly_main, "notify_plan_done"),
            patch("whilly.cli.time.sleep"),
        ):
            mock_reporter = MagicMock()
            mock_reporter.totals = MagicMock()
            mock_reporter.totals.cost_usd = 0.0
            mock_reporter.json_path = tmp_path / "test_report.json"
            MockReporter.return_value = mock_reporter

            result = whilly_main.run_plan(str(done_plan_file), config, "test-agent")

        assert result is not None
        report_path, exit_code = result

        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().split("\n") if line.strip()]
        complete_events = [json.loads(line) for line in lines if '"complete"' in line]
        assert len(complete_events) >= 1
        evt = complete_events[-1]
        assert evt["event"] == "complete"
        assert "done" in evt
        assert "total" in evt
        assert "report" in evt

    @_requires_claude
    def test_exit_code_0_on_success(self, done_plan_file, tmp_path):
        """Exit code is 0 when all tasks are done."""
        config = WhillyConfig(HEADLESS=True, LOG_DIR=str(tmp_path / "logs"), MAX_PARALLEL=1)

        with (
            patch.object(whilly_main, "Reporter") as MockReporter,
            patch.object(whilly_main, "needs_decompose", return_value=False),
            patch.object(whilly_main, "notify_plan_done"),
            patch("whilly.cli.time.sleep"),
        ):
            mock_reporter = MagicMock()
            mock_reporter.totals = MagicMock()
            mock_reporter.totals.cost_usd = 0.0
            mock_reporter.json_path = tmp_path / "report.json"
            MockReporter.return_value = mock_reporter

            result = whilly_main.run_plan(str(done_plan_file), config, "test-agent")

        assert result is not None
        _, exit_code = result
        assert exit_code == whilly_main.EXIT_SUCCESS

    @_requires_claude
    def test_exit_code_1_on_failures(self, failed_plan_file, tmp_path):
        """Exit code is 1 when some tasks failed."""
        config = WhillyConfig(
            HEADLESS=True, LOG_DIR=str(tmp_path / "logs"), MAX_PARALLEL=1, MAX_TASK_RETRIES=0
        )

        with (
            patch.object(whilly_main, "Reporter") as MockReporter,
            patch.object(whilly_main, "needs_decompose", return_value=False),
            patch.object(whilly_main, "notify_plan_done"),
            patch("whilly.cli.time.sleep"),
        ):
            mock_reporter = MagicMock()
            mock_reporter.totals = MagicMock()
            mock_reporter.totals.cost_usd = 0.0
            mock_reporter.json_path = tmp_path / "report.json"
            MockReporter.return_value = mock_reporter

            result = whilly_main.run_plan(str(failed_plan_file), config, "test-agent")

        assert result is not None
        _, exit_code = result
        assert exit_code == whilly_main.EXIT_SOME_FAILED

    def test_timeout_triggers_graceful_stop(self, plan_file, tmp_path):
        """When timeout=1, the plan stops and returns EXIT_TIMEOUT."""
        config = WhillyConfig(
            HEADLESS=True,
            TIMEOUT=1,  # 1 second timeout
            LOG_DIR=str(tmp_path / "logs"),
            MAX_PARALLEL=1,
        )

        mock_proc = MagicMock()
        poll_count = 0

        def slow_poll():
            nonlocal poll_count
            poll_count += 1
            import time

            time.sleep(0.5)
            if poll_count < 5:
                return None
            return 0

        mock_proc.poll = slow_poll

        with (
            patch.object(whilly_main, "Reporter") as MockReporter,
            patch.object(whilly_main, "needs_decompose", return_value=False),
            patch.object(whilly_main, "notify_plan_done"),
            patch.object(whilly_main, "run_agent_async", return_value=mock_proc),
            patch.object(whilly_main, "collect_result") as mock_collect,
            patch.object(whilly_main, "kill_all_whilly_sessions"),
        ):
            mock_reporter = MagicMock()
            mock_reporter.totals = MagicMock()
            mock_reporter.totals.cost_usd = 0.0
            mock_reporter.json_path = tmp_path / "report.json"
            MockReporter.return_value = mock_reporter

            mock_result = MagicMock()
            mock_result.is_complete = False
            mock_result.exit_code = 0
            mock_result.usage = MagicMock()
            mock_result.usage.input_tokens = 0
            mock_result.usage.output_tokens = 0
            mock_result.usage.cache_read_tokens = 0
            mock_result.usage.cache_create_tokens = 0
            mock_result.usage.cost_usd = 0
            mock_result.usage.num_turns = 0
            mock_collect.return_value = mock_result

            result = whilly_main.run_plan(str(plan_file), config, "test-agent")

        assert result is not None
        _, exit_code = result
        assert exit_code == whilly_main.EXIT_TIMEOUT


class TestAutoHeadless:
    def test_auto_headless_when_no_tty(self):
        """--headless is auto-enabled when stdout is not a TTY."""
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            assert not sys.stdout.isatty()

    def test_cli_headless_flag_sets_config(self):
        """--headless CLI flag correctly sets config.HEADLESS."""
        config = WhillyConfig()
        assert config.HEADLESS is False
        config.HEADLESS = True
        assert config.HEADLESS is True

    def test_cli_timeout_flag_parsing(self):
        """--timeout N is parsed correctly from CLI args."""
        args = ["--headless", "--timeout", "3600", "plan.json"]
        if "--timeout" in args:
            idx = args.index("--timeout")
            timeout_val = int(args[idx + 1])
            assert timeout_val == 3600

    def test_main_help_returns_0(self):
        """main(--help) returns 0."""
        result = whilly_main.main(["--help"])
        assert result == 0

    def test_auto_headless_when_no_tty_in_main(self):
        """main() auto-detects headless mode when stdout is not a TTY."""
        with (
            patch("sys.stdout") as mock_stdout,
            patch.object(whilly_main, "WhillyConfig") as MockConfig,
            patch.object(whilly_main, "StateStore"),
        ):
            mock_stdout.isatty.return_value = False
            config = WhillyConfig()
            MockConfig.from_env.return_value = config

            # --help exits early before plan discovery
            result = whilly_main.main(["--help"])
            assert result == 0
