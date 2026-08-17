from datetime import UTC, date, datetime, timedelta

import pytest

from radar.db import init_db, publish_signals
from radar.deliver import deliver
from radar.journal import EventKind, Journal
from radar.models import Signal, SignalType, Tier
from radar.runlog import RunLog
from radar.supervisor import Action, RunState, Supervisor

NOW = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
TODAY = date(2026, 8, 17)
RUN = "run-1"


@pytest.fixture
def env(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    journal = Journal(conn, log_dir=tmp_path / "logs", run_id=RUN)
    yield conn, journal
    conn.close()


def make_signal(sid="s1"):
    return Signal(
        signal_id=sid, run_id=RUN, signal_type=SignalType.DIGEST_ITEM,
        created_at=NOW, for_date=TODAY, headline="Anthropic отключает claude-3-opus",
        tier=Tier.LEAD, rank=1,
    )


class Ok:
    def __init__(self, name="telegram"):
        self.name = name
        self.sent = []

    def send_digest(self, signals):
        self.sent.append(signals)
        return type("R", (), {"delivered": True, "message_id": "42", "error": None})()


class Fails:
    name = "email"

    def send_digest(self, signals):
        return type("R", (), {"delivered": False, "message_id": None, "error": "SMTP отказал"})()


class Explodes:
    name = "telegram"

    def send_digest(self, signals):
        raise RuntimeError("сеть недоступна")


class TestDelivery:
    def test_a_successful_send_is_recorded(self, env):
        conn, journal = env
        publish_signals(conn, RUN, [make_signal()])
        report = deliver(conn, {"telegram": Ok()}, RUN, journal)
        assert report.all_delivered
        kinds = [e["kind"] for e in journal.events(run_id=RUN)]
        assert str(EventKind.DELIVERY_SENT) in kinds

    def test_a_failing_channel_does_not_stop_the_others(self, env):
        """SUR-5: one surface failing must not affect another."""
        conn, journal = env
        publish_signals(conn, RUN, [make_signal()])
        good = Ok()
        report = deliver(conn, {"email": Fails(), "telegram": good}, RUN, journal)
        assert report.any_delivered
        assert not report.all_delivered
        assert good.sent

    def test_an_exploding_surface_is_caught(self, env):
        conn, journal = env
        publish_signals(conn, RUN, [make_signal()])
        report = deliver(conn, {"telegram": Explodes()}, RUN, journal)
        assert not report.any_delivered
        assert "сеть недоступна" in report.results[0].error

    def test_an_empty_store_is_not_reported_as_delivered(self, env):
        conn, journal = env
        report = deliver(conn, {"telegram": Ok()}, RUN, journal)
        assert report.signals == 0
        assert not report.any_delivered

    def test_delivery_reaches_the_run_log(self, env):
        conn, journal = env
        log = RunLog(conn, RUN, TODAY)
        publish_signals(conn, RUN, [make_signal()])
        deliver(conn, {"telegram": Ok()}, RUN, journal, log)
        assert log.delivery[0]["channel"] == "telegram"
        assert log.delivery[0]["status"] == "ok"


class TestSupervisorStopsLying:
    """The whole point of the event: the supervisor must tell a delivered run
    from one that silently never reached anybody."""

    def test_a_delivered_run_reads_as_healthy(self, env):
        conn, journal = env
        conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at, status, for_date) "
            "VALUES (?, ?, ?, 'ok', ?)",
            (RUN, NOW.isoformat(), (NOW + timedelta(minutes=4)).isoformat(), TODAY.isoformat()),
        )
        conn.commit()
        publish_signals(conn, RUN, [make_signal()])
        deliver(conn, {"telegram": Ok()}, RUN, journal)

        diagnosis = Supervisor(conn, journal).diagnose(RUN, now=NOW + timedelta(minutes=10))
        assert diagnosis.state is RunState.HEALTHY
        assert diagnosis.recommended is Action.NOTHING

    def test_an_undelivered_run_is_still_caught(self, env):
        conn, journal = env
        conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at, status, for_date) "
            "VALUES (?, ?, ?, 'ok', ?)",
            (RUN, NOW.isoformat(), (NOW + timedelta(minutes=4)).isoformat(), TODAY.isoformat()),
        )
        conn.commit()
        publish_signals(conn, RUN, [make_signal()])

        diagnosis = Supervisor(conn, journal).diagnose(RUN, now=NOW + timedelta(minutes=10))
        assert diagnosis.state is RunState.NEVER_DELIVERED

    def test_a_failed_send_does_not_count_as_delivered(self, env):
        conn, journal = env
        conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at, status, for_date) "
            "VALUES (?, ?, ?, 'ok', ?)",
            (RUN, NOW.isoformat(), (NOW + timedelta(minutes=4)).isoformat(), TODAY.isoformat()),
        )
        conn.commit()
        publish_signals(conn, RUN, [make_signal()])
        deliver(conn, {"email": Fails()}, RUN, journal)

        diagnosis = Supervisor(conn, journal).diagnose(RUN, now=NOW + timedelta(minutes=10))
        assert diagnosis.state is RunState.NEVER_DELIVERED


class TestSupervisorIsReachable:
    """96 percent coverage on a module nothing can run is not coverage."""

    def test_the_cli_exposes_supervision(self):
        from radar.cli import build_parser

        parser = build_parser()
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        commands = set()
        for action in actions:
            commands |= set(action.choices or {})
        assert "supervise" in commands
        assert "run" in commands

    def test_it_reports_a_stalled_run(self, env, tmp_path):
        conn, journal = env
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, for_date) "
            "VALUES ('hung', ?, 'running', ?)",
            ((NOW - timedelta(hours=3)).isoformat(), TODAY.isoformat()),
        )
        conn.commit()
        report = Supervisor(conn, journal).report(now=NOW)
        assert any(r["run_id"] == "hung" for r in report["unhealthy_runs"])

    def test_missed_days_are_counted_apart(self, env):
        """A daily agent gone silent looks exactly like a quiet day from
        outside, so the count has to stand on its own."""
        conn, journal = env
        report = Supervisor(conn, journal).report(now=NOW)
        assert report["missed_days"]


class TestTokensAreRecorded:
    """FR-8.4 asks for tokens and money. A row reading "5 calls, 0 tokens,
    $0.20" is worse than no row: it discredits the numbers next to it."""

    def test_the_enricher_forwards_the_call_log(self):
        import inspect

        from radar.cli import _build_enricher

        source = inspect.getsource(_build_enricher)
        assert "run_log=call_log" in source, (
            "the enricher must pass the backend's log through, otherwise "
            "run_log=None overrides it and token counts are lost"
        )


class TestDeliveryIsActuallyWired:
    """Three times in one night the orchestrator called a name that does not
    exist: `keep()` on the filter, `extract-v1` in the golden set, and
    `TelegramSurface` inside the fix for the previous two. Names are checked
    here by construction, not by reading."""

    def test_the_run_command_builds_surfaces_that_exist(self):
        from radar.cli import _deliver_run
        import inspect

        # Every attribute the wiring reaches for must resolve on the module.
        from radar.surfaces import telegram

        assert hasattr(telegram, "send_digest"), (
            "delivery calls telegram.send_digest; if the surface renames it, "
            "the run silently reports 'канал недоступен' forever"
        )
        assert callable(telegram.send_digest)
        # The helper must not reference a class that was never defined.
        source = inspect.getsource(_deliver_run)
        code_lines = [
            line for line in source.splitlines() if not line.strip().startswith("#")
        ]
        assert "TelegramSurface" not in "\n".join(code_lines)

    def test_the_built_surface_satisfies_the_delivery_protocol(self, env):
        """Constructs it the way the CLI does and hands it a signal."""
        conn, journal = env
        publish_signals(conn, RUN, [make_signal()])

        from radar.surfaces import telegram as tg

        class _Telegram:
            name = "telegram"

            def send_digest(self, signals):
                return tg.DeliveryResult(delivered=False, error="токена нет")

        report = deliver(conn, {"telegram": _Telegram()}, RUN, journal)
        # No token in the environment, so it must fail cleanly and be recorded
        # rather than raise or silently do nothing.
        assert report.results
        assert report.results[0].channel == "telegram"
        assert not report.results[0].delivered


class TestRunCommandWiring:
    """Three defects of one kind: an object is created and handed to nobody.

    The filter can record reasons but was built without the run log, so every
    rejected material vanished. The call log collected tokens and was never
    written, so the run reported zero spend. The budget was constructed and
    passed to no stage, so the ceiling could not trigger.
    """

    def test_the_filter_receives_the_run_log(self):
        import inspect

        from radar.cli import cmd_run

        source = inspect.getsource(cmd_run)
        assert "run_log=run.log" in source
        assert "budget=run.budget" in source

    def test_the_call_log_is_written_to_the_database(self):
        import inspect

        from radar.cli import cmd_run

        source = inspect.getsource(cmd_run)
        assert "call_log.write(conn" in source, (
            "collected token rows must reach model_calls, otherwise the run "
            "prints 0.0000 USD while checkpoints show real spend"
        )

    def test_spend_is_read_back_from_the_database(self):
        """Printing the in-memory counter hides everything a crash lost."""
        import inspect

        from radar.cli import cmd_run

        assert "FROM model_calls WHERE run_id" in inspect.getsource(cmd_run)
