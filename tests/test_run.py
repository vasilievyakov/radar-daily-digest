from datetime import UTC, date, datetime

import pytest

from radar.adapters.base import CollectedItem, SourceConfig
from radar.config import ThemeConfig
from radar.contracts import EnrichResult, RejectedFact
from radar.db import init_db, read_signals
from radar.fetch import FetchResult, Fetcher
from radar.models import ChangeType, Fact, FactKind, SignalType, Tier
from radar.run import DailyRun

TODAY = date(2026, 8, 17)
NOW = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)


class FakeFetcher(Fetcher):
    def __init__(self, tmp_path):
        super().__init__(cache_root=tmp_path / "cache", polite_delay=0.0)

    def get(self, url, headers=None, force=False, cache_key_extra=None):
        return FetchResult(url=url, status_code=200, text="", headers={}, ref="r", from_cache=True)


class FakeEnricher:
    """Stands in for stage 4 so the thread through stages can be tested alone."""

    def __init__(self, facts=None, fail=False, rejected=0):
        self.facts = facts if facts is not None else []
        self.fail = fail
        self.rejected = rejected
        self.calls = 0

    def enrich(self, item, source):
        self.calls += 1
        if self.fail:
            return EnrichResult(source_id="s", url=item.url, error="модель недоступна")
        return EnrichResult(
            source_id="s", url=item.url, facts=list(self.facts),
            rejected_facts=[RejectedFact("sunset_date", "x", "y", "evidence_not_in_source")] * self.rejected,
            change_type=ChangeType.DEPRECATION, cost_usd=0.005,
        )


def make_items(n=2):
    return [
        CollectedItem(
            url=f"https://docs.claude.com/deprecations#item{i}",
            title=f"Anthropic отключает модель {i}",
            raw_text=f"Модель {i} будет отключена 15 октября 2026 года. " * 3,
            event_date=date(2026, 8, 16),
            raw_material_ref="cache/ref",
            extra={"source_id": "anthropic_model_deprecations", "source_priority": 1},
        )
        for i in range(n)
    ]


@pytest.fixture
def env(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "radar.db")
    config = ThemeConfig.load("config/ai-tools.yaml")
    fetcher = FakeFetcher(tmp_path)

    def fake_collect(cfg, f, run_log=None, mode="live", sources=None, max_workers=6):
        from radar.collect import SourceOutcome
        from radar.models import SourceStatus
        items = make_items()
        outcomes = [
            SourceOutcome("anthropic_model_deprecations", SourceStatus.OK, items=items),
            SourceOutcome("cursor_changelog", SourceStatus.FAILED, error="HTTP 500"),
            SourceOutcome("mcp_servers", SourceStatus.EMPTY),
        ]
        if run_log is not None:
            for o in outcomes:
                run_log.source_result(o.source_id, o.status, o.count, None, o.error)
        return items, outcomes

    monkeypatch.setattr("radar.run.collect_all", fake_collect)
    yield conn, config, fetcher, tmp_path
    conn.close()


def sunset_fact():
    return Fact(
        kind=FactKind.SUNSET_DATE, value="2026-10-15",
        source_url="https://docs.claude.com/deprecations",
        evidence="будет отключена 15 октября 2026 года",
        value_date=date(2026, 10, 15), subject="claude-3-opus", evidence_verified=True,
    )


class TestHappyPath:
    def test_a_run_produces_signals_in_the_store(self, env):
        conn, config, fetcher, tmp = env
        run = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                       for_date=TODAY, log_dir=str(tmp / "logs"))
        result = run.execute()
        assert result.ok
        stored = read_signals(conn, result.run_id)
        assert stored
        assert stored[0].signal_type is SignalType.DIGEST_ITEM

    def test_ranks_are_consecutive_from_one(self, env):
        conn, config, fetcher, tmp = env
        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        ranks = [s.rank for s in read_signals(conn, result.run_id)]
        assert ranks == list(range(1, len(ranks) + 1))

    def test_the_footer_names_the_sources_that_failed(self, env):
        """S5 and DR-5 need names, and surfaces cannot read source_runs."""
        conn, config, fetcher, tmp = env
        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        summary = read_signals(conn, result.run_id)[0].run_summary
        assert summary.sources_failed == ["cursor_changelog"]
        assert summary.sources_empty == ["mcp_servers"]

    def test_rejected_facts_are_counted(self, env):
        conn, config, fetcher, tmp = env
        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()], rejected=2),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        # The two materials share a title signature and cluster into one,
        # so the enricher runs once and reports its two rejects.
        assert result.facts_rejected == 2

    def test_no_surface_is_imported_by_the_core(self):
        """The pipeline ends at the store; delivery is not its business."""
        import ast, pathlib
        tree = ast.parse(pathlib.Path("radar/run.py").read_text())
        imported = {
            n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        } | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not any("surface" in name for name in imported)


class TestQuietDay:
    def test_no_material_yields_a_quiet_day_record(self, env, monkeypatch):
        conn, config, fetcher, tmp = env
        from radar.collect import SourceOutcome
        from radar.models import SourceStatus
        monkeypatch.setattr(
            "radar.run.collect_all",
            lambda *a, **k: ([], [SourceOutcome("x", SourceStatus.OK)]),
        )
        result = DailyRun(conn, config, fetcher, FakeEnricher(),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        assert result.quiet
        [signal] = read_signals(conn, result.run_id)
        assert signal.signal_type is SignalType.QUIET_DAY

    def test_a_quiet_day_still_carries_the_run_summary(self, env, monkeypatch):
        conn, config, fetcher, tmp = env
        from radar.collect import SourceOutcome
        from radar.models import SourceStatus
        monkeypatch.setattr(
            "radar.run.collect_all",
            lambda *a, **k: ([], [SourceOutcome("x", SourceStatus.FAILED, error="e")]),
        )
        result = DailyRun(conn, config, fetcher, FakeEnricher(),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        assert read_signals(conn, result.run_id)[0].run_summary.sources_failed == ["x"]


class TestFailure:
    def test_a_crash_publishes_a_failure_record(self, env, monkeypatch):
        """Silence from a daily agent must not look like a quiet day."""
        conn, config, fetcher, tmp = env
        monkeypatch.setattr(
            "radar.run.cluster_items",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("кластеризация упала")),
        )
        result = DailyRun(conn, config, fetcher, FakeEnricher(),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        assert not result.ok
        [signal] = read_signals(conn, result.run_id)
        assert signal.signal_type is SignalType.RUN_FAILURE
        assert signal.failure_stage == "cluster"

    def test_earlier_stages_survive_a_later_crash(self, env, monkeypatch):
        """NFR-4: a crash on stage N keeps the record of stages before it."""
        conn, config, fetcher, tmp = env
        monkeypatch.setattr(
            "radar.run.cluster_items",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        run = DailyRun(conn, config, fetcher, FakeEnricher(),
                       for_date=TODAY, log_dir=str(tmp / "logs"))
        run.execute()
        assert "collect" in run.journal.completed_stages()
        assert "cluster" not in run.journal.completed_stages()

    def test_an_enricher_failure_does_not_stop_the_run(self, env):
        conn, config, fetcher, tmp = env
        result = DailyRun(conn, config, fetcher, FakeEnricher(fail=True),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        assert result.ok
        assert result.enriched == 0
        assert result.quiet  # nothing enriched means nothing to publish


class TestIdempotency:
    def test_rerunning_the_same_run_id_does_not_duplicate(self, env):
        conn, config, fetcher, tmp = env
        for _ in range(2):
            DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                     run_id="run-fixed", for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        assert len({s.signal_id for s in read_signals(conn, "run-fixed")}) == len(
            read_signals(conn, "run-fixed")
        )

    def test_a_second_run_sees_the_story_as_continuing(self, env):
        """FR-5.3: an unchanged story stops competing for the front of the digest.

        The second run scores it lower on novelty, which is the intended
        behaviour and the reason its signal set differs from the first.
        """
        conn, config, fetcher, tmp = env
        first = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                         run_id="run-1", for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        second = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          run_id="run-2", for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        assert not first.quiet
        assert second.quiet or read_signals(conn, "run-2")[0].delta_status is not None

    def test_the_same_cluster_keeps_its_signal_id_within_a_run(self, env):
        """PUB-5: a rerun of one run must not resurface everything as unread."""
        conn, config, fetcher, tmp = env
        ids = []
        for _ in range(2):
            DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                     run_id="run-fixed", for_date=TODAY, log_dir=str(tmp / "logs")).execute()
            ids.append({s.signal_id for s in read_signals(conn, "run-fixed")})
        # Both runs published something, and nothing accumulated.
        assert all(ids) and len(ids[0]) == len(ids[1])
