"""Wiring tests for OC-111 / OC-112 / OC-113.

Phase 1 shipped the AgentBackend Protocol + ClaudeBackend + OpenCodeBackend,
each unit-tested in isolation. The wiring work (ef9e7ca) plumbed backend
selection through the CLI, the tmux runner, and the Decision Gate. This
file pins down the *integration points* so regressions in any one of the
three surfaces fail loudly:

* CLI  — ``--agent {claude,opencode}`` sets config.AGENT_BACKEND and
         propagates ``WHILLY_AGENT_BACKEND`` into process env so children
         (tmux wrappers, subprocess agents) see it.
* tmux — ``launch_agent(backend=...)`` assembles argv from
         ``backend.build_command`` rather than hard-coded Claude args, and
         auto-resolves the active backend from env when ``backend=None``.
* gate — ``decision_gate._default_runner`` honors ``WHILLY_AGENT_BACKEND``
         via the shared ``active_backend_from_env`` helper, so OpenCode
         runs don't silently call Claude for the gate decision.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from whilly.agents import (
    DEFAULT_BACKEND,
    ClaudeBackend,
    OpenCodeBackend,
    active_backend_from_env,
)
from whilly.agents.base import AgentResult, AgentUsage


# ── Shared helper ──────────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure WHILLY_AGENT_BACKEND starts unset for each test."""
    monkeypatch.delenv("WHILLY_AGENT_BACKEND", raising=False)
    return monkeypatch


# ── active_backend_from_env (the shared env resolver) ─────────────────────────


class TestActiveBackendFromEnv:
    def test_default_is_claude_when_env_unset(self, clean_env):
        b = active_backend_from_env()
        assert isinstance(b, ClaudeBackend)
        assert DEFAULT_BACKEND == "claude"

    def test_opencode_when_env_set(self, clean_env):
        clean_env.setenv("WHILLY_AGENT_BACKEND", "opencode")
        assert isinstance(active_backend_from_env(), OpenCodeBackend)

    def test_claude_when_env_set(self, clean_env):
        clean_env.setenv("WHILLY_AGENT_BACKEND", "claude")
        assert isinstance(active_backend_from_env(), ClaudeBackend)

    def test_unknown_raises(self, clean_env):
        clean_env.setenv("WHILLY_AGENT_BACKEND", "bogus")
        with pytest.raises(ValueError, match="Unknown agent backend"):
            active_backend_from_env()

    def test_case_insensitive(self, clean_env):
        clean_env.setenv("WHILLY_AGENT_BACKEND", "OpenCode")
        assert isinstance(active_backend_from_env(), OpenCodeBackend)


# ── OC-111: CLI --agent flag parsing ──────────────────────────────────────────


class TestCLIAgentFlag:
    """cli.main parses ``--agent`` before dispatching; we don't need a full
    plan file — the flag must either succeed and be stripped (followed by
    plan discovery) or error out with a clear message. We stub plan
    discovery so the test stays isolated from the filesystem."""

    def test_agent_opencode_sets_env(self, clean_env, tmp_path, monkeypatch):
        plan = tmp_path / "tasks.json"
        plan.write_text(
            '{"project":"t","tasks":[{"id":"A","status":"done","description":"noop",'
            '"dependencies":[],"key_files":[],"priority":"low",'
            '"acceptance_criteria":[],"test_steps":[]}]}'
        )
        monkeypatch.chdir(tmp_path)
        # All tasks already done → run_plan exits before spawning agents.
        monkeypatch.setenv("WHILLY_HEADLESS", "1")

        from whilly.cli import main

        rc = main(["--agent", "opencode", str(plan)])

        assert rc == 0
        assert os.environ.get("WHILLY_AGENT_BACKEND") == "opencode"

    def test_agent_unknown_exits_nonzero(self, clean_env, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        from whilly.cli import main

        rc = main(["--agent", "bogus", "tasks.json"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "Unknown backend" in captured.out or "Unknown backend" in captured.err

    def test_agent_missing_value_exits_nonzero(self, clean_env, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        from whilly.cli import main

        # --agent at end without value
        rc = main(["--agent"])
        assert rc == 1


# ── OC-112: tmux_runner.launch_agent honors backend ───────────────────────────


class _FakeBackend:
    """Minimal AgentBackend stub — only what launch_agent touches."""

    name = "fake"

    def build_command(self, prompt, model=None, *, safe_mode=None):
        # Prompt MUST be the last element — tmux_runner depends on that invariant.
        return ["/opt/fake-bin", "--run", "--model", model or "fake-model", prompt]


class TestTmuxLaunchAgentBackend:
    def test_explicit_backend_used_to_build_wrapper(self, tmp_path):
        from whilly import tmux_runner

        captured_cmd = []

        def fake_run(cmd, *args, **kwargs):
            captured_cmd.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        fake_backend = _FakeBackend()

        with (
            patch.object(tmux_runner, "TMUX", "/usr/bin/tmux"),
            patch.object(tmux_runner.subprocess, "run", side_effect=fake_run),
        ):
            agent = tmux_runner.launch_agent(
                task_id="T1",
                prompt="do the thing",
                model="fake-model",
                log_dir=tmp_path,
                backend=fake_backend,
            )

        # Last call is new-session with zsh -ic wrapper.
        tmux_call = [c for c in captured_cmd if c[1:2] == ["new-session"]][0]
        wrapper = tmux_call[-1]
        # Wrapper must contain the backend's binary and model, NOT the literal prompt
        # (prompt comes via $(cat …)).
        assert "/opt/fake-bin" in wrapper
        assert "fake-model" in wrapper
        assert "do the thing" not in wrapper
        assert "$(cat" in wrapper
        assert agent.task_id == "T1"

    def test_no_backend_resolves_from_env(self, clean_env, tmp_path):
        """When backend=None, launch_agent must fall back to
        active_backend_from_env — not a hard-coded Claude instance."""
        from whilly import tmux_runner

        captured_cmd = []

        def fake_run(cmd, *args, **kwargs):
            captured_cmd.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        clean_env.setenv("WHILLY_AGENT_BACKEND", "opencode")

        with (
            patch.object(tmux_runner, "TMUX", "/usr/bin/tmux"),
            patch.object(tmux_runner.subprocess, "run", side_effect=fake_run),
        ):
            tmux_runner.launch_agent(
                task_id="T2",
                prompt="hi",
                model="claude-opus-4-6",  # OpenCode will normalize → anthropic/claude-opus-4-6
                log_dir=tmp_path,
            )

        tmux_call = [c for c in captured_cmd if c[1:2] == ["new-session"]][0]
        wrapper = tmux_call[-1]
        # OpenCode-shaped argv: `opencode run …` with normalized provider prefix.
        assert "opencode" in wrapper
        assert "anthropic/claude-opus-4-6" in wrapper

    def test_bad_backend_build_command_raises(self, tmp_path):
        """Guards the last-element-prompt invariant in launch_agent.
        A backend whose build_command doesn't place prompt last must be
        rejected with a clear RuntimeError — not a silent mis-quoted shell."""
        from whilly import tmux_runner

        class BadBackend:
            name = "bad"

            def build_command(self, prompt, model=None, *, safe_mode=None):
                return ["/fake", prompt, "--trailing-arg"]  # wrong order

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            return result

        with (
            patch.object(tmux_runner, "TMUX", "/usr/bin/tmux"),
            patch.object(tmux_runner.subprocess, "run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError, match="prompt as the last argv element"):
                tmux_runner.launch_agent(
                    task_id="T3",
                    prompt="hi",
                    model="m",
                    log_dir=tmp_path,
                    backend=BadBackend(),
                )


# ── OC-113: decision_gate._default_runner uses the env backend ────────────────


class TestDecisionGateBackendSelection:
    def test_default_runner_uses_active_env_backend(self, clean_env):
        """The default runner must route through active_backend_from_env,
        so flipping WHILLY_AGENT_BACKEND switches the gate's backend too."""
        from whilly import decision_gate

        clean_env.setenv("WHILLY_AGENT_BACKEND", "opencode")

        captured = {}
        fake_result = AgentResult(
            result_text='{"decision":"proceed","reason":"ok"}',
            usage=AgentUsage(cost_usd=0.0001),
            exit_code=0,
        )

        def fake_run(self, prompt, model=None, timeout=None, cwd=None):
            captured["backend"] = self.name
            captured["model"] = model
            return fake_result

        with patch.object(OpenCodeBackend, "run", fake_run), patch.object(ClaudeBackend, "run", fake_run):
            result = decision_gate._default_runner("prompt", "claude-haiku-4-5", 30)

        assert result is fake_result
        assert captured["backend"] == "opencode"

    def test_default_runner_falls_back_to_claude(self, clean_env):
        from whilly import decision_gate

        captured = {}
        fake_result = AgentResult(exit_code=0)

        def fake_run(self, prompt, model=None, timeout=None, cwd=None):
            captured["backend"] = self.name
            return fake_result

        with patch.object(ClaudeBackend, "run", fake_run), patch.object(OpenCodeBackend, "run", fake_run):
            decision_gate._default_runner("prompt", "claude-haiku-4-5", 30)

        assert captured["backend"] == "claude"
