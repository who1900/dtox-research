# Tool quality report: 2026-10-03

## Краткий вывод

Предварительная экспертная оценка: **7/10** (не объективный benchmark). Инструмент уже полезен для извлечения и bounded-поиска, но поиск пропускает часть описательных запросов, а результаты аудита до исправления нельзя считать надёжным подтверждением claims. Демо уместно позиционировать как **free beta + devnet hackathon demo**, не как production-grade систему.

| Область | Оценка / результат | Комментарий |
|---|---:|---|
| Извлечение | 9/10 | Сильное структурированное извлечение; ограничения различаются по источнику и лимиту. |
| Discovery | 7/10 | Хорошо на части запросов с известными именами/ID, слабее на описательных запросах и claims без ID. |
| Audit до исправления | 6/10 | ERC-4337 claim дал `weak_signal_only`, несмотря на уже имевшийся релевантный материал в `known_limitations`. |
| Надёжность ограниченной выборки | 8/10 | Все 10 поисковых запросов завершились без ошибки и без partial-результата. Это не сертификат production-надежности. |

## Discovery: live pre-fix, 10 запросов

| Запрос / ожидаемая цель | Результат |
|---|---|
| ERC-20 → EIP-20 | Найден, rank 1 |
| ERC-4337 → EIP-4337 | Найден, rank 1 |
| EIP-7702 → EIP-7702 | Не попал в top 10 |
| Oracle / Chainlink claim → WP Chainlink v2 | Не попал в top 10 |
| HotStuff → canonical 1803.05069 | OA:W2964159515, rank 3; это тот же paper, засчитан как canonical twin |
| Durable Solana nonce → 2 документа | Найдены, ranks 1 и 2 |
| Reflexion → arXiv 2303.11366 | Найден, rank 1 |
| Generative Agents → arXiv 2304.03442 | Не попал в top 10 |
| Entropy / attention variance across LLM layers → D2O, MEDA, CAKE | D2O не найден; MEDA alias acl:2025.naacl-long.125 найден на rank 7; CAKE не найден |
| Builder / Slither → 1908.09878 | Найден, rank 3 |

**Итог выборки:** hit@3 6/10, hit@10 7/10, MRR@10 0.481; ошибок 0, partial 0. Canonical twins засчитываются как попадание только при подтверждённом совпадении paper. Есть 13 ожидаемых target-paper пар и 8 попаданий, если оценивать пары отдельно; не смешивать эту метрику с query-level hit rate.

Время поиска: 4.484-7.934 с, включая MCP overhead. Отдельная health-выборка из 24 измерений: API p50 1.476 с, p95 10.587 с. Это наблюдения, не стресс-тест и не SLO.

## Extraction: примеры и ограничения

- MEV, arXiv 2101.05511 (2021): 7/7 объектов, 1 алгоритм, 6 уравнений, 2211 символов; `exact_latex=true` означает source-native marker, а не независимую byte-for-byte проверку с upstream, `has_BEVAuctionModel`, 3.507 с.
- D2O: 12/39 уравнений, 2944 символа при лимите 3000; `truncated=true`, 3.469 с.
- ERC-4337 limitations: 1205 символов, `exact_latex=false`; предупреждение о central trust point присутствовало, 3.047 с.
- Strict bundle: 2000 фактических и объявленных символов; `generated=false`, `search_partial=false`, `partial=true`, 6.806 с.
- Два запроса вне области индекса (perovskite, CRISPR) корректно отклонены с `in_scope=false` и `out_of_index_scope`; 7.085 с и 5.931 с.

## Audit до исправления

Идея: `ERC-4337 EntryPoint security design for an execution agent`. Проверяемое утверждение: `ERC-4337 EntryPoint is a central trust point whose implementation concentrates account abstraction security risks`. Глубокий режим, 12.939 с: `weak_signal_only`, best score 0.765, evidence пустой. При этом известное ограничение о central trust point уже было в `known_limitations`; это не новое обнаружение источника. Корневая причина по переданным данным: ветка `_topFalse` пропускает обычную exact injection.

Отдельный отрицательный пример: claim «ERC-4337 EntryPoint prevents every possible DoS» получил verdict `readings_reported_on_similar_claim`. Существующий `does_not_assert` относится к похожему вопросу, в котором читатель использует один кошелёк. Это не тот же claim; новых записей в реестр не добавляли.

## После исправления: live-проверка

`main.py` задеплоен на Contabo, source-only hash `e2d81bb1e1492368c9e9a558a0046abf5fbef8b4d76375dc36ae830f9a6bf6b2`; health `ok`, сервис active. Source backup создан; БД и остальные сервисы без изменений. Полный набор: 540 passed, 1 skipped, запуск агента-кодера Luna. Focused pytest запущен ведущим: 9 passed, 2 subtests, 1.07 с, 3 существующих предупреждения (deprecation и cache permissions).

Повторный полный ERC-4337 audit занял 15.558 с: verdict остался `weak_signal_only`, best score 0.765, evidence пустой. `named_sources` отдельно показывает явно названный источник EIP-4337, score 0.755, band `weak`, `verification_required=true`; `ordinary_claim_retrieval` уже нашёл тот же источник. Источник: [EIP-4337](https://eips.ethereum.org/EIPS/eip-4337); получен фрагмент секции Security Considerations и provenance `fulltext=true`, не новый markdown full text. До исправления relevant known limits уже содержали EIP-4337; после остались `[eip4337, eip7521, eip7887]`. Изменение пережило evidence cut, но claim не подтверждён.

Fast audit: 6.773 с, тот же `weak_signal_only` и named source. До и после исправления known limits: `[eip4337, eip7521, eip7887]`. Это подтверждает inspectability/hydration источника, не правильность claim.

Отрицательный claim «prevents every possible DoS» повторно проверен за 16.131 с: verdict `readings_reported_on_similar_claim`, claim не подтверждён, `verification_required=true`. Существующий `does_not_assert` относится к похожему вопросу, в котором читатель использует один кошелёк. Новых записей в реестр не добавляли.

Три коротких post-fix ordinary search regression: ERC-4337, nonce, Reflexion вернули ожидаемые цели на rank 1; ошибок 0, partial 0; 3.979 с, 2.252 с и 3.433 с. До исправления все три также были rank 1. В этом узком наборе ухудшения не обнаружено; это не оценка всех инструментов и не SLO.

Сравнение полного аудита 12.939 с до и 15.558 с после остаётся единичными замерами, не статистическим доказательством замедления или отсутствия регрессии.

Readonly SQLite counts до и после совпадают: `claim_nodes=54`, `claim_judgments=16`, `claim_links=2`. В этом проходе не создано новых verdicts, links или nodes. API/MCP active, source hash совпадает с `e2d81bb1e1492368c9e9a558a0046abf5fbef8b4d76375dc36ae830f9a6bf6b2`, health `ok`.

Наблюдение корпуса на 2026-10-03 14:03:46 UTC: 180 182 done, web3: 17 253, agents 18 645, llms 148 598, builder 16 398. Категории могут пересекаться, их нельзя суммировать как уникальные записи.

Будущий приоритет: улучшить discovery описательных запросов и claims без известных ID. Цель: не просто «больше источников» или «выше ingestion». Не блокировать hackathon submission ожиданием полного восстановления; durability proof нужен для production claims. Оценка остаётся экспертной: 7/10, не benchmark; deployment не является production certificate.

## Границы

Это экспертный quality review по переданным наблюдениям, не полный аудит безопасности Solana-программы, не новый payment pen test, не mainnet sign-off и не доказательство durability. Не выполнять выводов о восстановлении платежей или mainnet готовности.
