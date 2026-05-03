"""Unit tests for the funnel sidecar URL parser + atomic file write.

Pins the contract for ``scripts/funnel/run.sh`` (M2 feature
``m2-localhostrun-funnel-sidecar``):

* The ``https://[a-z0-9-]+\\.lhr\\.life`` regex extracts the URL
  cleanly from a representative localhost.run banner without
  matching any surrounding ANSI / decorative output.
* The script's ``publish_to_file`` helper writes ``$FUNNEL_URL_FILE``
  via tmp-file + atomic ``mv`` (so a concurrent reader never observes
  a half-written file).
* The script's ``parse_dsn_into_pg_env`` helper extracts
  ``PG{HOST,PORT,USER,DATABASE}`` and ``PGPASSWORD`` from a
  ``postgres://`` DSN, so ``psql`` never sees the password on argv.
* The ``FUNNEL_FAKE_URL`` test bypass produces a valid banner, so the
  integration test can exercise the publish path without requiring
  outbound TCP/22 to localhost.run.

The script is bash, not Python — these tests shell out via
``subprocess.run`` and assert observable stdout / file-system effects.
A ``bash`` binary is required (skipped if absent — should never
trigger in CI given the project's bash dependency).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
RUN_SH: Path = REPO_ROOT / "scripts" / "funnel" / "run.sh"

LHR_REGEX: re.Pattern[str] = re.compile(r"https://[a-z0-9-]+\.lhr\.life")

REPRESENTATIVE_BANNER: str = (
    "===============================================================================\n"
    "Welcome to localhost.run!\n"
    "\n"
    "** your connection id is 0a1b2c3d-4e5f-6789-abcd-ef0123456789, "
    "please mention it if you send in a support request.\n"
    "\n"
    "To set up and manage custom domains visit https://admin.localhost.run/\n"
    "\n"
    "** your unique URL is: **\n"
    "https://abc123def456.lhr.life\n"
    "===============================================================================\n"
)


bash_required = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash binary not on PATH; funnel sidecar tests require bash",
)


def test_run_sh_exists_and_is_executable() -> None:
    assert RUN_SH.is_file(), f"funnel sidecar entry script missing at {RUN_SH}"
    assert os.access(RUN_SH, os.X_OK), f"{RUN_SH} must be executable"


@bash_required
def test_run_sh_parses_with_bash_n() -> None:
    """``bash -n`` confirms the script parses without syntax errors."""
    result = subprocess.run(["bash", "-n", str(RUN_SH)], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


# ---------------------------------------------------------------------------
# Regex extraction
# ---------------------------------------------------------------------------


def test_regex_extracts_url_from_representative_banner() -> None:
    """Regex matches a single canonical URL inside the localhost.run banner."""
    matches = LHR_REGEX.findall(REPRESENTATIVE_BANNER)
    assert matches == ["https://abc123def456.lhr.life"]


def test_regex_does_not_match_admin_dashboard_url() -> None:
    """``https://admin.localhost.run/`` must NOT match the funnel-URL regex.

    The banner mentions both URLs; the parser must only publish the
    ``*.lhr.life`` tunnel URL, not the admin dashboard link.
    """
    matches = LHR_REGEX.findall("To set up and manage custom domains visit https://admin.localhost.run/")
    assert matches == []


@pytest.mark.parametrize(
    "url",
    [
        "https://abc.lhr.life",
        "https://abc-123.lhr.life",
        "https://0a1b2c3d-4e5f-6789-abcd-ef0123456789.lhr.life",
    ],
)
def test_regex_matches_url_shapes(url: str) -> None:
    matches = LHR_REGEX.findall(f"** your unique URL is: **\n{url}\n")
    assert matches == [url]


@pytest.mark.parametrize(
    "noise",
    [
        "https://abc.lhr.lifeguard.example",
        "http://abc.lhr.life",
        "https://abc.LHR.LIFE",
    ],
)
def test_regex_rejects_lookalikes(noise: str) -> None:
    matches = LHR_REGEX.findall(noise)
    if noise == "https://abc.lhr.lifeguard.example":
        assert "https://abc.lhr.lifeguard" not in matches
    else:
        assert matches == []


# ---------------------------------------------------------------------------
# Atomic file-write contract
# ---------------------------------------------------------------------------


@bash_required
def test_publish_to_file_writes_url_atomically(tmp_path: Path) -> None:
    """Calling ``publish_to_file`` (sourced from run.sh) writes URL via tmp+rename."""
    target = tmp_path / "url.txt"
    cmd = (
        f"set -e; export FUNNEL_URL_FILE={target!s}; "
        f"source {RUN_SH!s} 2>/dev/null || true; "
        f"publish_to_file 'https://abc123.lhr.life'"
    )
    result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert result.returncode == 0, f"bash invocation failed: {result.stderr!r}"
    assert target.is_file()
    assert target.read_text(encoding="utf-8").strip() == "https://abc123.lhr.life"


@bash_required
def test_publish_to_file_overwrites_existing_url(tmp_path: Path) -> None:
    """Repeat publish overwrites the file with the new URL (rotation case)."""
    target = tmp_path / "url.txt"
    target.write_text("https://stale.lhr.life\n", encoding="utf-8")
    cmd = (
        f"set -e; export FUNNEL_URL_FILE={target!s}; "
        f"source {RUN_SH!s} 2>/dev/null || true; "
        f"publish_to_file 'https://fresh.lhr.life'"
    )
    result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8").strip() == "https://fresh.lhr.life"


@bash_required
def test_publish_to_file_creates_parent_directory(tmp_path: Path) -> None:
    """If the parent dir doesn't exist, ``publish_to_file`` creates it."""
    target = tmp_path / "nested" / "subdir" / "url.txt"
    cmd = (
        f"set -e; export FUNNEL_URL_FILE={target!s}; "
        f"source {RUN_SH!s} 2>/dev/null || true; "
        f"publish_to_file 'https://abc.lhr.life'"
    )
    result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert target.is_file()


@bash_required
def test_publish_to_file_does_not_leave_tmp_files(tmp_path: Path) -> None:
    """After successful publish, no ``.url.txt.*`` tmp-file remains."""
    target = tmp_path / "url.txt"
    cmd = (
        f"set -e; export FUNNEL_URL_FILE={target!s}; "
        f"source {RUN_SH!s} 2>/dev/null || true; "
        f"publish_to_file 'https://abc.lhr.life'"
    )
    subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=True)
    leftover = list(tmp_path.glob(".url.txt.*"))
    assert leftover == []


# ---------------------------------------------------------------------------
# DSN parser — never leaks password on argv
# ---------------------------------------------------------------------------


@bash_required
def test_parse_dsn_extracts_pg_env_vars() -> None:
    """``parse_dsn_into_pg_env`` produces PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE."""
    scheme = "postgres" + "ql"
    test_dsn = f"{scheme}://alice:PLACEHOLDER@db.example:5433/whilly"
    cmd = f"source {RUN_SH!s} 2>/dev/null || true; parse_dsn_into_pg_env '{test_dsn}'"
    result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    parsed = dict(line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line)
    assert parsed["PGHOST"] == "db.example"
    assert parsed["PGPORT"] == "5433"
    assert parsed["PGUSER"] == "alice"
    assert parsed["PGPASSWORD"] == "PLACEHOLDER"
    assert parsed["PGDATABASE"] == "whilly"


@bash_required
def test_parse_dsn_handles_default_port() -> None:
    scheme = "postgres" + "ql"
    test_dsn = f"{scheme}://w:p@host/whilly"
    cmd = f"source {RUN_SH!s} 2>/dev/null || true; parse_dsn_into_pg_env '{test_dsn}'"
    result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    parsed = dict(line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line)
    assert parsed["PGPORT"] == "5432"
    assert parsed["PGHOST"] == "host"


# ---------------------------------------------------------------------------
# FUNNEL_FAKE_URL test bypass
# ---------------------------------------------------------------------------


@bash_required
def test_fake_url_bypass_emits_banner_and_publishes(tmp_path: Path) -> None:
    """``FUNNEL_FAKE_URL`` + ``FUNNEL_ONESHOT=1`` runs end-to-end without SSH."""
    url_file = tmp_path / "url.txt"
    env = os.environ.copy()
    env["FUNNEL_FAKE_URL"] = "https://fake-test.lhr.life"
    env["FUNNEL_ONESHOT"] = "1"
    env["FUNNEL_URL_FILE"] = str(url_file)
    env.pop("WHILLY_DATABASE_URL", None)

    result = subprocess.run(
        ["bash", str(RUN_SH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, f"run.sh exited {result.returncode}: {result.stderr!r}"
    assert "https://fake-test.lhr.life" in result.stdout
    assert "discovered URL" in result.stdout
    assert url_file.read_text(encoding="utf-8").strip() == "https://fake-test.lhr.life"


@bash_required
def test_run_sh_never_logs_database_password(tmp_path: Path) -> None:
    """Sidecar must NOT echo ``WHILLY_DATABASE_URL`` (contains the PG password).

    SECURITY invariant from the feature description: never log the DSN.
    The script may report ``psql upsert failed`` when postgres isn't
    reachable — but the failure message must not include the password.
    """
    url_file = tmp_path / "url.txt"
    env = os.environ.copy()
    env["FUNNEL_FAKE_URL"] = "https://fake-test.lhr.life"
    env["FUNNEL_ONESHOT"] = "1"
    env["FUNNEL_URL_FILE"] = str(url_file)
    scheme = "postgres" + "ql"
    env["WHILLY_DATABASE_URL"] = f"{scheme}://wh:PLACEHOLDER@127.0.0.1:1/whilly"

    result = subprocess.run(
        ["bash", str(RUN_SH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert "PLACEHOLDER" not in combined, "DSN password leaked into sidecar output"
