"""Pure frozen-baseline evaluation policies for proposed improvements."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class ExperimentBlocked(RuntimeError):
    """Comparison evidence is stale, overlapping, or insufficient."""


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class TrizRecord:
    contradiction: str
    ideal_outcome: str
    existing_resources: tuple[str, ...]
    alternatives: tuple[str, ...]
    falsifying_measurement: str

    def __post_init__(self) -> None:
        for name in ("contradiction", "ideal_outcome", "falsifying_measurement"):
            _text(getattr(self, name), name)
        if not self.existing_resources or not self.alternatives:
            raise ValueError("TRIZ resources and alternatives are required")


@dataclass(frozen=True)
class ExperimentManifest:
    id: str
    hypothesis: str
    baseline_sha: str
    candidate_sha: str
    dataset_hash: str
    heldout_hash: str
    rubric_version: str
    policy_version: str
    model_versions: tuple[str, ...]
    acceptance_limits: Mapping[str, float]
    triz_record: TrizRecord

    def __post_init__(self) -> None:
        for name in (
            "id",
            "hypothesis",
            "baseline_sha",
            "candidate_sha",
            "dataset_hash",
            "heldout_hash",
            "rubric_version",
            "policy_version",
        ):
            _text(getattr(self, name), name)
        if not self.model_versions or not self.acceptance_limits:
            raise ValueError("model versions and owner-approved acceptance limits are required")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) for value in self.acceptance_limits.values()
        ):
            raise ValueError("acceptance limits must be numeric")
        object.__setattr__(self, "acceptance_limits", MappingProxyType(dict(self.acceptance_limits)))


@dataclass(frozen=True)
class Metric:
    value: float | None
    sample_size: int


@dataclass(frozen=True)
class EvaluationReport:
    experiment_id: str
    baseline_sha: str
    candidate_sha: str
    metrics: Mapping[str, Metric]
    unknown_cost_count: int
    recommendation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


class ExperimentService:
    """Compare frozen evidence; recommendations never authorize rollout."""

    def compare(self, manifest: ExperimentManifest, results: list[dict]) -> EvaluationReport:
        if not results:
            raise ExperimentBlocked("missing_results")
        if any(result.get("dataset_hash") != manifest.dataset_hash for result in results):
            raise ExperimentBlocked("dataset_changed")
        if any(result.get("heldout_hash") != manifest.heldout_hash for result in results):
            raise ExperimentBlocked("heldout_changed")
        tuning = {str(result.get("example_id")) for result in results if result.get("split") == "tuning"}
        heldout = {str(result.get("example_id")) for result in results if result.get("split") == "heldout"}
        if tuning & heldout:
            raise ExperimentBlocked("heldout_overlap")
        evaluated = [result for result in results if result.get("split") == "heldout"]
        if not evaluated:
            raise ExperimentBlocked("missing_heldout_results")

        def rate(field: str) -> Metric:
            values = [result[field] for result in evaluated if isinstance(result.get(field), bool)]
            return Metric(sum(bool(value) for value in values) / len(values) if values else None, len(values))

        latencies = [result["latency_ms"] for result in evaluated if isinstance(result.get("latency_ms"), int)]
        costs = [float(result["cost"]) for result in evaluated if isinstance(result.get("cost"), (int, float))]
        unknown_cost_count = sum(result.get("cost") is None for result in evaluated)
        metrics = {
            "correctness_rate": rate("correct"),
            "missed_escalation_rate": rate("missed_escalation"),
            "regression_rate": rate("regression"),
            "repeated_failure_rate": rate("repeated_failure"),
            "mean_latency_ms": Metric(sum(latencies) / len(latencies) if latencies else None, len(latencies)),
            "total_known_cost": Metric(sum(costs), len(costs)),
        }
        recommendation = "candidate"
        if unknown_cost_count or any(metric.value is None for metric in metrics.values()):
            recommendation = "blocked_missing_evidence"
        regression = metrics["regression_rate"].value
        missed = metrics["missed_escalation_rate"].value
        if (regression is not None and regression > manifest.acceptance_limits.get("max_regression_rate", 0.0)) or (
            missed is not None and missed > manifest.acceptance_limits.get("max_missed_escalation_rate", 0.0)
        ):
            recommendation = "reject"
        return EvaluationReport(
            experiment_id=manifest.id,
            baseline_sha=manifest.baseline_sha,
            candidate_sha=manifest.candidate_sha,
            metrics=metrics,
            unknown_cost_count=unknown_cost_count,
            recommendation=recommendation,
        )


__all__ = [
    "EvaluationReport",
    "ExperimentBlocked",
    "ExperimentManifest",
    "ExperimentService",
    "Metric",
    "TrizRecord",
]
