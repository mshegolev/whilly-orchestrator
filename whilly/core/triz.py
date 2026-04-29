"""Per-task TRIZ contradiction analyzer for Whilly v4.1 (TASK-104b).

Replaces the v3 plan-level TRIZ analyzer (``whilly/triz_analyzer.py``,
shellouts to Claude with a 300s timeout) with a *per-task* analyzer that
runs as a fail-task hook (TASK-104b).

Public surface
--------------
* :class:`TrizFinding` — frozen dataclass carrying the contradiction
  shape (``contradiction_type`` + ``reason``) returned by the analyzer
  on a positive verdict. Frozen so the value can be round-tripped to
  ``dataclasses.asdict`` for the ``triz.contradiction`` event row's
  ``detail`` JSONB without defensive copies.
* :func:`analyze_contradiction` — given a :class:`~whilly.core.models.Task`,
  spawn the ``claude`` CLI with a per-task TRIZ prompt and a hard
  ``timeout=25`` (HARD constraint: the visibility-timeout window is
  30 s — VAL-TRIZ-006 / VAL-TRIZ-007). Returns the :class:`TrizFinding`
  on a positive verdict, ``None`` otherwise (no contradiction *and*
  every soft-fail mode: claude absent, timeout, malformed JSON, claude
  non-zero exit).

Layering exception (PRD SC-6 / TC-8)
------------------------------------
This module lives under :mod:`whilly.core` and would normally be barred
from importing :mod:`subprocess` / :mod:`shutil` by the
``core-purity`` import-linter contract. The TASK-104b spec mandates the
analyzer's location at ``whilly/core/triz.py`` because the validation
contract pins ``subprocess.run`` calls observable inside this module
(VAL-TRIZ-006). The contract grants this module a documented exception
via ``ignore_imports`` so the rest of :mod:`whilly.core` can stay pure.

Failure modes — fail-open (VAL-TRIZ-015)
----------------------------------------
The analyzer never re-raises into its caller for the documented failure
modes (claude absent, timeout, malformed JSON, claude non-zero exit).
Each failure mode emits **exactly one** WARNING-level log record on the
``whilly.core.triz`` logger (VAL-TRIZ-012) carrying a structured
``record.event`` field drawn from a documented enum (VAL-TRIZ-016):

* :data:`LOG_EVENT_CLAUDE_MISSING` (``"triz.claude_missing"``) — emitted
  when ``shutil.which("claude")`` returns ``None`` *or* the subprocess
  raises :class:`FileNotFoundError`. Distinguishes "operator hasn't
  installed claude" from "claude returned garbage".
* :data:`LOG_EVENT_TIMEOUT` (``"triz.timeout"``) — emitted when the
  subprocess raises :class:`subprocess.TimeoutExpired`. The hook
  (in :mod:`whilly.adapters.db.repository`) writes a separate
  ``triz.error`` event row with ``detail = {"reason": "timeout"}`` to
  surface the timeout to dashboards / post-mortems even though the
  analyzer itself returns ``None``.
* :data:`LOG_EVENT_PARSE_ERROR` (``"triz.parse_error"``) — emitted when
  ``json.loads`` rejects the subprocess stdout, or when the parsed
  document is missing the required ``contradictory`` /
  ``contradiction_type`` / ``reason`` shape.

Public consumers can pin on the exact strings (VAL-TRIZ-016 evidence:
``record.event in {"triz.claude_missing", "triz.timeout",
"triz.parse_error"}``).
"""

from __future__ import annotations

import json
import logging

# fmt: off
import shutil  # noqa: I001 — see module docstring "Layering exception"
import subprocess  # noqa: I001 — see module docstring "Layering exception"
# fmt: on
from dataclasses import dataclass

from whilly.core.models import Task

__all__ = [
    "CLAUDE_BIN",
    "LOG_EVENT_CLAUDE_MISSING",
    "LOG_EVENT_PARSE_ERROR",
    "LOG_EVENT_TIMEOUT",
    "TIMEOUT_SECONDS",
    "TrizFinding",
    "TrizOutcome",
    "analyze_contradiction",
    "analyze_contradiction_with_outcome",
]

logger = logging.getLogger(__name__)


# Hard subprocess timeout (HARD constraint per VAL-TRIZ-006 /
# VAL-TRIZ-007). Must stay strictly below the 30 s claim
# visibility-timeout window so a hung TRIZ run can never make the
# parent worker race the visibility-timeout sweep that would
# re-PENDING the task while the FAIL transition is still in-flight.
TIMEOUT_SECONDS: int = 25


# Default executable name on PATH. Operators can override via
# ``CLAUDE_BIN`` env var in adjacent tooling; this module deliberately
# doesn't read env vars itself so the unit tests have nothing to
# monkeypatch beyond ``subprocess.run`` / ``shutil.which``. The
# repository-layer hook decides whether to call us at all (env var
# ``WHILLY_TRIZ_ENABLED``) — see :class:`whilly.adapters.db.repository
# .TaskRepository.fail_task`.
CLAUDE_BIN: str = "claude"


# Stable enum values for structured warning logs. Pinned by
# VAL-TRIZ-016 / the public log contract. Surfaced as
# ``record.event`` extra on each WARNING record.
LOG_EVENT_CLAUDE_MISSING: str = "triz.claude_missing"
LOG_EVENT_TIMEOUT: str = "triz.timeout"
LOG_EVENT_PARSE_ERROR: str = "triz.parse_error"


# Reason strings used by :class:`TrizOutcome` to communicate the
# specific soft-fail mode upward to the executor hook. The repository
# uses ``"timeout"`` to drive the ``triz.error`` event row write
# (VAL-TRIZ-004); the other two are diagnostic only — no event row.
ERROR_REASON_TIMEOUT: str = "timeout"
ERROR_REASON_CLAUDE_MISSING: str = "claude_missing"
ERROR_REASON_PARSE_ERROR: str = "parse_error"


@dataclass(frozen=True)
class TrizFinding:
    """Pure-data outcome of a positive :func:`analyze_contradiction` verdict.

    Frozen + value-equality so the executor hook can serialise it
    directly into the ``triz.contradiction`` event row's ``detail``
    JSONB column via :func:`dataclasses.asdict`.

    Attributes
    ----------
    contradiction_type:
        Short label describing the contradiction kind, drawn from
        canonical TRIZ vocabulary: ``"technical"`` (one parameter
        improves while another worsens) or ``"physical"`` (object must
        have property X *and* not-X simultaneously). The analyzer
        echoes whatever string Claude returns; downstream consumers
        should not depend on a closed enum since real TRIZ analysis
        often surfaces hybrid labels.
    reason:
        Human-readable summary of the contradiction. The live-smoke
        contract (VAL-TRIZ-013) requires ≥ 20 chars of natural
        language; the analyzer does not enforce a floor itself —
        operators should treat very short reason strings as a hint
        Claude failed to engage with the prompt.
    """

    contradiction_type: str
    reason: str


@dataclass(frozen=True)
class TrizOutcome:
    """Internal richer outcome used by the executor hook.

    The public :func:`analyze_contradiction` collapses everything except
    a positive verdict to ``None`` (per the spec'd ``TrizFinding | None``
    return type). The hook in
    :class:`whilly.adapters.db.repository.TaskRepository` needs more
    detail to decide whether to write a ``triz.error`` event row
    (timeout) versus skip silently (claude absent / parse error). This
    type carries that classification.

    Attributes
    ----------
    finding:
        The :class:`TrizFinding` on a positive verdict; ``None``
        otherwise (no contradiction, OR any soft-fail mode).
    error_reason:
        ``None`` on the success / no-contradiction path. One of
        :data:`ERROR_REASON_TIMEOUT`, :data:`ERROR_REASON_CLAUDE_MISSING`,
        :data:`ERROR_REASON_PARSE_ERROR` on the soft-fail path. The
        hook layer keys on ``ERROR_REASON_TIMEOUT`` to write a
        ``triz.error`` event row (VAL-TRIZ-004) and ignores the other
        two (VAL-TRIZ-003 / VAL-TRIZ-005 — log only, no event row).
    """

    finding: TrizFinding | None
    error_reason: str | None = None


# Master prompt for the per-task TRIZ analyzer. Kept short and
# deliberately structured so Claude's --print mode (no tool use)
# returns a small JSON payload that we can parse with stdlib json.
# The "ONLY JSON" stipulation is critical — anything else triggers the
# parse_error soft-fail mode.
_PROMPT_TEMPLATE = """\
You are a TRIZ (Theory of Inventive Problem Solving) analyst.

Analyse the task below for a *single* technical or physical contradiction
that, if present, would block straightforward implementation.

Task description:
{description}

Acceptance criteria:
{acceptance}

Output ONLY valid JSON, no markdown, no prose. Use this exact shape:
{{
  "contradictory": true | false,
  "contradiction_type": "technical" | "physical" | "" ,
  "reason": "<one or two sentences naming the contradiction; empty when contradictory=false>"
}}

Rules:
- "contradictory": true only when there is a clear, named contradiction.
- "contradiction_type": "" when contradictory=false; otherwise either
  "technical" (one parameter improves while another worsens) or "physical"
  (the object must have a property and its negation simultaneously).
- "reason": >= 20 characters when contradictory=true; "" when
  contradictory=false.
"""


def _build_prompt(task: Task) -> str:
    """Build the per-task TRIZ prompt for the ``claude`` subprocess.

    Pure: depends only on the input :class:`Task`. The prompt is
    structured so a forgetful Claude that drops back to natural prose
    still triggers the parse_error soft-fail mode rather than
    accidentally returning a malformed dict that we'd nominally
    accept.
    """
    desc = (task.description or "").strip() or "(no description)"
    if task.acceptance_criteria:
        acceptance = "\n".join(f"- {ac}" for ac in task.acceptance_criteria)
    else:
        acceptance = "(none)"
    return _PROMPT_TEMPLATE.format(description=desc, acceptance=acceptance)


def _log_warning(event: str, message: str, *args: object) -> None:
    """Emit a single structured WARNING record on the module logger.

    Centralised so every soft-fail mode goes through the same surface:
    one ``logger.warning`` call, one record, ``record.event`` set to
    the documented enum value (VAL-TRIZ-012 / VAL-TRIZ-016).
    """
    logger.warning(message, *args, extra={"event": event})


def _parse_finding(raw: str) -> TrizFinding | None:
    """Parse the ``claude`` subprocess stdout into a :class:`TrizFinding`.

    Returns
    -------
    TrizFinding | None
        - ``TrizFinding`` when the JSON is valid AND
          ``contradictory == True`` AND
          ``contradiction_type`` is a non-empty string AND
          ``reason`` is a non-empty string.
        - ``None`` when the JSON is valid but ``contradictory == False``
          (the no-contradiction happy path; not an error).

    Raises
    ------
    ValueError
        On malformed JSON or on a payload whose shape doesn't match
        the contract. The caller (:func:`_analyze`) translates
        ``ValueError`` into the parse_error soft-fail mode.
    """
    text = raw.strip()
    if not text:
        raise ValueError("empty subprocess stdout")
    # Trim a leading ``json``-fenced block if Claude couldn't help itself.
    if text.startswith("```"):
        # naive fence stripping — covers the ```json ... ``` case
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"json decode error: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError("top-level JSON must be an object")
    contradictory = doc.get("contradictory")
    if contradictory is False:
        return None
    if contradictory is not True:
        raise ValueError("missing or non-boolean 'contradictory' key")
    contradiction_type = doc.get("contradiction_type")
    reason = doc.get("reason")
    if not isinstance(contradiction_type, str) or not contradiction_type:
        raise ValueError("missing or empty 'contradiction_type'")
    if not isinstance(reason, str) or not reason:
        raise ValueError("missing or empty 'reason'")
    return TrizFinding(contradiction_type=contradiction_type, reason=reason)


def _analyze(task: Task) -> TrizOutcome:
    """Subprocess the ``claude`` CLI and classify its outcome.

    Returns a :class:`TrizOutcome` carrying either the parsed finding
    or the soft-fail classification (so the executor hook can decide
    to write a ``triz.error`` event row on timeout).

    Failure modes (each emits exactly one structured WARNING):

    * claude absent — :func:`shutil.which` returns ``None`` or
      :class:`FileNotFoundError` is raised by the subprocess.
    * subprocess :class:`subprocess.TimeoutExpired` after
      :data:`TIMEOUT_SECONDS`.
    * non-zero exit — surfaces as parse_error (the stdout is unlikely
      to contain valid JSON).
    * malformed / shape-mismatched JSON — surfaces as parse_error.
    """
    if shutil.which(CLAUDE_BIN) is None:
        _log_warning(
            LOG_EVENT_CLAUDE_MISSING,
            "TRIZ analyzer: %r CLI not found on PATH; skipping",
            CLAUDE_BIN,
        )
        return TrizOutcome(finding=None, error_reason=ERROR_REASON_CLAUDE_MISSING)

    prompt = _build_prompt(task)
    cmd = [CLAUDE_BIN, "--print", "-p", prompt]
    try:
        # Hard timeout (VAL-TRIZ-006 / VAL-TRIZ-007). ``check=False`` so a
        # non-zero exit lands in our parse_error path — we never want
        # ``CalledProcessError`` propagating into the executor.
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log_warning(
            LOG_EVENT_TIMEOUT,
            "TRIZ analyzer: claude CLI timed out after %ss",
            TIMEOUT_SECONDS,
        )
        return TrizOutcome(finding=None, error_reason=ERROR_REASON_TIMEOUT)
    except FileNotFoundError:
        # ``shutil.which`` already gated this; defensive in case
        # ``claude`` was removed mid-call (unlikely but cheap to guard).
        _log_warning(
            LOG_EVENT_CLAUDE_MISSING,
            "TRIZ analyzer: %r CLI vanished mid-call (FileNotFoundError); skipping",
            CLAUDE_BIN,
        )
        return TrizOutcome(finding=None, error_reason=ERROR_REASON_CLAUDE_MISSING)

    if completed.returncode != 0:
        _log_warning(
            LOG_EVENT_PARSE_ERROR,
            "TRIZ analyzer: claude exited with non-zero code %d (stderr=%r); treating as parse error",
            completed.returncode,
            (completed.stderr or "")[:200],
        )
        return TrizOutcome(finding=None, error_reason=ERROR_REASON_PARSE_ERROR)

    try:
        finding = _parse_finding(completed.stdout or "")
    except ValueError as exc:
        _log_warning(
            LOG_EVENT_PARSE_ERROR,
            "TRIZ analyzer: failed to parse claude output: %s",
            exc,
        )
        return TrizOutcome(finding=None, error_reason=ERROR_REASON_PARSE_ERROR)

    return TrizOutcome(finding=finding, error_reason=None)


def analyze_contradiction_with_outcome(task: Task) -> TrizOutcome:
    """Public richer-outcome variant of :func:`analyze_contradiction`.

    Used by the executor hook in
    :class:`whilly.adapters.db.repository.TaskRepository.fail_task` to
    decide whether to write a ``triz.error`` event row (timeout case).

    Returns
    -------
    TrizOutcome
        See :class:`TrizOutcome` for the field semantics.
    """
    return _analyze(task)


def analyze_contradiction(task: Task) -> TrizFinding | None:
    """Run the per-task TRIZ analyzer.

    Spawns the ``claude`` CLI with a TRIZ-tuned prompt and a hard
    25-second timeout. Returns the :class:`TrizFinding` on a positive
    verdict; ``None`` for every other outcome (no contradiction *and*
    every documented soft-fail mode: claude absent, timeout, malformed
    JSON, claude non-zero exit). Never re-raises (VAL-TRIZ-015).

    Side effects:

    * Subprocesses ``claude`` (one invocation per call).
    * Emits **exactly one** WARNING record on the ``whilly.core.triz``
      logger per soft-fail mode (VAL-TRIZ-012). The record carries a
      ``record.event`` extra drawn from
      ``{"triz.claude_missing", "triz.timeout", "triz.parse_error"}``
      (VAL-TRIZ-016).

    Args
    ----
    task:
        Task to analyse. Only ``description`` and ``acceptance_criteria``
        are read into the prompt; the rest of the dataclass is ignored.

    Returns
    -------
    TrizFinding | None
        ``TrizFinding(contradiction_type, reason)`` on a positive
        verdict; ``None`` otherwise.
    """
    return _analyze(task).finding
