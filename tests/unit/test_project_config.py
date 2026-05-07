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
