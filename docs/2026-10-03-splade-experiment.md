# SPLADE: локальный offline-пилот, 2026-10-03

## Результат

| Метод | Hit@10 / 33 | MRR@50 | Comparator alerts |
|---|---:|---:|---:|
| Dense BGE-small | 32/33 | 0.7675 | 6 |
| SPLADE | 32/33 | 0.8378 | 5 |
| BM25 whitespace | 29/33 | 0.7651 | 6 |
| Dense + BM25, RRF | 33/33 | 0.8409 | 5 |
| Dense + SPLADE, RRF | 33/33 | 0.8470 | 6 |
| Dense + BM25 + SPLADE, RRF | 33/33 | 0.8712 | 6 |

Добавление SPLADE к Dense + BM25 не изменило Hit@10: обе конфигурации нашли target для 33 из 33 фраз. MRR@50 вырос с 0.8409 до 0.8712, comparator alerts выросли с 5 до 6. Парное сравнение рангов: 6 фраз улучшились, 3 ухудшились, 24 без изменения.

### Holdout, отдельно

| Метод | Hit@10 / 12 | MRR@50 | Comparator alerts |
|---|---:|---:|---:|
| Dense BGE-small | 12/12 | 0.8403 | 0 |
| SPLADE | 11/12 | 0.8177 | 1 |
| BM25 whitespace | 10/12 | 0.5925 | 4 |
| Dense + BM25, RRF | 12/12 | 0.8056 | 1 |
| Dense + SPLADE, RRF | 12/12 | 0.8542 | 0 |
| Dense + BM25 + SPLADE, RRF | 12/12 | 0.8819 | 1 |

## Как читать цифры

Это известный-source retrieval test, не оценка precision и не production-сравнение. `Hit@10` означает, что хотя бы один допустимый target оказался в top 10 статей; это не recall по всем релевантным источникам. В двух multi-target группах достаточно любого из двух target. MRR обрезан на top 50. Всего 33 запроса: 21 в 7 группах и 12 holdout-запросов в 4 группах, по 3 варианта на каждую из 11 групп. Покрыты 13 из 13 target ID; в corpus выбраны все 26 anchor ID.

Hard negatives являются диагностическими компараторами. Alert учитывается, только когда negative в top 10 и опережает target либо target не найден в top 50. Это не штраф к Hit@10 и не нормированная доля ошибок. Примеры сдвигов при добавлении SPLADE к Dense + BM25:

- Oracle: Chainlink поднялся с 2-го места на 1-е; alert на comparator 1509.08443 исчез.
- ERC20: target опустился с 1-го на 2-е место в двух запросах, уступив ERC721, который стал comparator alert на 1-м месте.
- Self-consistency, запрос `sample-and-vote`: target опустился с 1-го на 4-е место.

BM25 здесь whitespace-baseline, не production FTS5; обнаруженный artifact нулевого score устранён до финального прогона. Dense - точный cosine по локальным векторам BGE-small 384D, не production hierarchical/coarse retrieval и не ANN с квантованием. Слои не фильтровались. Фикстурные anchors специально добавлены в корпус; остальные записи не являются репрезентативной выборкой.

Дедупликация до RRF идёт по source `paper_id`, не по полному canonical identity: arXiv/PMLR/ACL/OpenAlex aliases одной работы могут остаться отдельными source IDs в ранжировании. Алиасы учитываются при проверке target hit, но это не полная canonical-paper дедупликация. Ограничение одинаково для всех методов; fixture-driven aliases не расширялись.

## Данные и воспроизводимость

- Corpus: 1 357 чанков, 878 статей: 498 anchor и 859 background. Background взят из bounded scroll и не стратифицирован; порядок scroll может смещать выборку к ранним ID. Не трактовать метрики как популяционную оценку.
- SHA256 corpus: `e0a509e1db258e0a0149fc129046497eb315bb91178520a92514abfb42c6eeec`. SPLADE: 221 013 ненулевых весов, в среднем 162.9 на чанк. Грубая проекция на 10 744 782 чанка: около 14 GB decimal сырых sparse-данных до overhead. Это не прогноз размера индекса и не доказательство достаточности RAM.
- Документы закодированы на локальном CUDA GPU, Torch FP16, SPLADE revision `762be6a7206e2f299182705972a65e5c46e62be2`; query vectors получены CPU ONNX Runtime, revision `600ce23fd7611e71694fcf724f46778f1ad24c1c`. На 5 документах и 33 запросах минимальный cosine parity: 0.999999135 и 0.999998816 соответственно; tokenizer совпал на всех 38 входах. Evaluation использовал CPU query vectors и дал те же итоги, что GPU-query summary.
- Сырые top-10 списки CPU/GPU совпали для всех 33 запросов и всех 6 методов. QA: 14 unit tests passed, включая regression для unknown query. [Sanitized public summary](../eval/results/2026-10-03-splade-summary.json) содержит метаданные без полного текста.
- GPU encode: 1 264 за 181.0289942 s плюс 93 за 13.9381296 s, всего 1 357 за 194.9671238 s, около 6.96 документов/с. Включено checkpoint I/O, исключена холодная загрузка модели. Наблюдаемое использование всего GPU было 765 MiB; 342 650 880 allocated bytes в parity относятся только к памяти, учтённой PyTorch, а не ко всему GPU.
- Тёплые query latency, 33 запроса с tokenizer, без warmup: локальная Windows CPU, 2 потока, p50 111.57 ms, p95 140.34 ms, max 144.47 ms; GPU p50 37.03 ms, p95 44.90 ms. Это не latency Oracle ARM-сервера и не end-to-end замер.

Локальный GPU подтвердил техническую выполнимость без оплаченного API и без изменений production. Контрольные снимки до/после показывали API OK, Qdrant green, нулевую очередь и одинаковые 10 744 782 points; четыре latency sample не являются нагрузочным тестом.

## Решение

Не деплоить и не переиндексировать по итогам этого пилота. Следующий шаг - отдельное ограниченное сравнение на реальной стадии candidate papers/coarse metadata, с замороженными параметрами и representative background. Длинную индексацию запускать только после отдельного согласования.
