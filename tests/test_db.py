import sqlite3
from datetime import UTC, date, datetime

import pytest

from radar.db import (
    connect,
    corpus_readiness,
    dump_state,
    init_db,
    publish_signals,
    read_signals,
)
from radar.models import ChangeType, Signal, SignalType, Tier

NOW = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    yield conn
    conn.close()


def make_signal(signal_id: str, run_id: str = "run-1", rank: int = 1) -> Signal:
    return Signal(
        signal_id=signal_id,
        run_id=run_id,
        signal_type=SignalType.DIGEST_ITEM,
        created_at=NOW,
        for_date=date(2026, 8, 17),
        headline="Anthropic отключает claude-3-opus",
        summary="Полный текст без усечения.",
        change_type=ChangeType.DEPRECATION,
        vendor="anthropic",
        rank=rank,
        score=88,
        tier=Tier.LEAD,
    )


def insert_statement(
    conn, statement_id, vendor, change_type, index=0, event_date="2026-05-01"
):
    conn.execute(
        "INSERT INTO event_statements (statement_id, text, vendor, change_type, event_date, "
        "source_url, statement_index, evidence, ingested_at, ingest_mode, extractor_model, "
        "prompt_version, raw_material_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            statement_id,
            f"{vendor} announced {change_type}",
            vendor,
            change_type,
            event_date,
            f"https://example.test/{statement_id}",
            index,
            "verbatim quote",
            NOW.isoformat(),
            "backfill",
            "test-model",
            "v1",
            "cache/abc",
        ),
    )
    conn.commit()


class TestSignalStore:
    def test_roundtrip_preserves_the_full_contract(self, db):
        publish_signals(db, "run-1", [make_signal("s1")])
        [restored] = read_signals(db, "run-1")
        assert restored.headline == "Anthropic отключает claude-3-opus"
        assert restored.tier is Tier.LEAD
        assert restored.schema_version == 1

    def test_rerun_replaces_instead_of_duplicating(self, db):
        publish_signals(db, "run-1", [make_signal("s1"), make_signal("s2", rank=2)])
        publish_signals(db, "run-1", [make_signal("s1")])
        assert len(read_signals(db, "run-1")) == 1

    def test_rerun_leaves_other_runs_untouched(self, db):
        publish_signals(db, "run-1", [make_signal("s1")])
        publish_signals(db, "run-2", [make_signal("s2", run_id="run-2")])
        publish_signals(db, "run-1", [make_signal("s1")])
        assert len(read_signals(db, "run-2")) == 1

    def test_read_without_run_id_returns_the_latest(self, db):
        publish_signals(db, "run-1", [make_signal("s1")])
        publish_signals(db, "run-2", [make_signal("s2", run_id="run-2")])
        assert [s.signal_id for s in read_signals(db)] == ["s2"]

    def test_empty_store_reads_as_empty(self, db):
        assert read_signals(db) == []

    def test_a_failed_write_leaves_the_previous_run_intact(self, db, monkeypatch):
        publish_signals(db, "run-1", [make_signal("s1")])
        broken = make_signal("s2")
        monkeypatch.setattr(
            type(broken),
            "model_dump_json",
            lambda self: (_ for _ in ()).throw(RuntimeError),
        )
        with pytest.raises(RuntimeError):
            publish_signals(db, "run-1", [broken])
        assert len(read_signals(db, "run-1")) == 1


class TestReadOnlyAccess:
    def test_a_surface_cannot_write(self, tmp_path):
        path = tmp_path / "radar.db"
        conn = init_db(path)
        publish_signals(conn, "run-1", [make_signal("s1")])
        conn.close()

        ro = connect(path, read_only=True)
        assert len(read_signals(ro)) == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM signals")
        ro.close()


class TestCorpus:
    def test_same_url_and_index_cannot_be_inserted_twice(self, db):
        insert_statement(db, "st1", "anthropic", "deprecation")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO event_statements (statement_id, text, vendor, change_type, "
                "source_url, statement_index, evidence, ingested_at, ingest_mode, "
                "extractor_model, prompt_version, raw_material_ref) "
                "VALUES ('st2', 't', 'anthropic', 'deprecation', "
                "'https://example.test/st1', 0, 'q', ?, 'backfill', 'm', 'v1', 'r')",
                (NOW.isoformat(),),
            )

    def test_fts_index_follows_inserts(self, db):
        insert_statement(db, "st1", "anthropic", "deprecation")
        rows = db.execute(
            "SELECT rowid FROM event_statements_fts WHERE event_statements_fts MATCH 'anthropic'"
        ).fetchall()
        assert len(rows) == 1

    def test_readiness_is_false_on_a_thin_corpus(self, db):
        for i in range(20):
            insert_statement(db, f"r{i}", "anthropic", "release", index=i)
        report = corpus_readiness(db, {})
        assert report["total_statements"] == 20
        assert report["ready_for_trend_demo"] is False
        assert report["dense_cells"] == []

    def test_readiness_counts_dense_cells_not_volume(self, db):
        for vendor in ("anthropic", "openai", "google"):
            for i in range(3):
                insert_statement(db, f"{vendor}-{i}", vendor, "deprecation", index=i)
        report = corpus_readiness(db, {})
        assert report["total_statements"] == 9
        assert report["ready_for_trend_demo"] is True
        assert report["vendors_with_dense_cell"] == ["anthropic", "google", "openai"]

    def test_dump_state_reports_every_layer(self, db):
        insert_statement(db, "st1", "anthropic", "deprecation")
        publish_signals(db, "run-1", [make_signal("s1")])
        state = dump_state(db)
        assert state["event_statements"] == 1
        assert state["signals"] == 1
        assert state["corpus_depth"]["earliest"] == "2026-05-01"


class TestFragmentHandling:
    """One page carries many events, each addressed by its own anchor."""

    def test_dedup_keeps_anchored_items_apart(self):
        from radar.cache import canonical_url

        base = "https://docs.claude.com/en/release-notes/overview"
        assert canonical_url(f"{base}#august-11-2026", keep_fragment=True) != canonical_url(
            f"{base}#july-30-2026", keep_fragment=True
        )

    def test_http_cache_still_treats_them_as_one_page(self):
        from radar.cache import HttpCache

        base = "https://docs.claude.com/en/release-notes/overview"
        assert HttpCache.key_for(f"{base}#august-11-2026") == HttpCache.key_for(
            f"{base}#july-30-2026"
        )

    def test_a_whole_feed_does_not_collapse_into_one_material(self):
        """130 entries of the Anthropic feed differ only by anchor."""
        from radar.cache import canonical_url

        base = "https://docs.claude.com/en/release-notes/overview"
        keys = {canonical_url(f"{base}#day-{i}", keep_fragment=True) for i in range(130)}
        assert len(keys) == 130
