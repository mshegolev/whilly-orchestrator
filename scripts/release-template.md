## [X.Y.Z] - YYYY-MM-DD

> **One-paragraph summary of the release.** What changed at a high
> level, what milestone it belongs to (if any), and whether it is
> strictly additive / drop-in for downstream consumers.

### Breaking changes

(none)

<!--
If there ARE breaking changes, replace "(none)" with a bulleted list,
e.g.:

- `whilly worker connect` now requires `--bootstrap-token`; the legacy
  `WHILLY_WORKER_BOOTSTRAP_TOKEN` env-var fallback is removed.
- Database migration 0NN renames `events.detail` → `events.payload`.
  Downstream readers must update before upgrading.
-->

### Upgrade notes

(none — drop-in replacement; see CHANGELOG for cumulative changes.)

<!--
For non-trivial upgrades, list the steps here, e.g.:

- `pip install --upgrade whilly-orchestrator==X.Y.Z` /
  `pip install --upgrade whilly-worker==X.Y.Z` (the worker
  meta-package keeps its `==X.Y.Z` pin to the orchestrator —
  upgrade both in lockstep).
- `docker pull mshegolev/whilly:X.Y.Z` for the multi-arch image.
- Schema migration: `alembic upgrade head` applies 0NN.
- Any new env vars / flags / opt-in switches.
-->

### Added

- New features, services, endpoints, CLI subcommands, env vars.

### Changed

- Behaviour or defaults that shifted (note backwards-compat impact).

### Fixed

- User-visible bug fixes with the symptom + root cause in one line.

### Deprecated

- APIs / flags / env vars marked for removal in a future release.

### Removed

- Items that are gone in this release (rare; usually deferred to a
  major bump).

### Security

- CVEs addressed, hardening steps, dependency bumps for known issues.

---

**Authoring conventions**

- Always include the `### Breaking changes` and `### Upgrade notes`
  subsections, even when the value is `(none)`. Validators
  (`VAL-CROSS-RELEASE-005` / `VAL-CROSS-RELEASE-905`) scan all
  release bodies for these literal headers.
- Keep section headings at H3 (`###`) so they nest under the H2
  release title (`## [X.Y.Z] - YYYY-MM-DD`).
- For PyPI / Docker Hub / multi-arch coordinates, copy the exact
  shape used by the most recent release body so downstream tooling
  (`pip install whilly-orchestrator==X.Y.Z`, `docker pull
  mshegolev/whilly:X.Y.Z`) keeps grepping cleanly.

**Publishing checklist**

1. Draft body from this template; fill the subsections that apply,
   leave the empty ones with `(none)`.
2. `gh release create vX.Y.Z --notes-file <path>` (or
   `gh release edit vX.Y.Z --notes-file <path>` for retroactive
   amendments). Save the rendered body under
   `out/release-amendments/vX.Y.Z-body-<original|amended>.md` for
   audit evidence.
3. Verify both required subsections survived the upload:
   ```bash
   gh release view vX.Y.Z --json body | jq -r .body \
     | rg -qF 'Breaking changes' \
     && gh release view vX.Y.Z --json body | jq -r .body \
        | rg -qF 'Upgrade notes' \
     && echo OK
   ```
