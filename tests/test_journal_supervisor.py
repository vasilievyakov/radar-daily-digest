import json
from datetime import UTC, date, datetime, timedelta

import pytest

from radar.db import init_db, publish_signals
from radar.journal import EventKind, Journal, Outcome
from radar.models import Signal, SignalType
from radar.runlog import RunLog
from radar.supervisor import Action, RunState, Supervisor

NOW = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
RUN = "20260817T060000-abc123"


@pytest.fixture
def env(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    journal = Journal(conn, log_dir=tmp_path / "logs", run_id=RUN)
    yield conn, journal, tmp_path
    conn.close()


def start_run(conn, run_id=RUN, status="running", started=NOW, finished=None, cost=0.0):
    conn.execute(
        "INSERT INTO runs (run_id, started_at, finished_at, status, for_date, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, "
        "finished_at = excluded.finished_at",
        (
            run_id,
            started.isoformat(),
            finished.isoformat() if finished else None,
            status,
            started.date().isoformat(),
            cost,
        ),
    )
    conn.commit()


def make_signal(signal_id="s1", run_id=RUN):
    return Signal(
        signal_id=signal_id,
        run_id=run_id,
        signal_type=SignalType.DIGEST_ITEM,
        created_at=NOW,
        for_date=date(2026, 8, 17),
        headline="Anthropic отключает claude-3-opus",
    )


class TestJournalWrites:
    def test_event_lands_in_both_sinks(self, env):
        conn, journal, tmp_path = env
        journal.record(EventKind.RUN_STARTED, actor="pipeline", target=RUN)
        assert len(journal.events(run_id=RUN)) == 1
        lines = Journal.read_file(journal.path)
        assert lines[0]["kind"] == "run.started"

    def test_sequence_is_monotonic(self, env):
        _, journal, _ = env
        for _ in range(5):
            journal.record(EventKind.SOURCE_FETCHED, actor="collect")
        assert [e["seq"] for e in journal.events(run_id=RUN)] == [1, 2, 3, 4, 5]

    def test_sequence_survives_a_restart(self, env):
        conn, journal, tmp_path = env
        journal.record(EventKind.RUN_STARTED, actor="pipeline")
        reopened = Journal(conn, log_dir=tmp_path / "logs", run_id=RUN)
        reopened.record(EventKind.RUN_RESUMED, actor="pipeline")
        assert [e["seq"] for e in reopened.events(run_id=RUN)] == [1, 2]

    def test_payload_round_trips(self, env):
        _, journal, _ = env
        journal.record(
            EventKind.MODEL_CALLED, actor="enrich", model="opus", cost_usd=0.012
        )
        assert journal.events(run_id=RUN)[0]["payload"]["cost_usd"] == 0.012

    def test_file_is_append_only(self, env):
        _, journal, _ = env
        journal.record(EventKind.RUN_STARTED, actor="pipeline")
        first = journal.path.read_text()
        journal.record(EventKind.RUN_FINISHED, actor="pipeline")
        assert journal.path.read_text().startswith(first)

    def test_truncated_trailing_line_is_skipped_not_fatal(self, env):
        _, journal, _ = env
        journal.record(EventKind.RUN_STARTED, actor="pipeline")
        with journal.path.open("a", encoding="utf-8") as handle:
            handle.write('{"event_id": "broke')
        assert len(Journal.read_file(journal.path)) == 1

    def test_action_records_failure_and_reraises(self, env):
        _, journal, _ = env
        with pytest.raises(RuntimeError):
            with journal.action(
                EventKind.STAGE_STARTED, EventKind.STAGE_FINISHED, "enrich"
            ):
                raise RuntimeError("model timeout")
        kinds = [e["kind"] for e in journal.events(run_id=RUN)]
        assert kinds == ["stage.started", "stage.failed"]
        assert "model timeout" in journal.events(run_id=RUN)[1]["payload"]["error"]

    def test_action_records_success_with_duration(self, env):
        _, journal, _ = env
        with journal.action(
            EventKind.STAGE_STARTED, EventKind.STAGE_FINISHED, "collect"
        ) as out:
            out["items"] = 14
        finished = journal.events(run_id=RUN)[1]
        assert finished["outcome"] == "ok"
        assert finished["payload"]["items"] == 14
        assert finished["duration_ms"] is not None

    def test_keyboard_interrupt_is_recorded(self, env):
        """A killed run must not look like a clean one."""
        _, journal, _ = env
        with pytest.raises(KeyboardInterrupt):
            with journal.action(
                EventKind.STAGE_STARTED, EventKind.STAGE_FINISHED, "enrich"
            ):
                raise KeyboardInterrupt
        assert journal.events(run_id=RUN)[1]["outcome"] == "failed"


class TestCheckpoints:
    def test_checkpoint_marks_a_stage_done(self, env):
        _, journal, _ = env
        journal.checkpoint("collect", item_count=42)
        assert journal.completed_stages()["collect"]["item_count"] == 42

    def test_checkpoint_is_idempotent(self, env):
        _, journal, _ = env
        journal.checkpoint("collect", item_count=1)
        journal.checkpoint("collect", item_count=99)
        stages = journal.completed_stages()
        assert len(stages) == 1
        assert stages["collect"]["item_count"] == 99

    def test_state_payload_survives(self, env):
        _, journal, _ = env
        journal.checkpoint("enrich", item_count=3, cursor="page-2")
        assert journal.completed_stages()["enrich"]["state"]["cursor"] == "page-2"


class TestSupervisor:
    def test_finished_and_delivered_run_is_healthy(self, env):
        conn, journal, _ = env
        start_run(conn, status="ok", finished=NOW + timedelta(minutes=4))
        journal.record(EventKind.DELIVERY_SENT, actor="telegram")
        diagnosis = Supervisor(conn, journal).diagnose(
            RUN, now=NOW + timedelta(minutes=10)
        )
        assert diagnosis.state is RunState.HEALTHY
        assert diagnosis.recommended is Action.NOTHING

    def test_signals_written_but_never_delivered_resumes(self, env):
        conn, journal, _ = env
        start_run(conn, status="ok", finished=NOW + timedelta(minutes=4))
        publish_signals(conn, RUN, [make_signal()])
        diagnosis = Supervisor(conn, journal).diagnose(
            RUN, now=NOW + timedelta(minutes=10)
        )
        assert diagnosis.state is RunState.NEVER_DELIVERED
        assert diagnosis.recommended is Action.RESUME

    def test_a_hung_run_is_stalled_not_running(self, env):
        conn, journal, _ = env
        start_run(conn, status="running")
        journal.checkpoint("collect", item_count=10)
        diagnosis = Supervisor(conn, journal).diagnose(
            RUN, now=NOW + timedelta(hours=2)
        )
        assert diagnosis.state is RunState.STALLED
        assert diagnosis.recommended is Action.RESUME
        assert diagnosis.next_stage == "cluster"

    def test_a_young_run_is_left_alone(self, env):
        conn, journal, _ = env
        start_run(conn, status="running")
        diagnosis = Supervisor(conn, journal).diagnose(
            RUN, now=NOW + timedelta(minutes=3)
        )
        assert diagnosis.state is RunState.RUNNING
        assert diagnosis.recommended is Action.NOTHING

    def test_a_failed_run_without_checkpoints_restarts(self, env):
        conn, journal, _ = env
        start_run(conn, status="failed")
        diagnosis = Supervisor(conn, journal).diagnose(
            RUN, now=NOW + timedelta(minutes=10)
        )
        assert diagnosis.recommended is Action.RESTART

    def test_a_failed_run_with_checkpoints_resumes_from_the_gap(self, env):
        conn, journal, _ = env
        start_run(conn, status="failed")
        for stage in ("collect", "cluster", "filter"):
            journal.checkpoint(stage)
        diagnosis = Supervisor(conn, journal).diagnose(
            RUN, now=NOW + timedelta(minutes=10)
        )
        assert diagnosis.recommended is Action.RESUME
        assert diagnosis.next_stage == "enrich"

    def test_resume_plan_lists_remaining_stages_in_order(self, env):
        conn, journal, _ = env
        start_run(conn, status="failed")
        journal.checkpoint("collect")
        journal.checkpoint("cluster")
        plan = Supervisor(conn, journal).resume_plan(RUN)
        assert plan[:2] == ["filter", "enrich"]
        assert "collect" not in plan

    def test_failures_are_collected_into_the_diagnosis(self, env):
        conn, journal, _ = env
        start_run(conn, status="failed")
        journal.record(
            EventKind.SOURCE_FAILED,
            actor="cursor_changelog",
            outcome=Outcome.FAILED,
            error="HTTP 500",
        )
        diagnosis = Supervisor(conn, journal).diagnose(
            RUN, now=NOW + timedelta(minutes=10)
        )
        assert "cursor_changelog: HTTP 500" in diagnosis.failures

    def test_unknown_run_returns_none(self, env):
        conn, journal, _ = env
        assert Supervisor(conn, journal).diagnose("nope") is None

    def test_scan_reports_only_unhealthy_runs(self, env):
        conn, journal, _ = env
        start_run(conn, run_id="good", status="ok", finished=NOW)
        Journal(conn, log_dir=journal.log_dir, run_id="good").record(
            EventKind.DELIVERY_SENT, actor="telegram"
        )
        start_run(conn, run_id="bad", status="failed")
        problems = Supervisor(conn, journal).scan(now=NOW + timedelta(hours=1))
        assert [d.run_id for d in problems] == ["bad"]

    @staticmethod
    def _delivered(conn, run_id, channel="telegram", status="ok"):
        """Write the delivery record the run log keeps in log_json."""
        import json

        conn.execute(
            "UPDATE runs SET log_json = ? WHERE run_id = ?",
            (json.dumps({"delivery": [{"channel": channel, "status": status}]}), run_id),
        )
        conn.commit()

    def test_missed_days_catches_an_agent_that_stopped_running(self, env):
        """A daily agent gone silent is indistinguishable from a quiet day."""
        conn, journal, _ = env
        start_run(conn, run_id="d1", status="ok",
                  started=NOW - timedelta(days=1), finished=NOW)
        self._delivered(conn, "d1")

        missed = Supervisor(conn, journal).missed_days(expected_days=3, now=NOW)

        assert "2026-08-16" not in missed
        assert missed == ["2026-08-14", "2026-08-15"]

    def test_a_finished_run_that_delivered_nothing_does_not_cover_the_day(self, env):
        """The query asked for status='ok', which knows nothing about delivery:
        a week where the reader received nothing reported full coverage."""
        conn, journal, _ = env
        start_run(conn, run_id="d2", status="ok",
                  started=NOW - timedelta(days=1), finished=NOW)

        missed = Supervisor(conn, journal).missed_days(expected_days=3, now=NOW)

        assert "2026-08-16" in missed

    def test_a_run_whose_only_channel_failed_does_not_cover_it_either(self, env):
        conn, journal, _ = env
        start_run(conn, run_id="d3", status="ok",
                  started=NOW - timedelta(days=1), finished=NOW)
        self._delivered(conn, "d3", channel="email", status="failed")

        assert "2026-08-16" in Supervisor(conn, journal).missed_days(
            expected_days=3, now=NOW
        )

    def test_report_is_serializable_for_a_recovery_agent(self, env):
        conn, journal, _ = env
        start_run(conn, status="failed")
        journal.checkpoint("collect")
        report = Supervisor(conn, journal).report(now=NOW + timedelta(hours=1))
        assert json.loads(json.dumps(report))
        assert report["actions"][0]["resume_from"] == "cluster"


class TestJournalWithRunLog:
    def test_both_writers_share_one_database(self, env):
        conn, journal, _ = env
        log = RunLog(conn, RUN, date(2026, 8, 17))
        with log.stage("collect", in_count=3) as record:
            record["out_count"] = 3
        journal.checkpoint("collect", item_count=3)
        log.finish("ok")
        assert journal.completed_stages()["collect"]["item_count"] == 3
        assert (
            conn.execute("SELECT status FROM runs WHERE run_id = ?", (RUN,)).fetchone()[
                0
            ]
            == "ok"
        )


class TestAStallIsWrittenDown:
    """The supervisor could always see a hung run. Nothing recorded it.

    Eight runs sat at status `running` into a second day. Anything reading
    "the latest run" — the audit script included — picks up a zombie and finds
    a perfectly consistent set of zeroes. A green report about a run that never
    happened is worse than a red one about a run that failed.
    """

    def test_a_hung_run_gets_its_verdict_in_the_store(self, tmp_path):
        conn = init_db(tmp_path / "r.db")
        journal = Journal(conn, log_dir=tmp_path / "logs", run_id="sup")
        long_ago = (datetime.now(UTC) - timedelta(hours=9)).isoformat()
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, for_date) VALUES "
            "('hung', ?, 'running', '2026-08-17')", (long_ago,)
        )
        conn.commit()

        closed = Supervisor(conn, journal).close_stalled()

        assert closed == ["hung"]
        status = conn.execute(
            "SELECT status FROM runs WHERE run_id = 'hung'"
        ).fetchone()[0]
        assert status == "stalled"

    def test_a_run_still_going_is_left_alone(self, tmp_path):
        conn = init_db(tmp_path / "r.db")
        journal = Journal(conn, log_dir=tmp_path / "logs", run_id="sup")
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, for_date) VALUES "
            "('live', ?, 'running', '2026-08-18')",
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()

        assert Supervisor(conn, journal).close_stalled() == []
        status = conn.execute(
            "SELECT status FROM runs WHERE run_id = 'live'"
        ).fetchone()[0]
        assert status == "running"
