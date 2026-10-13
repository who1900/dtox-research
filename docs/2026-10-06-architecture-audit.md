# Архитектурный аудит dtox-research

**Дата:** 2026-10-06  
**Тип:** read-only review, без тестовых прогонов и изменений кода  
**Основной вывод:** основной долг здесь в управлении состояниями и ownership индексов, а не в выборе Qdrant или BM25. Направление решения: модульный монолит с одним владельцем синхронизации, без новых сервисов.

## Рамки и достоверность

Production-наблюдения ниже включают проверку живого сервера, таймера и repair ledger. Hash `pipeline/service.py` совпал с production; hash `pipeline/extractor.py` также совпал. Только нормализованный LF hash `api/main.py` отличается: локальный `070e1e...`, production `c7fc69...`; локальные API-наблюдения не объявляются production-фактами.

Других файлов не изменяли. Не выполнялись deploy, платежи, операции с секретами или commits. Performance- и quality-показатели не оценивались.

## Выводы по приоритету

### P1. Ошибка parser может превратиться в успешный короткий текст

**Production-подтверждено.** В `pipeline/service.py:2818-2829` catch широкого исключения parser-а создаёт fallback из первых 1700 символов. Он записывается в chunk cache, затем выставляются `status=chunked` и `extractor_version=4`; downstream получает короткий fallback как chunked.

**Риск:** неполное извлечение выглядит как успешно обработанный документ и расходится с реальным coverage. Это проблема статуса и качества результата, не основание менять backend поиска.

**Конвергенция:** возвращать явный parser result со статусом и quality/provenance. Parser failure должен оставлять paper пригодным к безопасному повтору и не заменять существующие хорошие индексы коротким fallback-текстом. Локальный файл для сверки, но не утверждение о line parity: [pipeline/service.py](../pipeline/service.py), [pipeline/extractor.py](../pipeline/extractor.py).

### P1. Потеря старого индекса до проверки возможности восстановления

**Production-подтверждено.** Reset helper в [ops/reprocess_lost_body.py](../ops/reprocess_lost_body.py#L205) поддерживает удаление FTS, но включённый и активный timer запускает `--feed --apply --skip-fts`: пустые `fts_ids` пропускают FTS delete, поэтому FTS rows остаются. Этот запуск удаляет Qdrant points и caches до успешного refetch, что может оставить stale lexical rows. Timer задан в [dtox-reprocess.timer](../ops/deploy/contabo/dtox-reprocess.timer) и [dtox-reprocess.service](../ops/deploy/contabo/dtox-reprocess.service); one-shot phase неактивна между 15-минутными запусками, это ожидаемо.

**Риск:** если повторный fetch или parse не удастся после удаления, прежние Qdrant points и caches уже утрачены, а оставшиеся FTS rows могут быть устаревшими. Текущий one-shot график сам по себе не является проблемой.

**Конвергенция:** сначала получить и проверить replacement, затем атомарно публиковать новую revision; предыдущую revision сохранять до подтверждённого commit всех требуемых представлений. Feed может готовить работу, но destructive apply должен быть revision-aware и resumable.

### P1. Repair добавляет пропущенный текст в конец section

**Подтверждён кодом; live wrong-order не подтверждён.** Planner ставит новым chunks `max(existing chunk_index) + 1` в [plan_missing_prose_repair.py](../ops/plan_missing_prose_repair.py#L515). Exact-section reader сортирует chunks по `chunk_index` и собирает текст в этой последовательности: [api/main.py](../api/main.py#L6713), [api/main.py](../api/main.py#L6733). UUID точки идентифицирует chunk, но не исходную позицию в section.

Следствие условное: если добавленный prose принадлежит середине section, reader поместит его после существующих абзацев. Код доказывает такое поведение; unit test проверяет добавление пропущенного текста, но live-неверная последовательность абзацев не наблюдалась.

**Конвергенция:** read contract должен собирать canonical normalized section/assembly из source отдельно от stable point UUID. Если старый chunk уже содержит текст до и после пропуска, отдельный `source_position` для нового chunk не восстановит внутренний порядок. Single writer может materialize canonical section text и chunks как согласованные представления. Не менять старые UUID вслепую и не обещать исправление одной сортировкой.

### P2. Repair отдельно владеет записью в индексы, а штатная очередь не является заменой

Новый repair состоит из [plan_missing_prose_repair.py](../ops/plan_missing_prose_repair.py), [apply_missing_prose_repair.py](../ops/apply_missing_prose_repair.py) и [repair_prose_corpus.py](../ops/repair_prose_corpus.py), около 2129 строк суммарно по проверенному снимку. Он вводит собственный ledger, journal и координацию записей. Обычная очередь в [pipeline/service.py](../pipeline/service.py#L3054) ведёт coarse index, paper FTS, chunk FTS и metadata, но не является полноценным writer-ом full-text vectors. Поэтому просто направить repair в существующую очередь нельзя.

**Риск:** два владельца синхронизации повышают число переходов состояния и способов получить расхождение между Qdrant, FTS, caches и metadata.

**Конвергенция:** один owner для index sync. Repair должен производить валидированный change job для этого owner-а. Сохранить journal и crash-resume, чтобы миграция не ослабила recovery.

### P2. FTS collision probe не доказывает глобальную уникальность UUID

В [apply_missing_prose_repair.py](../ops/apply_missing_prose_repair.py#L544)-[apply_missing_prose_repair.py](../ops/apply_missing_prose_repair.py#L567) collision check строит ограниченный AND-запрос по четырём или более различным английским словам длиной от пяти символов. `point_id` в FTS unindexed, поэтому запрос не может установить, существует ли где-либо именно этот UUID; текст с недостаточным числом подходящих токенов блокируется даже при потенциально допустимом repair.

**Production-состояние:** три записи `manual_attention` имели journal в состоянии `prepared`. Это не partial-corruption и не свидетельство частичной мутации.

**Конвергенция:** проверять идентичность по индексируемому ключу/receipt или side catalog с точной связью `point_id -> row_id`, а не выводить идентичность из лексического совпадения.

### P2. Глобальный `data_version` может вызывать starvation, но live hang не доказан

Preflight в [apply_missing_prose_repair.py](../ops/apply_missing_prose_repair.py#L704)-[apply_missing_prose_repair.py](../ops/apply_missing_prose_repair.py#L707) отвергает snapshot при изменении SQLite `data_version`. Любая обычная запись в FTS способна инвалидировать preflight независимо от paper, который ремонтируется.

**Оговорка:** это риск повторных конфликтов или starvation при активных FTS writes, а не доказанный live hang.

**Конвергенция:** заменить глобальную версию на revision/receipt конкретного paper и проверять конфликт именно этой revision.

### P2. Runner определяет transient-сбой по тексту сообщения

[repair_prose_corpus.py](../ops/repair_prose_corpus.py#L678)-[repair_prose_corpus.py](../ops/repair_prose_corpus.py#L687) классифицирует ошибки через поиск английских фраз и слов в `str(exc)`. Изменение текста исключения может незаметно перевести retry в blocked или наоборот.

**Конвергенция:** typed error code с явным `retryable`, причиной и безопасностью повторного применения; строка сообщения остаётся только для диагностики.

### P2. Завершённый frontier не равен полной проверке

В [repair_prose_corpus.py](../ops/repair_prose_corpus.py#L578)-[repair_prose_corpus.py](../ops/repair_prose_corpus.py#L583) итог `frontier_drained` и exit code 0 сами по себе не меняют `papers.status` и не заявляют healthy. Проблема в том, что итоговая формулировка может не различать «не проверено» и «полностью проверено», а `unresolved_count` не покрывает `skipped_missing_source`, cache и unsupported состояния.

**Оговорка:** это не скрытый переход paper в `done`. Это неполный итоговый отчёт о coverage.

**Конвергенция:** отдельно показывать `inspected`, `resolved`, `safe_out_of_scope` и `unresolved`; отсутствие входных данных не считать успешным подтверждением полноты.

### P2. Обычный build cursor и top-up имеют разные гарантии

[build_fts.py](../pipeline/build_fts.py#L138)-[build_fts.py](../pipeline/build_fts.py#L150) записывает страницы и cursor, но штатно завершённое значение `None` означает начало нового полного прохода при следующем запуске. Повторные строки при этом не защищены явным уникальным ограничением в этом writer-е. `--topup` дедуплицирует по snapshot уже имеющихся `point_id` и добавляет новые ID в набор по мере прохода, но не учитывает canonical side catalog.

Это не основание утверждать, что каждый top-up создаёт дубликаты. Это разные контракты resume и dedupe, которые должны быть явными.

**Конвергенция:** отделить `completed` от `cursor=None`, а dedupe основывать на одном canonical identity catalog для всех writer-ов.

### P2. Whitepaper mastercoin имеет устаревшую provenance

[whitepapers.py](../pipeline/whitepapers.py#L45) всё ещё связывает slug `mastercoin` с Tendermint, а [whitepaper_sources.json](../api/whitepaper_sources.json#L11) содержит runtime override. Это признак разошедшейся source identity, а не повод наращивать каталог исключений.

**Конвергенция:** canonical source migration с сохранением legacy alias и проверяемой provenance.

## Вторичные локальные наблюдения, не production-верификация

- Search path в локальном [api/main.py](../api/main.py#L2448) разветвляется по hierarchical, chunk lexical и paper lexical режимам; отдельный paper-level ranker находится в [api/main.py](../api/main.py#L6258). Это выглядит как накопленная матрица экспериментов. Конвергенция: выбрать одну production search policy, вынести общие channel status и fusion, после измерений удалить выключенные эксперименты и one-offs. Это не утверждение о production-конфигурации.
- В ветке без rank channels tie-break использует множество в [api/main.py](../api/main.py#L1258), тогда как соседняя ветка сохраняет encounter order. Равные fusion scores могут иметь нестабильный порядок. Это только локальная гипотеза до проверки production hash.
- Локальный hierarchical fallback в [api/main.py](../api/main.py#L1141) возвращает legacy результаты после исключения; marker добавляется только при `fallback_reason == "coarse_fallback"` в строке 1147. Для прочих причин fallback degraded status может не попасть в ответ. Это только локальная гипотеза до проверки production hash.
- X402 дублирует контракт: [tools.ts](../x402-gateway/src/tools.ts#L120) задаёт `get_code` с четырьмя полями и constraints, [payments.ts](../x402-gateway/src/payments.ts#L59) держит JSON Schema с `paper_id` и `target_elements`. В [upstream.ts](../x402-gateway/src/upstream.ts#L63) повторяется English regex-классификация сообщений. Свести к общей Zod source schema и generated JSON Schema; сохранить запрет signed upstream retry в [upstream.ts](../x402-gateway/src/upstream.ts#L81). `X402_EVM_NETWORK` alias не считать дефектом.

## Live ledger snapshot

Первый наблюдавшийся ledger snapshot: 447 records = 6 `applied_partial` + 277 `blocked` + 3 `manual_attention` + 14 `no_change` + 1 `retry` + 146 outside. Отдельно зафиксировано 41 new chunks; chunks и records являются разными единицами учёта.

Эти числа не являются количеством повреждённых papers, независимой проверкой Qdrant vectors или свидетельством полной проверки корпуса. Три `manual_attention` означали `prepared` journals, не partial corruption.

## Сохраняемые защиты

Не ослаблять WAL, atomic cache writes, deadlines, resource caps, dry-run, проверки старых vectors, source provenance и authentication. Они являются полезными инвариантами. Selective patch safety для dirty tree тоже легитимна. Временные AST splicers [prepare_audit_fix_patch.py](../ops/prepare_audit_fix_patch.py#L95) и [prepare_paper_timeout_patch.py](../ops/prepare_paper_timeout_patch.py#L47) не следует превращать в постоянный release path: выпускать консолидированный artifact из известной revision, сохраняя пользовательские dirty edits.

## Приоритетный план

1. Ввести parser result status/quality и сделать reprocess nondestructive до успешной проверки replacement.
2. Разделить stable UUID identity и canonical normalized section assembly from source; обеспечить корректный порядок даже когда старый chunk пересекает место пропуска.
3. Свести штатную индексацию и repair к одному index-write contract с durable journal и typed errors.
4. Выбрать одну production search policy, унифицировать channel status/fusion, затем убирать выключенные эксперименты после evidence.

## Минимальные acceptance invariants

- Parser exception не может дать статус завершённого качественного извлечения.
- При неудачном повторном parse прежний paper/index остаётся доступным.
- Вставка prose в gap середины section возвращает правильный порядок и не меняет UUID существующих chunks.
- Несвязанная FTS запись не инвалидирует preflight; конфликт revision того же paper блокирует apply.
- После crash/resume повторная операция идемпотентна и все требуемые индексы сходятся.
- Отсутствующие inputs не отображаются как healthy completion; safe out-of-scope отделён от uninspected.

