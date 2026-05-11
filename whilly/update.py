"""Version update checks and explicit package update helpers.

This module keeps network and subprocess boundaries injectable so unit tests do
not depend on PyPI or mutate the local development environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.request import urlopen

from whilly import __version__

PACKAGE_NAME = "whilly-orchestrator"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
UPDATE_MODE_ENV = "WHILLY_UPDATE_MODE"

_VERSION_PART_RE = re.compile(r"\d+")


class UpdateMode(str, Enum):
    OFF = "off"
    CHECK = "check"
    INSTALL = "install"


class InstallerKind(str, Enum):
    PIP = "pip"
    PIPX = "pipx"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class UpdateCheckResult:
    installed_version: str
    latest_version: str | None
    update_available: bool | None
    error: str | None = None


@dataclass(frozen=True)
class InstallCommand:
    kind: InstallerKind
    argv: tuple[str, ...]
    guidance: str


@dataclass(frozen=True)
class UpdateInstallResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    dry_run: bool


def _version_key(version: str) -> tuple[int, ...]:
    release = version.strip().lstrip("v").split("+", 1)[0].split("-", 1)[0]
    parts = [int(match.group(0)) for match in _VERSION_PART_RE.finditer(release)]
    return tuple(parts or [0])


def compare_versions(left: str, right: str) -> int:
    """Compare dotted release versions without adding a runtime dependency."""

    left_parts = _version_key(left)
    right_parts = _version_key(right)
    length = max(len(left_parts), len(right_parts))
    left_padded = left_parts + (0,) * (length - len(left_parts))
    right_padded = right_parts + (0,) * (length - len(right_parts))
    if left_padded > right_padded:
        return 1
    if left_padded < right_padded:
        return -1
    return 0


def fetch_latest_version(
    *,
    url: str = PYPI_JSON_URL,
    timeout: float = 5.0,
    opener: Callable[..., Any] = urlopen,
) -> str:
    """Return the latest published package version from the PyPI JSON API."""

    with opener(url, timeout=timeout) as response:
        payload = json.load(response)
    version = payload.get("info", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("PyPI response did not include info.version")
    return version


def check_for_update(
    *,
    installed_version: str = __version__,
    latest_version_loader: Callable[[], str] = fetch_latest_version,
) -> UpdateCheckResult:
    """Check for a newer release without mutating the installation."""

    try:
        latest_version = latest_version_loader()
    except Exception as exc:  # noqa: BLE001 - user-facing diagnostic must capture all boundary failures.
        return UpdateCheckResult(
            installed_version=installed_version,
            latest_version=None,
            update_available=None,
            error=str(exc) or exc.__class__.__name__,
        )
    return UpdateCheckResult(
        installed_version=installed_version,
        latest_version=latest_version,
        update_available=compare_versions(latest_version, installed_version) > 0,
        error=None,
    )


def _normalize_installer(installer: str) -> str:
    return installer.strip().lower().replace("_", "-")


def build_install_command(
    *,
    installer: str = "auto",
    environ: Mapping[str, str] | None = None,
    python_executable: str = sys.executable,
    pipx_executable: str | None = None,
) -> InstallCommand:
    """Build the package-manager command for a manual or automatic update."""

    env = os.environ if environ is None else environ
    pipx = shutil.which("pipx") if pipx_executable is None else pipx_executable
    selected = _normalize_installer(installer)

    if selected == "auto":
        selected = "pipx" if pipx and ("PIPX_HOME" in env or "PIPX_BIN_DIR" in env) else "pip"

    if selected == "pipx":
        if not pipx:
            return InstallCommand(
                kind=InstallerKind.UNSUPPORTED,
                argv=(),
                guidance="pipx was requested but no pipx executable was found. Install pipx or use --installer pip.",
            )
        return InstallCommand(
            kind=InstallerKind.PIPX,
            argv=(pipx, "upgrade", PACKAGE_NAME),
            guidance=f"Run `{pipx} upgrade {PACKAGE_NAME}`.",
        )

    if selected == "pip":
        return InstallCommand(
            kind=InstallerKind.PIP,
            argv=(python_executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME),
            guidance=f"Run `{python_executable} -m pip install --upgrade {PACKAGE_NAME}`.",
        )

    return InstallCommand(
        kind=InstallerKind.UNSUPPORTED,
        argv=(),
        guidance=f"Unsupported installer {installer!r}. Use one of: auto, pip, pipx.",
    )


def run_package_update(
    *,
    dry_run: bool,
    installer: str = "auto",
    environ: Mapping[str, str] | None = None,
    python_executable: str = sys.executable,
    pipx_executable: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> UpdateInstallResult:
    """Run or print the package update command."""

    command = build_install_command(
        installer=installer,
        environ=environ,
        python_executable=python_executable,
        pipx_executable=pipx_executable,
    )
    if command.kind is InstallerKind.UNSUPPORTED:
        return UpdateInstallResult(command.argv, 2, "", command.guidance, dry_run)

    if dry_run:
        return UpdateInstallResult(command.argv, 0, "", "", True)

    try:
        completed = runner(command.argv, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        return UpdateInstallResult(command.argv, 127, "", str(exc), False)
    except OSError as exc:
        return UpdateInstallResult(command.argv, 1, "", str(exc), False)

    return UpdateInstallResult(
        command=command.argv,
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        dry_run=False,
    )


def resolve_update_mode(
    environ: Mapping[str, str] | None = None,
    *,
    explicit_mode: str | None = None,
) -> UpdateMode:
    """Resolve automatic update policy; unknown values fail closed to off."""

    env = os.environ if environ is None else environ
    raw = explicit_mode if explicit_mode is not None else env.get(UPDATE_MODE_ENV, UpdateMode.OFF.value)
    try:
        return UpdateMode(raw.strip().lower())
    except ValueError:
        return UpdateMode.OFF
