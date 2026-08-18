"""Run log writer.

Opened at stage zero, not assembled at the end. A crash on stage four has to
leave the log of stages one through three behind (NFR-4), which is only true
if every stage writes as it goes.

The log is also the artifact users are pointed at (FR-8.5, S6), so it records
why a material was dropped, what a source answered, and what the run cost.

And whether it was asked at all. `source_runs.network` exists because the log
was, for a while, unable to tell a checked source from a replayed one: the
fetcher served the archive unconditionally, the latency column filled up with
the cost of parsing a megabyte of HTML, and a run in which no request left the
machine produced a table of green rows with plausible response times. A run log
that cannot show that is worse than no run log, because it is believed.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any, Protocol, runtime_checkable

from radar.models import SourceStatus


def ensure_source_runs_network(conn: sqlite3.Connection) -> None:
    """Add `source_runs.network` to a database that predates it.

    Idempotent, and run when a `RunLog` opens rather than from the schema, so
    an existing radar.db gains the column without a migration step anyone has
    to remember. `PRAGMA table_info` is addressed positionally: the connection
    may or may not carry a row factory.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(source_runs)")}
    if "network" in columns:
        return
    with conn:
        conn.execute("ALTER TABLE source_runs ADD COLUMN network TEXT")


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


@runtime_checkable
class RunLogLike(Protocol):
    """What a stage may assume about a run log.

    Declared so a stand-in can be checked structurally: `_CallLog` implemented
    three of nine methods, claimed to be a RunLog, and cost four materials on
    a live run. A protocol makes that a type error instead of an
    AttributeError halfway through a paid stage.
    """

    def model_call(self, *args: Any, **kwargs: Any) -> None: ...
    def note(self, message: str) -> None: ...
    def filtered(self, *args: Any, **kwargs: Any) -> None: ...
    def source_result(self, *args: Any, **kwargs: Any) -> None: ...
    def delivered(self, *args: Any, **kwargs: Any) -> None: ...


class RunLog:
    def __init__(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        for_date: date,
        started_at: datetime | None = None,
    ) -> None:
        self.conn = conn
        self.run_id = run_id
        self.for_date = for_date
        self.started_at = started_at or datetime.now(UTC)
        self.status = "running"
        self.stages: list[dict[str, Any]] = []
        self.cost_usd = 0.0
        self.original_cost_usd = 0.0
        self.model_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.notes: list[str] = []
        self.delivery: list[dict[str, Any]] = []
        self.finished_at: datetime | None = None
        ensure_source_runs_network(conn)
        with self.conn:
            self.conn.execute(
                # Restarting a run clears finished_at as well: otherwise the
                # row reads as running and finished at the same time.
                "INSERT INTO runs (run_id, started_at, status, for_date) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET started_at = excluded.started_at, "
                "status = excluded.status, finished_at = NULL",
                (
                    run_id,
                    self.started_at.isoformat(),
                    self.status,
                    for_date.isoformat(),
                ),
            )

    @contextmanager
    def stage(self, name: str, in_count: int = 0):
        """Record a stage even when it raises."""
        record: dict[str, Any] = {
            "stage": name,
            "in_count": in_count,
            "out_count": 0,
            "started_at": datetime.now(UTC).isoformat(),
            "errors": [],
        }
        self.stages.append(record)
        started = datetime.now(UTC)
        try:
            yield record
        except BaseException as exc:
            # BaseException, not Exception: a run killed by Ctrl-C would
            # otherwise pass through `finally` and flush with errors: [],
            # leaving a false record of a stage that completed cleanly.
            record["errors"].append(f"{type(exc).__name__}: {exc}")
            record["duration_ms"] = int(
                (datetime.now(UTC) - started).total_seconds() * 1000
            )
            self.flush()
            raise
        finally:
            record.setdefault(
                "duration_ms", int((datetime.now(UTC) - started).total_seconds() * 1000)
            )
            self.flush()

    def source_result(
        self,
        source_id: str,
        status: SourceStatus,
        items_count: int = 0,
        latency_ms: int | None = None,
        error: str | None = None,
        network: str | None = None,
    ) -> None:
        """One source's answer, including whether it was an answer at all.

        `network` is COALESCEd rather than overwritten. The row is upserted
        twice on a daily run — the collector writes what the source did, and
        the pipeline later downgrades an `ok` with nothing new to `quiet` — and
        only the first of those two writers knows what the network did. A plain
        `excluded.network` would blank the column on the second pass, which is
        the same information loss this column was added to end.
        """
        with self.conn:
            self.conn.execute(
                "INSERT INTO source_runs (run_id, source_id, status, items_count, latency_ms, "
                "error, network) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, source_id) DO UPDATE SET status = excluded.status, "
                "items_count = excluded.items_count, latency_ms = excluded.latency_ms, "
                "error = excluded.error, "
                "network = COALESCE(excluded.network, source_runs.network)",
                (
                    self.run_id,
                    source_id,
                    str(status),
                    items_count,
                    latency_ms,
                    error,
                    network or None,
                ),
            )

    def filtered(
        self,
        url: str,
        title: str,
        reason_code: str,
        stage: str,
        note: str | None = None,
        item_key: str | None = None,
    ) -> None:
        """Nothing dropped disappears: it stays visible with a reason (FR-3.3).

        Keyed by the material rather than by its address. One deprecation page
        hands over ten sections under a single anchor, so the URL identified
        nine of them as the same row: the funnel reported ten dropped and the
        page could name one.
        """
        key = item_key or url
        with self.conn:
            self.conn.execute(
                "INSERT INTO filtered_items "
                "(run_id, url, title, reason_code, reason_note, stage, item_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, item_key, stage) DO UPDATE SET "
                "title = excluded.title, reason_code = excluded.reason_code, "
                "reason_note = excluded.reason_note",
                (self.run_id, url, title, reason_code, note, stage, key),
            )

    def model_call(
        self,
        stage: str,
        model: str,
        provider: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        cached: bool = False,
        original_cost_usd: float = 0.0,
    ) -> None:
        self.model_calls += 1
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost_usd
        # What the work costs when nothing is cached. Kept apart from the money
        # actually spent so a run can say both, and a fast cheap run cannot be
        # mistaken for a fast cheap pipeline.
        self.original_cost_usd += original_cost_usd or cost_usd
        with self.conn:
            self.conn.execute(
                "INSERT INTO model_calls (call_id, run_id, stage, model, provider, "
                "tokens_in, tokens_out, cost_usd, cached, original_cost_usd, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    self.run_id,
                    stage,
                    model,
                    provider,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                    int(cached),
                    original_cost_usd or cost_usd,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def note(self, message: str) -> None:
        self.notes.append(message)

    def delivered(
        self,
        channel: str,
        status: str,
        message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.delivery.append(
            {
                "channel": channel,
                "status": status,
                "message_id": message_id,
                "error": error,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "for_date": self.for_date.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status,
            "stages": self.stages,
            "notes": self.notes,
            "delivery": self.delivery,
            "cost": {
                "model_calls": self.model_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "usd": round(self.cost_usd, 4),
            },
        }

    def flush(self) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET status = ?, cost_usd = ?, model_calls = ?, tokens_in = ?, "
                "tokens_out = ?, log_json = ? WHERE run_id = ?",
                (
                    self.status,
                    round(self.cost_usd, 6),
                    self.model_calls,
                    self.tokens_in,
                    self.tokens_out,
                    json.dumps(self.as_dict(), ensure_ascii=False),
                    self.run_id,
                ),
            )

    def finish(self, status: str = "ok") -> None:
        self.status = status
        self.finished_at = datetime.now(UTC)
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at = ? WHERE run_id = ?",
                (self.finished_at.isoformat(), self.run_id),
            )
        self.flush()


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Hard per-run ceiling (NFR-6, FR-6.8).

    Checked before a call rather than recorded after one: a limit that is only
    noticed in the log has not limited anything.
    """

    def __init__(self, limit_usd: float) -> None:
        self.limit_usd = limit_usd
        self.spent_usd = 0.0

    def check(self, estimated_usd: float = 0.0) -> None:
        if self.spent_usd + estimated_usd > self.limit_usd:
            raise BudgetExceeded(
                f"budget {self.limit_usd:.2f} USD exhausted: spent {self.spent_usd:.4f}, "
                f"next call ~{estimated_usd:.4f}"
            )

    def charge(self, usd: float) -> None:
        self.spent_usd += usd

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)
