"""Unit tests for ``docker/cli_adapter.py``.

Adapter — это shim, который whilly-worker зовёт как ``$CLAUDE_BIN`` для
agentic CLI'ев (claude-code / opencode / gemini). Внутри adapter спавнит
native CLI subprocess и парсит native output → whilly envelope.

Что покрываем:

* Dispatch by ``WHILLY_CLI`` env: пустой → permanent error, unknown →
  permanent error.
* claude-code passthrough: corretly invokes claude with whilly's argv,
  returns claude's stdout/stderr/returncode 1-в-1.
* opencode adapter: парсит JSONL stream, находит `result` event, конвертит
  в whilly envelope. Ошибки (exit ≠ 0, empty stream, no result event).
* gemini adapter: парсит single-JSON `{response, stats}` → envelope. Stats
  парсятся в input/output tokens. Exit 42 = permanent (input error),
  остальные ≠ 0 = retriable.
* COMPLETE marker: автоматически добавляется (default), отключается
  через ``WHILLY_FORCE_COMPLETE=0``.
* Argv compat: те же флаги whilly-worker'а (``-p``,
  ``--dangerously-skip-permissions``, ``--output-format json``,
  ``--model``, ``--permission-mode``).

subprocess.run мокаем через monkeypatch — реальные CLI'и в test environment
обычно отсутствуют (это unit-тест, не integration).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def adapter():
    """Load docker/cli_adapter.py as module via importlib."""
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "docker" / "cli_adapter.py"
    spec = importlib.util.spec_from_file_location("cli_adapter", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Чистый env для каждого теста."""
    for var in (
        "WHILLY_CLI",
        "WHILLY_FORCE_COMPLETE",
        "LLM_MODEL",
        "LLM_PROVIDER",
        "LLM_TIER_OVERRIDE",
    ):
        monkeypatch.delenv(var, raising=False)


def _mock_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Helper для подмены subprocess.run."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestDispatch:
    def test_no_cli_env_returns_permanent_error(self, adapter, capsys):
        rc = adapter.main(["-p", "task"])
        assert rc == 2
        out = capsys.readouterr().out
        envelope = json.loads(out)
        assert "WHILLY_CLI" in envelope["result"]
        assert "failed to authenticate" in envelope["result"]

    def test_unknown_cli_returns_permanent_error(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "not-a-real-cli")
        rc = adapter.main(["-p", "task"])
        assert rc == 2
        out = capsys.readouterr().out
        envelope = json.loads(out)
        assert "unknown WHILLY_CLI" in envelope["result"]


class TestClaudeCodePassthrough:
    def test_passthrough_invokes_claude_with_whilly_argv(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "claude-code")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _mock_run(
                stdout=json.dumps({"result": "done <promise>COMPLETE</promise>"}),
                returncode=0,
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc = adapter.main(
            [
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--model",
                "claude-sonnet-4-5",
                "-p",
                "do thing",
            ]
        )
        assert rc == 0

        # Adapter должен вызвать `claude` с теми же ключевыми флагами
        cmd = captured["cmd"]
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "json"
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "claude-sonnet-4-5"
        assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "do thing"

        # Stdout from claude pass through unchanged
        out = capsys.readouterr().out
        envelope = json.loads(out)
        assert "<promise>COMPLETE</promise>" in envelope["result"]

    def test_claude_binary_missing_returns_permanent_error(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "claude-code")
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(side_effect=FileNotFoundError("claude not found")),
        )
        rc = adapter.main(["-p", "x"])
        assert rc == 2
        envelope = json.loads(capsys.readouterr().out)
        assert "failed to authenticate" in envelope["result"]

    def test_claude_timeout_returns_retriable(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "claude-code")
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=600)),
        )
        rc = adapter.main(["-p", "x"])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert "timeout" in envelope["result"]


class TestOpencodeAdapter:
    def test_jsonl_stream_with_result_event(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "opencode")
        events = [
            {"event": "init", "session_id": "s1"},
            {"event": "message", "content": "Working..."},
            {"event": "tool_use", "tool": "edit"},
            {
                "event": "result",
                "result": "Task done.",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "cost_usd": 0.005,
            },
        ]
        stdout = "\n".join(json.dumps(e) for e in events)

        def fake_run(cmd, **kwargs):
            assert cmd[0] == "opencode"
            assert "run" in cmd
            assert "--format" in cmd and cmd[cmd.index("--format") + 1] == "json"
            return _mock_run(stdout=stdout, returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc = adapter.main(["-p", "do thing"])
        assert rc == 0

        envelope = json.loads(capsys.readouterr().out)
        assert "Task done." in envelope["result"]
        assert envelope["usage"]["input_tokens"] == 100
        assert envelope["usage"]["output_tokens"] == 50
        assert envelope["total_cost_usd"] == 0.005
        # COMPLETE marker auto-added
        assert "<promise>COMPLETE</promise>" in envelope["result"]

    def test_provider_model_format_passed_through(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "opencode")
        monkeypatch.setenv("LLM_MODEL", "openrouter/meta-llama/llama-3.3-70b:free")

        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _mock_run(stdout=json.dumps({"event": "result", "result": "ok"}), returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc = adapter.main(["-p", "x"])
        assert rc == 0
        cmd = captured["cmd"]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "openrouter/meta-llama/llama-3.3-70b:free"

    def test_non_provider_model_filtered_out(self, adapter, monkeypatch):
        """OpenCode требует ``provider/model``; модель без слэша не передаём."""
        monkeypatch.setenv("WHILLY_CLI", "opencode")
        monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")  # no slash

        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _mock_run(stdout=json.dumps({"event": "result", "result": "ok"}), returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter.main(["-p", "x"])
        # --model без слэша игнорим — opencode возьмёт свой default
        assert "--model" not in captured["cmd"]

    def test_empty_response_returns_retriable(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "opencode")
        monkeypatch.setattr(subprocess, "run", MagicMock(return_value=_mock_run(stdout="", returncode=0)))
        rc = adapter.main(["-p", "x"])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert "empty response" in envelope["result"]

    def test_non_zero_exit_returns_retriable(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "opencode")
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(return_value=_mock_run(stdout="", stderr="API error", returncode=1)),
        )
        rc = adapter.main(["-p", "x"])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert "exit 1" in envelope["result"]

    def test_message_fallback_when_no_result_event(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "opencode")
        events = [
            {"event": "init"},
            {"event": "message", "content": "First chunk. "},
            {"event": "message", "content": "Second chunk."},
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        monkeypatch.setattr(subprocess, "run", MagicMock(return_value=_mock_run(stdout=stdout, returncode=0)))
        rc = adapter.main(["-p", "x"])
        assert rc == 0
        envelope = json.loads(capsys.readouterr().out)
        assert "First chunk." in envelope["result"]
        assert "Second chunk." in envelope["result"]

    def test_garbage_lines_in_stream_are_skipped(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "opencode")
        # opencode иногда пишет ANSI / debug перед JSONL — игнорируем
        stdout = (
            "DEBUG: starting\n\x1b[32mGreen text\x1b[0m\n" + json.dumps({"event": "result", "result": "Done."}) + "\n"
        )
        monkeypatch.setattr(subprocess, "run", MagicMock(return_value=_mock_run(stdout=stdout, returncode=0)))
        rc = adapter.main(["-p", "x"])
        assert rc == 0
        envelope = json.loads(capsys.readouterr().out)
        assert "Done." in envelope["result"]


class TestGeminiAdapter:
    def test_single_json_response(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "gemini")
        gemini_output = {
            "response": "Task complete.",
            "stats": {"models": {"gemini-2.0-flash": {"tokens": {"prompt": 50, "candidates": 25}}}},
        }

        def fake_run(cmd, **kwargs):
            assert cmd[0] == "gemini"
            assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "do thing"
            assert "--output-format" in cmd
            return _mock_run(stdout=json.dumps(gemini_output), returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc = adapter.main(["-p", "do thing"])
        assert rc == 0

        envelope = json.loads(capsys.readouterr().out)
        assert "Task complete." in envelope["result"]
        assert envelope["usage"]["input_tokens"] == 50
        assert envelope["usage"]["output_tokens"] == 25

    def test_input_error_42_returns_permanent(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "gemini")
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(return_value=_mock_run(stdout="", stderr="Bad prompt", returncode=42)),
        )
        rc = adapter.main(["-p", "x"])
        assert rc == 2  # permanent — bad input
        envelope = json.loads(capsys.readouterr().out)
        assert "input error" in envelope["result"]

    def test_general_error_returns_retriable(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "gemini")
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(return_value=_mock_run(stdout="", stderr="API down", returncode=1)),
        )
        rc = adapter.main(["-p", "x"])
        assert rc == 1  # retriable
        envelope = json.loads(capsys.readouterr().out)
        assert "exit 1" in envelope["result"]

    def test_malformed_json_returns_retriable(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "gemini")
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(return_value=_mock_run(stdout="not json at all", returncode=0)),
        )
        rc = adapter.main(["-p", "x"])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert "malformed JSON" in envelope["result"]


class TestForceComplete:
    def test_default_on_appends_marker(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "gemini")
        # Gemini не даёт COMPLETE marker — adapter должен добавить
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(return_value=_mock_run(stdout=json.dumps({"response": "Plain reply"}), returncode=0)),
        )
        rc = adapter.main(["-p", "x"])
        assert rc == 0
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["result"].endswith("<promise>COMPLETE</promise>")

    def test_off_passes_through(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "gemini")
        monkeypatch.setenv("WHILLY_FORCE_COMPLETE", "0")
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(return_value=_mock_run(stdout=json.dumps({"response": "No marker"}), returncode=0)),
        )
        rc = adapter.main(["-p", "x"])
        assert rc == 0
        envelope = json.loads(capsys.readouterr().out)
        # Без force-complete — marker не добавляется
        assert "<promise>COMPLETE</promise>" not in envelope["result"]

    def test_on_does_not_double_marker(self, adapter, capsys, monkeypatch):
        monkeypatch.setenv("WHILLY_CLI", "opencode")
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(
                return_value=_mock_run(
                    stdout=json.dumps(
                        {
                            "event": "result",
                            "result": "Done. <promise>COMPLETE</promise>",
                        }
                    ),
                    returncode=0,
                )
            ),
        )
        rc = adapter.main(["-p", "x"])
        assert rc == 0
        envelope = json.loads(capsys.readouterr().out)
        # marker не дублируется
        assert envelope["result"].count("<promise>COMPLETE</promise>") == 1


class TestArgvCompat:
    """Все argv-формы, которые whilly-worker когда-либо передаёт."""

    @pytest.mark.parametrize(
        "argv",
        [
            # Default mode (WHILLY_CLAUDE_SAFE != 1)
            [
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--model",
                "claude-opus-4-6[1m]",
                "-p",
                "task description",
            ],
            # Safe mode
            [
                "--permission-mode",
                "acceptEdits",
                "--output-format",
                "json",
                "--model",
                "test",
                "-p",
                "x",
            ],
            # Long prompt with newlines and special chars
            [
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--model",
                "m",
                "-p",
                'Multi-line\nprompt with\ttabs and "quotes"',
            ],
        ],
    )
    def test_no_crash_on_any_known_argv(self, adapter, monkeypatch, argv):
        monkeypatch.setenv("WHILLY_CLI", "gemini")
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(return_value=_mock_run(stdout=json.dumps({"response": "ok"}), returncode=0)),
        )
        rc = adapter.main(argv)
        assert rc == 0


class TestModelResolution:
    """Какую модель в итоге попросил adapter у CLI."""

    def test_explicit_non_default_model_wins(self, adapter, monkeypatch):
        """Если whilly передал нестандартную модель явно — используем её."""
        monkeypatch.setenv("WHILLY_CLI", "claude-code")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _mock_run(stdout=json.dumps({"result": "ok"}), returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter.main(
            [
                "--output-format",
                "json",
                "--model",
                "claude-haiku-4-5",  # явный non-default
                "-p",
                "x",
            ]
        )
        cmd = captured["cmd"]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-haiku-4-5"

    def test_default_model_yields_to_picker(self, adapter, monkeypatch):
        """Если whilly передал свой default — picker берёт верх."""
        monkeypatch.setenv("WHILLY_CLI", "gemini")
        monkeypatch.setenv("LLM_MODEL", "gemini-2.0-flash-lite")  # из picker'а
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _mock_run(stdout=json.dumps({"response": "ok"}), returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter.main(
            [
                "--output-format",
                "json",
                "--model",
                "claude-opus-4-6[1m]",  # whilly's DEFAULT_MODEL
                "-p",
                "x",
            ]
        )
        # picker'овская модель должна победить дефолт whilly
        cmd = captured["cmd"]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gemini-2.0-flash-lite"
