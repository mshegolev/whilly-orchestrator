"""Parent-side bounded JSON transport for the isolated Cognee worker."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from contextlib import nullcontext
from pathlib import Path
from typing import AsyncContextManager, Awaitable, Callable

from whilly.swarm.learning.retrieval import RetrievalRecord, RetrievalRequest, RetrievalResult, RetrievalUnavailable
from .cognee_sandbox import allowed_environment, sandbox_command

MAX_INPUT_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
MAX_RECORDS = 32
MAX_QUERY_CHARS = 4096


class ProtocolError(ValueError):
    pass


def encode_request(request: RetrievalRequest) -> str:
    if type(request) is not RetrievalRequest or type(request.query) is not str or type(request.records) is not tuple:
        raise ProtocolError("input_schema")
    for record in request.records:
        if type(record) is not RetrievalRecord or type(record.id) is not str or type(record.body) is not str:
            raise ProtocolError("input_schema")
    if len({record.id for record in request.records}) != len(request.records):
        raise ProtocolError("input_ids")
    if len(request.records) > MAX_RECORDS:
        raise ProtocolError("record_limit")
    if len(request.query) > MAX_QUERY_CHARS:
        raise ProtocolError("query_limit")
    payload = {"version": 1, "query": request.query, "records": [{"id": r.id, "body": r.body} for r in request.records]}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProtocolError("input_encoding") from exc
    if size > MAX_INPUT_BYTES:
        raise ProtocolError("input_limit")
    return encoded


def decode_result(raw: str, request: RetrievalRequest) -> RetrievalResult:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError("malformed_output") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "ids", "elapsed_ms"}
        or type(payload.get("version")) is not int
        or payload["version"] != 1
    ):
        raise ProtocolError("output_schema")
    ids = payload["ids"]
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ProtocolError("output_ids")
    allowed = {record.id for record in request.records}
    if len(ids) != len(set(ids)) or not set(ids) <= allowed:
        raise ProtocolError("output_ids")
    elapsed_ms = payload["elapsed_ms"]
    if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int) or elapsed_ms < 0:
        raise ProtocolError("output_elapsed")
    return RetrievalResult(tuple(ids), elapsed_ms)


def build_command(runtime: Path, request_dir: Path, model_dirs: tuple[Path, ...] = ()) -> list[str]:
    worker = Path(__file__).with_name("cognee_worker.py")
    return sandbox_command(
        [str(runtime / "bin" / "python"), str(worker)],
        runtime=runtime,
        request_dir=request_dir,
        model_dirs=model_dirs,
        worker_dir=worker.parent,
    )


class CogneeRetriever:
    def __init__(
        self,
        runtime: Path,
        *,
        models_dir: Path,
        process_factory: Callable[..., Awaitable[asyncio.subprocess.Process]] | None = None,
        command_factory: Callable[[Path, Path, Path], list[str]] | None = None,
        lock_fds: tuple[int, ...] = (),
    ) -> None:
        self.runtime = runtime
        self.models_dir = models_dir
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self.command_factory = command_factory
        self.lock_fds = lock_fds
        self.last_child_alive = False

    async def rank(
        self,
        request: RetrievalRequest,
        *,
        request_dir: Path,
        deadline: float,
        lock_fds: tuple[int, ...] = (),
        launch_guard: Callable[[], AsyncContextManager[None]] | None = None,
    ) -> RetrievalResult:
        try:
            encoded = encode_request(request)
        except ProtocolError as exc:
            raise RetrievalUnavailable(str(exc)) from None
        spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
        process = None
        readers: set[asyncio.Task] = set()
        # Reserve one second of the <=90s operation budget for normal cleanup.
        # Safety cleanup may exceed it: the caller must retain leases until rank returns.
        started = time.monotonic()
        budget = max(0.0, min(deadline - started, 90.0))
        operation_end = started + budget - min(1.0, budget * 0.1)
        try:
            if time.monotonic() >= deadline:
                raise RetrievalUnavailable("timeout")
            command = (
                self.command_factory(self.runtime, request_dir, self.models_dir)
                if self.command_factory
                else build_command(self.runtime, request_dir, (self.models_dir,))
            )
            pass_fds = tuple(dict.fromkeys((*self.lock_fds, *lock_fds)))
            async with asyncio.timeout(max(0, operation_end - time.monotonic())):
                async with launch_guard() if launch_guard is not None else nullcontext():
                    # The guard owns the cross-process source/tombstone check.
                    # Do not create even the spawn task before it grants entry.
                    if asyncio.current_task().cancelling():
                        raise asyncio.CancelledError
                    if time.monotonic() >= operation_end:
                        raise RetrievalUnavailable("timeout")
                    spawn = asyncio.create_task(
                        self.process_factory(
                            *command,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            start_new_session=True,
                            pass_fds=pass_fds,
                            cwd=str(request_dir),
                            env=allowed_environment(request_dir, self.models_dir),
                            limit=MAX_OUTPUT_BYTES,
                        )
                    )
                    try:
                        process = await asyncio.shield(spawn)
                        self.last_child_alive = True
                    except BaseException:
                        # Late spawn + kill/reap must finish before releasing the fence.
                        try:
                            await self._finish_cleanup(spawn, readers)
                        finally:
                            spawn = None
                        raise
            payload = (encoded + "\n").encode()
            streams = {"stdout": process.stdout, "stderr": process.stderr}
            results: dict[str, bytes] = {}
            names: dict[asyncio.Task[bytes | None], str] = {}
            for name, stream in streams.items():
                task = asyncio.create_task(_read_bounded(stream, MAX_OUTPUT_BYTES))
                readers.add(task)
                names[task] = name

            async def send() -> None:
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()

            sender = asyncio.create_task(send())
            readers.add(sender)
            pending = set(readers)
            while pending:
                done, pending = await asyncio.wait(
                    pending, timeout=max(0, operation_end - time.monotonic()), return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    raise RetrievalUnavailable("timeout")
                for task in done:
                    value = task.result()
                    if task is sender:
                        continue
                    if value is None:
                        raise RetrievalUnavailable("output_limit")
                    results[names[task]] = value
            stdout = results["stdout"]
            await asyncio.wait_for(process.wait(), timeout=max(0, operation_end - time.monotonic()))
            if process.returncode != 0:
                raise RetrievalUnavailable("worker_exit")
            try:
                return decode_result(stdout.decode("utf-8"), request)
            except UnicodeDecodeError:
                raise RetrievalUnavailable("output_encoding") from None
            except ProtocolError as exc:
                raise RetrievalUnavailable(str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise RetrievalUnavailable("timeout") from exc
        except RetrievalUnavailable:
            raise
        except (OSError, RuntimeError):
            raise RetrievalUnavailable("worker_start_failed" if process is None else "worker_io_failed") from None
        finally:
            if spawn is not None:
                await self._finish_cleanup(spawn, readers)

    async def _finish_cleanup(
        self, spawn: asyncio.Task[asyncio.subprocess.Process], readers: set[asyncio.Task]
    ) -> None:
        cleanup = asyncio.create_task(self._cleanup(spawn, readers))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _cleanup(self, spawn: asyncio.Task[asyncio.subprocess.Process], readers: set[asyncio.Task]) -> None:
        try:
            process = await spawn
        except Exception:
            return
        try:
            await self._terminate(process)
            self.last_child_alive = False
        finally:
            await self._cancel_readers(readers)

    async def _cancel_readers(self, readers: set[asyncio.Task]) -> None:
        for reader in readers:
            reader.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin may return EPERM for an already-empty process group.
            # Still wait for actual exit; never release ownership based on EPERM.
            pass
        await process.wait()
        # wait() only reaps the leader; acknowledge only after the group disappears.
        while True:
            try:
                if not await _group_present(process.pid):
                    return
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, RetrievalUnavailable):
                # Verification failure is not evidence of exit. Keep ownership and retry.
                pass
            await asyncio.sleep(0.02)


async def _group_present(pgid: int) -> bool:
    probe = await asyncio.create_subprocess_exec(
        "/bin/ps",
        "-axo",
        "pgid=",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    found = False
    async for line in probe.stdout:
        if line.strip() == str(pgid).encode():
            found = True
    if await probe.wait() != 0:
        raise RetrievalUnavailable("cleanup_verification_failed")
    return found


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(min(4096, limit + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return None


__all__ = ["CogneeRetriever", "ProtocolError", "decode_result", "encode_request"]
