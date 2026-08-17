# Разведка источников: RSS/Atom и Telegram

Дата проверки — 17 августа 2026 года. Закрываются два типа адаптеров: `rss` и `telegram_channel`.
Типы `html_scrape` и `github_releases` уже покрыты и здесь не пересматриваются, за одним
исключением: у двух действующих html-источников нашлись официальные ленты, и об этом сказано
отдельно.

## Как проверялось

Каждый адрес открыт `curl` без JavaScript, с записью кода ответа и объёма тела. Ленты затем
разобраны через `feedparser` из `.venv` проекта: считалось число записей, доля записей с
`published_parsed`, крайние даты в одной выдаче и медианная длина тела записи после снятия
разметки. Telegram-превью считались по числу уникальных `data-post` и по атрибутам `datetime`.

Проверено 90 адресов лент и 63 Telegram-канала. В таблицы вынесены те, по которым есть что
сказать; полностью мёртвые догадки опущены, кроме тех, что важны как предупреждение.

Роль обоих типов в проекте вторичная. Бэкфилл у них не предполагается, накопление идёт
естественно в ежедневных прогонах, а правило SRC-2 запрещает публиковать факт на одном лишь
материале приоритета 5.

## Таблица 1. Ленты RSS и Atom

Столбец «Записей» — сколько записей отдаёт одна выдача. «Глубина» — крайние даты внутри этой же
выдачи. «Текст» — что лежит в записи: полное тело материала или сниппет.

### Рекомендую включить

| Вендор или издание | URL ленты | HTTP | Записей | Глубина по датам | Текст | Вывод |
| --- | --- | --- | --- | --- | --- | --- |
| Anthropic, Claude Platform release notes | `https://platform.claude.com/docs/en/release-notes/feed.xml` | 200, 157 КБ | 130, все датированы | 2024-05-10 — 2026-08-11 | Полный текст заметки в `description`, медиана 258 знаков | Лучшая находка. Официальная лента изменений API, SDK и Консоли. Содержит цены и сроки: например, запись за 10 августа отменяет ранее назначенное повышение Sonnet 5 с 2/10 до 3/15 долларов за MTok. Приоритет 2 |
| Cursor, changelog | `https://cursor.com/changelog/rss.xml` | 200, 126 КБ | 50, все датированы | 2026-02-12 — 2026-08-17 | Полный текст в `content:encoded`, медиана 1264 знака | Сильнее действующего html-скрейпа, у которого `min_expected_items: 3`. Приоритет 2 |
| GitHub, Changelog | `https://github.blog/changelog/feed/` | 200, 53 КБ | 10, все датированы | 2026-08-11 — 2026-08-14 | Полный текст, медиана 2110 знаков | Плотная лента, десять записей покрывают четыре дня. Приоритет 2 |
| Model Context Protocol, блог | `https://blog.modelcontextprotocol.io/index.xml` | 200, 420 КБ | 24, все датированы | 2025-07-02 — 2026-07-28 | Полный текст, медиана 7324 знака | Единственный официальный текстовый канал MCP помимо репозиториев. Записей мало, но каждая содержательна. Приоритет 2 |
| OpenAI, News | `https://openai.com/news/rss.xml` | 200, 689 КБ | 1132, все датированы | 2015-12-11 — 2026-08-17 | Сниппет, медиана 150 знаков | Официальная лента с исключительной глубиной. Тело придётся добирать со страницы. Приоритет 2 |
| n8n, блог | `https://n8n.io/blog/rss/` | 200, 219 КБ | 15, все датированы | 2026-08-03 — 2026-08-14 | Полный текст, медиана 10713 знаков | Единственная работающая лента n8n. Уклон маркетинговый, фильтру придётся отсеивать много. Приоритет 2 |
| Simon Willison, тег llms | `https://simonwillison.net/tags/llms.atom` | 200, 205 КБ | 30, все датированы | 2026-08-01 — 2026-08-16 | Полный текст, медиана 1193 знака | Самое точное издание по теме: релизы моделей, изменения API, поведение инструментов. Приоритет 4 |
| Hacker News, поисковая лента | `https://hnrss.org/newest?q=Claude+OR+OpenAI+OR+Cursor&count=50` | 200, 40 КБ | 50, все датированы | 2026-08-15 — 2026-08-17 | Заголовок и ссылка, медиана 156 знаков | Агрегатор для полноты охвата. Параметр `count` работает, запрос настраивается. Приоритет 5 |

### Работают, но включать с оговоркой

| Вендор или издание | URL ленты | HTTP | Записей | Глубина по датам | Текст | Вывод |
| --- | --- | --- | --- | --- | --- | --- |
| Google, блог Gemini | `https://blog.google/products/gemini/rss/` | 200, 30 КБ | 20, все датированы | 2026-07-21 — 2026-08-17 | Сниппет, медиана 104 знака | Работает, но сниппет короче анонса. Годится как сигнал, не как материал |
| Google, блог AI | `https://blog.google/technology/ai/rss/` | 200, 32 КБ | 20, все датированы | 2026-06-17 — 2026-08-17 | Сниппет, медиана 118 знаков | То же самое, шире по теме и потому шумнее |
| Cursor, форум, раздел announcements | `https://forum.cursor.com/c/announcements.rss` | 200, 85 КБ | 25, все датированы | 2026-05-07 — 2026-08-17 | Полный текст первого сообщения, медиана 1430 знаков | Официальные объявления: доступность моделей, тарифы, ответ на отчёт Mindgard. Наполовину дублирует changelog |
| Anthropic, статус | `https://status.anthropic.com/history.rss` | 200, 34 КБ | 25, все датированы | 2026-07-25 — 2026-08-17 | Хронология инцидента, медиана 346 знаков | Инциденты, а не изменения. В словарь `change_types` ложатся только как `other`. Брать, если нужны условия эксплуатации |
| OpenAI, статус | `https://status.openai.com/history.rss` (то же по `.atom`) | 200, 97 КБ | 92, все датированы | 2026-05-20 — 2026-08-13 | Медиана 134 знака | То же ограничение, но глубже по истории |
| Cursor, статус | `https://status.cursor.com/history.rss` | 200, 32 КБ | 25, все датированы | 2026-07-20 — 2026-08-17 | Медиана 333 знака | То же ограничение |
| GitHub, статус | `https://www.githubstatus.com/history.rss` | 200, 94 КБ | 25, все датированы | 2026-07-24 — 2026-08-17 | Медиана 1415 знаков | То же ограничение, тело подробнее прочих |
| The Register, раздел AI/ML | `https://www.theregister.com/software/ai_ml/headlines.atom` | 200, 320 КБ | 50, все датированы | 2026-04-29 — 2026-08-17 | Полный текст, медиана 3999 знаков | Издание с полным телом и приличной глубиной. Много отраслевых новостей, которые тема исключает. Приоритет 4 |
| The New Stack, раздел AI | `https://thenewstack.io/category/ai/feed/` | 200, 298 КБ | 26, все датированы | 2026-07-29 — 2026-08-17 | Полный текст, медиана 5610 знаков | Ближе к инструментам, чем к индустрии. Приоритет 4 |
| InfoQ, AI/ML/Data | `https://feed.infoq.com/ai-ml-data-eng/` | 200, 27 КБ | 15, все датированы | 2026-08-12 — 2026-08-17 | Сниппет, медиана 375 знаков | Работает, но тела нет и тема шире нужной |
| Simon Willison, тег anthropic | `https://simonwillison.net/tags/anthropic.atom` | 200, 319 КБ | 30, все датированы | 2026-06-16 — 2026-08-16 | Медиана 2099 знаков | Пересекается с лентой `llms`. Брать одну из двух |
| GitHub Changelog, метка copilot | `https://github.blog/changelog/label/copilot/feed/` | 200, 52 КБ | 10, все датированы | 2026-08-07 — 2026-08-14 | Полный текст, медиана 1630 знаков | Подмножество общей ленты changelog. Смысл только если общая окажется шумной |

### Проверены и отклонены

| Вендор или издание | URL ленты | HTTP | Записей | Глубина по датам | Текст | Вывод |
| --- | --- | --- | --- | --- | --- | --- |
| Vercel, changelog | `https://vercel.com/atom` (равно `https://vercel.com/changelog/rss.xml`) | 200, 3,2 МБ | 1473, все датированы | 2016-10-25 — 2026-08-17 | Полный текст, медиана 662 знака | Технически отличная лента с десятилетней историей. Но `vercel` отсутствует в `corpus.vendors`, и записи не пройдут обязательный фильтр ретривала. Включать только вместе с правкой словаря |
| Supabase, блог | `https://supabase.com/rss.xml` | 200, 159 КБ | 419, все датированы | 2020-05-01 — 2026-08-07 | Сниппет, медиана 77 знаков | То же: вендора нет в словаре, плюс тело пустое по существу |
| Ars Technica, AI | `https://arstechnica.com/ai/feed/` | 200, 71 КБ | 20, все датированы | 2026-08-06 — 2026-08-17 | Полный текст, медиана 1002 знака | Новости про ИИ вообще, что тема исключает прямо |
| The Verge, AI | `https://www.theverge.com/rss/ai-artificial-intelligence/index.xml` | 200, 29 КБ | 10, все датированы | 2026-08-13 — 2026-08-17 | Медиана 684 знака | То же |
| TechCrunch, AI | `https://techcrunch.com/category/artificial-intelligence/feed/` | 200, 18 КБ | 20, все датированы | 2026-08-13 — 2026-08-17 | Сниппет, медиана 109 знаков | То же, плюс сильный уклон в инвестиции и кадры |
| ZDNet, AI | `https://www.zdnet.com/topic/artificial-intelligence/rss.xml` | 200, 12 КБ | 20, все датированы | 2026-08-13 — 2026-08-17 | Сниппет, медиана 133 знака | То же |
| VentureBeat, AI | `https://venturebeat.com/category/ai/feed/` | 200, 121 КБ | 7, все датированы | 2026-01-07 — 2026-05-19 | Медиана 13028 знаков | Лента застыла три месяца назад. Не брать |
| Latent Space | `https://www.latent.space/feed` | 200, 957 КБ | 20, все датированы | 2026-07-28 — 2026-08-15 | Полный текст, медиана 17142 знака | Эссе и расшифровки подкаста. Мнения, а не изменения |
| Import AI | `https://importai.substack.com/feed` | 200, 458 КБ | 20, все датированы | 2026-03-23 — 2026-08-17 | Медиана 16852 знака | То же |
| The Pragmatic Engineer | `https://newsletter.pragmaticengineer.com/feed` | 200, 694 КБ | 20, все датированы | 2026-06-16 — 2026-08-14 | Медиана 11969 знаков | Тема инженерных практик, а не изменений в инструментах |
| n8n, форум, раздел announcements | `https://community.n8n.io/c/announcements.rss` | 200, 90 КБ | 25, все датированы | 2025-08-01 — 2026-08-06 | Медиана 1135 знаков | Лента живая, но содержание — жизнь форума: бейджи, боты, опросы. Релизов там нет |
| Hugging Face, блог | `https://huggingface.co/blog/feed.xml` | 200, 249 КБ | 843, все датированы | 2020-02-14 — 2026-08-17 | Тела нет вовсе: в `item` только `title`, `link`, `pubDate` | Формально глубочайшая лента в подборке и при этом бесполезная. Содержание — статьи сообщества, не изменения продукта |
| GitHub Releases в формате Atom | `https://github.com/OWNER/REPO/releases.atom` | 200 | Ровно 10, все датированы | Зависит от частоты релизов: у `anthropics/claude-code` десять записей покрывают 2026-08-06 — 2026-08-14 | Полный текст релиза, медиана 2587 знаков у `claude-code` | Проверено 22 репозитория, везде ровно десять записей. Параметры `?page=2` и `?after=` игнорируются, тело ответа побайтно совпадает с первой страницей. Действующий адаптер `github_releases` с пагинацией строго лучше. Смысла заводить их как `rss` нет |

## Предупреждения: адреса, которые выглядят рабочими

Все перечисленные отдают HTTP 200 и заметный объём, но лент не содержат. Именно на них ломается
проверка «код ответа двухсотый, значит работает».

| URL | Что на самом деле |
| --- | --- |
| `https://docs.claude.com/rss.xml` | 200, 904 КБ. Это HTML документации, ноль записей |
| `https://blog.langchain.com/rss/` и `https://blog.langchain.dev/rss/` | 200, 207 КБ. Отдаётся страница Webflow, ноль записей. У блога LangChain работающей ленты нет |
| `https://openrouter.ai/rss.xml` | 200, 126 КБ. HTML, ноль записей |
| `https://www.infoworld.com/category/artificial-intelligence/index.rss` | 200, 267 КБ. HTML, ноль записей |
| `https://developers.openai.com/rss.xml` | 200, 138 датированных записей. Объявлена на `platform.openai.com/docs/changelog` через `<link rel="alternate">` как «Changelog, OpenAI API», но ни одной записи из раздела changelog в ней нет: только страницы `learn` и документация. Последняя дата — 2026-05-05, то есть лента стоит три с половиной месяца |
| `https://docs.n8n.io/changelog/release-notes/rss.xml` | Объявлена в `<link rel="alternate">` на странице release-notes и отдаёт 404 с телом «No updates found in page». Варианты `feed_rss_created.xml` и `feed_rss_updated.xml` тоже 404 |
| `https://developers.googleblog.com/feeds/posts/default` | 200, 20 записей. Ни `pubDate`, ни `published`: ноль дат во всём документе. Адаптер не сможет применить окно в 26 часов |
| `https://blog.modelcontextprotocol.io/rss.xml` | 404, страница Hugo «404 Page not found». Рабочий адрес — `/index.xml` |
| `https://www.anthropic.com/rss.xml`, `/news/rss.xml`, `/news/feed.xml`, `/atom.xml`, `/feed`, `/engineering/rss.xml` | Все 404, тело 58 КБ — оформленная страница ошибки. Ленты блога у Anthropic нет; официальная лента есть только у release notes |
| `https://supabase.com/changelog/feed.xml`, `https://cursor.com/blog/rss.xml`, `https://status.n8n.io/history.rss`, `https://status.openrouter.ai/history.rss`, `https://modelcontextprotocol.io/rss.xml`, `https://developers.googleblog.com/en/rss/` | 404 |

## Таблица 2. Telegram-каналы

Превью читаются обычным `curl` без заголовков и без авторизации: подмена User-Agent ничего не
меняет. Даты у каждого поста лежат в атрибуте `datetime` тега `<time>`, извлекаются надёжно.
Одна страница отдаёт от 12 до 20 постов в зависимости от их длины.

### Рекомендую включить

| Канал | URL превью | HTTP | Постов в превью | Тематическая точность | Язык | Вывод |
| --- | --- | --- | --- | --- | --- | --- |
| tsingular | `https://t.me/s/tsingular` | 200, 177 КБ | 20, 2026-08-14 — 2026-08-17 | Высокая. Из выборки: добавление квантованного Qwen 3.8 в Ollama, выход нового агентского харнесса, изменение авторизации агентов в Metronix Memory, новая команда `/loop` у Hermes | Русский | Ближе всех к теме изменений в инструментах, а не новостей про ИИ. Приоритет 5 |
| ai_machinelearning_big_data | `https://t.me/s/ai_machinelearning_big_data` | 200, 173 КБ | 15, 2026-08-13 — 2026-08-17 | Средневысокая. Релизы моделей и режимов API с деталями: Gemini 3.7 Flash, режим Ultrafast для GPT-5.6 Sol, GLM-5.3. Разбавлено рекламой курсов | Русский | Хорошая плотность фактов, подтверждающих первоисточники. Приоритет 5 |
| data_secrets | `https://t.me/s/data_secrets` | 200, 119 КБ | 12, 2026-08-13 — 2026-08-17 | Средневысокая. Тот же профиль: релизы моделей, изменения в API. Присутствуют реклама и отраслевые сплетни | Русский | Приоритет 5 |
| HackerNewsFeed | `https://t.me/s/HackerNewsFeed` | 200, 98 КБ | 20, 2026-08-13 — 2026-08-17 | Средняя. Зеркало ленты Hacker News: только заголовок и ссылка. В выборке — превью DeepSeek Harness, анонс Gemini 3.7 Flash, разбор Ultrafast от Cerebras | Английский | Сигнал обнаружения, а не материал. Дублирует ленту `hnrss`, брать что-то одно. Приоритет 5 |

### Резерв

| Канал | URL превью | HTTP | Постов в превью | Тематическая точность | Язык | Вывод |
| --- | --- | --- | --- | --- | --- | --- |
| seeallochnaya | `https://t.me/s/seeallochnaya` | 200, 202 КБ | 20, 2026-07-30 — 2026-08-14 | Средняя. Технически глубоко, но заметная доля — разборы инцидентов и происходящего в отрасли | Русский | Держать в резерве: качество высокое, попадание в тему нестабильное |
| devfm | `https://t.me/s/devfm` | 200, 185 КБ | 20, 2026-07-12 — 2026-08-17 | Средняя. Инструменты разработчика и агенты: Codex Desktop, написание скиллов, Slidev. Формат личных заметок | Русский | Резерв |
| pwnai | `https://t.me/s/pwnai` | 200, 178 КБ | 19, 2026-07-13 — 2026-08-17 | Узкая, но точная. Безопасность LLM: garak, promptfoo, разбор System Card для Claude Opus 5 | Русский | Единственный найденный канал, закрывающий `change_type: security`. Резерв |
| ai_newz | `https://t.me/s/ai_newz` | 200, 121 КБ | 14, 2026-08-04 — 2026-08-17 | Средняя. Релизы моделей и research вперемешку с рекламой | Русский | Резерв, сильно пересекается с data_secrets |

### Отклонены

| Канал | URL превью | HTTP | Постов в превью | Тематическая точность | Язык | Вывод |
| --- | --- | --- | --- | --- | --- | --- |
| denissexy | `https://t.me/s/denissexy` | 200, 150 КБ | 18, 2026-08-02 — 2026-08-14 | Низкая. Личный микс: шахматы, фотографии Плутона, изредка цены на Codex | Русский | Слишком мало попаданий в тему |
| cgevent | `https://t.me/s/cgevent` | 200, 163 КБ | 18, 2026-08-13 — 2026-08-17 | Низкая для этой темы. Генеративная графика и звук: Suno, ComfyUI, Qwen Layered | Русский | Другой стек, вне активного стека читателя |
| llm_under_hood | `https://t.me/s/llm_under_hood` | 200, 139 КБ | 19, 2026-07-03 — 2026-08-14 | Низкая. Собственные бенчмарки и продвижение своей платформы | Русский | Отклонить |
| vibe_coding | `https://t.me/s/vibe_coding` | 200, 138 КБ | 20, 2026-07-09 — 2026-08-16 | Низкая. Личный опыт, изредка про сброс лимитов Anthropic | Русский | Наблюдения без первоисточника, авторитет не тянет даже на пятый приоритет |
| boris_again | `https://t.me/s/boris_again` | 200, 169 КБ | 20, 2026-08-03 — 2026-08-17 | Низкая. Недельные дайджесты, карьера, рассуждения | Русский | Отклонить |
| gonzo_ML | `https://t.me/s/gonzo_ML` | 200, 115 КБ | 20, 2026-08-14 — 2026-08-17 | Низкая. Разборы статей | Русский | Плюс техническая помеха: текст извлекается лишь у трёх постов из двадцати, остальное пересылки |
| nn_for_science | `https://t.me/s/nn_for_science` | 200, 174 КБ | 18, 2026-07-25 — 2026-08-14 | Низкая. Репортажи с конференций | Русский | Отклонить |
| neuraldeep | `https://t.me/s/neuraldeep` | 200, 133 КБ | 17, 2026-08-02 — 2026-08-17 | Низкая. Собственный продукт и локальный инференс | Русский | Отклонить |
| zen_of_python | `https://t.me/s/zen_of_python` | 200, 174 КБ | 20, 2026-07-28 — 2026-08-17 | Вне темы, но качество высокое. Релизы Python, uv, FastAPI, PEP | Русский | Смежная тема. Пригодится, если тема проекта когда-нибудь расширится на язык и сборку |
| NeuralShit | `https://t.me/s/NeuralShit` | 200, 131 КБ | 19, 2026-08-10 — 2026-08-17 | Вне темы. Мемы и потребительский ИИ | Русский | Отклонить |
| exploitex | `https://t.me/s/exploitex` | 200, 127 КБ | 17, все за 2026-08-17 | Вне темы. Общие новости и развлечения | Русский | Отклонить |
| llm_notes | `https://t.me/s/llm_notes` | 200, 219 КБ | 19, 2026-01-27 — 2026-07-27 | Низкая. Курсы и новости | Русский | Молчит с 27 июля |
| huggingface | `https://t.me/s/huggingface` | 200, 120 КБ | 20, 2026-02-11 — 2026-05-10 | Средняя, но канал неофициальный: автоматическое зеркало твиттера | Английский | Молчит с 10 мая |

### Мёртвые и несуществующие превью

Отдельно, потому что все они отвечают HTTP 200 и на беглый взгляд неотличимы от рабочих.

| Канал | HTTP и объём | Что внутри |
| --- | --- | --- |
| `openai`, `claudecode`, `cursor_ide`, `ai_coding`, `githubprojects`, `towards_ai`, `aiwithvibes`, `agentsdev`, `ai_agents`, `n8n_ru`, `n8n_community`, `theaidigest`, `epsilon_correction`, `OpenAIDevs`, `hackernewsrobot` | 200, от 9,5 до 11 КБ | Ноль постов. Канала нет либо превью закрыто. Объём около 10 КБ — надёжный признак пустышки |
| `anthropic_ai` | 200, 29 КБ | Три поста, последний 8 января 2023 года |
| `claude_ai` | 200, 30 КБ | Три поста, последний 1 февраля 2023 года |
| `modelcontextprotocol` | 200, 22 КБ | Один пост, 21 марта 2025 года |
| `claude_code_ru` | 200, 51 КБ | Пять постов, последний 4 января 2026 года |
| `cursor_ai` | 200, 42 КБ | Пять постов, последний 11 апреля 2025 года |
| `mcp_servers` | 200, 82 КБ | Тринадцать постов, из них половина — служебные сообщения о переименовании канала |
| `ai_pub`, `devtools`, `openai_news`, `ainewsdaily` | 200 | Живые страницы с двадцатью постами, но последние записи 2021, 2021, 2021 и 2023 годов |

Официальных каналов у Anthropic, OpenAI, Cursor и n8n в Telegram не нашлось: все совпадающие
по имени адреса либо пусты, либо заброшены три года назад.

## Два наблюдения о глубине

**Telegram листается назад.** Параметр `?before=<message_id>` работает: запрос
`https://t.me/s/data_secrets?before=9705` вернул ещё 14 постов, до 9 августа. То есть
ограниченный бэкфилл технически возможен постраничным обходом. Флаг `backfill_supported`
всё равно оставлен `false`: авторитет источника пятого приоритета не оправдывает такой обход,
а SRC-2 всё равно не даст опубликовать факт на одном таком материале.

**Часть лент даёт историю глубже, чем принято ожидать от RSS.** У `openai.com/news/rss.xml`
это 1132 записи с 2015 года, у ленты release notes Anthropic — 130 записей с мая 2024, у
`vercel.com/atom` — 1473 записи с 2016 года. Здесь `backfill_supported: false` означает не
«истории нет», а «отдельный режим бэкфилла не нужен»: первый же обычный прогон заберёт всё,
что лента отдаёт. Если позже понадобится честный бэкфилл по этим адресам, флаг можно поднять
без изменения кода адаптера.

## Замечание по действующим источникам

Две находки перекрывают уже работающие html-скрейпы и, по-моему, должны их заменить.

`anthropic_api_release_notes` скрейпит `docs.claude.com/en/release-notes/api`. На этой самой
странице объявлена лента `platform.claude.com/docs/en/release-notes/feed.xml`, которая отдаёт
тот же материал в разобранном виде, с датами и глубиной до мая 2024 года. Заметки по Claude Code
она не покрывает, поэтому источник `anthropic_claude_code_release_notes` остаётся как есть.

`cursor_changelog` скрейпит `cursor.com/changelog` и заведён с `min_expected_items: 3`. Лента
`cursor.com/changelog/rss.xml` отдаёт 50 записей за полгода с полным текстом.

Если обе замены принять, целевое число источников по SRC-1 не нарушается: одиннадцать
действующих плюс `github_changelog_rss`, `mcp_blog_rss`, `simonwillison_llms_atom`,
`tg_tsingular` и `tg_ai_ml_big_data` дают ровно шестнадцать.

## Фрагмент YAML для секции `sources`

Включены только те источники, что укладываются в SRC-1. Остальные проверенные оставлены с
`enabled: false`, чтобы их можно было поднять одной правкой без повторной разведки.

```yaml
  # Приоритет 2: официальные ленты вендоров. Бэкфилл не поддерживается —
  # лента отдаёт свой срез целиком при первом же обычном прогоне.
  - id: anthropic_release_notes_rss
    type: rss
    url: https://platform.claude.com/docs/en/release-notes/feed.xml
    priority: 2
    enabled: true
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 100

  - id: cursor_changelog_rss
    type: rss
    url: https://cursor.com/changelog/rss.xml
    priority: 2
    enabled: true
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 30

  - id: github_changelog_rss
    type: rss
    url: https://github.blog/changelog/feed/
    priority: 2
    enabled: true
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 8

  - id: mcp_blog_rss
    type: rss
    url: https://blog.modelcontextprotocol.io/index.xml
    priority: 2
    enabled: true
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 15

  # Лента отдаёт 1132 записи с 2015 года, но тело — сниппет в 150 знаков.
  # Материал придётся дочитывать со страницы на этапе обогащения.
  - id: openai_news_rss
    type: rss
    url: https://openai.com/news/rss.xml
    priority: 2
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 50

  # Единственная работающая лента n8n: docs.n8n.io/changelog/release-notes/rss.xml
  # объявлена в <link rel="alternate">, но отдаёт 404.
  - id: n8n_blog_rss
    type: rss
    url: https://n8n.io/blog/rss/
    priority: 2
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 5

  - id: google_gemini_blog_rss
    type: rss
    url: https://blog.google/products/gemini/rss/
    priority: 2
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 10

  - id: cursor_forum_announcements_rss
    type: rss
    url: https://forum.cursor.com/c/announcements.rss
    priority: 2
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 10

  # Статусные страницы. Содержат инциденты, а не изменения продукта: в словарь
  # change_types они ложатся только как other. Включать, если нужны условия
  # эксплуатации, а не изменения.
  - id: anthropic_status_rss
    type: rss
    url: https://status.anthropic.com/history.rss
    priority: 2
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 10

  - id: openai_status_rss
    type: rss
    url: https://status.openai.com/history.rss
    priority: 2
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 10

  # Приоритет 4: профильные издания.
  - id: simonwillison_llms_atom
    type: rss
    url: https://simonwillison.net/tags/llms.atom
    priority: 4
    enabled: true
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 20

  - id: theregister_ai_atom
    type: rss
    url: https://www.theregister.com/software/ai_ml/headlines.atom
    priority: 4
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 30

  - id: thenewstack_ai_rss
    type: rss
    url: https://thenewstack.io/category/ai/feed/
    priority: 4
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 15

  # Приоритет 5: агрегатор. По SRC-2 не может быть единственным основанием
  # для публикации факта.
  - id: hnrss_ai_tools
    type: rss
    url: https://hnrss.org/newest?q=Claude+OR+OpenAI+OR+Cursor&count=50
    priority: 5
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 20

  # Приоритет 5: Telegram. Веб-превью читается без авторизации, одна страница
  # отдаёт 12–20 постов с датами. Подтверждение, а не первоисточник (SRC-2).
  - id: tg_tsingular
    type: telegram_channel
    url: https://t.me/s/tsingular
    priority: 5
    enabled: true
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 10

  - id: tg_ai_ml_big_data
    type: telegram_channel
    url: https://t.me/s/ai_machinelearning_big_data
    priority: 5
    enabled: true
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 10

  - id: tg_data_secrets
    type: telegram_channel
    url: https://t.me/s/data_secrets
    priority: 5
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 10

  - id: tg_hackernewsfeed
    type: telegram_channel
    url: https://t.me/s/HackerNewsFeed
    priority: 5
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 15

  - id: tg_pwnai
    type: telegram_channel
    url: https://t.me/s/pwnai
    priority: 5
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 8

  - id: tg_devfm
    type: telegram_channel
    url: https://t.me/s/devfm
    priority: 5
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 10

  - id: tg_seeallochnaya
    type: telegram_channel
    url: https://t.me/s/seeallochnaya
    priority: 5
    enabled: false
    backfill_supported: false
    backfill_depth_days: 0
    min_expected_items: 10
```
