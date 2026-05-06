from __future__ import annotations

import base64

from whilly.llm_otel import configured_exporters


def test_langfuse_exporter_uses_host_and_basic_auth(monkeypatch) -> None:
    monkeypatch.setenv("WHILLY_LLM_OPS_EXPORTERS", "langfuse")
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    (spec,) = configured_exporters()

    assert spec.name == "langfuse"
    assert spec.endpoint == "http://langfuse:3000/api/public/otel/v1/traces"
    expected = base64.b64encode(b"pk-lf-test:sk-lf-test").decode("ascii")
    assert spec.headers["Authorization"] == f"Basic {expected}"
    assert spec.headers["x-langfuse-ingestion-version"] == "4"


def test_phoenix_exporter_adds_bearer_auth_and_trace_path(monkeypatch) -> None:
    monkeypatch.setenv("WHILLY_LLM_OPS_EXPORTERS", "phoenix")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006")
    monkeypatch.setenv("PHOENIX_API_KEY", "phoenix-key")

    (spec,) = configured_exporters()

    assert spec.name == "phoenix"
    assert spec.endpoint == "http://phoenix:6006/v1/traces"
    assert spec.headers["Authorization"] == "Bearer phoenix-key"


def test_generic_otel_exporter_respects_custom_headers(monkeypatch) -> None:
    monkeypatch.setenv("WHILLY_LLM_OPS_EXPORTERS", "otel")
    monkeypatch.setenv("WHILLY_LLM_OPS_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("WHILLY_LLM_OPS_OTLP_HEADERS", "Authorization=Bearer token,x-test=yes")

    (spec,) = configured_exporters()

    assert spec.name == "otel"
    assert spec.endpoint == "http://collector:4318/v1/traces"
    assert spec.headers == {"Authorization": "Bearer token", "x-test": "yes"}


def test_no_exporter_without_endpoint_or_backend_env(monkeypatch) -> None:
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

    assert configured_exporters() == ()
