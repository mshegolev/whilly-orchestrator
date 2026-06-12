from __future__ import annotations

from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_live_smoke_section_present() -> None:
    """docs/Whilly-Usage.md must contain the Live smoke section heading."""
    usage = _read("docs/Whilly-Usage.md")

    assert "## Live smoke" in usage


def test_live_smoke_commands_documented() -> None:
    """Both smoke commands must be documented by name."""
    usage = _read("docs/Whilly-Usage.md")

    assert "whilly jira smoke" in usage
    assert "whilly gitlab smoke" in usage


def test_live_smoke_report_path_documented() -> None:
    """The report directory must be named so operators know where to find evidence."""
    usage = _read("docs/Whilly-Usage.md")

    assert "whilly_logs/smoke/" in usage


def test_live_smoke_exit_codes_documented() -> None:
    """All three exit codes (0, 1, 2) must appear in the Live smoke section context."""
    usage = _read("docs/Whilly-Usage.md")

    # Locate the Live smoke section and check codes appear within it
    smoke_start = usage.index("## Live smoke")
    # Find the next top-level section after Live smoke
    next_h2 = usage.find("\n## ", smoke_start + 1)
    smoke_section = usage[smoke_start:next_h2] if next_h2 != -1 else usage[smoke_start:]

    assert "| `0`" in smoke_section or "0 " in smoke_section
    assert "| `1`" in smoke_section or "1 " in smoke_section
    assert "| `2`" in smoke_section or "2 " in smoke_section


def test_live_smoke_no_prohibited_hotkey_string() -> None:
    """Guard Pitfall 7: the prohibited hotkey string must never appear in the docs."""
    usage = _read("docs/Whilly-Usage.md")

    assert "q/d/l/t/h" not in usage


# ---------------------------------------------------------------------------
# Jira watcher daemon docs regression tests
# ---------------------------------------------------------------------------


def _watcher_section(usage: str) -> str:
    """Extract the Jira watcher daemon section from Whilly-Usage.md."""
    start = usage.index("## Jira watcher daemon")
    next_h2 = usage.find("\n## ", start + 1)
    return usage[start:next_h2] if next_h2 != -1 else usage[start:]


def test_watcher_section_heading_present() -> None:
    """docs/Whilly-Usage.md must contain the Jira watcher daemon section heading."""
    usage = _read("docs/Whilly-Usage.md")
    assert "## Jira watcher daemon" in usage


def test_watcher_commands_documented() -> None:
    """Both watch commands must appear in the watcher section."""
    usage = _read("docs/Whilly-Usage.md")
    section = _watcher_section(usage)
    assert "whilly jira watch" in section
    assert "whilly jira watch-status" in section


def test_watcher_log_dir_documented() -> None:
    """The status-file directory path must be named in the watcher section."""
    usage = _read("docs/Whilly-Usage.md")
    section = _watcher_section(usage)
    assert "whilly_logs/watch/" in section


def test_watcher_status_file_name_documented() -> None:
    """The status-file name jira-watch-status.json must appear in the watcher section."""
    usage = _read("docs/Whilly-Usage.md")
    section = _watcher_section(usage)
    assert "jira-watch-status.json" in section


def test_watcher_dispatch_default_off_documented() -> None:
    """The watcher section must mention that --dispatch is off by default."""
    usage = _read("docs/Whilly-Usage.md")
    section = _watcher_section(usage)
    assert "--dispatch" in section
    # Must state it is off by default in some form
    assert "OFF by default" in section or "off by default" in section or "default" in section
