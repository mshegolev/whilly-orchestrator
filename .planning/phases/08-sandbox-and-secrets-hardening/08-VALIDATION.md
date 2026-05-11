---
phase: 08
slug: sandbox-and-secrets-hardening
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-05-08
---

# Phase 08 - Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.3, pytest-asyncio >=0.23 |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`, testpaths=`tests`, asyncio_mode=`auto`) |
| **Quick run command** | `.venv/bin/python -m pytest -q tests/unit/test_secret_lint.py tests/unit/test_runner_env.py tests/unit/test_prompt_sanitizer.py tests/unit/test_claude_subprocess_env.py --maxfail=1` |
| **Full suite command** | `make test` |
| **Estimated runtime** | ~30-180 seconds for focused security suites; full suite varies with Docker availability |

---

## Sampling Rate

- **After every task commit:** Run `.venv/bin/python -m pytest -q tests/unit/test_secret_lint.py tests/unit/test_runner_env.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py --maxfail=1`
- **After every plan wave:** Run `.venv/bin/python -m pytest -q tests/unit/test_prompt_sanitizer.py tests/unit/test_prompt_sanitizer_wiring.py tests/unit/test_verification_runner.py tests/unit/test_claude_subprocess_env.py tests/unit/test_compliance_report.py --maxfail=1`
- **Before `$gsd-verify-work`:** Run `.venv/bin/python -m ruff check whilly/ tests/`, `.venv/bin/python -m ruff format --check whilly/ tests/`, `.venv/bin/lint-imports --config .importlinter`, and `make test` when practical.
- **Max feedback latency:** 180 seconds for focused suites.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 08-01-01 | 01 | 1 | SEC-01 | unit | `.venv/bin/python -m pytest -q tests/unit/test_secret_lint.py tests/unit/test_prompt_sanitizer.py tests/unit/test_prompt_sanitizer_wiring.py --maxfail=1` | W0 needed | pending |
| 08-01-02 | 01 | 1 | SEC-02 | unit | `.venv/bin/python -m pytest -q tests/unit/test_runner_env.py tests/unit/test_claude_subprocess_env.py tests/unit/test_claude_cli.py tests/unit/test_worker_default_deny.py --maxfail=1` | W0 needed | pending |
| 08-01-03 | 01 | 1 | SEC-03 | unit/integration | `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/integration/test_transport_tasks.py --maxfail=1` | Extend existing | pending |
| 08-01-04 | 01 | 1 | SEC-01 SEC-03 | unit | `.venv/bin/python -m pytest -q tests/unit/test_verification_runner.py tests/unit/test_compliance_report.py --maxfail=1` | Extend existing | pending |

*Status: pending, green, red, flaky.*

---

## Wave 0 Requirements

- [ ] `tests/unit/test_secret_lint.py` - covers SEC-01 secret pattern metadata, redaction, mapping scans, excerpts, and no raw secret payloads.
- [ ] `tests/unit/test_runner_env.py` - covers SEC-02 base allowlist, provider-required credentials, proxy layering, and hidden env exclusion.
- [ ] Extend `tests/unit/test_claude_subprocess_env.py` - assert unrelated secrets are absent, not only proxy keys present.
- [ ] Extend `tests/unit/test_local_worker.py` and `tests/unit/test_remote_worker.py` - cover `secret_lint_blocked` pre-run failure and runner-not-called behavior.
- [ ] Extend `tests/integration/test_transport_tasks.py` - cover remote fail prelude acceptance for `secret_lint_blocked`.
- [ ] Extend `tests/unit/test_compliance_report.py` - keep sandbox/VM isolation PARTIAL while evidence mentions env and secret guards.

---

## Manual-Only Verifications

All Phase 8 behaviors have automated verification. Full VM/container isolation is explicitly out of
scope and remains a documented residual risk rather than a manual acceptance target.

---

## Validation Sign-Off

- [x] All tasks have automated verify commands or Wave 0 dependencies.
- [x] Sampling continuity: no 3 consecutive tasks without automated verify.
- [x] Wave 0 covers all missing references.
- [x] No watch-mode flags.
- [x] Feedback latency target under 180 seconds for focused suites.
- [x] `nyquist_compliant: true` set in frontmatter.

**Approval:** approved 2026-05-08 for Phase 8 planning.
