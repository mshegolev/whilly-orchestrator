#syntax=docker/dockerfile:1.6
# Whilly Orchestrator — production image (опубликован в Docker Hub / GHCR).
#
# Этот Dockerfile отличается от Dockerfile.demo:
#   * Не тащит в runtime тесты, fixtures, examples/, README'ы.
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

# ─── Stage 2: runtime ────────────────────────────────────────────────────────
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
RUN npm install -g --omit=dev \
        @anthropic-ai/claude-code \
        @google/gemini-cli \
        opencode-ai \
        @openai/codex \
    && npm cache clean --force \
    && rm -rf /root/.npm

# Sanity build-time: все четыре CLI должны быть в PATH. Падаем здесь, а не
# на runtime в чужом проекте, если npm-пакет переименовали. Дополнительно
# валидируем что `--version` отрабатывает у каждого: gemini-cli на node18
# падал с "Invalid regular expression flags" — именно этот failure mode
# мы поймали бы здесь и заглушили fix, если кто-то откатит Node-bump.
RUN command -v claude && command -v gemini && command -v opencode && command -v codex \
    && claude --version >/dev/null \
    && gemini --version >/dev/null \
    && opencode --version >/dev/null \
    && codex --version >/dev/null \
    && echo "agentic CLIs ready: claude / gemini / opencode / codex"

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
