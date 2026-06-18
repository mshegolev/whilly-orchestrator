# Capability: drifted (known-drift validation fixture)

This is a deliberately drifted fixture spec used to validate the semantic
drift-detection engine. Its single normative requirement makes a concrete,
checkable claim that the paired `module.py` plainly violates.

## Requirement: return shape

The `summarize` function SHALL return a JSON object (a Python `dict`
serialized as a JSON object) mapping result fields to their values. It SHALL
NOT return a bare string.

### Scenario: caller receives a structured object

- GIVEN a caller invokes `summarize(...)`
- WHEN the function returns
- THEN the return value is a `dict` (a JSON object), so the caller can index
  fields by key.
