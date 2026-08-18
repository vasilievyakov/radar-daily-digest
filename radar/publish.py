"""Stage 7: publication.

The pipeline ends here. The core does not know that Telegram, email or a
native client exist; it writes Signal records and stops. Everything past this
point belongs to surfaces.

Two things are built here rather than left to surfaces, and both for the same
reason: a surface deriving them would be recomputing the corpus, which SUR-2
forbids, and three surfaces would word one claim three ways.

- `context_note`, the sentence above the precedent list. Every number in it
  comes from the precedents themselves, so it cannot claim more than they
  support (FR-6.16).
- `upcoming`, the deadlines shown on a quiet day. Silence is filled with what
  the reader planned to forget, taken from facts already verified.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from radar.assertions import resolve_context_label, validate_precedents
from radar.cache import digest
from radar.cluster import Cluster
from radar.collect import SourceOutcome
from radar.delta import DeltaOutcome
from radar.models import (
    ChangeType,
    DatePrecision,
    ContextLabel,
    Fact,
    FactKind,
    RunSummary,
    Signal,
    SignalType,
    SourceStatus,
    Tier,
    UpcomingDeadline,
)
from radar.retrieval import RetrievalResult
from radar.trends import ROUTINE_TYPES

MONTHS_GENITIVE = [
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
]

# Beyond this a deadline is too far away to be worth the quiet-day block.
UPCOMING_HORIZON_DAYS = 120
MAX_UPCOMING = 3
# Rows read before deduplication. The corpus holds one obligation several
# times over, so reading `MAX_UPCOMING` rows would return one event three
# times and never reach the second.
UPCOMING_SCAN_LIMIT = 200

# `gemini-2.5-flash-image`, `claude-mythos-preview`, `top_p`: starts with a
# letter and carries an internal dot, hyphen or underscore. Ordinary Russian
# prose has none of those, so a sentence without a technical name yields an
# empty set and is treated as unidentifiable rather than as a new event.
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._\-][A-Za-z0-9]+)+")


def _day_month(value: date) -> str:
    return f"{value.day} {MONTHS_GENITIVE[value.month - 1]}"


def make_signal_id(run_id: str, cluster_id: str) -> str:
    """Stable across reruns of the same run (PUB-5).

    A surface tracks what the reader has already seen by this id, so a
    regenerated run must not resurface everything as unread.
    """
    return digest("signal", run_id, cluster_id)[:20]


def build_context_note(
    label: ContextLabel | None,
    precedents: list[Any],
    vendor_label: str,
    change_type_label: str,
    as_of: date,
    *,
    total_found: int | None = None,
    earliest_match: date | None = None,
    change_type: ChangeType | None = None,
) -> str | None:
    """One sentence, every number of which the corpus can back.

    FR-6.18 bans quantifiers without a number: "вендор всё чаще" is forbidden,
    "третий раз с мая" is allowed and must come with the records.

    `precedents` is a page, not the evidence base: retrieval caps it at
    `max_results`. Counting it printed "13-й раз" for every event with twelve
    or more precedents — a number produced by the pagination constant, not by
    the corpus. `total_found` and `earliest_match` come from the retrieval
    COUNT(*) over the strict filter and are what the sentence quotes.
    """
    if label is None or label is ContextLabel.NOT_FOUND_IN_CORPUS:
        return None
    # A vendor ships releases; saying so seventeen times over is not context.
    # The trends stage already holds this judgement (ROUTINE_TYPES) and the
    # readiness report prints it — "routine change type, recurrence carries no
    # information" — while the card said "Anthropic: other, the 17th time since
    # August 17" about sixteen bullet points of one changelog.
    if change_type in ROUTINE_TYPES:
        return None
    dated = sorted(p.event_date for p in precedents if getattr(p, "event_date", None))
    if len(dated) < 2:
        return None

    # A pattern needs an interval. "The third time since 17 August", said on
    # 17 August, is a claim about repetition with no time in it: the records
    # behind it were all written this morning, usually from one page. The
    # sentence is worth making only when the corpus reaches back past today.
    earliest_known = min([earliest_match, dated[0]]) if earliest_match else dated[0]
    if earliest_known >= as_of:
        return None

    # Never below what is on the page: the shown records are themselves proof.
    matches = max(total_found, len(precedents)) if total_found else len(precedents)
    count = matches + 1  # the precedents plus today's event
    earliest = min(earliest_match, dated[0]) if earliest_match else dated[0]
    since = (
        f"с {_day_month(earliest)}"
        if earliest.year == as_of.year
        else f"с {_day_month(earliest)} {earliest.year} года"
    )
    ordinal = {2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый"}.get(count)
    head = f"{ordinal} раз" if ordinal else f"{count}-й раз"

    if label is ContextLabel.ESCALATION:
        return f"{head} {since}, и в этот раз требование ужесточилось."
    return f"{vendor_label}: {change_type_label.lower()}, {head} {since}."


def upcoming_identifiers(text: str) -> set[str]:
    """Technical names inside a statement: `gemini-2.5-flash-image` and kin.

    The subject of a deprecation is the identifier a reader greps their code
    for, and it is the only part of the sentence that survives four different
    wordings of the same announcement. A token qualifies when it starts with a
    letter and carries an internal dot or hyphen, which keeps model and package
    names and leaves ordinary Russian prose out.
    """
    return {match.group(0).lower() for match in _IDENTIFIER_RE.finditer(text)}


def _same_event(left: tuple[str, set[str]], right: tuple[str, set[str]]) -> bool:
    """Two statements about one obligation, on the same date and vendor.

    Identifiers decide it. Two named models sharing a shutdown date are two
    deadlines and both belong in the block; the same model named twice is one.
    A sentence that names no identifier ("стабильная модель Gemini 2.5 Flash
    для работы с изображениями") cannot be told apart from its neighbours, so
    it folds into the group rather than standing beside it as a third copy.
    """
    left_vendor, left_ids = left
    right_vendor, right_ids = right
    if left_vendor != right_vendor:
        return False
    if not left_ids or not right_ids:
        return True
    return bool(left_ids & right_ids)


def collect_upcoming(
    conn: sqlite3.Connection, as_of: date, limit: int = MAX_UPCOMING
) -> list[UpcomingDeadline]:
    """Dated obligations already in the corpus, nearest first.

    One obligation appears in the corpus many times over: several sources, and
    several extraction passes over one source, each wording it their own way.
    Keying by product was not enough — the same shutdown arrives as product
    `Gemini API`, product `Gemini Robotics API` and product `NULL`, so the
    quiet-day block filled all three of its slots with one event. Deduplication
    therefore happens on what the sentences agree about: date, vendor and the
    identifier they name.
    """
    rows = conn.execute(
        "SELECT vendor, product, text, event_date, date_precision, source_url "
        "FROM event_statements "
        "WHERE change_type IN ('deprecation', 'breaking_change') "
        "AND event_date IS NOT NULL AND event_date > ? AND event_date <= ? "
        # Wide enough that duplicates cannot crowd out the second real
        # deadline: the horizon already bounds this, the cap is a guard.
        "ORDER BY event_date ASC LIMIT ?",
        (
            as_of.isoformat(),
            date.fromordinal(as_of.toordinal() + UPCOMING_HORIZON_DAYS).isoformat(),
            UPCOMING_SCAN_LIMIT,
        ),
    ).fetchall()

    groups: list[tuple[date, str, set[str], Any]] = []
    for row in rows:
        when = date.fromisoformat(row["event_date"])
        text = (row["text"] or "").strip()
        vendor = row["vendor"] or ""
        ids = upcoming_identifiers(text)
        for index, (seen_when, seen_vendor, seen_ids, best) in enumerate(groups):
            if seen_when != when or not _same_event(
                (vendor, ids), (seen_vendor, seen_ids)
            ):
                continue
            seen_ids |= ids
            # The wording kept is the shortest that names the identifier: the
            # reader wants the model and the date, not the fourth paraphrase.
            if _better_wording(row, best):
                groups[index] = (seen_when, seen_vendor, seen_ids, row)
            break
        else:
            groups.append((when, vendor, ids, row))

    out: list[UpcomingDeadline] = []
    for when, _vendor, _ids, row in groups[:limit]:
        out.append(
            UpcomingDeadline(
                when=when,
                what=row["text"].strip(),
                vendor=row["vendor"],
                source_url=row["source_url"],
                # Carried through: a year recovered from context must not
                # render as a firm deadline.
                date_precision=DatePrecision(row["date_precision"] or "day"),
            )
        )
    return out


def _better_wording(candidate: Any, current: Any) -> bool:
    """Deterministic pick among duplicates, so reruns say the same thing."""
    candidate_text = (candidate["text"] or "").strip()
    current_text = (current["text"] or "").strip()
    candidate_named = bool(upcoming_identifiers(candidate_text))
    current_named = bool(upcoming_identifiers(current_text))
    if candidate_named != current_named:
        return candidate_named
    if len(candidate_text) != len(current_text):
        return len(candidate_text) < len(current_text)
    return candidate_text < current_text


def build_run_summary(
    outcomes: list[SourceOutcome],
    materials_collected: int,
    materials_filtered: int,
    cost_usd: float = 0.0,
    last_success: date | None = None,
    name_of: dict[str, str] | None = None,
) -> RunSummary:
    """Names, not just counts.

    Surfaces cannot read `source_runs` (SUR-1), so a footer naming the source
    that failed is only possible if the name travels inside the contract.
    """
    name_of = name_of or {}
    return RunSummary(
        sources_checked=len(outcomes),
        sources_failed=[
            name_of.get(o.source_id, o.source_id)
            for o in outcomes
            if o.status is SourceStatus.FAILED
        ],
        sources_empty=[
            name_of.get(o.source_id, o.source_id)
            for o in outcomes
            if o.status is SourceStatus.EMPTY
        ],
        materials_collected=materials_collected,
        materials_filtered=materials_filtered,
        last_success_date=last_success,
        cost_usd=round(cost_usd, 4),
    )


def choose_due_date(
    facts: list[Fact], as_of: date
) -> tuple[date | None, DatePrecision]:
    """The one date a card is about, decided in the core and only here.

    Nearest obligation still ahead; if every date has passed, the most recent
    one, because "the deadline was three days ago" is news too. An inferred
    date never leads: showing "in 59 days" for a year recovered from context is
    false precision (FR-5.12).
    """
    dated = [
        (f.value_date, f.date_precision)
        for f in facts
        if f.value_date is not None
        and f.kind in {FactKind.SUNSET_DATE, FactKind.EFFECTIVE_DATE}
        and f.date_precision is not DatePrecision.INFERRED
    ]
    if not dated:
        return None, DatePrecision.DAY
    ahead = [pair for pair in dated if pair[0] >= as_of]
    if ahead:
        return min(ahead, key=lambda pair: pair[0])
    return max(dated, key=lambda pair: pair[0])


def build_signal(
    run_id: str,
    for_date: date,
    cluster: Cluster,
    facts: list[Fact],
    delta: DeltaOutcome | None,
    retrieval: RetrievalResult | None,
    score: int,
    rationale: str,
    tier: Tier,
    rank: int,
    *,
    headline: str,
    summary: str,
    why_it_matters: str = "",
    # Extracted by stage 4 and previously hardcoded to None here, so every
    # card claimed the vendor changed something unnamed.
    product: str | None = None,
    vendor_label: str = "",
    change_type_label: str = "",
    run_summary: RunSummary | None = None,
    run_log_url: str | None = None,
    created_at: datetime | None = None,
    trend: dict[str, Any] | None = None,
) -> Signal:
    """Assemble one digest item.

    Nothing here truncates or marks up: PUB-2 makes that a surface operation,
    and a signal stored in Telegram's shape would make every later surface
    unbuildable on data already collected.
    """
    precedents = retrieval.precedents if retrieval else []
    # The fifteenth guard found written and never called. Precedents reach the
    # card and the count in "the twentieth time since February", so one from
    # another vendor, another change type, or the same record twice is a false
    # number on the page — and the query that fetched them is not the only
    # thing that can be wrong about them. Applied here rather than at the call
    # site because here it cannot be skipped.
    dropped: list[tuple[str, str]] = []
    if precedents:
        precedents, dropped = validate_precedents(
            precedents, cluster.vendor, cluster.change_type
        )
    report = retrieval.report if retrieval else None
    if report is not None and dropped:
        report = report.model_copy(
            update={
                "shown": len(precedents),
                "total_found": max(0, report.total_found - len(dropped)),
                "strict_hits": max(0, report.strict_hits - len(dropped)),
            }
        )
    due = choose_due_date(facts, for_date)
    # The count decides the label, never the model (FR-5.9, FR-6.17). Passed
    # in rather than read from a retriever so this function stays pure.
    label = (
        resolve_context_label(None, precedents, in_trend=trend is not None)
        if retrieval is not None
        else None
    )

    return Signal(
        signal_id=make_signal_id(run_id, cluster.cluster_id),
        run_id=run_id,
        signal_type=SignalType.DIGEST_ITEM,
        created_at=created_at or datetime.now(UTC),
        for_date=for_date,
        headline=headline,
        summary=summary,
        why_it_matters=why_it_matters,
        change_type=ChangeType(cluster.change_type) if cluster.change_type else None,
        vendor=cluster.vendor,
        product=product,
        facts=facts,
        primary_url=cluster.primary.url,
        duplicates_count=cluster.duplicates_count,
        delta_status=delta.status if delta else None,
        delta_note=delta.note if delta else None,
        in_progress=bool(delta is not None and not delta.is_publishable),
        days_tracked=delta.days_tracked if delta else 1,
        context_label=label,
        # Populated at last. The field, the label `trend_member` and the
        # rendering for both existed from the start and nothing ever wrote
        # them: the daily run never consulted the trends table.
        trend_id=str(trend["trend_id"]) if trend else None,
        precedents=precedents,
        retrieval=report,
        context_note=build_context_note(
            label,
            precedents,
            vendor_label or (cluster.vendor or ""),
            change_type_label or (cluster.change_type or ""),
            for_date,
            # The count and the date come from the corpus query, not from the
            # capped list above it.
            total_found=report.total_found if report else None,
            earliest_match=report.earliest_event_date if report else None,
            change_type=ChangeType(cluster.change_type) if cluster.change_type else None,
        ),
        due_date=due[0],
        due_precision=due[1],
        score=score,
        score_rationale=rationale,
        rank=rank,
        tier=tier,
        run_summary=run_summary,
        run_log_url=run_log_url,
    )


def build_quiet_day(
    conn: sqlite3.Connection,
    run_id: str,
    for_date: date,
    run_summary: RunSummary,
    run_log_url: str | None = None,
    created_at: datetime | None = None,
) -> Signal:
    """Absence of signals is a signal, and it goes into the store (PUB-4).

    Not a special case handled by each surface: a record every surface can
    render on its own, which is what SUR-4 requires.
    """
    return Signal(
        signal_id=make_signal_id(run_id, "quiet-day"),
        run_id=run_id,
        signal_type=SignalType.QUIET_DAY,
        created_at=created_at or datetime.now(UTC),
        for_date=for_date,
        headline="Сегодня в вашем стеке ничего не изменилось",
        summary="",
        upcoming=collect_upcoming(conn, for_date),
        run_summary=run_summary,
        tier=Tier.LEAD,
        rank=1,
        run_log_url=run_log_url,
    )


def build_run_failure(
    run_id: str,
    for_date: date,
    stage: str,
    reason: str,
    run_summary: RunSummary,
    run_log_url: str | None = None,
    created_at: datetime | None = None,
) -> Signal:
    """A run that died reports itself.

    `run_failure` appears once in the PRD enum and nowhere else, yet a daily
    agent gone silent looks from outside exactly like a quiet day. That is the
    one failure this product cannot afford.
    """
    return Signal(
        signal_id=make_signal_id(run_id, "run-failure"),
        run_id=run_id,
        signal_type=SignalType.RUN_FAILURE,
        created_at=created_at or datetime.now(UTC),
        for_date=for_date,
        headline=f"Прогон {_day_month(for_date)} не завершился",
        summary="",
        failure_reason=reason,
        failure_stage=stage,
        run_summary=run_summary,
        tier=Tier.LEAD,
        rank=1,
        run_log_url=run_log_url,
    )


def facts_to_upcoming(
    facts: list[Fact], as_of: date, limit: int = MAX_UPCOMING
) -> list[UpcomingDeadline]:
    """Fallback source for the quiet-day block when the corpus is thin."""
    out: list[UpcomingDeadline] = []
    for fact in facts:
        if fact.kind not in {FactKind.SUNSET_DATE, FactKind.EFFECTIVE_DATE}:
            continue
        when = fact.value_date
        if when is None or when <= as_of:
            continue
        out.append(
            UpcomingDeadline(
                when=when,
                what=fact.subject or fact.value,
                source_url=fact.source_url,
                date_precision=fact.date_precision,
            )
        )
    out.sort(key=lambda d: d.when)
    return out[:limit]
