"""Unit tests for ``docker/llm_shim.py``.

Shim вызывается whilly-worker'ом как drop-in замена ``claude`` CLI:

    $CLAUDE_BIN --dangerously-skip-permissions --output-format json \\
                --model X -p "<prompt>"

И обязан выдавать на stdout single-envelope JSON совместимый с
``whilly.adapters.runner.result_parser.parse_output``. Эти тесты проверяют:

* Happy-path: успешный POST → правильный envelope (result, usage,
  total_cost_usd, num_turns, duration_ms).
* Auth fail (401/403): envelope содержит ``failed to authenticate`` —
  whilly классифицирует как permanent (no retry).
* Server error (5xx): envelope содержит ``API Error: 5xx`` — whilly
  классифицирует как retriable.
* Network error: ``API Error: <ClassName>`` — retriable.
* Malformed response: ``API Error: malformed response shape`` — retriable.
* Missing creds: ``failed to authenticate: missing LLM credentials`` —
  permanent (env vars не выставлены).
* Argv compatibility: shim принимает ``--dangerously-skip-permissions`` /
  ``--permission-mode`` / ``--output-format`` / ``--model`` без падения.
* Picker integration: если LLM_PROVIDER задан а LLM_MODEL пуст —
  вызывает pick_model.
* LLM_FORCE_COMPLETE: если включено и модель забыла marker — добавляет.
* OpenRouter headers: HTTP-Referer и X-Title прокидываются если заданы.

httpx мокаем через ``httpx.MockTransport`` — это идиоматический способ
из самой httpx-документации, не требует внешних либ типа responses/respx.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture(scope="module")
def shim():
    """Load docker/llm_shim.py as module via importlib (см. test_llm_resource_picker)."""
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "docker" / "llm_shim.py"
    spec = importlib.util.spec_from_file_location("llm_shim", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Гарантируем чистый env для каждого теста."""
    for var in (
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_PROVIDER",
        "LLM_TIMEOUT",
        "LLM_TEMPERATURE",
        "LLM_HTTP_REFERER",
        "LLM_X_TITLE",
        "LLM_FORCE_COMPLETE",
        "LLM_TIER_OVERRIDE",
    ):
        monkeypatch.delenv(var, raising=False)


def _ok_response(content: str, *, prompt_tokens=10, completion_tokens=20):
    """Минимальная OpenAI-shape success response."""
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def _install_mock_transport(monkeypatch, handler):
    """Подменяем httpx.Client чтобы он использовал MockTransport.

    Нельзя просто ``httpx.Client = ...``, потому что это readonly module-level.
    Вместо этого патчим конструктор Client'а внутри shim'а — он импортирует
    httpx как ``import httpx`` и вызывает ``httpx.Client(...)``.
    """
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


class TestHappyPath:
    def test_minimal_success(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://api.example/v1")
        monkeypatch.setenv("LLM_API_KEY", "test-key")

        captured_request: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_request["url"] = str(request.url)
            captured_request["headers"] = dict(request.headers)
            captured_request["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json=_ok_response(
                    "Done. <promise>COMPLETE</promise>",
                    prompt_tokens=42,
                    completion_tokens=84,
                ),
            )

        _install_mock_transport(monkeypatch, handler)

        rc = shim.main(
            [
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--model",
                "test-model",
                "-p",
                "do thing",
            ]
        )
        assert rc == 0

        out = capsys.readouterr().out
        envelope = json.loads(out)

        # Shape parser ждёт от Claude CLI:
        assert "<promise>COMPLETE</promise>" in envelope["result"]
        assert envelope["num_turns"] == 1
        assert isinstance(envelope["duration_ms"], int)
        assert envelope["usage"]["input_tokens"] == 42
        assert envelope["usage"]["output_tokens"] == 84
        assert envelope["usage"]["cache_read_input_tokens"] == 0
        assert envelope["usage"]["cache_creation_input_tokens"] == 0

        # Запрос — правильный URL и заголовок
        assert captured_request["url"] == "https://api.example/v1/chat/completions"
        assert captured_request["headers"]["authorization"] == "Bearer test-key"
        # System prompt + user prompt в payload
        msgs = captured_request["body"]["messages"]
        assert msgs[0]["role"] == "system"
        assert "<promise>COMPLETE</promise>" in msgs[0]["content"]
        assert msgs[1] == {"role": "user", "content": "do thing"}
        assert captured_request["body"]["model"] == "test-model"

    def test_optional_headers(self, shim, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
        monkeypatch.setenv("LLM_API_KEY", "or-key")
        monkeypatch.setenv("LLM_HTTP_REFERER", "https://github.com/whilly")
        monkeypatch.setenv("LLM_X_TITLE", "Whilly Demo")

        captured: dict = {}

        def handler(request):
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json=_ok_response("ok <promise>COMPLETE</promise>"))

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 0
        assert captured["headers"]["http-referer"] == "https://github.com/whilly"
        assert captured["headers"]["x-title"] == "Whilly Demo"


class TestErrors:
    def test_missing_creds_returns_2_and_emits_auth_error(self, shim, capsys):
        # Никакие env vars не выставлены (см. autouse reset_env)
        rc = shim.main(["-p", "x"])
        assert rc == 2

        out = capsys.readouterr().out
        envelope = json.loads(out)
        # Whilly's _is_auth_error matches "failed to authenticate" — permanent
        assert "failed to authenticate" in envelope["result"]

    def test_401_returns_auth_error(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "bad")

        def handler(_request):
            return httpx.Response(401, text="Unauthorized")

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 1

        envelope = json.loads(capsys.readouterr().out)
        assert "failed to authenticate" in envelope["result"]
        assert "401" in envelope["result"]

    def test_403_returns_auth_error(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")

        def handler(_request):
            return httpx.Response(403, text="Forbidden")

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 1

        envelope = json.loads(capsys.readouterr().out)
        # Both "failed to authenticate" AND "403" must be present so whilly's
        # _is_auth_error catches it via either substring.
        assert "failed to authenticate" in envelope["result"]
        assert "403" in envelope["result"]

    def test_500_returns_retriable_api_error(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")

        def handler(_request):
            return httpx.Response(500, text="boom")

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 1

        envelope = json.loads(capsys.readouterr().out)
        # Whilly's _is_retriable_error matches "api error: 500" (lower-cased)
        assert "API Error: 500" in envelope["result"]

    def test_network_error_returns_api_error(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")

        def handler(_request):
            raise httpx.ConnectError("DNS fail")

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 1

        envelope = json.loads(capsys.readouterr().out)
        assert "API Error" in envelope["result"]
        assert "ConnectError" in envelope["result"]

    def test_malformed_response_returns_api_error(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")

        def handler(_request):
            # 200 OK но без поля choices — модельная ошибка типа Groq quota
            return httpx.Response(200, json={"error": "rate limited"})

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 1

        envelope = json.loads(capsys.readouterr().out)
        assert "malformed response" in envelope["result"]


class TestArgvCompat:
    """Эти флаги whilly-worker всегда передаёт — shim не должен на них падать."""

    def test_accepts_full_claude_argv(self, shim, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")

        def handler(_request):
            return httpx.Response(200, json=_ok_response("<promise>COMPLETE</promise>"))

        _install_mock_transport(monkeypatch, handler)

        # Точная argv-форма из whilly.adapters.runner.claude_cli.build_command:
        rc = shim.main(
            [
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--model",
                "claude-opus-4-6[1m]",
                "-p",
                "long prompt with newlines\nand stuff",
            ]
        )
        assert rc == 0

    def test_accepts_safe_mode_argv(self, shim, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")

        def handler(_request):
            return httpx.Response(200, json=_ok_response("<promise>COMPLETE</promise>"))

        _install_mock_transport(monkeypatch, handler)

        # WHILLY_CLAUDE_SAFE=1 переключает worker на:
        rc = shim.main(
            [
                "--permission-mode",
                "acceptEdits",
                "--output-format",
                "json",
                "--model",
                "test",
                "-p",
                "x",
            ]
        )
        assert rc == 0


class TestPickerIntegration:
    def test_provider_without_model_invokes_picker(self, shim, monkeypatch, capsys):
        """LLM_PROVIDER + (no LLM_MODEL) → picker подбирает модель."""
        monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
        monkeypatch.setenv("LLM_API_KEY", "gsk_x")
        monkeypatch.setenv("LLM_PROVIDER", "groq")
        monkeypatch.setenv("LLM_TIER_OVERRIDE", "small")

        captured: dict = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_ok_response("<promise>COMPLETE</promise>"))

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 0
        # Picker для groq+small возвращает llama-3.1-8b-instant
        assert captured["body"]["model"] == "llama-3.1-8b-instant"

    def test_explicit_model_beats_provider_pick(self, shim, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")
        monkeypatch.setenv("LLM_PROVIDER", "groq")
        monkeypatch.setenv("LLM_MODEL", "my-special-model")
        monkeypatch.setenv("LLM_TIER_OVERRIDE", "small")

        captured: dict = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_ok_response("<promise>COMPLETE</promise>"))

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 0
        assert captured["body"]["model"] == "my-special-model"

    def test_unknown_provider_returns_permanent_error(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")
        monkeypatch.setenv("LLM_PROVIDER", "no-such-provider")
        # No LLM_MODEL — picker is invoked and bails on unknown provider

        rc = shim.main(["-p", "x"])
        assert rc == 2
        envelope = json.loads(capsys.readouterr().out)
        assert "failed to authenticate" in envelope["result"]


class TestForceComplete:
    def test_off_by_default_envelope_passes_through(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")

        def handler(_request):
            # Модель забыла COMPLETE marker
            return httpx.Response(200, json=_ok_response("just text, no marker"))

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 0
        envelope = json.loads(capsys.readouterr().out)
        # COMPLETE НЕ должен появиться (whilly retry-логика обработает)
        assert "<promise>COMPLETE</promise>" not in envelope["result"]
        assert envelope["result"] == "just text, no marker"

    def test_on_appends_marker_if_missing(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")
        monkeypatch.setenv("LLM_FORCE_COMPLETE", "1")

        def handler(_request):
            return httpx.Response(200, json=_ok_response("plain text reply"))

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 0
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["result"].endswith("<promise>COMPLETE</promise>")
        assert "plain text reply" in envelope["result"]

    def test_on_does_not_double_marker(self, shim, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")
        monkeypatch.setenv("LLM_FORCE_COMPLETE", "1")

        def handler(_request):
            return httpx.Response(
                200,
                json=_ok_response("ok done <promise>COMPLETE</promise>"),
            )

        _install_mock_transport(monkeypatch, handler)
        rc = shim.main(["-p", "x"])
        assert rc == 0
        envelope = json.loads(capsys.readouterr().out)
        # Не должен быть удвоен
        assert envelope["result"].count("<promise>COMPLETE</promise>") == 1
