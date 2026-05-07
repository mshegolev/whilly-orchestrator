"""Tests for universal project configuration and adaptive plan generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whilly.adapters.filesystem.plan_io import parse_plan_dict
from whilly.cli import main
from whilly.project_config import ProjectConfigError, build_plan_payload, load_project_config, project_config_from_dict


def _etl_config() -> dict:
    return {
        "name": "SALES_ETL release verification",
        "project_type": "etl",
        "description": "Verify ETL release from Jira through STLC.",
        "environment": "STAGE",
        "task_sources": [{"kind": "jira", "ref": "REL-1234"}],
        "repositories": [
            {
                "id": "etl-main",
                "role": "code",
                "provider": "gitlab",
                "repo_full_name": "example/etl/etl-main",
                "clone_url": "https://gitlab.example.test/example/etl/etl-main.git",
                "ref": "20260507",
                "ref_type": "tag",
            },
            {
                "id": "deploy",
                "role": "deployment",
                "provider": "gitlab",
                "repo_full_name": "example/etl/deploy",
                "clone_url": "https://gitlab.example.test/example/etl/deploy.git",
                "ref": "release-20260507",
                "ref_type": "branch",
            },
            {
                "id": "tests",
                "role": "tests",
                "path": "~/etl_testing",
                "suite": "SALES_ETL",
                "writable": True,
            },
        ],
        "human_loop": {
            "enabled": True,
            "approval_channel": "slack:#qa-release",
            "instructions": "QA engineer approves test plan, STAGE deploy, and final release decision.",
        },
        "release_policy": {"success_state": "Deploy"},
        "outputs": {"release_context": "out/rel-1234-release-context.json"},
    }


def test_etl_config_uses_default_qa_stlc_pipeline_and_generates_valid_plan() -> None:
    config = project_config_from_dict(_etl_config())

    payload = build_plan_payload(config, plan_id="etl-release")
    plan, tasks = parse_plan_dict(payload)

    assert plan.id == "etl-release"
    assert len(tasks) == 8
    assert payload["repo_targets"][0]["id"] == "gitlab:example/etl/etl-main"
    assert any("HUMAN-IN-THE-LOOP CHECKPOINT" in task["description"] for task in payload["tasks"])
    generate = next(task for task in payload["tasks"] if task["id"].endswith("GENERATE-AUTOTESTS"))
    assert generate["key_files"] == ["~/etl_testing", "out/rel-1234-release-context.json"]


def test_graphql_config_generates_api_autotest_pipeline() -> None:
    config = project_config_from_dict(
        {
            "name": "Billing GraphQL API",
            "project_type": "graphql_api",
            "task_sources": [{"kind": "github", "ref": "owner/api#42"}],
            "repositories": [
                {
                    "id": "api",
                    "role": "code",
                    "provider": "github",
                    "repo_full_name": "owner/api",
                    "clone_url": "https://github.com/owner/api.git",
                },
                {"id": "api-tests", "role": "tests", "path": "tests/graphql", "writable": True},
            ],
            "human_loop": {"required_steps": ["generate-api-autotests"]},
        }
    )

    payload = build_plan_payload(config)

    assert [task["id"] for task in payload["tasks"]] == [
        "CFG-001-COLLECT-API-REQUIREMENTS",
        "CFG-002-INSPECT-SCHEMA",
        "CFG-003-GENERATE-API-AUTOTESTS",
        "CFG-004-RUN-API-TESTS",
        "CFG-005-HUMAN-API-REVIEW",
    ]
    generated = payload["tasks"][2]
    assert "GraphQL contract and integration tests" in generated["description"]
    assert "HUMAN-IN-THE-LOOP CHECKPOINT" in generated["description"]


def test_feature_development_config_supports_decomposition_to_implementation() -> None:
    config = project_config_from_dict(
        {
            "name": "Feature delivery",
            "project_type": "feature_development",
            "task_sources": [{"kind": "manual_prd", "ref": "docs/feature.md"}],
            "repositories": [
                {
                    "id": "app",
                    "role": "code",
                    "provider": "github",
                    "repo_full_name": "owner/app",
                    "clone_url": "https://github.com/owner/app.git",
                },
                {"id": "tests", "role": "tests", "path": "tests", "writable": True},
            ],
        }
    )

    payload = build_plan_payload(config)

    assert any(task["id"].endswith("DECOMPOSE-FEATURE") for task in payload["tasks"])
    assert any(task["id"].endswith("IMPLEMENT-FEATURE") for task in payload["tasks"])
    assert any(task["id"].endswith("GENERATE-TESTS") for task in payload["tasks"])


@pytest.mark.parametrize(
    "project_type",
    ["python_backend", "etl_pipeline", "documentation", "graphql_api", "generic"],
)
def test_public_target_project_types_are_supported(project_type: str) -> None:
    config = project_config_from_dict({"name": f"{project_type} profile", "project_type": project_type})

    assert config.project_type == project_type
    assert config.pipeline


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("etl", "etl_pipeline"), ("feature_development", "python_backend")],
)
def test_legacy_project_type_aliases_are_canonicalized(alias: str, canonical: str) -> None:
    config = project_config_from_dict({"name": "Legacy profile", "project_type": alias})

    assert config.project_type == canonical
    assert build_plan_payload(config)["origin"]["decomposition_mode"] == f"configured:{canonical}"


@pytest.mark.parametrize(
    ("field", "payload", "message"),
    [
        ("task_sources", {"task_sources": [{"kind": "spreadsheet"}]}, "unsupported task source kind"),
        ("sinks", {"sinks": [{"type": "email"}]}, "unsupported sink type"),
        ("default_runner", {"default_runner": "unknown_runner"}, "unsupported default_runner"),
    ],
)
def test_project_config_rejects_unknown_source_sink_and_runner_names(
    field: str,
    payload: dict,
    message: str,
) -> None:
    data = {"name": f"Bad {field}", "project_type": "generic", **payload}

    with pytest.raises(ProjectConfigError, match=message):
        project_config_from_dict(data)


def test_project_config_rejects_required_human_review_stage_that_is_not_in_pipeline() -> None:
    with pytest.raises(ProjectConfigError, match="required human_loop step .* is not in pipeline"):
        project_config_from_dict(
            {
                "name": "Missing required stage",
                "project_type": "generic",
                "human_loop": {"required_steps": ["publish"]},
            }
        )


def test_project_config_rejects_disabled_human_loop_with_required_steps() -> None:
    with pytest.raises(ProjectConfigError, match="human_loop.enabled is false"):
        project_config_from_dict(
            {
                "name": "Contradictory review",
                "project_type": "generic",
                "human_loop": {"enabled": False, "required_steps": ["verify"]},
            }
        )


def test_project_config_rejects_unsafe_verification_commands() -> None:
    with pytest.raises(ProjectConfigError, match="unsafe verification command"):
        project_config_from_dict(
            {
                "name": "Unsafe verification",
                "project_type": "generic",
                "verification": {
                    "commands": [
                        {"name": "cleanup", "command": "rm -rf /", "required": True},
                    ]
                },
            }
        )


def test_project_config_accepts_target_profile_shape() -> None:
    config = project_config_from_dict(
        {
            "project": {
                "name": "Target profile",
                "type": "python_backend",
                "default_runner": "opencode",
            },
            "sources": [{"type": "github_issues", "ref": "owner/repo#1"}],
            "pipeline": {
                "stages": [
                    {"id": "intake", "type": "intake", "title": "Intake"},
                    {"id": "execute", "type": "execute", "title": "Execute", "depends_on": ["intake"]},
                    {"id": "verify", "type": "verify", "title": "Verify", "depends_on": ["execute"]},
                ]
            },
            "verification": {"commands": [{"name": "unit", "command": "pytest -q tests/unit", "required": True}]},
            "sinks": [{"type": "jsonl", "config": {"path": "out/events.jsonl"}}],
            "human_loop": {"required_steps": ["verify"]},
        }
    )

    assert config.name == "Target profile"
    assert config.default_runner == "opencode"
    assert config.task_sources[0].kind == "github_issues"
    assert config.pipeline[2].human_gate is True
    assert config.verification_commands[0].command == "pytest -q tests/unit"
    assert config.sinks[0].type == "jsonl"


def test_explicit_pipeline_overrides_preset_and_validates_repo_roles() -> None:
    with pytest.raises(ProjectConfigError, match="unknown repo_role"):
        project_config_from_dict(
            {
                "name": "Bad config",
                "project_type": "generic",
                "pipeline": [{"id": "x", "kind": "development", "title": "X", "repo_role": "missing"}],
            }
        )


def test_project_config_cli_generates_plan_file(tmp_path: Path) -> None:
    config_path = tmp_path / "project.json"
    out_path = tmp_path / "plan.json"
    config_path.write_text(json.dumps(_etl_config()), encoding="utf-8")

    code = main(["project-config", "plan", str(config_path), "--plan-id", "P-ETL", "--out", str(out_path)])

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["plan_id"] == "P-ETL"
    assert payload["origin"]["system"] == "project_config"


def test_load_project_config_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    path.write_text(
        """
name = "GraphQL API"
project_type = "graphql_api"

[[task_sources]]
kind = "jira"
ref = "API-1"

[[repositories]]
id = "api"
role = "code"
provider = "github"
repo_full_name = "owner/api"
clone_url = "https://github.com/owner/api.git"

[[repositories]]
id = "api-tests"
role = "tests"
path = "tests/graphql"
writable = true
""",
        encoding="utf-8",
    )

    config = load_project_config(path)

    assert config.project_type == "graphql_api"
    assert config.pipeline[0].id == "collect-api-requirements"
