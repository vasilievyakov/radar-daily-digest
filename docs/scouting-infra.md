# Разведка источников: агентная инфраструктура, оркестрация, данные

Срез: LlamaIndex, CrewAI, AutoGen, Microsoft Semantic Kernel и Agent Framework, Pydantic AI,
Instructor, Vercel AI SDK, Haystack, DSPy, Temporal, Modal, Ray, Weights & Biases, Langfuse,
Braintrust, векторные хранилища (Pinecone, Weaviate, Qdrant, Chroma, Milvus, pgvector),
Supabase и Neon.

Дата проверки: 2026-08-17. Окно «12 месяцев» отсчитывается от 2025-08-17, окно «18 месяцев» —
от 2025-02-17.

Каждый адрес в таблице открыт лично: `curl -sS -L` с браузерным User-Agent, затем подсчёт
уникальных дат тремя форматами (`2026-08-14`, `Aug 14, 2026`, `14 August 2026`). Для репозиториев —
`gh api repos/OWNER/REPO/releases?per_page=100` с разбором тела релизов. Финальный адрес после
редиректов зафиксирован отдельно, чтобы не повторить ловушку `docs.cursor.com/changelog`.

## Итог одной строкой

Плотных ячеек `(вендор, тип изменения)` с тремя и более датированными событиями закрывается **27**
у **17 вендоров**. Порог записки (`min_vendors_with_dense_cell: 3`) перекрыт с большим запасом.
Настоящие migration guides с датами в тексте нашлись у трёх вендоров: Chroma, Pydantic AI, Milvus.

## Таблица источников

### Оркестрация и агентные фреймворки

| Вендор | Источник | Тип источника | Закрывает | HTTP | Дат или релизов за 12 мес | Глубина | Вывод |
|---|---|---|---|---|---|---|---|
| LlamaIndex | `https://developers.llamaindex.ai/python/framework/changelog/` | html_scrape | breaking_change, deprecation, release | 200, 3.8 МБ | 22 даты (58 за 18 мес) | 2023-06-02 … 2026-03-16 | Брать. Лучший источник среза: 39 заголовков «Breaking Changes», 112 упоминаний `deprecate`, 27 «Deprecated» на одной странице. Отстаёт от GitHub примерно на квартал, поэтому нужен вместе с релизами |
| LlamaIndex | `run-llama/llama_index` | github_releases | breaking_change, deprecation, release | API 200 | 28 релизов, из них 10 с разделами о ломающих изменениях | 2024-10-25 … 2026-06-24 | Брать. Средний размер тела релиза 4949 знаков — есть что извлекать |
| CrewAI | `https://docs.crewai.com/en/changelog` | html_scrape | release, breaking_change, deprecation | 200, 2.6 МБ | 101 дата (121 за 18 мес) | 2023-11-14 … 2026-08-14 | Брать. Самая высокая плотность дат в срезе. Редирект уводит на версионный путь `/v1.15.16/en/changelog`, канонический адрес отрабатывает корректно, но парсер не должен закреплять номер версии |
| CrewAI | `crewAIInc/crewAI` | github_releases | release, breaking_change | API 200 | 100 релизов на первой странице | 2025-12-19 … 2026-08-14 | Брать с оговоркой: тела короткие (678 знаков), темп выпуска высокий, для бэкфилла нужна пагинация |
| Microsoft Agent Framework | `microsoft/agent-framework` | github_releases | breaking_change, deprecation, release | API 200 | 100 релизов, 59 с разделами о ломающих изменениях и отключениях | 2025-11-05 … 2026-08-14 | Брать. Преемник AutoGen и агентной части Semantic Kernel, тела релизов 2902 знака |
| Microsoft Agent Framework | `https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-semantic-kernel/` | html_scrape | breaking_change | 200, 80 КБ | 2 даты в разметке | одна страница | Брать как единичный документ, не как ленту. Настоящий migration guide, но дата публикации в HTML почти не выражена — её берут из метаданных Microsoft Learn |
| Semantic Kernel | `microsoft/semantic-kernel` | github_releases | breaking_change, release | API 200 | 43 релиза, 8 с ломающими изменениями | 2025-02-20 … 2026-08-06 | Брать. Тела 2600 знаков |
| Semantic Kernel | `https://learn.microsoft.com/en-us/semantic-kernel/support/migration/v1-migration-guide` | html_scrape | breaking_change | 200, 57 КБ | 3 даты | одна страница | Брать как якорный документ. Раздел `/support/migration/` содержит ещё пять руководств (function-calling, kernel-events-and-filters, group-chat-orchestration, agent-framework-rc); индексной страницы `/support/migration` нет, она отдаёт 404 |
| AutoGen | `microsoft/autogen` | github_releases | release | API 200 | 3 релиза | 2023-09-19 … 2025-09-30 | Отбросить как самостоятельный источник. Проект фактически влит в Agent Framework, за 12 месяцев три релиза. Alias `autogen` вести на Microsoft |
| Pydantic AI | `https://pydantic.dev/docs/ai/project/changelog/` | html_scrape | breaking_change, deprecation | 200, 184 КБ | 11 дат (18 за 18 мес) | 2025-04-15 … 2026-06-23 | Брать. Датированный migration guide: разделы «Breaking Changes» и «Upgrade Guide», отдельно «Changes not covered by deprecation warnings» и «Changes covered by deprecation warnings». Переход 1.0.0 (2025-09-04) → 2.0.0 (2026-06-23) с семью датированными бетами |
| Pydantic AI | `pydantic/pydantic-ai` | github_releases | release, breaking_change | API 200 | 100 релизов, 22 с ломающими изменениями | 2026-02-18 … 2026-08-15 | Брать. Первая страница покрывает лишь полгода, для бэкфилла нужна пагинация |
| Instructor | `https://raw.githubusercontent.com/instructor-ai/instructor/main/CHANGELOG.md` | html_scrape | release, breaking_change | 200, 29 КБ | 15 дат | 2025-08-27 … 2026-08-09 | Брать. Чистый Markdown с датами, разбирается тривиально. Страница `python.useinstructor.com/CHANGELOG/` отдаёт 404 |
| Vercel AI SDK | `https://ai-sdk.dev/docs/migration-guides` | html_scrape | breaking_change | 200, 340 КБ | 0 дат | 12 руководств | Брать как каталог, не как ленту. Есть отдельные страницы для 3.1, 3.2, 3.3, 3.4, 4.0, 4.1, 4.2, 5.0, 5.0-data, 6.0, 7.0 и versioning. Дат внутри нет ни на одной |
| Vercel AI SDK | `https://registry.npmjs.org/ai` | http_json | breaking_change, release | 200, 5.8 МБ | 908 публикаций версий | 2014-08-15 … 2026-08 | Брать как источник дат для мажоров: 5.0.0 — 2025-07-31, 6.0.0 — 2025-12-22, 7.0.0 — 2026-06-25. Три мажора за тринадцать месяцев, каждый со своим migration guide |
| Vercel AI SDK | `vercel/ai` | github_releases | — | API 200 | 800 релизов укладываются в 2026-08-07 … 2026-08-14 | нет | Отбросить. Монорепозиторий выпускает пакеты пачками, 100 релизов покрывают четыре дня, тела по 200 знаков. Для бэкфилла потребовались бы сотни страниц API |
| Haystack | `deepset-ai/haystack` | github_releases | release, breaking_change | API 200 | 52 релиза, 9 с ломающими изменениями | 2024-12-18 … 2026-07-20 | Брать. Тела 3770 знаков, есть переход 2.31 → 3.0.0 (2026-07-20) |
| Haystack | `https://haystack.deepset.ai/release-notes` | html_scrape | release | 200, 55 КБ | 12 дат | 2026-02-26 … 2026-07-20 | Брать как датированный индекс. Содержательный текст лежит на отдельных страницах релизов, сама страница — только список. `docs.haystack.deepset.ai/docs/changelog` отдаёт 404, `docs/migration` открывается, но дат в нём нет |
| DSPy | `stanfordnlp/dspy` | github_releases | breaking_change, release | API 200 | 14 релизов, 7 с ломающими изменениями | 2024-09-23 … 2026-08-03 | Брать. Тела 3177 знаков, половина релизов содержит разделы о совместимости. Страница `dspy.ai/community/migration/` отдаёт 404 |

### Исполнение, вычисления, наблюдаемость

| Вендор | Источник | Тип источника | Закрывает | HTTP | Дат или релизов за 12 мес | Глубина | Вывод |
|---|---|---|---|---|---|---|---|
| Temporal | `https://temporal.io/changelog` | html_scrape | deprecation, breaking_change, limits, release | 200, 543 КБ | 41 дата (51 за 18 мес) | 2022-03-29 … 2026-08-07 | Брать. 23 упоминания `deprecation` и 8 «breaking change» на странице, записи размечены по компонентам: Cloud, Server, UI, CLI и SDK для девяти языков. Исходный адрес `temporal.io/change-log` корректно ведёт сюда. `docs.temporal.io/releases` и `docs.temporal.io/references/server-release-notes` отдают 404 |
| Temporal | `temporalio/temporal` | github_releases | breaking_change, release | API 200 | 27 релизов, 8 с ломающими изменениями | 2021-09-16 … 2026-07-08 | Брать. Тела 3088 знаков |
| Temporal | `temporalio/sdk-python` | github_releases | breaking_change, deprecation | API 200 | 26 релизов, 12 с ломающими изменениями и отключениями | 2022-03-18 … 2026-07-29 | Брать. Самая высокая доля ломающих изменений среди SDK Temporal |
| Modal | `https://modal.com/docs/sdk/py/changelog` | html_scrape | deprecation, breaking_change, release | 200, 146 КБ | 26 дат (35 за 18 мес) | 2025-05-16 … 2026-08-12 | Брать. Страница прямо объявляет себя лентой «features, enhancements, fixes, and deprecations»: 30 упоминаний отключений, 12 «removed», 8 «breaking». Адрес `modal.com/docs/reference/changelog` редиректит сюда |
| Modal | `modal-labs/modal-client` | github_releases | — | API 200 | 0 релизов | нет | Отбросить. GitHub Releases в репозитории не ведутся |
| Ray и Ray Serve | `ray-project/ray` | github_releases | breaking_change, deprecation, release | API 200 | 18 релизов, 10 с ломающими изменениями и отключениями | 2020-01-27 … 2026-08-11 | Брать. Средний размер тела 11 464 знака — самые подробные release notes в срезе. `docs.ray.io/en/latest/ray-overview/deprecations.html` и `whats-new.html` отдают 404 |
| Weights & Biases | `https://docs.wandb.ai/release-notes/server-releases` | html_scrape | release, breaking_change | 200, 849 КБ | 31 дата | 2025-05-01 … 2026-08-11 | Брать. Есть разделы «Breaking changes» и записи о миграциях, но их немного |
| Weights & Biases | `https://docs.wandb.ai/release-notes/sdk-releases` | html_scrape | release | 200, 2.4 МБ | 23 даты (33 за 18 мес) | 2018-10-05 … 2026-08-04 | Брать вторым номером. Индекс `docs.wandb.ai/release-notes` дат почти не содержит, работать нужно с подстраницами `server-releases`, `sdk-releases`, `weave-sdk-releases` |
| Weights & Biases | `wandb/wandb` | github_releases | release, breaking_change | API 200 | 22 релиза, 5 с ломающими изменениями | 2022-05-26 … 2026-08-12 | Брать |
| Langfuse | `https://langfuse.com/changelog` | html_scrape | release, breaking_change | 200, 362 КБ | 38 дат, 50 ссылок вида `/changelog/2026-08-17-…` | 2026-04-08 … 2026-08-17 | Брать. Дата зашита в сам адрес записи, парсинг устойчив. Первая страница покрывает четыре месяца, для глубины нужна пагинация или разбор ссылок |
| Langfuse | `langfuse/langfuse` | github_releases | release, breaking_change | API 200 | 100 релизов, 26 с ломающими изменениями за 18 мес | 2026-04-22 … 2026-08-17 | Брать. Мажор v4 вышел 2026-08-17 |
| Braintrust | `https://www.braintrust.dev/docs/changelog` | — | — | 200, 2.2 МБ | 0 дат в HTML, 0 дат после рендеринга TabStack | нет | Отбросить. Записи вообще не датированы: заголовки без дат, только порядок. 31 упоминание «breaking» привязать не к чему |
| Braintrust | `braintrustdata/braintrust-sdk` | github_releases | release | API 200 | 74 релиза, 6 с ломающими изменениями | 2025-10-22 … 2026-08-17 | Брать как слабый источник. Тела 570 знаков, плотной ячейки не даёт |
| LangSmith | `https://docs.langchain.com/langsmith/changelog` | html_scrape | release, breaking_change | 200, 1.8 МБ | 25 дат (46 за 18 мес) | 2025-01-20 … 2026 | Не брать в этом срезе: пересекается с зоной второго разведчика по LangChain. Источник рабочий, `changelog.langchain.com` редиректит сюда. Решение о владельце — за оркестратором |

### Векторные хранилища и данные

| Вендор | Источник | Тип источника | Закрывает | HTTP | Дат или релизов за 12 мес | Глубина | Вывод |
|---|---|---|---|---|---|---|---|
| Pinecone | `https://docs.pinecone.io/release-notes/2026` | html_scrape | deprecation, breaking_change, limits, release | 200, 609 КБ | 28 дат | 2026-01-15 … 2026-08-13 | Брать. 44 упоминания отключений, 7 «breaking change», 3 «rate limit» на одной странице. Голый `/release-notes` редиректит на текущий год |
| Pinecone | `https://docs.pinecone.io/release-notes/2025` | html_scrape | deprecation, breaking_change, release | 200, 900 КБ | 22 даты (52 за 18 мес) | весь 2025 год | Брать для бэкфилла |
| Pinecone | `https://docs.pinecone.io/release-notes/2024` | html_scrape | deprecation, breaking_change | 200, 894 КБ | 93 уникальные даты за год | весь 2024 год | Брать для исторического корпуса, если понадобится глубина сверх 18 месяцев |
| Pinecone | `pinecone-io/pinecone-python-client` | github_releases | breaking_change | API 200 | 8 релизов, 8 из 16 за 18 мес с ломающими изменениями | 2021-10-31 … 2026-06-04 | Брать. Тела 4659 знаков, освещают переходы клиента между версиями API |
| Weaviate | `weaviate/weaviate` | github_releases | breaking_change, deprecation, release | API 200 | 100 релизов, 93 с разделами о ломающих изменениях | 2026-01-12 … 2026-08-06 | Брать. Наивысшая доля явных breaking-разделов в срезе: они у Weaviate стандартный пункт шаблона релиза |
| Weaviate | `https://docs.weaviate.io/weaviate/release-notes` | html_scrape | release | 200, 52 КБ | 7 дат (11 за 18 мес) | 2023-03-07 … 2026-08-04 | Брать как карту версий: таблица «Weaviate Database и клиентские библиотеки» с датами первых релизов минорных линий и матрицей совместимости клиентов Python, TypeScript, Go, Java, C#. Содержательного текста об изменениях на странице нет |
| Weaviate | `https://docs.weaviate.io/deploy/migration` | html_scrape | breaking_change | 200, 32 КБ | 0 дат | версии 1.5 … 1.31 | Брать как справочник, не как ленту. Настоящий набор migration guides: `/deploy/migration/weaviate-1-25` (миграция на Raft), `/deploy/migration/weaviate-1-30` (BlockMax WAND), `/deploy/migration/archive`. Даты в тексте отсутствуют, привязка только к номерам версий. Адрес `/deploy/migration/index` отдаёт 404, `/weaviate/client-libraries/python/v3_v4_migration` молча редиректит на общую страницу клиента — типичная ловушка |
| Qdrant | `qdrant/qdrant` | github_releases | deprecation, breaking_change, release | API 200 | 13 релизов, 5 с отключениями и ломающими изменениями | 2022-06-24 … 2026-08-05 | Брать. У релизов есть постоянный раздел «Deprecations», включая объявления с горизонтом: в v1.17.0 заранее объявлено удаление RocksDB и старых search-эндпоинтов в v1.18. Тела 3172 знака |
| Qdrant | `https://qdrant.tech/documentation/release-notes/` | — | — | 200, 44 КБ | 0 дат в HTML | — | Отбросить. Страница оказалась зеркалом GitHub Releases: TabStack рендерит её содержимое, но дат нет и там, потому что GitHub отдаёт относительное время. Идти напрямую в API |
| Chroma | `https://docs.trychroma.com/docs/overview/migration` | html_scrape | breaking_change | 200, 513 КБ | 1 дата за 12 мес, 11 дат всего | 2023-07-17 … 2025-03-01 | Брать как эталонный датированный migration guide. «Migration Log» с записями вида «v1.0.0 — March 1, 2025», «v0.6.0 — December 30, 2024», у каждой раздел «Breaking changes». Свежих записей мало, для текущей ленты нужен GitHub. Адреса `/updates/migration`, `/updates/troubleshooting`, `/production/administration/migration` отдают 404 |
| Chroma | `chroma-core/chroma` | github_releases | release, breaking_change | API 200 | 40 релизов, 12 с ломающими изменениями за 18 мес | 2023-08-29 … 2026-05-05 | Брать. Тела 4099 знаков |
| Milvus | `https://milvus.io/docs/release_notes.md` | html_scrape, требует рендеринга | breaking_change, release | curl уходит в цикл редиректов (50 переходов, 302), TabStack отдаёт полностью | по рендерингу: 3.0.0 — 2026-07-29, 3.0-beta — 2026-05-09 | линия 3.0.x | Брать только через рендеринг. Ценность в разделе «Compatibility and behavior notes»: правила отката 2.6 → 3.0, opt-in новых версий индексов, переход GPU-образов на CUDA 12.9 с потерей совместимости с Ubuntu 20.04 |
| Milvus | `milvus-io/milvus` | github_releases | release, breaking_change | API 200 | 42 релиза, 4 с явными ломающими изменениями | 2024-04-16 … 2026-08-04 | Брать как основной источник по Milvus. Тела 4208 знаков, обычный HTTP, дат достаточно |
| pgvector | `https://raw.githubusercontent.com/pgvector/pgvector/master/CHANGELOG.md` | html_scrape | release | 200, 7 КБ | 6 дат | 2023-01-11 … 2026-07-29 | Брать как тонкий источник. Даты в заголовках версий, но содержание почти целиком исправления. Плотной ячейки по четырём типам не даёт. GitHub Releases в репозитории не ведутся |
| Supabase | `https://supabase.com/changelog` | html_scrape | release | 200, 846 КБ | 68 дат (103 за 18 мес) | 2021-11-30 … 2026-07-30, верхняя запись ленты — 2026-07-30 | Брать как ленту релизов, не как источник ломающих изменений: на первой странице ноль упоминаний отключений, цен, квот и лимитов. Записи размечены метками вида «Bug Fix». Извлекатель дат ловит и артефакт 2027-01-31 из служебной разметки — парсеру нужен потолок по текущей дате |
| Neon | `https://neon.com/docs/changelog` | html_scrape | release | 200, 2.6 МБ | 58 дат (86 за 18 мес) | 2022-06-08 … 2026-08-14 | Брать как ленту релизов. Разметка удобная: заголовки `### 2026-08-14`. Отключений на первой странице всего три упоминания, плотной ячейки по четырём типам не даёт. Как и у Supabase, в разметке встречается артефакт с датой из будущего (2026-12-31) |
| Neon | `neondatabase/neon` | github_releases | — | API 200 | 0 релизов за 12 мес | 2025-01-30 … 2025-07-29 | Отбросить. Публикация релизов прекращена в июле 2025, тела по 173 знака |

## Что закрывается: ячейки (вендор, тип изменения)

Ячейка засчитана, если по проверенному источнику видно не менее трёх датированных событий типа
за 12–18 месяцев.

| Вендор | breaking_change | deprecation | pricing | limits |
|---|---|---|---|---|
| LlamaIndex | да | да | нет | нет |
| CrewAI | да | да | нет | нет |
| Microsoft Agent Framework | да | да | нет | нет |
| Semantic Kernel | да | нет | нет | нет |
| Pydantic AI | да | да | нет | нет |
| Vercel AI SDK | да | нет | нет | нет |
| Haystack | да | нет | нет | нет |
| DSPy | да | нет | нет | нет |
| Temporal | да | да | нет | нет |
| Modal | да | да | нет | нет |
| Ray | да | да | нет | нет |
| Pinecone | да | да | нет | пограничная |
| Weaviate | да | да | нет | нет |
| Qdrant | да | да | нет | нет |
| Chroma | да | нет | нет | нет |
| Langfuse | да | нет | нет | нет |
| Instructor | да | нет | нет | нет |

Итого 27 закрытых ячеек у 17 вендоров.

Пограничные, не засчитаны: Weights & Biases (`breaking_change` — два раздела на странице серверных
релизов и пять релизов из двадцати двух), Milvus (`breaking_change` — четыре релиза из сорока двух
с явными формулировками), Supabase и Neon (все четыре типа), pgvector, Braintrust.

Отдельно стоит сказать про `pricing` и `limits`: в этом срезе они почти пусты. Инфраструктурные
проекты объявляют цены на маркетинговых страницах и в блогах, а не в changelog. Единственный
источник, где лимиты попадают в датированную ленту, — Pinecone (три упоминания rate limit в
release notes за 2026 год) и Temporal (записи о квотах в разделе Cloud). Если ячейки `pricing`
и `limits` нужны для готовности корпуса, их следует искать в срезе платформенных вендоров,
а не здесь.

## Настоящие migration guides с датами

Полностью датированные, готовые к разбору:

1. **Chroma**, `https://docs.trychroma.com/docs/overview/migration`. «Migration Log» — список
   записей «версия — дата», у каждой раздел «Breaking changes». Одиннадцать датированных записей
   от 2023-07-17 до 2025-03-01. Формально это и есть образцовая структура для типа
   `breaking_change`: дата, версия, перечень сломанного.
2. **Pydantic AI**, `https://pydantic.dev/docs/ai/project/changelog/`. Страница объединяет
   changelog и upgrade guide. Разделы «Breaking Changes» и «Upgrade Guide», версии датированы
   в заголовках, отдельно разведены «изменения, не покрытые предупреждениями об устаревании»
   и «изменения, покрытые предупреждениями». Переход 1.0.0 (2025-09-04) → 2.0.0 (2026-06-23)
   расписан через семь датированных бет.
3. **Milvus**, `https://milvus.io/docs/release_notes.md`. Каждый релиз открывается строкой
   «Release date: July 29, 2026», а заканчивается разделом «Compatibility and behavior notes»
   с правилами отката и условиями потери совместимости. Требует рендеринга.

Датированы частично, полагаться нельзя:

- **Semantic Kernel и Agent Framework** на Microsoft Learn. Руководства настоящие
  (`v1-migration-guide`, `function-calling-migration-guide`, `kernel-events-and-filters-migration`,
  `group-chat-orchestration-migration-guide`, `agent-framework-rc-migration-guide`,
  `from-semantic-kernel`), но дата публикации в HTML почти не выражена: две-три даты на страницу.
  Внешние индексаторы её знают, сама страница — нет.

Недатированные, но содержательные:

- **Vercel AI SDK** — двенадцать руководств по адресам `/docs/migration-guides/*`, ни одной даты.
  Даты восстанавливаются из npm registry по времени публикации мажоров.
- **Weaviate** — `/deploy/migration` и версионные страницы, привязка только к номерам версий.
- **Haystack** — `docs.haystack.deepset.ai/docs/migration`, дат нет.

## Пять лучших источников среза

1. `https://developers.llamaindex.ai/python/framework/changelog/` — 319 дат всего, 39 заголовков
   «Breaking Changes», 112 упоминаний устаревания, 27 «Deprecated». Обычный curl, без рендеринга.
   Единственный минус — отставание от репозитория примерно на квартал.
2. `https://docs.crewai.com/en/changelog` — 181 дата, из них 101 за последние 12 месяцев.
   Самая плотная лента среза, записи структурированы по разделам Features, Bug Fixes,
   Documentation, Contributors.
3. `https://docs.pinecone.io/release-notes/2026` вместе с `/2025` и `/2024` — единственный
   вендор среза, у которого объявления об отключениях идут датированным потоком: 44 упоминания
   на странице 2026 года. Плюс глубина: 93 уникальные даты за 2024 год.
4. `https://temporal.io/changelog` — 240 дат, глубина с 2022 года, записи размечены по
   компонентам (Cloud, Server, UI, CLI, девять SDK) и по типу (Feature, Deprecation).
   Готовая разметка под `vendor` и `change_type`.
5. `microsoft/agent-framework` через GitHub Releases — 100 релизов за 12 месяцев, из них 59
   с разделами о ломающих изменениях и отключениях, тела по 2902 знака. Здесь же лежит
   развязка судьбы AutoGen и Semantic Kernel.

Шестым, если позволит бюджет, стоит взять `weaviate/weaviate`: 93 релиза из 100 несут явный
раздел о ломающих изменениях, потому что это пункт их шаблона.

## Фрагмент для `sources`

```yaml
  # ---------- Агентная инфраструктура и оркестрация ----------

  # Приоритет 1: датированные changelog и реестры отключений, берутся обычным curl.
  - id: llamaindex_changelog
    type: html_scrape
    url: https://developers.llamaindex.ai/python/framework/changelog/
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  # Редирект уводит на версионный путь /v1.15.16/en/changelog. Канонический адрес
  # отрабатывает, но номер версии в парсере закреплять нельзя.
  - id: crewai_changelog
    type: html_scrape
    url: https://docs.crewai.com/en/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 30

  - id: pinecone_release_notes_2026
    type: html_scrape
    url: https://docs.pinecone.io/release-notes/2026
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 360
    min_expected_items: 15

  - id: pinecone_release_notes_2025
    type: html_scrape
    url: https://docs.pinecone.io/release-notes/2025
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  # Записи размечены по компонентам (Cloud, Server, UI, CLI, SDK) и по типу
  # (Feature, Deprecation). Адрес temporal.io/change-log редиректит сюда.
  - id: temporal_changelog
    type: html_scrape
    url: https://temporal.io/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 25

  # Страница прямо объявляет себя лентой отключений клиентской библиотеки.
  # modal.com/docs/reference/changelog редиректит сюда.
  - id: modal_python_changelog
    type: html_scrape
    url: https://modal.com/docs/sdk/py/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 15

  # Дата зашита в адрес каждой записи (/changelog/2026-08-17-langfuse-v4).
  # Первая страница покрывает четыре месяца, глубже — только пагинацией.
  - id: langfuse_changelog
    type: html_scrape
    url: https://langfuse.com/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 180
    min_expected_items: 20

  # Датированный migration guide: разделы Breaking Changes и Upgrade Guide,
  # переход 1.0.0 (2025-09-04) -> 2.0.0 (2026-06-23).
  - id: pydantic_ai_changelog
    type: html_scrape
    url: https://pydantic.dev/docs/ai/project/changelog/
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 8

  # Migration Log с записями "версия - дата" и разделами Breaking changes.
  # Эталон структуры, но свежих записей мало: последняя от 2025-03-01.
  - id: chroma_migration_log
    type: html_scrape
    url: https://docs.trychroma.com/docs/overview/migration
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 3

  - id: instructor_changelog
    type: html_scrape
    url: https://raw.githubusercontent.com/instructor-ai/instructor/main/CHANGELOG.md
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 360
    min_expected_items: 10

  # Приоритет 2: рабочие ленты релизов без плотности по ломающим изменениям
  # либо требующие подстраниц.
  - id: wandb_server_release_notes
    type: html_scrape
    url: https://docs.wandb.ai/release-notes/server-releases
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 15

  - id: wandb_sdk_release_notes
    type: html_scrape
    url: https://docs.wandb.ai/release-notes/sdk-releases
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 15

  # Карта версий: таблица минорных линий с датами и матрица совместимости
  # клиентских библиотек. Содержательный текст изменений живёт в GitHub Releases.
  - id: weaviate_release_notes
    type: html_scrape
    url: https://docs.weaviate.io/weaviate/release-notes
    priority: 2
    enabled: true
    parser_hint: dated_table
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 5

  - id: haystack_release_notes
    type: html_scrape
    url: https://haystack.deepset.ai/release-notes
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 360
    min_expected_items: 8

  - id: supabase_changelog
    type: html_scrape
    url: https://supabase.com/changelog
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 30

  # Разметка заголовками ### YYYY-MM-DD, разбирается тривиально.
  - id: neon_changelog
    type: html_scrape
    url: https://neon.com/docs/changelog
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 30

  - id: pgvector_changelog
    type: html_scrape
    url: https://raw.githubusercontent.com/pgvector/pgvector/master/CHANGELOG.md
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 5

  # Приоритет 2: одиночные migration guides. Не ленты, а якорные документы;
  # дату события берут из версии, а не из страницы.
  - id: semantic_kernel_v1_migration
    type: html_scrape
    url: https://learn.microsoft.com/en-us/semantic-kernel/support/migration/v1-migration-guide
    priority: 2
    enabled: true
    parser_hint: single_document
    backfill_supported: false
    min_expected_items: 1

  - id: agent_framework_migration_from_sk
    type: html_scrape
    url: https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-semantic-kernel/
    priority: 2
    enabled: true
    parser_hint: single_document
    backfill_supported: false
    min_expected_items: 1

  # Каталог из двенадцати руководств (3.1 ... 7.0 и versioning). Дат внутри нет:
  # их подставляют из npm registry по времени публикации мажоров.
  - id: vercel_ai_sdk_migration_guides
    type: html_scrape
    url: https://ai-sdk.dev/docs/migration-guides
    priority: 2
    enabled: true
    parser_hint: link_index
    backfill_supported: false
    min_expected_items: 8

  # Источник дат для мажоров AI SDK: 5.0.0 - 2025-07-31, 6.0.0 - 2025-12-22,
  # 7.0.0 - 2026-06-25. GitHub Releases для vercel/ai непригодны: сто релизов
  # покрывают четыре дня.
  - id: vercel_ai_sdk_npm_versions
    type: http_json
    url: https://registry.npmjs.org/ai
    priority: 2
    enabled: true
    parser_hint: npm_time_map
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 3

  # Требует рендеринга: обычный curl уходит в цикл из пятидесяти редиректов,
  # TabStack отдаёт страницу целиком. Включать после того, как в коллекторе
  # появится рендеринг; до тех пор Milvus закрыт через GitHub Releases.
  - id: milvus_release_notes
    type: html_scrape
    url: https://milvus.io/docs/release_notes.md
    priority: 2
    enabled: false
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 5

  # Приоритет 3: GitHub Releases, полная история через пагинацию.
  - id: gh_llamaindex
    type: github_releases
    url: https://github.com/run-llama/llama_index
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  - id: gh_crewai
    type: github_releases
    url: https://github.com/crewAIInc/crewAI
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 180
    min_expected_items: 20

  - id: gh_microsoft_agent_framework
    type: github_releases
    url: https://github.com/microsoft/agent-framework
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 360
    min_expected_items: 20

  - id: gh_semantic_kernel
    type: github_releases
    url: https://github.com/microsoft/semantic-kernel
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  - id: gh_pydantic_ai
    type: github_releases
    url: https://github.com/pydantic/pydantic-ai
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 180
    min_expected_items: 20

  - id: gh_haystack
    type: github_releases
    url: https://github.com/deepset-ai/haystack
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  - id: gh_dspy
    type: github_releases
    url: https://github.com/stanfordnlp/dspy
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 10

  - id: gh_temporal_server
    type: github_releases
    url: https://github.com/temporalio/temporal
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 15

  - id: gh_temporal_sdk_python
    type: github_releases
    url: https://github.com/temporalio/sdk-python
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 15

  # Средний размер тела релиза 11 464 знака: самые подробные release notes среза.
  - id: gh_ray
    type: github_releases
    url: https://github.com/ray-project/ray
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 10

  - id: gh_wandb
    type: github_releases
    url: https://github.com/wandb/wandb
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 15

  - id: gh_langfuse
    type: github_releases
    url: https://github.com/langfuse/langfuse
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 180
    min_expected_items: 20

  # Раздел Breaking Changes входит в шаблон релиза: 93 из 100 релизов за
  # 12 месяцев его содержат.
  - id: gh_weaviate
    type: github_releases
    url: https://github.com/weaviate/weaviate
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 360
    min_expected_items: 20

  # Постоянный раздел Deprecations с горизонтом: в v1.17.0 заранее объявлено
  # удаление RocksDB и старых search-эндпоинтов в v1.18.
  - id: gh_qdrant
    type: github_releases
    url: https://github.com/qdrant/qdrant
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 10

  - id: gh_chroma
    type: github_releases
    url: https://github.com/chroma-core/chroma
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 15

  - id: gh_milvus
    type: github_releases
    url: https://github.com/milvus-io/milvus
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  - id: gh_pinecone_python
    type: github_releases
    url: https://github.com/pinecone-io/pinecone-python-client
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 5

  # Тела релизов около 570 знаков, плотной ячейки не даёт. Оставлен как
  # единственный датированный след Braintrust: их docs/changelog не датирован вовсе.
  - id: gh_braintrust_sdk
    type: github_releases
    url: https://github.com/braintrustdata/braintrust-sdk
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 360
    min_expected_items: 10
```

## Предложения для `corpus.vendors`

```yaml
    - id: llamaindex
      label: "LlamaIndex"
      aliases: [LlamaIndex, llama_index, llama-index, "Llama Index", run-llama, LlamaIndexTS, LlamaCloud]
    - id: crewai
      label: "CrewAI"
      aliases: [CrewAI, crewai, crewAIInc, "Crew AI", "CrewAI AMP"]
    - id: microsoft_ai
      label: "Microsoft AI Frameworks"
      aliases: [
        "Semantic Kernel", semantic-kernel, SemanticKernel,
        "Agent Framework", "Microsoft Agent Framework", agent-framework,
        AutoGen, autogen, "AutoGen Studio", AgentChat
      ]
    - id: pydantic_ai
      label: "Pydantic AI"
      aliases: [
        "Pydantic AI", pydantic-ai, PydanticAI, pydantic_ai,
        Logfire, "Pydantic Logfire"
      ]
    - id: instructor
      label: "Instructor"
      aliases: [Instructor, instructor, "instructor-ai", useinstructor]
    - id: vercel_ai_sdk
      label: "Vercel AI SDK"
      aliases: [
        "Vercel AI SDK", "AI SDK", ai-sdk, "ai-sdk.dev", "@ai-sdk",
        Vercel, vercel
      ]
    - id: haystack
      label: "Haystack"
      aliases: [Haystack, haystack, deepset, "deepset-ai", "Haystack Enterprise", "deepset Cloud"]
    - id: dspy
      label: "DSPy"
      aliases: [DSPy, dspy, "DSPy framework", stanfordnlp]
    - id: temporal
      label: "Temporal"
      aliases: [Temporal, temporal, "Temporal Cloud", temporalio, "temporal.io"]
    - id: modal
      label: "Modal"
      aliases: [Modal, modal, "Modal Labs", modal-labs, "modal.com"]
    - id: ray
      label: "Ray"
      aliases: [Ray, ray, "Ray Serve", "Ray Data", "Ray Train", "ray-project", Anyscale, anyscale, KubeRay]
    - id: wandb
      label: "Weights & Biases"
      aliases: [
        "Weights & Biases", "Weights and Biases", wandb, "W&B",
        Weave, "W&B Weave", "W&B Server", "W&B Models"
      ]
    - id: langfuse
      label: "Langfuse"
      aliases: [Langfuse, langfuse, "langfuse.com"]
    - id: braintrust
      label: "Braintrust"
      aliases: [Braintrust, braintrust, braintrustdata, "braintrust.dev", "Braintrust Data"]
    - id: pinecone
      label: "Pinecone"
      aliases: [Pinecone, pinecone, "pinecone-io", "Pinecone Assistant", "Pinecone Inference", "Pinecone Database"]
    - id: weaviate
      label: "Weaviate"
      aliases: [Weaviate, weaviate, "Weaviate Cloud", WCD, "Weaviate Database", "Weaviate Agents", Engram]
    - id: qdrant
      label: "Qdrant"
      aliases: [Qdrant, qdrant, "Qdrant Cloud", "Qdrant Edge", "qdrant.tech"]
    - id: chroma
      label: "Chroma"
      aliases: [Chroma, chroma, ChromaDB, chromadb, "Chroma Cloud", trychroma, "chroma-core"]
    - id: milvus
      label: "Milvus"
      aliases: [Milvus, milvus, "milvus-io", Zilliz, zilliz, "Zilliz Cloud", "Zilliz Lakebase"]
    - id: pgvector
      label: "pgvector"
      aliases: [pgvector, "pg_vector", "pgvector extension", pgvectorscale]
    - id: supabase
      label: "Supabase"
      aliases: [Supabase, supabase, "Supabase AI", "Supabase Vector", "supabase.com"]
    - id: neon
      label: "Neon"
      aliases: [Neon, neon, "Neon Postgres", neondatabase, "neon.tech", "neon.com", Electric, PGlite]
```

Три замечания к словарю.

Первое. `microsoft_ai` собирает три проекта в одного вендора намеренно. AutoGen фактически влит
в Agent Framework, агентная часть Semantic Kernel — тоже; миграционные документы ссылаются друг
на друга. Раздельные `autogen` и `semantic_kernel` дадут три полупустые ячейки вместо одной
плотной. Если раздельность нужна по продуктовым соображениям, `microsoft_ai` придётся разбить,
и тогда AutoGen плотную ячейку не наберёт: три релиза за 12 месяцев.

Второе. `vercel` как alias у `vercel_ai_sdk` рискует притянуть новости о хостинге, не связанные
с SDK. Если `vercel.com/changelog` попадёт в источники, alias нужно убрать и оставить только
`ai-sdk`-формы.

Третье. Neon поглотил Electric и PGlite (объявление в changelog от 2026-08-14), поэтому обе
марки внесены в алиасы Neon. Проверить это стоит ещё раз, когда появятся первые записи корпуса:
если Electric сохранит отдельную ленту, alias придётся выделить.

## Отброшенные адреса

Записаны, чтобы их не проверяли повторно.

| Адрес | Что не так |
|---|---|
| `https://docs.llamaindex.ai/en/stable/CHANGELOG/` | 404, редирект на `developers.llamaindex.ai` |
| `https://www.braintrust.dev/docs/changelog` | 200, но записи не датированы ни в HTML, ни после рендеринга |
| `https://qdrant.tech/documentation/release-notes/` | 200, ноль дат: зеркало GitHub Releases с относительным временем |
| `https://docs.weaviate.io/weaviate/client-libraries/python/v3_v4_migration` | Молчаливый редирект на общую страницу клиента. Ловушка того же рода, что `docs.cursor.com/changelog` |
| `https://docs.trychroma.com/updates/migration`, `/updates/troubleshooting`, `/production/administration/migration` | 404 |
| `https://docs.temporal.io/releases`, `/references/server-release-notes` | 404 |
| `https://docs.ray.io/en/latest/ray-overview/deprecations.html`, `/whats-new.html` | 404 |
| `https://learn.microsoft.com/en-us/semantic-kernel/support/migration` и `/migration/` | 404: индексной страницы нет, только отдельные руководства |
| `https://docs.haystack.deepset.ai/docs/changelog` | 404 |
| `https://python.useinstructor.com/CHANGELOG/` | 404 |
| `https://dspy.ai/community/migration/` | 404 |
| `https://docs.pinecone.io/reference/api/deprecations` | 404: отдельного реестра отключений у Pinecone нет, всё в release notes |
| `https://docs.langchain.com/changelog` | 404; рабочий адрес `/langsmith/changelog`, но это зона второго разведчика |
| `https://raw.githubusercontent.com/deepset-ai/haystack/main/CHANGELOG.md` | 404 |
| `https://raw.githubusercontent.com/vercel/ai/main/packages/ai/CHANGELOG.md` | 200, 272 КБ, одна дата: формат changesets дат не хранит |
| `github.com/modal-labs/modal-client`, `github.com/neondatabase/neon` | Releases не ведутся либо прекращены |
| `github.com/vercel/ai` | 800 релизов укладываются в неделю, бэкфилл нереален |
| `github.com/run-llama/LlamaIndexTS` | Тела релизов 98 знаков, ломающих разделов нет |
