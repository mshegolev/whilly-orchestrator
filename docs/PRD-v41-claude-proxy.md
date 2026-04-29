# PRD — Claude proxy / tunnel integration

**Version:** 0.1 (draft)
**Date:** 2026-04-29
**Owner:** v4.1 backlog (TASK-109)
**Tracker:** [`.planning/v4-1_tasks.json`](https://github.com/mshegolev/whilly-orchestrator/blob/main/.planning/v4-1_tasks.json) → TASK-109

## 1. Контекст

В корпоративных средах Anthropic API часто доступен только через прокси (SSH-туннель + HTTP proxy на промежуточном хосте). Типичная схема у нашего основного оператора:

```
laptop                   gpt-proxy                 api.anthropic.com
  ↓ ssh -L 11112:127.0.0.1:8888 gpt-proxy
  → 127.0.0.1:11112 (HTTPS proxy) → tunnel → gpt-proxy:8888 → internet → Anthropic
```

В терминале это работает прозрачно через shell-функцию `claudeproxy`, которая:
1. Поднимает (или предполагает поднятым) `ssh -L 11112:127.0.0.1:8888 gpt-proxy`.
2. Устанавливает `HTTPS_PROXY=http://127.0.0.1:11112`.
3. Запускает реальный `claude` binary.

Из shell-функции выходит, что `claude` aliased на `claudeproxy`. Из Python `subprocess.Popen(["claude", ...])` shell-aliases не resolve'ятся — Whilly v4 пытается запустить bare `claude`, который без proxy не достучится до Anthropic API. **Сейчас оператор вынужден либо запускать Whilly из shell вручную с правильным окружением, либо лезть в код подменять CLAUDE_BIN.**

При этом:
- Worker процесс **сам не должен** ходить через прокси — он стучится в Postgres (`localhost:5432`), control plane (`localhost:8000` или внутренний host), и эти хосты обычно отключены от внешнего интернета. Прокси на эти запросы либо ломается («refused to proxy»), либо вносит латентность.
- Только subprocess, который ходит к Anthropic API, должен быть проксирован.

## 2. Цели

**G1.** Whilly v4 worker и `whilly init` должны уметь автоматически проксировать только Claude-вызовы (через настройку HTTPS_PROXY на уровне Claude subprocess'а, не Whilly process'а).

**G2.** Локальные / внутренние хосты (Postgres, control plane, любые `127.0.0.1`/`localhost`/`*.internal`) обходят прокси через `NO_PROXY` env var.

**G3.** Опциональная авто-проверка SSH-туннеля: если `HTTPS_PROXY` указан, но `127.0.0.1:11112` не отвечает — Whilly выдаёт понятную ошибку «туннель не поднят, сделай `ssh -L ...`» вместо confusing `Connection refused` от глубоко внутри Claude HTTP-клиента.

**G4.** Backwards-compat: оператор без proxy ничего не настраивает, всё работает как сейчас (default — никаких HTTPS_PROXY).

## 3. Не-цели

**NG1.** Whilly **не управляет SSH-туннелем сам**. Туннель — операторская задача (запустил `ssh -L ... &` один раз и забыл, или systemd unit). Whilly только проверяет что endpoint отвечает.

**NG2.** Не покрываем custom Anthropic endpoints (`ANTHROPIC_BASE_URL`). Это отдельная задача — там нужно прокидывать другой env var Claude'у.

**NG3.** Не делаем GUI / TUI настройку прокси. Только env vars.

**NG4.** Не покрываем corporate proxy с auth (`http://user:pass@proxy:port`). Auth-прокси на v4.1 не нужны; добавим если попросят.

## 4. Functional Requirements

### FR-1. Env-var surface

Новые env vars читаются `WhillyConfig` (или эквивалентом):

| Env var | Default | Назначение |
|---------|---------|------------|
| `WHILLY_CLAUDE_PROXY_URL` | `""` (off) | HTTPS proxy URL для Claude subprocess. Например `http://127.0.0.1:11112`. Пустая строка — proxy отключён, Claude идёт напрямую. |
| `WHILLY_CLAUDE_NO_PROXY` | auto-built | Список хостов которым НЕ ходить через прокси. Default: `localhost,127.0.0.1,::1`. Можно расширить через override (`*.internal,10.0.0.0/8`). |
| `WHILLY_CLAUDE_PROXY_PROBE` | `1` (on) | Если `1` — перед запуском Claude проверить `127.0.0.1:11112` TCP-handshake'ом (50ms timeout). Если порт не отвечает — fail с понятным сообщением. `0` — пропустить probe. |

`CLAUDE_BIN` — уже есть; не меняется.

### FR-2. Subprocess env injection

Все места, где Whilly запускает Claude через `subprocess.Popen` / `asyncio.create_subprocess_exec`, инжектируют `HTTPS_PROXY` + `NO_PROXY` в `env=` параметр subprocess'а **только если** `WHILLY_CLAUDE_PROXY_URL` установлен. Whilly process сам остаётся без этих переменных — его asyncpg / httpx HTTP-клиенты идут напрямую.

Места:
- `whilly/adapters/runner/claude_cli.py` — основной runner (worker → Claude).
- `whilly/prd_generator.py::_call_claude` — PRD/tasks generator (init wizard → Claude).
- `whilly/prd_launcher.py` — interactive Claude session.

### FR-3. Pre-flight probe

Если `WHILLY_CLAUDE_PROXY_URL` указан и `WHILLY_CLAUDE_PROXY_PROBE=1`:

1. Распарсить URL → `(host, port)`.
2. `socket.create_connection((host, port), timeout=0.5)`.
3. Connection refused / timeout → exit с понятным сообщением:
   ```
   whilly: Claude proxy unreachable at http://127.0.0.1:11112
   Hint: bring up the SSH tunnel first:
     ssh -fN -L 11112:127.0.0.1:8888 gpt-proxy
   To skip this check: WHILLY_CLAUDE_PROXY_PROBE=0
   ```
4. Connection ok → продолжаем как обычно.

Probe бежит один раз на старте worker / init процесса, а не перед каждым subprocess. Кеш не нужен — туннель либо есть, либо нет.

### FR-4. Logging

При установке прокси — INFO-лог:
```
whilly: Claude subprocess will use HTTPS_PROXY=http://127.0.0.1:11112 (NO_PROXY=localhost,127.0.0.1,::1)
```

При успешном probe:
```
whilly: Claude proxy probe ok (127.0.0.1:11112)
```

При неудачном probe — выше уже описано.

### FR-5. Default behaviour without proxy

Если `WHILLY_CLAUDE_PROXY_URL` пустой / unset:
- Никаких HTTPS_PROXY / NO_PROXY в subprocess env.
- Никакого probe.
- Whilly ведёт себя как сейчас.

### FR-6. CLI flags для `whilly init` / `whilly run`

Опциональные флаги, которые перекрывают env vars (для удобства one-shot вызова):

```
--claude-proxy URL      — same as WHILLY_CLAUDE_PROXY_URL
--no-claude-proxy       — explicitly disable proxy even if env var is set
```

CLI-флаги > env vars — стандартная иерархия, как у `--connect` / `WHILLY_CONTROL_URL` в `whilly-worker`.

## 5. Non-Functional Requirements

**NFR-1.** Никаких новых зависимостей. `socket` для probe — stdlib.

**NFR-2.** Probe должен быть < 100ms на happy path (TCP handshake к localhost). На fail — < 1s timeout.

**NFR-3.** Тесты: probe — unit-тест с фейковым TCP сервером в `tests/unit/test_claude_proxy_probe.py`. Subprocess env injection — unit-тест с monkeypatched `subprocess.Popen`. Integration test (опциональный, skip если нет SSH-туннеля): прогнать `whilly init --headless --claude-proxy http://127.0.0.1:11112 "..."` против реального туннеля.

**NFR-4.** Документация в `docs/Whilly-Claude-Proxy-Guide.md` с примером `ssh -L` + systemd unit для туннеля + рекомендации NO_PROXY для типичных corporate сред.

## 6. Success Criteria

**SC-1.** Set `WHILLY_CLAUDE_PROXY_URL=http://127.0.0.1:11112`, поднять SSH-туннель, запустить `whilly init "..." --headless`. Claude subprocess должен достучаться до Anthropic API через прокси, PRD сгенерироваться, план импортироваться. Pinned by manual demo + integration test (skipped without tunnel).

**SC-2.** Без `WHILLY_CLAUDE_PROXY_URL` (default) — `whilly init` работает как сейчас (regression-free). Pinned by `tests/integration/test_init_e2e.py` без proxy env.

**SC-3.** С `WHILLY_CLAUDE_PROXY_URL=http://127.0.0.1:99999` (порт не слушает) — `whilly init` упадёт за < 1s с понятной подсказкой про SSH-туннель. Pinned by `tests/unit/test_claude_proxy_probe.py`.

**SC-4.** Worker process сам **не** ходит через прокси: asyncpg connection к Postgres / httpx к control plane не получают `HTTPS_PROXY`. Pinned by unit-тест проверяющий env diff между Whilly process'ом и subprocess'ом.

**SC-5.** Покрытие нового кода ≥ 80% (`whilly/adapters/runner/claude_cli.py` после изменений + новый proxy-helper модуль).

## 7. Constraints

**C-1.** Python 3.12+ (v4 baseline).

**C-2.** stdlib only — никаких `requests` / `httpx` / `paramiko` для probe. Только `socket`.

**C-3.** Никаких изменений в `whilly/core/` — proxy logic это adapter-layer concern (subprocess env). `lint-imports` core-purity не должен сработать.

**C-4.** Probe и subprocess env injection должны быть synchronous — мы запускаем их в местах где async-context может быть, но может и не быть (CLI runs `asyncio.run`, но probe бежит до этого).

## 8. Open Questions

**OQ-1.** Где живёт shared probe + env-injection helper? **Резолв:** новый модуль `whilly/adapters/runner/proxy.py` — рядом с `claude_cli.py`. Это adapter-layer (трогает `os.environ`, `socket`), импортируется и `claude_cli.py`, и `prd_generator.py` (через тонкую public функцию). `prd_generator.py` сейчас в legacy-pile, но импорт тонкий — один helper'ный вызов.

**OQ-2.** Что делать если `claudeproxy` shell-функция уже задаёт HTTPS_PROXY в env переменных самой shell-сессии? **Резолв:** Whilly уважает существующий `HTTPS_PROXY` если есть — наш `WHILLY_CLAUDE_PROXY_URL` перекрывает только если установлен. То есть приоритет: CLI flag > `WHILLY_CLAUDE_PROXY_URL` > inherited `HTTPS_PROXY`. Если ничего не задано — никакого прокси.

**OQ-3.** Стоит ли подержать `HTTP_PROXY` вдобавок к `HTTPS_PROXY`? **Резолв:** Не стоит. Anthropic API только HTTPS, `HTTP_PROXY` для него не используется. Не плодим лишнее.

**OQ-4.** Что если `WHILLY_CLAUDE_NO_PROXY` пуст? **Резолв:** Default value — `localhost,127.0.0.1,::1`. Если оператор явно ставит пустую строку — используем её (значит "ничего не исключать", полный прокси для всех хостов). Это легитимный operator override, не bug.

## 9. Dependencies

* **Hard dependencies:** none. Self-contained.
* **Soft dependencies:** TASK-104a (whilly init) — мы добавляем proxy support и в него тоже. 104a уже done, так что это не блокер, а area of effect.
* **Unblocks:** ничего — proxy support это independently-shipped feature.

## 10. Out of scope

* Auth-proxy (`http://user:pass@host:port`) — добавить отдельной задачей если попросят.
* Custom `ANTHROPIC_BASE_URL` через тот же proxy — отдельная задача (TASK-109b future).
* Автоподнятие SSH-туннеля Whilly'ем (NG1).
* SOCKS5 proxy — `claude` CLI может не уметь SOCKS, и это редкий case.

## 11. Definition of Done

1. Все 5 SC зелёные.
2. Новый код в `whilly/adapters/runner/proxy.py` + изменения в `claude_cli.py` / `prd_generator.py` / `prd_launcher.py` / `cli/init.py` / `cli/run.py` проходят `ruff check + format`, `lint-imports` (core-purity не нарушен), `mypy --strict whilly/core/` (core не трогается, должен остаться clean).
3. Coverage ≥ 80% на новом модуле `proxy.py`.
4. `docs/Whilly-Claude-Proxy-Guide.md` написан.
5. CHANGELOG `[Unreleased]` обновлён с `### Added — WHILLY_CLAUDE_PROXY_URL support`.
6. Unit + integration тесты для всех путей зелёные.
7. Один real-world demo: оператор может вручную проверить `whilly init --headless` через свой реальный gpt-proxy туннель и получить корректный план.
