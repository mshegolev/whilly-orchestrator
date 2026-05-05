"""Unit tests for the M2 PR-feedback env vars wired into :class:`WhillyConfig`.

Covers:

* ``WHILLY_ITERATE_ON_FAILURE`` parses to a bool, defaults False.
* ``WHILLY_PR_FEEDBACK_POLL_INTERVAL`` parses to int, defaults 60.
* ``WHILLY_MAX_REVIEW_ITERATIONS`` parses to int, defaults 3.
* The dataclass exposes attributes for each new env var.
* Setting the env vars at non-default values flows through
  :meth:`WhillyConfig.from_env`.
"""

from __future__ import annotations

import pytest

from whilly.config import WhillyConfig


@pytest.fixture(autouse=True)
def _clear_pr_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any inherited M2 env vars so the unit tests see defaults."""
    for key in (
        "WHILLY_ITERATE_ON_FAILURE",
        "WHILLY_PR_FEEDBACK_POLL_INTERVAL",
        "WHILLY_MAX_REVIEW_ITERATIONS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_dataclass_exposes_new_attributes() -> None:
    cfg = WhillyConfig()
    assert hasattr(cfg, "ITERATE_ON_FAILURE")
    assert hasattr(cfg, "PR_FEEDBACK_POLL_INTERVAL")
    assert hasattr(cfg, "MAX_REVIEW_ITERATIONS")


def test_defaults_match_documented_values() -> None:
    cfg = WhillyConfig()
    assert cfg.ITERATE_ON_FAILURE is False
    assert cfg.PR_FEEDBACK_POLL_INTERVAL == 60
    assert cfg.MAX_REVIEW_ITERATIONS == 3


def test_from_env_picks_up_iterate_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHILLY_ITERATE_ON_FAILURE", "1")
    cfg = WhillyConfig.from_env()
    assert cfg.ITERATE_ON_FAILURE is True


def test_from_env_picks_up_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHILLY_PR_FEEDBACK_POLL_INTERVAL", "120")
    cfg = WhillyConfig.from_env()
    assert cfg.PR_FEEDBACK_POLL_INTERVAL == 120


def test_from_env_picks_up_max_review_iterations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHILLY_MAX_REVIEW_ITERATIONS", "5")
    cfg = WhillyConfig.from_env()
    assert cfg.MAX_REVIEW_ITERATIONS == 5


def test_from_env_only_picks_up_pr_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``from_env_only`` (the env-only loader used by tests bypassing TOML)
    must also expose the new attrs at non-default values."""
    monkeypatch.setenv("WHILLY_ITERATE_ON_FAILURE", "true")
    monkeypatch.setenv("WHILLY_PR_FEEDBACK_POLL_INTERVAL", "30")
    monkeypatch.setenv("WHILLY_MAX_REVIEW_ITERATIONS", "0")
    cfg = WhillyConfig.from_env_only()
    assert cfg.ITERATE_ON_FAILURE is True
    assert cfg.PR_FEEDBACK_POLL_INTERVAL == 30
    assert cfg.MAX_REVIEW_ITERATIONS == 0
