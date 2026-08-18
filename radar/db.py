"""Storage. One SQLite file holds all three layers (PRD 5.8).

State, corpus and signals live in one file because that resolves three things
at once: PUB-6 atomicity and PUB-5 idempotency become a single transaction,
FR-5.20 transparency is served by `dump`, and open question 10 (how a surface
reaches the signals) answers itself — a surface opens the same file read-only.

Layer boundaries are enforced by access mode, not by convention: surfaces get
a connection opened with mode=ro and can never write.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from radar.models import Signal

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Operational layer (State): 30 days, rewritten, read by pass A.
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id       TEXT PRIMARY KEY,
    dedup_key        TEXT NOT NULL,
    title            TEXT NOT NULL,
    primary_url      TEXT,
    vendor           TEXT,
    change_type      TEXT,
    duplicates_count INTEGER NOT NULL DEFAULT 0,
    first_seen_run   TEXT,
    last_seen_run    TEXT,
    days_tracked     INTEGER NOT NULL DEFAULT 1,
    delta_status     TEXT,
    delta_note       TEXT,
    facts_json       TEXT NOT NULL DEFAULT '[]',
    resolved_at      TEXT,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clusters_dedup ON clusters(dedup_key);
CREATE INDEX IF NOT EXISTS idx_clusters_seen ON clusters(last_seen_run);

CREATE TABLE IF NOT EXISTS raw_items (
    id           TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    raw_ref      TEXT,
    seen_in_json TEXT NOT NULL DEFAULT '[]',
    cluster_id   TEXT REFERENCES clusters(cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_items_cluster ON raw_items(cluster_id);

-- Corpus: append-only, unbounded horizon, read by pass B.
CREATE TABLE IF NOT EXISTS event_statements (
    statement_id     TEXT PRIMARY KEY,
    cluster_id       TEXT,
    text             TEXT NOT NULL,
    vendor           TEXT NOT NULL,
    product          TEXT,
    change_type      TEXT NOT NULL,
    event_date       TEXT,
    date_precision   TEXT NOT NULL DEFAULT 'day',
    version          TEXT,
    source_url       TEXT NOT NULL,
    statement_index  INTEGER NOT NULL DEFAULT 0,
    evidence         TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    ingest_mode      TEXT NOT NULL,
    extractor_model  TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    raw_material_ref TEXT NOT NULL,
    embedding        BLOB,
    supersedes       TEXT REFERENCES event_statements(statement_id),
    -- What makes two records the same event: vendor, kind, date and the named
    -- subject. Distinct from the key below, which is about where a record came
    -- from. A page that prints one retirement in two tables produced two
    -- records with different origins and one identity, and the context label
    -- counted them as two precedents.
    event_key        TEXT NOT NULL DEFAULT '',
    -- Which rule produced `event_key`. A corpus outlives the function that
    -- keyed it, and a key from a replaced definition is worse than no key: the
    -- unique index keeps enforcing a consistency nobody computes any more.
    identity_version TEXT NOT NULL DEFAULT '',
    -- Idempotency key for backfill (FR-6.7): canonical URL plus the position
    -- of the event inside the material.
    UNIQUE (source_url, statement_index)
);
CREATE INDEX IF NOT EXISTS idx_es_filter ON event_statements(vendor, change_type, event_date);
CREATE INDEX IF NOT EXISTS idx_es_date ON event_statements(event_date);

CREATE VIRTUAL TABLE IF NOT EXISTS event_statements_fts USING fts5(
    text,
    content='event_statements',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS event_statements_ai AFTER INSERT ON event_statements BEGIN
    INSERT INTO event_statements_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS event_statements_ad AFTER DELETE ON event_statements BEGIN
    INSERT INTO event_statements_fts(event_statements_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS event_statements_au AFTER UPDATE ON event_statements BEGIN
    INSERT INTO event_statements_fts(event_statements_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
    INSERT INTO event_statements_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS trends (
    trend_id            TEXT PRIMARY KEY,
    label               TEXT NOT NULL,
    vendor              TEXT,
    change_types_json   TEXT NOT NULL DEFAULT '[]',
    member_ids_json     TEXT NOT NULL DEFAULT '[]',
    first_observed      TEXT,
    last_observed       TEXT,
    cadence_days        REAL,
    trajectory          TEXT NOT NULL DEFAULT 'emerging',
    evidence_refs_json  TEXT NOT NULL DEFAULT '[]',
    updated_at          TEXT NOT NULL
);

-- Signals: the only table surfaces are allowed to read.
CREATE TABLE IF NOT EXISTS signals (
    signal_id      TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    signal_type    TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    for_date       TEXT NOT NULL,
    vendor         TEXT,
    change_type    TEXT,
    score          INTEGER NOT NULL DEFAULT 0,
    rank           INTEGER NOT NULL DEFAULT 0,
    tier           TEXT NOT NULL DEFAULT 'standard',
    payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_run ON signals(run_id);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(for_date, rank);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    for_date     TEXT NOT NULL,
    cost_usd     REAL NOT NULL DEFAULT 0,
    model_calls  INTEGER NOT NULL DEFAULT 0,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    log_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_runs (
    run_id      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    items_count INTEGER NOT NULL DEFAULT 0,
    latency_ms  INTEGER,
    error       TEXT,
    PRIMARY KEY (run_id, source_id)
);

CREATE TABLE IF NOT EXISTS filtered_items (
    run_id      TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason_note TEXT,
    stage       TEXT NOT NULL,
    -- The material, not its address. A deprecation table hands over ten
    -- sections under one anchor, and keying by URL made nine of the ten
    -- rejections overwrite each other: the funnel said "10 dropped" and the
    -- page could account for one. FR-3.3 asks that nothing dropped disappear.
    item_key    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, item_key, stage)
);

CREATE TABLE IF NOT EXISTS model_calls (
    call_id     TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    model       TEXT NOT NULL,
    provider    TEXT,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    cached      INTEGER NOT NULL DEFAULT 0,
    -- What the call would have cost had the cache missed. Computed in llm.py
    -- since the cache was written and never stored anywhere, so a run served
    -- entirely from cache reported its price as the price of the work — which
    -- is how "1:19 and $0.16" went into an acceptance report for a run whose
    -- forty-two enrichment calls were all hits.
    original_cost_usd REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_calls_run ON model_calls(run_id);
"""


def _adapt(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def connect(path: str | Path, read_only: bool = False) -> sqlite3.Connection:
    """Open the store. Surfaces must pass read_only=True (FR-5.22)."""
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


# Empty keys are exempt: a statement whose subject could not be named is not
# claimed to be unique, and refusing to store it would lose the event.
EVENT_KEY_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_es_event "
    "ON event_statements(event_key) WHERE event_key <> ''"
)


def migrate_event_key(conn: sqlite3.Connection) -> str | None:
    """Bring a corpus written before event identity existed up to date.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists,
    so a database from yesterday has no `event_key` column and the unique index
    over it cannot be created. Adding the column is safe; creating the index is
    not, because the corpus it is meant to protect may already hold the
    duplicates it forbids. In that case the index is skipped and the reason
    returned, rather than taking down every command that opens the database.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(event_statements)")}
    if not columns:
        return None
    if "event_key" not in columns:
        conn.execute(
            "ALTER TABLE event_statements ADD COLUMN event_key TEXT NOT NULL DEFAULT ''"
        )
    try:
        conn.execute(EVENT_KEY_INDEX)
    except sqlite3.IntegrityError:
        rows = conn.execute(
            "SELECT count(*) FROM (SELECT event_key FROM event_statements "
            "WHERE event_key <> '' GROUP BY event_key HAVING count(*) > 1)"
        ).fetchone()[0]
        return (
            f"корпус содержит {rows} событий, записанных больше одного раза; "
            "индекс уникальности не создан — пересоберите корпус"
        )
    return None


def migrate_filtered_key(conn: sqlite3.Connection) -> None:
    """Rekey filtered_items from the URL to the material.

    SQLite cannot alter a primary key, so the table is rebuilt. Old rows keep
    their URL as the key, which is what they were stored under anyway.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(filtered_items)")}
    if not columns or "item_key" in columns:
        return
    conn.executescript(
        "ALTER TABLE filtered_items RENAME TO filtered_items_old;\n"
        "CREATE TABLE filtered_items (\n"
        "    run_id      TEXT NOT NULL,\n"
        "    url         TEXT NOT NULL,\n"
        "    title       TEXT NOT NULL,\n"
        "    reason_code TEXT NOT NULL,\n"
        "    reason_note TEXT,\n"
        "    stage       TEXT NOT NULL,\n"
        "    item_key    TEXT NOT NULL DEFAULT '',\n"
        "    PRIMARY KEY (run_id, item_key, stage)\n"
        ");\n"
        "INSERT OR IGNORE INTO filtered_items "
        "(run_id, url, title, reason_code, reason_note, stage, item_key) "
        "SELECT run_id, url, title, reason_code, reason_note, stage, url "
        "FROM filtered_items_old;\n"
        "DROP TABLE filtered_items_old;\n"
    )


def migrate_original_cost(conn: sqlite3.Connection) -> None:
    """Add the would-have-cost column to a table that predates it."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(model_calls)")}
    if columns and "original_cost_usd" not in columns:
        conn.execute(
            "ALTER TABLE model_calls "
            "ADD COLUMN original_cost_usd REAL NOT NULL DEFAULT 0"
        )


def migrate_identity_version(conn: sqlite3.Connection) -> int:
    """Re-key records written under an older definition of event identity.

    Adding a column is not enough here. When the rule changes, the rows keyed
    by the previous rule stay behind and quietly mean something else — after
    three edits to the subject extractor, sixty-one records held keys the
    current function would never produce, one of them identified by a month.
    Returns how many rows were re-keyed.
    """
    from radar.normalize import IDENTITY_VERSION, event_identity

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(event_statements)")}
    if not columns:
        return 0
    if "identity_version" not in columns:
        conn.execute(
            "ALTER TABLE event_statements "
            "ADD COLUMN identity_version TEXT NOT NULL DEFAULT ''"
        )
    stale = conn.execute(
        "SELECT statement_id, vendor, change_type, event_date, product, evidence, "
        "text FROM event_statements WHERE identity_version <> ?",
        (IDENTITY_VERSION,),
    ).fetchall()
    if not stale:
        return 0
    rekeyed = 0
    with conn:
        for row in stale:
            key = event_identity(
                row["vendor"], row["change_type"], row["event_date"],
                row["product"], row["evidence"], row["text"],
            )
            try:
                conn.execute(
                    "UPDATE event_statements SET event_key = ?, identity_version = ? "
                    "WHERE statement_id = ?",
                    (key, IDENTITY_VERSION, row["statement_id"]),
                )
            except sqlite3.IntegrityError:
                # The new rule says this row is the same event as another. The
                # corpus is append-only, so the row stays; it loses its key and
                # stops being counted twice.
                conn.execute(
                    "UPDATE event_statements SET event_key = '', identity_version = ? "
                    "WHERE statement_id = ?",
                    (IDENTITY_VERSION, row["statement_id"]),
                )
            rekeyed += 1
    return rekeyed


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(DDL)
    migrate_event_key(conn)
    migrate_filtered_key(conn)
    migrate_original_cost(conn)
    migrate_identity_version(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def publish_signals(
    conn: sqlite3.Connection, run_id: str, signals: Iterable[Signal]
) -> int:
    """Write a whole run atomically (PUB-6), replacing any prior attempt (PUB-5).

    A surface reading concurrently sees either the previous run or this one in
    full, never a partially written set.
    """
    rows = [
        (
            s.signal_id,
            s.run_id,
            s.schema_version,
            str(s.signal_type),
            _adapt(s.created_at),
            _adapt(s.for_date),
            s.vendor,
            str(s.change_type) if s.change_type else None,
            s.score,
            s.rank,
            str(s.tier),
            s.model_dump_json(),
        )
        for s in signals
    ]
    with conn:  # single transaction
        conn.execute("DELETE FROM signals WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO signals (signal_id, run_id, schema_version, signal_type, "
            "created_at, for_date, vendor, change_type, score, rank, tier, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def read_signals(conn: sqlite3.Connection, run_id: str | None = None) -> list[Signal]:
    """The whole read contract for a surface: latest run, or a named one."""
    if run_id is None:
        # rowid breaks the tie: two runs can share a timestamp, and "latest"
        # then has to mean the one written last.
        row = conn.execute(
            "SELECT run_id FROM signals ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return []
        run_id = row["run_id"]
    rows = conn.execute(
        "SELECT payload_json FROM signals WHERE run_id = ? ORDER BY rank ASC", (run_id,)
    ).fetchall()
    return [Signal.model_validate_json(r["payload_json"]) for r in rows]


def dump_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """Human-readable snapshot of every layer.

    FR-5.20 prefers files over a database for demo transparency. A database is
    the right call for atomicity, so transparency is served here instead.
    """
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for table in ("clusters", "event_statements", "trends", "signals", "runs"):
        out[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    out["corpus_depth"] = dict(
        conn.execute(
            "SELECT MIN(event_date) AS earliest, MAX(event_date) AS latest "
            "FROM event_statements WHERE event_date IS NOT NULL"
        ).fetchone()
    )
    out["cells"] = [
        dict(r)
        for r in conn.execute(
            "SELECT vendor, change_type, COUNT(*) AS n FROM event_statements "
            "GROUP BY vendor, change_type ORDER BY n DESC"
        ).fetchall()
    ]
    return out


def corpus_readiness(
    conn: sqlite3.Connection, config: dict[str, Any]
) -> dict[str, Any]:
    """Answer the one question that decides whether DR-8 exists in the data.

    Absolute corpus size does not predict it: retrieval filters are
    conjunctive, so what matters is density inside a (vendor, change_type) cell.
    """
    readiness = config.get("corpus", {}).get("readiness", {})
    dense_types = readiness.get(
        "dense_cell_change_types",
        ["deprecation", "breaking_change", "pricing", "limits"],
    )
    min_events = int(readiness.get("min_events_per_dense_cell", 3))
    min_vendors = int(readiness.get("min_vendors_with_dense_cell", 3))

    placeholders = ",".join("?" * len(dense_types))
    cells = [
        dict(r)
        for r in conn.execute(
            f"SELECT vendor, change_type, COUNT(*) AS n FROM event_statements "
            f"WHERE change_type IN ({placeholders}) "
            f"GROUP BY vendor, change_type HAVING n >= ? ORDER BY n DESC",
            (*dense_types, min_events),
        ).fetchall()
    ]
    vendors = sorted({c["vendor"] for c in cells})
    total = conn.execute("SELECT COUNT(*) AS n FROM event_statements").fetchone()["n"]
    return {
        "total_statements": total,
        "dense_cells": cells,
        "vendors_with_dense_cell": vendors,
        "ready_for_trend_demo": len(vendors) >= min_vendors,
        "required_vendors": min_vendors,
        "required_events_per_cell": min_events,
    }


def json_default(value: Any) -> str:
    return _adapt(value) if not isinstance(value, (dict, list)) else json.dumps(value)
