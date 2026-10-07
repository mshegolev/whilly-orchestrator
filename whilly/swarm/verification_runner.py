"""Execute enrolled verification gates through the guarded executor.

The runner keeps the acceptance boolean separate from parser counters.  Store
acceptance therefore remains a compatibility wrapper while structured gate
evidence retains collected/passed/skipped counts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from whilly.core.swarm_execution import ExecutionPolicy
from whilly.swarm.execution import GuardedExecutor
from whilly.swarm.verification import GateEvidence, parse_gate_result

__all__ = ["VerificationRunner"]


class VerificationRunner:
    """Run trusted verification commands and produce store-compatible items."""

    @staticmethod
    def wrap_evidence(
        *,
        stage: str,
        category: str,
        argv: tuple[str, ...],
        head_sha: str,
        policy_digest: str,
        outcome: str,
        collected: int | None,
        passed: int | None,
        skipped: int | None,
        exit_code: int | None = 0,
    ) -> dict[str, Any]:
        evidence = GateEvidence(
            stage=stage,
            category=category,
            argv=argv,
            outcome=outcome,
            exit_code=exit_code,
            collected=collected,
            passed=passed,
            skipped=skipped,
            head_sha=head_sha,
            policy_digest=policy_digest,
        )
        # Keep the legacy top-level envelope as well as the authoritative
        # structured evidence.  Review/store consumers from Task 6 still
        # read these fields directly.
        return {
            "passed": evidence.outcome == "passed",
            "argv": list(evidence.argv),
            "exit_code": evidence.exit_code,
            "outcome": evidence.outcome,
            "stdout_tail": "",
            "evidence": {
                "stage": evidence.stage,
                "category": evidence.category,
                "argv": list(evidence.argv),
                "outcome": evidence.outcome,
                "exit_code": evidence.exit_code,
                "collected": evidence.collected,
                "passed": evidence.passed,
                "skipped": evidence.skipped,
                "head_sha": evidence.head_sha,
                "policy_digest": evidence.policy_digest,
            },
        }

    @classmethod
    def item_from_evidence(cls, evidence: GateEvidence) -> dict[str, Any]:
        return cls.wrap_evidence(
            stage=evidence.stage,
            category=evidence.category,
            argv=evidence.argv,
            head_sha=evidence.head_sha,
            policy_digest=evidence.policy_digest,
            outcome=evidence.outcome,
            collected=evidence.collected,
            passed=evidence.passed,
            skipped=evidence.skipped,
            exit_code=evidence.exit_code,
        )

    async def run(
        self,
        commands: Sequence[tuple[str, str, tuple[str, ...]]],
        *,
        executor: GuardedExecutor,
        policy: ExecutionPolicy,
        environment: Mapping[str, str],
        cwd: str | Path,
        log_dir: str | Path,
        stage: str,
        head_sha: str,
        policy_digest: str,
    ) -> list[dict[str, Any]]:
        """Run ``(category, parser_kind, argv)`` commands with enrolled parsers."""
        items: list[dict[str, Any]] = []
        for category, parser_kind, argv in commands:
            outcome = await executor.run(
                argv,
                phase="verify",
                cwd=cwd,
                policy=policy,
                environment=environment,
                log_dir=log_dir,
            )
            report = Path(outcome.stdout_path).read_bytes()
            evidence = parse_gate_result(
                parser_kind,
                outcome,
                report,
                stage=stage,
                category=category,
                argv=tuple(argv),
                head_sha=head_sha,
                policy_digest=policy_digest,
            )
            item = self.item_from_evidence(evidence)
            # Keep the legacy review/store envelope while making the nested
            # GateEvidence the authoritative structured result.
            item.update(
                {
                    "argv": list(argv),
                    "exit_code": outcome.exit_code,
                    "timed_out": outcome.timed_out,
                    "outcome": evidence.outcome,
                    "duration_seconds": round(outcome.duration_seconds, 3),
                    "stdout_tail": _tail(outcome.stdout_path, 1500),
                    "stderr_tail": _tail(outcome.stderr_path, 1500),
                    "log": outcome.stdout_path,
                }
            )
            items.append(item)
        return items


def _tail(path: str | Path, limit: int) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""
