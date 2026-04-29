"""Version-consistency gate (TASK-033a, PRD release-train invariant).

Three places carry the Whilly version string and must move together on
every release:

* ``whilly/__init__.py::__version__`` — runtime version exposed via
  ``import whilly; whilly.__version__``.
* ``pyproject.toml::project.version`` — build-time version read by
  setuptools and stamped onto the ``whilly-orchestrator`` wheel.
* ``whilly_worker/pyproject.toml::project.version`` — meta-package
  version, plus the pinned ``dependencies = ["whilly-orchestrator[worker]==X.Y.Z"]``
  edge that gates ``pip install whilly-worker`` to a matching
  control-plane build.

A drift between any of these is a release accident waiting to happen:
``pip install whilly-worker`` resolving to an older wheel than the
control plane it's meant to talk to silently breaks the wire protocol
(SC-3). This test makes the invariant a hard CI gate so a forgotten
file in a future bump fails loudly instead of shipping.

Why a runtime test instead of a pre-commit grep
-----------------------------------------------
A grep would catch the obvious case (literal ``3.3.0`` left in one
file), but it can't catch the subtler case: someone adds a *fourth*
place that needs the version (e.g. a Sphinx config) and the grep
misses it because it isn't on the grep's allowlist. Anchoring this in
``__version__`` + parsed pyproject.tomls means the test fails the
moment a new place ships with a wrong value, and the failure points at
the actual mismatching files in the assertion message.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import whilly

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ORCHESTRATOR_PYPROJECT: Path = REPO_ROOT / "pyproject.toml"
WORKER_META_PYPROJECT: Path = REPO_ROOT / "whilly_worker" / "pyproject.toml"


def _load_version(pyproject: Path) -> str:
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]


def _load_worker_dep_pin(pyproject: Path) -> str:
    """Return the version pin in ``whilly_worker``'s dependency on the
    orchestrator wheel — i.e. the ``X.Y.Z`` in
    ``"whilly-orchestrator[worker]==X.Y.Z"``.
    """
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    deps: list[str] = data["project"]["dependencies"]
    [orchestrator_dep] = [d for d in deps if d.startswith("whilly-orchestrator")]
    # Format: "whilly-orchestrator[worker]==4.0.0"
    _, _, version = orchestrator_dep.partition("==")
    assert version, f"missing == pin in {orchestrator_dep!r}"
    return version


def test_version_is_synchronised_across_release_train_files() -> None:
    """All three version strings + the worker→orchestrator pin must agree."""
    runtime = whilly.__version__
    orchestrator = _load_version(ORCHESTRATOR_PYPROJECT)
    worker_meta = _load_version(WORKER_META_PYPROJECT)
    worker_pin = _load_worker_dep_pin(WORKER_META_PYPROJECT)

    expected = {
        "whilly.__version__": runtime,
        "pyproject.toml": orchestrator,
        "whilly_worker/pyproject.toml": worker_meta,
        "whilly_worker/pyproject.toml dependency pin": worker_pin,
    }
    distinct_values = set(expected.values())
    assert len(distinct_values) == 1, (
        "Whilly version drift detected — these files must all share the same version string:\n  "
        + "\n  ".join(f"{key} = {value!r}" for key, value in expected.items())
    )


def test_runtime_version_is_pep440_parseable() -> None:
    """Cheap sanity check: ``__version__`` is a parseable PEP 440 release.

    A typo like ``__version__ = "4.0"`` (missing a segment) still
    type-checks but breaks ``pip``'s version comparison. ``packaging``
    isn't a runtime dep of whilly so we hand-roll a tiny check rather
    than introducing an import.
    """
    parts = whilly.__version__.split(".")
    assert len(parts) == 3, f"expected 3 dotted segments in __version__, got {whilly.__version__!r}"
    for segment in parts:
        assert segment.isdigit(), f"non-numeric segment in __version__: {segment!r} in {whilly.__version__!r}"


def test_python_version_floor_matches_pyproject() -> None:
    """The pyproject ``requires-python`` floor must hold at runtime.

    ``pip install whilly-orchestrator`` on a too-old Python rejects the
    install at metadata-resolution time, but a developer running
    ``pip install -e .`` from a checkout can wedge themselves on a
    pre-3.12 interpreter and only discover it via a confusing
    ``TaskGroup`` ImportError much later. This test trips the moment
    pytest is invoked on a sub-floor interpreter.
    """
    with ORCHESTRATOR_PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    requires = data["project"]["requires-python"]
    # Format examples: ">=3.12", ">=3.10,<4.0".
    assert requires.startswith(">="), f"unexpected requires-python format: {requires!r}"
    floor_str = requires.removeprefix(">=").split(",", 1)[0].strip()
    floor_parts = tuple(int(s) for s in floor_str.split("."))
    assert sys.version_info >= floor_parts, (
        f"running Python {sys.version_info[:3]} but pyproject declares requires-python={requires!r}"
    )
