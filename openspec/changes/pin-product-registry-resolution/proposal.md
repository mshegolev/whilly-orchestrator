## Why

Registry identity and approval binding must not omit equivalent remote syntax,
resolved declared state storage, or recognized inline-shell option forms.

## What Changes

- Normalize explicit HTTPS/SSH default ports before canonical remote identity.
- Reject percent-escaped hostname syntax before duplicate checks.
- Bind resolved declared state_dir into stored registry and approval digests.
- Reject combined POSIX command flags and case-insensitive PowerShell inline forms.

## Impact

- Affected specs: orchestration-loop.
- Affected code: product_registry and registry snapshot serialization.
- No network, publication, merge, credentials or external effects are introduced.
