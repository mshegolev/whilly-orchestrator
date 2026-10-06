"""Unit tests for the swarm registry, plan contract and result parsers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.swarm_helpers import make_bare_repo, make_repo, write_registry
from whilly.swarm.plan import PlanError, extract_plan_json, validate_plan
from whilly.swarm.prompts import ResultError, parse_review, parse_worker_result
from whilly.swarm.registry import RegistryError, load_registry


MANDATORY_POLICY = {
    "test": [["python", "-m", "pytest", "--junitxml", "reports/test.xml"]],
    "lint": [["ruff", "check", "--output-format", "json", "."]],
    "architecture": [["python", "architecture.py", "--json"]],
    "protected_paths": [],
    "toolchain_id": "test-toolchain",
}


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    lib = make_repo(tmp_path, "demo-lib")
    make_bare_repo(tmp_path, "demo-api")
    return write_registry(
        tmp_path / "registry.json",
        {
            "demo-lib": {"path": str(lib), "purpose": "Library", "verification": [["true"]]},
            "demo-api": {"path": "demo-api.git", "purpose": "API", "depends_on": ["demo-lib"], "base_ref": "main"},
        },
        roles={
            "implementer": {"purpose": "Writes code", "projects": ["demo-lib", "demo-api"]},
            "evaluator": {"purpose": "Evaluates", "projects": ["demo-api"]},
        },
    )


def test_valid_registry_with_bare_repo_and_relative_path(registry_path: Path) -> None:
    registry = load_registry(registry_path)
    assert registry.projects["demo-api"].bare is True
    assert registry.projects["demo-lib"].bare is False
    assert Path(registry.projects["demo-api"].path).is_absolute()
    assert registry.limits.max_parallel == 2


def test_registry_accepts_bare_store_inside_project_root(tmp_path: Path) -> None:
    source = make_repo(tmp_path / "source", "demo")
    project_root = tmp_path / "demo-root"
    project_root.mkdir()
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(project_root / ".git")],
        check=True,
        capture_output=True,
    )
    path = write_registry(
        tmp_path / "registry.json",
        {"demo": {"path": str(project_root), "purpose": "Bare project with sibling policy files"}},
    )

    registry = load_registry(path)

    assert registry.projects["demo"].bare is True
    assert registry.projects["demo"].path == str(project_root.resolve())


def test_registry_round_trips_mandatory_policy_and_rejects_shell_commands(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "demo")
    path = write_registry(
        tmp_path / "registry.json",
        {"demo": {"path": str(repo), "purpose": "Demo", "verification_policy": MANDATORY_POLICY}},
    )
    registry = load_registry(path)
    project = registry.projects["demo"]
    assert project.verification_policy is not None
    snapshot_policy = registry.to_snapshot()["projects"]["demo"]["verification_policy"]
    assert snapshot_policy["test"] == MANDATORY_POLICY["test"]
    assert ".gitlab-ci.yml" in snapshot_policy["protected_paths"]
    _mutate(path, lambda data: data["projects"]["demo"]["verification_policy"].update(test=["pytest -q"]))
    with pytest.raises(RegistryError, match="argv_invalid"):
        load_registry(path)


def _mutate(path: Path, fn) -> None:
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda d: d["projects"]["demo-lib"].update(depends_on=["ghost"]), "unknown project 'ghost'"),
        (lambda d: d["projects"]["demo-lib"].update(depends_on=["demo-api"]), "dependency cycle"),
        (lambda d: d["projects"]["demo-lib"].update(path="/nonexistent/swarm/path"), "does not exist"),
        (lambda d: d["projects"]["demo-lib"].update(base_ref="no-such-branch"), "does not resolve"),
        (lambda d: d["projects"]["demo-lib"].update(context_files=["../secret"]), "without '..'"),
        (lambda d: d["projects"]["demo-lib"].update(verification=["pytest -q"]), "argv"),
        (lambda d: d["roles"]["implementer"].update(projects=["ghost"]), "unknown project 'ghost'"),
        (lambda d: d.update(limits={"max_parallel": 50}), "out of range"),
        (lambda d: d.update(limits={"heartbeat_seconds": 30, "lease_seconds": 60}), "3x"),
        (lambda d: d.update(surprise=True), "unknown top-level keys"),
        (lambda d: d["projects"].update({"Bad ID": d["projects"]["demo-lib"]}), "invalid project id"),
    ],
)
def test_registry_rejections(registry_path: Path, mutation, expected: str) -> None:
    _mutate(registry_path, mutation)
    with pytest.raises(RegistryError) as info:
        load_registry(registry_path)
    assert expected in str(info.value)


def test_registry_rejects_non_root_directory_inside_a_repo(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "outer")
    (repo / "sub").mkdir()
    path = write_registry(tmp_path / "r.json", {"inner": {"path": str(repo / "sub"), "purpose": "x"}})
    with pytest.raises(RegistryError, match="not the root"):
        load_registry(path)


def _plan(**overrides):
    task = {
        "id": "lib-change",
        "project": "demo-lib",
        "role": "implementer",
        "description": "Change the library",
        "depends_on": [],
        "verification": [],
    }
    task.update(overrides)
    return {"summary": "s", "tasks": [task]}


def test_valid_plan_and_topological_order(registry_path: Path) -> None:
    registry = load_registry(registry_path)
    data = {
        "summary": "two tasks",
        "tasks": [
            {
                "id": "api",
                "project": "demo-api",
                "role": "evaluator",
                "description": "d",
                "depends_on": ["lib"],
                "verification": [["true"]],
            },
            {"id": "lib", "project": "demo-lib", "role": "implementer", "description": "d"},
        ],
    }
    plan = validate_plan(data, registry)
    assert plan.topological_ids() == ["lib", "api"]


def test_policy_project_does_not_need_planner_supplied_duplicate_commands(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "demo")
    path = write_registry(
        tmp_path / "registry.json",
        {"demo": {"path": str(repo), "purpose": "Demo", "verification_policy": MANDATORY_POLICY}},
    )
    registry = load_registry(path)
    plan = validate_plan(
        {"summary": "s", "tasks": [{"id": "change", "project": "demo", "role": "implementer", "description": "d"}]},
        registry,
    )
    assert plan.tasks[0].verification == ()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"project": "ghost"}, "unknown project"),
        ({"role": "ghost"}, "unknown role"),
        ({"role": "evaluator"}, "not registered for project"),
        ({"depends_on": ["nope"]}, "unknown task"),
        ({"depends_on": ["lib-change"]}, "depends on itself"),
        ({"description": " "}, "description"),
        ({"project": "demo-api", "role": "evaluator", "verification": []}, "no verification"),
        ({"id": "Bad Id"}, "is invalid"),
        ({"extra": 1}, "unknown keys"),
    ],
)
def test_plan_rejections(registry_path: Path, overrides, expected: str) -> None:
    registry = load_registry(registry_path)
    with pytest.raises(PlanError) as info:
        validate_plan(_plan(**overrides), registry)
    assert expected in str(info.value)


def test_plan_cycle_and_duplicates(registry_path: Path) -> None:
    registry = load_registry(registry_path)
    base = {"project": "demo-lib", "role": "implementer", "description": "d"}
    with pytest.raises(PlanError, match="cycle"):
        validate_plan(
            {"tasks": [{"id": "a", "depends_on": ["b"], **base}, {"id": "b", "depends_on": ["a"], **base}]}, registry
        )
    with pytest.raises(PlanError, match="duplicate"):
        validate_plan({"tasks": [{"id": "a", **base}, {"id": "a", **base}]}, registry)


def test_plan_task_limit(registry_path: Path) -> None:
    _mutate(registry_path, lambda d: d.update(limits={"max_tasks": 1}))
    registry = load_registry(registry_path)
    base = {"project": "demo-lib", "role": "implementer", "description": "d"}
    with pytest.raises(PlanError, match="too many tasks"):
        validate_plan({"tasks": [{"id": "a", **base}, {"id": "b", **base}]}, registry)


def test_extract_plan_json() -> None:
    assert extract_plan_json("just talk") is None
    assert extract_plan_json('text\n```json\n{"tasks": []}\n```\n') == {"tasks": []}
    with pytest.raises(PlanError, match="not valid JSON"):
        extract_plan_json('```json\n{"tasks": [,]}\n```')


def test_worker_result_and_review_parsing() -> None:
    ok = parse_worker_result('x\n```json\n{"status": "done", "summary": "s", "touched_files": ["a"]}\n```')
    assert ok["status"] == "done"
    for bad in ("no json", '```json\n{"status": "maybe", "summary": "s"}\n```', '```json\n{"status": "done"}\n```'):
        with pytest.raises(ResultError):
            parse_worker_result(bad)
    assert parse_review('```json\n{"verdict": "approve"}\n```')["verdict"] == "approve"
    with pytest.raises(ResultError):
        parse_review('```json\n{"verdict": "lgtm"}\n```')
