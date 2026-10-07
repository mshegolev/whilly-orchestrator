## ADDED Requirements

### Requirement: Artifact fences do not interfere with plan parsing
The system SHALL consume labelled artifact fences without mistaking their closing delimiters for unlabelled plan blocks.

#### Scenario: BMAD artifacts precede a canonical JSON plan
- **WHEN** a planner returns a bmad-artifacts block followed by a json plan block
- **THEN** only the canonical JSON plan is parsed for task validation
