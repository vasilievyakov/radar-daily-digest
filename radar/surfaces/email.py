"""Email surface: the digest with its evidence unfolded (PRD 10.3).

Telegram is the sixty-second morning scan. The letter is the other half of the
pair: everything above the bar arrives as a full card, every fact carries its
verbatim quote and its link, every context label brings the precedents that
produced it. S2 ends here — a link from Telegram lands on this text.

The surface only renders (SUR-2). It never filters by significance, re-ranks,
calls a model, computes trends or enriches facts. Capacity is expressed through
the tier the core already assigned: the letter takes LEAD and STANDARD, the
Telegram surface takes LEAD alone. No threshold lives here.

Imports are limited to radar.models, radar.db and the standard library; the
static test in tests/test_surface_email.py enforces that.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from pathlib import Path

from radar.db import connect, read_signals
from radar.models import (
    SIGNAL_SCHEMA_VERSION,
    ChangeType,
    ContextLabel,
    DatePrecision,
    DeltaStatus,
    Fact,
    Precedent,
    RunSummary,
    Signal,
    SignalType,
    Tier,
)

# The letter's capacity, expressed in tiers assigned by the core.
EMAIL_TIERS: tuple[Tier, ...] = (Tier.LEAD, Tier.STANDARD)

SUBJECT_MAX = 72
TEXT_WIDTH = 74
WIDTH_PX = 600
SMTP_TIMEOUT = 30

# Mail clients drop web fonts and invert unstyled surfaces in dark mode, so the
# font stack stays local and every element carries explicit colours.
FONT = "Helvetica, Arial, sans-serif"
C_PAGE = "#f4f4f5"
C_CARD = "#ffffff"
C_HEAD = "#111111"
C_TEXT = "#1f1f1f"
C_MUTED = "#6b6b6b"
C_RULE = "#e4e4e7"
C_LINK = "#14508c"
C_QUOTE_BG = "#f7f7f8"
C_QUOTE_BAR = "#c9c9ce"

QUIET_INTRO = "Сегодня в вашем стеке ничего не изменилось."
QUIET_SUBJECT = "Сегодня без изменений"
FAILURE_SUBJECT = "Прогон не завершился"
FALLBACK_SUBJECT = "Изменения в вашем стеке"
RUN_LOG_LABEL = "Лог прогона"

MONTHS_GEN = (
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
MONTHS_NOM = (
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

FACT_LABELS = {
    "version": "Версия",
    "effective_date": "Вступает в силу",
    "sunset_date": "Дата отключения",
    "price": "Цена",
    "limit": "Лимит",
    "affected_product": "Затронутый продукт",
}

# FR-4 keeps a date the source never stated out of the card; the surface says
# so in words instead of substituting anything.
MISSING_DATE = {
    "sunset_date": "дата отключения в источнике не указана",
    "effective_date": "дата вступления в силу в источнике не указана",
}

CHANGE_TYPES_RU = {
    ChangeType.RELEASE: "релиз",
    ChangeType.BREAKING_CHANGE: "ломающее изменение",
    ChangeType.DEPRECATION: "отключение",
    ChangeType.PRICING: "цены",
    ChangeType.LIMITS: "лимиты",
    ChangeType.SECURITY: "безопасность",
    ChangeType.OTHER: "прочее",
}

# not_found_in_corpus is deliberately absent: a label that changes nothing in
# the reading teaches the reader to skip labels (voice.md, section 8).
CONTEXT_TITLES = {
    ContextLabel.RECURRING: "Похожее уже случалось",
    ContextLabel.ESCALATION: "Сюжет усиливается",
    ContextLabel.TREND_MEMBER: "Часть наблюдаемого тренда",
}

DELTA_TITLES = {
    DeltaStatus.NEW: "Появилось сегодня",
    DeltaStatus.CONTINUING: "Продолжение",
    DeltaStatus.UPDATED: "Изменилось со вчера",
    DeltaStatus.RESOLVED: "Закрыто",
}

# The core owns the key names in Signal.stats; the surface reads the ones it
# knows and stays silent about the rest.
SOURCE_KEYS = ("sources_checked", "sources_ok", "sources_total", "sources")
REJECTED_KEYS = ("filtered_out", "rejected", "items_filtered", "filtered")
COLLECTED_KEYS = ("collected", "items_collected", "raw_items", "materials")

REQUIRED_ENV = ("SMTP_HOST", "SMTP_FROM", "SMTP_TO")


class EmailConfigError(RuntimeError):
    """Delivery settings are incomplete. Never raised past send_digest()."""


# --------------------------------------------------------------------------
# Language helpers
# --------------------------------------------------------------------------


def _plural(n: int, one: str, few: str, many: str) -> str:
    tail = abs(n) % 100
    if 11 <= tail <= 14:
        return many
    last = tail % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _count(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {_plural(n, one, few, many)}"


_SPELLED = ("один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")


def _spelled_count(n: int, one: str, few: str, many: str) -> str:
    """«Два источника не ответили» — small counts are words in a sentence."""
    word = _SPELLED[n - 1] if 1 <= n <= len(_SPELLED) else str(n)
    return f"{word} {_plural(n, one, few, many)}"


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        return ""
    return text if text[-1] in ".!?…:" else text + "."


def _relative(days: int) -> str:
    if days == 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    if days == -1:
        return "вчера"
    if days > 0:
        return f"через {_count(days, 'день', 'дня', 'дней')}"
    return f"{_count(-days, 'день', 'дня', 'дней')} назад"


def _date_phrase(
    value: date | None,
    today: date,
    precision: DatePrecision = DatePrecision.DAY,
) -> str:
    """A date never travels without the distance to it (voice.md, section 4)."""
    if value is None:
        return ""
    days = (value - today).days
    relative = _relative(days)
    note = ""
    if precision is DatePrecision.MONTH:
        base = f"{MONTHS_NOM[value.month - 1]} {value.year}"
        note = "день в источнике не указан"
    elif precision is DatePrecision.YEAR:
        base = str(value.year)
        note = "месяц в источнике не указан"
    else:
        base = f"{value.day} {MONTHS_GEN[value.month - 1]}"
        if precision is DatePrecision.INFERRED:
            note = "год не указан в источнике"
        if days in (-1, 0, 1):
            base = ""
    phrase = f"{base}, {relative}" if base else relative
    return f"{phrase} ({note})" if note else phrase


def _parse_date_value(raw: str) -> tuple[date, DatePrecision] | None:
    """Read a date out of a fact value without guessing the missing parts."""
    text = raw.strip()
    if not text:
        return None
    parts = text.split("-")
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2])), DatePrecision.DAY
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1), DatePrecision.MONTH
        if len(parts) == 1 and len(text) == 4:
            return date(int(text), 1, 1), DatePrecision.YEAR
    except ValueError:
        return None
    return None


def _truncate_words(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 3:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def _display_url(url: str, limit: int = 62) -> str:
    shown = url.split("://", 1)[-1].rstrip("/")
    return shown if len(shown) <= limit else shown[: limit - 1] + "…"


def _safe_url(url: str | None) -> str:
    """Only http(s) leaves the surface as a link."""
    if not url:
        return ""
    candidate = url.strip()
    if any(ch.isspace() for ch in candidate):
        return ""
    lowered = candidate.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return candidate
    return ""


def _wrap(text: str, indent: str = "", hang: str | None = None) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        stripped = para.strip()
        if not stripped:
            lines.append("")
            continue
        lines.extend(
            textwrap.wrap(
                stripped,
                width=TEXT_WIDTH,
                initial_indent=indent,
                subsequent_indent=indent if hang is None else hang,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [indent.rstrip()]
        )
    return lines


def _stat(stats: Mapping[str, int], keys: Sequence[str]) -> int | None:
    for key in keys:
        if key in stats:
            return int(stats[key])
    return None


# --------------------------------------------------------------------------
# View model: built once, rendered twice
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _FactView:
    label: str
    quote: str
    url: str


@dataclass(frozen=True)
class _PrecedentView:
    date_text: str
    text: str
    meta: str
    url: str


@dataclass(frozen=True)
class _CardView:
    headline: str
    summary: str
    why: str
    facts: list[_FactView]
    # The sentence above the precedent list, written by the core, and the
    # heading used only when a signal was published without it.
    context_note: str
    context_title: str
    precedents: list[_PrecedentView]
    delta: str
    primary_url: str
    signal_id: str


@dataclass(frozen=True)
class _UpcomingView:
    date_text: str
    text: str
    url: str
    # A verbatim quote from a source is shown as one; a line the core wrote
    # for the reader is not dressed up as a quotation.
    quoted: bool = False


@dataclass(frozen=True)
class _BlockView:
    """Quiet day or run failure: one intro plus its proof lines."""

    intro: str
    detail: str
    upcoming: list[_UpcomingView]
    stats_line: str
    signal_id: str


@dataclass(frozen=True)
class _LetterView:
    subject: str
    header: str
    failure: _BlockView | None
    cards: list[_CardView]
    quiet: _BlockView | None
    # Sources that did not answer, named in the footer (voice.md, section 5).
    sources_line: str
    closing: str
    run_log_url: str
    schema_note: str


@dataclass(frozen=True)
class EmailDigest:
    subject: str
    text: str
    html: str
    signal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryResult:
    """Delivery failure is returned, never raised (SUR-5)."""

    delivered: bool
    subject: str
    recipients: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]
    user: str = ""
    password: str = ""


def select_cards(signals: Sequence[Signal]) -> list[Signal]:
    """Capacity by tier, order untouched (the core ranked it already)."""
    return [
        s
        for s in signals
        if s.signal_type is SignalType.DIGEST_ITEM and s.tier in EMAIL_TIERS
    ]


def _fact_view(fact: Fact, today: date) -> _FactView:
    kind = str(fact.kind)
    label = FACT_LABELS.get(kind, "Факт")
    parsed = _parse_date_value(fact.value)
    if kind in MISSING_DATE and parsed is None:
        text = MISSING_DATE[kind]
    elif parsed is not None and kind in MISSING_DATE:
        text = f"{label}: {_date_phrase(parsed[0], today, parsed[1])}"
    else:
        text = f"{label}: {fact.value}".strip()
    return _FactView(
        label=text,
        quote=fact.evidence.strip(),
        url=_safe_url(fact.source_url),
    )


def _precedent_view(precedent: Precedent, today: date) -> _PrecedentView:
    date_text = _date_phrase(precedent.event_date, today, precedent.date_precision)
    if not date_text:
        date_text = "дата в источнике не указана"
    meta = precedent.vendor
    change = CHANGE_TYPES_RU.get(precedent.change_type)
    if change:
        meta = f"{meta} · {change}" if meta else change
    return _PrecedentView(
        date_text=date_text,
        text=" ".join(precedent.text.split()),
        meta=meta,
        url=_safe_url(precedent.source_url),
    )


def _delta_line(signal: Signal) -> str:
    if signal.delta_status is None:
        return ""
    title = DELTA_TITLES.get(signal.delta_status, "")
    if not title:
        return ""
    note = " ".join((signal.delta_note or "").split())
    line = f"{title}: {note}" if note else f"{title}."
    line = _sentence(line)
    if signal.days_tracked > 1:
        history = _count(signal.days_tracked, "день", "дня", "дней")
        line = f"{line} История ведётся {history}."
    return line


def _card_view(signal: Signal, today: date) -> _CardView:
    """One card. The context sentence is quoted from the core, never composed.

    `context_note` is written once, in publish.build_context_note, and every
    number in it is backed by the precedent list underneath. Three surfaces
    wording the same claim three ways is what DR-10 promises not to do, so the
    heading below is a fallback for signals published before the field
    existed, not a second voice.
    """
    context_title = CONTEXT_TITLES.get(signal.context_label, "")
    precedents = [_precedent_view(p, today) for p in signal.precedents]
    note = _sentence(" ".join((signal.context_note or "").split()))
    if not context_title and precedents:
        context_title = "Прецеденты в корпусе"
    why = " ".join(signal.why_it_matters.split())
    return _CardView(
        headline=" ".join(signal.headline.split()) or FALLBACK_SUBJECT,
        summary=signal.summary.strip(),
        why=_sentence(f"Почему это важно: {why}") if why else "",
        facts=[_fact_view(f, today) for f in signal.facts],
        context_note=note if precedents else "",
        context_title="" if (note and precedents) else context_title,
        precedents=precedents,
        delta=_delta_line(signal),
        primary_url=_safe_url(signal.primary_url),
        signal_id=signal.signal_id,
    )


def _upcoming_views(signal: Signal, today: date) -> list[_UpcomingView]:
    """Deadlines the core already extracted, shown in the order it gave.

    `upcoming` is the field the core fills for exactly this block, and its
    wording is meant for the reader. A fact carries the vendor's own sentence
    instead, in the vendor's language, so it is the fallback for a signal
    published without the field.
    """
    views = [
        _UpcomingView(
            date_text=_date_phrase(item.when, today, item.date_precision),
            text=" ".join((item.what or "").split()),
            url=_safe_url(item.source_url),
        )
        for item in signal.upcoming
    ]
    if views:
        return views
    for fact in signal.facts:
        kind = str(fact.kind)
        if kind not in MISSING_DATE:
            continue
        parsed = _parse_date_value(fact.value)
        if parsed is None:
            continue
        views.append(
            _UpcomingView(
                date_text=_date_phrase(parsed[0], today, parsed[1]),
                text=" ".join(fact.evidence.split()),
                url=_safe_url(fact.source_url),
                quoted=True,
            )
        )
    return views


def _quiet_stats_line(stats: Mapping[str, int], summary: RunSummary | None) -> str:
    """The numbers that separate a quiet day from an agent gone silent.

    `Signal.stats` is a free-form extension dict that nothing in the core ever
    fills, so on live data this line was always empty and the letter said only
    that nothing happened, with nothing to show it had looked. `RunSummary` is
    the field the pipeline does populate.
    """
    parts: list[str] = []
    sources = _stat(stats, SOURCE_KEYS)
    if sources is None and summary is not None:
        sources = summary.sources_checked
    if sources is not None:
        checked = _count(sources, "источник", "источника", "источников")
        parts.append(f"Проверено {checked}")
    rejected = _stat(stats, REJECTED_KEYS)
    if rejected is None and summary is not None:
        rejected = summary.materials_filtered
    if rejected:
        dropped = _count(rejected, "материал", "материала", "материалов")
        parts.append(f"{dropped} отклонено")
    return _sentence(", ".join(parts)) if parts else ""


def _quiet_view(signal: Signal | None, today: date) -> _BlockView:
    if signal is None:
        return _BlockView(QUIET_INTRO, "", [], "", "")
    intro = _sentence(" ".join(signal.summary.split())) or QUIET_INTRO
    return _BlockView(
        intro=intro,
        detail="",
        upcoming=_upcoming_views(signal, today),
        stats_line=_quiet_stats_line(signal.stats, signal.run_summary),
        signal_id=signal.signal_id,
    )


def _failure_view(signal: Signal, today: date) -> _BlockView:
    phrase = _date_phrase(signal.for_date, today)
    prefix = (
        "Прогон не завершился"
        if phrase == "сегодня"
        else f"Прогон {phrase} не завершился"
    )
    reason = " ".join((signal.failure_reason or "").split())
    intro = _sentence(f"{prefix}: {reason}" if reason else prefix)
    collected = _stat(signal.stats, COLLECTED_KEYS)
    if collected is None and signal.run_summary is not None:
        # Same dead field as the quiet-day line: `stats` is never filled by
        # the core, `run_summary` is.
        collected = signal.run_summary.materials_collected or None
    detail = " ".join(signal.summary.split())
    if collected is not None:
        gathered = _count(collected, "материал", "материала", "материалов")
        detail = _sentence(f"Собрано {gathered}, обработать не удалось. {detail}")
    else:
        detail = _sentence(detail)
    return _BlockView(
        intro=intro,
        detail=detail,
        upcoming=[],
        stats_line="",
        signal_id=signal.signal_id,
    )


def _closing_line(signals: Sequence[Signal]) -> str:
    """Close a story when one closed today (voice.md, section 7).

    Every signal of the run is scanned, not only the ones drawn as cards. A
    story that closed is by definition not urgent, the core files it in the
    background band, and a letter looking only at its own cards would never
    close anything at all.
    """
    for signal in signals:
        if signal.delta_status is not DeltaStatus.RESOLVED:
            continue
        # Headline first, note second. `delta_note` for a resolved storyline
        # is the words "история закрыта", so putting it first produced
        # "Закрыто: история закрыта, история велась 4 дня" — a sentence that
        # never says what closed. The web page had the same inversion and the
        # digest ended every morning on twenty-seven copies of it.
        subject = " ".join((signal.headline or signal.delta_note or "").split())
        line = f"Закрыто: {subject.rstrip('.')}" if subject else "Закрыто"
        if signal.days_tracked > 1:
            history = _count(signal.days_tracked, "день", "дня", "дней")
            line = f"{line}, история велась {history}"
        return _sentence(line)
    return ""


def _sources_line(signals: Sequence[Signal]) -> str:
    """Sources that did not answer, in a calm tone (voice.md, section 5).

    The names travel inside the contract (`RunSummary`), because a surface may
    not read `source_runs` (SUR-1). A footer of counts without names would
    tell the reader that something is missing and refuse to say what.
    """
    summary = next((s.run_summary for s in signals if s.run_summary), None)
    if summary is None:
        return ""
    parts: list[str] = []
    failed = list(summary.sources_failed)
    if failed:
        head = _spelled_count(len(failed), "источник", "источника", "источников")
        verb = "не ответил" if len(failed) == 1 else "не ответили"
        parts.append(f"{head[:1].upper()}{head[1:]} {verb}: {', '.join(failed)}.")
    for name in summary.sources_empty:
        # HTTP 200 with nothing in it is a different fault, named apart.
        parts.append(f"{name} ответил, но ничего не отдал.")
    return " ".join(parts)


def _subject(failure: _BlockView | None, cards: Sequence[Signal], quiet: bool) -> str:
    """Same rule as the lock-screen line: the main fact, nothing about us."""
    if failure is not None:
        return FAILURE_SUBJECT
    if quiet or not cards:
        return QUIET_SUBJECT
    lead = cards[0]
    headline = " ".join(lead.headline.split()) or " ".join(lead.summary.split())
    return _truncate_words(headline, SUBJECT_MAX) or FALLBACK_SUBJECT


def _reference_date(signals: Sequence[Signal], today: date | None) -> date:
    if today is not None:
        return today
    if signals:
        return signals[0].for_date
    return date.today()


def build_view(signals: Sequence[Signal], today: date | None = None) -> _LetterView:
    reference = _reference_date(signals, today)
    failures = [s for s in signals if s.signal_type is SignalType.RUN_FAILURE]
    quiet_signals = [s for s in signals if s.signal_type is SignalType.QUIET_DAY]
    selected = select_cards(signals)

    failure = _failure_view(failures[0], reference) if failures else None
    cards = [_card_view(s, reference) for s in selected]
    quiet = None
    if not cards and failure is None:
        quiet = _quiet_view(quiet_signals[0] if quiet_signals else None, reference)

    run_log_url = ""
    for signal in signals:
        run_log_url = _safe_url(signal.run_log_url)
        if run_log_url:
            break

    schema_note = ""
    if any(s.schema_version > SIGNAL_SCHEMA_VERSION for s in signals):
        # SIG-5: a newer record is shown as far as this letter can read it.
        schema_note = (
            "Часть записей сохранена в более новом формате. "
            "Показано то, что письмо умеет прочитать."
        )

    header = f"Разбор за {_date_phrase(reference, reference)}"
    return _LetterView(
        subject=_subject(failure, selected, quiet is not None),
        header=header,
        failure=failure,
        cards=cards,
        quiet=quiet,
        sources_line=_sources_line(signals),
        closing=_closing_line(signals),
        # One address, once, at the end of the letter: five cards each ending
        # in the same link is the same sentence printed five times.
        run_log_url=run_log_url,
        schema_note=schema_note,
    )


# --------------------------------------------------------------------------
# Plain text. Read in terminals, and a letter without it looks careless.
# --------------------------------------------------------------------------


def _card_text(card: _CardView) -> list[str]:
    lines = [card.headline, "-" * min(len(card.headline), TEXT_WIDTH)]
    if card.summary:
        lines += ["", *_wrap(card.summary)]
    if card.why:
        lines += ["", *_wrap(card.why)]
    if card.primary_url:
        lines += ["", f"Первоисточник: {card.primary_url}"]
    if card.facts:
        lines += ["", "Факты"]
        for fact in card.facts:
            lines += _wrap(fact.label, indent="  ")
            if fact.quote:
                lines += _wrap(f"«{fact.quote}»", indent="    ")
            if fact.url:
                lines.append(f"    {fact.url}")
    if card.context_note or card.context_title:
        lines += ["", *_wrap(card.context_note or card.context_title)]
        for precedent in card.precedents:
            head = f"{precedent.date_text} — {precedent.text}"
            lines += _wrap(head, indent="  ")
            if precedent.meta:
                lines.append(f"    {precedent.meta}")
            if precedent.url:
                lines.append(f"    {precedent.url}")
    if card.delta:
        lines += ["", *_wrap(card.delta)]
    if card.signal_id:
        lines += ["", card.signal_id]
    return lines


def _block_text(block: _BlockView) -> list[str]:
    lines = _wrap(block.intro)
    if block.detail:
        lines += ["", *_wrap(block.detail)]
    if block.upcoming:
        lines += ["", "Ближайшее:"]
        for item in block.upcoming:
            what = f"«{item.text}»" if item.quoted else item.text
            lines += _wrap(f"{item.date_text} — {what}", hang="  ")
            if item.url:
                lines.append(f"  {item.url}")
    if block.stats_line:
        lines += ["", *_wrap(block.stats_line)]
    return lines


def render_text(view: _LetterView) -> str:
    sections: list[list[str]] = [[view.header, "=" * min(len(view.header), TEXT_WIDTH)]]
    if view.failure is not None:
        sections.append(_block_text(view.failure))
    if view.quiet is not None:
        sections.append(_block_text(view.quiet))
    sections.extend(_card_text(card) for card in view.cards)
    tail: list[str] = []
    if view.sources_line:
        tail += _wrap(view.sources_line)
    if view.closing:
        tail += _wrap(view.closing)
    if view.run_log_url:
        tail.append(f"{RUN_LOG_LABEL}: {view.run_log_url}")
    if view.schema_note:
        tail += _wrap(view.schema_note)
    if tail:
        sections.append(tail)
    body = "\n\n".join("\n".join(block).strip("\n") for block in sections if block)
    return body.rstrip() + "\n"


# --------------------------------------------------------------------------
# HTML. Tables instead of flex and grid, inline styles instead of a
# stylesheet, 600 pixels, no remote image or font, explicit colours on every
# element because mail clients rewrite dark surfaces on their own.
# --------------------------------------------------------------------------


def _para(
    text: str,
    size: int = 15,
    color: str = C_TEXT,
    top: int = 12,
    bold: bool = False,
) -> str:
    weight = "bold" if bold else "normal"
    body = escape(text).replace("\n\n", "<br><br>").replace("\n", "<br>")
    return (
        f'<p style="margin:{top}px 0 0 0;padding:0;font-family:{FONT};'
        f"font-size:{size}px;line-height:{round(size * 1.5)}px;font-weight:{weight};"
        f'color:{color};background-color:{C_CARD};">{body}</p>'
    )


def _heading(text: str) -> str:
    return (
        f'<h2 style="margin:0;padding:0;font-family:{FONT};font-size:20px;'
        f"line-height:27px;font-weight:bold;color:{C_HEAD};"
        f'background-color:{C_CARD};">{escape(text)}</h2>'
    )


def _label(text: str) -> str:
    return (
        f'<p style="margin:20px 0 0 0;padding:0;font-family:{FONT};font-size:12px;'
        f"line-height:16px;font-weight:bold;letter-spacing:0.6px;"
        f'text-transform:uppercase;color:{C_MUTED};background-color:{C_CARD};">'
        f"{escape(text)}</p>"
    )


def _anchor(url: str, label: str = "", size: int = 13) -> str:
    text = escape(label or _display_url(url))
    if not url:
        return (
            f'<span style="font-family:{FONT};font-size:{size}px;color:{C_MUTED};'
            f'background-color:{C_CARD};">{text}</span>'
        )
    return (
        f'<a href="{escape(url)}" style="font-family:{FONT};font-size:{size}px;'
        f'line-height:20px;color:{C_LINK};text-decoration:underline;">{text}</a>'
    )


def _quote_block(quote: str, url: str) -> str:
    inner = ""
    if quote:
        inner += (
            f'<span style="font-family:{FONT};font-size:14px;line-height:21px;'
            f'color:{C_TEXT};background-color:{C_QUOTE_BG};">«{escape(quote)}»</span>'
        )
    if url:
        if inner:
            inner += "<br>"
        inner += (
            f'<a href="{escape(url)}" style="font-family:{FONT};font-size:13px;'
            f'line-height:20px;color:{C_LINK};text-decoration:underline;">'
            f"{escape(_display_url(url))}</a>"
        )
    if not inner:
        return ""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="{C_QUOTE_BG}" style="width:100%;border-collapse:collapse;'
        f'background-color:{C_QUOTE_BG};"><tr><td bgcolor="{C_QUOTE_BG}" '
        f'style="padding:10px 12px;border-left:3px solid {C_QUOTE_BAR};'
        f'background-color:{C_QUOTE_BG};color:{C_TEXT};">{inner}</td></tr></table>'
    )


def _spacer(height: int) -> str:
    return (
        f'<div style="height:{height}px;line-height:{height}px;font-size:1px;'
        f'background-color:{C_CARD};">&nbsp;</div>'
    )


def _row(content: str, top_rule: bool = False) -> str:
    rule = f"border-top:1px solid {C_RULE};" if top_rule else ""
    return (
        f'<tr><td bgcolor="{C_CARD}" style="padding:24px;{rule}'
        f'background-color:{C_CARD};color:{C_TEXT};">{content}</td></tr>'
    )


def _card_html(card: _CardView) -> str:
    parts = [_heading(card.headline)]
    if card.summary:
        parts.append(_para(card.summary, size=16, top=14))
    if card.why:
        parts.append(_para(card.why, size=15, top=12))
    if card.primary_url:
        parts.append(
            f'<p style="margin:12px 0 0 0;padding:0;background-color:{C_CARD};">'
            + _anchor(card.primary_url, "Первоисточник", size=14)
            + "</p>"
        )
    if card.facts:
        parts.append(_label("Факты"))
        for fact in card.facts:
            parts.append(_para(fact.label, size=15, top=10, bold=True))
            block = _quote_block(fact.quote, fact.url)
            if block:
                parts.append(_spacer(6))
                parts.append(block)
    if card.context_note:
        parts.append(_para(card.context_note, size=15, top=20))
    elif card.context_title:
        parts.append(_label(card.context_title))
    if card.context_note or card.context_title:
        for precedent in card.precedents:
            parts.append(_para(precedent.date_text, size=14, top=10, bold=True))
            parts.append(_para(precedent.text, size=14, top=4))
            meta = precedent.meta
            if meta:
                parts.append(_para(meta, size=13, top=4, color=C_MUTED))
            if precedent.url:
                parts.append(
                    f'<p style="margin:4px 0 0 0;padding:0;'
                    f'background-color:{C_CARD};">' + _anchor(precedent.url) + "</p>"
                )
    if card.delta:
        parts.append(_para(card.delta, size=14, top=16, color=C_MUTED))
    if card.signal_id:
        parts.append(
            f'<p style="margin:18px 0 0 0;padding:0;background-color:{C_CARD};">'
            f'<span style="font-family:{FONT};font-size:12px;line-height:18px;'
            f'color:{C_MUTED};background-color:{C_CARD};">'
            f"{escape(card.signal_id)}</span></p>"
        )
    return "".join(parts)


def _block_html(block: _BlockView) -> str:
    parts = [_para(block.intro, size=17, top=0)]
    if block.detail:
        parts.append(_para(block.detail, size=15))
    if block.upcoming:
        parts.append(_label("Ближайшее"))
        for item in block.upcoming:
            parts.append(_para(item.date_text, size=15, top=10, bold=True))
            if item.quoted:
                block_html = _quote_block(item.text, item.url)
                if block_html:
                    parts.append(_spacer(6))
                    parts.append(block_html)
                continue
            if item.text:
                parts.append(_para(item.text, size=14, top=4))
            if item.url:
                parts.append(
                    f'<p style="margin:4px 0 0 0;padding:0;'
                    f'background-color:{C_CARD};">' + _anchor(item.url) + "</p>"
                )
    if block.stats_line:
        parts.append(_para(block.stats_line, size=13, top=18, color=C_MUTED))
    if block.signal_id:
        parts.append(_para(block.signal_id, size=12, top=8, color=C_MUTED))
    return "".join(parts)


def render_html(view: _LetterView) -> str:
    rows = [
        f'<tr><td bgcolor="{C_CARD}" style="padding:24px 24px 0 24px;'
        f'background-color:{C_CARD};">'
        f'<p style="margin:0;padding:0;font-family:{FONT};font-size:13px;'
        f"line-height:19px;letter-spacing:0.4px;color:{C_MUTED};"
        f'background-color:{C_CARD};">{escape(view.header)}</p></td></tr>'
    ]
    if view.failure is not None:
        rows.append(_row(_block_html(view.failure)))
    if view.quiet is not None:
        rows.append(_row(_block_html(view.quiet)))
    for index, card in enumerate(view.cards):
        rows.append(_row(_card_html(card), top_rule=index > 0))

    footer = []
    if view.sources_line:
        footer.append(_para(view.sources_line, size=14, top=0, color=C_MUTED))
    if view.closing:
        footer.append(
            _para(view.closing, size=14, top=10 if footer else 0, color=C_TEXT)
        )
    if view.run_log_url:
        footer.append(
            f'<p style="margin:10px 0 0 0;padding:0;background-color:{C_CARD};">'
            + _anchor(view.run_log_url, RUN_LOG_LABEL, size=13)
            + "</p>"
        )
    if view.schema_note:
        footer.append(_para(view.schema_note, size=12, top=10, color=C_MUTED))
    if footer:
        rows.append(_row("".join(footer), top_rule=True))

    body = "".join(rows)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light only">'
        '<meta name="supported-color-schemes" content="light only">'
        f"<title>{escape(view.subject)}</title>"
        "</head>"
        f'<body style="margin:0;padding:0;background-color:{C_PAGE};color:{C_TEXT};">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="{C_PAGE}" style="width:100%;border-collapse:collapse;'
        f'background-color:{C_PAGE};">'
        f'<tr><td align="center" bgcolor="{C_PAGE}" style="padding:24px 12px;'
        f'background-color:{C_PAGE};">'
        f'<table role="presentation" width="{WIDTH_PX}" cellpadding="0" '
        f'cellspacing="0" border="0" bgcolor="{C_CARD}" '
        f'style="width:{WIDTH_PX}px;max-width:100%;'
        f'border-collapse:collapse;background-color:{C_CARD};color:{C_TEXT};">'
        f"{body}"
        "</table></td></tr></table></body></html>"
    )


def build_email(signals: Sequence[Signal], today: date | None = None) -> EmailDigest:
    view = build_view(signals, today)
    ids = tuple(card.signal_id for card in view.cards)
    for block in (view.failure, view.quiet):
        if block is not None and block.signal_id:
            ids = (block.signal_id, *ids)
    return EmailDigest(
        subject=view.subject,
        text=render_text(view),
        html=render_html(view),
        signal_ids=ids,
    )


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def load_smtp_config(env: Mapping[str, str] | None = None) -> SmtpConfig:
    """Every parameter comes from the environment (NFR-11)."""
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV if not (env.get(name) or "").strip()]
    if missing:
        raise EmailConfigError("Не заданы переменные окружения: " + ", ".join(missing))
    raw_port = (env.get("SMTP_PORT") or "587").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise EmailConfigError(f"SMTP_PORT не число: {raw_port}") from exc
    recipients = tuple(
        part.strip()
        for part in (env.get("SMTP_TO") or "").replace(";", ",").split(",")
        if part.strip()
    )
    if not recipients:
        raise EmailConfigError("Не заданы переменные окружения: SMTP_TO")
    return SmtpConfig(
        host=(env.get("SMTP_HOST") or "").strip(),
        port=port,
        sender=(env.get("SMTP_FROM") or "").strip(),
        recipients=recipients,
        user=(env.get("SMTP_USER") or "").strip(),
        password=env.get("SMTP_PASSWORD") or "",
    )


def build_message(
    digest: EmailDigest, sender: str, recipients: Sequence[str]
) -> EmailMessage:
    """multipart/alternative: the terminal reader gets a real letter too."""
    message = EmailMessage()
    message["Subject"] = digest.subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    domain = sender.rpartition("@")[2] or "localhost"
    message["Message-ID"] = make_msgid(domain=domain)
    message["Auto-Submitted"] = "auto-generated"
    # base64 keeps Cyrillic intact on relays that never announced 8BITMIME.
    message.set_content(digest.text, subtype="plain", charset="utf-8", cte="base64")
    message.add_alternative(digest.html, subtype="html", charset="utf-8", cte="base64")
    return message


def send_digest(
    digest: EmailDigest,
    env: Mapping[str, str] | None = None,
    smtp_factory: object | None = None,
) -> DeliveryResult:
    """Send over STARTTLS. A refusal comes back as a result (SUR-5)."""
    try:
        config = load_smtp_config(env)
    except EmailConfigError as exc:
        return DeliveryResult(False, digest.subject, (), str(exc))

    factory = smtp_factory if smtp_factory is not None else smtplib.SMTP
    message = build_message(digest, config.sender, config.recipients)
    try:
        with factory(config.host, config.port, timeout=SMTP_TIMEOUT) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if config.user:
                smtp.login(config.user, config.password)
            smtp.send_message(message)
    except Exception as exc:  # a broken surface never touches the core
        return DeliveryResult(
            False,
            digest.subject,
            config.recipients,
            f"{type(exc).__name__}: {exc}",
        )
    return DeliveryResult(True, digest.subject, config.recipients)


def deliver(
    db_path: str | Path,
    run_id: str | None = None,
    today: date | None = None,
    env: Mapping[str, str] | None = None,
    smtp_factory: object | None = None,
) -> DeliveryResult:
    """Read the signal store read-only (FR-5.22), render, send."""
    conn = connect(db_path, read_only=True)
    try:
        signals = read_signals(conn, run_id)
    finally:
        conn.close()
    return send_digest(build_email(signals, today), env=env, smtp_factory=smtp_factory)
