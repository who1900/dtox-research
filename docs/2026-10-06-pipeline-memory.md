# Pipeline memory diagnosis — 2026-10-06

PRIVATE / LOCAL QA. Не публиковать и не включать в submission/deploy автоматически.

## Результат и границы уверенности

Подтверждена текущая деградация pipeline в shared-process PDF layout extraction.
Подтверждены небезопасная native concurrency и квадратичные memory allocations
в установленной upstream layout implementation. Это actionable defect evidence,
но не allocation profile погибшего PID: нельзя объявлять единственную причину
OOM в 17:21:49 UTC доказанной задним числом.

В разрешённых `pipeline/service.py` / `pipeline/extractor.py` внесён узкий
generic lifecycle fix, описанный ниже; native allocation root НЕ исправлен.
Заменять upstream layout algorithm monkey-patch'ем, отключать layout/OCR,
ограничивать страницы/элементы, понижать runtime или менять models без QA нельзя.
`SIGALRM` defect подтверждён отдельно; текущий stack не находится в LaTeX parser.

## Проверенные production facts

Все времена в этой таблице — UTC. Host log печатает UTC+02:00.

| Время | Наблюдение |
| --- | --- |
| 17:21:49 | Kernel global OOM убил `python`, PID 3656064, task cgroup `dtox-research.service`; `anon-rss:11411608kB`, `file-rss:128kB`, `total-vm:17127508kB`. |
| 17:21:51 | Service journal: результат `oom-kill`. |
| 17:22:21 | Journal: автоматический restart, counter 1. Диагностический агент restart не вызывал. |
| 17:49–17:50 | Новый PID 3667572: RSS 5499824 kB, 51 threads; host swap 12276/12287 MiB. |
| 17:51:03 | `/proc/3667572/status`: RSS 5782704 kB, RssAnon 5725360 kB; процессный VmSwap 0, threads 51. Smaps: ~5.73 million kB private dirty anonymous. |
| 17:53:16 | RSS 9088560 kB, RssAnon 9031216 kB, VmSwap 0, threads 51. |
| 17:54:09 | Один non-blocking py-spy snapshot: все четыре fulltext workers в PyMuPDF layout; RSS ещё 9088560 kB. |

Полный host swap не означает, что именно этот новый PID уже находится в swap:
его собственные `/proc` counters показывали 0. Не смешивать host/process metrics.

Live source совпал с локальным dirty source по SHA-256:

- `service.py`: `f8433ee87ff290a3eb6c09a96ec904958555619001b2b7d4362990b7057aa025`.
- `extractor.py`: `f26e3d5e43fbfe4d6aaa1d5f2cb43765bfcb96bdd7a6decec39e7f0dc41f274e`.

Это фиксирует проверенный snapshot, а не запрещает чужие последующие правки.

Installed metadata: `pymupdf4llm`, `pymupdf`, `pymupdf_layout` — все **1.28.0**.
Repo `requirements.txt` pin: `pymupdf4llm==0.0.27`. Версия installed source прочитана
на диске, без импорта/инициализации модели диагностическим агентом.
В `pymupdf4llm/__init__.py` 1.28.0 import автоматически активирует layout при его
доступности. В maps работающего PID есть MuPDF, ONNX Runtime и OpenBLAS.
Ни dependencies, ни models не менялись.

Четыре уже открытых временных PDF: 1277637, 1772581, 1677612, 10197392 bytes.
Они не конвертировались повторно, не читались целиком и не копировались.
Размер сжатого PDF не является оценкой размера layout graph/native allocations.

## Stack evidence

Использован уже установленный `py-spy dump --pid 3667572 --nonblocking`, с внешним
`timeout 8s`, без `--locals`, native unwind, остановки или записи профиля.
Non-blocking snapshot может быть неточным; не приписывать ему allocation accounting.

- `stage-fulltext` ждёт `as_completed` в `fulltext_step`.
- `ThreadPoolExecutor-0_0`, active+GIL: `page_get_textpage` → layout base-elements
  → `BoxRFDGNN.predict` → `parse_document` → `to_markdown`
  → `_fetch_openalex_fulltext` → `_fetch_one_fulltext`.
- Worker `_0_1`: `fz_features_for_region` → layout feature extraction.
- Worker `_0_2`: `get_filtered_distance_matrix` → directional NN graph
  → `get_nn_input_from_datadict` → layout prediction.
- Worker `_0_3`: NumPy `_mean` → ROI pooling → layout prediction.
- `stage-chunk` idle. SIGALRM/LaTeX в этом снимке не источник hot CPU stack.

Четыре simultaneous calls `pymupdf4llm.to_markdown` действительно выполняются
в одном pipeline PID, а не в отдельных conversion processes. Название installed
`MultiProcessWrapper` этого не исправляет: прочитанный `predict()` делегирует
`self._model.predict(page, ...)` непосредственно.

[Официальный PyMuPDF concurrency contract](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html)
не поддерживает multiple-thread use; рекомендует отдельные процессы и независимые
document objects. Нельзя считать thread pool безопасным native isolation.

## Подтверждённый memory mechanism

В installed `pymupdf/layout/common_util.py` directional-NN implementation строит
множество `(bbox_num, bbox_num)` matrices через `repeat(...).reshape(...)`:
четыре squared-distance matrices, direction/filter masks, filtered matrix copies
и дополнительные distance copies. `get_filtered_distance_matrix` делает
`np.copy(base_distance_matrix)` и `np.logical_or.reduce(...)`.

Таким образом scratch memory растёт O(N²) от числа page bounding boxes, а не
от количества output characters. Четыре overlapping native/layout conversions
делят parent address space и увеличивают общую memory exposure. Это согласуется
с stack и anonymous RSS growth; число N и allocation bytes отдельных workers
не измерялись. Исторический victim stack отсутствует.

## Проверка альтернатив: SQLite и lifetime

- Stage runners используют отдельные connections, не один общий Python connection.
- `upsert_discovered`: `SELECT * FROM papers WHERE arxiv_id=?` / `fetchone`, а не
  материализация всех papers. Именно этот frame был у stage-harvest в snapshot.
- Quality/recheck batches ограничены `S2_BATCH_SIZE=500`; fulltext selection —
  `FULLTEXT_CYCLE_LIMIT=150`; chunk/embed selections также имеют `LIMIT`.
- `graph_harvest_step`: database scan/grouping по citations может быть дорогим,
  но Python `_graph_queue` материализует максимум 5000 IDs.
- `status_counts`: GROUP BY возвращает status aggregates, не все records.
  В snapshot reporter ждёт здесь; это не доказательство DB memory leak.
- Startup `seed_latex_cache_from_fulltext().fetchall()` не имеет LIMIT: реальный
  отдельный потенциальный transient risk. Но он выполняется до stage startup;
  наблюдаемый PID уже прошёл его. Считать его источником текущего роста нельзя.
- `fulltext_step` сохраняет futures/results всего batch до конца: дополнительный
  retention risk. Однако четыре текущих workers ещё внутри conversion; rolling
  future window не исправит наблюдаемый native allocation stall.
- Download `resp.content` и Markdown output не имеют size bound. Это exposure,
  а не установленная причина многогигабайтного роста на четырёх показанных PDF.

Production database не открывалась диагностикой. Никаких больших corpus queries
или полного cache-directory inventory для этих выводов не требовалось.

## Latex deadline: отдельный подтверждённый defect

`extractor._parse_deadline()` возвращает no-op context вне main thread.
`run_stages_parallel()` вызывает `chunk_step()` из `stage-chunk`, поэтому заявленный
90-second SIGALRM budget там не обеспечивает deadline. `_parse_deadline` также
не является PDF/native memory guard и не охватывает `to_markdown`.

Короткое локальное воспроизведение с mocked signal handlers:
main-thread alarm calls `[90, 0]`; worker-thread alarm calls `[]`.
Настоящие сигналы, sleeps и pathological inputs не использовались.
Включать SIGALRM из worker или менять process-wide handler — небезопасный fix.

## Generic solution proposal без удаления функций

1. В изолированной QA-копии exact installed layout implementation заменить
   directional-NN dense scratch на row-block reducer: `(B,N)` вместо `(N,N)`.
   Обработать **все** bounding boxes и направления; сохранить те же masks,
   infinities, `argmin` tie-breaking, edge ordering и model inputs. Это уменьшает
   scratch complexity до O(BN), не режет страницы, формулы, таблицы или OCR.
   Нужен review всей функции и equivalence tests; это предложение, не готовый patch.
2. Native PDF conversion вынести в fresh single-job subprocess с независимым
   document/model lifecycle, без parent import PyMuPDF и без `fork()` уже
   multithreaded pipeline. Ограничить число одновременно живых conversion jobs,
   **не** количество обрабатываемых документов. Все jobs остаются в очереди.
3. Parent хранит bounded pending/result window. Output передаётся через временный
   UTF-8 файл, без duplicate large IPC strings; publish только после success.
4. Time/resource exhaustion — диагностируемый retryable failure, не successful
   abstract fallback и не fulltext proof. Последний durable status/cache сохраняется.
   Resource envelope подбирать на isolated QA, не выдумывать production defaults.
5. Отдельно изолировать LaTeX parser с реальным deadline, сохранив existing
   formulas/algorithms/code/tables/limitations, parser modes и canonical section sink.

Serialization lock сам по себе не ограничит O(N²) peak одного PDF. Глобальный
MemoryMax/RSS kill, выключение layout/OCR, page caps, downgrade или special IDs
не являются preserving root fix и не внедряются.

## Blocker и точное безопасное следующее действие

Root allocation находится в installed third-party layout code, вне write scope;
QA workstation не имеет доказанно идентичного native runtime/models, а исходный
OOM process уже погиб. Нельзя доказать no-feature-loss новым lifecycle wrapper
только mocked tests. Дополнительный real conversion на swap-exhausted production
host сейчас недопустим; диагностический агент его не запускал. Production pipeline
не изменён: текущий active OOM risk этим локальным patch не устранён.

Следующее действие для координатора: назвать отдельную QA-машину с существующим
exact 1.28.0 runtime/models, согласовать bounded resource/time budget и разрешить
перенос fixtures/version-source-hash manifest туда. Production остаётся нетронутым.
Сначала сравнить старую и isolated conversion по одному PDF с полным Markdown,
layout/OCR, UTF-8 и measured peak/process teardown; отдельно проверить POSIX
descendant cleanup и startup latency. Для native allocation root на QA нужны small
synthetic bbox equivalence tests dense-vs-blockwise, затем один PDF/job в isolated
subprocess с согласованным external time/resource envelope. Acceptance: идентичные
edges/ties на fixtures, полные Markdown sections/формулы/таблицы/OCR/UTF-8,
измеренный bounded scratch/lifecycle и retry без потери cache/status.
Upstream candidate implementation требует явного расширения scope; dependency
или model installation/downgrade из этого отчёта не следует.

## Выполненные checks и счётчики

- Только bounded SSH journal/proc/thread/library-source/heartbeat reads; один
  non-blocking stack snapshot. Env/key contents/unit definitions не читались.
- Нет production writes, deployment, restarts, ingestion, index rebuilds, installs
  или длинных benchmarks. Чужие dirty изменения сохранены; новый patch касается
  только PDF conversion boundary и fulltext future lifetime, не старых правок.
- Pure-extractor local regression: **25 passed, 3 skipped, 8 executed subtests**,
  ~0.15 s. Три skips: `pylatexenc` отсутствует; real LaTeX conversion ими не проверен.
- Финальный local lifecycle/pipeline regression: **108 passed raw executions,
  4 skipped, 20 passed subtests**, 23.59 s. Новых уникальных test methods: **16**
  (15 passed, 1 POSIX-only skipped). Остальные 3 skips — missing `pylatexenc`.
  Команда: `python -m pytest pipeline/tests/test_pdf_conversion_lifecycle.py
  pipeline/tests/test_extractor_audit_fixes.py pipeline/tests/test_extractor_converter_recovery.py
  pipeline/tests/test_extractor_section_sink.py pipeline/tests/test_parse_safety.py
  pipeline/tests/test_pipeline_audit_fixes.py -q -rs -p no:cacheprovider`.
  `git diff --check -- pipeline/extractor.py pipeline/service.py` passed.
  Две code iterations; все запущенные проверки завершились, heavy work не оставлено.
- Предыдущий readiness regression: **184 raw test executions**, не 184 новых
  уникальных теста. AST recount: **25 новых уникальных методов** (11 bundle/helper,
  10 probe, 4 MCP pagination). Alias superclass содержит 8 inherited test methods;
  inheritance/import collection повторяло часть исполнения. Повторы не новая coverage.

## Реализованный local lifecycle patch (не deploy)

- `extractor.convert_pdf_isolated(pdf_path, timeout)`: fresh `sys.executable -c`
  fixed bootstrap, без отдельного CLI-entrypoint, без импорта `service`/extractor,
  без fork multithreaded parent. Installed library/models/env не меняются.
- Единственный native call остаётся `to_markdown(path, show_progress=False)`.
  Layout, OCR, page selection и остальные defaults не переопределяются.
- Child writes полный Markdown в private temporary UTF-8 file. Parent читает его
  только после exit 0, строго UTF-8 и без newline normalization/character caps.
  stdout/stderr идут в DEVNULL: нет unbounded IPC buffers. Ошибка сообщает bounded
  reason и exit code, не raw native traceback. Это не ограничивает размер полного
  Markdown на диске или одной возвращаемой строки в parent.
- POSIX child имеет собственную session/process group; finally убивает остаток
  группы и reap'ит direct child при success, failure, timeout или interruption.
  Windows local tests проверяют direct-child kill/reap; descendant guarantee там
  не заявляется. Linux/POSIX descendant test добавлен, на Windows он skipped.
- ACL/OpenAlex fetch используют helper с 90/120 seconds соответственно: это
  существующие network timeout values, теперь также independent conversion
  deadlines. Это НЕ общий deadline download+conversion и НЕ прежний SIGALRM.
- `PDFConversionError`: `conversion_timeout`, `conversion_failed` (+ exit code),
  `invalid_output`, `conversion_io_failure`. Timeout/crash/invalid UTF-8/spawn/temp
  failure возвращают no fulltext и остаются в `quality_checked` retry queue,
  даже если stored abstract существует. Existing absent/short source abstract
  fallbacks сохранены, не выдаются за native fulltext success.
- `fulltext_step`: rolling window из `FULLTEXT_WORKERS=4` pending jobs, вместо
  удержания всех 150 futures/results. После completion слот пополняется сразу;
  все выбранные IDs обрабатываются, не режутся capability или batch limit.
  Один текущий consumed future допускает максимум 5 retained futures. DB/cache
  completion guard, sequential commits и retry после ошибки сохранены.
- Fresh process releases native/ONNX allocations после каждого job; parallelism
  остаётся 4. Это не снижает O(N²) peak одного PDF и не гарантирует, что сумма
  четырёх isolated jobs помещается в доступную host RAM. Model startup cost может
  снизить throughput; speed/no-feature-loss без exact native QA не заявляются.
- `_parse_deadline` вне main thread по-прежнему отдельный известный defect;
  этот patch его не маскирует и не перестраивает LaTeX pipeline.

Local QA использует fake converter внутри реального subprocess, а service
functions загружаются AST-only: никаких top-level service side effects/DB/сети.
Проверены полный multi-megabyte Markdown с formulas/algorithms/tables/limitations,
UTF-8/CRLF, fresh PIDs, partial-output rejection, kill/reap, retry/cache preservation,
bounded futures и отсутствие dropped IDs. Это lifecycle/transport tests, НЕ native
PDF layout/OCR equivalence tests. Новые test methods не наследуют чужие harness.

Этот отчёт фиксирует diagnosis, local lifecycle patch и native QA blocker,
а не production readiness/OOM green.
