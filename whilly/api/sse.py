"""SSE event-notify listener + per-subscriber fan-out broker (M3).

Owns the data-plane half of the M3 SSE pipeline: a dedicated asyncpg
``LISTEN whilly_events`` connection (NOT acquired from the pool, since
``LISTEN`` ties a connection up indefinitely) plus an in-memory
:class:`EventNotifyBroker` that copies each NOTIFY into every
subscribed :class:`asyncio.Queue`.

Lifespan integration
--------------------
:func:`whilly.adapters.transport.server.create_app` registers
:func:`event_notify_listener_loop` as a child of the lifespan
:class:`asyncio.TaskGroup` under the name
:data:`EVENT_NOTIFY_LISTENER_TASK_NAME`. The shared ``sweep_stop``
:class:`asyncio.Event` requests teardown — when set, the loop closes
its dedicated connection, signals every subscriber via the drop
sentinel and returns within ~1 second.

Fan-out semantics
-----------------
* Each ``GET /events/stream`` consumer calls
  :meth:`EventNotifyBroker.subscribe` to obtain a :class:`Subscriber`
  with a bounded :class:`asyncio.Queue`.
* :meth:`EventNotifyBroker.fan_out` copies one decoded NOTIFY payload
  onto every subscriber's queue via ``put_nowait``. The hot path is
  synchronous (no awaits) so a slow subscriber can never wedge the
  listener loop.
* When a subscriber's queue is full the broker drops it: the
  subscriber is removed from the live set, a :class:`_DropSentinel`
  carrying close code 1015 is shoved at the head of its queue, and a
  WARNING log is emitted ("dropping event for slow subscriber"). The
  SSE endpoint reads the sentinel and closes the underlying response
  with the matching WebSocket-style close code.

Reconnect policy
----------------
A dropped Postgres connection (server reboot, ``pg_terminate_backend``,
network blip) drives the listener through an exponential backoff
schedule of 1 s, 2 s, 4 s, 8 s, 30 s (capped) before retrying
:func:`asyncpg.connect`. Each backoff tick races against the shared
``stop`` event so a graceful shutdown during a backoff sleep returns
immediately rather than waiting out the full 30 s.

Malformed NOTIFY payload
------------------------
Postgres' :func:`pg_notify` accepts arbitrary text; the listener
defends against malformed JSON by catching :class:`json.JSONDecodeError`
on every payload, emitting a single WARNING ("skipping malformed event
payload") and continuing to read subsequent NOTIFYs (VAL-M3-SSE-
LISTENER-011).

Why a dedicated connection (not from the pool)?
-----------------------------------------------
``LISTEN`` registers a session-scoped subscription; pool checkouts are
ephemeral, so a pool-acquired connection would lose the LISTEN as soon
as it returned to the pool. The mission contract pins the dedicated
connection explicitly (VAL-M3-SSE-LISTENER-002) — the listener verifies
this by tagging its connection with ``application_name`` so
``pg_stat_activity`` can prove the extra session.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final

import asyncpg

logger = logging.getLogger(__name__)

#: Task name used by :func:`whilly.adapters.transport.server.create_app`
#: when scheduling the listener inside its lifespan TaskGroup.
#: Validators read this string to assert the task is wired up
#: (VAL-M3-SSE-LISTENER-001).
EVENT_NOTIFY_LISTENER_TASK_NAME: Final[str] = "whilly-event-notify-listener"

#: Postgres NOTIFY channel. Mirrors the literal in
#: ``whilly_notify_event()`` (migration 011) — both values must change
#: together (VAL-M3-MIGRATE-010-015).
NOTIFY_CHANNEL: Final[str] = "whilly_events"

#: Default per-subscriber queue depth. 1000 events is the contract
#: trigger for the slow-subscriber drop policy (VAL-M3-SSE-LISTENER-006);
#: at 100 events/sec sustained traffic that's a 10-second buffer before
#: backpressure kicks in.
DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE: Final[int] = 1000

#: Reconnect backoff schedule (seconds). Successive failed connection
#: attempts walk down the tuple; the final entry is the steady-state
#: cap. Pinned to ``(1, 2, 4, 8, 30)`` by VAL-M3-SSE-LISTENER-007.
DEFAULT_RECONNECT_BACKOFFS: Final[tuple[float, ...]] = (1.0, 2.0, 4.0, 8.0, 30.0)

#: ``application_name`` tagged on the dedicated listener connection.
#: Validators query ``pg_stat_activity.application_name LIKE
#: '%notify-listener%'`` to confirm the extra session
#: (VAL-M3-SSE-LISTENER-002).
LISTENER_APPLICATION_NAME: Final[str] = "whilly-notify-listener"

#: WebSocket-style close code used when dropping a slow subscriber.
#: 1015 is the canonical "abnormal close" hint; the SSE endpoint maps
#: it onto an HTTP-stream close so the client sees a clean termination
#: rather than a hung connection (VAL-M3-SSE-LISTENER-006).
SLOW_SUBSCRIBER_CLOSE_CODE: Final[int] = 1015


@dataclass
class _DropSentinel:
    """Marker queued onto a dropped subscriber's queue.

    The SSE endpoint reads each queue with ``await get()`` and treats
    a :class:`_DropSentinel` as a signal to close the response with
    :attr:`code`. Carrying the code on the sentinel avoids a separate
    out-of-band channel between broker and endpoint.
    """

    code: int


@dataclass(eq=False)
class Subscriber:
    """A single SSE-stream consumer registered with the broker.

    Each ``GET /events/stream`` handler holds one :class:`Subscriber`
    for the lifetime of the response. The handler reads
    :attr:`queue` until it observes a :class:`_DropSentinel` (slow
    drop) or its own task is cancelled (client disconnect).

    ``last_event_id`` carries the ``Last-Event-ID`` header value so
    the endpoint can replay any committed events with id > this value
    before it hands the subscriber over to the live fan-out (the
    handshake-no-gap contract, VAL-M3-SSE-LISTENER-901).
    """

    queue: asyncio.Queue[dict[str, Any] | _DropSentinel]
    last_event_id: int | None = None
    dropped: bool = False
    drop_code: int | None = None


class EventNotifyBroker:
    """In-memory fan-out registry feeding ``GET /events/stream`` clients.

    Owns the set of live :class:`Subscriber` instances plus the
    synchronous :meth:`fan_out` entry-point invoked from the asyncpg
    LISTEN callback. The broker is loop-affine (constructed inside the
    lifespan) but does not allocate the queues itself — callers do that
    via :meth:`subscribe`.

    Thread safety
        The broker is single-threaded by design: every public method
        runs on the FastAPI event loop. ``put_nowait`` and the
        subscriber-set mutations are therefore safe without locking.
    """

    def __init__(self, *, queue_maxsize: int = DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE) -> None:
        if queue_maxsize <= 0:
            raise ValueError(f"queue_maxsize must be > 0, got {queue_maxsize!r}")
        self._queue_maxsize = queue_maxsize
        self._subscribers: set[Subscriber] = set()

    @property
    def subscriber_count(self) -> int:
        """Number of currently-active (non-dropped) subscribers.

        Exposed verbatim under the metric name
        ``whilly_sse_subscribers_total`` by the M3 metrics module
        (separate feature). The property is the single source of
        truth so the metric and any in-process observer stay aligned.
        """
        return len(self._subscribers)

    @property
    def queue_maxsize(self) -> int:
        return self._queue_maxsize

    def subscribe(self, *, last_event_id: int | None = None) -> Subscriber:
        """Register a new SSE consumer and return its :class:`Subscriber`."""
        sub = Subscriber(
            queue=asyncio.Queue(maxsize=self._queue_maxsize),
            last_event_id=last_event_id,
        )
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        """Remove a subscriber. Idempotent — safe on already-dropped subs."""
        self._subscribers.discard(sub)

    def fan_out(self, payload: dict[str, Any]) -> int:
        """Copy ``payload`` onto every subscriber queue. Returns delivered count.

        Hot path — runs synchronously inside the asyncpg LISTEN
        callback, so it must never block. Uses ``put_nowait`` and
        catches :class:`asyncio.QueueFull` per-subscriber: a full
        queue triggers :meth:`_drop_slow` for that subscriber only;
        siblings keep flowing.
        """
        delivered = 0
        for sub in tuple(self._subscribers):
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._drop_slow(sub)
                continue
            delivered += 1
        return delivered

    def fan_out_text(self, raw: str) -> int:
        """Decode ``raw`` JSON and call :meth:`fan_out`.

        Defends against malformed payloads — :class:`json.JSONDecodeError`
        is logged at WARNING and swallowed so a bad NOTIFY does not
        crash the listener (VAL-M3-SSE-LISTENER-011).
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("skipping malformed event payload: %r", raw)
            return 0
        if not isinstance(payload, dict):
            logger.warning("skipping malformed event payload: not an object: %r", raw)
            return 0
        return self.fan_out(payload)

    def drop_all(self, *, code: int = SLOW_SUBSCRIBER_CLOSE_CODE) -> None:
        """Signal every subscriber to close (used on lifespan teardown)."""
        for sub in tuple(self._subscribers):
            self._send_drop_sentinel(sub, code=code)
            sub.dropped = True
            sub.drop_code = code
        self._subscribers.clear()

    def _drop_slow(self, sub: Subscriber) -> None:
        sub.dropped = True
        sub.drop_code = SLOW_SUBSCRIBER_CLOSE_CODE
        self._send_drop_sentinel(sub, code=SLOW_SUBSCRIBER_CLOSE_CODE)
        self._subscribers.discard(sub)
        logger.warning(
            "dropping event for slow subscriber: queue full (maxsize=%d, close_code=%d)",
            self._queue_maxsize,
            SLOW_SUBSCRIBER_CLOSE_CODE,
        )

    @staticmethod
    def _send_drop_sentinel(sub: Subscriber, *, code: int) -> None:
        sentinel = _DropSentinel(code=code)
        try:
            sub.queue.put_nowait(sentinel)
            return
        except asyncio.QueueFull:
            pass
        try:
            sub.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            sub.queue.put_nowait(sentinel)
        except asyncio.QueueFull:
            pass


@dataclass
class _ListenerState:
    """Mutable state shared between the loop body and probes (tests)."""

    connected: bool = False
    reconnect_attempts: int = 0
    backoff_idx: int = 0
    last_error: str | None = None
    notifies_received: int = 0
    on_connect_events: list[asyncio.Event] = field(default_factory=list)


async def _wait_with_stop(stop: asyncio.Event, delay: float) -> bool:
    """Sleep for ``delay`` seconds or until ``stop`` fires.

    Returns ``True`` if the stop event fired, ``False`` if the sleep
    elapsed normally. Bounded by ``delay`` so a never-set stop event
    cannot hold the caller forever.
    """
    if delay <= 0:
        return stop.is_set()
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        return False
    return True


async def _open_listener_connection(
    dsn: str,
    *,
    application_name: str,
) -> asyncpg.Connection:
    """Open a fresh asyncpg connection tagged with ``application_name``.

    Kept as a thin module-level helper so tests can monkeypatch the
    open path without touching :func:`event_notify_listener_loop`.
    """
    return await asyncpg.connect(
        dsn=dsn,
        server_settings={"application_name": application_name},
    )


async def event_notify_listener_loop(
    broker: EventNotifyBroker,
    dsn: str | None,
    stop: asyncio.Event,
    *,
    backoffs: tuple[float, ...] = DEFAULT_RECONNECT_BACKOFFS,
    application_name: str = LISTENER_APPLICATION_NAME,
    state: _ListenerState | None = None,
) -> None:
    """Run the LISTEN-fan-out loop until ``stop`` fires.

    The loop owns one dedicated asyncpg connection at a time. On any
    failure (connect refused, ``pg_terminate_backend``, network blip)
    it logs at WARNING / ERROR, sleeps for the next entry in
    ``backoffs``, and retries — racing every sleep against ``stop`` so
    teardown does not have to wait out the cap.

    When ``dsn`` is ``None`` the loop logs once at WARNING and parks
    on ``stop.wait()`` until shutdown, instead of busy-looping on
    :class:`asyncpg.InvalidArgumentError`. This keeps unit tests that
    construct ``create_app`` without a real database from spamming the
    log.
    """
    state = state if state is not None else _ListenerState()

    if dsn is None:
        logger.warning("event_notify_listener: no DSN provided; listener parked until shutdown")
        await stop.wait()
        return

    while not stop.is_set():
        try:
            conn = await _open_listener_connection(dsn, application_name=application_name)
        except Exception as exc:
            state.last_error = f"{type(exc).__name__}: {exc}"
            backoff = backoffs[min(state.backoff_idx, len(backoffs) - 1)]
            state.backoff_idx = min(state.backoff_idx + 1, len(backoffs) - 1)
            state.reconnect_attempts += 1
            logger.warning(
                "event_notify_listener: connect failed (%s); retry in %.1fs",
                state.last_error,
                backoff,
            )
            stopped = await _wait_with_stop(stop, backoff)
            if stopped:
                return
            continue

        state.backoff_idx = 0
        state.connected = True
        terminated = asyncio.Event()

        def _on_termination(_connection: asyncpg.Connection) -> None:
            terminated.set()

        def _on_notify(
            _connection: asyncpg.Connection,
            _pid: int,
            _channel: str,
            payload: str,
        ) -> None:
            state.notifies_received += 1
            broker.fan_out_text(payload)

        try:
            conn.add_termination_listener(_on_termination)
            await conn.add_listener(NOTIFY_CHANNEL, _on_notify)
            logger.info(
                "event_notify_listener: connected to %s; LISTEN %s",
                application_name,
                NOTIFY_CHANNEL,
            )
            for evt in state.on_connect_events:
                evt.set()
            stop_task = asyncio.ensure_future(stop.wait())
            term_task = asyncio.ensure_future(terminated.wait())
            try:
                done, pending = await asyncio.wait(
                    {stop_task, term_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (stop_task, term_task):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(stop_task, term_task, return_exceptions=True)

            if stop_task in done or stop.is_set():
                return
            logger.warning("event_notify_listener: connection terminated by server; reconnecting")
        except Exception as exc:
            state.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("event_notify_listener: unexpected error in listener body")
        finally:
            state.connected = False
            try:
                await conn.remove_listener(NOTIFY_CHANNEL, _on_notify)
            except Exception:
                pass
            try:
                await asyncio.wait_for(conn.close(), timeout=2.0)
            except Exception:
                try:
                    conn.terminate()
                except Exception:
                    pass

        if stop.is_set():
            return
        backoff = backoffs[min(state.backoff_idx, len(backoffs) - 1)]
        state.backoff_idx = min(state.backoff_idx + 1, len(backoffs) - 1)
        state.reconnect_attempts += 1
        stopped = await _wait_with_stop(stop, backoff)
        if stopped:
            return


__all__ = [
    "DEFAULT_RECONNECT_BACKOFFS",
    "DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE",
    "EVENT_NOTIFY_LISTENER_TASK_NAME",
    "EventNotifyBroker",
    "LISTENER_APPLICATION_NAME",
    "NOTIFY_CHANNEL",
    "SLOW_SUBSCRIBER_CLOSE_CODE",
    "Subscriber",
    "_DropSentinel",
    "_ListenerState",
    "event_notify_listener_loop",
]
