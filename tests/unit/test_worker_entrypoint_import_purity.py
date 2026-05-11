"""Worker entry-point import purity (M1, fix-m1-whilly-worker-fastapi-leak).

Reproduces the v4.4.0 regression where ``pip install whilly_worker==4.4.0
&& whilly-worker --help`` crashed with ``ModuleNotFoundError: No module
named 'fastapi'``. The crash was caused by ``whilly.cli.worker`` importing
``from whilly.adapters.transport.client import RemoteWorkerClient`` —
which loaded ``whilly/adapters/transport/__init__.py`` and eagerly pulled
in ``whilly.adapters.transport.auth`` (and therefore ``fastapi``). The
symmetric latent leak via ``whilly.worker.__init__`` → ``whilly.worker.local``
→ ``whilly.adapters.db.repository`` → ``asyncpg`` was the next failure
mode that would have surfaced as soon as fastapi was installed.

This module is the fast unit-test sibling of
``tests/integration/test_worker_image_import_purity.py``: it runs in the
same interpreter, clears ``sys.modules`` of the worker / control-plane
namespaces, imports ``whilly.cli.worker``, and asserts that no
control-plane-only top-level package leaked into ``sys.modules``.
"""

from __future__ import annotations

import importlib
import sys

# Top-level packages that must NEVER be imported by the worker entry
# closure. The set mirrors the import-linter ``worker-entry-purity``
# contract and the runtime ``test_worker_image_import_purity`` gate.
FORBIDDEN_TOP_LEVELS: tuple[str, ...] = (
    "fastapi",
    "asyncpg",
    "sqlalchemy",
    "alembic",
    "uvicorn",
)

# Module-name prefixes we need to clear before re-importing so the
# import is deterministic regardless of test ordering. Includes whilly
# itself (to force a fresh import) plus every forbidden top-level
# (so any prior leak from another test does not give a false positive).
_RESET_PREFIXES: tuple[str, ...] = ("whilly",) + FORBIDDEN_TOP_LEVELS


def _purge_sys_modules() -> None:
    """Remove every entry whose dotted name starts with a reset prefix."""
    for name in list(sys.modules):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in _RESET_PREFIXES):
            del sys.modules[name]


def _leaked_top_levels() -> list[str]:
    """Return the sorted list of forbidden top-levels currently in ``sys.modules``."""
    return sorted(top for top in FORBIDDEN_TOP_LEVELS if top in sys.modules)


def test_importing_whilly_cli_worker_does_not_pull_fastapi() -> None:
    """``import whilly.cli.worker`` must not surface ``fastapi`` in ``sys.modules``.

    This is the exact regression caught by ``pip install whilly_worker==4.4.0
    && whilly-worker --help``. Even when fastapi is installed in the dev
    venv (so the import does not crash), the leak burns the whole
    ``[worker]`` extras claim — a worker box must be able to install with
    only httpx + pydantic + whilly.core and run.
    """
    _purge_sys_modules()
    importlib.import_module("whilly.cli.worker")
    leaks = _leaked_top_levels()
    assert "fastapi" not in leaks, (
        "Importing whilly.cli.worker pulled fastapi into sys.modules. "
        "This is the v4.4.0 fastapi-leak bug: the worker entry closure must "
        "stay in the [worker] extras dep set (httpx + pydantic + whilly.core) "
        "and never reach the FastAPI surface. "
        f"All forbidden leaks: {leaks}"
    )


def test_importing_whilly_cli_worker_does_not_pull_asyncpg() -> None:
    """``import whilly.cli.worker`` must not surface ``asyncpg`` in ``sys.modules``.

    The symmetric latent leak: ``whilly.cli.worker`` → ``whilly.worker``
    → ``whilly.worker.local`` (eager) → ``whilly.adapters.db.repository``
    → ``asyncpg``. A worker container ships without asyncpg, so this is
    the next ModuleNotFoundError after the fastapi one would have
    surfaced.
    """
    _purge_sys_modules()
    importlib.import_module("whilly.cli.worker")
    leaks = _leaked_top_levels()
    assert "asyncpg" not in leaks, (
        "Importing whilly.cli.worker pulled asyncpg into sys.modules. "
        "The worker entry closure must not transitively touch the "
        "Postgres repository layer. "
        f"All forbidden leaks: {leaks}"
    )


def test_importing_whilly_cli_worker_does_not_pull_any_server_side_module() -> None:
    """Aggregate guard: no member of :data:`FORBIDDEN_TOP_LEVELS` may leak.

    Belt-and-braces — the two specific tests above are kept for clarity
    in the failure message, but this aggregate test catches any future
    server-only dependency (sqlalchemy, alembic, uvicorn) that sneaks
    into the worker entry closure.
    """
    _purge_sys_modules()
    importlib.import_module("whilly.cli.worker")
    leaks = _leaked_top_levels()
    assert not leaks, (
        "Importing whilly.cli.worker leaked control-plane-only top-level "
        f"packages into sys.modules: {leaks}. "
        f"Forbidden set: {sorted(FORBIDDEN_TOP_LEVELS)}. "
        "See .importlinter contract `worker-entry-purity` for the static graph guard."
    )
