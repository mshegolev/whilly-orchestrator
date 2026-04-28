"""Unit tests for whilly.agents.claude.ClaudeBackend.

Mirrors the legacy whilly.agent_runner test surface (subprocess mocked) and
adds Protocol-conformance checks. The legacy module is kept as a compat-shim
in a separate task; here we exercise the new class directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from whilly.agents.base import AgentBackend, COMPLETION_MARKER
from whilly.agents.claude import ClaudeBackend, DEFAULT_MODEL


# ── Protocol conformance ──────────────────────────────────────────────────────


class TestProtocol:
    def test_class_satisfies_protocol(self):
        backend: AgentBackend = ClaudeBackend()  # type-checks at runtime via duck-typing
        assert backend.name == "claude"

    def test_required_methods_present(self):
        b = ClaudeBackend()
        for attr in (
            "default_model",
            "normalize_model",
            "build_command",
            "parse_output",
            "is_complete",
            "run",
            "run_async",
            "collect_result",
            "collect_result_from_file",
        ):
            assert callable(getattr(b, attr)), f"missing method: {attr}"


# ── Model handling ────────────────────────────────────────────────────────────


class TestModel:
    def test_default_model_constant(self):
        b = ClaudeBackend()
        # WHILLY_MODEL env may override; ensure the constant is the fallback.
        with patch.dict("os.environ", {}, clear=False):
            import os as _os

            _os.environ.pop("WHILLY_MODEL", None)
            assert b.default_model() == DEFAULT_MODEL

    def test_normalize_passes_through(self):
        b = ClaudeBackend()
        assert b.normalize_model("claude-opus-4-6[1m]") == "claude-opus-4-6[1m]"
        assert b.normalize_model("custom") == "custom"


# ── Command building ─────────────────────────────────────────────────────────


class TestBuildCommand:
    def test_basic_shape(self):
        cmd = ClaudeBackend().build_command("hello")
        assert cmd[0] == "claude"
        # stream-json + --verbose: live JSONL events visible via `tail -f`.
        assert "--output-format" in cmd and "stream-json" in cmd
        assert "--verbose" in cmd
        assert "--model" in cmd
        assert "-p" in cmd
        assert cmd[-1] == "hello"

    def test_default_skip_permissions(self):
        cmd = ClaudeBackend().build_command("x")
        assert "--dangerously-skip-permissions" in cmd
        assert "acceptEdits" not in cmd

    def test_safe_mode_uses_accept_edits(self):
        cmd = ClaudeBackend().build_command("x", safe_mode=True)
        assert "--permission-mode" in cmd
        assert "acceptEdits" in cmd
        assert "--dangerously-skip-permissions" not in cmd

    def test_safe_mode_via_env(self):
        cmd_off = ClaudeBackend().build_command("x")
        with patch.dict("os.environ", {"WHILLY_CLAUDE_SAFE": "1"}):
            cmd_on = ClaudeBackend().build_command("x")
        assert "--dangerously-skip-permissions" in cmd_off
        assert "acceptEdits" in cmd_on

    def test_explicit_model(self):
        cmd = ClaudeBackend().build_command("x", model="claude-sonnet-4")
        i = cmd.index("--model")
        assert cmd[i + 1] == "claude-sonnet-4"

    def test_claude_bin_env_override(self):
        with patch.dict("os.environ", {"CLAUDE_BIN": "/opt/claude"}):
            cmd = ClaudeBackend().build_command("x")
        assert cmd[0] == "/opt/claude"


# ── Output parsing ────────────────────────────────────────────────────────────


def _claude_payload(result_text: str = "ok", cost: float = 0.0042) -> str:
    return json.dumps(
        {
            "result": result_text,
            "total_cost_usd": cost,
            "num_turns": 3,
            "duration_ms": 12345,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
        }
    )


class TestParseOutput:
    def test_full_payload(self):
        text, usage = ClaudeBackend().parse_output(_claude_payload("hello world", 0.5))
        assert text == "hello world"
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.cache_read_tokens == 10
        assert usage.cache_create_tokens == 5
        assert usage.cost_usd == pytest.approx(0.5)
        assert usage.num_turns == 3
        assert usage.duration_ms == 12345

    def test_empty_input(self):
        text, usage = ClaudeBackend().parse_output("")
        assert text == ""
        assert usage.cost_usd == 0.0

    def test_malformed_json_falls_back_to_raw(self):
        text, usage = ClaudeBackend().parse_output("not json at all")
        assert text == "not json at all"
        assert usage.cost_usd == 0.0

    def test_missing_optional_fields(self):
        minimal = json.dumps({"result": "x"})
        text, usage = ClaudeBackend().parse_output(minimal)
        assert text == "x"
        assert usage.input_tokens == 0
        assert usage.cost_usd == 0.0


def _stream_payload(result_text: str = "ok", cost: float = 0.0042) -> str:
    """Mimic real Claude CLI ``--output-format stream-json`` output (JSONL)."""
    lines = [
        {"type": "system", "subtype": "init", "session_id": "abc"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": result_text}]},
            "session_id": "abc",
        },
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result_text,
            "total_cost_usd": cost,
            "num_turns": 3,
            "duration_ms": 12345,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
        },
    ]
    return "\n".join(json.dumps(obj) for obj in lines) + "\n"


class TestParseStreamJson:
    """``--output-format stream-json`` produces JSONL with a final type=result event."""

    def test_picks_final_result_event(self):
        text, usage = ClaudeBackend().parse_output(_stream_payload("hello stream", cost=0.5))
        assert text == "hello stream"
        assert usage.cost_usd == pytest.approx(0.5)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.cache_read_tokens == 10
        assert usage.cache_create_tokens == 5
        assert usage.num_turns == 3
        assert usage.duration_ms == 12345

    def test_ignores_non_result_events(self):
        # Only system + assistant deltas, no result event yet (mid-stream).
        partial = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
            ]
        )
        text, usage = ClaudeBackend().parse_output(partial)
        # No result event → falls back to raw text + zero usage.
        assert "system" in text  # raw stream returned verbatim
        assert usage.cost_usd == 0.0

    def test_blank_lines_in_stream_are_skipped(self):
        payload = _stream_payload("ok") + "\n\n"
        text, usage = ClaudeBackend().parse_output(payload)
        assert text == "ok"
        assert usage.cost_usd > 0.0

    def test_garbage_lines_are_ignored(self):
        payload = "garbage line\n" + _stream_payload("done", cost=0.1) + "more garbage\n"
        text, usage = ClaudeBackend().parse_output(payload)
        assert text == "done"
        assert usage.cost_usd == pytest.approx(0.1)


# ── Completion detection ─────────────────────────────────────────────────────


class TestIsComplete:
    def test_marker_present(self):
        assert ClaudeBackend().is_complete(f"work done {COMPLETION_MARKER}")

    def test_marker_missing(self):
        assert not ClaudeBackend().is_complete("just text")

    def test_empty(self):
        assert not ClaudeBackend().is_complete("")
        assert not ClaudeBackend().is_complete(None)  # type: ignore[arg-type]


# ── run() with mocked subprocess ─────────────────────────────────────────────


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRun:
    def test_happy_path(self):
        payload = _claude_payload(f"all good {COMPLETION_MARKER}", cost=0.1)
        with patch("whilly.agents.claude.subprocess.run", return_value=_Proc(0, payload)):
            res = ClaudeBackend().run("anything")
        assert res.exit_code == 0
        assert res.is_complete
        assert res.usage.cost_usd == pytest.approx(0.1)
        assert "all good" in res.result_text

    def test_timeout(self):
        import subprocess as sp

        with patch("whilly.agents.claude.subprocess.run", side_effect=sp.TimeoutExpired("claude", 1)):
            res = ClaudeBackend().run("x", timeout=1)
        assert res.exit_code == -1
        assert "TIMEOUT" in res.result_text
        assert not res.is_complete

    def test_binary_missing(self):
        with patch("whilly.agents.claude.subprocess.run", side_effect=FileNotFoundError()):
            res = ClaudeBackend().run("x")
        assert res.exit_code == -2
        assert "not found" in res.result_text

    def test_non_zero_exit_returns_raw_when_unparseable(self):
        with patch("whilly.agents.claude.subprocess.run", return_value=_Proc(1, "", "boom")):
            res = ClaudeBackend().run("x")
        assert res.exit_code == 1
        assert "boom" in res.result_text


# ── collect_result_from_file ─────────────────────────────────────────────────


class TestCollectFromFile:
    def test_missing_file(self, tmp_path: Path):
        res = ClaudeBackend().collect_result_from_file(tmp_path / "nope.log")
        assert res.exit_code == -1

    def test_with_exit_code_marker(self, tmp_path: Path):
        log = tmp_path / "x.log"
        body = _claude_payload(f"done {COMPLETION_MARKER}") + "\nEXIT_CODE=0\n"
        log.write_text(body)
        res = ClaudeBackend().collect_result_from_file(log)
        assert res.exit_code == 0
        assert res.is_complete

    def test_invalid_exit_code_marker_ignored(self, tmp_path: Path):
        log = tmp_path / "x.log"
        log.write_text(_claude_payload("ok") + "\nEXIT_CODE=garbage\n")
        res = ClaudeBackend().collect_result_from_file(log)
        # Should not crash — just leaves exit_code at default 0.
        assert res.exit_code == 0


# ── run_async preamble ──────────────────────────────────────────────────────


class TestRunAsyncPreamble:
    def test_preamble_written_before_spawn(self, tmp_path: Path):
        log = tmp_path / "log.txt"

        with patch("whilly.agents.claude.subprocess.Popen") as popen_mock:
            ClaudeBackend().run_async("hello prompt", log_file=log)
        assert log.exists()
        content = log.read_text(encoding="utf-8")
        assert "whilly agent preamble" in content
        assert "backend   : claude" in content
        popen_mock.assert_called_once()


# ── EAGAIN retry on Popen ──────────────────────────────────────────────────


class TestRunAsyncEagainRetry:
    """Regression: one EAGAIN from fork/posix_spawn shouldn't kill the plan.

    macOS raises ``BlockingIOError(errno=35)`` from ``subprocess.Popen`` when
    RLIMIT_NPROC is momentarily exhausted. We retry with backoff instead of
    propagating the first transient failure.
    """

    def test_retries_and_succeeds(self, tmp_path: Path, monkeypatch):
        log = tmp_path / "log.txt"
        monkeypatch.setattr("whilly.agents.base.time.sleep", lambda *_: None)

        call_count = {"n": 0}

        def fake_popen(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise BlockingIOError(35, "Resource temporarily unavailable")
            return object()  # sentinel — base helper just returns this through

        with patch("whilly.agents.claude.subprocess.Popen", side_effect=fake_popen):
            ClaudeBackend().run_async("p", log_file=log)

        assert call_count["n"] == 3

    def test_reraises_after_all_retries(self, tmp_path: Path, monkeypatch):
        log = tmp_path / "log.txt"
        monkeypatch.setattr("whilly.agents.base.time.sleep", lambda *_: None)

        def always_eagain(*_a, **_kw):
            raise BlockingIOError(35, "Resource temporarily unavailable")

        with patch("whilly.agents.claude.subprocess.Popen", side_effect=always_eagain):
            with pytest.raises(BlockingIOError) as excinfo:
                ClaudeBackend().run_async("p", log_file=log)
        assert excinfo.value.errno == 35
