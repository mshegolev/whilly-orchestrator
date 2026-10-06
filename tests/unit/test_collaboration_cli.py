"""Worker collaboration CLI uses only the host-created local mailbox."""

import json

from whilly.cli.swarm import run_swarm_command


def test_collaboration_never_falls_back_to_database(monkeypatch, capsys):
    monkeypatch.delenv("WHILLY_SWARM_MAILBOX", raising=False)
    assert run_swarm_command(["collaborate", "--inbox"]) == 2
    assert "coordinator mailbox required" in capsys.readouterr().err


def test_collaboration_queues_data_locally(monkeypatch, tmp_path, capsys):
    root = tmp_path / "collaboration"
    (root / "outbox").mkdir(parents=True)
    (root / "inbox.json").write_text('{"blocker":null,"messages":[]}')
    monkeypatch.setenv("WHILLY_SWARM_MAILBOX", str(tmp_path))
    assert run_swarm_command(["collaborate", "--request", '{"op":"send","payload":{"body":"finding"}}']) == 0
    assert "queued locally" in capsys.readouterr().out
    assert json.loads(next((root / "outbox").glob("*.json")).read_text())["payload"]["body"] == "finding"


def test_collaboration_respects_named_setup_blocker(monkeypatch, tmp_path, capsys):
    root = tmp_path / "collaboration"
    root.mkdir()
    (root / "inbox.json").write_text('{"blocker":"message_delivery_policy_required","messages":[]}')
    monkeypatch.setenv("WHILLY_SWARM_MAILBOX", str(tmp_path))
    assert run_swarm_command(["collaborate", "--request", '{"op":"send"}']) == 2
    assert "message_delivery_policy_required" in capsys.readouterr().err
