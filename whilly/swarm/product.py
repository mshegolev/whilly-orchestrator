"""Product-level orchestration for durable swarm features."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from whilly.swarm.product_store import ProductStore
from whilly.swarm.runtime import SwarmService
from whilly.swarm.registry import load_registry
from whilly.swarm.verification import host_project_execution_binding


class ProductService:
    def __init__(self, pool: Any, registry_path: str) -> None:
        self.pool = pool
        self.registry_path = registry_path
        self.store = ProductStore(pool)

    def _registry_snapshot(self) -> dict[str, Any]:
        raw = Path(self.registry_path).read_bytes()
        data = json.loads(raw)
        data["hash"] = hashlib.sha256(raw).hexdigest()
        return data

    async def _create_session(self, title: str) -> str:
        return await SwarmService(self.pool).create_session(self.registry_path, title=title)

    async def add_message(self, body: str) -> dict[str, Any]:
        product = await self.store.ensure_product(self._registry_snapshot().get("name", "default"))
        return await self.store.add_message(product["id"], body)

    async def list_messages(self) -> list[dict[str, Any]]:
        return await self.store.list_messages("default")

    async def create_feature(self, title: str, intent: str) -> dict[str, Any]:
        await self.store.ensure_product(self._registry_snapshot().get("name", "default"))
        session_id = await self._create_session(title)
        return await self.store.create_feature(
            product_id="default",
            title=title,
            intent=intent,
            session_id=session_id,
            budget={"max_calls": 60, "max_elapsed_seconds": 7200},
        )

    async def prepare(
        self,
        feature_id: str,
        *,
        spec: Any,
        plan_revision: int,
        base_shas: dict[str, str],
        profiles: dict[str, Any],
        budget: dict[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        snapshot = self._registry_snapshot()
        registry = load_registry(self.registry_path)
        execution_binding: dict[str, Any] = {}
        unavailable: list[str] = []
        for project_id, base_sha in sorted(base_shas.items()):
            try:
                execution_binding[project_id] = host_project_execution_binding(registry.projects[project_id], base_sha)
            except Exception as exc:
                reason = str(exc) or "verification_unavailable"
                execution_binding[project_id] = {"status": "unavailable", "reason": reason}
                unavailable.append(reason)
        if unavailable:
            execution_binding["_status"] = "unavailable"
            execution_binding["_reason"] = unavailable[0]
        else:
            execution_binding["_status"] = "ready"
        if isinstance(spec, dict):
            bound_spec = {key: value for key, value in spec.items() if key != "execution_binding"}
            bound_spec["execution_binding"] = execution_binding
        elif isinstance(spec, str):
            bound_spec = {"document": spec, "execution_binding": execution_binding}
        else:
            raise ValueError("specification must be a string or object")
        return await self.store.prepare(
            feature_id,
            spec=bound_spec,
            plan_revision=plan_revision,
            base_shas=base_shas,
            profiles=profiles,
            budget=budget,
            registry_hash=snapshot["hash"],
            expected_revision=expected_revision,
            execution_binding=execution_binding,
        )

    async def set_spec(self, feature_id: str, body: Any) -> dict[str, Any]:
        result = await self.store.set_spec(feature_id, body)
        if result is None:
            raise KeyError(feature_id)
        return result

    async def set_budget(self, feature_id: str, budget: dict[str, int]) -> dict[str, Any]:
        result = await self.store.set_budget(feature_id, budget)
        if result is None:
            raise KeyError(feature_id)
        return result

    async def begin_run(self, feature_id: str, digest: str) -> dict[str, Any]:
        result = await self.store.begin_run(feature_id, digest)
        if result is None:
            raise ValueError("feature is not approved for this digest")
        return result

    async def finish(self, feature_id: str, status: str, *, expected_digest: str | None = None) -> dict[str, Any]:
        result = await self.store.finish(feature_id, status, expected_digest=expected_digest)
        if result is None:
            raise KeyError(feature_id)
        return result

    async def approve(self, feature_id: str, *, revision: int, digest: str) -> dict[str, Any]:
        result = await self.store.approve(feature_id, revision, digest)
        if result is None:
            raise ValueError("stale or already approved feature revision")
        return result

    async def invalidate(self, feature_id: str, reason: str) -> dict[str, Any]:
        result = await self.store.invalidate(feature_id, reason)
        if result is None:
            raise KeyError(feature_id)
        return result

    async def get_feature(self, feature_id: str) -> dict[str, Any] | None:
        return await self.store.get_feature(feature_id)

    async def list_features(self) -> list[dict[str, Any]]:
        return await self.store.list_features()
