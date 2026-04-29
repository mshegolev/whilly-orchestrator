---
title: Workstation bootstrap (machine B)
layout: default
nav_order: 6
description: "Подключить второй физический компьютер (laptop / VM / другой комп) к общему v4.1 Postgres-плану: установка, выбор режима доступа к Postgres, claim задач без коллизий с primary worker."
permalink: /whilly-workstation-bootstrap
---

# Whilly — bootstrap a second machine

> Полный runbook для онбординга вторичной машины (далее — **machine B**) в работающий Whilly v4.1 setup. Цель: заставить B claim'ить задачи из того же Postgres-плана, что и primary workstation (далее — **machine A**), без коллизий, без рассинхрона статусов и без двух разных Claude API-ключей. Эта страница — глубокая версия [`Continuing-On-Another-Machine.md`]({{ site.baseurl }}/continuing-on-another-machine); если ты ищешь TL;DR, читай тот документ.

## TL;DR

```bash
# на machine B, с нуля
git clone git@github.com:mshegolev/whilly-orchestrator.git
cd whilly-orchestrator
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# Postgres reach — выбери ОДИН вариант (см. секцию 4)
ssh -fN -L 5432:127.0.0.1:5432 user@machine-a            # ← простейший: SSH-tunnel
# Username и DB по умолчанию — `whilly` (из docker-compose.yml на machine A).
# Password — твой rotated secret; вынеси в env var, чтобы не светить в shell-history:
read -s PG_PASSWORD && export PG_PASSWORD
export WHILLY_DATABASE_URL=$(printf 'postgresql://%s:%s@%s:%s/%s' whilly "$PG_PASSWORD" 127.0.0.1 5432 whilly)

# Claude (если нужен proxy — см. Whilly-Claude-Proxy-Guide)
claude --version

# Smoke + claim одной задачи
whilly plan show "Whilly v4.1 — Out-of-scope items from v4.0 release"
whilly run --plan "Whilly v4.1 — Out-of-scope items from v4.0 release" --max-iterations 1
```

Если все 7 команд отработали без ошибок — machine B готов к параллельной работе с A. Если упало — мотай к секции [Troubleshooting](#troubleshooting).

## 1. Когда этот документ нужен

| Сценарий | Читай это |
|---|---|
| «У меня одна машина, хочу впервые поднять Whilly» | [`Getting-Started.md`]({{ site.baseurl }}/getting-started) |
| «Я уже знаю Whilly, переключаюсь на другую машину, не хочу всё перечитывать» | [`Continuing-On-Another-Machine.md`]({{ site.baseurl }}/continuing-on-another-machine) (cheat-sheet) |
| «Нужен production-grade onboarding второй машины с разбором всех вариантов сети, безопасности и troubleshoot'а» | **этот документ** |
| «Хочу запускать Whilly через корпоративный proxy» | [`Whilly-Claude-Proxy-Guide.md`]({{ site.baseurl }}/whilly-claude-proxy-guide) (TASK-109) |

Если у тебя только doc'и / тесты / code-review без orchestrator'а — секции 4-8 можно пропустить: для этого достаточно `git pull` и редактора.

## 2. Prerequisites

На machine B должно быть:

- **Python 3.10+** (3.12 предпочтительнее — CI matrix). Проверь: `python3 --version`.
- **git** + SSH-ключ, прописанный в GitHub.
- **claude** CLI на `PATH` — если планируешь запускать worker (а не только править docs). Установка: `npm install -g @anthropic-ai/claude-code` (или per platform binary). Аутентификация: `claude login`.
- **Один из трёх** механизмов сетевого доступа к Postgres на machine A — см. секцию 4.

Опционально, в зависимости от выбранного Postgres-режима:

- **Docker Desktop / docker.io** — для standalone-режима (4.1).
- **SSH access на machine A** — для tunnel-режима (4.2).
- **Tailscale / WireGuard** на обеих машинах — для mesh-режима (4.3).

## 3. Установка

```bash
# 1. Клонируем репо
git clone git@github.com:mshegolev/whilly-orchestrator.git
cd whilly-orchestrator
git checkout main
git pull --ff-only

# 2. Виртуальное окружение
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

# 3. Editable install + dev зависимости (alembic, pytest, ruff, asyncpg, ...)
pip install -e '.[dev]'

# 4. Sanity-check
whilly --help                  # CLI dispatcher
whilly-worker --help           # standalone remote-worker entry (TASK-022c)
ruff check whilly/ tests/      # должно сказать "All checks passed!"
```

Если `whilly --help` ругается на отсутствующий бинарь — pyenv shim не подхватил venv. Самый надёжный fallback — звать через интерпретатор: `python -m whilly --help`.

## 4. Postgres reach — выбери один режим

Это **главное** решение бутстрапа. От него зависит, шарится ли план между A и B (и значит работаете ли вы над одной задачей или над двумя независимыми).

| # | Режим | Setup на B | Сетевая зависимость | Shared plan? |
|---|---|---|---|---|
| 4.1 | Standalone (local docker) | `./scripts/db-up.sh` + `alembic upgrade head` + `whilly plan import .planning/v4-1_tasks.json` | нет | **нет** — у B свой Postgres, отдельный мир |
| 4.2 | SSH reverse-tunnel к A | `ssh -fN -L 5432:127.0.0.1:5432 user@A` | A должен быть достижим по SSH | **да** |
| 4.3 | Tailscale / WireGuard mesh | static internal IP к A's Postgres | mesh-сеть на обеих машинах | **да** |
| 4.4 | `scripts/whilly-share.sh` (TASK-111) | одной командой; пока **не реализовано** | публично-туннелируемый control plane | **да** |

### 4.1 Standalone (local Docker)

Подходит для: изолированной разработки, plane-без-сети, или экспериментов где не хочется пачкать общий план.

```bash
# 1. Запускаем Postgres контейнер. Скрипт идемпотентен и ждёт healthcheck.
./scripts/db-up.sh

# 2. Применяем миграции
alembic upgrade head

# 3. Импортируем v4.1 план в локальный Postgres
whilly plan import .planning/v4-1_tasks.json
```

После этого у B свой полный 31-task граф. Изменения статусов **не** долетают до A — это две отдельные базы. Подходит как fallback или offline-режим, но **не** удовлетворяет acceptance criteria TASK-110 ("shared plan with primary").

### 4.2 SSH reverse-tunnel к primary

**Самый простой способ получить shared plan.** Не требует расшаривания Postgres-порта в публичную сеть — A просто открывает SSH-доступ для B (типичная конфигурация для laptop ↔ VPS / dev-server).

На machine B:

```bash
# Forward A:5432 → localhost:5432 на B. -fN: fork и не запускать команду на удалённом хосте.
ssh -fN -L 5432:127.0.0.1:5432 user@machine-a

# Проверка туннеля: psql или pg_isready
pg_isready -h 127.0.0.1 -p 5432    # должно сказать "accepting connections"
```

Если уже занят локальный 5432 (своим Postgres'ом — например, после 4.1) — выбери другой порт:

```bash
ssh -fN -L 15432:127.0.0.1:5432 user@machine-a
export WHILLY_DATABASE_URL=$(printf 'postgresql://%s:%s@%s:%s/%s' whilly "$PG_PASSWORD" 127.0.0.1 15432 whilly)
```

Туннель умирает вместе с SSH-сессией. Чтобы он переживал sleep/wake/loss-of-net — оборачивай через `autossh`:

```bash
brew install autossh                                                  # macOS
autossh -M 0 -fN -o "ServerAliveInterval=30" -o "ServerAliveCountMax=3" \
    -L 5432:127.0.0.1:5432 user@machine-a
```

### 4.3 Tailscale / WireGuard mesh

Подходит для: постоянной двунаправленной связности (laptop работает из дома и кафе, machine A — на офисном столе или в облаке), когда SSH-tunnel становится утомительным к каждому wake.

Tailscale-flow (для WireGuard аналогично, замени имена):

```bash
# На обеих машинах:
brew install tailscale && sudo tailscale up         # macOS
# или: curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up

# На A: разрешить Tailscale-IP'ам доступ к Postgres'у. По умолчанию docker-compose
# биндит 127.0.0.1:5432; нужно либо bind 0.0.0.0:5432 в Tailscale-only network,
# либо открыть Postgres только для tailscale0 интерфейса через pg_hba + listen_addresses.
# Минимальная правка docker-compose.yml на A:
#   ports:
#     - "100.64.0.0/10:5432:5432"   # подсеть Tailscale CGNAT

# На B: используй Tailscale-имя или IP A
export WHILLY_DATABASE_URL=$(printf 'postgresql://%s:%s@%s:%s/%s' whilly "$PG_PASSWORD" machine-a.tail-XXXX.ts.net 5432 whilly)
```

⚠ Open-Postgres-в-mesh — это всё ещё credentials, прописанные в DSN. Дефолт из `docker-compose.yml` (username `whilly`, password — тот же) подходит **только** для local-dev; не открывай Postgres в mesh-сеть с дефолтным паролем. См. [Security caveats](#10-security-caveats).

### 4.4 Future: `scripts/whilly-share.sh` (TASK-111)

Когда [TASK-111](https://github.com/mshegolev/whilly-orchestrator/issues?q=TASK-111) ландится, секцию 4.2 / 4.3 можно будет заменить на одну команду:

```bash
# (placeholder — недоступно на момент TASK-110)
./scripts/whilly-share.sh --serve              # на A
./scripts/whilly-share.sh --connect <url>      # на B
```

Скрипт инкапсулирует cloudflared / ngrok-туннель к control plane, в обход прямой Postgres-экспозиции. Когда landed — обнови этот раздел и удали 4.2/4.3 как «legacy options».

## 5. `WHILLY_DATABASE_URL` recipe per mode

> Все DSN ниже используют bash-substitution `${PG_PASSWORD}` — установи переменную один раз
> и она подхватится во всех `export`-командах. Безопасный способ ввода (не пишет в shell-history):
> `read -s PG_PASSWORD && export PG_PASSWORD`. Альтернатива — keyring-вариант ниже.

> **Username и DB name по умолчанию — `whilly`** (значения из `docker-compose.yml`: `POSTGRES_USER` / `POSTGRES_DB` defaults). Password в local-dev тоже `whilly` — но для shared-mode setups (4.2 / 4.3) **обязательно ротируй на сильный random secret** до открытия Postgres'а в сеть. См. [Security caveats](#10-security-caveats).

| Режим | `WHILLY_DATABASE_URL` |
|---|---|
| 4.1 Standalone | `printf 'postgresql://%s:%s@%s:%s/%s' whilly "$PG_PASSWORD" 127.0.0.1 5432 whilly` |
| 4.2 SSH-tunnel (default port) | `printf 'postgresql://%s:%s@%s:%s/%s' whilly "$PG_PASSWORD" 127.0.0.1 5432 whilly` |
| 4.2 SSH-tunnel (alt port) | `printf 'postgresql://%s:%s@%s:%s/%s' whilly "$PG_PASSWORD" 127.0.0.1 15432 whilly` |
| 4.3 Tailscale (hostname) | `printf 'postgresql://%s:%s@%s:%s/%s' whilly "$PG_PASSWORD" machine-a.tail-XXXX.ts.net 5432 whilly` |
| 4.3 Tailscale (CGNAT IP) | `printf 'postgresql://%s:%s@%s:%s/%s' whilly "$PG_PASSWORD" 100.x.y.z 5432 whilly` |

Подставь свой пароль вместо `<PG_PASSWORD>`. Положи итог в `~/.zshrc` / `~/.bashrc` / `~/.config/fish/conf.d/whilly.fish` или в проект-локальный `.envrc` (если используешь direnv). **Не** коммить в git — пароль в DSN.

Для постоянной работы безопаснее вынести password в keyring и собирать DSN на лету:

```bash
# Один раз — записать password в OS keyring (он не уйдёт в git/историю shell'а):
python3 -c "import keyring, getpass; keyring.set_password('whilly', 'pg_password', getpass.getpass('PG password: '))"

# В shell-init собирать DSN из keyring:
PG_PASSWORD=$(python3 -c "import keyring; print(keyring.get_password('whilly','pg_password'))")
export WHILLY_DATABASE_URL=$(printf 'postgresql://%s:%s@%s:%s/%s' whilly "$PG_PASSWORD" 127.0.0.1 5432 whilly)
```

## 6. Claude CLI bootstrap

Anthropic credentials — per-machine, не передаются через git. Аутентифицируйся отдельно:

```bash
claude login                  # один раз на B
claude --version              # sanity
```

Если B стоит за корпоративным proxy и не достучится до `api.anthropic.com` напрямую — поднимай SSH-туннель к гейту с outbound и натравливай Whilly на него. Полный гайд — [`Whilly-Claude-Proxy-Guide.md`]({{ site.baseurl }}/whilly-claude-proxy-guide). Минимум:

```bash
ssh -fN -L 11112:127.0.0.1:8888 gpt-proxy.internal
export WHILLY_CLAUDE_PROXY_URL=http://127.0.0.1:11112
```

Эти env vars влияют **только** на дочерний Claude-процесс; asyncpg/httpx Whilly остаются direct (см. TASK-109).

## 7. Smoke tests

```bash
# 1. План видим из B (тот же 31-task граф что на A)
whilly plan show "Whilly v4.1 — Out-of-scope items from v4.0 release"
# Ожидаемо: список задач TASK-101 … TASK-111 со статусами как на primary

# 2. Claim ровно одной готовой задачи и завершение worker-цикла
whilly run --plan "Whilly v4.1 — Out-of-scope items from v4.0 release" --max-iterations 1
# Ожидаемо: registered worker=<hostB>-<uuid>, обработана одна pending-task,
# процесс вышел с кодом 0
```

Если задача успешно дошла до `done` или `failed` (терминальный статус) — на A в `whilly plan show` ты увидишь её новый статус. Это и есть критерий «B claim'ает из общего плана».

> **Note:** Описание TASK-110 упоминает `whilly worker local --once` — это устаревшее имя из ранних драфтов. Актуальная команда — `whilly run --plan <id> --max-iterations 1`. Флаг `--once` существует **только** в standalone-бинарнике `whilly-worker` (remote-worker для control-plane'а; см. `whilly-worker --help`).

## 8. Avoiding collisions with the primary worker

Whilly v4 спроектирован так, чтобы N независимых worker'ов могли одновременно тянуть задачи из одного плана:

- Каждый worker регистрируется со своим **`worker_id`** (по умолчанию `<hostname>-<8-hex-uuid>` — два worker'а на разных машинах никогда не столкнутся PK'ом).
- Claim'ят через `SELECT … FOR UPDATE SKIP LOCKED` — два worker'а никогда не возьмут одну и ту же строку.
- Heartbeat — независимый per-worker; зомби-worker (потерял сеть, упал ноут) детектится по lease-timeout, его задача возвращается в pool.

Что **нельзя** делать на B:

- Передавать `--worker-id <fixed-id>`, совпадающий с A's id. Это нарушит invariant и поломает heartbeat'ы обеим сторонам.
- Запускать `whilly plan import` второй раз поверх существующего `plan_id` — упадёт на foreign-key constraint. Если действительно нужно пересоздать — сначала `whilly plan reset` (когда [TASK-103](https://github.com/mshegolev/whilly-orchestrator/issues?q=TASK-103) ландится) или вручную: `DELETE FROM events WHERE task_id IN (SELECT id FROM tasks WHERE plan_id='…'); DELETE FROM tasks WHERE plan_id='…'; DELETE FROM plans WHERE id='…';`.
- Заводить долгоживущий `whilly run` (без `--max-iterations`) на ноуте, который уходит в sleep — heartbeat прекратится, A пометит worker'а stale, lease перейдёт обратно в pool, B при wake-up попробует завершить чужую с её точки зрения задачу. Используй `--max-iterations N` или останавливай worker'а до закрытия крышки.

Архитектурный фон — `docs/Whilly-v4-Worker-Protocol.md` (claim semantics, heartbeat, lease timeouts) и `whilly/cli/run.py` (флаги).

## 9. Sync auto-memory (optional)

`~/.claude/projects/<project-id>/memory/MEMORY.md` живёт на машине, на которой Claude был запущен. Между A и B оно **не** ходит. Если хочется continuity:

- **Quick path** — в первом промпте на B вставь 2-3 последних значимых memory-line из A.
- **Long-term** — держи `~/.claude/projects/<project-id>/memory/` в приватном git-репо или sync через Dropbox/iCloud/rsync.

`CLAUDE.md` (per-project, в корне репо) **уже** в git — синхронизируется автоматически с `git pull`.

## 10. Security caveats

| Риск | Mitigation |
|---|---|
| **Shared bearer tokens** между worker'ами (per-worker rotation — [TASK-101](https://github.com/mshegolev/whilly-orchestrator/issues?q=TASK-101), пока pending). | Не пиши план/токены в публичные репо; ограничь сетевую expose Postgres'а до lo / Tailscale-only. |
| **Anonymous tunnels** (cloudflared / ngrok без auth — релевантно когда TASK-111 ландится). | Включай auth-headers, привязывай к стабильным subdomain'ам с allowlist; не оставляй endpoint открытым на ночь. |
| **Postgres credential rotation**. Дефолтные `POSTGRES_USER` / `POSTGRES_PASSWORD` (из `docker-compose.yml`, оба `whilly`) подходят только для local docker; в любой shared-mode конфигурации меняй password на длинный random secret, храни в keyring, ротируй ежеквартально. | `docker compose down -v && POSTGRES_PASSWORD=$(openssl rand -hex 32) docker compose up -d`; обнови `WHILLY_DATABASE_URL` на A и B одновременно. |
| **Claude proxy leak** — если `HTTPS_PROXY` экспортирован в shell, его подхватит `httpx` parent-процесса Whilly и пошлёт control-plane запросы через тот же tunnel. | Используй `WHILLY_CLAUDE_PROXY_URL` (изолирован к спавну Claude) вместо глобального `HTTPS_PROXY`. См. TASK-109. |
| **`whilly init` мутирует `os.environ`** на CLI-флагах (TASK-109 follow-up). Не критично для secret'ов, но создавай новый shell для запусков с разными `--claude-proxy`. | Документировано в коде; не делай implicit assumptions, явно указывай флаги. |

## 11. Troubleshooting

**`whilly run: WHILLY_DATABASE_URL is not set`**
→ Не выставлен env var. Перепроверь `echo $WHILLY_DATABASE_URL`.

**`asyncpg.exceptions.InvalidPasswordError` или `connection refused`**
→ Туннель не поднят / упал. Перепроверь `pg_isready -h 127.0.0.1 -p 5432`. Для SSH-tunnel убедись `ps aux | grep ssh | grep 5432`.

**`whilly plan show 'Whilly v4.1 — …'` возвращает `plan not found`**
→ Ты в standalone-режиме (4.1) и не сделал `whilly plan import .planning/v4-1_tasks.json`, или подключился не к той Postgres-инстанции. Проверь `psql $WHILLY_DATABASE_URL -c 'SELECT id FROM plans;'`.

**`whilly run` claim'ает task, но Claude падает с `connection refused` к `api.anthropic.com`**
→ Сеть B не достучится напрямую до Anthropic. Настрой proxy через TASK-109 (`WHILLY_CLAUDE_PROXY_URL`).

**На primary внезапно `worker stale`-сообщения**
→ B ушёл в sleep с активным worker'ом. Останавливай worker до закрытия крышки или используй `--max-iterations`.

**`whilly --help` зависает / печатает мусор**
→ pyenv shim путает venv. Пробуй `python -m whilly --help`. Если помогло — добавь `.venv/bin` в начало `PATH` или используй direnv.

## 12. Related docs

- [`Continuing-On-Another-Machine.md`]({{ site.baseurl }}/continuing-on-another-machine) — короткий cheat-sheet для частого переключения между машинами.
- [`Getting-Started.md`]({{ site.baseurl }}/getting-started) — первая установка Whilly с нуля (single-machine).
- [`Whilly-Init-Guide.md`]({{ site.baseurl }}/whilly-init-guide) — `whilly init` flow для новых планов.
- [`Whilly-Claude-Proxy-Guide.md`]({{ site.baseurl }}/whilly-claude-proxy-guide) — TASK-109, Claude через HTTPS-proxy.
- [`Whilly-v4-Worker-Protocol.md`]({{ site.baseurl }}/whilly-v4-worker-protocol) — claim semantics, heartbeat, bearer token lifecycle.
- [`Whilly-Usage.md`]({{ site.baseurl }}/whilly-usage) — полный CLI / env-var reference.
- `scripts/db-up.sh` — idempotent local Postgres bootstrap.
- `whilly/cli/run.py`, `whilly/cli/worker.py` — local + remote worker entrypoints.
- `.planning/v4-1_tasks.json` — канонический task graph для v4.1.
