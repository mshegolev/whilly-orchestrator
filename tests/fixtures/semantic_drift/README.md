# Known-drift validation fixtures

These fixtures prove the semantic drift-detection engine
(`scripts/semantic_drift_check.py`) is **trustworthy, not merely plausible**
(requirement VALID-01): a deliberately drifted spec/code pair the engine must
flag, plus a clean control pair it must leave alone (no false positive).

> These are **standalone illustrative sources**, not real `whilly/` package
> code. This phase changes **zero `whilly/` behavior** — it only validates the
> existing engine against a known fixture.

## Directory layout

```
tests/fixtures/semantic_drift/
├── drifted/      # planted contradiction — engine must flag HIGH
│   ├── spec.md     capability spec with a concrete SHALL the module violates
│   ├── module.py   implementation that plainly violates the SHALL
│   └── matrix.md   coverage-matrix snippet mapping module.py -> "drifted"
├── clean/        # control — engine must report clean (zero HIGH)
│   ├── spec.md     capability spec whose SHALL matches the module exactly
│   ├── module.py   implementation that satisfies the SHALL
│   └── matrix.md   coverage-matrix snippet mapping module.py -> "clean"
└── README.md
```

Each fixture is **fully self-contained**: its own `spec.md`, `module.py`, and
`matrix.md`. The validation NEVER points at the real `openspec/specs` tree or
`openspec/COVERAGE-MATRIX.md`, so the fixtures cannot silently drift with the
real specs.

## The planted contradiction (drifted)

`drifted/spec.md` states the normative requirement:

> The `summarize` function **SHALL return a JSON object** (a Python `dict`
> serialized as a JSON object) mapping result fields to their values. It SHALL
> NOT return a bare string.

`drifted/module.py` plainly violates it — the function returns a **bare
string** instead of a `dict`. The violating statement is on its own line so
`file:line` evidence is unambiguous:

```python
return f"{field}={value}"  # VIOLATION: returns a bare string, not a JSON object
```

That is the planted drift: **spec says JSON object, code returns a string.**
A competent reviewer should flag this as a HIGH-severity drift (triage
`code-bug`).

## Why the control is clean

`clean/spec.md` states the same SHALL (return a JSON object), and
`clean/module.py` does exactly that — it returns a `dict`:

```python
return {field: value}
```

Spec and code agree, so the engine must report **zero HIGH findings** for the
control. A HIGH here would be a false positive.

## Expected verdict

| Fixture   | Expected engine verdict          | Assertion             |
| --------- | -------------------------------- | --------------------- |
| `drifted` | at least one **HIGH** finding    | `count_high(...) >= 1` |
| `clean`   | **clean** (no HIGH false positive) | `count_high(...) == 0` |

`count_high()` (in `tests/test_semantic_drift_fixture_validation.py`) counts
findings at the top severity `sdc.SEVERITIES[0]` (`"HIGH"`).

## How to reproduce

Two layers validate VALID-01:

### 1. Deterministic plumbing test — always runs, fully offline

A SCRIPTED reviewer (a valid HIGH finding for `drifted`, `[]` for `clean`)
feeds each fixture through the real `review_spec` pipeline and asserts the
harness classifies detected-HIGH vs clean. No network, no Claude CLI.

```bash
python3 -m pytest tests/test_semantic_drift_fixture_validation.py \
  -k "plumbing" -q
```

### 2. Live acceptance canary — runs when `claude` is on PATH

The REAL `claude_reviewer` reviews both fixtures. Skips automatically when
`shutil.which("claude") is None`; runs locally with `claude` installed and in
scheduled CI (which has the key) as a trustworthiness canary. It asserts ONLY
the **severity-level** outcome (`>=1` HIGH drifted, `0` HIGH control) — never
the model's exact wording, requirement string, or precise finding count, which
are non-deterministic.

```bash
python3 -m pytest tests/test_semantic_drift_fixture_validation.py \
  -k "live" -q
```

### `review_spec` invocation params

For a fixture `<kind>` (either `drifted` or `clean`):

| Param         | Value                                              |
| ------------- | -------------------------------------------------- |
| `slug`        | `<kind>` (the fixture directory name == the slug)  |
| `specs_root`  | `tests/fixtures/semantic_drift`                    |
| `repo_root`   | `tests/fixtures/semantic_drift/<kind>`             |
| `matrix_path` | `tests/fixtures/semantic_drift/<kind>/matrix.md`   |

`review_spec` reads the spec from `{specs_root}/{slug}/spec.md`, resolves the
module set from `matrix_path` (here `module.py`), reads that module relative to
`repo_root`, builds the review prompt, and hands it to the reviewer.
