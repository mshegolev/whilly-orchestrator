"""Built-in project-type pipeline presets."""

from __future__ import annotations

from whilly.project_config.models import PipelineStepConfig

PUBLIC_PROJECT_TYPES = frozenset({"python_backend", "etl_pipeline", "documentation", "graphql_api", "generic"})
PROJECT_TYPE_ALIASES = {
    "etl": "etl_pipeline",
    "feature_development": "python_backend",
}
SUPPORTED_PROJECT_TYPES = frozenset((*PUBLIC_PROJECT_TYPES, *PROJECT_TYPE_ALIASES))


def normalize_project_type(project_type: str) -> str:
    """Return the canonical public project type for ``project_type``."""

    kind = project_type.strip().lower()
    return PROJECT_TYPE_ALIASES.get(kind, kind)


def preset_pipeline(project_type: str) -> tuple[PipelineStepConfig, ...]:
    """Return a default pipeline for ``project_type``."""

    kind = normalize_project_type(project_type)
    if kind == "etl_pipeline":
        return _etl_pipeline()
    if kind == "graphql_api":
        return _graphql_pipeline()
    if kind == "python_backend":
        return _python_backend_pipeline()
    if kind == "documentation":
        return _documentation_pipeline()
    if kind == "generic":
        return _generic_pipeline()
    raise ValueError(f"unsupported project_type {project_type!r}; expected one of {sorted(PUBLIC_PROJECT_TYPES)}")


def _etl_pipeline() -> tuple[PipelineStepConfig, ...]:
    return (
        PipelineStepConfig(
            id="collect-release-context",
            kind="intake",
            title="Collect release context from Jira and linked artifacts",
            description="Read Jira root ticket, linked issues, Confluence/deployment links, and repository references.",
            acceptance_criteria=("Release context contains linked issues, external links, and repo/version hints.",),
        ),
        PipelineStepConfig(
            id="build-qa-test-plan",
            kind="qa_test_plan",
            title="Build QA/STLC test plan",
            description="Convert requirements and release artifacts into functional, deployment, regression, and audit cases.",
            depends_on=("collect-release-context",),
            human_gate=True,
            acceptance_criteria=("QA engineer reviews and approves the generated test plan before implementation.",),
        ),
        PipelineStepConfig(
            id="generate-autotests",
            kind="autotest_generation",
            title="Generate or update ETL autotests",
            description="Create focused automated tests in the ETL test monorepo for release requirements.",
            depends_on=("build-qa-test-plan",),
            repo_role="tests",
            acceptance_criteria=("Autotests cover every accepted release requirement.",),
            test_steps=("Run focused generated tests locally where possible.",),
        ),
        PipelineStepConfig(
            id="deploy-stage",
            kind="deployment",
            title="Deploy release to STAGE",
            description="Execute deployment instructions from linked artifacts against the STAGE environment.",
            depends_on=("generate-autotests",),
            repo_role="deployment",
            human_gate=True,
            acceptance_criteria=(
                "Human approval is recorded before STAGE deployment.",
                "Deployed version matches release tag/ref.",
            ),
        ),
        PipelineStepConfig(
            id="run-functional-tests",
            kind="test_execution",
            title="Run new functional release tests",
            description="Run the new automated test cases against STAGE.",
            depends_on=("deploy-stage",),
            repo_role="tests",
            acceptance_criteria=("Functional test results are captured with pass/fail evidence.",),
        ),
        PipelineStepConfig(
            id="run-regression-tests",
            kind="test_execution",
            title="Run ETL regression suite",
            description="Run required regression tests for the release area.",
            depends_on=("run-functional-tests",),
            repo_role="tests",
            acceptance_criteria=("Regression results are captured and linked to the release verification.",),
        ),
        PipelineStepConfig(
            id="audit-results",
            kind="qa_audit",
            title="Audit test results and classify failures",
            description="Decide whether failures are test issues, environment issues, or product defects.",
            depends_on=("run-regression-tests",),
            acceptance_criteria=("Every failure has an owner classification and evidence.",),
        ),
        PipelineStepConfig(
            id="release-decision",
            kind="release_decision",
            title="Move Jira release flow or create defect report",
            description="Close/move Jira tickets on pass, or create Jira bugs and wait for developer fix on fail.",
            depends_on=("audit-results",),
            human_gate=True,
            acceptance_criteria=("Human release decision is recorded before Jira state changes.",),
        ),
    )


def _graphql_pipeline() -> tuple[PipelineStepConfig, ...]:
    return (
        PipelineStepConfig(
            id="collect-api-requirements",
            kind="intake",
            title="Collect GraphQL API requirements",
            description="Read task source, schema links, endpoint/operation requirements, and acceptance criteria.",
            acceptance_criteria=("API behavior requirements and schema sources are explicit.",),
        ),
        PipelineStepConfig(
            id="inspect-schema",
            kind="analysis",
            title="Inspect GraphQL schema and operations",
            description="Map changed schema types, queries, mutations, auth rules, and error contracts.",
            depends_on=("collect-api-requirements",),
            repo_role="code",
            acceptance_criteria=("Changed operations and compatibility risks are listed.",),
        ),
        PipelineStepConfig(
            id="generate-api-autotests",
            kind="autotest_generation",
            title="Generate GraphQL contract and integration tests",
            description="Create tests for queries/mutations, auth, validation, errors, and backwards compatibility.",
            depends_on=("inspect-schema",),
            repo_role="tests",
            acceptance_criteria=("Generated tests cover positive, negative, auth, and compatibility cases.",),
        ),
        PipelineStepConfig(
            id="run-api-tests",
            kind="test_execution",
            title="Run GraphQL API tests",
            description="Run focused contract/integration tests and required regression subset.",
            depends_on=("generate-api-autotests",),
            repo_role="tests",
            acceptance_criteria=("Test output is captured and failures are classified.",),
        ),
        PipelineStepConfig(
            id="human-api-review",
            kind="human_gate",
            title="Human review of API test coverage",
            description="Reviewer verifies generated tests match API requirements before release/PR completion.",
            depends_on=("run-api-tests",),
            human_gate=True,
            acceptance_criteria=("Human approval or requested changes are recorded.",),
        ),
    )


def _python_backend_pipeline() -> tuple[PipelineStepConfig, ...]:
    return (
        PipelineStepConfig(
            id="decompose-feature",
            kind="decomposition",
            title="Decompose feature into implementation tasks",
            description="Turn feature requirements into implementation, test, and release tasks.",
            human_gate=True,
            acceptance_criteria=("Feature decomposition is reviewed before code changes start.",),
        ),
        PipelineStepConfig(
            id="implement-feature",
            kind="development",
            title="Implement feature changes",
            description="Modify application code according to the approved decomposition.",
            depends_on=("decompose-feature",),
            repo_role="code",
            acceptance_criteria=("Implementation satisfies accepted feature requirements.",),
        ),
        PipelineStepConfig(
            id="generate-tests",
            kind="autotest_generation",
            title="Generate or update automated tests",
            description="Add unit, integration, or end-to-end tests for the feature.",
            depends_on=("implement-feature",),
            repo_role="tests",
            acceptance_criteria=("Tests cover the changed feature behavior and regression risks.",),
        ),
        PipelineStepConfig(
            id="run-quality-gates",
            kind="quality_gate",
            title="Run quality gates",
            description="Run lint, typecheck, unit, integration, and configured project checks.",
            depends_on=("generate-tests",),
            acceptance_criteria=("All configured quality gates pass or failures are documented.",),
        ),
        PipelineStepConfig(
            id="review-and-release",
            kind="human_gate",
            title="Human review and release/PR decision",
            description="Request review, address feedback, and decide whether to release or open/update PR.",
            depends_on=("run-quality-gates",),
            human_gate=True,
            acceptance_criteria=("Human approval is recorded before final release or PR transition.",),
        ),
    )


def _documentation_pipeline() -> tuple[PipelineStepConfig, ...]:
    return (
        PipelineStepConfig(
            id="collect-doc-context",
            kind="intake",
            title="Collect documentation context",
            description="Read the requested documentation scope, source references, and audience requirements.",
            acceptance_criteria=("Documentation scope, source material, and target audience are explicit.",),
        ),
        PipelineStepConfig(
            id="draft-docs",
            kind="development",
            title="Draft documentation changes",
            description="Create or update documentation according to the accepted scope.",
            depends_on=("collect-doc-context",),
            repo_role="docs",
            acceptance_criteria=("Draft documentation addresses the requested scope with accurate source references.",),
        ),
        PipelineStepConfig(
            id="verify-docs",
            kind="quality_gate",
            title="Verify documentation quality",
            description="Run configured documentation lint, link, or extractability checks.",
            depends_on=("draft-docs",),
            repo_role="docs",
            acceptance_criteria=("Documentation checks pass or failures are documented with follow-up actions.",),
        ),
        PipelineStepConfig(
            id="human-doc-review",
            kind="human_gate",
            title="Human review of documentation",
            description="Reviewer confirms the documentation is accurate before publication or PR completion.",
            depends_on=("verify-docs",),
            human_gate=True,
            acceptance_criteria=("Human approval or requested changes are recorded.",),
        ),
    )


def _generic_pipeline() -> tuple[PipelineStepConfig, ...]:
    return (
        PipelineStepConfig(
            id="intake",
            kind="intake",
            title="Collect task context",
            acceptance_criteria=("Task context and success criteria are explicit.",),
        ),
        PipelineStepConfig(
            id="execute",
            kind="development",
            title="Execute configured work",
            depends_on=("intake",),
            acceptance_criteria=("Configured work is complete.",),
        ),
        PipelineStepConfig(
            id="verify",
            kind="quality_gate",
            title="Verify result",
            depends_on=("execute",),
            human_gate=True,
            acceptance_criteria=("Result is verified before completion.",),
        ),
    )
