"""The number in `context_note` must come from the corpus, not from the page.

Retrieval hands publication a capped list (`max_results_per_cluster`, 12 by
default). Counting that list printed "13-й раз" on every event with twelve or
more precedents: a number produced by a pagination constant, presented to the
reader as a fact about the vendor. The tests here run the real pipeline —
sqlite corpus, retriever, `build_signal` — and read the sentence that comes
out, so they fail on the behaviour rather than on the shape of the code.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest

from radar.adapters.base import CollectedItem
from radar.cluster import Cluster
from radar.db import init_db
from radar.models import ContextLabel, Tier
from radar.publish import build_context_note, build_signal
from radar.retrieval import CorpusRetriever

NOW = datetime(2026, 8, 17, tzinfo=UTC)
TODAY = date(2026, 8, 17)

WORDED = {"второй": 2, "третий": 3, "четвёртый": 4, "пятый": 5}


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    yield conn
    conn.close()


def add(conn, sid, event_date, vendor="anthropic", change_type="deprecation", index=0):
    conn.execute(
        "INSERT INTO event_statements (statement_id, text, vendor, change_type, "
        "event_date, source_url, statement_index, evidence, ingested_at, ingest_mode, "
        "extractor_model, prompt_version, raw_material_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            f"{vendor} retires a model",
            vendor,
            change_type,
            event_date.isoformat(),
            f"https://example.test/{sid}",
            index,
            "verbatim quote here",
            NOW.isoformat(),
            "backfill",
            "test-model",
            "v1",
            "cache/ref",
        ),
    )
    conn.commit()


def fill(conn, count, first_offset=10, step=5):
    """`count` dated statements, all inside the narrow window."""
    dates = [
        date.fromordinal(TODAY.toordinal() - first_offset - i * step)
        for i in range(count)
    ]
    for i, when in enumerate(dates):
        add(conn, f"s{i}", when, index=i)
    return sorted(dates)


def make_cluster():
    item = CollectedItem(
        url="https://docs.claude.com/deprecations",
        title="Anthropic отключает модель",
        raw_text="body",
    )
    return Cluster(
        cluster_id="c1",
        dedup_key="k",
        items=[item],
        vendor="anthropic",
        change_type="deprecation",
    )


def note_for(conn, cap):
    retrieval = CorpusRetriever(conn, {"max_results_per_cluster": cap}).find_precedents(
        "anthropic", "deprecation", TODAY
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
        vendor_label="Anthropic",
        change_type_label="Объявление об отключении",
    )
    return signal.context_note, retrieval


def spoken_count(note):
    """The ordinal the reader sees, as an integer."""
    digits = re.search(r"(\d+)-й раз", note)
    if digits:
        return int(digits.group(1))
    worded = re.search(r"(\w+) раз", note)
    assert worded, f"no countable ordinal in {note!r}"
    return WORDED[worded.group(1)]


class TestTheCountIsNotThePageSize:
    @pytest.mark.parametrize("cap", [3, 5, 12])
    def test_the_ordinal_never_equals_the_result_cap_plus_one(self, db, cap):
        """The failure this class of bug produces, stated directly.

        Twenty precedents in the corpus. Whatever the page holds, the sentence
        must not report the page: `cap + 1` is the fingerprint of a count taken
        from a truncated list.
        """
        fill(db, 20)
        note, retrieval = note_for(db, cap)
        assert retrieval.report.shown == cap
        assert spoken_count(note) != cap + 1
        assert spoken_count(note) == 21

    def test_the_ordinal_is_the_corpus_count_plus_today(self, db):
        """Seven precedents, a page big enough for all of them and then some."""
        fill(db, 7)
        note, retrieval = note_for(db, 12)
        assert retrieval.report.total_found == 7
        assert spoken_count(note) == 8

    def test_the_date_belongs_to_the_counted_set(self, db):
        """A true count beside the page's own earliest date claims a false span.

        The page is the newest five records; the count spans twenty. The date
        in the sentence has to come from the same query as the number.
        """
        dates = fill(db, 20)
        note, _ = note_for(db, 5)
        page_earliest = dates[-5]
        assert f"{dates[0].day} " in note
        assert f"с {page_earliest.day} " not in note

    def test_a_growing_corpus_moves_the_number(self, db):
        """The number tracks the corpus; only the page stays at the cap."""
        fill(db, 20)
        first_note, first = note_for(db, 12)
        for i in range(5):
            add(db, f"extra{i}", date.fromordinal(TODAY.toordinal() - 3), index=100 + i)
        second_note, second = note_for(db, 12)
        assert first.report.shown == second.report.shown == 12
        assert spoken_count(second_note) == spoken_count(first_note) + 5


class TestRetrievalCounts:
    def test_the_count_is_taken_apart_from_the_page(self, db):
        fill(db, 40, step=4)  # all forty inside the 180-day window
        report = (
            CorpusRetriever(db, {"max_results_per_cluster": 12})
            .find_precedents("anthropic", "deprecation", TODAY)
            .report
        )
        assert report.total_found == report.strict_hits == 40
        assert report.shown == 12

    def test_the_earliest_match_is_the_corpus_one(self, db):
        dates = fill(db, 30)
        result = CorpusRetriever(db, {"max_results_per_cluster": 12}).find_precedents(
            "anthropic", "deprecation", TODAY
        )
        assert result.report.earliest_event_date == dates[0]
        assert min(p.event_date for p in result.precedents) > dates[0]

    def test_the_count_respects_the_same_exclusions_as_the_page(self, db):
        """A record kept out of the list must not be counted into the number."""
        fill(db, 6)
        result = CorpusRetriever(db).find_precedents(
            "anthropic", "deprecation", TODAY, exclude_ids={"s0", "s1"}
        )
        assert result.report.total_found == 4
        assert {p.statement_id for p in result.precedents}.isdisjoint({"s0", "s1"})


class TestContextNoteDirectly:
    def test_the_page_length_is_the_fallback_only(self):
        """Called without a corpus count, the sentence still says something true."""
        from radar.models import ChangeType, Precedent

        page = [
            Precedent(
                statement_id=f"s{i}",
                text="t",
                source_url=f"https://example.test/s{i}",
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
                event_date=date(2026, 5, 12 + i),
            )
            for i in range(2)
        ]
        note = build_context_note(
            ContextLabel.RECURRING, page, "Anthropic", "Отключение", TODAY
        )
        assert spoken_count(note) == 3

    def test_a_corpus_count_below_the_page_cannot_shrink_the_claim(self):
        """The shown records are themselves evidence; the number cannot go under them."""
        from radar.models import ChangeType, Precedent

        page = [
            Precedent(
                statement_id=f"s{i}",
                text="t",
                source_url=f"https://example.test/s{i}",
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
                event_date=date(2026, 5, 12 + i),
            )
            for i in range(4)
        ]
        note = build_context_note(
            ContextLabel.RECURRING,
            page,
            "Anthropic",
            "Отключение",
            TODAY,
            total_found=2,
        )
        assert spoken_count(note) == 5
