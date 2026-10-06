from __future__ import annotations

from whilly.adapters.filesystem.memory_leases import LeaseStore
import asyncio
import multiprocessing
import os
from pathlib import Path
import stat
import pytest
import fcntl
import json


@pytest.mark.parametrize(
    "manifest",
    [
        '{"lease_id":"foreign","revision_ids":["r"]}',
        "{broken",
        "[]",
        json.dumps({"lease_id": "a" * 32, "revision_ids": "r"}),
        json.dumps({"lease_id": "a" * 32, "revision_ids": [None]}),
    ],
)
def test_boot_foreign_or_malformed_manifest_is_untouched(tmp_path, manifest):
    directory = tmp_path / "leases" / ("a" * 32)
    directory.mkdir(parents=True)
    (directory / "owner.lock").touch()
    (directory / "manifest.json").write_text(manifest)
    (directory / "payload").write_text("foreign body")
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert asyncio.run(LeaseStore(directory.parent).boot_cleanup()) == ()
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.parametrize("boot", [False, True])
@pytest.mark.parametrize("failure", ["owner.lock", "cancel", "manifest.json", "rmdir"])
def test_metadata_failure_retains_revision_across_restart(tmp_path, monkeypatch, boot, failure):
    original_unlink, original_rmdir = Path.unlink, Path.rmdir
    deletions = []

    def fail_unlink(path, *args, **kwargs):
        if path.parent == directory and path.name in {"owner.lock", "cancel", "manifest.json"}:
            deletions.append(path.name)
            if path.name == failure:
                raise PermissionError("injected metadata failure")
        return original_unlink(path, *args, **kwargs)

    def fail_rmdir(path, *args, **kwargs):
        if path == directory and failure == "rmdir":
            raise PermissionError("injected directory failure")
        return original_rmdir(path, *args, **kwargs)

    async def run():
        nonlocal directory
        store = LeaseStore(tmp_path / "leases")
        async with store.reserve(asyncio.get_running_loop().time() + 5) as lease:
            directory = lease.directory
            await lease.register(("retained-revision",))
            (directory / "cancel").touch()
            (directory / "payload").write_text("body")
            if boot:
                (directory / "retain").symlink_to(tmp_path / "outside")
            else:
                monkeypatch.setattr(Path, "unlink", fail_unlink)
                monkeypatch.setattr(Path, "rmdir", fail_rmdir)
        if boot:
            (directory / "retain").unlink()
            monkeypatch.setattr(Path, "unlink", fail_unlink)
            monkeypatch.setattr(Path, "rmdir", fail_rmdir)
            assert await store.boot_cleanup() == ()
        if "manifest.json" in deletions:
            assert deletions[-1] == "manifest.json"
        assert json.loads((directory / "manifest.json").read_text())["revision_ids"] == ["retained-revision"]
        restarted = LeaseStore(store.root)
        assert await restarted.pending((lease.id,))
        assert await restarted.pending(("retained-revision",))
        monkeypatch.setattr(Path, "unlink", original_unlink)
        monkeypatch.setattr(Path, "rmdir", original_rmdir)
        assert await restarted.boot_cleanup() == (lease.id,)
        assert not await restarted.pending(("retained-revision",))

    directory = None
    asyncio.run(run())


@pytest.mark.parametrize("boot", [False, True])
def test_nested_metadata_names_are_payload(tmp_path, boot):
    async def run():
        store = LeaseStore(tmp_path / "leases")
        async with store.reserve(asyncio.get_running_loop().time() + 5) as lease:
            await lease.register(("r",))
            payload = lease.directory / "payload"
            payload.mkdir()
            for name in ("manifest.json", "owner.lock", "cancel"):
                (payload / name).write_text("copied body")
            if boot:
                (lease.directory / "retain").symlink_to(tmp_path / "outside")
        if boot:
            (lease.directory / "retain").unlink()
            assert await store.boot_cleanup() == (lease.id,)
        assert not lease.directory.exists()
        assert not await store.pending((lease.id,))

    asyncio.run(run())


def test_boot_owner_open_failure_preserves_artifacts(tmp_path, monkeypatch):
    directory = tmp_path / "leases" / ("a" * 32)
    directory.mkdir(parents=True)
    (directory / "owner.lock").touch()
    (directory / "manifest.json").write_text(json.dumps({"lease_id": directory.name, "revision_ids": ["r"]}))
    original = os.open

    def denied(path, *args, **kwargs):
        if Path(path) == directory / "owner.lock":
            raise PermissionError("injected")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    store = LeaseStore(directory.parent)
    assert asyncio.run(store.boot_cleanup()) == ()
    assert (directory / "manifest.json").exists()


def _hold_lease(root: str, ready, release) -> None:
    async def run() -> None:
        store = LeaseStore(__import__("pathlib").Path(root), slots=1)
        async with store.reserve(asyncio.get_running_loop().time() + 5) as lease:
            await lease.register(("revision-a",))
            ready.set()
            release.wait(5)

    asyncio.run(run())


def _queued_worker(root: str, entered, release, waiting) -> None:
    async def run() -> None:
        class ObservedStore(LeaseStore):
            async def _acquire_lock(self, fd, deadline):
                if os.fstat(fd).st_ino == self._execution_path.stat().st_ino:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    waiting.wait(10)
                await super()._acquire_lock(fd, deadline)

        store = ObservedStore(Path(root))
        async with store.reserve(asyncio.get_running_loop().time() + 20):
            entered.set()
            assert release.wait(10)

    asyncio.run(run())


def test_lease_store_is_available(tmp_path) -> None:
    store = LeaseStore(tmp_path / "leases")
    assert store.root == tmp_path / "leases"


def test_second_process_waits_for_slot_and_real_manifest(tmp_path) -> None:
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_lease, args=(str(tmp_path / "leases"), ready, release))
    process.start()
    assert ready.wait(5)
    manifests = list((tmp_path / "leases").glob("*/manifest.json"))
    assert len(manifests) == 1
    release.set()
    process.join(5)
    assert process.exitcode == 0


def test_one_active_four_waiters(tmp_path) -> None:
    ctx = multiprocessing.get_context("spawn")
    waiting = ctx.Barrier(5)
    entered = [ctx.Event() for _ in range(4)]
    release = ctx.Event()
    processes = []

    async def run():
        store = LeaseStore(tmp_path / "leases")
        async with store.reserve(asyncio.get_running_loop().time() + 20):
            for event in entered:
                process = ctx.Process(target=_queued_worker, args=(str(store.root), event, release, waiting))
                process.start()
                processes.append(process)
            waiting.wait(10)
            assert all(not event.is_set() for event in entered)
            with pytest.raises(RuntimeError, match="queue_full"):
                async with store.reserve(asyncio.get_running_loop().time() + 20):
                    pytest.fail("sixth reservation admitted")
        release.set()

    try:
        asyncio.run(run())
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert all(event.is_set() for event in entered)
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)


def _crashing_owner(root, ready, release, closed):
    async def run():
        store = LeaseStore(Path(root))
        async with store.reserve(asyncio.get_running_loop().time() + 10) as lease:
            await lease.register(("crash-revision",))
            if os.fork() == 0:
                ready.wait(10)
                if not release.wait(15):
                    os._exit(2)
                for fd in lease.lock_fds:
                    os.close(fd)
                closed.set()
                os._exit(0)
            os._exit(137)

    asyncio.run(run())


def test_crashed_parent_child_inherits_all_locks(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release, closed = ctx.Barrier(2), ctx.Event(), ctx.Event()
    store = LeaseStore(tmp_path / "leases")
    owner = ctx.Process(target=_crashing_owner, args=(str(store.root), ready, release, closed))
    owner.start()
    try:
        ready.wait(10)
        owner.join(10)
        assert owner.exitcode == 137
        (manifest,) = store.root.glob("*/manifest.json")
        lease_id = manifest.parent.name
        for path in (store.root / "execution.lock", store.root / "slot-0.lock", manifest.parent / "owner.lock"):
            fd = os.open(path, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        restarted = LeaseStore(store.root)
        assert asyncio.run(restarted.boot_cleanup()) == ()
        assert manifest.exists()
        assert asyncio.run(restarted.pending((lease_id,)))
        assert asyncio.run(restarted.pending(("crash-revision",)))
        release.set()
        assert closed.wait(10)
        assert asyncio.run(restarted.boot_cleanup()) == (lease_id,)
        assert not asyncio.run(restarted.pending((lease_id,)))
        assert not asyncio.run(restarted.pending(("crash-revision",)))
    finally:
        release.set()
        if owner.is_alive():
            owner.terminate()
        owner.join(10)


def test_redaction_marker_and_failed_cleanup_are_retained(tmp_path) -> None:
    async def run() -> None:
        store = LeaseStore(tmp_path / "leases")
        async with store.reserve(asyncio.get_running_loop().time() + 2) as lease:
            await lease.register(("revision-a",))
            affected = await store.cancel_revision("revision-a")
            assert affected == (lease.id,)
            assert lease.cancelled()
            (lease.directory / "foreign").symlink_to(Path(os.devnull))
        assert lease.id in store.cleanup_pending
        assert lease.directory.exists()
        assert await store.boot_cleanup() == ()
        assert lease.directory.exists()

    asyncio.run(run())


def test_queue_full_is_immediate_and_pending_survives_missing_cancel(tmp_path) -> None:
    async def run() -> None:
        store = LeaseStore(tmp_path / "leases", slots=1)
        async with store.reserve(asyncio.get_running_loop().time() + 2) as lease:
            await lease.register(("revision-a",))
            assert await store.pending(("revision-a",))
            with pytest.raises(RuntimeError, match="queue_full"):
                async with store.reserve(asyncio.get_running_loop().time() + 30):
                    pass
            assert await store.pending(("revision-a",))

    asyncio.run(run())


def test_artifact_modes_and_root_symlink_rejection(tmp_path) -> None:
    async def run() -> None:
        store = LeaseStore(tmp_path / "leases")
        async with store.reserve(asyncio.get_running_loop().time() + 2) as lease:
            await lease.register(("r",))
            assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
            assert stat.S_IMODE(lease.directory.stat().st_mode) == 0o700
            assert stat.S_IMODE((lease.directory / "manifest.json").stat().st_mode) == 0o600
            assert len(lease.lock_fds) == 3

    asyncio.run(run())
    (tmp_path / "link").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        LeaseStore(tmp_path / "link")


def test_pending_ignores_root_lock_files_and_tracks_active_lease(tmp_path) -> None:
    async def run() -> None:
        store = LeaseStore(tmp_path / "leases")
        assert not await store.pending(("missing-revision",))
        async with store.reserve(asyncio.get_running_loop().time() + 2) as lease:
            await lease.register(("revision-a",))
            assert await store.pending((lease.id,))
            assert await store.pending(("revision-a",))
            assert await store.cancel_revision("revision-a") == (lease.id,)
            (lease.directory / "cancel").unlink()
            assert await store.pending(("revision-a",))
        assert not await store.pending((lease.id,))
        assert not await store.pending(("revision-a",))

    asyncio.run(run())
