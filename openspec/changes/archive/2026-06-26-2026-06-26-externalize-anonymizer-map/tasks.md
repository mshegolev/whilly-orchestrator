# Tasks

- [x] Add `_load_company_mappings()` to `whilly/adapters/runner/anonymizer.py`
- [x] Change `company_mappings` default_factory to `_load_company_mappings`
- [x] Remove all hardcoded company names from `anonymizer.py` and `claude_anonymizer_proxy.py`
- [x] Rewrite `tests/test_claude_anonymizer.py` with fictional `Globex` company
- [x] Add `test_default_map_empty_without_env` and `test_map_loaded_from_env`
- [x] Update `docs/anonymizer-usage.md` — fictional examples, env-var docs
- [x] Update `docs/PRD-post-auth-hardening.md` — generic phrasing
- [x] Author ADDED delta for `agent-dispatch`
- [x] `openspec validate 2026-06-26-externalize-anonymizer-map --strict` passes
- [x] `openspec archive 2026-06-26-externalize-anonymizer-map --yes`
