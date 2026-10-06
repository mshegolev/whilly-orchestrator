"""Persistence boundary for shared memory."""

from __future__ import annotations

from typing import Protocol

from .domain import KnowledgeRevision, Principal


class MemoryStore(Protocol):
    async def append(self, revision: KnowledgeRevision) -> KnowledgeRevision: ...

    async def visible(
        self, principal: Principal, product_id: str, project_ids: tuple[str, ...]
    ) -> list[KnowledgeRevision]: ...

    async def redact(self, principal: Principal, revision_id: str) -> bool: ...
