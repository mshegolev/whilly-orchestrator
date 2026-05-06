"""Optional OpenTelemetry export for Whilly LLM Ops.

The local LLM Ops layer is always available and writes Postgres events plus
filesystem artifacts. This module is deliberately optional: when the
OpenTelemetry packages are not installed, export is a no-op and task
execution continues.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from whilly.adapters.runner.result_parser import AgentResult

if False:  # pragma: no cover - type checkers only without runtime import cycle
    from whilly.llm_ops import LLMOpsSession

log = logging.getLogger(__name__)

EXPORTERS_ENV: Final[str] = "WHILLY_LLM_OPS_EXPORTERS"
OTLP_ENDPOINT_ENV: Final[str] = "WHILLY_LLM_OPS_OTLP_ENDPOINT"
OTLP_HEADERS_ENV: Final[str] = "WHILLY_LLM_OPS_OTLP_HEADERS"
CAPTURE_CONTENT_ENV: Final[str] = "WHILLY_LLM_OPS_CAPTURE_CONTENT"
OTLP_TIMEOUT_ENV: Final[str] = "WHILLY_LLM_OPS_OTLP_TIMEOUT"

_DEFAULT_LANGFUSE_HOST: Final[str] = "https://cloud.langfuse.com"
_DEFAULT_OTLP_TIMEOUT_SECONDS: Final[float] = 5.0
_MAX_TOOL_EVENTS: Final[int] = 50

_PROVIDER_CACHE: Any | None = None
_TRACER_CACHE: Any | None = None
_EXPORTER_NAMES_CACHE: tuple[str, ...] = ()
_MISSING_DEPS_LOGGED = False


@dataclass(frozen=True)
class OTelExporterSpec:
    """One OTLP/HTTP trace exporter destination."""

    name: str
    endpoint: str
    headers: dict[str, str]


def _want_exporter(name: str) -> bool:
    raw = os.environ.get(EXPORTERS_ENV, "").strip()
    if not raw:
        return True
    wanted = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if "none" in wanted or "off" in wanted:
        return False
    return name in wanted or "all" in wanted or "auto" in wanted


def _parse_headers(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            headers[key] = value.strip()
    return headers


def _langfuse_headers() -> dict[str, str]:
    headers = _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS"))
    headers.update(_parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")))
    headers.update(_parse_headers(os.environ.get(OTLP_HEADERS_ENV)))
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if public_key and secret_key and not any(k.lower() == "authorization" for k in headers):
        auth = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {auth}"
    headers.setdefault("x-langfuse-ingestion-version", "4")
    return headers


def _generic_headers() -> dict[str, str]:
    headers = _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS"))
    headers.update(_parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")))
    headers.update(_parse_headers(os.environ.get(OTLP_HEADERS_ENV)))
    return headers


def _phoenix_headers() -> dict[str, str]:
    headers = _generic_headers()
    api_key = os.environ.get("PHOENIX_API_KEY", "").strip()
    if api_key and not any(k.lower() == "authorization" for k in headers):
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(_parse_headers(os.environ.get("PHOENIX_CLIENT_HEADERS")))
    return headers


def _trace_endpoint(base: str) -> str:
    endpoint = base.rstrip("/")
    if endpoint.endswith("/v1/traces"):
        return endpoint
    return f"{endpoint}/v1/traces"


def configured_exporters() -> tuple[OTelExporterSpec, ...]:
    """Resolve configured external exporters from env without importing OTel."""

    specs: list[OTelExporterSpec] = []

    if _want_exporter("otel"):
        endpoint = (
            os.environ.get(OTLP_ENDPOINT_ENV)
            or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or ""
        ).strip()
        if endpoint:
            specs.append(OTelExporterSpec("otel", _trace_endpoint(endpoint), _generic_headers()))

    if _want_exporter("langfuse"):
        endpoint = os.environ.get("LANGFUSE_OTEL_ENDPOINT", "").strip()
        if not endpoint:
            host = os.environ.get("LANGFUSE_HOST", "").strip()
            if not host and os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
                host = _DEFAULT_LANGFUSE_HOST
            if host:
                endpoint = f"{host.rstrip('/')}/api/public/otel/v1/traces"
        if endpoint:
            specs.append(OTelExporterSpec("langfuse", endpoint, _langfuse_headers()))

    if _want_exporter("phoenix"):
        endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").strip()
        if endpoint:
            specs.append(OTelExporterSpec("phoenix", _trace_endpoint(endpoint), _phoenix_headers()))

    deduped: dict[tuple[str, str], OTelExporterSpec] = {}
    for spec in specs:
        deduped[(spec.name, spec.endpoint)] = spec
    return tuple(deduped.values())


def _timeout_seconds() -> float:
    raw = os.environ.get(OTLP_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_OTLP_TIMEOUT_SECONDS
    try:
        return max(0.1, float(raw))
    except ValueError:
        return _DEFAULT_OTLP_TIMEOUT_SECONDS


def _get_tracer() -> tuple[Any | None, tuple[str, ...]]:
    global _EXPORTER_NAMES_CACHE, _MISSING_DEPS_LOGGED, _PROVIDER_CACHE, _TRACER_CACHE

    specs = configured_exporters()
    if not specs:
        return None, ()
    if _TRACER_CACHE is not None:
        return _TRACER_CACHE, _EXPORTER_NAMES_CACHE

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError:
        if not _MISSING_DEPS_LOGGED:
            log.warning(
                "llm otel export configured but OpenTelemetry packages are missing; "
                "install `whilly-orchestrator[llmops]`"
            )
            _MISSING_DEPS_LOGGED = True
        return None, ()

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "whilly-orchestrator"),
            "service.namespace": "whilly",
            "deployment.environment": os.environ.get("WHILLY_ENV", os.environ.get("ENV", "development")),
        }
    )
    provider = TracerProvider(resource=resource)
    timeout = _timeout_seconds()
    names: list[str] = []
    for spec in specs:
        exporter = OTLPSpanExporter(endpoint=spec.endpoint, headers=spec.headers, timeout=timeout)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        names.append(spec.name)

    _PROVIDER_CACHE = provider
    _TRACER_CACHE = provider.get_tracer("whilly.llm_ops")
    _EXPORTER_NAMES_CACHE = tuple(names)
    return _TRACER_CACHE, _EXPORTER_NAMES_CACHE


def _started_at_ns(value: str) -> int | None:
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1_000_000_000)
    except ValueError:
        return None


def _content_capture_enabled() -> bool:
    return os.environ.get(CAPTURE_CONTENT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _usage_attributes(result: AgentResult | None) -> dict[str, int | float]:
    if result is None:
        return {}
    return {
        "gen_ai.usage.input_tokens": result.usage.input_tokens,
        "gen_ai.usage.output_tokens": result.usage.output_tokens,
        "whilly.llm.cache_read_tokens": result.usage.cache_read_tokens,
        "whilly.llm.cache_create_tokens": result.usage.cache_create_tokens,
        "whilly.llm.cost_usd": result.usage.cost_usd,
        "whilly.llm.num_turns": result.usage.num_turns,
        "whilly.llm.duration_ms": result.usage.duration_ms,
    }


def _tool_events(raw_log_path: Path, *, capture_content: bool) -> list[dict[str, Any]]:
    if not raw_log_path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for raw in raw_log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if len(out) >= _MAX_TOOL_EVENTS:
            break
        try:
            wrapper = json.loads(raw)
            inner = json.loads(wrapper.get("line", ""))
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if inner.get("type") != "tool_use":
            continue
        part = inner.get("part") if isinstance(inner.get("part"), dict) else {}
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        attrs: dict[str, Any] = {
            "whilly.tool.name": part.get("tool") or "",
            "whilly.tool.status": state.get("status") or "",
            "whilly.tool.title": part.get("title") or state.get("title") or "",
        }
        if "exit" in metadata:
            attrs["whilly.tool.exit_code"] = metadata.get("exit")
        if "truncated" in metadata:
            attrs["whilly.tool.output_truncated"] = bool(metadata.get("truncated"))
        if capture_content:
            tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
            if "command" in tool_input:
                attrs["whilly.tool.command"] = str(tool_input.get("command") or "")
            if "output" in metadata:
                attrs["whilly.tool.output"] = str(metadata.get("output") or "")
        out.append(attrs)
    return out


def emit_llm_otel_span(
    session: "LLMOpsSession",
    status: str,
    result: AgentResult | None,
    summary: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """Emit one task-run span to configured OTLP backends."""

    tracer, exporters = _get_tracer()
    if tracer is None:
        return {"enabled": False, "exporters": []}

    try:
        from opentelemetry.trace import Status, StatusCode
    except ImportError:
        return {"enabled": False, "exporters": []}

    capture_content = _content_capture_enabled()
    attrs: dict[str, Any] = {
        "whilly.task.id": session.task_id,
        "whilly.plan.id": session.plan_id,
        "whilly.worker.id": session.worker_id,
        "whilly.task.attempt": session.attempt,
        "whilly.session.id": session.session_id,
        "session.id": session.session_id,
        "langfuse.session.id": session.session_id,
        "langfuse.trace.name": f"whilly task {session.task_id}",
        "gen_ai.provider.name": session.provider,
        "gen_ai.request.model": session.model,
        "gen_ai.response.model": session.model,
        "gen_ai.operation.name": "execute_task",
        "whilly.llm.status": status,
        "whilly.artifact.ref": str(session.artifact_dir),
        "whilly.prompt.path": str(session.prompt_path),
        "whilly.raw_log.path": str(session.raw_log_path),
        "whilly.summary.path": str(session.summary_path),
    }
    attrs.update(_usage_attributes(result))
    if result is not None:
        attrs["whilly.llm.exit_code"] = result.exit_code
        attrs["whilly.llm.is_complete"] = result.is_complete
        attrs["whilly.llm.output_chars"] = len(result.output)
    if error:
        attrs["whilly.error"] = error

    start_time = _started_at_ns(session.started_at)
    span = tracer.start_span(
        "whilly.llm_run",
        attributes=attrs,
        start_time=start_time,
    )
    try:
        if status not in {"success", "started"} or error:
            span.set_status(Status(StatusCode.ERROR, error or status))
        else:
            span.set_status(Status(StatusCode.OK))
        span.add_event(
            "whilly.prompt_build",
            {
                "whilly.prompt.path": str(session.prompt_path),
                "whilly.prompt.chars": session.prompt_path.stat().st_size if session.prompt_path.is_file() else 0,
            },
        )
        for tool_attrs in _tool_events(session.raw_log_path, capture_content=capture_content):
            span.add_event("whilly.tool_use", tool_attrs)
        terminal_event = "whilly.complete" if status == "success" else "whilly.fail"
        span.add_event(
            terminal_event,
            {
                "whilly.summary.path": str(session.summary_path),
                "whilly.status": status,
            },
        )
        if capture_content:
            if session.prompt_path.is_file():
                span.add_event(
                    "gen_ai.user.message",
                    {"content": session.prompt_path.read_text(encoding="utf-8", errors="replace")},
                )
            if result is not None:
                span.add_event("gen_ai.choice", {"content": result.output})
        trace_id = f"{span.get_span_context().trace_id:032x}"
        span_id = f"{span.get_span_context().span_id:016x}"
    finally:
        span.end()

    summary["otel_trace_id"] = trace_id
    summary["otel_span_id"] = span_id
    return {
        "enabled": True,
        "exporters": list(exporters),
        "trace_id": trace_id,
        "span_id": span_id,
    }


def shutdown_otel_exporters() -> None:
    """Flush external exporters when a short-lived process wants to exit."""

    if _PROVIDER_CACHE is not None:
        try:
            _PROVIDER_CACHE.shutdown()
        except Exception:  # noqa: BLE001 - telemetry shutdown must be best-effort
            log.debug("llm otel provider shutdown failed", exc_info=True)


def _reset_for_tests() -> None:
    global _EXPORTER_NAMES_CACHE, _MISSING_DEPS_LOGGED, _PROVIDER_CACHE, _TRACER_CACHE

    _PROVIDER_CACHE = None
    _TRACER_CACHE = None
    _EXPORTER_NAMES_CACHE = ()
    _MISSING_DEPS_LOGGED = False


__all__ = [
    "CAPTURE_CONTENT_ENV",
    "EXPORTERS_ENV",
    "OTLP_ENDPOINT_ENV",
    "OTLP_HEADERS_ENV",
    "OTLP_TIMEOUT_ENV",
    "OTelExporterSpec",
    "configured_exporters",
    "emit_llm_otel_span",
    "shutdown_otel_exporters",
]
