## Why
Subscription-backed CLI users do not need a mandatory nominal USD limit.

## What Changes
- Allow absent/null profile budget_usd while rejecting invalid explicit numbers.
- Preserve explicit model selection and existing runtime/call/concurrency limits.
- Document that Codex does not enforce a per-process USD or turn limit.
- Reject unknown/non-string planner and reviewer engine selections at registry validation.

## Impact
- orchestration-loop; execution profiles and profile validation tests.
