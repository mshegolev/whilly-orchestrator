"""Minimal LLM Ops capture for Whilly worker runs.

The durable audit trail stays in Postgres ``events`` as small metadata
events. Large prompt / model-stream artifacts live on disk under
``WHILLY_LOG_DIR`` (default: ``whilly_logs``), which keeps the database from
turning into a transcript store while still making one task debuggable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from whilly.adapters.runner.result_parser import AgentResult
from whilly.core.models import Plan, Task, WorkerId

UTC = timezone.utc

log = logging.getLogger(__name__)

DEFAULT_LOG_DIR: Final[str] = "whilly_logs"
LOG_DIR_ENV: Final[str] = "WHILLY_LOG_DIR"
LLM_RAW_LOG_PATH_ENV: Final[str] = "WHILLY_LLM_RAW_LOG_PATH"
LLM_SESSION_ID_ENV: Final[str] = "WHILLY_LLM_SESSION_ID"

LLM_RUN_STARTED_EVENT_TYPE: Final[str] = "llm.run_started"
LLM_RUN_FINISHED_EVENT_TYPE: Final[str] = "llm.run_finished"
LLM_RUN_FAILED_EVENT_TYPE: Final[str] = "llm.run_failed"
LLM_RUN_CANCELLED_EVENT_TYPE: Final[str] = "llm.run_cancelled"

_SAFE_PART_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class LLMOpsSession:
    """Filesystem artifact bundle for one task attempt."""

    task_id: str
    plan_id: str
    worker_id: WorkerId
    attempt: int
    session_id: str
    provider: str
    model: str
    artifact_dir: Path
    prompt_path: Path
    raw_log_path: Path
    final_path: Path
    summary_path: Path
    compat_prompt_path: Path
    compat_log_path: Path
    events_path: Path
    started_at: str

    @contextmanager
    def runner_environment(self) -> Iterator[None]:
        """Expose artifact paths to subprocess adapters for this run."""

        previous_raw = os.environ.get(LLM_RAW_LOG_PATH_ENV)
        previous_session = os.environ.get(LLM_SESSION_ID_ENV)
        os.environ[LLM_RAW_LOG_PATH_ENV] = str(self.raw_log_path)
        os.environ[LLM_SESSION_ID_ENV] = self.session_id
        try:
            yield
        finally:
            if previous_raw is None:
                os.environ.pop(LLM_RAW_LOG_PATH_ENV, None)
            else:
                os.environ[LLM_RAW_LOG_PATH_ENV] = previous_raw
            if previous_session is None:
                os.environ.pop(LLM_SESSION_ID_ENV, None)
            else:
                os.environ[LLM_SESSION_ID_ENV] = previous_session


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_part(value: str) -> str:
    safe = _SAFE_PART_RE.sub("_", value).strip("._")
    return safe or "unknown"


def _log_dir(explicit: str | Path | None = None) -> Path:
    return Path(explicit or os.environ.get(LOG_DIR_ENV) or DEFAULT_LOG_DIR).expanduser()


def _provider_from_env() -> str:
    return (
        os.environ.get("WHILLY_CLI")
        or os.environ.get("LLM_PROVIDER")
        or ("claude-code" if os.environ.get("CLAUDE_BIN") else "claude")
    )


def _model_from_env() -> str:
    return os.environ.get("WHILLY_MODEL") or os.environ.get("LLM_MODEL") or os.environ.get("CLAUDE_MODEL") or ""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_task_event(session: LLMOpsSession, event_type: str, payload: dict[str, Any]) -> None:
    """Append a legacy-compatible per-task JSONL event line."""

    entry = {
        "ts": _utc_now(),
        "event": event_type,
        "task_id": session.task_id,
        **payload,
    }
    session.events_path.parent.mkdir(parents=True, exist_ok=True)
    with session.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def start_llm_session(
    task: Task,
    plan: Plan,
    worker_id: WorkerId,
    prompt: str,
    *,
    attempt: int | None = None,
    log_dir: str | Path | None = None,
) -> LLMOpsSession:
    """Create artifacts for one LLM run and write the exact prompt."""

    attempt_no = int(attempt if attempt is not None else task.version)
    base = _log_dir(log_dir)
    safe_task_id = _safe_part(task.id)
    started_at = _utc_now()
    session_id = f"{plan.id}/{task.id}/attempt-{attempt_no}"
    artifact_dir = base / "tasks" / safe_task_id / f"attempt-{attempt_no}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (base / "tasks").mkdir(parents=True, exist_ok=True)

    session = LLMOpsSession(
        task_id=task.id,
        plan_id=plan.id,
        worker_id=worker_id,
        attempt=attempt_no,
        session_id=session_id,
        provider=_provider_from_env(),
        model=_model_from_env(),
        artifact_dir=artifact_dir,
        prompt_path=artifact_dir / "prompt.txt",
        raw_log_path=artifact_dir / "raw.jsonl",
        final_path=artifact_dir / "final.txt",
        summary_path=artifact_dir / "summary.json",
        compat_prompt_path=base / f"{safe_task_id}_prompt.txt",
        compat_log_path=base / f"{safe_task_id}.log",
        events_path=base / "tasks" / f"{safe_task_id}.events.jsonl",
        started_at=started_at,
    )

    session.prompt_path.write_text(prompt, encoding="utf-8")
    session.compat_prompt_path.write_text(prompt, encoding="utf-8")
    _write_json(artifact_dir / "session.json", session_event_payload(session, "started"))
    append_task_event(session, LLM_RUN_STARTED_EVENT_TYPE, session_event_payload(session, "started"))
    return session


def usage_payload(result: AgentResult | None) -> dict[str, Any]:
    """Return OpenTelemetry-compatible usage keys plus Whilly's native fields."""

    if result is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_create_tokens": 0,
            "cost_usd": 0.0,
            "num_turns": 0,
            "duration_ms": 0,
        }
    usage = result.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_create_tokens": usage.cache_create_tokens,
        "cost_usd": usage.cost_usd,
        "num_turns": usage.num_turns,
        "duration_ms": usage.duration_ms,
        "gen_ai.usage.input_tokens": usage.input_tokens,
        "gen_ai.usage.output_tokens": usage.output_tokens,
    }


def session_event_payload(
    session: LLMOpsSession,
    status: str,
    result: AgentResult | None = None,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "whilly.llm_ops.v1",
        "task_id": session.task_id,
        "plan_id": session.plan_id,
        "worker_id": session.worker_id,
        "attempt": session.attempt,
        "session_id": session.session_id,
        "status": status,
        "provider": session.provider,
        "model": session.model,
        "gen_ai.provider.name": session.provider,
        "gen_ai.request.model": session.model,
        "artifact_ref": str(session.artifact_dir),
        "prompt_path": str(session.prompt_path),
        "raw_log_path": str(session.raw_log_path),
        "summary_path": str(session.summary_path),
        "started_at": session.started_at,
    }
    if result is not None:
        payload.update(
            {
                "exit_code": result.exit_code,
                "is_complete": result.is_complete,
                "output_chars": len(result.output),
                "usage": usage_payload(result),
            }
        )
    if error:
        payload["error"] = error
    return payload


def session_event_detail(
    session: LLMOpsSession,
    status: str,
    result: AgentResult | None = None,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        **session_event_payload(session, status, result, error=error),
        "final_path": str(session.final_path),
        "compat_prompt_path": str(session.compat_prompt_path),
        "compat_log_path": str(session.compat_log_path),
        "events_path": str(session.events_path),
    }


def finish_llm_session(
    session: LLMOpsSession,
    result: AgentResult | None,
    status: str,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """Persist final output, summary JSON, and compatibility log files."""

    finished_at = _utc_now()
    if result is not None:
        session.final_path.write_text(result.output, encoding="utf-8")

    summary = session_event_detail(session, status, result, error=error)
    summary["finished_at"] = finished_at
    try:
        from whilly.llm_otel import emit_llm_otel_span

        summary["otel_export"] = emit_llm_otel_span(session, status, result, summary, error=error)
    except Exception:  # noqa: BLE001 - external LLM Ops must never fail the worker
        log.warning("llm otel export failed: task=%s", session.task_id, exc_info=True)
        summary["otel_export"] = {"enabled": False, "exporters": [], "error": "export_failed"}
    _write_json(session.summary_path, summary)

    with session.compat_log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n--- whilly llm session {session.session_id} status={status} ---\n")
        if session.raw_log_path.is_file():
            raw_text = session.raw_log_path.read_text(encoding="utf-8", errors="replace")
            fh.write(raw_text)
            if not raw_text.endswith("\n"):
                fh.write("\n")
        if result is not None:
            fh.write("--- final output ---\n")
            fh.write(result.output)
            if not result.output.endswith("\n"):
                fh.write("\n")

    event_type = {
        "success": LLM_RUN_FINISHED_EVENT_TYPE,
        "failed": LLM_RUN_FAILED_EVENT_TYPE,
        "cancelled": LLM_RUN_CANCELLED_EVENT_TYPE,
    }.get(status, LLM_RUN_FINISHED_EVENT_TYPE)
    append_task_event(session, event_type, summary)
    return summary


def copy_artifacts_to(target_dir: Path, session: LLMOpsSession) -> None:
    """Copy one artifact directory, best-effort helper for future exporters."""

    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(session.artifact_dir, target_dir / session.artifact_dir.name, dirs_exist_ok=True)


__all__ = [
    "DEFAULT_LOG_DIR",
    "LLM_RAW_LOG_PATH_ENV",
    "LLM_RUN_CANCELLED_EVENT_TYPE",
    "LLM_RUN_FAILED_EVENT_TYPE",
    "LLM_RUN_FINISHED_EVENT_TYPE",
    "LLM_RUN_STARTED_EVENT_TYPE",
    "LLM_SESSION_ID_ENV",
    "LOG_DIR_ENV",
    "LLMOpsSession",
    "append_task_event",
    "copy_artifacts_to",
    "finish_llm_session",
    "session_event_detail",
    "session_event_payload",
    "start_llm_session",
    "usage_payload",
]
