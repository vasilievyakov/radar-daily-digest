from datetime import UTC, date, datetime, timedelta

import pytest

from radar.db import init_db, publish_signals
from radar.deliver import deliver, wanted
from radar.journal import EventKind, Journal
from radar.models import ChangeType, Signal, SignalType, Tier
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


class TestTheCommandsActuallyRun:
    """Executes the wiring instead of reading it.

    The previous versions of these checks used `inspect.getsource` and `in`.
    A director gutted every wiring line into a dead branch, left the searched
    substrings behind, and the whole suite stayed green. Names in Python
    resolve at call time, so the only proof is a call.
    """

    def test_the_run_command_executes_end_to_end(self, tmp_path, monkeypatch):
        from radar import cli
        from radar.contracts import EnrichResult

        class Stub:
            def enrich(self, item, source):
                return EnrichResult(source_id="s", url=item.url, facts=[])

        monkeypatch.setattr(cli, "_build_enricher", lambda *a, **k: Stub())
        monkeypatch.setattr(
            "radar.run.collect_all", lambda *a, **k: ([], [])
        )
        code = cli.main([
            "--db", str(tmp_path / "r.db"),
            "--cache", str(tmp_path / "cache"),
            "run", "--no-filter", "--log-dir", str(tmp_path / "logs"),
        ])
        assert code == 0
        # A run with nothing collected still has to publish a quiet day.
        import sqlite3

        conn = sqlite3.connect(tmp_path / "r.db")
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        conn.close()

    def test_the_run_command_records_what_it_spent(self, tmp_path, monkeypatch):
        """Walks the same lines a paid run walks: a call is logged, and the
        cost has to arrive in `model_calls`. Gutting `call_log.write` into a
        dead branch left every other test green."""
        import sqlite3

        from radar import cli
        from radar.contracts import EnrichResult

        class Paying:
            """Reports a call the way the real backend does."""

            def __init__(self, call_log):
                self.call_log = call_log

            def enrich(self, item, source):
                self.call_log.model_call(
                    stage="enrich",
                    model="anthropic/claude-haiku-4.5",
                    tokens_in=1500,
                    tokens_out=200,
                    cost_usd=0.0079,
                )
                return EnrichResult(source_id="s", url=item.url, facts=[])

        monkeypatch.setattr(cli, "_build_enricher", lambda cfg, a, f, log: Paying(log))

        from radar.adapters.base import CollectedItem
        from radar.collect import SourceOutcome
        from radar.models import SourceStatus

        item = CollectedItem(
            url="https://example.test/a#one", title="Отключение модели",
            raw_text="текст материала",
            # A real source id: enrichment now skips material whose source
            # vanished from the config rather than enriching against nothing,
            # and an invented id would exercise that path instead of this one.
            extra={"source_id": "anthropic_model_deprecations", "source_priority": 1},
        )
        monkeypatch.setattr(
            "radar.run.collect_all",
            lambda *a, **k: (
                [item],
                [SourceOutcome("anthropic_model_deprecations", SourceStatus.OK, items=[item])],
            ),
        )

        db = tmp_path / "r.db"
        assert cli.main([
            "--db", str(db), "--cache", str(tmp_path / "cache"),
            "run", "--no-filter", "--log-dir", str(tmp_path / "logs"),
        ]) == 0

        conn = sqlite3.connect(db)
        calls, tokens, cost = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_in), 0), COALESCE(SUM(cost_usd), 0) "
            "FROM model_calls"
        ).fetchone()
        conn.close()
        assert calls == 1, "вызов модели не записан в model_calls"
        assert tokens == 1500, "токены потеряны по дороге"
        assert cost > 0, "стоимость прогона осталась нулём"

    def test_the_deliver_flag_reaches_the_surface(self, tmp_path, monkeypatch):
        """`--deliver` was inert and nothing noticed; the wrapper could also
        call a method that does not exist."""
        from radar import cli
        from radar.contracts import EnrichResult

        sent: list = []

        class Spy:
            def send_digest(self, signals):
                sent.append(signals)
                return type("R", (), {"ok": True, "message_id": 1, "error": None})()

        monkeypatch.setattr("radar.surfaces.telegram.send_digest", Spy().send_digest)
        monkeypatch.setattr(
            cli, "_build_enricher",
            lambda *a, **k: type("E", (), {
                "enrich": lambda self, i, s: EnrichResult(source_id="s", url=i.url)
            })(),
        )
        monkeypatch.setattr("radar.run.collect_all", lambda *a, **k: ([], []))

        cli.main([
            "--db", str(tmp_path / "r.db"), "--cache", str(tmp_path / "cache"),
            "run", "--no-filter", "--deliver", "--log-dir", str(tmp_path / "logs"),
        ])
        assert sent, "--deliver не дошёл до поверхности"

    def test_the_supervise_command_executes(self, tmp_path):
        from radar import cli

        code = cli.main([
            "--db", str(tmp_path / "r.db"),
            "supervise", "--log-dir", str(tmp_path / "logs"),
        ])
        # Zero when everything is healthy, one when something needs attention;
        # either way it must not raise.
        assert code in (0, 1)

    def test_a_misspelled_surface_attribute_is_not_swallowed(self, tmp_path):
        """Three incidents tonight arrived as calm status messages."""
        import pytest as _pytest

        from radar.db import init_db, publish_signals
        from radar.deliver import deliver
        from radar.journal import Journal

        conn = init_db(tmp_path / "r.db")
        publish_signals(conn, RUN, [make_signal()])
        journal = Journal(conn, log_dir=tmp_path / "logs", run_id=RUN)

        class Misspelled:
            name = "telegram"

            def send_digest(self, signals):
                return self.send_digest_to_channel(signals)  # no such method

        with _pytest.raises(AttributeError):
            deliver(conn, {"telegram": Misspelled()}, RUN, journal)
        conn.close()


class TestStandInSatisfiesTheRealInterface:
    """Fifth instance of one class of error in a night, and the second one
    that arrived inside a fix for the previous ones.

    `_CallLog` stands in for `RunLog` so the sqlite connection stays on its
    own thread. It implemented three methods out of nine, enrichment called
    `filtered()` to record why it dropped an event, and four materials per run
    disappeared into AttributeError while 1321 tests stayed green.
    """

    def test_it_implements_every_method_it_stands_in_for(self):
        from radar.cli import _CallLog
        from radar.runlog import RunLog

        expected = {m for m in dir(RunLog) if not m.startswith("_")}
        missing = sorted(m for m in expected if not hasattr(_CallLog, m))
        assert missing == [], (
            f"подставка не реализует {missing}: вызов такого метода станет "
            "AttributeError посреди оплаченного прогона"
        )

    def test_the_call_it_actually_failed_on_works(self):
        from radar.cli import _CallLog

        log = _CallLog()
        log.filtered(url="https://example.test/a", title="Заголовок",
                     reason_code="unsupported_quantifier", stage="enrich")
        assert log.drops

    def test_buffered_drops_reach_the_database(self, tmp_path):
        import sqlite3

        from radar.cli import _CallLog
        from radar.db import init_db

        conn = init_db(tmp_path / "r.db")
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, for_date) "
            "VALUES ('r1', '2026-08-18T00:00:00+00:00', 'running', '2026-08-18')"
        )
        conn.commit()

        log = _CallLog()
        log.filtered(url="https://example.test/a", title="Заголовок",
                     reason_code="vendor_unresolved", stage="enrich")
        log.write(conn, "r1")

        rows = conn.execute(
            "SELECT reason_code FROM filtered_items WHERE run_id = 'r1'"
        ).fetchall()
        conn.close()
        assert rows and rows[0][0] == "vendor_unresolved"


class TestABrokenChannelLeavesATrace:
    def test_a_code_error_is_recorded_before_it_propagates(self, env):
        """Raising before the record left the supervisor with
        NEVER_DELIVERED and no reason attached."""
        import pytest as _pytest

        conn, journal = env
        publish_signals(conn, RUN, [make_signal()])

        class Misspelled:
            name = "telegram"

            def send_digest(self, signals):
                return self.nope(signals)

        with _pytest.raises(AttributeError):
            deliver(conn, {"telegram": Misspelled()}, RUN, journal)

        events = [e for e in journal.events(run_id=RUN)
                  if e["kind"] == str(EventKind.DELIVERY_FAILED)]
        assert events, "отказ канала не записан в журнал"
        assert "AttributeError" in str(events[0]["payload"].get("error"))


class TestTheSchedulerExists:
    """PRD measures "share of runs that ended in delivery >= 95%". With no
    scheduler in the repository that number is zero by construction, and the
    supervisor guards a job nobody starts."""

    def test_both_jobs_are_valid_plists(self):
        import plistlib
        from pathlib import Path

        files = sorted(Path("deploy").glob("*.plist"))
        assert len(files) == 2, "нет расписания для прогона и наблюдения"
        for path in files:
            plist = plistlib.loads(path.read_bytes())
            assert plist["Label"].startswith("com.radar.")
            assert plist["ProgramArguments"]

    def test_the_daily_job_delivers(self):
        """Without --deliver the supervisor sees NEVER_DELIVERED every day."""
        import plistlib
        from pathlib import Path

        plist = plistlib.loads(Path("deploy/com.radar.daily.plist").read_bytes())
        command = " ".join(plist["ProgramArguments"])
        assert "run" in command and "--deliver" in command

    def test_the_daily_job_uses_a_calendar_not_an_interval(self):
        """cron does not fire on a sleeping laptop and skips the day in
        silence; launchd runs the job on wake."""
        import plistlib
        from pathlib import Path

        plist = plistlib.loads(Path("deploy/com.radar.daily.plist").read_bytes())
        assert "StartCalendarInterval" in plist

    def test_supervision_runs_apart_from_the_run_it_watches(self):
        """An instrument that only runs inside the job it watches cannot
        report that the job never started."""
        import plistlib
        from pathlib import Path

        plist = plistlib.loads(Path("deploy/com.radar.supervise.plist").read_bytes())
        assert "StartInterval" in plist
        assert "supervise" in " ".join(plist["ProgramArguments"])


class TestOneRunHasOneCost:
    """The page had to say "counter says $0.42, rows say $4.27" because the
    run row counted only what passed through the run log in memory while
    enrichment wrote through the buffered log."""

    def test_the_run_row_matches_the_itemised_calls(self, tmp_path, monkeypatch):
        import sqlite3

        from radar import cli
        from radar.contracts import EnrichResult

        class Paying:
            def __init__(self, call_log):
                self.call_log = call_log

            def enrich(self, item, source):
                self.call_log.model_call(
                    stage="enrich", model="m", tokens_in=100, tokens_out=20,
                    cost_usd=0.05,
                )
                return EnrichResult(source_id="s", url=item.url, facts=[])

        monkeypatch.setattr(cli, "_build_enricher", lambda cfg, a, f, log: Paying(log))

        from radar.adapters.base import CollectedItem
        from radar.collect import SourceOutcome
        from radar.models import SourceStatus

        item = CollectedItem(
            url="https://example.test/a#one", title="Отключение",
            raw_text="текст",
            extra={"source_id": "anthropic_model_deprecations", "source_priority": 1},
        )
        monkeypatch.setattr(
            "radar.run.collect_all",
            lambda *a, **k: (
                [item],
                [SourceOutcome("anthropic_model_deprecations", SourceStatus.OK, items=[item])],
            ),
        )

        db = tmp_path / "r.db"
        # With --deliver, because delivery flushes the run log and an earlier
        # version of the reconciliation was silently undone by that flush.
        cli.main([
            "--db", str(db), "--cache", str(tmp_path / "c"),
            "run", "--no-filter", "--deliver", "--log-dir", str(tmp_path / "l"),
        ])
        conn = sqlite3.connect(db)
        run_cost, run_calls = conn.execute(
            "SELECT cost_usd, model_calls FROM runs"
        ).fetchone()
        rows_cost, rows_calls = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM model_calls"
        ).fetchone()
        conn.close()
        assert abs(run_cost - rows_cost) < 1e-9, "две разные цифры за один прогон"
        assert run_calls == rows_calls


class TestQuietDayIsStillDelivered:
    """PUB-4: silence reaches the reader as a message. Gating delivery on
    having digest items skipped the channel on exactly the day the product
    exists to speak about."""

    def test_the_channel_is_touched_when_there_is_nothing_to_report(
        self, tmp_path, monkeypatch
    ):
        from radar import cli
        from radar.contracts import EnrichResult

        sent: list = []

        def spy(signals):
            sent.append(signals)
            return type("R", (), {"ok": True, "message_id": 1, "error": None})()

        monkeypatch.setattr("radar.surfaces.telegram.send_digest", spy)
        monkeypatch.setattr(
            cli, "_build_enricher",
            lambda *a, **k: type("E", (), {
                "enrich": lambda self, i, s: EnrichResult(source_id="s", url=i.url)
            })(),
        )
        # Sources answered; nothing of theirs was worth publishing. That is a
        # quiet day, and it is the case PUB-4 is about.
        from radar.collect import SourceOutcome
        from radar.models import SourceStatus

        monkeypatch.setattr(
            "radar.run.collect_all",
            lambda *a, **k: ([], [SourceOutcome("anthropic_model_deprecations",
                                                SourceStatus.QUIET)]),
        )

        cli.main([
            "--db", str(tmp_path / "r.db"), "--cache", str(tmp_path / "c"),
            "run", "--no-filter", "--deliver", "--log-dir", str(tmp_path / "l"),
        ])
        assert sent, "тихий день не доставлен"
        assert sent[0][0].signal_type.value == "quiet_day"

    def test_a_run_that_reached_nothing_is_delivered_as_a_failure(
        self, tmp_path, monkeypatch
    ):
        """Not as a quiet day: the channel must not carry a calm sentence
        about a morning the agent never saw."""
        from radar import cli
        from radar.contracts import EnrichResult

        sent: list = []

        def spy(signals):
            sent.append(signals)
            return type("R", (), {"ok": True, "message_id": 1, "error": None})()

        monkeypatch.setattr("radar.surfaces.telegram.send_digest", spy)
        monkeypatch.setattr(
            cli, "_build_enricher",
            lambda *a, **k: type("E", (), {
                "enrich": lambda self, i, s: EnrichResult(source_id="s", url=i.url)
            })(),
        )
        monkeypatch.setattr("radar.run.collect_all", lambda *a, **k: ([], []))

        cli.main([
            "--db", str(tmp_path / "r.db"), "--cache", str(tmp_path / "c"),
            "run", "--no-filter", "--deliver", "--log-dir", str(tmp_path / "l"),
        ])

        assert sent, "молчание не доставлено вовсе"
        assert sent[0][0].signal_type.value == "run_failure"
        assert "ничего не изменилось" not in sent[0][0].headline


class TestTheReaderDecidesWhatWakesThem:
    """The only dial the product offers, and the one a grown-up asks for first.

    Everything the core judged significant is published either way (PUB-1);
    this decides what is worth interrupting a morning for. A product that walks
    into the network by itself and publishes by itself has to let the person it
    publishes to say "not this".
    """

    @staticmethod
    def _signal(signal_id, change_type, tier=Tier.STANDARD, kind=SignalType.DIGEST_ITEM):
        return Signal(
            signal_id=signal_id, run_id="run-1", signal_type=kind,
            created_at=datetime(2026, 8, 18, tzinfo=UTC), for_date=date(2026, 8, 18),
            headline=f"событие {signal_id}", change_type=change_type, tier=tier,
        )

    def test_only_the_named_types_go_out(self):
        signals = [
            self._signal("a", ChangeType.DEPRECATION),
            self._signal("b", ChangeType.RELEASE),
            self._signal("c", ChangeType.PRICING),
        ]
        keep, held = wanted(signals, {"wake_me_for": ["deprecation", "pricing"]})

        assert [s.signal_id for s in keep] == ["a", "c"]
        assert [s.signal_id for s in held] == ["b"]

    def test_a_tier_floor_holds_the_rest_back(self):
        signals = [
            self._signal("a", ChangeType.DEPRECATION, Tier.LEAD),
            self._signal("b", ChangeType.DEPRECATION, Tier.STANDARD),
        ]
        keep, held = wanted(signals, {"min_tier": "lead"})

        assert [s.signal_id for s in keep] == ["a"]

    def test_a_quiet_day_is_never_held_back(self):
        """Silencing the agent's statements about itself is how a dead agent
        looks healthy."""
        signals = [
            self._signal("q", None, kind=SignalType.QUIET_DAY),
            self._signal("f", None, kind=SignalType.RUN_FAILURE),
            self._signal("a", ChangeType.RELEASE),
        ]
        keep, held = wanted(signals, {"wake_me_for": ["deprecation"], "min_tier": "lead"})

        assert {s.signal_id for s in keep} == {"q", "f"}
        assert [s.signal_id for s in held] == ["a"]

    def test_an_empty_dial_sends_everything(self):
        signals = [self._signal("a", ChangeType.RELEASE)]
        keep, held = wanted(signals, {})

        assert keep == signals
        assert held == []


class TestTheBufferForwardsWhatItWasGiven:
    """`_CallLog` is the path every CLI run takes.

    A column it does not forward is a column that is always empty in practice:
    forty-four cache hits in one run reported "would have cost nothing", so the
    page could not print the honest comparison it had just been taught to
    print.
    """

    def test_the_would_have_cost_reaches_the_table(self, tmp_path):
        from radar import cli

        conn = init_db(tmp_path / "r.db")
        log = cli._CallLog()
        log.model_call(stage="enrich", model="haiku", tokens_in=10, tokens_out=5,
                       cost_usd=0.0, cached=True, original_cost_usd=0.0137)
        log.write(conn, "run-buf")

        row = conn.execute(
            "SELECT cost_usd, original_cost_usd FROM model_calls WHERE run_id = ?",
            ("run-buf",),
        ).fetchone()
        assert row["cost_usd"] == 0.0
        assert abs(row["original_cost_usd"] - 0.0137) < 1e-9

    def test_a_live_call_reports_the_same_figure_twice(self, tmp_path):
        from radar import cli

        conn = init_db(tmp_path / "r.db")
        log = cli._CallLog()
        log.model_call(stage="filter", model="haiku", cost_usd=0.17, cached=False)
        log.write(conn, "run-live")

        row = conn.execute(
            "SELECT cost_usd, original_cost_usd FROM model_calls WHERE run_id = ?",
            ("run-live",),
        ).fetchone()
        assert row["original_cost_usd"] == row["cost_usd"] == 0.17


class TestEveryDeclaredChannelIsActuallyWired:
    """`delivery.channels` said [telegram, email]; only Telegram was built.

    The email surface was written, tested and rendered for two days without a
    single run ever being handed to it — the config declared it, the run log
    would have shown it, and nothing called it. A channel nobody calls is
    indistinguishable from a channel that always fails silently.
    """

    def test_the_run_builds_both_channels(self, tmp_path, monkeypatch):
        from radar import cli

        built: list[str] = []

        class Spy:
            def __init__(self, name):
                self.name = name

            def send_digest(self, signals):
                built.append(self.name)
                return type("R", (), {"ok": True, "message_id": 1, "error": None})()

        monkeypatch.setattr(
            "radar.surfaces.telegram.send_digest",
            lambda signals: built.append("telegram") or type(
                "R", (), {"ok": True, "message_id": 1, "error": None}
            )(),
        )
        monkeypatch.setattr(
            "radar.surfaces.email.send_digest",
            lambda digest, **kw: built.append("email") or type(
                "R", (), {"ok": True, "message_id": 2, "error": None}
            )(),
        )

        conn = init_db(tmp_path / "r.db")
        signal = Signal(
            signal_id="s1", run_id="run-1", signal_type=SignalType.DIGEST_ITEM,
            created_at=datetime(2026, 8, 18, tzinfo=UTC), for_date=date(2026, 8, 18),
            headline="Anthropic отключает claude-3-opus", tier=Tier.LEAD,
        )
        publish_signals(conn, "run-1", [signal])

        from radar.runlog import RunLog

        run = type(
            "R", (), {
                "run_id": "run-1",
                "journal": Journal(conn, log_dir=tmp_path / "l", run_id="run-1"),
                "log": RunLog(conn, "run-1", date(2026, 8, 18)),
            },
        )()
        cli._deliver_run(conn, run, None)

        assert set(built) == {"telegram", "email"}
