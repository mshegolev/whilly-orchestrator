"""Structural tests for .github/workflows/semantic-drift.yml (CI-01).

Fully offline: no network, no `act`, no `gh`. We parse the workflow with
yaml.safe_load and string-assert the raw text. The scheduled semantic guard
must be triggered ONLY by schedule + workflow_dispatch (never pull_request /
push), invoke scripts/semantic_drift_check.py --all, upload the JSON artifact,
render the summary into GITHUB_STEP_SUMMARY, fail fast on a missing
ANTHROPIC_API_KEY, and consume the operator-supplied posture via an env var
rather than inline shell interpolation (command-injection mitigation T-32-01).
"""

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "semantic-drift.yml"


def _load() -> dict:
    assert _WORKFLOW.is_file(), f"missing {_WORKFLOW}"
    raw = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "workflow must parse as a mapping"
    return raw


def _triggers(doc: dict) -> dict:
    # PyYAML applies YAML 1.1 rules: the bare `on:` key parses to the Python
    # boolean True, NOT the string "on". The triggers mapping lives under True.
    assert True in doc, "workflow 'on' (parsed as True) is missing"
    triggers = doc[True]
    assert isinstance(triggers, dict), "'on' must be a mapping of triggers"
    return triggers


def test_workflow_is_valid_yaml():
    doc = _load()
    assert doc.get("name"), "workflow must declare a name"
    assert "jobs" in doc and isinstance(doc["jobs"], dict)


def test_triggers_are_schedule_and_workflow_dispatch_only():
    triggers = _triggers(_load())
    assert set(triggers.keys()) == {"schedule", "workflow_dispatch"}


def test_no_pull_request_or_push_trigger():
    triggers = _triggers(_load())
    assert "pull_request" not in triggers
    assert "push" not in triggers


def test_schedule_has_a_cron_entry():
    triggers = _triggers(_load())
    schedule = triggers["schedule"]
    assert isinstance(schedule, list) and schedule, "schedule must be a non-empty list"
    assert any("cron" in entry for entry in schedule), "schedule must declare a cron value"


def test_workflow_dispatch_declares_posture_input():
    triggers = _triggers(_load())
    dispatch = triggers["workflow_dispatch"]
    assert isinstance(dispatch, dict), "workflow_dispatch must declare inputs"
    inputs = dispatch.get("inputs") or {}
    assert "posture" in inputs, "workflow_dispatch must declare a 'posture' input"


def test_invokes_drift_check_all():
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/semantic_drift_check.py --all" in text


def test_uploads_artifact_and_writes_step_summary():
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "actions/upload-artifact" in text
    assert "GITHUB_STEP_SUMMARY" in text


def test_references_anthropic_api_key_secret():
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" in text
    assert "secrets.ANTHROPIC_API_KEY" in text


def test_posture_consumed_via_env_not_inline_interpolation():
    text = _WORKFLOW.read_text(encoding="utf-8")
    lines = text.splitlines()

    # The posture input must be bound to a POSTURE env var.
    assert any("POSTURE:" in ln and "github.event.inputs.posture" in ln for ln in lines), (
        "expected a POSTURE env assignment from github.event.inputs.posture"
    )

    # No run: shell line may embed the github.event.inputs.posture interpolation.
    # Walk the file, tracking whether we are inside a `run:` block-scalar body.
    in_run = False
    run_indent = 0
    for ln in lines:
        stripped = ln.strip()
        indent = len(ln) - len(ln.lstrip())
        if not in_run:
            if stripped.startswith("run:"):
                in_run = True
                run_indent = indent
            continue
        # Inside a run block: it ends at the next line indented <= the run key.
        if stripped and indent <= run_indent:
            in_run = False
            if stripped.startswith("run:"):
                in_run = True
                run_indent = indent
            continue
        assert "github.event.inputs.posture" not in ln, f"posture interpolated directly into a run shell line: {ln!r}"
