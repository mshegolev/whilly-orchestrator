# Whilly Orchestrator — Helm chart

Deploys Whilly to Kubernetes (corporate platform) from the single multi-role
image. Profile this chart was built for: **image mirrored to Harbor**,
**external managed Postgres**, **Ingress exposes the WUI (control-plane) only**.

## Components

| Object | Role (args) | Notes |
|---|---|---|
| `*-migrate` Job | `migrate` | Helm hook `pre-install,pre-upgrade` — runs `alembic upgrade head` before the control-plane rolls (no multi-replica migration race). |
| `*-control-plane` Deployment + Service | `control-plane` | FastAPI + WUI on `:8000`, `/health` probes. |
| `*-wui` Ingress | — | Routes the WUI host → control-plane Service. |
| `*-worker` Deployment | `worker` | Scale for parallelism; mutex is Postgres `FOR UPDATE SKIP LOCKED`. |
| `*-scheduler` Deployment (optional) | `whilly scheduler run` | Continuous Jira JQL intake → tasks. Enable via `integrations.scheduler.enabled`. |

## Prerequisites

1. **Mirror the image to Harbor** (cluster won't pull Docker Hub):
   ```bash
   docker pull mshegolev/whilly:4.7.0
   docker tag  mshegolev/whilly:4.7.0 <harbor>/<project>/whilly:4.7.0
   docker push <harbor>/<project>/whilly:4.7.0
   ```
2. **A managed Postgres** reachable from the cluster; have its DSN ready.
3. **A Secret** with the DSN + bootstrap token (and integration tokens).
   Recommended: Vault / ExternalSecrets renders it; this chart references it
   via `secrets.existingSecret` (`secrets.create=false`, the default).

   Keys expected in that secret:
   | Key | Used by |
   |---|---|
   | `WHILLY_DATABASE_URL` | all roles |
   | `WHILLY_WORKER_BOOTSTRAP_TOKEN` | control-plane, workers |
   | `JIRA_API_TOKEN` | when `integrations.jira.enabled` |
   | `GITLAB_TOKEN` | when `integrations.gitlab.enabled` |

## Install

```bash
helm upgrade --install whilly deploy/helm/whilly \
  -n whilly --create-namespace \
  --set image.repository=<harbor>/<project>/whilly \
  --set image.tag=4.7.0 \
  --set 'imagePullSecrets[0].name=harbor-pull' \
  --set secrets.existingSecret=whilly-secrets \
  --set worker.planId=demo \
  --set worker.replicas=3 \
  --set ingress.className=nginx \
  --set ingress.hosts[0].host=whilly.<corp-domain>
```

PoC without Vault (renders the secret from values — never commit real values):

```bash
helm upgrade --install whilly deploy/helm/whilly -n whilly --create-namespace \
  --set image.repository=<harbor>/<project>/whilly \
  --set secrets.create=true \
  --set secrets.values.WHILLY_DATABASE_URL='postgresql://whilly:pass@pg:5432/whilly' \
  --set secrets.values.WHILLY_WORKER_BOOTSTRAP_TOKEN="$(openssl rand -hex 32)" \
  --set worker.planId=demo
```

## Jira / GitLab

```bash
helm upgrade --install whilly deploy/helm/whilly -n whilly \
  --reuse-values \
  --set integrations.jira.enabled=true \
  --set integrations.jira.serverUrl=https://jira.example.com \
  --set integrations.jira.username=svc-jira \
  --set integrations.gitlab.enabled=true \
  --set integrations.gitlab.url=https://gitlab.example.com \
  --set integrations.scheduler.enabled=true   # continuous JQL intake
# JIRA_API_TOKEN / GITLAB_TOKEN must exist in the referenced secret.
```

Manage scheduler rules (they live in Postgres `scheduler_rules`):
```bash
kubectl -n whilly exec -it deploy/whilly-control-plane -- whilly scheduler list
```

## Using it

- **WUI**: `https://whilly.<corp-domain>/` (browser).
- **TUI**: two modes:
  - Direct DB (full): `WHILLY_DATABASE_URL=<dsn> whilly tui` (port-forward the managed PG if needed).
  - Read-only over FQDN (no DB reachability required):
    `whilly tui --connect https://whilly.<corp-domain> --token <worker-or-bootstrap-bearer>`
- **Scale**: `kubectl -n whilly scale deploy/whilly-worker --replicas=N`.
- **Into a container**:
  `kubectl -n whilly exec -it deploy/whilly-worker -- /usr/local/bin/whilly-entrypoint shell`.

## Gotchas baked into the chart

- **WHILLY_INSECURE=1** on workers — in-cluster `http://…-control-plane:8000`
  is non-loopback and the worker's URL-scheme guard would reject plain HTTP
  otherwise. TLS terminates at the Ingress.
- **Migrations run as a pre-upgrade hook**, so `controlPlane.replicas > 1`
  is safe.
- Image is **non-root** (`USER whilly`); `podSecurityContext.runAsNonRoot=true`.
