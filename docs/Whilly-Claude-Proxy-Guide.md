---
title: Claude API через proxy
layout: default
nav_order: 5
description: "Как настроить Whilly v4 чтобы Claude ходил через HTTPS-proxy / SSH-tunnel, а Postgres и control plane оставались direct."
permalink: /whilly-claude-proxy-guide
---

# Claude API через proxy / SSH-tunnel

> Когда Anthropic API доступен только через прокси (типично — SSH-туннель к промежуточному хосту с outbound-разрешением), Whilly должен направить **только** Claude-вызовы через прокси, оставив всю свою служебную инфраструктуру (Postgres, control plane) направленной напрямую. Эта страница — инструкция как.

## Зачем это нужно

В корпоративных окружениях Anthropic API обычно недоступен с laptop'а напрямую. Стандартное решение — SSH-туннель к VPS, который имеет доступ к интернету, плюс HTTP-proxy на нём:

```
laptop                         gpt-proxy.internal:22       api.anthropic.com
  |                                  |                              |
  |  ssh -L 11112:127.0.0.1:8888 ────┤  HTTP proxy on :8888 ────────┤
  |                                  |  (squid / mitmproxy / etc.)  |
  |                                  |                              |
laptop:11112  ◄──────────── tunnel ──┘                              |
  |                                                                 |
  |  HTTPS_PROXY=http://127.0.0.1:11112                              |
  └─ claude  ──── HTTPS CONNECT ──────────────────────────────────► |
```

Whilly v4 worker и `whilly init` запускают Claude через `subprocess` — а это значит, что shell-aliases и `claudeproxy`-функции до них не доходят. Нужен явный механизм через env vars или CLI flags.

## Базовая настройка (operator side)

```bash
# 1. Поднять SSH-туннель в фоне (один раз на сессию).
#    -fN: fork и не запускать команду на удалённой стороне.
ssh -fN -L 11112:127.0.0.1:8888 gpt-proxy.internal

# 2. Указать Whilly где proxy.
export WHILLY_CLAUDE_PROXY_URL=http://127.0.0.1:11112

# 3. (Опционально) хосты, которые НЕ должны идти через proxy.
#    По умолчанию — localhost,127.0.0.1,::1. Расширь под свою сеть.
export WHILLY_CLAUDE_NO_PROXY="localhost,127.0.0.1,::1,*.internal,10.0.0.0/8"

# 4. Запустить Whilly как обычно — Claude теперь пойдёт через proxy,
#    а asyncpg к Postgres и httpx к control plane — напрямую.
whilly init "build a CLI tool" --slug api-monitor
```

## Как Whilly резолвит настройки

Приоритет (первое непустое — побеждает):

1. CLI flag `--claude-proxy URL` (для одного запуска).
2. CLI flag `--no-claude-proxy` (опт-аут даже если env установлен).
3. Env var `WHILLY_CLAUDE_PROXY_URL`.
4. Inherited `HTTPS_PROXY` из shell (для совместимости с `claudeproxy` shell-функциями).
5. По умолчанию — без proxy.

```bash
# Override на один запуск:
whilly init "build X" --claude-proxy http://other-proxy:9999

# Опт-аут, даже если env проставлен (debug, fallback):
whilly init "build X" --no-claude-proxy

# Из shell-функции которая уже ставит HTTPS_PROXY:
HTTPS_PROXY=http://127.0.0.1:11112 whilly init "build X"
```

## Pre-flight probe

При активном proxy Whilly **один раз на старте** делает TCP-handshake к указанному `host:port`. Это проверяет что туннель действительно поднят, и выдаёт человекочитаемую ошибку если нет:

```
$ whilly init "build X"
whilly: Claude proxy unreachable at http://127.0.0.1:11112 (ConnectionRefusedError: [Errno 61])
Hint: bring up the SSH tunnel first, e.g.:
  ssh -fN -L 11112:127.0.0.1:8888 gpt-proxy
To skip this check: WHILLY_CLAUDE_PROXY_PROBE=0
```

Без probe failure surface'илась бы как многоминутный Claude HTTPS timeout где-то внутри HTTP-клиента — сложно дебажить.

Probe можно отключить:

```bash
WHILLY_CLAUDE_PROXY_PROBE=0 whilly init "build X"
```

Зачем отключать: некоторые корпоративные proxies отвергают bare TCP probes (срабатывает rate-limit или DPI-rule). Operator решает — лучше пропустить дешёвый probe и положиться на failure от реального Claude-вызова.

## NO_PROXY: локальные хосты

Whilly **никогда не модифицирует свой собственный process env**. `HTTPS_PROXY` устанавливается только в env переменных subprocess'а Claude. Но если у тебя в shell уже стоит `HTTPS_PROXY` глобально — оно унаследуется во весь Python-процесс, и тогда `asyncpg`/`httpx` тоже попробуют пойти через proxy.

`NO_PROXY` — твоя страховка от этого. По умолчанию Whilly выставляет:

```
NO_PROXY=localhost,127.0.0.1,::1
```

Это обходит proxy для:
- `localhost:5432` (Postgres в docker-compose)
- `127.0.0.1:8000` (FastAPI control plane локально)
- `::1:8000` (IPv6 loopback)

Расширяй под свою топологию. Типичные дополнения для corporate сетей:

```bash
export WHILLY_CLAUDE_NO_PROXY="localhost,127.0.0.1,::1,*.internal,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
```

## SSH tunnel как systemd unit (production)

Чтобы туннель не падал когда laptop засыпает — обернуть в `systemd --user` unit:

```ini
# ~/.config/systemd/user/whilly-claude-tunnel.service
[Unit]
Description=SSH tunnel for Whilly Claude proxy
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -N -L 11112:127.0.0.1:8888 -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes gpt-proxy.internal
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now whilly-claude-tunnel.service
systemctl --user status whilly-claude-tunnel.service
```

`ExitOnForwardFailure=yes` критично: если порт уже занят — service упадёт сразу, а не повиснет в зомби-состоянии.

## Troubleshooting

### `Claude proxy unreachable at http://127.0.0.1:11112`

Туннель не поднят или упал.

```bash
# Проверить вручную:
nc -z 127.0.0.1 11112 && echo "tunnel up" || echo "tunnel down"

# Поднять снова:
ssh -fN -L 11112:127.0.0.1:8888 gpt-proxy.internal

# Или systemd-restart:
systemctl --user restart whilly-claude-tunnel.service
```

### Worker подключается к Postgres через proxy (медленно / падает)

Проверь что `WHILLY_CLAUDE_NO_PROXY` включает твой Postgres host. `asyncpg` использует стандартный Python `urllib`-стиль NO_PROXY (хотя Postgres не HTTP — это нативный protocol; но если у тебя `HTTPS_PROXY` exported глобально, есть corner case с DNS).

```bash
# Дебаг:
WHILLY_DEBUG=1 whilly run --plan X 2>&1 | grep -i proxy
```

### `Hint: ssh -fN -L 11112:127.0.0.1:8888 gpt-proxy` — но мой gateway другой

Это generic-подсказка из `probe_proxy_or_raise`. Подставь свой реальный SSH-host вручную; URL-парсер показывает ровно тот port который Whilly попробовал, host — ваш.

### Claude всё равно использует свой credentials

`HTTPS_PROXY` не меняет API-key Claude. Если Claude CLI настроен с одним аккаунтом, а `gpt-proxy` ожидает другой — тут уже не Whilly's job, проверь `claude auth status` в spawned env.

## Связанные env vars

| Env var | По умолчанию | Назначение |
|---------|-------------|------------|
| `WHILLY_CLAUDE_PROXY_URL` | `""` (off) | URL HTTPS proxy. Пусто = не использовать. |
| `WHILLY_CLAUDE_NO_PROXY` | `localhost,127.0.0.1,::1` | Хосты в обход proxy. Пустая строка явно = ничего не исключать. |
| `WHILLY_CLAUDE_PROXY_PROBE` | `1` (on) | TCP probe перед запуском Claude. `0` отключает. |
| `HTTPS_PROXY` | inherited from shell | Fallback если `WHILLY_CLAUDE_PROXY_URL` не установлен. |
| `CLAUDE_BIN` | `claude` | Путь к Claude CLI binary. Override полезен если `claude` aliased на shell-функцию. |

## Связанные CLI flags

| Flag | На какой команде | Назначение |
|------|-------------------|------------|
| `--claude-proxy URL` | `whilly init` | Override env на один запуск. |
| `--no-claude-proxy` | `whilly init` | Force-disable proxy (опт-аут). |

## Под капотом

Whilly v4 централизует proxy logic в `whilly/adapters/runner/proxy.py`:

- `ProxySettings` — frozen dataclass с резолвенным URL + NO_PROXY.
- `resolve_proxy_settings(cli_url, cli_disabled, env)` — priority chain.
- `build_subprocess_env(parent_env, settings)` — env для subprocess (parent_env остаётся нетронутым).
- `probe_proxy_or_raise(url, timeout=0.5)` — TCP-handshake с актуальной diagnostic.

Все два места где Whilly спавнит Claude (`whilly/adapters/runner/claude_cli.py` и `whilly/prd_generator.py`) проходят через эти helpers. Полная архитектурная картина — [Whilly v4 Architecture]({{ site.baseurl }}/Whilly-v4-Architecture).

PRD: [`docs/PRD-v41-claude-proxy.md`](https://github.com/mshegolev/whilly-orchestrator/blob/main/docs/PRD-v41-claude-proxy.md).
