"""Unit tests for the testcontainer orphan-cleanup logic in tests/conftest.py.

Covers ``_cleanup_orphan_testcontainers`` and ``pytest_sessionstart``
(fix-m3-testcontainers-postgres-leak):

* docker CLI absent → no-op, returns ``[]``
* docker present, no matching containers → ``ps`` runs, ``rm`` does not
* docker present, two matching ids → ``rm -f <ids...>`` is invoked
* subprocess errors are swallowed (cleanup never crashes session start)
* ``pytest_sessionstart`` short-circuits when Docker is unreachable
* ``pytest_sessionstart`` invokes the cleanup helper when Docker is up

We mock subprocess.run so the tests are deterministic and don't actually
talk to Docker — they pin the *contract* of the cleanup helper, not the
real container lifecycle (that is exercised by the integration suite).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from tests import conftest as _conftest
from tests.conftest import (
    WHILLY_TESTCONTAINER_IMAGE,
    WHILLY_TESTCONTAINER_LABEL_KEY,
    WHILLY_TESTCONTAINER_LABEL_VALUE,
    _cleanup_orphan_testcontainers,
    pytest_sessionstart,
)


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[list[str]], subprocess.CompletedProcess[str]],
    *,
    docker_bin: str | None = "/usr/bin/docker",
) -> list[list[str]]:
    """Install a fake ``shutil.which`` + ``subprocess.run`` and capture argv lists."""
    captured: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return docker_bin if name == "docker" else None

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append(list(args))
        return handler(list(args))

    monkeypatch.setattr(_conftest.shutil, "which", fake_which)
    monkeypatch.setattr(_conftest.subprocess, "run", fake_run)
    return captured


def test_cleanup_returns_empty_when_docker_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ``docker`` binary on PATH → cleanup is a silent no-op."""
    monkeypatch.setattr(_conftest.shutil, "which", lambda _name: None)

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("subprocess.run must not be called when docker is absent")

    monkeypatch.setattr(_conftest.subprocess, "run", boom)
    assert _cleanup_orphan_testcontainers() == []


def test_cleanup_filters_by_label_and_ancestor_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """``docker ps`` is invoked with both label= and ancestor= filters and -aq."""

    def handler(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    captured = _patch_run(monkeypatch, handler)
    result = _cleanup_orphan_testcontainers()

    assert result == []
    assert len(captured) == 1, "no orphans → only the ps call should fire, not rm"
    ps_args = captured[0]
    assert ps_args[:3] == ["/usr/bin/docker", "ps", "-aq"]
    label_filter = f"label={WHILLY_TESTCONTAINER_LABEL_KEY}={WHILLY_TESTCONTAINER_LABEL_VALUE}"
    ancestor_filter = f"ancestor={WHILLY_TESTCONTAINER_IMAGE}"
    assert "--filter" in ps_args
    assert label_filter in ps_args
    assert ancestor_filter in ps_args


def test_cleanup_force_removes_each_orphan_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``ps`` returns ids, ``rm -f <ids...>`` is invoked exactly once."""

    def handler(args: list[str]) -> subprocess.CompletedProcess[str]:
        verb = args[1]
        if verb == "ps":
            return subprocess.CompletedProcess(args, 0, stdout="abc123\ndef456\n", stderr="")
        if verb == "rm":
            return subprocess.CompletedProcess(args, 0, stdout="abc123\ndef456\n", stderr="")
        raise AssertionError(f"unexpected docker verb: {verb}")

    captured = _patch_run(monkeypatch, handler)
    removed = _cleanup_orphan_testcontainers()

    assert removed == ["abc123", "def456"]
    assert len(captured) == 2
    rm_args = captured[1]
    assert rm_args[:3] == ["/usr/bin/docker", "rm", "-f"]
    assert rm_args[3:] == ["abc123", "def456"]


def test_cleanup_swallows_ps_failure_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero ``docker ps`` exit (daemon flake) → return ``[]`` without crashing."""

    def handler(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    _patch_run(monkeypatch, handler)
    assert _cleanup_orphan_testcontainers() == []


def test_cleanup_swallows_subprocess_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raised :class:`OSError` from ``subprocess.run`` is caught — never propagates."""
    monkeypatch.setattr(_conftest.shutil, "which", lambda _n: "/usr/bin/docker")

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError(99, "fake docker socket gone")

    monkeypatch.setattr(_conftest.subprocess, "run", boom)
    assert _cleanup_orphan_testcontainers() == []


def test_cleanup_swallows_subprocess_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """``subprocess.TimeoutExpired`` (hung docker CLI) is caught."""
    monkeypatch.setattr(_conftest.shutil, "which", lambda _n: "/usr/bin/docker")

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="docker ps", timeout=10)

    monkeypatch.setattr(_conftest.subprocess, "run", boom)
    assert _cleanup_orphan_testcontainers() == []


def test_pytest_sessionstart_skips_when_docker_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pytest_sessionstart`` exits early without calling cleanup if docker is down."""
    called: list[bool] = []
    monkeypatch.setattr(_conftest, "docker_available", lambda: False)
    monkeypatch.setattr(
        _conftest,
        "_cleanup_orphan_testcontainers",
        lambda **_kw: called.append(True) or [],
    )
    pytest_sessionstart(session=None)  # type: ignore[arg-type]
    assert called == []


def test_pytest_sessionstart_runs_cleanup_when_docker_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pytest_sessionstart`` invokes ``_cleanup_orphan_testcontainers`` when docker is up."""
    invocations: list[bool] = []

    def fake_cleanup(**_kw: Any) -> list[str]:
        invocations.append(True)
        return ["dead-beef"]

    monkeypatch.setattr(_conftest, "docker_available", lambda: True)
    monkeypatch.setattr(_conftest, "_cleanup_orphan_testcontainers", fake_cleanup)
    pytest_sessionstart(session=None)  # type: ignore[arg-type]
    assert invocations == [True]


def test_pytest_sessionstart_swallows_cleanup_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected exception from cleanup must not crash session start."""

    def explode(**_kw: Any) -> list[str]:
        raise RuntimeError("daemon evaporated")

    monkeypatch.setattr(_conftest, "docker_available", lambda: True)
    monkeypatch.setattr(_conftest, "_cleanup_orphan_testcontainers", explode)
    pytest_sessionstart(session=None)  # type: ignore[arg-type]
