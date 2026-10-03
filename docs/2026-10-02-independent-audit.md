# DTOX Research: независимый аудит

Дата: 2026-10-02. Тип: аудиторский вывод, не изменение системы. Язык: русский.

## Итог

**Сильная рабочая beta, не production-ready.** Поиск действительно возвращает полезные первоисточники, формулы и алгоритмы; provenance, ограничения области и бюджет ответа часто оформлены честно. Но целостность извлечения, восстановление pipeline после исключений, согласованность отчетов и запас ресурсов пока не позволяют считать сервис надежным платным production.

Субъективная оценка идеи: **8/10**. Текущей read-only beta: **около 6,5/10**. Платный production не оценивается без E2E оплаты, settlement и записи в цепь. Это экспертное суждение, не рассчитанный benchmark. Потенциал hackathon submission высокий; допуск paid mainnet остается закрытым. Эти два решения нельзя объединять.

## Границы и происхождение доказательств

- Основа: свежие live read-only проверки от 2026-10-02, локальные изолированные воспроизведения, короткие тесты и анализ исходников с привязками file:line.
- Production проверялся только read-only: без транзакций, платежей, подписанных записей и deployed fixes. Два write-инструмента проверялись на временных локальных fixtures, не в production.
- Локальный HEAD и проверенный GitHub main: `7e22e68fb2346e882a2cd9f7d5c396cdcc8cf161`. Последний успешный [CI run](https://github.com/who1900/dtox-research/actions/runs/36922278149) относится только к этому HEAD. Dirty API/attestor fixes и новые материалы не опубликованы. Успех этого CI нельзя переносить на dirty working tree.
- LF-normalized SHA-256 шести локальных файлов совпал с deployed source: `api/main.py`, `mcp/server.py`, `pipeline/service.py`, `pipeline/extractor.py`, `attestor/src/server.ts`, `attestor/src/sas.ts`. Это проверка шести исходников, не всего deployment, dist, зависимостей или revision.
- File:line ниже относится к текущему локальному исходнику. Например, latency gate находится в `ops/monitor.py:128`, а `as_of` добавляется в `api/main.py:4576` после вызова `_corpus_stats()`.
- Статусы доказательств: **live** означает ограниченное наблюдение работающего сервиса; **fixture** означает изолированное локальное воспроизведение; **code** означает проверенный control flow; **риск** означает возможное последствие, не произошедший инцидент.

## Findings

### F01. P1: исключение может оставить открытой транзакцию pipeline

**Доказательства:** `pipeline/service.py:1346`, `pipeline/service.py:3288`, `pipeline/service.py:3290`, `pipeline/service.py:3262`.

`get_conn()` использует обычный SQLite connection с implicit transactions. В обработчике ошибки `_stage_loop` нет `conn.rollback()` и connection продолжает использоваться. В изолированном SQLite fixture последовательность INSERT -> exception оставила `in_transaction=True`; второй writer получил блокировку. Watchdog наблюдает heartbeat и длительность busy, а ошибка обновляет heartbeat и переводит stage в backoff. Повторение ошибок с живым heartbeat может продолжаться без реального восстановления.

**Статус:** code + fixture, латентный failure mode. В свежем tail не обнаружен `database is locked`; текущая блокировка production не доказана. Нельзя объявлять этот дефект причиной сегодняшнего медленного роста done.

**Критерий исправления:** rollback на ошибке, корректный жизненный цикл connection, наблюдение последнего успешного прогресса отдельно от heartbeat; regression с двумя writers и ошибкой после INSERT. Сейчас это план, не выполненная правка.

### F02. P1: extractor может повреждать точный исходный код

**Доказательства:** `pipeline/extractor.py:166`, `pipeline/extractor.py:169`, `pipeline/extractor.py:455`.

Удаление LaTeX-комментариев применяется ко всем строкам до разбора окружений. Literal `%` внутри `verbatim`/`lstlisting` не защищен. Проверка валидного `verbatim` в изолированном воспроизведении подтвердила удаление `% 2;` из `int rem = value % 2;`, оставив `int rem = value`: это изменение семантики кода, а не косметика. Для продукта, обещающего точные алгоритмы и код, выбран P1.

**Статус:** code + fixture. Частота повреждений в корпусе неизвестна; не утверждается, что повреждены все листинги или уже выданный MEV-ответ. Нужны тесты защищенных окружений и адресная проверка затронутых документов, не слепая массовая переиндексация.

### F03. P1: емкость почти исчерпана, но OOM не наблюдался

**Доказательства:** live cgroup/Qdrant/disk срез; политика диска: `ops/monitor.py:54`, `ops/monitor.py:419`.

Qdrant использует 9,877-9,882 GiB при лимите 10 GiB, около 98,8%. Contabo имеет 39,14 GiB свободно и 90% used; monitor уже возвращает FAIL по порогу 40 GiB. Oracle имеет 120,27 GiB свободно и 38% used. Это разные диски и роли: свободное место Oracle не устраняет дефицит staging/API/pipeline state/FTS на Contabo.

**Статус:** live риск нехватки запаса. `oom=0`, `oom_kill=0`; накопленный `max=201192` за примерно три дня не увеличился между двумя сегодняшними чтениями. Защита от OOM и active guard timer не доказывают SLO или невозможность OOM. Причинная связь с latency не установлена.

**Критерий решения:** политика RAM и диска на основе общего бюджета хоста, модели нагрузки и безопасной retention. Нельзя вслепую поднимать лимит Qdrant выше 10 GiB на хосте с 12 GB RAM.

### F04. P2: registry, headline и scope описывают разные множества

**Доказательства:** `api/main.py:4901`, `api/main.py:4912`, `api/main.py:4923`, `api/main.py:5124`, `api/main.py:5135`, `api/registry_policy.py:4`.

В live fast-проверке GRPO корректно найдено evidence `2402.03300` из registry и выдан `prior_art_reported_on_similar_claim`. Одновременно ответ содержит `asserts=2`, `models=[]`, `on_record_total=1`, headline `0 read / 0 unread`, `pending=0`, `papers_on_subject=0` в outside-scope и `verification_required=false`. Pending по текущему claim и чтение по похожему claim не обязаны быть одним множеством, но ответ не объясняет эту границу. Проверка необходимости чтения зависит от `unread_strong`, а не от всей нерешенной registry evidence.

**Статус:** live + code. Это несогласованность аудиторского отчета и контракта полей, не доказанное нарушение security quorum. При `models=[]` публичные wallet-readings остаются pending, научное подтверждение автоматически не возникает.

**Критерий:** отдельные и явно названные множества current/similar/settled/pending/retrieved, согласованные headline и scope; regression на GRPO и linked/unlinked claims.

### F05. P2: coverage не канонизирует twins и неверно сообщает truncation

**Доказательства:** `api/main.py:3403`, `api/main.py:3168`, `api/main.py:5085`.

Coverage складывает raw paper IDs в set. Fixture с четырьмя twins дал coverage 4 при canonical facet count 1. Кроме того, `is_lower_bound` сравнивает число уникальных papers с cap, которым ограничены chunk hits. При 40 hits одного paper флаг оказался false, хотя выборка достигла cap и могла быть усечена.

**Статус:** code + fixture, без сканирования корпуса. Само отличие raw web3 count от canonical count не является ошибкой; ошибочно отсутствие единой семантики внутри report. Нужно разделить canonical papers, chunk cap и признак исчерпания запроса.

### F06. P2: trends превращает отбор без результатов в отсутствие литературы

**Доказательства:** `api/main.py:2700`, `api/main.py:2720`, `api/main.py:2724`, `api/main.py:2820`.

Live `research_trends('zero knowledge', web3, 2024..2026)` вернул `years=[]` и сообщение об отсутствии papers, хотя тема индексируется. Путь использует dense nearest-neighbour и общий cutoff 0,83. Локальная проверка этого пути поддержала вывод, что web3 semantic neighbours отсекаются. Ответ описывает отсутствие прошедших порог кандидатов как отсутствие документов.

**Статус:** live + code + fixture. Это конкретный дефект пути, не глобальный recall benchmark. Нужны layer-aware calibration, проверка полноты отбора и различение `no candidates`, `insufficient coverage`, `no papers`.

### F07. P2: Markdown теряет родительскую taxonomy и порядок текста

**Доказательства:** `pipeline/extractor.py:602`, `pipeline/extractor.py:609`, `pipeline/extractor.py:616`, `pipeline/extractor.py:629`.

Вложенные заголовки классифицируются без родительского контекста. У EIP-7702 есть body outline из 36 chunks, но подразделы Security размечены как `other`; запрос limitations сообщает, что соответствующего раздела нет. Это false negative taxonomy, не отсутствие security content в оригинале.

Fenced code сначала удаляется из prose, затем весь prose выводится раньше всех code blocks. Fixture `Before / code / After` стал `Before After / code`. Это нарушает чтение аргумента и привязку пояснений к листингу.

**Статус:** live для EIP-7702, code + fixture для механики. Нужны стек заголовков с наследованием раздела и последовательный emission элементов в исходном порядке.

### F08. P2: HAL после done не обновляется; dispatch не изолирует источник

**Доказательства:** `pipeline/service.py:3657`, `pipeline/service.py:3671`, `pipeline/service.py:3721`, `pipeline/service.py:1627`.

Seed использует INSERT OR IGNORE; после закрытия cursor latest-запрос не переоткрывается. Общий arXiv dispatch допускает cursor по `layer` и исключает ряд чужих prefixes, но не HAL. Локальные fixtures воспроизвели отсутствие refresh и возможность подобрать HAL cursor не тем dispatcher.

**Статус:** code + fixture. Масштаб production-пропусков не измерен. Нужны source ownership cursor, refresh-window и тесты exhaustion/reopen, без обхода внешних ограничений.

### F09. P2: повторное discovery не согласует state с индексами

**Доказательства:** `pipeline/service.py:1488`, `pipeline/service.py:1515`, `pipeline/service.py:1520`, `pipeline/service.py:2933`.

Повторное discovery может обновить layers/abstract у `done`, но не инициирует обновление coarse/FTS. Существующий `graph_unresolved` placeholder не переводится в `discovered`, а пустые title/year не заполняются этим update-path. Fixtures показали расхождение metadata и сохранение placeholder.

**Статус:** code + fixture. Нужны явные переходы состояния и idempotent reconciliation; количество рассинхронизированных production records неизвестно.

### F10. P2: replay chunk FTS не идемпотентен

**Доказательства:** `pipeline/service.py:2923`, `api/main.py:2117`, `api/main.py:2126`.

Запись chunk FTS использует INSERT без upsert по point ID. Replay может оставлять дубликаты и stale text. По наблюдаемой конфигурации основной hierarchical search не запускает chunk lexical upfront, что снижает риск для обычного поиска, но fallback/legacy продолжают использовать этот слой, а pipeline его наполняет. Размер chunk FTS около 16,16 GB.

**Статус:** code + fixture; размер и активный путь из live среза. Нужны idempotent replace/upsert, проверка stale rows и retention после оценки роли индекса. Удаление данных в этом аудите не выполнялось.

### F11. P2: обещание writes nothing неверно для локальной БД

**Доказательства:** `api/main.py:4321`, `api/main.py:4386`, `api/main.py:3790`, `api/main.py:3794`, `api/main.py:3808`, `mcp/server.py:631`, `x402-gateway/src/tools.ts:177`.

На изолированных временных fixtures `get_verdict_message` создал первый `claim_nodes` record. Запрос с invalid signature создал второй узел, хотя `judgments=0`. `_canonical_claim()` делает INSERT и commit до финальной проверки подписи. Поэтому подготовка сообщения не пишет в цепь, но не является полностью read-only для локального состояния.

**Статус:** code + fixture, не production exploit. Рост N nodes важен: `_similar_claim_nodes` читает все узлы и вычисляет сходство в полном проходе. Нужны точный контракт side effects, правила создания узлов и тест failed signature -> no unintended growth.

### F12. P2: Python MCP отдает ошибку как успешный tool result

**Доказательства:** `mcp/server.py:105`, `mcp/server.py:394`, `mcp/server.py:399`.

Live запрос неизвестного paper вернул JSON с `error`, но MCP `isError=false`. Общий Python helper возвращает error dict, а вызывающий код передает его как обычный результат. Агент, смотрящий только на протокольный статус, может трактовать отказ как успех.

**Статус:** live + code. Нужен единый error envelope и явный protocol error flag с regression для 400/401/404/429 и transport failure. x402 gateway отдельно проверяет root `error`; это не исправляет публичный Python MCP контракт.

### F13. P2, paid release gate: запись и settlement не атомарны

**Доказательства:** `x402-gateway/src/tools.ts:38`, `x402-gateway/src/upstream.ts:18`, `x402-gateway/src/upstream.ts:19`; локальная установленная зависимость `x402-gateway/node_modules/@x402/mcp/dist/esm/index.mjs:942`, `:972`, `:1046`; границы текущих тестов: `x402-gateway/test/tools.test.ts:34`, `:57`, `:78`, `x402-gateway/test/config.test.ts:5`.

В рассмотренном SDK flow verify предшествует handler, а обычный settlement следует за handler. Write-handler потенциально успевает записать в цепь до settlement failure. Отмена платежного пути по `isError` не откатывает уже выполненный внешний side effect.

Upstream проверяет root `error`/`isError`, но не классифицирует элементы `results`. Поэтому batch со всеми invalid signed items без root error не становится автоматически MCP error. Политика цены за attempt не установлена. Повторный transport-вызов также требует доказанной идемпотентности записи.

**Статус:** code и будущий gate. Это не доказанный mainnet exploit, не наблюдавшееся списание сегодня и не утверждение о недействующей проверке подписи. 15 gateway tests покрывают configuration и shadow/disabled routing, а не компенсацию, idempotency и paid settlement E2E.

**Критерий:** явно определить attempt/success billing; воспроизвести settlement failure после успешного handler, partial/all-invalid batch, retries и отсутствие двойной записи/оплаты. Сначала разрешенные testnet E2E, затем отдельное решение о mainnet.

### F14. P2: paid evidence bundle не соответствует бесплатному bundle-path

**Доказательства:** `x402-gateway/src/tools.ts:98`, `x402-gateway/src/tools.ts:108`, `x402-gateway/src/upstream.ts:37`, `mcp/server.py:268`.

Платный `get_evidence_bundle` проксирует обычный `search_research_paper`, а не публичный build-bundle path. Отличия аргументов, лимитов и доступа не доказывают новую сущность bundle. Значительная часть read-возможностей доступна бесплатно; политика дополнительной платной ценности пока не доказана.

**Статус:** code, продуктовый вывод. x402 transport является инфраструктурой оплаты, не автоматическим revenue engine. Adoption, конверсия, готовность платить и unit economics не измерены.

### F15. P2: публичные обещания опережают текущий контракт

**Доказательства:** `site/index.html:415`, `site/index.html:445`, `site/index.html:449`, `site/index.html:459`, `api/registry_policy.py:4`.

Страница пишет Twelve tools при фактических 13 публичных основных инструментах, утверждает, что independent wallets дают `confirmed_prior_art`, и сохраняет старый confirmed GRPO demo. Текущая policy различает signer identity и доказанную независимость reasoning; `models=[]` не дает автоматического научного подтверждения. Признание team demo wallets на странице полезно, но не устраняет противоречие соседнего утверждения.

**Статус:** live + code. Доступность страницы подтверждена HTTP 200; HTTP 401 на root сам по себе не доказывает поломку, поскольку корень защищен. Нужна согласованная публичная формулировка возможностей и границ, после публикации проверенного release. В этом задании страница не изменялась.

### F16. P2: self-host инструкция не доказана как turnkey

**Доказательства:** `README.md:11`, `README.md:73`, `README.md:80`, `README.md:87`, `README.md:93`, `api/main.py:120`, `api/main.py:2695`.

Простое копирование конфигурации не означает ее загрузку процессом; автоматический loader не обнаружен в рассмотренном пути запуска. Инструкция поднимает embedding на порту 8005, тогда как default API ожидает 8006. Часть путей доступа и state остается привязана к серверной структуре, включая hardcoded state location, несмотря на возможность задавать каталог данных. Clean self-host smoke test отсутствует.

Утверждение README о stdio-only Codex устарело; SUBMISSION уже содержит корректный direct URL. Это P2 документации и воспроизводимости, не план deploy в этом аудите. Ни конфигурация доступа, ни ее содержимое в отчет не включены.

### F17. P2: as_of не фиксирует воспроизводимый snapshot

**Доказательства:** `api/main.py:3632`, `api/main.py:4575`, `api/main.py:4576`, `api/main.py:4578`, `api/main.py:1293`.

Corpus stats читаются из текущего состояния, затем `as_of` получает текущее время. Note обещает воспроизводимость по той же дате, но API не привязывает запрос к immutable index version. Часть источников ведет на mutable GitHub main; hash фрагмента подтверждает фрагмент, а не полную версию исходного документа и индекса.

**Статус:** code + наблюдаемый контракт. Provenance полезен, но не равен snapshot reproducibility. Нужны immutable source revision/content digest и индексная версия, либо более узкое обещание observational timestamp.

### F18. P2: backup verification может дать ложное ok

**Доказательства:** `ops/backup.py:62`, `ops/backup.py:73`, `ops/backup.py:74`, `ops/backup.py:80`, `ops/backup.py:118`, `ops/backup.py:126`.

Reverse lexicographic sort имен может выбрать `state-pre-oai` раньше свежих датированных файлов; это не хронологический выбор. Если обязательная таблица не читается, helper сохраняет count `None`, но возвращает success после общего integrity check. Следовательно, `ok` не гарантирует наличие обязательной схемы или проверку самой свежей копии.

**Статус:** code. Возраст реально наблюдавшихся backups приведен ниже отдельно; helper не доказывает их непригодность. Fresh restore, recovery coarse_base и RTO/RPO не проверены.

### F19. P2: устойчивость к формулировке ограничена семенами и ручными связями

**Доказательства:** `api/main.py:3798`, `api/main.py:4808`, `api/main.py:4811`, `api/main.py:5073`, `mcp/server.py:791`.

Графовый поиск идет по citation edges и может расширить recall за пределы прямого сходства текста. Но набор посещаемых соседей зависит от seeds из registry и текущих результатов поиска. Чтение близких claim nodes дает кандидатов, а ручные связи фиксируют решение читателя; ни то ни другое не доказывает тождество любых перефразировок и не делает итог полностью независимым от wording.

**Статус:** code и граница позиционирования. Корректное обещание: дополнительные каналы recall и память явно связанных claims. Некорректное обещание: полная инвариантность вердикта к формулировке. Нужны regression с различными seeds, unrelated near-neighbours и проверенными эквивалентными claims.

## Measured

### Инфраструктура: состояние в момент чтения

| Объект | Наблюдение | Граница вывода |
|---|---|---|
| Oracle Qdrant | main green, optimizer ok; `papers_fulltext` 10 736 253 points; `coarse_base` 180 113 points | Близкий срез, не транзакционно единый snapshot; points не равны уникальным papers |
| Oracle disk | 129 142 906 880 B свободно, около 120,27 GiB; 38% used | Отдельный storage host |
| Contabo disk | 42 030 235 648 B свободно, около 39,14 GiB; 90% used | staging/API/pipeline state/FTS; FAIL по порогу 40 GiB |
| Qdrant memory | 9,877-9,882 GiB / 10 GiB, около 98,8% | Почти исчерпан лимит; не доказательство причины latency |
| Cgroup memory | anon 9 534 992 384 B, около 8,88 GiB; file 988 495 872 B | Компоненты памяти, не дополнительная память сверх total |
| Cgroup events | `oom=0`, `oom_kill=0`, `max=201192` накоплено примерно за 3 дня; сегодня между чтениями без прироста | Не был зафиксирован OOM; max не является счетчиком OOM |
| Защита | `oom_score_adj=-900`, guard timer active | Смягчение риска, не гарантия восстановления |
| Сервисы и stages | Основные services active; 14 stages + reporter current | Snapshot доступности, не overnight SLO |

### Corpus и acquisition

Raw done: **180 114** в observed epoch `1790956488.130` и **180 116** в `1790957143.915`. Прирост +2 за 655,785 s, около 10,9 min. Queue `quality_checked=13`; `deferred=28 972`, `rejected=40 725`, `off_niche=1 039 692`, `graph_unresolved=21`.

Полка done растет медленно в этом интервале. Это нельзя экстраполировать в articles/day или превращать в измерение first completions. Journal first-completion отсутствует, `updated_at` не отделяет reprocessing; monitor честно возвращает unknown (`ops/monitor.py:356`, `ops/monitor.py:365`).

Raw layer membership при done 180 114: `llm-slm=148 548`, `ai-agents=18 628`, `web3=17 243`, `builder-tech=16 386`. Один paper может входить в несколько layers, суммировать эти значения как объем корпуса нельзя. MCP `count_papers` дал canonical web3 **16 395**. Разница с raw web3 объясняется twins и сама по себе не ошибка. Качество покрытия builder-tech отдельно не проверено.

| Acquisition label | Records при done 180 114 |
|---|---:|
| latex | 129 380 |
| acl-pdf | 31 115 |
| openalex-oa-pdf | 3 755 |
| spec-markdown | 1 297 |
| whitepaper-pdf | 29 |
| abstract fallback | 6 471 |
| ACL abstract | 1 395 |
| IACR abstract | 4 890 |
| OpenAlex abstract | 1 782 |

**Fulltext acquired: 165 576 / 180 114, 91,93%. Abstract-only: 14 538 / 180 114, 8,07%.** Это acquisition classification, не оценка perfect parsing. Label `latex` применяется также к GitHub docs; 129 380 не означает ровно столько LaTeX-документов. Parsing quality на всем корпусе неизвестно.

### Внешние ограничения ingestion

В tail объемом около 350 000 bytes повторяются refs 429 (316), S2/recheck 429 (300), quality 429 (4). OpenAlex был paused до UTC midnight; на момент среза оставалось около 7,82 h. Это число сообщений в tail, не rate и не фиксированное временное окно. `database is locked` в этом tail не обнаружен.

Вывод: внешние квоты реально ограничивают часть pipeline; их вклад в throughput не рассчитан. Не обходить bans/limits и не снижать quality вслепую. Нужны fair priorities, backoff и наблюдаемый прогресс по источникам.

### Проверки и тесты

| Короткая проверка | Результат | Что не доказывает |
|---|---|---|
| API | 507 tests: 506 pass, 1 POSIX skip; 9,275 s | Работа skipped POSIX path, production E2E и dirty CI |
| Eval | 40 pass | Corpus-wide precision/recall |
| Python MCP | 11 pass | Корректность всех live protocol errors |
| Mocked attestor | 47 pass; 2,95 s | Реальное chain settlement и mainnet security |
| Gateway | 15 pass | Live payment, compensation и idempotency |
| TypeScript | `tsc --noEmit` для attestor и gateway: оба pass | Поведение внешних сетей |

Итого **619 passed + 1 skipped = 620 total**, не 620 passed; TypeScript отдельно. Это локальные проверки текущего аудита, не CI dirty code и не production E2E.

Live проверены **11 read tool names, 16 calls**, плюс **3 x402 discovery/challenge requests**. Read names: `search_research_paper`, `get_research_bundle`, `find_papers`, `get_paper`, `read_paper_section`, `count_papers`, `similar_papers`, `get_code_or_math_spec`, `compare_methods`, `research_trends`, `validate_project`. `get_verdict_message` и `record_signed_verdict` в production не вызывались. Следовательно, нельзя писать «13/13 tools end-to-end».

### Latency: отдельные MCP-наблюдения

| Операция в ограниченной live выборке | Время, s |
|---|---:|
| Count | 4,114 |
| Similar | 4,346 |
| Math | 4,635 |
| Nonce search | 5,443 |
| Trends | 5,126 |
| Descriptive account search | 5,512 |
| MEV bundle | 6,488 |
| Scope check | 7,439 |
| EIP-7702 limitations | 8,625 |
| On-chain oracle find | 9,255 |
| Get paper | 9,893 |
| Agent find | 10,656 |
| Compare | 11,883 |
| Exact EIP-7702 find | 13,270 |
| Fast registry validate | 20,160 |
| Unknown paper error | Время не приведено; JSON error при `isError=false` |

Это малая convenience sample. Время включает transport/plugin/network и обработку сервиса. Это не p95, не load test и не причинный анализ Qdrant. Два API `/v1/search` samples дали p50 0,907 s и p95 2,009 s, но двух samples меньше monitor floor 20. Middleware измеряет только `/v1/search` (`api/main.py:134`, `api/main.py:139`), не find/validate/bundle; gate недостаточных samples находится в `ops/monitor.py:128`. Два API samples нельзя сопоставлять с полной пользовательской latency как единую метрику.

### Качество ответов: конкретные успехи и промахи

- Nonce search находит реальные Solana docs; verbal feedback для AI-agent приводит Reflexion в top.
- MEV source `2101.05511`: возвращены исходный алгоритм и 6 equations, 2 211 chars, без truncation. Это сильная демонстрация value proposition, но не гарантия extractor для всех документов.
- Запрос вне области о soft gripper отклонен; provenance, fulltext status, ограничения и бюджет ответа полезны и часто честны.
- Descriptive on-chain oracle find не вывел Chainlink в top 5. Descriptive account delegation дал OpenZeppelin reference, но EIP-7702 отсутствовал в top 5; exact ID дал EIP-7702 top 1.
- Эти два промаха и точное попадание по ID показывают зависимость от wording, но не позволяют объявить общий recall или precision числом.
- ZK trends, EIP-7702 limitations и GRPO registry report имеют конкретные дефекты, перечисленные в F04-F07. Они не обнуляют полезность остальных read-путей.

### Оплата, публичная поверхность и recovery

Публичная research page отвечала 200, root 401. Live paid discovery сообщил **только Solana devnet USDC**. Неоплаченный `get_evidence_bundle` вернул payment challenge с MCP `isError=true`, ценой **$0.01**, amount **10000**. Сегодня оплаченный settlement test не выполнялся. Base/Ethereum/Arbitrum являются testnet configuration, а не четырьмя доказанными live E2E сетями.

Daily backups state и judgments были возрастом около **14,6 h**. Fulltext Qdrant snapshot датирован **2026-09-27 06:03 UTC**, размер около **32,49 GB**, при weekly schedule. Recovery snapshot для coarse_base не проверен. Возраст копии и наличие расписания не дают измеренный restore RTO/RPO.

Ограниченный secret-pattern scan tracked text ничего не обнаружил. Это не history scan, не полный security audit и не гарантия отсутствия утечек.

### Поддерживаемость

Текущие файлы: `api/main.py` **5906 строк**, `pipeline/service.py` **3952 строки**, включая пустые. Это большие зоны смешанных контрактов, storage и control flow. В extractor много source-specific conventions, а systematic tests на их взаимодействие недостаточны. Зеленые тесты подтверждают проверенные сценарии, не полноту покрытия или отсутствие latent bugs. Размер файла сам по себе не дефект; сначала нужны regression contracts, затем обоснованное разделение ответственности.

## Unverified

1. Overnight SLO, uptime/availability на длительном интервале и устойчивость под реальной конкуренцией.
2. Репрезентативные end-to-end p95/p99 по каждому инструменту, load profile и причинная декомпозиция latency.
3. First completions, reprocessing и articles/day: нет необходимого журнала событий.
4. Полное соответствие deployed revision/dist/dependencies и локального dirty working tree; verified source hashes покрывают только шесть файлов.
5. Corpus-wide parsing integrity, доля поврежденных code blocks, canonical coverage, precision/recall, полнота HAL/index reconciliation и устойчивость вердикта к перефразировкам. Граф зависит от seeds, ручные links не доказывают универсальную эквивалентность.
6. Paid settlement, chain write E2E, retries/compensation/idempotency и mainnet readiness. Проверки с mock chain не заменяют эти доказательства.
7. Независимость reviewer models при публичных `models=[]`; согласие wallets не является научной независимостью.
8. Fresh restore state/judgments/fulltext/coarse_base, пригодность всех копий и измеренные RTO/RPO.
9. History security scan, полный security audit, adoption, retention, revenue, willingness to pay и unit economics.
10. Качество и полнота builder-tech: размер layer не заменяет отдельную проверку поиска, taxonomy и evidence.
11. License: полнота и корректность лицензий и прав использования документов, code fragments и источников по всему корпусу не проверены. Наличие provenance или license metadata не доказывает разрешение на любое повторное использование.

Unknown не означает fail. Однако paid/mainnet release не может опираться на unknown там, где нужны явные доказательства безопасности и recovery.

## Plan: порядок работ и критерии допуска

Ни одна задача ниже в этом аудите не выполнялась. Первые исправления детерминированные, не требуют дополнительного LLM или расходов на модель.

| Порядок | Работа | Проверяемый результат |
|---|---|---|
| 1 | Rollback, lifecycle SQLite connection, watchdog реального прогресса | Исключение после INSERT не удерживает writer lock; повторяющиеся ошибки не маскируются heartbeat; тесты recovery |
| 2 | Source integrity и taxonomy | Literal `%` сохранен в code environments; порядок Before/code/After сохранен; Security parent наследуется; адресная оценка поврежденных источников |
| 3 | Registry/report/coverage/trends contracts и MCP errors | Согласованные current/similar/pending поля; canonical twins; честный chunk truncation; ZK не превращается в false absence; errors имеют protocol flag |
| 4 | RAM и disk policy | Отдельный бюджет каждого хоста, запас для snapshot/ingestion, контролируемая retention; решение без blind raise лимита 10 GiB |
| 5 | Ingestion quotas и согласование индексов | Fair priorities при 429, source-owned HAL cursors и refresh; done/placeholder reconciliation; idempotent FTS; journal first-completion/reprocessing |
| 6 | Regression release, recovery и public docs | Короткие targeted regressions, корректный выбор backup и обязательная schema check; разрешенный fresh restore; затем чистый опубликованный commit и CI для него; truthful сайт/self-host smoke |
| 7 | Unpaid vs settled paid gate перед mainnet | Отдельно доказанные challenge, разрешенная оплата, settlement, chain write, failed settlement и retries; ясная billing policy и paid bundle value; отдельное решение о mainnet |

До paid mainnet нужно закрыть риски F01-F03, доказать платежный жизненный цикл F13 и recovery, устранить вводящие в заблуждение публичные контракты. Конкретные численные SLO должны быть согласованы и измерены, а не выдуманы по этому snapshot.

Для hackathon можно показывать read-only beta, реальный MEV/nonce/Reflexion value, честный pending registry и границы devnet. Нельзя заявлять 13/13 production E2E, независимое научное подтверждение, полную независимость вердикта от формулировки, четыре live paid networks или доказанный paid production.

**Финальный вывод:** инженерная основа и полезность реальны. Главный следующий шаг не увеличение обещаний или LLM spend, а восстановление точности контрактов, целостности источников и доказуемого recovery. Это аудит системы; подготовлены только два локальных файла отчета, без изменений кода, README, SUBMISSION, commit, push или deploy.
