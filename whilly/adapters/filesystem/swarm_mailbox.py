"""Descriptor-relative, bounded transport for task-local swarm mailboxes."""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import sys
import uuid
from itertools import islice
from pathlib import Path
from typing import Any

MAX_BYTES = 64_000
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 100
_REQUEST_NAME = re.compile(r"[a-f0-9]{32}\.json\Z")
_STATE_NAMES = frozenset({"inbox.json", "status.json"})


class MailboxIntegrityError(ValueError):
    """Raised when the mailbox boundary is not a trusted directory/file."""


class MailboxDirectory:
    """Own opened descriptors for all mailbox operations in one sync attempt."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._root_fd: int | None = None
        self._outbox_fd: int | None = None
        self._receipts_fd: int | None = None

    def __enter__(self) -> MailboxDirectory:
        try:
            self._root_fd = _open_or_create_path(self.root)
            self._outbox_fd = _open_child_directory(self._root_fd, "outbox", "outbox")
            self._receipts_fd = _open_child_directory(self._root_fd, "receipts", "receipts")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        for attribute in ("_receipts_fd", "_outbox_fd", "_root_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, attribute, None)

    def request_names(self, limit: int) -> tuple[str, ...]:
        outbox_fd = self._require(self._outbox_fd)
        names: list[str] = []
        cap = min(max(limit, 0), MAX_ENTRIES)
        with os.scandir(outbox_fd) as entries:
            for entry in islice(entries, cap):
                if _REQUEST_NAME.fullmatch(entry.name):
                    names.append(entry.name)
        return tuple(names)

    def entry_count(self, limit: int = MAX_ENTRIES) -> int:
        """Count at most ``limit`` directory entries without materializing them."""
        count = 0
        cap = min(max(limit, 0), MAX_ENTRIES)
        with os.scandir(self._require(self._outbox_fd)) as entries:
            for _entry in islice(entries, cap):
                count += 1
        return count

    def read_request(self, name: str, max_bytes: int = MAX_BYTES) -> bytes:
        _validate_request_name(name)
        return _read_regular(self._require(self._outbox_fd), name, max_bytes, "request")

    def write_request(self, name: str, payload: dict) -> None:
        _validate_request_name(name)
        self._write_json(self._require(self._outbox_fd), name, payload)

    def write_receipt(self, name: str, payload: dict) -> None:
        _validate_request_name(name)
        self._write_json(self._require(self._receipts_fd), name, payload)

    def remove_request(self, name: str) -> None:
        _validate_request_name(name)
        try:
            os.unlink(name, dir_fd=self._require(self._outbox_fd))
        except FileNotFoundError:
            return

    def has_receipt(self, name: str) -> bool:
        _validate_request_name(name)
        descriptor = _openat(self._require(self._receipts_fd), name, os.O_RDONLY | os.O_NONBLOCK)
        if descriptor is None:
            return False
        try:
            _require_regular(descriptor, "receipt")
            return True
        finally:
            os.close(descriptor)

    def receipt_is_special_rejection(self, name: str) -> bool:
        _validate_request_name(name)
        try:
            payload = json.loads(_read_regular(self._require(self._receipts_fd), name, MAX_BYTES, "receipt"))
        except (ValueError, json.JSONDecodeError, TypeError):
            return False
        return isinstance(payload, dict) and payload.get("special_file") is True

    def write_state(self, name: str, payload: dict) -> None:
        _validate_state_name(name)
        self._write_json(self._require(self._root_fd), name, payload)

    def read_state(self, name: str, max_bytes: int = MAX_STATE_BYTES) -> bytes:
        _validate_state_name(name)
        if max_bytes > MAX_STATE_BYTES:
            raise ValueError("state read limit exceeds mailbox state cap")
        return _read_regular(self._require(self._root_fd), name, max_bytes, "state")

    def _write_json(self, directory_fd: int, name: str, payload: dict) -> None:
        content = json.dumps(payload, default=str).encode()
        temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _require(descriptor: int | None) -> int:
        if descriptor is None:
            raise RuntimeError("mailbox directory is not open")
        return descriptor


def _open_or_create_path(path: Path) -> int:
    raw_path = os.fspath(path)
    absolute = raw_path.startswith(os.sep)
    components = _safe_path_components(raw_path, absolute=absolute)
    descriptor = os.open(os.sep if absolute else ".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in components:
            child_fd = _advance_directory(descriptor, component, create=True, label="mailbox path")
            os.close(descriptor)
            descriptor = child_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _safe_path_components(raw_path: str, *, absolute: bool) -> tuple[str, ...]:
    components = [part for part in raw_path.split(os.sep) if part not in ("", ".")]
    if ".." in components:
        raise MailboxIntegrityError("mailbox path must not contain parent traversal")
    if sys.platform == "darwin" and absolute and components and components[0] in {"var", "tmp"}:
        alias = os.path.join(os.sep, components[0])
        link_stat = os.lstat(alias) if os.path.islink(alias) else None
        target = os.readlink(alias) if link_stat is not None and link_stat.st_uid == 0 else None
        expected = os.path.join(os.sep, "private", components[0])
        if target in {expected, expected.lstrip(os.sep)}:
            components[0:1] = ["private", components[0]]
    return tuple(components)


def _advance_directory(parent_fd: int, name: str, *, create: bool, label: str) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise MailboxIntegrityError(f"{label} must be a directory") from exc
    try:
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise MailboxIntegrityError(f"{label} must be a directory") from exc
    return child_fd


def _open_child_directory(parent_fd: int, name: str, label: str) -> int:
    return _advance_directory(parent_fd, name, create=True, label=label)


def _openat(directory_fd: int, name: str, flags: int) -> int | None:
    try:
        return os.open(name, flags | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENXIO):
            raise MailboxIntegrityError("mailbox entry must not be a symlink or special file") from exc
        raise MailboxIntegrityError("mailbox entry cannot be opened") from exc


def _require_regular(descriptor: int, label: str) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise MailboxIntegrityError(f"{label} must be a regular file")


def _read_regular(directory_fd: int, name: str, max_bytes: int, label: str) -> bytes:
    descriptor = _openat(directory_fd, name, os.O_RDONLY | os.O_NONBLOCK)
    if descriptor is None:
        raise ValueError(f"{label} request disappeared")
    try:
        _require_regular(descriptor, label)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{label} too large")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        offset += os.write(descriptor, content[offset:])


def _validate_request_name(name: str) -> None:
    if not _REQUEST_NAME.fullmatch(name):
        raise ValueError("invalid mailbox request name")


def _validate_state_name(name: str) -> None:
    if name not in _STATE_NAMES:
        raise ValueError("invalid mailbox state name")
