"""Host-owned composition for the L1 and Cognee memory backends."""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whilly.adapters.db.learning_memory import PostgresMemoryStore
from whilly.adapters.filesystem.knowledge_sources import GitSourceVerifier
from whilly.adapters.filesystem.memory_leases import LeaseStore
from whilly.adapters.memory.cognee_process import CogneeRetriever
from whilly.swarm.learning.domain import ContextPackage
from whilly.swarm.learning.lookup import LookupService, RedactionResult
from whilly.swarm.learning.memory import MemoryService
from whilly.swarm.learning.retrieval import RetrievalResult, RetrievalUnavailable
from datetime import datetime, timezone


class _L1Retriever:
    async def rank(self, request, *, request_dir, deadline, lock_fds=(), launch_guard=None):
        return RetrievalResult(tuple(record.id for record in request.records), 0)


class _UnavailableRetriever:
    def __init__(self, code: str):
        self.code = code

    async def rank(self, *args, **kwargs):
        raise RetrievalUnavailable(self.code)


@dataclass(frozen=True)
class MemoryConfig:
    backend: str
    runtime: Path | None
    models_dir: Path | None
    state_dir: Path


@dataclass(frozen=True)
class MemoryStatus:
    backend: str
    ready: bool
    last_elapsed_ms: int | None
    error_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "ready": self.ready,
            "last_elapsed_ms": self.last_elapsed_ms,
            "error_code": self.error_code,
        }


def _venv_root(raw: str | None) -> Path | None:
    if not raw:
        return None
    # Path.absolute() makes the path stable without following a symlink to the
    # interpreter, which is important when the host deliberately exposes a
    # venv through a stable link.
    return Path(raw).absolute().parent.parent


def memory_config() -> MemoryConfig:
    backend = (os.environ.get("WHILLY_MEMORY_BACKEND") or "l1").strip().lower()
    if backend not in {"l1", "cognee"}:
        raise ValueError("memory_backend_invalid")
    state = Path(os.environ.get("WHILLY_COGNEE_STATE_DIR") or ".whilly/cognee-state").absolute()
    if backend == "l1":
        return MemoryConfig(backend, None, None, state)
    return MemoryConfig(
        backend,
        _venv_root(os.environ.get("WHILLY_COGNEE_PYTHON")),
        Path(os.environ["WHILLY_COGNEE_MODELS_DIR"]).absolute() if os.environ.get("WHILLY_COGNEE_MODELS_DIR") else None,
        state,
    )


class MemoryCoordinator:
    """One lookup/redaction boundary shared by API, planner and workers."""

    def __init__(self, lookup: LookupService, config: MemoryConfig, l1: MemoryService):
        self.lookup = lookup
        self.config = config
        self.l1 = l1
        self.last_elapsed_ms: int | None = None
        self.error_code: str | None = None
        self._started = False

    async def startup(self) -> None:
        if not self._started:
            await self.lookup._leases.boot_cleanup()
            self._started = True

    def _readiness_error(self) -> str | None:
        if self.config.backend == "l1":
            return None
        if sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
            return "sandbox_unavailable"
        if self.config.runtime is None:
            return "runtime_missing"
        python = self.config.runtime / "bin" / "python"
        if not python.is_file() or not os.access(python, os.X_OK):
            return "runtime_python_missing"
        if self.config.models_dir is None or not self.config.models_dir.is_dir():
            return "models_missing"
        nested_layouts = {
            self.config.models_dir / "hub" / "models--fastino--gliner2.5-base-v1": {"config.json", "model.safetensors"},
            self.config.models_dir / "fastembed" / "models--qdrant--bge-small-en-v1.5-onnx-q": {
                "config.json",
                "model_optimized.onnx",
            },
        }
        if not all(
            path.is_dir() and required <= {child.name for child in path.rglob("*") if child.is_file()}
            for path, required in nested_layouts.items()
        ):
            return "models_config_missing"
        return None

    def status(self) -> MemoryStatus:
        readiness_error = self._readiness_error()
        ready = readiness_error is None
        error = self.error_code or readiness_error
        return MemoryStatus(self.config.backend, ready, self.last_elapsed_ms, error)

    async def context(self, *args, **kwargs) -> ContextPackage:
        await self.startup()
        if self.config.backend == "cognee" and not self.status().ready:
            self.error_code = self.status().error_code
            raise RetrievalUnavailable(self.error_code or "memory_backend_unavailable")
        started = time.monotonic()
        try:
            if self.config.backend == "l1":
                result = await self.l1.context(*args[:3], max_chars=kwargs["max_chars"], now=datetime.now(timezone.utc))
            else:
                result = await self.lookup.context(*args, **kwargs)
        except Exception as exc:
            self.error_code = getattr(exc, "code", None) or "lookup_unavailable"
            raise
        self.last_elapsed_ms = round((time.monotonic() - started) * 1000)
        self.error_code = None
        return result

    async def redact(self, *args, **kwargs) -> RedactionResult:
        await self.startup()
        return await self.lookup.redact(*args, **kwargs)


_COORDINATOR_CACHE: dict[tuple[int, str, str, str, str], MemoryCoordinator] = {}


def build_memory_coordinator(pool: Any, registry: Any) -> MemoryCoordinator:
    config = memory_config()
    cache_key = (
        id(pool),
        str(getattr(registry, "source_path", "")),
        config.backend,
        str(config.runtime or ""),
        str(config.models_dir or ""),
        str(config.state_dir),
    )
    cached = _COORDINATOR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    projects = {pid: (project.path, project.base_ref) for pid, project in registry.projects.items()}
    store = PostgresMemoryStore(pool)
    verifier = GitSourceVerifier(projects)
    leases = LeaseStore(config.state_dir / "leases")
    retriever = _L1Retriever()
    if config.backend == "cognee":
        if config.runtime is not None and config.models_dir is not None:
            retriever = CogneeRetriever(config.runtime, models_dir=config.models_dir)
        else:
            retriever = _UnavailableRetriever("runtime_missing")
    coordinator = MemoryCoordinator(
        LookupService(store, retriever, leases, verifier), config, MemoryService(store, verifier)
    )
    _COORDINATOR_CACHE[cache_key] = coordinator
    return coordinator


# Explicit name for composition callers; no domain module imports Cognee.
build_memory_service = build_memory_coordinator
