import re
from datetime import UTC, date, datetime, timedelta

import pytest

from radar.adapters.base import CollectedItem
from radar.cluster import Cluster
from radar.config import ThemeConfig
from radar.contracts import EnrichResult, RejectedFact
from radar.db import init_db, read_signals
from radar.fetch import FetchResult, Fetcher
from radar.models import (
    ChangeType,
    DatePrecision,
    EventStatement,
    Fact,
    FactKind,
    SignalType,
)
from radar import run as run_module
from radar.run import DailyRun, _headline_for
from radar.scoring import vendor_labels

TODAY = date(2026, 8, 17)
NOW = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)


class FakeFetcher(Fetcher):
    def __init__(self, tmp_path):
        super().__init__(cache_root=tmp_path / "cache", polite_delay=0.0)

    def get(self, url, headers=None, force=False, cache_key_extra=None):
        return FetchResult(url=url, status_code=200, text="", headers={}, ref="r", from_cache=True)


class FakeEnricher:
    """Stands in for stage 4 so the thread through stages can be tested alone.

    Returns a statement, not only facts. It used to return facts alone, which
    is a shape the real stage produces only when the model found no event at
    all — and the pipeline turned that into a card reading "Anthropic сообщает
    об изменении" with nothing under it. Every test here ran through that
    branch while claiming to test the ordinary one.
    """

    def __init__(self, facts=None, fail=False, rejected=0):
        self.facts = facts if facts is not None else []
        self.fail = fail
        self.rejected = rejected
        self.calls = 0

    def enrich(self, item, source):
        self.calls += 1
        if self.fail:
            return EnrichResult(source_id="s", url=item.url, error="модель недоступна")
        statement = EventStatement(
            statement_id=f"st-{abs(hash(item.url)) % 10**8}",
            text=f"Anthropic объявила об отключении модели из материала «{item.title}».",
            vendor=str(source.vendor or "anthropic") if source else "anthropic",
            product=(item.title or "модель")[:40],
            change_type=ChangeType.DEPRECATION,
            event_date=date(2026, 10, 15),
            source_url=item.url,
            evidence=(item.raw_text or item.title or "цитата")[:40],
            ingested_at=NOW,
            ingest_mode="live",
            extractor_model="fake",
            prompt_version="test",
            raw_material_ref=item.raw_material_ref or "ref",
        )
        return EnrichResult(
            source_id="s", url=item.url, facts=list(self.facts),
            statements=[statement],
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
        # The footer exists to name a source; asserting the slug froze the
        # defect in place under a docstring about names.
        assert summary.sources_failed == ["Cursor changelog"]
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


class TestFunnelAddsUp:
    """FR-8.3: everything dropped is recorded with a reason.

    The run-log page is the one artifact whose whole job is to earn trust, and
    a reader subtracts its numbers in their head. Material that leaves the
    funnel without a line in the log is the fastest way to lose that argument.
    """

    def test_material_below_the_threshold_is_recorded_not_vanished(self, env, monkeypatch):
        conn, config, fetcher, tmp = env
        # Thresholds high enough that nothing clears them.
        raised = dict(config.data)
        raised["scoring"] = {**config.scoring, "publish_threshold": 99, "digest_threshold": 98}
        config.data = raised

        run = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                       for_date=TODAY, log_dir=str(tmp / "logs"))
        result = run.execute()

        dropped = conn.execute(
            "SELECT reason_code, stage FROM filtered_items WHERE run_id = ? AND stage = 'score'",
            (result.run_id,),
        ).fetchall()
        assert dropped
        assert dropped[0][0] == "ниже_порога_публикации"

    def test_the_note_says_the_score_and_the_threshold(self, env):
        conn, config, fetcher, tmp = env
        raised = dict(config.data)
        raised["scoring"] = {**config.scoring, "publish_threshold": 99, "digest_threshold": 98}
        config.data = raised
        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        note = conn.execute(
            "SELECT reason_note FROM filtered_items WHERE run_id = ? AND stage = 'score'",
            (result.run_id,),
        ).fetchone()[0]
        assert "оценка" in note and "порог" in note


class TestFilterWiring:
    """The orchestrator must call the filter that exists, not one it imagines."""

    def test_the_real_filter_interface_is_used(self, env):
        conn, config, fetcher, tmp = env

        class RealShapedFilter:
            """Mirrors radar.filter.RelevanceFilter: one `run` returning an outcome."""

            def __init__(self):
                self.called = 0

            def run(self, clusters):
                self.called += 1

                class Outcome:
                    def __init__(self, kept):
                        self.clusters = kept
                        self.unjudged = []

                return Outcome(list(clusters))

        spy = RealShapedFilter()
        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          relevance_filter=spy, for_date=TODAY,
                          log_dir=str(tmp / "logs")).execute()
        assert spy.called == 1
        assert result.ok

    def test_a_filter_raising_lets_material_through(self, env):
        conn, config, fetcher, tmp = env

        class Broken:
            def run(self, clusters):
                raise RuntimeError("бэкенд недоступен")

        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          relevance_filter=Broken(), for_date=TODAY,
                          log_dir=str(tmp / "logs")).execute()
        assert result.ok
        assert result.relevant > 0


class TestSeamsCarryValues:
    """Stage tests prove each stage computes; these prove the output arrives.

    Every defect found on live data had one shape: a value that should exist
    is absent, and every consumer downstream has a valid path for absence.
    Nothing throws, and the run stays green while the product degrades.
    """

    def test_change_type_from_enrichment_reaches_the_signal(self, env):
        conn, config, fetcher, tmp = env
        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        signals = read_signals(conn, result.run_id)
        assert signals[0].change_type is not None
        assert str(signals[0].change_type) == "deprecation"

    def test_the_change_type_weight_is_actually_applied(self, env):
        """Without the seam a deprecation scores like an ordinary release."""
        conn, config, fetcher, tmp = env
        DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                 run_id="typed", for_date=TODAY,
                 log_dir=str(tmp / "logs")).execute()

        class Untyped(FakeEnricher):
            def enrich(self, item, source):
                out = super().enrich(item, source)
                out.change_type = None
                return out

        DailyRun(conn, config, fetcher, Untyped([sunset_fact()]),
                 run_id="untyped", for_date=TODAY,
                 log_dir=str(tmp / "logs")).execute()
        typed_score = read_signals(conn, "typed")[0].score
        untyped_score = read_signals(conn, "untyped")[0].score
        assert typed_score > untyped_score

    def test_the_rationale_names_the_change_not_only_the_plumbing(self, env):
        conn, config, fetcher, tmp = env
        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        rationale = read_signals(conn, result.run_id)[0].score_rationale
        assert rationale
        assert "отключени" in rationale.lower() or "deprecation" in rationale.lower()


class TestCompletenessIsVisible:
    """A strict conjunctive filter turns a misclassification into a confident
    "no precedents". The relaxed count is the only thing that makes that
    silence visible, and it has to reach a human, not just a column."""

    def test_a_near_miss_is_named_in_the_run_log(self, env):
        conn, config, fetcher, tmp = env
        # The same real event filed as limits in March and pricing in June:
        # the strict filter finds nothing, the relaxed one finds both.
        for i, (ct, day) in enumerate(
            [("limits", "2026-03-01"), ("limits", "2026-04-01")]
        ):
            conn.execute(
                "INSERT INTO event_statements (statement_id, text, vendor, change_type, "
                "event_date, source_url, statement_index, evidence, ingested_at, "
                "ingest_mode, extractor_model, prompt_version, raw_material_ref) "
                "VALUES (?, ?, 'anthropic', ?, ?, ?, ?, 'q', ?, 'backfill', 'm', 'v2', 'r')",
                (f"st{i}", "лимиты изменены", ct, day,
                 f"https://example.test/{i}", i, NOW.isoformat()),
            )
        conn.commit()

        class PricingEnricher(FakeEnricher):
            def enrich(self, item, source):
                out = super().enrich(item, source)
                out.change_type = ChangeType.PRICING
                return out

        run = DailyRun(conn, config, fetcher, PricingEnricher([sunset_fact()]),
                       for_date=TODAY, log_dir=str(tmp / "logs"))
        run.execute()
        notes = " ".join(run.log.notes)
        # One aggregate sentence, not one line per storyline: fifteen copies of
        # "strict 12, relaxed 27" were the last thing on the page every morning
        # and nobody could act on any of them. The count still has to reach a
        # human, which is what this asserts.
        assert "расширенный поиск нашёл больше строгого" in notes
        assert "не попали под точный тип изменения" in notes

    def test_an_exact_match_produces_no_noise(self, env):
        """Nothing to report when both counts agree."""
        conn, config, fetcher, tmp = env
        run = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                       for_date=TODAY, log_dir=str(tmp / "logs"))
        run.execute()
        assert not any("расширенный" in n for n in run.log.notes)


class TestHeadlineIsAClaimAboutTheEvent:
    """A headline names what changed, never the page it was found on.

    The material below is verbatim from the run of 2026-08-18
    (`20260818T005156-c52a20`), where sixteen of thirty-four headlines were
    the names of source pages: "Model status - claude-opus-5" is the title of
    the whole Anthropic deprecations registry and reads the same under every
    event on it.
    """

    @staticmethod
    def cluster_titled(title, vendor="anthropic", change_type="deprecation"):
        item = CollectedItem(
            url="https://docs.claude.com/en/docs/about-claude/model-deprecations",
            title=title,
            raw_text="",
            raw_material_ref="cache/ref",
            extra={"source_id": "anthropic_model_deprecations"},
        )
        return Cluster(
            cluster_id="c1", dedup_key="d1", items=[item],
            vendor=vendor, change_type=change_type,
        )

    @staticmethod
    def statement(text, product=None, vendor="anthropic",
                  change_type=ChangeType.DEPRECATION):
        return EventStatement(
            statement_id="st1", text=text, vendor=vendor, product=product,
            change_type=change_type, source_url="https://docs.claude.com/x",
            evidence="quote", ingested_at=NOW, ingest_mode="live",
            extractor_model="model", prompt_version="extract-v2",
            raw_material_ref="cache/ref",
        )

    def test_a_page_title_does_not_survive_a_usable_statement(self):
        """The defect itself: rank 1 of the run, verbatim.

        The statement ran to 129 characters, the gate accepted nothing over
        120, and the page title went out under it.
        """
        title = "Model status - claude-opus-5"
        headline = _headline_for(
            self.cluster_titled(title),
            self.statement(
                "Anthropic уведомила разработчиков о предстоящем прекращении "
                "работы модели Claude Opus 4.1 (claude-opus-4-1-20250805) в "
                "Claude API. Модель будет отключена 5 августа 2026 года.",
                product="Claude Opus 4.1",
            ),
            [sunset_fact()], {"anthropic": "Anthropic"}, {},
        )
        assert headline != title
        assert "Model status" not in headline
        assert "Anthropic" in headline
        assert "Claude Opus 4.1" in headline
        # The claim, not the second sentence and not the whole statement.
        assert "будет отключена" not in headline

    # Verbatim from the run: the page title on the left went out as the
    # headline, the statement on the right was sitting behind it unused.
    @pytest.mark.parametrize(
        "title, vendor, change_type, statement_text",
        [
            (
                "Model status - claude-opus-4-8", "anthropic", "deprecation",
                "Anthropic уведомила разработчиков о предстоящем прекращении поддержки модели Claude Opus 4.1 (claude-opus-4-1-20250805) в Claude API.",
            ),
            (
                "Legacy and end-of-life (EOL) models - Amazon", "aws", "deprecation",
                "AWS Bedrock перемещает Claude 3 Haiku в состояние Legacy, с эффективной датой 10 марта 2026 года и датой снятия 10 сентября 2026 года.",
            ),
            (
                "Model pricing - September 1, 2026", "anthropic", "pricing",
                "Anthropic отменила плановое повышение цен на Claude Sonnet 5 с 2 долларов за миллион входных токенов и 10 долларов за миллион выходных до 3 и 15 долларов соответственно.",
            ),
            (
                "Changes scheduled for 2027-01-01", "github", "breaking_change",
                "GitHub удаляет поле databaseId из типов CheckAnnotation и Artifact в GraphQL API, рекомендуя использовать fullDatabaseId вместо него.",
            ),
            (
                "Schema changes for 2026-08-17", "github", "deprecation",
                "GitHub объявила об удалении поля type из типа CopilotAgentTask; разработчикам рекомендуется использовать codingAgentFilter и codingAgentTypeFilter.",
            ),
            (
                "Gemini image models - gemini-3.1-flash-image", "google", "deprecation",
                "Google объявила о снятии с поддержки трёх основных моделей Gemini 2.5 (gemini-2.5-pro, gemini-2.5-flash, gemini-2.5-flash-lite) 20 октября 2026 года.",
            ),
            (
                "Veo models - veo-3.1-fast-generate-001", "google", "release",
                "Google выпустила две новые модели для видеогенерации: veo-3.1-generate-001 и veo-3.1-fast-generate-001, доступные с 17 ноября 2025 года.",
            ),
            (
                "v5.0.0-beta.8", "oh_my_openagent", "deprecation",
                "Выпущена версия 5.0.0-beta.8 Oh My OpenAgent с поддержкой параллельного выполнения десятков агентов, входа через подписку Cursor и Grok 4.6 как модели по умолчанию.",
            ),
        ],
    )
    def test_no_page_title_from_the_run_reaches_a_headline(
        self, title, vendor, change_type, statement_text
    ):
        headline = _headline_for(
            self.cluster_titled(title, vendor, change_type),
            self.statement(statement_text, vendor=vendor,
                           change_type=ChangeType(change_type)),
            [], vendor_labels(ThemeConfig.load("config/ai-tools.yaml").data), {},
        )
        assert headline != title
        # Every statement the core writes is Russian; raw source text is not.
        assert re.search(r"[а-яё]", headline, re.IGNORECASE)
        assert len(headline) > 20

    def test_a_cluster_without_a_statement_still_gets_a_claim(self):
        """Enrichment can return facts and no statement. That used to print
        the page name; it now says who did what, with what, and by when."""
        headline = _headline_for(
            self.cluster_titled("Model status - claude-opus-5"),
            None, [sunset_fact()], {"anthropic": "Anthropic"}, {},
        )
        assert headline != "Model status - claude-opus-5"
        assert "Anthropic" in headline
        assert "claude-3-opus" in headline
        assert "15 октября 2026" in headline

    def test_a_deprecation_is_an_announcement_not_a_shutdown(self):
        """The composed form may not claim more than the change type does."""
        headline = _headline_for(
            self.cluster_titled("Model status - claude-opus-5"),
            None, [sunset_fact()], {"anthropic": "Anthropic"}, {},
        )
        assert "объявляет об отключении" in headline

    def test_a_date_recovered_from_context_stays_out_of_the_headline(self):
        """FR-4.4 in a headline: a guessed year is not a deadline to print."""
        guessed = Fact(
            kind=FactKind.SUNSET_DATE, value="2026-10-15",
            source_url="https://docs.claude.com/deprecations",
            evidence="будет отключена 15 октября", value_date=date(2026, 10, 15),
            date_precision=DatePrecision.INFERRED, subject="claude-3-opus",
            evidence_verified=True,
        )
        headline = _headline_for(
            self.cluster_titled("Model status - claude-opus-5"),
            None, [guessed], {"anthropic": "Anthropic"}, {},
        )
        assert "claude-3-opus" in headline
        assert "2026" not in headline

    def test_a_deadline_belonging_to_another_model_is_not_borrowed(self):
        """One registry page carries two models retiring on two dates. Lifting
        one of those dates onto a headline about the other invents it."""
        def sunset(subject, when):
            return Fact(
                kind=FactKind.SUNSET_DATE, value=when.isoformat(),
                source_url="https://docs.claude.com/deprecations",
                evidence=f"{subject} retired", value_date=when,
                subject=subject, evidence_verified=True,
            )

        headline = _headline_for(
            self.cluster_titled("Model status - claude-opus-5"),
            None,
            [
                Fact(kind=FactKind.AFFECTED_PRODUCT, value="claude-opus-4-1",
                     source_url="https://docs.claude.com/deprecations",
                     evidence="claude-opus-4-1", evidence_verified=True),
                sunset("claude-sonnet-4", date(2026, 6, 15)),
                sunset("claude-haiku-3", date(2026, 4, 20)),
            ],
            {"anthropic": "Anthropic"}, {},
        )
        assert "claude-opus-4-1" in headline
        assert "июня" not in headline
        assert "апреля" not in headline

    def test_an_enumeration_is_never_cut_in_half(self):
        """Rank 13 of the run: a comma-separated list of three model ids. The
        shortening pass must not leave the reader with the first two."""
        headline = _headline_for(
            self.cluster_titled("Embeddings models - text-embedding-004",
                                vendor="google"),
            self.statement(
                "Google объявляет об отключении трёх моделей семейства Gemini "
                "2.5: gemini-2.5-pro, gemini-2.5-flash и gemini-2.5-flash-lite.",
                vendor="google",
            ),
            [], {"google": "Google"}, {},
        )
        assert headline.endswith("gemini-2.5-flash-lite")

    def test_the_headline_is_stored_whole_and_carries_no_markup(self):
        """Truncation is a surface operation; the core stores the claim."""
        headline = _headline_for(
            self.cluster_titled("Model pricing - September 1, 2026",
                                change_type="pricing"),
            self.statement(
                "Anthropic отменила плановое повышение цен на Claude Sonnet 5 с "
                "2 долларов за миллион входных токенов и 10 долларов за миллион "
                "выходных до 3 и 15 долларов соответственно.",
                change_type=ChangeType.PRICING,
            ),
            [], {"anthropic": "Anthropic"}, {},
        )
        assert not headline.endswith("…")
        assert not headline.endswith("...")
        assert not re.search(r"[*_`#\[\]]", headline)
        assert "соответственно" in headline

    def test_the_run_stores_a_claim_when_the_source_page_has_an_english_title(
        self, env, monkeypatch
    ):
        """End to end: the page title reaches the store only over the fix."""
        conn, config, fetcher, tmp = env
        title = "Model status - claude-opus-5"
        items = [
            CollectedItem(
                url="https://docs.claude.com/en/docs/about-claude/"
                    "model-deprecations#model-status",
                title=title,
                raw_text="claude-opus-4-1-20250805 will be retired "
                         "August 5, 2026. " * 3,
                event_date=date(2026, 8, 16),
                raw_material_ref="cache/ref",
                extra={"source_id": "anthropic_model_deprecations",
                       "source_priority": 1},
            )
        ]

        def collect_page_titled(cfg, f, run_log=None, mode="live",
                                sources=None, max_workers=6):
            return items, []

        monkeypatch.setattr("radar.run.collect_all", collect_page_titled)
        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()
        stored = read_signals(conn, result.run_id)
        assert stored
        assert stored[0].headline != title
        assert re.search(r"[а-яё]", stored[0].headline, re.IGNORECASE)


class TestOneCardPerEvent:
    """Clustering runs before the model and cannot see what the pages agree on.

    Five pages announcing one Veo shutdown — a table row, a release note, two
    mirrors of one document, a changelog line — differ in every word and match
    on nothing clustering can compare. What they share appears only after
    extraction: the vendor, the kind of change, the model being retired.
    """

    def _enriched(self, wordings: list[tuple[str, str]]):
        out = []
        for index, (product, text) in enumerate(wordings):
            item = CollectedItem(
                url=f"https://example.test/page-{index}",
                title=text[:40],
                raw_text=text,
                raw_material_ref="cache/ref",
                extra={"source_id": f"src-{index}"},
            )
            cluster = Cluster(
                cluster_id=f"c{index}",
                dedup_key=f"d{index}",
                items=[item],
                vendor="google",
                change_type="deprecation",
            )
            statement = EventStatement(
                statement_id=f"st-{index}",
                text=text,
                vendor="google",
                product=product,
                change_type=ChangeType.DEPRECATION,
                event_date=date(2026, 6, 30),
                source_url=f"https://example.test/page-{index}",
                evidence=product,
                ingested_at=datetime(2026, 8, 18, tzinfo=UTC),
                ingest_mode="live",
                extractor_model="fake",
                prompt_version="test",
                raw_material_ref="ref",
            )
            fact = Fact(
                kind=FactKind.SUNSET_DATE,
                value=f"2026-06-30 ({index})",
                source_url=f"https://example.test/page-{index}",
                evidence=product,
                value_date=date(2026, 6, 30),
                evidence_verified=True,
            )
            out.append((cluster, [fact], [statement]))
        return out

    def test_five_wordings_of_one_shutdown_become_one_card(self):
        enriched = self._enriched([
            ("veo-2.0-generate-001", "Google отключает veo-2.0-generate-001 30 июня."),
            ("veo-2.0-generate-001", "Google прекращает поддержку veo-2.0-generate-001."),
            ("veo-2.0-generate-001", "Отключение veo-2.0-generate-001 назначено на 30 июня 2026."),
            ("veo-2.0-generate-001", "Google объявила о выводе veo-2.0-generate-001."),
            ("veo-2.0-generate-001", "veo-2.0-generate-001 будет отключена."),
        ])

        collapsed, dropped = run_module._collapse_to_events(enriched)

        assert len(collapsed) == 1
        assert dropped == 4
        cluster, facts, statements = collapsed[0]
        # Merged, not discarded: the card can say it was seen in five places.
        assert cluster.duplicates_count == 4
        assert len(facts) == 5
        assert len(statements) == 5

    def test_every_merge_is_recorded_under_its_own_key(self):
        """Otherwise the funnel says seven merged and names two.

        The five sections behind one shutdown share a URL, so a rejection keyed
        by address overwrites its neighbours — the same defect that made the
        corpus store one event eight times, one layer up.
        """
        class SpyLog:
            def __init__(self):
                self.rows = []

            def filtered(self, **kwargs):
                self.rows.append(kwargs)

        enriched = self._enriched([
            ("veo-2.0-generate-001", "Google отключает veo-2.0-generate-001 30 июня."),
            ("veo-2.0-generate-001", "Отключение veo-2.0-generate-001 назначено."),
            ("veo-2.0-generate-001", "Google выводит veo-2.0-generate-001."),
        ])
        # One address for all three, as a page with a single anchor gives.
        for cluster, _f, _s in enriched:
            cluster.items[0].url = "https://example.test/deprecations#veo"

        log = SpyLog()
        collapsed, dropped = run_module._collapse_to_events(enriched, log)

        assert dropped == 2
        assert len(log.rows) == 2
        assert len({row["item_key"] for row in log.rows}) == 2

    def test_two_models_retired_the_same_day_stay_two_cards(self):
        enriched = self._enriched([
            ("veo-2.0-generate-001", "Google отключает veo-2.0-generate-001 30 июня."),
            ("veo-3.0-generate-001", "Google отключает veo-3.0-generate-001 30 июня."),
        ])

        collapsed, dropped = run_module._collapse_to_events(enriched)

        assert len(collapsed) == 2
        assert dropped == 0

    def test_the_richer_extraction_leads(self):
        enriched = self._enriched([
            ("veo-2.0-generate-001", "Короткая формулировка."),
            ("veo-2.0-generate-001", "Google отключает veo-2.0-generate-001 30 июня 2026 года."),
        ])
        # Give the second one another verified fact, so it is the fuller read.
        enriched[1][1].append(
            Fact(
                kind=FactKind.AFFECTED_PRODUCT,
                value="veo-2.0-generate-001",
                source_url="https://example.test/page-1",
                evidence="veo-2.0-generate-001",
                evidence_verified=True,
            )
        )

        collapsed, _ = run_module._collapse_to_events(enriched)

        assert len(collapsed) == 1
        _, _, statements = collapsed[0]
        assert statements[0].text.startswith("Google отключает")


class TestEverySignalLinksItsRunLog:
    """The one link that turns a claim into something a reader can check.

    `run_log_url` is on the contract, the web surface renders it, and no code
    path ever set it: it was None on all thirty-four signals of the last run.
    A card saying "the fourth time since February" with no way to see how that
    was computed is asking to be believed.
    """

    def test_digest_items_carry_it(self, env):
        conn, config, fetcher, tmp = env
        result = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()

        stored = read_signals(conn, result.run_id)
        assert stored
        assert all(s.run_log_url for s in stored)

    def test_an_absolute_base_from_config_wins(self, env):
        conn, config, fetcher, tmp = env
        config.data.setdefault("delivery", {})["run_log_url_base"] = "https://radar.test/"
        run = DailyRun(conn, config, fetcher, FakeEnricher([sunset_fact()]),
                       for_date=TODAY, log_dir=str(tmp / "logs"))

        assert run.run_log_url == "https://radar.test/run-log.html"


class TestTheLineThatAsksForAction:
    """«Почему это важно» is the one line written to make somebody act.

    It was also the one line nobody proof-read: fourteen cards of thirty-four
    said "срок наступает через 1 дней", and the clause after the full stop
    began in lower case. Two lines above, on the same card, a surface with a
    plural helper printed "через 241 день" correctly — the helper lived in
    email.py, where the core could not reach it.
    """

    @staticmethod
    def _cluster_and_facts(days_ahead: int):
        item = CollectedItem(
            url="https://docs.claude.com/deprecations",
            title="Model deprecations",
            raw_text="",
            raw_material_ref="ref",
            extra={"source_id": "anthropic_model_deprecations"},
        )
        cluster = Cluster(cluster_id="c1", dedup_key="d1", items=[item],
                          vendor="anthropic", change_type="deprecation")
        when = date(2026, 8, 18) + timedelta(days=days_ahead)
        facts = [
            Fact(kind=FactKind.SUNSET_DATE, value=when.isoformat(),
                 source_url=item.url, evidence="shutdown", value_date=when,
                 subject="claude-3-opus", evidence_verified=True),
            Fact(kind=FactKind.AFFECTED_PRODUCT, value="claude-3-opus",
                 source_url=item.url, evidence="claude-3-opus",
                 evidence_verified=True),
        ]
        return cluster, facts

    @pytest.mark.parametrize(
        "days_ahead,expected",
        [(1, "через 1 день"), (2, "через 2 дня"), (5, "через 5 дней"),
         (11, "через 11 дней"), (21, "через 21 день"), (241, "через 241 день"),
         (642, "через 642 дня")],
    )
    def test_the_number_of_days_agrees(self, days_ahead, expected):
        cluster, facts = self._cluster_and_facts(days_ahead)
        text = run_module._why_it_matters(cluster, facts, date(2026, 8, 18))
        assert expected in text

    def test_every_clause_starts_with_a_capital(self):
        cluster, facts = self._cluster_and_facts(5)
        text = run_module._why_it_matters(cluster, facts, date(2026, 8, 18))

        clauses = [part.strip() for part in text.split(". ") if part.strip()]
        assert len(clauses) > 1
        for clause in clauses:
            assert clause[0].isupper(), f"строчная после точки: {clause!r}"

    def test_an_identifier_keeps_its_case(self):
        cluster, facts = self._cluster_and_facts(5)
        text = run_module._why_it_matters(cluster, facts, date(2026, 8, 18))
        assert "claude-3-opus" in text


class TestNotKnowingIsNotQuiet:
    """«Проверено 0 источников» under «Сегодня ничего не изменилось».

    The two states rendered identically: same headline, same calm tone, the
    count one line below in smaller type. A run that reached nothing told the
    reader with complete confidence that nothing had happened — which is the
    single failure this product exists to prevent, arriving through its own
    front door.
    """

    def test_a_run_that_checked_nothing_does_not_claim_a_quiet_day(
        self, env, monkeypatch
    ):
        conn, config, fetcher, tmp = env
        # Nothing collected and nobody checked: an unreachable network, a
        # misconfigured source list, a filesystem that lost the config.
        monkeypatch.setattr(run_module, "collect_all", lambda *a, **k: ([], []))
        result = DailyRun(
            conn, config, fetcher, FakeEnricher([sunset_fact()]),
            for_date=TODAY, log_dir=str(tmp / "logs"),
        ).execute()

        stored = read_signals(conn, result.run_id)
        assert stored
        assert stored[0].signal_type is SignalType.RUN_FAILURE
        assert "ничего не изменилось" not in stored[0].headline
        assert stored[0].failure_reason

    def test_a_real_quiet_day_still_reads_as_one(self, env, monkeypatch):
        conn, config, fetcher, tmp = env
        # Sources answered, nothing of theirs was significant enough.
        run = DailyRun(conn, config, fetcher, FakeEnricher([]),
                       for_date=TODAY, log_dir=str(tmp / "logs"))
        monkeypatch.setattr(run, "_assemble_and_rank", lambda *a, **k: [])
        result = run.execute()

        stored = read_signals(conn, result.run_id)
        assert stored[0].signal_type is SignalType.QUIET_DAY
        assert stored[0].run_summary.sources_checked > 0


class TestAMaterialWithNothingInItIsNotACard:
    """«Anthropic сообщает об изменении» — ноль фактов, 41 балл, порог 35.

    A table row reading "claude-opus-4-8 | Active | N/A | Not sooner than May
    28, 2027" states no event, and the model correctly returned none. The stage
    reported success, the cluster travelled on, and the publication threshold
    asks for a score, not for a single verified fact. A card with nothing on it
    is worse than no card.
    """

    def test_it_never_reaches_the_digest(self, env):
        conn, config, fetcher, tmp = env

        class SilentEnricher(FakeEnricher):
            def enrich(self, item, source):
                out = super().enrich(item, source)
                out.statements = []
                out.facts = []
                return out

        result = DailyRun(conn, config, fetcher, SilentEnricher(),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()

        stored = read_signals(conn, result.run_id)
        assert all(s.signal_type is not SignalType.DIGEST_ITEM for s in stored)

    def test_the_funnel_is_told_why(self, env):
        conn, config, fetcher, tmp = env

        class SilentEnricher(FakeEnricher):
            def enrich(self, item, source):
                out = super().enrich(item, source)
                out.statements = []
                out.facts = []
                return out

        result = DailyRun(conn, config, fetcher, SilentEnricher(),
                          for_date=TODAY, log_dir=str(tmp / "logs")).execute()

        rows = conn.execute(
            "SELECT reason_code FROM filtered_items WHERE run_id = ? AND stage = 'enrich'",
            (result.run_id,),
        ).fetchall()
        assert rows
        assert all(row["reason_code"] == "нечего_извлечь" for row in rows)


class TestALongHeadlineLosesOnlyItsTail:
    """Two headlines ran to 191 and 252 characters — five lines on the card.

    Both were lists of circumstances. The cut is structural, never mid-thought:
    the earliest comma that leaves a whole claim standing. Taking the last one
    instead cut inside the list and kept all 252 characters.
    """

    PRICE = (
        "Anthropic сделал стандартной ценой Claude Sonnet 5 прежнее вводное "
        "предложение в размере $2 за миллион входных токенов и $10 за миллион "
        "выходных токенов, действовавшее до 31 августа 2026 года"
    )
    SECURITY = (
        "Anthropic усилила защиту Claude Code, отклоняя пути с использованием "
        "пространства имён Windows NT при удалённом чтении файлов, "
        "восстановлении сеанса, включении CLAUDE.md, скриптах workflow и "
        "загрузке файлов для предотвращения утечки учетных данных NTLM"
    )

    def test_a_trailing_participle_goes(self):
        out = run_module._tighten(self.PRICE)
        assert "действовавшее" not in out
        assert out.endswith("выходных токенов")

    def test_the_cut_is_the_earliest_comma_not_the_last(self):
        out = run_module._tighten(self.SECURITY)
        assert out == "Anthropic усилила защиту Claude Code"

    def test_an_enumeration_of_products_is_never_split(self):
        listing = (
            "AWS Bedrock объявил об отключении модели AI21 Labs Jamba 1.5 Large, "
            "Claude Opus 4.1 и Cohere Command R"
        )
        assert run_module._tighten(listing) == listing

    def test_a_short_headline_is_left_alone(self):
        short = "Google объявляет об отключении трёх моделей Veo 30 июня 2026 года"
        assert run_module._tighten(short) == short

    def test_nothing_is_cut_mid_thought(self):
        for text in (self.PRICE, self.SECURITY):
            out = run_module._tighten(text)
            assert not out.endswith((",", "…", "...", " и", " с", " для"))


class TestACancellationDoesNotPromiseADeadline:
    """Лид дайджеста: «Anthropic отменила повышение цен» — «срок через 14 дней».

    The event carries a date — the day the rise would have taken effect — and
    the news is precisely that nothing happens then. There is no field for
    this: the model reports a date and the pipeline cannot tell an obligation
    from its cancellation, so the wording is read. The date is still shown; it
    is simply not promised.
    """

    @staticmethod
    def _inputs():
        item = CollectedItem(url="https://docs.claude.com/pricing", title="Model pricing",
                             raw_text="", raw_material_ref="ref")
        cluster = Cluster(cluster_id="c1", dedup_key="d1", items=[item],
                          vendor="anthropic", change_type="pricing")
        facts = [Fact(kind=FactKind.EFFECTIVE_DATE, value="2026-09-01",
                      source_url=item.url, evidence="September 1, 2026",
                      value_date=date(2026, 9, 1), evidence_verified=True)]
        return cluster, facts

    def test_a_cancelled_rise_states_the_date_without_promising_it(self):
        cluster, facts = self._inputs()
        text = run_module._why_it_matters(
            cluster, facts, date(2026, 8, 18),
            statement="Anthropic отменила ранее запланированное повышение цен до $3/$15.",
        )
        assert "рок наступает" not in text
        assert "1 сентября" in text

    def test_an_actual_deadline_is_still_promised(self):
        cluster, facts = self._inputs()
        text = run_module._why_it_matters(
            cluster, facts, date(2026, 8, 18),
            statement="Anthropic поднимает цену Claude Sonnet 5 с 1 сентября.",
        )
        assert "рок наступает через 14 дней" in text  # первая буква поднята

    def test_a_price_that_merely_stays_is_not_a_deadline_either(self):
        cluster, facts = self._inputs()
        text = run_module._why_it_matters(
            cluster, facts, date(2026, 8, 18),
            statement="Anthropic сохраняет цену $2/$10 как стандартную.",
        )
        assert "рок наступает" not in text
