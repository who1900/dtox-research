# SPLADE candidate channel: preregistered experiment

> **Оговорка об ошибке конфигурации (2026-10-04).** В `eval/splade_candidate_probe.py` ни `_set_env`, ни config не задавали `HIER_CHUNK_LEXICAL`; production `systemctl` подтвердил значение `0`, тогда как на строке 128 main.py значение по умолчанию равно "1". Сохранённый `probe.json` содержит 33 пары: baseline 31 paper + chunk / 2 paper; treatment 31 paper + chunk / 2 paper. У первого результата treatment, eip:1155, fusion_score=0.025653; внутренние ранги: dense=2, lex=2, global=9. Это соответствует secondary RRF `1/62 + .6/63`, а не hierarchical `2/.8/1`. Прежнее равенство 15/33 → 15/33 описывает стендовый режим с chunk lexical и повторной fusion, а не active production setting `HIER_CHUNK_LEXICAL=0`. Это ошибка настройки стенда и приписывания результата production-конфигурации, не свидетельство невоспроизводимости данных или нечестности пилота. Исправленная диагностика завершена: 32 полные пары и 1 partial за 93.333 секунды. Результаты и ограничения приведены в [отдельном отчёте о поисковых потерях](2026-10-04-search-loss-diagnosis.md).

Статус: архивный результат с оговоркой о конфигурации. Первичный прогон не является корректным сравнением с active production lexical setting (0). Исправленная диагностика завершена и описана в связанном отчёте: 32 полные пары и 1 partial.

## Гипотеза и границы

Проверяем, улучшит ли ограниченный SPLADE-канал полноту поиска по замороженному пилоту из 4 023 бумаг: рассмотреть первые 40 SPLADE-кандидатов и добавить к существующим Stage A dense-40 + FTS-15 не более 10 новых canonical кандидатов. Положительный результат означает только прохождение указанного диагностического гейта, не готовность к выпуску.

Базовая точка: заявленный production main SHA `bd38ed430a1ced3167ff4eef7c075e4bd5d662c87c4dfd62d3589c74a8387d19`. Состояние активных grouped/rank channels: 0. В этом эксперименте существующие graph, global, floors и exact/soft layers не меняются. SPLADE-derived score в ранжирование не передаётся: входной список содержит только ranked IDs. Для каждого добавленного кандидата `paper_score = min(p['paper_score'] for p in ORIGINAL_dense_FTS_shortlist)`; то есть всем добавленным бумагам назначается один и тот же floor из существующего dense+FTS shortlist. `chunk_relevance_floor` не меняется. SPLADE не получает отдельный голос в RRF.

## Фиксированные данные и выборка

- 33 замороженные фразы: 21 исходная и 12 holdout, 11 групп по 3 фразы.
- Метки используются только для расчёта метрик, не для выбора кандидатов, настройки, порогов или параметров.
- Запустить диагностику для всех 33 фраз; не ограничиваться шестью фразами с известным улучшением.
- Бумажные якоря намеренно смещают фон выборки. Результаты диагностические и не оценивают несмещённое качество на продакшене.
- SPLADE restricted pilot: 4 023 бумаги; полнота покрытия полного SPLADE-корпуса в Qdrant не измерена.

## Протокол сравнения

Сравнить baseline и candidate на одних и тех же 33 фразах, попарно, чередуя порядок baseline/candidate. Запустить полный текущий search code в приватном серверном процессе; это не чистый офлайн-поиск. Офлайн sampled только SPLADE shortlist. Использованные embeddings были cached; latency не считать доказательством end-to-end скорости. Не изменять baseline-результаты, порядок, scores или состав исходного префикса: candidate обязан сохранять весь исходный префикс Stage A без перестановок и изменений и только добавлять новые canonical candidates. Проверить это отдельными preservation assertions.

Замеры локальной задержки не подтверждают production latency. Ранее наблюдавшиеся локальные SPLADE CPU 111/140 ms не являются Oracle-измерениями и не используются как доказательство производительности или критерием успеха.

## Заранее установленный gate

Gate проходит, только если одновременно выполнены все условия:

1. Есть валидная пара baseline/candidate для всех 33 фраз.
2. Ошибок и частичных результатов не стало больше.
3. В целом и отдельно на holdout не ухудшились ни final hit@10, ни MRR@10. По каждой из 11 групп не уменьшается индикатор `all_3_hit@10`: если все три формулировки baseline-группы находят свои цели в top-10, treatment также сохраняет все три.
4. Ни одна известная baseline-цель не потеряла hit.
5. Число hard-negative alerts не увеличилось. Это проверка тревог, не precision.
6. Есть хотя бы один положительный сдвиг: вырос final hit@10 или MRR@10.
7. Все assertions сохранения Stage A пройдены.

Любое невыполненное или нерассчитанное условие означает, что gate не пройден или не подтверждён; усреднение улучшений не отменяет отдельных регрессий.

## Безусловное ограничение выпуска

В рамках этого запуска SPLADE в production не деплоить независимо от результата gate. Пилот покрывает только 4 023 бумаги с принудительно включёнными якорями; полное покрытие SPLADE в Qdrant и ресурсы/производительность production не измерены. Офлайн-результат не снимает это ограничение.

## Результаты и артефакты

## Фактический результат

Источники: [финальная сводка парного прогона](../eval/results/2026-10-04-splade-candidate-summary.json), evaluation `paired_final_search_known_source_probe`, и локальный приватный артефакт `.scratch_dtox/splade-candidates-20261004/probe.json`. SHA-256 финальной summary JSON: `4b80b919a628918536aa9b374f09169dc4c2759447fdf1e33807c3030e73f52a`; SHA-256 исходного кода анализатора (`source_sha256`): `2a0785ec2b80c16cf309e7fb7f2d3923be925cce34444685edb81058ade86c13`; SHA-256 raw probe JSON: `3445a5fa9e2dba072822ec96796fb0765e189cf9f5b80fdb134810bb3a49cd92`. Probe: 33/33 завершённых пар, 0 skipped, `runner_error` null, elapsed 163.626 s; append limit 10, examined SPLADE rank limit 40. SHA main совпал с preregistered `bd38ed430a1ced3167ff4eef7c075e4bd5d662c87c4dfd62d3589c74a8387d19`.

- Валидные пары: 33/33; failed, missing, duplicate, skipped, error, partial, extra, malformed и fixture mismatch: по 0.
- Overall: baseline и treatment final hit@10 = 15/33; MRR@10 = 0.3167 для обоих.
- Holdout: baseline и treatment final hit@10 = 2/12; MRR@10 = 0.1042 для обоих. На исходных 21: hit@10 = 13/21 и MRR@10 = 0.4381 для обоих.
- По 11 группам all-three-hit@10 status не ухудшился ни в одной. Две группы имели все три цели в top-10 и сохранили это; ни одна baseline-цель не потеряла hit.
- Hard-negative inspection warnings: 1 overall и 0 holdout в обеих ветках; роста нет. Это warnings, не оценка precision.
- Добавлено ровно 10 новых кандидатов на запрос, 330 суммарно. Реальный primary Stage A: 1 695 -> 2 025; это число не включает некоторые soft-partner additions на последующих этапах. Сумма количества результатов по 33 final top-10: 322 -> 321; это не число уникальных ID в объединении.
- Summary сообщает 0 target wins, 0 losses, 33 ties; ни одна известная цель не потеряла hit. В восьми запросах вытеснен baseline top-10 ID, и все восемь вытесненных ID не были target IDs, поэтому нельзя утверждать, что весь ranking остался неизменным.
- Probe показывает, что в 7 запросах среди добавлений был known-source target. В 3 запросах baseline уже находил один допустимый целевой источник; это не устанавливает, что совпадал тот же документ или канал. В 4 baseline-missing случаях добавление не привело цель в final top-10: capped-delegation (`eip:20`), guess-and-check (`2211.17192`, `2302.01318`), frozen LoRA (`2106.09685`) и HotStuff three-phase (`1803.05069`). Новых final target hits не появилось. Это подтверждает, что добавления реально проходили в пайплайн, но не дали новых ответов.
- Prefix preservation assertions пройдены в коде. Runner не записал отдельные prefix/lexical boolean fields; независимая проверка этих assertions по raw output невозможна. Поэтому наличие code assertion зафиксировано, но самостоятельного raw-подтверждения нет.
- Latency single sweep: cached embeddings; p50 baseline 1734.81 ms -> treatment 2165.9 ms, p95 5022.674 ms -> 5009.308 ms, mean 2374.38 ms -> 2499.97 ms, paired mean delta +125.593 ms, median delta -59.22 ms. SPLADE query encoding исключено. Это шумный одиночный замер, не end-to-end speed proof; по нему нельзя заявлять ни ускорение, ни статистически значимую регрессию.

## Runtime и состояние индекса после проверки

- Post-check: API OK, оба Qdrant green, optimizer OK, queue 0. Production main SHA остался `bd38ed430a1ced3167ff4eef7c075e4bd5d662c87c4dfd62d3589c74a8387d19`. Временный серверный probe удалён.
- Между pre/post health счётчиками coarse index изменился 180186 -> 180187, full index 10744782 -> 10744870: фоновой live ingestion добавил 1 paper и 88 chunks. Фразы, vectors и SPLADE pilot были frozen, но индекс не был frozen; ingestion не останавливался и не изменялся.
- Тесты экспериментального кода: 55 unit tests passed, 3 subtests, 1.27 s. Это не полный production suite.
- Post-check health не является доказательством production latency или readiness. Paid API не вызывались; production services не менялись.

Итог preregistered screen: FAIL. Нет положительного прироста final hit@10 или MRR@10. Prefix preservation assertions пройдены в коде, но runner не записал отдельные prefix/lexical boolean fields, поэтому независимая проверка по raw output невозможна. Остальные quality checks сводки прошли. Рекомендация: HOLD_NO_DEPLOY, не выполнять full reindex. Следующий шаг исследования: проверить Stage B и final rank path, не запускать model reindex на основании этого пилота. Ограниченный 4k SPLADE pilot с anchor bias не доказывает бесполезность полного SPLADE. Coverage полного корпуса и production latency остаются невалидированными.
