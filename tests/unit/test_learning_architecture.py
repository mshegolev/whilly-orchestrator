"""Executable architecture checks for the learning domain boundary."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


LINT_IMPORTS = str(Path(sys.executable).with_name("lint-imports"))


def test_production_learning_import_contract_passes() -> None:
    result = subprocess.run([LINT_IMPORTS, "--config", ".importlinter"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "swarm learning domain and ports must remain framework-free" in result.stdout


def test_import_contract_fails_for_forbidden_framework_import(tmp_path) -> None:
    package = tmp_path / "isolated_learning"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text("", encoding="utf-8")
    config = tmp_path / ".importlinter"
    config.write_text(
        "[importlinter]\nroot_packages =\n    isolated_learning\ninclude_external_packages = True\n\n"
        "[importlinter:contract:test]\nname = isolated purity\ntype = forbidden\n"
        "source_modules =\n    isolated_learning\nforbidden_modules =\n    fastapi\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [LINT_IMPORTS, "--config", str(config), "--no-cache"],
        cwd=tmp_path,
        env={**__import__("os").environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    init.write_text("import fastapi\n", encoding="utf-8")
    result = subprocess.run(
        [LINT_IMPORTS, "--config", str(config), "--no-cache"],
        cwd=tmp_path,
        env={**__import__("os").environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "isolated purity" in result.stdout + result.stderr
