"""Append-only journal of everything the system does.

The run log answers "what happened in this run" for a human. The journal
answers "what state is the system in" for a machine: an ordered, append-only
event stream plus stage checkpoints, so a supervisor can tell a run that
finished from a run that died, and resume the second one without repeating
paid work.

Two sinks on purpose. JSONL on disk survives a corrupted database and can be
tailed while a run is going; the table makes the same events queryable. The
file is the source of truth if the two ever disagree, because it is only ever
appended to.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class EventKind(StrEnum):
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
    RUN_RESUMED = "run.resumed"
    STAGE_STARTED = "stage.started"
    STAGE_FINISHED = "stage.finished"
    STAGE_FAILED = "stage.failed"
    STAGE_SKIPPED = "stage.skipped"
    CHECKPOINT_SAVED = "checkpoint.saved"
    SOURCE_FETCHED = "source.fetched"
    SOURCE_EMPTY = "source.empty"
    SOURCE_FAILED = "source.failed"
    MODEL_CALLED = "model.called"
    ITEM_FILTERED = "item.filtered"
    FACT_REJECTED = "fact.rejected"
    LABEL_DOWNGRADED = "label.downgraded"
    SIGNAL_PUBLISHED = "signal.published"
    DELIVERY_SENT = "delivery.sent"
    DELIVERY_FAILED = "delivery.failed"
    BUDGET_EXCEEDED = "budget.exceeded"


class Outcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    PARTIAL = "partial"


JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS journal (
    event_id    TEXT PRIMARY KEY,
    run_id      TEXT,
    seq         INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    actor       TEXT NOT NULL,
    target      TEXT,
    outcome     TEXT NOT NULL,
    duration_ms INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_journal_run ON journal(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_journal_kind ON journal(kind, ts);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id       TEXT NOT NULL,
    stage        TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    item_count   INTEGER NOT NULL DEFAULT 0,
    state_json   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, stage)
);
"""


@dataclass(slots=True)
class Event:
    event_id: str
    run_id: str | None
    seq: int
    ts: datetime
    kind: EventKind
    actor: str
    target: str | None
    outcome: Outcome
    duration_ms: int | None
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "run_id": self.run_id,
                "seq": self.seq,
                "ts": self.ts.isoformat(),
                "kind": str(self.kind),
                "actor": self.actor,
                "target": self.target,
                "outcome": str(self.outcome),
                "duration_ms": self.duration_ms,
                "payload": self.payload,
            },
            ensure_ascii=False,
            default=str,
        )


class Journal:
    def __init__(
        self,
        conn: sqlite3.Connection,
        log_dir: str | Path = "logs",
        run_id: str | None = None,
    ) -> None:
        self.conn = conn
        self.run_id = run_id
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = self._last_seq()
        self.conn.executescript(JOURNAL_DDL)
        self.conn.commit()

    def _last_seq(self) -> int:
        try:
            row = self.conn.execute(
                "SELECT MAX(seq) AS n FROM journal WHERE run_id IS ?", (self.run_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row["n"] or 0) if row else 0

    @property
    def path(self) -> Path:
        return self.log_dir / f"journal-{datetime.now(UTC):%Y-%m-%d}.jsonl"

    def record(
        self,
        kind: EventKind,
        actor: str,
        target: str | None = None,
        outcome: Outcome = Outcome.OK,
        duration_ms: int | None = None,
        **payload: Any,
    ) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(
                event_id=uuid.uuid4().hex,
                run_id=self.run_id,
                seq=self._seq,
                ts=datetime.now(UTC),
                kind=kind,
                actor=actor,
                target=target,
                outcome=outcome,
                duration_ms=duration_ms,
                payload=payload,
            )
            # File first: if the process dies between the two writes, the
            # durable append is the one that survives.
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.to_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            with self.conn:
                self.conn.execute(
                    "INSERT INTO journal (event_id, run_id, seq, ts, kind, actor, target, "
                    "outcome, duration_ms, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.run_id,
                        event.seq,
                        event.ts.isoformat(),
                        str(event.kind),
                        event.actor,
                        event.target,
                        str(event.outcome),
                        event.duration_ms,
                        json.dumps(event.payload, ensure_ascii=False, default=str),
                    ),
                )
            return event

    @contextmanager
    def action(
        self,
        kind_started: EventKind,
        kind_finished: EventKind,
        actor: str,
        target: str | None = None,
        **payload: Any,
    ):
        """Bracket an action so a crash leaves a `failed` event, not silence."""
        started = datetime.now(UTC)
        self.record(kind_started, actor, target, **payload)
        result: dict[str, Any] = {}
        try:
            yield result
        except BaseException as exc:
            self.record(
                EventKind.STAGE_FAILED
                if "stage" in str(kind_started)
                else kind_finished,
                actor,
                target,
                outcome=Outcome.FAILED,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                error=f"{type(exc).__name__}: {exc}",
                **result,
            )
            raise
        else:
            self.record(
                kind_finished,
                actor,
                target,
                outcome=Outcome.OK,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                **result,
            )

    def checkpoint(self, stage: str, item_count: int = 0, **state: Any) -> None:
        """Mark a stage complete so a resumed run can skip it."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO checkpoints (run_id, stage, completed_at, item_count, state_json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id, stage) DO UPDATE SET "
                "completed_at = excluded.completed_at, item_count = excluded.item_count, "
                "state_json = excluded.state_json",
                (
                    self.run_id,
                    stage,
                    datetime.now(UTC).isoformat(),
                    item_count,
                    json.dumps(state, ensure_ascii=False, default=str),
                ),
            )
        self.record(
            EventKind.CHECKPOINT_SAVED,
            actor=stage,
            target=self.run_id,
            item_count=item_count,
        )

    def completed_stages(self, run_id: str | None = None) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT stage, completed_at, item_count, state_json FROM checkpoints "
            "WHERE run_id IS ?",
            (run_id or self.run_id,),
        ).fetchall()
        return {
            r["stage"]: {
                "completed_at": r["completed_at"],
                "item_count": r["item_count"],
                "state": json.loads(r["state_json"]),
            }
            for r in rows
        }

    def events(
        self, run_id: str | None = None, kind: EventKind | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM journal WHERE 1=1"
        params: list[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if kind is not None:
            query += " AND kind = ?"
            params.append(str(kind))
        query += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]

    @staticmethod
    def read_file(path: str | Path) -> list[dict[str, Any]]:
        """Replay from disk. Truncated trailing lines are skipped, not fatal."""
        events: list[dict[str, Any]] = []
        file = Path(path)
        if not file.exists():
            return events
        for line in file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
