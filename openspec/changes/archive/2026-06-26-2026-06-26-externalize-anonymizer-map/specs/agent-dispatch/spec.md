## ADDED Requirements

### Requirement: Anonymizer map loaded from environment
The system SHALL load the anonymizer's redaction map from the
`WHILLY_ANONYMIZER_MAP` environment variable (a JSON object string) at
`Anonymizer` construction time via `_load_company_mappings()`, and MUST
default to an empty map — performing no redaction — when the variable is
unset, empty, or contains invalid JSON.  No company name SHALL be hardcoded
in source; all real company names MUST be supplied through local,
gitignored environment configuration.

#### Scenario: Map loaded when env var is set
- **WHEN** `WHILLY_ANONYMIZER_MAP` is set to a valid JSON object string
- **THEN** `Anonymizer().company_mappings` SHALL equal the parsed dict
- **AND** anonymization SHALL replace occurrences of keys with their values

#### Scenario: Empty map when env var is absent
- **WHEN** `WHILLY_ANONYMIZER_MAP` is unset or empty
- **THEN** `Anonymizer().company_mappings` SHALL be an empty dict
- **AND** `anonymize_text` SHALL return the input unchanged with an empty mapping

#### Scenario: Empty map on invalid JSON
- **WHEN** `WHILLY_ANONYMIZER_MAP` contains a string that is not valid JSON
- **THEN** the system SHALL log a WARNING and `Anonymizer().company_mappings` SHALL be an empty dict
