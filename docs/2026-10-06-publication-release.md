# Publication release status: 2026-10-06

**Status: DEPLOYED · first-publication path only.** Rollout script сообщил `Rollout complete`; backup: `/opt/dtox-release-20261006/20261006T155411Z` (15:54 UTC). Это не доказательство того, что уже прошла реальная опубликованная job. Historical replacement не включён; legacy FTS migration остаётся BLOCKED.

## Проверки

| Область | Результат | Интерпретация |
|---|---|---|
| API unittest (свежий main) | 834 run; 1 skipped; OK = 833 PASS | Это unittest, не agent pytest run на 853. |
| Main pipeline unittest | 163 run; 5 skipped; OK = 158 PASS | Свежий main. |
| MCP / ops / x402 | 36 PASS / 169 PASS / 46 PASS offline | Непересекающиеся наборы; x402 не live payment/E2E. |
| Сумма main suites | **1242 PASS + 6 skipped = 1248 cases** | Не означает полноту production/Linux coverage. |
| Native Linux API | 54 tests; OK; 0.783 s | Native subset; не прибавлен к suite total. |
| Native Linux pipeline | 72 tests; OK; 27.826 s; real `fcntl` preimport | Native subset; не прибавлен к suite total. |
| Expanded `reads` live-before | 15.308 s; 9 read calls; все вызовы `tool ok`, error count 0; `partial=true` у `find_papers`, `get_code_or_math_spec`, `get_research_bundle` | Инструментальная доступность наблюдалась; полнота не принята. Partial guard оставляет профиль FAIL. |
| Expanded `reads` live-after | 8.887 s; 9 tools / 12 HTTP; error count 0; `partial=true` у spec и bundle | Completeness FAIL. Discovery IDs изменились на `2605.14306` / `2604.03173`; bundle сохранил те же ACL IDs, 907 chars. Compare вернул 0 tables — ожидаемое отсутствие, не transport error. |
| Expanded `semantic` live-before | 13.703 s; 6 queries, все вызовы `tool ok`; пересечение top-2 ID для трёх парафразов: 0 / 0 / 0 | Наблюдение retrieval-разброса, не semantic accuracy/recall и не quality PASS. Отрицательный probe оценивается только по scope. |
| Expanded `semantic` live-after | 6 queries; тот же набор IDs; новых изменений не наблюдалось | Никакого semantic accuracy PASS: сравнение наблюдательное, gold labels нет. |

Повтор reads-after с UTF-8 клиентом: 7.622 s, 9 tools / 12 HTTP, errors 0; spec/bundle partial, completeness FAIL. Реальная секция ровно 1000 символов, как в provenance. Прежнее превышение вызвал Latin-1 в тестовом SSE клиенте, не сервер. Общий декодер исправлен. Spec returned/available считают элементы, chars считает символы; единицы в отчёте исправлены.

Post-deploy MDX: acquisition_type=spec-markdown, extraction_type=markdown_source, exact_latex=false; stored_acquisition_type=latex сохранён. Hash и URL не изменились. Общее правило метаданных, не исключение по ID и не улучшение ranking.

Validate после выкладки: DAG 13.294 s, повтор исходного DA-sampling 14.660 s, оба успешны. Причина первого сбоя до выкладки не установлена. Corpus: 182100 документов, web3 17370; слои пересекаются. get_verdict_message успешен без chain write. Проверены 12 уникальных read/preparation tools; inventory 13. Production record_signed_verdict и реальные платежи не вызывались.

## Publication gate и инварианты

- Первая публикация допускается только для успешно chunked документа с `parse_status='ok'`, `completion_history.first_known=1` и `completed=0`. Отсутствующая/NULL history считается legacy и блокируется (`legacy_or_completed`). Уже опубликованный snapshot immutable.
- Snapshot проверяет cache hash и соответствие cache↔points: paper/chunk IDs, стабильный UUIDv5 point ID, непустой текст и конечные векторы согласованной размерности. Staged artifact ограничен publication cache и защищён hash/revision checks.
- Для видимости одной ревизии обязательны preflight, ACK Qdrant, ACK chunk FTS и completion. Durable ACK пишется в publication journal после каждого внешнего подтверждения; только затем одна SQLite transaction фиксирует paper completion и journal `published`.
- Это не распределённая транзакция: частичные внешние ACK могут требовать retry/repair, но пока gate не пройден, incomplete/pending/error ревизия не считается опубликованной. Metadata/coarse/paper FTS — вторичные проекции.
- Новые документы используют обычный pipeline. Исторические замены остаются только staged; применение **BLOCKED** до точного legacy FTS ID→UUID mapping и revision-aware reader switch. Старые опубликованные данные/индексы должны оставаться доступны.
- Развёрнутый scope ограничен first-publication path. Нет утверждения о завершённой/подтверждённой опубликованной job; для неё требуется отдельное доказательство.

## Открытые gates

- Подтвердить первую реальную published job. Все шесть runtime-хешей совпали, три службы active, NRestarts=0, журнал создан; publication/chunked samples пусты. Завершённый боевой цикл Qdrant + FTS пока не наблюдался.
- Диагностировать предыдущую live `validate_project` DA-sampling ошибку; новый DAG-запрос успешен, но не доказывает, что исходная ошибка устранена.
- Reads-after остаётся partial у spec/bundle. Сохранять partial guard; не объявлять полноту по `error_count=0`.
- Legacy FTS migration и глобальная уникальность legacy ID mapping — PENDING/BLOCKED; миграция не запускалась. Full scan/backfill не подразумевается.
- Semantic retrieval остаётся отдельной quality-задачей. Предстоящие изменения publication/source provenance не являются изменениями ranking и не закрывают качество retrieval.

**Итог:** код, runtime-хеши и журнал first-publication механизма подтверждены на production. Actual published-job proof pending; причина первого validate сбоя не установлена. В окончательных офлайн-наборах новых регрессий не обнаружено. Шесть поисковых ID sets сохранились, но это не гарантия всего корпуса и не semantic accuracy PASS. Ранжирование не менялось. Commit/push, массовая переиндексация, платежи и доступы не менялись.

## Deployment evidence

| Evidence | Status / value |
|---|---|
| Rollout output / backup | `Rollout complete`; `/opt/dtox-release-20261006/20261006T155411Z`; 15:54 UTC |
| Confirmation time | 15:54 UTC, 2026-10-06 |
| Deployed scope | First-publication path only; no historical replacement |
| Runtime | 3 services active; NRestarts=0; hashes match all 6 files |
| Journal initialization | Tables exist; `sample[]` and `chunked[]` are empty |
| Actual published-job proof | PENDING; no completed Qdrant + FTS job observed |
| Final release status | DEPLOYED, first-publication path only; historical replacement blocked |

## Update 2026-10-06: поздний proof и readiness, срез 18:12:43 UTC

Append-only обновление. Текст выше остаётся историческим снимком 15:54 UTC; его `PENDING actual published-job proof` не описывает позднее состояние. Финальные security deployment details от main ещё PENDING. Глобальный production GO: НЕТ.

- По свидетельству main естественная job `pmlr:v306/xian26a` подтверждена 17:49:55 UTC: 87 points, `state=done`, `parse_status=ok`, history `first_known=1`, `completed=1`. Preflight, FTS/Qdrant ACK, cache SHA и artifact revision verified; FTS exact, Qdrant payload semantic equality при missing/null normalization, все 87 проверены и API-visible. Actual-job gate закрыт для этой job, не для всего корпуса.
- На 17:39 UTC: 9 published документов, 182109 документов state; Qdrant Oracle green, 10912569 points. Points не papers, acquisition abstract/fulltext mixed. Historical replacement и legacy FTS migration по-прежнему не включены.
- API/MCP readiness: main guarded deployment 18:11 UTC, 4 файла; backup `/opt/dtox-release-20261006/20261006T1811-readiness-api`, SHA prefixes main `422c6986..`, evidence `94d148..`, bundle `d744b0..`, MCP `470436..`; health, route, cursor active. Canonical metadata bounded shortlist, opaque spec pagination/partial causes, malformed signed batch fail-closed.
- Frozen fixture raw SHA verified: `9b666344cd3e137e0fe532046b5a13e59c23c5e5c907628e7f1b1e597d0ab5ff`. Public before 24 attempts, 19 complete, 5 lexical partial, 10 hit@10. Public after 18:12:15 -> 18:12:43 UTC, 28.255 s: 24 complete, 0 partial/errors, 13 hit@10, 11 hit@3, 1/8 groups all-three FAIL. Все прежние 10 complete hits сохранены. Candidate internal hit@3=12, не публичные 11. Один новый Web3/smart-contract hit на прежде полном ответе; два новых раньше invalid, чистый semantic gain ими не доказан. Это не precision/recall или blind-human relevance proof.
- Свежие main suites: API 862 run / 1 skip; pipeline 182 run / 5 skip; MCP 62, ops 179, eval 40, attestor 55, gateway 59 PASS. Total 1433 PASS + 6 skip = 1439 raw executions, не новая unique coverage. Native Linux backend 107 PASS за 1.832 s отдельно; три ранние неудачные попытки setup/dependencies не скрыты. TS builds обоих PASS.
- SAS read-only compatibility 18:12:08 UTC PASS: actual publication authority `39ay..`, candidate credential/schema owner/authority/signers/layout/names/genesis verified. Без wallet reads/RPC writes/fresh payments. Attestor/gateway security changes пока OFFLINE ONLY; deployment pending. Main обнаружил старый gateway с unsigned auto-retries без module shared schema. Code-only deployment планируется без изменения flags/prices и без transactions; завершение не заявлено.
- Quality FAIL 1/8, pipeline OOM root не исправлен, restore UNKNOWN требует owner-approved isolated Oracle target (не Contabo), money/live E2E historical. OOM 17:21:49 UTC до main tests: `anon-rss=11411608 kB`, autorestart; поздние 4 native PDF conversions около 9 GB. Upstream O(N²), runtime 1.28.0 против repo pin 0.0.27, historic allocator unknown; Fermat proposal/local lifecycle patch не deployed. Layout/OCR не отключены, page cap и новая ingestion этим docs-pass не вводились.

Сводка: [readiness Markdown](2026-10-06-readiness-final.md), [локальный HTML](2026-10-06-readiness-final.html), [memory diagnosis](2026-10-06-pipeline-memory.md). Источники: приватные before/after/candidate JSON и сообщения main, без новых live-вызовов. Черновик готов к freeze, не финализирован до следующего deployment update main.

## Update 2026-10-06: scoped security deployment decision, confirmation PENDING

Позднее сообщение main уточняет и ограничивает прежний план; старые snapshots выше сохранены. Полный новый gateway candidate НЕ deploy: compatibility/payment-policy gate FAIL, HELD до explicit business approval и compatibility patch. Глобальный production GO остаётся НЕТ.

- Current runtime `tools.get_evidence_bundle` проксирует search, сохраняет `section_type`, `element_type`, `year_from`, `limit=1..15`, default 8. Новый local shared schema убирает filters и проксирует real bundle; advertised 15 / 30000 расходятся с API bundle max 5 / 20000. Это несовместимость и потеря возможностей.
- Local payments добавляет upfront `record_signed_verdict`, меняя текущую SDK default authorization. Main проверил local `@x402/svm 2.24.0` default authorization и отсутствие extra call в старом live wrapper. Полный rollout требует business approval; зелёные gateway checks (включая упомянутые 57 tests) не закрывают policy/compatibility FAIL. Прежние 59 PASS остаются offline snapshot, total не пересчитан.
- Main планирует только `upstream.ts/js` + `errors.ts/js`: generic decoder и no-retry writes. Старые tools/server/config, prices/env, filters и payment policy сохраняются. Новый shared schema, real bundle proxy и payments в этот scope не входят. Deployment PENDING до подтверждения main; transactions не заявлены.
- Attestor: план 3 TS + 3 JS security files после native Node20 checks, deployment PENDING. Свежий read-only SAS actual PASS сохраняется, но не является runtime deployment или свежим monetary E2E proof.

Это docs-only уточнение по сообщению main. Исходники, продукт и live services этим pass не затрагивались. [Readiness](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) синхронизированы; финализация ждёт scoped deployment confirmation.

## Update 2026-10-06: live pagination и неполный native attestor, rollout PENDING

Новое evidence main, точное время этого update не передано; прежние snapshots сохранены. Финализировать readiness до rollout result нельзя.

- Production public MCP pagination: source `2111.00396`, S4 equation, `maxchars=500`. Первая страница 3 chunks / 500 chars / `next_cursor`; вторая 500 chars, nonempty, тот же clipped chunk с одинаковым raw hash длиной 64 и продолженным `returned_offset`. `partial_reasons` ровно `character_budget`, `exact_latex=true`. Это доказательство continuation в одном live сценарии, не zero-partial claim или гарантия корпуса; не часть прежних 24 retrieval queries.
- Native minimal gateway decoder: Node20.20.1, 14 checks PASS, zero network. Это native check, НЕ deployment. Minimal rollout только decoder/no-retry scope по предыдущему Update остаётся PENDING.
- Staged native attestor: 55 tests PASS, но 1 test-module import FAIL, поскольку coordinator package пропустил `sas-readonly-probe.mjs`. Native green НЕ принят. Main предоставит fixture и повторит прогон; completion пока PENDING. Package omission не скрывается за PASS и не объявляется product regression без свидетельства.
- Full gateway candidate по-прежнему HELD, compatibility/payment-policy FAIL. Новые checks не закрывают policy gate, не подтверждают payment/live E2E и не пересчитывают прежние 1433 PASS + 6 skip raw executions.

Только docs по сообщению main, без source edits или live calls. [Readiness](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) обновлены условно; deployment и финализация PENDING до rollout result main.

## Update 2026-10-06: final native attestor, Fermat local candidate, guarded rollout STAGED

Позднее evidence main; прежние snapshots сохранены, включая прежний package/import FAIL. Rollout ещё НЕ запущен, финализация PENDING до результата main.

- Missing native helper исправлен. Final attestor native Node20.20.1: 58 PASS / 0 fail, 55 прежних + 3 read-only probe unit fixtures. Предыдущий `sas-readonly-probe.mjs` import FAIL теперь исторический; native tests PASS не равен deployment или денежному E2E.
- Fermat: финальный LOCAL CANDIDATE lifecycle wrapper в `pipeline/extractor.py`, `pipeline/service.py`, `test_pdf_conversion_lifecycle`. 108 raw PASS / 4 skip, в том числе 15 новых PASS / 1 новый skip. Не production deployment, native layout/OCR equivalence или OOM root fix; isolated exact-runtime QA needed. Прежнее описание «продолжает предложение» больше не текущий статус candidate, memory gate всё ещё FAIL.
- Minimal security rollout STAGED, guarded 10 files: 6 attestor + 4 gateway upstream/errors. План guard сравнивает before/after ВСЕ 9 paid schemas, prices/mode/networks/SAS identities и сохраняет runtime old server/tools/config hashes. Эти guard results ещё НЕ подтверждены, так как rollout не выполнен. Full gateway HELD: feature/filter и payment-policy incompatibility не закрыты.
- Новые native/local runs не прибавлены к прежнему snapshot 1433 PASS + 6 skip, не считаются новой unique coverage. Нет свежего monetary proof; price/mode/payment policy change не заявлен.

Обновлены только [readiness](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) плюс этот append-only Update. Источник: сообщение main, без новых product/live calls. NO GO сохранён, ждём rollout result.

## Update 2026-10-06: исправление runtime baseline, smoke ABORTED до изменений

Важная коррекция main. Прежние Update сохранены как snapshots, но их выводы о running gateway из source TS НЕВЕРНЫ и отозваны. Это относится к claims о legacy filters/search proxy runtime, отсутствии extra upfront call и новой policy change относительно live.

- Source TS old, но actual compiled `dist/tools.js` УЖЕ new real-bundle schema, без legacy filters, default 3. `dist/server.js` импортирует `payments.js`. Upfront может уже быть active; `dist/payments` main ещё проверяет. Runtime payment policy UNKNOWN до полной inspection. TS-derived diff не доказывает потерю runtime filters; preservation legacy filters больше не заявляется.
- Predeployment smoke нашёл mismatch и ABORTED ДО любого backup/install/restart. Никаких deployment изменений этой попыткой. Scoped deployment PENDING, full gateway HELD, compatibility guard FAIL. Native gateway 14 checks и attestor 58/0 остаются test evidence, не rollout proof.
- Revised scoped guard должен сравнить и сохранить ВСЕ actual 9 schemas и service info/prices before/after, actual runtime server/tools/config hashes; не выдуманный legacy TS contract. План 10 files не расширен. Сравнения ещё не выполнены; ждём dist inspection и rollout result main. Новых monetary claims нет.
- Пользователь разрешил local isolated native PDF QA. Отдельный GPT6.1 Sol agent владеет private `pdf-native-qa` и его отчётом, без prod code; этот docs-pass не дублирует работу. Fermat остаётся LOCAL CANDIDATE; native QA результата нет, OOM root не закрыт.

Только docs по сообщению main. [Readiness](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) исправлены; NO GO и финализация PENDING до rollout result сохранены.

## Update 2026-10-06: actual dist payments подтверждён, pre-existing build drift

Main завершил указанную dist проверку. Предыдущее `runtime policy UNKNOWN` теперь историческое: `dist/payments.js` УЖЕ имеет explicit upfront signed writes / authorization reads. `dist/tools.js` УЖЕ real-bundle query/layer schema, limit max 15 / default 3, maxchars max 30000, strict, без legacy filters; `dist/server.js` импортирует payments. Source TS old не отражал running compiled contract. SOURCE/JS BUILD DRIFT существовал до этой попытки.

Прежние claims о введении новой payment policy rollout и удалении существующих runtime filters НЕВЕРНЫ, отозваны. Full gateway HELD по другой сохраняющейся причине: pre-existing schema/API bundle limits mismatch 15/30000 против API max 5/20000, compatibility не подтверждена.

Preflight FAILED/ABORTED до любых writes, backup/install/restart: initial guard ошибочно считал TS running runtime. Никаких deployment изменений этой попыткой. Revised minimal upstream/errors + attestor scope должен сохранить ВСЕ actual 9 schemas И service info/prices И JS server/tools/config/payments before/after. Successful rollout proof пока PENDING. Native tests не deployment, dist inspection не monetary E2E. Все прежние snapshots сохранены с этой явной коррекцией; текущие [readiness](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) исправлены, NO GO не снят.

## Update 2026-10-06 18:39:27 UTC: scoped security DEPLOYED, PDF native QA PENDING

Main подтвердил corrected actual-JS-baseline rollout SUCCESS 18:39:27 UTC. Backup `/opt/dtox-release-20261006/20261006T1905-security-compatible`: `1905` только label, не фактическое deployment time. Старые PENDING snapshots выше исторические, включая первый TS-derived guard FAILED/ABORTED до любых writes/backup/install/restart.

- Deployed и exact new hashes verified: 6 attestor TS+JS (message/sas/server), 4 gateway upstream/errors TS+JS. Private `../.codex-artifacts/dtox-readiness-20261006/rollout-security.sh` содержит exact 10 new и 7 preserved SHA256; manifest прочитан docs-pass, script не выполнялся им. Runtime confirmation от main.
- Before/after read-only smoke PASS: exact ВСЕ 9 actual compiled paid-MCP input schemas + service info/prices + live/devnet networks + SAS authority/credential/schema identities. Existing server/tools/config И dist/payments JS SHA сохранены. Нет env/price/policy changes или money writes.
- Explicit compiled upfront signed writes / authorization reads действовали до turn и НЕ изменены. Full gateway HELD по pre-existing build drift и bundle limits 15/30000 против backend 5/20000, compatibility всё ещё не принята. Это не новый filter loss или payment-policy change. Legacy filters runtime не имел, сохранение legacy filters не утверждается.
- Main local PDF mock: 16 run, 15 PASS / 1 skip, не native equivalence. Native PDF QA отдельного user-authorized агента выполняется в private local venv с bounded synthetic fixtures; результата нет, без prod code. Fermat LOCAL CANDIDATE, native QA PENDING, OOM root не закрыт.
- Snapshot 1433 PASS + 6 skip raw executions не увеличен новыми native/mock runs. Fresh monetary E2E отсутствует. Глобальный NO GO сохраняется: quality 1/8 FAIL, OOM root не исправлен, restore UNKNOWN, money E2E historical, full gateway compatibility HELD.

Security statuses в [readiness](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) завершены; PDF часть PENDING до native результата main. Этот pass только docs, без product/live calls или source edits.

## Update 2026-10-06 18:41:04 UTC: services active, pipeline memory proof PENDING

По post-rollout проверке main все 5 служб API/MCP/attestor/gateway/pipeline active. Qdrant Oracle green, optimizer ok, 10912622 points (+53 к прежним 10912569). Это points, не papercount; прирост не приписывается deployment.

Host memory report: 24031 MiB, available 9887 MiB, swap 12226/12287 MiB. `systemctl MainPID=3679545` показывает RSS 1792 kB / 1 thread / elapsed около 3 min, но вероятно это shell wrapper, не actual Python. 1792 kB НЕ полный pipeline RSS, нельзя использовать его как memory healthy или OOM root fixed proof. Main проверяет actual cgroup Python и late OOM, результат ещё PENDING. Service active не закрывает memory gate. Native PDF QA отдельно PENDING; security deployment SUCCESS 18:39:27 UTC не отменяется, глобальный NO GO сохраняется.

Обновлены только [readiness](2026-10-06-readiness-final.md), [HTML](2026-10-06-readiness-final.html) и этот append-only Update, без новых live calls.

## Update 2026-10-06 18:43 UTC: actual Python memory и поздний exit 137

Main уточнил wrapper/actual process. `MainPID=3679545` это `flock`, RSS 1792 kB НЕ Python memory. Actual cgroup stage Python PID 3679546: RSS 2002504 kB, 49 threads, `VmSwap=0`. Cgroup `MemoryCurrent=3414425600 B` может включать cache; не смешивать с Python RSS и host swap/available. Прежние 24031 MiB host memory не означают 24 GB available или memory fixed.

Pipeline main process exited 18:37:33 UTC, status 137, reported memory peak **10.5G**, единица сохранена. Autorestart 18:38:03 UTC, counter 2; `NRestarts=2`. Это ДО security SUCCESS 18:39:27 UTC; main pipeline restart не вызывал. Kernel reason 137 проверяется, поздний OOM НЕ подтверждён без kernel output. Исторический OOM 17:21:49 подтверждён независимо. Active health не stability PASS, memory/failure gate FAIL и root не исправлен.

Scoped security deployment остаётся DEPLOYED/guard PASS; full gateway HELD, native PDF QA PENDING. [Readiness](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) уточнены только по main, без новых live calls. Глобальный NO GO сохранён.

## Update 2026-10-06: kernel подтвердил второй OOM 18:37:31 UTC

Поздний reason больше не PENDING. Kernel: `global_oom`, killed Python PID 3667572, task memcg `dtox-research`, `total-vm=17381620 kB`, `anon-rss=11466160 kB`, `file-rss=128 kB`. OOM invoker gunicorn другого cohosted проекта, `slopng` cpuset: это НЕ доказывает его единственной причиной или точный allocator root. Global cohost contention/native allocation risk остаётся активным.

Unit exit 137: 18:37:33 UTC; autoRestart: 18:38:03 UTC, `NRestarts=2`; reported peak **10.5G**, единица сохранена. Оба подтверждённых OOM 17:21:49 и 18:37:31 были ДО security rollout SUCCESS 18:39:27 UTC. Security scope pipeline не deploy, main pipeline restart не вызывал; этот OOM не новая security rollout regression. Read-only security guard/nonregression PASS ограничен своим scope, не pipeline stability.

Текущий actual Python 3679546 RSS 2002504 kB / 49 threads / VmSwap 0, cgroup MemoryCurrent 3414425600 B (возможно cache). MainPID flock 3679545 RSS 1792 kB не Python. Active health не stability PASS, memory/failure gate FAIL; native PDF QA всё ещё PENDING, NO GO сохранён. Текущие [readiness](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) исправлены, прежний reason-pending Update оставлен историческим. Только docs, без новых live calls.

## Update 2026-10-06: FINAL report, bounded native PDF smoke CLOSED / PASS

Private `../.codex-artifacts/dtox-readiness-20261006/pdf-native-qa/REPORT.md` прочитан полностью, дополнен final main facts. Старые PDF-PENDING snapshots выше исторические. Bounded smoke CLOSED / PASS, НЕ full native/memory production GO.

- Approved private Windows venv, exact PyMuPDF4LLM/PyMuPDF/Layout trio 1.28.0. Local Python 3.13.3 / NumPy 2.4.4 / ONNX 1.30.0 против prod Linux Python 3.12.3 / NumPy 2.5.1 / ONNX 1.28.0. Main read-only metadata без native init подтвердил ВСЕ 20 model/config path/size/SHA matches. Это не platform/transitive/ABI equivalence; full source comparison не подтверждён.
- Один 2-page synthetic text/formulas/code/table/limitations PDF: old direct 1.28.0 / candidate byte-EQUAL, 455 UTF-8 B, SHA256 `93e52dce83b1b24765f83ca647aa785e94071bb7221ed15ad89c51a71975ccf1`. 2 native page chunks concatenated text также equal, не pipeline semantic chunks/index/corpus proof.
- 3 последовательные conversions, total 8.629 s foreground. External Windows Job 120 s / 1536 MiB не triggered, timeout/memory-kill не dynamic-tested. Cleanup подтверждён после outer Job, не product Windows descendant guarantee или POSIX teardown proof.
- Peak job COMMIT old 586084352 B, candidate 607526912 B: candidate ВЫШЕ, не memory improvement; COMMIT не RSS. Old включает fixture/PNG generation. Single tiny case не доказывает throughput/capacity. Transient OS-temp output удалён, later confinement harness change не rerun-verified.
- OCR engines отсутствуют, OCR/image fixture НЕ проверен, не установлен/не заменён. Heavy bbox/dense-vs-blockwise root comparison не выполнялся. Full exact Linux/native/OCR/POSIX QA, root fix и capacity gate открыты; wrapper НЕ deployed. Global cohost contention/native allocations остаются риском.
- Оба kernel OOM 17:21:49 и 18:37:31 ДО security SUCCESS 18:39:27; security scope pipeline не deploy. Active не stability PASS. Scoped security DEPLOYED/guard PASS, actual 9 schemas/service info/prices/networks/SAS identities и server/tools/config/payments JS preserved; upfront policy уже существовала и не изменена. Full gateway HELD по pre-existing drift/limits 15/30000 против API 5/20000, не новой filter loss/policy change.

FINAL [readiness](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) завершены. NO GO: quality 1/8 FAIL, memory/full native gate, restore UNKNOWN, money/live E2E historical, full gateway compatibility HELD. Snapshot 1433 PASS + 6 skip raw executions не увеличен smoke conversions. Только docs, без новых tests/live calls, code/deploy/secret/env/commit/push; старые claims сохранены с датированными коррекциями.

## Update 2026-10-06: FINISHED bounded release audit, source/model identity closed

Final main correction: stored identity JSON подтверждает ВСЕ 4 layout source SHA + 20/20 model/config path/size/SHA matches. Прежнее «full source comparison не подтверждён» выше историческое. Windows/Linux, Python/NumPy/ONNX различаются: ABI/full native QA FAIL/UNKNOWN, это не exact execution equivalence.

Bounded smoke CLOSED / PASS; private [Native QA REPORT](C:/Users/User/Documents/work/Claude/.codex-artifacts/dtox-readiness-20261006/pdf-native-qa/REPORT.md). Wrapper НЕ deployed; root O(N²), global cohost contention и оба kernel OOM до security не закрыты. Scoped security DEPLOYED/guard PASS, full gateway HELD по pre-existing build drift/limits, actual payment policy unchanged.

FINISHED bounded release audit, текущие tests/jobs завершены; НЕ все goals achieved. Глобальный NO GO сохраняется по quality, memory/full-native, restore UNKNOWN, historical money E2E и gateway compatibility. Итоговые [Markdown](2026-10-06-readiness-final.md) и [HTML](2026-10-06-readiness-final.html) финализированы. Только три согласованных docs; никаких дополнительных evidence probes/live tests, code/env/secret/deploy/commit/push действий. Исторические snapshots сохранены с явными коррекциями.
