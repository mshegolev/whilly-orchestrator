"""Unit tests for ``whilly init`` CLI (TASK-104a-3 + TASK-104a-4).

Cover the composition root in :mod:`whilly.cli.init` against the PRD
in ``docs/PRD-v41-prd-wizard-port.md`` (FR-1..FR-8 + parts of SC).

Strategy: every test injects fakes for the four production seams
(``interactive_runner``, ``headless_runner``, ``tasks_builder``,
``plan_inserter``) so the suite never spawns a Claude subprocess and
never hits Postgres. The seams themselves are thin wrappers over real
modules — their wiring is exercised end-to-end in TASK-104a-5's
integration test, not here.

What this file pins:
* FR-1 / FR-2 — argparse layout and TTY-vs-flag mode resolution
* FR-3 — slug derivation and validation
* FR-5 — success message format and exit code
* FR-6 — ``--no-import`` shortcut skips DB
* FR-7 — idempotency: existing PRD blocks without ``--force``
* FR-8 — error paths return the right non-zero exit codes
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from whilly.cli.init import (
    EXIT_ENVIRONMENT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USER_ERROR,
    _build_parser,
    _resolve_mode,
    _slugify,
    _validate_slug,
    run_init_command,
)


# ─── _slugify ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello World", "hello-world"),
        ("Build a CLI tool for monitoring API", "build-a-cli-tool-for-monitoring-api"),
        ("UPPER  case   spaces!!", "upper-case-spaces"),
        ("with/slashes\\and:colons", "with-slashes-and-colons"),
        ("---leading-trailing---", "leading-trailing"),
        (
            "a  very  long  description  with  more  than  eight  words  total",
            "a-very-long-description-with-more-than-eight",
        ),
        ("", "plan"),
        ("!!!", "plan"),
        ("123 456", "123-456"),
        ("café résumé", "caf-r-sum"),  # non-ASCII collapses to its hyphen-runs
    ],
)
def test_slugify_kebab_cases_input(text: str, expected: str) -> None:
    """_slugify produces deterministic kebab-case from arbitrary text."""
    assert _slugify(text) == expected


# ─── _validate_slug ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "slug",
    ["a", "ab", "feature-x", "task-104a-3", "v4-1-prd-wizard", "0", "abc-123-def"],
)
def test_validate_slug_accepts_legal(slug: str) -> None:
    """All legal kebab-case identifiers pass."""
    assert _validate_slug(slug) is None


@pytest.mark.parametrize(
    "slug",
    ["", "-foo", "foo-", "Foo", "feature x", "feature/x", "--", "a--", "_underscore"],
)
def test_validate_slug_rejects_illegal(slug: str) -> None:
    """Bad input gets a non-None error message."""
    err = _validate_slug(slug)
    assert err is not None
    assert "slug" in err.lower()


# ─── _resolve_mode ─────────────────────────────────────────────────────────


def test_resolve_mode_interactive_flag_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)  # would be headless by default
    assert _resolve_mode(force_interactive=True, force_headless=False) == "interactive"


def test_resolve_mode_headless_flag_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)  # would be interactive by default
    assert _resolve_mode(force_interactive=False, force_headless=True) == "headless"


def test_resolve_mode_default_in_tty_is_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert _resolve_mode(force_interactive=False, force_headless=False) == "interactive"


def test_resolve_mode_default_no_tty_is_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _resolve_mode(force_interactive=False, force_headless=False) == "headless"


# ─── argparse layout (FR-1) ────────────────────────────────────────────────


def test_parser_idea_required() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])  # missing positional


def test_parser_joins_multi_word_idea() -> None:
    parser = _build_parser()
    args = parser.parse_args(["build", "a", "thing"])
    assert args.idea == ["build", "a", "thing"]


def test_parser_mode_flags_mutually_exclusive() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["idea", "--interactive", "--headless"])


def test_parser_all_flags_present() -> None:
    """Snapshot of advertised flags — pins FR-1 / SC-3 of the PRD."""
    parser = _build_parser()
    help_text = parser.format_help()
    for flag in ["--slug", "--interactive", "--headless", "--no-import", "--force", "--model", "--output-dir"]:
        assert flag in help_text, f"--help missing advertised flag {flag}"


# ─── run_init_command happy path (FR-2 / FR-4 / FR-5) ──────────────────────


def _make_fake_runner_writes_prd(prd_text: str = "# PRD\n\nfake content\n"):
    """Build a fake interactive/headless runner that writes a deterministic PRD."""

    def runner(*, idea: str, slug: str, output_dir: Path, model: str) -> Any:
        path = Path(output_dir).resolve() / f"PRD-{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prd_text, encoding="utf-8")
        return 0  # interactive runner returns int; headless returns None — both OK here

    return runner


def _fake_tasks_builder(payload_tasks: list[dict] | None = None):
    """Build a fake tasks_builder returning a canned payload."""

    def builder(*, prd_path: Path, plan_id: str, model: str) -> dict:
        return {
            "project": "Fake project",
            "plan_id": plan_id,
            "tasks": payload_tasks
            or [
                {
                    "id": "TASK-001",
                    "status": "pending",
                    "priority": "high",
                    "description": "Fake task",
                    "dependencies": [],
                    "key_files": [],
                    "acceptance_criteria": [],
                    "test_steps": [],
                }
            ],
        }

    return builder


def _fake_plan_inserter(call_log: list[tuple[str, str]] | None = None, return_count: int = 1):
    """Build a fake plan_inserter that just records calls."""

    def inserter(*, payload: dict, plan_id: str, dsn: str) -> int:
        if call_log is not None:
            call_log.append((plan_id, dsn))
        return return_count

    return inserter


def test_init_happy_path_headless_imports_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end happy path through the headless flow."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    inserter_log: list[tuple[str, str]] = []

    rc = run_init_command(
        ["build a CLI tool", "--headless", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(inserter_log, return_count=1),
    )

    assert rc == EXIT_OK
    assert (tmp_path / "PRD-build-a-cli-tool.md").exists()
    assert inserter_log == [("build-a-cli-tool", "postgresql://fake/test")]

    out = capsys.readouterr().out
    assert "Plan 'build-a-cli-tool' imported" in out
    assert "whilly run --plan build-a-cli-tool" in out
    assert "whilly plan show build-a-cli-tool" in out


def test_init_explicit_slug_overrides_auto_derivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    rc = run_init_command(
        ["any idea", "--slug", "explicit-slug", "--headless", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(return_count=2),
    )
    assert rc == EXIT_OK
    assert (tmp_path / "PRD-explicit-slug.md").exists()
    assert "Plan 'explicit-slug' imported (2 tasks)" in capsys.readouterr().out


# ─── --no-import (FR-6) ────────────────────────────────────────────────────


def test_init_no_import_skips_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--no-import: PRD written, plan_inserter never called."""
    monkeypatch.delenv("WHILLY_DATABASE_URL", raising=False)
    inserter_log: list[tuple[str, str]] = []

    rc = run_init_command(
        ["test idea", "--headless", "--no-import", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(inserter_log),
    )

    assert rc == EXIT_OK
    assert (tmp_path / "PRD-test-idea.md").exists()
    assert inserter_log == []  # never called
    assert "--no-import was set" in capsys.readouterr().out


# ─── idempotency / --force (FR-7) ──────────────────────────────────────────


def test_init_existing_prd_without_force_aborts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-7: re-running with same slug + existing PRD → exit 1."""
    existing = (tmp_path / "PRD-existing.md").resolve()
    existing.write_text("already here", encoding="utf-8")

    rc = run_init_command(
        ["whatever", "--slug", "existing", "--headless", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )

    assert rc == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--force" in err


def test_init_existing_prd_with_force_proceeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-7: --force lets the wizard overwrite an existing PRD."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    existing = (tmp_path / "PRD-overwrite.md").resolve()
    existing.write_text("old content", encoding="utf-8")

    rc = run_init_command(
        ["new idea", "--slug", "overwrite", "--headless", "--force", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(prd_text="# new PRD\n"),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    assert rc == EXIT_OK
    assert "new PRD" in (tmp_path / "PRD-overwrite.md").read_text(encoding="utf-8")


# ─── error paths (FR-8) ────────────────────────────────────────────────────


def test_init_empty_idea_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_init_command(["   "])
    assert rc == EXIT_USER_ERROR
    assert "cannot be empty" in capsys.readouterr().err


def test_init_invalid_slug_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_init_command(
        ["idea", "--slug", "Bad Slug!", "--headless", "--output-dir", str(tmp_path)],
    )
    assert rc == EXIT_USER_ERROR
    assert "slug" in capsys.readouterr().err.lower()


def test_init_wizard_exits_without_prd_returns_user_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-8: wizard exit 0 but PRD not written → exit 1."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")

    def runner_no_write(*, idea: str, slug: str, output_dir: Path, model: str) -> int:
        return 0  # exits cleanly but writes nothing

    rc = run_init_command(
        ["idea", "--headless", "--output-dir", str(tmp_path)],
        headless_runner=runner_no_write,
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    assert rc == EXIT_USER_ERROR
    assert "without writing" in capsys.readouterr().err.lower()


def test_init_interactive_runner_nonzero_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Interactive runner returning non-zero exit → exit 1."""

    def failing_runner(*, idea: str, slug: str, output_dir: Path, model: str) -> int:
        return 1

    rc = run_init_command(
        ["idea", "--interactive", "--output-dir", str(tmp_path)],
        interactive_runner=failing_runner,
    )
    assert rc == EXIT_USER_ERROR
    assert "wizard exited" in capsys.readouterr().err.lower()


def test_init_tasks_builder_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """generate_tasks_dict failure → exit 1, PRD left for inspection."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")

    def bad_builder(*, prd_path: Path, plan_id: str, model: str) -> dict:
        raise RuntimeError("mock parse failure")

    rc = run_init_command(
        ["idea", "--headless", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=bad_builder,
        plan_inserter=_fake_plan_inserter(),
    )
    assert rc == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "task generation failed" in err
    assert "mock parse failure" in err
    # PRD file remains for inspection.
    assert (tmp_path / "PRD-idea.md").exists()


def test_init_missing_database_url_returns_env_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-8: WHILLY_DATABASE_URL unset → exit 2 (env error), PRD preserved."""
    monkeypatch.delenv("WHILLY_DATABASE_URL", raising=False)

    rc = run_init_command(
        ["idea", "--headless", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    assert rc == EXIT_ENVIRONMENT_ERROR
    err = capsys.readouterr().err
    assert "WHILLY_DATABASE_URL" in err
    assert (tmp_path / "PRD-idea.md").exists()


def test_init_inserter_failure_returns_user_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DB INSERT exception → exit 1, PRD left."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")

    def crash_inserter(*, payload: dict, plan_id: str, dsn: str) -> int:
        raise Exception("connection refused")

    rc = run_init_command(
        ["idea", "--headless", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=crash_inserter,
    )
    assert rc == EXIT_USER_ERROR
    assert "import failed" in capsys.readouterr().err
    assert (tmp_path / "PRD-idea.md").exists()


def test_init_keyboard_interrupt_returns_130(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl-C during wizard → exit 130 (POSIX SIGINT convention)."""

    def interrupted_runner(*, idea: str, slug: str, output_dir: Path, model: str) -> int:
        raise KeyboardInterrupt

    rc = run_init_command(
        ["idea", "--interactive", "--output-dir", str(tmp_path)],
        interactive_runner=interrupted_runner,
    )
    assert rc == EXIT_INTERRUPTED
    assert "interrupted" in capsys.readouterr().err.lower()
