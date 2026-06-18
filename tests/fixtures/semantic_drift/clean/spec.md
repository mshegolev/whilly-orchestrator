# Capability: clean (control validation fixture)

This is the clean control fixture used to validate the semantic
drift-detection engine reports no false positive. Its single normative
requirement EXACTLY matches the paired `module.py`.

## Requirement: return shape

The `summarize` function SHALL return a JSON object (a Python `dict`
serialized as a JSON object) mapping result fields to their values.

### Scenario: caller receives a structured object

- GIVEN a caller invokes `summarize(...)`
- WHEN the function returns
- THEN the return value is a `dict` (a JSON object), so the caller can index
  fields by key.
