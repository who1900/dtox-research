# Проверка перефразировок, 2026-10-03

Основной результат из raw metrics [serial JSON](../eval/results/2026-10-03-paraphrase-serial.json): все 21 ответа валидны, transport errors и partial отсутствуют; hit@3 = 12/21, hit@10 = 13/21, MRR@10 = 0,446031746. Известный источник найден во всех трёх формулировках у 2/7 групп (oracle и MEDA). Пересчёт для получения 13/21 не требуется: scorer уже сопоставляет идентичность через twins. Общий quality gate не пройден: остаются пропуски известных targets и один comparator-флаг.

[Fixture](../eval/paraphrase_cases_20261003.json): 7 групп по 3 английские формулировки одного технического вопроса, без названий работ и идентификаторов в запросах. Запросы и ожидаемые источники выбраны до первоначального live; после него исправлена только идентичность одного источника и добавлено пояснение в scope_notes. По данным основного тестового прогона: выполнено 574 теста, 573 passed, 1 skipped; все 14 новых тестов прошли и входят в этот итог.

## Условия и полнота

Фактические строки parallel JSON: `started_utc = 2026-10-03T15:50:34.519995+00:00`, `ended_utc = 2026-10-03T15:51:29.881890+00:00`. Длительность 55,36 с, около 55 с.

Первоначальный parallel: публичный MCP `https://read.whoim.space/mcp`, `strict=false`, limit 10, workers 2, timeout 22 с, budget 180 с. Transport errors: 0. Все 13 partial имеют `lexical_channel=skipped`; у 12 warning `lexical retrieval incomplete`, у одного Reflexion warning `soft partner retrieval incomplete`. Это неполнота retrieval, а не ошибка транспорта. Invalid ответы исключены из hit@10 и не получают семантический pass/miss.

Основной serial: тот же публичный MCP, `strict=false`, limit 10, workers 1, timeout 22 с, budget 120 с. Фактические строки serial JSON: `started_utc = 2026-10-03T15:56:57.582114+00:00`, `ended_utc = 2026-10-03T15:57:28.709028+00:00`; длительность 31,13 с. Все 21 ответа имеют `lexical_channel=paper`, без partial и ошибок.

## Основные serial-ранги

A/B/C соответствуют порядку неизменённых фраз в fixture. Ранги сверены с raw rows и scorer. `-` означает отсутствие известного target в возвращённых строках, а не отсутствие подходящей работы вообще. Для oracle и speculative достаточно любого указанного target семейства.

| Группа | A | B | C |
| --- | --- | --- | --- |
| Fungible token interface | 2 | - | - |
| External oracle | 1 (v1) | 1 (v2) | 1 (v2) |
| Exact IO-aware attention | 2 | 1 | - |
| Speculative decoding | 3 (`2302.01318`) | - | 1 (`2211.17192`, PMLR twin) |
| Verbal reflection | 2 | - | - |
| Multimodal entropy KV allocation | 1 | 1 | 5 |
| Auxiliary MoE balancing loss | 3 | - | - |

Итог serial: hit@10 = 13/21, all-three-valid-hit = 2/7. Пропуски в полных ответах относятся к известным anchors; эти метрики не доказывают нерелевантность остальных найденных работ.

## Вторичные parallel-наблюдения

По [исходному parallel JSON](../eval/results/2026-10-03-paraphrase-live.json): 8 полных ответов, hit@10 = 4/8, all-three-valid-hit = 1/7 (MEDA). 13 invalid не считаются семантическими miss или pass. Этот итог по неполному прогону несопоставим напрямую с serial 13/21 и не является before/after доказательством улучшения retrieval.

Числа ниже взяты из raw rows, включая partial. `P` означает partial и исключение из scoring.

| Группа | A | B | C |
| --- | --- | --- | --- |
| Fungible token interface | 1P | нет P | нет P |
| External oracle | 2P (v1) | 9P (v1) | 1P (v2) |
| Exact IO-aware attention | 1P | 1P | 1P |
| Speculative decoding | 3P (`2302.01318`) | нет P | 3P (PMLR, аннотация идентичности) |
| Verbal reflection | 9P | нет | нет |
| Multimodal entropy KV allocation | 1 | 1 | 5 |
| Auxiliary MoE balancing loss | 3 | нет | нет |

MEDA возвращён как `2502.17599` в A и как заранее записанный alias `acl:2025.naacl-long.125` в B/C; raw twins также содержат `2502.17599`. Механизм распределяет кэш по transformer layers согласно cross-modal attention entropy, без условия о распределении по головам. Первичный источник: [MEDA в ACL Anthology](https://aclanthology.org/2025.naacl-long.125/).

В parallel speculative C raw rank 3 занимает `pmlr:v202/leviathan23a`, а twins уже содержат `2211.17192`. Первичный обзор подтвердил совпадение названия Fast Inference from Transformers via Speculative Decoding и авторов Yaniv Leviathan, Matan Kalman, Yossi Matias с [официальной страницей PMLR](https://proceedings.mlr.press/v202/leviathan23a.html). После первоначального live этот alias добавлен в текущий fixture для будущих запусков. Запросы, canonical targets и механизмы сохранены; это коррекция идентичности источника.

Оба исходных JSON сохраняют оригинальный embedded fixture и hash `a6917880a3958352289ce037cd67cc87262ca4be61b5630c087c1a046542bb54`: serial также захватил snapshot без нового alias. Файлы прогонов сохранены. В parallel scoring speculative C имеет `valid=false`, `first_target_rank=null`: причиной исключения является partial. Наблюдаемый 3P после подтверждения идентичности приведён как аннотация, без изменения исходных 4/8 или 1/7; raw twin уже позволял сопоставить работу.

Raw serial scoring уже даёт 21 valid, hit@3 12/21, hit@10 13/21, all-three-valid-hit 2/7 и один comparator-флаг. Отдельная offline-проверка в памяти с текущей fixture подтвердила нулевую delta: speculative C уже возвращает canonical `2211.17192` на rank 1 и PMLR twin. Заявление об исходных 12/21 и исправлении до 13/21 неверно. Явный alias полезен для будущих benchmarks, но здесь не добавил попадания. Новые golden или results-файлы не создавались.

## Первичные источники и comparator-флаг

Метаданные target/comparator взяты из fixture и первичного обзора до live; ссылки ниже фиксируют механизм выбора. Список известных targets не исчерпывает научно допустимые ответы.

| Механизм | Target | Близкий comparator |
| --- | --- | --- |
| Fungible interface | [EIP-20](https://eips.ethereum.org/EIPS/eip-20) | [EIP-721](https://eips.ethereum.org/EIPS/eip-721): уникальное владение NFT; EIP-1155 поддерживает fungibles и не выбран negative |
| External oracle | Chainlink v1/v2; [v1 PDF как первичный источник семейства](https://research.chain.link/whitepaper-v1.pdf) | [Weaver](https://arxiv.org/abs/1509.08443): timeline ordering |
| Exact tiled attention | [FlashAttention](https://arxiv.org/abs/2205.14135) | [PagedAttention](https://arxiv.org/abs/2309.06180): paging KV cache |
| Draft verification | [Speculative decoding](https://arxiv.org/abs/2211.17192), [speculative sampling](https://arxiv.org/abs/2302.01318) | [FlashAttention](https://arxiv.org/abs/2205.14135): attention kernel |
| Trial feedback memory | [Reflexion](https://arxiv.org/abs/2303.11366) | [ReAct](https://arxiv.org/abs/2210.03629): чередование reasoning/actions |
| Cross-modal entropy allocation | [MEDA](https://arxiv.org/abs/2502.17599) | [MiniCache](https://arxiv.org/abs/2405.14366): слияние по глубине |
| Auxiliary balancing loss | [Switch Transformers](https://arxiv.org/abs/2101.03961) | [Auxiliary-Loss-Free Load Balancing](https://arxiv.org/abs/2408.15664): expert bias |

Один comparator-флаг в обоих прогонах: в MoE A `2408.15664` занимает rank 1 выше Switch `2101.03961` на rank 3. Это сравнение фокусного механизма. Loss-free работа обсуждает auxiliary-loss baselines, поэтому флаг требует проверки конкретного passage и не доказывает нерелевантность всей работы или научную ошибку. Попадание Switch учитывается независимо от этого флага.

## Воспроизведение

Команды выполнять из корня репозитория. Повторный live с workers 1 использует текущую fixture с коррекцией alias; эти команды здесь не запускались.

```powershell
python eval/paraphrase_eval.py --cases eval/paraphrase_cases_20261003.json --transport mcp --endpoint https://read.whoim.space/mcp --workers 1 --timeout 22 --max-queries 21 --budget-seconds 180 --output eval/results/paraphrase-rerun.json
```

Offline scoring использует embedded оригинальную fixture исходного JSON и пишет отдельный файл. Exit 1 ожидаем из-за quality gate, а не из-за transport error.

```powershell
python eval/paraphrase_eval.py --results eval/results/2026-10-03-paraphrase-live.json --output eval/results/paraphrase-offline-rescore.json
```

## Ограничения и after-проверки

Known-source hit не измеряет precision или полноту всех правильных ответов. Все запросы английские; pure-vector isolation не проводилась.

По данным основного исполнителя, предварительная serial-диагностика трёх неизменённых запросов первой группы дала ранги ERC-20 `[2, нет, нет]`, latency `[2953.72, 2029.59, 1633.3]` мс, `partial=false`, `lexical_channel=paper`. Полный повтор без параллельности также завершился без partial: отделили операционную неполноту первоначального прогона от сохраняющихся пропусков известных targets. Одиночное сравнение с другим порядком и возможным прогревом не доказывает причинного влияния concurrency.

По подтверждённой after-проверке основного исполнителя API и MCP active; оба source hashes совпали с before: API `b89696b05a4da0b574c1afc1b907e976fb4b035a643760807c14c3a41fcdcf88`, exact `d43cfac8c0c771df1378fc96f0f2572b3ff8b1cae29b79416798c2867056a353`. Production code не менялся, source hash delta отсутствует.

По сверке основного исполнителя registry before/after: 54 nodes / 16 judgments / 2 links, без изменения счётчиков. Judgment writes в этой работе не выполнялись.

Исправление production или устойчивость качества по этим прогонам не заявляются.
