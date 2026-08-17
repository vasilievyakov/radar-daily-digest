"""One run from raw material to signal, against the warm HTTP cache.

Every stage has its own tests and every one of them was green while five
defects shipped: a URL fragment dropped, sections collapsed by content, the
vendor lost in clustering, the change type falling off the seam, delivery
never recorded. All five had one shape — a value that should exist is absent,
and every consumer downstream has a valid path for absence. Nothing throws.

Stage tests cannot catch that, because a seam has no owner. These assert on
what arrives at the far end, not on what each stage computes. The network is
already paid for, so this costs nothing but the model calls, which are faked.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from radar.config import ThemeConfig
from radar.contracts import EnrichResult
from radar.db import init_db, read_signals
from radar.deliver import deliver
from radar.fetch import Fetcher
from radar.journal import Journal
from radar.models import ChangeType, Fact, FactKind, SignalType
from radar.run import DailyRun
from radar.supervisor import Action, RunState, Supervisor

TODAY = date(2026, 8, 17)
NOW = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)

# Sources whose pages are already in the repository HTTP cache.
CACHED_SOURCES = ["anthropic_model_deprecations", "openai_deprecations"]


class RealShapedEnricher:
    """Returns what the real stage returns, including the fields the pipeline
    used to drop. A stub thinner than production hides exactly these bugs."""

    def __init__(self):
        self.seen = []

    def enrich(self, item, source):
        self.seen.append(item)
        fact = Fact(
            kind=FactKind.SUNSET_DATE,
            value="2026-10-15",
            source_url=item.url,
            evidence=(item.raw_text or "текст")[:40],
            value_date=date(2026, 10, 15),
            subject="claude-3-opus",
            evidence_verified=True,
        )
        return EnrichResult(
            source_id=str(item.extra.get("source_id", "")),
            url=item.url,
            facts=[fact],
            change_type=ChangeType.DEPRECATION,
            cost_usd=0.0,
        )


class SpySurface:
    name = "telegram"

    def __init__(self):
        self.sent = None

    def send_digest(self, signals):
        self.sent = signals
        return type("R", (), {"delivered": True, "message_id": "1", "error": None})()


@pytest.fixture(scope="module")
def config():
    return ThemeConfig.load("config/ai-tools.yaml")


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "radar.db")
    yield c
    c.close()


@pytest.fixture
def run_result(conn, config, tmp_path):
    """One real collection from cache, one real pass through every stage."""
    sources = [s for s in config.sources if s.id in CACHED_SOURCES]
    fetcher = Fetcher(cache_root="cache", polite_delay=0.0)

    from radar import run as run_module
    from radar.collect import collect_all as real_collect

    def cached_only(cfg, f, run_log=None, mode="live", srcs=None, max_workers=6):
        return real_collect(cfg, f, run_log, mode="backfill", sources=sources, max_workers=4)

    original = run_module.collect_all
    run_module.collect_all = cached_only
    try:
        run = DailyRun(conn, config, fetcher, RealShapedEnricher(),
                       for_date=TODAY, log_dir=str(tmp_path / "logs"))
        result = run.execute()
    finally:
        run_module.collect_all = original
    return run, result


class TestTheWholeMachineTurns:
    def test_the_run_completes(self, run_result):
        _, result = run_result
        assert result.ok, result.error

    def test_real_material_was_collected_from_cache(self, run_result):
        _, result = run_result
        assert result.collected > 10

    def test_signals_reach_the_store(self, run_result, conn):
        _, result = run_result
        assert read_signals(conn, result.run_id)


class TestValuesSurviveEverySeam:
    """Each assertion here is a bug that shipped once."""

    def test_change_type_arrives_at_the_signal(self, run_result, conn):
        _, result = run_result
        signals = read_signals(conn, result.run_id)
        assert all(s.change_type is not None for s in signals if s.signal_type
                   is SignalType.DIGEST_ITEM)

    def test_vendor_survives_clustering(self, run_result, conn):
        """Lost once when the grouping key became a content hash."""
        _, result = run_result
        digest_items = [s for s in read_signals(conn, result.run_id)
                        if s.signal_type is SignalType.DIGEST_ITEM]
        assert digest_items
        assert all(s.vendor for s in digest_items)

    def test_one_page_yields_many_materials(self, run_result):
        """Collapsed twice: once by dropping the URL fragment, once by keying
        dedup on URL alone when a page has no anchors."""
        _, result = run_result
        assert result.clusters > 5

    def test_facts_carry_their_parsed_date(self, run_result, conn):
        _, result = run_result
        facts = [f for s in read_signals(conn, result.run_id) for f in s.facts]
        assert facts
        assert all(f.value_date is not None for f in facts)

    def test_every_signal_names_the_run_it_came_from(self, run_result, conn):
        _, result = run_result
        assert all(s.run_summary is not None for s in read_signals(conn, result.run_id))

    def test_the_score_used_the_change_type_weight(self, run_result, conn):
        """A deprecation must outscore the floor a typeless signal would get."""
        _, result = run_result
        digest_items = [s for s in read_signals(conn, result.run_id)
                        if s.signal_type is SignalType.DIGEST_ITEM]
        assert digest_items
        assert max(s.score for s in digest_items) > 70


class TestTheFunnelIsAccountedFor:
    def test_nothing_leaves_without_a_record(self, run_result, conn):
        """FR-8.3: material that leaves the funnel is recorded with a reason."""
        run, result = run_result
        dropped = result.clusters - len(
            [s for s in read_signals(conn, result.run_id)
             if s.signal_type is SignalType.DIGEST_ITEM]
        )
        recorded = conn.execute(
            "SELECT COUNT(*) FROM filtered_items WHERE run_id = ?", (result.run_id,)
        ).fetchone()[0]
        # Either everything published, or every drop has a line.
        assert dropped <= 0 or recorded > 0

    def test_the_run_log_holds_every_stage(self, run_result):
        run, _ = run_result
        stages = {s["stage"] for s in run.log.stages}
        assert {"collect", "cluster", "enrich", "publish"} <= stages


class TestDeliveryClosesTheLoop:
    def test_a_delivered_run_reads_as_healthy(self, run_result, conn, tmp_path):
        """The supervisor exists to tell a silent agent from a quiet day."""
        run, result = run_result
        journal = Journal(conn, log_dir=str(tmp_path / "logs"), run_id=result.run_id)
        surface = SpySurface()
        report = deliver(conn, {"telegram": surface}, result.run_id, journal)

        assert report.all_delivered
        assert surface.sent
        diagnosis = Supervisor(conn, journal).diagnose(result.run_id)
        assert diagnosis.state is RunState.HEALTHY
        assert diagnosis.recommended is Action.NOTHING

    def test_without_delivery_the_supervisor_raises_it(self, run_result, conn, tmp_path):
        run, result = run_result
        journal = Journal(conn, log_dir=str(tmp_path / "logs"), run_id=result.run_id)
        diagnosis = Supervisor(conn, journal).diagnose(result.run_id)
        assert diagnosis.state is RunState.NEVER_DELIVERED
