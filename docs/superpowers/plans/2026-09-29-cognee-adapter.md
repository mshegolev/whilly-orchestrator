# Cognee Memory Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подключить локальный Cognee к канонической памяти Whilly и передавать проверенный контекст главному агенту и исполнителям Claude/Codex.

**Architecture:** PostgreSQL остаётся источником истины. Изолированный процесс Cognee ранжирует только разрешённые проверенные записи во временном индексе; приложение перепроверяет возвращённые ID и версии. Контекст привязывается к одобрению и повторно проверяется перед запуском исполнителя.

**Tech Stack:** Python 3.12, PostgreSQL/Alembic, FastAPI, pytest, Cognee 1.6.1 в отдельном venv, локальные embedding/GLiNER модели; macOS sandbox-exec для текущей установки.

**Spec:** `docs/superpowers/specs/2026-09-29-cognee-adapter-design.md`

## Global Constraints

- Одна одновременная индексация на локальную установку; очередь ограничена четырьмя ожидающими запросами. Межпроцессные ограничения, не только asyncio.Lock.
- Начальные защитные пределы: 32 записи, 128 KiB суммарного входа и 90 секунд на запрос, включая очередь и очистку. Превышение именуется явно.
- Cognee не получает секреты, raw-сессии, доступ к репозиториям или полномочия запускать задачи. Генеративный LLM не используется.
- Локальные модели предзагружены; рабочему процессу запрещена сеть. Если изоляция недоступна, backend unavailable; небезопасного fallback нет.
- Временные данные ограничены приватным каталогом запроса. После завершения/отмены/timeout процесс завершается и reap выполняется до подтверждения очистки.
- Только verified, разрешённые, непросроченные, непротиворечивые записи с подтверждённым источником. Непроверенные записи — именованные пропуски.
- Отключённый Cognee сохраняет L1; сбой выбранного Cognee блокирует зависимое планирование. Отключение backend не отменяет проверки уже одобренных привязок.
- Публичные файлы содержат только нейтральные примеры. Настройки установки/секреты не коммитятся. Существующий dirty worktree сохраняется.
- До пяти дешёвых субагентов одновременно, без дорогого fallback; задачи 1 и 2 независимы, затем 3 → 4 → 5. DB-тесты выполняет контроллер последовательно.
- OpenSpec propose → apply → archive. Не архивировать незавершённые соседние инкременты SWARM-LEARNING. Коммитить только доказуемо собственные изменения после проверки; смешанные исходные файлы оставить без коммита.

## Review Focus

1. Большое тело и огромные поздние omissions: упаковка должна завершаться и соблюдать итоговый JSON-бюджет (задача 1).
2. Отзыв записи между разрешением чтения и созданием копии: после redaction нельзя зарегистрировать новую копию (задача 3).
3. Два web/worker процесса и аварийный выход владельца: один индекс, ограниченная очередь, безопасная уборка без удаления чужих каталогов (задачи 2–3).
4. Подложенные/повторные ID, лишнее тело, Unicode и обрезанный stdout: протокол не должен обходить канонический источник или лимит байтов (задачи 2–3).
5. Истечение срока после одобрения и отключение backend: запуск блокируется независимо от флага; новые посторонние знания одобрение не отменяют (задачи 1, 4).

## Task 1: Исправить L1 и привязки контекста

**Files:** Modify `whilly/swarm/learning/memory.py`, `whilly/swarm/learning_binding.py`; tests `tests/unit/test_learning_memory_2.py`, `tests/unit/test_learning_binding.py`. Create `openspec/changes/add-cognee-memory/{proposal.md,design.md,tasks.md,specs/swarm-memory/spec.md}`.

**Interfaces:** Сохранить `build_context(items, *, max_chars, now) -> ContextPackage`; добавить необязательный `omissions: tuple[str, ...] = ()`, чтобы все метаданные упаковывались вместе. Сохранить публичные сигнатуры `validate_binding` и `bound_worker_prompt`; в последнем проверять именно snapshot, из которого строится prompt, без непроверенного повторного чтения.

- [ ] Записать OpenSpec изменение с требованиями и сценариями из согласованной спецификации; проверить `openspec validate add-cognee-memory --strict`.
- [ ] Добавить `test_oversized_metadata_terminates`: отдельный subprocess с deadline 2s, `assert child.exitcode == 0`; `test_late_omissions_fit_budget`: `assert len(render_context(package)) <= 256`, включая Unicode/длинный URI/тысячи omissions.
- [ ] Добавить параметризованный `test_bound_revision_loses_eligibility` для expires/conflicts/retracted/source unavailable; ожидать ошибку до передачи prompt. `test_unrelated_revision_keeps_binding` должен успешно валидировать прежнюю привязку.
- [ ] Запустить новые точечные тесты `.venv/bin/python -m pytest -q tests/unit/test_learning_memory_2.py tests/unit/test_learning_binding.py -k 'oversized_metadata or late_omissions or loses_eligibility or unrelated_revision'`; зафиксировать RED без оставшихся дочерних процессов.
- [ ] Заменить повторяющееся сжатие метаданных конечным алгоритмом удаления элементов; считать размер окончательного JSON, включая все пропуски. Дополнить проверку bound snapshot сроком, конфликтами, статусом и источником непосредственно перед построением prompt.
- [ ] Повторить выбранные тесты и оба файла полностью, затем review диффа: GREEN, отсутствие вечных циклов и обхода binding при выключении памяти.

## Task 2: Изолированный Cognee worker и ограниченный протокол

**Files:** Create `whilly/swarm/learning/retrieval.py` (чистые DTO/порт), `whilly/adapters/memory/cognee_process.py` (родитель), `whilly/adapters/memory/cognee_worker.py` (child), `whilly/adapters/memory/cognee_sandbox.py` (OS-политика), `requirements/cognee-worker.txt`; tests `tests/unit/test_cognee_process.py`, `tests/integration/test_cognee_worker.py`. Добавить package initializer при необходимости.

**Interfaces:** Frozen `RetrievalRecord(id: str, body: str)`; `RetrievalRequest(query: str, records: tuple[RetrievalRecord, ...])`; `RetrievalResult(ids: tuple[str, ...], elapsed_ms: int)`. Порт `Retriever.rank(request: RetrievalRequest, *, request_dir: Path, deadline: float) -> RetrievalResult` async; `RetrievalUnavailable(code: str)`. `CogneeRetriever` реализует порт; deadline — monotonic абсолютное время.

- [ ] Добавить `test_protocol_rejects_invalid_output`: duplicate/foreign IDs, unknown JSON keys/body, malformed/truncated JSON → именованная ошибка; `test_utf8_input_cap` проверяет `len(serialized.encode('utf-8')) <= 131072` для всего envelope, не только тел.
- [ ] Добавить fake-child тесты `test_timeout_kills_process_group`, `test_cancel_reaps_child`, `test_stdout_cap`: предел ответа 16 KiB, stderr тоже 16 KiB без записи тел в operational logs; выход и отсутствие оставшегося потомка проверяются фактически.
- [ ] Запустить `.venv/bin/python -m pytest -q tests/unit/test_cognee_process.py`, получить RED; реализовать строгий versioned JSON stdin/stdout без shell, 32 записи, ограничение query до 4096 символов и общий byte cap. Сокращение набора выполняет coordinator с именованными omissions, не worker молча.
- [ ] Реализовать sandbox deny-default: read только runtime/dependencies/необходимых OS ресурсов/предзагруженных моделей и собственного request dir; write только request dir; сеть запрещена, env allowlist без API-ключей. Все Cognee/SQLite/LanceDB/temp/log/history пути внутри request dir, model cache read-only; unsupported OS → unavailable.
- [ ] В отдельном venv закрепить совместимые версии Cognee 1.6.1 и зависимостей по проверенному локальному пилоту, не менять основной venv. Реализовать non-generative ingestion и поиск; stdout только IDs, тела ответа не принимаются.
- [ ] Повторить unit GREEN. Добавить реальные negative probes `test_sandbox_denies_network_and_foreign_file` (локальный слушатель и контрольный файл вне allowlist), positive model-load/index probe; проверить отказ сети именно из дочерней среды, не только содержимое профиля.

## Task 3: Coordinator, межпроцессные leases и redaction

**Files:** Create `whilly/swarm/learning/lookup.py` (use case), `whilly/adapters/filesystem/memory_leases.py` (локальные leases); modify `whilly/swarm/learning/memory.py`, `whilly/swarm/learning/ports.py`, `whilly/adapters/db/learning_memory.py`; tests `tests/unit/test_memory_lookup.py`, `tests/integration/test_memory_leases.py`.

**Interfaces:** `LookupService.context(principal: Principal, product_id: str, project_ids: tuple[str, ...], query: str, *, max_chars: int) -> ContextPackage` async. `LookupService.redact(principal: Principal, revision_id: str) -> RedactionResult` async; frozen `RedactionResult(redacted: bool, purge_pending: bool)`. `LeaseStore.reserve(deadline: float)` async context manager → `LookupLease`; lease has `id: str`, `directory: Path`, async `register(revision_ids: tuple[str, ...])`, `cancelled() -> bool`. `LeaseStore.fence()` async context manager сериализует регистрацию и redaction; `cancel_revision(id: str) -> tuple[str, ...]`, `pending(ids: tuple[str, ...]) -> bool` async.

- [ ] Добавить тесты: `test_acl_before_index` → чужое тело никогда не поступает fake retriever; `test_only_verified_current_records` → invalid записи только omissions; `test_canonical_reread_rejects_changed_revision` → версия, изменившаяся в поиске, не выдаётся; `test_foreign_id_rejected` → ошибка, не пустой успешный ответ.
- [ ] Добавить двухпроцессные тесты `test_one_active_four_waiters`, `test_redaction_fences_registration`, `test_crashed_owner_cleanup_preserves_foreign_directory`. Использовать barriers, а не случайные sleep: redaction конкурирует с register до копирования тела; после tombstone новый child с этой записью не запускается.
- [ ] Запустить unit и lease-тесты, получить RED. Реализовать проверку ACL/store-visible и Git/expiry/conflicts до индексации; отбирать детерминированно до 32/128KiB с omissions. Пустой eligible набор возвращает пустой пакет без запуска Cognee.
- [ ] Реализовать приватный installation lease root 0700, request dirs 0700 и manifests 0600 только с opaque revision IDs. Пять flock slots ограничивают active+waiting, отдельный execution flock ограничивает индекс одним процессом. Async ожидание неблокирующее, cancellation-aware, единый deadline; удержание execution lock до reap/cleanup.
- [ ] Под коротким межпроцессным fence повторно читать canonical eligibility и регистрировать IDs до передачи тела child. Redaction под тем же fence проверяет права, делает canonical tombstone, выставляет cancel markers затронутым leases; затем ждёт очистки вне fence. Coordinator опрашивает cancellation не реже 100ms и перед возвратом результата. Не удерживать fence во время Cognee.
- [ ] При redaction сразу закрыть выдачу, вернуть `purge_pending=true`, если cleanup не подтверждён. При сбое удаления оставить lease для повторной очистки; отсутствие маркера не считать успехом. Boot cleanup только adapter-owned manifests с незанятым owner lock, без следования symlinks/проверки лишь по PID.
- [ ] После поиска отвергнуть неизвестные/повторные IDs и заново проверить canonical snapshot/hash/ACL/source под fence; построить пакет только из него. Повторить unit и multiprocess tests: GREEN; проверить, что cleanup не освобождает execution slot раньше child exit.

## Task 4: Подключить chief, исполнителей, API и статус

**Files:** Modify `whilly/swarm/learning_binding.py`, `whilly/swarm/product_workflow.py`, `whilly/swarm/runtime.py`, `whilly/api/swarm_memory.py`, `whilly/adapters/transport/server.py`, `whilly/api/static/product-swarm.js`, `whilly/api/templates/product_swarm.html.j2`; create `whilly/adapters/memory/config.py` (composition/config). Tests: `tests/unit/test_learning_memory_3.py`, `tests/unit/test_learning_binding.py`, `tests/unit/test_product_workflow.py`, `tests/swarm_memory.test.cjs`, `tests/integration/test_learning_memory_store.py`.

**Interfaces:** Добавить keyword `query: str = ''` к `planning_context`; chief передаёт feature intent + пользовательский запрос с явной ошибкой превышения лимита. `WHILLY_MEMORY_BACKEND=l1|cognee`, default l1; `WHILLY_COGNEE_PYTHON`, `WHILLY_COGNEE_MODELS_DIR`, `WHILLY_COGNEE_STATE_DIR` задаёт host, не API caller. Admin-only GET `/api/v1/swarm/knowledge/status` → backend, ready, last_elapsed_ms, error_code; существующий GET принимает optional query; redaction ответ расширяется `purge_pending`.

- [ ] Добавить ASGI тесты `test_status_requires_admin`, `test_redaction_reports_pending`, `test_cognee_failure_blocks_plan` (нет planner dispatch), `test_disabled_backend_still_checks_existing_binding`; status без текстов/URI/секретов. Добавить JS test отображения empty/unavailable/ready через textContent.
- [ ] Добавить `test_both_engines_receive_same_approved_binding`: fake Claude/Codex child captures prompt, `assert claude_manifest == codex_manifest`; project-dependency filtering исключает неразрешённые IDs. Отдельно проверить expiry непосредственно перед каждым запуском.
- [ ] Запустить изменённые unit/JS тесты и получить RED; собрать зависимости на transport boundary, не импортировать Cognee в domain. Подключить coordinator ко всем memory lookup/redaction путям; сохранить прежние auth/CSRF проверки. В UI показать backend readiness и именованную ошибку, не маскировать пустую память.
- [ ] Запустить unit файлы и `node --test tests/swarm_memory.test.cjs`, import-linter test. Последовательно выполнить DB/API tests через существующий private test wrapper с точными файлами `test_learning_memory_store.py`, `test_product_schema_roundtrip.py`, `test_alembic_full_chain.py`; проверить migration036, rollback/upgrade тестовой БД и ACL с реальным PostgreSQL.
- [ ] Review GREEN: candidate нельзя повысить до verified из недоверенного API; readiness не означает, что реальные знания уже загружены. Реальная CLI-приёмка остаётся отдельной проверкой задачи 5.

## Task 5: Реальная приёмка и безопасное локальное включение

**Files:** Extend `tests/integration/test_cognee_worker.py`; create `docs/Cognee-Memory.md`; update OpenSpec change/tasks and coverage only for реализованный инкремент. Private installation config/runtime изменяет только контроллер, не субагенты.

- [ ] Добавить реальный synthetic smoke: current/expired/conflicted/foreign/deletion-canary записи, проверка returned IDs и canonical текста, cancel/redaction во время поиска, cleanup файлов и процессов. Фикстуры verified создаёт доверенный test setup, не публичный ingestion endpoint.
- [ ] Запустить smoke с реальным Cognee и OS sandbox, предзагруженными моделями и лимитом 90s; измерить cold/warm время. При превышении не увеличивать лимит молча: оставить backend выключенным и доложить измеренный blocker. Проверить negative network/file probes и пустой реестр отдельно.
- [ ] Проверить реальную CLI-передачу Claude/Codex без внешнего model call, если установленный CLI предоставляет достоверный offline validation режим. Если нет — отметить real-session acceptance как не выполненную, не подменять fake transport тестом и не запускать платный внешний вызов без отдельного разрешения.
- [ ] Выполнить focused Ruff, unit/integration/JS/architecture checks и `git diff --check`; сохранить команды, версии, результаты и ограничения в handoff без секретов. Независимый дешёвый reviewer проверяет границы доступа, гонки и откат.
- [ ] Перед live изменениями подтвердить отсутствие активных задач и снять приватную восстановимую копию конфигурации/локальной БД штатными средствами установки. Применить только проверенную миграцию036, настроить отдельный venv/model paths и backend flags, перезапустить существующий локальный сервис.
- [ ] После restart проверить health, admin status, empty knowledge, synthetic lookup и отзыв отдельной canary-записи в изолированном test scope; canary не попадает в IC planning context. Убедиться в отсутствии временных копий/процессов. Сбой → вернуть config flags и перезапустить, не делать разрушительный downgrade рабочей БД.
- [ ] Документировать отключение backend и сохранение binding validation; отсутствие массовой загрузки, расписаний, MCP/global CLI hooks и обещания физического стирания. Архивировать OpenSpec только при выполненных критериях; иначе оставить открытые пункты с точной причиной.

## Self-review / handoff

Покрытие спецификации: authority/ACL/provenance → 1,3; offline изоляция/протокол → 2; lifecycle/redaction/очередь → 3; approval/оба engine/UI → 4; реальные проверки/включение/откат → 5. Пять Review Focus закреплены именованными тестами. Интерфейсы между задачами указаны выше; новый постоянный индекс, автономные исследования и ingestion всех проектов не добавляются.

Статус: план одобрен и реализован дешёвыми субагентами с контроллером; локальная активация выполнена 2026-09-29. Исходные пошаговые чеклисты выше сохранены как инструкция, итоговый статус задач и evidence — ниже и в `docs/Cognee-Memory.md`.

- [x] Task 1: bounded L1 context and canonical approval binding.
- [x] Task 2: real offline SDK, strict protocol, enforced sandbox, process lifecycle.
- [x] Task 3: scoped coordinator, launch fencing, queue and recoverable cleanup.
- [x] Task 4: backend composition, chief/worker wiring, both-engine fake-child transport and status UI.
- [x] Task 5: scoped acceptance, backup, migration, local activation and browser verification.

Итог: 140 focused Python tests, 2 JS tests, 3 architecture contracts; отдельные PostgreSQL проверки 7+1. Полный suite не объявляется зелёным. Реальные платные CLI/model сессии не запускались; подтверждена передача через реальные transport adapters к локальным fake children. Локальный canary использовал отдельный синтетический product scope и был удалён; массовый импорт знаний не включался.
