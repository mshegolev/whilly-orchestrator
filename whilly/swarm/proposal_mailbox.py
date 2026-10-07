"""Bounded, host-pinned file IPC for observe-and-propose workers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from whilly.adapters.filesystem.swarm_mailbox import MAX_BYTES as _MAX_BYTES
from whilly.adapters.filesystem.swarm_mailbox import MailboxDirectory, MailboxIntegrityError
from whilly.swarm.learning.domain import Principal
from whilly.swarm.learning.proposals import TaskProposal

_REQUEST_NAME = re.compile(r"[a-f0-9]{32}\.json\Z")
_MAX_REQUESTS = 100
_REQUEST_FIELDS = frozenset(
    {
        "op",
        "target_project",
        "target_module",
        "evidence_refs",
        "outcome",
        "contract_impact",
        "acceptance",
        "dependencies",
        "resource_class",
    }
)
_REJECTION = {"status": "rejected", "reason": "invalid_or_unauthorized_proposal"}
MAX_BYTES = _MAX_BYTES


def _string_list(request: dict[str, Any], name: str) -> tuple[str, ...]:
    values = request[name]
    if type(values) is not list or any(type(value) is not str for value in values):
        raise ValueError(f"{name} must be a list of strings")
    return tuple(values)


def _proposal(request: dict[str, Any], *, feature_id: str, task_id: str, stem: str) -> TaskProposal:
    if set(request) != _REQUEST_FIELDS or request.get("op") != "propose":
        raise ValueError("invalid proposal request")
    for name in (
        "target_project",
        "target_module",
        "outcome",
        "contract_impact",
        "resource_class",
    ):
        if type(request[name]) is not str:
            raise ValueError(f"{name} must be a string")
    return TaskProposal(
        id=f"proposal-{task_id}-{stem}",
        origin_feature_id=feature_id,
        origin_task_id=task_id,
        target_project=request["target_project"],
        target_module=request["target_module"],
        evidence_refs=_string_list(request, "evidence_refs"),
        outcome=request["outcome"],
        contract_impact=request["contract_impact"],
        acceptance=_string_list(request, "acceptance"),
        dependencies=_string_list(request, "dependencies"),
        resource_class=request["resource_class"],
    )


async def sync_proposal_mailbox(
    service: Any,
    root: Path,
    *,
    principal: Principal,
    feature_id: str,
    task_id: str,
) -> None:
    """Drain bounded local proposal requests without opening an execution path."""

    status = {
        "mode": "propose_only",
        "blocker": None if service is not None else "proposal_service_required",
        "feature_id": feature_id,
        "task_id": task_id,
    }
    with MailboxDirectory(root) as mailbox:
        mailbox.write_state("status.json", status)
        if service is None:
            return
        for name in mailbox.request_names(_MAX_REQUESTS):
            if mailbox.has_receipt(name):
                if not mailbox.receipt_is_special_rejection(name):
                    mailbox.remove_request(name)
                continue
            try:
                request = json.loads(mailbox.read_request(name))
                if not isinstance(request, dict):
                    raise ValueError("invalid proposal request")
                proposal = _proposal(request, feature_id=feature_id, task_id=task_id, stem=name[:-5])
                result = await service.submit(principal, proposal)
                receipt = result.__dict__ if hasattr(result, "__dict__") else result
            except MailboxIntegrityError:
                rejection = _REJECTION.copy()
                rejection["special_file"] = True
                mailbox.write_receipt(name, rejection)
                continue
            except (ValueError, PermissionError):
                receipt = _REJECTION.copy()
            # Service/DB failures and local receipt failures propagate; the outbox
            # remains available for a stable-ID retry when durability is unknown.
            mailbox.write_receipt(name, receipt)
            mailbox.remove_request(name)
