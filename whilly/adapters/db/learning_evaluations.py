"""PostgreSQL persistence for research reports and frozen evaluations."""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any

import asyncpg

from whilly.swarm.learning.experiments import DecisionReceipt, EvaluationReport, Metric
from whilly.swarm.learning.research import DailyReport, ReportSource


class PostgresLearningControlStore:
    def __init__(self, pool: asyncpg.Pool, *, product_id: str) -> None:
        if not product_id.strip():
            raise ValueError("product_id must be non-empty")
        self.pool = pool
        self.product_id = product_id

    async def save_report(self, report: DailyReport) -> DailyReport:
        payload = _dump(report)
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """INSERT INTO swarm_learning_reports(product_id,run_id,report) VALUES ($1,$2,$3::jsonb)
                ON CONFLICT (product_id,run_id) DO NOTHING""",
                self.product_id,
                report.run_id,
                payload,
            )
            if result.endswith("0"):
                existing = await conn.fetchval(
                    "SELECT report::text FROM swarm_learning_reports WHERE product_id=$1 AND run_id=$2",
                    self.product_id,
                    report.run_id,
                )
                existing_report = _daily(json.loads(existing))
                same_digest = bool(report.request_digest) and existing_report.request_digest == report.request_digest
                same_payload = json.loads(existing) == json.loads(payload)
                if not (same_digest or same_payload):
                    raise ValueError("report identity conflict")
                return existing_report
        return report

    async def get_report(self, run_id: str) -> DailyReport | None:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT report FROM swarm_learning_reports WHERE product_id=$1 AND run_id=$2",
                self.product_id,
                run_id,
            )
        return _daily(_object(value)) if value is not None else None

    async def stop(self, run_id: str) -> bool:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO swarm_learning_stops(product_id,run_id) VALUES ($1,$2)
                ON CONFLICT (product_id,run_id) DO NOTHING""",
                self.product_id,
                run_id,
            )
            await conn.execute(
                """UPDATE swarm_learning_runs SET stop_requested=TRUE,status='stopped',blocker='stop_requested',
                finished_at=COALESCE(finished_at,NOW()) WHERE product_id=$1 AND id=$2 AND status='running'""",
                self.product_id,
                run_id,
            )
        return True

    async def is_stopped(self, run_id: str) -> bool:
        async with self.pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """SELECT EXISTS(
                    SELECT 1 FROM swarm_learning_stops WHERE product_id=$1 AND run_id=$2
                    UNION ALL
                    SELECT 1 FROM swarm_learning_runs WHERE product_id=$1 AND id=$2 AND stop_requested
                    )""",
                    self.product_id,
                    run_id,
                )
            )

    async def save_evaluation(self, report: EvaluationReport, *, proposer_id: str, policy_version: str) -> None:
        payload = _dump(report)
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """INSERT INTO swarm_learning_experiments(product_id,id,proposer_id,policy_version,report)
                VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT (product_id,id) DO NOTHING""",
                self.product_id,
                report.experiment_id,
                proposer_id,
                policy_version,
                payload,
            )
            if result.endswith("0"):
                existing = await conn.fetchrow(
                    """SELECT proposer_id,policy_version,report::text FROM swarm_learning_experiments
                    WHERE product_id=$1 AND id=$2""",
                    self.product_id,
                    report.experiment_id,
                )
                if (
                    existing["proposer_id"] != proposer_id
                    or existing["policy_version"] != policy_version
                    or json.loads(existing["report"]) != json.loads(payload)
                ):
                    raise ValueError("evaluation identity conflict")

    async def save_decision(self, receipt: DecisionReceipt) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO swarm_learning_experiment_decisions
                (product_id,experiment_id,actor_id,decision,reason,rollback_reference,execution_authorized)
                VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                self.product_id,
                receipt.experiment_id,
                receipt.actor_id,
                receipt.decision,
                receipt.reason,
                receipt.rollback_reference,
                receipt.execution_authorized,
            )

    async def get_evaluation(self, experiment_id: str) -> EvaluationReport | None:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT report FROM swarm_learning_experiments WHERE product_id=$1 AND id=$2",
                self.product_id,
                experiment_id,
            )
        return _evaluation(_object(value)) if value is not None else None

    async def experiment_metadata(self, experiment_id: str) -> dict[str, str] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT proposer_id,policy_version FROM swarm_learning_experiments
                WHERE product_id=$1 AND id=$2""",
                self.product_id,
                experiment_id,
            )
        return dict(row) if row is not None else None

    async def decisions(self, experiment_id: str) -> tuple[DecisionReceipt, ...]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT actor_id,decision,reason,rollback_reference,execution_authorized
                FROM swarm_learning_experiment_decisions WHERE product_id=$1 AND experiment_id=$2
                ORDER BY event_id""",
                self.product_id,
                experiment_id,
            )
        return tuple(
            DecisionReceipt(
                experiment_id,
                row["actor_id"],
                row["decision"],
                row["reason"],
                row["rollback_reference"],
                row["execution_authorized"],
            )
            for row in rows
        )


def _dump(value: Any) -> str:
    return json.dumps(_jsonable(value))


def _object(value: Any) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    return dict(parsed)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _daily(value: dict[str, Any]) -> DailyReport:
    value["generated_at"] = datetime.fromisoformat(value["generated_at"])
    value["sources"] = tuple(ReportSource(**source) for source in value["sources"])
    for name in ("observations", "missing_data", "hypotheses", "proposals", "policy_actions"):
        value[name] = tuple(value[name])
    return DailyReport(**value)


def _evaluation(value: dict[str, Any]) -> EvaluationReport:
    value["metrics"] = {name: Metric(**metric) for name, metric in value["metrics"].items()}
    return EvaluationReport(**value)


__all__ = ["PostgresLearningControlStore"]
