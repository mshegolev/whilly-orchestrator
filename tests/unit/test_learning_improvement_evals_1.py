from __future__ import annotations

import pytest

from whilly.swarm.learning.experiments import ExperimentBlocked, ExperimentManifest, ExperimentService, TrizRecord


def _manifest(**overrides: object) -> ExperimentManifest:
    values = {
        "id": "exp-1",
        "hypothesis": "A narrower prompt reduces missed escalations",
        "baseline_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "dataset_hash": "dataset-v1",
        "heldout_hash": "heldout-v1",
        "rubric_version": "r1",
        "policy_version": "p1",
        "model_versions": ("model-cheap-v1",),
        "acceptance_limits": {"max_regression_rate": 0.0, "max_missed_escalation_rate": 0.1},
        "triz_record": TrizRecord(
            contradiction="more context increases cost",
            ideal_outcome="correct escalation with bounded context",
            existing_resources=("verified memory",),
            alternatives=("no change", "narrow retrieval"),
            falsifying_measurement="held-out missed escalation rate increases",
        ),
    }
    values.update(overrides)
    return ExperimentManifest(**values)


def _result(**overrides: object) -> dict:
    value = {
        "example_id": "held-1",
        "split": "heldout",
        "dataset_hash": "dataset-v1",
        "heldout_hash": "heldout-v1",
        "baseline_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "rubric_version": "r1",
        "policy_version": "p1",
        "model_version": "model-cheap-v1",
        "correct": True,
        "missed_escalation": False,
        "regression": False,
        "repeated_failure": False,
        "latency_ms": 100,
        "cost": 0.25,
    }
    value.update(overrides)
    return value


def test_changed_dataset_invalidates_comparison() -> None:
    with pytest.raises(ExperimentBlocked, match="dataset_changed"):
        ExperimentService().compare(_manifest(), [_result(dataset_hash="dataset-v2")])


def test_tuning_heldout_overlap_blocks() -> None:
    with pytest.raises(ExperimentBlocked, match="heldout_overlap"):
        ExperimentService().compare(
            _manifest(),
            [_result(split="tuning", example_id="same"), _result(split="heldout", example_id="same")],
        )


def test_unknown_cost_is_not_zero() -> None:
    report = ExperimentService().compare(_manifest(), [_result(cost=None)])

    assert report.metrics["total_known_cost"].value == 0
    assert report.metrics["total_known_cost"].sample_size == 0
    assert report.unknown_cost_count == 1
    assert report.recommendation == "blocked_missing_evidence"


def test_non_numeric_cost_and_partial_metrics_block_recommendation() -> None:
    report = ExperimentService().compare(
        _manifest(),
        [_result(example_id="held-1"), _result(example_id="held-2", cost="unavailable", correct="missing")],
    )

    assert report.unknown_cost_count == 1
    assert report.metrics["correctness_rate"].sample_size == 1
    assert report.recommendation == "blocked_missing_evidence"


def test_every_declared_acceptance_limit_is_enforced() -> None:
    report = ExperimentService().compare(
        _manifest(acceptance_limits={"min_correctness_rate": 1.0}),
        [_result(correct=False)],
    )

    assert report.recommendation == "reject"


def test_result_provenance_must_match_frozen_manifest() -> None:
    with pytest.raises(ExperimentBlocked, match="candidate_changed"):
        ExperimentService().compare(_manifest(), [_result(candidate_sha="c" * 40)])

    report = ExperimentService().compare(_manifest(), [_result()])
    assert report.manifest["dataset_hash"] == "dataset-v1"
    assert report.manifest["triz_record"]["falsifying_measurement"]
    with pytest.raises(TypeError):
        report.manifest["acceptance_limits"]["max_regression_rate"] = 1


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1, True])
def test_invalid_cost_is_missing_evidence(cost: object) -> None:
    report = ExperimentService().compare(_manifest(), [_result(cost=cost)])

    assert report.unknown_cost_count == 1
    assert report.recommendation == "blocked_missing_evidence"


@pytest.mark.parametrize("limit", [float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_rate_limit_is_rejected(limit: float) -> None:
    with pytest.raises(ValueError):
        _manifest(acceptance_limits={"min_correctness_rate": limit})


def test_regression_blocks_recommendation() -> None:
    report = ExperimentService().compare(_manifest(), [_result(regression=True)])

    assert report.metrics["regression_rate"].value == 1.0
    assert report.metrics["correctness_rate"].sample_size == 1
    assert report.recommendation == "reject"
