"""The signals as a document: what the native client reads (MAC-3)."""

from __future__ import annotations

import ast
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from radar.models import (
    ChangeType,
    Fact,
    FactKind,
    RunSummary,
    Signal,
    SignalType,
    Tier,
)
from radar.surfaces import feed as feed_surface

TODAY = date(2026, 8, 18)
NOW = datetime(2026, 8, 18, 6, 0, tzinfo=UTC)


def a_signal(rank: int = 1, **kwargs) -> Signal:
    defaults = dict(
        signal_id=f"sig-{rank}",
        run_id="run-1",
        signal_type=SignalType.DIGEST_ITEM,
        created_at=NOW,
        for_date=TODAY,
        headline=f"Событие {rank}",
        summary="Что произошло.",
        change_type=ChangeType.DEPRECATION,
        vendor="Anthropic",
        rank=rank,
        tier=Tier.LEAD,
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                source_url="https://example.com/a",
                evidence="will be retired on October 15, 2026",
                confidence="high",
                evidence_verified=True,
            )
        ],
    )
    defaults.update(kwargs)
    return Signal(**defaults)


def test_every_signal_survives_the_round_trip():
    """Verbatim, field for field.

    A feed that reshapes the contract becomes a second contract, and the first
    time the two disagree the client renders a field the core stopped writing.
    """
    signals = [a_signal(1), a_signal(2, vendor="OpenAI")]
    document = feed_surface.build_feed(signals, for_date=TODAY, now=NOW)

    assert document["signal_count"] == 2
    restored = [Signal.model_validate(row) for row in document["signals"]]
    assert restored == signals


def test_the_envelope_names_the_run_and_the_day():
    document = feed_surface.build_feed([a_signal()], for_date=TODAY, now=NOW)
    assert document["run_id"] == "run-1"
    assert document["for_date"] == "2026-08-18"
    assert document["generated_at"].startswith("2026-08-18T06:00")
    assert document["feed_version"] == feed_surface.FEED_VERSION


def test_a_quiet_run_still_writes_a_document(tmp_path):
    """Absence of signals and absence of a run are different failures.

    A client that finds no file cannot tell "nothing happened today" from "the
    pipeline never ran", and MAC-8 turns on exactly that distinction.
    """
    path = feed_surface.write_feed([], tmp_path / "signals.json", for_date=TODAY, now=NOW)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["signals"] == []
    assert document["signal_count"] == 0
    assert document["for_date"] == "2026-08-18"


def test_the_write_is_atomic_and_leaves_nothing_behind(tmp_path):
    """A watcher must never read half a document."""
    target = tmp_path / "signals.json"
    feed_surface.write_feed([a_signal()], target, for_date=TODAY, now=NOW)
    assert target.exists()
    assert list(tmp_path.iterdir()) == [target]


def test_rewriting_replaces_rather_than_appends(tmp_path):
    target = tmp_path / "signals.json"
    feed_surface.write_feed([a_signal(1), a_signal(2)], target, for_date=TODAY, now=NOW)
    feed_surface.write_feed([a_signal(1)], target, for_date=TODAY, now=NOW)
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["signal_count"] == 1


def test_run_summary_reaches_the_document():
    """SUR-1: the client cannot read `source_runs`, so the names travel here."""
    summary = RunSummary(sources_checked=77, sources_failed=["Mistral AI — блог"])
    document = feed_surface.build_feed(
        [a_signal(run_summary=summary)], for_date=TODAY, now=NOW
    )
    written = document["signals"][0]["run_summary"]
    assert written["sources_checked"] == 77
    assert written["sources_failed"] == ["Mistral AI — блог"]


def test_the_document_is_readable_by_a_person():
    """Indented and unescaped: a demo opens this file on stage."""
    text = feed_surface.render_feed([a_signal()], for_date=TODAY, now=NOW)
    assert "Событие 1" in text  # not \\u0421\\u043e...
    assert text.endswith("\n")


# -- architecture ------------------------------------------------------


def test_the_feed_surface_imports_nothing_from_the_pipeline():
    """SUR-2 for the second surface, checked the same way as for the first."""
    source = Path("radar/surfaces/feed.py").read_text(encoding="utf-8")
    allowed = {"radar.models"}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "относительные импорты скрывают зависимость"
            names = [node.module or ""]
        else:
            continue
        for module in names:
            root = module.split(".")[0]
            if root == "radar":
                assert module in allowed, module
            else:
                assert root in sys.stdlib_module_names, module
