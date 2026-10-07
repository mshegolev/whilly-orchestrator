## ADDED Requirements

### Requirement: Focus-only keyboard hints
The operator UI SHALL support F to label visible enabled controls, letter sequences to focus without activation, Escape to cancel, and ? for keyboard help while preserving native keyboard behavior outside hint mode.

#### Scenario: Safe hint selection
- **WHEN** an operator enters the label of an execution control
- **THEN** that control receives focus without clicking or sending a request
- **AND** the hint letters do not trigger existing dashboard shortcuts

#### Scenario: Text entry and dynamic pages
- **WHEN** an operator types in an editable control or uses a modifier/composition event
- **THEN** global navigation does not intercept that event
- **AND** navigation remains available after a dashboard refresh
