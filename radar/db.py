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
    -- Idempotency key for backfill (FR-6.7): canonical URL plus the position
    -- of the event inside the material.
    UNIQUE (source_url, statement_index)
);
CREATE INDEX IF NOT EXISTS idx_es_filter ON event_statements(vendor, change_type, event_date);
CREATE INDEX IF NOT EXISTS idx_es_date ON event_statements(event_date);
-- Empty keys are exempt: a statement whose subject could not be named is not
-- claimed to be unique, and refusing to store it would lose the event.
CREATE UNIQUE INDEX IF NOT EXISTS idx_es_event
    ON event_statements(event_key) WHERE event_key <> '';

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
    PRIMARY KEY (run_id, url, stage)
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


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(DDL)
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
