import json
from datetime import UTC, date, datetime

import pytest

from radar.db import connect, init_db
from radar.models import SourceStatus
from radar.runlog import Budget, BudgetExceeded, RunLog, new_run_id

DAY = date(2026, 8, 17)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "radar.db"


@pytest.fixture
def db(db_path):
    conn = init_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def log(db):
    return RunLog(db, "run-1", DAY)


def run_row(conn, run_id="run-1"):
    return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def stored_log(conn, run_id="run-1"):
    return json.loads(run_row(conn, run_id)["log_json"])


class TestRunId:
    def test_it_carries_the_timestamp(self):
        assert new_run_id(datetime(2026, 8, 17, 6, 30, 5, tzinfo=UTC)).startswith(
            "20260817T063005-"
        )

    def test_two_ids_from_the_same_moment_differ(self):
        now = datetime(2026, 8, 17, 6, 30, 5, tzinfo=UTC)
        assert new_run_id(now) != new_run_id(now)

    def test_ids_are_unique_across_many_calls(self):
        assert len({new_run_id() for _ in range(200)}) == 200


class TestRunRow:
    def test_creating_the_log_opens_the_run(self, db):
        started = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
        RunLog(db, "run-1", DAY, started_at=started)
        row = run_row(db)
        assert row["status"] == "running"
        assert row["for_date"] == "2026-08-17"
        assert row["started_at"] == started.isoformat()
        assert row["finished_at"] is None

    def test_reopening_the_same_run_id_does_not_duplicate(self, db):
        RunLog(db, "run-1", DAY)
        RunLog(db, "run-1", DAY)
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

    def test_finish_stamps_the_end_and_the_status(self, db, log):
        log.finish()
        row = run_row(db)
        assert row["finished_at"] is not None
        assert row["status"] == "ok"
        assert stored_log(db)["status"] == "ok"

    def test_finish_can_record_a_failure(self, db, log):
        log.finish("failed")
        assert run_row(db)["status"] == "failed"


class TestStages:
    def test_a_stage_records_its_duration_and_counts(self, db, log):
        with log.stage("collect", in_count=12) as record:
            record["out_count"] = 9
        [stage] = stored_log(db)["stages"]
        assert stage["stage"] == "collect"
        assert stage["in_count"] == 12
        assert stage["out_count"] == 9
        assert stage["duration_ms"] >= 0
        assert stage["errors"] == []

    def test_stages_are_written_as_they_finish(self, db, log):
        with log.stage("collect"):
            pass
        assert [s["stage"] for s in stored_log(db)["stages"]] == ["collect"]
        with log.stage("extract"):
            pass
        assert [s["stage"] for s in stored_log(db)["stages"]] == ["collect", "extract"]

    def test_a_failing_stage_is_logged_and_the_error_propagates(self, db, log):
        with pytest.raises(ValueError, match="extractor exploded"):
            with log.stage("extract", in_count=9):
                raise ValueError("extractor exploded")
        [stage] = stored_log(db)["stages"]
        assert stage["errors"] == ["ValueError: extractor exploded"]
        assert stage["duration_ms"] >= 0

    def test_a_crash_leaves_the_earlier_stages_on_disk(self, db, db_path):
        """NFR-4: what survives a crash is what a post-mortem can read."""
        log = RunLog(db, "run-1", DAY)
        with log.stage("collect", in_count=40) as record:
            record["out_count"] = 37
        log.source_result("anthropic-news", SourceStatus.OK, items_count=37)
        log.filtered("https://example.test/ad", "Ad", "not_relevant", "collect")
        log.model_call("dedup", "haiku", tokens_in=1200, tokens_out=300, cost_usd=0.02)

        with pytest.raises(RuntimeError, match="provider is down"):
            with log.stage("extract", in_count=37):
                raise RuntimeError("provider is down")

        # Read through a separate connection: only committed state counts.
        post_mortem = connect(db_path)
        try:
            row = post_mortem.execute(
                "SELECT * FROM runs WHERE run_id = 'run-1'"
            ).fetchone()
            payload = json.loads(row["log_json"])
            collect, extract = payload["stages"]
            assert collect["stage"] == "collect"
            assert collect["out_count"] == 37
            assert collect["errors"] == []
            assert extract["errors"] == ["RuntimeError: provider is down"]
            assert row["cost_usd"] == pytest.approx(0.02)
            assert row["model_calls"] == 1
            counts = {
                table: post_mortem.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE run_id = 'run-1'"
                ).fetchone()[0]
                for table in ("source_runs", "filtered_items", "model_calls")
            }
            assert counts == {"source_runs": 1, "filtered_items": 1, "model_calls": 1}
        finally:
            post_mortem.close()

    def test_an_interrupted_stage_is_not_logged_as_clean(self, db, log):
        with pytest.raises(KeyboardInterrupt):
            with log.stage("extract"):
                raise KeyboardInterrupt
        [stage] = stored_log(db)["stages"]
        assert stage["errors"] != []

    def test_a_crash_does_not_close_the_run(self, db, log):
        with pytest.raises(RuntimeError):
            with log.stage("extract"):
                raise RuntimeError("boom")
        row = run_row(db)
        assert row["status"] == "running"
        assert row["finished_at"] is None


class TestSourceResults:
    def test_a_source_result_is_stored(self, db, log):
        log.source_result("hn", SourceStatus.OK, items_count=30, latency_ms=412)
        row = db.execute("SELECT * FROM source_runs WHERE source_id = 'hn'").fetchone()
        assert row["status"] == "ok"
        assert row["items_count"] == 30
        assert row["latency_ms"] == 412
        assert row["error"] is None

    def test_a_repeat_updates_instead_of_duplicating(self, db, log):
        log.source_result("hn", SourceStatus.FAILED, error="timeout")
        log.source_result("hn", SourceStatus.OK, items_count=30)
        rows = db.execute("SELECT * FROM source_runs WHERE source_id = 'hn'").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"
        assert rows[0]["items_count"] == 30
        assert rows[0]["error"] is None

    def test_empty_is_not_the_same_as_failed(self, db, log):
        log.source_result("client-rendered", SourceStatus.EMPTY, items_count=0)
        log.source_result("broken-feed", SourceStatus.FAILED, error="HTTP 500")
        statuses = dict(
            db.execute("SELECT source_id, status FROM source_runs").fetchall()
        )
        assert statuses == {"client-rendered": "empty", "broken-feed": "failed"}

    def test_the_same_source_in_another_run_is_a_separate_row(self, db, log):
        other = RunLog(db, "run-2", DAY)
        log.source_result("hn", SourceStatus.OK, items_count=1)
        other.source_result("hn", SourceStatus.FAILED, error="timeout")
        rows = db.execute(
            "SELECT run_id, status FROM source_runs ORDER BY run_id"
        ).fetchall()
        assert [(r["run_id"], r["status"]) for r in rows] == [
            ("run-1", "ok"),
            ("run-2", "failed"),
        ]


class TestFiltered:
    def test_a_drop_keeps_its_reason(self, db, log):
        log.filtered(
            "https://example.test/x",
            "Yet another AI roundup",
            "not_relevant",
            "triage",
            note="no vendor change",
        )
        row = db.execute("SELECT * FROM filtered_items").fetchone()
        assert row["reason_code"] == "not_relevant"
        assert row["reason_note"] == "no vendor change"
        assert row["stage"] == "triage"
        assert row["title"] == "Yet another AI roundup"

    def test_a_repeat_updates_instead_of_duplicating(self, db, log):
        log.filtered("https://example.test/x", "T", "not_relevant", "triage")
        log.filtered("https://example.test/x", "T", "duplicate", "triage", note="dup")
        rows = db.execute("SELECT * FROM filtered_items").fetchall()
        assert len(rows) == 1
        assert rows[0]["reason_code"] == "duplicate"
        assert rows[0]["reason_note"] == "dup"

    def test_the_same_url_dropped_at_another_stage_is_a_separate_row(self, db, log):
        log.filtered("https://example.test/x", "T", "not_relevant", "triage")
        log.filtered("https://example.test/x", "T", "no_evidence", "extract")
        rows = db.execute(
            "SELECT stage, reason_code FROM filtered_items ORDER BY stage"
        ).fetchall()
        assert [(r["stage"], r["reason_code"]) for r in rows] == [
            ("extract", "no_evidence"),
            ("triage", "not_relevant"),
        ]


class TestModelCalls:
    def test_a_call_is_stored_with_its_cost(self, db, log):
        log.model_call(
            "extract",
            "claude-sonnet",
            provider="anthropic",
            tokens_in=1500,
            tokens_out=400,
            cost_usd=0.031,
        )
        row = db.execute("SELECT * FROM model_calls").fetchone()
        assert row["stage"] == "extract"
        assert row["model"] == "claude-sonnet"
        assert row["provider"] == "anthropic"
        assert row["tokens_in"] == 1500
        assert row["tokens_out"] == 400
        assert row["cost_usd"] == pytest.approx(0.031)
        assert row["cached"] == 0

    def test_costs_and_tokens_accumulate(self, db, log):
        log.model_call(
            "extract", "sonnet", tokens_in=1000, tokens_out=200, cost_usd=0.02
        )
        log.model_call("score", "haiku", tokens_in=500, tokens_out=100, cost_usd=0.003)
        assert log.model_calls == 2
        assert log.tokens_in == 1500
        assert log.tokens_out == 300
        assert log.cost_usd == pytest.approx(0.023)
        assert db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 2

    def test_the_totals_reach_the_run_row(self, db, log):
        log.model_call(
            "extract", "sonnet", tokens_in=1000, tokens_out=200, cost_usd=0.02
        )
        log.finish()
        row = run_row(db)
        assert row["cost_usd"] == pytest.approx(0.02)
        assert row["model_calls"] == 1
        assert row["tokens_in"] == 1000
        assert row["tokens_out"] == 200

    def test_a_cached_call_is_marked_as_such(self, db, log):
        log.model_call("extract", "sonnet", cached=True)
        assert db.execute("SELECT cached FROM model_calls").fetchone()[0] == 1

    def test_identical_calls_are_two_rows(self, db, log):
        log.model_call("extract", "sonnet", cost_usd=0.01)
        log.model_call("extract", "sonnet", cost_usd=0.01)
        assert db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 2


class TestAsDict:
    def test_it_holds_stages_cost_and_notes(self, log):
        with log.stage("collect", in_count=5) as record:
            record["out_count"] = 4
        log.note("2 sources returned nothing")
        log.model_call("score", "haiku", tokens_in=100, tokens_out=20, cost_usd=0.0011)
        log.delivered("telegram", "sent", message_id="42")
        payload = log.as_dict()
        assert payload["run_id"] == "run-1"
        assert payload["for_date"] == "2026-08-17"
        assert payload["status"] == "running"
        assert [s["stage"] for s in payload["stages"]] == ["collect"]
        assert payload["notes"] == ["2 sources returned nothing"]
        assert payload["delivery"] == [
            {
                "channel": "telegram",
                "status": "sent",
                "message_id": "42",
                "error": None,
            }
        ]
        assert payload["cost"] == {
            "model_calls": 1,
            "tokens_in": 100,
            "tokens_out": 20,
            "usd": 0.0011,
        }

    def test_it_is_json_serialisable(self, log):
        with log.stage("collect"):
            pass
        assert json.loads(json.dumps(log.as_dict()))["stages"][0]["stage"] == "collect"


class TestBudget:
    def test_a_call_within_the_limit_passes(self):
        budget = Budget(1.0)
        budget.check(0.4)
        budget.charge(0.4)
        budget.check(0.4)

    def test_the_exact_limit_still_passes(self):
        Budget(1.0).check(1.0)

    def test_an_overrun_is_refused_before_the_call(self):
        budget = Budget(1.0)
        budget.charge(0.9)
        with pytest.raises(BudgetExceeded):
            budget.check(0.2)
        # Refused, not recorded: a rejected call must not cost anything.
        assert budget.spent_usd == pytest.approx(0.9)
        assert budget.remaining_usd == pytest.approx(0.1)

    def test_the_message_names_the_limit_and_the_spend(self):
        budget = Budget(0.5)
        budget.charge(0.5)
        with pytest.raises(BudgetExceeded, match=r"budget 0\.50 USD exhausted"):
            budget.check(0.01)

    def test_charging_eats_the_remainder(self):
        budget = Budget(2.0)
        budget.charge(0.5)
        assert budget.remaining_usd == pytest.approx(1.5)
        budget.charge(1.25)
        assert budget.remaining_usd == pytest.approx(0.25)

    def test_the_remainder_never_goes_negative(self):
        budget = Budget(1.0)
        budget.charge(3.0)
        assert budget.remaining_usd == 0.0
        assert budget.spent_usd == pytest.approx(3.0)

    def test_a_zero_budget_refuses_any_priced_call(self):
        budget = Budget(0.0)
        budget.check(0.0)
        with pytest.raises(BudgetExceeded):
            budget.check(0.0001)
