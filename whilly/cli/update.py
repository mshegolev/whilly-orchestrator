"""CLI for Whilly version checks and explicit package updates."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence

from whilly.update import (
    PACKAGE_NAME,
    UPDATE_MODE_ENV,
    UpdateCheckResult,
    UpdateInstallResult,
    UpdateMode,
    check_for_update,
    resolve_update_mode,
    run_package_update,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly update",
        description="Check for newer Whilly releases and run explicit package updates.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("check", help="Check for a newer release without changing the environment.")

    install = subparsers.add_parser("install", help="Manually update the installed Whilly package.")
    install.add_argument("--dry-run", action="store_true", help="Print the package-manager command without running it.")
    install.add_argument(
        "--installer",
        choices=("auto", "pip", "pipx"),
        default="auto",
        help="Package manager to use. auto prefers pipx when running in a pipx context, otherwise pip.",
    )

    auto = subparsers.add_parser("auto", help="Run the explicit automatic update policy once.")
    auto.add_argument(
        "--mode",
        choices=("off", "check", "install"),
        default=None,
        help=f"Override {UPDATE_MODE_ENV}; default is off when neither is set.",
    )
    auto.add_argument("--dry-run", action="store_true", help="Print the update command instead of running it.")
    auto.add_argument(
        "--installer",
        choices=("auto", "pip", "pipx"),
        default="auto",
        help="Package manager to use when policy is install.",
    )
    return parser


def _print_check_result(
    result: UpdateCheckResult,
    *,
    stdout: object,
    stderr: object,
    prefix: str = "",
) -> int:
    if result.error:
        stderr.write(
            f"whilly update: could not check latest version: {result.error}\n"
            f"Update manually with `python -m pip install --upgrade {PACKAGE_NAME}` or retry later.\n"
        )
        stderr.flush()
        return 1

    if result.update_available:
        stdout.write(
            f"{prefix}whilly {result.installed_version} -> {result.latest_version} available.\n"
            "Run `whilly update install` to update manually.\n"
        )
    else:
        stdout.write(f"{prefix}whilly {result.installed_version} is up to date.\n")
    stdout.flush()
    return 0


def _print_install_result(result: UpdateInstallResult, *, stdout: object, stderr: object) -> int:
    command_text = " ".join(result.command) if result.command else "<unsupported>"
    if result.dry_run:
        stdout.write(f"Would run: {command_text}\n")
        stdout.flush()
        return 0

    if result.returncode == 0:
        stdout.write("Whilly update command completed.\n")
        if result.stdout:
            stdout.write(result.stdout)
            if not result.stdout.endswith("\n"):
                stdout.write("\n")
        stdout.flush()
        return 0

    stderr.write(f"whilly update: update command failed ({result.returncode}): {command_text}\n")
    if result.stderr:
        stderr.write(result.stderr)
        if not result.stderr.endswith("\n"):
            stderr.write("\n")
    stderr.flush()
    return result.returncode


def run_update_command(
    argv: Sequence[str],
    *,
    checker: Callable[[], UpdateCheckResult] = check_for_update,
    installer: Callable[..., UpdateInstallResult] = run_package_update,
    environ: dict[str, str] | None = None,
    stdout: object | None = None,
    stderr: object | None = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    env = os.environ if environ is None else environ

    parser = _build_parser()
    args = parser.parse_args(list(argv))
    if args.command is None:
        parser.print_help(out)
        return 0

    if args.command == "check":
        return _print_check_result(checker(), stdout=out, stderr=err)

    if args.command == "install":
        result = installer(dry_run=bool(args.dry_run), installer=args.installer)
        return _print_install_result(result, stdout=out, stderr=err)

    if args.command == "auto":
        mode = resolve_update_mode(env, explicit_mode=args.mode)
        if mode is UpdateMode.OFF:
            out.write(
                f"Automatic updates are off. Set {UPDATE_MODE_ENV}=check or {UPDATE_MODE_ENV}=install, "
                "or pass `whilly update auto --mode check|install`.\n"
            )
            out.flush()
            return 0

        check_result = checker()
        if mode is UpdateMode.CHECK:
            rc = _print_check_result(check_result, stdout=out, stderr=err, prefix="Auto-update check: ")
            if rc == 0 and check_result.update_available:
                out.write(f"Set {UPDATE_MODE_ENV}=install to apply updates automatically.\n")
                out.flush()
            return rc

        check_rc = _print_check_result(check_result, stdout=out, stderr=err, prefix="Auto-update check: ")
        if check_rc != 0 or not check_result.update_available:
            return check_rc
        install_result = installer(dry_run=bool(args.dry_run), installer=args.installer)
        install_rc = _print_install_result(install_result, stdout=out, stderr=err)
        if install_rc == 0:
            out.write(f"Auto-update installed whilly {check_result.latest_version}.\n")
            out.flush()
        return install_rc

    parser.error(f"unknown command: {args.command}")
    return 2
