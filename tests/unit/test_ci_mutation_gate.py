"""Structural contract for the scoped mutation-testing CI gate."""

from pathlib import Path

import yaml


_CI_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"


def test_mutation_job_runs_after_lint_and_unit_tests() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    mutation = workflow["jobs"]["mutation"]

    assert set(mutation["needs"]) == {"lint", "test"}
    assert mutation["permissions"] == {"contents": "read"}
    checkout = next(step for step in mutation["steps"] if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"] == {
        "repository": "${{ github.event.pull_request.head.repo.full_name || github.repository }}",
        "ref": "${{ github.head_ref || github.ref_name }}",
        "persist-credentials": False,
    }
    commands = "\n".join(str(step.get("run", "")) for step in mutation["steps"])
    assert "mutmut run --max-children 1" in commands
    assert "mutation_status=$?" in commands
    assert "mutmut results --all true" in commands
    assert "results_status=$?" in commands
    assert 'exit "$mutation_status"' in commands
    assert 'exit "$results_status"' in commands
