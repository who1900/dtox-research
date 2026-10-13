# Архитектурные исправления: 2026-10-06

Ограниченный первый этап deployment выполнен guarded rollout и проверен read-only smoke. Это не завершённая архитектурная миграция. Корпус не объявлен восстановленным; production readiness не подтверждена. Статусы и результаты ниже являются переданными наблюдениями на момент проверок, а не новыми live-проверками при написании документа.

## Текущий статус и пауза

- Production pipeline, API и MCP активны на проверенном deployed code; API health `status=ok`.
- На проверке Qdrant: `green`, optimizer `ok`, **10 910 862 points**. Это число точек, не `paper_count` и не число полных статей.
- Старый `reprocess.timer` остановлен **и disabled**.
- Transient corpus worker остановлен; journals сохранены. Исторические записи о running worker больше не описывают текущий статус.
- Recovery worker остановлен; его локальная замена в production не выкладывалась.
- Полные сканы, corpus build и миграции для этих исправлений не запускались.

## Что изменено локально

В ограниченный rollout вошли parser/status, collector, canonical API-кандидат, FTS completion, reprocess fail-closed и общий document module (7 файлов, включая две идентичные копии модуля). Локальными остались x402 schema/retry changes и независимые recovery engine/planner/runner. Gateway runtime не менялся.

- **Parser P1:** исправлены service failure/empty handling и extractor collector. Новая extractor policy не вводилась; это не доказательство восстановления ранее потерянного текста всего корпуса.
- **Canonical collector/read:** unsplit `section_sink` собирается в том же parser pass, без второго parse и per-chunk duplication. Default chunk output не изменён. Сохраняются повторяющиеся code/algorithm/formula и их whitespace; builder отвергает legacy split guessing. Manifest задаёт derived canonical section text в исходном порядке source, отдельно от per-point text и stable UUID. Чтение ограничено cache hash и gate `status=done`; parser не запускается в request path.
- **x402:** общие Zod-схемы платных tools задают единый источник MCP-контракта и generated discovery JSON Schema. Read retry классифицируется по structured error/status/cause; неизвестная ошибка не повторяется. Signed operations никогда не повторяются. Defaults, validation, compatibility, network/auth и цены платежей сохранены.
- **FTS build:** durable completed state отделён от стартового cursor. Страница, cursor и completion фиксируются одной SQLite-транзакцией, включая пустую финальную страницу. Legacy JSON `null` означает завершённый build; отсутствующий offset означает первый запуск. Завершённый повторный запуск не делает Qdrant calls/inserts; failed page не завершает build. Top-up сохранён. Это не решение глобальной UUID-уникальности legacy FTS.
- **Recovery engine writer RETIRED:** prepared/partial journals v1/v2 отвергаются до любых мутаций. Для `done` v1 разрешена только scoped read-only verification cache, Qdrant и receipt новых FTS rows. Результат явно сохраняет `legacy_fts_baseline_verified=false`: same-paper baseline proof остаётся pending. Глобальных FTS-сканов нет; receipt новых строк не доказывает legacy baseline или глобальную UUID-уникальность. Активного независимого writer нет; новый apply остаётся fail closed, не использует lexical fake proof и не возобновляет partial journals записью.
- **Coverage:** missing source/cache и unsupported inputs остаются uninspected/unresolved, не считаются resolved.
- **Reprocess RETIRED/read-only:** retirement из-за дефекта доступа при `status=done` проверен. `--apply` возвращает exit code 2 до открытия DB и reset, без изменений. Timer остановлен и disabled.

## Проверки: отдельные, неаддитивные прогоны

| Контур | Переданный результат | Граница подтверждения |
|---|---|---|
| x402 | 46 tests PASS; build PASS | Main независимо подтвердил результат |
| Native pipeline | 115 tests PASS; 20.893 s | Включая FIFO |
| Native ops | 112 tests PASS; 18.720 s | Planner / reprocess / engine / runner |
| Native API candidate | 8/8 tests PASS; 0.427 s | Три функции, с PROD modules |
| Main local | 120 tests PASS; 14.476 s | Включая canonical reader |
| Local pipeline | 115 tests: 114 PASS + 1 Windows FIFO skip; 11.838 s | Skip не считается PASS; native FIFO проверен отдельно |
| MCP | 36 tests PASS | Отдельный прогон |
| API selected | 72 tests PASS | Выбранные тесты, не полный API suite |

Эти числа нельзя складывать: прогоны могут пересекаться и относятся к разным снимкам кода. Ранее переданные 26 parser/reprocess, 74 pipeline и 15 build safety PASS являются историческими отдельными прогонами, не прибавляются к новым результатам. При написании отчёта тесты и live/network-проверки не запускались.

## Ограниченный API-кандидат

Prebuilt API-кандидат ограничен тремя функциями относительно hash-guarded production baseline `c7fc69`; переданный SHA-prefix кандидата `bff708...`. Такой кандидат сохраняет unrelated dirty local API, не копирует всю локальную версию. Native-проверка с PROD modules: **8/8 PASS, 0.427 s**. Первоначальный import failure был только environment-проблемой; после корректного stage `PYTHONPATH` прогон прошёл, без исправления кода ради этого сбоя.

Проверенный ограниченный API-кандидат выложен в `/opt/dtox-research-api/main.py`; unrelated dirty API changes не выкладывались. Private native test artifacts остаются в `/opt/dtox-architecture-20261006.JHtOTn`. Это не подтверждает всю локальную API-версию.

## Что ещё не исправлено

- **Один publication owner:** единый владелец публикации согласованных Qdrant/FTS/cache/metadata представлений ещё не реализован.
- **Global FTS identity migration и same-paper baseline proof:** exact indexed UUID identity для legacy/unmapped FTS и доказательство baseline той же статьи ещё не установлены. Миграция и сканы не запущены; retirement writer не решает эти задачи и не заменяет их receipt новых строк.
- **Исторические missing inputs:** нужны fresh acquisition и отдельная проверка; parser fix не создаёт отсутствующие source/cache.
- **Search policy consolidation:** единая production search policy и сведение накопленных веток остаются pending.
- **Canonical whitepaper identity migration:** каноническая source identity/provenance с совместимостью legacy alias остаётся pending.

Фактическое будущее corpus recovery остаётся заблокированным до одного publication owner и exact mapping migration, без обхода full scan. Missing inputs требуют fresh acquisition. Сертификат научной полноты корпуса не получен.

## Deployment

**deployed: ограниченный первый этап**

История: первая попытка была отклонена auto-review до выполнения. После явного разрешения пользователя guarded rollout завершился успешно. Backup создан в `/opt/dtox-architecture-backups/20261006-20261006-135353-c8aa9708`; в нём проверены хэши 5 ранее существовавших code files. Это code-only backup, не полная копия БД. Все 7 целевых файлов имеют ожидаемые deployed hashes; 6 additive parse columns присутствуют. Перезапущены только `dtox-research` и `dtox-research-api`; MCP не перезапускался. Локальные x402 changes и ops recovery engine в production не выкладывались. Commit/push не выполнялись; secrets и payment runtime не менялись.

Bounded post-deploy smoke: три пары MCP до/после совпали по источникам, тексту и хэшам (EIP-4337 search; 347-символьная section; 7 фрагментов с уравнениями D2O из 39 доступных). Для исторического section order указано `verified=false` с явным legacy fallback. API health `ok`, все три unit активны, Qdrant `green`, optimizer `ok`, **10 910 862 points**. Все 14 стадий запущены, `NRestarts=0`. В `pipeline.log` (серверная зона CEST, UTC+02:00) после cutoff `2026-10-06 15:53:55` просмотрены 40 строк без ошибок/SQLite lock; прежние lock errors от `2026-10-06 11:12:59` не относятся к post-deploy окну. В journal API 18 строк, startup complete, ошибок нет.

Это короткий smoke, не полный SLO, performance improvement или сертификация всего корпуса. Единственная health выборка не доказывает улучшение производительности. Production `--apply` не запускался: команда была заблокирована до выполнения; native fail-closed tests остаются валидными. Никакого дополнительного разрешения для уже выполненного ограниченного этапа не требуется.

## Историческая запись

[Corpus prose recovery](2026-10-06-corpus-prose-recovery.md) сохранён как исторический документ с новым superseded/current-pause banner. Его прежние test counts, pilot и running observations не являются подтверждением состояния текущего revised code или восстановленного корпуса.
