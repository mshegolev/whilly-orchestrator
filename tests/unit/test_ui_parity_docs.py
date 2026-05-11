from __future__ import annotations

from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_operator_docs_pin_current_tui_wui_hotkeys() -> None:
    getting_started = _read("docs/Getting-Started.md")
    usage = _read("docs/Whilly-Usage.md")

    for text in (getting_started, usage):
        assert "1-5=switch" in text
        assert "Overview, Compliance, Plans/Tasks, Workers, and Events" in text
        assert "p" in text
        assert "Pause workers" in text or "pause workers" in text
        assert "a/x/c" in text or "a / x / c" in text
        assert "q/d/l/t/h" not in text


def test_codex_mission_docs_pin_ui_fragment_boundary() -> None:
    mission = _read("docs/CODEX-MISSION.md")

    assert "Current v1.1 Operator UI Parity Evidence" in mission
    assert "1-5=switch" in mission
    assert "_logs.html" in mission
    assert "routeable noncanonical fragment" in mission
    assert "_admin.html" in mission
    assert "_prd.html" in mission
    assert "quarantined inactive WUI fragments" in mission
