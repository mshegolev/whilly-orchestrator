## MODIFIED Requirements

### Requirement: Global exception hook installation
The system SHALL, via `global_exception_handler`, run analyze-then-apply on an
uncaught exception, and SHALL print the full formatted traceback EXCEPT when
`apply_fix` succeeds — in which case it SHALL print a restart notice and return
early without printing the traceback. `enable_self_healing` SHALL install
`global_exception_handler` as `sys.excepthook`.

#### Scenario: handler prints full traceback when no fix is applied
- **WHEN** `global_exception_handler` is invoked for an uncaught exception and
  `apply_fix` returns `False` (or no `CodeError` is classified)
- **THEN** the system SHALL attempt `analyze_error` and `apply_fix`
- **AND** SHALL print the full formatted traceback afterward

#### Scenario: handler returns early after a successful auto-fix
- **WHEN** `global_exception_handler` is invoked and `apply_fix` returns `True`
- **THEN** the system SHALL print a restart notice and return early
- **AND** SHALL NOT print the full formatted traceback
