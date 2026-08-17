# Разведка источников: провайдеры моделей и инференса

Срез: Mistral, Cohere, Groq, Together AI, Fireworks AI, DeepSeek, xAI, Perplexity,
AWS Bedrock, Azure OpenAI, Google Vertex AI, Replicate, Hugging Face, OpenRouter,
Ollama, vLLM. Дата проверки — 17 августа 2026 года.

Каждый адрес из таблицы открыт вручную через `curl -sS -L` без JavaScript.
Столбец «дат» — число уникальных дат, вытащенных из отданного HTML регулярным
выражением по трём форматам: `YYYY-MM-DD`, `Month D, YYYY` и `M/D/YYYY`.
Третий формат добавлен по ходу разведки: страница моделей Mistral отдаёт таблицу
отключений именно в нём, и на исходном выражении она выглядела мёртвой.

---

## 1. Проверенные страницы

### Реестры отключений — первый приоритет

| Вендор | URL | Закрывает | HTTP | Размер | Уник. дат | Глубина | Доступ | Вывод |
|---|---|---|---|---|---|---|---|---|
| Together AI | `https://docs.together.ai/docs/deprecations` | deprecation | 200 | 513 КБ | 40 | 2024-08-22 … 2026-08-04 | curl | Брать. Таблица «Removal date / Model», 315 строк, 39 уникальных дат снятия, 220 строк за последние 540 дней. Плюс формальная политика: 2–3 недели уведомления, список активных редиректов моделей |
| Groq | `https://console.groq.com/docs/deprecations` | deprecation | 200 | 480 КБ | 38 | 2024-10-18 … 2026-08-16 | curl | Брать. 22 датированных объявления, у каждого дата письма, дата выключения и модель-замена. Эталон жанра наравне с реестром Anthropic |
| Azure OpenAI (Microsoft Foundry) | `https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule` | deprecation | 200 | 68 КБ | 118 | 2024-05-13 … 2028-02-20 | curl | Брать. 191 строка таблицы, из них 15 Deprecated, 23 Retired, 2 Legacy; 139 строк несут ISO-дату. Покрывает не только OpenAI, но и размещённые в Foundry модели Anthropic, Mistral, DeepSeek, Fireworks |
| Mistral | `https://docs.mistral.ai/models` | deprecation | 200 | 1226 КБ | 20 | 2024-11-30 … 2026-08-31 | curl | Брать. Раздел «Deprecated & retired models»: 43 строки, в каждой дата объявления и дата вывода; 33 строки с датой объявления за последние 540 дней. Даты в формате `M/D/YYYY` — парсер обязан его понимать |
| AWS Bedrock | `https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html` | deprecation, pricing | 200 | 27 КБ | 26 | 2026-01-30 … 2027-01-08 | curl | Брать. 20 пар «Legacy date / EOL date» плюс третья колонка «Public extended access start date». Отдельная ценность: политика прямо обещает повышение цены в период продлённого доступа, то есть строка таблицы одновременно событие типа pricing |
| Google (Vertex AI → Gemini Enterprise Agent Platform) | `https://cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions` | deprecation | 200 | 364 КБ | 42 | 2023-06-07 … 2027-04-01 | curl, редирект | Брать, но записывать канонический адрес назначения. Ведёт на `docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions`. Таблицы «Latest available models» и «Retired models», 13 уникальных дат вывода |
| Cohere | `https://docs.cohere.com/docs/deprecations` | deprecation | 200 | 636 КБ | 9 | 2024-12-02 … 2026-04-04 | curl | Брать. Пять датированных объявлений, три из них внутри восемнадцатимесячного окна. Внутри объявлений таблицы с ценой снимаемой модели и заменой |
| Google (Vertex AI, generative-ai) | `https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations` | deprecation | 200 | 108 КБ | 3 | 2025-06-24 … 2026-06-24 | curl | Не брать. Одна запись во всей таблице — вывод модуля Generative AI из Vertex AI SDK. Сама страница помечена как более не обновляемая |

### Changelog с разделами ломающих изменений и отключений

| Вендор | URL | Закрывает | HTTP | Размер | Уник. дат | Глубина | Доступ | Вывод |
|---|---|---|---|---|---|---|---|---|
| Together AI | `https://docs.together.ai/docs/changelog` | deprecation, pricing, limits, release | 200 | 1117 КБ | 105 | 2025-07-08 … 2026-08-17 | curl | Брать. 109 датированных записей за 13 месяцев: 30 про отключения, 5 про изменение цены, 3 про лимиты. Единственный источник среза, который сам по себе закрывает три ячейки |
| Fireworks AI | `https://docs.fireworks.ai/updates/changelog` | deprecation, release | 200 | 573 КБ | 31 | 2025-05-19 … 2026-08-14 | curl | Брать. 29 записей, 8 из них озаглавлены «Serverless deprecation» с датой и картой миграции. Адрес `docs.fireworks.ai/changelog` отдаёт 404, рабочий путь — `/updates/changelog` |
| AWS Bedrock | `https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-ug-doc-history.html` | release, breaking_change | 200 | 106 КБ | 175 | 2023-09-28 … 2026-08-15 | curl, редирект | Брать вторым номером. Таблица «Change / Description / Date», самая глубокая история в срезе. Минус — половина записей про изменения самой документации, а не продукта. Запрошенный `doc-history.html` редиректит на `bedrock-ug-doc-history.html` |
| Mistral | `https://docs.mistral.ai/resources/changelogs` | release, deprecation | 200 | 1387 КБ | 55 | 2024-01-11 … 2026-07-16 | curl | Брать. 54 датированные записи с 2024 года. В разметке `id="date-YYYY-MM-DD"` и `data-changelog-entry="true"` — структура для парсера идеальная. Содержание преимущественно релизное, ценность как фон к таблице отключений |
| Cohere | `https://docs.cohere.com/v2/changelog` | release | 200 | 1789 КБ | 121 | 2022-10-18 … 2026-07-07 | curl, с оговоркой | Брать с осторожностью. В сыром HTML 58 ISO-дат, но в DOM после вырезания `<script>` их остаётся 10–11: страница подгружает историю, а curl видит только последнюю страницу плюс оглавление. Для ежедневного сбора хватает, для бэкфилла нет |
| Groq | `https://console.groq.com/docs/changelog` | release, deprecation | 200 | 557 КБ | 46 | 2025-04-14 … 2026-04-18 | curl, нужен особый разбор | Брать с оговоркой. В DOM даты выглядят как «Apr 18» без года; ISO-дата лежит только в служебном JSON Next.js внутри `<script>`. Стандартный обход DOM даст запись без года. Последняя запись — апрель 2026, страница подзаброшена |
| DeepSeek | `https://api-docs.deepseek.com/updates` | pricing, release | 200 | 44 КБ | 21 | 2024-05-17 … 2026-08-16 | curl | Брать. 19 записей с заголовком `Date: YYYY-MM-DD`, лёгкая страница. Три записи прямо про цену, включая переход на пиковый и внепиковый тариф с 16 августа 2026 года. Отдельная страница `quick_start/pricing` дат не содержит вовсе |
| Google (Vertex AI) | `https://cloud.google.com/vertex-ai/generative-ai/docs/release-notes` | release | 200 | 283 КБ | 184 | 2024-03-29 … 2026-08-11 | curl, редирект | Брать третьим номером. Очень объёмно и датировано, но это в основном релизы. Есть архив: `.../docs/release-notes-archive`, 36 дат |
| Replicate | `https://replicate.com/changelog` | release | 200 | 386 КБ | 234 | 2022-03-28 … 2026-04-21 | curl | Брать низким приоритетом. 127 датированных записей, заголовки размечены как `<h2 id="2026-04-21-...">`. Ни одной плотной ячейки не закрывает: слово «deprecat» встречается 8 раз за четыре года. Последняя запись — апрель 2026 |
| Perplexity | `https://docs.perplexity.ai/docs/resources/changelog.md` | deprecation, pricing | 200 | 41 КБ | 9 | апрель 2024 … август 2026 | curl | Брать. 46 записей, размеченных тегами: Deprecation — 7, Pricing — 3, Security — 2, Rate Limits — 1. Точность дат месячная: `label="July 2026"`, дня нет. HTML-версия той же страницы весит 726 КБ и даёт то же содержание |
| OpenRouter | `https://openrouter.ai/docs/changelog` | breaking_change | 200 | 469 КБ | 9 | 2026-07-03 … 2026-08-08 | curl | Брать для ежедневного сбора, не для бэкфилла. Автогенерируемый diff OpenAPI, у записей явный раздел «Breaking changes» с человеческим комментарием о миграции — по типу изменения это лучший источник среза. Но страница держит только последние девять выпусков, то есть примерно шесть недель |
| xAI | `https://docs.x.ai/developers/release-notes` | release, pricing | 200 | 530 КБ | 5 | ноябрь 2024 … август 2026 | нужен рендеринг | Брать низким приоритетом. `docs.x.ai/docs/changelog` — 404. Канонический адрес отдаёт 200, но curl достаёт только пять дат: записи сгруппированы заголовками месяцев без дня, и сами заголовки приходят при рендеринге. TabStack вытаскивает всё, глубина с ноября 2024 года. Точность дат месячная |
| Hugging Face | `https://huggingface.co/changelog` | — | 200 | 153 КБ | 0 | — | нужен рендеринг | Не брать. curl не даёт ни одной даты. TabStack вытаскивает записи в формате «Aug 12, 26», но содержание — функции Хаба (доступы, метрики трафика, MCP), а не изменения Inference Providers. Отдельного changelog у Inference Providers нет: `docs/inference-providers/changelog` — 404, страница `index` дат не содержит |

### GitHub Releases

| Вендор | URL | Закрывает | HTTP | Размер | Уник. дат | Глубина | Доступ | Вывод |
|---|---|---|---|---|---|---|---|---|
| vLLM | `https://github.com/vllm-project/vllm/releases` | breaking_change, deprecation | 200 | 1732 КБ | 10 на странице | 2025-06-10 … 2026-08-11 (40 выпусков) | github_releases | Брать. Единственный источник среза, который уверенно закрывает ячейку breaking_change. В 40 последних выпусках 28 несут содержание про ломающие изменения или отключения, у 10 есть отдельный раздел «Breaking Changes & Deprecations» со списком снятых флагов и переименований. Тела крупных выпусков — от 14 до 124 КБ |
| Ollama | `https://github.com/ollama/ollama/releases` | release | 200 | 531 КБ | 9 на странице | 2026-07-11 … 2026-08-15 (15 выпусков) | github_releases | Не брать. Выпуски частые, но тела от 37 до 1400 символов и по содержанию это перечень новых моделей и исправлений. Ни одной плотной ячейки; ровно тот случай, когда «вендор выпустил версию четырнадцать раз» не является наблюдением |

### Проверено и отброшено

| URL | HTTP | Дат | Причина |
|---|---|---|---|
| `https://docs.together.ai/changelog` | 404 | 0 | Неверный путь. Рабочий — `/docs/changelog` |
| `https://docs.fireworks.ai/changelog` | 404 | 0 | Неверный путь. Рабочий — `/updates/changelog` |
| `https://docs.x.ai/docs/changelog` | 404 | 2 | Неверный путь. Рабочий — `/developers/release-notes` |
| `https://huggingface.co/docs/inference-providers/changelog` | 404 | 0 | Страницы не существует |
| `https://replicate.com/docs/reference/deprecations` | 404 | 0 | Страницы не существует |
| `https://api-docs.deepseek.com/news` | 200 | 0 | Витрина анонсов без дат в разметке |
| `https://api-docs.deepseek.com/quick_start/pricing` | 200 | 0 | Текущий прайс без истории |
| `https://console.groq.com/docs/rate-limits` | 200 | 0 | Текущие лимиты без истории |
| `https://docs.together.ai/docs/serverless/rate-limits` | 200 | 1 | Текущие лимиты без истории |
| `https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle` | 200 | 14 | Даты — это идентификаторы версий API (`2024-03-01`), а не даты событий |
| `https://learn.microsoft.com/en-us/azure/ai-foundry/openai/whats-new` | 200 | 13 | Редиректит на `foundry-classic`, заголовки месячные, дублирует расписание отключений |
| `https://openrouter.ai/docs/api_reference/versioning` | 200 | 1 | Политика без истории |
| `https://docs.mistral.ai/resources/release-notes` | 200 | 1 | Пусто без рендеринга, дублирует changelog |
| `https://console.groq.com/docs/changelog/rss.xml` | 404 | 0 | RSS у Groq в разметке страницы не объявлен, хотя кнопка «RSS» на ней есть |

### Две ловушки редиректа

Помимо уже описанного в конфиге случая с Cursor, в этом срезе встретились ещё две.

Первая: `cloud.google.com/vertex-ai/generative-ai/...` целиком переехал на
`docs.cloud.google.com/...`, а часть страниц — ещё и в новое дерево
`gemini-enterprise-agent-platform`. Сама страница отключений Vertex AI при этом
несёт баннер «Vertex AI documentation is no longer being updated». Запрос по
старому адресу возвращает 200 и содержательную страницу, но писать в конфиг надо
адрес назначения, иначе однажды придёт 200 с чужим содержанием.

Вторая: `learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements`
отдаёт 200 и всего 14 дат, потому что после переезда на `azure/foundry/...` эта
страница стала описанием политики жизненного цикла, а таблица переехала на
соседнюю `model-retirement-schedule` со 118 датами. Отличить одно от другого по
коду ответа нельзя — только по содержанию.

### Зеркала в Markdown

Документация на Mintlify (Together AI, Fireworks, Cohere, Perplexity, OpenRouter)
отдаёт любую страницу в чистом Markdown, если дописать к пути `.md`. Проверено:

| Адрес | Размер HTML | Размер `.md` | Дат в `.md` |
|---|---|---|---|
| `https://docs.together.ai/docs/changelog.md` | 1117 КБ | 81 КБ | 104 |
| `https://docs.together.ai/docs/deprecations.md` | 513 КБ | 48 КБ | 39 |
| `https://docs.fireworks.ai/updates/changelog.md` | 573 КБ | 45 КБ | 30 |
| `https://openrouter.ai/docs/changelog.md` | 469 КБ | 25 КБ | 8 |
| `https://docs.cohere.com/docs/deprecations.md` | 636 КБ | 6 КБ | 8 |

Дат столько же, объём меньше в десять–сто раз, разметка `<Update label="...">`
вокруг каждой записи разбирается одной регуляркой. Текущий адаптер `html_page`
разбирает DOM через BeautifulSoup и на Markdown развалится, поэтому в конфиг ниже
записаны HTML-адреса. Если когда-нибудь появится `parser_hint: dated_markdown`,
переключение на зеркала снимет с этих пяти источников около 2,7 МБ трафика за
прогон и заодно снимет проблему с датами в служебном JSON.

---

## 2. Сколько ячеек закрывается

Считались события с датой внутри окна 6–18 месяцев назад от 17 августа 2026 года.
Порог ячейки — три события.

**deprecation — закрывается у десяти вендоров.**

| Вендор | Событий | Источник |
|---|---|---|
| Groq | 15 датированных объявлений | реестр отключений |
| Together AI | 30 записей changelog плюс 39 дат снятия в реестре | оба |
| Mistral | 33 строки с датой объявления | таблица на странице моделей |
| Azure OpenAI | 38 строк Deprecated и Retired | расписание отключений |
| AWS Bedrock | 20 пар Legacy/EOL | таблица жизненного цикла |
| Google | 13 дат вывода моделей | таблица версий |
| Fireworks AI | 8 записей | changelog |
| Perplexity | 7 записей с тегом Deprecation | changelog |
| vLLM | 20+ выпусков с разделом об отключениях | GitHub Releases |
| Cohere | 3 объявления | реестр отключений |

**breaking_change — закрывается уверенно у одного вендора.**

vLLM: десять выпусков за четырнадцать месяцев с явным разделом «Breaking Changes
& Deprecations». OpenRouter набирает три раздела «Breaking changes», но все три
уложились в шесть недель — по числу событий ячейка закрыта, по глубине нет.
У остальных ломающие изменения не выделены в отдельную рубрику и извлекаются
только смысловой разметкой на стадии обогащения.

**pricing — закрывается у двух вендоров.**

Together AI: пять записей об изменении цены с апреля по июнь 2026 года.
Perplexity: три записи с тегом Pricing (март и апрель 2025, июль 2026), точность
месячная. Ещё два кандидата не дотягивают: DeepSeek даёт две ценовые записи
внутри окна (декабрь 2025 и август 2026, вторая — переход на пиковый и
внепиковый тариф), xAI — три, но две из них суть цены на запуске новой модели,
а не изменение цены.

**limits — закрывается у одного вендора, и то впритык.**

Together AI: ровно три записи (сентябрь 2025, апрель 2026, июль 2026). У
остальных страницы лимитов показывают текущее состояние без истории — проверено
у Groq, Together AI и Perplexity. Это системное свойство жанра, а не пробел
разведки: лимиты меняют молча.

**Итого: 14 плотных ячеек** — десять по deprecation, одна по breaking_change,
две по pricing, одна по limits. Порог конфига (`min_vendors_with_dense_cell: 3`)
перекрывается по deprecation более чем втрое; узкое место — breaking_change,
pricing и limits, и оно узкое у самих вендоров, а не у разведки.

---

## 3. Пять лучших адресов по убыванию ценности

1. **`https://docs.together.ai/docs/deprecations`** — 39 уникальных дат снятия
   с августа 2024 по август 2026, 220 строк внутри полутора лет, curl без
   оговорок. Плюс формальная политика уведомления и список активных редиректов
   моделей: редирект — это тихое ломающее изменение, и здесь он задокументирован.
2. **`https://console.groq.com/docs/deprecations`** — 22 датированных
   объявления, у каждого три даты (письмо, выключение, факт) и модель-замена.
   По структуре ближе всего к реестру Anthropic, который уже принят за образец.
3. **`https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule`**
   — 118 уникальных дат, 191 строка, 38 из них в состоянии Deprecated или
   Retired. Одна страница закрывает отключения сразу по нескольким чужим
   вендорам, размещённым в Foundry.
4. **`https://docs.together.ai/docs/changelog`** — 109 датированных записей за
   тринадцать месяцев, из них 30 про отключения, 5 про цену, 3 про лимиты.
   Единственная страница среза, которая в одиночку закрывает три ячейки.
5. **`https://github.com/vllm-project/vllm/releases`** — единственный источник,
   который закрывает breaking_change по-настоящему: явный раздел со списком
   снятых флагов, переименованных параметров и убранных переменных окружения,
   десять выпусков за четырнадцать месяцев.

Следом, почти вплотную: таблица жизненного цикла AWS Bedrock, таблица версий
Google, таблица отключений на странице моделей Mistral, changelog Fireworks.

---

## 4. Фрагмент для секции `sources`

```yaml
  # --- Провайдеры моделей и инференса ---
  # Приоритет 1: официальные реестры отключений. Все проверены curl без JS
  # 17 августа 2026 года, даты присутствуют в отданном HTML.

  - id: together_deprecations
    type: html_scrape
    url: https://docs.together.ai/docs/deprecations
    priority: 1
    enabled: true
    parser_hint: dated_table
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  - id: groq_deprecations
    type: html_scrape
    url: https://console.groq.com/docs/deprecations
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 10

  # Канонический адрес после переезда ai-foundry -> foundry. Соседняя страница
  # concepts/model-retirements отдаёт 200 и всего 14 дат: там осталась политика,
  # а таблица уехала сюда. Отличается только содержанием, не кодом ответа.
  - id: azure_model_retirement_schedule
    type: html_scrape
    url: https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule
    priority: 1
    enabled: true
    parser_hint: dated_table
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 30

  # Таблица «Deprecated & retired models» внизу страницы моделей. Даты в
  # формате M/D/YYYY: без поддержки этого формата страница выглядит мёртвой.
  - id: mistral_model_deprecations
    type: html_scrape
    url: https://docs.mistral.ai/models
    priority: 1
    enabled: true
    parser_hint: dated_table
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 15

  # Колонки Legacy date / EOL date / Public extended access start date.
  # Продлённый доступ по политике AWS идёт по повышенной цене, поэтому строка
  # таблицы — событие и deprecation, и pricing одновременно.
  - id: bedrock_model_lifecycle
    type: html_scrape
    url: https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html
    priority: 1
    enabled: true
    parser_hint: dated_table
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 10

  # Запрошенный cloud.google.com/vertex-ai/... редиректит сюда: Vertex AI
  # переехал в Gemini Enterprise Agent Platform, старое дерево заморожено.
  - id: google_model_versions
    type: html_scrape
    url: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions
    priority: 1
    enabled: true
    parser_hint: dated_table
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 10

  - id: cohere_deprecations
    type: html_scrape
    url: https://docs.cohere.com/docs/deprecations
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 3

  # Приоритет 1: changelog, в которых отключения и цены — отдельные рубрики.

  - id: together_changelog
    type: html_scrape
    url: https://docs.together.ai/docs/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 400
    min_expected_items: 40

  # docs.fireworks.ai/changelog — 404. Рабочий путь: /updates/changelog.
  - id: fireworks_changelog
    type: html_scrape
    url: https://docs.fireworks.ai/updates/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 450
    min_expected_items: 15

  # Автогенерируемый diff OpenAPI с человеческим комментарием о миграции в
  # каждом разделе «Breaking changes». Страница держит только последние девять
  # выпусков (около шести недель), поэтому бэкфилл выключен.
  - id: openrouter_api_changelog
    type: html_scrape
    url: https://openrouter.ai/docs/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: false
    backfill_depth_days: 60
    min_expected_items: 5

  - id: deepseek_changelog
    type: html_scrape
    url: https://api-docs.deepseek.com/updates
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 8

  # Записи размечены тегами Deprecation, Pricing, Rate Limits, Security.
  # Точность дат месячная: label="July 2026", дня в разметке нет.
  - id: perplexity_changelog
    type: html_scrape
    url: https://docs.perplexity.ai/docs/resources/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 10

  # Приоритет 2: changelog с релизным содержанием и полезным фоном.

  # Разметка id="date-YYYY-MM-DD" и data-changelog-entry="true".
  - id: mistral_changelog
    type: html_scrape
    url: https://docs.mistral.ai/resources/changelogs
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  # В сыром HTML 58 ISO-дат, в DOM после вырезания script остаётся 10-11:
  # история подгружается. Для ежедневного сбора хватает, для бэкфилла нет.
  - id: cohere_changelog
    type: html_scrape
    url: https://docs.cohere.com/v2/changelog
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: false
    backfill_depth_days: 90
    min_expected_items: 5

  # Заголовки в DOM выглядят как «Apr 18», без года; ISO-дата лежит только в
  # служебном JSON Next.js внутри <script>. Обычный обход DOM даст запись без
  # года, которую адаптер обязан пометить INFERRED, а не достроить текущим.
  # Последняя запись — апрель 2026, страница подзаброшена.
  - id: groq_changelog
    type: html_scrape
    url: https://console.groq.com/docs/changelog
    priority: 2
    enabled: false
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 480
    min_expected_items: 10

  # Самая глубокая история в срезе, но половина записей про правки самой
  # документации. Запрошенный doc-history.html редиректит на этот адрес.
  - id: bedrock_doc_history
    type: html_scrape
    url: https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-ug-doc-history.html
    priority: 2
    enabled: true
    parser_hint: dated_table
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 30

  - id: google_vertex_release_notes
    type: html_scrape
    url: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/release-notes
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 30

  # Ни одной плотной ячейки: «deprecat» встречается 8 раз за четыре года.
  # Берётся как фон по релизам, последняя запись — апрель 2026.
  - id: replicate_changelog
    type: html_scrape
    url: https://replicate.com/changelog
    priority: 3
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  # Приоритет 3: GitHub Releases.
  # В 40 последних выпусках 28 несут ломающие изменения или отключения,
  # у 10 есть отдельный раздел «Breaking Changes & Deprecations».
  # Единственный источник среза, закрывающий ячейку breaking_change.
  - id: gh_vllm
    type: github_releases
    url: https://github.com/vllm-project/vllm
    priority: 3
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 15
```

Не включено сознательно:

- `https://github.com/ollama/ollama` — выпуски частые, тела от 37 до 1400
  символов, содержание релизное. Плотных ячеек не даёт.
- `https://huggingface.co/changelog` — без рендеринга нуль дат, с рендерингом
  содержание про Хаб, а не про Inference Providers.
- `https://docs.x.ai/developers/release-notes` — требует рендеринга, точность
  дат месячная. Стоит вернуться, если появится адаптер с рендерингом.

---

## 5. Предложения для `corpus.vendors`

Существующая запись `google` уже содержит алиасы `"Vertex AI"` и `vertex`.
Её стоит расширить: Vertex AI переехал в Gemini Enterprise Agent Platform, и
без нового алиаса записи из переехавшего дерева не сматчатся.

```yaml
    # Расширение существующей записи
    - id: google
      label: "Google"
      aliases: [Google, Gemini, gemini, "Google AI", "Vertex AI", vertex,
                "Gemini Enterprise Agent Platform", "Agent Platform",
                "Google Cloud", gcp, "Generative AI on Vertex AI"]

    # Новые вендоры
    - id: mistral
      label: "Mistral AI"
      aliases: [Mistral, "Mistral AI", mistralai, "La Plateforme", Ministral,
                Magistral, Devstral, Voxtral, Codestral, Pixtral]

    - id: cohere
      label: "Cohere"
      aliases: [Cohere, cohere, Command, "Command R", "Command A", Rerank,
                "Cohere Labs", Aya, North]

    - id: groq
      label: "Groq"
      aliases: [Groq, GroqCloud, groq, "Groq API", "groq-sdk"]

    - id: together
      label: "Together AI"
      aliases: ["Together AI", Together, together, "together.ai", "Together API",
                "Together Inference", togethercomputer]

    - id: fireworks
      label: "Fireworks AI"
      aliases: ["Fireworks AI", Fireworks, fireworks, "fireworks.ai", firectl,
                FireOptimizer, FireFunction]

    - id: deepseek
      label: "DeepSeek"
      aliases: [DeepSeek, deepseek, "DeepSeek API", "deepseek-ai", "DeepSeek-V3",
                "DeepSeek-V4", "DeepSeek-R1"]

    - id: xai
      label: "xAI"
      aliases: [xAI, "x.ai", XAI, Grok, grok, "Grok API", "Grok Build", SpaceXAI]

    - id: perplexity
      label: "Perplexity"
      aliases: [Perplexity, perplexity, "Perplexity API", "Sonar", "pplx",
                "Perplexity Sonar", "Router API"]

    - id: aws
      label: "AWS Bedrock"
      aliases: [AWS, "Amazon Bedrock", Bedrock, bedrock, "Amazon Web Services",
                "AWS Bedrock", "bedrock-runtime", "Amazon Nova"]

    - id: microsoft
      label: "Microsoft Azure AI"
      aliases: [Azure, "Azure OpenAI", "Azure OpenAI Service", Microsoft,
                "Microsoft Foundry", "Azure AI Foundry", "Foundry Models",
                "AI Foundry"]

    - id: replicate
      label: "Replicate"
      aliases: [Replicate, replicate, "replicate.com", "Replicate API"]

    - id: huggingface
      label: "Hugging Face"
      aliases: ["Hugging Face", HuggingFace, huggingface, "HF Hub",
                "Inference Providers", "huggingface_hub", "hf.co"]

    - id: openrouter
      label: "OpenRouter"
      aliases: [OpenRouter, openrouter, "openrouter.ai", "OpenRouter API"]

    - id: vllm
      label: "vLLM"
      aliases: [vLLM, vllm, "vllm-project", "vLLM V1", "vLLM engine"]

    - id: ollama
      label: "Ollama"
      aliases: [Ollama, ollama, "Ollama CLI", "ollama serve"]
```

Три замечания к нормализации.

Первое: `microsoft` и `aws` названы по компании, а не по продукту, потому что
расписание отключений Azure охватывает и модели OpenAI, и Anthropic, и Mistral, и
DeepSeek, и Fireworks, размещённые в Foundry. Одна запись Azure о выводе
`gpt-4o` — это событие Microsoft, а не OpenAI: даты Azure и даты OpenAI не
совпадают. То же у Bedrock, где документация прямо предупреждает, что даты
жизненного цикла специфичны для Bedrock и отличаются от дат провайдера модели.

Второе: `ollama` и `huggingface` внесены в словарь, хотя источников по ним я не
рекомендую. Вендор в словаре нужен раньше источника: эти имена регулярно
встречаются в чужих changelog (Together, Fireworks, Groq упоминают выкладку
весов на Hugging Face), и без записи в словаре такие упоминания дадут пустой
`vendor`.

Третье: алиасы вроде `Grok`, `Command`, `North` — обычные слова. Если сопоставление
идёт простым вхождением подстроки без учёта регистра, `Command` даст ложные
срабатывания на любом упоминании командной строки. Либо матчить регистрозависимо
и по границам слова, либо убрать односложные алиасы и оставить составные.
