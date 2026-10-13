# Диагностика потерь поиска: corrected trace-probe

Дата: 2026-10-04  
Статус: диагностический отчёт, без изменений production.

## Краткий вывод

Исправленный прогон не обнаружил изменения известных целевых попаданий от добавления SPLADE-кандидатов. Среди 32 полных пар все 32 остались ничьими по target hit/rank; на 6 запросах изменился состав top-10 за счёт вытеснения нецелевых ID. Один treatment неполон по отдельной ошибке lexical retrieval, поэтому его нельзя использовать для вывода о причине потерь.

Проверенные случаи указывают на узкие места в слоях отбора и ранжирования после обнаружения бумаги. Правило жёсткого floor отсекает некоторые содержательно подходящие бумаги, хотя они найдены, а графовый поиск поднимал отдельные бумаги на 3 или 4 место. Это ранг поиска, а не доказательство корректности. Отдельно ограниченный candidate pool может не включать нужную бумагу. Это локальная диагностика на небольшой anchor-смещённой выборке, а не оценка качества поиска на населении запросов.

## Идентичность и условия прогона

- Trace-probe SHA: `48f7f6b0ecb09c3a627c00e86550ff0211fb6edb89fcae3b47d324c33c879644`.
- Использованы cached small-384 и base-768 embeddings; порядок пар AB/BA; 33 пары; elapsed 93.333 секунды.
- `HIER_CHUNK_LEXICAL=0` задан явно; фактическое значение `False`.
- Веса `2 / 0.8 / 1`, как в production main SHA `bd38ed430a1ced3167ff4eef7c075e4bd5d662c87c4dfd62d3589c74a8387d19`.
- Main проверен read-only, exact per-paper lookup, 5 запросов.
- Raw trace: `C:/Users/User/Documents/work/Claude/.scratch_dtox/splade-candidates-20261004/trace-probe.json`.
- Агрегаты: `eval/results/2026-10-04-splade-candidate-corrected-summary.json`.
- Raw trace SHA-256 проверен: `48f7f6b0ecb09c3a627c00e86550ff0211fb6edb89fcae3b47d324c33c879644`. Trace доступен вне репозитория; агрегаты сверены с parsed summary, а run config и pair status прочитаны из raw JSON.

## Результаты 33 пар

| Метрика | Baseline | Treatment |
|---|---:|---:|
| Безусловный hit@10 | 16/33 | 16/33 |
| Безусловный MRR sum / 33 | 12.7 / 33 | 12.7 / 33 |
| Безусловный MRR@10 | 0.38484848 | 0.38484848 |
| Полные пары | 32 | 32 |
| Hit@10 на полных парах | 16/32 | 16/32 |
| MRR@10 на полных парах | 0.396875 | 0.396875 |

- На 32 полных парах: 0 wins, 0 losses, 32 ties.
- Разбивка без изменения: исходные 14/21, holdout 2/12.
- В 6 запросах вытеснены baseline top-10 ID; все вытесненные ID не были целевыми.
- По всем 33 наблюдениям итоговые hit и MRR равны. Однако один treatment неполон, поэтому строгий парный знаменатель для quality comparison равен 32.

### Полный путь целей по 32 treatment

Отдельный ручной просмотр main проследил все 32 полных запроса, а не только добавленные SPLADE-кандидаты. Для всех 32 найден принятый источник на pre-filter hierarchical этапе с global/graph. После floor и dedupe целевой документ оставался для 19 запросов; в final top-10 остались цели для 16.

```text
32 цели найдены до pre-filter
  -> 19 целей после floor и dedupe
  -> 16 целей в final top-10

Потери: 13 ниже non-exempt floor, 3 прошли floor, но остались за final top-10
```

13 floor losses охватывают spec guess-and-check, reflexion EPI и linguistic-feedback, MoE balanced и regularized, три GRPO, две LoRA и три self-consistency цели. Это наблюдение системного поведения слоя floor на данном cohort, не проблема отдельных per-paper правил.

Три цели, прошедшие floor, но оставшиеся вне final top-10: delegated balances ERC-20 на ранге 18; HotStuff three-phase на ранге 30; responsive HotStuff primary arXiv на ранге 44. Последняя дедуплицировалась в принятый alias `oa:W2964159515` на ранге 22. Значит, этот responsive HotStuff случай не является доказательством раннего candidate-cap omission.

Первоначальный подсчёт 18 был ошибкой сравнения: raw OpenAlex ID был в верхнем регистре, а fixture использовал нормализованный alias. После проверки retained alias значение исправлено на 19. Независимый analyzer подтвердил нормализацию alias и поток 32 → 19 → 16. [JSON-результат](../eval/results/2026-10-04-search-loss-diagnosis.json).

## Неполный treatment

В запросе `capped-delegation` (`eip:20`) treatment помечен `lexical retrieval incomplete`. Прогон содержит частичный результат: SPLADE append к этому запросу не был обработан. Это отдельная неполнота выполнения, а не установленная причина остальных случаев. Не приписывать отсутствие выигрыша здесь floor, fusion или SPLADE-кандидату.

## Трассировка диагностических случаев

| Запрос / целевая бумага | Наблюдение в trace | Вывод |
|---|---|---|
| guess-and-check, `2211.17192` | Stage B score 0.75954056; hierarchical rank 4; floor 0.79; бумага удалена | Floor выше score и отсекает найденную цель. |
| guess-and-check, `2302.01318` | Stage B score 0.7692795; hierarchical rank 3; floor 0.79; бумага удалена | Rank 3 не преодолевает hard floor; сам по себе этот ранг не доказывает корректность результата. |
| frozen LoRA, `2106.09685` | Stage B score 0.7518654; hierarchical rank 10; floor 0.79; бумага удалена | Найденная цель не проходит floor. |
| HotStuff, `1803.05069` | Stage B score 0.8126259; hierarchical rank 30 и dedupe rank 30; floor 0.7; отсутствует в final top-10 | Потеря происходит после этих рангов; не сводить случай к одному score floor. |

### Отдельная проверка candidate pool

Для `parallel-verification` Stage B было 120 hits, 57 candidate papers и 38 уникальных бумаг; `2211.17192` отсутствовал. Его exact per-paper score 0.71948. Бумага уже индексирована. Score ниже последнего score пула 0.7823906 и ниже floor 0.79. Это независимый путь потери: ограниченный pool может не открыть кандидата для последующих слоёв.

Baseline запроса `parallel-verification` уже находил другой допустимый источник, `2302.01318`. Следовательно, пример pool omission для `parallel-verification` не является новым query-level miss baseline и не доказывает отсутствие любого релевантного результата.

### Контекст ранжирования и точных запросов

- Read-only counts: `eip:20` 19 chunks; проверенные spec papers по 53 chunks каждый; LoRA 94; HotStuff 118.
- Exact within-paper scores для guess-and-check: `2211.17192` 0.7638313, `2302.01318` 0.77261215.
- Exact score frozen LoRA: 0.74902874, по-прежнему ниже floor 0.79.
- Exact score HotStuff: 0.81398416, выше floor.
- Exact per-paper результаты использованы только для диагностики после прогона, не для выбора кандидатов.
- Для HotStuff paper score treatment был назначен искусственно как минимум исходного shortlist: 0.6644. У него dense rank 26, нет lexical/global rank, fusion score 0.023256, hierarchical rank 30 и dedupe rank 30; в final top-10 он не попал. Это объясняет слабую позицию в данном пути, но не доказывает, что floor был единственной причиной.

## Latency: только описательные числа

На 32 полных парах с cached embeddings: p50 baseline 1218.3 ms, treatment 1245.52 ms; p95 2194.595 ms и 1811.9505 ms соответственно; paired mean delta treatment минус baseline −30.131875 ms. SPLADE query encoding исключён. Это не скорость SPLADE и не production gain: одиночный замер не является end-to-end сравнением и не подтверждает ускорение.

## Интерпретация и границы

Данные поддерживают гипотезу, что hard floor в последующих слоях способен отсечь найденные валидные paraphrase-цели. Графовый rank 3 или 4 означает позицию в поиске, а не подтверждение корректности. Также candidate pool Stage B не гарантирует включение каждой уже индексированной бумаги. Эти механизмы различны и требуют измерения на уровне источника и чанка.

Выборка мала, содержит paper anchors и не представляет случайную популяцию запросов. Live index не был заморожен. Есть один неполный treatment. Поэтому нельзя выводить общую частоту ошибок, production-качество или причинность для всех запросов. Исправленный trace supersedes прежнее описание этого corrected run как ещё ожидающего; [предыдущий SPLADE-отчёт](2026-10-04-splade-candidate-channel.md) остаётся архивом первичного пилота и его оговорки, а не заменой этого результата.

## Следующий эксперимент и ограничения действий

Следующий глобальный эксперимент: раздельно измерить source-level discovery и chunk-level qualification, с bounded per-paper chunks и калиброванным ranking, aware of provenance. Сначала определить, теряется ли источник при candidate generation или уже найденный источник не проходит chunk/floor/ranking path.

- Не снижать глобально floor на основании этих четырёх случаев.
- Не добавлять отдельные per-paper правила.
- Не объявлять SPLADE бесполезным по anchor-смещённому пилоту на 4k корпусе.
- Этот отчёт не разрешает production edits, deployment, новые модели, paid API или full reindex.

## Post-check

- Подтверждены API status OK, оба Qdrant green, optimizer OK. Cached health использовал 4 samples и не является latency test.
- Индексные счётчики coarse 180187 и full 10744870 совпали с pre-check. Production main SHA не изменился: `bd38ed430a1ced3167ff4eef7c075e4bd5d662c87c4dfd62d3589c74a8387d19`.
- Временный remote trace удалён после копирования raw и сверки hash. Runtime trace wrapper SHA: `fe5220b1ca0c3dd1785ca8d29ec61829ebebd30c16b2d5e0114a045c3032a946`.
- Local wrapper SHA `4c8a0...` отличается из-за тестового рефакторинга `_set_env` из nested function в exposed helper. Byte-identical wrapper/runtime не заявляется. Флаг `HIER_CHUNK_LEXICAL` со значением false подтверждён в каждом trace.
- Production не менялся. Эти проверки не доказывают production readiness и не устанавливают отсутствие регрессий вне данного набора из 33 пар.
- QA: 30 тестов диагностических helpers пройдены за 0.30 секунды. Это не полный production suite. Отчёт диагностирует потери и не предписывает production fix.
