## ADDED Requirements

### Requirement: Optional subscription monetary limit
Execution profiles SHALL accept an absent or null monetary limit while retaining
explicit model selection, runtime timeout and feature/global call admission.
An explicit monetary limit SHALL be positive and finite. Unknown costs SHALL
remain unknown, and unsupported provider caps SHALL not be described as enforced.

#### Scenario: Subscription CLI profile
- **WHEN** the operator configures an explicit model without budget_usd
- **THEN** profile validation succeeds without inventing a dollar amount
- **AND** runtime and call admission limits still apply

#### Scenario: Invalid explicit monetary limit
- **WHEN** budget_usd is zero, negative, nonfinite, boolean or a string
- **THEN** validation rejects the profile before execution

#### Scenario: Invalid agent engine selection
- **WHEN** the configured planner or reviewer engine is not a string naming a configured engine
- **THEN** registry validation rejects it before runtime lookup
