# Source extraction benchmark: frozen protocol 1

Локальный reusable harness: `ops/source_extraction_benchmark.py`. Это не repair-vN,
не production pipeline и не разрешение на установку, acquisition или запуск.
CLI: `seal`, `collect`, `grade`. Backend runners готовит и проверяет MAIN отдельно.
Никакой production-модуль harness не импортирует.

## Offline adapters (отдельный этап реализации)

`ops/extraction_benchmark_backend.py` принимает обязательные `--input --output
--config --request`. Пример argv для обоих engines в sealed harness config:
`["/eval/venv/bin/python", "-B", "/eval/staged/extraction_benchmark_backend.py",
"--input", "{input}", "--output", "{output}", "--config", "{config}",
"--request", "{request}"]`. Пути здесь иллюстративны, не defaults.
Оба engine объявляют **output_scope: document**. Отдельный pinned backend JSON:

```json
{
  "schema": "dtox-extraction-backend-1",
  "backend": "pymupdf",
  "output_scope": "document",
  "packages": {"pymupdf": "1.28.0", "pymupdf4llm": "1.28.0", "pymupdf-layout": "1.28.0"},
  "native_build": "/eval/baseline/native_build",
  "internal_timeouts_seconds": {"arxiv_pdf": 90, "other_pdf": 120},
  "source_pins": {
    "extractor": {"path": "/eval/staged/extractor.py", "sha256": "REPLACE"},
    "pdf_memory": {"path": "/eval/staged/pdf_memory.py", "sha256": "REPLACE"},
    "common_util": {"path": "/eval/baseline/native_build/pymupdf/layout/common_util.py", "sha256": "REPLACE"}
  }
}
```

Extractor/helper должны лежать рядом. Адаптер проверяет pins и вызывает существующий
`verify_native_build`, затем **один** `convert_pdf_isolated` с native_build и
оригинальными 90/120 s. Не build, не monkeypatch, не upstream substitute. Markdown
сохраняется без изменений, без выдуманной page/section/table/formula разметки.
PyMuPDF metadata используется только для фактического числа страниц, не для текста.
Успешный whole-document вызов доказывает отсутствие page slicing, но не отсутствие
смысловых пропусков; это проверяют независимые references.

Для candidate backend JSON:

```json
{
  "schema": "dtox-extraction-backend-1",
  "backend": "docling",
  "output_scope": "document",
  "packages": {
    "docling": "2.137.0", "docling-core": "REPLACE_ACTUAL_INSTALLED_VERSION",
    "pymupdf": "1.28.0", "torch": "2.14.1+cpu",
    "rapidocr": "REPLACE_ACTUAL_INSTALLED_VERSION", "onnxruntime": "REPLACE_ACTUAL_INSTALLED_VERSION"
  },
  "artifacts_path": "/eval/models",
  "model_pins": [{"path": "/eval/models/REPLACE_EVERY_FILE", "sha256": "REPLACE"}],
  "ocr": {
    "lang": ["ch"], "model_size": "small",
    "det_model_path": "/eval/models/REPLACE_DET.onnx",
    "cls_model_path": "/eval/models/REPLACE_CLS.onnx",
    "rec_model_path": "/eval/models/REPLACE_REC.onnx",
    "rec_keys_path": null
  }
}
```

Это шаблоны, не runnable config. MAIN подставляет реальные pins **до seal**.
`model_pins` перечисляет все файлы локального artifacts tree, включая layout/table
weights/configs и OCR; отсутствующие, дополнительные или изменённые файлы отклоняются.
Symlinks моделей не поддерживаются; OCR checkpoints обязательны и входят в pins.
`rec_keys_path` — pinned файл, если нужен модели; null для встроенного charset.
Язык ровно один, явно совпадает с подготовленным recognizer. Все перечисленные
package versions сверяются до конвертации; полный dependency receipt остаётся в
harness config. Никакой адаптер не скачивает/подготавливает модели.

Docling использует StandardPdfPipeline, CPU/4 threads, RapidOCR/onnxruntime,
local artifacts_path. Remote services/plugins, picture/chart/code/formula
enrichment и image export выключены, document_timeout=None (нет нового default
timer). HF/Transformers offline и CPU env выставляются **до** backend imports.
Настройки сверены с [Docling v2.137.0 options](https://github.com/docling-project/docling/blob/v2.137.0/docling/datamodel/pipeline_options.py)
и [offline RapidOCR implementation](https://github.com/docling-project/docling/blob/v2.137.0/docling/models/stages/ocr/rapid_ocr_model.py).
По actual installed inventory от MAIN: default layout — Heron, upstream revision
main; TableStructureModel использует docling-project/docling-models v2.3.0;
OCR artifacts — RapidOcr, onnxruntime/ch/small. `main` не immutable pin: MAIN
сохраняет resolved commits в acquisition receipt, а adapter проверяет локальные
file SHA. ONNX-layout override этим adapter не выбран; CPU layout остаётся штатным
для pinned Docling. Заявленные ~52 OCR languages не являются quality evidence.
Обязателен внешний sandbox harness: env flags не заменяют блокировку сети/записи.
Единственный output JSON резервируется через exclusive create; существующий файл
не перезаписывается. Library scratch/cache направлены в его parent, не host home.
Не запускать wrapper standalone с production mounts или writable originals.

Docling конвертирует весь документ один раз. JSON содержит полный Markdown,
native_document, native_pages (экспорт уже полученного документа, включая blank
pages), headings/provenance/errors. Структурные projections используют только
native formula labels, native table cells (self_ref ID; start row/column, без
размножения merged cells) и явные native heading children для section body.
Соседние Markdown абзацы не объявляются section body эвристически. Page exports
служат provenance; grading остаётся document-scoped и не доказывает точное
расположение совпадения на source-reference page.

OK требует native success, отсутствие native errors, совпадение полного набора
processed/document pages с PDF metadata count. Partial/ошибки сохраняют доступный
текст/native records и возвращают nonzero; retry/fallback отсутствует.
Original SHA проверяется до и после. Неизвестная edition остаётся в provenance.

**Fair grading:** baseline не предоставляет native structural records;
`structure_support` отмечает not_exposed. Их отсутствие не semantic FAIL, наличие
у candidate не quality PASS/gain. Capability описывается отдельно. Grader использует
одинаковые frozen Markdown checks и отдельные independently reviewed semantic
outputs; без последних NOT_MEASURED и закрытый gate. Mock tests adapters не
подтверждают реальную конвертацию, offline-model completeness или quality.

## До seal

- MAIN выбирает **100 физических реальных документов** из зафиксированного inventory
  независимо от успеха любого extractor. Manifest сохраняет все выбранные строки,
  включая missing/unreadable/неустановленную идентичность. Не заменять failures.
- Классы multi-label: `encoding`, `layout`, `math`, `tables`, `scans`, `source`,
  `healthy` (legacy label контрольной когорты). Controls означают независимо
  проверенные sampled body passages/pages, **не wholepaper guaranteed healthy**.
  Отсутствие BAD-маркеров, arXiv HTML происхождение или старый pilot label не review.
- До результатов зафиксировать references, нормализацию, thresholds, runner/config,
  зависимости, ограничения и sampling method. Не менять gates после holdout.
  Новая конфигурация требует нового seal; это новый эксперимент, не retry старого.
- SHA защищает неизменность, не доказывает правдивость sampling/identity/review.
  MAIN проверяет эти утверждения и evidence отдельно. Review через согласие readers
  не подходит. Evidence URI — ссылка на независимо сохранённый page review,
  не инструкция harness автоматически скачать её.

Manifest JSON, верхний уровень:

```json
{
  "schema": "dtox-source-extraction-benchmark-1",
  "sampling": {
    "method": "REPLACE: independent selection rule recorded before extraction",
    "inventory_sha256": "REPLACE_WITH_64_HEX",
    "selected_before_extraction": true,
    "success_independent": true
  },
  "documents": []
}
```

Каждый document содержит `document_id` (сохранить version, не канонизировать её
прочь), `classes`, `source_kind` (`arxiv_pdf`/`other_pdf`), `exceptions` (строки,
включая candidate-specific limitations; пустой список явно), `references`.
`source_uri`, `source_version`, `page_count`, `real_document` имеют форму
`{"status":"established|missing|unestablished","value":...}`. Неизвестное значение
пишется явно, обычно `null`. Для established page_count нужен положительный int;
для подтверждённого real_document — literal `true`, подкреплённый inventory review.
`original`: `{"path":"/absolute/local/original.pdf","sha256":"64hex",
"status":"available|missing|unreadable|unestablished"}`; path/hash могут быть null
при отсутствии. Нельзя автоматически повысить declared unavailable до available.
Неизвестная source_version **не блокирует** quality grading на тех же sealed bytes
при установленном ID/URI и SHA-bound original. Она остаётся явной в provenance;
historical/corpus edition identity этим экспериментом не доказывается.

Reference example (поля `review` обязательны; отсутствие/неподтверждённость не PASS):

```json
{
  "check_id": "p12-snippet-1", "type": "snippet", "page": 12,
    "locator": "page 12, left column, paragraph 2 / rendered crop receipt", "content_scope": "body",
  "normalization": "exact", "expected": "Independently transcribed text",
  "review": {
    "status": "verified", "method": "independent_page_review",
    "reviewer": "independent reviewer ID", "evidence_uri": "local-review-receipt:ID",
    "original_sha256": "REPLACE_WITH_ORIGINAL_SHA256"
  }
}
```

Quality types: `snippet` (однократное точное вхождение); `ordered_anchors`
(expected — список минимум двух уникальных anchors в заданном порядке).
Один frozen reference и normalization применяются к Markdown обоих engines.
Это проверка видимых строк/порядка, не полная semantic fidelity. Не создавать
engine-specific expected values из результатов. Body evidence обязательно;
одни titles не доказывают body fidelity.

Semantic types: `formula` (source-grounded expected expression), `table_cell`
(source table_id/row/column и expected cell), `section` (title и expected body).
Эти frozen source references **не** сопоставляются с backend arrays/IDs и не
оцениваются substring rule. Они требуют внешнего independent output/page review
обоих результатов на одной source reference; backend self grades не принимаются.
Нормализация только `exact` или явно выбранная `nfc_whitespace`; нет case-fold,
NFKC, dictionary repair, algebraic equivalence или fuzzy matching.
Минимальные quality checks: layout → ordered_anchors; все остальные классы →
snippet. Дополнительные required semantic refs/reviews: math → formula;
tables → table_cell; source → section. Для multi-label нужны все применимые.
`min_reviewed_checks_per_document` теперь минимум quality references; structural
refs не заменяют текстовые. Отсутствие required semantic source ref также закрывает
gate и отдельно показывается в missing_semantic_required_types/documents.
Проверять распределённые страницы; несколько snippets не доказывают полноту статьи.
Для каждого control обязательны ≥2 independently verified quality body refs на
≥2 разных body pages (`content_scope: body`, pages в пределах source page_count).
Titles/abstract-only evidence не подменяет body refs: reviewer подтверждает область
body и locator, а harness проверяет frozen metadata/bindings, не угадывает область
по строке. Для `content_scope` допустимы body/title/other; отсутствие не считается
body. На двухколоночном control дополнительно нужен verified body ordered_anchors.
`healthy_control_review` содержит `status: verified`,
`method: independent_sampled_source_pages_review`, reviewer/evidence_uri/original_sha256
и **явный boolean two_column** (true, если применима проверка двух колонок).
Неизвестный layout не молча single-column: отсутствие boolean закрывает control gate.
`independent_whole_document_review` разрешён как альтернативный более широкий review,
но не обязателен; минимальные body refs/pages/read-order нужны и при нём.

Report.control_scope и document.control_scope: `checked passages/pages NOT wholepaper healthy`.
`control_body_pages` раскрывает sampled scope, `control_review_method` — provenance.
Автоматический completeness report отдельно показывает observed/expected page_count,
coverage/status; `text_completeness_proven: false`. Page count и successful whole-doc
attempt не доказывают body fidelity/полноту текста. Missing body/page/read-order refs,
непроверенный control и control regressions по-прежнему закрывают соответствующие
gates, включая holdout. Этот контракт не требует просматривать сотни остальных
страниц controls и не утверждает их качество.

### Independent semantic output review

Source references, scope и gates frozen **до outputs**. После collection reviewer
сопоставляет source page с реальным сохранённым output, а не наличие native schema.
Для document scope output_locator задаёт проверенный Markdown span; если структура
выражена неоднозначно/утрачена, reviewer фиксирует FAIL с evidence, а не выдаёт
автоматический PASS. Review не читает holdout для настройки extractor/gates.
Внешний JSON, не поле extraction.json:

```json
{
  "schema": "dtox-independent-output-reviews-1",
  "seal_sha256": "REPLACE_SEAL_SHA",
  "reviews": [{
    "document_id": "REPLACE", "check_id": "p12-formula-1", "engine": "pymupdf",
    "status": "verified", "outcome": "PASS",
    "method": "independent_output_page_review", "reviewer": "REPLACE_REVIEWER",
    "evidence_uri": "local-review-receipt:REPLACE",
    "original_sha256": "REPLACE_ORIGINAL_SHA", "output_sha256": "REPLACE_EXTRACTION_JSON_SHA",
    "reference_sha256": "REPLACE_REFERENCE_SHA", "source_page": 12,
    "source_locator": "source page 12, equation 4",
    "output_locator": "document Markdown, reviewed equation span"
  }]
}
```

По одной записи на document_id/check_id/engine (оба engines обязательны для
измеренности required semantics). Reference SHA вычисляется `reference_sha(ref)`:
canonical JSON всех reference fields кроме review/check_id/locator. Seal binding
защищает и остальные frozen поля. Изменённые source/output/reference bindings или
неверный method → NOT_MEASURED, не FAIL. `status: missing|unreviewed`,
`outcome: NOT_MEASURED` допустимы явно; отсутствие записи означает то же.
Duplicate/foreign entries либо foreign seal отклоняют bundle. Для alias IDs refs
сохраняются, физические одинаковые checks dedup; неизвестный alias review не
выбрасывается. SHA/обязательные поля не доказывают добросовестность reviewer — MAIN
проверяет независимость и evidence отдельно. Reader consensus/backend verdict не
считается independent output/page review.

Grade принимает пару `--semantic-reviews REVIEWS_JSON --semantic-reviews-sha256 SHA`.
Отсутствие файла разрешает получить incomplete report, **не** semantic gate PASS.
Quality report сохраняет review-file/bundle SHA. Для quality и semantic отдельно:
declared, missing, passed_over_declared и observed_reviewed_checks. Missing не
вычёркивается из declared denominator и не считается проверенным semantic FAIL.
`observed_reviewed_checks` верхнего уровня row/summary теперь только Markdown quality.
`checks` сохраняет оба типа с measurement/outcome; capability.records_present —
лишь наличие нативных записей, без quality inference.

Gain считается **только** по paired Markdown quality checks. Семантические reviews
служат отдельным required gate: missing закрывает измеренность; candidate FAIL
закрывает соответствующий class gate; независимо проверенный baseline FAIL сам по
себе не запрещает candidate. Проверенные semantic regressions также учитываются в
control/class no-regression gates, но native schema availability никогда не gain.
Healthy/holdout requirements этим изменением не ослабляются.

## Baseline и eval environment

Цель — Oracle ARM/aarch64, CPU16, RAM64 GiB, отдельный
`/opt/dtox-extraction-eval-20261012/venv`; не production venv.
Предоставленный dry-run resolution: Docling 2.137.0;
PyMuPDF/pymupdf4llm/pymupdf-layout 1.28.0; torch 2.14.1+cpu,
torchvision 0.29.1+cpu. Это исходный dry-run, не extraction-quality evidence.

Обновление от MAIN: изолированная CPU-установка на Oracle выполнена,
`pip check` сообщил `No broken requirements found`. ARM native baseline уже собран
существующим helper в `/opt/dtox-extraction-eval-20261012/baseline/native_build`,
`verify_native_build` успешен, patched SHA совпал. Extractor/helper staged в
`/baseline/staged` root444 с проверенными hashes; свободно 82.61 GB. Это сообщённое
MAIN build evidence, **не** evidence PDF extraction или качества. PDF/model downloads
ещё не выполнялись. Будущий Docling adapter: только ONNX Runtime OCR path, code/
picture/formula enrichment OFF; default download_models, тянущий одновременно
torch:ch и onnxruntime:ch, не использовать. Эти настройки должны войти в pinned
candidate config. Реализация adapters — отдельная задача, не часть harness.

Baseline — **current source-equivalent ARM**, не upstream и не deployed x86 binary:

- deployed extractor SHA `c112a5df23ff588ab4dee3f46676a703a009ce0f308058bea073cd11eb50e503`;
- native patch revision `dtox-directional-nn-row-block-v1`;
- upstream common_util.py SHA `f51244f60096f8b01181b330f14d02a5a1f9c9f6755a63e115d553cb80628680`;
- patched SHA `a6ced24e14c96bb073d0fee6dc0af45b4893ec0eddb895e96ed4213a1bc6b5d9`.

Будущий staged ARM build использует существующий
`pipeline.pdf_memory.build_dependency_patch` на ARM wheel и
`verify_native_build`, не перенос Contabo x86 bindings и не runtime monkeypatch.
Harness **не выполняет build/install**. MAIN проверяет wrapper: он должен вызывать
тот же `convert_pdf_isolated` с native_build, OCR/layout defaults, полным документом
и timeout **90 s arxiv_pdf / 120 s other_pdf**. Эти baseline-internal limits
сохраняются в public request и отличаются от optional harness limits. При таком
сравнении нельзя приписывать timeout-free candidate speed/coverage чистому алгоритму.
Порядок запусков чередуется, но это не устраняет thermal/cache/модельные эффекты.

## Config и runner contract

Обязательные поля config (schema как у manifest):

- `job_runtime: "infinity"`, `automatic_retry: false`.
- `baseline_contract`: comparison_kind `current_source_equivalent` либо
  `exact_deployed_build`, description, source_pins (дублируются в baseline engine
  pins), runtime_monkeypatch `false`, internal_timeouts_seconds с arxiv_pdf/other_pdf
  (исходные значения или явно null). Остальной build/source provenance записывается
  в этом же объекте. **Значения SHA/hardware/path не hardcoded в harness**.
- `environment`: точный snapshot команды `environment --packages docling pymupdf
  pymupdf4llm pymupdf-layout torch torchvision`, запущенной будущим eval Python.
  Поля: architecture, cpu_count, ram_bytes (фактическая RAM ОС, не nominal 64 GiB),
  python_executable, python_version, venv, packages. Collect сравнивает фактический
  snapshot с sealed config. Другой hardware/path допустим в новом эксперименте.
- `sandbox`: `launcher` — absolute bwrap path + SHA; `readonly` — список pins
  runtime mounts. Pin: `{path, sha256, kind:"file"|"tree"}`. Tree SHA — SHA256
  canonical JSON отсортированных `[relative_path,file_sha]`,
  `[relative_path,"directory"]`, `[relative_path,"symlink",link_target]`.
  `pin_sha` доступен для подготовки pins, но ничего автоматически не устанавливает.
  `os_readonly` — отдельные OS mounts `/usr`, `/lib`, `/lib64`, `/bin`, `/sbin`
  или узкие fonts/SSL/loader paths, без хеширования всего OS tree. Каждый содержит
  path и `identity_receipt` (file pin). Receipt JSON:
  `{schema:"dtox-os-runtime-identity-1",architecture:"...",mounts:["/usr",...],
  identity_files:[{path:"/var/lib/dpkg/status",sha256:"..."},...]}`.
  Проверяются receipt и небольшие package/OS metadata files. Это package identity,
  **не побайтовая аттестация всей ОС**. `forbidden_roots` — явный непустой список
  production runtime/DB/storage roots; запрещены их mounts и output внутри них.
- `engines`: **только** `pymupdf` и `docling`. Каждый: `role`
  (`exact_current_baseline`/`candidate`), description, whole_document `true`,
  output_scope (`document`/`pages`), dependency_manifest_sha256, `pins`, `config`
  (pinned JSON path), `argv` (список без shell).
  Ровно по одному отдельному аргументу `{input}`, `{output}`, `{config}`, `{request}`.
  Пример: `["/eval/venv/bin/python","/eval/wrappers/baseline.py",
  "{input}","{output}","{config}","{request}"]`. Launcher не shell.
  Для этого run baseline source_pins включают extractor.py с deployed SHA,
  pdf_memory.py и patched common_util.py с указанным выше SHA. В будущем имена/SHA
  определяются config, не глобальным allowlist. Dependency receipt проверяет MAIN.
- `document_limits`: ровно label, wall_seconds, cpu_seconds, address_space_bytes.
  Все три null означают без дополнительных лимитов; **нет default timer**.
  Числа вводятся только заранее. CPU/AS — POSIX per-process inherited rlimits,
  не aggregate job/cgroup limits; AS не равно RSS. Wall — на один launch.
- `gates`: expected_physical_documents (`100` для реального этапа),
  min_documents_per_class_split, min_reviewed_checks_per_document,
  min_observed_check_ratio (`{all: number, encoding: optional number, ...}` —
  candidate thresholds по классам, all как fallback), max_healthy_regressions,
  max_class_regressions, min_candidate_check_gain. Все thresholds задаются MAIN
  **до seal**, harness не выбирает их по результатам.
  Optional `required_classes`: непустой список уникальных class names выбранного
  route, frozen до outputs; default — все классы для backwards compatibility.
  Все классы всё равно выводятся. Отсутствующий в sample класс получает
  measurement_status NOT_MEASURED, exception CLASS_ABSENT_FROM_FROZEN_SAMPLE,
  decision NOT_ACCEPTED, не PASS. Unrequired classes не принимаются этим route.
  `required_route_status: PASS` возможен только при eligibility всех required
  classes и control/holdout gates; при subset decision остаётся class-specific,
  universal_winner=false. Controls обязательны и при отсутствии healthy в
  required_classes. Изменение route после outputs запрещено, нужен новый seal.
  Optional `min_semantic_checks_per_class_split`: counts по math/tables/source,
  например `{"math": 4,"tables": 6}` — **иллюстрация**, не defaults. MAIN задаёт
  реальные counts до outputs; они считаются по distinct physical frozen references
  соответствующего semantic type отдельно в development/holdout и для обоих engines.
  Missing required reviews или недостаточный declared count закрывают class gate.
  Никаких hardcoded 10+ checks. Existing per-document semantic requirements остаются.

Sandbox: Linux bubblewrap PID/network namespaces, no inherited secrets,
single original read-only, runtime mounts read-only, only per-engine result writable.
Default `sandbox.execution_identity` — `{"mode":"unprivileged"}`; root collector
без opt-in запрещён. Для подтверждённого Oracle plumbing допускается ровно
`{"mode":"root_setup_fixed_uid","uid":65534,"gid":65534}`: collector требует
euid0. Root branch использует `--unshare-ipc --unshare-pid --unshare-net
--unshare-uts --unshare-cgroup`, **без userns**, и `--chdir /` (не /output до setuid).
После `--cap-drop ALL` только trusted helper получает `--cap-add CAP_SETUID`,
`CAP_SETGID`, `CAP_SETPCAP`. Обязателен `sandbox.setpriv` pin:
`{"path":"/usr/bin/setpriv","sha256":"9922bc8da40cd9f1024beae714aee76c2b7d2abac563391235f32e30141d35da"}`
(SHA данного Oracle run, не универсальная константа). Preflight проверяет SHA и
наличие helper в read-only runtime mounts. Перед engine argv вызывается setpriv
`--reuid=65534 --regid=65534 --clear-groups --bounding-set=-all --inh-caps=-all
--ambient-caps=-all --no-new-privs --`. Default unprivileged branch сохраняет
`--unshare-all`; root branch не использует `--uid/--gid` bwrap.
Никаких privileged parsers, weakening AppArmor/sysctl или fallback на host.
Старый probe с `uid_map: 65534 0 1` НЕ доказывал deprivilege: это host-root.
По сообщению MAIN исправленный empty probe 22:03:16Z подтвердил identity uid_map,
реальные uid/gid65534, CapInh/Prm/Eff/Bnd/Amb=0, host-written file owner65534,
output write и отсутствие /opt,/home/routes. Receipt:
`/opt/dtox-source-recovery-20261012/bwrap-real-uid-plumbing/cwd-fixed-receipt.json`.
Это plumbing evidence, не исполнение данным harness и не PDF parsing acceptance.

Root mode требует `execution_identity_uid: 65534` в обоих pinned backend configs.
Backend сохраняет UID/EUID/GID/EGID/groups, все пять capability sets, uid_map/gid_map
и NoNewPrivs в result provenance. До PDF hashing/backend imports проверяет реальные
65534, пустые supplementary groups, caps0, identity mappings и NoNewPrivs=1;
ошибка остаётся ERROR, не conversion success. Поле optional для test/backcompat,
но root preflight требует его. Backend меняет cwd на output parent перед imports,
уже после setpriv. Права output не расширяются.

Outer output создаётся exclusively root-private0700; fixed-UID mode проверяет
owner/mode до launch. Только свежие per-engine directory/home/tmp получают
chown65534, не originals/runtime/широкие пути. После завершения executor collector
возвращает **только новый per-engine bind root** root:root0700, без recursive chown,
до чтения outputs. MAIN обязан обеспечить чтение staged runtime и single original
UID65534 без изменения originals этим harness.

`sandbox.output_read_limits` может явно задать положительные integer fields
json_bytes/log_bytes/artifact_bytes/max_entries. Default, закреплённый harness SHA:
64 MiB JSON, 16 MiB каждый log, 64 MiB каждый прочий artifact, 4096 entries на run.
Это security read bounds, не document extraction timers; большие outputs дают
INVALID_OUTPUT/inconclusive, не пропадают из denominator. Inventory streaming,
без обхода symlinks; symlink/junction/reparse, nonregular и hardlinked files
отклоняются до content reads. JSON/log SHA и JSON parsing используют bounded
descriptor reads, O_NOFOLLOW/O_NONBLOCK на Linux, fstat identity/change checks.
Postconditions проверяют весь per-run output inventory, включая caches/artifacts,
не только extraction.json. Плохие artifacts не читаются collector root. Если parser
занял reserved collector result.json, exclusive write останавливает collection;
не перезаписывать/продолжать такой run. Никаких parser runs до отдельного review.
Config должен монтировать узкие staged runtime trees, **не repo/storage/home/active
DB/Qdrant/model-download cache**. Runtime pins не могут перекрывать originals,
manifest/config inputs, seal и outputs. Symlinks runtime учитываются в tree SHA;
executable target должен находиться в mounted runtime. Все веса/models заранее
размещаются в pinned read-only runtime; сеть отключена, auto-download не работает.
Не ослаблять sandbox, если namespaces недоступны: это prerequisite failure.
Никаких references/classes/split harness не передаёт runner. Request содержит
только ID, source metadata/hash/kind, whole-document flag и baseline timeout.

Runner пишет extraction.json и любые raw artifacts в свой `/output`, логи stdout/
stderr сохраняет parent. Нормализованный output — **один** из вариантов:

```json
{"document":{"text":"full unchanged extracted Markdown","formulas":[],"tables":[],"sections":[]},
 "coverage":{"whole_document":true,"page_count":100}}
```

либо `{"pages":[{"page":1,"text":"...","formulas":[],"tables":[],"sections":[]}, ...]}`
со всеми page IDs, включая blank pages. Tables: `{id,cells:[{row,column,text}]}`;
sections: `{title,text}`; formulas — строки. Нет first-N-pages.
Document mode нужен для текущего whole-Markdown baseline: не выдумывать page
assignment и не заменять baseline отдельными per-page conversions. References
остаются source-page-grounded, но location match в этом режиме **document-wide**,
что явно записывается в отчёт. Coverage metadata не является независимым доказательством
полноты: MAIN проверяет pinned runner отсутствие page limit. Page count независимо
подтверждается в manifest. Backend quality/verdict fields игнорируются.
Adapters не меняют extraction features; их normalization/mapping code тоже pin.
Отсутствие формулы/таблицы в структурном output — FAIL соответствующей проверки,
не разрешение backend самостоятельно присвоить себе PASS.

## Фазы и доказательства

```text
python -B ops/source_extraction_benchmark.py seal --manifest MANIFEST --config CONFIG --output NEW_SEAL_DIR
python -B ops/source_extraction_benchmark.py collect --seal SEAL_JSON --seal-sha256 SHA --output NEW_COLLECTION_DIR
python -B ops/source_extraction_benchmark.py grade --seal SEAL_JSON --seal-sha256 SHA --collection COLLECTION_JSON --collection-sha256 SHA --output NEW_GRADE_DIR
```

Это интерфейс, не команды к исполнению сейчас. Каждый output directory новый,
parent уже существует; mkdir exclusive + постоянный LOCK. Нет resume/overwrite/
automatic retry. Interrupt сохраняет partial files; incomplete collection не
принимается как complete. Failed/unreadable inputs дают две явные engine records,
не исчезают. Physical aliases запускаются один раз, строки и distinct reference
checks остаются видимы; identical checks не размножают физическое evidence.
Conflicting duplicate IDs останавливают seal, exact duplicates считаются явно.

Split: physical content groups стратифицируются: любой healthy alias относит всю
группу к control, иначе challenge. Внутри каждой страты сортировка по
SHA256(min doc ID группы), doc ID tie-break; первые floor(4*N_stratum/5) development,
остальные holdout. Для 80 challenge +20 control это64/16 и16/4, итого80/20 физических
документов. Policy записывается в seal payload. Aliases не пересекают split;
logical-row counts отдельно. Добавление документа меняет
эксперимент и требует нового seal, split не пересчитывается в существующем.

Collection сохраняет argv, order, before/after original SHA, per-engine directory,
exit/status/error, stdout/stderr hashes, output SHA, elapsed, CPU, peak RSS.
Outer runtime/model/OS-identity/environment preflight теперь имеет отдельные
elapsed/self-CPU в collection.preflight_costs и quality.cost_scope.outer_preflight;
для старых/uninstrumented collections — null, не выдуманный ноль.
Per-document resource metrics остаются полным cold wrapper process: validation,
hashing, imports, model loading, conversion, export. Повторный asset hashing
**сохранён**: readonly bind mount не исключает host mutation. Его отдельное время
не instrumented внутри adapter: cost_scope.asset_hashing помечает NOT_MEASURED,
included_in_per_document_metrics=true. Нельзя называть эти времена inference-only
или вычитать оценочный hashing cost. Новый immutable snapshot/preflight-once путь
требует отдельного одобренного изменения, здесь не реализован.
Linux wait4 rusage: CPU launcher с учтёнными descendants; peak RSS **не сумма
concurrent process-tree RSS**. SIGKILL не объявляется OOM без evidence. Никакого
job-level timer; только explicit per-document limits и существующие baseline limits.

Grade проверяет цепочку seal → collection → results/logs/output. Reports сохраняют
documents/classes/splits, physical/logical denominators, PASS/FAIL/unknown checks,
exceptions. Missing/unreviewed/unbound refs, identity/health gaps, incomplete
output дают `INCONCLUSIVE_NOT_MEASURED`; unknown никогда не PASS. Check ratios —
только observed independently reviewed checks, **не full precision/recall**.
Baseline обязан быть измерен, но **не обязан PASS**: его failures — часть сравнения.
`class_decisions` допускают `CLASS_SPECIFIC_CANDIDATES`, если выбранный класс проходит
development/holdout, независимые healthy controls, sample size, references,
predeclared candidate ratio/improvement и class no-regression gates. Неудача другого
класса не превращается в глобальную победу и не запрещает class-scoped eligibility;
все его failures/denominators сохраняются. Глобальный PASS требует всех классов.
Даже успешный результат
не разрешает production publication, reindex или migration.

Source-image semantic gold (frozen before collection): formula/table_cell/section
refs may use `expected_kind="source_image"`, `expected_image={path,sha256}` (absolute
grading-side path), `original_sha256`, physical `page`, and `source_locator`.
`expected` is a descriptive region label, never a transcribed expression or substring
check. Source review must independently bind original SHA and image SHA. Page-wide
regions such as “all mathematical expressions on this source page” / “all table
cells on this source page” require explicit curator confirmation of the observed
dimension on that identified physical page; no first-equation/cell heuristics.
The manifest/seal and reference SHA bind all these fields; seal rehashes gold bytes.
Gold images stay grading-side, outside all parser runtime mounts and requests.
Later `independent_output_page_review` must bind seal, reference SHA, original SHA,
actual output SHA, source page, `expected_image_sha256`, the exact frozen
`source_locator`, and `full_declared_region_reviewed=true`. Review covers the full
declared source region against actual output, not native structured-field availability.
Missing/unreviewed/ambiguous/unreadable/changed gold or output review stays
INCONCLUSIVE_NOT_MEASURED; no automatic PASS and no semantic substring evaluation.
For image-based table regions no fabricated table ID/row/column coordinates are
required. Textual snippet/ordered_anchors checks remain common Markdown checks with
NFC + whitespace normalization only; no casefold/NFKC/fuzzy/word repair. All classes,
missing checks, controls and holdout denominators remain in reports. This bounded
page-region evidence does not claim whole-paper semantic fidelity or healthy papers.

Pre-output paired gain counts all frozen checks measured independently for BOTH
engines: common Markdown checks plus semantic output reviews, never native schema
capabilities or missing outcomes. Regressions retain the same paired definition.
Positive improvement is required for nonhealthy repair classes, not controls.
Healthy controls separately require full measurement, candidate ratio 1.0 and zero
regressions; `healthy_gate_status` reports this mandatory gate independently of
required repair classes. This run freezes encoding/layout/math/tables as required
repair classes, with all 20 controls retained; absent source/scans stay NOT_MEASURED.

Future integration: существующий `publication.prepare_snapshot` — FIRST PUBLICATION
ONLY, он отклоняет legacy/already_published. Updating/replacing published documents
потребует отдельного reviewed transactional/versioned workflow; benchmark его не
реализует и не считает уже готовым.
