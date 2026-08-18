"""Supervision and recovery.

Reads the journal and decides what to do about runs that did not end cleanly.
A run that died on stage four is not the same as a run that never started, and
neither is the same as one that finished with failures. The distinction has to
be machine-readable, because the thing acting on it runs at 08:00 unattended.

Nothing here calls a model. Recovery decisions are rules over recorded state,
so they can be tested and explained.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from radar.journal import EventKind, Journal, Outcome

# Order matters: a resumed run restarts at the first stage without a checkpoint.
PIPELINE_STAGES = [
    "collect",
    "cluster",
    "filter",
    "enrich",
    "contextualize",
    "score",
    "publish",
    "deliver",
]

# Stages whose work is paid for and cached; repeating them is wasteful but safe.
PAID_STAGES = {"filter", "enrich", "contextualize"}


class RunState(StrEnum):
    HEALTHY = "healthy"
    RUNNING = "running"
    STALLED = "stalled"
    FAILED = "failed"
    NEVER_DELIVERED = "never_delivered"


class Action(StrEnum):
    NOTHING = "nothing"
    RESUME = "resume"
    RESTART = "restart"
    ALERT = "alert"


@dataclass(slots=True)
class RunDiagnosis:
    run_id: str
    state: RunState
    started_at: datetime
    finished_at: datetime | None
    completed_stages: list[str]
    next_stage: str | None
    failures: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    signals_written: int = 0
    delivered: bool = False
    recommended: Action = Action.NOTHING
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": str(self.state),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "completed_stages": self.completed_stages,
            "next_stage": self.next_stage,
            "failures": self.failures,
            "cost_usd": round(self.cost_usd, 4),
            "signals_written": self.signals_written,
            "delivered": self.delivered,
            "recommended": str(self.recommended),
            "reason": self.reason,
        }


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Supervisor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        journal: Journal,
        stall_after: timedelta = timedelta(minutes=30),
    ) -> None:
        self.conn = conn
        self.journal = journal
        # A run is stalled rather than running once it exceeds this. NFR-2
        # budgets ten minutes, so thirty is a generous ceiling that still
        # catches a hung process before the next scheduled run.
        self.stall_after = stall_after

    def diagnose(self, run_id: str, now: datetime | None = None) -> RunDiagnosis | None:
        now = now or datetime.now(UTC)
        row = self.conn.execute(
            "SELECT run_id, started_at, finished_at, status, cost_usd FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None

        started = _parse_ts(row["started_at"]) or now
        finished = _parse_ts(row["finished_at"])
        checkpoints = self.journal.completed_stages(run_id)
        completed = [s for s in PIPELINE_STAGES if s in checkpoints]
        next_stage = next((s for s in PIPELINE_STAGES if s not in checkpoints), None)

        failures = [
            f"{e['actor']}: {e['payload'].get('error', 'unknown')}"
            for e in self.journal.events(run_id=run_id)
            if e["outcome"] == str(Outcome.FAILED)
        ]
        signals = self.conn.execute(
            "SELECT COUNT(*) AS n FROM signals WHERE run_id = ?", (run_id,)
        ).fetchone()["n"]
        delivered = any(
            e["kind"] == str(EventKind.DELIVERY_SENT)
            for e in self.journal.events(run_id=run_id, kind=EventKind.DELIVERY_SENT)
        )

        diagnosis = RunDiagnosis(
            run_id=run_id,
            state=RunState.RUNNING,
            started_at=started,
            finished_at=finished,
            completed_stages=completed,
            next_stage=next_stage,
            failures=failures,
            cost_usd=float(row["cost_usd"] or 0.0),
            signals_written=signals,
            delivered=delivered,
        )
        self._classify(diagnosis, row["status"], now)
        return diagnosis

    def _classify(self, d: RunDiagnosis, status: str, now: datetime) -> None:
        age = now - d.started_at

        if status == "ok" and d.delivered:
            d.state = RunState.HEALTHY
            d.recommended = Action.NOTHING
            d.reason = "прогон завершён, доставка выполнена"
            return

        if status == "ok" and d.signals_written and not d.delivered:
            # The expensive part is done; only delivery is missing, and
            # re-running the pipeline would pay for extraction twice.
            d.state = RunState.NEVER_DELIVERED
            d.recommended = Action.RESUME
            d.reason = "сигналы записаны, доставка не подтверждена"
            return

        if status == "failed":
            d.state = RunState.FAILED
            d.recommended = Action.RESUME if d.completed_stages else Action.RESTART
            d.reason = (
                f"прогон упал на стадии {d.next_stage}"
                if d.next_stage
                else "прогон упал"
            )
            return

        if status == "running" and age > self.stall_after:
            d.state = RunState.STALLED
            d.recommended = Action.RESUME if d.completed_stages else Action.RESTART
            d.reason = (
                f"прогон висит {int(age.total_seconds() // 60)} минут без завершения"
            )
            return

        d.state = RunState.RUNNING
        d.recommended = Action.NOTHING
        d.reason = "прогон идёт"

    def close_stalled(self, now: datetime | None = None) -> list[str]:
        """Write the diagnosis down, not just report it.

        `diagnose` has always been able to see a hung run; nothing ever
        recorded the verdict, so eight runs sat at status `running` into a
        second day. Anything reading "the latest run" picks up a zombie and
        finds a perfectly consistent set of zeroes — a green report about a run
        that never happened is worse than a red one about a run that failed.
        """
        now = now or datetime.now(UTC)
        closed: list[str] = []
        rows = self.conn.execute(
            "SELECT run_id FROM runs WHERE status = 'running'"
        ).fetchall()
        for row in rows:
            diagnosis = self.diagnose(row["run_id"], now)
            if diagnosis is None or diagnosis.state is not RunState.STALLED:
                continue
            with self.conn:
                self.conn.execute(
                    "UPDATE runs SET status = 'stalled' WHERE run_id = ?",
                    (row["run_id"],),
                )
            self.journal.record(
                EventKind.RUN_FAILED,
                actor="supervisor",
                target=row["run_id"],
                outcome=Outcome.FAILED,
                reason=diagnosis.reason,
            )
            closed.append(row["run_id"])
        return closed

    def scan(self, now: datetime | None = None, limit: int = 20) -> list[RunDiagnosis]:
        """Every run that is not cleanly finished, newest first."""
        now = now or datetime.now(UTC)
        rows = self.conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        found = [self.diagnose(r["run_id"], now) for r in rows]
        return [d for d in found if d and d.state is not RunState.HEALTHY]

    def missed_days(
        self, expected_days: int = 7, now: datetime | None = None
    ) -> list[str]:
        """Dates with no delivered run.

        A daily agent that silently stopped running looks identical to a quiet
        day from the outside, and that is the one failure the product cannot
        afford: silence is a promised feature here.
        """
        now = now or datetime.now(UTC)
        rows = self.conn.execute(
            "SELECT DISTINCT for_date FROM runs WHERE status = 'ok'"
        ).fetchall()
        delivered = {r["for_date"] for r in rows}
        expected = {
            (now.date() - timedelta(days=offset)).isoformat()
            for offset in range(1, expected_days + 1)
        }
        return sorted(expected - delivered)

    def resume_plan(self, run_id: str) -> list[str]:
        """Stages still to run, in order."""
        checkpoints = self.journal.completed_stages(run_id)
        return [s for s in PIPELINE_STAGES if s not in checkpoints]

    def report(self, now: datetime | None = None) -> dict[str, Any]:
        """Everything a recovery agent needs, in one call."""
        now = now or datetime.now(UTC)
        problems = self.scan(now)
        return {
            "generated_at": now.isoformat(),
            "unhealthy_runs": [d.as_dict() for d in problems],
            "missed_days": self.missed_days(now=now),
            "actions": [
                {
                    "run_id": d.run_id,
                    "action": str(d.recommended),
                    "reason": d.reason,
                    "resume_from": d.next_stage,
                }
                for d in problems
                if d.recommended is not Action.NOTHING
            ],
        }
