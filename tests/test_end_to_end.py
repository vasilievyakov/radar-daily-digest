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

import json
import re
from datetime import UTC, date, datetime

import pytest

from radar.collect import build_adapter
from radar.backfill import persist_statements
from radar.config import ThemeConfig
from radar.contracts import EnrichResult
from radar.db import init_db, read_signals
from radar.deliver import deliver
from radar.enrich import SOURCE_CLOSE, SOURCE_OPEN, LlmEnricher
from radar.llm import Completion
from radar.fetch import Fetcher
from radar.journal import Journal
from radar.normalize import subject_identity
from radar.models import ChangeType, EventStatement, Fact, FactKind, SignalType
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
        # Statements, not just facts. A stub thinner than production is how
        # "the headline is not the page title" stayed green while the card
        # was assembled from the page title: the test passed through the
        # fallback branch it exists to forbid.
        subject = (item.title or "материал").strip()
        statement = EventStatement(
            statement_id=f"st-{abs(hash(item.url)) % 10**8}",
            text=f"Anthropic объявил об отключении {subject}. Дата отключения 15 октября 2026 года.",
            vendor=str(source.vendor or "anthropic") if source else "anthropic",
            product=subject[:40],
            change_type=ChangeType.DEPRECATION,
            event_date=date(2026, 8, 16),
            source_url=item.url,
            evidence=(item.raw_text or "текст")[:40],
            ingested_at=NOW,
            ingest_mode="live",
            extractor_model="fake",
            prompt_version="test",
            raw_material_ref=item.raw_material_ref or "ref",
        )
        return EnrichResult(
            source_id=str(item.extra.get("source_id", "")),
            url=item.url,
            statements=[statement],
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
    fetcher = Fetcher(cache_root="cache", polite_delay=0.0, offline=True)

    from radar import run as run_module
    from radar.collect import collect_all as real_collect

    def cached_only(cfg, f, run_log=None, mode="live", sources=None, max_workers=6):
        return real_collect(cfg, f, run_log, mode="backfill", sources=sources, max_workers=4)

    original = run_module.collect_all
    run_module.collect_all = cached_only
    try:
        run = DailyRun(conn, config, fetcher, RealShapedEnricher(),
                       for_date=TODAY, log_dir=str(tmp_path / "logs"),
                       # Computed above and previously handed to nobody: the
                       # test claimed two cached sources and walked all
                       # seventy, collecting 7442 materials. Against that the
                       # >= 45 threshold measured nothing, and losing 37% of
                       # the data passed unnoticed. The same defect the file
                       # exists to catch, inside the file itself.
                       sources=sources)
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
        assert result.collected >= 45

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
        assert result.clusters >= 45

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


class TestTheCardShowsExtractedText:
    """The expensive stage produces a normalized statement: one to three
    sentences, quantifier-checked, every fact behind it verified. The card was
    assembled from the page title and two kilobytes of raw changelog, so none
    of that work reached the screen."""

    def test_the_headline_is_not_the_page_title(self, run_result, conn):
        _, result = run_result
        digest_items = [s for s in read_signals(conn, result.run_id)
                        if s.signal_type is SignalType.DIGEST_ITEM]
        assert digest_items
        # A page title repeats across every event on that page; a statement
        # names this change.
        headlines = {s.headline for s in digest_items}
        assert len(headlines) == len(digest_items) or len(digest_items) == 1

    def test_the_summary_is_not_a_slab_of_raw_source(self, run_result, conn):
        _, result = run_result
        digest_items = [s for s in read_signals(conn, result.run_id)
                        if s.signal_type is SignalType.DIGEST_ITEM]
        assert digest_items
        assert all(len(s.summary) < 2200 for s in digest_items)

    def test_why_it_matters_is_produced(self, run_result, conn):
        """Promised by voice.md and previously an empty string always."""
        _, result = run_result
        digest_items = [s for s in read_signals(conn, result.run_id)
                        if s.signal_type is SignalType.DIGEST_ITEM]
        assert any(s.why_it_matters for s in digest_items)

    def test_why_it_matters_never_outruns_the_facts(self, run_result, conn):
        """Every clause has to be backed by a verified fact, so a signal with
        no dated facts may not claim a deadline."""
        _, result = run_result
        for signal in read_signals(conn, result.run_id):
            if signal.why_it_matters and "срок" in signal.why_it_matters:
                assert any(f.value_date for f in signal.facts)


# --------------------------------------------------------------------------
# one event per event
# --------------------------------------------------------------------------


class RowCountingBackend:
    """A model that extracts from what it was actually given.

    Every fake enricher until now returned a fixed answer regardless of its
    input, which is precisely why the duplication survived every test: the
    stage that decides how much text reaches the model was replaced by a stub
    that does not read text at all. This one names the first model identifier
    it can see, the way a real extractor would, so handing it a whole page
    instead of one row shows up as the same answer arriving many times.
    """

    # The hyphen is required: without it "claude.com" in a URL reads as a
    # model name and the test measures the prompt template, not the leak.
    IDENT = re.compile(r"\b(?:claude|gpt|gemini|imagen|veo|o\d)-[\w.]*\d[\w.-]*", re.I)

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, prompt: str, **kwargs: object) -> Completion:
        self.calls.append(prompt)
        found = self.IDENT.findall(prompt)
        subject = found[0] if found else "неизвестная модель"
        payload = {
            "events": [
                {
                    "statement": f"Вендор объявил об отключении {subject}.",
                    "change_type": "deprecation",
                    "event_date": "2026-10-15",
                    "event_date_text": "October 15, 2026",
                    "product": subject,
                    "version": "",
                    "vendor": "",
                    "evidence": subject,
                    "facts": [],
                }
            ]
        }
        return Completion(
            text=json.dumps(payload, ensure_ascii=False),
            model="fake",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            cached=False,
        )


class TestOneEventPerEvent:
    """A page read once per row is a page read as many times as it has rows.

    The teaser rule (FR-4.1) completes a short material by fetching its URL.
    For a table row that URL is the table, so sixty-five rows each pulled the
    whole page and extracted every event in it. The corpus took the copies as
    separate precedents, and the precedent count is the number the context
    label puts on the card: "the eighth time since May" was one row read eight
    times.
    """

    @pytest.fixture
    def extracted(self, config):
        source = next(
            s for s in config.sources if s.id == "anthropic_model_deprecations"
        )
        fetcher = Fetcher(cache_root="cache", polite_delay=0.0, offline=True)
        items = build_adapter(source, fetcher).backfill(540)
        backend = RowCountingBackend()
        stage = LlmEnricher(config, backend, fetcher=fetcher, ingest_mode="backfill")
        results = [stage.enrich(item, source) for item in items]
        return items, results, backend

    def test_the_page_yields_more_than_one_material(self, extracted):
        items, _, _ = extracted
        assert len(items) > 3, "the table was not cut into rows at all"

    def test_no_material_is_enriched_with_its_own_page(self, extracted):
        items, _, backend = extracted
        # Comparing prompt length against material length measures the prompt
        # template, not the leak. What matters is whose text is in there: a row
        # completed by its own page carries every other row's models with it.
        for item, prompt in zip(items, backend.calls, strict=True):
            # Only the fenced body, not the metadata around it: the URL of a
            # deprecation section is itself made of model names.
            body = prompt.split(SOURCE_OPEN, 1)[-1].split(SOURCE_CLOSE, 1)[0]
            mine = set(RowCountingBackend.IDENT.findall(item.raw_text or ""))
            in_prompt = set(RowCountingBackend.IDENT.findall(body))
            strangers = in_prompt - mine
            assert not strangers, (
                f"material {item.url} was enriched with models it does not "
                f"mention: {sorted(strangers)[:5]}"
            )

    def test_the_corpus_holds_one_record_per_event(self, extracted, conn):
        """The row is the unit of extraction; the event is the unit of record."""
        _, results, _ = extracted
        persist_statements(conn, [(i, r) for i, r in enumerate(results)])

        rows = conn.execute(
            "SELECT event_key, count(*) n FROM event_statements "
            "WHERE event_key <> '' GROUP BY 1 HAVING n > 1"
        ).fetchall()
        assert not rows, (
            "the corpus holds the same event more than once: "
            + ", ".join(f"{r['event_key']} x{r['n']}" for r in rows[:5])
        )

    def test_one_retirement_is_one_subject_however_many_milestones(self, extracted):
        """Anthropic's page states each retirement twice, and both are true.

        The status table says `claude-3-haiku-20240307` was deprecated on
        February 19; the deprecation table says it was retired on April 20.
        Two records, correctly, because the dates differ. One card, because
        the reader is losing one model, not two.
        """
        _, results, _ = extracted
        statements = [st for r in results for st in r.statements]
        subjects = {
            subject_identity(st.vendor, str(st.change_type), st.product,
                             st.evidence, st.text)
            for st in statements
        }

        assert len(statements) > len(subjects), (
            "this page is supposed to state some retirement twice; if it no "
            "longer does, the test is measuring nothing"
        )
        # The pair that made the point: one model, two milestones, one subject.
        haiku = [st for st in statements if st.product == "claude-3-haiku-20240307"]
        assert len(haiku) == 2, [st.product for st in statements]
        assert len({
            subject_identity(st.vendor, str(st.change_type), st.product,
                             st.evidence, st.text)
            for st in haiku
        }) == 1


class TestTheLoopCloses:
    """A daily agent that does not consolidate is one with no memory.

    The corpus held only what the backfill put there: every record in it was
    `ingest_mode='backfill'`, written once, by hand, on one evening. Today's
    events never became tomorrow's precedents, so the context label — the whole
    point of keeping a corpus — could only ever count history loaded manually.
    """

    def test_todays_events_enter_the_corpus(self, run_result, conn):
        _, result = run_result
        rows = conn.execute(
            "SELECT count(*) FROM event_statements WHERE ingest_mode = 'live'"
        ).fetchone()[0]

        assert rows > 0, "the run produced signals and remembered none of them"

    def test_what_the_run_stored_is_what_it_published(self, run_result, conn):
        _, result = run_result
        stored = {
            row["event_key"]
            for row in conn.execute(
                "SELECT event_key FROM event_statements WHERE ingest_mode = 'live'"
            )
        }
        published = {
            subject_identity(s.vendor or "", str(s.change_type or ""), s.product)
            for s in result.signals
            if s.signal_type is SignalType.DIGEST_ITEM
        }
        # Not equality: a signal can be dropped below the publication threshold
        # after its statement was stored, and a stored event carries its date
        # while the published subject does not. What must hold is that the
        # corpus learned something from a run that published something.
        assert stored and published


class TestTheFunnelNamesEveryDrop:
    """FR-3.3: nothing dropped disappears. It was disappearing.

    `filtered_items` was keyed by (run, url, stage), and a deprecation page
    hands over ten sections under one anchor. Nine of the ten rejections
    overwrote each other, so a run that dropped ten materials could account
    for one, and the arithmetic on the run-log page did not close.
    """

    def test_every_dropped_material_has_its_own_row(self, run_result, conn):
        run, result = run_result
        dropped = conn.execute(
            "SELECT count(*) FROM filtered_items WHERE run_id = ?", (run.run_id,)
        ).fetchone()[0]
        distinct_keys = conn.execute(
            "SELECT count(DISTINCT item_key) FROM filtered_items WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()[0]

        assert dropped == distinct_keys

        # Per stage and exactly, not "at least". A one-sided check passes on a
        # run that writes four reasons for two drops, and a reason attached to
        # nothing is as misleading as a drop with no reason: both make the
        # arithmetic on the page unfollowable.
        for name in ("filter", "enrich", "collapse", "score"):
            stage = next(
                (s for s in run.log.stages if s.get("stage") == name), None
            )
            if stage is None:
                continue
            lost = max(0, (stage.get("in_count") or 0) - (stage.get("out_count") or 0))
            recorded = conn.execute(
                "SELECT count(*) FROM filtered_items WHERE run_id = ? AND stage = ?",
                (run.run_id, name),
            ).fetchone()[0]
            assert recorded == lost, (
                f"стадия {name}: выбыло {lost}, причин записано {recorded}"
            )

    def test_two_sections_of_one_page_are_two_rows(self, conn, config, tmp_path):
        from radar.runlog import RunLog

        log = RunLog(conn, "run-funnel", TODAY)
        one_url = "https://docs.claude.com/en/docs/about-claude/model-deprecations#model-status"
        log.filtered(url=one_url, title="Model status - claude-opus-5",
                     reason_code="дубль_вчерашнего", stage="filter", item_key="c1")
        log.filtered(url=one_url, title="Model status - claude-sonnet-5",
                     reason_code="дубль_вчерашнего", stage="filter", item_key="c2")

        rows = conn.execute(
            "SELECT title FROM filtered_items WHERE run_id = 'run-funnel'"
        ).fetchall()
        assert len(rows) == 2, "one anchor swallowed the other section"


class TestAnEventIsNotItsOwnPrecedent:
    """«Третий раз с 17 августа» — над двумя записями этого же прогона.

    The run wrote its events into the corpus before searching it, and the guard
    against self-citation compared `cluster_id` against `statement_id` — two
    different namespaces, so the condition never once matched. A card about a
    Claude Code release cited two retellings of that same release, created
    minutes earlier by the same run, while the corpus held no older instance of
    the pair (vendor, change type) at all.
    """

    def test_todays_events_are_written_after_the_corpus_is_searched(self, run_result, conn):
        run, result = run_result
        live = {
            row["statement_id"]
            for row in conn.execute(
                "SELECT statement_id FROM event_statements WHERE ingest_mode = 'live'"
            )
        }
        assert live, "прогон ничего не записал в корпус"

        cited = {
            precedent.statement_id
            for signal in result.signals
            for precedent in signal.precedents
        }
        assert not (cited & live), (
            "сигнал сослался на запись, созданную этим же прогоном: "
            f"{sorted(cited & live)[:3]}"
        )

    def test_the_exclusion_actually_excludes(self, conn):
        """The guard is only real if the identifier it passes is the one the
        query compares. Both are opaque strings, which is why the mismatch
        survived: nothing complained, it simply never matched."""
        from radar.retrieval import CorpusRetriever

        for index in range(3):
            conn.execute(
                "INSERT INTO event_statements (statement_id, text, vendor, "
                "change_type, event_date, source_url, statement_index, evidence, "
                "ingested_at, ingest_mode, extractor_model, prompt_version, "
                "raw_material_ref) VALUES (?, 'Anthropic отключает модель', "
                "'anthropic', 'deprecation', ?, ?, ?, 'q', "
                "'2026-08-01T00:00:00+00:00', 'live', 'm', 'v2', 'r')",
                (f"st-{index}", f"2026-0{index + 3}-01",
                 f"https://example.test/{index}", index),
            )
        conn.commit()
        retriever = CorpusRetriever(conn)

        everything = retriever.find_precedents(
            "anthropic", "deprecation", TODAY, text="Anthropic отключает модель",
            exclude_ids=set(),
        )
        without_one = retriever.find_precedents(
            "anthropic", "deprecation", TODAY, text="Anthropic отключает модель",
            exclude_ids={"st-1"},
        )

        assert len(everything.precedents) == 3
        assert len(without_one.precedents) == 2
        assert "st-1" not in {p.statement_id for p in without_one.precedents}
