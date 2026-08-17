"""Backfill tests. No network, no model: the collector and the enricher are
fakes, and the enricher is bound to the `Enricher` protocol, not to stage 4.

Pricing is pinned so that money is exact rather than approximate: the theme
config prices input at zero and output at 100 USD per million tokens, which
makes every material cost exactly 0.07 USD both in the estimate backfill
reserves and in what the fake reports back. A ceiling is then a countable
number of calls instead of a rounding argument.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from radar import backfill as bf
from radar.adapters.base import CollectedItem, SourceConfig
from radar.collect import SourceOutcome
from radar.config import ThemeConfig
from radar.contracts import EnrichResult, RejectedFact
from radar.db import init_db
from radar.journal import EventKind, Journal
from radar.models import ChangeType, DatePrecision, EventStatement, Fact, SourceStatus

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
MODEL = "test/extractor"
# input priced at zero, output at 100 USD/MTok: 700 output tokens = 0.07 USD.
COST_PER_MATERIAL = bf.OUTPUT_TOKENS_PER_MATERIAL / 1_000_000 * 100.0


def limit_for(materials: int) -> float:
    """Room for exactly N materials, plus a cent so that float arithmetic
    rather than the ceiling never decides the last call."""
    return materials * COST_PER_MATERIAL + 0.01


# -- fixtures ---------------------------------------------------------


def make_config(
    source_ids=("alpha", "beta"),
    budget: dict | None = None,
    concurrency: int = 4,
    priorities: dict[str, int] | None = None,
) -> ThemeConfig:
    priorities = priorities or {}
    data = {
        "theme": {"name": "test", "description": "test theme"},
        "corpus": {
            "vendors": [
                {"id": "anthropic", "label": "Anthropic"},
                {"id": "openai", "label": "OpenAI"},
                {"id": "google", "label": "Google"},
            ],
            "change_types": [
                {"id": "deprecation", "label": "Отключение"},
                {"id": "release", "label": "Релиз"},
            ],
            "readiness": {
                "min_events_per_dense_cell": 3,
                "min_vendors_with_dense_cell": 3,
                "dense_cell_change_types": ["deprecation", "breaking_change"],
            },
        },
        "sources": [
            {
                "id": sid,
                "type": "html_scrape",
                "url": f"https://{sid}.test/changelog",
                "priority": priorities.get(sid, 1),
                "backfill_supported": True,
                "backfill_depth_days": 540,
            }
            for sid in source_ids
        ],
        "models": {"enrich": MODEL},
        "llm": {
            "concurrency": concurrency,
            "pricing": {MODEL: {"input": 0.0, "output": 100.0}},
        },
        "trends": {"min_members": 3, "dormant_after_days": 3650},
    }
    if budget is not None:
        data["budget"] = budget
    return ThemeConfig(data)


def make_item(
    source_id: str, index: int, text: str = "материал источника"
) -> CollectedItem:
    return CollectedItem(
        url=f"https://{source_id}.test/changelog#item-{index}",
        title=f"{source_id} {index}",
        raw_text=text,
        extra={"source_id": source_id},
    )


def make_statement(
    url: str,
    position: int,
    vendor: str = "anthropic",
    change_type: str = "deprecation",
    event_date: date | None = None,
) -> EventStatement:
    return EventStatement(
        statement_id=f"{url}#{position}",
        text=f"{vendor} отключает эндпоинт {position}",
        vendor=vendor,
        change_type=ChangeType(change_type),
        event_date=event_date or date(2026, 3, 1),
        date_precision=DatePrecision.DAY,
        source_url=url,
        evidence="will be retired on",
        ingested_at=NOW,
        # Deliberately wrong: the writer must stamp `backfill` itself (FR-6.4).
        ingest_mode="live",
        extractor_model=MODEL,
        prompt_version="extract-v1",
        raw_material_ref="cache/http/aa/deadbeef",
    )


class FakeEnricher:
    """Stage 4 as the contract describes it: never raises, always accounts."""

    def __init__(
        self,
        statements_per_item: int = 1,
        cost_usd: float = COST_PER_MATERIAL,
        vendor_of=None,
        empty_urls: set[str] | None = None,
        rejected: int = 0,
        delay: float = 0.0,
    ) -> None:
        self.statements_per_item = statements_per_item
        self.cost_usd = cost_usd
        self.vendor_of = vendor_of or (lambda item: "anthropic")
        self.empty_urls = empty_urls or set()
        self.rejected = rejected
        self.delay = delay
        self.calls: list[str] = []
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def enrich(self, item: CollectedItem, source: SourceConfig) -> EnrichResult:
        with self._lock:
            self.calls.append(item.url)
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            statements = []
            if item.url not in self.empty_urls:
                for position in range(self.statements_per_item):
                    statements.append(
                        make_statement(
                            item.url,
                            position,
                            vendor=self.vendor_of(item),
                            event_date=date(2026, 1, 1)
                            + timedelta(days=30 * (position + len(self.calls) % 5)),
                        )
                    )
            facts = [
                Fact(
                    kind="sunset_date",
                    value="2026-09-01",
                    source_url=item.url,
                    evidence="will be retired on",
                    evidence_verified=True,
                )
            ]
            rejected = [
                RejectedFact(
                    kind="version",
                    value="v9",
                    evidence="not in source",
                    reason="цитата не найдена в исходном тексте",
                )
                for _ in range(self.rejected)
            ]
            return EnrichResult(
                source_id=source.id,
                url=item.url,
                statements=statements,
                facts=facts,
                rejected_facts=rejected,
                change_type=ChangeType.DEPRECATION,
                cost_usd=self.cost_usd,
            )
        finally:
            with self._lock:
                self._in_flight -= 1


def fake_collector(items_by_source: dict[str, list[CollectedItem]]):
    """Stand-in for radar.collect.collect_all with the same call shape."""

    def collect_all(
        config, fetcher, run_log=None, mode="live", sources=None, max_workers=6
    ):
        chosen = [s.id for s in (sources or [])]
        outcomes = []
        items: list[CollectedItem] = []
        for source_id in chosen:
            source_items = items_by_source.get(source_id, [])
            items.extend(source_items)
            outcomes.append(
                SourceOutcome(
                    source_id,
                    SourceStatus.OK if source_items else SourceStatus.EMPTY,
                    items=list(source_items),
                )
            )
        return items, outcomes

    return collect_all


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    yield conn
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Everything a run needs except the items, which each test supplies."""

    class Env:
        def __init__(self) -> None:
            self.log_dir = str(tmp_path / "logs")
            self.fetcher = object()  # never used: the collector is faked

        def install(self, items_by_source):
            monkeypatch.setattr(bf, "collect_all", fake_collector(items_by_source))

        def run(self, conn, config, enricher, **kwargs):
            options = bf.BackfillOptions(**kwargs)
            return bf.run_backfill(
                conn,
                config,
                self.fetcher,
                enricher,
                options,
                log_dir=self.log_dir,
                progress=None,
            )

    return Env()


def rows(conn):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT source_url, statement_index, ingest_mode, vendor, change_type "
            "FROM event_statements ORDER BY source_url, statement_index"
        )
    ]


# -- the limit is mandatory -------------------------------------------


class TestLimitRequired:
    def test_no_limit_anywhere_is_a_refusal(self):
        config = make_config()  # no budget section at all
        with pytest.raises(bf.BudgetNotSet) as exc:
            bf.resolve_limit(None, config)
        assert "--limit-usd" in str(exc.value)

    def test_refusal_happens_before_any_work(self, db, env):
        config = make_config()
        enricher = FakeEnricher()
        env.install({"alpha": [make_item("alpha", 0)]})
        with pytest.raises(bf.BudgetNotSet):
            env.run(db, config, enricher, limit_usd=None)
        assert enricher.calls == []
        assert rows(db) == []

    def test_config_supplies_the_limit_when_the_flag_does_not(self):
        config = make_config(budget={"max_usd_per_backfill": 4.0})
        assert bf.resolve_limit(None, config) == 4.0

    def test_flag_wins_over_config(self):
        config = make_config(budget={"max_usd_per_backfill": 4.0})
        assert bf.resolve_limit(1.5, config) == 1.5

    def test_zero_is_refused_too(self):
        config = make_config(budget={"max_usd_per_backfill": 0})
        with pytest.raises(bf.BudgetNotSet):
            bf.resolve_limit(None, config)


# -- dry run ----------------------------------------------------------


class TestDryRun:
    def test_prices_the_run_without_calling_the_model(self, db, env):
        config = make_config()
        items = {
            "alpha": [make_item("alpha", i) for i in range(5)],
            "beta": [make_item("beta", i) for i in range(3)],
        }
        env.install(items)
        enricher = FakeEnricher()
        report = env.run(db, config, enricher, limit_usd=8.0, dry_run=True)

        assert enricher.calls == []
        assert report.dry_run is True
        assert report.materials_collected == 8
        assert report.materials_pending == 8
        assert report.estimated_usd == pytest.approx(8 * COST_PER_MATERIAL)
        assert report.batches_total == 2  # one per source, both under batch_size
        assert rows(db) == []

    def test_batches_split_a_large_source_and_shape_the_estimate(self, db, env):
        config = make_config(source_ids=("alpha",), concurrency=4)
        env.install({"alpha": [make_item("alpha", i) for i in range(250)]})
        report = env.run(
            db, config, None, limit_usd=100.0, dry_run=True, batch_size=100
        )
        assert report.batches_total == 3
        assert report.longest_batch == 100
        assert report.eta_seconds == pytest.approx(100 * bf.SECONDS_PER_MATERIAL)

    def test_warns_when_the_estimate_exceeds_the_limit(self, db, env):
        env.install({"alpha": [make_item("alpha", i) for i in range(100)]})
        report = env.run(
            db, make_config(source_ids=("alpha",)), None, limit_usd=0.5, dry_run=True
        )
        assert report.estimated_usd > report.limit_usd
        assert any("превышает лимит" in note for note in report.notes)


# -- writing the corpus ------------------------------------------------


class TestWrite:
    def test_rows_are_marked_backfill_and_indexed_by_position(self, db, env):
        config = make_config(source_ids=("alpha",))
        env.install({"alpha": [make_item("alpha", 0)]})
        enricher = FakeEnricher(statements_per_item=3)
        report = env.run(db, config, enricher, limit_usd=1.0)

        stored = rows(db)
        assert len(stored) == 3
        assert {row["ingest_mode"] for row in stored} == {bf.INGEST_MODE}
        assert [row["statement_index"] for row in stored] == [0, 1, 2]
        assert report.statements_added == 3
        assert report.facts_kept == 1

    def test_report_counts_facts_the_verifier_refused(self, db, env):
        config = make_config(source_ids=("alpha",))
        env.install({"alpha": [make_item("alpha", i) for i in range(2)]})
        enricher = FakeEnricher(rejected=2)
        report = env.run(db, config, enricher, limit_usd=1.0)
        assert report.facts_rejected == 4

    def test_persist_is_insert_or_ignore(self, db):
        url = "https://alpha.test/changelog#item-0"
        result = EnrichResult(
            source_id="alpha",
            url=url,
            statements=[make_statement(url, 0), make_statement(url, 1)],
        )
        assert bf.persist_statements(db, [(0, result)]) == (2, 0)
        # A second write of the same material collides on (source_url, index).
        assert bf.persist_statements(db, [(0, result)]) == (0, 2)
        assert len(rows(db)) == 2

    def test_materials_sharing_one_url_do_not_collide(self, db):
        """A changelog page without anchors hands over many materials under
        one address; two of them must not overwrite each other."""
        url = "https://alpha.test/changelog"
        first = EnrichResult(
            source_id="alpha", url=url, statements=[make_statement(url, 0)]
        )
        second = EnrichResult(
            source_id="alpha", url=url, statements=[make_statement(url, 0)]
        )
        assert bf.persist_statements(db, [(0, first), (1, second)]) == (2, 0)
        stored = rows(db)
        assert [row["statement_index"] for row in stored] == [
            0,
            bf.EVENTS_PER_MATERIAL,
        ]
        ids = {
            row[0] for row in db.execute("SELECT statement_id FROM event_statements")
        }
        assert len(ids) == 2

    def test_slots_follow_document_order(self):
        items = [
            make_item("alpha", 0),
            CollectedItem(url="https://alpha.test/x", title="t", raw_text="a"),
            CollectedItem(url="https://alpha.test/x", title="t", raw_text="b"),
        ]
        assert bf.material_slots(items) == [0, 0, 1]

    def test_backfill_produces_no_delivery_and_no_signals(self, db, env):
        config = make_config(source_ids=("alpha",))
        env.install({"alpha": [make_item("alpha", i) for i in range(3)]})
        report = env.run(db, config, FakeEnricher(), limit_usd=1.0)

        journal = Journal(db, log_dir=env.log_dir, run_id=report.run_id)
        assert journal.events(run_id=report.run_id, kind=EventKind.DELIVERY_SENT) == []
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM filtered_items").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 0


# -- idempotency -------------------------------------------------------


class TestIdempotency:
    def test_second_run_adds_nothing_and_calls_nothing(self, db, env):
        config = make_config()
        items = {
            "alpha": [make_item("alpha", i) for i in range(3)],
            "beta": [make_item("beta", i) for i in range(2)],
        }
        env.install(items)

        first = env.run(db, config, FakeEnricher(statements_per_item=2), limit_usd=2.0)
        assert first.statements_added == 10
        assert first.complete

        second_enricher = FakeEnricher(statements_per_item=2)
        second = env.run(db, config, second_enricher, limit_usd=2.0)

        assert second_enricher.calls == []
        assert second.statements_added == 0
        assert second.materials_already_ingested == 5
        assert second.spent_usd == 0.0
        assert len(rows(db)) == 10

    def test_new_materials_from_a_better_parser_are_picked_up(self, db, env):
        config = make_config(source_ids=("alpha",))
        env.install({"alpha": [make_item("alpha", 0)]})
        env.run(db, config, FakeEnricher(), limit_usd=1.0)

        env.install({"alpha": [make_item("alpha", 0), make_item("alpha", 1)]})
        enricher = FakeEnricher()
        report = env.run(db, config, enricher, limit_usd=1.0)

        assert enricher.calls == ["https://alpha.test/changelog#item-1"]
        assert report.statements_added == 1
        assert len(rows(db)) == 2


# -- the ceiling stops the run, it does not lose work ------------------


class TestBudget:
    def test_run_stops_on_the_ceiling_and_keeps_what_it_bought(self, db, env):
        config = make_config(source_ids=("alpha",), concurrency=1)
        env.install({"alpha": [make_item("alpha", i) for i in range(10)]})
        enricher = FakeEnricher()
        # Room for exactly three materials at 0.07 each.
        report = env.run(db, config, enricher, limit_usd=limit_for(3), batch_size=10)

        assert report.budget_exhausted is True
        assert len(enricher.calls) == 3
        assert report.spent_usd == pytest.approx(3 * COST_PER_MATERIAL)
        assert report.spent_usd <= report.limit_usd
        assert len(rows(db)) == 3  # the partial batch is written down anyway
        assert report.complete is False

    def test_shared_budget_holds_under_concurrency(self, db, env):
        config = make_config(source_ids=("alpha",), concurrency=8)
        env.install({"alpha": [make_item("alpha", i) for i in range(40)]})
        enricher = FakeEnricher(delay=0.01)
        report = env.run(
            db,
            config,
            enricher,
            limit_usd=limit_for(5),
            concurrency=8,
            batch_size=1,  # forty batches, eight workers, one shared ceiling
        )

        assert enricher.max_in_flight > 1, "параллель не сработала"
        assert len(enricher.calls) == 5
        assert report.spent_usd <= report.limit_usd + 1e-9
        assert len(rows(db)) == 5

    def test_budget_exhaustion_is_journalled(self, db, env):
        config = make_config(source_ids=("alpha",), concurrency=1)
        env.install({"alpha": [make_item("alpha", i) for i in range(4)]})
        report = env.run(
            db, config, FakeEnricher(), limit_usd=limit_for(1), batch_size=4
        )
        journal = Journal(db, log_dir=env.log_dir, run_id=report.run_id)
        events = journal.events(run_id=report.run_id, kind=EventKind.BUDGET_EXCEEDED)
        assert len(events) == 1

    def test_ceiling_adapts_when_calls_cost_more_than_the_price_table_says(
        self, db, env
    ):
        """The Claude CLI bills its own session, well above the token price.
        The run must notice and reserve against what it is actually paying."""
        config = make_config(source_ids=("alpha",), concurrency=1)
        env.install({"alpha": [make_item("alpha", i) for i in range(60)]})
        real_cost = 10 * COST_PER_MATERIAL
        enricher = FakeEnricher(cost_usd=real_cost)
        report = env.run(
            db, config, enricher, limit_usd=1.0, concurrency=1, batch_size=60
        )

        assert report.budget_exhausted is True
        # Without adaptation the estimate would let 14 calls through at 0.7 USD
        # each of overspend; with it the run stops within a call of the limit.
        assert report.spent_usd <= 1.0 + real_cost
        assert len(enricher.calls) <= 2 + int(1.0 / real_cost)

    def test_shared_budget_reserve_is_atomic(self):
        budget = bf.SharedBudget(1.0)
        errors: list[Exception] = []
        granted: list[int] = []

        def take():
            try:
                budget.reserve(0.1)
                granted.append(1)
            except Exception as exc:  # BudgetExceeded
                errors.append(exc)

        threads = [threading.Thread(target=take) for _ in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(granted) == 10
        assert len(errors) == 40
        assert budget.spent_usd == pytest.approx(1.0)


# -- warming the prefix cache ------------------------------------------


class TestWarmup:
    def test_one_call_runs_before_the_fan_out(self, db, env):
        """Eight workers starting together all miss the prefix cache and each
        pay to create it. One call first makes the rest cache reads."""
        config = make_config(source_ids=("alpha",), concurrency=8)
        env.install({"alpha": [make_item("alpha", i) for i in range(9)]})

        order: list[int] = []

        class OrderedEnricher(FakeEnricher):
            def enrich(self, item, source):
                with self._lock:
                    order.append(self._in_flight)
                return super().enrich(item, source)

        enricher = OrderedEnricher(delay=0.01)
        report = env.run(
            db, config, enricher, limit_usd=5.0, concurrency=8, batch_size=4
        )

        assert order[0] == 0, "первый вызов идёт в одиночку"
        assert enricher.max_in_flight > 1, "после прогрева пошла параллель"
        assert report.warmup_usd == pytest.approx(COST_PER_MATERIAL)
        assert report.materials_processed == 9
        assert len(rows(db)) == 9

    def test_warmup_is_checkpointed_under_its_own_stage(self, db, env):
        config = make_config(source_ids=("alpha",), concurrency=2)
        env.install({"alpha": [make_item("alpha", i) for i in range(3)]})
        report = env.run(db, config, FakeEnricher(), limit_usd=5.0, batch_size=2)

        stages = Journal(
            db, log_dir=env.log_dir, run_id=report.run_id
        ).completed_stages(report.run_id)
        assert "backfill:alpha:warmup" in stages
        assert report.complete
        assert len(rows(db)) == 3

    def test_a_failed_warmup_only_warns(self, db, env):
        config = make_config(source_ids=("alpha",), concurrency=2)
        env.install({"alpha": [make_item("alpha", i) for i in range(3)]})

        class BrokenFirstCall(FakeEnricher):
            def enrich(self, item, source):
                if not self.calls:
                    self.calls.append(item.url)
                    raise RuntimeError("модель недоступна")
                return super().enrich(item, source)

        report = env.run(db, config, BrokenFirstCall(), limit_usd=5.0, batch_size=2)

        assert any("прогрев" in note for note in report.notes)
        assert report.materials_failed == 1
        assert report.statements_added == 2, "остальные материалы обработаны"

    def test_warmup_can_be_switched_off(self, db, env):
        config = make_config(source_ids=("alpha",), concurrency=2)
        env.install({"alpha": [make_item("alpha", i) for i in range(2)]})
        report = env.run(db, config, FakeEnricher(), limit_usd=5.0, warm_prefix=False)
        assert report.warmup_usd == 0.0
        assert len(rows(db)) == 2


# -- resuming ----------------------------------------------------------


class TestResume:
    def test_a_crash_leaves_checkpoints_and_resume_finishes_the_rest(
        self, db, env, monkeypatch
    ):
        config = make_config(source_ids=("alpha", "beta"), concurrency=1)
        items = {
            # alpha is larger, so its batch is dispatched first
            "alpha": [make_item("alpha", i) for i in range(3)],
            "beta": [make_item("beta", i) for i in range(2)],
        }
        env.install(items)

        real_persist = bf.persist_statements
        state = {"calls": 0}

        def flaky_persist(conn, results):
            state["calls"] += 1
            if state["calls"] == 2:
                raise RuntimeError("процесс убит на середине")
            return real_persist(conn, results)

        monkeypatch.setattr(bf, "persist_statements", flaky_persist)
        first = FakeEnricher()
        with pytest.raises(RuntimeError):
            env.run(
                db,
                config,
                first,
                limit_usd=5.0,
                concurrency=1,
                batch_size=100,
                warm_prefix=False,
            )

        assert len(rows(db)) == 3, "успевшее записаться остаётся в корпусе"
        run_id = bf.find_unfinished_run(db)
        assert run_id is not None
        journal = Journal(db, log_dir=env.log_dir, run_id=run_id)
        stages = journal.completed_stages(run_id)
        assert bf.batch_stage("alpha", 0) in stages
        assert bf.batch_stage("beta", 0) not in stages

        monkeypatch.setattr(bf, "persist_statements", real_persist)
        second = FakeEnricher()
        report = env.run(
            db,
            config,
            second,
            limit_usd=5.0,
            resume=True,
            concurrency=1,
            warm_prefix=False,
        )

        assert report.run_id == run_id
        assert report.resumed is True
        assert all("beta" in url for url in second.calls)
        assert len(second.calls) == 2
        assert len(rows(db)) == 5
        assert report.complete

    def test_checkpoint_skips_a_batch_that_produced_nothing(self, db, env):
        """A material with no statements leaves no URL behind: only the
        checkpoint can stop a resumed run from paying for it again."""
        config = make_config(source_ids=("alpha", "beta"), concurrency=1)
        empty_url = "https://alpha.test/changelog#item-0"
        env.install(
            {
                "alpha": [make_item("alpha", 0)],
                "beta": [make_item("beta", 0)],
            }
        )
        enricher = FakeEnricher(empty_urls={empty_url})
        # Room for one material only: alpha runs, beta does not.
        first = env.run(
            db,
            config,
            enricher,
            limit_usd=limit_for(1),
            concurrency=1,
            batch_size=1,
            warm_prefix=False,
        )
        assert first.budget_exhausted

        resumed_enricher = FakeEnricher()
        second = env.run(
            db,
            config,
            resumed_enricher,
            limit_usd=5.0,
            resume=True,
            concurrency=1,
            batch_size=1,
            warm_prefix=False,
        )
        assert empty_url not in resumed_enricher.calls
        assert second.resumed is True

    def test_ctrl_c_keeps_the_finished_batches_and_resume_finishes(self, db, env):
        config = make_config(source_ids=("alpha",), concurrency=1)
        env.install({"alpha": [make_item("alpha", i) for i in range(4)]})

        class InterruptingEnricher(FakeEnricher):
            def enrich(self, item, source):
                if len(self.calls) >= 1:
                    self.calls.append(item.url)
                    raise KeyboardInterrupt
                return super().enrich(item, source)

        enricher = InterruptingEnricher()
        report = env.run(
            db,
            config,
            enricher,
            limit_usd=5.0,
            concurrency=1,
            batch_size=1,
            warm_prefix=False,
        )

        assert report.interrupted is True
        assert report.complete is False
        assert len(rows(db)) == 1, "первая пачка записана до прерывания"
        assert report.spent_usd == pytest.approx(COST_PER_MATERIAL), (
            "прерванный вызов не оплачивается"
        )

        run_id = bf.find_unfinished_run(db)
        assert run_id == report.run_id
        stages = Journal(db, log_dir=env.log_dir, run_id=run_id).completed_stages(
            run_id
        )
        assert bf.batch_stage("alpha", 0) in stages
        assert bf.batch_stage("alpha", 1) not in stages

        resumed = env.run(
            db,
            config,
            FakeEnricher(),
            limit_usd=5.0,
            resume=True,
            concurrency=1,
            batch_size=1,
            warm_prefix=False,
        )
        assert resumed.run_id == run_id
        assert resumed.complete
        assert len(rows(db)) == 4

    def test_resume_carries_the_spend_into_the_new_budget(self, db, env):
        config = make_config(source_ids=("alpha",), concurrency=1)
        env.install({"alpha": [make_item("alpha", i) for i in range(6)]})
        first = env.run(
            db,
            config,
            FakeEnricher(),
            limit_usd=limit_for(2),
            concurrency=1,
            batch_size=1,
        )
        assert first.budget_exhausted
        spent = first.spent_usd

        second = env.run(
            db,
            config,
            FakeEnricher(),
            limit_usd=limit_for(3),
            resume=True,
            concurrency=1,
            batch_size=1,
        )
        # The ceiling covers the whole effort, so only one more material fits.
        assert second.spent_usd == pytest.approx(spent + COST_PER_MATERIAL)
        assert second.budget_exhausted is True
        assert len(rows(db)) == 3


# -- the verdict -------------------------------------------------------


class TestReadinessAndTrends:
    def test_report_answers_the_question_of_the_evening(self, db, env):
        vendors = {"alpha": "anthropic", "beta": "openai", "gamma": "google"}
        config = make_config(source_ids=tuple(vendors), concurrency=2)
        items = {sid: [make_item(sid, 0)] for sid in vendors}
        env.install(items)

        def vendor_of(item: CollectedItem) -> str:
            return vendors[item.extra["source_id"]]

        class DatedEnricher(FakeEnricher):
            def enrich(self, item, source):
                result = super().enrich(item, source)
                for offset, statement in enumerate(result.statements):
                    # Monthly cadence: recurring, and not routine noise.
                    statement.event_date = date(2026, 1, 5) + timedelta(
                        days=30 * offset
                    )
                return result

        enricher = DatedEnricher(statements_per_item=3, vendor_of=vendor_of)
        report = env.run(db, config, enricher, limit_usd=5.0)

        assert report.statements_added == 9
        assert report.readiness["ready_for_trend_demo"] is True
        assert set(report.readiness["vendors_with_dense_cell"]) == {
            "anthropic",
            "openai",
            "google",
        }
        assert len(report.trends_accepted) == 3
        assert db.execute("SELECT COUNT(*) FROM trends").fetchone()[0] == 3
        assert {(c["vendor"], c["change_type"], c["n"]) for c in report.cells} == {
            ("anthropic", "deprecation", 3),
            ("openai", "deprecation", 3),
            ("google", "deprecation", 3),
        }

    def test_thin_corpus_reports_not_ready(self, db, env):
        config = make_config(source_ids=("alpha",))
        env.install({"alpha": [make_item("alpha", 0)]})
        report = env.run(db, config, FakeEnricher(), limit_usd=1.0)
        assert report.readiness["ready_for_trend_demo"] is False
        assert report.trends_accepted == []


# -- source selection --------------------------------------------------


class TestSelection:
    def test_priority_filter(self):
        config = make_config(
            source_ids=("alpha", "beta", "gamma"),
            priorities={"alpha": 1, "beta": 2, "gamma": 3},
        )
        assert [s.id for s in bf.select_sources(config, priorities=[1])] == ["alpha"]
        assert [s.id for s in bf.select_sources(config, priorities=[1, 3])] == [
            "alpha",
            "gamma",
        ]
        assert len(bf.select_sources(config)) == 3

    def test_named_sources_win(self):
        config = make_config(source_ids=("alpha", "beta"))
        assert [s.id for s in bf.select_sources(config, ["beta"])] == ["beta"]

    def test_unknown_source_is_an_error(self):
        config = make_config(source_ids=("alpha",))
        with pytest.raises(ValueError):
            bf.select_sources(config, ["nope"])

    def test_eta_accounts_for_the_longest_batch(self):
        assert bf.estimate_eta_seconds([], 8) == 0.0
        # One huge batch bounds the run regardless of how many workers there are.
        assert bf.estimate_eta_seconds([100, 1, 1], 8) == pytest.approx(
            100 * bf.SECONDS_PER_MATERIAL
        )
        assert bf.estimate_eta_seconds([10] * 8, 8) == pytest.approx(
            10 * bf.SECONDS_PER_MATERIAL
        )


# -- the run log -------------------------------------------------------


class TestRunLog:
    def test_backfill_logs_a_call_per_material_by_default(self, db, env):
        config = make_config(source_ids=("alpha",))
        env.install({"alpha": [make_item("alpha", i) for i in range(3)]})
        report = env.run(db, config, FakeEnricher(), limit_usd=1.0)

        rows_ = [
            dict(r)
            for r in db.execute(
                "SELECT model, cost_usd, cached FROM model_calls WHERE run_id = ?",
                (report.run_id,),
            )
        ]
        assert len(rows_) == 3
        assert {row["model"] for row in rows_} == {MODEL}
        assert sum(row["cost_usd"] for row in rows_) == pytest.approx(report.spent_usd)

    def test_the_backend_may_own_the_call_rows_instead(self, db, env):
        """When the backend writes its own rows the run must not add a second
        set: that would count every call twice."""
        config = make_config(source_ids=("alpha",))
        env.install({"alpha": [make_item("alpha", i) for i in range(3)]})
        report = env.run(
            db, config, FakeEnricher(), limit_usd=1.0, log_model_calls=False
        )
        assert db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        # The run row still carries the money, which is what a resume reads.
        cost = db.execute(
            "SELECT cost_usd FROM runs WHERE run_id = ?", (report.run_id,)
        ).fetchone()[0]
        assert cost == pytest.approx(report.spent_usd)

    def test_call_log_carries_tokens_into_model_calls(self, db):
        from radar.cli import _CallLog

        log = _CallLog()
        log.model_call(
            stage="enrich",
            model=MODEL,
            provider="anthropic",
            tokens_in=15918,
            tokens_out=812,
            cost_usd=0.0079,
            cached=False,
        )
        log.model_call(
            stage="enrich", model=MODEL, tokens_in=100, tokens_out=20, cost_usd=0.0
        )
        assert log.write(db, "backfill-test") == (16018, 832)

        rows_ = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM model_calls WHERE run_id = ?", ("backfill-test",)
            )
        ]
        assert len(rows_) == 2
        assert rows_[0]["tokens_in"] == 15918
        assert rows_[0]["provider"] == "anthropic"


# -- cost profiles -----------------------------------------------------


class TestCostProfiles:
    def test_cli_profile_prices_per_call_not_per_token(self, db, env):
        config = make_config(source_ids=("alpha",))
        env.install({"alpha": [make_item("alpha", i) for i in range(100)]})
        report = env.run(
            db,
            config,
            None,
            limit_usd=100.0,
            dry_run=True,
            cost_profile=bf.COST_PROFILE_CLI,
        )
        expected = 100 * bf.CLI_WARM_CALL_USD + (
            bf.CLI_COLD_CALL_USD - bf.CLI_WARM_CALL_USD
        )
        assert report.estimated_usd == pytest.approx(expected)

    def test_token_profile_uses_the_price_table(self, db, env):
        config = make_config(source_ids=("alpha",))
        env.install({"alpha": [make_item("alpha", i) for i in range(10)]})
        report = env.run(db, config, None, limit_usd=100.0, dry_run=True)
        assert report.estimated_usd == pytest.approx(10 * COST_PER_MATERIAL)


# -- CLI ---------------------------------------------------------------


class TestCli:
    def _config_file(self, tmp_path, with_budget: bool) -> str:
        import yaml

        config = make_config(source_ids=("alpha",))
        data = dict(config.data)
        if with_budget:
            data["budget"] = {"max_usd_per_backfill": 8.0}
        path = tmp_path / "theme.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return str(path)

    def test_backfill_without_a_limit_exits_with_a_message(self, tmp_path, capsys):
        from radar.cli import main

        code = main(
            [
                "--db",
                str(tmp_path / "radar.db"),
                "--config",
                self._config_file(tmp_path, with_budget=False),
                "--cache",
                str(tmp_path / "cache"),
                "--logs",
                str(tmp_path / "logs"),
                "backfill",
            ]
        )
        assert code == 2
        assert "Лимит стоимости не задан" in capsys.readouterr().err

    def test_dry_run_prints_the_estimate(self, tmp_path, capsys, monkeypatch):
        from radar.cli import main

        monkeypatch.setattr(
            bf, "collect_all", fake_collector({"alpha": [make_item("alpha", 0)]})
        )
        code = main(
            [
                "--db",
                str(tmp_path / "radar.db"),
                "--config",
                self._config_file(tmp_path, with_budget=True),
                "--cache",
                str(tmp_path / "cache"),
                "--logs",
                str(tmp_path / "logs"),
                "backfill",
                "--dry-run",
                "--limit-usd",
                "8",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "ОЦЕНКА СТОИМОСТИ" in out
        assert "ни одного вызова модели" in out

    def test_status_and_doctor_run(self, tmp_path, capsys, monkeypatch):
        from radar.cli import main

        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        common = [
            "--db",
            str(tmp_path / "radar.db"),
            "--config",
            self._config_file(tmp_path, with_budget=True),
            "--cache",
            str(tmp_path / "cache"),
            "--logs",
            str(tmp_path / "logs"),
        ]
        assert main([*common, "status"]) == 0
        assert "ГОТОВНОСТЬ КОРПУСА" in capsys.readouterr().out
        assert main([*common, "doctor"]) == 0
        assert "ПРОВЕРКА ОКРУЖЕНИЯ" in capsys.readouterr().out

    def test_trends_command(self, tmp_path, capsys, monkeypatch):
        from radar.cli import main

        common = [
            "--db",
            str(tmp_path / "radar.db"),
            "--config",
            self._config_file(tmp_path, with_budget=True),
            "--logs",
            str(tmp_path / "logs"),
        ]
        assert main([*common, "trends", "--min-members", "3"]) == 0
        assert "ТРЕНДЫ" in capsys.readouterr().out
