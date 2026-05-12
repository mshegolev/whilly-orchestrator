# Whilly Jira Scheduler

Comprehensive JQL-based scheduler for continuous Jira issue intake with event-driven webhooks and metrics.

## Overview

The scheduler consists of 5 integrated phases:

1. **Interactive TUI** — Single-issue Jira intake with Rich UI
2. **Project Map** — Automatic Jira→Git repository resolution
3. **JQL Scheduler Foundation** — Async polling, deduplication, configuration
4. **Documentation Flow** — Rule documentation and reporting
5. **MCP Registry** — Tool discovery and external integration

## Core Components

### Models (`scheduler/models.py`)

- **SchedulerRule** — Frozen dataclass for rule configuration
  - JQL filter, poll interval, deduplication strategy
  - Plan configuration and custom metadata
  
- **SchedulerPollCycle** — Mutable dataclass for poll execution records
  - Status, issue counts, timestamps
  - Error tracking and result persistence

### JQL Executor (`scheduler/jql_executor.py`)

Executes JQL queries against Jira REST API v3:

```python
from whilly.scheduler import execute_jql

issues = execute_jql("project = EINVY AND status = Open", max_results=50)
```

Returns issue dictionaries with fields: key, summary, status, assignee, reporter, etc.

### Deduplicator (`scheduler/deduplicator.py`)

SHA256-based deduplication using configurable field sets:

```python
from whilly.scheduler import deduplicate_issues

unique, duplicates = deduplicate_issues(
    issues,
    fields_to_hash=("summary",)
)
```

### Repository Pattern (`scheduler/repository.py`)

Abstract interface with two implementations:

- **InMemorySchedulerRepository** — For testing and development
- **SQLSchedulerRepository** — For PostgreSQL (optional)

```python
repo = InMemorySchedulerRepository()
await repo.create_rule(rule)
await repo.record_poll_cycle(cycle)
```

### Async Worker (`scheduler/worker.py`)

Continuous polling loop with callbacks:

```python
worker = SchedulerWorker(
    rules,
    poll_callback=on_poll_cycle,
    on_issues_found=on_issues_found
)
await worker.run(duration_seconds=3600)
```

### Rate Limiting (`scheduler/rate_limit.py`)

Exponential/linear/fibonacci backoff with retry logic:

```python
limiter = RateLimiter(
    strategy=BackoffStrategy.EXPONENTIAL,
    max_retries=5,
    initial_delay=1.0
)
result = await limiter.call_with_retry(async_function)
```

Poll-specific rate limiter for API compliance:

```python
poll_limiter = PollRateLimiter(
    min_interval_seconds=1.0,
    max_requests_per_minute=60
)
await poll_limiter.wait_until_ready()
```

### Webhooks (`scheduler/webhooks.py`)

Event-driven issue intake via Jira webhooks:

```python
handler = WebhookEventHandler()
handler.register_callback("jira:issue_created", on_issue_created)

await handler.handle_event(jira_webhook_payload)
```

Event matching against JQL rules:

```python
event = JiraWebhookEvent.from_jira_payload(payload)
if event.matches_rule("project = EINVY AND status = Open"):
    # Process event
```

### Metrics (`scheduler/metrics.py`)

Comprehensive metrics collection:

```python
metrics = MetricsCollector()

metric = PollMetrics(
    rule_id="rule-1",
    success=True,
    duration_seconds=1.5,
    issues_found=10,
    issues_unique=8
)
metrics.record_poll(metric)

summary = metrics.get_summary()
metrics.export_json(Path("metrics.json"))
```

### Documentation (`scheduler/docs.py`)

Generate markdown documentation:

```python
docs = SchedulerDocumentation()

rule_md = docs.generate_rule_markdown(rule)
index_md = docs.generate_rules_index(all_rules)
report_md = docs.generate_poll_cycle_report(cycle)
```

## CLI Commands

### Validate Configuration

```bash
whilly scheduler validate scheduler-config.toml
```

Output:
```
✓ Configuration valid (2 rules)
  - rule-1: EINVY Open Bugs [enabled]
  - rule-2: EINVY Features [disabled]
```

### List Rules

```bash
whilly scheduler list scheduler-config.toml --enabled-only
```

### Run Scheduler Worker

```bash
whilly scheduler run scheduler-config.toml --duration 3600 --log-level INFO
```

## Configuration Format

### TOML Example

```toml
[[rules]]
id = "einvy-bugs"
name = "EINVY Open Bugs"
jira_project_key = "EINVY"
jql_filter = "project = EINVY AND type = Bug AND status = Open"
poll_interval_seconds = 300
enabled = true
deduplication_fields = ["summary", "status"]

[[rules]]
id = "einvy-features"
name = "EINVY Feature Requests"
jira_project_key = "EINVY"
jql_filter = "project = EINVY AND type = Feature AND status = Backlog"
poll_interval_seconds = 600
enabled = false
```

### JSON Example

```json
{
  "rules": [
    {
      "id": "rule-1",
      "name": "Rule 1",
      "jira_project_key": "TEST",
      "jql_filter": "project = TEST",
      "poll_interval_seconds": 300,
      "enabled": true
    }
  ]
}
```

## Database Schema

### scheduler_rules

```sql
CREATE TABLE scheduler_rules (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    description TEXT,
    enabled BOOLEAN DEFAULT true,
    jira_project_key VARCHAR NOT NULL,
    jql_filter TEXT NOT NULL,
    poll_interval_seconds INTEGER DEFAULT 300,
    max_results_per_poll INTEGER DEFAULT 50,
    deduplication_fields TEXT,
    plan_config TEXT,
    custom_metadata TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE INDEX idx_scheduler_rules_project_enabled 
ON scheduler_rules(jira_project_key, enabled);
```

### scheduler_poll_cycles

```sql
CREATE TABLE scheduler_poll_cycles (
    id SERIAL PRIMARY KEY,
    rule_id VARCHAR REFERENCES scheduler_rules(id),
    poll_status VARCHAR,
    total_issues_found INTEGER,
    new_issues_created INTEGER,
    duplicate_issues_skipped INTEGER,
    error_message TEXT,
    jql_results TEXT,
    deduplicated_issues TEXT,
    created_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE INDEX idx_scheduler_poll_cycles_rule_created
ON scheduler_poll_cycles(rule_id, created_at);

CREATE INDEX idx_scheduler_poll_cycles_status
ON scheduler_poll_cycles(poll_status);
```

## Test Coverage

**Total:** 32 tests across 3 test modules

### test_scheduler.py (28 tests)
- Models, repositories, deduplication, configuration
- Documentation generation
- Rate limiting and retry logic
- Webhook parsing and event handling
- Metrics collection and aggregation
- MCP registry and profiles

### test_scheduler_integration.py (4 tests)
- End-to-end poll cycles
- Retry with metrics integration
- Webhook event routing with rule matching
- Multi-rule metrics aggregation

All tests: **32/32 PASSED ✅**

## Architecture Patterns

1. **Repository Pattern** — Abstract data persistence
2. **Registry Pattern** — Tool and profile discovery
3. **Dataclass Serialization** — `to_dict()` for persistence
4. **Custom Exceptions** — Domain-specific error handling
5. **Async/Await** — Non-blocking operations
6. **Callback-Based Events** — Pluggable handlers

## Future Enhancements

- [ ] Confluence publishing integration
- [ ] SQL repository with asyncpg
- [ ] Prometheus metrics export
- [ ] Jira event filters (webhooks vs polling)
- [ ] Rule templates and presets
- [ ] Multi-tenant configuration
- [ ] Scheduler dashboard UI

## License

Same as Whilly project.
