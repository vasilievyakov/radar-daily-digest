"""Stage 5A: short-term delta against previous runs.

Deterministic on purpose. Pass B asks the corpus what a signal means in
historical context and needs a model for that; pass A only asks whether we
saw this exact story yesterday, and exact matching answers it better than any
model would. A model would also regroup materials differently each morning,
and every claim here is a join on `cluster_id` staying stable.

The visible promise is "third day running". That is checkable by anyone in the
audience, so it has to be true rather than plausible.

The same question is asked one stage earlier and about a smaller thing. Before
a material can be a story it has to be new, and "new" for a daily feed means
"the system had not seen this record before" — not "the event it describes is
dated recently". The two coincide for a changelog and come apart completely
for a table of future retirements, where every row is dated ahead of today and
therefore clears any window, every morning, forever. `filter_unseen` below is
where that distinction is drawn, and it lives here because this module is the
one that already remembers previous runs. An adapter cannot answer it: it is
handed a URL and a window and has no history to compare against.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from radar.adapters.base import CollectedItem
from radar.cluster import Cluster
from radar.models import DeltaStatus, Fact

# Operational layer horizon (PRD 5.8). Older clusters are the corpus's job.
STATE_HORIZON_DAYS = 30


# --------------------------------------------------------------------------
# First sighting: is this record new to us at all?
# --------------------------------------------------------------------------


def material_id(item: CollectedItem) -> str:
    """Identity of a material, stable across runs.

    Deliberately the very key the collector already dedupes by, rather than a
    second opinion about identity: canonical URL plus the head of the text.
    Two notions of "the same material" in one pipeline drift apart, and the
    day they do, the ledger below starts hiding real news or re-announcing old
    rows, with nothing in the log to say which.

    Content participates for the same reason it does in the collector: a page
    whose headings carry no anchors gives every section one address. It also
    makes the right thing happen when a table row is edited — a retirement
    date moved from November to September is a different record, and that is
    news, not a repeat.
    """
    from radar.collect import dedupe_key

    return dedupe_key(item)


def _first_seen_before(stamp: str, as_of: date) -> bool:
    """Was this row written down by an earlier day's run?

    An unreadable timestamp counts as earlier. The row exists, so the material
    was seen; the date is consulted only to let the same day's rerun repeat
    itself, and a stamp we cannot read is no evidence that the day is today.
    """
    try:
        return date.fromisoformat(stamp.strip()[:10]) < as_of
    except (AttributeError, ValueError):
        return True


def filter_unseen(
    conn: sqlite3.Connection,
    items: list[CollectedItem],
    as_of: date | None = None,
    now: datetime | None = None,
) -> list[CollectedItem]:
    """Keep the materials this run is the first to meet; record all of them.

    Freshness in the live run is first sighting, not event date. A changelog
    entry and a row of a retirement table are then treated alike: each is news
    on the day it appears and silent afterwards, and a quiet day becomes
    reachable on the full config instead of being arithmetically impossible.

    Two properties this has to keep:

    * Idempotent within the day (PUB-5). A material first written down today
      stays new for every rerun today, so a repeated run publishes the same
      digest instead of an empty one. Only a stamp from an earlier day closes
      a record.
    * Silent in backfill. Backfill never calls this — `radar.backfill` builds
      the corpus straight from `collect_all(mode="backfill")` — so the whole
      depth of a table still reaches the corpus untouched.

    `cluster_id` is deliberately left NULL: `raw_items` points at `clusters`,
    which `prune_state` deletes from after thirty days, and a filled-in
    reference would turn pruning into a foreign-key error.
    """
    as_of = as_of or datetime.now(UTC).date()
    now = now or datetime.now(UTC)
    if now.date() != as_of:
        # A run stamped for a day other than the wall clock: `--for-date`,
        # a catch-up after an outage, the demo playing two mornings in one
        # afternoon. The ledger answers "which run-day met this first", so it
        # has to record the run's day; keeping the clock's would make both of
        # those mornings the same day and the second one empty.
        now = datetime.combine(as_of, now.timetz())

    kept: list[CollectedItem] = []
    rows: list[tuple[Any, ...]] = []
    in_batch: set[str] = set()
    for item in items:
        key = material_id(item)
        if key in in_batch:  # the collector merges these already; belt and braces
            continue
        in_batch.add(key)
        known = conn.execute(
            "SELECT collected_at FROM raw_items WHERE id = ?", (key,)
        ).fetchone()
        if known is not None and _first_seen_before(known[0], as_of):
            continue
        kept.append(item)
        rows.append(
            (
                key,
                str(item.extra.get("source_id", "")),
                item.url,
                item.title,
                item.published_at.isoformat() if item.published_at else None,
                now.isoformat(),
                item.raw_material_ref,
                json.dumps(item.extra.get("seen_in", []), ensure_ascii=False),
            )
        )

    if rows:
        with conn:
            conn.executemany(
                "INSERT INTO raw_items (id, source_id, url, title, published_at, "
                "collected_at, raw_ref, seen_in_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                # First sighting is the fact being stored, so a rerun must not
                # move the timestamp forward: that would make today's records
                # new again tomorrow.
                "ON CONFLICT(id) DO NOTHING",
                rows,
            )
    return kept


@dataclass(slots=True)
class DeltaOutcome:
    cluster_id: str
    status: DeltaStatus
    days_tracked: int = 1
    note: str | None = None
    new_facts: list[Fact] = field(default_factory=list)
    first_seen_run: str | None = None

    @property
    def is_publishable(self) -> bool:
        """`continuing` without change goes to the folded section (FR-5.3)."""
        return self.status is not DeltaStatus.CONTINUING


def _fact_key(fact: Fact) -> tuple[str, str]:
    return (str(fact.kind), fact.value.strip().casefold())


def _load_state(conn: sqlite3.Connection, cluster_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()
    return dict(row) if row else None


def _describe_new_facts(facts: list[Fact]) -> str:
    """Say what changed in words a reader can check against the card.

    Every number in the sentence comes from a verified fact, so the phrasing
    cannot claim more than the evidence behind it.
    """
    readable = {
        "sunset_date": "названа дата отключения",
        "effective_date": "названа дата вступления в силу",
        "version": "названа версия",
        "price": "названа цена",
        "limit": "названо значение лимита",
        "affected_product": "назван затронутый продукт",
    }
    parts = [f"{readable.get(str(f.kind), str(f.kind))}: {f.value}" for f in facts[:3]]
    return "вчера этого не было, сегодня " + ", ".join(parts) if parts else ""


def compute_delta(
    conn: sqlite3.Connection,
    cluster: Cluster,
    facts: list[Fact],
    run_id: str,
    as_of: date | None = None,
) -> DeltaOutcome:
    """Status of one cluster relative to the operational layer."""
    as_of = as_of or datetime.now(UTC).date()
    state = _load_state(conn, cluster.cluster_id)

    if state is None:
        return DeltaOutcome(
            cluster_id=cluster.cluster_id,
            status=DeltaStatus.NEW,
            days_tracked=1,
            first_seen_run=run_id,
        )

    known = {_fact_key(Fact.model_validate(f)) for f in json.loads(state["facts_json"])}
    fresh = [f for f in facts if _fact_key(f) not in known]
    days = int(state["days_tracked"] or 1)
    # Same run seen twice must not inflate the counter: idempotent reruns are
    # explicitly allowed (PUB-5), and "third day" would quietly become fourth.
    if state["last_seen_run"] != run_id:
        days += 1

    if state["resolved_at"]:
        return DeltaOutcome(
            cluster_id=cluster.cluster_id,
            status=DeltaStatus.RESOLVED,
            days_tracked=days,
            note="история закрыта",
            first_seen_run=state["first_seen_run"],
        )

    if fresh:
        return DeltaOutcome(
            cluster_id=cluster.cluster_id,
            status=DeltaStatus.UPDATED,
            days_tracked=days,
            note=_describe_new_facts(fresh),
            new_facts=fresh,
            first_seen_run=state["first_seen_run"],
        )

    return DeltaOutcome(
        cluster_id=cluster.cluster_id,
        status=DeltaStatus.CONTINUING,
        days_tracked=days,
        first_seen_run=state["first_seen_run"],
    )


def resolve_expired(conn: sqlite3.Connection, as_of: date | None = None) -> list[str]:
    """Close stories whose announced date has arrived.

    A sunset date that has passed is no longer a warning, and leaving it open
    would keep the digest carrying weight it should have put down.
    """
    as_of = as_of or datetime.now(UTC).date()
    closed: list[str] = []
    rows = conn.execute(
        "SELECT cluster_id, facts_json FROM clusters WHERE resolved_at IS NULL"
    ).fetchall()
    for row in rows:
        for raw in json.loads(row["facts_json"]):
            fact = Fact.model_validate(raw)
            if str(fact.kind) not in {"sunset_date", "effective_date"}:
                continue
            try:
                when = date.fromisoformat(fact.value.strip()[:10])
            except ValueError:
                continue
            if when <= as_of:
                closed.append(row["cluster_id"])
                break
    if closed:
        with conn:
            conn.executemany(
                "UPDATE clusters SET resolved_at = ? WHERE cluster_id = ?",
                [(as_of.isoformat(), cid) for cid in closed],
            )
    return closed


def save_state(
    conn: sqlite3.Connection,
    cluster: Cluster,
    facts: list[Fact],
    outcome: DeltaOutcome,
    run_id: str,
    as_of: date | None = None,
) -> None:
    """Persist the cluster so tomorrow's run can compare against it (FR-5.5)."""
    as_of = as_of or datetime.now(UTC).date()
    existing = _load_state(conn, cluster.cluster_id)
    merged = json.loads(existing["facts_json"]) if existing else []
    known = {_fact_key(Fact.model_validate(f)) for f in merged}
    merged.extend(f.model_dump(mode="json") for f in facts if _fact_key(f) not in known)

    with conn:
        conn.execute(
            "INSERT INTO clusters (cluster_id, dedup_key, title, primary_url, vendor, "
            "change_type, duplicates_count, first_seen_run, last_seen_run, days_tracked, "
            "delta_status, delta_note, facts_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cluster_id) DO UPDATE SET title = excluded.title, "
            "duplicates_count = excluded.duplicates_count, "
            "last_seen_run = excluded.last_seen_run, days_tracked = excluded.days_tracked, "
            "delta_status = excluded.delta_status, delta_note = excluded.delta_note, "
            "facts_json = excluded.facts_json, updated_at = excluded.updated_at",
            (
                cluster.cluster_id,
                cluster.dedup_key,
                cluster.title,
                cluster.primary.url,
                cluster.vendor,
                cluster.change_type,
                cluster.duplicates_count,
                outcome.first_seen_run or run_id,
                run_id,
                outcome.days_tracked,
                str(outcome.status),
                outcome.note,
                json.dumps(merged, ensure_ascii=False),
                datetime.now(UTC).isoformat(),
            ),
        )


def prune_state(conn: sqlite3.Connection, as_of: date | None = None) -> int:
    """Drop clusters past the horizon.

    Safe because the operational layer is derived: FR-5.19 makes it
    rebuildable from the corpus, which is append-only.

    `raw_items` is not swept with them, and must not be. It is not a cache of
    recent stories but the record of what the system has ever laid eyes on;
    forgetting a row after thirty days means announcing the same retirement
    table again in the thirty-first morning. It costs about a hundred rows a
    day.
    """
    as_of = as_of or datetime.now(UTC).date()
    cutoff = (as_of - timedelta(days=STATE_HORIZON_DAYS)).isoformat()
    with conn:
        cursor = conn.execute("DELETE FROM clusters WHERE updated_at < ?", (cutoff,))
    return cursor.rowcount
