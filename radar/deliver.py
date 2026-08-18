"""Delivery: reads signals, hands them to surfaces, records the outcome.

This layer exists because neither side may do the job alone. The core ends at
the store (PUB-1) and knows nothing about channels. A surface may not import
the journal — it reads signals and draws, and the static tests enforce that.
So the party that calls the surface is the one that records what happened.

Without it `DELIVERY_SENT` was never emitted by anything but a test, and the
supervisor — the instrument built against the one failure this product cannot
afford, a daily agent gone quiet — reported every healthy run as undelivered.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from radar.db import read_signals
from radar.journal import EventKind, Journal, Outcome
from radar.models import Signal
from radar.runlog import RunLog


class Surface(Protocol):
    """What delivery needs from a channel, and nothing more."""

    name: str

    def send_digest(self, signals: list[Signal]) -> Any: ...


@dataclass(slots=True)
class ChannelResult:
    channel: str
    delivered: bool
    message_id: str | None = None
    error: str | None = None


@dataclass(slots=True)
class DeliveryReport:
    run_id: str
    signals: int = 0
    results: list[ChannelResult] = field(default_factory=list)

    @property
    def any_delivered(self) -> bool:
        return any(r.delivered for r in self.results)

    @property
    def all_delivered(self) -> bool:
        return bool(self.results) and all(r.delivered for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "signals": self.signals,
            "channels": [
                {"channel": r.channel, "delivered": r.delivered, "error": r.error}
                for r in self.results
            ],
        }


def _outcome_of(result: Any, channel: str) -> ChannelResult:
    """Surfaces return their own result type; normalize without importing them."""
    if result is None:
        return ChannelResult(
            channel, delivered=False, error="поверхность ничего не вернула"
        )
    delivered = bool(getattr(result, "delivered", getattr(result, "ok", False)))
    return ChannelResult(
        channel=channel,
        delivered=delivered,
        message_id=str(getattr(result, "message_id", "") or "") or None,
        error=getattr(result, "error", None),
    )


def deliver(
    conn: sqlite3.Connection,
    surfaces: dict[str, Surface],
    run_id: str | None = None,
    journal: Journal | None = None,
    run_log: RunLog | None = None,
) -> DeliveryReport:
    """Send one run to every configured channel.

    A channel that fails does not stop the others (SUR-5), and every attempt
    is recorded either way. A quiet day is delivered like anything else: the
    record exists, so silence reaches the reader as a message (PUB-4).
    """
    signals = read_signals(conn, run_id)
    resolved_run = run_id or (signals[0].run_id if signals else "")
    report = DeliveryReport(run_id=resolved_run, signals=len(signals))

    if not signals:
        # Nothing to send is not the same as a quiet day: a quiet day is a
        # record. This is an empty store, and saying so beats sending nothing.
        if journal is not None:
            journal.record(
                EventKind.DELIVERY_FAILED,
                actor="deliver",
                target=resolved_run,
                outcome=Outcome.SKIPPED,
                reason="в хранилище нет сигналов этого прогона",
            )
        return report

    for channel, surface in surfaces.items():
        started = datetime.now(UTC)
        try:
            raw = surface.send_digest(signals)
            result = _outcome_of(raw, channel)
        except (AttributeError, TypeError, NameError, ImportError) as exc:
            # `TelegramSurface` did not exist and this line reported it as
            # "канал недоступен" for as long as nobody looked. It is re-raised,
            # but only after the attempt is recorded: a channel that vanishes
            # without a line leaves the supervisor saying NEVER_DELIVERED with
            # no reason attached.
            result = ChannelResult(
                channel, delivered=False, error=f"{type(exc).__name__}: {exc}"
            )
            _record(report, result, run_log, journal, channel, started, resolved_run,
                    len(signals))
            raise
        except Exception as exc:
            result = ChannelResult(
                channel, delivered=False, error=f"{type(exc).__name__}: {exc}"
            )

        _record(report, result, run_log, journal, channel, started, resolved_run,
                len(signals))
    return report


def _record(
    report: DeliveryReport,
    result: ChannelResult,
    run_log: RunLog | None,
    journal: Journal | None,
    channel: str,
    started: datetime,
    run_id: str,
    signals: int,
) -> None:
    """Write down one attempt, whatever its outcome."""
    report.results.append(result)
    duration = int((datetime.now(UTC) - started).total_seconds() * 1000)
    if run_log is not None:
        run_log.delivered(
            channel=channel,
            status="ok" if result.delivered else "failed",
            message_id=result.message_id,
            error=result.error,
        )
    if journal is not None:
        journal.record(
            EventKind.DELIVERY_SENT if result.delivered else EventKind.DELIVERY_FAILED,
            actor="deliver",
            target=channel,
            outcome=Outcome.OK if result.delivered else Outcome.FAILED,
            duration_ms=duration,
            run_id_delivered=run_id,
            signals=signals,
            error=result.error,
        )
