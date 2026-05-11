"""tmux launch_agent shell-quoting hardening (M1 VAL-SEC-027/028).

Every interpolated field in :func:`whilly.tmux_runner.launch_agent` must
land inside the wrapper string as a shell-quoted literal so a sentinel
value with metacharacters (a) round-trips byte-for-byte through
``shlex.split`` and (b) does not execute as code if the wrapper is ever
replayed through ``subprocess.run(..., shell=True)``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class _FakeBackend:
    """AgentBackend stub that lets tests inject a sentinel into ``model``.

    Mirrors the production backend contract: the prompt MUST be the last
    argv element so :func:`launch_agent` can replace it with the
    ``$(cat …)`` shell substitution.
    """

    name = "fake"

    def build_command(self, prompt, model=None, *, safe_mode=None):
        return ["/opt/fake-bin", "--run", "--model", model or "fake-model", prompt]


def _capture_wrapper(monkeypatch, *, task_id: str, model: str, cwd: Path | None, log_dir: Path) -> str:
    """Run :func:`launch_agent` against a captured-argv tmux stub.

    Returns the wrapper string handed to ``zsh -ic`` so the assertions
    below can replay it through ``shlex.split`` and a real shell to
    verify safety.
    """
    from whilly import tmux_runner

    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        return result

    with (
        patch.object(tmux_runner, "TMUX", "/usr/bin/tmux"),
        patch.object(tmux_runner.subprocess, "run", side_effect=fake_run),
    ):
        tmux_runner.launch_agent(
            task_id=task_id,
            prompt="hi",
            model=model,
            log_dir=log_dir,
            cwd=cwd,
            backend=_FakeBackend(),
        )

    new_session_calls = [c for c in captured if len(c) >= 2 and c[1] == "new-session"]
    assert new_session_calls, "tmux new-session never invoked"
    return new_session_calls[-1][-1]


def test_sentinel_field_value_round_trips_through_shlex(tmp_path: Path) -> None:
    """A sentinel ``model`` value with mixed metacharacters survives in the wrapper.

    VAL-SEC-027: ``shlex.quote`` produces a single shell-quoted literal,
    so ``shlex.split(wrapper)`` returns tokens that contain the original
    byte string verbatim.
    """
    sentinel = "a b'c\"d$e\\`f"
    wrapper = _capture_wrapper(
        pytest.MonkeyPatch(),
        task_id="T1",
        model=sentinel,
        cwd=tmp_path,
        log_dir=tmp_path,
    )

    tokens = shlex.split(wrapper)
    assert sentinel in tokens, f"sentinel not preserved as a single token: {tokens!r}"


def test_canary_payload_does_not_execute(tmp_path: Path) -> None:
    """Replaying the wrapper through ``subprocess.run(..., shell=True)`` with
    a malicious ``model`` value must NOT create the canary file.

    VAL-SEC-028: the wrapper is fed to ``zsh -ic`` in production, which
    is functionally equivalent to ``shell=True`` for the injection
    surface. We isolate the canary path inside ``tmp_path`` so the test
    cannot pollute the host filesystem even if the assertion fails.
    """
    canary = tmp_path / "whilly_pwned"
    payload_model = f'evil"; touch {canary}; #'

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()

    wrapper = _capture_wrapper(
        pytest.MonkeyPatch(),
        task_id="T2",
        model=payload_model,
        cwd=cwd,
        log_dir=log_dir,
    )

    # Strip the agent invocation tail so we don't try to spawn /opt/fake-bin
    # in the test shell — we only need the preamble + cd_prefix path the
    # injection would ride through. The preamble is the first ``;``-separated
    # block; the agent invocation begins after the last ``cd``/``echo``/
    # ``date`` segment. Replay just up to the first prefix-cmd token.
    safe_prefix = wrapper.split("/opt/fake-bin", 1)[0]

    env = os.environ.copy()
    env.pop("BASH_ENV", None)
    result = subprocess.run(
        safe_prefix.rstrip("; ") + "; true",
        shell=True,
        capture_output=True,
        env=env,
        cwd=str(tmp_path),
        timeout=10,
    )

    assert not canary.exists(), (
        f"canary file {canary} was created — wrapper executed injected payload "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )


def test_every_interpolated_field_is_shell_quoted(tmp_path: Path) -> None:
    """Defence-in-depth: a sentinel injected into ``cwd`` and ``model`` survives
    intact, and the raw bytes of the sentinel never appear unquoted in the
    wrapper (i.e. the only occurrence is inside a ``shlex.quote`` literal).
    """
    sentinel = "a b'c\"d$e\\`f"
    safe_cwd = tmp_path / "work"
    safe_cwd.mkdir()

    wrapper = _capture_wrapper(
        pytest.MonkeyPatch(),
        task_id="T3",
        model=sentinel,
        cwd=safe_cwd,
        log_dir=tmp_path,
    )

    # The sentinel must appear in the wrapper as a shlex-quoted literal,
    # which means the literal substring is wrapped in single quotes (or
    # escaped via shlex.quote's `\'` rotation pattern). Either way,
    # shlex.split must surface it as a complete token.
    tokens = shlex.split(wrapper)
    assert sentinel in tokens, f"sentinel not preserved as a token: {tokens!r}"

    # And the wrapper must contain the shlex.quote rendering of the sentinel
    # somewhere — proving that field is being routed through shlex.quote and
    # not raw-interpolated.
    assert shlex.quote(sentinel) in wrapper, "sentinel value not present as a shlex.quote literal in wrapper"


def test_backend_name_with_metacharacter_does_not_execute(tmp_path: Path) -> None:
    """The ``backend.name`` attribute lands in the preamble unquoted today —
    after the M1 hardening it must be ``shlex.quote``-wrapped just like every
    other interpolated field.
    """
    canary = tmp_path / "whilly_backend_pwned"

    class _MaliciousBackend(_FakeBackend):
        name = f'evil"; touch {canary}; #'

    from whilly import tmux_runner

    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        return result

    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    with (
        patch.object(tmux_runner, "TMUX", "/usr/bin/tmux"),
        patch.object(tmux_runner.subprocess, "run", side_effect=fake_run),
    ):
        tmux_runner.launch_agent(
            task_id="T4",
            prompt="hi",
            model="m",
            log_dir=log_dir,
            cwd=None,
            backend=_MaliciousBackend(),
        )

    new_session = [c for c in captured if len(c) >= 2 and c[1] == "new-session"][-1]
    wrapper = new_session[-1]

    safe_prefix = wrapper.split("/opt/fake-bin", 1)[0]
    env = os.environ.copy()
    env.pop("BASH_ENV", None)
    subprocess.run(
        safe_prefix.rstrip("; ") + "; true",
        shell=True,
        capture_output=True,
        env=env,
        cwd=str(tmp_path),
        timeout=10,
    )

    assert not canary.exists(), "canary file created — backend.name metacharacters executed despite shlex.quote"
