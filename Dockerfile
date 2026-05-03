#syntax=docker/dockerfile:1.6
# Whilly Orchestrator — production image (опубликован в Docker Hub / GHCR).
#
# Этот Dockerfile отличается от Dockerfile.demo:
#   * Не тащит в runtime тесты, README'ы. examples/ + tests/fixtures/fake_claude*.sh нужны для workshop-demo.sh.
#   * Ставит whilly из локального source'а через `pip install '.[server,worker]'`
#     (не [all], не [dev]) — минимальная зависимость для двух ролей: control-plane
#     (FastAPI + asyncpg + alembic) и worker (httpx).
#   * Использует production-вариант alembic.ini с абсолютным путём к миграциям
#     внутри venv'а (избегаем sys.path-коллизий, не дублируем source).
#   * Поддерживает multi-arch (amd64 + arm64) — ставится через buildx в CI.
#
# Build (one-arch, локально):
#   docker build -t whilly:dev .
#
# Build (multi-arch, через buildx — обычно делает CI):
#   docker buildx build --platform linux/amd64,linux/arm64 -t mshegolev/whilly:4.1.0 --push .
#
# Run (control-plane):
#   # WHILLY_DATABASE_URL должен прийти из secrets manager / Docker secret /
#   # Kubernetes secret. Не хардкодьте его в команде / Dockerfile.
#   docker run --rm -p 8000:8000 \
#     --env-file ./secrets.env \
#     mshegolev/whilly:4.1.0 control-plane
#
# Run (worker):
#   docker run --rm \
#     --env-file ./worker-secrets.env \
#     -e WHILLY_CONTROL_URL=https://control.example.com \
#     -e WHILLY_PLAN_ID=my-plan \
#     -v /usr/local/bin/claude:/usr/local/bin/claude:ro \
#     mshegolev/whilly:4.1.0 worker

ARG PYTHON_VERSION=3.12

# ─── Stage 1: builder ────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# build-essential нужен на случай, если для arm64 какая-то зависимость не имеет
# готового wheel'а и собирается из sdist (asyncpg обычно имеет — но mariadb /
# psycopg иногда нет; страховка дешёвая).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Слой кэшируемый: meta-файлы pyproject.toml + minimal whilly/__init__.py
# (нужен setuptools'у чтобы прочитать __version__). Если изменится только
# исходник — этот слой переиспользуется.
COPY pyproject.toml README.md LICENSE ./
COPY whilly/__init__.py ./whilly/__init__.py

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install '.[server,worker]'

# Полный source — теперь устанавливаем whilly без deps (всё уже есть из
# предыдущего шага), не editable. После этого исходники в /build больше не
# нужны — в runtime копируется только venv.
COPY whilly ./whilly
RUN /opt/venv/bin/pip install --no-deps .

# ─── Stage 2: worker-builder (worker-only dep closure) ───────────────────────
# Worker import-path purity (VAL-M1-COMPOSE-012, AGENTS.md "Worker import-path
# purity" invariant). The legacy `runtime` stage (last stage in this file) installs
# `'.[server,worker]'` because it is a multi-role image (control-plane + worker
# dispatched at runtime via /usr/local/bin/whilly-entrypoint). That image's
# `pip list` therefore includes fastapi / asyncpg / sqlalchemy / uvicorn /
# alembic — fine for control-plane, but a violation of the "worker dep closure
# stays minimal" invariant when the worker role is the only one ever invoked.
#
# This stage builds a SEPARATE venv that ONLY contains the worker dep closure:
# `pip install '.[worker]'` brings in httpx + the base deps (rich / pydantic /
# typer / psutil / platformdirs / keyring). It does NOT install the `[server]`
# extra, so fastapi / asyncpg / sse-starlette / jinja2 /
# prometheus-fastapi-instrumentator never land in the venv. The published
# whilly source itself contains modules that import those packages (e.g.
# whilly.adapters.transport.server, whilly.adapters.db.repository) but those
# modules are simply not exercised by the `whilly-worker` entry point — and
# `pip list` only cares about installed *distributions*, not unused Python
# files on disk.
#
# The matching regression test is
# tests/integration/test_worker_image_import_purity.py — it builds this stage
# (`docker build --target worker -t whilly-worker:test .`) and asserts that
# `pip list` shows ZERO matches for fastapi / asyncpg / sse-starlette /
# prometheus-fastapi-instrumentator / jinja2.
FROM python:${PYTHON_VERSION}-slim-bookworm AS worker-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# build-essential as a defensive measure for arm64 sdist fallbacks (matches
# the rationale in stage 1).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY whilly/__init__.py ./whilly/__init__.py

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install '.[worker]'

# Full source — install whilly itself without re-resolving deps. Same shape as
# stage 1's two-step install so the layer cache is invalidated only when the
# whilly/ tree changes, not when an unrelated file in the repo root does.
COPY whilly ./whilly
RUN /opt/venv/bin/pip install --no-deps .


# ─── Stage 3: worker (worker-only runtime image) ─────────────────────────────
# Minimal worker-only runtime image. CMD is `["worker"]` so the same
# whilly-entrypoint dispatcher selects the worker role. Production multi-role
# image (`runtime` stage — the LAST stage in this file, kept last so it is
# the default `docker buildx build .` target) stays the canonical published artefact
# (`mshegolev/whilly:<version>`); this stage is opt-in for operators who want
# a slimmer worker image whose `pip list` is provably free of control-plane
# Python distributions.
#
# Backwards compat: the existing single-host demo (`docker-compose.demo.yml`),
# the published `mshegolev/whilly:4.3.1` image, and the `runtime` stage's
# `["control-plane"]` CMD are all UNCHANGED. This stage is purely additive.
# This stage produces the slim worker image. Build it explicitly via
# `docker buildx build --target worker -t whilly-worker:<tag> .`. The
# default-target image (last stage in this file) is the `runtime`
# (control-plane) image — keep `runtime` last so `docker buildx build .`
# (no --target) produces the canonical control-plane image.
FROM python:${PYTHON_VERSION}-slim-bookworm AS worker

ARG WHILLY_VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    WHILLY_LOG_LEVEL=INFO

# tini — correct PID 1.
# curl — health-poll the control-plane in entrypoint.sh's worker branch.
# ca-certificates — TLS to the control-plane / Anthropic / etc.
# git — `whilly worker connect` may inspect git config in some flows.
# nodejs — Node 22 LTS via NodeSource APT repo, required by `opencode-ai`
#   (the v4.4 default agent CLI per feature m1-opencode-groq-default).
#   See the runtime stage (last stage in this file) for the full rationale (gemini-cli ≥20,
#   Debian bookworm ships node 18 → too old for the modern CLI tooling).
# unzip — opencode-ai's npm postinstall extracts a bundled bun binary.
#
# The `lint-imports` core-purity contract is concerned with *Python*
# distributions in pip's site-packages — adding system-level node + the
# opencode CLI binary on PATH does not violate it (the contract still
# verifies fastapi / asyncpg / etc. are absent from `pip list`). See the
# matching regression test
# tests/integration/test_worker_image_import_purity.py.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         tini curl ca-certificates git unzip gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
    && groupadd --system --gid 1000 whilly \
    && useradd  --system --uid 1000 --gid whilly --create-home --home /home/whilly whilly

# Install opencode CLI (and any other configured agent CLIs) globally —
# `opencode-ai` is the v4.4 default agent CLI in worker containers
# (m1-opencode-groq-default; canonical npm name verified against
# https://opencode.ai/docs/install/). `--omit=dev` keeps the install
# closure minimal; `npm cache clean` + `rm -rf /root/.npm` reclaims the
# layer's tmp.
#
# Configurable via WHILLY_AGENT_CLIS build-arg (added in v4.4.x). Default
# preserves the worker-stage's existing 1-CLI install (zero functional
# regression). Constrained build environments can override the install set
# — same flag as the runtime stage:
#
#   # slim build with only opencode (== current default; explicit form):
#   docker buildx build --target worker \
#       --build-arg WHILLY_AGENT_CLIS='opencode-ai' \
#       -t whilly-worker:slim .
#
#   # skip npm install entirely (BYO binary via volume-mount):
#   docker buildx build --target worker \
#       --build-arg WHILLY_AGENT_CLIS='' \
#       -t whilly-worker:no-clis .
#
#   # add additional CLIs alongside opencode:
#   docker buildx build --target worker \
#       --build-arg WHILLY_AGENT_CLIS='opencode-ai @anthropic-ai/claude-code' \
#       -t whilly-worker:multi .
ARG WHILLY_AGENT_CLIS="opencode-ai"
RUN if [ -n "${WHILLY_AGENT_CLIS}" ]; then \
        npm install -g --omit=dev ${WHILLY_AGENT_CLIS} \
        && npm cache clean --force \
        && rm -rf /root/.npm; \
    else \
        echo "WHILLY_AGENT_CLIS empty — skipping agent CLI npm install"; \
    fi

# Build-time sanity check: when opencode-ai is in the install set the binary
# must be on PATH and `opencode --version` must execute (catches NodeSource /
# npm regressions at image-build time rather than at first agent invocation
# in production). Slim / empty WHILLY_AGENT_CLIS builds skip the check
# cleanly so an operator who deliberately omits opencode is not blocked.
RUN if echo " ${WHILLY_AGENT_CLIS} " | grep -q ' opencode-ai '; then \
        command -v opencode \
        && opencode --version >/dev/null \
        && echo "opencode CLI ready for worker stage"; \
    else \
        echo "opencode-ai not in WHILLY_AGENT_CLIS=${WHILLY_AGENT_CLIS:-<empty>} — skipping opencode sanity check"; \
    fi

# Copy the worker-only venv built above.
COPY --from=worker-builder /opt/venv /opt/venv

# Re-use the same entrypoint dispatcher so this image's worker role behaves
# identically to the runtime image's worker role (connect-flow + legacy paths
# both supported). No alembic.ini / control_plane.py / cli_adapter.py here —
# they would be dead weight in a worker-only image.
COPY docker/entrypoint.sh /usr/local/bin/whilly-entrypoint
RUN chmod +x /usr/local/bin/whilly-entrypoint \
    && chown -R whilly:whilly /home/whilly

LABEL org.opencontainers.image.title="whilly-worker" \
      org.opencontainers.image.description="Whilly v4 — worker-only runtime image (httpx + whilly.core; no fastapi/asyncpg/sse-starlette/jinja2/prometheus)" \
      org.opencontainers.image.version="${WHILLY_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/mshegolev/whilly-orchestrator" \
      org.opencontainers.image.documentation="https://github.com/mshegolev/whilly-orchestrator#readme" \
      org.opencontainers.image.url="https://github.com/mshegolev/whilly-orchestrator" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Mikhail Shchegolev <mshegolev@gmail.com>" \
      org.opencontainers.image.vendor="mshegolev"

WORKDIR /home/whilly
USER whilly

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/whilly-entrypoint"]
CMD ["worker"]


# ─── Stage 4: runtime ────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# Build args для OCI labels — заполняются из workflow'а через --build-arg.
ARG WHILLY_VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    WHILLY_LOG_LEVEL=INFO \
    ALEMBIC_CONFIG=/opt/whilly/alembic.ini

# tini — корректный PID 1 (forwarding SIGTERM, reaping zombies).
# curl — для healthcheck'а и ожидания control-plane'a в worker entrypoint'е.
# ca-certificates — TLS-связь с PyPI / Anthropic API / etc.
# nodejs/npm/git/unzip нужны для agentic CLI'ев (claude-code / gemini-cli /
# opencode) и их runtime-зависимостей (git для diff/commit, unzip для
# opencode'овского postinstall script'а).
#
# Node 22 LTS via NodeSource APT repo (v4.3.1 hotfix): Debian bookworm ships
# nodejs 18, but @google/gemini-cli requires Node ≥20 (uses regex flags
# unsupported by V8 в node18 → "Invalid regular expression flags"). Node 22
# LTS satisfies all three CLIs (claude-code ≥18, gemini ≥20, opencode ships
# its own bundled bun binary). NodeSource pkg ships npm, поэтому убрали `npm`
# из apt list. `gnupg` нужен только для setup_22.x (он ставит keyring); чистим
# через `apt-get purge --auto-remove` после.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         tini curl ca-certificates git unzip gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
    && groupadd --system --gid 1000 whilly \
    && useradd  --system --uid 1000 --gid whilly --create-home --home /home/whilly whilly

# ─── Agentic CLI tools (опционально, но включены в production image) ─────
# Four production-ready coding agents shipped в образ:
#
# 1) @anthropic-ai/claude-code — Anthropic's official CLI. Whilly изначально
#    под него заточен (см. whilly/adapters/runner/claude_cli.py); whilly's
#    argv совпадает 1-в-1, native output уже в Whilly-shape envelope.
#    Sub-agents, skills (~/.claude/skills), MCP servers, hooks.
#    Авторизация: ANTHROPIC_API_KEY env или `claude login` (OAuth).
#
# 2) @google/gemini-cli — Google's official CLI с free-tier (1500 req/day
#    на gemini-2.0-flash). Sub-agents, skills, MCP, file-tools, code search.
#    Headless: `gemini -p "<prompt>" --output-format json --model X`.
#    Авторизация: GEMINI_API_KEY env (https://aistudio.google.com/apikey).
#
# 3) opencode-ai — open source agentic CLI поддерживающий ЛЮБЫХ providers
#    (Anthropic, OpenAI, Groq, OpenRouter, Cerebras, Ollama, Gemini etc.)
#    через models.dev. Sub-agents, skills (читает .claude/skills для
#    совместимости с claude-code), MCP, ACP (Agent Client Protocol).
#    Headless: `opencode run --format json --model provider/model "..."`.
#    Авторизация: per-provider env (OPENROUTER_API_KEY / ANTHROPIC_API_KEY
#    / GROQ_API_KEY / etc) — opencode сам разберётся.
#
# 4) @openai/codex — OpenAI's official Codex CLI (gpt-5.x семейство).
#    Sub-agents, skills, MCP, plugins, AGENTS.md, hooks, sandbox modes.
#    Headless: `codex exec --json -o <file> -m <model> "<prompt>"`.
#    Авторизация: OPENAI_API_KEY env или `codex login` (ChatGPT OAuth для
#    gpt-5.5; gpt-5.4/mini работают через API key).
#
# Whilly-worker зовёт один из них через CLAUDE_BIN+WHILLY_CLI (см.
# docker/cli_adapter.py). Если установка увеличит размер образа сверх
# приемлемого — можно будет вынести в отдельный target `whilly:agents`.
#
# Configurable via WHILLY_AGENT_CLIS build-arg (added in v4.4.x). Default
# preserves the historical 4-CLI install (zero functional regression for
# default builds). Pass a different (smaller) space-separated package list
# to slim the image for constrained build environments — e.g. Colima VM
# with limited disk:
#
#   docker buildx build \
#       --build-arg WHILLY_AGENT_CLIS='opencode-ai' \
#       -t whilly:slim .
#
# Or pass an empty string to skip the npm-install layer entirely (operator
# BYOs the CLI binary via volume-mount or follow-on RUN layer):
#
#   docker buildx build --build-arg WHILLY_AGENT_CLIS='' -t whilly:no-clis .
ARG WHILLY_AGENT_CLIS="@anthropic-ai/claude-code @google/gemini-cli opencode-ai @openai/codex"
RUN if [ -n "${WHILLY_AGENT_CLIS}" ]; then \
        npm install -g --omit=dev ${WHILLY_AGENT_CLIS} \
        && npm cache clean --force \
        && rm -rf /root/.npm; \
    else \
        echo "WHILLY_AGENT_CLIS empty — skipping agent CLI npm install"; \
    fi

# Sanity build-time: все CLI из WHILLY_AGENT_CLIS должны быть в PATH (или
# их npm-имена должны соответствовать установленным бинарям). Падаем здесь,
# а не на runtime в чужом проекте, если npm-пакет переименовали.
# Дополнительно валидируем что `--version` отрабатывает у каждого присутствующего:
# gemini-cli на node18 падал с "Invalid regular expression flags" — именно
# этот failure mode мы поймали бы здесь и заглушили fix, если кто-то откатит
# Node-bump.
#
# The check is conditional on each CLI's npm package being in the install set
# so slim / empty WHILLY_AGENT_CLIS builds don't fail spuriously.
RUN if echo " ${WHILLY_AGENT_CLIS} " | grep -q ' @anthropic-ai/claude-code '; then \
        command -v claude && claude --version >/dev/null && echo "claude CLI ready"; \
    fi \
    && if echo " ${WHILLY_AGENT_CLIS} " | grep -q ' @google/gemini-cli '; then \
        command -v gemini && gemini --version >/dev/null && echo "gemini CLI ready"; \
    fi \
    && if echo " ${WHILLY_AGENT_CLIS} " | grep -q ' opencode-ai '; then \
        command -v opencode && opencode --version >/dev/null && echo "opencode CLI ready"; \
    fi \
    && if echo " ${WHILLY_AGENT_CLIS} " | grep -q ' @openai/codex '; then \
        command -v codex && codex --version >/dev/null && echo "codex CLI ready"; \
    fi \
    && echo "agentic CLIs ready (set: ${WHILLY_AGENT_CLIS:-<none>})"

# Копируем уже установленный venv. Multi-arch это переживает: buildx делает
# отдельный builder-слой для каждой arch, и runtime тоже per-arch — пути
# `/opt/venv/lib/python3.12/site-packages` идентичны на amd64 / arm64.
COPY --from=builder /opt/venv /opt/venv

# alembic.ini для production: абсолютный путь к миграциям внутри venv'а,
# никакого `prepend_sys_path = .` — мы не хотим shadowing'а пакета `whilly`
# через WORKDIR (см. комментарий в самом файле).
COPY docker/alembic.prod.ini /opt/whilly/alembic.ini

# Production launcher для control-plane'а. uvicorn --factory не может
# передать pool в create_app(pool, ...), поэтому открываем asyncpg pool
# здесь и зовём create_app(pool) явно — same shape as integration tests.
COPY docker/control_plane.py /opt/whilly/docker/control_plane.py

# Demo plans consumed by workshop-demo.sh (`whilly plan import
# /opt/whilly/examples/demo/parallel.json`). Without them the published
# multi-role image fails the workshop demo at the plan-import step
# (VAL-M1-COMPOSE-011). The directory is ~10 KB total — negligible.
COPY examples /opt/whilly/examples/

# Fake Claude stubs consumed by workshop-demo.sh's `--cli stub` path
# (Round-6 v4.4.2 publish smoke against VAL-M1-COMPOSE-011 caught the
# regression: workers claim tasks but fail with `claude binary not found
# at /opt/whilly/tests/fixtures/fake_claude_demo.sh`). Dockerfile.demo
# already ships these via the same paths; the production runtime image
# now mirrors that contract so `WHILLY_IMAGE_TAG=<ver> bash
# workshop-demo.sh --cli stub` drains its 5 demo tasks end-to-end.
# Combined size is ~3 KB — negligible.
COPY tests/fixtures/fake_claude.sh /opt/whilly/tests/fixtures/fake_claude.sh
COPY tests/fixtures/fake_claude_demo.sh /opt/whilly/tests/fixtures/fake_claude_demo.sh

# Adapter + raw shim + cgroup-aware model picker для agentic CLI workflow:
#   - cli_adapter.py: транслирует whilly's argv в native argv каждого CLI
#     (claude-code/opencode/gemini), парсит native output → whilly envelope.
#   - llm_shim.py: raw OpenAI-compatible API call (без agentic capabilities).
#     Drop-in замена CLAUDE_BIN для случая «нужно быстро + дёшево + без
#     файловых операций».
#   - llm_resource_picker.py: подбирает модель под cgroup-лимиты контейнера.
#     Используется обоими режимами (shim + adapter).
COPY docker/cli_adapter.py /opt/whilly/docker/cli_adapter.py
COPY docker/llm_shim.py /opt/whilly/docker/llm_shim.py
COPY docker/llm_resource_picker.py /opt/whilly/docker/llm_resource_picker.py

# Точка входа — диспатчер ролей (control-plane / worker / migrate / shell).
COPY docker/entrypoint.sh /usr/local/bin/whilly-entrypoint
RUN chmod +x /usr/local/bin/whilly-entrypoint \
    /opt/whilly/docker/cli_adapter.py \
    /opt/whilly/docker/llm_shim.py \
    /opt/whilly/docker/llm_resource_picker.py \
    /opt/whilly/tests/fixtures/fake_claude.sh \
    /opt/whilly/tests/fixtures/fake_claude_demo.sh \
    && chown -R whilly:whilly /opt/whilly /home/whilly

# OCI labels — Docker Hub и GHCR показывают их на странице тэгов;
# `org.opencontainers.image.source` связывает образ с git-репо в GHCR.
LABEL org.opencontainers.image.title="whilly-orchestrator" \
      org.opencontainers.image.description="Whilly v4 — distributed orchestrator for AI coding agents (Postgres + FastAPI + remote workers)" \
      org.opencontainers.image.version="${WHILLY_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/mshegolev/whilly-orchestrator" \
      org.opencontainers.image.documentation="https://github.com/mshegolev/whilly-orchestrator#readme" \
      org.opencontainers.image.url="https://github.com/mshegolev/whilly-orchestrator" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Mikhail Shchegolev <mshegolev@gmail.com>" \
      org.opencontainers.image.vendor="mshegolev"

WORKDIR /opt/whilly
USER whilly
EXPOSE 8000

# Health check на уровне образа: control-plane отвечает на /health, worker
# тоже отвечает healthy потому что curl не падает на отсутствующем порту 8000
# — так что зашиваем check только под control-plane роль и оставляем NONE для
# worker'а через `docker run --no-healthcheck` либо переопределение в compose.
# Для production-control-plane это и есть основной use case.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -sf http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/whilly-entrypoint"]
CMD ["control-plane"]
