from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from whilly.adapters.runner.result_parser import AgentResult, AgentUsage
from whilly.core.models import Plan, Task, TaskStatus
from whilly.llm_ops import (
    LLM_RAW_LOG_PATH_ENV,
    LLM_RUN_FINISHED_EVENT_TYPE,
    LLM_RUN_STARTED_EVENT_TYPE,
    LLM_SESSION_ID_ENV,
    finish_llm_session,
    session_event_payload,
    start_llm_session,
)


@pytest.fixture(autouse=True)
def clean_otel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "WHILLY_LLM_OPS_EXPORTERS",
        "WHILLY_LLM_OPS_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "LANGFUSE_OTEL_ENDPOINT",
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "PHOENIX_COLLECTOR_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)


def _task() -> Task:
    return Task(id="PAR-001", status=TaskStatus.IN_PROGRESS, version=2, description="demo task")


def _plan() -> Plan:
    return Plan(id="demo", name="Demo")


def test_llm_session_writes_prompt_events_and_summary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("WHILLY_CLI", "opencode")
    monkeypatch.setenv("WHILLY_MODEL", "opencode/big-pickle")

    session = start_llm_session(_task(), _plan(), "worker-1", "do the work", attempt=2)

    assert session.prompt_path.read_text() == "do the work"
    assert session.compat_prompt_path.read_text() == "do the work"
    assert session.provider == "opencode"
    assert session.model == "opencode/big-pickle"

    events = [json.loads(line) for line in session.events_path.read_text().splitlines()]
    assert events[-1]["event"] == LLM_RUN_STARTED_EVENT_TYPE

    result = AgentResult(
        output="done <promise>COMPLETE</promise>",
        usage=AgentUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, duration_ms=250),
        exit_code=0,
        is_complete=True,
    )
    summary = finish_llm_session(session, result, "success")

    assert session.final_path.read_text() == result.output
    persisted_summary = json.loads(session.summary_path.read_text())
    assert persisted_summary["usage"]["input_tokens"] == 10
    assert persisted_summary["otel_export"] == {"enabled": False, "exporters": []}
    assert summary["usage"]["cost_usd"] == 0.001
    events = [json.loads(line) for line in session.events_path.read_text().splitlines()]
    assert events[-1]["event"] == LLM_RUN_FINISHED_EVENT_TYPE
    assert "final output" in session.compat_log_path.read_text()


def test_runner_environment_restores_previous_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))
    monkeypatch.setenv(LLM_RAW_LOG_PATH_ENV, "/tmp/old-raw.jsonl")
    monkeypatch.setenv(LLM_SESSION_ID_ENV, "old-session")
    session = start_llm_session(_task(), _plan(), "worker-1", "prompt", attempt=1)

    with session.runner_environment():
        assert os.environ[LLM_RAW_LOG_PATH_ENV] == str(session.raw_log_path)
        assert os.environ[LLM_SESSION_ID_ENV] == session.session_id

    assert os.environ[LLM_RAW_LOG_PATH_ENV] == "/tmp/old-raw.jsonl"
    assert os.environ[LLM_SESSION_ID_ENV] == "old-session"


def test_session_payload_contains_otel_style_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))
    session = start_llm_session(_task(), _plan(), "worker-1", "prompt", attempt=1)
    result = AgentResult(
        output="done",
        usage=AgentUsage(input_tokens=3, output_tokens=5),
        exit_code=0,
        is_complete=False,
    )

    payload = session_event_payload(session, "failed", result)

    assert payload["gen_ai.provider.name"]
    assert payload["gen_ai.request.model"] == ""
    assert payload["usage"]["gen_ai.usage.input_tokens"] == 3
    assert payload["usage"]["gen_ai.usage.output_tokens"] == 5
