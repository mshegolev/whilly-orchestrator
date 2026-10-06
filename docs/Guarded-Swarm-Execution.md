# Guarded swarm execution

Локальный full-cycle Whilly проходит только через coordinator-owned boundary:

1. обсуждение и планирование;
2. явное подтверждение revision и digest;
3. отдельный candidate repository;
4. worker в изолированной среде с отдельными `HOME` и `TMPDIR`;
5. coordinator-owned commit;
6. обязательные test, lint и architecture gates;
7. проверка неизменности SHA/tree;
8. независимый read-only reviewer;
9. локальный результат для передачи оператору.

Результат считается доверенным только по host-owned данным попытки: пути
candidate, base/head SHA, digest политики, структурированным GateEvidence и
вердикту независимого reviewer. Поля, присланные worker в JSON-результате,
не являются доказательством и не могут подменить эти значения.

В этой версии публикация в GitLab намеренно возвращает
`publication_unavailable`. Push, merge, deploy и реальные provider calls не
входят в локальный acceptance.

## Ограничения и откат

Если на хосте нет `sandbox-exec`, не удаётся ограничить credential source или
не проходит deny-probe, запуск блокируется с именованной причиной. Откат
безопасен: остановить локальный coordinator, сохранить candidate/logs для
диагностики и удалить только временные demo-артефакты; зарегистрированные
checkout и remote не изменяются.
