"""The signal feed as a file: the surface a native client reads.

MAC-3 leaves the transport to the core, and this is the choice: one JSON
document beside the three HTML pages, written by the same build, holding the
signals of a run verbatim as `Signal.model_dump` produces them.

Verbatim matters. A feed that reshapes the contract into something convenient
for one client becomes a second contract to keep in step, and the first time
the two disagree the client shows a field the core stopped emitting. Here the
document adds an envelope — which run, which day, when it was written — and
touches nothing inside a signal.

The client is forbidden to read the store (SUR-1) and forbidden to judge
(SUR-2). Both hold trivially when all it can see is this file: there is no
ranking to redo, because `rank` arrived decided, and no corpus to query.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from radar.models import Signal

FEED_VERSION = 1


def build_feed(
    signals: Sequence[Signal],
    *,
    for_date: date,
    run_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The feed document for one run.

    An empty run is still a document. A client that finds no file cannot tell
    "nothing happened today" from "the pipeline never ran", and MAC-8 turns on
    exactly that distinction: the first is a quiet day, the second is staleness
    to be shown as such.
    """
    moment = now or datetime.now(timezone.utc)
    target = run_id or (signals[0].run_id if signals else None)
    return {
        "feed_version": FEED_VERSION,
        "generated_at": moment.astimezone(timezone.utc).isoformat(),
        "run_id": target,
        "for_date": for_date.isoformat(),
        "signal_count": len(signals),
        "signals": [s.model_dump(mode="json") for s in signals],
    }


def render_feed(
    signals: Sequence[Signal],
    *,
    for_date: date,
    run_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """The document as text, one signal per line group, stable key order."""
    document = build_feed(signals, for_date=for_date, run_id=run_id, now=now)
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def write_feed(
    signals: Sequence[Signal],
    path: str | Path,
    *,
    for_date: date,
    run_id: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Write the feed atomically.

    A client watching the file for changes will read it the moment it appears,
    and a half-written document is a parse error on stage. Rename is atomic on
    the same filesystem, so the watcher never sees a partial file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_feed(signals, for_date=for_date, run_id=run_id, now=now)
    staging = target.with_suffix(target.suffix + ".part")
    staging.write_text(text, encoding="utf-8")
    staging.replace(target)
    return target
