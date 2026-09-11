# dtox research — Colosseum pitch deck template

Версия: 11 сентября 2026  
Хакатон: Crypto World's Fair, 14 сентября — 12 октября 2026  
Формат: 11 слайдов, pitch 2–3 минуты

> Это рабочий шаблон, а не готовый текст для отправки. Всё в квадратных скобках нужно заменить подтверждёнными данными. Не добавлять обещания, которых нет в демо.

## Главный посыл

**dtox helps builders validate Web3 hypotheses against research, protocol specifications and technical evidence.**

Не продавать проект как «ещё один поиск по статьям». Продавать его как инфраструктуру проверки Web3-гипотез, особенно на стыке блокчейна, AI-агентов и LLM: поиск прецедентов, извлечение формул и алгоритмов, сравнение методов и аудит технических claims с источниками.

---

## Слайд 1. Что это

### На слайде

**dtox research**  
Scientific validation for Web3 builders and agents

Validate the hypothesis. Inspect the evidence. Build on what is real.

[QR-код на рабочее демо] · [GitLab] · [контакт]

### Визуал

Один реальный ответ агента: утверждение → фрагмент Methods/Limitations → источник. Не архитектурная схема и не логотипы технологий.

### Что сказать

«Web3-команды постоянно соединяют криптографию, распределённые системы и AI, но проверяют идеи фрагментами из Google, PDF и документации. dtox проверяет техническую гипотезу по научным работам и спецификациям, а затем отдаёт агенту конкретные доказательства через MCP».

---

## Слайд 2. Проблема

### На слайде

**Web3 moves faster than its evidence layer.**

- Исследования разбросаны между arXiv, IACR, EIP, SIMD и whitepapers.
- Поиск заканчивается списком документов.
- PDF расходует контекст и теряет структуру.
- Косинусная близость не доказывает совпадение утверждений.
- Пустая выдача ошибочно воспринимается как новизна.

### Визуал

Слева: 40-страничный PDF. Справа: три объекта `equation`, `algorithm`, `limitation` с источниками.

### Что сказать

«Чтобы проверить ZK-схему, механизм консенсуса, MEV-дизайн или агентный платёж, разработчик ищет сразу в нескольких несовместимых источниках. Обычный поиск даёт документы, но не отвечает, существует ли уже такой механизм, на каких формулах он основан и где ломается».

---

## Слайд 3. Продукт

### На слайде

**One MCP for Web3 R&D**

1. `search_research_paper`
2. `get_code_or_math_spec`
3. `compare_methods`
4. `research_trends`
5. `validate_project`
6. `record_claim_judgment`
7. `link_claim_nodes`

### Визуал

Короткий поток:

`Agent → claim → hybrid retrieval → evidence objects → cited answer`

### Что сказать

«Один интерфейс закрывает поиск по Web3-исследованиям и спецификациям, извлечение реализации, сравнение бенчмарков, анализ трендов и аудит гипотез. AI-agents и LLM становятся вторым слоем корпуса для проверки идей на стыке направлений».

---

## Слайд 4. Живое демо

### На слайде

**From a technical claim to evidence in one agent workflow**

1. Агент формулирует проверяемый claim.
2. dtox находит кандидатов тремя каналами.
3. Агент получает Methods, equations, tables и limitations.
4. Ответ содержит URL, источник и границы уверенности.

### Что показать

Записанный заранее, но настоящий сценарий:

> «Проверь гипотезу: AI-агент оплачивает API через x402, а повторные микроплатежи агрегируются в state channel с ZK-доказательством. Найди существующие схемы, ограничения и алгоритмы».

Показать MCP-вызов, структурированный ответ, переход к формуле и исходной статье. Не печатать запрос вручную в кадре и не ждать загрузку.

### Критерий готовности

Демо воспроизводится с чистого клиента Codex/Claude/Gemini и завершается менее чем за [ИЗМЕРИТЬ] секунд.

---

## Слайд 5. Почему результат лучше обычного RAG

### На слайде

**Three retrieval channels. One evidence contract.**

- Dense embeddings находят смысл.
- BM25 сохраняет точные технические термины.
- Citation graph находит работы вне словаря запроса.
- Cross-encoder переставляет неоднозначные кандидаты.
- Section-aware extraction возвращает нужный объект.

### Визуал

Три стрелки `Dense / Lexical / Citation graph` сходятся в `RRF + reranker`, затем расходятся на `equation / algorithm / table / limitation`.

### Что сказать

«Каналы ошибаются по-разному. Их объединение повышает recall, а кросс-энкодер и нишевой классификатор снижают число омонимов вроде “oracle”, “agent” или “memory” из другой области».

---

## Слайд 6. Что уже работает

### На слайде

**Production baseline — 11 Sep 2026**

- **145,509** проиндексированных документов
- **13,792** Web3-документа в активном индексе
- **8,040,959** структурных поисковых чанков
- **2,858,368** рёбер графа цитирований
- **7** MCP-инструментов
- **8** типов источников
- **15,867** документов обработано за последние 24 часа*

Web3-корпус объединяет криптографию, блокчейн-протоколы, DeFi, MEV, ZK, consensus, smart-contract security, Solana и agent payments. Профильные источники: IACR ePrint 4,842; Ethereum EIP 447; Solana SIMD 65; отраслевые whitepapers 19; дополнительно релевантные работы из arXiv и OpenAlex.

Все источники: arXiv 103,175; ACL Anthology 32,328; IACR ePrint 4,842; OpenAlex OA 2,951; PMLR 1,682; Ethereum EIP 447; Solana SIMD 65; отраслевые whitepapers 19.

\* Включает обработку накопленного backlog; не подавать как устойчивую ежедневную скорость.

### Визуал

Три крупные карточки: documents / chunks / citation edges. Внизу компактная полоса источников.

### Что сказать

«Это не макет. В активном индексе почти четырнадцать тысяч Web3-документов, включая исследования, протокольные спецификации и отраслевые whitepapers. Индекс, API, MCP, ingestion pipeline и платёжный gateway работают на сервере. Для документов без доступного полного текста ответ явно помечается `fulltext: false`».

---

## Слайд 7. Почему Solana и x402

### На слайде

**Agents can buy evidence, not subscriptions.**

- Бесплатный preview для discovery.
- Платные evidence/spec/compare/trends/audit вызовы.
- USDC pay-per-call через x402.
- Один gateway для Solana и Base.
- Без аккаунта и постоянного API-ключа.

### Текущий честный статус

Gateway развёрнут в `shadow` mode. Реализованы `@x402/mcp`, `@x402/svm`, `@x402/evm` и `@solana/kit`; настроены Solana Devnet и Base Sepolia. Во время хакатона нужно включить реальную оплату, показать транзакцию в Solana Explorer и измерить стоимость/латентность полного вызова.

### Визуал

`Agent → HTTP 402 → USDC payment → MCP evidence → receipt` с двумя settlement-ветками: Solana / Base.

### Что сказать

«Solana здесь не декоративна. Она даёт агенту программируемый платёж за единичное доказательство без регистрации и подписки. Если убрать сеть, исчезает permissionless machine-to-machine business model».

---

## Слайд 8. Конкуренты

### На слайде

| Продукт | Подтверждённый масштаб | Основной продукт | Где dtox отличается |
|---|---:|---|---|
| Semantic Scholar | 214 млн papers; 2.49 млрд citations | Универсальный поиск и academic graph | dtox оптимизирован под агентные доказательства и структурные элементы узких технических ниш |
| Consensus | 220+ млн peer-reviewed papers; 7+ млн пользователей | Ответы и синтез научного консенсуса | dtox отдаёт MCP-объекты реализации, claim registry и pay-per-call |
| Elicit | 138+ млн papers | Systematic review, screening и reports | dtox фокусируется на Methods/Algorithms/Math/Limitations и встраивается в runtime агента |
| Обычный vector RAG | Зависит от пользователя | Семантический поиск по загруженным документам | dtox добавляет BM25, citation graph, reranking, provenance и воспроизводимый corpus snapshot |
| **dtox** | 145,509 документов; 13,792 по Web3; 8.04 млн структурных чанков | Проверка Web3-гипотез через API/MCP | Web3-first корпус, структурные доказательства и USDC x402 settlement |

### Важная формулировка

Не говорить «у конкурентов нет full-text, API или MCP». У некоторых они есть. Наше преимущество — не максимальный общий корпус, а глубокий Web3-first индекс, который соединяет академические статьи с IACR, EIP, SIMD и whitepapers, а затем отдаёт структурные доказательства через MCP и x402.

### Визуал

Таблица без галочек «мы лучше во всём». Подсветить только строку dtox и одну колонку дифференциации.

---

## Слайд 9. Защитимый актив

### На слайде

**The moat is the Web3 evidence graph.**

- Research paper / EIP / SIMD → section → element → claim
- Citation neighbourhood with hub penalty
- Reader judgments with provenance
- Linked formulations of the same question
- Regression harness for retrieval and verdict drift

### Визуал

Мини-граф: один claim, три формулировки, две статьи, формула, limitation и два независимых reader judgments.

### Что сказать

«Сами документы доступны другим. Трудно копируется связанная карта Web3-знаний: какая работа или спецификация что именно утверждает, где лежит доказательство, чем оно ограничено и как это пересекается с агентами и LLM».

---

## Слайд 10. Бизнес и рост

### На слайде

**Free discovery. Paid certainty.**

- Preview: бесплатно
- Evidence/spec/compare: микроплатёж
- Full audit: более дорогой вызов
- Позже: командные лимиты и private corpora

### Подставить до подачи

- Цена каждого вызова: текущая конфигурация или новая подтверждённая сеткой.
- Себестоимость: embedding + rerank + storage + facilitator fee.
- Валовая маржа после network/RPC/compute.
- [N] внешних тестировщиков, [N] успешных MCP-сессий, [N] повторных пользователей.

### Визуал

Простой flywheel:

`more agent calls → more reviewed claims → better evidence → more useful calls`

### Что сказать

«Пользователь не покупает место в подписке. Агент покупает ровно тот уровень уверенности, который нужен задаче: preview, evidence или полный audit».

---

## Слайд 11. Команда, хакатонный sprint и ask

### На слайде

**What we will prove during Crypto World's Fair**

1. Live Solana USDC x402 payment.
2. Agent completes paid R&D audit end-to-end.
3. Public SDK/setup in one command.
4. External users and measured repeat usage.
5. Reproducible benchmark against baseline search.

**Team:** [имя, роль, релевантный опыт]  
**Ask:** [конкретно: testers / design partners / accelerator / grant]  
**Links:** [demo] · [repo] · [docs] · [Explorer transaction]

### Визуал

Четырёхнедельная шкала: payment → demo → testers → submission.

### Что сказать

«Базовая research-инфраструктура существовала до хакатона и будет раскрыта как pre-existing. В рамках sprint мы превращаем её в проверяемый Solana-native продукт: реальная оплата, агентный пользовательский путь, измеримая экономика и внешняя валидация».

---

## Что особенно важно судьям Colosseum

Официальные критерии: functionality, potential impact, novelty, UX, open-source/composability и business plan. Поэтому презентация должна доказать:

1. **Работает:** короткое живое демо и резервная запись.
2. **Solana необходима:** реальная devnet/mainnet-транзакция, не просто логотип.
3. **Есть новый примитив:** evidence-as-a-service для автономных агентов.
4. **Есть бизнес:** измеримая себестоимость и pay-per-call экономика.
5. **Команда продолжит после хакатона:** конкретный следующий milestone.

Не тратить слайды на общий TAM AI, историю блокчейна, устройство HNSW или длинный roadmap.

## Обязательные данные перед финальной версией

- [ ] Рабочий публичный demo URL без пароля для судей.
- [ ] Solana Explorer URL успешной x402-транзакции.
- [ ] Реальная цена и end-to-end latency каждого paid tool.
- [ ] Минимум 5–10 внешних тестировщиков и короткие цитаты обратной связи.
- [ ] Честное сравнение одинаковых запросов: dtox vs baseline.
- [ ] История создания и founder-market fit в двух предложениях.
- [ ] Команда и роли.
- [ ] Disclosure: что существовало до 14 сентября и что создано за sprint.
- [ ] Pitch video 2–3 минуты и отдельное technical demo ≤3 минут.

## Источники цифр и правил

- [Colosseum Crypto World's Fair: даты, число builders и требования](https://colosseum.com/hackathon?year=fall2026)
- [Официальные советы Colosseum по pitch и technical demo](https://blog.colosseum.com/perfecting-your-hackathon-submission/)
- [Как Colosseum оценивает founder intent и working demo](https://blog.colosseum.com/how-to-win-a-colosseum-hackathon/)
- [Semantic Scholar API: 214 млн papers, 2.49 млрд citations](https://www.semanticscholar.org/product/api)
- [Consensus: 220+ млн papers и 7+ млн пользователей](https://help.consensus.app/en/articles/9922726-why-do-people-choose-consensus)
- [Elicit: 138+ млн papers и API/MCP](https://elicit.com/)
- [x402: HTTP-native pay-per-call и multi-network support](https://docs.cdp.coinbase.com/x402/welcome)
- [Coinbase Agentic Wallet MCP: Base и Solana payments](https://docs.cdp.coinbase.com/agentic-wallet/mcp/welcome)
- Статистика dtox: live-снимок `state.db`, Qdrant и `judgments.db` от 11 сентября 2026.
