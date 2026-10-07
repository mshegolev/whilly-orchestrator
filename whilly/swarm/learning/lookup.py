"""Coordinator for ACL-safe, bounded temporary memory lookup."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from .domain import ContextPackage, KnowledgeRevision, Principal
from .memory import build_context
from .ports import MemoryStore
from .retrieval import RetrievalRecord, RetrievalRequest, RetrievalResult, RetrievalUnavailable, Retriever


@dataclass(frozen=True)
class RedactionResult:
    redacted: bool
    purge_pending: bool


class LookupService:
    def __init__(
        self, store: MemoryStore, retriever: Retriever, leases, verifier=None, *, timeout: float = 90.0
    ) -> None:
        self._store = store
        self._retriever = retriever
        self._leases = leases
        self._verifier = verifier
        self._timeout = timeout

    async def context(
        self, principal: Principal, product_id: str, project_ids: tuple[str, ...], query: str, *, max_chars: int
    ) -> ContextPackage:
        if len(query) > 4096:
            raise RetrievalUnavailable("query_limit")
        deadline = time.monotonic() + self._timeout

        async def flow() -> ContextPackage:
            visible = await self._store.visible(principal, product_id, project_ids)
            eligible = await self._eligible(visible)
            if not eligible:
                return self._package([], visible, max_chars)
            fingerprints = {item.id: self._fingerprint(item) for item in eligible}
            async with self._leases.reserve(deadline) as lease:
                async with self._leases.fence():
                    current_items = await self._store.visible(principal, product_id, project_ids)
                    current = {item.id: item for item in current_items}
                    candidates = [
                        item
                        for item in current_items
                        if item.id in fingerprints and self._fingerprint(item) == fingerprints[item.id]
                    ]
                current_eligible = await self._eligible(candidates)
                async with self._leases.fence():
                    reread_items = await self._store.visible(principal, product_id, project_ids)
                    current = {item.id: item for item in reread_items}
                    candidates = [
                        item
                        for item in current_eligible
                        if item.id in current
                        and self._basic_eligible(current[item.id])
                        and self._fingerprint(item) == self._fingerprint(current[item.id])
                    ]
                    records = self._bounded_records(candidates, query)
                    await lease.register(tuple(record.id for record in records))
                if not records:
                    return self._package([], reread_items, max_chars)
                request = RetrievalRequest(query[:4096], records)
                result = await self._rank(lease, request, deadline)
                selected = tuple(result.ids)
                if len(selected) != len(set(selected)):
                    raise RetrievalUnavailable("duplicate_ids")
                allowed = {record.id for record in records}
                if any(item_id not in allowed for item_id in selected):
                    raise RetrievalUnavailable("unknown_id")
                if lease.cancelled():
                    raise RetrievalUnavailable("redaction_cancelled")
                async with self._leases.fence():
                    reread_items = await self._store.visible(principal, product_id, project_ids)
                    reread = {item.id: item for item in reread_items}
                    for item_id in selected:
                        if item_id not in reread or self._fingerprint(reread[item_id]) != self._fingerprint(
                            current[item_id]
                        ):
                            raise RetrievalUnavailable("canonical_revision_changed")
                        if not self._basic_eligible(reread[item_id]):
                            raise RetrievalUnavailable("canonical_revision_unavailable")
                    selected_verified = await self._verify_selected(selected, reread)
                    if not all(selected_verified.values()):
                        raise RetrievalUnavailable("canonical_revision_unavailable")
                    if lease.cancelled():
                        raise RetrievalUnavailable("redaction_cancelled")
                    checked = [reread[item_id] for item_id in selected]
                return self._package(checked, visible, max_chars, selected=selected)

        try:
            return await asyncio.wait_for(flow(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise RetrievalUnavailable("timeout") from exc
        except RuntimeError as exc:
            if str(exc) == "memory_lease_queue_full":
                raise RetrievalUnavailable("queue_full") from exc
            raise

    async def _rank(self, lease, request: RetrievalRequest, deadline: float) -> RetrievalResult:
        if lease.cancelled():
            raise RetrievalUnavailable("redaction_cancelled")

        @asynccontextmanager
        async def launch_guard():
            # The adapter holds this through actual OS spawn, not only task creation.
            # A concurrent tombstone must linearize either before or after spawn.
            async with self._leases.fence():
                if lease.cancelled():
                    raise RetrievalUnavailable("redaction_cancelled")
                yield

        rank_task = asyncio.create_task(
            self._retriever.rank(
                request,
                request_dir=lease.directory,
                deadline=deadline,
                lock_fds=lease.lock_fds,
                launch_guard=launch_guard,
            )
        )
        try:
            while True:
                if lease.cancelled():
                    raise RetrievalUnavailable("redaction_cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RetrievalUnavailable("timeout")
                done, _ = await asyncio.wait({rank_task}, timeout=min(0.1, remaining))
                if not done:
                    continue
                try:
                    result = rank_task.result()
                except RetrievalUnavailable:
                    raise
                except asyncio.CancelledError as exc:
                    raise RetrievalUnavailable("retrieval_cancelled") from exc
                except Exception as exc:
                    raise RetrievalUnavailable("backend_failure") from exc
                if lease.cancelled():
                    raise RetrievalUnavailable("redaction_cancelled")
                return result
        finally:
            if not rank_task.done():
                rank_task.cancel()
            await asyncio.gather(rank_task, return_exceptions=True)

    async def _eligible(self, items: list[KnowledgeRevision]) -> list[KnowledgeRevision]:
        result: list[KnowledgeRevision] = []
        for item in items:
            if not self._basic_eligible(item) or self._verifier is None:
                continue
            check = await self._verifier.check(item)
            if check.status == "verified" and (
                not item.source_uri.startswith("git:") or check.revision == item.source_sha
            ):
                result.append(item)
        return sorted(result, key=lambda item: item.id)

    async def _verify_selected(self, ids: tuple[str, ...], current: dict[str, KnowledgeRevision]) -> dict[str, bool]:
        if self._verifier is None:
            return {item_id: False for item_id in ids}
        result: dict[str, bool] = {}
        for item_id in ids:
            item = current[item_id]
            check = await self._verifier.check(item)
            result[item_id] = check.status == "verified" and (
                not item.source_uri.startswith("git:") or check.revision == item.source_sha
            )
        return result

    @staticmethod
    def _basic_eligible(item: KnowledgeRevision) -> bool:
        if item.status != "verified" or item.verified_at is None or item.conflicts:
            return False
        if item.source_uri.startswith("git:") and not item.source_sha:
            return False
        if not item.source_uri.startswith("git:") and item.expires_at is None:
            return False
        return item.expires_at is None or item.expires_at > datetime.now(timezone.utc)

    @staticmethod
    def _fingerprint(item: KnowledgeRevision) -> tuple:
        return tuple(getattr(item, field) for field in item.__dataclass_fields__)

    @staticmethod
    def _bounded_records(items: list[KnowledgeRevision], query: str) -> tuple[RetrievalRecord, ...]:
        records: list[RetrievalRecord] = []
        query = query[:4096]
        for item in sorted(items, key=lambda value: value.id):
            if len(records) >= 32:
                break
            candidate = tuple(records + [RetrievalRecord(item.id, item.body)])
            envelope = {"version": 1, "query": query, "records": [{"id": r.id, "body": r.body} for r in candidate]}
            if len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 128 * 1024:
                records.append(RetrievalRecord(item.id, item.body))
        return tuple(records)

    @staticmethod
    def _package(
        checked: list[KnowledgeRevision], visible: list[KnowledgeRevision], max_chars: int, *, selected=()
    ) -> ContextPackage:
        selected_set = set(selected)
        omissions = tuple(item.id for item in visible if item.id not in selected_set)
        return build_context(
            checked, max_chars=max_chars, now=datetime.now(timezone.utc), omissions=omissions, preserve_order=True
        )

    async def redact(self, principal: Principal, revision_id: str) -> RedactionResult:
        async with self._leases.fence():
            redacted = await self._store.redact(principal, revision_id)
            affected = self._leases._cancel_revision_locked(revision_id) if redacted else ()
        if not affected:
            return RedactionResult(redacted, False)
        until = time.monotonic() + min(self._timeout, 1.0)
        while time.monotonic() < until and await self._leases.pending(affected):
            await asyncio.sleep(0.01)
        return RedactionResult(redacted, await self._leases.pending(affected))
