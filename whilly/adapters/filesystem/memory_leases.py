"""Process-safe, failure-retaining leases for temporary memory indexes."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _open_lock(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _manifest_bytes(directory: Path) -> bytes:
    """Validate ownership before any recovery deletion; never follow links."""
    fd = os.open(directory / "manifest.json", os.O_RDONLY | _NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("invalid lease manifest")
        raw = stream.read()
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or set(data) != {"lease_id", "revision_ids"}
        or data["lease_id"] != directory.name
        or not isinstance(data["revision_ids"], list)
        or any(not isinstance(value, str) or not value.strip() for value in data["revision_ids"])
    ):
        raise ValueError("invalid lease manifest")
    return raw


def _remove_metadata(directory: Path) -> None:
    """Delete manifest last, restoring recoverable ownership if rmdir fails.

    Callers hold the fence and owner lock through deletion and restoration.
    Empty unregistered reservations legitimately have no manifest.
    """
    manifest = directory / "manifest.json"
    raw = _manifest_bytes(directory) if manifest.exists() or manifest.is_symlink() else None
    artifacts = [directory / name for name in ("cancel", "owner.lock", "manifest.json")]
    if any(path.is_symlink() for path in artifacts):
        raise OSError("unsafe lease metadata")
    try:
        for path in artifacts:
            if path.exists():
                path.unlink()
        directory.rmdir()
    except OSError:
        if raw is not None and not manifest.exists():
            temporary = directory / (".manifest-" + secrets.token_hex(16))
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, manifest)
        # The old owner fd remains locked; fence serialization protects the
        # replacement inode until a subsequent recovery attempt can acquire it.
        if not (directory / "owner.lock").exists():
            os.close(_open_lock(directory / "owner.lock"))
        raise


@dataclass
class LookupLease:
    id: str
    directory: Path
    _owner_fd: int
    _execution_fd: int
    _slot_fd: int
    _cancel_marker: Path

    @property
    def lock_fds(self) -> tuple[int, ...]:
        return (self._execution_fd, self._owner_fd, self._slot_fd)

    async def register(self, revision_ids: tuple[str, ...]) -> None:
        path = self.directory / "manifest.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(
                fd,
                json.dumps({"lease_id": self.id, "revision_ids": list(revision_ids)}, separators=(",", ":")).encode(),
            )
        finally:
            os.close(fd)

    def cancelled(self) -> bool:
        return self._cancel_marker.is_file() and not self._cancel_marker.is_symlink()


class LeaseStore:
    def __init__(self, root: Path, *, slots: int = 5) -> None:
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError("lease root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._slots = slots
        self._execution_path = self.root / "execution.lock"
        self.cleanup_pending: set[str] = set()

    @asynccontextmanager
    async def reserve(self, deadline: float):
        slot_fd = await self._acquire_slot()
        lease_id = secrets.token_hex(16)
        directory = self.root / lease_id
        directory.mkdir(mode=0o700)
        owner_fd = _open_lock(directory / "owner.lock")
        execution_fd = _open_lock(self._execution_path)
        try:
            await self._acquire_lock(owner_fd, deadline)
            await self._acquire_lock(execution_fd, deadline)
            yield LookupLease(lease_id, directory, owner_fd, execution_fd, slot_fd, directory / "cancel")
        finally:
            await asyncio.shield(self._cleanup(lease_id, directory, owner_fd, execution_fd, slot_fd))

    async def _acquire_slot(self) -> int:
        for index in range(self._slots):
            fd = _open_lock(self.root / f"slot-{index}.lock")
            if _try_lock(fd):
                return fd
            os.close(fd)
        raise RuntimeError("memory_lease_queue_full")

    @staticmethod
    async def _acquire_lock(fd: int, deadline: float) -> None:
        while not _try_lock(fd):
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("memory_lease_deadline")
            await asyncio.sleep(0.01)

    @staticmethod
    def _close(fd: int) -> None:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    async def _cleanup(self, lease_id: str, directory: Path, owner_fd: int, execution_fd: int, slot_fd: int) -> None:
        async with self.fence():
            await self._cleanup_locked(lease_id, directory, owner_fd, execution_fd, slot_fd)

    async def _cleanup_locked(
        self, lease_id: str, directory: Path, owner_fd: int, execution_fd: int, slot_fd: int
    ) -> None:
        failed = False
        reserved = {"manifest.json", "owner.lock", "cancel"}
        try:
            if directory.is_symlink() or not directory.is_dir():
                failed = True
            else:
                for child in sorted(
                    (
                        child
                        for child in directory.rglob("*")
                        if child.parent != directory or child.name not in reserved
                    ),
                    key=lambda item: len(item.parts),
                    reverse=True,
                ):
                    if child.is_symlink():
                        failed = True
                    elif child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
                if not failed:
                    _remove_metadata(directory)
        except (OSError, ValueError):
            failed = True
        finally:
            for fd in (execution_fd, owner_fd, slot_fd):
                self._close(fd)
        if failed:
            self.cleanup_pending.add(lease_id)
        else:
            self.cleanup_pending.discard(lease_id)

    async def boot_cleanup(self) -> tuple[str, ...]:
        async with self.fence():
            return await self._boot_cleanup_locked()

    async def _boot_cleanup_locked(self) -> tuple[str, ...]:
        removed: list[str] = []
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir() or len(directory.name) != 32:
                continue
            fd = -1
            try:
                int(directory.name, 16)
                manifest = directory / "manifest.json"
                owner_path = directory / "owner.lock"
                if (
                    manifest.is_symlink()
                    or owner_path.is_symlink()
                    or not manifest.is_file()
                    or not owner_path.is_file()
                ):
                    continue
                fd = os.open(owner_path, os.O_RDWR | _NOFOLLOW)
                if not _try_lock(fd):
                    continue
                _manifest_bytes(directory)
                failed = False
                reserved = {"manifest.json", "owner.lock", "cancel"}
                for child in sorted(
                    (
                        child
                        for child in directory.rglob("*")
                        if child.parent != directory or child.name not in reserved
                    ),
                    key=lambda item: len(item.parts),
                    reverse=True,
                ):
                    if child.is_symlink():
                        failed = True
                    elif child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
                if not failed:
                    _remove_metadata(directory)
                    self.cleanup_pending.discard(directory.name)
                    removed.append(directory.name)
                else:
                    self.cleanup_pending.add(directory.name)
            except (OSError, ValueError):
                self.cleanup_pending.add(directory.name)
            finally:
                self._close(fd)
        return tuple(removed)

    @asynccontextmanager
    async def fence(self):
        fd = _open_lock(self.root / "fence.lock")
        try:
            await self._acquire_lock(fd, asyncio.get_running_loop().time() + 90)
            yield
        finally:
            self._close(fd)

    async def cancel_revision(self, revision_id: str) -> tuple[str, ...]:
        async with self.fence():
            return self._cancel_revision_locked(revision_id)

    def _cancel_revision_locked(self, revision_id: str) -> tuple[str, ...]:
        affected: list[str] = []
        for directory in self.root.iterdir():
            manifest = directory / "manifest.json"
            if directory.is_symlink() or not manifest.is_file() or manifest.is_symlink():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if data.get("lease_id") != directory.name or revision_id not in data.get("revision_ids", []):
                    continue
                fd = os.open(directory / "cancel", os.O_WRONLY | os.O_CREAT | _NOFOLLOW, 0o600)
                self._close(fd)
                affected.append(directory.name)
            except (OSError, ValueError, AttributeError):
                continue
        return tuple(sorted(affected))

    async def pending(self, ids: tuple[str, ...]) -> bool:
        # ``ids`` may contain either opaque lease IDs or revision IDs.  Root
        # lock files are installation artifacts, not leases, and must never
        # make an unrelated query permanently pending.
        wanted = set(ids)
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir() or len(directory.name) != 32:
                continue
            try:
                int(directory.name, 16)
            except ValueError:
                continue
            if directory.name in wanted:
                return True
            manifest = directory / "manifest.json"
            if manifest.is_file() and not manifest.is_symlink():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    if wanted.intersection(data.get("revision_ids", [])):
                        return True
                except (OSError, ValueError, AttributeError):
                    return True
        self.cleanup_pending.intersection_update(
            directory.name
            for directory in self.root.iterdir()
            if directory.is_dir() and not directory.is_symlink() and len(directory.name) == 32
        )
        return False
