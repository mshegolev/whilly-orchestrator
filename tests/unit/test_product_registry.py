"""Inert policy parsing, complete snapshots and approval mutation fencing."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from whilly.swarm.product_registry import ProductRegistryError, load_product_registry
from whilly.swarm.registry import load_registry, registry_from_dict


def policy(index=1):
    return {
        "canonical_remote": f"https://gitlab.example.com/demo/repo-{index}.git",
        "gitlab_project_id": index,
        "target_branch": "master",
        "target_protected": True,
        "branch_prefix": "whilly/",
        "checks": {
            "fast": [["python", "-m", "pytest", "-q", "tests/unit"]],
            "full": [["bash", "bin/preflight.sh"]],
            "ci": ["test", "build"],
        },
        "allowed_paths": ["src/**", "tests/**"],
        "worktree_owner": "coordinator",
        "contracts": ["demo-api-v1"],
        "artifacts": [{"name": "package", "kind": "package", "digest": "sha256"}],
        "delivery": {
            "stage": {"mode": "pipeline", "job": "deploy-stage", "observations": ["smoke-stage"]},
            "prod": {
                "mode": "pipeline",
                "job": "deploy-prod",
                "observations": ["smoke-prod"],
                "requires_approval": True,
            },
        },
        "compensation": {
            "strategy": "revert_mr",
            "checks": [["bash", "bin/preflight.sh"]],
            "stage_rollback_job": "rollback-stage",
        },
        "manual_decisions": ["external_effect", "secrets", "network_policy", "kept_storage", "prod_release"],
    }


def registry(count=13):
    projects = {
        f"repo-{index}": {
            "path": f"../repo-{index}",
            "base_ref": "master",
            "purpose": "Synthetic component",
            "depends_on": [f"repo-{index - 1}"] if index > 1 else [],
            "product_policy": policy(index),
        }
        for index in range(1, count + 1)
    }
    return {
        "version": 1,
        "name": "Synthetic product",
        "projects": projects,
        "roles": {"implementer": {"purpose": "Implement", "projects": list(projects)}},
    }


def write(tmp_path, data):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_all_thirteen_policies_are_retained_without_git_or_network(tmp_path, monkeypatch):
    import whilly.swarm.registry as legacy

    monkeypatch.setattr(legacy, "_git", lambda *args: pytest.fail("policy parsing must not launch Git"))
    path = write(tmp_path, registry())
    snapshot = load_product_registry(path)
    assert len(snapshot.projects) == 13
    first = snapshot.projects["repo-1"]
    assert first.gitlab_project_id == 1
    assert first.canonical_remote == "https://gitlab.example.com/demo/repo-1.git"
    assert first.target_branch == "master"
    assert first.branch_prefix == "whilly/"
    assert first.depends_on == ()
    assert snapshot.projects["repo-13"].depends_on == ("repo-12",)
    assert first.to_dict()["delivery"]["prod"]["requires_approval"] is True
    assert first.to_dict()["compensation"]["strategy"] == "revert_mr"
    assert first.to_dict()["checks"]["ci"] == ["test", "build"]
    assert snapshot.policy_digest == hashlib.sha256(snapshot.canonical_policy_bytes).hexdigest()
    assert snapshot.registry_digest == hashlib.sha256(snapshot.canonical_registry_bytes).hexdigest()
    assert snapshot.digest == snapshot.registry_digest


def test_snapshot_roundtrip_retains_policy_and_profiles(tmp_path):
    data = registry(2)
    data["profiles"] = {
        "worker-cheap": {
            "engine": "claude",
            "model": "synthetic-model",
            "reasoning_effort": "low",
            "max_turns": 5,
            "timeout_seconds": 60,
            "budget_usd": 1,
        }
    }
    path = write(tmp_path, data)
    legacy = load_registry(path, check_git=False)
    restored = registry_from_dict(
        {k: v for k, v in legacy.to_snapshot().items() if k != "_source_path"}, check_git=False
    )
    assert restored.projects["repo-1"].to_dict()["product_policy"] == policy()
    assert restored.profiles["worker-cheap"].model == "synthetic-model"
    snapshot = load_product_registry(path)
    dumped = snapshot.to_dict()
    dumped["projects"]["repo-1"]["product_policy"]["target_branch"] = "other"
    assert snapshot.projects["repo-1"].target_branch == "master"
    with pytest.raises(TypeError):
        snapshot.projects["foreign"] = snapshot.projects["repo-1"]
    with pytest.raises(FrozenInstanceError):
        snapshot.projects["repo-1"].target_branch = "other"


def test_legacy_valid_without_product_policy_but_product_request_fails(tmp_path):
    data = registry(2)
    for item in data["projects"].values():
        del item["product_policy"]
    path = write(tmp_path, data)
    assert len(load_registry(path, check_git=False).projects) == 2
    with pytest.raises(ProductRegistryError, match="product_policy_required"):
        load_product_registry(path)


@pytest.mark.parametrize("field", list(policy()))
def test_missing_policy_field_blocks_entire_registry(tmp_path, field):
    data = registry(2)
    del data["projects"]["repo-2"]["product_policy"][field]
    with pytest.raises(ProductRegistryError):
        load_product_registry(write(tmp_path, data))


@pytest.mark.parametrize(
    "field,value",
    [
        ("canonical_remote", ""),
        ("canonical_remote", "http://gitlab.example.com/group/repo.git"),
        ("canonical_remote", "https://user:password@gitlab.example.com/group/repo.git"),
        ("canonical_remote", "file:///repo.git"),
        ("canonical_remote", "ext::helper"),
        ("gitlab_project_id", True),
        ("gitlab_project_id", "1"),
        ("gitlab_project_id", -1),
        ("target_branch", "-master"),
        ("target_branch", "master..other"),
        ("target_branch", "refs/heads/master"),
        ("target_protected", False),
        ("branch_prefix", "master"),
        ("branch_prefix", "../"),
        ("checks", {"fast": [], "full": [], "ci": []}),
        ("checks", {"fast": [["true"]], "full": ["bash gate.sh"], "ci": ["test"]}),
        ("artifacts", [{"name": "image", "kind": "image", "digest": "latest"}]),
        ("delivery", {"stage": {"mode": "pipeline", "job": "deploy", "observations": []}}),
        ("compensation", {"strategy": "reset", "checks": [["true"]], "stage_rollback_job": "rollback"}),
        ("allowed_paths", ["../secret"]),
        ("allowed_paths", ["/absolute"]),
        ("worktree_owner", ""),
        ("manual_decisions", []),
    ],
)
def test_invalid_policy_never_silently_drops_a_project(tmp_path, field, value):
    data = registry(13)
    data["projects"]["repo-13"]["product_policy"][field] = value
    with pytest.raises(ProductRegistryError):
        load_product_registry(write(tmp_path, data))


@pytest.mark.parametrize(
    "defect", ["cycle", "unknown_dependency", "duplicate_project_id", "duplicate_remote", "unknown_key"]
)
def test_registry_graph_and_identity_defects_are_blocked(tmp_path, defect):
    data = registry(2)
    if defect == "cycle":
        data["projects"]["repo-1"]["depends_on"] = ["repo-2"]
    elif defect == "unknown_dependency":
        data["projects"]["repo-1"]["depends_on"] = ["absent"]
    elif defect == "unknown_key":
        data["projects"]["repo-1"]["product_policy"]["skip_hooks"] = True
    else:
        field = "gitlab_project_id" if defect == "duplicate_project_id" else "canonical_remote"
        data["projects"]["repo-2"]["product_policy"][field] = data["projects"]["repo-1"]["product_policy"][field]
    with pytest.raises(ProductRegistryError):
        load_product_registry(write(tmp_path, data))


@pytest.mark.parametrize(
    "literal",
    [
        "glpat-" + "a" * 24,
        "ghp_" + "b" * 36,
        "sk-" + "c" * 40,
        "-----BEGIN PRIVATE KEY-----",
        "Bearer syntheticcredential",
    ],
)
def test_secret_literals_are_rejected_without_echo(tmp_path, literal):
    data = registry(2)
    data["overview"] = literal
    with pytest.raises(ProductRegistryError) as error:
        load_product_registry(write(tmp_path, data))
    assert error.value.error_code == "registry_secret_literal"
    assert literal not in str(error.value)


def test_duplicate_json_keys_cannot_hide_policy_or_projects(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text('{"projects":{},"projects":{}}', encoding="utf-8")
    with pytest.raises(ProductRegistryError, match="registry_duplicate_key"):
        load_product_registry(path)


@pytest.mark.parametrize(
    "mutation", ["remote", "target", "checks", "dependencies", "artifact", "delivery", "compensation", "profile"]
)
def test_policy_mutation_invalidates_snapshot_and_approval(tmp_path, mutation):
    data = registry(2)
    path = write(tmp_path, data)
    original = load_product_registry(path)
    binding = {"plan": {"tasks": ["repo-1"]}, "revision": 1, "base_shas": {"repo-1": "a" * 40}}
    approval = original.approval_digest(binding)
    if mutation == "profile":
        data["agent"] = {"model": "changed-model"}
    elif mutation == "dependencies":
        data["projects"]["repo-2"]["depends_on"] = []
    else:
        p = data["projects"]["repo-1"]["product_policy"]
        if mutation == "remote":
            p["canonical_remote"] = "https://gitlab.example.com/demo/changed.git"
        elif mutation == "target":
            p["target_branch"] = "main"
        elif mutation == "checks":
            p["checks"]["full"] = [["bash", "bin/another-gate.sh"]]
        elif mutation == "artifact":
            p["artifacts"][0]["name"] = "changed-package"
        elif mutation == "delivery":
            p["delivery"]["stage"]["job"] = "changed-job"
        elif mutation == "compensation":
            p["compensation"]["stage_rollback_job"] = "changed-rollback"
    write(tmp_path, data)
    current = load_product_registry(path)
    assert current.digest != original.digest
    assert current.approval_digest(binding) != approval
    if mutation == "profile":
        assert current.policy_digest == original.policy_digest
    else:
        assert current.policy_digest != original.policy_digest
    with pytest.raises(ProductRegistryError, match="product_registry_changed"):
        original.assert_unchanged(path)


def test_canonical_key_order_and_whitespace_do_not_change_binding(tmp_path):
    data = registry(2)
    path = write(tmp_path, data)
    original = load_product_registry(path)
    path.write_text(json.dumps(copy.deepcopy(data), sort_keys=True, indent=4), encoding="utf-8")
    current = load_product_registry(path)
    assert current.digest == original.digest
    assert current.policy_digest == original.policy_digest
    original.assert_unchanged(path)
    assert current.approval_digest({"revision": 1}) != current.approval_digest({"revision": 2})


def test_demo_fixture_has_thirteen_complete_policy_projects():
    snapshot = load_product_registry(Path("examples/product-registry-demo.json"))
    assert len(snapshot.projects) == 13


@pytest.mark.parametrize(
    "field,value",
    [("branch_prefix", "master/"), ("allowed_paths", ["~/.ssh/**"]), ("allowed_paths", ["C:/outside/**"])],
)
def test_policy_cannot_collide_with_target_or_expand_foreign_roots(tmp_path, field, value):
    data = registry(2)
    data["projects"]["repo-1"]["product_policy"][field] = value
    with pytest.raises(ProductRegistryError):
        load_product_registry(write(tmp_path, data))


@pytest.mark.parametrize("binding", [{1: "value"}, {"nested": {True: "value"}}, {"bad": float("nan")}])
def test_approval_binding_rejects_ambiguous_or_nonfinite_json(tmp_path, binding):
    snapshot = load_product_registry(write(tmp_path, registry(2)))
    with pytest.raises(ProductRegistryError):
        snapshot.approval_digest(binding)


@pytest.mark.parametrize(
    "section,field,value", [("artifact", "kind", []), ("delivery", "mode", {}), ("artifact", "digest", False)]
)
def test_wrong_nested_types_have_named_policy_error(tmp_path, section, field, value):
    data = registry(2)
    p = data["projects"]["repo-1"]["product_policy"]
    if section == "artifact":
        p["artifacts"][0][field] = value
    else:
        p["delivery"]["stage"][field] = value
    with pytest.raises(ProductRegistryError):
        load_product_registry(write(tmp_path, data))


@pytest.mark.parametrize(
    "remote", ["git@gitlab.example.com:demo/repo-1.git", "ssh://git@gitlab.example.com:2222/demo/repo-1.git"]
)
def test_git_user_ssh_remotes_are_explicit_inert_declarations(tmp_path, remote):
    data = registry(2)
    data["projects"]["repo-1"]["product_policy"]["canonical_remote"] = remote
    assert load_product_registry(write(tmp_path, data)).projects["repo-1"].canonical_remote == remote


def test_tuple_sequences_cannot_hide_ambiguous_binding_keys(tmp_path):
    snapshot = load_product_registry(write(tmp_path, registry(2)))
    with pytest.raises(ProductRegistryError):
        snapshot.approval_digest({"nested": ({True: "value"},)})


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_product_registry_requires_integer_schema_version(tmp_path, version):
    data = registry(2)
    data["version"] = version
    with pytest.raises(ProductRegistryError):
        load_product_registry(write(tmp_path, data))


@pytest.mark.parametrize(
    "remote,expected",
    [
        ("https://gitlab.example.com:443/demo/repo-1.git", "https://gitlab.example.com/demo/repo-1.git"),
        ("ssh://git@gitlab.example.com:22/demo/repo-1.git", "ssh://git@gitlab.example.com/demo/repo-1.git"),
    ],
)
def test_explicit_default_port_has_same_canonical_binding(tmp_path, remote, expected):
    data = registry(2)
    data["projects"]["repo-1"]["product_policy"]["canonical_remote"] = expected
    path = write(tmp_path, data)
    before = load_product_registry(path)
    data["projects"]["repo-1"]["product_policy"]["canonical_remote"] = remote
    after = load_product_registry(write(tmp_path, data))
    assert after.projects["repo-1"].canonical_remote == expected
    assert after.policy_digest == before.policy_digest
    assert after.registry_digest == before.registry_digest
    assert after.approval_digest({"revision": 1}) == before.approval_digest({"revision": 1})


def test_default_https_port_cannot_hide_duplicate_repository_identity(tmp_path):
    data = registry(2)
    data["projects"]["repo-2"]["product_policy"]["canonical_remote"] = "https://gitlab.example.com:443/demo/repo-1.git"
    with pytest.raises(ProductRegistryError, match="product_project_identity_duplicate"):
        load_product_registry(write(tmp_path, data))


@pytest.mark.parametrize("host", ["%67itlab.example.com", "gitlab%2eexample.com"])
def test_percent_escaped_hostname_is_rejected_before_identity(tmp_path, host):
    data = registry(2)
    data["projects"]["repo-1"]["product_policy"]["canonical_remote"] = f"https://{host}/demo/repo-1.git"
    with pytest.raises(ProductRegistryError):
        load_product_registry(write(tmp_path, data))


def test_same_registry_json_binds_relative_state_dir_per_source_directory(tmp_path, monkeypatch):
    data = registry(2)
    for project_id, project in data["projects"].items():
        project["path"] = str(tmp_path / "shared" / project_id)
    data["state_dir"] = "state"
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    # A runtime override cannot replace the declared policy in its snapshot.
    monkeypatch.setenv("WHILLY_SWARM_STATE_DIR", str(tmp_path / "override"))
    first = load_product_registry(write(first_root, data))
    second = load_product_registry(write(second_root, data))
    assert first.registry_digest != second.registry_digest
    assert first.to_dict()["state_dir"] == str(first_root / "state")
    assert second.to_dict()["state_dir"] == str(second_root / "state")
    assert first.policy_digest == second.policy_digest
    assert first.approval_digest({"revision": 1}) != second.approval_digest({"revision": 1})
    legacy = registry_from_dict(first.to_dict(), check_git=False)
    assert legacy.to_snapshot()["state_dir"] == str(first_root / "state")
    restored = load_product_registry(write(second_root, first.to_dict()))
    assert restored.registry_digest == first.registry_digest


@pytest.mark.parametrize("relative", [False, True])
def test_state_dir_symlink_retarget_invalidates_same_json_snapshot(tmp_path, relative):
    first, second, link = tmp_path / "first-state", tmp_path / "second-state", tmp_path / "state-link"
    first.mkdir()
    second.mkdir()
    link.symlink_to(first, target_is_directory=True)
    data = registry(2)
    data["state_dir"] = link.name if relative else str(link)
    path = write(tmp_path, data)
    original_json = path.read_bytes()
    before = load_product_registry(path)
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    assert path.read_bytes() == original_json
    after = load_product_registry(path)
    assert before.registry_digest != after.registry_digest
    assert before.to_dict()["state_dir"] == str(first)
    assert after.to_dict()["state_dir"] == str(second)
    assert before.policy_digest == after.policy_digest
    assert before.approval_digest({"revision": 1}) != after.approval_digest({"revision": 1})
    with pytest.raises(ProductRegistryError, match="product_registry_changed"):
        before.assert_unchanged(path)


def test_absolute_state_dir_resolution_failure_is_named(tmp_path):
    link = tmp_path / "state-loop"
    link.symlink_to(link)
    data = registry(2)
    data["state_dir"] = str(link)
    with pytest.raises(ProductRegistryError, match="product_registry_invalid"):
        load_product_registry(write(tmp_path, data))


@pytest.mark.parametrize(
    "shell,flag",
    [
        ("bash", "-lc"),
        ("sh", "-ec"),
        ("bash", "-cl"),
        ("BASH", "-xec"),
        ("/bin/bash", "-lc"),
        ("powershell", "-command"),
        ("powershell", "-COMMAND"),
        ("pwsh", "-Command"),
        ("powershell.exe", "-cOmMaNd"),
        ("pwsh.exe", "-EncodedCommand"),
        ("powershell", "-ec"),
        ("pwsh", "-co"),
    ],
)
@pytest.mark.parametrize("section", ["fast", "full", "compensation"])
def test_combined_or_case_variant_inline_shell_is_rejected_everywhere(tmp_path, shell, flag, section):
    data = registry(2)
    p = data["projects"]["repo-1"]["product_policy"]
    commands = p["compensation"] if section == "compensation" else p["checks"]
    commands["checks" if section == "compensation" else section] = [[shell, flag, "synthetic-code"]]
    with pytest.raises(ProductRegistryError):
        load_product_registry(write(tmp_path, data))


@pytest.mark.parametrize(
    "command",
    [["bash", "bin/preflight.sh", "--check"], ["sh", "-e", "bin/preflight.sh"], ["pwsh", "-File", "scripts/check.ps1"]],
)
def test_script_file_commands_still_pass_inline_shell_guard(tmp_path, command):
    data = registry(2)
    data["projects"]["repo-1"]["product_policy"]["checks"]["full"] = [command]
    assert load_product_registry(write(tmp_path, data)).projects["repo-1"].to_dict()["checks"]["full"] == [command]
