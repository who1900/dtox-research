# Source discovery

`SOURCE_DISCOVERY=1` включает вспомогательный канал чтения поверх уже
полученных результатов: основной список `results` и `count` не меняются.
Возвращается отдельный `source_candidates`, не более `min(limit, 10)` записей.
В compact cards нет excerpt text, terms, repos, layers и аналогичных широких
полей. Сохраняются ID, title, URL, section metadata, acquisition evidence и
provenance, включая fragment scope/hash. `evidence.returned_chars=0` отражает,
что текст фрагмента в карточке не возвращён.

Кандидаты выбираются чередованием fused- и citation-ranked каналов из уже
имеющихся hierarchy/citation pools; существующие хиты не дублируются. Новых
поисковых или модельных вызовов нет. При `strict`, заданном `min_score` или
`dedupe=false` канал исключён. Validate и research bundle используют только
`results`, не кандидатов.

Кандидаты являются непроверенными leads для чтения: citation указывает, что
читать, но не подтверждает релевантность; соседняя работа может описывать другой
механизм. Сначала проверьте `results`; если подходящего материала там нет,
изучите outline или sections кандидата и сравните механизм. Не объявляйте prior
art автоматически.
Дополнительный список увеличивает объём ответа и контекстные затраты.

## Retrieval benchmark

В основном retrieval benchmark завершились 32 из 33 запросов. Primary exact:
32/32; hit@10 без изменений: 16/32; combined discovery: 29/32. Зафиксировано 9
candidate observations на известных hard negatives. Три пропуска относятся к
complete queries: episodic-diagnosis, three-phase-quorum-certificates и
responsive-bft-leader-failover. Отдельно есть один incomplete treatment partial
в testcase capped-delegation, не входящий в эти три пропуска. Причина partial,
returned partial warning не сохранился. Источник-критерий
29/32 измерялся до последующей serialization/hydration оптимизации, форма
карточек проверялась позже. Не считать это доказательством прироста precision;
зависимость поиска от формулировки полностью не устранена.

До оптимизаций медиана дополнительного вывода составляла 19,090 символов
(+120.6% aggregate); baseline p50 1,226 ms, treatment 1,887 ms, среднее
увеличение 528 ms. Этот результат не является post-optimization speed proof.

В финальном 4-pair server diagnostic завершились обе стороны только у двух
пар. technical-interface: old 981.11 ms, new 1,809.34 ms, обе complete,
primary одинаковый, cards без excerpt. frozen-LoRA: old 1,078.16 ms, new
646.31 ms, обе complete, primary одинаковый. Две скорости направлены в разные
стороны, поэтому ускорение не доказано. guess: old 1,654.87 ms complete, new
2,926.16 ms partial, исключено из сравнения. balanced: old 2,439.38 ms partial,
new 3,006.46 ms partial, исключено. Эти partial не используются для вывода о
скорости.

Новые compact cards уменьшили, но не устранили output overhead: technical-interface
26,994 против 15,780 символов, разница 11,214 (+71.1%); frozen-LoRA 29,090
против 17,553, разница 11,537 (+65.7%). Нулевой token/output overhead не
заявляется.

Metadata lookup для source candidates и results теперь объединён: один общий
lookup на search level. Точный новый путь делает point lookup по `arxiv_id`,
выбирает `PAPER_COLUMNS + status` и проверяет `status == done` в Python, без
schema changes или query hints. На трёх наборах ID строки совпали; замер point
lookup составил от 0.42 до 0.52 ms против 458 ms у старого `status` index scan.
Добавлены три dedicated SQL point-lookup tests.

Metadata lookup для candidates и results объединён в один lookup на search
level. Point lookup по `arxiv_id` выбирает `PAPER_COLUMNS + status` и проверяет
`status == done` в Python без schema changes или query hints. На трёх наборах ID
строки совпали; замер составил от 0.42 до 0.52 ms против 458 ms у старого
`status` index scan. Добавлены три dedicated SQL point-lookup tests.

Финальный локальный core SHA: `a4b1326ff5e7609d13b0cc68070b6ddb4b734a1117b10c8cbe1450ac281f77a6`.
На нём прогнано 655 API tests за 8.8 s: 654 passed и 1 skipped. Также прошли
24 MCP tests за 0.37 s.
Root full-primary reading proof остался 32/32. Final serial и metadata
status-only lookup tweak не меняли target criterion.

2026-10-04 Main подтвердил production API SHA
`a4b1326ff5e7609d13b0cc68070b6ddb4b734a1117b10c8cbe1450ac281f77a6`, health
HTTP 200 / `ok`, API и MCP active, `SOURCE_DISCOVERY=0`. API optimization
развёрнута; candidate feature остаётся held OFF. Rollback backup сохранён.

Public MCP SDK initialize/list/call прошли успешно: 13 tools, `tool_error=false`,
3 результата (`eip:20`, `eip:5700`, `eip:3525`), `partial=false`, без
`source_candidates`, как ожидается при OFF. MCP description code на отдельном
public server не развёрнут, его документ не менялся; он optional и при feature
OFF не требует рестарта.

Первоначальная checksum command завершилась ошибкой из-за CR в последней
pipelined bash line; production SHA позже подтверждён Main. Источник-критерий
29/32 получен ранее, а финальные server timing пары не доказывают ускорение.

**Статус:** API optimization deployed, candidate feature held OFF; deployment
и public MCP подтверждены. MCP description update optional, not deployed.
Не интерпретировать видимость источников как доказательство прироста precision.
