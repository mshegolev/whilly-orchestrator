# LLM Ops

Whilly records LLM runs in two layers:

1. Local artifacts and Postgres events, enabled by default.
2. Optional OpenTelemetry export to Langfuse, Phoenix, or any OTLP/HTTP backend.

The default local layer writes:

- `events`: compact `llm.run_started`, `llm.run_finished`, `llm.run_failed` rows.
- `whilly_logs/tasks/<task-id>/attempt-<n>/prompt.txt`: exact prompt.
- `whilly_logs/tasks/<task-id>/attempt-<n>/raw.jsonl`: native CLI stream.
- `whilly_logs/tasks/<task-id>/attempt-<n>/summary.json`: provider/model/tokens/cost/artifact refs.

Use it with:

```bash
whilly logs --list
whilly logs PAR-001
```

For the Docker demo, the control plane also serves a small UI:

```text
http://127.0.0.1:8000/llm-ops
```

## Slack Demo Notifications

Set either an Incoming Webhook or a Slack bot token before running
`workshop-demo.sh` to post one message per task with a link back to the
LLM Ops UI:

```bash
export WHILLY_SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
export WHILLY_SLACK_NOTIFY_EVENTS=all      # started + terminal
export WHILLY_PUBLIC_BASE_URL=http://127.0.0.1:8000
bash workshop-demo.sh --cli opencode --workers 2 --keep-running
```

Bot-token mode uses Slack `chat.postMessage`. If no channel is set, it
defaults to `C0B1WT58EBE`:

```bash
export SLACK_ACCESS_TOKEN='xoxb-...'
export WHILLY_SLACK_NOTIFY_EVENTS=all
bash workshop-demo.sh --cli opencode --workers 2 --keep-running
```

`WHILLY_SLACK_NOTIFY_EVENTS` accepts `terminal` (default), `started`,
`all`, or `none`. Slack delivery is best-effort: webhook failures are
logged and never change task status. Set `WHILLY_SLACK_ENABLED=0` to
disable both webhook and bot-token demo notifications.

## Langfuse

Install optional tracing dependencies:

```bash
pip install 'whilly-orchestrator[llmops]'
```

Configure export:

```bash
export WHILLY_LLM_OPS_EXPORTERS=langfuse
export LANGFUSE_HOST=http://langfuse:3000
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
```

Whilly sends OTLP/HTTP traces to:

```text
${LANGFUSE_HOST}/api/public/otel/v1/traces
```

Each Whilly task run becomes a `whilly.llm_run` span with:

- `session.id` / `langfuse.session.id`
- `whilly.task.id`, `whilly.plan.id`, `whilly.worker.id`
- `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`
- `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`
- `whilly.tool_use` events parsed from the CLI stream

By default, prompt and completion text are not exported externally. The trace contains file paths. To export prompt/output content into the backend:

```bash
export WHILLY_LLM_OPS_CAPTURE_CONTENT=1
```

## OpenLLMetry

OpenLLMetry/Traceloop is useful when Whilly calls LLMs through Python SDKs or a LiteLLM proxy. Whilly's current worker path launches CLI subprocesses, so the reliable instrumentation point is the Whilly wrapper around the subprocess. That wrapper already emits standard OTel spans; OpenLLMetry can be added later for SDK/proxy paths without changing the task model.

## Phoenix Or Generic OTLP

Phoenix:

```bash
export WHILLY_LLM_OPS_EXPORTERS=phoenix
export PHOENIX_COLLECTOR_ENDPOINT=http://phoenix:6006
export PHOENIX_API_KEY=...
```

Generic OTLP/HTTP collector:

```bash
export WHILLY_LLM_OPS_EXPORTERS=otel
export WHILLY_LLM_OPS_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
export WHILLY_LLM_OPS_OTLP_HEADERS='Authorization=Bearer token'
```

## Notes

Langfuse's current self-hosted stack is more than a single Postgres container for production-scale deployments: use the official Langfuse compose/Helm setup and configure Postgres, ClickHouse, Redis/Valkey, and object storage according to their docs.

References:

- Langfuse OTLP endpoint: https://langfuse.com/integrations/native/opentelemetry
- OpenTelemetry GenAI semantic conventions: https://opentelemetry.io/docs/specs/semconv/gen-ai/
- OpenLLMetry Python SDK: https://docs.traceloop.com/docs/openllmetry/getting-started-python
- Phoenix tracing: https://arize.com/docs/phoenix/get-started/get-started-tracing
