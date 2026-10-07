"""Proposal submission stays inside the host-created local IPC boundary."""

from __future__ import annotations

import json

from whilly.cli.swarm import run_swarm_command


def test_proposal_cli_never_falls_back_to_database(monkeypatch, capsys):
    monkeypatch.delenv("WHILLY_SWARM_MAILBOX", raising=False)
    assert run_swarm_command(["propose-task", "--request", '{"op":"propose"}']) == 2
    assert "coordinator mailbox required" in capsys.readouterr().err


def test_proposal_cli_only_queues_a_local_request(monkeypatch, tmp_path, capsys):
    root = tmp_path / "proposals"
    (root / "outbox").mkdir(parents=True)
    (root / "status.json").write_text('{"mode":"propose_only","blocker":null}')
    monkeypatch.setenv("WHILLY_SWARM_MAILBOX", str(tmp_path))
    body = {"op": "propose", "outcome": "Review neighboring module"}
    assert run_swarm_command(["propose-task", "--request", json.dumps(body)]) == 0
    assert "queued locally" in capsys.readouterr().out
    assert json.loads(next((root / "outbox").glob("*.json")).read_text()) == body


def test_proposal_cli_rejects_approval_commands(monkeypatch, tmp_path, capsys):
    root = tmp_path / "proposals"
    (root / "outbox").mkdir(parents=True)
    (root / "status.json").write_text('{"mode":"propose_only","blocker":null}')
    monkeypatch.setenv("WHILLY_SWARM_MAILBOX", str(tmp_path))
    assert run_swarm_command(["propose-task", "--request", '{"op":"approve"}']) == 2
    assert "only propose" in capsys.readouterr().err
    assert not list((root / "outbox").iterdir())
