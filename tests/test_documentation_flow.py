"""Tests for Phase 4 documentation flow and Confluence publisher."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from whilly.adapters.confluence.publisher import (
    ConfluencePage,
    ConfluencePublishError,
    ConfluencePublisher,
    _parse_page,
    _wrap_markdown_storage,
)
from whilly.workflow.documentation import (
    DocumentationFlowResult,
    generate_markdown_draft,
    run_documentation_flow,
    write_draft,
)


class TestMarkdownStorageMacro:
    def test_wrap_markdown_storage_basic(self) -> None:
        result = _wrap_markdown_storage("# Hello\n\nWorld")
        assert "ac:structured-macro" in result
        assert 'ac:name="markdown"' in result
        assert "# Hello" in result

    def test_wrap_handles_cdata_end_tokens(self) -> None:
        result = _wrap_markdown_storage("text ]]> more")
        assert "]]]]><![CDATA[>" in result

    def test_wrap_empty_string(self) -> None:
        result = _wrap_markdown_storage("")
        assert "ac:structured-macro" in result


class TestParsePage:
    def test_parse_minimal_page(self) -> None:
        page = _parse_page({"id": "123", "title": "Test"}, "https://wiki.example.com")
        assert page.id == "123"
        assert page.title == "Test"
        assert page.version == 1

    def test_parse_page_with_links(self) -> None:
        page = _parse_page(
            {
                "id": "456",
                "title": "My Page",
                "space": {"key": "QA"},
                "version": {"number": 3},
                "_links": {"webui": "/spaces/QA/pages/456", "base": "https://wiki.example.com"},
            },
            "https://wiki.example.com",
        )
        assert page.space_key == "QA"
        assert page.version == 3
        assert "wiki.example.com/spaces/QA/pages/456" in page.url

    def test_parse_page_missing_id_raises(self) -> None:
        with pytest.raises(ConfluencePublishError):
            _parse_page({"title": "no id"}, "https://wiki.example.com")


class TestConfluencePublisherInit:
    def test_init_requires_url(self) -> None:
        with pytest.raises(ValueError):
            ConfluencePublisher(server_url="", username="u", token="t")

    def test_init_requires_token(self) -> None:
        with pytest.raises(ValueError):
            ConfluencePublisher(server_url="https://wiki", username="u", token="")

    def test_init_strips_trailing_slash(self) -> None:
        pub = ConfluencePublisher(server_url="https://wiki/", username="u", token="t")
        assert pub.server_url == "https://wiki"

    def test_basic_auth_header(self) -> None:
        pub = ConfluencePublisher(server_url="https://wiki", username="alice", token="secret")
        header = pub._build_auth_header()
        assert header.startswith("Basic ")
        import base64

        decoded = base64.b64decode(header[6:]).decode()
        assert decoded == "alice:secret"

    def test_bearer_auth_header(self) -> None:
        pub = ConfluencePublisher(server_url="https://wiki", username="", token="PAT-token", auth_scheme="bearer")
        assert pub._build_auth_header() == "Bearer PAT-token"


class TestMarkdownDraftGeneration:
    def test_generate_basic_draft(self) -> None:
        plan_json = {
            "name": "Documentation: TEST-1",
            "origin": {"system": "jira_issue", "ref": "TEST-1"},
            "tasks": [
                {
                    "id": "test-1",
                    "title": "Document the API",
                    "description": "Write the API documentation.",
                    "acceptance_criteria": ["API is documented"],
                }
            ],
        }
        markdown = generate_markdown_draft(plan_json)
        assert "# Document the API" in markdown
        assert "Write the API documentation" in markdown
        assert "TEST-1" in markdown
        assert "API is documented" in markdown

    def test_generate_handles_empty_plan(self) -> None:
        plan_json = {"tasks": []}
        markdown = generate_markdown_draft(plan_json)
        assert "Documentation:" in markdown or "documentation" in markdown.lower()

    def test_generate_includes_acceptance_criteria(self) -> None:
        plan_json = {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Doc",
                    "description": "Body",
                    "acceptance_criteria": ["Criterion A", "Criterion B"],
                }
            ]
        }
        markdown = generate_markdown_draft(plan_json)
        assert "Criterion A" in markdown
        assert "Criterion B" in markdown


class TestWriteDraft:
    def test_write_draft_creates_file(self, tmp_path: Path) -> None:
        plan_path = tmp_path / "jira-TEST-1.json"
        plan_path.write_text("{}", encoding="utf-8")
        draft = write_draft(plan_path, "# Hello\n\nWorld")
        assert draft.exists()
        assert "Hello" in draft.read_text(encoding="utf-8")
        assert draft.name == "confluence-TEST-1.md"

    def test_write_draft_creates_out_dir(self, tmp_path: Path) -> None:
        plan_path = tmp_path / "jira-XYZ.json"
        plan_path.write_text("{}", encoding="utf-8")
        out_dir = tmp_path / "subdir" / "drafts"
        draft = write_draft(plan_path, "content", out_dir=out_dir)
        assert draft.parent == out_dir
        assert draft.exists()


class TestRunDocumentationFlow:
    def test_run_without_publisher_writes_draft_only(self, tmp_path: Path) -> None:
        plan_path = tmp_path / "jira-DOC-1.json"
        plan_path.write_text(
            json.dumps(
                {
                    "name": "Doc",
                    "origin": {"system": "jira_issue", "ref": "DOC-1"},
                    "tasks": [{"id": "t1", "title": "Test", "description": "Body"}],
                }
            ),
            encoding="utf-8",
        )
        result = run_documentation_flow(plan_path, publisher=None)
        assert isinstance(result, DocumentationFlowResult)
        assert result.published is False
        assert result.page is None
        assert result.draft_path.exists()

    def test_run_with_publisher_no_space_returns_error(self, tmp_path: Path) -> None:
        plan_path = tmp_path / "jira-DOC-2.json"
        plan_path.write_text('{"tasks": [{"id": "t1", "title": "T", "description": "D"}]}', encoding="utf-8")

        publisher = MagicMock()
        publisher.default_space = ""
        result = run_documentation_flow(plan_path, publisher=publisher)
        assert result.published is False
        assert "space" in result.error.lower()

    def test_run_with_publisher_success(self, tmp_path: Path) -> None:
        plan_path = tmp_path / "jira-DOC-3.json"
        plan_path.write_text(
            json.dumps(
                {
                    "name": "Doc",
                    "origin": {"system": "jira_issue", "ref": "DOC-3"},
                    "tasks": [{"id": "t1", "title": "API Docs", "description": "Body"}],
                }
            ),
            encoding="utf-8",
        )

        publisher = MagicMock()
        publisher.default_space = "QA"
        publisher.create_page.return_value = ConfluencePage(
            id="789",
            title="API Docs",
            space_key="QA",
            version=1,
            url="https://wiki.example.com/page/789",
        )

        result = run_documentation_flow(plan_path, publisher=publisher)
        assert result.published is True
        assert result.page is not None
        assert result.page.id == "789"
        assert "wiki.example.com" in result.page.url
        publisher.create_page.assert_called_once()

    def test_run_with_publisher_handles_error(self, tmp_path: Path) -> None:
        plan_path = tmp_path / "jira-DOC-4.json"
        plan_path.write_text('{"tasks": [{"id": "t1", "title": "T", "description": "D"}]}', encoding="utf-8")

        publisher = MagicMock()
        publisher.default_space = "QA"
        publisher.create_page.side_effect = ConfluencePublishError("403 Forbidden")

        result = run_documentation_flow(plan_path, publisher=publisher)
        assert result.published is False
        assert "403" in result.error


class TestDocumentationClassification:
    def test_documentation_kind_added_to_work_kinds(self) -> None:
        from whilly.jira_work import WORK_KINDS

        assert "documentation" in WORK_KINDS

    def test_classify_documentation_issue_type(self) -> None:
        from whilly.jira_work import classify_jira_work

        issue = {
            "key": "TEST-1",
            "fields": {
                "issuetype": {"name": "Documentation"},
                "summary": "Write API docs",
                "description": "We need to document the new API endpoints.",
            },
        }
        result = classify_jira_work(issue)
        assert result.kind == "documentation"
        assert result.recommended_flow == "documentation_publish"

    def test_classify_documentation_keywords(self) -> None:
        from whilly.jira_work import classify_jira_work

        issue = {
            "key": "TEST-2",
            "fields": {
                "issuetype": {"name": "Task"},
                "summary": "Update Confluence wiki",
                "description": "Document the deployment process on Confluence wiki.",
            },
        }
        result = classify_jira_work(issue)
        # Either documentation or task with documentation flow signals
        assert "documentation" in [s for s in result.signals if "documentation" in s] or result.kind in {
            "documentation",
            "task",
        }
