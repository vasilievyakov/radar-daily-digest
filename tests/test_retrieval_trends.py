from datetime import UTC, date, datetime

import pytest

from radar.db import init_db
from radar.models import ChangeType, ContextLabel
from radar.retrieval import CorpusRetriever, fts_query
from radar.trends import (
    MIN_CADENCE_DAYS,
    default_label,
    find_candidates,
    save_trends,
    trend_for_statement,
)

NOW = datetime(2026, 8, 17, tzinfo=UTC)
TODAY = date(2026, 8, 17)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    yield conn
    conn.close()


def add(
    conn, sid, vendor, change_type, event_date, text=None, index=None, supersedes=None
):
    conn.execute(
        "INSERT INTO event_statements (statement_id, text, vendor, change_type, event_date, "
        "source_url, statement_index, evidence, ingested_at, ingest_mode, extractor_model, "
        "prompt_version, raw_material_ref, supersedes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            text or f"{vendor} announced a {change_type} event",
            vendor,
            change_type,
            event_date,
            f"https://example.test/{sid}",
            index if index is not None else 0,
            "verbatim quote here",
            NOW.isoformat(),
            "backfill",
            "test-model",
            "v1",
            "cache/ref",
            supersedes,
        ),
    )
    conn.commit()


class TestFtsQuery:
    def test_punctuation_cannot_break_the_query(self):
        query = fts_query('claude-3 "opus" retired; NEAR(x)')
        assert '"' in query
        assert ";" not in query.replace('"', "")

    def test_short_words_are_dropped(self):
        assert "in" not in fts_query("in the deprecation")

    def test_empty_text_yields_empty_query(self):
        assert fts_query("") == ""


class TestMandatoryFilters:
    def test_missing_vendor_returns_nothing(self, db):
        """Without both filters the query would match the whole corpus."""
        for i in range(5):
            add(db, f"s{i}", "anthropic", "deprecation", "2026-05-01", index=i)
        result = CorpusRetriever(db).find_precedents(None, "deprecation", TODAY)
        assert result.hits == []

    def test_missing_change_type_returns_nothing(self, db):
        add(db, "s1", "anthropic", "deprecation", "2026-05-01")
        result = CorpusRetriever(db).find_precedents("anthropic", None, TODAY)
        assert result.hits == []


class TestRetrieval:
    def test_finds_same_vendor_and_type_inside_the_window(self, db):
        for i in range(3):
            add(db, f"s{i}", "anthropic", "deprecation", f"2026-0{5 + i}-01", index=i)
        result = CorpusRetriever(db).find_precedents("anthropic", "deprecation", TODAY)
        assert len(result.hits) == 3
        assert result.report.strict_hits == 3

    def test_other_vendors_are_excluded(self, db):
        add(db, "a1", "anthropic", "deprecation", "2026-05-01")
        add(db, "o1", "openai", "deprecation", "2026-05-02")
        result = CorpusRetriever(db).find_precedents("anthropic", "deprecation", TODAY)
        assert [h.statement_id for h in result.hits] == ["a1"]

    def test_events_outside_the_widest_window_are_excluded(self, db):
        add(db, "old", "anthropic", "deprecation", "2019-01-01")
        result = CorpusRetriever(db).find_precedents("anthropic", "deprecation", TODAY)
        assert result.hits == []

    def test_undated_records_are_excluded(self, db):
        """A record with no event date cannot be placed in a window."""
        add(db, "s1", "anthropic", "deprecation", None)
        result = CorpusRetriever(db).find_precedents("anthropic", "deprecation", TODAY)
        assert result.hits == []

    def test_results_are_capped_but_the_full_count_survives(self, db):
        for i in range(20):
            add(db, f"s{i}", "anthropic", "deprecation", "2026-05-01", index=i)
        result = CorpusRetriever(db, {"max_results_per_cluster": 12}).find_precedents(
            "anthropic", "deprecation", TODAY
        )
        assert len(result.hits) == 12
        assert result.report.strict_hits == 20
        assert result.report.shown == 12

    def test_a_superseded_record_is_not_cited(self, db):
        add(db, "old", "anthropic", "deprecation", "2026-05-01")
        add(
            db,
            "new",
            "anthropic",
            "deprecation",
            "2026-06-01",
            index=1,
            supersedes="old",
        )
        result = CorpusRetriever(db).find_precedents("anthropic", "deprecation", TODAY)
        assert [h.statement_id for h in result.hits] == ["new"]

    def test_the_cluster_itself_can_be_excluded(self, db):
        add(db, "self", "anthropic", "deprecation", "2026-08-01")
        add(db, "other", "anthropic", "deprecation", "2026-05-01", index=1)
        result = CorpusRetriever(db).find_precedents(
            "anthropic", "deprecation", TODAY, exclude_ids={"self"}
        )
        assert [h.statement_id for h in result.hits] == ["other"]

    def test_narrow_window_is_preferred_when_it_already_suffices(self, db):
        for i in range(3):
            add(db, f"s{i}", "anthropic", "deprecation", f"2026-0{6 + i}-01", index=i)
        result = CorpusRetriever(db, {"windows": [180, 365]}).find_precedents(
            "anthropic", "deprecation", TODAY
        )
        assert result.window_used == 180

    def test_window_widens_when_the_narrow_one_is_thin(self, db):
        add(db, "recent", "anthropic", "deprecation", "2026-08-01")
        add(db, "older", "anthropic", "deprecation", "2025-10-01", index=1)
        result = CorpusRetriever(db, {"windows": [180, 365]}).find_precedents(
            "anthropic", "deprecation", TODAY
        )
        assert result.window_used == 365
        assert len(result.hits) == 2

    def test_precedents_carry_denormalized_content(self, db):
        add(db, "s1", "anthropic", "deprecation", "2026-05-01")
        add(db, "s2", "anthropic", "deprecation", "2026-06-01", index=1)
        precedents = (
            CorpusRetriever(db)
            .find_precedents("anthropic", "deprecation", TODAY)
            .precedents
        )
        assert precedents[0].text
        assert precedents[0].source_url.startswith("https://")
        assert precedents[0].change_type is ChangeType.DEPRECATION


class TestRelaxedQuery:
    def test_a_neighbouring_type_miss_becomes_a_visible_number(self, db):
        """The classifier calling one event limits and another pricing must
        not read as an absence of precedents."""
        add(db, "p1", "openai", "pricing", "2026-06-01")
        add(db, "l1", "openai", "limits", "2026-03-01", index=1)
        add(db, "l2", "openai", "limits", "2026-04-01", index=2)
        result = CorpusRetriever(db).find_precedents("openai", "pricing", TODAY)
        assert result.report.strict_hits == 1
        assert result.report.relaxed_hits == 3
        assert {h.statement_id for h in result.relaxed_only} == {"l1", "l2"}

    def test_relaxed_hits_never_become_published_precedents(self, db):
        add(db, "p1", "openai", "pricing", "2026-06-01")
        add(db, "l1", "openai", "limits", "2026-03-01", index=1)
        result = CorpusRetriever(db).find_precedents("openai", "pricing", TODAY)
        assert [p.statement_id for p in result.precedents] == ["p1"]


class TestLabelling:
    def test_a_single_precedent_is_not_a_pattern(self, db):
        add(db, "s1", "anthropic", "deprecation", "2026-05-01")
        retriever = CorpusRetriever(db)
        result = retriever.find_precedents("anthropic", "deprecation", TODAY)
        assert retriever.label_for(result) is ContextLabel.NOT_FOUND_IN_CORPUS

    def test_two_precedents_make_it_recurring(self, db):
        add(db, "s1", "anthropic", "deprecation", "2026-05-01")
        add(db, "s2", "anthropic", "deprecation", "2026-06-01", index=1)
        retriever = CorpusRetriever(db)
        result = retriever.find_precedents("anthropic", "deprecation", TODAY)
        assert retriever.label_for(result) is ContextLabel.RECURRING

    def test_coverage_explains_an_empty_result(self, db):
        """Absence has to be readable as thin coverage, not as a first occurrence."""
        add(db, "r1", "cursor", "release", "2026-05-01")
        coverage = CorpusRetriever(db).coverage_for("cursor")
        assert coverage["statements"] == 1
        assert coverage["by_change_type"] == {"release": 1}


class TestTrendCandidates:
    def test_three_events_form_a_candidate(self, db):
        for i in range(3):
            add(db, f"s{i}", "anthropic", "deprecation", f"2026-0{4 + i}-01", index=i)
        accepted, _ = find_candidates(db)
        assert len(accepted) == 1
        assert accepted[0].members == 3

    def test_two_events_are_rejected(self, db):
        for i in range(2):
            add(db, f"s{i}", "anthropic", "deprecation", f"2026-0{4 + i}-01", index=i)
        accepted, rejected = find_candidates(db)
        assert accepted == []
        assert "нужно 3" in rejected[0].rejected_reason

    def test_routine_releases_are_not_a_trend(self, db):
        """Fourteen releases in a half-year is a formally perfect empty trend."""
        for i in range(14):
            add(db, f"r{i}", "anthropic", "release", "2026-05-01", index=i)
        accepted, rejected = find_candidates(db)
        assert accepted == []
        assert "рутинный" in rejected[0].rejected_reason

    def test_an_event_the_vendor_produces_constantly_is_not_a_line(self, db):
        """Nine deprecations on one day is routine, whatever the type says."""
        for i in range(9):
            add(db, f"d{i}", "openai", "deprecation", "2026-05-01", index=i)
        accepted, rejected = find_candidates(db)
        reasons = " ".join(c.rejected_reason or "" for c in rejected)
        assert accepted == []
        assert "постоянно" in reasons

    def test_a_deprecations_only_corpus_still_yields_a_trend(self, db):
        """The main demo case: the source is a deprecations registry, so this
        vendor has no other change type in the corpus at all."""
        for i, day in enumerate(["2026-02-01", "2026-04-01", "2026-06-01"]):
            add(db, f"d{i}", "anthropic", "deprecation", day, index=i)
        accepted, _ = find_candidates(db)
        assert [c.change_type for c in accepted] == ["deprecation"]
        assert accepted[0].background_share == 1.0

    def test_a_distinct_line_survives_alongside_routine_noise(self, db):
        for i in range(20):
            add(db, f"r{i}", "anthropic", "release", "2026-05-01", index=i)
        for i in range(3):
            add(
                db,
                f"d{i}",
                "anthropic",
                "deprecation",
                f"2026-0{4 + i}-01",
                index=100 + i,
            )
        accepted, _ = find_candidates(db)
        assert [c.change_type for c in accepted] == ["deprecation"]
        assert (accepted[0].cadence_days() or 0) >= MIN_CADENCE_DAYS

    def test_member_list_matches_the_records_exactly(self, db):
        for i in range(3):
            add(db, f"s{i}", "anthropic", "deprecation", f"2026-0{4 + i}-01", index=i)
        accepted, _ = find_candidates(db)
        assert sorted(accepted[0].member_ids) == ["s0", "s1", "s2"]
        assert len(accepted[0].urls) == 3

    def test_cadence_uses_the_median_not_the_mean(self, db):
        for i, day in enumerate(
            ["2026-01-01", "2026-02-01", "2026-03-01", "2026-08-01"]
        ):
            add(db, f"s{i}", "anthropic", "deprecation", day, index=i)
        accepted, _ = find_candidates(db)
        assert accepted[0].cadence_days() == pytest.approx(30.5, abs=2)

    def test_a_line_without_recent_events_goes_dormant(self, db):
        for i, day in enumerate(["2025-01-01", "2025-02-01", "2025-03-01"]):
            add(db, f"s{i}", "anthropic", "deprecation", day, index=i)
        accepted, _ = find_candidates(db)
        assert str(accepted[0].trajectory(TODAY)) == "dormant"

    def test_label_only_states_what_the_records_support(self, db):
        for i in range(3):
            add(db, f"s{i}", "anthropic", "deprecation", f"2026-0{4 + i}-01", index=i)
        accepted, _ = find_candidates(db)
        label = default_label(accepted[0])
        assert "3 события" in label
        assert "2026-04-01" in label


class TestTrendPersistence:
    def test_saving_is_idempotent(self, db):
        for i in range(3):
            add(db, f"s{i}", "anthropic", "deprecation", f"2026-0{4 + i}-01", index=i)
        accepted, _ = find_candidates(db)
        save_trends(db, accepted, as_of=TODAY)
        save_trends(db, accepted, as_of=TODAY)
        assert db.execute("SELECT COUNT(*) FROM trends").fetchone()[0] == 1

    def test_a_statement_can_be_traced_back_to_its_trend(self, db):
        for i in range(3):
            add(db, f"s{i}", "anthropic", "deprecation", f"2026-0{4 + i}-01", index=i)
        accepted, _ = find_candidates(db)
        save_trends(db, accepted, as_of=TODAY)
        found = trend_for_statement(db, "s1")
        assert found is not None
        assert found["vendor"] == "anthropic"

    def test_an_unrelated_statement_has_no_trend(self, db):
        assert trend_for_statement(db, "nope") is None
