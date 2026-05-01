"""Unit tests for the v3 legacy CLI-flag shim in :mod:`whilly.cli`.

The shim translates v3-era top-level long flags into the v4
subcommand surface so STRICT backwards compatibility holds (per
``AGENTS.md``). Without it, every legacy invocation
(``whilly --tasks ...``, ``whilly --headless``, ``whilly --init ...``,
``whilly --prd-wizard``, ``whilly --resume``, ``whilly --reset PLAN``,
``whilly --all``, plus the ``--workspace`` / ``--worktree`` /
``--no-workspace`` / ``--no-worktree`` opt-in/no-op modifiers) would
die in the v4 dispatcher with "unknown command" — that's exactly the
M1 user-testing finding we are fixing here:

    VAL-CROSS-BACKCOMPAT-008  ``whilly --tasks tasks.json`` resolves
    VAL-CROSS-BACKCOMPAT-009  ``whilly --headless`` exit-code contract
    VAL-CROSS-BACKCOMPAT-905  ``--workspace`` opt-in path still parsed
    VAL-CROSS-BACKCOMPAT-906  ``--no-workspace`` / ``--no-worktree`` no-ops
    VAL-M1-BACKCOMPAT-901     ``whilly --workspace --tasks tasks.json``
    VAL-M1-BACKCOMPAT-902     ``whilly --init "desc"`` PRD pipeline
    VAL-M1-BACKCOMPAT-903     ``whilly --prd-wizard --help`` dispatch

Each assertion has a corresponding test case below.

What we cover here
------------------
* Each legacy verb routes to the right v4 subcommand handler.
* No-op modifiers (workspace/worktree) are silently consumed.
* ``--headless`` exports ``WHILLY_HEADLESS=1`` and is stripped.
* Legacy modifiers can be combined (``--no-workspace --tasks``).
* ``whilly --resume`` and ``whilly --all`` exit 0 with a diagnostic
  (they have no v4 equivalent, but legacy scripts must not break).
* New-style invocations (``whilly run ...``, ``whilly init ...``,
  ``whilly worker connect ...``) route unchanged.
* The shim does not interfere with ``whilly --help`` / ``whilly -V``.

How we isolate
--------------
We patch the v4 subcommand entry points (``run_run_command``,
``run_init_command``, ``run_plan_command``, ``run_worker_command``,
``run_register_command``, ``run_connect_command``, ``run_dashboard_command``)
on :mod:`whilly.cli` so the shim's routing decision is observable
without spinning up Postgres or invoking Claude.
"""

from __future__ import annotations

from typing import Sequence

import pytest

import whilly.cli as cli
from whilly.cli import _apply_legacy_shim, main


# ─── helper: spy that records the args it was called with ──────────────


class _Spy:
    """Minimal callable spy — records ``args`` and returns ``rc``."""

    def __init__(self, rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, argv: Sequence[str], **_kwargs: object) -> int:
        # Store as list so equality checks against test expectations
        # don't fail when argparse passes a generator-like view.
        self.calls.append(list(argv))
        return self.rc


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip env vars the shim might mutate so tests stay independent."""
    monkeypatch.delenv("WHILLY_HEADLESS", raising=False)


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Spy]:
    """Patch every v4 subcommand entry point to a spy.

    Returning the mapping lets each test assert the routing decision
    without monkeypatching at use-site.
    """
    bag: dict[str, _Spy] = {
        "run": _Spy(),
        "init": _Spy(),
        "plan": _Spy(),
        "dashboard": _Spy(),
        "worker": _Spy(),
        "register": _Spy(),
        "connect": _Spy(),
        "forge": _Spy(),
    }
    # The dispatcher imports each handler lazily; inject the spies into
    # the source modules so the eventual ``from whilly.cli.run import
    # run_run_command`` picks up our stub.
    import whilly.cli.dashboard as cli_dashboard
    import whilly.cli.init as cli_init
    import whilly.cli.plan as cli_plan
    import whilly.cli.run as cli_run
    import whilly.cli.worker as cli_worker
    import whilly.forge.intake as forge_intake

    monkeypatch.setattr(cli_run, "run_run_command", bag["run"])
    monkeypatch.setattr(cli_init, "run_init_command", bag["init"])
    monkeypatch.setattr(cli_plan, "run_plan_command", bag["plan"])
    monkeypatch.setattr(cli_dashboard, "run_dashboard_command", bag["dashboard"])
    monkeypatch.setattr(cli_worker, "run_worker_command", bag["worker"])
    monkeypatch.setattr(cli_worker, "run_register_command", bag["register"])
    monkeypatch.setattr(cli_worker, "run_connect_command", bag["connect"])
    monkeypatch.setattr(forge_intake, "run_forge_command", bag["forge"])
    return bag


# ─── shim function: pure routing decisions (no I/O) ─────────────────────


class TestApplyLegacyShim:
    """Direct tests of :func:`whilly.cli._apply_legacy_shim`.

    These bypass :func:`main` so we can pin the rewrite contract without
    side effects from argparse / handler dispatch.
    """

    def test_empty_args_returns_passthrough(self) -> None:
        new_args, exit_code = _apply_legacy_shim([])
        assert new_args is None
        assert exit_code is None

    def test_v4_subcommand_passthrough(self) -> None:
        """``whilly run --plan X`` is not legacy; shim must not touch it."""
        new_args, exit_code = _apply_legacy_shim(["run", "--plan", "X"])
        assert new_args is None
        assert exit_code is None

    def test_unknown_top_level_flag_is_passthrough(self) -> None:
        """Unrecognised flags fall through so the existing 'unknown command' diagnostic still fires."""
        new_args, exit_code = _apply_legacy_shim(["--bogus"])
        assert new_args is None
        assert exit_code is None

    # VAL-CROSS-BACKCOMPAT-008 ------------------------------------------------

    def test_tasks_routes_to_run_with_plan(self) -> None:
        """``whilly --tasks PATH`` → ``whilly run --plan PATH``."""
        new_args, exit_code = _apply_legacy_shim(["--tasks", "tasks.json"])
        assert new_args == ["run", "--plan", "tasks.json"]
        assert exit_code is None

    def test_tasks_with_extra_argv_is_preserved(self) -> None:
        """Trailing args (``--max-iterations 1``) survive the rewrite."""
        new_args, _ = _apply_legacy_shim(["--tasks", "p.json", "--max-iterations", "1"])
        assert new_args == ["run", "--plan", "p.json", "--max-iterations", "1"]

    def test_tasks_without_path_returns_exit_2(self) -> None:
        """``whilly --tasks`` (no path) emits a clear diagnostic + exit 2."""
        new_args, exit_code = _apply_legacy_shim(["--tasks"])
        assert new_args is None
        assert exit_code == 2

    # VAL-CROSS-BACKCOMPAT-009 ------------------------------------------------

    def test_headless_sets_env_and_strips_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--headless`` exports ``WHILLY_HEADLESS=1`` and disappears from argv."""
        monkeypatch.delenv("WHILLY_HEADLESS", raising=False)
        new_args, _ = _apply_legacy_shim(["--headless", "--tasks", "p.json"])
        import os

        assert os.environ.get("WHILLY_HEADLESS") == "1"
        assert new_args == ["run", "--plan", "p.json"]

    # VAL-CROSS-BACKCOMPAT-905 / 906 -----------------------------------------

    def test_workspace_modifier_is_silently_consumed(self) -> None:
        new_args, exit_code = _apply_legacy_shim(["--workspace", "--tasks", "p.json"])
        assert new_args == ["run", "--plan", "p.json"]
        assert exit_code is None

    def test_no_workspace_no_worktree_modifiers_are_silently_consumed(self) -> None:
        new_args, exit_code = _apply_legacy_shim(["--no-workspace", "--no-worktree", "--tasks", "p.json"])
        assert new_args == ["run", "--plan", "p.json"]
        assert exit_code is None

    def test_only_modifiers_falls_back_to_help(self) -> None:
        """``whilly --no-workspace`` alone should invoke v4 default help (empty argv)."""
        new_args, exit_code = _apply_legacy_shim(["--no-workspace"])
        assert new_args == []
        assert exit_code is None

    # VAL-M1-BACKCOMPAT-901 ---------------------------------------------------

    def test_workspace_with_tasks_fixture_path_routes_correctly(self) -> None:
        new_args, _ = _apply_legacy_shim(["--workspace", "--tasks", "fixtures/tasks.json"])
        assert new_args == ["run", "--plan", "fixtures/tasks.json"]

    # VAL-M1-BACKCOMPAT-902 ---------------------------------------------------

    def test_init_strips_legacy_plan_and_go_modifiers(self) -> None:
        """v3's ``--plan`` / ``--go`` modifiers are no-ops in v4 init."""
        new_args, _ = _apply_legacy_shim(["--init", "demo backcompat", "--plan", "--go"])
        assert new_args == ["init", "demo backcompat"]

    def test_init_without_description_returns_exit_2(self) -> None:
        new_args, exit_code = _apply_legacy_shim(["--init"])
        assert new_args is None
        assert exit_code == 2

    # VAL-M1-BACKCOMPAT-903 ---------------------------------------------------

    def test_prd_wizard_help_routes_to_init_help(self) -> None:
        """``whilly --prd-wizard --help`` → ``whilly init --help``.

        Smoke test that doesn't require a Claude binary — pins the
        VAL-M1-BACKCOMPAT-903 contract.
        """
        new_args, exit_code = _apply_legacy_shim(["--prd-wizard", "--help"])
        assert new_args == ["init", "--help"]
        assert exit_code is None

    def test_prd_wizard_with_slug_routes_to_interactive_init(self) -> None:
        new_args, _ = _apply_legacy_shim(["--prd-wizard", "myslug"])
        # The slug doubles as the idea positional and the --slug arg
        # so v4 init's required-positional / slug validation both pass.
        assert new_args == ["init", "--interactive", "--slug", "myslug", "myslug"]

    def test_prd_wizard_without_args_routes_to_interactive_init(self) -> None:
        new_args, _ = _apply_legacy_shim(["--prd-wizard"])
        assert new_args == ["init", "--interactive", "wizard"]

    # --resume / --reset / --all ---------------------------------------------

    def test_resume_is_noop_with_diagnostic(self, capsys: pytest.CaptureFixture[str]) -> None:
        new_args, exit_code = _apply_legacy_shim(["--resume"])
        assert new_args is None
        assert exit_code == 0
        assert "no-op" in capsys.readouterr().err

    def test_reset_routes_to_plan_reset_keep_tasks(self) -> None:
        new_args, _ = _apply_legacy_shim(["--reset", "myplan"])
        assert new_args == ["plan", "reset", "myplan", "--keep-tasks", "--yes"]

    def test_reset_without_plan_returns_exit_2(self) -> None:
        new_args, exit_code = _apply_legacy_shim(["--reset"])
        assert new_args is None
        assert exit_code == 2

    def test_all_is_noop_with_diagnostic(self, capsys: pytest.CaptureFixture[str]) -> None:
        new_args, exit_code = _apply_legacy_shim(["--all"])
        assert new_args is None
        assert exit_code == 0
        assert "no-op" in capsys.readouterr().err


# ─── full main() dispatch: shim + v4 subcommand wiring ─────────────────


class TestMainDispatchWithShim:
    """End-to-end (shim + dispatcher) routing tests using the spy fixture."""

    # VAL-CROSS-BACKCOMPAT-008
    def test_main_tasks_routes_to_run(self, spies: dict[str, _Spy]) -> None:
        rc = main(["--tasks", "tasks.json"])
        assert rc == 0
        assert spies["run"].calls == [["--plan", "tasks.json"]]
        # Sanity: no other handler fired.
        assert spies["init"].calls == []
        assert spies["plan"].calls == []

    # VAL-CROSS-BACKCOMPAT-009
    def test_main_headless_with_tasks_sets_env_and_routes(
        self, spies: dict[str, _Spy], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WHILLY_HEADLESS", raising=False)
        rc = main(["--headless", "--tasks", "p.json"])
        assert rc == 0
        import os

        assert os.environ.get("WHILLY_HEADLESS") == "1"
        assert spies["run"].calls == [["--plan", "p.json"]]

    # VAL-CROSS-BACKCOMPAT-906
    def test_main_no_workspace_no_worktree_with_tasks(self, spies: dict[str, _Spy]) -> None:
        rc = main(["--no-workspace", "--no-worktree", "--tasks", "p.json"])
        assert rc == 0
        assert spies["run"].calls == [["--plan", "p.json"]]

    def test_main_no_workspace_alone_prints_help(
        self, spies: dict[str, _Spy], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Just ``whilly --no-workspace`` → v4 help (exit 0)."""
        rc = main(["--no-workspace"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Whilly v4" in out
        assert spies["run"].calls == []
        assert spies["init"].calls == []

    # VAL-M1-BACKCOMPAT-901
    def test_main_workspace_with_tasks_routes_to_run(self, spies: dict[str, _Spy]) -> None:
        rc = main(["--workspace", "--tasks", "fixtures/tasks.json"])
        assert rc == 0
        assert spies["run"].calls == [["--plan", "fixtures/tasks.json"]]

    # VAL-M1-BACKCOMPAT-902
    def test_main_init_dispatches_init_pipeline(self, spies: dict[str, _Spy]) -> None:
        rc = main(["--init", "demo backcompat", "--plan", "--go"])
        assert rc == 0
        # ``--plan`` and ``--go`` are dropped (v4 init has no equivalent).
        assert spies["init"].calls == [["demo backcompat"]]

    # VAL-M1-BACKCOMPAT-903
    def test_main_prd_wizard_help_dispatches_init_help(self, spies: dict[str, _Spy]) -> None:
        rc = main(["--prd-wizard", "--help"])
        assert rc == 0
        assert spies["init"].calls == [["--help"]]

    def test_main_prd_wizard_with_slug_dispatches_interactive(self, spies: dict[str, _Spy]) -> None:
        rc = main(["--prd-wizard", "demo-slug"])
        assert rc == 0
        assert spies["init"].calls == [["--interactive", "--slug", "demo-slug", "demo-slug"]]

    def test_main_resume_exits_zero_without_dispatch(self, spies: dict[str, _Spy]) -> None:
        rc = main(["--resume"])
        assert rc == 0
        assert spies["run"].calls == []
        assert spies["init"].calls == []

    def test_main_all_exits_zero_without_dispatch(self, spies: dict[str, _Spy]) -> None:
        rc = main(["--all"])
        assert rc == 0
        assert spies["run"].calls == []
        assert spies["plan"].calls == []

    def test_main_reset_dispatches_plan_reset(self, spies: dict[str, _Spy]) -> None:
        rc = main(["--reset", "myplan"])
        assert rc == 0
        assert spies["plan"].calls == [["reset", "myplan", "--keep-tasks", "--yes"]]

    # ── New-style invocations: must remain untouched by the shim ──

    def test_main_new_style_run_unchanged(self, spies: dict[str, _Spy]) -> None:
        rc = main(["run", "--plan", "p"])
        assert rc == 0
        assert spies["run"].calls == [["--plan", "p"]]

    def test_main_new_style_init_unchanged(self, spies: dict[str, _Spy]) -> None:
        rc = main(["init", "fresh idea"])
        assert rc == 0
        assert spies["init"].calls == [["fresh idea"]]

    def test_main_new_style_worker_connect_unchanged(self, spies: dict[str, _Spy]) -> None:
        rc = main(["worker", "connect", "--url", "http://x"])
        assert rc == 0
        assert spies["connect"].calls == [["--url", "http://x"]]

    def test_main_new_style_plan_show_unchanged(self, spies: dict[str, _Spy]) -> None:
        rc = main(["plan", "show", "p"])
        assert rc == 0
        assert spies["plan"].calls == [["show", "p"]]

    def test_main_new_style_dashboard_unchanged(self, spies: dict[str, _Spy]) -> None:
        rc = main(["dashboard", "--once"])
        assert rc == 0
        assert spies["dashboard"].calls == [["--once"]]

    # ── Ensure --help / --version paths still work ──

    def test_main_dash_help_prints_v4_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--help"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Whilly v4" in out

    def test_main_dash_version_prints_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--version"])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.startswith("whilly ")

    def test_main_unknown_command_exits_2_without_shim_interference(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["bogus"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown command" in err


# ─── Sanity: the shim does not throw on weird inputs ──────────────────


def test_shim_handles_doubled_headless_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two ``--headless`` tokens collapse cleanly (idempotent)."""
    monkeypatch.delenv("WHILLY_HEADLESS", raising=False)
    new_args, _ = _apply_legacy_shim(["--headless", "--headless", "--tasks", "p"])
    assert new_args == ["run", "--plan", "p"]


def test_shim_does_not_swallow_v4_help_after_modifiers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``whilly --no-workspace --help`` should print v4 help without dispatching."""
    monkeypatch.delenv("WHILLY_HEADLESS", raising=False)
    rc = main(["--no-workspace", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Whilly v4" in out


def test_shim_module_constants_are_frozensets() -> None:
    """Tests pin the public-ish surface so a refactor that converts the
    legacy tables to a mutable list (and accidentally lets a caller
    mutate them) shows up as a failed import-time guarantee."""
    assert isinstance(cli._LEGACY_NOOP_FLAGS, frozenset)
    assert isinstance(cli._LEGACY_VERB_FLAGS, frozenset)
    # The exact membership is the contract.
    assert cli._LEGACY_NOOP_FLAGS == {
        "--workspace",
        "--worktree",
        "--no-workspace",
        "--no-worktree",
    }
    assert "--tasks" in cli._LEGACY_VERB_FLAGS
    assert "--init" in cli._LEGACY_VERB_FLAGS
    assert "--prd-wizard" in cli._LEGACY_VERB_FLAGS
    assert "--resume" in cli._LEGACY_VERB_FLAGS
    assert "--reset" in cli._LEGACY_VERB_FLAGS
    assert "--all" in cli._LEGACY_VERB_FLAGS
    assert "--headless" in cli._LEGACY_VERB_FLAGS
