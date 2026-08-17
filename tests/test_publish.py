from datetime import UTC, date, datetime

import pytest

from radar.adapters.base import CollectedItem
from radar.assertions import find_unsupported_quantifiers
from radar.cluster import Cluster
from radar.collect import SourceOutcome
from radar.db import init_db, publish_signals, read_signals
from radar.delta import DeltaOutcome
from radar.models import (
    ChangeType,
    ContextLabel,
    DeltaStatus,
    Fact,
    FactKind,
    Precedent,
    SignalType,
    SourceStatus,
    Tier,
)
from radar.publish import (
    build_context_note,
    build_quiet_day,
    build_run_failure,
    build_run_summary,
    build_signal,
    collect_upcoming,
    facts_to_upcoming,
    make_signal_id,
)
from radar.retrieval import RetrievalResult, RetrievalHit

NOW = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
TODAY = date(2026, 8, 17)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    yield conn
    conn.close()


def make_cluster(cluster_id="c1"):
    item = CollectedItem(
        url="https://docs.claude.com/deprecations#opus",
        title="Anthropic отключает claude-3-opus",
        raw_text="body",
    )
    return Cluster(cluster_id=cluster_id, dedup_key="k", items=[item],
                   vendor="anthropic", change_type="deprecation")


def precedent(sid, when):
    return Precedent(statement_id=sid, text="Anthropic retired a model",
                     source_url=f"https://example.test/{sid}", vendor="anthropic",
                     change_type=ChangeType.DEPRECATION, event_date=when)


def add_statement(conn, sid, vendor, change_type, event_date, product=None, index=0):
    conn.execute(
        "INSERT INTO event_statements (statement_id, text, vendor, product, change_type, "
        "event_date, source_url, statement_index, evidence, ingested_at, ingest_mode, "
        "extractor_model, prompt_version, raw_material_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, f"{vendor} отключает {product or 'модель'}", vendor, product, change_type,
         event_date, f"https://example.test/{sid}", index, "q", NOW.isoformat(),
         "backfill", "m", "v1", "r"),
    )
    conn.commit()


class TestContextNote:
    def test_two_precedents_produce_a_countable_sentence(self):
        note = build_context_note(
            ContextLabel.RECURRING,
            [precedent("s1", date(2026, 5, 12)), precedent("s2", date(2026, 7, 1))],
            "Anthropic", "Объявление об отключении", TODAY,
        )
        assert "третий раз" in note
        assert "12 мая" in note

    def test_a_single_precedent_yields_no_claim(self):
        """Below the evidence threshold there is nothing to assert."""
        assert build_context_note(
            ContextLabel.RECURRING, [precedent("s1", date(2026, 5, 12))],
            "Anthropic", "x", TODAY,
        ) is None

    def test_absence_of_precedents_yields_no_claim(self):
        assert build_context_note(ContextLabel.NOT_FOUND_IN_CORPUS, [], "A", "x", TODAY) is None

    def test_the_sentence_never_uses_a_quantifier_without_a_number(self):
        """FR-6.18: 'вендор всё чаще' is banned, 'третий раз с мая' is not."""
        for label in (ContextLabel.RECURRING, ContextLabel.ESCALATION, ContextLabel.TREND_MEMBER):
            note = build_context_note(
                label,
                [precedent("s1", date(2026, 5, 12)), precedent("s2", date(2026, 7, 1))],
                "Anthropic", "Объявление об отключении", TODAY,
            )
            assert find_unsupported_quantifiers(note) == []

    def test_an_older_year_is_named_explicitly(self):
        note = build_context_note(
            ContextLabel.RECURRING,
            [precedent("s1", date(2025, 5, 12)), precedent("s2", date(2026, 7, 1))],
            "Anthropic", "x", TODAY,
        )
        assert "2025 года" in note

    def test_escalation_reads_differently(self):
        note = build_context_note(
            ContextLabel.ESCALATION,
            [precedent("s1", date(2026, 5, 12)), precedent("s2", date(2026, 7, 1))],
            "Anthropic", "x", TODAY,
        )
        assert "ужесточилось" in note


class TestSignalIdentity:
    def test_the_id_is_stable_across_reruns(self):
        """PUB-5: a regenerated run must not resurface everything as unread."""
        assert make_signal_id("run-1", "c1") == make_signal_id("run-1", "c1")

    def test_different_clusters_get_different_ids(self):
        assert make_signal_id("run-1", "c1") != make_signal_id("run-1", "c2")

    def test_different_runs_get_different_ids(self):
        assert make_signal_id("run-1", "c1") != make_signal_id("run-2", "c1")


class TestBuildSignal:
    def test_a_signal_carries_the_evidence_base(self):
        fact = Fact(kind=FactKind.SUNSET_DATE, value="2026-10-15",
                    source_url="https://example.test/a", evidence="retired on October 15",
                    value_date=date(2026, 10, 15), subject="claude-3-opus")
        retrieval = RetrievalResult(hits=[
            RetrievalHit("s1", "t", "https://example.test/s1", "anthropic", "deprecation",
                         date(2026, 5, 12), "day", "q"),
            RetrievalHit("s2", "t", "https://example.test/s2", "anthropic", "deprecation",
                         date(2026, 7, 1), "day", "q"),
        ])
        signal = build_signal(
            "run-1", TODAY, make_cluster(), [fact],
            DeltaOutcome("c1", DeltaStatus.NEW), retrieval, 88, "почему", Tier.LEAD, 1,
            headline="Anthropic отключает claude-3-opus", summary="Полный текст.",
            vendor_label="Anthropic", change_type_label="Объявление об отключении",
        )
        assert signal.facts[0].value_date == date(2026, 10, 15)
        assert len(signal.precedents) == 2
        assert signal.context_label is ContextLabel.RECURRING
        assert "третий раз" in signal.context_note

    def test_a_thin_retrieval_downgrades_the_label(self):
        retrieval = RetrievalResult(hits=[
            RetrievalHit("s1", "t", "u", "anthropic", "deprecation", date(2026, 5, 12), "day", "q")
        ])
        signal = build_signal("run-1", TODAY, make_cluster(), [], None, retrieval,
                              50, "r", Tier.STANDARD, 1, headline="h", summary="s")
        assert signal.context_label is ContextLabel.NOT_FOUND_IN_CORPUS
        assert signal.context_note is None

    def test_nothing_is_truncated_or_marked_up(self):
        """PUB-2 and SIG-1: shaping belongs to the surface."""
        long_summary = "Очень длинный текст. " * 200
        signal = build_signal("run-1", TODAY, make_cluster(), [], None, None,
                              50, "r", Tier.STANDARD, 1,
                              headline="h", summary=long_summary)
        assert signal.summary == long_summary
        assert "<" not in signal.summary and "*" not in signal.summary


class TestRunSummary:
    def test_failed_sources_travel_by_name(self):
        """A surface cannot read source_runs, so names must be in the contract."""
        outcomes = [
            SourceOutcome("cursor_changelog", SourceStatus.FAILED, error="HTTP 500"),
            SourceOutcome("mcp_servers", SourceStatus.EMPTY),
            SourceOutcome("anthropic_api", SourceStatus.OK),
        ]
        summary = build_run_summary(outcomes, 40, 23, name_of={"cursor_changelog": "Cursor changelog"})
        assert summary.sources_failed == ["Cursor changelog"]
        assert summary.sources_empty == ["mcp_servers"]
        assert summary.sources_checked == 3

    def test_empty_is_reported_apart_from_failed(self):
        """HTTP 200 with nothing extractable is a different fault."""
        outcomes = [SourceOutcome("a", SourceStatus.EMPTY), SourceOutcome("b", SourceStatus.FAILED)]
        summary = build_run_summary(outcomes, 0, 0)
        assert summary.sources_empty == ["a"]
        assert summary.sources_failed == ["b"]


class TestQuietDay:
    def test_a_quiet_day_is_a_record_not_silence(self, db):
        """PUB-4: absence of signals reaches the store."""
        signal = build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 23))
        assert signal.signal_type is SignalType.QUIET_DAY
        assert signal.headline

    def test_silence_is_filled_with_what_the_reader_forgot(self, db):
        add_statement(db, "s1", "anthropic", "deprecation", "2026-10-15", product="claude-3-opus")
        add_statement(db, "s2", "openai", "deprecation", "2026-11-01", product="gpt-4o", index=1)
        signal = build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0))
        assert [u.when for u in signal.upcoming] == [date(2026, 10, 15), date(2026, 11, 1)]

    def test_past_deadlines_do_not_appear(self, db):
        add_statement(db, "s1", "anthropic", "deprecation", "2026-01-01")
        assert build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0)).upcoming == []

    def test_deadlines_beyond_the_horizon_do_not_appear(self, db):
        add_statement(db, "s1", "anthropic", "deprecation", "2027-06-01")
        assert build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0)).upcoming == []

    def test_the_same_deadline_is_not_listed_twice(self, db):
        add_statement(db, "s1", "anthropic", "deprecation", "2026-10-15", product="claude-3-opus")
        add_statement(db, "s2", "anthropic", "deprecation", "2026-10-15", product="claude-3-opus", index=1)
        assert len(collect_upcoming(db, TODAY)) == 1

    def test_an_empty_corpus_yields_no_block(self, db):
        assert build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0)).upcoming == []


class TestRunFailure:
    def test_a_dead_run_reports_itself(self):
        signal = build_run_failure("run-1", TODAY, "enrich", "таймаут модели",
                                   build_run_summary([], 34, 0))
        assert signal.signal_type is SignalType.RUN_FAILURE
        assert signal.failure_stage == "enrich"
        assert "17 августа" in signal.headline

    def test_failure_differs_from_a_quiet_day(self, db):
        quiet = build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0))
        failure = build_run_failure("run-1", TODAY, "enrich", "x", build_run_summary([], 0, 0))
        assert quiet.signal_id != failure.signal_id
        assert quiet.signal_type is not failure.signal_type


class TestFactsFallback:
    def test_future_dated_facts_become_deadlines(self):
        facts = [
            Fact(kind=FactKind.SUNSET_DATE, value="2026-10-15", source_url="u", evidence="q",
                 value_date=date(2026, 10, 15), subject="claude-3-opus"),
            Fact(kind=FactKind.VERSION, value="4.1", source_url="u", evidence="q"),
        ]
        upcoming = facts_to_upcoming(facts, TODAY)
        assert [u.what for u in upcoming] == ["claude-3-opus"]

    def test_a_fact_without_a_parsed_date_is_skipped(self):
        facts = [Fact(kind=FactKind.SUNSET_DATE, value="дата не указана",
                      source_url="u", evidence="q")]
        assert facts_to_upcoming(facts, TODAY) == []


class TestPersistence:
    def test_a_whole_run_round_trips_through_the_store(self, db):
        signals = [
            build_signal("run-1", TODAY, make_cluster("c1"), [], None, None, 90, "r",
                         Tier.LEAD, 1, headline="Первый", summary="s"),
            build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0)),
        ]
        publish_signals(db, "run-1", signals)
        restored = read_signals(db, "run-1")
        assert len(restored) == 2
        assert restored[0].headline
