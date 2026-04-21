"""Unit tests for whilly.agents.opencode.OpenCodeBackend.

Subprocess is mocked everywhere — no real OpenCode binary required. The
parser is stressed against the JSON shapes documented in the module's
``parse_output`` docstring (single object, top-level array, NDJSON,
embedded JSON in plaintext).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from whilly.agents.base import AgentBackend, COMPLETION_MARKER
from whilly.agents.opencode import OpenCodeBackend, DEFAULT_MODEL


# ── Protocol conformance ─────────────────────────────────────────────────────


class TestProtocol:
    def test_class_satisfies_protocol(self):
        backend: AgentBackend = OpenCodeBackend()
        assert backend.name == "opencode"

    def test_required_methods(self):
        b = OpenCodeBackend()
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
            assert callable(getattr(b, attr)), f"missing: {attr}"


# ── Model normalisation ─────────────────────────────────────────────────────


class TestNormalizeModel:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("claude-opus-4-6[1m]", "anthropic/claude-opus-4-6"),
            ("claude-sonnet-4-5", "anthropic/claude-sonnet-4-5"),
            ("anthropic/claude-haiku", "anthropic/claude-haiku"),
            ("openai/gpt-5", "openai/gpt-5"),
            ("gpt-5", "openai/gpt-5"),
            ("o3-mini", "openai/o3-mini"),
            ("gemini-2.5-pro", "google/gemini-2.5-pro"),
            ("llama-3.3-70b", "meta/llama-3.3-70b"),
            ("deepseek-r1", "deepseek/deepseek-r1"),
            ("qwen2.5-coder", "qwen/qwen2.5-coder"),
            ("custom-model", "custom-model"),  # unknown stays bare
            ("", DEFAULT_MODEL),  # empty falls back
        ],
    )
    def test_mappings(self, given, expected):
        assert OpenCodeBackend().normalize_model(given) == expected


class TestDefaultModel:
    def test_constant_when_no_env(self):
        with patch.dict("os.environ", {}, clear=False):
            import os as _os

            _os.environ.pop("WHILLY_MODEL", None)
            assert OpenCodeBackend().default_model() == DEFAULT_MODEL

    def test_env_override_normalised(self):
        with patch.dict("os.environ", {"WHILLY_MODEL": "claude-sonnet-4-5"}):
            assert OpenCodeBackend().default_model() == "anthropic/claude-sonnet-4-5"


# ── Command building ────────────────────────────────────────────────────────


class TestBuildCommand:
    def test_basic_shape(self):
        cmd = OpenCodeBackend().build_command("hello")
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert "--format" in cmd and "json" in cmd
        assert "--model" in cmd
        assert cmd[-1] == "hello"

    def test_skip_permissions_default(self):
        cmd = OpenCodeBackend().build_command("x")
        assert "--dangerously-skip-permissions" in cmd

    def test_safe_mode_omits_skip_flag(self):
        cmd = OpenCodeBackend().build_command("x", safe_mode=True)
        assert "--dangerously-skip-permissions" not in cmd

    def test_safe_mode_via_env(self):
        with patch.dict("os.environ", {"WHILLY_OPENCODE_SAFE": "1"}):
            cmd = OpenCodeBackend().build_command("x")
        assert "--dangerously-skip-permissions" not in cmd

    def test_explicit_model_normalised(self):
        cmd = OpenCodeBackend().build_command("x", model="claude-haiku-4-5")
        i = cmd.index("--model")
        assert cmd[i + 1] == "anthropic/claude-haiku-4-5"

    def test_bin_env_override(self):
        with patch.dict("os.environ", {"WHILLY_OPENCODE_BIN": "/opt/oc"}):
            cmd = OpenCodeBackend().build_command("x")
        assert cmd[0] == "/opt/oc"


# ── Parser shapes ───────────────────────────────────────────────────────────


class TestParseSingleObject:
    def test_full_summary_like_claude(self):
        payload = json.dumps(
            {
                "result": "all done",
                "total_cost_usd": 0.0123,
                "num_turns": 2,
                "duration_ms": 5000,
                "usage": {"input_tokens": 100, "output_tokens": 30, "cache_read_input_tokens": 10},
            }
        )
        text, usage = OpenCodeBackend().parse_output(payload)
        assert text == "all done"
        assert usage.cost_usd == pytest.approx(0.0123)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 30
        assert usage.cache_read_tokens == 10
        assert usage.num_turns == 2
        assert usage.duration_ms == 5000

    def test_alternate_field_names(self):
        payload = json.dumps({"output": "result text", "cost": 0.05})
        text, usage = OpenCodeBackend().parse_output(payload)
        assert text == "result text"
        assert usage.cost_usd == pytest.approx(0.05)


class TestParseTopLevelArray:
    def test_array_of_events(self):
        events = [
            {"type": "start"},
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
            {"type": "summary", "usage": {"input_tokens": 50, "output_tokens": 25}, "cost_usd": 0.001},
        ]
        text, usage = OpenCodeBackend().parse_output(json.dumps(events))
        assert "hello" in text and "world" in text
        assert usage.input_tokens == 50
        assert usage.output_tokens == 25
        assert usage.cost_usd == pytest.approx(0.001)


class TestParseNDJSON:
    def test_line_delimited(self):
        lines = [
            json.dumps({"type": "text", "text": "step 1"}),
            json.dumps({"type": "text", "text": "step 2"}),
            json.dumps({"type": "done", "total_cost_usd": 0.02}),
        ]
        text, usage = OpenCodeBackend().parse_output("\n".join(lines))
        assert "step 1" in text
        assert "step 2" in text
        assert usage.cost_usd == pytest.approx(0.02)

    def test_blank_lines_ignored(self):
        body = json.dumps({"text": "a"}) + "\n\n" + json.dumps({"text": "b"}) + "\n"
        text, _ = OpenCodeBackend().parse_output(body)
        assert "a" in text and "b" in text


class TestParseEmbeddedInPlaintext:
    def test_extracts_blob(self):
        body = f'starting...\n{{"type":"text","text":"the answer is 42 {COMPLETION_MARKER}"}}\ndone.'
        text, _ = OpenCodeBackend().parse_output(body)
        assert "42" in text
        # is_complete should fire on the marker.
        assert OpenCodeBackend().is_complete(text)


class TestParseDefensive:
    def test_empty_input(self):
        text, usage = OpenCodeBackend().parse_output("")
        assert text == ""
        assert usage.cost_usd == 0.0

    def test_garbage_returns_raw(self):
        text, usage = OpenCodeBackend().parse_output("not json at all")
        assert text == "not json at all"
        assert usage.cost_usd == 0.0

    def test_cost_missing_defaults_zero(self):
        body = json.dumps({"text": "hi"})
        _, usage = OpenCodeBackend().parse_output(body)
        assert usage.cost_usd == 0.0

    def test_anthropic_style_content_blocks(self):
        body = json.dumps(
            {
                "content": [
                    {"type": "text", "text": "first chunk"},
                    {"type": "text", "text": "second chunk"},
                ],
                "usage": {"input_tokens": 5, "output_tokens": 10},
            }
        )
        text, usage = OpenCodeBackend().parse_output(body)
        assert "first chunk" in text
        assert "second chunk" in text
        assert usage.input_tokens == 5

    def test_multiple_events_accumulate_cost(self):
        events = [
            json.dumps({"type": "tool", "cost_usd": 0.001}),
            json.dumps({"type": "tool", "cost_usd": 0.002}),
            json.dumps({"type": "done", "text": "ok"}),
        ]
        _, usage = OpenCodeBackend().parse_output("\n".join(events))
        assert usage.cost_usd == pytest.approx(0.003)


# ── is_complete ────────────────────────────────────────────────────────────


class TestIsComplete:
    def test_marker_present(self):
        assert OpenCodeBackend().is_complete(f"work {COMPLETION_MARKER}")

    def test_marker_missing(self):
        assert not OpenCodeBackend().is_complete("just text")


# ── run() with mocked subprocess ───────────────────────────────────────────


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRun:
    def test_happy_path(self):
        payload = json.dumps({"result": f"done {COMPLETION_MARKER}", "total_cost_usd": 0.05})
        with patch("whilly.agents.opencode.subprocess.run", return_value=_Proc(0, payload)):
            res = OpenCodeBackend().run("anything")
        assert res.exit_code == 0
        assert res.is_complete
        assert res.usage.cost_usd == pytest.approx(0.05)

    def test_timeout(self):
        import subprocess as sp

        with patch("whilly.agents.opencode.subprocess.run", side_effect=sp.TimeoutExpired("opencode", 1)):
            res = OpenCodeBackend().run("x", timeout=1)
        assert res.exit_code == -1
        assert "TIMEOUT" in res.result_text

    def test_binary_missing(self):
        with patch("whilly.agents.opencode.subprocess.run", side_effect=FileNotFoundError()):
            res = OpenCodeBackend().run("x")
        assert res.exit_code == -2
        assert "not found" in res.result_text


# ── collect_result_from_file ──────────────────────────────────────────────


class TestCollectFromFile:
    def test_missing_file(self, tmp_path: Path):
        res = OpenCodeBackend().collect_result_from_file(tmp_path / "nope.log")
        assert res.exit_code == -1

    def test_with_marker_and_exit_code(self, tmp_path: Path):
        log = tmp_path / "x.log"
        body = json.dumps({"result": f"done {COMPLETION_MARKER}", "cost_usd": 0.1}) + "\nEXIT_CODE=0\n"
        log.write_text(body)
        res = OpenCodeBackend().collect_result_from_file(log)
        assert res.exit_code == 0
        assert res.is_complete
        assert res.usage.cost_usd == pytest.approx(0.1)


# ── run_async preamble ───────────────────────────────────────────────────


class TestRunAsyncPreamble:
    def test_preamble_written(self, tmp_path: Path):
        log = tmp_path / "log.txt"

        with patch("whilly.agents.opencode.subprocess.Popen") as popen_mock:
            OpenCodeBackend().run_async("p", log_file=log)
        text = log.read_text(encoding="utf-8")
        assert "whilly agent preamble" in text
        assert "backend   : opencode" in text
        popen_mock.assert_called_once()


# ── Factory integration ────────────────────────────────────────────────────


class TestFactory:
    def test_get_backend_resolves(self):
        from whilly.agents import get_backend

        assert get_backend("claude").name == "claude"
        assert get_backend("opencode").name == "opencode"

    def test_get_backend_case_insensitive(self):
        from whilly.agents import get_backend

        assert get_backend("OPENCODE").name == "opencode"

    def test_get_backend_unknown_raises(self):
        from whilly.agents import get_backend

        with pytest.raises(ValueError, match="Unknown agent backend"):
            get_backend("aider")

    def test_available_backends_lists_both(self):
        from whilly.agents import available_backends

        names = available_backends()
        assert "claude" in names and "opencode" in names
