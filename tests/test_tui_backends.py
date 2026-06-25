import httpx
import pytest

from whilly.cli.tui_backends import HttpOperatorBackend
from whilly.operator_snapshot_codec import snapshot_to_dict, snapshot_from_dict


def _sample_payload() -> dict:
    # Build a snapshot payload via the codec from a minimal real snapshot.
    from datetime import datetime, timezone
    from whilly.operator_views import (
        ComplianceSummary,
        OperatorControlState,
        OperatorSnapshot,
    )

    ts = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    snap = OperatorSnapshot(
        rendered_at=ts,
        summary=ComplianceSummary(
            total_tasks=0,
            tasks_by_status={},
            workers_online=0,
            workers_total=0,
            failed_tasks=0,
            open_review_gaps=0,
        ),
        tasks=(),
        workers=(),
        events=(),
        review_gaps=(),
        control_state=OperatorControlState(),
    )
    return snapshot_to_dict(snap)


async def test_http_backend_parses_snapshot():
    payload = _sample_payload()
    expected = snapshot_from_dict(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/operator/snapshot"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    backend = HttpOperatorBackend("https://whilly.corp", "tok", insecure=False, transport=transport)
    snap = await backend.fetch_snapshot(plan_id=None)
    assert snap == expected
    assert backend.read_only is True
    await backend.close()


async def test_http_backend_sends_plan_filter():
    payload = _sample_payload()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["plan"] = request.url.params.get("plan")
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    backend = HttpOperatorBackend("https://whilly.corp", "tok", transport=transport)
    await backend.fetch_snapshot(plan_id="p1")
    assert seen["plan"] == "p1"
    await backend.close()


async def test_http_backend_rejects_plain_http_non_loopback():
    with pytest.raises(ValueError):
        HttpOperatorBackend("http://whilly.corp", "tok", insecure=False)


async def test_http_backend_allows_plain_http_with_insecure():
    backend = HttpOperatorBackend("http://whilly.corp", "tok", insecure=True)
    assert backend.read_only is True
    await backend.close()


async def test_http_backend_allows_loopback_plain_http():
    backend = HttpOperatorBackend("http://127.0.0.1:8000", "tok", insecure=False)
    assert backend.read_only is True
    await backend.close()
