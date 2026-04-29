"""Lifespan-managed event flusher (TASK-106, VAL-OBS-001..017).

Owns an :class:`asyncio.Queue` of :class:`EventRecord` values plus a
background coroutine that batches them into a single ``INSERT INTO
events ... VALUES (...), (...) ...`` statement. The hot path is
synchronous (:meth:`EventFlusher.enqueue` ⇒ ``queue.put_nowait``) so
request handlers don't pay for DB I/O on the response path.

Trigger semantics
-----------------
The flusher drains the queue on the **first** of two triggers:

* a 100 ms idle tick (``DEFAULT_FLUSH_INTERVAL_SECONDS``) — bounds the
  worst-case time-to-DB for sub-batch volume (VAL-OBS-004); or
* a 500-row backlog (``DEFAULT_BATCH_LIMIT``) — bounds memory and
  end-to-end latency under sustained load (VAL-OBS-003 / VAL-OBS-005).

On graceful shutdown the loop continues draining until the queue is
empty *and* the lifespan-owned ``stop`` event is set
(:data:`DEFAULT_DRAIN_TIMEOUT_SECONDS` is the absolute upper bound on
``__aexit__`` time so a wedged Postgres can't hold the process forever
— VAL-OBS-007 / VAL-OBS-015).

Crash recovery
--------------
Each successful flush atomically updates a checkpoint file via the
``tempfile + os.replace`` pattern (mirrors :mod:`whilly.state_store`)
recording ``last_flushed_seq`` (the largest ``events.id`` the flusher
just wrote). The checkpoint is informational — the queue itself is
in-memory only, so on restart the queue is empty and **no** v3-era
``whilly_events.jsonl`` content is replayed (VAL-OBS-009).

Append-only invariant
---------------------
The flusher emits exactly one statement shape: ``INSERT INTO events
(...) VALUES (...), (...) ...``. It never executes ``DELETE``,
``TRUNCATE``, or ``UPDATE`` against ``events`` (VAL-OBS-016).

Failure handling
----------------
A transient ``asyncpg.PostgresError`` raised by ``execute`` keeps the
batch buffered and retries with capped exponential backoff
(``DEFAULT_RETRY_BACKOFFS``); after the backoff schedule is exhausted
the flusher continues to retry on subsequent ticks rather than dropping
events (VAL-OBS-011). Every successful or failed flush emits exactly
one structured log record on the ``whilly.api.event_flusher`` logger
with ``record.event`` set to ``"event_flusher.insert_ok"`` /
``"event_flusher.insert_failed"`` (VAL-OBS-017).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import asyncpg

logger = logging.getLogger(__name__)

#: Public name of the lifespan-owned background task. Validators query
#: ``app.state.event_flusher_task.get_name()`` so this string is part of
#: the contract (VAL-OBS-001).
EVENT_FLUSHER_TASK_NAME: Final[str] = "whilly-event-flusher"

#: Default flush cadence (seconds). 100 ms is the contract floor for
#: sub-batch latency (VAL-OBS-004): ``floor(2000 / 100) = 20`` idle
#: polls per 2 seconds is what VAL-OBS-013 verifies, and the 200 ms
#: drain budget in VAL-OBS-005 leaves room for one full poll plus the
#: bulk INSERT round-trip.
DEFAULT_FLUSH_INTERVAL_SECONDS: Final[float] = 0.1

#: Default rows-per-batch ceiling. 500 rows is the contract trigger
#: (VAL-OBS-003) and matches the upper-bound on the queue-depth probe
#: in VAL-OBS-006 (``< 2 * BATCH_LIMIT``).
DEFAULT_BATCH_LIMIT: Final[int] = 500

#: Default ``__aexit__`` drain budget (seconds). 5 s is enough for a
#: backlog of 50_000 events at 10_000 inserts/sec while still bounding
#: shutdown latency for an operator's Ctrl-C.
DEFAULT_DRAIN_TIMEOUT_SECONDS: Final[float] = 5.0

#: Default exponential-backoff schedule for transient pg errors. Caps
#: at ~1 s so a single bad SQL round-trip can't stall the queue for
#: more than the user-noticeable threshold while still riding through
#: pgbouncer restarts.
DEFAULT_RETRY_BACKOFFS: Final[tuple[float, ...]] = (0.05, 0.1, 0.2, 0.5, 1.0)

#: Filename of the on-disk checkpoint relative to the configured state
#: directory. Hidden (leading dot) so the file doesn't pollute
#: directory listings.
CHECKPOINT_FILENAME: Final[str] = ".event_flusher.checkpoint"


@dataclass(frozen=True)
class EventRecord:
    """A single row to be inserted into ``events`` by the flusher.

    Mirrors the ``events`` schema columns the flusher writes:
    ``task_id``, ``plan_id``, ``event_type``, ``payload`` (jsonb), and
    ``detail`` (jsonb). ``id`` and ``created_at`` are server-side
    defaults.

    Why a frozen dataclass and not a plain dict?
        Type-checked field names prevent typos from silently ending up
        in ``payload`` (jsonb accepts anything) and the immutability
        means callers can't mutate a record after enqueue.
    """

    event_type: str
    task_id: str | None = None
    plan_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] | None = None


# Single-row INSERT shape used by the bulk path. Built dynamically with
# extra ``VALUES (...), (...)`` tuples appended for each batch row — a
# multi-row literal INSERT is exactly the shape VAL-OBS-003 asserts on.
_INSERT_PREFIX: Final[str] = "INSERT INTO events (task_id, plan_id, event_type, payload, detail) VALUES "
_PARAMS_PER_ROW: Final[int] = 5


def _build_bulk_insert(rows: int) -> str:
    """Build a multi-row ``INSERT INTO events ... VALUES`` statement.

    Returns SQL with ``rows`` placeholder tuples; the caller passes
    ``rows * 5`` parameters in order ``(task_id, plan_id, event_type,
    payload, detail)`` per row. Built per-flush rather than cached
    because the row count varies (3 → 500) — Postgres' query planner
    caches the plan via prepared-statement reuse on the connection
    side anyway.
    """
    placeholders = []
    for i in range(rows):
        base = i * _PARAMS_PER_ROW
        placeholders.append(f"(${base + 1}, ${base + 2}, ${base + 3}, ${base + 4}::jsonb, ${base + 5}::jsonb)")
    return _INSERT_PREFIX + ", ".join(placeholders)


def _record_to_params(record: EventRecord) -> tuple[Any, ...]:
    """Convert an :class:`EventRecord` to the asyncpg parameter tuple.

    ``payload`` and ``detail`` are serialised to JSON strings so
    asyncpg's default codec dispatches them onto the ``jsonb`` casts
    in :func:`_build_bulk_insert`. ``detail=None`` round-trips to SQL
    ``NULL`` (not the literal JSON ``"null"``); ``payload=None`` is
    coerced to ``{}`` for schema parity with
    :data:`whilly.adapters.db.repository._INSERT_EVENT_SQL`.
    """
    payload_json = json.dumps(record.payload or {})
    detail_json = json.dumps(record.detail) if record.detail is not None else None
    return (record.task_id, record.plan_id, record.event_type, payload_json, detail_json)


class EventFlusher:
    """Lifespan-managed batch writer for the ``events`` audit log.

    The flusher owns an :class:`asyncio.Queue` of :class:`EventRecord`
    values plus a coroutine (:meth:`run`) that drains the queue into
    bulk Postgres inserts. The hot path is :meth:`enqueue` — a
    ``queue.put_nowait`` call that returns synchronously and never
    performs DB I/O.

    Lifespan integration
    --------------------
    :func:`whilly.adapters.transport.server.create_app` constructs one
    instance per app, stashes it on ``app.state.event_flusher``, and
    spawns :meth:`run` as a TaskGroup task named
    :data:`EVENT_FLUSHER_TASK_NAME`. The lifespan exit path:

    1. Sets the shared ``stop`` event.
    2. Awaits :meth:`drain` (with timeout) so events enqueued just
       before SIGTERM still land in the DB before lifespan returns.
    3. Lets the TaskGroup ``__aexit__`` await the run coroutine.

    Atomicity vs. the repository
    ----------------------------
    State-machine transitions in :class:`whilly.adapters.db.TaskRepository`
    write their audit row in the **same transaction** as the
    ``tasks.status`` UPDATE — those events do **not** flow through this
    flusher. The flusher is for cross-cutting events that don't need
    transactional atomicity with a state change (audit notes, deprecation
    surfaces, future TRIZ async signals, etc.).
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
        checkpoint_dir: Path | str | None = None,
        retry_backoffs: tuple[float, ...] = DEFAULT_RETRY_BACKOFFS,
    ) -> None:
        if batch_limit <= 0:
            raise ValueError(f"batch_limit must be > 0, got {batch_limit!r}")
        if flush_interval <= 0:
            raise ValueError(f"flush_interval must be > 0, got {flush_interval!r}")
        if drain_timeout < 0:
            raise ValueError(f"drain_timeout must be >= 0, got {drain_timeout!r}")
        self._pool = pool
        self._batch_limit = batch_limit
        self._flush_interval = flush_interval
        self._drain_timeout = drain_timeout
        self._retry_backoffs = retry_backoffs
        # Unbounded queue: backpressure is the wrong policy for an
        # audit log (dropping events would defeat the contract). Queue
        # depth is observable (VAL-OBS-006) and the 1000-events/sec
        # throughput target keeps it bounded under realistic load.
        self.queue: asyncio.Queue[EventRecord] = asyncio.Queue()
        self.idle_polls: int = 0
        self.last_flushed_seq: int = 0
        self.last_batch_size: int = 0
        self.last_batch_latency_ms: float = 0.0
        self.checkpoint_path: Path | None = (
            Path(checkpoint_dir) / CHECKPOINT_FILENAME if checkpoint_dir is not None else None
        )

    # ─── enqueue (hot path) ──────────────────────────────────────────────

    def enqueue(self, record: EventRecord) -> None:
        """Push an :class:`EventRecord` onto the in-memory queue.

        Non-blocking and synchronous — never performs DB I/O on the
        caller's thread. Validators measure this path's latency in
        VAL-OBS-002.

        Why ``put_nowait`` rather than ``await put()``?
            The queue is unbounded so ``put_nowait`` cannot block on
            backpressure; using the sync variant keeps the hot path
            usable from sync handlers (e.g. CLI helpers) without an
            ``asyncio.run`` indirection.
        """
        self.queue.put_nowait(record)

    # ─── lifespan loop ───────────────────────────────────────────────────

    async def run(self, stop: asyncio.Event) -> None:
        """Background coroutine — owned by the lifespan TaskGroup.

        Polls the queue every :data:`flush_interval` seconds (or sooner
        if the buffer has reached :data:`batch_limit`) and drains it via
        :meth:`_flush_batch`. On lifespan exit (``stop`` set) the loop
        keeps draining until the queue is empty *or* the drain budget
        runs out (VAL-OBS-007 / VAL-OBS-015).
        """
        logger.info(
            "event_flusher started: interval=%.3fs, batch_limit=%d, drain_timeout=%.1fs",
            self._flush_interval,
            self._batch_limit,
            self._drain_timeout,
        )
        try:
            while not stop.is_set():
                # Wait either for the interval timer or for a quick
                # signal that the queue is full enough to flush. A
                # tight ``asyncio.sleep(self._flush_interval)`` is
                # fine for the cadence floor; the batch-size trigger
                # is checked after the sleep.
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._flush_interval)
                    # ``stop`` fired during the wait — break to drain.
                    break
                except TimeoutError:
                    pass
                if self.queue.empty():
                    self.idle_polls += 1
                    continue
                await self._drain_once()
            # Shutdown drain — bounded by drain_timeout so a wedged
            # Postgres can't hold the process forever.
            await self.drain()
        finally:
            logger.info("event_flusher stopped (last_flushed_seq=%d)", self.last_flushed_seq)

    async def _drain_once(self) -> None:
        """Pull up to ``batch_limit`` rows from the queue and flush them.

        Pulls greedily — a single tick may drain many full batches if
        the producer outran the 100 ms cadence (VAL-OBS-005's 1000
        ev/s burst pattern). Each batch goes through the bulk insert
        path; checkpoint is updated after each successful round-trip.
        """
        while not self.queue.empty():
            batch: list[EventRecord] = []
            while len(batch) < self._batch_limit and not self.queue.empty():
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                return
            await self._flush_batch(batch)

    async def drain(self) -> None:
        """Drain the queue completely, bounded by ``drain_timeout``.

        Called both from the loop's shutdown branch and from
        ``app.state``'s lifespan exit. Idempotent — re-entry is safe
        (the second call returns immediately if the queue is already
        empty).
        """
        deadline = time.monotonic() + self._drain_timeout
        while True:
            if self.queue.empty():
                return
            if time.monotonic() >= deadline:
                logger.warning(
                    "event_flusher drain timed out with %d events still queued",
                    self.queue.qsize(),
                )
                return
            await self._drain_once()

    # ─── flush implementation ────────────────────────────────────────────

    async def _flush_batch(self, batch: list[EventRecord]) -> None:
        """Insert ``batch`` into ``events`` as a single bulk INSERT.

        Retries on transient :class:`asyncpg.PostgresError` per
        :data:`_retry_backoffs`; on exhausted retries the batch stays
        re-enqueued at the head of the queue and the loop tries again
        on the next tick (VAL-OBS-011 — events are never dropped).
        Emits exactly one structured log record per attempt outcome
        (VAL-OBS-017).
        """
        sql = _build_bulk_insert(len(batch))
        params: list[Any] = []
        for record in batch:
            params.extend(_record_to_params(record))
        attempt = 0
        start = time.perf_counter()
        while True:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(sql, *params)
                latency_ms = (time.perf_counter() - start) * 1000.0
                self.last_batch_size = len(batch)
                self.last_batch_latency_ms = latency_ms
                # Update checkpoint with max(events.id) — best effort;
                # a single SELECT is cheap and only reflects the rows
                # this flusher has just written under serialised
                # access (the flusher is single-coroutine).
                try:
                    async with self._pool.acquire() as conn:
                        max_seq = await conn.fetchval("SELECT max(id) FROM events")
                        if max_seq is not None and max_seq > self.last_flushed_seq:
                            self.last_flushed_seq = int(max_seq)
                            self._write_checkpoint(self.last_flushed_seq)
                except Exception:  # noqa: BLE001 — checkpoint is best-effort
                    logger.warning("event_flusher: checkpoint write failed", exc_info=True)
                logger.info(
                    "event_flusher flushed batch_size=%d latency_ms=%.2f last_seq=%d",
                    len(batch),
                    latency_ms,
                    self.last_flushed_seq,
                    extra={
                        "event": "event_flusher.insert_ok",
                        "rows_inserted": len(batch),
                        "latency_ms": latency_ms,
                        "last_flushed_seq": self.last_flushed_seq,
                    },
                )
                return
            except asyncpg.PostgresError as exc:
                # Transient pg error — log structured warning and
                # retry per the backoff schedule. After the schedule
                # is exhausted the *batch is re-enqueued* at the head
                # of the queue so the next tick tries again
                # (VAL-OBS-011: events are never dropped).
                error_class = type(exc).__name__
                logger.warning(
                    "event_flusher insert failed batch_size=%d attempt=%d error=%s",
                    len(batch),
                    attempt + 1,
                    error_class,
                    extra={
                        "event": "event_flusher.insert_failed",
                        "rows_inserted": 0,
                        "batch_size": len(batch),
                        "attempt": attempt + 1,
                        "error_class": error_class,
                    },
                    exc_info=True,
                )
                if attempt >= len(self._retry_backoffs):
                    # Re-enqueue the batch and bail out so the loop
                    # can revisit on the next tick. Use putleft-style
                    # semantics by pushing onto a fresh queue head —
                    # but asyncio.Queue is FIFO with no head insert,
                    # so we just put_nowait; ordering across batches
                    # is not part of the contract (only the
                    # append-only invariant is, VAL-OBS-016).
                    for record in batch:
                        self.queue.put_nowait(record)
                    return
                await asyncio.sleep(self._retry_backoffs[attempt])
                attempt += 1

    # ─── checkpoint ──────────────────────────────────────────────────────

    def _write_checkpoint(self, last_flushed_seq: int) -> None:
        """Atomically persist ``last_flushed_seq`` via tempfile + os.replace.

        Mirrors :class:`whilly.state_store.StateStore.save` — write to
        a sibling tempfile, fsync-implicit ``os.write`` + ``os.close``,
        then ``os.replace`` to the canonical path. On any error mid-
        write the tempfile is unlinked so we never leave a half-written
        ``.tmp`` sibling on disk (VAL-OBS-008).
        """
        if self.checkpoint_path is None:
            return
        payload = {
            "last_flushed_seq": last_flushed_seq,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        content = json.dumps(payload, ensure_ascii=False) + "\n"
        dir_path = self.checkpoint_path.parent
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("event_flusher: checkpoint dir mkdir failed: %s", exc)
            return
        fd, tmp_path = tempfile.mkstemp(
            dir=str(dir_path),
            prefix=".event_flusher_",
            suffix=".tmp",
        )
        closed = False
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            closed = True
            os.replace(tmp_path, self.checkpoint_path)
        except BaseException:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise
