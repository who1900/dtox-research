# Publication ownership and rollout boundary

**Owner note · 6 октября 2026**

**DEPLOYED — first-publication path only.** Rollout output: `Rollout complete`; backup `/opt/dtox-release-20261006/20261006T155411Z`, 15:54 UTC. Это не доказательство actual published job. Historical replacement не развёрнут; его статус остаётся BLOCKED.

Это текущая заметка о владельцах и следующих шагах. Она не заменяет и не переписывает исторические архитектурные аудиты или отчёт `architecture-fixes`.

## Когда публикация становится видимой

Хранилища не образуют распределённую транзакцию. Записи выполняются последовательно; каждый durable ACK фиксируется до перехода к следующей системе. Публикация становится видимой только после подтверждения всех обязательных частей одной ревизии:

1. `chunk_cache` присутствует, проверен и совпадает с ожидаемым `cache_hash` (это не original source cache);
2. Qdrant подтвердил запись новой ревизии;
3. chunk FTS подтвердил запись той же ревизии;
4. только после этих подтверждений одна SQLite-транзакция атомарно фиксирует видимое состояние paper и journal `published` / `done`.

Это атомарность SQLite visibility commit, не cross-store transaction: при частичном ACK внешние записи могут ожидать retry/repair, но остаются невидимы через publication gate. Неполные, ожидающие и ошибочные ревизии скрыты.

Метаданные каталога, coarse index и paper-level FTS — вторичные проекции. Их допустимо догонять eventual-consistency механизмом; они не являются доказательством публикации основного текста.

Visibility gate покрывает прямые fulltext-пути: обычный Qdrant search/cache, payload hydration, scroll и grouped hierarchical stage B. Coarse collection остаётся вторичной проекцией; она не заменяет проверку опубликованной fulltext-ревизии.

## Допуск новых документов и исторические замены

Путь первой публикации разрешён только действительно новым документам: `completion_history.first_known=1` и `completed=0`. Уже опубликованные или исторические документы не должны попадать в этот путь как «новые».

Whitepaper consumer: существующая `done` paper может получить immutable staged candidate; записи с `status != done` и stage/busy состоянием пропускаются. Genuinely new acquired whitepaper идёт обычным pipeline, где `first_known=1` записывается атомарно с admission. Исторические IACR/EIP/ERC/whitepaper замены — только staged, не published.

При admission fulltext/chunk consumers фильтруют `first_known` до `LIMIT`, чтобы занятые/legacy позиции не вытесняли genuinely new acquisition. Синхронизация метаданных меняет только `layers`/`year` через owned FTS rowids; body и cache не пересобираются. Publication требует строгий `parse_status='ok'`. При ошибке completion SQL транзакция откатывается; retry ACK сохраняется до успешного завершения и не допускает двойного учёта.

Только исторические IACR, EIP/ERC и whitepaper замены можно валидировать и ставить в source journal как **staged**. Это не означает published и не разрешает замену старых данных. Историческое применение остаётся **BLOCKED** до точной миграции legacy FTS-идентификаторов и переключения читателей на revision-aware old/new схему. Новая whitepaper acquisition уже идёт обычным pipeline. До миграции старая опубликованная версия и её индексы должны оставаться доступными.

Revision gate закрывает уже полученные points, но ожидающие marked points могут занимать места в Qdrant top-K до hydration и фильтрации. Это может временно снизить полноту выдачи. Такая просадка recall не является доказательством отсутствия документа или его содержания.

Непомеченные legacy points остаются видимыми для совместимости. Это не гарантия здоровья, целостности или полноты всего корпуса.

## Проверки и пределы доказательств

- Fresh main suites: API **833 PASS + 1 skipped**; pipeline **158 PASS + 5 skipped**; MCP **36 PASS**; ops **169 PASS**; x402 **46 PASS offline**. Итого **1242 PASS + 6 skipped**. Native subsets не прибавлены повторно.
- Native Linux subsets, не добавленные к total: API **54 tests OK, 0.783 s**; pipeline **72 tests OK, 27.826 s**, real `fcntl` preimport.
- Post-deploy rollout report: complete; backup `/opt/dtox-release-20261006/20261006T155411Z`, 15:54 UTC. Main confirms 3 services active, NRestarts=0, hashes match all 6 files; publication journal tables exist, `sample[]` and `chunked[]` empty. Actual published-job proof pending.
- GitHub MDX: acquisition `spec-markdown`, extraction `markdown_source`, exact_latex=false; stored acquisition `latex`, hash и URL сохранены. Provenance only, not ranking.
- Expanded reads-after: 9 tools / 12 HTTP, 8.887 s, errors 0, но partial у spec и bundle — полнота FAIL. Новые discovery IDs `2605.14306` / `2604.03173`; bundle те же ACL IDs, 907 chars; compare tables 0 (ожидаемое отсутствие).
- Semantic-after: тот же набор IDs для 6 probes, новых изменений не наблюдалось. Gold labels нет: это не accuracy/recall PASS. `validate_project` для DAG вернул success, прежняя DA-sampling ошибка остаётся необъяснённой.
- `get_verdict_message` ранее ответил успешно без chain write. Покрытие eventual read tools не считать полным: stable reads profile покрывает 9 перечисленных reads; validate/message проверялись отдельно.
- Эти данные не доказывают полноту legacy корпуса, отсутствие будущих bugs или завершённую published job.

## Архитектурная опора и следующие шаги

Механизм остаётся в существующем `embed_step`/publication journal и shared FTS catalog с rowid-операциями, ограниченными owned rows. Новый service/broker и full scan не нужны. Доказательство глобальной уникальности legacy UUID mapping ещё pending; оно требуется до reader switch.

First-publication path развёрнут; дальнейшие gates:

1. Подтвердить первую фактическую published job с runtime/job evidence.
2. Runtime-хеши и журнал подтверждены. Реальная секция ровно 1000 символов; тестовый SSE клиент исправлен на UTF-8. Повтор исходного DA-sampling validate после выкладки успешен за 14.660 s; причина первого сбоя не установлена. Итог: [release report](2026-10-06-publication-release.md).
3. Диагностировать необъяснённую DA-sampling `validate_project` ошибку и проверить reads partial flags.
4. Согласовать legacy FTS UUID mapping и revision-aware readers до любого historical replacement. Историческая замена остаётся BLOCKED.
5. Отдельно оценивать semantic retrieval на gold set; текущие ID overlap observations не являются accuracy gate.

Не расширять deployed scope до исторических replacements без отдельного approval и migration gate. Не выполнять full scan/reindex или hardware changes; не добавлять новый service/broker, не раскрывать secrets и не затрагивать платежи.

## Ограничения области

Эта работа не меняет исторические audit-отчёты, не утверждает состояние старых Qdrant points по новому runtime-замеру и не выполняет live migration. Никакие secrets, платежные механизмы или аппаратная конфигурация не являются частью publication gate.
