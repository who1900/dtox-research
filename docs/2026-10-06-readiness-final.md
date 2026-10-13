# Итоговая готовность DTOX: 2026-10-06

Статус: FINISHED bounded release audit. SECURITY DEPLOYED; bounded native PDF smoke CLOSED / PASS. Глобальный production GO: НЕТ, не все цели достигнуты.
Локальный приватный отчёт. Public MCP run: 18:12:43 UTC; security SUCCESS: 18:39:27 UTC; late pipeline snapshot: 18:43 UTC. Текущие проверки/jobs завершены, ожидания результата нет. Exact Linux/full native gate FAIL/UNKNOWN, memory/root gates FAIL; это остаточные риски, не выполняющиеся тесты.

## Решение

API/MCP, first-publication, scoped security rollout и один bounded native PDF smoke подтверждены. Это не готовность всего продукта: quality FAIL (1/8 групп), memory/full native gate NO GO, два kernel OOM до security, root не исправлен, restore UNKNOWN, money/live E2E исторический. Full gateway HELD: pre-existing build drift и limits 15/30000 против API 5/20000, compatibility не принята. Первый TS-derived guard aborted до writes, corrected actual JS rollout SUCCESS 18:39:27 UTC. Existing compiled payment policy НЕ изменена. Lifecycle wrapper LOCAL CANDIDATE, НЕ deployed.

## Поиск: замороженный fixture, не precision/recall

| Показатель | Public before | Public after | Candidate internal |
|---|---:|---:|---:|
| Запросы | 24 | 24 | 24 |
| Полные / operational valid ответы | 19 | 24 | Не public proof |
| Partial / errors | 5 lexical / 0 | 0 / 0 | 0 errors |
| Known-source hit@10 | 10 | 13 | 13 |
| Known-source hit@3 | 8 | 11 | 12 |
| Группы со всеми 3 hit@10 | 1/8 | 1/8, FAIL | Не заменяет public gate |

Public after: 18:12:15 -> 18:12:43 UTC, 28.255 s, 24 реальных MCP-запроса. Все 10 прежних полных hit@10 сохранены, потеряно 0. Candidate internal не равен публичному прогону.

Три новых hit@10: `structured-dynamics`, `length-extrapolation-question`, `cross-chain-fuzzer`. Первые два прежде operational invalid из-за lexical partial, поэтому их нельзя считать чистым semantic gain. Единственное новое попадание на ранее полном ответе: Web3/smart-contract fuzzing, `cross-chain-fuzzer`, слой `builder-tech`. Причинный чистый semantic gain этим также не доказан. Единственная группа с 3/3: data-availability sampling.

Fixture authored агентом, known-source anchors, без слепых человеческих labels. Это не precision, exhaustive recall, независимая relevance-оценка или гарантия index coverage. Нулевые hard-negative flags не закрывают quality gate. Полный ответ API не означает fulltext каждого источника.

### Production public MCP pagination proof

По новому свидетельству main: source `2111.00396`, S4, equation, `maxchars=500`. Первая страница: 3 chunks, 500 chars и `next_cursor`. Вторая: 500 chars, nonempty; тот же clipped chunk, одинаковый 64-символьный raw hash, продолженный `returned_offset`. `partial_reasons` ровно `character_budget`, `exact_latex=true`. Подтверждено continuation внутри clipped chunk в одном реальном pagination сценарии, не отсутствие partial и не полнота всего корпуса. Эти вызовы не входят в прежние 24 retrieval queries; docs-pass их не повторял.

## Что изменено и что действительно deployed

| Область | Статус и границы |
|---|---|
| API/MCP readiness | DEPLOYED main, guarded rollout 18:11 UTC: 4 файла; health, route, cursor active. Canonical metadata для bounded shortlist; opaque spec pagination и явные partial causes; malformed signed batch fail-closed. |
| Backup | `/opt/dtox-release-20261006/20261006T1811-readiness-api` |
| SHA256 deployment | `main: 422c6986..`, `evidence: 94d148..`, `bundle: d744b0..`, `mcp: 470436..`; три последних только префиксы из сообщения main, не полные хеши. |
| First-publication | DEPLOYED; одна естественная опубликованная job подтверждена, подробности ниже. Historical replacement / legacy FTS migration не включены. |
| Gateway full candidate | HELD, НЕ deploy. Pre-existing schema/API bundle limits mismatch 15/30000 против 5/20000, compatibility не принята. Source/JS build drift реален; runtime filters уже отсутствовали, upfront payments уже active. Rollout не объявляется источником этих изменений. |
| Gateway scoped deployment | DEPLOYED main 18:39:27 UTC: 4 файла `upstream.ts/js` + `errors.ts/js`, generic decoder/no-retry writes, exact new hashes verified. Первый failed preflight исторический, до writes/backup/install/restart. Legacy filters не заявляются: actual schema уже без них. |
| Attestor deployment | DEPLOYED main 18:39:27 UTC: 3 TS + 3 JS security files, exact new hashes verified. Missing helper исправлен; final native Node20.20.1: 58 PASS / 0 fail (55 прежних + 3 read-only probe unit fixtures). Не monetary E2E. |
| Security guard | PASS: before/after read-only smoke, exact ВСЕ 9 actual compiled paid-MCP input schemas + service info/prices + live/devnet networks + SAS authority/credential/schema identities. Existing server/tools/config И `dist/payments.js` SHA сохранены. Нет env/price/policy changes или money writes. |
| Security backup | `/opt/dtox-release-20261006/20261006T1905-security-compatible`; `1905` только label, НЕ время rollout. Фактическое время SUCCESS: 18:39:27 UTC. |
| Security hash manifest | Exact 10 new SHA256 и 7 preserved SHA256: private `../.codex-artifacts/dtox-readiness-20261006/rollout-security.sh` относительно repo. Manifest локально прочитан, script не выполнялся; deployment hash verification сообщил main. |
| Post-rollout 18:41:04 UTC | Все 5 служб API/MCP/attestor/gateway/pipeline active. Это service status, не доказательство memory health или устранения OOM. |
| SAS compatibility | Read-only PASS 18:12:08 UTC: actual publication authority `39ay..`, candidate credential/schema owner, authority, signers, layout, names, genesis verified. Без чтения wallet/секретов и RPC writes; не fresh payment/E2E. |

Deployment/SAS/publication/test facts выше переданы main. Этот docs-pass повторно не обращался к продукту или live-сервисам.

### Gateway runtime correction: pre-existing drift, full candidate HELD

Предыдущий вывод о running gateway по source TS был НЕВЕРЕН и отозван. Source TS old: search proxy, legacy filters, default 8. Actual running compiled `dist/tools.js` УЖЕ real-bundle schema: query/layer, limit max 15 / default 3, maxchars max 30000, strict, без legacy filters. `dist/server.js` импортирует `payments.js`. Source/JS BUILD DRIFT существовал до этой попытки. Rollout не вводит новый proxy и не удаляет существующие runtime filters; сохранение legacy TS filters утверждать нельзя.

Main подтвердил: `dist/payments.js` УЖЕ имел explicit upfront signed writes и authorization reads. Claims об отсутствии extra call в live wrapper и о новой payment-policy change этим rollout отозваны. Local SDK `@x402/svm 2.24.0` default authorization не описывает actual wrapper. Schema limits 15/30000 против API bundle max 5/20000: pre-existing mismatch, compatibility не принята. Денежного E2E нет; dist inspection не равна fresh monetary proof.

Первый preflight FAILED/ABORTED ДО writes, backup/install/restart: initial guard ошибочно считал TS actual runtime. После исправления actual JS baseline scoped rollout SUCCESS 18:39:27 UTC: 10 файлов, exact hashes и read-only guard PASS. ВСЕ 9 actual schemas, service info/prices/networks/SAS identities и existing server/tools/config/payments JS сохранены. Existing explicit upfront signed writes/authorization reads действовали до turn и остались неизменными. Full gateway HELD по pre-existing drift/limits и непроверенной compatibility, не из-за новой policy/filter потери. Новых money writes не было.

## Проверки: исполнения, не новая уникальная coverage

| Набор main | Итог |
|---|---|
| API | 862 run, 1 skip, 861 PASS |
| Pipeline | 182 run, 5 skip, 177 PASS |
| MCP / ops / eval | 62 / 179 / 40 PASS |
| Attestor / gateway | 55 / 59 PASS offline |
| Всего | 1433 PASS + 6 skip = 1439 raw executions |
| Native Linux backend | 107 PASS, 1.832 s; отдельная проверка, не прибавлена к total |
| Native minimal gateway | Node20.20.1: 14 decoder checks PASS, zero network; отдельно от прежнего total |
| Final native attestor | Node20.20.1: 58 PASS / 0 fail, включая 3 read-only probe unit fixtures; missing helper исправлен, отдельно от total |
| Fermat local lifecycle | 108 raw PASS / 4 skip; 15 новых PASS + 1 новый skip; LOCAL CANDIDATE, не native PDF equivalence |
| Main local PDF mock | 16 run, 15 PASS / 1 skip; mocked lifecycle, не native conversion, отдельно от snapshot |
| Bounded native PDF smoke | CLOSED / PASS: 3 последовательные conversions, 8.629 s, один 2-page fixture; не полный native gate, не прибавлено к snapshot |
| TypeScript build | Attestor и gateway PASS |

Повторяющиеся fixtures не новая unique coverage. Три ранние неуспешные попытки native Linux были setup/dependency failures; после подготовки финальный прогон полный PASS. Это не три product regressions, но попытки не скрыты. Skips сохранены; PASS не доказывает native PDF/OCR equivalence, restore или live payment.
Счётчики выше остаются предыдущим offline snapshot, не scoped-deployment acceptance. Отдельные зелёные gateway checks, включая упомянутые main 57 tests, не закрывают pre-existing limits/compatibility gate и не заменяют ранее сообщённые 59 raw executions; policy-change claim отозван после dist inspection.
Новые decoder 14, attestor 58/0, Fermat 108/4, PDF mock 16 run (15/1) и native smoke 3 conversions не прибавлены к snapshot 1433 PASS + 6 skip и не объявлены новой unique coverage. Прежний attestor import FAIL из-за missing helper не скрыт; corrected final native rerun 58/0. Conversions не test-case count.

### Fermat: LOCAL CANDIDATE, bounded smoke CLOSED, full native gate NO GO

Финальный lifecycle wrapper candidate: `pipeline/extractor.py`, `pipeline/service.py`, `test_pdf_conversion_lifecycle`. Local 108 raw PASS / 4 skip, 15 новых PASS / 1 новый skip; main PDF mock 16 run, 15 PASS / 1 skip отдельно. Wrapper НЕ deployed; upstream O(N²) не изменён. User-authorized отдельный агент завершил private `pdf-native-qa/REPORT.md`; этот docs-pass прочитал отчёт полностью и QA не повторял.

| Native smoke evidence | Результат и границы |
|---|---|
| Runtime | Exact trio PyMuPDF4LLM / PyMuPDF / Layout 1.28.0 в private venv. Local Windows Python 3.13.3, NumPy 2.4.4, ONNX 1.30.0; production Linux Python 3.12.3, NumPy 2.5.1, ONNX 1.28.0. Platform/transitive runtime НЕ exact. |
| Source/model identity | Main identity JSON: ВСЕ 4 layout source SHA и 20/20 model/config path/size/SHA match, read-only без native init. Identity файлов не ABI/native execution equivalence: Windows/Linux, Python/NumPy/ONNX различаются. |
| Output | 2-page synthetic text/formulas/code/table/limitations: old direct 1.28.0 и candidate byte-EQUAL, 455 UTF-8 B, SHA256 `93e52dce83b1b24765f83ca647aa785e94071bb7221ed15ad89c51a71975ccf1`. Не исторический old 0.0.27. |
| Page chunks | 2 native page chunks; concatenated text тот же 455 B / SHA. Это не pipeline semantic chunking, embedding/index или corpus QA. |
| Foreground budget | 3 sequential conversions, total 8.629 s; outer Windows Job 120 s / 1536 MiB не triggered. Timeout/memory-exhaustion kill этим run динамически НЕ проверен. |
| Cleanup | После outer Job cleanup нет живых recorded PIDs. Это НЕ самостоятельная Windows descendant guarantee wrapper и не POSIX teardown proof. |
| Peak job COMMIT | Old 586084352 B, candidate 607526912 B, candidate ВЫШЕ. COMMIT не RSS. Old включает fixture/PNG generation; single tiny case не даёт memory-improvement или throughput claim. |
| Не проверено | OCR engines отсутствуют, OCR/image fixture НЕ проверен, не установлен/не заменён. Нет heavy bbox stress или dense-vs-blockwise root comparison. Full Linux equivalence и OOM root fix НЕ приняты. |

Transient candidate output использовал OS-temp и был удалён; confinement harness исправлен после run, без повторного benchmark, этот confinement change не verified. Bounded smoke закрыт, НЕ full feature preservation. Полный native/ABI gate FAIL/UNKNOWN. Будущая отдельная работа: exact Linux/native/OCR/POSIX QA и bbox edges/ties/masks equivalence, не запущена этим audit; no production heavy PDF. Root/global cohost risk и production memory NO GO сохраняются.

## Публикация и корпус

Естественный publication proof: 17:49:55 UTC, `pmlr:v306/xian26a`, 87 points. `state=done`, `parse_status=ok`, `completion_history.first_known=1`, `completed=1`; preflight, FTS и Qdrant ACK; cache SHA / artifact revision проверены. FTS exact, Qdrant payload semantic equality с нормализацией missing/null; все 87 проверены и API-visible. Это закрывает отсутствие actual published-job proof в старом снимке, но только для этой job.

На 17:39 UTC наблюдались 9 published документов и 182109 документов state. Qdrant Oracle: green, 10912569 points. Points не равны papers; acquisition смешивает abstracts и fulltext. Ни 9 publications, ни один проверенный job не гарантируют весь корпус, все полные статьи или legacy replacement.

Поздний post-rollout 18:41:04 UTC: Qdrant Oracle green, optimizer ok, 10912622 points (+53 к прежнему snapshot). Это прирост points, не papers, не доказательство полного acquisition или эффект deployment.

## Gates и остаточные риски

| Gate | Статус | Безопасное следующее действие |
|---|---|---|
| Retrieval quality | FAIL: 1/8 групп | Отдельная quality-задача и независимые labels; не менять frozen fixture для улучшения результата. |
| Pipeline memory / stability | FAIL: второй kernel OOM 18:37:31, peak 10.5G, NRestarts=2 | 18:37:33 exit 137, 18:38:03 autorestart до security. Active не stability PASS. Fermat LOCAL CANDIDATE, bounded smoke PASS не root fix; global cohost contention/native allocation risk активен. |
| Full-app restore | UNKNOWN | Нужны разрешение владельца и изолированный Oracle target, не Contabo. |
| Money/live E2E | Не обновлён | Только историческое свидетельство; read-only SAS не заменяет transactions/payments. |
| Gateway compatibility | Full candidate HELD, pre-existing limits mismatch | 15/30000 schema против API max 5/20000; compatibility validation нужна. Actual compiled filters/payments уже известны, новая потеря filters или policy change не заявляется. |
| Scoped security deployment | CLOSED: DEPLOYED, guard PASS 18:39:27 UTC | 10 файлов, exact hashes verified; ALL actual 9 schemas + service info/prices/networks/SAS identities unchanged, server/tools/config/payments JS SHA preserved. Full gateway не входит. |
| Bounded native PDF smoke | CLOSED / PASS | Один 2-page fixture byte-EQUAL, exact trio, 4 source SHA и 20/20 model/config SHA match, 3 conversions. Без prod code, без repeat tests этим docs-pass. |
| Full native / memory gate | NO GO; full native/ABI FAIL/UNKNOWN | Windows/Linux и Python/NumPy/ONNX различаются; OCR, POSIX descendants, root bbox equivalence/capacity не проверены. Wrapper НЕ deployed; candidate commit выше. Нет незавершённых QA jobs. |

OOM 17:21:49 UTC предшествовал main tests: kernel убил pipeline Python, `anon-rss=11411608 kB`, затем автоматический restart. Поздний snapshot: четыре параллельные native PDF/layout conversions и около 9 GB RSS. Upstream O(N²) scratch подтверждён в текущей layout implementation; точный allocator исторически погибшего PID неизвестен. Runtime 1.28.0 отличается от repo pin 0.0.27. Local lifecycle patch не deployed, OOM root не закрыт. Layout/OCR не отключены, page cap не введён, новая ingestion этим docs-pass не запускалась. Подробности: [pipeline memory](2026-10-06-pipeline-memory.md).

18:41:04 UTC host memory report: 24031 MiB, available 9887 MiB, swap 12226/12287 MiB. Это host metrics, не proof что pipeline исправлен; 24031 MiB не available. `MainPID=3679545`, RSS 1792 kB / 1 thread, был wrapper metric, не Python memory.

Late snapshot main 18:43 UTC: MainPID 3679545 подтверждён как `flock`, НЕ Python. Actual cgroup stage Python PID 3679546: RSS 2002504 kB, 49 threads, `VmSwap=0`. Cgroup `MemoryCurrent=3414425600 B`, может включать cache; не то же самое, что Python RSS или host swap.

Второй kernel OOM подтверждён 18:37:31 UTC: убит Python PID 3667572, `total-vm=17381620 kB`, `anon-rss=11466160 kB`, `file-rss=128 kB`, `global_oom`, task memcg `dtox-research`. OOM invoker: gunicorn другого cohosted проекта, `slopng` cpuset; это НЕ доказывает его единственной причиной или точный allocator root. Global cohost contention и native allocations остаются активными рисками.

Unit exit 137: 18:37:33 UTC, reported memory peak **10.5G** (единица лога сохранена). Autorestart 18:38:03 UTC, counter 2; `NRestarts=2`. Оба подтверждённых OOM (17:21:49 и 18:37:31) ДО security SUCCESS 18:39:27 UTC. Security scope pipeline не deploy, main pipeline restart не вызывал. Поздний OOM нельзя приписывать security rollout как новой регрессии; limited read-only security nonregression не означает pipeline stability PASS. Active health не stability PASS, memory/failure gate FAIL.

## Источники и финализация

Приватные источники: `../.codex-artifacts/dtox-readiness-20261006/{release-before.json,release-after.json,candidate-results.json,rollout-security.sh}` относительно repo; [Private native QA REPORT](C:/Users/User/Documents/work/Claude/.codex-artifacts/dtox-readiness-20261006/pdf-native-qa/REPORT.md). Не публиковать raw payloads. Дополнительно: main messages и stored identity JSON (4 layout source + 20 model/config SHA matches), [publication snapshots + Updates](2026-10-06-publication-release.md), memory diagnosis. REPORT прочитан полностью; script/QA не запускались docs-pass. Identity/rollout success переданы main, без live перепроверки.

Raw SHA256 `eval/paraphrase_release_20261004.json` проверен: `9b666344cd3e137e0fe532046b5a13e59c23c5e5c907628e7f1b1e597d0ab5ff`. Встроенный fixture во всех трёх JSON структурно совпадает. Поле `fixture_sha256` public before/after: `c5d1703f9fa52573156ab38f553bf1d10cefdc3076883684d4f476992595be9e`, это отдельный digest, не raw file SHA. `candidate-results.fixture_raw_sha256` совпадает с raw SHA; candidate main SHA: `422c6986a5a383f0e4852a0d7f2a5e3ba1605499fb93002ca8a0e0a66f3e889c`.

FINISHED bounded release audit, Markdown/HTML синхронны, текущие tests/jobs завершены. Это НЕ все goals achieved: глобальный NO GO, quality/memory/full-native/restore/money/gateway ограничения сохранены. Только два readiness docs и append-only Update publication-release.md; без product/code/live calls, новых tests, deploy, secret/env reads, ingestion, commit/push. Старые snapshots сохранены с датированными коррекциями.
