# SPLADE paper-level Stage A: итоги offline-сравнения

**Production decision: HOLD_NO_DEPLOY.** Кодовый minimum screening gate записал PASS, но это не approval: он допускает ухудшение top-10 и MRR. Здесь top-10 и MRR ухудшились.

## Область и метод

- Offline known-source recall, не precision, не answer verdict и не оценка полноты ответа.
- 33 frozen English queries: 21 paraphrase + 12 holdout; canonical paper identity и aliases схлопнуты для подсчёта.
- Fair pilot comparison: один и тот же корпус; top-40 на dense, FTS5, SPLADE; итоговый RRF top-40. Фиксированные до измерения `k=60`, веса dense `2`, FTS5 `0.8`, SPLADE `1`; по результатам не перенастраивались.
- Равный top-40 comparator не является production Stage A. Реальный shortlist: dense 40 + lexical 15; для web3 lexical fetch 120, source reorder whitepapers/specs с лимитами 2+2, затем cap 15.
- FTS5 соответствует title + полный abstract, tokenizer `porter unicode61`, BM25 weights `0,3,1,0,0`; source balance включён.

## Корпус и provenance

- Pilot: 4 000 random-stratified background records, по 1 000 на strata, + 23 forced anchors. Итого 4 023 canonical papers; anchors присутствуют, missing vectors `0`, canonical duplicates в export `0`; twins/aliases учитывались через identity, не как дополнительные голоса.
- Это намеренно не population sample: strata populations builder-tech 11 470, agents 18 388, LLM 133 076, web3 17 253. Равная квота перепредставляет web3; 23 anchors также целевые. Нельзя переносить pilot recall на full-index population.
- Источник sample описан как 180 187 `done` + anchors; full observer видел 180 186 coarse points. Это разные счётчики/snapshot; расхождение не трактуется как потеря документа.
- У 57 записей abstract отсутствовал или был короче 80 символов; это сигнал риска неполного текста, не проверка его полноты.
- Эти 57 short-abstract/background records отмечены как title-only approximation production input; ни один forced anchor не затронут.
- Export manifest: 4 023 records, 4 000 background, 23 anchors, 0 missing vectors. Проверенные fixture/twins/query hashes совпадают с observer/export provenance.

## Результаты pilot: все 33 фразы

| Метод | Hit@10 | Hit@40 | MRR@40 | Comparator alerts |
|---|---:|---:|---:|---:|
| Dense | 27/33 | 29/33 | 0.5473 | 5 |
| FTS5 | 20/33 | 27/33 | 0.4430 | 5 |
| SPLADE | 23/33 | 28/33 | 0.4101 | 2 |
| RRF dense + FTS5 baseline | 28/33 | 29/33 | 0.5637 | 5 |
| RRF dense + SPLADE | 27/33 | 29/33 | 0.5675 | 4 |
| RRF dense + FTS5 + SPLADE | 26/33 | 30/33 | 0.5490 | 2 |

Для baseline vs all-channel treatment paired target rank: SPLADE лучше в 5, хуже в 9, одинаково в 19 случаях. Итого +1 hit@40, но -2 hit@10 и MRR ниже на 0.0147. Меньше comparator alerts не компенсируют эти recall/rank регрессии.

Устойчивость по группам: все три paraphrase hit@10 были у 8/11 групп на baseline и у 7/11 на all-channel treatment.

Candidate union отдельно от fair final-40: dense+FTS5 recall 31/33, с SPLADE 32/33; средний union вырос с 67.06 до 83.21 candidates. Это дополнительная candidate coverage с дополнительной стоимостью, не доказательство улучшения final ranking.

### Holdout, 12 фраз

| Baseline dense+FTS5 -> all-channel | Baseline | With SPLADE |
|---|---:|---:|
| Hit@10 | 8/12 | 8/12 |
| Hit@40 | 9/12 | 10/12 |
| MRR@40 | 0.35694 | 0.29692 |
| Comparator alerts | 2 | 0 |

Holdout top-10 без прироста, hit@40 +1, MRR ниже. Поэтому локальный screening PASS не означает устойчивого улучшения ранжирования.

## Screening gate и production решение

- Minimum screen из summary: PASS. Overall alerts 5 -> 2 и hit@40 29 -> 30; holdout alerts 2 -> 0 и hit@40 9 -> 10. Это правило проверяет отсутствие роста comparator alerts плюс хотя бы один hit@10/@40 gain.
- Screen не блокирует top-10/MRR regression: overall hit@10 28 -> 26, MRR@40 0.5637 -> 0.5490; paired rank 5 wins / 9 losses / 19 ties. Следовательно итоговое решение отдельно: **HOLD_NO_DEPLOY**, не production approval.
- Другие причины HOLD: SPLADE есть только в корпусе 4 023, а не 180 186; full sparse Qdrant cost и Oracle latency не измерялись.
- Практический следующий этап, если будет отдельно разрешён: shadow-only candidate addition, сохраняющий текущие candidates и rank budget; сначала full index и server-side latency/resource benchmark. Fixed-weight all-channel RRF с весами 2/0.8/1 не рекомендован к rollout.

## Full-index observer и границы выводов

- `full-stage-a-2s.json`: 33/33 observer queries, 0 dense errors, 0 FTS errors; production-matching timers: FTS 2s, Qdrant request 5s, global capture 90s.
- Реальный Stage A baseline нашёл target в 17/33: 14/21 основной fixture и 3/12 holdout. Это coarse + SQL lexical Stage A, shortlist максимум 40+15.
- Это не service final recall и не recall всего инструмента: дальнейшие global chunk и graph channels могут восстановить targets. Здесь нет judgments, production latency claim, precision или source/format verdict.
- Предыдущий `full-stage-a.json` был exploratory: три FTS errors при 1s timer. Для matching comparison использован `full-stage-a-2s.json`, не первый файл.
- Restricted SPLADE pilot нашёл 11 из 16 target misses Stage A. Это диагностический overlap на выборке с forced anchors и stratified background, не измеренная full-index SPLADE recovery и не основание заявлять SPLADE 28/33 на полном индексе.

## Encoding и latency

- Локальный GPU encode завершил 4 023/4 023 за 420.4475s без model-load времени: 9.568 docs/s, GTX 1650, наблюдалось 711 MiB, backend torch CUDA FP16, max length 512.
- Получено 672 311 sparse nonzeros, в среднем 167.12/document. Линейная оценка для 180 186: около 240.9 MB raw sparse pairs до overhead; full-index encoding не выполнялся. Оценка времени полного encode около 5.23 GPU hours, не запускать без отдельного разрешения.
- Ранее измеренный CPU query encoder: p50 111.6ms, p95 140.3ms, local Windows / ORT 2 threads, те же закэшированные 33 queries. Это не Oracle ARM latency.
- Scoring SPLADE postings на pilot: 0.3395s суммарно на 33 запроса, около 10.3ms/query; не включает full Qdrant indexing/search и не экстраполируется на 180k.
- Платные model API не вызывались. Production files/services не менялись.

## Postcheck

- API ok; оба Qdrant green, optimizer ok, `update_queue=0`.
- Coarse points: 180 186; full records: 10 744 782.
- Удалён только собственный server temp. Load-test не проводился; это не общий production-readiness claim.

## Артефакты и проверки

- Pilot comparison: `.scratch_dtox/splade-stage-a-20261003/comparison.json`, SHA-256 `83e1875ba7ab4fea556767c0d94fddf04d0089ade3beccca503cc05014df1b3e`.
- Full observer: `.scratch_dtox/splade-stage-a-20261003/full-stage-a-2s.json`, SHA-256 `34d11c84ca8bc448d209eedd8af225dc590497ee0098652b798c7457a5621c92`.
- Manifest: `.scratch_dtox/splade-stage-a-20261003/manifest.json`, SHA-256 `dbc4d9755d4d1e2eb49909f52686d7d13c789bec05b47a63729e474247f25784`.
- Fixture SHA-256: original `852f374aeb3bdfc0160bce9c46706bbac17ec49dc84b667faa24b51dbd60385d`; holdout `9f30c246aa4202c32a0a5a58a6bf37048d9e89029092f1c6b3fd0829c98ff716`.
- Финальная QA проверка: 35 passed, 3 subtests, 1.45s.
