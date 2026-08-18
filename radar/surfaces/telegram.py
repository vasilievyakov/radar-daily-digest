"""Telegram surface: the sixty-second morning read (PRD 10.3, DR-2).

The module is a renderer plus one HTTP call. Everything shown was decided by
the core — rank, tier, facts, precedents, delta. What belongs here is
truncation to the channel limit, HTML markup, navigation and delivery
(SUR-3). The shape of every message comes from docs/voice.md.

Channel capacity is expressed in tiers, never in score thresholds: the core
owns significance, the surface owns how much of it fits (SUR-2). For the same
reason the reference date is always a parameter — replaying an old run has to
produce the same text it produced that morning.

Optional keys this surface reads out of `Signal.stats`, all integers:

    sources_checked, sources_failed, sources_empty, items_rejected,
    items_collected, last_success_days_ago

Source names do not fit an int map, so a name travels in the key itself:
``"source_failed:Cursor changelog": 1`` and ``"source_empty:MCP servers": 1``.
Counts alone still render, without the names.
"""

from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from radar.db import connect, read_signals
from radar.models import (
    ChangeType,
    ContextLabel,
    DatePrecision,
    DeltaStatus,
    Fact,
    FactKind,
    Precedent,
    Signal,
    SignalType,
    Tier,
    UpcomingDeadline,
)

MAX_MESSAGE_CHARS = 4096
NOTIFICATION_CHARS = 40
MAX_ITEMS = 5
MAX_UPCOMING = 3
# Telegram carries the lead and the standard band; the background band lives
# in surfaces with more room (PRD 10.3). This is capacity, not a threshold.
VISIBLE_TIERS = (Tier.LEAD, Tier.STANDARD)

API_ROOT = "https://api.telegram.org"
PARSE_MODE = "HTML"
TIMEOUT_SECONDS = 15.0

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENV = "TELEGRAM_CHAT_ID"

QUIET_NOTIFICATION = "Сегодня без изменений"
FAILURE_NOTIFICATION = "Прогон не завершился"
QUIET_OPENING = "Сегодня в вашем стеке ничего не изменилось."
RUN_LOG_TEXT = "Лог прогона."

_MONTHS_GENITIVE = (
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
_MONTHS_NOMINATIVE = (
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
_MONTHS_SHORT = (
    "янв",
    "фев",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)

_DAYS = ("день", "дня", "дней")
_SOURCES = ("источник", "источника", "источников")
_MATERIALS = ("материал", "материала", "материалов")
_RECORDS = ("запись", "записи", "записей")

_COUNT_WORDS = (
    "",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
    "десять",
    "одиннадцать",
    "двенадцать",
)
# Accusative differs for the first two, and the precedent link needs it.
_COUNT_WORDS_ACC = {1: "одну", 2: "две"}

_DATE_KINDS = (FactKind.SUNSET_DATE, FactKind.EFFECTIVE_DATE)
# Change types that promise a date. Only for them does a missing date deserve
# a line of its own (voice.md, section 4).
_DATE_BEARING = frozenset(
    {
        ChangeType.DEPRECATION,
        ChangeType.BREAKING_CHANGE,
        ChangeType.PRICING,
        ChangeType.LIMITS,
    }
)
# Labels that earn the block. `not_found_in_corpus` is absent on purpose: a
# label that changes nothing in the reading teaches the reader to skip labels
# (voice.md, section 8). The wording of the block is not built from the label
# either — the core writes it into `context_note`.
_CONTEXT_LABELS = frozenset(
    {
        ContextLabel.RECURRING,
        ContextLabel.TREND_MEMBER,
        ContextLabel.ESCALATION,
    }
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_ISO_DAY = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_ISO_MONTH = re.compile(r"(\d{4})-(\d{2})(?!\d)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


# --------------------------------------------------------------------------
# Language helpers
# --------------------------------------------------------------------------


def _plural(count: int, forms: tuple[str, str, str]) -> str:
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return forms[2]
    tail %= 10
    if tail == 1:
        return forms[0]
    if 2 <= tail <= 4:
        return forms[1]
    return forms[2]


def _amount(count: int, forms: tuple[str, str, str]) -> str:
    return f"{count} {_plural(count, forms)}"


def _spelled(count: int, forms: tuple[str, str, str], accusative: bool = False) -> str:
    """Small counts read as words; beyond the table digits stay digits."""
    if 1 <= count < len(_COUNT_WORDS):
        word = (
            _COUNT_WORDS_ACC[count]
            if accusative and count in _COUNT_WORDS_ACC
            else _COUNT_WORDS[count]
        )
        return f"{word} {_plural(count, forms)}"
    return _amount(count, forms)


def _cut_words(text: str, limit: int) -> str:
    """Truncate on a word boundary, never mid-word."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.—–-")


def _sentences(text: str) -> list[str]:
    return [
        part.strip() for part in _SENTENCE_SPLIT.split(text.strip()) if part.strip()
    ]


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _terminated(text: str) -> str:
    text = text.strip()
    if text and text[-1] not in ".!?…":
        return text + "."
    return text


# --------------------------------------------------------------------------
# Dates. The reference day is always passed in (voice.md, section 4).
# --------------------------------------------------------------------------


def format_date(
    target: date,
    today: date,
    precision: DatePrecision = DatePrecision.DAY,
) -> str:
    """A date and the distance to it, so the reader never counts in their head."""
    if precision is DatePrecision.YEAR:
        return str(target.year)
    if precision is DatePrecision.MONTH:
        return f"{_MONTHS_NOMINATIVE[target.month - 1]} {target.year}"

    plain = f"{target.day} {_MONTHS_GENITIVE[target.month - 1]}"
    if precision is DatePrecision.INFERRED:
        # Distance computed from a guessed year would be false precision.
        return f"{plain} (год не указан в источнике)"

    delta = (target - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == -1:
        return "вчера"
    if delta > 0:
        return f"{plain}, через {_amount(delta, _DAYS)}"
    return f"{plain}, {_amount(-delta, _DAYS)} назад"


def _short_date(target: date) -> str:
    return f"{target.day} {_MONTHS_SHORT[target.month - 1]}"


def missing_date_phrase(change_type: ChangeType | None) -> str | None:
    """A date the source never gave is said out loud and never substituted."""
    if change_type is ChangeType.DEPRECATION:
        return "дата отключения в источнике не указана"
    if change_type in _DATE_BEARING:
        return "дата вступления в силу в источнике не указана"
    return None


def _parse_date(value: str) -> tuple[date, DatePrecision] | None:
    value = (value or "").strip()
    if not value:
        return None
    match = _ISO_DAY.search(value)
    if match:
        try:
            return date(int(match[1]), int(match[2]), int(match[3])), DatePrecision.DAY
        except ValueError:
            return None
    match = _ISO_MONTH.search(value)
    if match:
        try:
            return date(int(match[1]), int(match[2]), 1), DatePrecision.MONTH
        except ValueError:
            return None
    return None


def _date_fact(signal: Signal) -> tuple[Fact, date, DatePrecision] | None:
    """The fact that carries the date of the event, sunset before effective."""
    for kind in _DATE_KINDS:
        for fact in signal.facts:
            if fact.kind is not kind:
                continue
            parsed = _parse_date(fact.value)
            if parsed:
                return fact, parsed[0], parsed[1]
    return None


def _date_phrase(signal: Signal, today: date) -> str | None:
    found = _date_fact(signal)
    if found is None:
        return missing_date_phrase(signal.change_type)
    fact, target, precision = found
    phrase = format_date(target, today, precision)
    # "с 1 ноября" reads right, "с сегодня" does not.
    if fact.kind is FactKind.EFFECTIVE_DATE and phrase[:1].isdigit():
        return f"с {phrase}"
    return phrase


# --------------------------------------------------------------------------
# HTML markup. Escaping is the whole job here: headlines carry dots, hyphens,
# brackets, ampersands and Cyrillic.
# --------------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _absolute(url: str) -> bool:
    lowered = url.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _link(url: str | None, text: str) -> str:
    """A relative path is not a link outside the page that hosts it.

    `run_log_url` is "run-log.html" unless a public base is configured, which
    is right for the site and meaningless in a message: Bot API would carry a
    dead href out to the reader. Text without a link is honest; a link that
    goes nowhere is the kind of care that only looks like care.
    """
    if not url or not _absolute(url):
        return _esc(text)
    return f'<a href="{html.escape(url, quote=True)}">{_esc(text)}</a>'


def _pretty_url(url: str) -> str:
    return _SCHEME.sub("", url).removeprefix("www.").rstrip("/")


def _run_log_link(signal: Signal | None) -> str | None:
    if signal is None or not signal.run_log_url:
        return None
    return _link(signal.run_log_url, RUN_LOG_TEXT)


def _footer_id(signal: Signal | None) -> str | None:
    """`signal_id` in the basement, fine print: on stage it proves that three
    surfaces are showing one record (voice.md, section 8)."""
    if signal is None:
        return None
    return f"<code>{_esc(signal.signal_id)}</code>"


# --------------------------------------------------------------------------
# Blocks shared by every message type
# --------------------------------------------------------------------------


def _named(stats: dict[str, int], prefix: str) -> list[str]:
    return [key[len(prefix) :] for key in stats if key.startswith(prefix)]


def _counter(signal: Signal, keys: tuple[str, ...], fallback: int | None) -> int:
    """`stats` if it says anything, otherwise the contract.

    `Signal.stats` is documented as free-form extension and nothing in the core
    fills it: every live signal arrives with an empty dict. Read alone it made
    the whole footer of this surface vanish on real data while the tests, which
    hand-write `stats`, stayed green. `run_summary` is the field the pipeline
    actually populates, so it is what the surface falls back to.
    """
    for key in keys:
        if key in signal.stats:
            return int(signal.stats[key])
    return int(fallback or 0)


def _sources_footer(signal: Signal | None, run_log: str | None) -> str | None:
    """Unavailable sources, calm tone: a refusal is a working situation."""
    if signal is None:
        return None
    stats = signal.stats
    summary = signal.run_summary
    failed_names = _named(stats, "source_failed:") or list(
        summary.sources_failed if summary else []
    )
    empty_names = _named(stats, "source_empty:") or list(
        summary.sources_empty if summary else []
    )
    failed = int(stats.get("sources_failed", len(failed_names)))
    empty = int(stats.get("sources_empty", len(empty_names)))

    parts: list[str] = []
    if failed > 0:
        verb = "не ответил" if failed == 1 else "не ответили"
        head = _capitalize(_spelled(failed, _SOURCES))
        tail = f": {_esc(', '.join(failed_names))}" if failed_names else ""
        parts.append(f"{head} {verb}{tail}.")
    if empty > 0:
        # A source answering 200 with nothing is a different breakage and is
        # named apart.
        if empty_names:
            verb = (
                "ответил, но ничего не отдал"
                if empty == 1
                else "ответили, но ничего не отдали"
            )
            parts.append(f"{_esc(', '.join(empty_names))} {verb}.")
        else:
            verb = (
                "ответил, но ничего не отдал"
                if empty == 1
                else "ответили, но ничего не отдали"
            )
            parts.append(f"{_capitalize(_spelled(empty, _SOURCES))} {verb}.")
    if not parts:
        return None
    if run_log:
        parts.append(run_log)
    return " ".join(parts)


def _closing_line(signals: Sequence[Signal], shown: Sequence[Signal]) -> str | None:
    """Closing a story, if today closed one (voice.md, section 7)."""
    resolved = [
        s for s in signals if s.delta_status is DeltaStatus.RESOLVED and s.headline
    ]
    if not resolved:
        return None
    shown_ids = {s.signal_id for s in shown}
    signal = next((s for s in resolved if s.signal_id not in shown_ids), resolved[0])
    if signal.days_tracked > 0:
        return (
            f"Закрыто: {_esc(signal.headline.rstrip('.'))}, "
            f"история велась {_amount(signal.days_tracked, _DAYS)}."
        )
    return f"Закрыто: {_esc(signal.headline.rstrip('.'))}."


def _context_note(signal: Signal, today: date) -> str:
    """The sentence above the precedent list, as the core wrote it.

    `context_note` is composed once, in publish.build_context_note, where
    every number in it is backed by the precedent list. A surface writing its
    own would be retelling the corpus (SUR-2), and three surfaces would then
    word one claim three ways while DR-10 promises the hall one record with
    three faces. The fallback is for a signal published before the field
    existed; it claims only what the precedents themselves show, and it
    carries no label of its own (voice.md, section 8).
    """
    note = " ".join((signal.context_note or "").split())
    if note:
        # Left as written: the sentence opens with a vendor name, and n8n or
        # vLLM would be misspelled by capitalising it here.
        return _terminated(note)
    dated = [p for p in signal.precedents if p.event_date is not None]
    if not dated:
        return ""
    earliest = min(dated, key=lambda p: p.event_date or today)
    when = format_date(earliest.event_date or today, today, earliest.date_precision)
    return f"Самая ранняя запись в корпусе — {when}."


@dataclass(slots=True)
class _Upcoming:
    when: date
    precision: DatePrecision
    text: str


def _upcoming_from_precedent(precedent: Precedent) -> _Upcoming | None:
    if precedent.event_date is None or not precedent.text.strip():
        return None
    return _Upcoming(
        precedent.event_date, precedent.date_precision, precedent.text.strip()
    )


def _upcoming_from_fact(fact: Fact, signal: Signal) -> _Upcoming | None:
    if fact.kind not in _DATE_KINDS:
        return None
    parsed = _parse_date(fact.value)
    if parsed is None:
        return None
    target, precision = parsed
    remainder = _ISO_DAY.sub("", fact.value).strip(" —–-,;:")
    label = remainder or signal.headline.strip() or f"«{fact.evidence.strip()}»"
    if not label:
        return None
    return _Upcoming(target, precision, label)


def _upcoming_from_deadline(item: UpcomingDeadline) -> _Upcoming | None:
    text = " ".join((item.what or "").split())
    if not text:
        return None
    return _Upcoming(item.when, item.date_precision, text)


def _upcoming_block(signal: Signal | None, today: date) -> str | None:
    """Silence is filled with the deadlines the reader planned to forget.

    `upcoming` is the field the core fills for this block, and its wording is
    written for a reader. Precedents and facts are the fallback for a signal
    published before the field existed: they carry the vendor's own sentence.
    """
    if signal is None:
        return None
    entries: list[_Upcoming] = [
        entry
        for entry in (_upcoming_from_deadline(d) for d in signal.upcoming)
        if entry
    ]
    if not entries:
        for precedent in signal.precedents:
            entry = _upcoming_from_precedent(precedent)
            if entry:
                entries.append(entry)
        for fact in signal.facts:
            entry = _upcoming_from_fact(fact, signal)
            if entry:
                entries.append(entry)

    seen: set[tuple[date, str]] = set()
    ahead: list[_Upcoming] = []
    for entry in entries:
        if entry.when < today:
            continue
        key = (entry.when, entry.text)
        if key in seen:
            continue
        seen.add(key)
        ahead.append(entry)
    if not ahead:
        # No deadlines, no empty heading (voice.md, section 2).
        return None

    # Chronological order of one signal's own dates. Signal ranking is
    # untouched by this: it decides nothing about what is shown.
    ahead.sort(key=lambda item: item.when)
    lines = ["Ближайшее:"]
    for entry in ahead[:MAX_UPCOMING]:
        lines.append(
            f"{_esc(format_date(entry.when, today, entry.precision))} — {_esc(entry.text)}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The lead item: rank one gets the spread, ranks two to five get one line
# --------------------------------------------------------------------------


@dataclass
class _LeadParts:
    headline: str
    date_phrase: str | None
    summary: list[str]
    why: str
    evidence: str
    evidence_url: str | None
    context: list[str]
    show_why: bool = True
    show_context: bool = True
    headline_limit: int | None = None


def _lead_parts(signal: Signal, today: date) -> _LeadParts:
    evidence = ""
    evidence_url = signal.primary_url
    found = _date_fact(signal)
    quoted = found[0] if found else (signal.facts[0] if signal.facts else None)
    if quoted is not None and quoted.evidence.strip():
        evidence = quoted.evidence.strip()
        evidence_url = quoted.source_url or signal.primary_url

    context: list[str] = []
    if signal.context_label in _CONTEXT_LABELS and signal.precedents:
        note = _context_note(signal, today)
        if note:
            context.append(note)
        context.append(
            f"Показать {_spelled(len(signal.precedents), _RECORDS, accusative=True)}."
        )
    if signal.delta_status is DeltaStatus.UPDATED and signal.delta_note:
        context.insert(0, _terminated(_capitalize(signal.delta_note)))

    return _LeadParts(
        headline=signal.headline.strip(),
        date_phrase=_date_phrase(signal, today),
        summary=_sentences(signal.summary),
        why=signal.why_it_matters.strip(),
        evidence=evidence,
        evidence_url=evidence_url,
        context=context,
    )


def _lead_blocks(parts: _LeadParts, run_log_url: str | None) -> list[str]:
    headline = parts.headline
    if parts.headline_limit is not None:
        headline = _cut_words(headline, parts.headline_limit)
    blocks = [f"<b>{_esc(headline)}</b>"]

    body = " ".join(parts.summary).strip()
    if parts.date_phrase:
        opening = _capitalize(parts.date_phrase)
        paragraph = f"{_esc(_terminated(opening))} {_esc(body)}".strip()
    else:
        paragraph = _esc(body)
    if paragraph:
        blocks.append(paragraph)

    if parts.show_why and parts.why:
        blocks.append(f"Почему это важно: {_esc(_terminated(parts.why))}")

    if parts.evidence:
        quote = f"«{_esc(parts.evidence)}»"
        if parts.evidence_url:
            quote += "\n" + _link(parts.evidence_url, _pretty_url(parts.evidence_url))
        blocks.append(quote)
    elif parts.evidence_url:
        blocks.append(_link(parts.evidence_url, _pretty_url(parts.evidence_url)))

    if parts.show_context and parts.context:
        lines = [_esc(line) for line in parts.context]
        # The precedent list is navigation, and the run log is where it leads.
        if run_log_url and parts.context[-1].startswith("Показать "):
            lines[-1] = _link(run_log_url, parts.context[-1])
        blocks.append("\n".join(lines))

    return blocks


def _continues(text: str) -> str:
    """The tail after the dash continues the line, so it starts in lower case.

    Names keep their shape: a word with an upper-case letter beyond the first
    is a brand or a version, and lowering it would misspell it.
    """
    first = text.split(" ", 1)[0]
    if any(char.isupper() for char in first[1:]):
        return text
    return text[:1].lower() + text[1:]


def _compact_row(signal: Signal, today: date) -> str:
    head = _link(signal.primary_url, signal.headline.strip())
    tail = _date_phrase(signal, today)
    if not tail:
        sentences = _sentences(signal.summary)
        tail = _continues(sentences[0].rstrip(".")) if sentences else ""
    if not tail:
        return head
    return f"{head} — {_esc(tail)}"


# --------------------------------------------------------------------------
# Message types
# --------------------------------------------------------------------------


def _visible_items(signals: Iterable[Signal]) -> list[Signal]:
    """Channel capacity mapped onto the tier the core assigned (SUR-2)."""
    items = [
        s
        for s in signals
        if s.signal_type is SignalType.DIGEST_ITEM and s.tier in VISIBLE_TIERS
    ]
    # Honouring the core's rank, not producing one: unranked items keep their
    # incoming order at the end.
    items.sort(key=lambda s: s.rank if s.rank > 0 else len(items) + 1)
    return items[:MAX_ITEMS]


def _first_of(signals: Iterable[Signal], kind: SignalType) -> Signal | None:
    return next((s for s in signals if s.signal_type is kind), None)


def _join(blocks: Iterable[str | None]) -> str:
    return "\n\n".join(block for block in blocks if block)


def _fit(build: Callable[[], str], shrinkers: Sequence[Callable[[], bool]]) -> str:
    """Truncation is the surface's own job (SUR-3).

    Each shrinker removes one whole unit of meaning and reports whether it had
    anything left to remove. The date line and the source link are not among
    them and survive every pass.
    """
    text = build()
    for shrink in shrinkers:
        while len(text) > MAX_MESSAGE_CHARS and shrink():
            text = build()
        if len(text) <= MAX_MESSAGE_CHARS:
            break
    return text


def render_digest(signals: Sequence[Signal], today: date | None = None) -> str:
    items = _visible_items(signals)
    if not items:
        return render_quiet_day(_first_of(signals, SignalType.QUIET_DAY), today)

    lead = items[0]
    today = today or lead.for_date
    parts = _lead_parts(lead, today)
    rows = [_compact_row(s, today) for s in items[1:]]
    state = {"rows": len(rows)}

    run_log = _run_log_link(lead)
    footer = _sources_footer(lead, run_log)
    closing = _closing_line(signals, items)
    # SUR-7 asks for a way into the run log; the sources footer already
    # carries it when there is one.
    log_line = None if footer else run_log

    def build() -> str:
        blocks = _lead_blocks(parts, lead.run_log_url)
        if state["rows"]:
            blocks.append("\n".join(rows[: state["rows"]]))
        return _join([*blocks, footer, log_line, closing, _footer_id(lead)])

    def drop_sentence() -> bool:
        if len(parts.summary) > 1:
            parts.summary.pop()
            return True
        return False

    def drop_context() -> bool:
        if parts.show_context and parts.context:
            parts.show_context = False
            return True
        return False

    def drop_why() -> bool:
        if parts.show_why and parts.why:
            parts.show_why = False
            return True
        return False

    def drop_row() -> bool:
        if state["rows"] > 0:
            state["rows"] -= 1
            return True
        return False

    def drop_summary() -> bool:
        if parts.summary:
            parts.summary = []
            return True
        return False

    def cut_headline() -> bool:
        limit = (
            parts.headline_limit
            if parts.headline_limit is not None
            else len(parts.headline)
        )
        if limit <= NOTIFICATION_CHARS:
            return False
        parts.headline_limit = max(NOTIFICATION_CHARS, limit // 2)
        return True

    return _fit(
        build,
        [drop_sentence, drop_context, drop_why, drop_row, drop_summary, cut_headline],
    )


def render_quiet_day(signal: Signal | None, today: date | None = None) -> str:
    """Absence of signals is a message of its own (SUR-4, PUB-4).

    The proof line carries the whole difference between "сегодня тихо" and an
    agent that stopped running: a message with no numbers under it is
    indistinguishable from silence with a headline attached.
    """
    if today is None:
        today = signal.for_date if signal else date.today()

    run_log = _run_log_link(signal)
    summary = signal.run_summary if signal else None
    checked = (
        _counter(
            signal,
            ("sources_checked", "sources_total"),
            summary.sources_checked if summary else 0,
        )
        if signal
        else 0
    )
    rejected = (
        _counter(
            signal,
            ("items_rejected", "rejected"),
            summary.materials_filtered if summary else 0,
        )
        if signal
        else 0
    )

    proof_parts: list[str] = []
    if checked:
        proof_parts.append(f"Проверено {_amount(checked, _SOURCES)}")
    if rejected:
        piece = f"{_amount(rejected, _MATERIALS)} отклонено"
        proof_parts.append(piece if proof_parts else _capitalize(piece))
    proof = ", ".join(proof_parts)
    if proof:
        proof += "."
    if run_log:
        proof = f"{proof} {run_log}".strip()

    footer = _sources_footer(signal, None)
    closing = _closing_line([signal] if signal else [], [])
    return _join(
        [
            QUIET_OPENING,
            _upcoming_block(signal, today),
            footer,
            proof or run_log,
            closing,
            _footer_id(signal),
        ]
    )


def render_run_failure(signal: Signal, today: date | None = None) -> str:
    """A system that reports its own failure earns more than one that hides it."""
    today = today or signal.for_date
    when = f"{signal.for_date.day} {_MONTHS_GENITIVE[signal.for_date.month - 1]}"
    reason = (signal.failure_reason or "").strip().rstrip(".")
    opening = f"Прогон {when} не завершился"
    opening = f"{opening}: {_esc(reason)}." if reason else f"{opening}."

    sentences: list[str] = []
    summary = signal.run_summary
    collected = _counter(
        signal,
        ("items_collected", "collected"),
        summary.materials_collected if summary else 0,
    )
    if collected:
        sentences.append(
            f"Собрано {_amount(collected, _MATERIALS)}, обработать не удалось."
        )
    last_success = summary.last_success_date if summary else None
    days_ago = _counter(
        signal,
        ("last_success_days_ago",),
        (signal.for_date - last_success).days if last_success else 0,
    )
    if days_ago > 0:
        last = signal.for_date - timedelta(days=days_ago)
        sentences.append(
            f"Последняя удачная сводка — {last.day} {_MONTHS_GENITIVE[last.month - 1]}."
        )
    run_log = _run_log_link(signal)
    if run_log:
        sentences.append(run_log)

    return _join(
        [
            opening,
            " ".join(sentences) if sentences else None,
            _sources_footer(signal, None),
            _footer_id(signal),
        ]
    )


def render(signals: Sequence[Signal], today: date | None = None) -> str:
    """One message out of a run, whatever the run turned out to be."""
    failure = _first_of(signals, SignalType.RUN_FAILURE)
    if failure is not None:
        return render_run_failure(failure, today)
    return render_digest(signals, today)


def notification_line(signals: Sequence[Signal], today: date | None = None) -> str:
    """Forty characters for the lock screen (voice.md, section 1).

    Telegram builds the push preview from the head of the message, so the line
    is returned to the caller instead of being printed twice inside it.
    """
    if _first_of(signals, SignalType.RUN_FAILURE) is not None:
        return FAILURE_NOTIFICATION
    items = _visible_items(signals)
    if not items:
        return QUIET_NOTIFICATION

    lead = items[0]
    today = today or lead.for_date
    headline = lead.headline.strip()
    found = _date_fact(lead)
    if found:
        with_date = f"{headline} {_short_date(found[1])}"
        if len(with_date) <= NOTIFICATION_CHARS:
            return with_date
    return _cut_words(headline, NOTIFICATION_CHARS)


# --------------------------------------------------------------------------
# Reading and delivery
# --------------------------------------------------------------------------


def load_signals(db_path: str | Path, run_id: str | None = None) -> list[Signal]:
    """The whole data access of this surface: one read-only connection (SUR-1)."""
    conn = connect(db_path, read_only=True)
    try:
        return read_signals(conn, run_id)
    finally:
        conn.close()


@dataclass(slots=True)
class DeliveryResult:
    """Delivery outcome. A refused send is returned, never raised (SUR-5)."""

    ok: bool
    status: int | None = None
    message_id: int | None = None
    error: str | None = None
    chars: int = 0
    text: str = field(default="", repr=False)
    # The forty characters of the lock screen, produced on the delivery path
    # rather than only in tests. On an ordinary day it repeats the head of the
    # message; on a failed run the two part ways, and voice.md section 1 wants
    # «Прогон не завершился» rather than the first words of section 6.
    notification: str = ""


def _credentials() -> tuple[str, str] | str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    chat_id = os.environ.get(CHAT_ENV, "").strip()
    missing = [
        name for name, value in ((TOKEN_ENV, token), (CHAT_ENV, chat_id)) if not value
    ]
    if missing:
        return f"missing environment variables: {', '.join(missing)}"
    return token, chat_id


def send(
    text: str, timeout: float = TIMEOUT_SECONDS, notification: str = ""
) -> DeliveryResult:
    """Send one message. Secrets come from the environment only (NFR-11).

    `notification` travels back on the result: the Bot API has no field for
    the push preview, so the line the reader should see on a locked screen is
    handed to the caller instead of being guessed from the message.
    """
    credentials = _credentials()
    if isinstance(credentials, str):
        return DeliveryResult(
            ok=False,
            error=credentials,
            chars=len(text),
            text=text,
            notification=notification,
        )
    token, chat_id = credentials

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": PARSE_MODE,
        "disable_web_page_preview": True,
    }
    request = Request(
        f"{API_ROOT}/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:  # noqa: PERF203 - each failure needs its own wording
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # pragma: no cover - body already consumed
            detail = ""
        return DeliveryResult(
            ok=False,
            status=exc.code,
            error=f"HTTP {exc.code} {detail}".strip(),
            chars=len(text),
            text=text,
            notification=notification,
        )
    except (URLError, TimeoutError, OSError) as exc:
        return DeliveryResult(
            ok=False,
            error=str(exc),
            chars=len(text),
            text=text,
            notification=notification,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return DeliveryResult(
            ok=False,
            error=f"malformed response: {exc}",
            chars=len(text),
            text=text,
            notification=notification,
        )

    if not body.get("ok"):
        return DeliveryResult(
            ok=False,
            status=status,
            error=str(body.get("description", "rejected by Bot API")),
            chars=len(text),
            text=text,
            notification=notification,
        )
    return DeliveryResult(
        ok=True,
        status=status,
        message_id=(body.get("result") or {}).get("message_id"),
        chars=len(text),
        text=text,
        notification=notification,
    )


def send_digest(
    signals: Sequence[Signal],
    today: date | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> DeliveryResult:
    """Render a run and deliver it. A quiet day is delivered like any other.

    The lock-screen line is built here, on the same path as the message, so
    the two are always produced from one run rather than one of them being
    inferred from the other later.
    """
    return send(
        render(signals, today),
        timeout=timeout,
        notification=notification_line(signals, today),
    )
