"""Small, backend-neutral protocol for bounded memory retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AsyncContextManager, Callable, Protocol


@dataclass(frozen=True, slots=True)
class RetrievalRecord:
    id: str
    body: str


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    query: str
    records: tuple[RetrievalRecord, ...]


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    ids: tuple[str, ...]
    elapsed_ms: int


class RetrievalUnavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Retriever(Protocol):
    async def rank(
        self,
        request: RetrievalRequest,
        *,
        request_dir: Path,
        deadline: float,
        lock_fds: tuple[int, ...] = (),
        launch_guard: Callable[[], AsyncContextManager[None]] | None = None,
    ) -> RetrievalResult: ...
