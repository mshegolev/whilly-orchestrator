from __future__ import annotations

from whilly.cli import main


def test_main_dispatches_compliance_command(monkeypatch) -> None:
    import whilly.cli.compliance as compliance_cli

    called: dict[str, list[str]] = {}

    def fake_run(argv: list[str]) -> int:
        called["argv"] = argv
        return 17

    monkeypatch.setattr(compliance_cli, "run_compliance_command", fake_run)

    assert main(["compliance", "report", "--format", "json"]) == 17
    assert called["argv"] == ["report", "--format", "json"]


def test_main_dispatches_tui_command(monkeypatch) -> None:
    import whilly.cli.tui as tui_cli

    called: dict[str, list[str]] = {}

    def fake_run(argv: list[str]) -> int:
        called["argv"] = argv
        return 23

    monkeypatch.setattr(tui_cli, "run_tui_command", fake_run)

    assert main(["tui", "--plan", "P1"]) == 23
    assert called["argv"] == ["--plan", "P1"]
