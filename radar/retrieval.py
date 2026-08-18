"""Stage 5B: retrieval over the corpus.

Hybrid by FR-6.12: hard metadata filtering first, ranking inside the survivors
second. At corpus sizes this project will see, the filter does nearly all the
work — vendor plus change type plus a date window leaves single digits — so
the ranking exists to order them, not to find them.

Two queries run, not one. The strict query is what FR-6.13 mandates. The
relaxed query widens to neighbouring change types and never feeds a published
claim; its only job is to put a number on what the strict filter missed. A
classifier that called one event `limits` in March and `pricing` in June makes
the strict query return nothing, and the system then says "no precedents" with
complete confidence. That failure is invisible unless it is counted.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from radar.models import (
    ChangeType,
    ContextLabel,
    DatePrecision,
    Precedent,
    RetrievalReport,
)

DEFAULT_WINDOWS = [180, 365]
DEFAULT_MAX_RESULTS = 12
DEFAULT_MIN_EVIDENCE = 2

# Groups whose members are routinely confused for one another at extraction
# time. Used only by the relaxed query.
DEFAULT_RELAXED_GROUPS = [
    ["pricing", "limits"],
    ["deprecation", "breaking_change"],
]

_FTS_UNSAFE = re.compile(r"[^\w\s]", re.UNICODE)


def fts_query(text: str, max_terms: int = 12) -> str:
    """Turn free text into a safe FTS5 OR-query.

    User text reaches FTS5 as a query language, not as data: an unescaped
    quote or a bare NEAR turns into a syntax error mid-run.
    """
    cleaned = _FTS_UNSAFE.sub(" ", text or "")
    terms = [t for t in cleaned.split() if len(t) > 2][:max_terms]
    return " OR ".join(f'"{t}"' for t in terms)


@dataclass(slots=True)
class RetrievalHit:
    statement_id: str
    text: str
    source_url: str
    vendor: str
    change_type: str
    event_date: date | None
    date_precision: str
    evidence: str
    rank_score: float = 0.0

    def to_precedent(self) -> Precedent:
        return Precedent(
            statement_id=self.statement_id,
            text=self.text,
            source_url=self.source_url,
            event_date=self.event_date,
            date_precision=DatePrecision(self.date_precision)
            if self.date_precision
            else DatePrecision.DAY,
            vendor=self.vendor,
            change_type=ChangeType(self.change_type),
        )


@dataclass(slots=True)
class RetrievalResult:
    hits: list[RetrievalHit] = field(default_factory=list)
    report: RetrievalReport = field(default_factory=RetrievalReport)
    relaxed_only: list[RetrievalHit] = field(default_factory=list)
    window_used: int | None = None

    @property
    def precedents(self) -> list[Precedent]:
        return [h.to_precedent() for h in self.hits]


def _row_to_hit(row: sqlite3.Row, rank_score: float = 0.0) -> RetrievalHit:
    raw_date = row["event_date"]
    return RetrievalHit(
        statement_id=row["statement_id"],
        text=row["text"],
        source_url=row["source_url"],
        vendor=row["vendor"],
        change_type=row["change_type"],
        event_date=date.fromisoformat(raw_date) if raw_date else None,
        date_precision=row["date_precision"] or "day",
        evidence=row["evidence"],
        rank_score=rank_score,
    )


class CorpusRetriever:
    def __init__(
        self, conn: sqlite3.Connection, config: dict[str, Any] | None = None
    ) -> None:
        self.conn = conn
        cfg = config or {}
        self.windows: list[int] = cfg.get("windows", DEFAULT_WINDOWS)
        self.max_results: int = int(
            cfg.get("max_results_per_cluster", DEFAULT_MAX_RESULTS)
        )
        self.min_evidence: int = int(
            cfg.get("min_evidence_for_trend", DEFAULT_MIN_EVIDENCE)
        )
        self.relaxed_groups: list[list[str]] = cfg.get(
            "relaxed_change_type_groups", DEFAULT_RELAXED_GROUPS
        )

    def neighbours_of(self, change_type: str) -> list[str]:
        types = {change_type}
        for group in self.relaxed_groups:
            if change_type in group:
                types.update(group)
        return sorted(types)

    def _filter(
        self,
        vendor: str,
        change_types: list[str],
        as_of: date,
        window_days: int,
        exclude_ids: set[str],
    ) -> tuple[str, list[Any]]:
        """The strict filter as one FROM/WHERE clause, shared by both queries.

        Counting and listing must not be able to disagree, so neither builds
        its own predicate: the page of records the reader sees and the number
        printed above it are drawn from the same clause with the same
        parameters.
        """
        earliest = (as_of - timedelta(days=window_days)).isoformat()
        placeholders = ",".join("?" * len(change_types))
        params: list[Any] = [vendor, *change_types, earliest, as_of.isoformat()]

        sql = (
            "FROM event_statements s "
            f"WHERE s.vendor = ? AND s.change_type IN ({placeholders}) "
            "AND s.event_date IS NOT NULL AND s.event_date >= ? AND s.event_date <= ? "
            # Superseded records stay in the corpus (FR-5.18) but must not be
            # cited as if they were still current.
            "AND s.statement_id NOT IN (SELECT supersedes FROM event_statements "
            "WHERE supersedes IS NOT NULL)"
        )
        if exclude_ids:
            ids = sorted(exclude_ids)
            sql += f" AND s.statement_id NOT IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        return sql, params

    def _count(
        self,
        vendor: str,
        change_types: list[str],
        as_of: date,
        window_days: int,
        exclude_ids: set[str],
    ) -> tuple[int, date | None]:
        """How many records match, and how far back they go.

        Separate from the listing on purpose. `max_results` is a page size for
        the reader; a count taken from the page can never exceed it, and a
        sentence built on that length reports the pagination constant instead
        of the corpus. The oldest date comes from the same aggregate so the
        count and the date in that sentence describe one and the same set.
        """
        clause, params = self._filter(
            vendor, change_types, as_of, window_days, exclude_ids
        )
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n, MIN(s.event_date) AS earliest {clause}", params
        ).fetchone()
        if row is None:
            return 0, None
        raw = row["earliest"]
        return int(row["n"]), date.fromisoformat(raw) if raw else None

    def _query(
        self,
        vendor: str,
        change_types: list[str],
        as_of: date,
        window_days: int,
        text: str | None,
        exclude_ids: set[str],
    ) -> list[RetrievalHit]:
        clause, params = self._filter(
            vendor, change_types, as_of, window_days, exclude_ids
        )
        rows = self.conn.execute(
            f"SELECT s.* {clause} ORDER BY s.event_date DESC", params
        ).fetchall()
        hits = [_row_to_hit(r) for r in rows]

        if text:
            matched = self._semantic_order(text, {h.statement_id for h in hits})
            for hit in hits:
                hit.rank_score = matched.get(hit.statement_id, 0.0)
            # Freshness decides ties (FR-6.14).
            hits.sort(
                key=lambda h: (-h.rank_score, -(h.event_date or date.min).toordinal())
            )
        return hits

    def _semantic_order(self, text: str, candidate_ids: set[str]) -> dict[str, float]:
        """FTS5 relevance for the candidates the metadata filter left."""
        query = fts_query(text)
        if not query or not candidate_ids:
            return {}
        try:
            rows = self.conn.execute(
                "SELECT s.statement_id AS sid, bm25(event_statements_fts) AS score "
                "FROM event_statements_fts f "
                "JOIN event_statements s ON s.rowid = f.rowid "
                "WHERE event_statements_fts MATCH ? ",
                (query,),
            ).fetchall()
        except sqlite3.OperationalError:
            # A malformed query must not take the run down; the metadata
            # filter has already produced a usable answer on its own.
            return {}
        # bm25 returns lower-is-better; invert so higher means more relevant.
        return {r["sid"]: -float(r["score"]) for r in rows if r["sid"] in candidate_ids}

    def find_precedents(
        self,
        vendor: str | None,
        change_type: str | None,
        as_of: date,
        text: str | None = None,
        exclude_ids: set[str] | None = None,
    ) -> RetrievalResult:
        """Precedents for one cluster, with both counts recorded.

        Windows are tried shortest first and the search stops at the first one
        that clears the evidence threshold, so a claim is dated as tightly as
        the corpus allows.
        """
        result = RetrievalResult()
        result.report.windows_days = list(self.windows)
        if not vendor or not change_type:
            # FR-5.16: both are mandatory. Without them the filter would match
            # the whole corpus and every claim would look supported.
            return result

        exclude_ids = exclude_ids or set()
        # The window is chosen on the count, not on the page: a cap of 12 must
        # not make a window with 100 matches look the same as one with 12.
        strict_total = 0
        strict_earliest: date | None = None
        window_used: int | None = None
        for window in self.windows:
            strict_total, strict_earliest = self._count(
                vendor, [change_type], as_of, window, exclude_ids
            )
            window_used = window
            if strict_total >= self.min_evidence:
                break

        strict: list[RetrievalHit] = (
            self._query(vendor, [change_type], as_of, window_used, text, exclude_ids)
            if window_used is not None
            else []
        )

        widest = max(self.windows) if self.windows else 365
        relaxed_total, _ = self._count(
            vendor, self.neighbours_of(change_type), as_of, widest, exclude_ids
        )
        relaxed = self._query(
            vendor, self.neighbours_of(change_type), as_of, widest, text, exclude_ids
        )
        strict_ids = {h.statement_id for h in strict}

        result.hits = strict[: self.max_results]
        result.relaxed_only = [h for h in relaxed if h.statement_id not in strict_ids]
        result.window_used = window_used
        result.report = RetrievalReport(
            strict_hits=strict_total,
            relaxed_hits=relaxed_total,
            # Counted by the corpus, capped only for display. `shown` is the
            # page size; anything published as a number must come from here.
            total_found=strict_total,
            shown=len(result.hits),
            earliest_event_date=strict_earliest,
            windows_days=[window_used] if window_used else list(self.windows),
        )
        return result

    def label_for(self, result: RetrievalResult) -> ContextLabel:
        """Label implied by the count alone.

        The model never picks this. Below the threshold the answer is that the
        corpus holds nothing, phrased as coverage rather than as a property of
        the event.
        """
        if len(result.hits) < self.min_evidence:
            return ContextLabel.NOT_FOUND_IN_CORPUS
        return ContextLabel.RECURRING

    def coverage_for(self, vendor: str) -> dict[str, Any]:
        """How much of this vendor the corpus actually holds.

        Attached to a `not_found_in_corpus` label so the absence can be read
        as thin coverage rather than as evidence of a first occurrence.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(event_date) AS earliest, MAX(event_date) AS latest "
            "FROM event_statements WHERE vendor = ?",
            (vendor,),
        ).fetchone()
        by_type = {
            r["change_type"]: r["n"]
            for r in self.conn.execute(
                "SELECT change_type, COUNT(*) AS n FROM event_statements "
                "WHERE vendor = ? GROUP BY change_type",
                (vendor,),
            ).fetchall()
        }
        return {
            "vendor": vendor,
            "statements": row["n"],
            "earliest": row["earliest"],
            "latest": row["latest"],
            "by_change_type": by_type,
        }
