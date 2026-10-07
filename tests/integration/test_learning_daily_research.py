from __future__ import annotations

import asyncio

import pytest

from tests.integration.test_swarm_runtime import db_pool, swarm_dsn  # noqa: F401
from whilly.adapters.db.learning_evaluations import PostgresLearningControlStore
from whilly.swarm.learning.experiments import DecisionReceipt, EvaluationReport, Metric
from whilly.swarm.learning.research import DailyReport, ResearchController, RetrospectiveService

pytestmark = pytest.mark.integration


async def test_learning_run_migration_is_disabled_by_default(db_pool) -> None:  # noqa: F811
    row = await db_pool.fetchrow(
        "SELECT COUNT(*) AS count, COALESCE(bool_or(enabled), FALSE) AS enabled FROM swarm_learning_schedules"
    )
    assert row["count"] == 0
    assert row["enabled"] is False


async def test_reports_experiments_and_decisions_survive_store_restart(db_pool) -> None:  # noqa: F811
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO swarm_products(id,name) VALUES ('learning-test','Learning Test')")
    store = PostgresLearningControlStore(db_pool, product_id="learning-test")
    daily = DailyReport(
        run_id="manual-run-1",
        outcome="observed",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        sources=(),
        observations=("one blocked outcome",),
        sample_size=1,
        missing_data=(),
        hypotheses=("setup gap",),
        proposals=("review setup",),
        latency_ms=42,
        known_cost=0.25,
        unknown_cost_count=0,
    )
    evaluation = EvaluationReport(
        experiment_id="exp-1",
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
        metrics={"correctness_rate": Metric(1.0, 1)},
        unknown_cost_count=0,
        recommendation="candidate",
        manifest={"id": "exp-1", "policy_version": "p1"},
    )
    await store.save_report(daily)
    conflicting_daily = DailyReport(
        run_id="manual-run-1",
        outcome="observed",
        generated_at=daily.generated_at,
        sources=(),
        observations=("different evidence",),
        sample_size=1,
        missing_data=(),
        hypotheses=(),
        proposals=(),
        latency_ms=42,
        known_cost=0.25,
        unknown_cost_count=0,
    )
    with pytest.raises(ValueError, match="report identity conflict"):
        await store.save_report(conflicting_daily)
    await store.save_evaluation(evaluation, proposer_id="agent-1", policy_version="p1")
    await store.save_decision(DecisionReceipt("exp-1", "owner-1", "accept", "verified", None))

    restored = PostgresLearningControlStore(db_pool, product_id="learning-test")
    assert await restored.get_report("manual-run-1") == daily
    assert await restored.get_evaluation("exp-1") == evaluation
    decisions = await restored.decisions("exp-1")
    assert decisions[0].actor_id == "owner-1"
    assert decisions[0].execution_authorized is False
    assert await restored.stop("manual-run-2") is True
    assert await restored.is_stopped("manual-run-2") is True

    controller = ResearchController(restored, RetrospectiveService())
    events = [{"kind": "task_outcome", "outcome": "blocked", "cost": None}]
    first, second = await asyncio.gather(
        controller.run_fixture("manual-run-race", events),
        controller.run_fixture("manual-run-race", events),
    )
    assert first.report == second.report

    conflicting = EvaluationReport(
        experiment_id="exp-1",
        baseline_sha="a" * 40,
        candidate_sha="c" * 40,
        metrics={"correctness_rate": Metric(0.0, 1)},
        unknown_cost_count=0,
        recommendation="baseline",
        manifest={"id": "exp-1", "policy_version": "p1"},
    )
    with pytest.raises(ValueError, match="evaluation identity conflict"):
        await restored.save_evaluation(conflicting, proposer_id="agent-1", policy_version="p1")
