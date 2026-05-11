"""Tests for QA release Jira linked-artifact collection."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from whilly import config as cfg_mod
from whilly.qa_release.collector import _repo_hint_from_link, collect_release_context
from whilly.qa_release.autotest_writer import write_autotest_suite
from whilly.qa_release.models import (
    GitRepoHint,
    LinkedIssue,
    ReleaseContext,
    ReleaseLink,
    release_context_from_dict,
)
from whilly.qa_release.test_plan import build_test_plan


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _adf(text: str) -> dict[str, Any]:
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _issue(
    key: str,
    *,
    summary: str,
    description: str = "",
    links: list[dict[str, Any]] | None = None,
    status: str = "Ready",
    issue_type: str = "Story",
) -> dict[str, Any]:
    return {
        "key": key,
        "self": f"https://jira.example.test/rest/api/3/issue/{key}",
        "fields": {
            "summary": summary,
            "description": _adf(description),
            "issuelinks": links or [],
            "status": {"name": status},
            "issuetype": {"name": issue_type},
        },
    }


@pytest.fixture
def jira_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_SERVER_URL", "https://jira.example.test")
    monkeypatch.setenv("JIRA_USERNAME", "qa@example.test")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setattr(cfg_mod, "_toml_sections_cache", {"jira": {}})


def test_collect_release_context_fetches_linked_issues_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    jira_env: None,
) -> None:
    from whilly.sources import jira as jira_mod

    root_links = [
        {
            "type": {"outward": "verifies"},
            "outwardIssue": {"key": "REQ-100"},
        },
        {
            "type": {"inward": "is deployed by"},
            "inwardIssue": {"key": "OPS-200"},
        },
    ]
    issues = {
        "REL-1234": _issue(
            "REL-1234",
            summary="QA Verify SALES_ETL v.20260507",
            description="Release notes https://wiki.example.test/display/ETL/SALES_ETL+Release",
            links=root_links,
            issue_type="Release",
        ),
        "REQ-100": _issue(
            "REQ-100",
            summary="Business rule: churn date",
            description="Requirement source",
            status="In Progress",
        ),
        "OPS-200": _issue(
            "OPS-200",
            summary="Deploy SALES_ETL to STAGE",
            description="Use deploy repo",
            issue_type="Task",
        ),
    }
    remote_links = {
        "REL-1234": [
            {
                "relationship": "documented by",
                "object": {
                    "title": "Confluence requirements",
                    "url": "https://wiki.example.test/display/ETL/Business+Requirements",
                },
            },
            {
                "relationship": "implemented by",
                "object": {
                    "title": "GitLab release tag",
                    "url": "https://gitlab.example.test/example/etl/etl-main/-/tags/20260507",
                },
            },
        ],
        "REQ-100": [],
        "OPS-200": [
            {
                "relationship": "deploy instructions",
                "object": {
                    "title": "STAGE deployment instructions",
                    "url": "https://gitlab.example.test/example/etl/deploy/-/tree/release-20260507",
                },
            }
        ],
    }

    def fake_urlopen(req: Any, timeout: int | None = None) -> _FakeResponse:
        url = req.full_url
        if "/remotelink" in url:
            key = url.split("/issue/", 1)[1].split("/remotelink", 1)[0]
            return _FakeResponse(json.dumps(remote_links[key]).encode("utf-8"))
        key = url.split("/issue/", 1)[1].split("?", 1)[0]
        return _FakeResponse(json.dumps(issues[key]).encode("utf-8"))

    monkeypatch.setattr(jira_mod, "urlopen", fake_urlopen)

    context = collect_release_context("REL-1234")
    data = context.to_dict()

    assert data["root_key"] == "REL-1234"
    assert [issue["key"] for issue in data["linked_issues"]] == ["REL-1234", "REQ-100", "OPS-200"]
    assert {link["kind"] for link in data["links"]} >= {"confluence", "gitlab"}
    assert {
        (hint["provider"], hint["repo_full_name"], hint["ref_type"], hint["ref"]) for hint in data["repo_hints"]
    } == {
        ("gitlab", "example/etl/etl-main", "tag", "20260507"),
        ("gitlab", "example/etl/deploy", "branch", "release-20260507"),
    }


def test_collect_release_context_warns_when_remote_links_fail(
    monkeypatch: pytest.MonkeyPatch,
    jira_env: None,
) -> None:
    from urllib.error import HTTPError

    from whilly.sources import jira as jira_mod

    def fake_urlopen(req: Any, timeout: int | None = None) -> _FakeResponse:
        if "/remotelink" in req.full_url:
            raise HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=io.BytesIO(b"no access"))
        return _FakeResponse(
            json.dumps(
                _issue("REL-1", summary="release", description="https://gitlab.example.test/a/b/-/commit/abc")
            ).encode("utf-8")
        )

    monkeypatch.setattr(jira_mod, "urlopen", fake_urlopen)

    context = collect_release_context("REL-1", depth=0)

    assert context.warnings
    assert context.repo_hints[0].repo_full_name == "a/b"
    assert context.repo_hints[0].ref_type == "commit"


def test_repo_hint_detects_self_hosted_gitlab_links() -> None:
    link = ReleaseLink(
        url="https://git.example.test/group/subgroup/repo/-/tree/feature/release-123",
        title="Release branch",
        kind="other",
        source_issue_key="REL-1",
    )

    hint = _repo_hint_from_link(link)

    assert hint is not None
    assert hint.provider == "gitlab"
    assert hint.repo_full_name == "group/subgroup/repo"
    assert hint.ref_type == "branch"
    assert hint.ref == "feature/release-123"


def test_qa_release_collect_cli_writes_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from whilly.cli import qa_release as cli_mod
    from whilly.qa_release.models import ReleaseContext

    def fake_collect_release_context(jira_ref: str, *, depth: int, timeout: int) -> ReleaseContext:
        assert jira_ref == "REL-1234"
        assert depth == 1
        assert timeout == 7
        return ReleaseContext(
            root_key="REL-1234",
            root_summary="QA Verify",
            root_url="https://jira.example.test/browse/REL-1234",
            linked_issues=(),
            links=(),
            repo_hints=(),
        )

    monkeypatch.setattr(cli_mod, "collect_release_context", fake_collect_release_context)
    out = tmp_path / "release-context.json"

    code = cli_mod.run_qa_release_command(["collect", "REL-1234", "--timeout", "7", "--out", str(out)])

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["root_key"] == "REL-1234"


def _release_context() -> ReleaseContext:
    return ReleaseContext(
        root_key="REL-1234",
        root_summary="QA Verify SALES_ETL v.20260507",
        root_url="https://jira.example.test/browse/REL-1234",
        linked_issues=(
            LinkedIssue(
                key="REL-1234",
                summary="QA Verify SALES_ETL v.20260507",
                url="https://jira.example.test/browse/REL-1234",
                issue_type="Release",
                relation="root",
            ),
            LinkedIssue(
                key="REQ-100",
                summary="Business rule: churn date",
                url="https://jira.example.test/browse/REQ-100",
                description="Churn date output must match AE-824 business rule.",
                status="In Progress",
                relation="verifies",
            ),
            LinkedIssue(
                key="OPS-200",
                summary="Deploy SALES_ETL to STAGE",
                url="https://jira.example.test/browse/OPS-200",
                description="Deploy release to STAGE before QA.",
                issue_type="Task",
                relation="is deployed by",
            ),
        ),
        links=(
            ReleaseLink(
                url="https://wiki.example.test/display/ETL/Business+Requirements",
                title="Confluence requirements",
                kind="confluence",
                source_issue_key="REL-1234",
            ),
            ReleaseLink(
                url="https://gitlab.example.test/example/etl/deploy/-/tree/release-20260507",
                title="STAGE deployment instructions",
                kind="deployment",
                source_issue_key="OPS-200",
            ),
        ),
        repo_hints=(
            GitRepoHint(
                provider="gitlab",
                repo_full_name="example/etl/etl-main",
                url="https://gitlab.example.test/example/etl/etl-main/-/tags/20260507",
                clone_url="https://gitlab.example.test/example/etl/etl-main.git",
                ref="20260507",
                ref_type="tag",
                source_issue_key="REL-1234",
            ),
        ),
        generated_at="2026-05-07T00:00:00+00:00",
    )


def test_build_test_plan_covers_linked_requirements_and_release_scope() -> None:
    plan = build_test_plan(_release_context())
    data = plan.to_dict()

    assert data["release_key"] == "REL-1234"
    assert data["release_version"] == "20260507"
    assert [requirement["source_issue_key"] for requirement in data["requirements"]] == ["REQ-100", "OPS-200"]
    assert {"functional", "contract", "deployment", "regression"} <= {
        test_case["kind"] for test_case in data["test_cases"]
    }
    covered = {
        requirement_id for test_case in data["test_cases"] for requirement_id in test_case["source_requirement_ids"]
    }
    assert {requirement["id"] for requirement in data["requirements"]} <= covered


def test_release_context_round_trips_from_json_dict() -> None:
    context = _release_context()

    decoded = release_context_from_dict(context.to_dict())

    assert decoded == context


def test_write_autotest_suite_creates_generated_pytest_file(tmp_path: Path) -> None:
    plan = build_test_plan(_release_context())

    target = write_autotest_suite(plan, repo_root=tmp_path, suite="SALES_ETL")

    assert target == tmp_path / "bigdata_tests" / "SALES_ETL" / "tests" / "test_rel_1234_qa_release_plan.py"
    text = target.read_text(encoding="utf-8")
    assert "Generated by Whilly QA release" in text
    assert "QA_TEST_PLAN" in text
    assert "test_release_plan_has_requirement_coverage" in text


def test_write_autotest_suite_refuses_to_overwrite_manual_file(tmp_path: Path) -> None:
    plan = build_test_plan(_release_context())
    manual = tmp_path / "bigdata_tests" / "SALES_ETL" / "tests" / "test_rel_1234_qa_release_plan.py"
    manual.parent.mkdir(parents=True)
    manual.write_text("# manual test\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        write_autotest_suite(plan, repo_root=tmp_path, suite="SALES_ETL")


def test_qa_release_plan_and_scaffold_cli_write_files(
    tmp_path: Path,
) -> None:
    from whilly.cli import qa_release as cli_mod

    context_path = tmp_path / "release-context.json"
    plan_path = tmp_path / "test-plan.json"
    repo_root = tmp_path / "test-monorepo"
    context_path.write_text(json.dumps(_release_context().to_dict()), encoding="utf-8")

    plan_code = cli_mod.run_qa_release_command(["plan", str(context_path), "--out", str(plan_path)])
    scaffold_code = cli_mod.run_qa_release_command(
        ["scaffold-tests", str(plan_path), "--repo", str(repo_root), "--suite", "SALES_ETL"]
    )

    assert plan_code == 0
    assert scaffold_code == 0
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan_payload["release_key"] == "REL-1234"
    assert (repo_root / "bigdata_tests" / "SALES_ETL" / "tests" / "test_rel_1234_qa_release_plan.py").is_file()


def test_main_dispatches_qa_release(monkeypatch: pytest.MonkeyPatch) -> None:
    from whilly.cli import main
    from whilly.cli import qa_release as cli_mod

    captured: dict[str, list[str]] = {}

    def fake_run_qa_release_command(argv: list[str]) -> int:
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(cli_mod, "run_qa_release_command", fake_run_qa_release_command)

    code = main(["qa-release", "collect", "REL-1234"])

    assert code == 0
    assert captured["argv"] == ["collect", "REL-1234"]
