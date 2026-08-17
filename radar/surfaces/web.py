"""Web surface: static HTML pages built from the signal store.

DR-12 hands this surface the role of the third face, so it lives under the
same rules as the native client (SUR-1, SUR-2, MAC-2): it reads signals and
draws them. It does not filter by significance, does not rerank, never calls a
model and never computes a trend. Everything on the page was decided upstream.

Three pages, one self-contained file each: the digest of a run (FR-9.1), the
run log in words rather than JSON (FR-8.1..FR-8.5, FR-9.2..FR-9.4), and the
corpus itself (DR-9). No server, no fonts, no scripts, no network at render
time or at read time. A page opened by double click on a laptop with the wifi
switched off looks exactly as it does anywhere else, which is the only version
of "works on stage" worth having.

The reference date is an argument. The system clock is never read here: a page
rebuilt tomorrow from the same run must say the same thing it said today.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from radar.db import connect, corpus_readiness, dump_state, read_signals
from radar.models import (
    ChangeType,
    ContextLabel,
    DeltaStatus,
    DatePrecision,
    Fact,
    FactKind,
    Precedent,
    RunSummary,
    Signal,
    SignalType,
    UpcomingDeadline,
)

# -- vocabulary --------------------------------------------------------

MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

MONTHS_NOMINATIVE = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)

DAY_FORMS = ("день", "дня", "дней")
RECORD_FORMS = ("запись", "записи", "записей")
SOURCE_FORMS = ("источник", "источника", "источников")
MATERIAL_FORMS = ("материал", "материала", "материалов")
CALL_FORMS = ("вызов", "вызова", "вызовов")
STATEMENT_FORMS = ("запись", "записи", "записей")

NUMERALS_FEMININE = {
    1: "одну",
    2: "две",
    3: "три",
    4: "четыре",
    5: "пять",
    6: "шесть",
    7: "семь",
    8: "восемь",
    9: "девять",
}

NUMERALS_MASCULINE = {
    1: "один",
    2: "два",
    3: "три",
    4: "четыре",
    5: "пять",
    6: "шесть",
    7: "семь",
    8: "восемь",
    9: "девять",
}

CHANGE_TYPE_LABELS = {
    ChangeType.RELEASE: "релиз",
    ChangeType.BREAKING_CHANGE: "ломающее изменение",
    ChangeType.DEPRECATION: "отключение",
    ChangeType.PRICING: "цены",
    ChangeType.LIMITS: "лимиты",
    ChangeType.SECURITY: "безопасность",
    ChangeType.OTHER: "прочее",
}

# Column headers in the density table, where the full wording wraps.
CHANGE_TYPE_COLUMNS = {
    ChangeType.RELEASE: "релизы",
    ChangeType.BREAKING_CHANGE: "ломающие",
    ChangeType.DEPRECATION: "отключения",
    ChangeType.PRICING: "цены",
    ChangeType.LIMITS: "лимиты",
    ChangeType.SECURITY: "безопасность",
    ChangeType.OTHER: "прочее",
}

FACT_KIND_LABELS = {
    FactKind.VERSION: "Версия",
    FactKind.EFFECTIVE_DATE: "Дата вступления в силу",
    FactKind.SUNSET_DATE: "Дата отключения",
    FactKind.PRICE: "Цена",
    FactKind.LIMIT: "Лимит",
    FactKind.AFFECTED_PRODUCT: "Затронутый продукт",
}

DATE_FACT_KINDS = (FactKind.EFFECTIVE_DATE, FactKind.SUNSET_DATE)

STAGE_LABELS = {
    "collect": "Сбор",
    "cluster": "Кластеризация и дедупликация",
    "filter": "Фильтр релевантности",
    "enrich": "Обогащение",
    "delta": "Краткосрочная дельта",
    "contextualize": "Контекстуализация",
    "trends": "Тренды по корпусу",
    "score": "Скоринг и ранжирование",
    "publish": "Публикация сигналов",
    "observe": "Лог прогона",
    "deliver": "Доставка",
}

# Reason codes as the pipeline writes them (filter.ReasonCode, the three
# enrich drops, and the publication threshold). A code shown raw is a code the
# reader has to decode, and FR-9.2 puts that work on the page instead.
REASON_LABELS = {
    "не_относится_к_стеку": "не относится к стеку",
    "маркетинг_без_фактов": "маркетинг без фактов",
    "дубль_вчерашнего": "дубль вчерашнего",
    "слишком_общее": "слишком общее",
    "спекуляция_без_первоисточника": "спекуляция без первоисточника",
    "другое": "другое",
    "vendor_unresolved": "вендор не опознан по словарю",
    "unsupported_quantifier": "обобщение без числа",
    "statement_unsupported": "утверждение без подтверждающей цитаты",
    "ниже_порога_публикации": "ниже порога публикации",
}

SOURCE_STATUS_LABELS = {
    "ok": "ответил",
    "failed": "не ответил",
    "empty": "ответил, ничего не отдал",
    "skipped": "пропущен",
}

# `empty` covers two different things in the pipeline: a source that returned
# nothing at all and one that returned fewer items than the source config
# expects. «ничего не отдал» next to the number 8 in the same row is a lie the
# reader can see, so the two are worded apart.
SHORT_ANSWER_LABEL = "ответил меньше ожидаемого"

RUN_STATUS_LABELS = {
    "ok": "Прогон завершён",
    "running": "Прогон выполняется",
    "stalled": "Прогон завис",
    "failed": "Прогон не завершился",
    "partial": "Прогон завершён частично",
}

# A run that never wrote `finished_at` is only running while it is fresh.
# radar.supervisor calls the same window a stall; the surface cannot import it
# (SUR-2), so the number is repeated here with the reason it is that number.
STALL_AFTER = timedelta(minutes=30)

MISSING_SUNSET_DATE = "дата отключения в источнике не указана"
NOTHING_LEAD = "Записей за этот день нет"
NOTHING_NOTE = (
    "За этот день не записано ни изменений в вашем стеке, "
    "ни отметки о тихом дне."
)
INFERRED_YEAR_NOTE = "год не указан в источнике"
QUIET_DAY_LEAD = "Сегодня в вашем стеке ничего не изменилось"

# -- page shell --------------------------------------------------------


@dataclass(frozen=True)
class PageLinks:
    """Filenames of the three pages, so they can be renamed as a set."""

    digest: str = "digest.html"
    run_log: str = "run-log.html"
    corpus: str = "corpus.html"


DEFAULT_LINKS = PageLinks()

CSS = """
:root {
  color-scheme: light dark;
  --bg: #faf8f4;
  --panel: #fffdf8;
  --ink: #1f1d1a;
  --ink-soft: #5d564c;
  --ink-faint: #8b8275;
  --line: #e4ded2;
  --line-strong: #cac2b3;
  --accent: #7a4a1c;
  --quote-bg: #f2ede3;
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia,
    "Times New Roman", serif;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue",
    Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
    "Liberation Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #171613;
    --panel: #1e1d19;
    --ink: #e9e4d9;
    --ink-soft: #aba395;
    --ink-faint: #7f7769;
    --line: #33302a;
    --line-strong: #4b463c;
    --accent: #d8a066;
    --quote-bg: #211f1a;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--serif);
  font-size: 1.125rem;
  line-height: 1.62;
  text-rendering: optimizeLegibility;
}
.page { max-width: 42rem; margin: 0 auto; padding: 2.5rem 1.5rem 6rem; }
.page.wide { max-width: 58rem; }
a { color: var(--accent); text-underline-offset: 2px; text-decoration-thickness: 1px; }
a:hover { text-decoration-style: dotted; }
h1, h2, h3 { line-height: 1.24; font-weight: 600; }
h1 { font-size: 1.9rem; margin: 0 0 0.4rem; letter-spacing: -0.01em; }
h2 { font-size: 1.45rem; margin: 0 0 0.5rem; }
h3 {
  font-family: var(--sans);
  font-size: 0.82rem;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--ink-faint);
  font-weight: 600;
  margin: 2.6rem 0 0.8rem;
}
p { margin: 0 0 0.9rem; }
.nav {
  font-family: var(--sans);
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding-bottom: 1.6rem;
  border-bottom: 1px solid var(--line);
  margin-bottom: 2rem;
}
.nav a, .nav .here { margin-right: 1.4rem; }
.nav .here { color: var(--ink); border-bottom: 2px solid var(--accent); padding-bottom: 2px; }
.nav a { color: var(--ink-faint); text-decoration: none; }
.nav a:hover { color: var(--accent); }
.kicker {
  font-family: var(--sans);
  font-size: 0.78rem;
  letter-spacing: 0.11em;
  text-transform: uppercase;
  color: var(--ink-faint);
  margin: 0 0 0.5rem;
}
.masthead { margin-bottom: 2.4rem; }
.masthead .when-line { color: var(--ink-soft); font-size: 1rem; margin: 0; }
.card { border-top: 1px solid var(--line); padding: 1.7rem 0 0.4rem; }
.card.lead { border-top: 2px solid var(--line-strong); padding-top: 1.9rem; }
.card.lead h2 { font-size: 1.6rem; margin-bottom: 0.7rem; }
.lede .when { color: var(--ink); }
.why { color: var(--ink); }
.when-missing { font-family: var(--sans); font-size: 0.92rem; color: var(--ink-soft); }
.label { font-family: var(--sans); font-size: 0.85rem; color: var(--ink-faint); }
.fact { margin: 1rem 0 1.2rem; padding-left: 0.9rem; border-left: 2px solid var(--line-strong); }
.fact-head { margin: 0 0 0.35rem; font-family: var(--sans); font-size: 0.9rem; }
.fact-head .kind { color: var(--ink-faint); }
.fact-head .value { font-family: var(--mono); font-size: 0.88rem; }
blockquote {
  margin: 0 0 0.35rem;
  padding: 0.55rem 0.8rem;
  background: var(--quote-bg);
  font-family: var(--mono);
  font-size: 0.86rem;
  line-height: 1.5;
  border-radius: 2px;
}
.src { margin: 0; font-family: var(--mono); font-size: 0.8rem; word-break: break-word; }
.unverified { font-family: var(--sans); font-size: 0.78rem; color: var(--ink-faint); }
details { margin: 0.9rem 0; }
summary {
  cursor: pointer;
  list-style-position: outside;
}
summary::marker { color: var(--ink-faint); }
summary:hover { color: var(--accent); }
.more { font-family: var(--sans); font-size: 0.85rem; color: var(--accent); }
.card details > summary { font-size: 1.05rem; }
.precedents { margin: 0.9rem 0 0; padding-left: 1.1rem; }
.precedents li { margin-bottom: 1rem; }
.precedents p { margin: 0 0 0.25rem; }
.p-meta { font-family: var(--sans); font-size: 0.82rem; color: var(--ink-faint); }
.meta-row { font-family: var(--sans); font-size: 0.8rem; color: var(--ink-faint); margin: 0.6rem 0 0; }
.sig { font-family: var(--mono); font-size: 0.72rem; color: var(--ink-faint); margin: 0.5rem 0 0; }
.upcoming { list-style: none; padding: 0; margin: 0 0 1.4rem; }
.upcoming li { margin-bottom: 0.5rem; }
.upcoming .when { font-family: var(--mono); font-size: 0.9rem; }
.footer {
  margin-top: 3rem;
  padding-top: 1.2rem;
  border-top: 1px solid var(--line);
  font-family: var(--sans);
  font-size: 0.88rem;
  color: var(--ink-soft);
}
.footer .sig { font-size: 0.72rem; }
.scroll { overflow-x: auto; margin-bottom: 1.4rem; }
table { border-collapse: collapse; width: 100%; font-family: var(--sans); font-size: 0.9rem; }
caption { text-align: left; color: var(--ink-faint); font-size: 0.82rem; padding-bottom: 0.4rem; }
th {
  text-align: left;
  font-weight: 600;
  color: var(--ink-soft);
  border-bottom: 1px solid var(--line-strong);
  padding: 0.4rem 0.7rem 0.4rem 0;
  white-space: nowrap;
}
td { border-bottom: 1px solid var(--line); padding: 0.45rem 0.7rem 0.45rem 0; vertical-align: top; }
td.num, th.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
td.dense { font-weight: 600; color: var(--ink); }
td.empty { color: var(--ink-faint); }
.lead-note { color: var(--ink-soft); font-size: 1rem; }
.legend { font-family: var(--sans); font-size: 0.82rem; color: var(--ink-faint); }
.mono { font-family: var(--mono); }
.big { font-size: 2.6rem; font-family: var(--mono); letter-spacing: -0.02em; }
"""


# -- names on the screen -----------------------------------------------


@dataclass(frozen=True)
class Names:
    """How the page spells the identifiers the store keeps as slugs.

    `labels` maps a whole identifier to the name the theme config gives it;
    `words` maps one word of a slug to the spelling the config uses for it, so
    `google_vertex_release_notes_archive` can become a phrase a person reads
    without anyone inventing a name for it.
    """

    labels: dict[str, str] = field(default_factory=dict)
    words: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.labels or self.words)


NO_NAMES = Names()

_SLUG_SPLIT = re.compile(r"[_\-\s]+")
_CONFIG_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_VENDORS_HEAD = re.compile(r"^(\s*)vendors:\s*$")
_ENTRY_ID = re.compile(r"^\s*-\s+id:\s*[\"']?([A-Za-z0-9_.-]+)[\"']?\s*$")
_ENTRY_LABEL = re.compile(r"^\s*label:\s*(.+?)\s*$")
_ALIASES_HEAD = re.compile(r"^\s*aliases:\s*")


def _add_name(words: dict[str, str], name: str) -> None:
    """Remember a spelling the config states outright.

    Two kinds of evidence count. A name written as one word with a capital
    letter — Anthropic, Pinecone — says how that word is written. A word with
    an upper-case letter past the first — GitHub, OpenAI, API — is a spelling
    no rule derives, so it is kept wherever it appears. A plain word inside a
    longer name proves nothing about the word alone, and «Model Context
    Protocol» must not turn «model retirement schedule» into «Model».
    """
    tokens = _CONFIG_WORD.findall(name)
    if not tokens:
        return
    if len(tokens) == 1 and tokens[0][:1].isupper():
        words.setdefault(tokens[0].lower(), tokens[0])
    for token in tokens:
        if any(char.isupper() for char in token[1:]):
            words.setdefault(token.lower(), token)


def _add_line(words: dict[str, str], line: str) -> None:
    """One line of the vendors block, which may hold several aliases."""
    text = _ALIASES_HEAD.sub("", line).strip().strip("[]")
    for chunk in text.split(","):
        _add_name(words, chunk.strip().strip("\"'[]"))


def theme_names(config: dict[str, Any] | None) -> Names:
    """Names out of an already parsed theme config."""
    labels: dict[str, str] = {}
    words: dict[str, str] = {}
    vendors = ((config or {}).get("corpus") or {}).get("vendors") or []
    for vendor in vendors:
        vendor_id = str(vendor.get("id") or "").strip()
        label = str(vendor.get("label") or "").strip()
        if vendor_id and label:
            labels[vendor_id.lower()] = label
            if label.lower() == label and " " not in label:
                # A vendor whose own name is lower case, n8n among them.
                words.setdefault(vendor_id.lower(), label)
        if label:
            _add_name(words, label)
        for alias in vendor.get("aliases") or []:
            _add_name(words, str(alias))
    return Names(labels=labels, words=words)


def read_theme_names(path: str | Path) -> Names:
    """The same two keys, read straight out of the theme file.

    The surface may not import the config module: that module pulls the
    pipeline in behind it (SUR-2). Inventing a spelling is worse than printing
    a slug, so the id of a vendor and the label written next to it are read
    literally and nothing else in the file is interpreted. A file that is
    missing or shaped otherwise leaves the page with slugs.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return NO_NAMES
    labels: dict[str, str] = {}
    words: dict[str, str] = {}
    indent: int | None = None
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if indent is None:
            head = _VENDORS_HEAD.match(line)
            if head:
                indent = len(head.group(1))
            continue
        if stripped and not stripped.startswith("#"):
            if len(line) - len(line.lstrip()) <= indent:
                break
        if not stripped or stripped.startswith("#"):
            continue
        entry = _ENTRY_ID.match(line)
        if entry:
            current = entry.group(1)
            continue
        label = _ENTRY_LABEL.match(line)
        if label and current:
            value = label.group(1).strip().strip("\"'")
            labels[current.lower()] = value
            if value.lower() == value and " " not in value:
                words.setdefault(current.lower(), value)
            _add_name(words, value)
            continue
        _add_line(words, line)
    return Names(labels=labels, words=words)


def human_name(value: str, names: Names = NO_NAMES) -> str:
    """A slug as a person would write it (voice 3: «Cursor changelog»).

    A whole identifier the config names is printed with that name. Otherwise
    the slug becomes words, each one spelled the way the config spells it, and
    a word the config never mentions keeps its own letters.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    named = names.labels.get(raw.lower())
    if named:
        return named
    parts = [part for part in _SLUG_SPLIT.split(raw) if part]
    if not parts:
        return raw
    out: list[str] = []
    for index, part in enumerate(parts):
        spelled = names.words.get(part.lower())
        if spelled is not None:
            out.append(spelled)
        elif index == 0 and part.isalpha():
            # A word with a digit in it — n8n, gpt4 — is written the way it is
            # written, and capitalising it would misspell a product name.
            out.append(part[:1].upper() + part[1:])
        else:
            out.append(part)
    return " ".join(out)


# -- escaping ----------------------------------------------------------

_SAFE_SCHEMES = ("http://", "https://", "mailto:")
_WHITESPACE_RE = re.compile(r"\s+")


def esc(value: Any) -> str:
    """Everything reaching the page goes through here.

    Source titles are attacker-controlled text (NFR-13): a headline with angle
    brackets must render as characters, never as markup.
    """
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def safe_url(url: str | None) -> str | None:
    """Allow only schemes a browser can follow harmlessly."""
    candidate = (url or "").strip()
    if not candidate:
        return None
    lowered = candidate.lower()
    if not lowered.startswith(_SAFE_SCHEMES):
        return None
    return candidate


def shorten_url(url: str, limit: int = 68) -> str:
    text = re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def link(url: str | None, text: str | None = None) -> str:
    """A link when the URL is usable, plain text when it is not."""
    target = safe_url(url)
    label = text or (shorten_url(url) if url else "")
    if target is None:
        return esc(label)
    return f'<a href="{esc(target)}">{esc(label)}</a>'


def clean(text: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def sentence(text: str) -> str:
    """Capitalize and close a phrase that has to stand on its own line."""
    text = clean(text)
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if not text.endswith((".", "!", "?", ":")):
        text += "."
    return text


# -- numbers and dates -------------------------------------------------


def plural(count: int, forms: tuple[str, str, str]) -> str:
    n = abs(int(count))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def count_phrase(count: int, forms: tuple[str, str, str]) -> str:
    return f"{fmt_int(count)} {plural(count, forms)}"


def spelled_count_phrase(
    count: int,
    forms: tuple[str, str, str],
    numerals: dict[int, str] | None = None,
) -> str:
    """«три записи» reads as prose, «3 записи» reads as a metric."""
    numeral = (numerals or NUMERALS_FEMININE).get(count, fmt_int(count))
    return f"{numeral} {plural(count, forms)}"


def fmt_int(value: int | float) -> str:
    return f"{int(value):,}".replace(",", " ")


def fmt_money(usd: float) -> str:
    if usd and abs(usd) < 0.01:
        # Mixing $0.0040 with $0.21 in one column costs more legibility than
        # the fourth decimal is worth.
        return "< $0.01"
    return f"${usd:.2f}"


def fmt_duration(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    ms = int(ms)
    if ms < 1000:
        return f"{ms} мс"
    seconds = ms / 1000
    if seconds < 10:
        return f"{seconds:.1f}".replace(".", ",") + " с"
    if seconds < 60:
        return f"{round(seconds)} с"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} мин {rest} с"


def parse_dt(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def fmt_time(value: str | datetime | None) -> str:
    moment = parse_dt(value)
    return f"{moment:%H:%M:%S}" if moment else "—"


def parse_date_value(value: str | None) -> tuple[date | None, DatePrecision]:
    """Read a date out of a fact value without inventing one.

    A value that is not a date comes back as None and is printed verbatim.
    """
    text = (value or "").strip()
    if not text:
        return None, DatePrecision.DAY
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text), DatePrecision.DAY
        except ValueError:
            return None, DatePrecision.DAY
    if re.fullmatch(r"\d{4}-\d{2}", text):
        year, month = (int(part) for part in text.split("-"))
        try:
            return date(year, month, 1), DatePrecision.MONTH
        except ValueError:
            return None, DatePrecision.DAY
    if re.fullmatch(r"\d{4}", text):
        return date(int(text), 1, 1), DatePrecision.YEAR
    moment = parse_dt(text)
    if moment is not None:
        return moment.date(), DatePrecision.DAY
    return None, DatePrecision.DAY


def distance_phrase(value: date, today: date) -> str:
    days = (value - today).days
    if days == 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    if days == -1:
        return "вчера"
    if days > 0:
        return f"через {count_phrase(days, DAY_FORMS)}"
    return f"{count_phrase(-days, DAY_FORMS)} назад"


def fmt_date(
    value: date | None,
    today: date,
    precision: DatePrecision = DatePrecision.DAY,
) -> str:
    """Every date carries the distance to it (voice 4).

    Except a date whose year was restored from context: counting days to a
    guessed year would be false precision, so the mark replaces the count.
    """
    if value is None:
        return ""
    if precision is DatePrecision.YEAR:
        return f"{value.year} год"
    if precision is DatePrecision.MONTH:
        return f"{MONTHS_NOMINATIVE[value.month - 1]} {value.year}"
    head = f"{value.day} {MONTHS_GENITIVE[value.month - 1]}"
    if precision is DatePrecision.INFERRED:
        return f"{head} ({INFERRED_YEAR_NOTE})"
    if value.year != today.year:
        head += f" {value.year}"
    return f"{head}, {distance_phrase(value, today)}"


# -- signal reading (display only) -------------------------------------


def fact_date(fact: Fact) -> tuple[date | None, DatePrecision]:
    """The day a fact is about: the parsed field first, its value second.

    `value_date` is the form the core writes, and it wins whenever it is
    there. It is absent on everything published before the field existed, and
    on those records the day still stands in `value`. Reading the field alone
    puts «дата отключения в источнике не указана» one line above a quote that
    names the day, and a product whose argument is honesty cannot afford a
    card that contradicts itself.
    """
    if fact.value_date is not None:
        return fact.value_date, fact.date_precision
    parsed, parsed_precision = parse_date_value(fact.value)
    if parsed is None:
        return None, fact.date_precision
    # A precision the core recorded is a judgment about the source and beats
    # what the string shape suggests; the default one carries no judgment.
    if fact.date_precision is not DatePrecision.DAY:
        return parsed, fact.date_precision
    return parsed, parsed_precision


def signal_date(signal: Signal) -> tuple[date | None, DatePrecision]:
    """The date a card leads with: the first dated fact, in the order given."""
    for fact in signal.facts:
        if fact.kind not in DATE_FACT_KINDS:
            continue
        value, precision = fact_date(fact)
        if value is not None:
            return value, precision
    return None, DatePrecision.DAY


def signal_when(signal: Signal, today: date) -> str:
    """Date line for a card, or the honest admission that there is none."""
    value, precision = signal_date(signal)
    if value is not None:
        return fmt_date(value, today, precision)
    if signal.change_type is ChangeType.DEPRECATION:
        return MISSING_SUNSET_DATE
    return ""


# -- fragments ---------------------------------------------------------


def render_fact(fact: Fact, today: date) -> str:
    label = FACT_KIND_LABELS.get(fact.kind, str(fact.kind))
    when, precision = fact_date(fact)
    if when is not None:
        value = fmt_date(when, today, precision)
        if fact.subject:
            value = f"{value} — {clean(fact.subject)}"
    else:
        value = clean(fact.value)
    parts = [
        '<div class="fact">',
        f'<p class="fact-head"><span class="kind">{esc(label)}:</span> '
        f'<span class="value">{esc(value)}</span></p>',
    ]
    if fact.evidence:
        parts.append(f"<blockquote>«{esc(clean(fact.evidence))}»</blockquote>")
    if fact.source_url:
        parts.append(f'<p class="src">{link(fact.source_url)}</p>')
    if not fact.evidence_verified:
        parts.append(
            '<p class="unverified">цитата не сверена с сохранённым текстом</p>'
        )
    parts.append("</div>")
    return "\n".join(parts)


def render_facts(facts: list[Fact], today: date) -> str:
    if not facts:
        return ""
    body = "\n".join(render_fact(f, today) for f in facts)
    return f'<div class="facts">\n{body}\n</div>'


def render_precedent(precedent: Precedent, today: date) -> str:
    meta = []
    when = fmt_date(precedent.event_date, today, precedent.date_precision)
    if when:
        meta.append(f'<span class="mono">{esc(when)}</span>')
    if precedent.vendor:
        meta.append(esc(precedent.vendor))
    change = CHANGE_TYPE_LABELS.get(precedent.change_type, str(precedent.change_type))
    meta.append(esc(change))
    parts = ["<li>", f"<p>{esc(clean(precedent.text))}</p>"]
    parts.append('<p class="p-meta">' + " · ".join(meta) + "</p>")
    if precedent.source_url:
        parts.append(f'<p class="src">{link(precedent.source_url)}</p>')
    parts.append(f'<p class="sig">{esc(precedent.statement_id)}</p>')
    parts.append("</li>")
    return "\n".join(parts)


def render_context(signal: Signal, today: date) -> str:
    """The DR-8 moment: the claim, and the records it rests on, one click away.

    The sentence is `context_note`, written by the core. A surface composing
    its own would be retelling the corpus, which SUR-2 forbids, and three
    surfaces would word one claim three ways.

    `not_found_in_corpus` prints nothing (voice 8): a label that changes no
    reading teaches the reader to skip labels.
    """
    label = signal.context_label
    if label is None or label is ContextLabel.NOT_FOUND_IN_CORPUS:
        return ""
    if not signal.precedents:
        return ""
    note = clean(signal.context_note)
    more = "Показать " + spelled_count_phrase(len(signal.precedents), RECORD_FORMS)
    items = "\n".join(render_precedent(p, today) for p in signal.precedents)
    head = f"{esc(note)} " if note else ""
    return (
        '<details class="ctx">\n'
        f'<summary>{head}<span class="more">{esc(more)}</span></summary>\n'
        f'<ol class="precedents">\n{items}\n</ol>\n'
        "</details>"
    )


def render_card_body(signal: Signal, today: date) -> str:
    parts: list[str] = []
    when = signal_when(signal, today)
    missing_date = when == MISSING_SUNSET_DATE
    summary = clean(signal.summary)
    lede = []
    if when and not missing_date:
        lede.append(f'<span class="when">{esc(when)}.</span>')
    if summary:
        lede.append(esc(summary))
    if lede:
        parts.append('<p class="lede">' + " ".join(lede) + "</p>")
    if missing_date:
        parts.append(f'<p class="when-missing">{esc(sentence(when))}</p>')
    if signal.why_it_matters:
        parts.append(
            '<p class="why"><span class="label">Почему это важно:</span> '
            f"{esc(clean(signal.why_it_matters))}</p>"
        )
    parts.append(render_facts(signal.facts, today))
    parts.append(render_context(signal, today))
    meta: list[str] = []
    if signal.primary_url:
        meta.append("Первоисточник: " + link(signal.primary_url))
    if signal.days_tracked > 1:
        meta.append("История ведётся " + count_phrase(signal.days_tracked, DAY_FORMS))
    if meta:
        parts.append('<p class="meta-row">' + " · ".join(meta) + "</p>")
    parts.append(f'<p class="sig">{esc(signal.signal_id)}</p>')
    return "\n".join(part for part in parts if part)


def render_card(signal: Signal, today: date, *, lead: bool) -> str:
    """Rank one opens; the rest hold their body behind a native disclosure.

    Attention is distributed unevenly (voice 3), and the page follows that
    without hiding anything: every card is one click from its full evidence.
    """
    headline = esc(clean(signal.headline)) or esc(clean(signal.summary))
    if lead:
        return (
            '<article class="card lead">\n'
            f"<h2>{headline}</h2>\n"
            f"{render_card_body(signal, today)}\n"
            "</article>"
        )
    when = signal_when(signal, today)
    tail = f' <span class="when">— {esc(when)}</span>' if when else ""
    return (
        '<article class="card">\n'
        "<details>\n"
        f"<summary>{headline}{tail}</summary>\n"
        f"{render_card_body(signal, today)}\n"
        "</details>\n"
        "</article>"
    )


def render_run_summary_details(summary: RunSummary | None) -> str:
    """Proof of the check, folded away: it is evidence, not content (voice 2)."""
    if summary is None:
        return ""
    rows = [
        ("Проверено источников", fmt_int(summary.sources_checked)),
        ("Собрано материалов", fmt_int(summary.materials_collected)),
        ("Отклонено материалов", fmt_int(summary.materials_filtered)),
    ]
    body = "\n".join(
        f'<tr><td>{esc(label)}</td><td class="num">{esc(value)}</td></tr>'
        for label, value in rows
    )
    return (
        "<details>\n"
        '<summary><span class="more">Показать статистику прогона</span></summary>\n'
        f'<div class="scroll"><table>\n{body}\n</table></div>\n'
        "</details>"
    )


def checked_sentence(summary: RunSummary | None) -> str:
    """«Проверено 14 источников, 23 материала отклонено.» (voice 2)"""
    if summary is None:
        return ""
    parts = [f"Проверено {count_phrase(summary.sources_checked, SOURCE_FORMS)}"]
    rejected = summary.materials_filtered
    verb = "отклонён" if plural(rejected, MATERIAL_FORMS) == "материал" else "отклонено"
    parts.append(f"{count_phrase(rejected, MATERIAL_FORMS)} {verb}")
    return ", ".join(parts) + "."


def upcoming_entries(signal: Signal, today: date) -> list[UpcomingDeadline]:
    """Deadlines already extracted, nearest first (voice 2).

    `upcoming` is the contract field for this block and wins outright: its
    wording is meant for a reader, while a fact carries the vendor's quote.
    Facts are the fallback for a signal published without the field. Within
    either source, one day and one subject appear once.
    """
    entries: list[UpcomingDeadline] = []
    seen: set[tuple[date, str]] = set()

    def add(item: UpcomingDeadline) -> None:
        key = (item.when, clean(item.what).lower())
        if key in seen:
            return
        seen.add(key)
        entries.append(item)

    for item in signal.upcoming:
        if item.when >= today:
            add(item)
    if entries:
        return sorted(entries, key=lambda item: item.when)

    for fact in signal.facts:
        if fact.kind not in DATE_FACT_KINDS:
            continue
        when, precision = fact_date(fact)
        if when is None or when < today:
            continue
        add(
            UpcomingDeadline(
                when=when,
                what=clean(fact.subject) or clean(fact.evidence),
                source_url=fact.source_url,
                date_precision=precision,
            )
        )
    return sorted(entries, key=lambda item: item.when)


def render_upcoming(signal: Signal, today: date) -> str:
    entries = upcoming_entries(signal, today)
    if not entries:
        # No deadlines in the corpus means no heading either (voice 2).
        return ""
    items = []
    for item in entries:
        when = fmt_date(item.when, today, item.date_precision)
        tail = f" — {esc(clean(item.what))}" if item.what else ""
        source = (
            f' <span class="src">{link(item.source_url)}</span>'
            if item.source_url
            else ""
        )
        items.append(f'<li><span class="when">{esc(when)}</span>{tail}{source}</li>')
    return '<h3>Ближайшее</h3>\n<ul class="upcoming">\n' + "\n".join(items) + "\n</ul>"


# -- page shell --------------------------------------------------------


def render_nav(current: str, links: PageLinks) -> str:
    pages = (
        ("digest", "Сводка", links.digest),
        ("run_log", "Лог прогона", links.run_log),
        ("corpus", "Корпус", links.corpus),
    )
    parts = []
    for key, title, href in pages:
        if key == current:
            parts.append(f'<span class="here">{esc(title)}</span>')
        else:
            parts.append(f'<a href="{esc(href)}">{esc(title)}</a>')
    return '<nav class="nav">' + "".join(parts) + "</nav>"


def document(
    title: str, body: str, *, current: str, links: PageLinks, wide: bool = False
) -> str:
    css_class = "page wide" if wide else "page"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)}</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f'<div class="{css_class}">\n'
        f"{render_nav(current, links)}\n"
        f"{body}\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


# -- run log view ------------------------------------------------------


@dataclass(frozen=True)
class StageRow:
    name: str
    in_count: int = 0
    out_count: int = 0
    started_at: str | None = None
    duration_ms: int | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        """Objects that entered and did not leave.

        Only meaningful when a stage narrows its input; a stage that turns
        14 sources into 61 items drops nothing.
        """
        return max(0, self.in_count - self.out_count) if self.in_count else 0


@dataclass(frozen=True)
class SourceRow:
    source_id: str
    status: str
    items_count: int = 0
    latency_ms: int | None = None
    error: str | None = None

    @property
    def answer(self) -> str:
        """What the source did, in words the number next to them supports."""
        if self.status == "empty" and self.items_count > 0:
            return SHORT_ANSWER_LABEL
        return SOURCE_STATUS_LABELS.get(self.status, self.status)


@dataclass(frozen=True)
class FilteredRow:
    title: str
    url: str
    reason_code: str
    stage: str = ""
    reason_note: str | None = None


@dataclass(frozen=True)
class StageCost:
    stage: str
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0


@dataclass(frozen=True)
class RunHistoryRow:
    run_id: str
    for_date: date | None
    status: str
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    # Both moments travel so a row can say whether the run ever ended: four
    # rows of «Прогон выполняется» under one date is the page reporting a dead
    # run as a live one.
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class RunLogView:
    run_id: str
    for_date: date | None = None
    status: str = "ok"
    started_at: str | None = None
    finished_at: str | None = None
    stages: list[StageRow] = field(default_factory=list)
    sources: list[SourceRow] = field(default_factory=list)
    filtered: list[FilteredRow] = field(default_factory=list)
    costs: list[StageCost] = field(default_factory=list)
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    notes: list[str] = field(default_factory=list)
    delivery: list[dict[str, Any]] = field(default_factory=list)
    history: list[RunHistoryRow] = field(default_factory=list)
    # How many sources the run set out to check. Travels on the signal
    # (`RunSummary.sources_checked`); without it the page cannot tell a source
    # that stayed silent from a source that was never asked.
    sources_configured: int | None = None

    @property
    def itemized_calls(self) -> int:
        return sum(cost.calls for cost in self.costs)

    @property
    def itemized_tokens_in(self) -> int:
        return sum(cost.tokens_in for cost in self.costs)

    @property
    def itemized_tokens_out(self) -> int:
        return sum(cost.tokens_out for cost in self.costs)

    @property
    def itemized_usd(self) -> float:
        return sum(cost.usd for cost in self.costs)

    @property
    def dropped_total(self) -> int:
        return sum(stage.dropped for stage in self.stages)

    @property
    def failed_sources(self) -> list[SourceRow]:
        return [s for s in self.sources if s.status == "failed"]

    @property
    def empty_sources(self) -> list[SourceRow]:
        return [s for s in self.sources if s.status == "empty"]

    @property
    def short_sources(self) -> list[SourceRow]:
        """Answered, and brought back less than the source promised."""
        return [s for s in self.empty_sources if s.items_count > 0]

    @property
    def silent_sources(self) -> list[SourceRow]:
        return [s for s in self.empty_sources if s.items_count == 0]

    @property
    def latest_moment(self) -> datetime | None:
        """The newest moment the store recorded, and the page's «now».

        The system clock is never read here, so the reference for «started
        half an hour ago» comes from the data itself. A page rebuilt next week
        from the same store therefore says what it says today.
        """
        moments = [parse_dt(self.started_at), parse_dt(self.finished_at)]
        for stage in self.stages:
            started = parse_dt(stage.started_at)
            if started is not None:
                moments.append(started + timedelta(milliseconds=stage.duration_ms or 0))
        for row in self.history:
            moments += [parse_dt(row.started_at), parse_dt(row.finished_at)]
        known = [_utc(m) for m in moments if m is not None]
        return max(known) if known else None

    @property
    def superseded(self) -> bool:
        """A later run has started, so this one is not the one running now."""
        started = parse_dt(self.started_at)
        if started is None:
            return False
        return any(
            row.run_id != self.run_id
            and (later := parse_dt(row.started_at)) is not None
            and _utc(later) > _utc(started)
            for row in self.history
        )


def _utc(moment: datetime) -> datetime:
    """Stored moments are ISO with an offset; a bare one is read as UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def run_status_key(
    status: str,
    started_at: str | None,
    finished_at: str | None,
    reference: datetime | None,
    *,
    superseded: bool = False,
) -> str:
    """The status as the page states it, which is not always the stored one.

    A run without `finished_at` is reported as running by the store from the
    second it dies. The distinction between a run in flight and a run that
    hangs is a separate promise of this system, and the page has to make it:
    a later run started, or half an hour passed, and the run is stalled.
    """
    if finished_at or status not in ("running", "started", ""):
        return status
    if superseded:
        return "stalled"
    started = parse_dt(started_at)
    if started is None or reference is None:
        return status
    if _utc(reference) - _utc(started) > STALL_AFTER:
        return "stalled"
    return status


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    parsed, _ = parse_date_value(str(value) if value else "")
    return parsed


def load_run_log(
    conn: sqlite3.Connection, run_id: str | None = None
) -> RunLogView | None:
    """Read the run log tables. Read-only, and no pipeline stage is imported."""
    if run_id is None:
        row = conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        run_id = row["run_id"]
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    try:
        log = json.loads(run["log_json"] or "{}")
    except json.JSONDecodeError:
        log = {}
    stages = [
        StageRow(
            name=str(item.get("stage", "")),
            in_count=int(item.get("in_count") or 0),
            out_count=int(item.get("out_count") or 0),
            started_at=item.get("started_at"),
            duration_ms=item.get("duration_ms"),
            errors=[str(e) for e in item.get("errors", [])],
        )
        for item in log.get("stages", [])
    ]
    sources = [
        SourceRow(
            source_id=row["source_id"],
            status=row["status"],
            items_count=row["items_count"] or 0,
            latency_ms=row["latency_ms"],
            error=row["error"],
        )
        for row in conn.execute(
            "SELECT * FROM source_runs WHERE run_id = ? ORDER BY CASE status "
            "WHEN 'failed' THEN 0 WHEN 'empty' THEN 1 ELSE 2 END, source_id",
            (run_id,),
        )
    ]
    filtered = [
        FilteredRow(
            title=row["title"],
            url=row["url"],
            reason_code=row["reason_code"],
            stage=row["stage"],
            reason_note=row["reason_note"],
        )
        for row in conn.execute(
            "SELECT * FROM filtered_items WHERE run_id = ? ORDER BY stage, title",
            (run_id,),
        )
    ]
    costs = [
        StageCost(
            stage=row["stage"],
            calls=row["calls"],
            tokens_in=row["tokens_in"] or 0,
            tokens_out=row["tokens_out"] or 0,
            usd=row["usd"] or 0.0,
        )
        for row in conn.execute(
            "SELECT stage, COUNT(*) AS calls, SUM(tokens_in) AS tokens_in, "
            "SUM(tokens_out) AS tokens_out, SUM(cost_usd) AS usd FROM model_calls "
            "WHERE run_id = ? GROUP BY stage ORDER BY usd DESC",
            (run_id,),
        )
    ]
    history = [
        RunHistoryRow(
            run_id=row["run_id"],
            for_date=_as_date(row["for_date"]),
            status=row["status"],
            model_calls=row["model_calls"] or 0,
            tokens_in=row["tokens_in"] or 0,
            tokens_out=row["tokens_out"] or 0,
            usd=row["cost_usd"] or 0.0,
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
        for row in conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 12"
        )
    ]
    return RunLogView(
        run_id=run_id,
        for_date=_as_date(run["for_date"]),
        status=run["status"],
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        stages=stages,
        sources=sources,
        filtered=filtered,
        costs=costs,
        model_calls=run["model_calls"] or 0,
        tokens_in=run["tokens_in"] or 0,
        tokens_out=run["tokens_out"] or 0,
        usd=run["cost_usd"] or 0.0,
        notes=[str(note) for note in log.get("notes", [])],
        delivery=list(log.get("delivery", [])),
        history=history,
    )


# -- corpus view -------------------------------------------------------


@dataclass(frozen=True)
class CorpusCell:
    vendor: str
    change_type: str
    count: int


@dataclass(frozen=True)
class CorpusView:
    statements: int = 0
    earliest: date | None = None
    latest: date | None = None
    cells: list[CorpusCell] = field(default_factory=list)
    clusters: int = 0
    trends: int = 0
    signals: int = 0
    runs: int = 0
    dense_threshold: int = 3
    dense_vendors: list[str] = field(default_factory=list)
    ready: bool = False

    @property
    def vendors(self) -> list[str]:
        return sorted({cell.vendor for cell in self.cells})

    @property
    def depth_days(self) -> int | None:
        if self.earliest and self.latest:
            return (self.latest - self.earliest).days
        return None


def load_corpus(
    conn: sqlite3.Connection, config: dict[str, Any] | None = None
) -> CorpusView:
    state = dump_state(conn)
    readiness = corpus_readiness(conn, config or {})
    depth = state.get("corpus_depth") or {}
    cells = [
        CorpusCell(
            vendor=cell.get("vendor") or "—",
            change_type=cell.get("change_type") or "—",
            count=int(cell.get("n") or 0),
        )
        for cell in state.get("cells", [])
    ]
    return CorpusView(
        statements=int(state.get("event_statements") or 0),
        earliest=_as_date(depth.get("earliest")),
        latest=_as_date(depth.get("latest")),
        cells=cells,
        clusters=int(state.get("clusters") or 0),
        trends=int(state.get("trends") or 0),
        signals=int(state.get("signals") or 0),
        runs=int(state.get("runs") or 0),
        dense_threshold=int(readiness.get("required_events_per_cell") or 3),
        dense_vendors=list(readiness.get("vendors_with_dense_cell") or []),
        ready=bool(readiness.get("ready_for_trend_demo")),
    )


# -- digest page -------------------------------------------------------


def _digest_footer(signals: list[Signal], links: PageLinks) -> str:
    """Unanswered sources are a working situation, reported calmly (voice 5).

    The names travel inside the signal (`run_summary`), so the page never
    reaches into the run log tables to build its own footer.
    """
    lines: list[str] = []
    summary = next((s.run_summary for s in signals if s.run_summary), None)
    if summary is not None:
        if summary.sources_failed:
            names = ", ".join(esc(name) for name in summary.sources_failed)
            word = spelled_count_phrase(
                len(summary.sources_failed), SOURCE_FORMS, NUMERALS_MASCULINE
            )
            verb = "не ответил" if len(summary.sources_failed) == 1 else "не ответили"
            lines.append(f"{word.capitalize()} {verb}: {names}.")
        for name in summary.sources_empty:
            # HTTP 200 with nothing in it is a different fault, named apart.
            lines.append(f"{esc(name)} ответил, но ничего не отдал.")
    resolved = [s for s in signals if s.delta_status is DeltaStatus.RESOLVED]
    for signal in resolved:
        tail = ""
        if signal.days_tracked > 1:
            tail = ", история велась " + count_phrase(signal.days_tracked, DAY_FORMS)
        note = clean(signal.delta_note) or clean(signal.headline)
        lines.append(f"Закрыто: {esc(note)}{esc(tail)}.")
    lines.append(f'<a href="{esc(links.run_log)}">Лог прогона</a>.')
    body = "\n".join(f"<p>{line}</p>" for line in lines)
    return f'<div class="footer">\n{body}\n</div>'


def render_digest(
    signals: list[Signal],
    *,
    today: date,
    links: PageLinks = DEFAULT_LINKS,
) -> str:
    """One page for the run, whatever the run turned out to be.

    Quiet day and run failure are states of this page, not separate products
    (SUR-4): the surface renders what the core published.
    """
    ordered = sorted(signals, key=lambda s: (s.rank or 10**6, s.created_at))
    failures = [s for s in ordered if s.signal_type is SignalType.RUN_FAILURE]
    quiet = [s for s in ordered if s.signal_type is SignalType.QUIET_DAY]
    items = [s for s in ordered if s.signal_type is SignalType.DIGEST_ITEM]
    for_date = ordered[0].for_date if ordered else today

    if failures:
        body = _failure_body(failures[0], today)
        title = "Прогон не завершился"
    elif items:
        body = _items_body(items, today)
        title = clean(items[0].headline) or "Изменения в стеке"
    elif quiet:
        body = _quiet_body(quiet[0], today)
        title = "Сегодня без изменений"
    else:
        body = _nothing_body()
        title = NOTHING_LEAD

    head = (
        '<header class="masthead">\n'
        '<p class="kicker">Радар изменений</p>\n'
        f"<h1>{esc(_headline_for(failures, items, quiet, for_date, today))}</h1>\n"
        f'<p class="when-line">{esc(fmt_date(for_date, today))}</p>\n'
        "</header>"
    )
    footer = _digest_footer(ordered, links)
    return document(title, f"{head}\n{body}\n{footer}", current="digest", links=links)


def _headline_for(
    failures: list[Signal],
    items: list[Signal],
    quiet: list[Signal],
    for_date: date,
    today: date,
) -> str:
    if failures:
        return clean(failures[0].headline) or "Прогон не завершился"
    if items:
        return "Изменения в вашем стеке"
    if quiet:
        return clean(quiet[0].headline) or QUIET_DAY_LEAD
    return NOTHING_LEAD


def _items_body(items: list[Signal], today: date) -> str:
    cards = [render_card(items[0], today, lead=True)]
    cards += [render_card(signal, today, lead=False) for signal in items[1:]]
    return "\n".join(cards)


def _quiet_body(signal: Signal, today: date) -> str:
    """The day belongs to the reader; the run statistics come after, as proof."""
    parts: list[str] = []
    if signal.summary:
        parts.append(f'<p class="lead-note">{esc(clean(signal.summary))}</p>')
    parts.append(render_upcoming(signal, today))
    checked = checked_sentence(signal.run_summary)
    if checked:
        parts.append(f'<p class="meta-row">{esc(checked)}</p>')
    parts.append(render_run_summary_details(signal.run_summary))
    parts.append(f'<p class="sig">{esc(signal.signal_id)}</p>')
    return "\n".join(part for part in parts if part)


def _failure_body(signal: Signal, today: date) -> str:
    """A system that reports its own failure earns more than one that hides it."""
    parts: list[str] = []
    reason = clean(signal.failure_reason)
    stage = STAGE_LABELS.get(signal.failure_stage or "", clean(signal.failure_stage))
    if reason:
        parts.append(f'<p class="lead-note">{esc(sentence(reason))}</p>')
    if stage:
        parts.append(f'<p class="meta-row">Стадия: {esc(stage)}.</p>')
    if signal.summary:
        parts.append(f"<p>{esc(clean(signal.summary))}</p>")
    summary = signal.run_summary
    if summary is not None:
        collected = count_phrase(summary.materials_collected, MATERIAL_FORMS)
        line = f"Собрано {esc(collected)}, обработать не удалось."
        if summary.last_success_date is not None:
            when = fmt_date(summary.last_success_date, today)
            line += f" Последняя удачная сводка — {esc(when)}."
        parts.append(f"<p>{line}</p>")
    parts.append(render_upcoming(signal, today))
    parts.append(render_run_summary_details(summary))
    parts.append(f'<p class="sig">{esc(signal.signal_id)}</p>')
    return "\n".join(part for part in parts if part)


def _nothing_body() -> str:
    """The fourth state, in the voice of the other three.

    The day is already in the masthead and the heading already says there is
    nothing, so the body says the one thing neither of them does: what was
    looked for and did not turn up. No storage, no run: the reader has
    neither, and the words on the page are about the reader's day.
    """
    return f'<p class="lead-note">{esc(NOTHING_NOTE)}</p>'


# -- run log page ------------------------------------------------------


def _stage_table(stages: list[StageRow], reasons: dict[str, int]) -> str:
    """FR-8.1 plus the arithmetic FR-8.3 implies.

    A reader subtracts 44 minus 19 in three seconds. The page does the
    subtraction first and says how many of those drops carry a recorded
    reason, so the funnel closes on screen instead of in the reader's head.
    """
    if not stages:
        return "<p>Стадии не записаны.</p>"
    rows = []
    for stage in stages:
        errors = "; ".join(stage.errors) if stage.errors else "—"
        recorded = reasons.get(stage.name, 0)
        dropped = stage.dropped
        rows.append(
            "<tr>"
            f"<td>{esc(STAGE_LABELS.get(stage.name, stage.name))}</td>"
            f'<td class="num">{esc(fmt_time(stage.started_at))}</td>'
            f'<td class="num">{esc(fmt_duration(stage.duration_ms))}</td>'
            f'<td class="num">{esc(fmt_int(stage.in_count))}</td>'
            f'<td class="num">{esc(fmt_int(stage.out_count))}</td>'
            f'<td class="num">{esc(fmt_int(dropped) if dropped else "—")}</td>'
            f'<td class="num">{esc(fmt_int(recorded) if recorded else "—")}</td>'
            f"<td>{esc(errors)}</td>"
            "</tr>"
        )
    return (
        '<div class="scroll"><table>\n'
        '<thead><tr><th>Стадия</th><th class="num">Начало</th>'
        '<th class="num">Длительность</th><th class="num">Вошло</th>'
        '<th class="num">Вышло</th><th class="num">Отсеяно</th>'
        '<th class="num">Причин записано</th><th>Ошибки</th></tr></thead>\n'
        "<tbody>\n" + "\n".join(rows) + "\n</tbody></table></div>"
    )


def funnel_sentence(run: RunLogView) -> str:
    """Say the discrepancy out loud, or say that there is none."""
    dropped = run.dropped_total
    recorded = len(run.filtered)
    if dropped == 0 and recorded == 0:
        return "Ни один материал не отсеян."
    if recorded == dropped:
        return (
            f"Отсеяно {count_phrase(dropped, MATERIAL_FORMS)}, "
            "причина записана у каждого."
        )
    if recorded < dropped:
        gap = dropped - recorded
        return (
            f"Стадии отсеяли {count_phrase(dropped, MATERIAL_FORMS)}, "
            f"причины записаны у {fmt_int(recorded)}. "
            f"У {count_phrase(gap, MATERIAL_FORMS)} причина не записана."
        )
    return (
        f"Причин записано {fmt_int(recorded)} при "
        f"{count_phrase(dropped, MATERIAL_FORMS)} по счётчикам стадий: "
        "часть материалов отклонена на нескольких стадиях."
    )


def sources_sentence(run: RunLogView) -> str:
    """Seven rows under a run that set out to check fourteen reads as loss."""
    answered = len(run.sources)
    counts = {"ok": 0, "empty": 0, "failed": 0}
    for source in run.sources:
        counts[source.status] = counts.get(source.status, 0) + 1
    configured = run.sources_configured
    if configured and configured != answered:
        head = (
            f"В прогоне участвовало {count_phrase(configured, SOURCE_FORMS)}, "
            f"результат записан по {fmt_int(answered)}"
        )
    else:
        head = f"Опрошено {count_phrase(answered, SOURCE_FORMS)}"
    parts = []
    if counts.get("ok"):
        parts.append(
            f"{fmt_int(counts['ok'])} "
            + ("ответил" if counts["ok"] == 1 else "ответили")
        )
    if counts.get("failed"):
        verb = "не ответил" if counts["failed"] == 1 else "не ответили"
        parts.append(f"{fmt_int(counts['failed'])} {verb}")
    # The two halves of `empty` are counted apart for the same reason the
    # table words them apart: a source that brought eight items back did not
    # answer with nothing.
    short = len(run.short_sources)
    if short:
        verb = "ответил" if short == 1 else "ответили"
        parts.append(f"{fmt_int(short)} {verb} меньше ожидаемого")
    silent = len(run.silent_sources)
    if silent:
        verb = "ответил" if silent == 1 else "ответили"
        parts.append(f"{fmt_int(silent)} {verb} без записей")
    if not parts:
        return head + "."
    return head + ": " + ", ".join(parts) + "."


def _source_table(sources: list[SourceRow], names: Names = NO_NAMES) -> str:
    if not sources:
        return "<p>Источники в этом прогоне не опрашивались.</p>"
    rows = []
    for source in sources:
        latency = (
            fmt_duration(source.latency_ms) if source.latency_ms is not None else "—"
        )
        rows.append(
            "<tr>"
            f"<td>{esc(human_name(source.source_id, names))}</td>"
            f"<td>{esc(source.answer)}</td>"
            f'<td class="num">{esc(fmt_int(source.items_count))}</td>'
            f'<td class="num">{esc(latency)}</td>'
            f"<td>{esc(source.error or '—')}</td>"
            "</tr>"
        )
    return (
        '<div class="scroll"><table>\n'
        "<thead><tr><th>Источник</th><th>Ответ</th>"
        '<th class="num">Материалов</th><th class="num">Время ответа</th>'
        # Covers both a refusal and a 200 that carried nothing: the second is
        # not a refusal, and the column must not call it one.
        "<th>Причина</th></tr></thead>\n"
        "<tbody>\n" + "\n".join(rows) + "\n</tbody></table></div>"
    )


def _reason_text(row: FilteredRow) -> str:
    text = REASON_LABELS.get(row.reason_code, row.reason_code.replace("_", " "))
    if row.reason_note:
        text += f" ({row.reason_note})"
    return text


def _filtered_block(filtered: list[FilteredRow]) -> str:
    if not filtered:
        return "<p>Ни один материал не отклонён.</p>"
    rows = []
    for row in filtered:
        rows.append(
            "<tr>"
            f"<td>{link(row.url, clean(row.title) or shorten_url(row.url))}</td>"
            f"<td>{esc(_reason_text(row))}</td>"
            f"<td>{esc(STAGE_LABELS.get(row.stage, row.stage))}</td>"
            "</tr>"
        )
    count = count_phrase(len(filtered), MATERIAL_FORMS)
    return (
        "<details>\n"
        f'<summary>Отклонено {esc(count)}. <span class="more">Показать список</span></summary>\n'
        '<div class="scroll"><table>\n'
        "<thead><tr><th>Материал</th><th>Причина</th><th>Стадия</th></tr></thead>\n"
        "<tbody>\n" + "\n".join(rows) + "\n</tbody></table></div>\n"
        "</details>"
    )


def _cost_block(run: RunLogView) -> str:
    """FR-8.4. The total is the sum of the itemized calls, by construction.

    `runs.model_calls` is an in-memory counter and `model_calls` is the record
    of each call; a resumed run makes them disagree. The page adds up the
    records it shows and names the counter separately when the two differ,
    because a total that does not match the table below it destroys the only
    thing this page is for.
    """
    itemized = bool(run.costs)
    calls = run.itemized_calls if itemized else run.model_calls
    tokens_in = run.itemized_tokens_in if itemized else run.tokens_in
    tokens_out = run.itemized_tokens_out if itemized else run.tokens_out
    usd = run.itemized_usd if itemized else run.usd
    head = (
        f"Вызовов модели: {fmt_int(calls)}. "
        f"Токены: {fmt_int(tokens_in)} на вход, {fmt_int(tokens_out)} на выход. "
        f"Стоимость прогона: {fmt_money(usd)}."
    )
    parts = [f"<p>{esc(head)}</p>"]
    if itemized and (run.model_calls != calls or abs(run.usd - usd) > 0.005):
        note = (
            f"Счётчик прогона называет {count_phrase(run.model_calls, CALL_FORMS)} "
            f"и {fmt_money(run.usd)}. На странице сложены подробные записи: "
            f"{count_phrase(calls, CALL_FORMS)}, {fmt_money(usd)}."
        )
        parts.append(f'<p class="legend">{esc(note)}</p>')
    if run.costs:
        rows = "\n".join(
            "<tr>"
            f"<td>{esc(STAGE_LABELS.get(cost.stage, cost.stage))}</td>"
            f'<td class="num">{esc(fmt_int(cost.calls))}</td>'
            f'<td class="num">{esc(fmt_int(cost.tokens_in))}</td>'
            f'<td class="num">{esc(fmt_int(cost.tokens_out))}</td>'
            f'<td class="num">{esc(fmt_money(cost.usd))}</td>'
            "</tr>"
            for cost in run.costs
        )
        total = (
            "<tr>"
            "<td>Всего</td>"
            f'<td class="num dense">{esc(fmt_int(calls))}</td>'
            f'<td class="num dense">{esc(fmt_int(tokens_in))}</td>'
            f'<td class="num dense">{esc(fmt_int(tokens_out))}</td>'
            f'<td class="num dense">{esc(fmt_money(usd))}</td>'
            "</tr>"
        )
        parts.append(
            '<div class="scroll"><table>\n'
            '<thead><tr><th>Стадия</th><th class="num">Вызовов</th>'
            '<th class="num">Токенов на вход</th><th class="num">Токенов на выход</th>'
            '<th class="num">Стоимость</th></tr></thead>\n'
            "<tbody>\n" + rows + "\n" + total + "\n</tbody></table></div>"
        )
    return "\n".join(parts)


def _history_block(
    history: list[RunHistoryRow],
    today: date,
    current: str,
    reference: datetime | None = None,
) -> str:
    if len(history) < 2:
        return ""
    starts = [_utc(m) for m in (parse_dt(r.started_at) for r in history) if m]
    newest = max(starts) if starts else None
    rows = []
    for run in history:
        mark = " (этот прогон)" if run.run_id == current else ""
        when = fmt_date(run.for_date, today) if run.for_date else "—"
        started = parse_dt(run.started_at)
        status = run_status_key(
            run.status,
            run.started_at,
            run.finished_at,
            reference,
            superseded=bool(newest and started is not None and _utc(started) < newest),
        )
        rows.append(
            "<tr>"
            f"<td>{esc(when + mark)}</td>"
            f"<td>{esc(RUN_STATUS_LABELS.get(status, status))}</td>"
            f'<td class="num">{esc(fmt_int(run.model_calls))}</td>'
            f'<td class="num">{esc(fmt_int(run.tokens_in + run.tokens_out))}</td>'
            f'<td class="num">{esc(fmt_money(run.usd))}</td>'
            "</tr>"
        )
    return (
        "<h3>Предыдущие прогоны</h3>\n"
        '<div class="scroll"><table>\n'
        '<thead><tr><th>Дата</th><th>Итог</th><th class="num">Вызовов модели</th>'
        '<th class="num">Токенов</th><th class="num">Стоимость</th></tr></thead>\n'
        "<tbody>\n" + "\n".join(rows) + "\n</tbody></table></div>"
    )


def render_run_log(
    run: RunLogView | None,
    *,
    today: date,
    links: PageLinks = DEFAULT_LINKS,
    names: Names = NO_NAMES,
    now: datetime | None = None,
) -> str:
    """FR-9.2: the log is read by a person without technical training.

    Which means words, tables and reasons. No JSON on the page, no stack
    traces, no identifiers where a name will do.
    """
    if run is None:
        body = (
            '<header class="masthead"><p class="kicker">Радар изменений</p>'
            "<h1>Логов прогонов нет</h1></header>\n"
            "<p>В хранилище пока не записан ни один прогон.</p>"
        )
        return document("Лог прогона", body, current="run_log", links=links, wide=True)

    reference = now or run.latest_moment
    status_key = run_status_key(
        run.status,
        run.started_at,
        run.finished_at,
        reference,
        superseded=run.superseded,
    )
    status = RUN_STATUS_LABELS.get(status_key, status_key)
    when = fmt_date(run.for_date, today) if run.for_date else ""
    started = fmt_time(run.started_at)
    finished = fmt_time(run.finished_at)
    if run.finished_at:
        timing = f"Начало {started}, окончание {finished}."
    elif status_key == "stalled":
        timing = f"Начало {started}, окончание не записано."
    else:
        timing = f"Начало {started}."

    head = (
        '<header class="masthead">\n'
        '<p class="kicker">Лог прогона</p>\n'
        f"<h1>{esc(status)}</h1>\n"
        f'<p class="when-line">{esc(when)}. {esc(timing)}</p>\n'
        "</header>"
    )
    reasons: dict[str, int] = {}
    for row in run.filtered:
        reasons[row.stage] = reasons.get(row.stage, 0) + 1
    parts = [
        head,
        "<h3>Стадии</h3>",
        _stage_table(run.stages, reasons),
        f'<p class="legend">{esc(funnel_sentence(run))}</p>',
        "<h3>Источники</h3>",
        f"<p>{esc(sources_sentence(run))}</p>",
        _source_table(run.sources, names),
        "<h3>Отклонённые материалы</h3>",
        _filtered_block(run.filtered),
        "<h3>Стоимость</h3>",
        _cost_block(run),
    ]
    if run.notes:
        notes = "\n".join(f"<li>{esc(note)}</li>" for note in run.notes)
        parts += ["<h3>Замечания</h3>", f"<ul>\n{notes}\n</ul>"]
    if run.delivery:
        rows = "\n".join(
            "<li>"
            + esc(str(item.get("channel", "")))
            + " — "
            + esc(str(item.get("status", "")))
            + (f" ({esc(str(item.get('error')))})" if item.get("error") else "")
            + "</li>"
            for item in run.delivery
        )
        parts += ["<h3>Доставка</h3>", f"<ul>\n{rows}\n</ul>"]
    history = _history_block(run.history, today, run.run_id, reference)
    if history:
        parts.append(history)
    parts.append(
        '<div class="footer">\n'
        f'<p><a href="{esc(links.digest)}">Сводка этого прогона</a> · '
        f'<a href="{esc(links.corpus)}">Корпус</a></p>\n'
        f'<p class="sig">{esc(run.run_id)}</p>\n'
        "</div>"
    )
    title = f"Лог прогона — {when}" if when else "Лог прогона"
    return document(title, "\n".join(parts), current="run_log", links=links, wide=True)


# -- corpus page -------------------------------------------------------


def _density_table(corpus: CorpusView, names: Names = NO_NAMES) -> str:
    if not corpus.cells:
        return "<p>Корпус пуст.</p>"
    order = [str(ct) for ct in ChangeType]
    present = [t for t in order if any(c.change_type == t for c in corpus.cells)]
    present += sorted({c.change_type for c in corpus.cells} - set(present))
    totals: dict[str, int] = {}
    grid: dict[tuple[str, str], int] = {}
    for cell in corpus.cells:
        grid[(cell.vendor, cell.change_type)] = cell.count
        totals[cell.vendor] = totals.get(cell.vendor, 0) + cell.count
    vendors = sorted(totals, key=lambda v: (-totals[v], v))
    # ChangeType keys are enum members; the label lookup takes the raw value.
    header = "".join(
        f'<th class="num">{esc(_change_label(t, CHANGE_TYPE_COLUMNS))}</th>'
        for t in present
    )
    rows = []
    for vendor in vendors:
        cells = []
        for change_type in present:
            count = grid.get((vendor, change_type), 0)
            if count == 0:
                cells.append('<td class="num empty">—</td>')
            elif count >= corpus.dense_threshold:
                cells.append(f'<td class="num dense">{esc(fmt_int(count))}</td>')
            else:
                cells.append(f'<td class="num">{esc(fmt_int(count))}</td>')
        rows.append(
            f"<tr><td>{esc(human_name(vendor, names))}</td>"
            + "".join(cells)
            + f'<td class="num">{esc(fmt_int(totals[vendor]))}</td></tr>'
        )
    legend = (
        f"Ячейки, где записей не меньше {fmt_int(corpus.dense_threshold)}, "
        "выделены: по ним ретривал находит прецеденты."
    )
    return (
        '<div class="scroll"><table>\n'
        f'<thead><tr><th>Вендор</th>{header}<th class="num">Всего</th></tr></thead>\n'
        "<tbody>\n" + "\n".join(rows) + "\n</tbody></table></div>\n"
        f'<p class="legend">{esc(legend)}</p>'
    )


def _change_label(value: str, labels: dict[ChangeType, str] | None = None) -> str:
    labels = labels or CHANGE_TYPE_LABELS
    for member in ChangeType:
        if str(member) == value:
            return labels[member]
    return value


def render_corpus(
    corpus: CorpusView,
    *,
    today: date,
    links: PageLinks = DEFAULT_LINKS,
    names: Names = NO_NAMES,
) -> str:
    """DR-9: the volume figure is the proof the system outlives one question."""
    depth = ""
    if corpus.earliest and corpus.latest:
        depth = (
            f"От {fmt_date(corpus.earliest, today)}, "
            f"до {fmt_date(corpus.latest, today)}. "
            f"Глубина {count_phrase(corpus.depth_days or 0, DAY_FORMS)}."
        )
    head = (
        '<header class="masthead">\n'
        '<p class="kicker">Корпус событий</p>\n'
        f'<h1><span class="big">{esc(fmt_int(corpus.statements))}</span> '
        f"{esc(plural(corpus.statements, STATEMENT_FORMS))}</h1>\n"
        f'<p class="when-line">{esc(depth)}</p>\n'
        "</header>"
    )
    vendors = corpus.vendors
    parts = [head]
    if vendors:
        parts.append(
            f"<p>{esc(count_phrase(len(vendors), ('вендор', 'вендора', 'вендоров')))}: "
            + ", ".join(esc(human_name(v, names)) for v in vendors)
            + ".</p>"
        )
    readiness = (
        "Плотных ячеек хватает: прецеденты находятся у "
        + count_phrase(len(corpus.dense_vendors), ("вендора", "вендоров", "вендоров"))
        + "."
        if corpus.ready
        else "Плотных ячеек пока мало, часть событий останется без прецедентов."
    )
    parts.append(f'<p class="legend">{esc(readiness)}</p>')
    parts += ["<h3>Плотность по ячейкам</h3>", _density_table(corpus, names)]
    parts += [
        "<h3>Остальные слои хранилища</h3>",
        '<div class="scroll"><table>\n<tbody>\n'
        f'<tr><td>Сюжетов в оперативном слое</td><td class="num">{esc(fmt_int(corpus.clusters))}</td></tr>\n'
        f'<tr><td>Трендов</td><td class="num">{esc(fmt_int(corpus.trends))}</td></tr>\n'
        f'<tr><td>Опубликованных сигналов</td><td class="num">{esc(fmt_int(corpus.signals))}</td></tr>\n'
        f'<tr><td>Прогонов</td><td class="num">{esc(fmt_int(corpus.runs))}</td></tr>\n'
        "</tbody></table></div>",
    ]
    parts.append(
        '<div class="footer">\n'
        f'<p><a href="{esc(links.digest)}">Сводка</a> · '
        f'<a href="{esc(links.run_log)}">Лог прогона</a></p>\n'
        "</div>"
    )
    return document(
        "Корпус событий", "\n".join(parts), current="corpus", links=links, wide=True
    )


# -- assembly ----------------------------------------------------------


def build_site(
    db_path: str | Path,
    out_dir: str | Path,
    *,
    today: date,
    run_id: str | None = None,
    config: dict[str, Any] | None = None,
    links: PageLinks = DEFAULT_LINKS,
    names: Names | None = None,
    now: datetime | None = None,
) -> dict[str, Path]:
    """Write the three pages. Read-only on the store, by connection mode."""
    names = names if names is not None else theme_names(config)
    conn = connect(db_path, read_only=True)
    try:
        signals = read_signals(conn, run_id)
        target_run = run_id or (signals[0].run_id if signals else None)
        run = load_run_log(conn, target_run)
        corpus = load_corpus(conn, config)
    finally:
        conn.close()

    # How many sources the run set out to check lives in the contract rather
    # than in the log tables, and only the signals carry it.
    checked = next(
        (s.run_summary.sources_checked for s in signals if s.run_summary), None
    )
    if run is not None and checked:
        run = replace(run, sources_configured=checked)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pages = {
        "digest": (
            out / links.digest,
            render_digest(signals, today=today, links=links),
        ),
        "run_log": (
            out / links.run_log,
            render_run_log(run, today=today, links=links, names=names, now=now),
        ),
        "corpus": (
            out / links.corpus,
            render_corpus(corpus, today=today, links=links, names=names),
        ),
    }
    for path, html_text in pages.values():
        path.write_text(html_text, encoding="utf-8")
    return {name: path for name, (path, _) in pages.items()}


def reference_date(
    db_path: str | Path, run_id: str | None = None, override: date | None = None
) -> date | None:
    """The date relative dates are measured from, taken from the data.

    Never the system clock: a page rebuilt a week later has to say what it said
    on the morning it was published.
    """
    if override is not None:
        return override
    conn = connect(db_path, read_only=True)
    try:
        signals = read_signals(conn, run_id)
        if signals:
            return signals[0].for_date
        query = "SELECT for_date FROM runs"
        params: tuple[Any, ...] = ()
        if run_id:
            query += " WHERE run_id = ?"
            params = (run_id,)
        query += " ORDER BY started_at DESC, rowid DESC LIMIT 1"
        row = conn.execute(query, params).fetchone()
    finally:
        conn.close()
    return _as_date(row["for_date"]) if row else None


def main(argv: list[str] | None = None) -> int:
    """Build the pages for the latest run: `python -m radar.surfaces.web`."""
    parser = argparse.ArgumentParser(
        description="Собрать статические страницы поверхности из хранилища сигналов"
    )
    parser.add_argument("--db", default="data/radar.db", help="файл хранилища")
    parser.add_argument("--out", default="out", help="каталог для страниц")
    parser.add_argument(
        "--run-id", default=None, help="прогон; по умолчанию последний записанный"
    )
    parser.add_argument(
        "--today",
        default=None,
        help="опорная дата ISO; по умолчанию дата прогона из хранилища",
    )
    parser.add_argument(
        "--config",
        default="config/ai-tools.yaml",
        help="файл темы; из него берутся имена вендоров для страниц",
    )
    parser.add_argument(
        "--now",
        default=None,
        help=(
            "опорный момент ISO для отличия идущего прогона от зависшего; "
            "по умолчанию последний момент, записанный в хранилище"
        ),
    )
    args = parser.parse_args(argv)

    override = date.fromisoformat(args.today) if args.today else None
    try:
        today = reference_date(args.db, args.run_id, override)
    except sqlite3.OperationalError as error:
        parser.error(f"хранилище {args.db} не открывается на чтение: {error}")
    if today is None:
        parser.error("в хранилище нет ни сигналов, ни прогонов; укажите --today явно")

    moment = datetime.fromisoformat(args.now) if args.now else None
    paths = build_site(
        args.db,
        args.out,
        today=today,
        run_id=args.run_id,
        names=read_theme_names(args.config),
        now=moment,
    )
    print(f"опорная дата: {today.isoformat()}")
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
