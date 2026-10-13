# Оценка recall, 2026-10-03

## Итоговый срез

Финальный cut одобрен и развёрнут; API/MCP активны, deployed source hash
совпадает с проверенным final hash. Полные запросы, targets,
ранги, длительности, ошибки и partial сохранены в
[`api/tests/recall_cases_20261003.json`](../api/tests/recall_cases_20261003.json).

| Набор | hit@3 до → после | hit@10 до → после | MRR@10 до → после |
|---|---:|---:|---:|
| Discovery, 10 | 6/10 → 7/10 | 7/10 → 9/10 | 0.481 → 0.481 |
| Held-out, 8 | 4/8 → 4/8 | 4/8 → 4/8 | 0.333 → 0.333 |
| Вместе, 18 | 10/18 → 11/18 | 11/18 → 13/18 | 0.41534 → 0.41534 |

Ошибок и частичных ответов: 0 до и после. По итоговым результатам улучшился
hit@10 discovery и combined, hit@3 discovery вырос на один запрос, MRR не
изменился. Среди заметных сдвигов: ERC-20 ухудшился с ранга 1 до 2; Chainlink
v2 найден на ранге 6; Generative Agents на ранге 3; EIP-7702 остался miss.
Held-out промахи: DeepSeek, speculative decoding, self-consistency и VDF.

## Реализация и проверка

Final deployed main/source hash, confirmed: `b89696b05a4da0b574c1afc1b907e976fb4b035a643760807c14c3a41fcdcf88`.
API full suite: 559 passed, 1 skipped. Независимый прогон ведущего с named
audit: 13 passed, 4 subtests, 1.16 с. Deployment source-only; backup:
`main.before-final-fusion-20261003.py`.

Изменение финального cut сохраняет уже извлечённых кандидатов до финального
RRF с лимитом `max(limit, min(len(hits), 120))`, где `limit` это запрошенный
лимит (10 в этом benchmark), а не фиксированный top 10. Floor,
boilerplate filtering и non-hybrid поведение не менялись. Само сохранение
кандидатов не добавляет retrieval-вызовов. Title lookup может повторно найти
известный ID через прежний bounded injection path. Source-balanced lexical режим включается через
`PAPER_LEXICAL_SOURCE_BALANCE=1`, по умолчанию выключен. Дополнительные WP и
standards участвуют в RRF только для web3. Это lexical/title retrieval, не
семантическая модель перефразирования.

Дополнительные livechecks: исходный ERC-4337 audit с `deep=false` завершился
успешно за 14.189 с. `named_sources` нашёл `eip:4337` со score 0.755, band
`weak`, фрагментом security limitations о central trust point, provenance
spec markdown и `verification_required=true`. Ordinary retrieval также нашёл
источник. Текущий verdict `strong_candidates` опирается на другие четыре
улики и не является научным доказательством. Строгий поиск `EIP-20` с
`layer=web3`, `year_from=2026`, `year_to=2026`, `strict=true` вернул 0
результатов, точные фильтры, без ошибки и без relax. Исправление audit named
source path сохранено.

Registry counts проверены read-only по SSH: `claim_nodes=54`,
`claim_judgments=16`, `claim_links=2`. В этом turn реестр не менялся.

## Границы

18 запросов не являются репрезентативной выборкой. Retrieval hit не измеряет
precision и не подтверждает научное утверждение. Измеренные времена являются
длительностями bounded live-прогона, а не speed proof или SLO. Benchmark не
делает систему production-ready. Ранее полученный результат audit
`named_sources` сохраняется отдельно и этим срезом не отменяется.

Финальная read-only проверка canonical mapping подтвердила алиасы HotStuff и
MEDA; ни один оставшийся miss не оказался скрытой копией target. В запросе
EIP-7702 описано временное задание contract code, а исходное условие
спецификации было постоянным; ожидаемый ID не навязывался. Документы и
платёжный контур не менялись. Другие задачи production readiness, включая
проверку восстановления, этим исправлением не закрыты. Скорость отдельно не
проверялась, SLO не заявляется. Нагрузочный прогон не выполнялся.
