# Статус регрессии, 2026-10-06

Проверки main выполнены 2026-10-06 около 15:08–15:10 UTC. Итог: работоспособность проверенных сценариев подтверждена, наблюдаемой регрессии в них нет. Глобальное отсутствие регрессий и готовность production не подтверждены.

## Свежие проверки

- `pipeline.tests.test_publication`, `test_publication_failures`, `test_parse_safety`, `test_pipeline_audit_fixes`, `test_fts_catalog`, `test_build_fts_safety`, `test_whitepaper_staging`: 113 cases за 14.197 с, 112 PASS и один Windows FIFO skip.
- API publication visibility, canonical section read, grouped search, hierarchy rank channels и ops source staging/cached reprocess safety: 55 PASS за 4.368 с. Некорректный Qdrant в negative-test fixture ожидаем, не live failure.
- `unittest discover -s mcp/tests -q`: 36 PASS за 0.397 с.
- Свежий `python -m py_compile` для 9 модулей завершился с exit 0; это не тесты и к числу cases не прибавляется.
- Итого в трёх непересекающихся свежих наборах: 203 PASS и 1 SKIP из 204 cases. Предыдущие прогоны не суммировались. `git diff --check` завершился с exit 0; были только предупреждения CRLF.

## Ограниченные live-проверки

- `ops/release_readonly_probe.py`: 6/6 HTTP requests за 7.021 с, inventory содержит 13 exact tools; durable nonce для `find_papers` PASS; spec `2101.05511`: PASS. Ожидаемый missing-id 404 корректно отмечен как error. partial=false, truncated=false; `tool_error_count=1` соответствует ожидаемому 404, прочих ошибок 0. Это не runtime-проверка всех 13 tools.
- Live semantic query: EIP-4337 account abstraction bundler simulation validation, web3 limit 3, 8.287 с с учётом сети/connector. Получены `eip:4337`, `eip:7562`, `ghOpenZeppelin`; источники, лицензии и ограничения учтены.
- `read_paper_section` для `eip:4337`, Simulation Specification: 1510 chars за 3.329 с; `section_order.verified=false` оставлен как есть.
- D2O `2406.13035`, equations, `max_chars=2500`: 10 из 39 доступных, 2282 chars за 2.754 с, `truncated=true` по запрошенному бюджету, не сбой; `partial=false`, `time_budget_exhausted=false`, source LaTeX exact=true. С 7 фрагментами предыдущего прогона не сравнивать: параметры другие.
- По read-only SSH активны `dtox-research`, `dtox-research-api`, `dtox-mcp`; у каждого `NRestarts=0`. `/v1/health` OK. Неверный первый путь `/health` дал 404 и не означает поломку API. Health latency основана лишь на двух samples: p50 0.452, p95 2.426, это не SLO.
- Optional reranker: `enabled=false`, `closed`; выводов о наличии или выключении cross-encoder не делать. Qdrant `papers_fulltext`: green, optimizer OK, 10911096 points, dim 384, queue 0. Points не равны числу статей; Qdrant queue не является ingest queue.
- Live hashes: pipeline `cc69ce0b6deb9b25b21a0f99e8c3a0ce5c20124b16c6fcbce897a5d1cc7d6028`, API `bff7089541a9c60291bf4e80f5d7c54dfbce09fd9725c53d5405172df6a9643e`; это прежние deployed версии. Новый publication owner/visibility gate существует локально, не deployed.
- Legacy reprocess timer неактивен/disabled; previous corpus recovery unit неактивен. В последних ограниченных API logs видны HTTP 200 и ожидаемые 404, серверных ошибок не видно. Это не 24-часовая проверка.

## Границы и остаточные вопросы

Код продукта, сервер, платежи, секреты, DB writes, deploy, commit и push в этом проходе не менялись. Локально покрыты инварианты publication journal/ACK, Qdrant FTS, done visibility, idempotency и staging preserve legacy; они не проверены на production Linux с новым publisher. См. [локальные правки publication owner](2026-10-06-publication-owner.md).

Историческая FTS replacement/migration exact UUID остаётся blocked. Не сертифицированы full corpus integrity, paraphrase recall, runtime всех 13 tools, performance p95, payments/Solana и mainnet.

В наблюдаемом ответе `GHOpenZeppelin` имеет `acquisition_type=latex`, хотя `extraction_type=markdown_source`; `parse_completeness=unknown`. Это отмечаем как наблюдаемую несогласованность provenance-классификации, не как доказанную новую регрессию.
