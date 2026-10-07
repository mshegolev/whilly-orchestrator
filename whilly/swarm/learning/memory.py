"""Bounded context assembly and source verification service for learning memory."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from .domain import ContextPackage, KnowledgeRevision, Principal, SourceCheck
from .ports import MemoryStore


class SourceVerifier(Protocol):
    async def check(self, item: KnowledgeRevision) -> SourceCheck: ...


def build_context(
    items: list[KnowledgeRevision],
    *,
    max_chars: int,
    now: datetime,
    omissions: tuple[str, ...] = (),
    preserve_order: bool = False,
) -> ContextPackage:
    if max_chars < 256:
        raise ValueError("max_chars must be at least 256")
    usable: list[KnowledgeRevision] = []
    omission_values: list[str] = list(omissions)
    conflicts: list[str] = []
    for item in items:
        if (
            item.status in {"retracted", "superseded", "stale"}
            or item.conflicts
            or (item.expires_at is not None and item.expires_at <= now)
        ):
            omission_values.append(item.id)
            conflicts.extend(item.conflicts)
            continue
        usable.append(item)
        conflicts.extend(item.conflicts)
    if not preserve_order:
        usable.sort(key=lambda item: (item.status != "verified", item.id))
    omission_values = tuple(sorted(set(omission_values)))
    conflict_values = tuple(sorted(set(conflicts)))
    included: list[KnowledgeRevision] = []
    for item in usable:
        candidate = ContextPackage(
            tuple(included + [item]),
            omission_values,
            conflict_values,
            tuple(existing.id for existing in included) + (item.id,),
        )
        if len(render_context(candidate)) > max_chars:
            omission_values = tuple(sorted(set(omission_values + (item.id,))))
            continue
        included.append(item)
    return _package_with_bounded_metadata(tuple(included), omission_values, conflict_values, max_chars)


def _package_with_bounded_metadata(
    items: tuple[KnowledgeRevision, ...], omissions: tuple[str, ...], conflicts: tuple[str, ...], max_chars: int
) -> ContextPackage:
    bounded_items = list(items)
    bounded_omissions = list(dict.fromkeys(omissions))
    bounded_conflicts = list(dict.fromkeys(conflicts))
    # Avoid quadratic re-serialization when a caller supplies a very large
    # omission set; the count is the useful bounded metadata in that case.
    if len(bounded_omissions) > 256:
        bounded_omissions = [f"bounded:{len(bounded_omissions)} omissions"]
    if len(bounded_conflicts) > 256:
        bounded_conflicts = [f"bounded:{len(bounded_conflicts)} conflicts"]
    while True:
        package = ContextPackage(
            tuple(bounded_items),
            tuple(bounded_omissions),
            tuple(bounded_conflicts),
            tuple(item.id for item in bounded_items),
        )
        if len(render_context(package)) <= max_chars:
            return package
        if bounded_omissions:
            bounded_omissions.pop()
        elif bounded_conflicts:
            bounded_conflicts.pop()
        elif bounded_items:
            bounded_items.pop()
        else:
            # The fixed envelope is below the minimum accepted budget; this is
            # defensive only, but guarantees a terminating bounded algorithm.
            return package


def render_context(package: ContextPackage) -> str:
    payload = {
        "bounded": True,
        "items": [
            {
                "id": item.id,
                "kind": item.kind,
                "body": item.body,
                "status": item.status,
                "classification": item.classification,
                "source_uri": item.source_uri,
                "source_sha": item.source_sha,
                "verified_at": item.verified_at.isoformat() if item.verified_at else None,
                "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            }
            for item in package.items
        ],
        "revision_manifest": list(package.revision_manifest),
        "omissions": list(package.omissions),
        "conflicts": list(package.conflicts),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class MemoryService:
    def __init__(self, store: MemoryStore, verifier: SourceVerifier) -> None:
        self._store = store
        self._verifier = verifier

    async def context(
        self,
        principal: Principal,
        product_id: str,
        project_ids: tuple[str, ...],
        *,
        max_chars: int,
        now: datetime,
    ) -> ContextPackage:
        visible = await self._store.visible(principal, product_id, project_ids)
        checked: list[KnowledgeRevision] = []
        externally_unbounded: list[str] = []
        for item in visible:
            if item.expires_at is not None and item.expires_at <= now:
                checked.append(item)
                continue
            if item.source_uri.startswith("git:") and item.source_sha:
                result = await self._verifier.check(item)
                if result.status == "changed":
                    item = replace(item, status="stale")
                elif result.status == "unavailable":
                    item = replace(item, status="candidate", verified_at=None)
            elif not item.source_uri.startswith("git:") and (item.expires_at is None or item.expires_at <= now):
                externally_unbounded.append(item.id)
                continue
            checked.append(item)
        return build_context(
            checked,
            max_chars=max_chars,
            now=now,
            omissions=tuple(sorted(set(externally_unbounded))),
        )
