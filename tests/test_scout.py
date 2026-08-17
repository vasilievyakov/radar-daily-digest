from datetime import UTC, datetime, timedelta

import pytest

from radar.db import init_db
from radar.scout import Candidate, RepoObservation, Scout

NOW = datetime(2026, 8, 17, tzinfo=UTC)


@pytest.fixture
def scout(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    yield Scout(conn, {"min_stars_gained": 300, "min_releases": 3})
    conn.close()


def obs(name, stars, topics=None):
    return RepoObservation(
        full_name=name,
        stars=stars,
        pushed_at="2026-08-15T00:00:00Z",
        topics=topics or ["llm"],
        description=f"{name} does things",
    )


class TestGrowth:
    def test_a_single_snapshot_yields_unknown_not_zero(self, scout):
        """Zero would read as 'not growing'; unknown is the truth."""
        scout.record([obs("acme/tool", 5000)], now=NOW)
        assert scout.growth("acme/tool") is None

    def test_growth_is_the_delta_between_snapshots(self, scout):
        scout.record([obs("acme/tool", 5000)], now=datetime.now(UTC) - timedelta(days=10))
        scout.record([obs("acme/tool", 5800)], now=datetime.now(UTC))
        assert scout.growth("acme/tool") == 800

    def test_snapshots_outside_the_window_are_ignored(self, scout):
        scout.record([obs("acme/tool", 1000)], now=datetime.now(UTC) - timedelta(days=200))
        scout.record([obs("acme/tool", 5000)], now=datetime.now(UTC) - timedelta(days=5))
        scout.record([obs("acme/tool", 5100)], now=datetime.now(UTC))
        assert scout.growth("acme/tool", window_days=30) == 100

    def test_recording_the_same_moment_twice_is_idempotent(self, scout):
        scout.record([obs("acme/tool", 5000)], now=NOW)
        scout.record([obs("acme/tool", 5000)], now=NOW)
        rows = scout.conn.execute("SELECT COUNT(*) FROM repo_snapshots").fetchone()[0]
        assert rows == 1


class TestProposals:
    def test_a_repository_already_in_the_config_is_skipped(self, scout):
        known = {"https://github.com/anthropics/claude-code"}
        found = scout.propose([obs("anthropics/claude-code", 90000)], known, check_releases=False)
        assert found == []

    def test_matching_is_case_insensitive(self, scout):
        known = {"https://github.com/Anthropics/Claude-Code"}
        found = scout.propose([obs("anthropics/claude-code", 90000)], known, check_releases=False)
        assert found == []

    def test_a_new_repository_is_proposed(self, scout):
        found = scout.propose([obs("newco/agent", 9000)], set(), check_releases=False)
        assert [c.full_name for c in found] == ["newco/agent"]

    def test_slow_growth_is_filtered_out(self, scout):
        scout.record([obs("slow/repo", 5000)], now=datetime.now(UTC) - timedelta(days=10))
        scout.record([obs("slow/repo", 5050)], now=datetime.now(UTC))
        found = scout.propose([obs("slow/repo", 5050)], set(), check_releases=False)
        assert found == []

    def test_fast_growth_survives(self, scout):
        scout.record([obs("fast/repo", 5000)], now=datetime.now(UTC) - timedelta(days=10))
        scout.record([obs("fast/repo", 6000)], now=datetime.now(UTC))
        found = scout.propose([obs("fast/repo", 6000)], set(), check_releases=False)
        assert found[0].stars_gained == 1000

    def test_proposals_are_ordered_by_growth(self, scout):
        for name, before, after in [("a/x", 100, 900), ("b/y", 100, 500)]:
            scout.record([obs(name, before)], now=datetime.now(UTC) - timedelta(days=5))
            scout.record([obs(name, after)], now=datetime.now(UTC))
        found = scout.propose(
            [obs("a/x", 900), obs("b/y", 500)], set(), check_releases=False
        )
        assert [c.full_name for c in found] == ["a/x", "b/y"]

    def test_proposals_persist_for_review(self, scout):
        scout.propose([obs("newco/agent", 9000)], set(), check_releases=False)
        assert [r["full_name"] for r in scout.pending()] == ["newco/agent"]

    def test_a_rejected_candidate_leaves_the_queue(self, scout):
        scout.propose([obs("newco/agent", 9000)], set(), check_releases=False)
        scout.decide("newco/agent", "rejected", "не про изменения в инструментах")
        assert scout.pending() == []


class TestYamlOutput:
    def test_candidate_renders_a_pasteable_source(self):
        candidate = Candidate(
            full_name="zed-industries/zed",
            stars=60000,
            stars_gained=1200,
            release_count=54,
            description="editor",
            topics=["llm"],
            reason="",
        )
        block = candidate.as_source_yaml(vendor="zed")
        assert "id: gh_zed_industries_zed" in block
        assert "url: https://github.com/zed-industries/zed" in block
        assert "vendor: zed" in block

    def test_vendor_defaults_to_a_placeholder_for_a_human(self):
        """The vendor is a dictionary decision, not something to guess."""
        candidate = Candidate("a/b", 1, 1, 1, "", [], "")
        assert "vendor: TODO" in candidate.as_source_yaml()
