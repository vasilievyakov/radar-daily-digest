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
    DatePrecision,
    RetrievalReport,
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
    choose_due_date,
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
    return Cluster(
        cluster_id=cluster_id,
        dedup_key="k",
        items=[item],
        vendor="anthropic",
        change_type="deprecation",
    )


def precedent(sid, when):
    return Precedent(
        statement_id=sid,
        text="Anthropic retired a model",
        source_url=f"https://example.test/{sid}",
        vendor="anthropic",
        change_type=ChangeType.DEPRECATION,
        event_date=when,
    )


def add_statement(
    conn, sid, vendor, change_type, event_date, product=None, index=0, text=None
):
    conn.execute(
        "INSERT INTO event_statements (statement_id, text, vendor, product, change_type, "
        "event_date, source_url, statement_index, evidence, ingested_at, ingest_mode, "
        "extractor_model, prompt_version, raw_material_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            text or f"{vendor} отключает {product or 'модель'}",
            vendor,
            product,
            change_type,
            event_date,
            f"https://example.test/{sid}",
            index,
            "q",
            NOW.isoformat(),
            "backfill",
            "m",
            "v1",
            "r",
        ),
    )
    conn.commit()


# Verbatim from data/radar.db: one Google shutdown, extracted seven times over
# from three pages, each pass wording it differently and disagreeing about the
# product. This is what the quiet-day block actually had to render.
ROBOTICS_WORDINGS = [
    (
        None,
        "Google запланировала отключение модели gemini-robotics-er-1.6-preview "
        "31 августа 2026 года.",
    ),
    (
        None,
        "Google объявила об отключении предварительной модели робототехники "
        "gemini-robotics-er-1.6-preview с датой завершения 31 августа 2026 года.",
    ),
    (
        "Gemini API",
        "Google объявила о прекращении поддержки модели "
        "gemini-robotics-er-1.6-preview с датой отключения 31 августа 2026 года.",
    ),
    (
        None,
        "Google объявляет о прекращении поддержки предпросмотровой модели robotics "
        "gemini-robotics-er-1.6-preview 31 августа 2026 года.",
    ),
    (
        "Gemini Robotics API",
        "Google прекратит поддержку модели "
        "gemini-robotics-er-1.6-preview в API Gemini 31 августа 2026 года.",
    ),
    (
        "Gemini API",
        "Google объявила о закрытии модели gemini-robotics-er-1.6-preview "
        "31 августа 2026 года.",
    ),
    (
        "Gemini API",
        "Google объявила об отключении модели gemini-robotics-er-1.6-preview "
        "в API Gemini 31 августа 2026 года.",
    ),
]


def add_robotics_shutdown(conn, offset=0):
    for position, (product, text) in enumerate(ROBOTICS_WORDINGS):
        add_statement(
            conn,
            f"rob-{offset + position}",
            "google",
            "deprecation",
            "2026-08-31",
            product=product,
            index=position,
            text=text,
        )


class TestContextNote:
    def test_two_precedents_produce_a_countable_sentence(self):
        note = build_context_note(
            ContextLabel.RECURRING,
            [precedent("s1", date(2026, 5, 12)), precedent("s2", date(2026, 7, 1))],
            "Anthropic",
            "Объявление об отключении",
            TODAY,
        )
        assert "третий раз" in note
        assert "12 мая" in note

    def test_a_single_precedent_yields_no_claim(self):
        """Below the evidence threshold there is nothing to assert."""
        assert (
            build_context_note(
                ContextLabel.RECURRING,
                [precedent("s1", date(2026, 5, 12))],
                "Anthropic",
                "x",
                TODAY,
            )
            is None
        )

    def test_absence_of_precedents_yields_no_claim(self):
        assert (
            build_context_note(ContextLabel.NOT_FOUND_IN_CORPUS, [], "A", "x", TODAY)
            is None
        )

    def test_the_sentence_never_uses_a_quantifier_without_a_number(self):
        """FR-6.18: 'вендор всё чаще' is banned, 'третий раз с мая' is not."""
        for label in (
            ContextLabel.RECURRING,
            ContextLabel.ESCALATION,
            ContextLabel.TREND_MEMBER,
        ):
            note = build_context_note(
                label,
                [precedent("s1", date(2026, 5, 12)), precedent("s2", date(2026, 7, 1))],
                "Anthropic",
                "Объявление об отключении",
                TODAY,
            )
            assert find_unsupported_quantifiers(note) == []

    def test_an_older_year_is_named_explicitly(self):
        note = build_context_note(
            ContextLabel.RECURRING,
            [precedent("s1", date(2025, 5, 12)), precedent("s2", date(2026, 7, 1))],
            "Anthropic",
            "x",
            TODAY,
        )
        assert "2025 года" in note

    def test_escalation_reads_differently(self):
        note = build_context_note(
            ContextLabel.ESCALATION,
            [precedent("s1", date(2026, 5, 12)), precedent("s2", date(2026, 7, 1))],
            "Anthropic",
            "x",
            TODAY,
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
        fact = Fact(
            kind=FactKind.SUNSET_DATE,
            value="2026-10-15",
            source_url="https://example.test/a",
            evidence="retired on October 15",
            value_date=date(2026, 10, 15),
            subject="claude-3-opus",
        )
        retrieval = RetrievalResult(
            hits=[
                RetrievalHit(
                    "s1",
                    "t",
                    "https://example.test/s1",
                    "anthropic",
                    "deprecation",
                    date(2026, 5, 12),
                    "day",
                    "q",
                ),
                RetrievalHit(
                    "s2",
                    "t",
                    "https://example.test/s2",
                    "anthropic",
                    "deprecation",
                    date(2026, 7, 1),
                    "day",
                    "q",
                ),
            ]
        )
        signal = build_signal(
            "run-1",
            TODAY,
            make_cluster(),
            [fact],
            DeltaOutcome("c1", DeltaStatus.NEW),
            retrieval,
            88,
            "почему",
            Tier.LEAD,
            1,
            headline="Anthropic отключает claude-3-opus",
            summary="Полный текст.",
            vendor_label="Anthropic",
            change_type_label="Объявление об отключении",
        )
        assert signal.facts[0].value_date == date(2026, 10, 15)
        assert len(signal.precedents) == 2
        assert signal.context_label is ContextLabel.RECURRING
        assert "третий раз" in signal.context_note

    def test_a_thin_retrieval_downgrades_the_label(self):
        retrieval = RetrievalResult(
            hits=[
                RetrievalHit(
                    "s1",
                    "t",
                    "u",
                    "anthropic",
                    "deprecation",
                    date(2026, 5, 12),
                    "day",
                    "q",
                )
            ]
        )
        signal = build_signal(
            "run-1",
            TODAY,
            make_cluster(),
            [],
            None,
            retrieval,
            50,
            "r",
            Tier.STANDARD,
            1,
            headline="h",
            summary="s",
        )
        assert signal.context_label is ContextLabel.NOT_FOUND_IN_CORPUS
        assert signal.context_note is None

    def test_nothing_is_truncated_or_marked_up(self):
        """PUB-2 and SIG-1: shaping belongs to the surface."""
        long_summary = "Очень длинный текст. " * 200
        signal = build_signal(
            "run-1",
            TODAY,
            make_cluster(),
            [],
            None,
            None,
            50,
            "r",
            Tier.STANDARD,
            1,
            headline="h",
            summary=long_summary,
        )
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
        summary = build_run_summary(
            outcomes, 40, 23, name_of={"cursor_changelog": "Cursor changelog"}
        )
        assert summary.sources_failed == ["Cursor changelog"]
        assert summary.sources_empty == ["mcp_servers"]
        assert summary.sources_checked == 3

    def test_empty_is_reported_apart_from_failed(self):
        """HTTP 200 with nothing extractable is a different fault."""
        outcomes = [
            SourceOutcome("a", SourceStatus.EMPTY),
            SourceOutcome("b", SourceStatus.FAILED),
        ]
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
        add_statement(
            db, "s1", "anthropic", "deprecation", "2026-10-15", product="claude-3-opus"
        )
        add_statement(
            db, "s2", "openai", "deprecation", "2026-11-01", product="gpt-4o", index=1
        )
        signal = build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0))
        assert [u.when for u in signal.upcoming] == [
            date(2026, 10, 15),
            date(2026, 11, 1),
        ]

    def test_past_deadlines_do_not_appear(self, db):
        add_statement(db, "s1", "anthropic", "deprecation", "2026-01-01")
        assert (
            build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0)).upcoming
            == []
        )

    def test_deadlines_beyond_the_horizon_do_not_appear(self, db):
        add_statement(db, "s1", "anthropic", "deprecation", "2027-06-01")
        assert (
            build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0)).upcoming
            == []
        )

    def test_the_same_deadline_is_not_listed_twice(self, db):
        add_statement(
            db, "s1", "anthropic", "deprecation", "2026-10-15", product="claude-3-opus"
        )
        add_statement(
            db,
            "s2",
            "anthropic",
            "deprecation",
            "2026-10-15",
            product="claude-3-opus",
            index=1,
        )
        assert len(collect_upcoming(db, TODAY)) == 1

    def test_an_empty_corpus_yields_no_block(self, db):
        assert (
            build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0)).upcoming
            == []
        )


class TestUpcomingDeduplication:
    """The block has three slots and the corpus holds every event many times.

    Keying on the product was not enough: the same shutdown is stored with
    product `Gemini API`, product `Gemini Robotics API` and product NULL, so on
    the live corpus all three slots went to one event. What the wordings agree
    about is the date, the vendor and the identifier they name.
    """

    def test_one_shutdown_worded_seven_ways_is_one_deadline(self, db):
        add_robotics_shutdown(db)
        upcoming = collect_upcoming(db, TODAY)
        assert len(upcoming) == 1
        assert "gemini-robotics-er-1.6-preview" in upcoming[0].what

    def test_duplicates_do_not_crowd_out_the_next_deadline(self, db):
        """Reading only three rows meant never reaching the second event."""
        add_robotics_shutdown(db)
        add_statement(
            db,
            "img-1",
            "google",
            "deprecation",
            "2026-10-02",
            product="Gemini API",
            text="Google объявила о закрытии модели gemini-2.5-flash-image "
            "2 октября 2026 года.",
        )
        assert [u.when for u in collect_upcoming(db, TODAY)] == [
            date(2026, 8, 31),
            date(2026, 10, 2),
        ]

    def test_two_models_retiring_on_one_day_stay_two_deadlines(self, db):
        add_statement(
            db,
            "d1",
            "anthropic",
            "deprecation",
            "2026-10-15",
            text="Anthropic отключает claude-3-opus-20240229 15 октября 2026 года.",
        )
        add_statement(
            db,
            "d2",
            "anthropic",
            "deprecation",
            "2026-10-15",
            index=1,
            text="Anthropic отключает claude-3-haiku-20240307 15 октября 2026 года.",
        )
        assert len(collect_upcoming(db, TODAY)) == 2

    def test_a_wording_that_names_nothing_is_not_a_second_deadline(self, db):
        """«стабильная модель Gemini 2.5 Flash» names no identifier.

        Unidentifiable prose cannot be told apart from its neighbours, so it
        folds into the group rather than standing beside it as another copy.
        """
        add_statement(
            db,
            "img-1",
            "google",
            "deprecation",
            "2026-10-02",
            text="Google объявила о закрытии модели gemini-2.5-flash-image "
            "2 октября 2026 года.",
        )
        add_statement(
            db,
            "img-2",
            "google",
            "deprecation",
            "2026-10-02",
            index=1,
            text="Google прекращает поддержку стабильной модели Gemini 2.5 Flash "
            "для работы с изображениями 2 октября 2026 года.",
        )
        assert len(collect_upcoming(db, TODAY)) == 1

    def test_the_wording_kept_is_the_same_on_every_rerun(self, db):
        add_robotics_shutdown(db)
        first = collect_upcoming(db, TODAY)
        second = collect_upcoming(db, TODAY)
        assert [u.what for u in first] == [u.what for u in second]


class TestRunFailure:
    def test_a_dead_run_reports_itself(self):
        signal = build_run_failure(
            "run-1", TODAY, "enrich", "таймаут модели", build_run_summary([], 34, 0)
        )
        assert signal.signal_type is SignalType.RUN_FAILURE
        assert signal.failure_stage == "enrich"
        assert "17 августа" in signal.headline

    def test_failure_differs_from_a_quiet_day(self, db):
        quiet = build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0))
        failure = build_run_failure(
            "run-1", TODAY, "enrich", "x", build_run_summary([], 0, 0)
        )
        assert quiet.signal_id != failure.signal_id
        assert quiet.signal_type is not failure.signal_type


class TestFactsFallback:
    def test_future_dated_facts_become_deadlines(self):
        facts = [
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                source_url="u",
                evidence="q",
                value_date=date(2026, 10, 15),
                subject="claude-3-opus",
            ),
            Fact(kind=FactKind.VERSION, value="4.1", source_url="u", evidence="q"),
        ]
        upcoming = facts_to_upcoming(facts, TODAY)
        assert [u.what for u in upcoming] == ["claude-3-opus"]

    def test_a_fact_without_a_parsed_date_is_skipped(self):
        facts = [
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="дата не указана",
                source_url="u",
                evidence="q",
            )
        ]
        assert facts_to_upcoming(facts, TODAY) == []


class TestPersistence:
    def test_a_whole_run_round_trips_through_the_store(self, db):
        signals = [
            build_signal(
                "run-1",
                TODAY,
                make_cluster("c1"),
                [],
                None,
                None,
                90,
                "r",
                Tier.LEAD,
                1,
                headline="Первый",
                summary="s",
            ),
            build_quiet_day(db, "run-1", TODAY, build_run_summary([], 0, 0)),
        ]
        publish_signals(db, "run-1", signals)
        restored = read_signals(db, "run-1")
        assert len(restored) == 2
        assert restored[0].headline


class TestRecurrenceIsNotClaimedForRoutine:
    """A vendor ships releases. Saying so seventeen times over is not context.

    The trends stage already excludes RELEASE and OTHER from recurrence — a
    change a vendor makes every few days carries no information by repeating,
    and the readiness report prints exactly that. The card did not know it, so
    sixteen bullet points of one Claude Code changelog, stored as sixteen
    records of type "other", produced "Anthropic: other, the 17th time since
    August 17".
    """

    def _precedents(self, n, change_type=ChangeType.OTHER):
        return [
            Precedent(
                statement_id=f"p{i}",
                text=f"пункт {i}",
                source_url="https://example.test/changelog",
                event_date=date(2026, 8, 17),
                vendor="anthropic",
                change_type=change_type,
            )
            for i in range(n)
        ]

    def test_no_sentence_for_a_routine_type(self):
        note = build_context_note(
            ContextLabel.RECURRING,
            self._precedents(16),
            "Anthropic",
            "прочее",
            date(2026, 8, 18),
            total_found=16,
            change_type=ChangeType.OTHER,
        )
        assert note is None

    def test_no_sentence_for_releases_either(self):
        note = build_context_note(
            ContextLabel.RECURRING,
            self._precedents(4, ChangeType.RELEASE),
            "Google",
            "релиз",
            date(2026, 8, 18),
            total_found=4,
            change_type=ChangeType.RELEASE,
        )
        assert note is None

    def test_a_deprecation_still_gets_one(self):
        note = build_context_note(
            ContextLabel.RECURRING,
            self._precedents(3, ChangeType.DEPRECATION),
            "AWS Bedrock",
            "объявление об отключении",
            date(2026, 8, 18),
            total_found=13,
            change_type=ChangeType.DEPRECATION,
        )
        assert note and "14-й раз" in note


class TestOneDatePerCard:
    """Three consumers picked the card's date three ways.

    The page took the first dated fact in list order, scoring took the nearest
    one still ahead, the core took a third. They disagreed on fifteen cards of
    thirty-four, and a card about today's announcement carried a deadline
    belonging to a neighbouring row of the same table.
    """

    @staticmethod
    def _fact(kind, value, when, precision=DatePrecision.DAY):
        return Fact(
            kind=kind, value=value, source_url="https://example.test/x",
            evidence=value, value_date=when, date_precision=precision,
            evidence_verified=True,
        )

    def test_the_nearest_obligation_ahead_wins(self):
        facts = [
            self._fact(FactKind.SUNSET_DATE, "2027-05-01", date(2027, 5, 1)),
            self._fact(FactKind.EFFECTIVE_DATE, "2026-09-01", date(2026, 9, 1)),
            self._fact(FactKind.SUNSET_DATE, "2024-01-01", date(2024, 1, 1)),
        ]
        assert choose_due_date(facts, date(2026, 8, 18))[0] == date(2026, 9, 1)

    def test_a_passed_deadline_is_still_the_news(self):
        facts = [
            self._fact(FactKind.SUNSET_DATE, "2024-01-01", date(2024, 1, 1)),
            self._fact(FactKind.SUNSET_DATE, "2026-08-15", date(2026, 8, 15)),
        ]
        # Nothing ahead: the most recent passed date, not the oldest one. The
        # card said "expired 649 days ago" under a headline about this week.
        assert choose_due_date(facts, date(2026, 8, 18))[0] == date(2026, 8, 15)

    def test_an_inferred_date_never_leads(self):
        facts = [
            self._fact(FactKind.SUNSET_DATE, "2026-09-01", date(2026, 9, 1),
                       DatePrecision.INFERRED),
        ]
        assert choose_due_date(facts, date(2026, 8, 18))[0] is None

    def test_a_card_with_no_dates_says_so(self):
        assert choose_due_date([], date(2026, 8, 18)) == (None, DatePrecision.DAY)

    def test_the_signal_carries_the_choice(self):
        from radar.cluster import Cluster
        from radar.adapters.base import CollectedItem

        item = CollectedItem(url="https://example.test/x", title="t", raw_text="")
        cluster = Cluster(cluster_id="c1", dedup_key="d1", items=[item],
                          vendor="anthropic", change_type="deprecation")
        facts = [
            self._fact(FactKind.SUNSET_DATE, "2026-10-15", date(2026, 10, 15)),
            self._fact(FactKind.EFFECTIVE_DATE, "2026-09-01", date(2026, 9, 1)),
        ]
        signal = build_signal(
            "run-1", date(2026, 8, 18), cluster, facts, None, None,
            score=0, rationale="", tier=Tier.STANDARD, rank=1,
            headline="заголовок", summary="текст",
        )
        assert signal.due_date == date(2026, 9, 1)


class TestPrecedentsAreCheckedBeforeTheyAreCounted:
    """The fifteenth guard found written and never called.

    Precedents reach the card and the number in "the twentieth time since
    February". One from another vendor, another change type, or the same record
    twice is a false number on a page whose whole claim is that its numbers can
    be checked. The query is not the only thing that can be wrong about them —
    and the check belongs where it cannot be skipped.
    """

    @staticmethod
    def _hit(statement_id, vendor="anthropic", change_type="deprecation"):
        from radar.retrieval import RetrievalHit

        return RetrievalHit(
            statement_id=statement_id, text="Anthropic отключает модель",
            source_url="https://example.test/x", vendor=vendor,
            change_type=change_type, event_date=date(2026, 3, 1),
            date_precision="day", evidence="q",
        )

    def _signal(self, precedents):
        from radar.cluster import Cluster
        from radar.adapters.base import CollectedItem
        from radar.retrieval import RetrievalResult

        item = CollectedItem(url="https://example.test/x", title="t", raw_text="")
        cluster = Cluster(cluster_id="c1", dedup_key="d1", items=[item],
                          vendor="anthropic", change_type="deprecation")
        report = RetrievalReport(
            strict_hits=len(precedents), relaxed_hits=len(precedents),
            total_found=len(precedents), shown=len(precedents),
        )
        retrieval = RetrievalResult(hits=precedents, report=report)
        return build_signal(
            "run-1", date(2026, 8, 18), cluster, [], None, retrieval,
            score=0, rationale="", tier=Tier.STANDARD, rank=1,
            headline="заголовок", summary="текст",
        )

    def test_a_precedent_from_another_vendor_is_dropped(self):
        signal = self._signal([
            self._hit("a"), self._hit("b"),
            self._hit("c", vendor="google"),
        ])
        assert [p.statement_id for p in signal.precedents] == ["a", "b"]

    def test_the_same_record_twice_is_counted_once(self):
        signal = self._signal([
            self._hit("a"), self._hit("a"), self._hit("b"),
        ])
        assert [p.statement_id for p in signal.precedents] == ["a", "b"]

    def test_the_printed_number_drops_with_them(self):
        clean = self._signal([self._hit(x) for x in "abc"])
        dirty = self._signal([
            self._hit("a"), self._hit("b"), self._hit("c"),
            self._hit("d", change_type="release"),
        ])
        assert clean.retrieval.total_found == dirty.retrieval.total_found == 3
        assert clean.context_note == dirty.context_note


class TestAPatternNeedsAnInterval:
    """«Третий раз с 17 августа», сказанное семнадцатого августа.

    The records behind such a sentence were all written that morning, usually
    from one page: it is a claim about repetition containing no time. Measured
    on the last run, four of twenty-five recurrence sentences rested on
    precedents sharing a single day.
    """

    @staticmethod
    def _precedents(day):
        return [
            Precedent(
                statement_id=f"p{i}", text="Anthropic отключает модель",
                source_url=f"https://example.test/{i}", event_date=day,
                vendor="anthropic", change_type=ChangeType.DEPRECATION,
            )
            for i in range(3)
        ]

    def test_todays_records_do_not_make_a_pattern(self):
        today = date(2026, 8, 18)
        note = build_context_note(
            ContextLabel.RECURRING, self._precedents(today), "Anthropic",
            "объявление об отключении", today, total_found=3,
            earliest_match=today, change_type=ChangeType.DEPRECATION,
        )
        assert note is None

    def test_a_corpus_reaching_back_still_speaks(self):
        note = build_context_note(
            ContextLabel.RECURRING, self._precedents(date(2026, 6, 2)),
            "Anthropic", "объявление об отключении", date(2026, 8, 18),
            total_found=3, earliest_match=date(2026, 6, 2),
            change_type=ChangeType.DEPRECATION,
        )
        assert note and "2 июня" in note
