import json
from datetime import UTC, date, datetime, timedelta

import json

import pytest

from radar.adapters.base import CollectedItem
from radar.cluster import Cluster
from radar.db import init_db
from radar.delta import (
    compute_delta,
    filter_unseen,
    material_id,
    prune_state,
    resolve_expired,
    save_state,
)
from radar.models import DeltaStatus, Fact, FactKind

NOW = datetime(2026, 8, 17, tzinfo=UTC)
TODAY = date(2026, 8, 17)
TOMORROW = TODAY + timedelta(days=1)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    yield conn
    conn.close()


def make_cluster(cluster_id="c1", title="Anthropic отключает claude-3-opus"):
    item = CollectedItem(
        url="https://docs.claude.com/deprecations#opus",
        title=title,
        raw_text="body",
    )
    return Cluster(
        cluster_id=cluster_id,
        dedup_key="sig",
        items=[item],
        vendor="anthropic",
        change_type="deprecation",
    )


def fact(kind=FactKind.SUNSET_DATE, value="2026-10-15"):
    return Fact(
        kind=kind,
        value=value,
        source_url="https://example.test/a",
        evidence="retired on October 15",
        confidence="high",
    )


class TestFirstAppearance:
    def test_an_unseen_cluster_is_new(self, db):
        outcome = compute_delta(db, make_cluster(), [fact()], "run-1", TODAY)
        assert outcome.status is DeltaStatus.NEW
        assert outcome.days_tracked == 1

    def test_a_new_cluster_is_publishable(self, db):
        outcome = compute_delta(db, make_cluster(), [], "run-1", TODAY)
        assert outcome.is_publishable


class TestContinuation:
    def test_the_same_story_without_change_is_continuing(self, db):
        cluster, facts = make_cluster(), [fact()]
        first = compute_delta(db, cluster, facts, "run-1", TODAY)
        save_state(db, cluster, facts, first, "run-1", TODAY)
        second = compute_delta(db, cluster, facts, "run-2", TODAY)
        assert second.status is DeltaStatus.CONTINUING

    def test_continuing_is_not_republished_in_the_main_block(self, db):
        """FR-5.3: unchanged stories go to the folded section."""
        cluster, facts = make_cluster(), [fact()]
        save_state(
            db,
            cluster,
            facts,
            compute_delta(db, cluster, facts, "run-1", TODAY),
            "run-1",
        )
        assert compute_delta(db, cluster, facts, "run-2", TODAY).is_publishable is False

    def test_the_day_counter_advances_once_per_run(self, db):
        cluster, facts = make_cluster(), [fact()]
        for run in ("run-1", "run-2", "run-3"):
            outcome = compute_delta(db, cluster, facts, run, TODAY)
            save_state(db, cluster, facts, outcome, run, TODAY)
        assert outcome.days_tracked == 3

    def test_a_rerun_of_the_same_run_does_not_inflate_the_counter(self, db):
        """PUB-5 allows idempotent reruns; 'third day' must not become fourth."""
        cluster, facts = make_cluster(), [fact()]
        first = compute_delta(db, cluster, facts, "run-1", TODAY)
        save_state(db, cluster, facts, first, "run-1", TODAY)
        again = compute_delta(db, cluster, facts, "run-1", TODAY)
        assert again.days_tracked == 1


class TestUpdated:
    def test_a_new_fact_makes_it_updated(self, db):
        cluster = make_cluster()
        save_state(
            db, cluster, [], compute_delta(db, cluster, [], "run-1", TODAY), "run-1"
        )
        outcome = compute_delta(db, cluster, [fact()], "run-2", TODAY)
        assert outcome.status is DeltaStatus.UPDATED

    def test_the_note_states_exactly_what_appeared(self, db):
        """FR-5.2 wants a phrase the reader can check against the card."""
        cluster = make_cluster()
        save_state(
            db, cluster, [], compute_delta(db, cluster, [], "run-1", TODAY), "run-1"
        )
        outcome = compute_delta(db, cluster, [fact()], "run-2", TODAY)
        assert "дата отключения" in outcome.note
        assert "2026-10-15" in outcome.note

    def test_only_genuinely_new_facts_are_listed(self, db):
        cluster = make_cluster()
        old = [fact(FactKind.VERSION, "4.1")]
        save_state(
            db, cluster, old, compute_delta(db, cluster, old, "run-1", TODAY), "run-1"
        )
        outcome = compute_delta(db, cluster, [*old, fact()], "run-2", TODAY)
        assert [f.value for f in outcome.new_facts] == ["2026-10-15"]

    def test_the_same_fact_written_differently_is_not_new(self, db):
        cluster = make_cluster()
        old = [fact(FactKind.VERSION, "Claude 4.1")]
        save_state(
            db, cluster, old, compute_delta(db, cluster, old, "run-1", TODAY), "run-1"
        )
        outcome = compute_delta(
            db, cluster, [fact(FactKind.VERSION, "  claude 4.1 ")], "run-2", TODAY
        )
        assert outcome.status is DeltaStatus.CONTINUING


def store(db, cluster, facts, run="run-1", as_of=TODAY):
    """Put a cluster into the operational layer with the facts it carries."""
    outcome = compute_delta(db, cluster, facts, run, as_of)
    save_state(db, cluster, facts, outcome, run, as_of)
    return outcome


def resolved_at(db, cluster):
    return db.execute(
        "SELECT resolved_at FROM clusters WHERE cluster_id = ?",
        (cluster.cluster_id,),
    ).fetchone()[0]


class TestResolution:
    def test_a_passed_sunset_date_closes_the_story(self, db):
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, "2026-08-01")]
        save_state(
            db,
            cluster,
            facts,
            compute_delta(db, cluster, facts, "run-1", TODAY),
            "run-1",
        )
        assert resolve_expired(db, TODAY) == [cluster.cluster_id]

    def test_a_future_date_leaves_it_open(self, db):
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, "2027-01-01")]
        save_state(
            db,
            cluster,
            facts,
            compute_delta(db, cluster, facts, "run-1", TODAY),
            "run-1",
        )
        assert resolve_expired(db, TODAY) == []

    def test_an_unparseable_date_does_not_crash_resolution(self, db):
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, "дата в источнике не указана")]
        save_state(
            db,
            cluster,
            facts,
            compute_delta(db, cluster, facts, "run-1", TODAY),
            "run-1",
        )
        assert resolve_expired(db, TODAY) == []

    def test_a_resolved_cluster_reports_as_resolved(self, db):
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, "2026-08-01")]
        save_state(
            db,
            cluster,
            facts,
            compute_delta(db, cluster, facts, "run-1", TODAY),
            "run-1",
        )
        resolve_expired(db, TODAY)
        assert (
            compute_delta(db, cluster, facts, "run-2", TODAY).status
            is DeltaStatus.RESOLVED
        )


class TestWhatCloses:
    """A story is over when there is nothing left ahead of it.

    The rule is the latest of the dates the cluster carries, not any of them.
    A cluster's facts accumulate across runs and across the sections of one
    page, so "any date" reads the retirement of a neighbouring table row as
    the end of this story.
    """

    def test_one_expired_date_among_later_ones_does_not_close_it(self, db):
        """The defect, stated as a test: 27 of 34 cards were closed by this."""
        cluster = make_cluster()
        facts = [
            fact(FactKind.SUNSET_DATE, "2024-11-06"),
            fact(FactKind.SUNSET_DATE, "2026-08-05"),
            fact(FactKind.SUNSET_DATE, "2027-07-24"),
        ]
        store(db, cluster, facts)
        assert resolve_expired(db, TODAY) == []

    def test_a_story_closes_once_the_last_date_is_behind_us(self, db):
        cluster = make_cluster()
        facts = [
            fact(FactKind.SUNSET_DATE, "2024-11-06"),
            fact(FactKind.SUNSET_DATE, "2026-08-05"),
        ]
        store(db, cluster, facts)
        assert resolve_expired(db, date(2026, 8, 5)) == []
        assert resolve_expired(db, date(2026, 8, 6)) == [cluster.cluster_id]

    def test_the_day_the_deadline_arrives_the_story_is_still_open(self, db):
        """A page cannot say "сегодня" and "история закрыта" in one breath.

        The date arriving is the most useful morning this card ever has; it
        closes the morning after.
        """
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, TODAY.isoformat())]
        store(db, cluster, facts)
        assert resolve_expired(db, TODAY) == []
        assert resolve_expired(db, TOMORROW) == [cluster.cluster_id]

    def test_a_deadline_tomorrow_is_not_a_closed_story(self, db):
        """Cohere Command R, from the run of 18 August 2026.

        The card announced a shutdown on the 19th and carried a February
        `effective_date` from an earlier day of the same table. The old rule
        read February, closed the story, and the page said "19 августа,
        завтра" and "история закрыта" one under the other.
        """
        cluster = make_cluster(title="Legacy and end-of-life (EOL) models - Cohere")
        facts = [
            fact(FactKind.EFFECTIVE_DATE, "2026-02-19"),
            fact(FactKind.SUNSET_DATE, "2026-08-19"),
        ]
        store(db, cluster, facts)
        assert resolve_expired(db, date(2026, 8, 18)) == []

    def test_a_future_effective_date_holds_the_story_open(self, db):
        """Why the kind of date does not decide it.

        Judging on `sunset_date` alone looks stricter and is not: this card's
        own date is the October release, and the passed sunsets belong to the
        neighbouring rows of the same deprecation table.
        """
        cluster = make_cluster(title="Model status - claude-haiku-4-5-20251001")
        facts = [
            fact(FactKind.SUNSET_DATE, "2026-08-05"),
            fact(FactKind.EFFECTIVE_DATE, "2026-10-15"),
        ]
        store(db, cluster, facts)
        assert resolve_expired(db, TODAY) == []

    def test_a_story_with_no_date_at_all_is_never_closed_here(self, db):
        """A release note carries a version and no date. Silence is not
        completion; it leaves the layer through `prune_state`."""
        cluster = make_cluster(title="v2.1.234")
        store(db, cluster, [fact(FactKind.VERSION, "2.1.234")])
        assert resolve_expired(db, TODAY) == []
        assert resolve_expired(db, TODAY + timedelta(days=365)) == []

    def test_an_unreadable_date_does_not_hold_a_finished_story_open(self, db):
        cluster = make_cluster()
        facts = [
            fact(FactKind.SUNSET_DATE, "дата в источнике не указана"),
            fact(FactKind.SUNSET_DATE, "2026-08-01"),
        ]
        store(db, cluster, facts)
        assert resolve_expired(db, TODAY) == [cluster.cluster_id]

    def test_closing_is_recorded_once(self, db):
        cluster = make_cluster()
        store(db, cluster, [fact(FactKind.SUNSET_DATE, "2026-08-01")])
        assert resolve_expired(db, TODAY) == [cluster.cluster_id]
        assert resolve_expired(db, TOMORROW) == []


class TestReopening:
    """A closed story that speaks again is open again.

    The closure is an inference from the dates on file; a fresh fact is an
    observation made this morning. Checking the flag before the facts made the
    inference permanent and unfalsifiable.
    """

    def test_a_new_fact_on_a_closed_story_is_reported(self, db):
        cluster = make_cluster()
        old = [fact(FactKind.SUNSET_DATE, "2026-08-01")]
        store(db, cluster, old)
        resolve_expired(db, TODAY)

        outcome = compute_delta(
            db,
            cluster,
            [*old, fact(FactKind.SUNSET_DATE, "2027-03-01")],
            "run-2",
            TODAY,
        )
        assert outcome.status is DeltaStatus.UPDATED
        assert [f.value for f in outcome.new_facts] == ["2027-03-01"]
        assert "2027-03-01" in outcome.note

    def test_a_wrongly_closed_story_can_come_back_to_life(self, db):
        """The second-order defect: closed once meant closed forever.

        A card closed in error had no way back — the resolved branch returned
        before the new facts were looked at, so tomorrow's announcement could
        never reach the reader.
        """
        cluster = make_cluster()
        old = [fact(FactKind.SUNSET_DATE, "2027-07-24")]
        store(db, cluster, old)
        # closed by hand, the way the old rule would have closed it
        db.execute("UPDATE clusters SET resolved_at = ?", (TODAY.isoformat(),))
        db.commit()

        fresh = [*old, fact(FactKind.EFFECTIVE_DATE, "2026-12-01")]
        outcome = compute_delta(db, cluster, fresh, "run-2", TOMORROW)
        assert outcome.status is DeltaStatus.UPDATED
        save_state(db, cluster, fresh, outcome, "run-2", TOMORROW)
        assert resolved_at(db, cluster) is None
        # and the next quiet morning does not silently fall back to closed
        assert (
            compute_delta(db, cluster, fresh, "run-3", TOMORROW).status
            is DeltaStatus.CONTINUING
        )

    def test_a_story_that_stays_finished_closes_again_the_same_run(self, db):
        """Reopening is not a leak: `resolve_expired` runs after `save_state`
        in the same stage and re-earns the closure at once."""
        cluster = make_cluster()
        old = [fact(FactKind.SUNSET_DATE, "2026-08-01")]
        store(db, cluster, old)
        resolve_expired(db, TODAY)

        fresh = [*old, fact(FactKind.EFFECTIVE_DATE, "2026-07-01")]
        outcome = compute_delta(db, cluster, fresh, "run-2", TODAY)
        save_state(db, cluster, fresh, outcome, "run-2", TODAY)
        assert outcome.status is DeltaStatus.UPDATED  # today it said something
        assert resolve_expired(db, TODAY) == [cluster.cluster_id]
        assert (
            compute_delta(db, cluster, fresh, "run-3", TODAY).status
            is DeltaStatus.RESOLVED
        )

    def test_a_quiet_run_does_not_reopen_a_closed_story(self, db):
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, "2026-08-01")]
        store(db, cluster, facts)
        resolve_expired(db, TODAY)
        outcome = compute_delta(db, cluster, facts, "run-2", TODAY)
        save_state(db, cluster, facts, outcome, "run-2", TODAY)
        assert outcome.status is DeltaStatus.RESOLVED
        assert resolved_at(db, cluster) == TODAY.isoformat()


class TestState:
    def test_state_survives_a_reconnect(self, tmp_path):
        """FR-5.5: state outlives the process."""
        path = tmp_path / "radar.db"
        conn = init_db(path)
        cluster, facts = make_cluster(), [fact()]
        save_state(
            conn,
            cluster,
            facts,
            compute_delta(conn, cluster, facts, "run-1", TODAY),
            "run-1",
        )
        conn.close()

        reopened = init_db(path)
        assert compute_delta(reopened, cluster, facts, "run-2", TODAY).days_tracked == 2
        reopened.close()

    def test_facts_accumulate_rather_than_overwrite(self, db):
        cluster = make_cluster()
        save_state(
            db,
            cluster,
            [fact(FactKind.VERSION, "4.1")],
            compute_delta(db, cluster, [], "run-1", TODAY),
            "run-1",
        )
        save_state(
            db,
            cluster,
            [fact()],
            compute_delta(db, cluster, [fact()], "run-2", TODAY),
            "run-2",
        )
        stored = json.loads(
            db.execute(
                "SELECT facts_json FROM clusters WHERE cluster_id = ?",
                (cluster.cluster_id,),
            ).fetchone()[0]
        )
        assert len(stored) == 2

    def test_pruning_drops_only_stale_clusters(self, db):
        fresh, old = make_cluster("fresh"), make_cluster("old")
        for c in (fresh, old):
            save_state(
                db, c, [], compute_delta(db, c, [], "run-1", TODAY), "run-1", TODAY
            )
        db.execute(
            "UPDATE clusters SET updated_at = ? WHERE cluster_id = 'old'",
            ("2026-01-01T00:00:00+00:00",),
        )
        db.commit()
        assert prune_state(db, TODAY) == 1
        remaining = [
            r[0] for r in db.execute("SELECT cluster_id FROM clusters").fetchall()
        ]
        assert remaining == ["fresh"]


# --------------------------------------------------------------------------
# first sighting
# --------------------------------------------------------------------------


def table_row(
    model="claude-3-opus-20240229",
    retirement="2027-01-05",
    source_id="anthropic_model_deprecations",
):
    """A row of a retirement table: dated far ahead, unchanged for months.

    This is the shape that broke the window. Every such row clears "the event
    has not happened yet" on any day the digest is ever run.
    """
    return CollectedItem(
        url="https://docs.claude.com/en/docs/about-claude/model-deprecations",
        title=f"Model status - {model}",
        raw_text=f"{model} | Deprecated | January 5, 2026 | {retirement}",
        event_date=date.fromisoformat(retirement),
        extra={"source_id": source_id},
    )


def article(slug="gpt-5-1", day="2026-08-17"):
    return CollectedItem(
        url=f"https://openai.com/changelog#{slug}",
        title=f"{day}: {slug}",
        raw_text=f"We are releasing {slug}.",
        event_date=date.fromisoformat(day),
        extra={"source_id": "openai_news_feed"},
    )


class TestFirstSighting:
    def test_a_material_nobody_has_seen_is_new(self, db):
        assert filter_unseen(db, [table_row(), article()], TODAY) != []

    def test_the_same_page_the_next_day_yields_nothing(self, db):
        """The defect, stated as a test.

        Three sources hand over forty rows of future retirements every
        morning. Judged by event date all forty are permanently fresh; judged
        by first sighting they are news once.
        """
        rows = [table_row(f"model-{n}") for n in range(40)]
        assert len(filter_unseen(db, rows, TODAY)) == 40
        assert filter_unseen(db, rows, TOMORROW) == []

    def test_a_quiet_day_is_reachable_at_all(self, db):
        """DR-3 on the full config: zero materials has to be a possible day."""
        page = [table_row(f"model-{n}") for n in range(17)] + [article()]
        filter_unseen(db, page, TODAY)
        assert filter_unseen(db, page, TOMORROW) == []

    def test_a_rerun_of_the_same_day_repeats_itself(self, db):
        """PUB-5: the second attempt at today must publish today's digest,
        not an empty one."""
        rows = [table_row(), article()]
        first = filter_unseen(db, rows, TODAY)
        assert [i.url for i in filter_unseen(db, rows, TODAY)] == [i.url for i in first]

    def test_two_mornings_replayed_on_one_afternoon_stay_two_mornings(self, db):
        """How the quiet day gets demonstrated: `--for-date` twice in a row.

        The ledger records the run's own day, not the wall clock, or both
        replays would land on the same date and the second one would still be
        the first.
        """
        rows = [table_row(f"model-{n}") for n in range(3)]
        afternoon = datetime(2026, 8, 20, 15, 0, tzinfo=UTC)
        assert len(filter_unseen(db, rows, TODAY, now=afternoon)) == 3
        assert filter_unseen(db, rows, TOMORROW, now=afternoon) == []

    def test_a_row_that_changed_is_news_again(self, db):
        filter_unseen(db, [table_row(retirement="2027-01-05")], TODAY)
        moved = table_row(retirement="2026-09-01")
        assert len(filter_unseen(db, [moved], TOMORROW)) == 1

    def test_a_new_row_on_a_known_page_comes_through_alone(self, db):
        known = [table_row(f"model-{n}") for n in range(10)]
        filter_unseen(db, known, TODAY)
        fresh = filter_unseen(db, known + [table_row("model-new")], TOMORROW)
        assert [i.title for i in fresh] == ["Model status - model-new"]

    def test_a_material_that_disappears_and_returns_is_not_news_twice(self, db):
        row = table_row()
        filter_unseen(db, [row], TODAY)
        filter_unseen(db, [], TOMORROW)  # the page dropped the row for a day
        assert filter_unseen(db, [row], TOMORROW + timedelta(days=1)) == []

    def test_two_sources_carrying_one_material_see_it_once(self, db):
        both = [table_row(), table_row(source_id="anthropic_api_release_notes")]
        assert len(filter_unseen(db, both, TODAY)) == 1


class TestLedger:
    def test_what_was_seen_is_written_down(self, db):
        item = table_row()
        item.extra["seen_in"] = ["anthropic_model_deprecations"]
        filter_unseen(db, [item], TODAY, now=datetime(2026, 8, 17, 6, 30, tzinfo=UTC))
        row = db.execute("SELECT * FROM raw_items").fetchone()
        assert row["id"] == material_id(item)
        assert row["source_id"] == "anthropic_model_deprecations"
        assert row["collected_at"].startswith("2026-08-17T06:30")
        assert json.loads(row["seen_in_json"]) == ["anthropic_model_deprecations"]

    def test_first_sighting_is_never_moved_forward(self, db):
        """If a rerun restamped the row, today's materials would be new again
        tomorrow and the defect would come back through the back door."""
        row = table_row()
        filter_unseen(db, [row], TODAY, now=datetime(2026, 8, 17, 6, 0, tzinfo=UTC))
        filter_unseen(db, [row], TODAY, now=datetime(2026, 8, 17, 23, 0, tzinfo=UTC))
        stamps = [r[0] for r in db.execute("SELECT collected_at FROM raw_items")]
        assert stamps == ["2026-08-17T06:00:00+00:00"]

    def test_the_ledger_holds_no_cluster_reference(self, db):
        """`raw_items.cluster_id` points at a table pruning deletes from."""
        filter_unseen(db, [table_row()], TODAY)
        assert db.execute("SELECT cluster_id FROM raw_items").fetchone()[0] is None

    def test_pruning_the_state_does_not_forget_what_was_seen(self, db):
        cluster = make_cluster()
        save_state(
            db,
            cluster,
            [],
            compute_delta(db, cluster, [], "run-1", TODAY),
            "run-1",
            TODAY,
        )
        filter_unseen(db, [table_row()], TODAY)
        db.execute("UPDATE clusters SET updated_at = '2026-01-01T00:00:00+00:00'")
        db.commit()
        prune_state(db, TODAY)
        assert db.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 1
        assert filter_unseen(db, [table_row()], TOMORROW) == []

    def test_an_unreadable_stamp_counts_as_seen(self, db):
        row = table_row()
        filter_unseen(db, [row], TODAY)
        db.execute("UPDATE raw_items SET collected_at = 'вчера'")
        db.commit()
        assert filter_unseen(db, [row], TOMORROW) == []


class TestIdentity:
    def test_identity_is_the_key_the_collector_dedupes_by(self, db):
        from radar.collect import dedupe_key

        assert material_id(table_row()) == dedupe_key(table_row())

    def test_one_page_of_sections_is_many_materials(self, db):
        """Sections of an anchorless page share a URL and must not collapse."""
        sections = [
            CollectedItem(url="https://x.test/log", title=f"h{n}", raw_text=f"body {n}")
            for n in range(3)
        ]
        assert len({material_id(s) for s in sections}) == 3


class TestTheSweepWorksBothWays:
    """A rule and the table it governs must not be able to disagree.

    When the closing rule was corrected, thirty-three verdicts written by the
    previous version stayed in `clusters`, and the sweep read only unclosed
    rows — so the corrected rule could never revisit what the old one had
    condemned. The digest went on printing "closed" over dates in 2027.
    """

    def _cluster(self, conn, cluster_id, when, resolved_at=None):
        facts = [{
            "kind": "sunset_date", "value": when.isoformat(),
            "source_url": "https://example.test/x", "evidence": "shutdown",
            "value_date": when.isoformat(), "date_precision": "day",
            "evidence_verified": True, "confidence": "high", "subject": None,
        }]
        conn.execute(
            "INSERT INTO clusters (cluster_id, dedup_key, title, primary_url, vendor, "
            "change_type, duplicates_count, first_seen_run, last_seen_run, "
            "days_tracked, delta_status, facts_json, updated_at, resolved_at) "
            "VALUES (?, ?, 't', 'u', 'anthropic', 'deprecation', 0, 'r1', 'r1', 1, "
            "'continuing', ?, '2026-08-18', ?)",
            (cluster_id, cluster_id, json.dumps(facts), resolved_at),
        )
        conn.commit()

    def test_a_verdict_from_the_old_rule_is_lifted(self, db):
        # Closed yesterday by the "any date" rule; its own date is next year.
        self._cluster(db, "c-wrong", date(2027, 2, 5), resolved_at="2026-08-17")

        resolve_expired(db, date(2026, 8, 18))

        left = db.execute(
            "SELECT resolved_at FROM clusters WHERE cluster_id = 'c-wrong'"
        ).fetchone()[0]
        assert left is None, "исправленное правило не пересмотрело чужой приговор"

    def test_a_story_that_is_genuinely_over_stays_closed(self, db):
        self._cluster(db, "c-done", date(2026, 8, 1), resolved_at="2026-08-02")

        resolve_expired(db, date(2026, 8, 18))

        assert db.execute(
            "SELECT resolved_at FROM clusters WHERE cluster_id = 'c-done'"
        ).fetchone()[0] is not None

    def test_it_still_closes_what_has_passed(self, db):
        self._cluster(db, "c-new", date(2026, 8, 17))

        closed = resolve_expired(db, date(2026, 8, 18))

        assert closed == ["c-new"]
