"""Pure frozen-baseline evaluation policies for proposed improvements."""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from whilly.swarm.learning.domain import Principal


class ExperimentBlocked(RuntimeError):
    """Comparison evidence is stale, overlapping, or insufficient."""


class DecisionBlocked(RuntimeError):
    """An evaluation decision lacks current owner authority."""


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


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
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in self.acceptance_limits.values()
        ):
            raise ValueError("acceptance limits must be finite numeric values")
        for name, value in self.acceptance_limits.items():
            if name.endswith("_rate") and not 0 <= value <= 1:
                raise ValueError("rate acceptance limits must be between zero and one")
            if name in {"max_mean_latency_ms", "max_total_known_cost"} and value < 0:
                raise ValueError("latency and cost acceptance limits must be non-negative")
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
    manifest: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        if not self.manifest:
            raise ValueError("frozen experiment manifest is required")
        object.__setattr__(self, "manifest", _freeze(self.manifest))


@dataclass(frozen=True)
class ExportPolicy:
    enabled: bool = False
    include_raw: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.include_raw, bool):
            raise ValueError("export policy flags must be boolean")
        if self.include_raw:
            raise ValueError("raw transcripts and secrets cannot be exported")


@dataclass(frozen=True)
class ExportReceipt:
    experiment_id: str
    outcome: str
    external_reference: str | None = None
    blocker: str | None = None


@dataclass(frozen=True)
class DecisionReceipt:
    experiment_id: str
    actor_id: str
    decision: str
    reason: str
    rollback_reference: str | None
    execution_authorized: bool = False


class EvaluationStore(Protocol):
    async def save_evaluation(self, report: EvaluationReport, *, proposer_id: str, policy_version: str) -> None: ...

    async def save_decision(self, receipt: DecisionReceipt) -> None: ...


class EvalSink(Protocol):
    async def write(self, report: EvaluationReport, policy: ExportPolicy) -> str: ...


class ExperimentDecisionService:
    """Persist evidence first; optional export and decisions grant no execution authority."""

    def __init__(self, store: EvaluationStore, sink: EvalSink) -> None:
        self._store = store
        self._sink = sink

    async def persist_and_export(
        self,
        report: EvaluationReport,
        policy: ExportPolicy,
        *,
        proposer_id: str,
        policy_version: str,
    ) -> ExportReceipt:
        await self._store.save_evaluation(report, proposer_id=proposer_id, policy_version=policy_version)
        if not policy.enabled:
            return ExportReceipt(report.experiment_id, "local_only")
        try:
            reference = await self._sink.write(report, policy)
        except Exception:
            return ExportReceipt(report.experiment_id, "export_failed", blocker="optional_export_unavailable")
        return ExportReceipt(report.experiment_id, "exported", external_reference=reference)

    async def record_decision(
        self,
        principal: Principal,
        experiment_id: str,
        decision: str,
        reason: str,
        *,
        proposer_id: str,
        owner: bool,
        approved_policy_version: str | None = None,
        current_policy_version: str | None = None,
        rollback_artifact: str | None = None,
    ) -> DecisionReceipt:
        if not owner or principal.actor_id == proposer_id:
            raise DecisionBlocked("owner_required")
        if approved_policy_version != current_policy_version:
            raise DecisionBlocked("policy_version_changed")
        if decision not in {"accept", "reject", "rollback"}:
            raise DecisionBlocked("invalid_decision")
        _text(reason, "reason")
        if decision == "rollback" and not rollback_artifact:
            raise DecisionBlocked("rollback_reference_required")
        receipt = DecisionReceipt(
            experiment_id=experiment_id,
            actor_id=principal.actor_id,
            decision=decision,
            reason=reason,
            rollback_reference=rollback_artifact,
        )
        await self._store.save_decision(receipt)
        return receipt


class ExperimentService:
    """Compare frozen evidence; recommendations never authorize rollout."""

    def compare(self, manifest: ExperimentManifest, results: list[dict]) -> EvaluationReport:
        if not results:
            raise ExperimentBlocked("missing_results")
        if any(result.get("dataset_hash") != manifest.dataset_hash for result in results):
            raise ExperimentBlocked("dataset_changed")
        if any(result.get("heldout_hash") != manifest.heldout_hash for result in results):
            raise ExperimentBlocked("heldout_changed")
        if any(result.get("baseline_sha") != manifest.baseline_sha for result in results):
            raise ExperimentBlocked("baseline_changed")
        if any(result.get("candidate_sha") != manifest.candidate_sha for result in results):
            raise ExperimentBlocked("candidate_changed")
        if any(result.get("rubric_version") != manifest.rubric_version for result in results):
            raise ExperimentBlocked("rubric_changed")
        if any(result.get("policy_version") != manifest.policy_version for result in results):
            raise ExperimentBlocked("policy_changed")
        if any(result.get("model_version") not in manifest.model_versions for result in results):
            raise ExperimentBlocked("model_changed")
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

        latencies = [
            result["latency_ms"]
            for result in evaluated
            if isinstance(result.get("latency_ms"), int)
            and not isinstance(result.get("latency_ms"), bool)
            and result["latency_ms"] >= 0
        ]
        costs = [
            float(result["cost"])
            for result in evaluated
            if isinstance(result.get("cost"), (int, float))
            and not isinstance(result.get("cost"), bool)
            and math.isfinite(result["cost"])
            and result["cost"] >= 0
        ]
        unknown_cost_count = len(evaluated) - len(costs)
        metrics = {
            "correctness_rate": rate("correct"),
            "missed_escalation_rate": rate("missed_escalation"),
            "regression_rate": rate("regression"),
            "repeated_failure_rate": rate("repeated_failure"),
            "mean_latency_ms": Metric(sum(latencies) / len(latencies) if latencies else None, len(latencies)),
            "total_known_cost": Metric(sum(costs), len(costs)),
        }
        recommendation = "candidate"
        if unknown_cost_count or any(
            metric.value is None or metric.sample_size != len(evaluated) for metric in metrics.values()
        ):
            recommendation = "blocked_missing_evidence"
        limit_metrics = {
            "min_correctness_rate": ("correctness_rate", "min"),
            "max_missed_escalation_rate": ("missed_escalation_rate", "max"),
            "max_regression_rate": ("regression_rate", "max"),
            "max_repeated_failure_rate": ("repeated_failure_rate", "max"),
            "max_mean_latency_ms": ("mean_latency_ms", "max"),
            "max_total_known_cost": ("total_known_cost", "max"),
        }
        unsupported = set(manifest.acceptance_limits) - set(limit_metrics)
        if unsupported:
            raise ExperimentBlocked("unsupported_acceptance_limit")
        breached = False
        for limit_name, threshold in manifest.acceptance_limits.items():
            metric_name, direction = limit_metrics[limit_name]
            value = metrics[metric_name].value
            if value is not None and (
                (direction == "min" and value < threshold) or (direction == "max" and value > threshold)
            ):
                breached = True
        if breached:
            recommendation = "reject"
        return EvaluationReport(
            experiment_id=manifest.id,
            baseline_sha=manifest.baseline_sha,
            candidate_sha=manifest.candidate_sha,
            metrics=metrics,
            unknown_cost_count=unknown_cost_count,
            recommendation=recommendation,
            manifest={
                "id": manifest.id,
                "hypothesis": manifest.hypothesis,
                "baseline_sha": manifest.baseline_sha,
                "candidate_sha": manifest.candidate_sha,
                "dataset_hash": manifest.dataset_hash,
                "heldout_hash": manifest.heldout_hash,
                "rubric_version": manifest.rubric_version,
                "policy_version": manifest.policy_version,
                "model_versions": manifest.model_versions,
                "acceptance_limits": dict(manifest.acceptance_limits),
                "triz_record": {
                    "contradiction": manifest.triz_record.contradiction,
                    "ideal_outcome": manifest.triz_record.ideal_outcome,
                    "existing_resources": manifest.triz_record.existing_resources,
                    "alternatives": manifest.triz_record.alternatives,
                    "falsifying_measurement": manifest.triz_record.falsifying_measurement,
                },
            },
        )


__all__ = [
    "DecisionBlocked",
    "DecisionReceipt",
    "EvalSink",
    "EvaluationReport",
    "EvaluationStore",
    "ExperimentDecisionService",
    "ExperimentBlocked",
    "ExperimentManifest",
    "ExperimentService",
    "ExportPolicy",
    "ExportReceipt",
    "Metric",
    "TrizRecord",
]
