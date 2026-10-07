# Mutation testing pilot

This repository has a scoped mutmut pilot for the pure decision gate evaluator.
It measures whether `tests/unit/core/test_gates.py` detects small behavior
changes in `whilly/core/gates.py`. It is a diagnostic signal, not a merge
threshold; the observed pilot result does not establish a target score.

## Scope and commands

The project-local development extra pins `mutmut==3.8.0`. The TOML settings
mutate only `whilly/core/gates.py` and select only
`tests/unit/core/test_gates.py`. `whilly/core/` is copied into mutmut's
temporary test tree because the gate imports shared core models; only the gate
module itself is eligible for mutation. The generated `mutants/` directory is
ignored by Git.

Use Python 3.12 and the project development extra:

```sh
uv sync --python 3.12 --extra dev
.venv/bin/python -m pytest -q tests/unit/core/test_gates.py
.venv/bin/mutmut run --max-children 1
.venv/bin/mutmut results --all true
```

`--max-children 1` makes the pilot serial. `process_isolation = "fork"` uses
mutmut's documented POSIX process model. The generated state is cached under
`mutants/`; `on_dependency_change = "rerun"` invalidates cached results when
tracked non-Python dependencies such as `pyproject.toml` or `uv.lock` change.

## Baseline and first measured pilot

Measured in a macOS arm64 environment (`Darwin 25.5.0`), Python 3.12.0,
uv 0.11.16, mutmut 3.8.0:

| Command | Result | Wall time |
| --- | --- | ---: |
| `.venv/bin/python -m pytest -q tests/unit/core/test_gates.py` | 18 passed; 1 pre-existing `testcontainers.postgres` deprecation warning | 9.33 s |
| `.venv/bin/mutmut run --max-children 1` | 24 mutants; 20 killed; 4 survived; 6.41 mutations/s | 5.74 s |

The pytest summary reports 0.26 s of test time; 9.33 s is the enclosing wall
time measured by `/usr/bin/time -p`. The initial mutmut attempt exposed that
copying only the target file leaves its `whilly.core.models` import unavailable
in the isolated test tree. Adding `also_copy = ["whilly/core/"]` resolved that
setup issue without widening the mutation target.

## Survivor review

The pilot produced these survivors:

| Mutant | Observed change | Disposition |
| --- | --- | --- |
| `x_evaluate_decision_gate__mutmut_4` | Replace the empty fallback for `task.description` with `"XXXX"` before checking its length. | Equivalent for this evaluator: both empty and four-character values remain below `MIN_DESCRIPTION_LEN`, so the returned missing-field tuple and verdict do not change. |
| `x_evaluate_decision_gate__mutmut_14` | Add `XX` around the fixed reason prefix. | Survives because tests do not freeze exact reason prose. The contract checks a non-empty reason and missing-field labels; the exact wording is explicitly not contractual. |
| `x_evaluate_decision_gate__mutmut_15` | Uppercase the fixed reason prefix. | Same non-contractual presentation survivor as mutant 14. |
| `x_evaluate_decision_gate__mutmut_17` | Change the separator used to join multiple missing-field labels. | Presentation-only under the current contract, which requires the labels but does not prescribe punctuation. If exact log formatting becomes an interface, add an explicit test then. |

These dispositions describe this pilot only. They are not exclusions from
future reports and no score threshold is set. Reassess survivors when the
decision-gate contract or operator-facing reason format changes.

## Continuous integration

The `mutation` job in `.github/workflows/ci.yml` starts only after both the
`lint` and `test` jobs pass. It installs the development extra, including the
explicitly pinned mutmut version, runs the same scoped pilot serially, and
prints all results even when the mutation run fails. The job preserves that
failure status as a hard gate for mutation harness or test failures, while the
reviewed survivors remain diagnostic until measured evidence supports a
repository policy and threshold.
