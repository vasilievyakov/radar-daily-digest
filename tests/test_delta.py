import json
from datetime import UTC, date, datetime

import pytest

from radar.adapters.base import CollectedItem
from radar.cluster import Cluster
from radar.db import init_db
from radar.delta import compute_delta, prune_state, resolve_expired, save_state
from radar.models import DeltaStatus, Fact, FactKind

NOW = datetime(2026, 8, 17, tzinfo=UTC)
TODAY = date(2026, 8, 17)


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
        kind=kind, value=value, source_url="https://example.test/a",
        evidence="retired on October 15", confidence="high",
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
        save_state(db, cluster, facts, compute_delta(db, cluster, facts, "run-1", TODAY), "run-1")
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
        save_state(db, cluster, [], compute_delta(db, cluster, [], "run-1", TODAY), "run-1")
        outcome = compute_delta(db, cluster, [fact()], "run-2", TODAY)
        assert outcome.status is DeltaStatus.UPDATED

    def test_the_note_states_exactly_what_appeared(self, db):
        """FR-5.2 wants a phrase the reader can check against the card."""
        cluster = make_cluster()
        save_state(db, cluster, [], compute_delta(db, cluster, [], "run-1", TODAY), "run-1")
        outcome = compute_delta(db, cluster, [fact()], "run-2", TODAY)
        assert "дата отключения" in outcome.note
        assert "2026-10-15" in outcome.note

    def test_only_genuinely_new_facts_are_listed(self, db):
        cluster = make_cluster()
        old = [fact(FactKind.VERSION, "4.1")]
        save_state(db, cluster, old, compute_delta(db, cluster, old, "run-1", TODAY), "run-1")
        outcome = compute_delta(db, cluster, [*old, fact()], "run-2", TODAY)
        assert [f.value for f in outcome.new_facts] == ["2026-10-15"]

    def test_the_same_fact_written_differently_is_not_new(self, db):
        cluster = make_cluster()
        old = [fact(FactKind.VERSION, "Claude 4.1")]
        save_state(db, cluster, old, compute_delta(db, cluster, old, "run-1", TODAY), "run-1")
        outcome = compute_delta(db, cluster, [fact(FactKind.VERSION, "  claude 4.1 ")], "run-2", TODAY)
        assert outcome.status is DeltaStatus.CONTINUING


class TestResolution:
    def test_a_passed_sunset_date_closes_the_story(self, db):
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, "2026-08-01")]
        save_state(db, cluster, facts, compute_delta(db, cluster, facts, "run-1", TODAY), "run-1")
        assert resolve_expired(db, TODAY) == [cluster.cluster_id]

    def test_a_future_date_leaves_it_open(self, db):
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, "2027-01-01")]
        save_state(db, cluster, facts, compute_delta(db, cluster, facts, "run-1", TODAY), "run-1")
        assert resolve_expired(db, TODAY) == []

    def test_an_unparseable_date_does_not_crash_resolution(self, db):
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, "дата в источнике не указана")]
        save_state(db, cluster, facts, compute_delta(db, cluster, facts, "run-1", TODAY), "run-1")
        assert resolve_expired(db, TODAY) == []

    def test_a_resolved_cluster_reports_as_resolved(self, db):
        cluster = make_cluster()
        facts = [fact(FactKind.SUNSET_DATE, "2026-08-01")]
        save_state(db, cluster, facts, compute_delta(db, cluster, facts, "run-1", TODAY), "run-1")
        resolve_expired(db, TODAY)
        assert compute_delta(db, cluster, facts, "run-2", TODAY).status is DeltaStatus.RESOLVED


class TestState:
    def test_state_survives_a_reconnect(self, tmp_path):
        """FR-5.5: state outlives the process."""
        path = tmp_path / "radar.db"
        conn = init_db(path)
        cluster, facts = make_cluster(), [fact()]
        save_state(conn, cluster, facts, compute_delta(conn, cluster, facts, "run-1", TODAY), "run-1")
        conn.close()

        reopened = init_db(path)
        assert compute_delta(reopened, cluster, facts, "run-2", TODAY).days_tracked == 2
        reopened.close()

    def test_facts_accumulate_rather_than_overwrite(self, db):
        cluster = make_cluster()
        save_state(db, cluster, [fact(FactKind.VERSION, "4.1")],
                   compute_delta(db, cluster, [], "run-1", TODAY), "run-1")
        save_state(db, cluster, [fact()],
                   compute_delta(db, cluster, [fact()], "run-2", TODAY), "run-2")
        stored = json.loads(
            db.execute("SELECT facts_json FROM clusters WHERE cluster_id = ?",
                       (cluster.cluster_id,)).fetchone()[0]
        )
        assert len(stored) == 2

    def test_pruning_drops_only_stale_clusters(self, db):
        fresh, old = make_cluster("fresh"), make_cluster("old")
        for c in (fresh, old):
            save_state(db, c, [], compute_delta(db, c, [], "run-1", TODAY), "run-1", TODAY)
        db.execute("UPDATE clusters SET updated_at = ? WHERE cluster_id = 'old'",
                   ("2026-01-01T00:00:00+00:00",))
        db.commit()
        assert prune_state(db, TODAY) == 1
        remaining = [r[0] for r in db.execute("SELECT cluster_id FROM clusters").fetchall()]
        assert remaining == ["fresh"]
