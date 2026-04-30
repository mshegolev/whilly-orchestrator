# Demo checklist — 2 параллельных воркера в Docker

Цель: запустить Whilly локально + 2 docker-контейнера-воркера, импортировать
простой план из 2 независимых задач, и увидеть, как они выполняются
параллельно — каждая на своём контейнере.

Архитектура того, что мы запускаем:

```
              ┌───────────────────┐
              │   postgres        │
              │   (контейнер #1)  │
              └─────────▲─────────┘
                        │ asyncpg
              ┌─────────┴─────────┐
              │   control-plane   │
              │   (контейнер #2)  │
              │   FastAPI :8000   │
              └─▲───────────────▲─┘
                │HTTP claim     │HTTP claim
        ┌───────┴───┐   ┌───────┴────┐
        │ worker-1  │   │  worker-2  │
        │(контейнер)│   │ (контейнер)│
        │  PAR-001  │   │   PAR-002  │
        └───────────┘   └────────────┘
```

> «2 хоста» = 2 реплики сервиса `worker` через `--scale worker=2`. С точки
> зрения Whilly это две разных машины: каждая регистрируется отдельно,
> каждая claim'ит свою задачу, каждая держит свой heartbeat.

---

## Pre-flight (один раз)

- [ ] Docker Desktop / docker-engine запущен и `docker info` отвечает.
- [ ] В корне репо есть файлы:
      `Dockerfile.demo`, `docker/entrypoint.sh`, `docker-compose.demo.yml`,
      `examples/demo/parallel.json`, `tests/fixtures/fake_claude.sh`
      (всё уже в проекте — это артефакты демо).
- [ ] Порты `5432` и `8000` свободны на хосте
      (`lsof -i :5432 -i :8000 | grep LISTEN` — должно быть пусто).
- [ ] Из прошлых демо ничего не висит:
      ```bash
      docker compose -f docker-compose.demo.yml down -v
      ```

---

## Шаг 1 — Сборка образа

Собирайте через **обычный `docker build`**, а не через `docker compose build`
— compose требует buildx ≥ 0.17, а на многих машинах стоит более старая
версия (получите ошибку `compose build requires buildx 0.17 or later`).
Обычный `docker build` использует встроенный builder и работает везде.

```bash
docker build -f Dockerfile.demo -t whilly-demo:latest .
```

- [ ] Сборка завершилась без ошибок.
- [ ] Образ появился: `docker images | grep whilly-demo` (видно
      `whilly-demo   latest   <digest>   ...`).

> Один и тот же образ используется и control-plane, и воркером —
> различие только в `command:` (см. `docker-compose.demo.yml`).
>
> В compose'е стоит `pull_policy: never` — это значит, что compose **не**
> попытается достать `whilly-demo` из Docker Hub (где его нет) и не упадёт
> с `pull access denied`. Если вдруг забыли собрать — compose чётко
> скажет «image not found locally» вместо мутного pull error.

### Если `docker build` тоже ругается на buildx

На совсем старых сборках Docker (`< 20.10`) можно явно отключить BuildKit:

```bash
DOCKER_BUILDKIT=0 docker build -f Dockerfile.demo -t whilly-demo:latest .
```

Multi-stage build из `Dockerfile.demo` отлично работает и в legacy-builder'е.

---

## Шаг 2 — Поднимаем БД + control-plane

```bash
docker compose -f docker-compose.demo.yml up -d postgres control-plane
docker compose -f docker-compose.demo.yml ps
```

- [ ] `whilly-demo-postgres` — статус `healthy`.
- [ ] `whilly-demo-control-plane` — статус `healthy`.
- [ ] `curl -sf http://127.0.0.1:8000/health` возвращает `{"status":"ok"}`.

Контрольная команда — миграции применились автоматически:

```bash
docker compose -f docker-compose.demo.yml exec postgres \
  psql -U whilly -d whilly -c "\dt"
```

- [ ] В выводе видны таблицы: `plans`, `tasks`, `workers`, `events`, `alembic_version`.

---

## Шаг 3 — Запускаем 2 воркера параллельно

```bash
docker compose -f docker-compose.demo.yml up -d --scale worker=2 worker
docker compose -f docker-compose.demo.yml ps
```

- [ ] Видно **две** реплики воркера (имена вида `whilly-orchestrator-worker-1`
      и `whilly-orchestrator-worker-2`).
- [ ] Оба в статусе `running` (без `Restarting`).

Каждый воркер при старте:
1. Ждёт `/health` у control-plane.
2. Регистрируется через bootstrap-token и получает уникальный
   `worker_id` + per-worker bearer.
3. Начинает long-poll по `POST /tasks/claim`.

Проверка регистрации:

```bash
docker compose -f docker-compose.demo.yml exec postgres \
  psql -U whilly -d whilly -c "SELECT worker_id, hostname, status FROM workers;"
```

- [ ] Две строки, у каждой свой `worker_id` и свой `hostname`.

---

## Шаг 4 — Импортируем простой 2-задачный план

```bash
docker compose -f docker-compose.demo.yml exec control-plane \
  whilly plan import examples/demo/parallel.json
```

- [ ] Сообщение `imported plan_id=parallel tasks=2`.

Проверка состояния:

```bash
docker compose -f docker-compose.demo.yml exec control-plane \
  whilly plan show parallel
```

- [ ] Видны `PAR-001` и `PAR-002` в статусе `PENDING`,
      без зависимостей друг от друга — ровно то, что нужно для параллели.

> Воркеры **запущены с `WHILLY_PLAN_ID=demo` по умолчанию**. Чтобы они
> разбирали план `parallel`, либо переопределите его через `.env` /
> `WHILLY_PLAN_ID=parallel docker compose ... up`, либо просто перезапустите
> воркеры с правильным id (см. шаг 5).

---

## Шаг 5 — Перенацеливаем воркеры на план `parallel`

Самое простое — задать env через переменную окружения compose'а:

```bash
WHILLY_PLAN_ID=parallel \
  docker compose -f docker-compose.demo.yml up -d --scale worker=2 \
  --force-recreate worker
```

- [ ] `docker compose ... ps` показывает 2 воркера в `running`.
- [ ] В логах хотя бы одного воркера есть строка `registered worker_id=...`.

> Альтернатива: завести `.env` файл рядом с `docker-compose.demo.yml`
> с `WHILLY_PLAN_ID=parallel` — compose подхватит автоматически.

---

## Шаг 6 — Смотрим, как задачи разъезжаются по воркерам

В **двух разных терминалах** одновременно:

**Терминал A** — логи первого воркера:
```bash
docker logs -f whilly-orchestrator-worker-1
```

**Терминал B** — логи второго воркера:
```bash
docker logs -f whilly-orchestrator-worker-2
```

> Точные имена реплик можно подсмотреть через
> `docker compose -f docker-compose.demo.yml ps` — compose нумерует их
> `<project>-worker-1`, `<project>-worker-2`.

- [ ] В одном из терминалов появляется строка про claim `PAR-001`.
- [ ] **В другом** (не в том же!) — claim `PAR-002`.
- [ ] Через несколько секунд оба сообщают `COMPLETE` — параллельно.

Если хочется одного объединённого лога — просто:

```bash
docker compose -f docker-compose.demo.yml logs -f worker
```

(compose префиксует строки именами реплик).

---

## Шаг 7 — Подтверждаем параллельность через БД

Сразу после `up -d --scale worker=2` (пока stub Claude ~2с обрабатывает
задачу) — поймать «middle frame»:

```bash
docker compose -f docker-compose.demo.yml exec postgres \
  psql -U whilly -d whilly -c "
    SELECT id, status, claimed_by, claimed_at
    FROM tasks
    WHERE plan_id='parallel'
    ORDER BY id;"
```

- [ ] У `PAR-001` и `PAR-002` **разные** значения `claimed_by`.
- [ ] Оба `claimed_at` в пределах нескольких секунд друг от друга
      (доказывает, что claim'ы шли параллельно, а не последовательно).

После завершения:

```bash
docker compose -f docker-compose.demo.yml exec control-plane \
  whilly plan show parallel
```

- [ ] Оба `DONE`.

---

## Шаг 8 — Audit-log для слайда

```bash
docker compose -f docker-compose.demo.yml exec postgres \
  psql -U whilly -d whilly -c "
    SELECT task_id, event_type, ts, detail->>'worker_id' AS worker_id
    FROM events
    WHERE plan_id='parallel'
    ORDER BY id;"
```

- [ ] У каждой задачи есть `CLAIM` и `COMPLETE`.
- [ ] `worker_id` в claim'ах — две **разных** строки.

> Это «доказательство параллельности» для презентации в одном кадре:
> два разных `worker_id` берут две разные задачи в одном плане
> с пересекающимися timestamp'ами.

---

## Шаг 9 — Финал и cleanup

```bash
# Остановить, БД сохранить (быстрый рестарт)
docker compose -f docker-compose.demo.yml down

# Полный сброс (включая том БД)
docker compose -f docker-compose.demo.yml down -v
```

---

## Раскадровка для слайдов (~2 минуты)

| Кадр | Что показать                                                | Команда |
|------|-------------------------------------------------------------|---------|
| 1    | Архитектура (диаграмма из этого файла)                     | слайд   |
| 2    | Стек поднялся: postgres + control-plane + 2 worker'а       | `docker compose ps` |
| 3    | Воркеры зарегистрировались, у каждого свой ID              | `psql ... SELECT worker_id, hostname FROM workers` |
| 4    | План импортирован, 2 задачи `PENDING`                      | `whilly plan show parallel` |
| 5    | Live-логи двух воркеров рядом                              | два `docker logs -f` |
| 6    | Middle frame: 2 разных `claimed_by` в `tasks`              | `psql ... SELECT id, status, claimed_by FROM tasks` |
| 7    | Финал: оба `DONE`, audit-log с двумя `worker_id`           | `whilly plan show parallel` + events |

---

## Если что-то пошло не так

| Симптом                                           | Что проверить |
|---------------------------------------------------|---------------|
| Только один воркер claim'ит задачи                | `docker compose ps` — реально ли 2 реплики; в env воркера должен **не быть** WHILLY_WORKER_TOKEN (тогда регистрируется), и **не быть** WHILLY_WORKER_ID (тогда уникален). |
| `register failed: 401`                            | Не совпадают `WHILLY_WORKER_BOOTSTRAP_TOKEN` у control-plane и worker. По умолчанию обоим прописан `demo-bootstrap`. |
| Воркеры стартуют и сразу падают (`Restarting`)    | `docker compose logs worker` — обычно `WHILLY_PLAN_ID` пустой или control-plane не успел подняться. |
| Задачи висят в `PENDING`, воркеры в `running`     | План импортирован с другим `plan_id`, чем `WHILLY_PLAN_ID` у воркеров. Сверьте: `whilly plan show parallel` vs `docker compose exec worker env | grep PLAN`. |
| `claimed_by` совпадает у обеих задач              | Воркер ровно один (scale не сработал), либо первый воркер успел оба раза — увеличьте время stub'а или добавьте `sleep` в `fake_claude.sh`. |
