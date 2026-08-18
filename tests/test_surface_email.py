"""Email surface tests: no network, SMTP substituted everywhere."""

from __future__ import annotations

import ast
import smtplib
import sys
from datetime import UTC, date, datetime
from email import message_from_bytes
from email import policy as email_policy
from pathlib import Path

import pytest

from radar.db import init_db, publish_signals
from radar.models import (
    ChangeType,
    ContextLabel,
    DatePrecision,
    DeltaStatus,
    Fact,
    FactKind,
    Precedent,
    RunSummary,
    Signal,
    SignalType,
    Tier,
    UpcomingDeadline,
)
from radar.surfaces import email as surface
from radar.surfaces.email import (
    DeliveryResult,
    EmailConfigError,
    build_email,
    build_message,
    deliver,
    load_smtp_config,
    select_cards,
    send_digest,
)

TODAY = date(2026, 8, 17)
NOW = datetime(2026, 8, 17, 7, 0, tzinfo=UTC)
MODULE_PATH = Path(surface.__file__)

ENV = {
    "SMTP_HOST": "smtp.example.test",
    "SMTP_PORT": "2525",
    "SMTP_USER": "radar@example.test",
    "SMTP_PASSWORD": "secret",
    "SMTP_FROM": "radar@example.test",
    "SMTP_TO": "reader@example.test, second@example.test",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real socket attempt fails the test rather than leaving the box."""

    def forbidden(*args, **kwargs):
        raise AssertionError("the surface must not open a connection in tests")

    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)


class FakeSMTP:
    """Records the conversation instead of holding one."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls = False
        self.login_args = None
        self.sent = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.tls = True
        self.context = context

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.instances = []
    yield
    FakeSMTP.instances = []


def flat(text: str) -> str:
    """Compare against wrapped plain text without minding the line breaks."""
    return " ".join(text.split())


def digest_item(
    signal_id: str = "sig-001",
    tier: Tier = Tier.LEAD,
    headline: str = "Anthropic отключает claude-3-opus",
    **overrides,
) -> Signal:
    payload = dict(
        signal_id=signal_id,
        run_id="run-1",
        signal_type=SignalType.DIGEST_ITEM,
        created_at=NOW,
        for_date=TODAY,
        headline=headline,
        summary="Запросы к модели начнут возвращать ошибку.",
        why_it_matters="модель используется в двух ваших проектах",
        change_type=ChangeType.DEPRECATION,
        vendor="anthropic",
        product="claude-3-opus",
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                source_url="https://docs.claude.com/deprecations",
                evidence="claude-3-opus will be retired on October 15, 2026",
                confidence="high",
                evidence_verified=True,
            )
        ],
        primary_url="https://docs.claude.com/deprecations",
        delta_status=DeltaStatus.CONTINUING,
        delta_note="добавлена дата отключения",
        days_tracked=3,
        score=93,
        score_rationale="высокая ставка по стеку читателя",
        rank=1,
        tier=tier,
        run_log_url="https://radar.local/runs/run-1",
    )
    payload.update(overrides)
    return Signal(**payload)


def quiet_day(**overrides) -> Signal:
    payload = dict(
        signal_id="sig-quiet",
        run_id="run-2",
        signal_type=SignalType.QUIET_DAY,
        created_at=NOW,
        for_date=TODAY,
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                source_url="https://docs.claude.com/deprecations",
                evidence="claude-3-opus will be retired on October 15, 2026",
            ),
            Fact(
                kind=FactKind.EFFECTIVE_DATE,
                value="2026-11-01",
                source_url="https://platform.openai.com/docs/guides/rate-limits",
                evidence="new limits take effect on November 1",
            ),
        ],
        stats={"sources_checked": 14, "filtered_out": 23},
        run_log_url="https://radar.local/runs/run-2",
    )
    payload.update(overrides)
    return Signal(**payload)


def run_failure(**overrides) -> Signal:
    payload = dict(
        signal_id="sig-fail",
        run_id="run-3",
        signal_type=SignalType.RUN_FAILURE,
        created_at=NOW,
        for_date=TODAY,
        failure_reason="сбой на стадии обогащения",
        summary="Последняя удачная сводка — 16 августа.",
        stats={"collected": 34},
        run_log_url="https://radar.local/runs/run-3",
    )
    payload.update(overrides)
    return Signal(**payload)


# --------------------------------------------------------------------------
# Both parts of the letter
# --------------------------------------------------------------------------


def test_message_is_multipart_alternative_with_both_parts():
    digest = build_email([digest_item()], today=TODAY)
    message = build_message(digest, "radar@example.test", ["reader@example.test"])

    assert message.get_content_type() == "multipart/alternative"
    parts = message.get_payload()
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert "Anthropic отключает claude-3-opus" in parts[0].get_content()
    assert "<table" in parts[1].get_content()


def test_plain_text_is_a_real_letter_not_a_stub():
    digest = build_email([digest_item()], today=TODAY)
    text = digest.text

    assert "Anthropic отключает claude-3-opus" in text
    assert "Запросы к модели начнут возвращать ошибку." in text
    assert "Почему это важно: модель используется в двух ваших проектах." in text
    assert "«claude-3-opus will be retired on October 15, 2026»" in text
    assert "https://docs.claude.com/deprecations" in text
    assert "Лог прогона: https://radar.local/runs/run-1" in text
    assert "<" not in text and "&nbsp;" not in text
    assert max(len(line) for line in text.splitlines() if " " in line) <= 80


def test_html_and_text_carry_the_same_cards():
    digest = build_email(
        [digest_item(), digest_item("sig-002", Tier.STANDARD, "OpenAI меняет лимиты")],
        today=TODAY,
    )
    for headline in ("Anthropic отключает claude-3-opus", "OpenAI меняет лимиты"):
        assert headline in digest.text
        assert headline in digest.html


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------


def test_html_is_escaped_in_every_field():
    hostile = '<script>alert("x")</script> & <b>'
    signal = digest_item(
        headline=f"Anthropic {hostile}",
        summary=f"Суть {hostile}",
        why_it_matters=f"Важно {hostile}",
        vendor=f"vendor {hostile}",
        facts=[
            Fact(
                kind=FactKind.VERSION,
                value=f"1.0 {hostile}",
                source_url="https://example.test/a",
                evidence=f"quote {hostile}",
            )
        ],
        context_label=ContextLabel.RECURRING,
        precedents=[
            Precedent(
                statement_id="st-1",
                text=f"Прецедент {hostile}",
                source_url="https://example.test/b",
                event_date=date(2026, 6, 1),
                vendor=f"vendor {hostile}",
                change_type=ChangeType.DEPRECATION,
            )
        ],
        delta_note=f"Дельта {hostile}",
    )
    html = build_email([signal], today=TODAY).html

    escaped = "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &lt;b&gt;"
    assert hostile not in html
    assert "<script" not in html
    assert "</script>" not in html
    # Every field carries the hostile string; every copy arrives escaped.
    assert html.count(escaped) >= 6
    assert hostile in build_email([signal], today=TODAY).text


def test_non_http_urls_never_become_links():
    signal = digest_item(
        primary_url="javascript:alert(1)",
        facts=[
            Fact(
                kind=FactKind.VERSION,
                value="1.0",
                source_url="javascript:alert(2)",
                evidence="quote",
            )
        ],
        run_log_url="ftp://example.test/log",
    )
    digest = build_email([signal], today=TODAY)

    assert "javascript:" not in digest.html
    assert "javascript:" not in digest.text
    assert "ftp://" not in digest.html


# --------------------------------------------------------------------------
# Subject
# --------------------------------------------------------------------------


def test_subject_of_a_regular_day_carries_the_main_fact():
    digest = build_email(
        [digest_item(), digest_item("sig-002", Tier.STANDARD, "OpenAI меняет лимиты")],
        today=TODAY,
    )

    assert digest.subject == "Anthropic отключает claude-3-opus"
    lowered = digest.subject.lower()
    for forbidden in ("дайджест", "radar", "радар", "прогон", "2026", "17.08"):
        assert forbidden not in lowered
    assert "пункт" not in lowered


def test_subject_is_truncated_on_a_word_boundary():
    long_headline = (
        "Anthropic отключает claude-3-opus и переводит всех клиентов "
        "на claude-opus-4 без переходного периода"
    )
    digest = build_email([digest_item(headline=long_headline)], today=TODAY)

    assert len(digest.subject) <= surface.SUBJECT_MAX
    assert digest.subject.endswith("…")
    assert digest.subject.rstrip("…") in long_headline


def test_subject_of_a_quiet_day():
    assert build_email([quiet_day()], today=TODAY).subject == "Сегодня без изменений"
    assert build_email([], today=TODAY).subject == "Сегодня без изменений"


def test_subject_of_a_failed_run():
    assert build_email([run_failure()], today=TODAY).subject == "Прогон не завершился"


# --------------------------------------------------------------------------
# The full card
# --------------------------------------------------------------------------


def test_full_card_unfolds_the_evidence_with_precedents():
    signal = digest_item(
        context_label=ContextLabel.RECURRING,
        precedents=[
            Precedent(
                statement_id="st-1",
                text="Anthropic объявила отключение claude-2.1",
                source_url="https://docs.claude.com/deprecations#claude-2",
                event_date=date(2026, 6, 1),
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            ),
            Precedent(
                statement_id="st-2",
                text="Anthropic объявила отключение claude-instant",
                source_url="https://docs.claude.com/deprecations#instant",
                event_date=date(2026, 5, 10),
                date_precision=DatePrecision.MONTH,
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            ),
        ],
    )
    digest = build_email([signal], today=TODAY)

    for rendered in (digest.text, digest.html):
        assert "Anthropic отключает claude-3-opus" in rendered
        assert "Запросы к модели начнут возвращать ошибку." in rendered
        assert "Почему это важно" in rendered
        assert "Дата отключения: 15 октября, через 59 дней" in rendered
        assert "claude-3-opus will be retired on October 15, 2026" in rendered
        assert "Похожее уже случалось" in rendered
        assert "Anthropic объявила отключение claude-2.1" in rendered
        assert "1 июня, 77 дней назад" in rendered
        assert "май 2026, 99 дней назад (день в источнике не указан)" in rendered
        assert "Продолжение: добавлена дата отключения." in rendered
        assert "История ведётся 3 дня." in rendered
    assert "docs.claude.com/deprecations#instant" in digest.html
    assert "https://docs.claude.com/deprecations#instant" in digest.text
    assert "https://radar.local/runs/run-1" in digest.text
    assert "sig-001" in digest.text


def test_summary_is_never_truncated():
    long_summary = "Очень длинная суть. " * 40
    digest = build_email([digest_item(summary=long_summary)], today=TODAY)
    words = long_summary.split()

    assert " ".join(digest.text.split()).count("Очень длинная суть.") == 40
    assert digest.html.count("Очень длинная суть.") == 40
    assert words[-1] in digest.html


def test_card_without_precedents_shows_no_context_block():
    digest = build_email(
        [digest_item(context_label=ContextLabel.NOT_FOUND_IN_CORPUS)], today=TODAY
    )

    for rendered in (digest.text, digest.html):
        assert "not_found_in_corpus" not in rendered
        assert "Прецеденты" not in rendered
        assert "Похожее уже случалось" not in rendered
    assert "Anthropic отключает claude-3-opus" in digest.text


def test_hidden_fields_never_reach_the_screen():
    signal = digest_item(duplicates_count=8)
    digest = build_email([signal], today=TODAY)

    for rendered in (digest.text, digest.html):
        assert "93" not in rendered
        assert "высокая ставка" not in rendered
        assert "duplicates" not in rendered
        assert "run-1" not in rendered.replace("https://radar.local/runs/run-1", "")


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def test_relative_dates_accompany_every_date():
    phrase = surface._date_phrase
    assert phrase(date(2026, 10, 15), TODAY) == "15 октября, через 59 дней"
    assert phrase(date(2026, 6, 1), TODAY) == "1 июня, 77 дней назад"
    assert phrase(TODAY, TODAY) == "сегодня"
    assert phrase(date(2026, 8, 18), TODAY) == "завтра"
    assert phrase(date(2026, 8, 16), TODAY) == "вчера"
    assert phrase(date(2026, 8, 18), TODAY) == "завтра"
    assert (
        phrase(date(2026, 10, 15), TODAY, DatePrecision.INFERRED)
        == "15 октября, через 59 дней (год не указан в источнике)"
    )


def test_reference_date_is_a_parameter():
    digest = build_email([digest_item()], today=date(2026, 10, 14))
    assert "завтра" in digest.text
    assert "через 59 дней" not in digest.text


def test_missing_date_is_named_and_never_substituted():
    signal = digest_item(
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="",
                source_url="https://docs.claude.com/deprecations",
                evidence="the model will be retired",
            )
        ],
        context_label=ContextLabel.RECURRING,
        precedents=[
            Precedent(
                statement_id="st-1",
                text="Прошлое отключение",
                source_url="https://docs.claude.com/old",
                event_date=None,
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            )
        ],
    )
    digest = build_email([signal], today=TODAY)

    for rendered in (digest.text, digest.html):
        assert "дата отключения в источнике не указана" in rendered
        assert "Дата отключения:" not in rendered
        assert "дата в источнике не указана" in rendered


# --------------------------------------------------------------------------
# Quiet day and failed run (SUR-4)
# --------------------------------------------------------------------------


def test_quiet_day_renders_the_voice_text_with_upcoming_deadlines():
    digest = build_email([quiet_day()], today=TODAY)

    for rendered in (digest.text, digest.html):
        assert "Сегодня в вашем стеке ничего не изменилось." in rendered
        assert "Ближайшее" in rendered
        assert "15 октября, через 59 дней" in rendered
        assert "1 ноября, через 76 дней" in rendered
        assert "Проверено 14 источников, 23 материала отклонено." in rendered
    assert "https://radar.local/runs/run-2" in digest.text


def test_quiet_day_without_deadlines_drops_the_heading():
    digest = build_email([quiet_day(facts=[], stats={})], today=TODAY)

    assert "Ближайшее" not in digest.text
    assert "Ближайшее" not in digest.html
    assert "Сегодня в вашем стеке ничего не изменилось." in digest.text


def test_quiet_day_counts_come_from_the_field_the_core_fills():
    """`stats` is free-form extension and the pipeline never writes it.

    A live quiet_day arrives with an empty dict and a populated `run_summary`.
    Read from `stats` alone, the letter said that nothing happened and offered
    nothing to show it had looked — the shape of an agent gone silent.
    """
    digest = build_email(
        [
            quiet_day(
                facts=[],
                stats={},
                run_summary=RunSummary(
                    sources_checked=5,
                    sources_empty=["worldmonitor"],
                    materials_filtered=0,
                ),
            )
        ],
        today=TODAY,
    )

    for rendered in (flat(digest.text), digest.html):
        assert "Проверено 5 источников." in rendered
    # Nothing was rejected, and "0 материалов отклонено" reads as a broken
    # counter rather than as a calm day.
    assert "0 материалов" not in digest.text


def test_run_failure_reports_itself():
    digest = build_email([run_failure()], today=TODAY)

    for rendered in (flat(digest.text), digest.html):
        assert "Прогон не завершился: сбой на стадии обогащения." in rendered
        assert "Собрано 34 материала, обработать не удалось." in rendered
        assert "Последняя удачная сводка — 16 августа." in rendered
    assert "https://radar.local/runs/run-3" in digest.text


def test_empty_run_still_produces_a_letter():
    digest = build_email([], today=TODAY)

    assert digest.subject == "Сегодня без изменений"
    assert "Сегодня в вашем стеке ничего не изменилось." in digest.text
    assert "Сегодня в вашем стеке ничего не изменилось." in digest.html


def test_closed_story_gets_the_last_line():
    signal = digest_item(
        delta_status=DeltaStatus.RESOLVED,
        delta_note="миграция на Responses API завершена",
        days_tracked=12,
    )
    digest = build_email([signal], today=TODAY)

    assert (
        "Закрыто: миграция на Responses API завершена, история велась 12 дней."
        in digest.text
    )


# --------------------------------------------------------------------------
# The letter as a whole: one wording, one footer, one address
# --------------------------------------------------------------------------


def test_the_context_sentence_is_the_one_the_core_wrote():
    """DR-10: three faces of one record, and one wording of the claim."""
    note = "Anthropic: отключения, третий раз с 12 мая."
    signal = digest_item(
        context_label=ContextLabel.RECURRING,
        context_note=note,
        precedents=[
            Precedent(
                statement_id="st-1",
                text="Anthropic объявила отключение claude-2.1",
                source_url="https://docs.claude.com/deprecations#claude-2",
                event_date=date(2026, 5, 12),
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            )
        ],
    )
    digest = build_email([signal], today=TODAY)

    for rendered in (flat(digest.text), digest.html):
        assert note in rendered
        # The letter's own heading was a third wording of the same claim.
        assert "Похожее уже случалось" not in rendered
        assert "Anthropic объявила отключение claude-2.1" in rendered


def test_a_story_closed_in_the_background_still_closes_the_letter():
    """A closed story is never urgent, so it never arrives as a card."""
    closed = digest_item(
        "sig-closed",
        Tier.BACKGROUND,
        "Миграция на Responses API",
        delta_status=DeltaStatus.RESOLVED,
        delta_note="миграция на Responses API завершена",
        days_tracked=12,
    )
    digest = build_email([digest_item(), closed], today=TODAY)

    assert [s.signal_id for s in select_cards([digest_item(), closed])] == ["sig-001"]
    for rendered in (flat(digest.text), digest.html):
        assert (
            "Закрыто: миграция на Responses API завершена, история велась 12 дней."
            in rendered
        )


def test_the_footer_names_the_sources_that_did_not_answer():
    signal = digest_item(
        run_summary=RunSummary(
            sources_checked=14,
            sources_failed=["Cursor changelog", "MCP servers"],
            sources_empty=["Pinecone release notes"],
            materials_collected=41,
            materials_filtered=23,
        )
    )
    digest = build_email([signal], today=TODAY)

    for rendered in (flat(digest.text), digest.html):
        assert "Два источника не ответили: Cursor changelog, MCP servers." in rendered
        assert "Pinecone release notes ответил, но ничего не отдал." in rendered


def test_a_run_where_every_source_answered_says_nothing_about_sources():
    digest = build_email(
        [digest_item(run_summary=RunSummary(sources_checked=14))], today=TODAY
    )

    assert "не ответил" not in digest.text
    assert "ничего не отдал" not in digest.text


def test_the_run_log_address_is_printed_once():
    """Five cards ending in the same link is one sentence printed five times."""
    signals = [
        digest_item(f"sig-{index}", Tier.STANDARD, f"Заголовок {index}")
        for index in range(5)
    ]
    digest = build_email(signals, today=TODAY)

    assert digest.text.count("https://radar.local/runs/run-1") == 1
    assert digest.text.count(surface.RUN_LOG_LABEL) == 1
    assert digest.html.count("https://radar.local/runs/run-1") == 1


def test_upcoming_shows_the_line_the_core_wrote_for_the_reader():
    """The block is for a Russian reader, not for the vendor's own sentence."""
    signal = quiet_day(
        upcoming=[
            UpcomingDeadline(
                when=date(2026, 10, 15),
                what="отключается claude-3-opus",
                vendor="anthropic",
                source_url="https://docs.claude.com/deprecations",
            ),
            UpcomingDeadline(
                when=date(2026, 11, 1),
                what="новые лимиты Tier 1 в OpenAI API",
                vendor="openai",
                source_url="https://platform.openai.com/docs/guides/rate-limits",
            ),
        ]
    )
    digest = build_email([signal], today=TODAY)

    assert "15 октября, через 59 дней — отключается claude-3-opus" in flat(digest.text)
    for rendered in (flat(digest.text), digest.html):
        assert "отключается claude-3-opus" in rendered
        assert "новые лимиты Tier 1 в OpenAI API" in rendered
        # The English sentence from the source belongs to the card, not here.
        assert "will be retired on October 15" not in rendered
        # And a line written for the reader is not dressed as a quotation.
        assert "«отключается claude-3-opus»" not in rendered


def test_upcoming_falls_back_to_the_facts_and_quotes_them_as_quotes():
    digest = build_email([quiet_day()], today=TODAY)

    assert "«claude-3-opus will be retired on October 15, 2026»" in flat(digest.text)
    assert "15 октября, через 59 дней" in digest.text


# --------------------------------------------------------------------------
# Capacity by tier, not by threshold
# --------------------------------------------------------------------------


def test_letter_takes_lead_and_standard_and_leaves_background():
    signals = [
        digest_item("sig-1", Tier.LEAD, "Первый"),
        digest_item("sig-2", Tier.STANDARD, "Второй"),
        digest_item("sig-3", Tier.BACKGROUND, "Третий"),
    ]

    assert [s.signal_id for s in select_cards(signals)] == ["sig-1", "sig-2"]
    digest = build_email(signals, today=TODAY)
    assert "Первый" in digest.text
    assert "Второй" in digest.text
    assert "Третий" not in digest.text
    assert "Третий" not in digest.html


def test_order_from_the_store_is_preserved():
    signals = [
        digest_item("sig-2", Tier.STANDARD, "Второй"),
        digest_item("sig-1", Tier.LEAD, "Первый"),
    ]
    text = build_email(signals, today=TODAY).text

    assert text.index("Второй") < text.index("Первый")
    assert build_email(signals, today=TODAY).subject == "Второй"


def test_quiet_day_signal_is_not_drawn_as_a_card():
    digest = build_email([quiet_day(tier=Tier.LEAD)], today=TODAY)

    assert "Сегодня в вашем стеке ничего не изменилось." in digest.text
    assert select_cards([quiet_day(tier=Tier.LEAD)]) == []


# --------------------------------------------------------------------------
# Mail layout constraints
# --------------------------------------------------------------------------


def test_html_holds_no_external_resources():
    digest = build_email(
        [digest_item(context_label=ContextLabel.RECURRING), quiet_day()], today=TODAY
    )
    html = digest.html

    for forbidden in (
        "<img",
        "<script",
        "<style",
        "<link",
        "src=",
        "url(",
        "@import",
        "background=",
        "@font-face",
    ):
        assert forbidden not in html


def test_html_uses_tables_and_inline_styles_at_600_pixels():
    html = build_email([digest_item()], today=TODAY).html

    assert "width:600px" in html
    assert 'width="600"' in html
    assert "<table" in html
    assert "display:flex" not in html
    assert "display:grid" not in html
    assert 'style="' in html
    # Dark mode is rewritten by clients, so colours are stated everywhere.
    assert html.count("background-color:") > 10
    assert 'name="color-scheme"' in html
    assert html.count("color:") > html.count("background-color:")


# --------------------------------------------------------------------------
# Headers and encoding
# --------------------------------------------------------------------------


def test_headers_encode_cyrillic_subject_correctly():
    digest = build_email([digest_item()], today=TODAY)
    message = build_message(
        digest, "radar@example.test", ["reader@example.test", "second@example.test"]
    )
    raw = message.as_bytes()
    headers = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]

    assert "Anthropic отключает claude-3-opus".encode() not in headers
    assert b"=?utf-8?" in headers.lower()

    parsed = message_from_bytes(raw, policy=email_policy.default)
    assert parsed["Subject"] == "Anthropic отключает claude-3-opus"
    assert parsed["To"] == "reader@example.test, second@example.test"
    assert parsed["From"] == "radar@example.test"
    assert parsed["Message-ID"].endswith("@example.test>")
    assert parsed["Date"]
    plain = parsed.get_body(preferencelist=("plain",)).get_content()
    html_part = parsed.get_body(preferencelist=("html",)).get_content()
    assert "Anthropic отключает claude-3-opus" in plain
    assert "Anthropic отключает claude-3-opus" in html_part


def test_quiet_day_subject_survives_a_round_trip():
    digest = build_email([quiet_day()], today=TODAY)
    message = build_message(digest, "radar@example.test", ["reader@example.test"])
    parsed = message_from_bytes(message.as_bytes(), policy=email_policy.default)

    assert parsed["Subject"] == "Сегодня без изменений"


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def test_send_uses_starttls_and_env_settings():
    digest = build_email([digest_item()], today=TODAY)
    result = send_digest(digest, env=ENV, smtp_factory=FakeSMTP)

    assert result == DeliveryResult(
        True,
        "Anthropic отключает claude-3-opus",
        ("reader@example.test", "second@example.test"),
        None,
    )
    sent = FakeSMTP.instances[0]
    assert (sent.host, sent.port) == ("smtp.example.test", 2525)
    assert sent.tls is True
    assert sent.login_args == ("radar@example.test", "secret")
    assert sent.closed is True
    assert sent.sent[0]["Subject"] == "Anthropic отключает claude-3-opus"


def test_smtp_failure_comes_back_as_a_result():
    digest = build_email([digest_item()], today=TODAY)

    class RefusingSMTP(FakeSMTP):
        def send_message(self, message):
            raise smtplib.SMTPRecipientsRefused({"reader@example.test": (550, b"no")})

    result = send_digest(digest, env=ENV, smtp_factory=RefusingSMTP)

    assert result.delivered is False
    assert "SMTPRecipientsRefused" in result.error
    assert result.recipients == ("reader@example.test", "second@example.test")


def test_unreachable_host_does_not_raise():
    digest = build_email([digest_item()], today=TODAY)

    def broken(host, port, timeout=None):
        raise OSError("connection refused")

    result = send_digest(digest, env=ENV, smtp_factory=broken)

    assert result.delivered is False
    assert "connection refused" in result.error


def test_missing_environment_returns_a_result_and_names_the_variables():
    digest = build_email([digest_item()], today=TODAY)
    result = send_digest(digest, env={}, smtp_factory=FakeSMTP)

    assert result.delivered is False
    assert "SMTP_HOST" in result.error
    assert "SMTP_FROM" in result.error
    assert "SMTP_TO" in result.error
    assert FakeSMTP.instances == []

    with pytest.raises(EmailConfigError):
        load_smtp_config({})


def test_login_is_skipped_when_no_credentials_are_set():
    env = dict(ENV, SMTP_USER="", SMTP_PASSWORD="")
    result = send_digest(
        build_email([quiet_day()], today=TODAY), env=env, smtp_factory=FakeSMTP
    )

    assert result.delivered is True
    assert FakeSMTP.instances[0].login_args is None
    assert FakeSMTP.instances[0].tls is True


def test_config_reads_a_recipient_list():
    config = load_smtp_config(dict(ENV, SMTP_TO="a@test; b@test,c@test"))

    assert config.recipients == ("a@test", "b@test", "c@test")
    assert config.port == 2525


def test_default_port_is_the_starttls_one():
    env = dict(ENV)
    env.pop("SMTP_PORT")
    assert load_smtp_config(env).port == 587


def test_deliver_reads_the_store_read_only(tmp_path):
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    publish_signals(conn, "run-1", [digest_item(), quiet_day(run_id="run-1")])
    conn.close()

    result = deliver(db_path, today=TODAY, env=ENV, smtp_factory=FakeSMTP)

    assert result.delivered is True
    assert result.subject == "Anthropic отключает claude-3-opus"
    body = FakeSMTP.instances[0].sent[0]
    plain = body.get_body(preferencelist=("plain",)).get_content()
    assert "Anthropic отключает claude-3-opus" in plain


# --------------------------------------------------------------------------
# Static guarantees (SUR-2)
# --------------------------------------------------------------------------


def _module_tree() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def test_only_models_db_and_stdlib_are_imported():
    imported: set[str] = set()
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports hide the dependency"
            assert node.module is not None
            imported.add(node.module)

    for name in imported:
        root = name.split(".")[0]
        if root == "radar":
            assert name in {"radar.models", "radar.db"}, name
        else:
            assert root in sys.stdlib_module_names, name
    assert "radar.models" in imported
    assert "radar.db" in imported


def test_no_pipeline_stage_is_reachable_from_the_surface():
    source = MODULE_PATH.read_text(encoding="utf-8")
    for stage in (
        "radar.collect",
        "radar.enrich",
        "radar.scoring",
        "radar.filter",
        "radar.llm",
        "radar.trends",
        "radar.retrieval",
        "radar.delta",
        "radar.cluster",
        "radar.runlog",
        "importlib",
    ):
        assert f"import {stage}" not in source
        assert f"from {stage}" not in source


def test_surface_never_reads_the_ranking_fields():
    forbidden = {"score", "score_rationale", "duplicates_count", "rank"}
    seen = {
        node.attr
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    }
    assert seen == set()


def test_surface_never_reorders_signals():
    calls = set()
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                calls.add(func.attr)
    assert "sorted" not in calls
    assert "sort" not in calls


class TestAClosingLineSaysWhatClosed:
    """«Закрыто: история закрыта, история велась 4 дня».

    `delta_note` for a resolved storyline is literally the words "история
    закрыта", so reading it before the headline produced a sentence that never
    names its subject. Twenty-seven of them ended the digest every morning.
    """

    def test_the_subject_is_named_when_there_is_no_note(self):
        signal = digest_item(
            headline="AWS Bedrock отключает Cohere Command R.",
            delta_status=DeltaStatus.RESOLVED,
            delta_note=None,
            days_tracked=4,
        )

        line = surface._closing_line([signal])

        assert "Cohere Command R" in line
        assert ".," not in line

    def test_a_note_that_says_something_still_wins(self):
        signal = digest_item(
            headline="OpenAI отключает Assistants API.",
            delta_status=DeltaStatus.RESOLVED,
            delta_note="миграция на Responses API завершена",
            days_tracked=12,
        )

        line = surface._closing_line([signal])

        assert "миграция на Responses API завершена" in line

    def test_it_falls_back_to_the_note_when_there_is_no_headline(self):
        signal = digest_item(
            headline="",
            delta_status=DeltaStatus.RESOLVED,
            delta_note="история закрыта",
            days_tracked=2,
        )

        assert surface._closing_line([signal])
