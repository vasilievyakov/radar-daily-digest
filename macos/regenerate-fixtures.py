"""Rebuild the client's fixtures from the core.

Hand-written fixtures drift. These three lagged the contract by six fields and
invented a `stats` shape the core never emitted, and the Swift decoder was
checked against them rather than against anything real — which is how it came
to read a payload the pipeline had stopped producing.

Two of the three are built by the same functions the pipeline calls; the third
is the top-ranked signal of the latest run, copied verbatim. Neither can go
stale without this script failing or the shape changing under it.

    .venv/bin/python macos/regenerate-fixtures.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.db import connect, read_signals  # noqa: E402
from radar.models import RunSummary  # noqa: E402
from radar.publish import build_quiet_day, build_run_failure  # noqa: E402

HERE = Path(__file__).parent / "Sources" / "RadarChecks" / "fixtures"
DB = Path(__file__).resolve().parents[1] / "data" / "radar.db"
# Frozen so a regeneration produces a diff only when the shape changed.
MOMENT = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)


def write(name: str, payload: dict) -> None:
    path = HERE / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{path.name}: {len(payload)} полей")


def main() -> int:
    conn = connect(DB, read_only=True)
    try:
        signals = read_signals(conn, None)
        if not signals:
            print("в хранилище нет сигналов: фикстуры не пересобраны", file=sys.stderr)
            return 1

        # A real card, chosen for depth rather than for rank: the client has to
        # render facts, precedents and a context note, and a thin card proves
        # nothing about any of them.
        richest = max(
            (s for s in signals if s.signal_type == "digest_item"),
            key=lambda s: (len(s.precedents), len(s.facts)),
            default=signals[0],
        )
        write("digest_item", richest.model_dump(mode="json"))

        summary = RunSummary(
            sources_checked=77,
            sources_failed=["Mistral AI — блог"],
            sources_empty=[],
            materials_collected=107,
            materials_filtered=33,
            last_success_date=date(2026, 8, 17),
            cost_usd=0.1971,
        )
        quiet = build_quiet_day(
            conn,
            run_id="20260818T060000-fixture",
            for_date=date(2026, 8, 18),
            run_summary=summary,
            run_log_url="run-log.html",
            created_at=MOMENT,
        )
        write("quiet_day", quiet.model_dump(mode="json"))

        failure = build_run_failure(
            run_id="20260818T060000-fixture",
            for_date=date(2026, 8, 18),
            stage="enrich",
            reason="модель не ответила ни на один запрос: обогащение не выполнено",
            run_summary=summary,
            run_log_url="run-log.html",
            created_at=MOMENT,
        )
        write("run_failure", failure.model_dump(mode="json"))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
