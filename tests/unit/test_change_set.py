"""Pure product change-set contracts; no provider or database."""

from dataclasses import FrozenInstanceError, replace

import pytest

from whilly.swarm.change_set import (
    ChangeSetStatus as C,
    RepoChangeStatus as R,
    Evidence,
    EvidenceOutcome as O,
    ProductChangeSet,
    RepoChange,
    TransitionError,
    canonical_digest,
)

SHA = "a" * 40
DIGEST = "b" * 64
SNAPSHOT = {"projects": {"demo": {"depends_on": []}}}


def proof(kind="verification", outcome=O.PASSED, **kwargs):
    values = dict(kind=kind, outcome=outcome, sha=SHA, job_id="job-1", environment="local")
    values.update(kwargs)
    return Evidence(**values)


def change(status=C.DRAFT, repo_status=R.PLANNED, resume_status=None):
    value = ProductChangeSet.create(
        change_id="change-demo",
        product_id="product-demo",
        goal="Deliver compatible components",
        acceptance_criteria=("Stage chat works",),
        registry_snapshot=SNAPSHOT,
        base_shas={"demo": SHA},
        approval_digest=DIGEST,
    )
    return replace(
        value,
        status=status,
        repo_changes=(replace(value.repo_changes[0], status=repo_status),),
        resume_status=resume_status,
    )


FORWARD = [
    C.DRAFT,
    C.PLANNED,
    C.EXECUTING,
    C.VERIFYING_REPOS,
    C.VERIFYING_INTEGRATION,
    C.READY_TO_MERGE,
    C.MERGING,
    C.MERGED,
    C.DEPLOYING_STAGE,
    C.ACCEPTING_STAGE,
    C.DONE,
]
PRE_MERGE = FORWARD[:6]
ALLOWED = (
    list(zip(FORWARD, FORWARD[1:]))
    + [(state, target) for state in PRE_MERGE for target in (C.BLOCKED, C.FAILED, C.MANUAL_DECISION_REQUIRED)]
    + [(C.MERGING, C.FAILED), (C.MERGING, C.PARTIAL_MERGE)]
    + [(state, C.ROLLING_BACK) for state in (C.MERGED, C.DEPLOYING_STAGE, C.ACCEPTING_STAGE, C.PARTIAL_MERGE)]
    + [(C.ROLLING_BACK, C.ROLLED_BACK), (C.ROLLING_BACK, C.ROLLBACK_FAILED), (C.ROLLBACK_FAILED, C.ROLLING_BACK)]
)


@pytest.mark.parametrize("source,target", ALLOWED)
def test_allowed_change_set_transition_preserves_definition_and_increments_version(source, target):
    failed = target in {C.BLOCKED, C.FAILED, C.MANUAL_DECISION_REQUIRED, C.PARTIAL_MERGE, C.ROLLBACK_FAILED}
    evidence = proof(outcome=O.FAILED if failed else O.PASSED)
    repo_status = R.ARTIFACT_READY if source in FORWARD[7:] else R.PLANNED
    if target in {C.PARTIAL_MERGE, C.MERGED}:
        repo_status = R.MERGED
    if target == C.ROLLED_BACK:
        repo_status = R.REVERTED
    if target == C.DONE:
        evidence = proof("stage_acceptance", environment="stage")
    result = change(source, repo_status).transition(target, evidence)
    assert result.status == target and result.version == 2
    assert result.change_id == "change-demo" and result.registry_digest == canonical_digest(SNAPSHOT)
    assert result.last_evidence == evidence


@pytest.mark.parametrize(
    "source,target",
    [
        (s, t)
        for s in C
        for t in C
        if (s, t) not in ALLOWED
        and s
        not in {
            C.BLOCKED,
            C.MANUAL_DECISION_REQUIRED,
        }
    ],
)
def test_unlisted_change_set_transitions_fail(source, target):
    with pytest.raises(TransitionError):
        change(source).transition(target, proof("stage_acceptance", environment="stage"))


@pytest.mark.parametrize("blocked", [C.BLOCKED, C.MANUAL_DECISION_REQUIRED])
@pytest.mark.parametrize("prior", PRE_MERGE)
def test_blocked_transition_can_only_resume_its_recorded_boundary(blocked, prior):
    value = change(blocked, resume_status=prior)
    assert value.transition(prior, proof()).status == prior
    for wrong in set(C) - {prior}:
        with pytest.raises(TransitionError):
            value.transition(wrong, proof())


@pytest.mark.parametrize(
    "evidence",
    [
        proof(),
        proof("stage_acceptance"),
        proof("stage_acceptance", outcome=O.FAILED, environment="stage"),
        Evidence("stage_acceptance", O.UNAVAILABLE, absence="stage_acceptance_not_run", environment="stage"),
    ],
)
def test_done_requires_passed_stage_acceptance(evidence):
    with pytest.raises(TransitionError):
        change(C.ACCEPTING_STAGE, R.ARTIFACT_READY).transition(C.DONE, evidence)


def test_done_and_merged_require_all_mandatory_repo_parts():
    with pytest.raises(TransitionError):
        change(C.ACCEPTING_STAGE, R.MERGED).transition(C.DONE, proof("stage_acceptance", environment="stage"))
    with pytest.raises(TransitionError):
        change(C.MERGING, R.READY_TO_MERGE).transition(C.MERGED, proof())
    with pytest.raises(TransitionError):
        change(C.MERGING, R.MERGED).transition(C.FAILED, proof(outcome=O.FAILED))


def test_red_stage_probe_can_start_compensation_but_not_acknowledge_unfinished_reverts():
    failed = proof("stage_acceptance", outcome=O.FAILED, environment="stage")
    assert change(C.ACCEPTING_STAGE, R.ARTIFACT_READY).transition(C.ROLLING_BACK, failed).status == C.ROLLING_BACK
    with pytest.raises(TransitionError):
        change(C.ROLLING_BACK, R.REVERT_OPEN).transition(C.ROLLED_BACK, proof("compensation"))


def test_noncanonical_definitions_and_mismatched_repo_baselines_are_rejected():
    with pytest.raises(ValueError):
        ProductChangeSet.create(
            change_id="c",
            product_id="p",
            goal="g",
            acceptance_criteria="criterion",
            registry_snapshot=SNAPSHOT,
            base_shas={"demo": SHA},
        )
    value = change()
    with pytest.raises(ValueError):
        replace(value, repo_changes=(RepoChange("demo", "c" * 40),))


def test_immutable_models_reject_mutable_record_and_evidence_fields():
    value = change()
    with pytest.raises(ValueError):
        replace(value, repo_changes=list(value.repo_changes))
    with pytest.raises(ValueError):
        proof(environment=["stage"])
    with pytest.raises(ValueError):
        replace(value.repo_changes[0], last_evidence={"kind": "mutable"})


def test_execution_requires_a_recorded_approval_digest():
    value = replace(change(C.PLANNED), approval_digest=None)
    with pytest.raises(TransitionError):
        value.transition(C.EXECUTING, proof())
    admitted = value.transition(C.EXECUTING, proof("approval", details={"approval_digest": DIGEST}))
    assert admitted.approval_digest == DIGEST
    with pytest.raises(TransitionError):
        change(C.PLANNED).transition(C.EXECUTING, proof("approval", details={"approval_digest": "c" * 64}))


@pytest.mark.parametrize(
    "prior",
    [R.PLANNED, R.WORKTREE_READY, R.IMPLEMENTING, R.LOCAL_VERIFIED, R.MR_OPEN, R.PIPELINE_GREEN, R.READY_TO_MERGE],
)
def test_blocked_repo_preserves_its_resume_boundary(prior):
    blocked = RepoChange("demo", SHA, status=prior).transition(R.BLOCKED, proof(outcome=O.FAILED))
    assert blocked.transition(prior, proof()).status == prior
    with pytest.raises(TransitionError):
        blocked.transition(R.ARTIFACT_READY, proof())


REPO_FORWARD = [
    R.PLANNED,
    R.WORKTREE_READY,
    R.IMPLEMENTING,
    R.LOCAL_VERIFIED,
    R.MR_OPEN,
    R.PIPELINE_GREEN,
    R.READY_TO_MERGE,
    R.MERGED,
    R.ARTIFACT_READY,
]
REPO_ALLOWED = (
    list(zip(REPO_FORWARD, REPO_FORWARD[1:]))
    + [(s, t) for s in REPO_FORWARD[:7] for t in (R.BLOCKED, R.FAILED)]
    + [
        (R.PLANNED, R.NOT_IMPACTED),
        (R.MERGED, R.REVERT_OPEN),
        (R.ARTIFACT_READY, R.REVERT_OPEN),
        (R.REVERT_OPEN, R.REVERTED),
        (R.REVERT_OPEN, R.ROLLBACK_FAILED),
        (R.ROLLBACK_FAILED, R.REVERT_OPEN),
    ]
)


@pytest.mark.parametrize("source,target", REPO_ALLOWED)
def test_repo_allowed_transitions(source, target):
    failed = target in {R.BLOCKED, R.FAILED, R.ROLLBACK_FAILED}
    evidence = proof(
        "impact_graph" if target == R.NOT_IMPACTED else "verification", outcome=O.FAILED if failed else O.PASSED
    )
    result = RepoChange("demo", SHA, status=source).transition(target, evidence)
    assert result.status == target and result.version == 2
    assert result.mandatory is (target != R.NOT_IMPACTED)


@pytest.mark.parametrize("source,target", [(s, t) for s in R for t in R if (s, t) not in REPO_ALLOWED])
def test_repo_unlisted_transitions_fail(source, target):
    with pytest.raises(TransitionError):
        RepoChange("demo", SHA, status=source).transition(target, proof())


def test_evidence_absence_is_named_and_cannot_be_green():
    with pytest.raises(ValueError):
        Evidence("gate", O.PASSED)
    with pytest.raises(ValueError):
        Evidence("gate", O.PASSED, absence="not_run")
    with pytest.raises(ValueError):
        Evidence("gate", O.PASSED, command=("test",), sha=SHA)
    assert Evidence("gate", O.UNAVAILABLE, absence="provider_unavailable").absence == "provider_unavailable"
    with pytest.raises(TransitionError):
        change().transition(C.PLANNED, Evidence("gate", O.UNAVAILABLE, absence="not_run"))


def test_definition_and_evidence_are_deeply_immutable_and_cover_all_registry_projects():
    snapshot = {"projects": {"one": {"depends_on": []}, "two": {"depends_on": ["one"]}}}
    value = ProductChangeSet.create(
        change_id="c",
        product_id="p",
        goal="g",
        acceptance_criteria=("a",),
        registry_snapshot=snapshot,
        base_shas={"one": SHA, "two": SHA},
    )
    snapshot["projects"]["one"]["depends_on"].append("two")
    assert value.registry_snapshot["projects"]["one"]["depends_on"] == ()
    assert {r.repo_id for r in value.repo_changes} == {"one", "two"}
    with pytest.raises(TypeError):
        value.base_shas["one"] = "f" * 40
    with pytest.raises(FrozenInstanceError):
        value.status = C.DONE
    with pytest.raises(ValueError):
        ProductChangeSet.create(
            change_id="c",
            product_id="p",
            goal="g",
            acceptance_criteria=("a",),
            registry_snapshot=snapshot,
            base_shas={"one": SHA},
        )


def test_dependency_cycles_and_unknown_repos_are_rejected():
    for projects in (
        {"one": {"depends_on": ["missing"]}},
        {"one": {"depends_on": ["two"]}, "two": {"depends_on": ["one"]}},
    ):
        with pytest.raises(ValueError):
            ProductChangeSet.create(
                change_id="c",
                product_id="p",
                goal="g",
                acceptance_criteria=("a",),
                registry_snapshot={"projects": projects},
                base_shas={k: SHA for k in projects},
            )
