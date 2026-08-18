"""Trend detection over the corpus.

A trend is a grouping query, not a model output. `GROUP BY vendor, change_type
HAVING count >= N` produces the member list by construction, so FR-6.16 holds
structurally: there is no path by which a claim exists without the records
behind it. A model may later write the human label, and nothing else.

The harder problem is specificity. A vendor that ships weekly satisfies "three
events with a common trait" trivially, and "Anthropic released a version
fourteen times this half-year" is a formally perfect, substantively empty
trend. It would survive every evidence rule in the PRD and still read as
imitation of insight. So a candidate is also measured by cadence: an event a
vendor produces every few days carries no information by recurring, whatever
its type. Share of the vendor's record was the first heuristic here and it was
wrong — a corpus fed by a deprecations registry holds only deprecations for
that vendor, so the real trend scored as pure background.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from radar.cache import digest
from radar.models import ChangeType, Trajectory

DEFAULT_MIN_MEMBERS = 3
DEFAULT_DORMANT_AFTER = 90

# Specificity is measured by cadence, not by share of the vendor's record.
# Share looked reasonable and is wrong here: a corpus fed by a deprecations
# registry holds nothing but deprecations for that vendor, so the real trend
# would score 100 percent "background" and be discarded. Frequency is what
# separates a line from routine: an event a vendor produces every few days
# carries no information by recurring.
MIN_CADENCE_DAYS = 7

# Share stays as a secondary signal, but only once the corpus knows a vendor
# well enough for a proportion to mean anything.
BACKGROUND_SHARE_CEILING = 0.9
MIN_TYPES_FOR_SHARE_TEST = 3

# Types that are routine by nature. Excluded from trends unless a caller asks
# for them explicitly.
ROUTINE_TYPES = {ChangeType.RELEASE, ChangeType.OTHER}


@dataclass(slots=True)
class TrendCandidate:
    vendor: str
    change_type: str
    member_ids: list[str] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    background_share: float = 0.0
    rejected_reason: str | None = None

    @property
    def trend_id(self) -> str:
        return digest("trend", self.vendor, self.change_type)[:20]

    @property
    def members(self) -> int:
        return len(self.member_ids)

    @property
    def first_observed(self) -> date | None:
        return min(self.dates) if self.dates else None

    @property
    def last_observed(self) -> date | None:
        return max(self.dates) if self.dates else None

    def cadence_days(self) -> float | None:
        """Median interval between events. Median rather than mean: one long
        gap should not turn a steady cadence into a made-up number."""
        if len(self.dates) < 2:
            return None
        ordered = sorted(self.dates)
        gaps = [
            (later - earlier).days
            for earlier, later in zip(ordered, ordered[1:], strict=False)
        ]
        return float(statistics.median(gaps)) if gaps else None

    def trajectory(
        self, as_of: date, dormant_after: int = DEFAULT_DORMANT_AFTER
    ) -> Trajectory:
        if not self.dates:
            return Trajectory.DORMANT
        idle = (as_of - max(self.dates)).days
        if idle > dormant_after:
            return Trajectory.DORMANT
        # Four points is the modal trend in a thin corpus, and three gaps are
        # not enough to tell acceleration from noise: every shape tested at
        # n=4 came out "accelerating", including a flat one. Below six events
        # the honest answer is that the line is only emerging.
        if len(self.dates) < 6:
            return Trajectory.EMERGING
        ordered = sorted(self.dates)
        midpoint = len(ordered) // 2
        early = ordered[: midpoint + 1]
        late = ordered[midpoint:]
        early_span = (early[-1] - early[0]).days or 1
        late_span = (late[-1] - late[0]).days or 1
        early_rate = len(early) / early_span
        late_rate = len(late) / late_span
        if late_rate > early_rate * 1.5:
            return Trajectory.ACCELERATING
        return Trajectory.STEADY

    def as_record(self, as_of: date, label: str, dormant_after: int) -> dict[str, Any]:
        return {
            "trend_id": self.trend_id,
            "label": label,
            "vendor": self.vendor,
            "change_types": [self.change_type],
            "member_ids": self.member_ids,
            "first_observed": self.first_observed,
            "last_observed": self.last_observed,
            "cadence_days": self.cadence_days(),
            "trajectory": str(self.trajectory(as_of, dormant_after)),
            "evidence_refs": self.urls,
        }


def _vendor_type_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Distinct change types the corpus holds per vendor."""
    return {
        r["vendor"]: r["n"]
        for r in conn.execute(
            "SELECT vendor, COUNT(DISTINCT change_type) AS n FROM event_statements "
            "GROUP BY vendor"
        ).fetchall()
    }


def _vendor_totals(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        r["vendor"]: r["n"]
        for r in conn.execute(
            "SELECT vendor, COUNT(*) AS n FROM event_statements GROUP BY vendor"
        ).fetchall()
    }


def find_candidates(
    conn: sqlite3.Connection,
    min_members: int = DEFAULT_MIN_MEMBERS,
    include_routine: bool = False,
    since: date | None = None,
) -> tuple[list[TrendCandidate], list[TrendCandidate]]:
    """Group the corpus into candidates. Returns (accepted, rejected).

    Rejected candidates are returned rather than dropped: "these three groups
    exist but are this vendor's normal behaviour" is a real finding, and it is
    what keeps an empty trend off the screen.
    """
    totals = _vendor_totals(conn)
    type_counts = _vendor_type_counts(conn)
    params: list[Any] = []
    where = "WHERE event_date IS NOT NULL"
    if since:
        where += " AND event_date >= ?"
        params.append(since.isoformat())

    rows = conn.execute(
        f"SELECT vendor, change_type, statement_id, event_date, source_url, text "
        f"FROM event_statements {where} ORDER BY vendor, change_type, event_date",
        params,
    ).fetchall()

    grouped: dict[tuple[str, str], TrendCandidate] = {}
    for row in rows:
        key = (row["vendor"], row["change_type"])
        candidate = grouped.setdefault(
            key, TrendCandidate(vendor=key[0], change_type=key[1])
        )
        candidate.member_ids.append(row["statement_id"])
        candidate.dates.append(date.fromisoformat(row["event_date"]))
        candidate.urls.append(row["source_url"])
        candidate.texts.append(row["text"])

    accepted: list[TrendCandidate] = []
    rejected: list[TrendCandidate] = []
    for candidate in grouped.values():
        total = totals.get(candidate.vendor, 0) or 1
        candidate.background_share = candidate.members / total

        if candidate.members < min_members:
            candidate.rejected_reason = (
                f"участников {candidate.members}, нужно {min_members}"
            )
            rejected.append(candidate)
        elif not include_routine and ChangeType(candidate.change_type) in ROUTINE_TYPES:
            candidate.rejected_reason = (
                "рутинный тип изменения, повторяемость неинформативна"
            )
            rejected.append(candidate)
        elif (cadence := candidate.cadence_days()) is not None and cadence < MIN_CADENCE_DAYS:
            candidate.rejected_reason = (
                f"медианный интервал {cadence:.0f} дней: вендор делает это постоянно, "
                "повторение не несёт информации"
            )
            rejected.append(candidate)
        elif (
            type_counts.get(candidate.vendor, 1) >= MIN_TYPES_FOR_SHARE_TEST
            and candidate.background_share > BACKGROUND_SHARE_CEILING
        ):
            candidate.rejected_reason = (
                f"доля {candidate.background_share:.0%} записей вендора при "
                f"{type_counts.get(candidate.vendor)} типах: обычное поведение, а не линия"
            )
            rejected.append(candidate)
        else:
            accepted.append(candidate)

    accepted.sort(key=lambda c: (-c.members, c.vendor))
    rejected.sort(key=lambda c: (-c.members, c.vendor))
    return accepted, rejected


def default_label(
    candidate: TrendCandidate, labels: dict[str, str] | None = None
) -> str:
    """Label built from counts, usable without a model.

    Every number in it comes from the member list, so the phrase cannot say
    more than the records support.
    """
    labels = labels or {}
    vendor = labels.get(candidate.vendor, candidate.vendor)
    type_label = labels.get(candidate.change_type, candidate.change_type)
    first = candidate.first_observed
    cadence = candidate.cadence_days()
    parts = [f"{vendor}: {type_label}, {candidate.members} события"]
    if first:
        parts.append(f"с {first.isoformat()}")
    if cadence:
        parts.append(f"медианный интервал {int(cadence)} дней")
    return ", ".join(parts)


def save_trends(
    conn: sqlite3.Connection,
    candidates: list[TrendCandidate],
    as_of: date | None = None,
    dormant_after: int = DEFAULT_DORMANT_AFTER,
    labels: dict[str, str] | None = None,
) -> int:
    """Persist accepted candidates. Recomputed in full on every pass (FR-6.11).

    "In full" has to mean it. Two things were missing and both let the table
    drift away from the corpus it summarises: `first_observed` was left out of
    the upsert, so the oldest date froze at whatever the first pass saw — the
    anthropic/deprecation line still said September 2024 while its own corpus
    started in October 2025; and a cell that stopped qualifying kept its row,
    so openai/deprecation went on claiming twenty-five members against
    twenty-nine in the corpus and a cadence nobody had recomputed.

    A line that no longer holds is deleted rather than left stale. The corpus
    is append-only; this table is a derived view of it and owes it nothing.
    """
    as_of = as_of or datetime.now(UTC).date()
    written = 0
    keep = {candidate.trend_id for candidate in candidates}
    with conn:
        for candidate in candidates:
            record = candidate.as_record(
                as_of, default_label(candidate, labels), dormant_after
            )
            conn.execute(
                "INSERT INTO trends (trend_id, label, vendor, change_types_json, "
                "member_ids_json, first_observed, last_observed, cadence_days, trajectory, "
                "evidence_refs_json, updated_at) VALUES (?, ?, ?, json(?), json(?), ?, ?, ?, ?, "
                "json(?), ?) ON CONFLICT(trend_id) DO UPDATE SET label = excluded.label, "
                "member_ids_json = excluded.member_ids_json, "
                "first_observed = excluded.first_observed, "
                "last_observed = excluded.last_observed, cadence_days = excluded.cadence_days, "
                "trajectory = excluded.trajectory, "
                "evidence_refs_json = excluded.evidence_refs_json, "
                "updated_at = excluded.updated_at",
                (
                    record["trend_id"],
                    record["label"],
                    record["vendor"],
                    _json(record["change_types"]),
                    _json(record["member_ids"]),
                    record["first_observed"].isoformat()
                    if record["first_observed"]
                    else None,
                    record["last_observed"].isoformat()
                    if record["last_observed"]
                    else None,
                    record["cadence_days"],
                    record["trajectory"],
                    _json(record["evidence_refs"]),
                    datetime.now(UTC).isoformat(),
                ),
            )
            written += 1

        stale = [
            row["trend_id"]
            for row in conn.execute("SELECT trend_id FROM trends")
            if row["trend_id"] not in keep
        ]
        if stale:
            conn.executemany(
                "DELETE FROM trends WHERE trend_id = ?", [(tid,) for tid in stale]
            )
    return written


def trend_for_statement(
    conn: sqlite3.Connection, statement_id: str
) -> dict[str, Any] | None:
    """Trend a corpus record belongs to, if any."""
    row = conn.execute(
        "SELECT * FROM trends WHERE EXISTS ("
        "SELECT 1 FROM json_each(trends.member_ids_json) WHERE value = ?)",
        (statement_id,),
    ).fetchone()
    return dict(row) if row else None


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def active_trend(
    conn: sqlite3.Connection,
    vendor: str | None,
    change_type: str | None,
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """The line this cell belongs to, if the trends pass accepted one.

    Looked up by cell rather than by statement, because a card is written
    before its own event reaches the corpus — `trend_for_statement` can only
    answer for records already stored, which is never true of today's news.
    A dormant or closed line is not offered: saying "part of a pattern" about
    something that stopped happening claims more than the corpus supports.
    """
    if not vendor or not change_type:
        return None
    # The same derivation the writer uses. Spelling it "vendor:type" here
    # would look right and match nothing.
    row = conn.execute(
        "SELECT * FROM trends WHERE trend_id = ?",
        (digest("trend", vendor, change_type)[:20],),
    ).fetchone()
    if row is None:
        return None
    if str(row["trajectory"]) in {str(Trajectory.DORMANT), str(Trajectory.CLOSED)}:
        return None
    return dict(row)
