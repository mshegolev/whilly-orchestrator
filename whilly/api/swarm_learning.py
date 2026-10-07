"""Admin-only manual research and evaluated-improvement control surface."""

from __future__ import annotations

from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from whilly.adapters.db.learning_evaluations import PostgresLearningControlStore
from whilly.api.admin_users_routes import require_admin_role
from whilly.api.auth_routes import DEFAULT_SESSION_COOKIE_NAME
from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.experiments import DecisionBlocked, ExperimentDecisionService
from whilly.swarm.learning.research import ResearchBlocked, ResearchController, RetrospectiveService


class DryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    events: list[dict[str, Any]] = Field(max_length=1000)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject", "rollback"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_policy_version: str = Field(min_length=1, max_length=128)
    rollback_artifact: str | None = Field(default=None, max_length=256)


class _DisabledSink:
    async def write(self, report, policy):
        del report, policy
        raise RuntimeError("export_policy_required")


def _evaluation_payload(value: Any) -> dict[str, object]:
    return {
        "experiment_id": value.experiment_id,
        "baseline_sha": value.baseline_sha,
        "candidate_sha": value.candidate_sha,
        "metrics": {
            name: {"value": metric.value, "sample_size": metric.sample_size} for name, metric in value.metrics.items()
        },
        "unknown_cost_count": value.unknown_cost_count,
        "recommendation": value.recommendation,
        "manifest": _plain(value.manifest),
    }


def _plain(value: Any) -> Any:
    if hasattr(value, "items"):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def build_learning_router(
    pool: asyncpg.Pool,
    secret: bytes,
    *,
    cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
    store: Any | None = None,
) -> APIRouter:
    """Build a fail-closed surface; live schedules and exports stay disabled."""

    backend = store or PostgresLearningControlStore(pool, product_id="default")
    research = ResearchController(backend, RetrospectiveService())
    decisions = ExperimentDecisionService(backend, _DisabledSink())
    admin = Depends(require_admin_role(pool=pool, secret=secret, cookie_name=cookie_name))
    router = APIRouter(prefix="/api/v1/swarm/learning", tags=["swarm-learning"])

    @router.get("/status")
    async def status(principal: dict[str, object] = admin) -> dict[str, object]:
        del principal
        return {
            "schedule_enabled": False,
            "export_enabled": False,
            "blockers": ["schedule_configuration_required", "export_policy_required"],
        }

    @router.post("/research/dry-run")
    async def dry_run(request: DryRunRequest, principal: dict[str, object] = admin) -> dict[str, object]:
        del principal
        try:
            result = await research.run_fixture(request.run_id, request.events)
        except ResearchBlocked as exc:
            status_code = 409 if str(exc) == "stop_requested" else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return jsonable_encoder(result)

    @router.get("/research/{run_id}/report")
    async def report(run_id: str, principal: dict[str, object] = admin) -> dict[str, object]:
        del principal
        value = await research.report(run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="report_not_found")
        return jsonable_encoder(value)

    @router.post("/research/{run_id}/stop")
    async def stop(run_id: str, principal: dict[str, object] = admin) -> dict[str, object]:
        del principal
        return {"run_id": run_id, "stopped": await research.stop(run_id), "outcome": "stop_requested"}

    @router.get("/experiments/{experiment_id}")
    async def experiment(experiment_id: str, principal: dict[str, object] = admin) -> dict[str, object]:
        del principal
        value = await backend.get_evaluation(experiment_id)
        if value is None:
            raise HTTPException(status_code=404, detail="experiment_not_found")
        return {
            "report": _evaluation_payload(value),
            "decisions": jsonable_encoder(await backend.decisions(experiment_id)),
            "execution_authorized": False,
        }

    @router.post("/experiments/{experiment_id}/decision")
    async def decide(
        experiment_id: str,
        request: DecisionRequest,
        principal: dict[str, object] = admin,
    ) -> dict[str, object]:
        metadata = await backend.experiment_metadata(experiment_id)
        if metadata is None:
            raise HTTPException(status_code=404, detail="experiment_not_found")
        actor = str(principal.get("username") or principal.get("email") or "")
        try:
            receipt = await decisions.record_decision(
                Principal(actor, ("default",), (), ("internal",)),
                experiment_id,
                request.decision,
                request.reason,
                proposer_id=metadata["proposer_id"],
                owner=True,
                approved_policy_version=request.expected_policy_version,
                current_policy_version=metadata["policy_version"],
                rollback_artifact=request.rollback_artifact,
            )
        except DecisionBlocked as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return jsonable_encoder(receipt)

    return router


__all__ = ["DecisionRequest", "DryRunRequest", "build_learning_router"]
