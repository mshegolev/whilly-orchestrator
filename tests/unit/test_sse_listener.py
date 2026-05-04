"""Unit tests for the M3 SSE event-notify listener + broker.

Drives :class:`whilly.api.sse.EventNotifyBroker` and
:func:`whilly.api.sse.event_notify_listener_loop` through their
public surface using a stub asyncpg connection so the suite stays
free of testcontainers / Docker. Integration coverage of the real
LISTEN-NOTIFY round-trip lives in
``tests/integration/test_sse_listener_pg_drop.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest

from whilly.api.sse import (
    DEFAULT_RECONNECT_BACKOFFS,
    DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE,
    EVENT_NOTIFY_LISTENER_TASK_NAME,
    LISTENER_APPLICATION_NAME,
    NOTIFY_CHANNEL,
    SLOW_SUBSCRIBER_CLOSE_CODE,
    EventNotifyBroker,
    _DropSentinel,
    _ListenerState,
    event_notify_listener_loop,
)


def test_listener_task_name_constant() -> None:
    assert EVENT_NOTIFY_LISTENER_TASK_NAME == "whilly-event-notify-listener"


def test_notify_channel_constant() -> None:
    assert NOTIFY_CHANNEL == "whilly_events"


def test_listener_application_name_marker() -> None:
    assert "notify-listener" in LISTENER_APPLICATION_NAME


def test_default_reconnect_backoffs_match_contract() -> None:
    assert DEFAULT_RECONNECT_BACKOFFS == (1.0, 2.0, 4.0, 8.0, 30.0)


def test_default_subscriber_queue_maxsize_matches_drop_threshold() -> None:
    assert DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE == 1000


def test_slow_subscriber_close_code_is_1015() -> None:
    assert SLOW_SUBSCRIBER_CLOSE_CODE == 1015


@pytest.mark.asyncio
async def test_broker_subscribe_returns_subscriber_with_queue() -> None:
    broker = EventNotifyBroker()
    sub = broker.subscribe()
    assert isinstance(sub.queue, asyncio.Queue)
    assert sub.queue.maxsize == DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE
    assert sub.dropped is False
    assert sub.drop_code is None
    assert sub.last_event_id is None


@pytest.mark.asyncio
async def test_broker_subscribe_with_last_event_id() -> None:
    broker = EventNotifyBroker()
    sub = broker.subscribe(last_event_id=42)
    assert sub.last_event_id == 42


@pytest.mark.asyncio
async def test_broker_subscriber_count_increments() -> None:
    broker = EventNotifyBroker()
    assert broker.subscriber_count == 0
    a = broker.subscribe()
    b = broker.subscribe()
    assert broker.subscriber_count == 2
    broker.unsubscribe(a)
    assert broker.subscriber_count == 1
    broker.unsubscribe(b)
    assert broker.subscriber_count == 0


@pytest.mark.asyncio
async def test_broker_unsubscribe_is_idempotent() -> None:
    broker = EventNotifyBroker()
    sub = broker.subscribe()
    broker.unsubscribe(sub)
    broker.unsubscribe(sub)
    assert broker.subscriber_count == 0


@pytest.mark.asyncio
async def test_broker_fan_out_delivers_to_all_subscribers() -> None:
    broker = EventNotifyBroker()
    a = broker.subscribe()
    b = broker.subscribe()
    payload = {"event_id": 1, "event_type": "task.created"}
    delivered = broker.fan_out(payload)
    assert delivered == 2
    received_a = await a.queue.get()
    received_b = await b.queue.get()
    assert received_a == payload
    assert received_b == payload


@pytest.mark.asyncio
async def test_broker_fan_out_text_decodes_json_payload() -> None:
    broker = EventNotifyBroker()
    sub = broker.subscribe()
    delivered = broker.fan_out_text('{"event_id": 7, "event_type": "task.created"}')
    assert delivered == 1
    received = await sub.queue.get()
    assert received == {"event_id": 7, "event_type": "task.created"}


@pytest.mark.asyncio
async def test_broker_fan_out_text_skips_malformed_json(caplog: pytest.LogCaptureFixture) -> None:
    broker = EventNotifyBroker()
    sub = broker.subscribe()
    with caplog.at_level(logging.WARNING, logger="whilly.api.sse"):
        delivered = broker.fan_out_text("NOT-JSON-{")
    assert delivered == 0
    assert sub.queue.empty()
    assert any("skipping malformed event payload" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_broker_fan_out_text_skips_non_object(caplog: pytest.LogCaptureFixture) -> None:
    broker = EventNotifyBroker()
    sub = broker.subscribe()
    with caplog.at_level(logging.WARNING, logger="whilly.api.sse"):
        delivered = broker.fan_out_text('"a string"')
    assert delivered == 0
    assert sub.queue.empty()
    assert any("skipping malformed event payload" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_broker_fan_out_drops_slow_subscriber(caplog: pytest.LogCaptureFixture) -> None:
    broker = EventNotifyBroker(queue_maxsize=2)
    slow = broker.subscribe()
    fast = broker.subscribe()
    with caplog.at_level(logging.WARNING, logger="whilly.api.sse"):
        broker.fan_out({"event_id": 1})
        broker.fan_out({"event_id": 2})
        # Drain ``fast`` so it never trips backpressure; ``slow`` does.
        while not fast.queue.empty():
            fast.queue.get_nowait()
        broker.fan_out({"event_id": 3})
    assert slow.dropped is True
    assert slow.drop_code == SLOW_SUBSCRIBER_CLOSE_CODE
    assert slow not in broker._subscribers
    assert fast in broker._subscribers
    assert any("dropping event for slow subscriber" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_broker_dropped_subscriber_receives_close_sentinel() -> None:
    broker = EventNotifyBroker(queue_maxsize=2)
    slow = broker.subscribe()
    broker.fan_out({"event_id": 1})
    broker.fan_out({"event_id": 2})
    broker.fan_out({"event_id": 3})
    saw_sentinel = False
    seen: list[Any] = []
    while not slow.queue.empty():
        item = slow.queue.get_nowait()
        seen.append(item)
        if isinstance(item, _DropSentinel):
            saw_sentinel = True
            assert item.code == SLOW_SUBSCRIBER_CLOSE_CODE
    assert saw_sentinel, f"expected drop sentinel; saw: {seen!r}"


@pytest.mark.asyncio
async def test_broker_fan_out_continues_to_other_subscribers_when_one_drops() -> None:
    broker = EventNotifyBroker(queue_maxsize=2)
    slow = broker.subscribe()
    fast = broker.subscribe()
    seen_fast: list[Any] = []
    for i in range(5):
        broker.fan_out({"event_id": i})
        while not fast.queue.empty():
            seen_fast.append(fast.queue.get_nowait())
    assert slow.dropped is True
    assert any(isinstance(x, dict) and x.get("event_id") == 4 for x in seen_fast)


@pytest.mark.asyncio
async def test_broker_drop_all_signals_every_subscriber() -> None:
    broker = EventNotifyBroker()
    a = broker.subscribe()
    b = broker.subscribe()
    broker.drop_all()
    assert broker.subscriber_count == 0
    assert a.dropped is True
    assert b.dropped is True
    sentinel_a = a.queue.get_nowait()
    sentinel_b = b.queue.get_nowait()
    assert isinstance(sentinel_a, _DropSentinel)
    assert isinstance(sentinel_b, _DropSentinel)
    assert sentinel_a.code == SLOW_SUBSCRIBER_CLOSE_CODE


def test_broker_constructor_rejects_zero_maxsize() -> None:
    with pytest.raises(ValueError):
        EventNotifyBroker(queue_maxsize=0)


def test_broker_constructor_rejects_negative_maxsize() -> None:
    with pytest.raises(ValueError):
        EventNotifyBroker(queue_maxsize=-1)


@pytest.mark.asyncio
async def test_listener_loop_with_none_dsn_parks_until_stop(caplog: pytest.LogCaptureFixture) -> None:
    broker = EventNotifyBroker()
    stop = asyncio.Event()
    state = _ListenerState()
    with caplog.at_level(logging.WARNING, logger="whilly.api.sse"):
        task = asyncio.create_task(
            event_notify_listener_loop(broker, None, stop, state=state),
            name="test-listener-park",
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
    assert state.connected is False
    assert state.reconnect_attempts == 0
    assert any("no DSN provided" in rec.getMessage() for rec in caplog.records)


class _StubConnection:
    def __init__(self) -> None:
        self.added: list[tuple[str, Any]] = []
        self.removed: list[tuple[str, Any]] = []
        self._term_callbacks: list[Any] = []
        self.closed = False

    def add_termination_listener(self, cb: Any) -> None:
        self._term_callbacks.append(cb)

    async def add_listener(self, channel: str, cb: Any) -> None:
        self.added.append((channel, cb))

    async def remove_listener(self, channel: str, cb: Any) -> None:
        self.removed.append((channel, cb))

    async def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.closed = True

    def trigger_termination(self) -> None:
        for cb in self._term_callbacks:
            cb(self)

    def trigger_notify(self, payload: str) -> None:
        for channel, cb in self.added:
            if channel == NOTIFY_CHANNEL:
                cb(self, 1234, channel, payload)


@pytest.mark.asyncio
async def test_listener_loop_connects_listens_and_clean_teardown() -> None:
    broker = EventNotifyBroker()
    stop = asyncio.Event()
    state = _ListenerState()
    connect_event = asyncio.Event()
    state.on_connect_events.append(connect_event)

    conns: list[_StubConnection] = []

    async def fake_open(dsn: str, *, application_name: str) -> _StubConnection:
        c = _StubConnection()
        conns.append(c)
        return c

    with patch("whilly.api.sse._open_listener_connection", new=fake_open):
        task = asyncio.create_task(
            event_notify_listener_loop(broker, "postgresql://x", stop, state=state),
            name="test-listener-clean",
        )
        await asyncio.wait_for(connect_event.wait(), timeout=1.0)
        assert state.connected is True
        assert conns[0].added and conns[0].added[0][0] == NOTIFY_CHANNEL
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert state.connected is False
    assert conns[0].closed is True
    assert state.reconnect_attempts == 0


@pytest.mark.asyncio
async def test_listener_loop_fans_out_decoded_notify_payload() -> None:
    broker = EventNotifyBroker()
    sub = broker.subscribe()
    stop = asyncio.Event()
    state = _ListenerState()
    connect_event = asyncio.Event()
    state.on_connect_events.append(connect_event)

    conns: list[_StubConnection] = []

    async def fake_open(dsn: str, *, application_name: str) -> _StubConnection:
        c = _StubConnection()
        conns.append(c)
        return c

    with patch("whilly.api.sse._open_listener_connection", new=fake_open):
        task = asyncio.create_task(
            event_notify_listener_loop(broker, "postgresql://x", stop, state=state),
        )
        await asyncio.wait_for(connect_event.wait(), timeout=1.0)
        conns[0].trigger_notify('{"event_id": 11, "event_type": "task.created"}')
        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert item == {"event_id": 11, "event_type": "task.created"}
    assert state.notifies_received == 1


@pytest.mark.asyncio
async def test_listener_loop_reconnects_after_termination(caplog: pytest.LogCaptureFixture) -> None:
    broker = EventNotifyBroker()
    stop = asyncio.Event()
    state = _ListenerState()
    first = asyncio.Event()
    second = asyncio.Event()
    state.on_connect_events.append(first)

    conns: list[_StubConnection] = []
    connection_count = 0

    async def fake_open(dsn: str, *, application_name: str) -> _StubConnection:
        nonlocal connection_count
        connection_count += 1
        if connection_count == 2 and not second.is_set():
            state.on_connect_events.append(second)
        c = _StubConnection()
        conns.append(c)
        return c

    backoffs = (0.01, 0.02, 0.04)

    with caplog.at_level(logging.WARNING, logger="whilly.api.sse"):
        with patch("whilly.api.sse._open_listener_connection", new=fake_open):
            task = asyncio.create_task(
                event_notify_listener_loop(
                    broker,
                    "postgresql://x",
                    stop,
                    backoffs=backoffs,
                    state=state,
                ),
            )
            await asyncio.wait_for(first.wait(), timeout=1.0)
            conns[0].trigger_termination()
            await asyncio.wait_for(second.wait(), timeout=2.0)
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)

    assert connection_count >= 2
    assert state.reconnect_attempts >= 1
    assert any("terminated" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_listener_loop_uses_exponential_backoff_on_connect_failure() -> None:
    broker = EventNotifyBroker()
    stop = asyncio.Event()
    state = _ListenerState()

    fail_count = 0

    async def fake_open(dsn: str, *, application_name: str) -> Any:
        nonlocal fail_count
        fail_count += 1
        raise ConnectionRefusedError(f"attempt {fail_count}")

    backoffs = (0.01, 0.02, 0.04, 0.08, 0.30)

    with patch("whilly.api.sse._open_listener_connection", new=fake_open):
        task = asyncio.create_task(
            event_notify_listener_loop(
                broker,
                "postgresql://x",
                stop,
                backoffs=backoffs,
                state=state,
            ),
        )
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert state.reconnect_attempts >= 2
    assert state.connected is False
    assert state.last_error is not None
    assert state.backoff_idx == len(backoffs) - 1 or state.backoff_idx >= 2


@pytest.mark.asyncio
async def test_listener_loop_stop_during_backoff_returns_quickly() -> None:
    broker = EventNotifyBroker()
    stop = asyncio.Event()
    state = _ListenerState()

    async def fake_open(dsn: str, *, application_name: str) -> Any:
        raise ConnectionRefusedError("nope")

    backoffs = (5.0, 5.0)

    with patch("whilly.api.sse._open_listener_connection", new=fake_open):
        loop = asyncio.get_running_loop()
        start = loop.time()
        task = asyncio.create_task(
            event_notify_listener_loop(
                broker,
                "postgresql://x",
                stop,
                backoffs=backoffs,
                state=state,
            ),
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.5)
        elapsed = loop.time() - start

    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_listener_loop_resets_backoff_on_successful_connect() -> None:
    broker = EventNotifyBroker()
    stop = asyncio.Event()
    state = _ListenerState()
    third_connect = asyncio.Event()

    fail_count = 0

    async def fake_open(dsn: str, *, application_name: str) -> Any:
        nonlocal fail_count
        fail_count += 1
        if fail_count <= 2:
            raise ConnectionRefusedError(f"attempt {fail_count}")
        third_connect.set()
        return _StubConnection()

    backoffs = (0.01, 0.02, 0.04)

    with patch("whilly.api.sse._open_listener_connection", new=fake_open):
        task = asyncio.create_task(
            event_notify_listener_loop(
                broker,
                "postgresql://x",
                stop,
                backoffs=backoffs,
                state=state,
            ),
        )
        await asyncio.wait_for(third_connect.wait(), timeout=1.0)
        await asyncio.sleep(0.02)
        assert state.connected is True
        assert state.backoff_idx == 0
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
