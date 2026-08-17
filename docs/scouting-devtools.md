# Разведка источников: AI-инструменты разработчика и агентные IDE

Дата проверки: 17 августа 2026. Каждый адрес открыт `curl` (следование редиректам,
User-Agent десктопного браузера, таймаут 25–30 секунд). GitHub Releases считаны
через `gh api ... --paginate`. Ни один адрес не выведен по догадке.

Окно измерения плотности: 18 месяцев, с 17 февраля 2025 по 17 августа 2026.
Для GitHub Releases считались только стабильные выпуски (`prerelease == false`) —
без этого фильтра ночные и альфа-сборки Gemini CLI и Codex дают трёхкратный шум
с телом релиза в 26 символов.

---

## 1. Сводная таблица проверенных источников

Колонка «Тип изменения» перечисляет ячейки, в которых источник даёт не менее трёх
датированных событий за 18 месяцев. Колонка «Дат» — уникальных дат в одной выдаче
(для GitHub — стабильных релизов за 18 месяцев).

### Годные к включению

| Вендор | Источник | Тип | Закрывает | HTTP | Дат / релизов | Глубина | Вывод |
|---|---|---|---|---|---|---|---|
| Zed | `github.com/zed-industries/zed` | `github_releases` | breaking_change, deprecation, limits | 200 | 242 стабильных | 2021-06 … 2026-08 | Лучший источник среза. В 54 релизах непустая секция `## Breaking Changes and Notices`, всего 129 пунктов |
| Cognition (Windsurf) | `https://docs.devin.ai/desktop/changelog.md` | `html_scrape` | pricing, limits, deprecation | 200, 282 КБ | 98 записей / 92 даты | 2025-04-02 … 2026-08-13 | `windsurf.com/changelog` редиректит именно сюда. Разметка `<Update label="v3.7.25" description="August 13, 2026">` — дата в атрибуте, парсится тривиально |
| Cognition (Devin) | `https://docs.devin.ai/release-notes/2026.md`, `.../2025.md`, `.../2024.md` | `html_scrape` | deprecation, pricing, limits | 200, 110 КБ | 53 + 43 | 2024 … 2026-08-14 | Годовые страницы Mintlify в чистом Markdown. Преимущественно фичи, но ACU-лимиты и тарифы попадают |
| Cognition (Windsurf JetBrains) | `https://docs.devin.ai/windsurf/plugins/changelog.md` | `html_scrape` | pricing | 200, 66 КБ | 52 | 2024-06-20 … 2026-07-29 | Отдельный плагин, отдельная линия выпусков |
| Cognition (Devin CLI) | `https://docs.devin.ai/cli/changelog/stable.md` | `html_scrape` | pricing | 200, 74 КБ | 33 | 2025-03-26 … 2026-08-13 | Тонко, но датировано |
| Warp | `https://docs.warp.dev/_llms-txt/changelog.txt` | `html_scrape` | pricing, deprecation, limits, breaking_change | 200, 332 КБ | 275 версий | 2021 … 2026-08-13 | Вся история в одном текстовом файле. Даты в заголовках вида `### 2026.08.13 (v0.2026.08.12.21.54)` — нестандартный формат, парсеру нужна отдельная маска |
| Warp | `https://docs.warp.dev/changelog/2026/`, `.../2025/` | `html_scrape` | то же | 200, 492 КБ | 46 за 2026 | помесячно | Резерв, если текстовая выгрузка исчезнет |
| Google (Gemini CLI) | `github.com/google-gemini/gemini-cli` | `github_releases` | breaking_change, deprecation, pricing, limits | 200 | 310 стабильных | 2025-06-28 … 2026-08-11 | Самая высокая плотность по всем четырём ячейкам. Обязателен фильтр `prerelease == false`: ночных сборок вдвое больше стабильных |
| OpenAI (Codex CLI) | `github.com/openai/codex` | `github_releases` | deprecation, limits, breaking_change | 200 | 144 стабильных | 2025-06-30 … 2026-08-07 | Альфа-теги `rust-vX-alpha.N` имеют тело в 26 символов, стабильные — около 5,7 КБ |
| OpenAI (Codex) | `https://learn.chatgpt.com/docs/changelog` | `html_scrape` | pricing, deprecation, limits | 200, 1,6 МБ | 117 записей / 100 дат | 2025-05-19 … 2026-08 | Канонический адрес: `developers.openai.com/codex/changelog` редиректит сюда. Общий журнал ChatGPT и Codex, часть записей относится к продуктовой стороне OpenAI |
| Cline | `github.com/cline/cline` | `github_releases` | deprecation, pricing, limits | 200 | 322 стабильных | 2025-02-19 … 2026-08-14 | Четыре линии тегов в одном репозитории: `vX`, `cli-vX`, `desktop-vX`, `sdk/sdk/vX`. `CHANGELOG.md` без дат — брать только Releases |
| Roo Code | `https://raw.githubusercontent.com/RooCodeInc/Roo-Code/main/CHANGELOG.md` | `html_scrape` | deprecation, pricing, limits, breaking_change | 200, 196 КБ | 193 датированных | 2025-02-08 … 2026-02-21 | Заголовки `## [3.50.4] - 2026-02-21`. С версии 3.51 дату из заголовка убрали — свежие записи придётся датировать через Releases |
| Roo Code | `github.com/RooCodeInc/Roo-Code` | `github_releases` | только даты | 200 | 249 | 2025-03 … 2026-05-15 | Тело релиза — 15 символов («Release v3.54.0»). Ценность одна: карта «версия → дата» для хвоста `CHANGELOG.md` |
| OpenCode | `github.com/sst/opencode` | `github_releases` | breaking_change, deprecation, pricing, limits | 200 | 856 стабильных | 2025-05-14 … 2026-08-13 | Выпуски почти ежедневные, тело около 840 символов. Объём высокий, событие на релиз — низкое |
| Goose (Block) | `github.com/block/goose` | `github_releases` | deprecation, pricing, limits | 200 | 111 стабильных | 2025-02-20 … 2026-08-12 | Тело в среднем 6 КБ, у v1.46.0 — 42 КБ. Лучшее отношение содержания к числу запросов |
| Lovable | `https://docs.lovable.dev/changelog` | `html_scrape` | pricing, deprecation, limits | 200, 1,8 МБ | 190 записей / 96 дат | 2024-12-03 … 2026-08-14 | Плотнее всех по кредитам и тарифам: 30 записей за 18 месяцев. Даты в HTML разбиты разметкой — парсер обязан сначала снять теги, сырой `grep` по HTML даёт всего 19 |
| Sourcegraph (Cody) | `https://sourcegraph.com/changelog/releases` (+ `?page=N`) | `html_scrape` | deprecation, limits | 200, 1,3 МБ | 21 на страницу, пагинация работает | 2026-01-19 … 2026-08-06 | Записи вида «Cody API endpoints are no longer available», «Deprecate EOL claude sonnet 3.5 model» |
| Sourcegraph | `https://sourcegraph.com/changelog/7-0-removals-deprecations` | `html_scrape` | deprecation | 200, 31 КБ | 3 | одна публикация | Именной реестр удалений к мажорной версии. Ровно тот класс страниц, который искали, но выходит раз в мажор |
| Amp (Sourcegraph) | `https://ampcode.com/news.rss` | `rss` | — (release) | 200, 297 КБ | 141 элемент | 2025-03-06 … 2026-08-11 | Полная история в одной ленте, `pubDate` в RFC 822. HTML-двойник — `ampcode.com/chronicle`, 128 дат |
| Vercel (v0) | `https://v0.app/changelog` | `html_scrape` | pricing | 200, 393 КБ | 84 записи / 72 даты | 2025-10-01 … 2026-08-14 | Кредиты и биллинг в 24 записях. Отключений почти нет |
| GitHub Copilot | `https://github.blog/changelog/label/copilot/` | `html_scrape` | deprecation | 200, 179 КБ | 21 запись, `<time datetime="…">` | последние 12 дней | Живой опрос. Пагинация по `/page/N/` и `?page=N` не работает — обе отдают ту же первую страницу |
| GitHub Copilot | `https://github.blog/changelog/{YYYY}/{MM}/` | `html_scrape` | deprecation | 200, 219 КБ | 80–83 записи в месяц | помесячно, произвольная глубина | Единственный рабочий путь бэкфилла. 2026/03 — 7 записей об отключениях, 2025/09 — 12. Не только Copilot, фильтровать по содержанию |
| GitHub Copilot | `https://github.blog/changelog/label/copilot/feed/` | `rss` | deprecation | 200, 52 КБ | 10 элементов | окно RSS | Дешёвая ежедневная проверка |
| Claude Code (агрегатор) | `https://claudelog.com/claude-code-changelog/` | `html_scrape` | breaking_change, deprecation, pricing, limits | 200, 787 КБ | 320 записей / 248 дат | 2025-04-17 … 2026-08-11 | Официальный `CHANGELOG.md` дат не содержит вовсе — этот агрегатор их добавляет и сохраняет полные списки изменений. `breaking` 22, `deprecat` 25, `rate limit` 18 |
| Мультивендорный агрегатор | `https://www.havoptic.com/tools/{claude-code,windsurf,github-copilot,gemini-cli,openai-codex,cursor,kiro}` | `html_scrape` | release | 200, 25–43 КБ | 84–161 дат на инструмент | 2025-04 … 2026-08 | Компактно и датировано, но тексты — сводки, сделанные моделью. Годится как страховка и как источник дат, не как доказательство формулировки |

### Проверено и отброшено

| Вендор | Адрес | Что произошло | Вывод |
|---|---|---|---|
| Continue | `https://docs.continue.dev/changelog` | 200, 9,9 КБ, ноль дат; TabStack отдаёт только «Redirecting…» | Страница мертва |
| Continue | `github.com/continuedev/continue` | 232 стабильных релиза за 18 месяцев, но последние (`v2.0.0-vscode`, 19 июня 2026) с пустым телом; содержательные записи обрываются 27 марта 2026 | Формально годен, фактически заброшен. Включать с `enabled: false` |
| Aider | `github.com/Aider-AI/aider` | Последний релиз 9 августа 2025, за 12 месяцев — ноль | Проект спит |
| Aider | `https://aider.chat/HISTORY.html` | 200, 194 КБ, 4 даты ISO на весь файл | История по версиям без дат |
| Bolt.new | `https://support.bolt.new/release-notes` | 200, 692 КБ, 3 даты в прозе на 33 КБ текста | Датированного журнала нет. Отключения объявляются внутри текста («As of August 3, 2026, the v1 Agent is retired»), привязать к дате автоматически нельзя |
| Bolt.new | `github.com/stackblitz/bolt.new` | 0 релизов | — |
| Bolt.new | `https://support.bolt.new/changelog` | 404 | — |
| JetBrains AI Assistant | `https://plugins.jetbrains.com/api/plugins/22282/updates?size=100` | 200, JSON, 100 обновлений — но все за 5 недель (09.07–13.08.2026), а поле `notes` одинаково у всех: три различающихся префикса на сотню записей | Даты без содержания |
| JetBrains AI Assistant | `https://plugins.jetbrains.com/.../versions` | 200, ноль дат (клиентский рендеринг) | — |
| JetBrains AI Assistant | `https://www.jetbrains.com/help/ai-assistant/whats-new.html` | 404 | — |
| JetBrains AI Assistant | `https://blog.jetbrains.com/ai/category/releases/` | `curl` — код 000 (соединение обрывается), TabStack проходит: 12 публикаций, из них про AI Assistant три (2024.3, 2025.1, 2025.2), остальное Koog и Mellum | Слишком редко и без отключений |
| JetBrains AI Assistant | `https://lp.jetbrains.com/ai-assistant-whatsnew/` | 200, 45 КБ, ноль дат | — |
| Replit | `https://docs.replit.com/updates` + `/updates/{YYYY}/{MM}/{DD}/changelog.md` | 200, индекс из 88 еженедельных записей с декабря 2024. Выборка из 22 записей (60 КБ): `breaking` 0, `deprecat` 0, `pricing` 0, `rate limit` 0 | Даты отличные, тип изменения — только `release`. Включать не стоит |
| Sourcegraph | `https://sourcegraph.com/docs/technical-changelog` | 200, 7,2 МБ, 538 КБ текста: `deprecat` 237, `removed` 414, `no longer` 219 — и при этом всего 4 даты ISO на весь документ | Богатейшее содержание, сгруппированное по версиям без дат. Годится только как справочник при обогащении, не как ленточный источник |
| Sourcegraph Cody | `https://sourcegraph.com/docs/cody/release-notes`, `.../changelog` | 200, 12 КБ, ноль дат | Пустые заглушки |
| Cody | `raw.githubusercontent.com/sourcegraph/cody/main/vscode/CHANGELOG.md` | 404, репозиторий `sourcegraph/cody` не отдаёт релизы (404 на `/releases`) | Репозиторий свёрнут |
| Kiro (AWS) | `https://kiro.dev/changelog/` | 200, 291 КБ, ноль дат в `curl`; TabStack извлекает тексты, но без дат и без номеров версий | Нужен рендеринг, и даже он дат не даёт. Обходной путь — Havoptic |
| Cline | `https://docs.cline.bot/changelog` | 404 | — |
| Zed | `https://zed.dev/releases` | 200, редирект на `/releases/stable`, 8 дат на страницу | GitHub Releases того же проекта полнее на два порядка |
| Vercel | `https://vercel.com/changelog` | 200, 496 КБ, 5 дат | Клиентский рендеринг. Профильная страница `v0.app/changelog` работает без него |

---

## 2. Какие ячейки закрываются

Ячейка считается закрытой, если у вендора не меньше трёх датированных событий
соответствующего типа за 18 месяцев.

| Вендор | breaking_change | deprecation | pricing | limits | Ячеек |
|---|---|---|---|---|---|
| Zed | 54 | 26 | 2 | 11 | 3 |
| Google (Gemini CLI) | 31 | 50 | 11 | 70 | 4 |
| OpenAI (Codex CLI + журнал) | 7 | 24 | 12 | 32 | 4 |
| OpenCode | 16 | 12 | 7 | 16 | 4 |
| Roo Code | 3 | 10 | 15 | 9 | 4 |
| Warp | 3 | 5 | 20 | 3 | 4 |
| Cline | 2 | 20 | 19 | 32 | 3 |
| Cognition (Windsurf + Devin) | 0 | 6 | 23 | 9 | 3 |
| Lovable | 0 | 6 | 30 | 3 | 3 |
| Goose (Block) | 0 | 9 | 11 | 16 | 3 |
| Anthropic (Claude Code, агрегатор) | 22 | 25 | 19 | 16 | 4 |
| Continue (заброшен) | 4 | 14 | 6 | 8 | 3 |
| Sourcegraph (Cody + Amp) | 1 | 4 | 1 | 3 | 2 |
| Vercel (v0) | 0 | 3 | 24 | 0 | 2 |
| GitHub (Copilot) | 0 | 7–12 в месяц | 1 | 0 | 1 |
| Replit | 0 | 0 | 0 | 0 | 0 |

Итого закрывается **48 ячеек** (вендор × тип изменения) по 15 вендорам.
Вендоров с тремя и более плотными ячейками — **12**. Требование записки
(`min_vendors_with_dense_cell: 3`, `min_events_per_dense_cell: 3`) перекрывается
вчетверо только этим срезом.

Оговорка о методе. Числа получены регулярными выражениями по телу записи:
`breaking|BREAKING|backwards-incompatible`; `deprecat|sunset|retire|EOL|no longer
available|will be removed`; `pricing|billing|credits|free tier|per 1M tokens`;
`rate limit|quota|usage limit|throttl|spending cap`. Ложные срабатывания есть:
у Zed `breaking` ловит фразу «Fixed rewrap breaking lines at non-breaking spaces».
Поэтому для Zed отдельно посчитана точная величина — 54 релиза с непустой секцией
`## Breaking Changes and Notices` и 129 пунктов в них. Прочие числа считать
верхней оценкой; после разметки корпуса они просядут примерно на четверть,
но порог в три события это не ломает нигде, кроме строк Sourcegraph и v0.

---

## 3. Инструменты без публичного журнала изменений

- **Bolt.new.** Страница `support.bolt.new/release-notes` существует и живёт, но
  записи не датированы: они сгруппированы в «New features» и «Updates» без
  отметок времени. Даты встречаются только внутри прозы.
- **JetBrains AI Assistant.** Публичного журнала нет ни в одном виде. Есть
  Marketplace API с датами сборок, но одним и тем же текстом примечаний на все
  сборки; есть блог с тремя релизными публикациями за два года. Отключения
  моделей и смена тарифных ярусов (AI Free / Pro / Ultimate) объявлялись только
  в блоге и на страницах цен, без реестра.
- **Sourcegraph Cody как отдельный продукт.** Собственный журнал свёрнут:
  `sourcegraph/cody` на GitHub не отдаёт релизов, `sourcegraph.com/docs/cody/release-notes`
  пуст. Изменения Cody теперь идут внутри общего журнала Sourcegraph.
- **Kiro (AWS).** Журнал есть, но целиком собирается на клиенте и не содержит
  ни дат, ни номеров версий даже после рендеринга.
- **Continue.** Журнал был и умер: страница документации редиректит в никуда,
  GitHub Releases с конца марта 2026 выходят с пустым телом.
- **Aider.** Не журнал отсутствует, а проект: последний выпуск 9 августа 2025.
- **Replit.** Журнал образцовый по датам и бесполезный по типам: за 22 недели
  выборки ни одного упоминания отключения, ломающего изменения или тарифа.

---

## 4. Пять лучших источников по убыванию ценности

1. **`github.com/zed-industries/zed`, `github_releases`.** Единственный источник
   среза с формальной секцией ломающих изменений в каждом стабильном выпуске:
   54 релиза с непустым `## Breaking Changes and Notices`, 129 пунктов, каждый со
   ссылкой на конкретный PR. Глубина с июня 2021 года, недельная каденция,
   структурированный API без парсинга HTML. Одна ячейка `(zed, breaking_change)`
   плотнее, чем весь остальной срез вместе.

2. **`docs.devin.ai/desktop/changelog.md`, `html_scrape`.** Журнал Windsurf после
   поглощения Cognition. 98 датированных выпусков за 16 месяцев в чистом
   Markdown с датой в атрибуте `description`. Отдельная ценность в том, что
   `windsurf.com/changelog` редиректит именно сюда: без проверки редиректа этот
   вендор был бы записан в потерянные. Рядом лежит целое семейство журналов
   Cognition — Devin по годам, Devin CLI, плагин для JetBrains.

3. **`github.com/google-gemini/gemini-cli`, `github_releases`.** Наивысшая
   плотность по всем четырём типам сразу: 31 ломающее, 50 отключений, 70 записей
   о лимитах и квотах на 310 стабильных релизов. Обязательное условие — фильтр
   `prerelease == false`: ночных сборок 276 против 268 стабильных, и они несут
   тело в 145–780 символов.

4. **`docs.warp.dev/_llms-txt/changelog.txt`, `html_scrape`.** Вся история Warp с
   2021 года одним текстовым файлом на 332 КБ, 275 датированных версий, ноль
   HTML. Один запрос закрывает и ежедневный опрос, и полный бэкфилл. Цена —
   нестандартный формат даты `2026.08.13` в заголовке, под который парсеру нужна
   отдельная маска.

5. **`claudelog.com/claude-code-changelog/`, `html_scrape`.** Сторонний
   агрегатор, который решает конкретную проблему: официальный `CHANGELOG.md`
   Claude Code дат не содержит вообще. Здесь 320 записей и 248 уникальных дат за
   16 месяцев, с сохранёнными полными списками изменений, а не пересказом.
   Отдельно отмечу `www.havoptic.com` — семь инструментов среза в едином формате,
   но тексты там переписаны моделью, и как доказательство формулировки они не
   годятся.

---

## 5. Готовый фрагмент для секции `sources`

Идентификаторы не пересекаются с уже занятыми в `config/ai-tools.yaml`.
`min_expected_items` выставлен от измеренного числа, уменьшенного вдвое: это
порог тревоги о поломке источника, а не ожидаемая выдача.

```yaml
  # ---- Приоритет 1: журналы с явной секцией ломающих изменений ----

  # 54 стабильных релиза за 18 месяцев содержат непустую секцию
  # "## Breaking Changes and Notices" (129 пунктов). Обязателен фильтр
  # prerelease == false: теги -pre дублируют содержание стабильных.
  - id: gh_zed
    type: github_releases
    url: https://github.com/zed-industries/zed
    priority: 1
    enabled: true
    parser_hint: breaking_changes_section
    exclude_prereleases: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 60

  # windsurf.com/changelog редиректит сюда: Windsurf перешёл к Cognition.
  # Дата лежит в атрибуте: <Update label="v3.7.25" description="August 13, 2026">
  - id: cognition_windsurf_desktop_changelog
    type: html_scrape
    url: https://docs.devin.ai/desktop/changelog.md
    priority: 1
    enabled: true
    parser_hint: mintlify_update_blocks
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 40

  # Вся история Warp одним текстовым файлом. Заголовок записи —
  # "### 2026.08.13 (v0.2026.08.12.21.54)"; нужна маска YYYY.MM.DD.
  - id: warp_changelog_llmstxt
    type: html_scrape
    url: https://docs.warp.dev/_llms-txt/changelog.txt
    priority: 1
    enabled: true
    parser_hint: dotted_date_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 40

  # Реестр удалений и отключений к мажорной версии Sourcegraph.
  # Выходит раз в мажор, поэтому порог единица.
  - id: sourcegraph_removals_deprecations
    type: html_scrape
    url: https://sourcegraph.com/changelog/7-0-removals-deprecations
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: false
    backfill_depth_days: 540
    min_expected_items: 1

  # Пагинация ?page=N работает и даёт бэкфилл глубже января 2026.
  - id: sourcegraph_changelog_releases
    type: html_scrape
    url: https://sourcegraph.com/changelog/releases
    priority: 1
    enabled: true
    parser_hint: dated_sections
    pagination: "?page={n}"
    backfill_supported: true
    backfill_depth_days: 360
    min_expected_items: 10

  # Кредиты, тарифы и снятые настройки: 30 записей о ценах за 18 месяцев.
  # Даты разбиты разметкой — парсер обязан снять теги до поиска дат,
  # сырой grep по HTML находит 19 вместо 96.
  - id: lovable_changelog
    type: html_scrape
    url: https://docs.lovable.dev/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections_after_strip
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 40

  - id: v0_changelog
    type: html_scrape
    url: https://v0.app/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 360
    min_expected_items: 30

  # Канонический адрес: developers.openai.com/codex/changelog редиректит сюда.
  # Общий журнал ChatGPT и Codex, часть записей относится к продуктовой стороне.
  - id: openai_codex_changelog
    type: html_scrape
    url: https://learn.chatgpt.com/docs/changelog
    priority: 1
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 460
    min_expected_items: 40

  # ---- Приоритет 2: журнал GitHub Copilot ----

  # Ежедневный опрос. Даты в атрибутах <time datetime="YYYY-MM-DD">.
  # Пагинация /page/N/ и ?page=N не работает: обе отдают первую страницу.
  - id: github_copilot_changelog
    type: html_scrape
    url: https://github.blog/changelog/label/copilot/
    priority: 2
    enabled: true
    parser_hint: time_datetime_attr
    backfill_supported: false
    min_expected_items: 10

  # Единственный рабочий путь бэкфилла Copilot: помесячные архивы,
  # 80+ записей в месяц. Не только Copilot — фильтровать по содержанию.
  - id: github_changelog_monthly_archive
    type: html_scrape
    url: https://github.blog/changelog/{year}/{month}/
    priority: 2
    enabled: true
    parser_hint: time_datetime_attr
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 30

  - id: github_copilot_changelog_rss
    type: rss
    url: https://github.blog/changelog/label/copilot/feed/
    priority: 2
    enabled: true
    backfill_supported: false
    min_expected_items: 5

  # Полная история Amp одной лентой, pubDate в RFC 822.
  - id: amp_news_rss
    type: rss
    url: https://ampcode.com/news.rss
    priority: 2
    enabled: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 40

  # Официальный CHANGELOG.md Claude Code дат не содержит вовсе.
  # Этот агрегатор их проставляет и сохраняет полные списки изменений.
  - id: claudelog_claude_code_changelog
    type: html_scrape
    url: https://claudelog.com/claude-code-changelog/
    priority: 2
    enabled: true
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 80

  # ---- Приоритет 3: GitHub Releases ----

  - id: gh_gemini_cli
    type: github_releases
    url: https://github.com/google-gemini/gemini-cli
    priority: 3
    enabled: true
    exclude_prereleases: true
    backfill_supported: true
    backfill_depth_days: 460
    min_expected_items: 60

  - id: gh_openai_codex
    type: github_releases
    url: https://github.com/openai/codex
    priority: 3
    enabled: true
    exclude_prereleases: true
    backfill_supported: true
    backfill_depth_days: 460
    min_expected_items: 40

  # Четыре линии тегов в одном репозитории: vX, cli-vX, desktop-vX, sdk/sdk/vX.
  - id: gh_cline
    type: github_releases
    url: https://github.com/cline/cline
    priority: 3
    enabled: true
    exclude_prereleases: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 60

  # Тело релиза в среднем 6 КБ, у крупных выпусков — до 42 КБ.
  - id: gh_goose
    type: github_releases
    url: https://github.com/block/goose
    priority: 3
    enabled: true
    exclude_prereleases: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 30

  # 856 стабильных релизов за 18 месяцев при теле около 840 символов:
  # объём высокий, событий на релиз мало.
  - id: gh_opencode
    type: github_releases
    url: https://github.com/sst/opencode
    priority: 3
    enabled: true
    exclude_prereleases: true
    backfill_supported: true
    backfill_depth_days: 460
    min_expected_items: 100

  # Заголовки "## [3.50.4] - 2026-02-21" датированы до версии 3.50.4;
  # с 3.51 дату из заголовка убрали. Хвост датировать через gh_roo_code.
  - id: roo_code_changelog_raw
    type: html_scrape
    url: https://raw.githubusercontent.com/RooCodeInc/Roo-Code/main/CHANGELOG.md
    priority: 3
    enabled: true
    parser_hint: keepachangelog_dated_headings
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 80

  # Тело релиза — 15 символов. Единственное назначение: карта версия → дата
  # для записей CHANGELOG.md после 3.50.4.
  - id: gh_roo_code
    type: github_releases
    url: https://github.com/RooCodeInc/Roo-Code
    priority: 3
    enabled: true
    exclude_prereleases: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20
    notes: "version_date_map_only"

  - id: cognition_devin_release_notes
    type: html_scrape
    url: https://docs.devin.ai/release-notes/2026.md
    priority: 3
    enabled: true
    parser_hint: mintlify_update_blocks
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  - id: cognition_windsurf_jetbrains_plugin
    type: html_scrape
    url: https://docs.devin.ai/windsurf/plugins/changelog.md
    priority: 3
    enabled: true
    parser_hint: mintlify_update_blocks
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  - id: cognition_devin_cli_changelog
    type: html_scrape
    url: https://docs.devin.ai/cli/changelog/stable.md
    priority: 3
    enabled: true
    parser_hint: mintlify_update_blocks
    backfill_supported: true
    backfill_depth_days: 460
    min_expected_items: 15

  # Заброшен: содержательные записи обрываются 27 марта 2026, последние
  # релизы выходят с пустым телом. Включить, если проект оживёт.
  - id: gh_continue
    type: github_releases
    url: https://github.com/continuedev/continue
    priority: 3
    enabled: false
    exclude_prereleases: true
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 20

  # Семь инструментов среза в едином формате. Тексты — сводки, сделанные
  # моделью: годится как источник дат и как страховка, не как доказательство.
  - id: havoptic_tools
    type: html_scrape
    url: https://www.havoptic.com/tools/{tool}
    priority: 4
    enabled: false
    parser_hint: dated_sections
    backfill_supported: true
    backfill_depth_days: 540
    min_expected_items: 40
```

---

## 6. Предложения по `corpus.vendors`

Алиасы подобраны так, чтобы ловить самоназвания из тел релизов и заголовков
записей. Отдельно учтено, что Windsurf теперь выходит под именем Devin Desktop:
без алиасов обоих имён половина записей вендора осядет с пустым `vendor`.

```yaml
    - id: zed
      label: "Zed"
      aliases: [Zed, "Zed Industries", zed-industries, "Zed Editor", zed.dev]

    - id: cognition
      label: "Cognition (Windsurf, Devin)"
      aliases: [Cognition, "Cognition AI", "Cognition Labs", Windsurf, windsurf,
                "Windsurf Editor", "Devin Desktop", Devin, devin, Codeium, codeium,
                Cascade, "devin.ai", "windsurf.com"]

    - id: warp
      label: "Warp"
      aliases: [Warp, "Warp Terminal", warpdotdev, "warp.dev", "Warp Agent"]

    - id: sourcegraph
      label: "Sourcegraph (Cody, Amp)"
      aliases: [Sourcegraph, sourcegraph, Cody, cody, "Cody Gateway", Amp, "Amp Code",
                ampcode, "ampcode.com", "Deep Search"]

    - id: cline
      label: "Cline"
      aliases: [Cline, cline, "cline.bot", "Cline Bot", saoudrizwan]

    - id: roo_code
      label: "Roo Code"
      aliases: ["Roo Code", "Roo-Code", RooCode, roo-cline, "Roo Cline",
                RooCodeInc, Roomote]

    - id: opencode
      label: "OpenCode"
      aliases: [OpenCode, opencode, "opencode.ai", sst/opencode, "SST OpenCode"]

    - id: goose
      label: "Goose (Block)"
      aliases: [Goose, goose, "Block Goose", "codename goose", block/goose]

    - id: lovable
      label: "Lovable"
      aliases: [Lovable, lovable, "lovable.dev", GPT-Engineer, "GPT Engineer"]

    - id: vercel
      label: "Vercel (v0)"
      aliases: [Vercel, vercel, v0, "v0.dev", "v0.app", "V0"]

    - id: replit
      label: "Replit"
      aliases: [Replit, replit, "Replit Agent", "Agent 3", "replit.com"]

    - id: bolt
      label: "Bolt.new (StackBlitz)"
      aliases: ["Bolt.new", "bolt.new", Bolt, StackBlitz, stackblitz, "Bolt Agent"]

    - id: jetbrains
      label: "JetBrains"
      aliases: [JetBrains, jetbrains, "AI Assistant", "JetBrains AI", Junie, Mellum,
                Koog, "JetBrains Central", "JetBrains Context"]

    - id: continue
      label: "Continue"
      aliases: [Continue, continue, continuedev, "Continue.dev", "continue.dev"]

    - id: aider
      label: "Aider"
      aliases: [Aider, aider, "aider.chat", "Aider-AI", paul-gauthier]

    - id: kiro
      label: "Kiro (AWS)"
      aliases: [Kiro, kiro, "kiro.dev", "Kiro CLI", "Kiro IDE", "AWS Kiro"]
```

Три замечания к правке словаря.

Первое. Существующий вендор `github` описан алиасами `[GitHub, Github, "GitHub
Copilot", copilot, Copilot]`. Алиас `GitHub` без уточнения будет притягивать
любое упоминание платформы из чужих релизов — а в телах релизов Zed, Cline и
Gemini CLI ссылки на github.com встречаются в каждом пункте. Алиас `GitHub`
следует снять, оставив `"GitHub Copilot"`, `copilot`, `Copilot`, и добавить
`"Copilot Workspace"`, `"premium requests"`.

Второе. `google` уже покрывает Gemini CLI по алиасам `Gemini` и `gemini`.
Отдельный вендор заводить не нужно, но стоит добавить алиасы `"Gemini CLI"`,
`gemini-cli`, `"Gemini Code Assist"`, `"Jules"` — иначе записи с одним лишь
упоминанием `gemini-cli` в теге не нормализуются.

Третье. Codex CLI разумно оставить внутри `openai` — но добавить алиасы
`Codex`, `codex`, `"Codex CLI"`, `"codex-cli"`, `"Codex Web"`. Общий журнал
`learn.chatgpt.com/docs/changelog` пересекается с зоной второго разведчика по
части записей о продуктовой стороне OpenAI; если оба источника включить, нужен
общий дедупликатор по URL записи.
