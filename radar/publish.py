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

import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from radar.assertions import resolve_context_label
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
) -> str | None:
    """One sentence, every number of which the precedent list can back.

    FR-6.18 bans quantifiers without a number: "вендор всё чаще" is forbidden,
    "третий раз с мая" is allowed and must come with the records.
    """
    if label is None or label is ContextLabel.NOT_FOUND_IN_CORPUS:
        return None
    dated = sorted(p.event_date for p in precedents if getattr(p, "event_date", None))
    if len(dated) < 2:
        return None

    count = len(precedents) + 1  # the precedents plus today's event
    earliest = dated[0]
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


def collect_upcoming(
    conn: sqlite3.Connection, as_of: date, limit: int = MAX_UPCOMING
) -> list[UpcomingDeadline]:
    """Dated obligations already in the corpus, nearest first."""
    rows = conn.execute(
        "SELECT vendor, product, text, event_date, date_precision, source_url "
        "FROM event_statements "
        "WHERE change_type IN ('deprecation', 'breaking_change') "
        "AND event_date IS NOT NULL AND event_date > ? AND event_date <= ? "
        "ORDER BY event_date ASC LIMIT ?",
        (
            as_of.isoformat(),
            date.fromordinal(as_of.toordinal() + UPCOMING_HORIZON_DAYS).isoformat(),
            limit * 3,
        ),
    ).fetchall()

    seen: set[tuple[str, str]] = set()
    out: list[UpcomingDeadline] = []
    for row in rows:
        when = date.fromisoformat(row["event_date"])
        subject = row["product"] or row["vendor"]
        key = (when.isoformat(), subject or "")
        if key in seen:
            continue
        seen.add(key)
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
        if len(out) >= limit:
            break
    return out


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
    vendor_label: str = "",
    change_type_label: str = "",
    run_summary: RunSummary | None = None,
    run_log_url: str | None = None,
    created_at: datetime | None = None,
) -> Signal:
    """Assemble one digest item.

    Nothing here truncates or marks up: PUB-2 makes that a surface operation,
    and a signal stored in Telegram's shape would make every later surface
    unbuildable on data already collected.
    """
    precedents = retrieval.precedents if retrieval else []
    # The count decides the label, never the model (FR-5.9, FR-6.17). Passed
    # in rather than read from a retriever so this function stays pure.
    label = (
        resolve_context_label(None, precedents)
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
        product=None,
        facts=facts,
        primary_url=cluster.primary.url,
        duplicates_count=cluster.duplicates_count,
        delta_status=delta.status if delta else None,
        delta_note=delta.note if delta else None,
        days_tracked=delta.days_tracked if delta else 1,
        context_label=label,
        precedents=precedents,
        retrieval=retrieval.report if retrieval else None,
        context_note=build_context_note(
            label,
            precedents,
            vendor_label or (cluster.vendor or ""),
            change_type_label or (cluster.change_type or ""),
            for_date,
        ),
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
