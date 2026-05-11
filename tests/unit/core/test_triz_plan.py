"""Unit tests for deterministic v4 plan-level TRIZ preflight."""

from __future__ import annotations

import json

from whilly.core.models import Plan, Priority, Task, TaskStatus
from whilly.core.triz import analyze_plan_triz, format_plan_triz_report, plan_triz_report_to_dict


def _task(
    task_id: str,
    *,
    description: str = "Implement a concrete, testable user-facing behavior.",
    dependencies: tuple[str, ...] = (),
    key_files: tuple[str, ...] = (),
    acceptance_criteria: tuple[str, ...] = ("behavior is observable",),
    test_steps: tuple[str, ...] = ("pytest -q",),
) -> Task:
    return Task(
        id=task_id,
        status=TaskStatus.PENDING,
        dependencies=dependencies,
        key_files=key_files,
        priority=Priority.MEDIUM,
        description=description,
        acceptance_criteria=acceptance_criteria,
        test_steps=test_steps,
    )


def test_plan_triz_approves_clean_plan() -> None:
    plan = Plan(
        id="clean",
        name="Clean",
        tasks=(
            _task("A", description="Implement the API validation behavior.", key_files=("whilly/a.py",)),
            _task(
                "B",
                description="Add dashboard rendering for validation results.",
                dependencies=("A",),
                key_files=("whilly/b.py",),
            ),
        ),
    )

    report = analyze_plan_triz(plan)

    assert report.verdict == "approve"
    assert report.findings == ()
    assert report.ideality_score == 1.0
    assert "no structural" in report.summary


def test_plan_triz_reports_gate_dependency_duplicate_and_resource_findings() -> None:
    plan = Plan(
        id="messy",
        name="Messy",
        tasks=(
            _task(
                "A",
                description="short",
                acceptance_criteria=(),
                test_steps=(),
                key_files=("whilly/shared.py",),
            ),
            _task(
                "B",
                description="Implement shared behavior",
                dependencies=("MISSING",),
                key_files=("whilly/shared.py",),
            ),
            _task("C", description="Implement shared behavior"),
        ),
    )

    report = analyze_plan_triz(plan)
    categories = {finding.category for finding in report.findings}

    assert report.verdict == "revise"
    assert "weak_task_definition" in categories
    assert "missing_dependency_resource" in categories
    assert "duplicate_work" in categories
    assert "resource_conflict" in categories
    assert ("B", "C") in report.mergeable_groups
    assert "C" in report.removable_tasks


def test_plan_triz_rejects_dependency_cycle() -> None:
    plan = Plan(
        id="cycle",
        name="Cycle",
        tasks=(
            _task("A", dependencies=("B",)),
            _task("B", dependencies=("A",)),
        ),
    )

    report = analyze_plan_triz(plan)

    assert report.verdict == "reject"
    assert any(finding.category == "dependency_contradiction" for finding in report.findings)
    assert any(finding.severity == "critical" for finding in report.findings)


def test_plan_triz_report_serializes_and_formats() -> None:
    report = analyze_plan_triz(
        Plan(
            id="p",
            name="P",
            tasks=(
                _task(
                    "A",
                    description="Build a generic future-proof framework",
                    acceptance_criteria=(),
                    test_steps=(),
                ),
            ),
        )
    )

    payload = plan_triz_report_to_dict(report)
    text = format_plan_triz_report(report)

    json.dumps(payload)
    assert payload["plan_id"] == "p"
    assert "TRIZ Plan Analysis: p" in text
    assert "over_engineering_risk" in text
