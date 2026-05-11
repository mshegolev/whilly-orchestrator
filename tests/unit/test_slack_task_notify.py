from __future__ import annotations

from pathlib import Path
from typing import Any

from whilly.adapters.runner.result_parser import AgentResult, AgentUsage
from whilly.llm_ops import LLMOpsSession
from whilly.slack_task_notify import (
    API_BASE_URL_ENV,
    CHANNEL_ENV,
    CHANNEL_URL_ENV,
    DEFAULT_DEMO_CHANNEL_ID,
    ENABLED_ENV,
    EVENTS_ENV,
    LEGACY_CHANNEL_ENV,
    LEGACY_TOKEN_ENV,
    LEGACY_WEBHOOK_ENV,
    PUBLIC_BASE_URL_ENV,
    TOKEN_ENV,
    WEBHOOK_ENV,
    notify_slack_task_started,
    notify_slack_task_terminal,
)


def _session(tmp_path: Path) -> LLMOpsSession:
    return LLMOpsSession(
        task_id="PAR-001",
        plan_id="demo",
        worker_id="worker-1",
        attempt=2,
        session_id="demo/PAR-001/attempt-2",
        provider="opencode",
        model="opencode/big-pickle",
        artifact_dir=tmp_path / "tasks" / "PAR-001" / "attempt-2",
        prompt_path=tmp_path / "tasks" / "PAR-001" / "attempt-2" / "prompt.txt",
        raw_log_path=tmp_path / "tasks" / "PAR-001" / "attempt-2" / "raw.jsonl",
        final_path=tmp_path / "tasks" / "PAR-001" / "attempt-2" / "final.txt",
        summary_path=tmp_path / "tasks" / "PAR-001" / "attempt-2" / "summary.json",
        compat_prompt_path=tmp_path / "PAR-001_prompt.txt",
        compat_log_path=tmp_path / "PAR-001.log",
        events_path=tmp_path / "tasks" / "PAR-001.events.jsonl",
        started_at="2026-05-06T00:00:00+00:00",
    )


def _result() -> AgentResult:
    return AgentResult(
        output="<promise>COMPLETE</promise>",
        usage=AgentUsage(cost_usd=0.012, duration_ms=1234),
        exit_code=0,
        is_complete=True,
    )


def test_no_webhook_is_noop(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def http_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_s: float) -> None:
        calls.append((url, headers, payload, timeout_s))

    monkeypatch.delenv(WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(LEGACY_WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(LEGACY_TOKEN_ENV, raising=False)
    monkeypatch.setenv(EVENTS_ENV, "all")

    notify_slack_task_started(_session(tmp_path), http_post=http_post)
    notify_slack_task_terminal(_session(tmp_path), "DONE", _result(), http_post=http_post)

    assert calls == []


def test_default_mode_sends_terminal_only(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def http_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_s: float) -> None:
        calls.append((url, headers, payload, timeout_s))

    monkeypatch.setenv(WEBHOOK_ENV, "https://hooks.slack.test/services/demo")
    monkeypatch.delenv(EVENTS_ENV, raising=False)
    monkeypatch.setenv(PUBLIC_BASE_URL_ENV, "http://demo.local:8000")

    session = _session(tmp_path)
    notify_slack_task_started(session, http_post=http_post)
    notify_slack_task_terminal(session, "DONE", _result(), http_post=http_post)

    assert len(calls) == 1
    url, headers, payload, timeout_s = calls[0]
    assert url == "https://hooks.slack.test/services/demo"
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert timeout_s == 5.0
    assert "Whilly DONE PAR-001" in payload["text"]
    assert "logs=http://demo.local:8000/llm-ops?task_id=PAR-001" in payload["text"]


def test_all_mode_sends_started(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def http_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_s: float) -> None:
        calls.append((url, headers, payload, timeout_s))

    monkeypatch.setenv(WEBHOOK_ENV, "https://hooks.slack.test/services/demo")
    monkeypatch.setenv(EVENTS_ENV, "all")

    notify_slack_task_started(_session(tmp_path), http_post=http_post)

    assert len(calls) == 1
    assert "Whilly STARTED PAR-001" in calls[0][2]["text"]
    assert "model=opencode/big-pickle" in calls[0][2]["text"]


def test_disabled_mode_skips_events(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def http_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_s: float) -> None:
        calls.append((url, headers, payload, timeout_s))

    monkeypatch.setenv(WEBHOOK_ENV, "https://hooks.slack.test/services/demo")
    monkeypatch.setenv(EVENTS_ENV, "none")

    notify_slack_task_started(_session(tmp_path), http_post=http_post)
    notify_slack_task_terminal(_session(tmp_path), "FAILED", _result(), reason="boom", http_post=http_post)

    assert calls == []


def test_slack_enabled_kill_switch_skips_events(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def http_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_s: float) -> None:
        calls.append((url, headers, payload, timeout_s))

    monkeypatch.setenv(WEBHOOK_ENV, "https://hooks.slack.test/services/demo")
    monkeypatch.setenv(EVENTS_ENV, "all")
    monkeypatch.setenv(ENABLED_ENV, "0")

    notify_slack_task_started(_session(tmp_path), http_post=http_post)

    assert calls == []


def test_transport_errors_are_swallowed(monkeypatch, tmp_path: Path) -> None:
    def http_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_s: float) -> None:
        raise OSError("network down")

    monkeypatch.setenv(WEBHOOK_ENV, "https://hooks.slack.test/services/demo")

    notify_slack_task_terminal(_session(tmp_path), "DONE", _result(), http_post=http_post)


def test_bot_token_fallback_uses_default_demo_channel(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def http_post(
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        calls.append((url, headers, payload, timeout_s))
        return {"ok": True}

    monkeypatch.delenv(WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(LEGACY_WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(CHANNEL_ENV, raising=False)
    monkeypatch.delenv(LEGACY_CHANNEL_ENV, raising=False)
    monkeypatch.delenv(CHANNEL_URL_ENV, raising=False)
    monkeypatch.setenv(TOKEN_ENV, "xoxb-demo")
    monkeypatch.setenv(API_BASE_URL_ENV, "https://slack.test/api")

    notify_slack_task_terminal(_session(tmp_path), "DONE", _result(), http_post=http_post)

    assert len(calls) == 1
    url, headers, payload, timeout_s = calls[0]
    assert url == "https://slack.test/api/chat.postMessage"
    assert headers["Authorization"] == "Bearer xoxb-demo"
    assert payload["channel"] == DEFAULT_DEMO_CHANNEL_ID
    assert "Whilly DONE PAR-001" in payload["text"]
    assert timeout_s == 5.0


def test_channel_url_can_override_default_channel(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def http_post(
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        calls.append((url, headers, payload, timeout_s))
        return {"ok": True}

    monkeypatch.delenv(WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(LEGACY_WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(CHANNEL_ENV, raising=False)
    monkeypatch.delenv(LEGACY_CHANNEL_ENV, raising=False)
    monkeypatch.setenv(TOKEN_ENV, "xoxb-demo")
    monkeypatch.setenv(
        CHANNEL_URL_ENV,
        "https://app.slack.com/client/T0B1R7WABFY/C0B1WT58EBE?entry_point=redirect_flow",
    )

    notify_slack_task_terminal(_session(tmp_path), "DONE", _result(), http_post=http_post)

    assert calls[0][2]["channel"] == "C0B1WT58EBE"


def test_channel_value_can_be_full_slack_url(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def http_post(
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        calls.append((url, headers, payload, timeout_s))
        return {"ok": True}

    monkeypatch.delenv(WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(LEGACY_WEBHOOK_ENV, raising=False)
    monkeypatch.setenv(TOKEN_ENV, "xoxb-demo")
    monkeypatch.setenv(
        CHANNEL_ENV,
        "https://app.slack.com/client/T0B1R7WABFY/C0B1WT58EBE?entry_point=redirect_flow",
    )

    notify_slack_task_terminal(_session(tmp_path), "DONE", _result(), http_post=http_post)

    assert calls[0][2]["channel"] == "C0B1WT58EBE"
