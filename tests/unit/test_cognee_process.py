from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from whilly.swarm.learning.retrieval import RetrievalRecord, RetrievalRequest


def test_protocol_rejects_invalid_output() -> None:
    from whilly.adapters.memory.cognee_process import ProtocolError, decode_result

    request = RetrievalRequest("q", (RetrievalRecord("a", "body"),))
    for payload in (
        {"version": True, "ids": ["a"], "elapsed_ms": 1},
        {"version": 1.0, "ids": ["a"], "elapsed_ms": 1},
        {"version": 1, "ids": ["a", "a"], "elapsed_ms": 1},
        {"version": 1, "ids": ["foreign"], "elapsed_ms": 1},
        {"version": 1, "ids": ["a"], "elapsed_ms": 1, "body": "leak"},
    ):
        with pytest.raises(ProtocolError):
            decode_result(json.dumps(payload), request)
    with pytest.raises(ProtocolError):
        decode_result('{"version": 1, "ids": [', request)


def test_utf8_input_cap() -> None:
    from whilly.adapters.memory.cognee_process import encode_request

    records = tuple(RetrievalRecord(str(i), "ж" * 1900) for i in range(32))
    encoded = encode_request(RetrievalRequest("q", records))
    assert len(encoded.encode("utf-8")) <= 131072


@pytest.mark.parametrize(
    "invalid",
    [
        RetrievalRequest(123, ()),
        RetrievalRequest("q", [RetrievalRecord("a", "body")]),
        RetrievalRequest("q", ({"id": "a", "body": "body"},)),
        RetrievalRequest("q", (RetrievalRecord(1, "body"),)),
        RetrievalRequest("q", (RetrievalRecord("a", None),)),
        RetrievalRequest("q", (RetrievalRecord("a", "one"), RetrievalRecord("a", "two"))),
    ],
)
def test_invalid_request_is_named(invalid) -> None:
    from whilly.adapters.memory.cognee_process import ProtocolError, encode_request

    with pytest.raises(ProtocolError):
        encode_request(invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["spawn_error", "spawn_timeout"])
async def test_spawn_failure_is_named(tmp_path: Path, failure: str) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    async def spawn(*args, **kwargs):
        if failure == "spawn_error":
            raise OSError("private diagnostic must not escape")
        await asyncio.sleep(0.08)
        raise OSError("delayed failure")

    retriever = CogneeRetriever(
        tmp_path, models_dir=tmp_path, process_factory=spawn, command_factory=lambda *_: [sys.executable]
    )
    with pytest.raises(RetrievalUnavailable) as error:
        await retriever.rank(
            RetrievalRequest("q", ()), request_dir=tmp_path, deadline=asyncio.get_running_loop().time() + 0.02
        )
    assert error.value.code == ("worker_start_failed" if failure == "spawn_error" else "timeout")


@pytest.mark.asyncio
async def test_sandbox_start_failure_is_named(tmp_path: Path) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    def command(*args):
        raise RuntimeError("sandbox_unavailable")

    retriever = CogneeRetriever(tmp_path, models_dir=tmp_path, command_factory=command)
    with pytest.raises(RetrievalUnavailable, match="worker_start_failed"):
        await retriever.rank(
            RetrievalRequest("q", ()), request_dir=tmp_path, deadline=asyncio.get_running_loop().time() + 2
        )


async def wait_started(path: Path) -> int:
    async with asyncio.timeout(2):
        while not path.exists() or not path.read_text():
            await asyncio.sleep(0.01)
    return int(path.read_text())


async def assert_pid_gone(pid: int) -> None:
    async with asyncio.timeout(2):
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel", "leader_exit"])
async def test_started_descendant_is_gone_before_ack(tmp_path: Path, mode: str) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    # The descendant itself publishes its PID only after installing SIGTERM ignore.
    child = "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open('ready','w').write(str(os.getpid())); time.sleep(30)"
    script = tmp_path / "family.py"
    script.write_text(
        "import subprocess,sys,time,os,json\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
        "while not os.path.exists('ready'): time.sleep(.005)\n"
        + ("sys.exit(0)\n" if mode == "leader_exit" else "time.sleep(30)\n")
    )
    spawned = []

    async def spawn(*args, **kwargs):
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        spawned.append(process)
        return process

    retriever = CogneeRetriever(
        tmp_path, models_dir=tmp_path, process_factory=spawn, command_factory=lambda *_: [sys.executable, str(script)]
    )
    before = asyncio.all_tasks()
    task = asyncio.create_task(
        retriever.rank(
            RetrievalRequest("q", ()), request_dir=tmp_path, deadline=asyncio.get_running_loop().time() + 0.6
        )
    )
    pid = await wait_started(tmp_path / "ready")
    os.kill(pid, 0)
    if mode == "cancel":
        task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError if mode == "cancel" else RetrievalUnavailable):
            await task
        assert spawned[0].returncode is not None
        await assert_pid_gone(spawned[0].pid)
        # Require group disappearance at acknowledgement, not just eventually.
        from whilly.adapters.memory.cognee_process import _group_present

        assert not await _group_present(spawned[0].pid)
        await assert_pid_gone(pid)
        assert not (asyncio.all_tasks() - before - {asyncio.current_task()})
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            os.killpg(spawned[0].pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.mark.asyncio
async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    script = tmp_path / "sleep_child.py"
    script.write_text(
        "import subprocess, sys, time; p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "open('grandchild.pid', 'w').write(str(p.pid)); time.sleep(30)"
    )
    retriever = CogneeRetriever(tmp_path, models_dir=tmp_path, command_factory=lambda *_: [sys.executable, str(script)])
    with pytest.raises(RetrievalUnavailable, match="timeout"):
        await retriever.rank(
            RetrievalRequest("q", ()), request_dir=tmp_path, deadline=asyncio.get_running_loop().time() + 0.1
        )
    assert retriever.last_child_alive is False
    assert (tmp_path / "grandchild.pid").exists()
    await assert_pid_gone(int((tmp_path / "grandchild.pid").read_text()))


@pytest.mark.asyncio
async def test_cancel_reaps_child(tmp_path: Path) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever

    script = tmp_path / "sleep_child.py"
    script.write_text("import time; time.sleep(30)")
    retriever = CogneeRetriever(tmp_path, models_dir=tmp_path, command_factory=lambda *_: [sys.executable, str(script)])
    task = asyncio.create_task(retriever.rank(RetrievalRequest("q", ()), request_dir=tmp_path, deadline=9999999999.0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert retriever.last_child_alive is False


@pytest.mark.asyncio
async def test_stdout_cap(tmp_path: Path) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    script = tmp_path / "loud_child.py"
    script.write_text("import sys; sys.stdout.write('x' * 20000); sys.stdout.flush()")
    retriever = CogneeRetriever(tmp_path, models_dir=tmp_path, command_factory=lambda *_: [sys.executable, str(script)])
    with pytest.raises(RetrievalUnavailable, match="output_limit"):
        await retriever.rank(
            RetrievalRequest("q", ()), request_dir=tmp_path, deadline=asyncio.get_running_loop().time() + 2
        )


@pytest.mark.asyncio
async def test_stderr_cap_after_stdout_eof_stops_promptly(tmp_path: Path) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    script = tmp_path / "stderr_loud_child.py"
    script.write_text("import os,time; os.close(1); time.sleep(.05); os.write(2,b'x'*20000); time.sleep(30)")
    retriever = CogneeRetriever(tmp_path, models_dir=tmp_path, command_factory=lambda *_: [sys.executable, str(script)])
    with pytest.raises(RetrievalUnavailable, match="output_limit"):
        await retriever.rank(
            RetrievalRequest("q", ()), request_dir=tmp_path, deadline=asyncio.get_running_loop().time() + 2
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_delayed_spawn_retains_cleanup_until_actual_reap(tmp_path: Path, mode: str) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    created = asyncio.Event()
    release_handle = asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        processes.append(process)
        created.set()
        await release_handle.wait()
        return process

    retriever = CogneeRetriever(
        tmp_path,
        models_dir=tmp_path,
        process_factory=delayed_spawn,
        command_factory=lambda *_: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    task = asyncio.create_task(
        retriever.rank(
            RetrievalRequest("q", ()), request_dir=tmp_path, deadline=asyncio.get_running_loop().time() + 0.1
        )
    )
    await asyncio.wait_for(created.wait(), 2)
    if mode == "cancel":
        task.cancel()
    await asyncio.sleep(0.15)
    assert not task.done()  # Operation deadline elapsed; ownership is still held.
    if mode == "cancel":
        task.cancel()  # Repeated cancellation must not interrupt cleanup.
    release_handle.set()
    with pytest.raises(asyncio.CancelledError if mode == "cancel" else RetrievalUnavailable):
        await task
    assert processes[0].returncode == -signal.SIGKILL
    await assert_pid_gone(processes[0].pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("noisy_fd", [1, 2])
async def test_overflow_reaps_promptly_even_other_stream_eof(tmp_path: Path, noisy_fd: int) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    processes = []

    async def spawn(*args, **kwargs):
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        processes.append(process)
        return process

    code = f"import os,time; os.close({3 - noisy_fd}); time.sleep(.05); os.write({noisy_fd},b'x'*20000); time.sleep(30)"
    retriever = CogneeRetriever(
        tmp_path, models_dir=tmp_path, process_factory=spawn, command_factory=lambda *_: [sys.executable, "-c", code]
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(RetrievalUnavailable, match="output_limit"):
        await retriever.rank(RetrievalRequest("q", ()), request_dir=tmp_path, deadline=started + 5)
    assert asyncio.get_running_loop().time() - started < 1.5
    assert processes[0].returncode == -signal.SIGKILL
    await assert_pid_gone(processes[0].pid)


@pytest.mark.asyncio
async def test_successful_json_stdout_and_diagnostic_stderr(tmp_path: Path) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever

    script = tmp_path / "successful_child.py"
    script.write_text(
        "import json, sys; print('diagnostic', file=sys.stderr); "
        "print(json.dumps({'version': 1, 'ids': ['known'], 'elapsed_ms': 1}))"
    )
    retriever = CogneeRetriever(tmp_path, models_dir=tmp_path, command_factory=lambda *_: [sys.executable, str(script)])
    result = await retriever.rank(
        RetrievalRequest("q", (RetrievalRecord("known", "body"),)),
        request_dir=tmp_path,
        deadline=asyncio.get_running_loop().time() + 2,
    )
    assert result.ids == ("known",)


def test_production_command_uses_runtime_and_sandbox(tmp_path: Path) -> None:
    from whilly.adapters.memory.cognee_process import build_command

    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "python").symlink_to(sys.executable)
    command = build_command(runtime, tmp_path, (tmp_path / "models",))
    assert command[0].endswith("sandbox-exec")
    assert Path(command[-2]).resolve() == (runtime / "bin" / "python").resolve()
    assert command[-1].endswith("cognee_worker.py")


@pytest.mark.asyncio
async def test_launch_guard_tombstone_rejects_before_process_factory(tmp_path: Path) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    called = False

    async def spawn(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("spawn forbidden after tombstone")

    @asynccontextmanager
    async def guard():
        raise RetrievalUnavailable("memory_redacted")
        yield  # async context manager whose acquisition rejects the tombstone

    retriever = CogneeRetriever(
        tmp_path, models_dir=tmp_path, process_factory=spawn, command_factory=lambda *_: [sys.executable]
    )
    with pytest.raises(RetrievalUnavailable, match="memory_redacted"):
        await retriever.rank(
            RetrievalRequest("q", ()),
            request_dir=tmp_path,
            deadline=asyncio.get_running_loop().time() + 2,
            launch_guard=guard,
        )
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_launch_guard_holds_until_late_spawn_cancel_reaped(tmp_path: Path, mode: str) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever, RetrievalUnavailable

    held = False
    created = asyncio.Event()
    release = asyncio.Event()
    processes = []

    @asynccontextmanager
    async def guard():
        nonlocal held
        held = True
        try:
            yield
        finally:
            assert processes[0].returncode == -signal.SIGKILL
            await assert_pid_gone(processes[0].pid)
            held = False

    async def spawn(*args, **kwargs):
        assert held
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        processes.append(process)
        created.set()
        await release.wait()
        assert held
        return process

    retriever = CogneeRetriever(
        tmp_path,
        models_dir=tmp_path,
        process_factory=spawn,
        command_factory=lambda *_: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    task = asyncio.create_task(
        retriever.rank(
            RetrievalRequest("q", ()),
            request_dir=tmp_path,
            deadline=asyncio.get_running_loop().time() + (0.15 if mode == "timeout" else 2),
            launch_guard=guard,
        )
    )
    await asyncio.wait_for(created.wait(), 1)
    if mode == "cancel":
        task.cancel()
    await asyncio.sleep(0.2 if mode == "timeout" else 0.02)
    assert held and not task.done()
    if mode == "cancel":
        task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError if mode == "cancel" else RetrievalUnavailable):
        await task
    assert not held


@pytest.mark.asyncio
async def test_launch_guard_released_before_protocol_io(tmp_path: Path) -> None:
    from whilly.adapters.memory.cognee_process import CogneeRetriever

    @asynccontextmanager
    async def guard():
        yield
        (tmp_path / "released").touch()

    code = (
        "import os,time,json,sys\n"
        "while not os.path.exists('released'): time.sleep(.005)\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'version':1,'ids':[],'elapsed_ms':0}))\n"
    )
    retriever = CogneeRetriever(tmp_path, models_dir=tmp_path, command_factory=lambda *_: [sys.executable, "-c", code])
    result = await retriever.rank(
        RetrievalRequest("q", ()),
        request_dir=tmp_path,
        deadline=asyncio.get_running_loop().time() + 2,
        launch_guard=guard,
    )
    assert result.ids == ()
