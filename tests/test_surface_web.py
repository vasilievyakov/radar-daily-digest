"""Tests for the static web surface.

Two kinds of checks live here. The first kind reads the generated HTML and
asks whether a person on the far side of a projector can use it. The second
kind reads the module's own syntax tree and asks whether the surface stayed a
surface: SUR-2 is an architectural claim, and a claim nobody tests decays into
a comment.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import sys
from datetime import UTC, date, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

from radar.db import init_db, publish_signals
from radar.models import (
    ChangeType,
    ContextLabel,
    DatePrecision,
    DeltaStatus,
    Fact,
    FactKind,
    Precedent,
    RetrievalReport,
    RunSummary,
    Signal,
    SignalType,
    Tier,
    UpcomingDeadline,
)
from radar.surfaces import web

TODAY = date(2026, 8, 17)
NOW = datetime(2026, 8, 17, 6, 30, tzinfo=UTC)

WEB_SOURCE = Path(web.__file__).read_text(encoding="utf-8")

# A title carrying markup, an ampersand and a quote: everything a changelog
# can legally contain and a page must never execute.
NASTY_TITLE = '<script>alert("x")</script> Tier 1 & Tier 2 <b>"limits"</b>'


# -- fixtures ----------------------------------------------------------


def make_lead_signal(**overrides) -> Signal:
    data = dict(
        signal_id="sig-lead",
        run_id="run-1",
        signal_type=SignalType.DIGEST_ITEM,
        created_at=NOW,
        for_date=TODAY,
        headline="Anthropic отключает claude-3-opus",
        summary="Запросы к модели начнут возвращать ошибку; на замену предложен claude-opus-4.",
        why_it_matters="Модель используется в двух ваших проектах.",
        change_type=ChangeType.DEPRECATION,
        vendor="anthropic",
        product="claude-3-opus",
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                value_date=date(2026, 10, 15),
                subject="claude-3-opus",
                source_url="https://docs.claude.com/en/docs/about-claude/model-deprecations",
                evidence="claude-3-opus will be retired on October 15, 2026",
                confidence="high",
                evidence_verified=True,
            ),
            Fact(
                kind=FactKind.AFFECTED_PRODUCT,
                value="claude-3-opus",
                source_url="https://docs.claude.com/en/docs/about-claude/model-deprecations",
                evidence="claude-3-opus",
                evidence_verified=True,
            ),
        ],
        primary_url="https://docs.claude.com/en/docs/about-claude/model-deprecations",
        duplicates_count=8,
        delta_status=DeltaStatus.NEW,
        delta_note="повторяется третий раз",
        context_note=(
            "Третий раз с мая Anthropic объявляет отключение "
            "с двухмесячным предупреждением."
        ),
        days_tracked=1,
        context_label=ContextLabel.RECURRING,
        precedents=[
            Precedent(
                statement_id="stmt-001",
                text="Anthropic объявил отключение claude-2.1 с уведомлением за два месяца.",
                source_url="https://docs.claude.com/deprecations#claude-2-1",
                event_date=date(2026, 5, 15),
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            ),
            Precedent(
                statement_id="stmt-002",
                text="Anthropic объявил отключение claude-instant-1.2.",
                source_url="https://docs.claude.com/deprecations#claude-instant",
                event_date=date(2026, 6, 30),
                date_precision=DatePrecision.INFERRED,
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            ),
            Precedent(
                statement_id="stmt-003",
                text="Anthropic объявил отключение claude-3-sonnet.",
                source_url="https://docs.claude.com/deprecations#claude-3-sonnet",
                event_date=date(2026, 7, 21),
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            ),
        ],
        retrieval=RetrievalReport(
            strict_hits=3, relaxed_hits=5, total_found=5, shown=3
        ),
        run_summary=RunSummary(
            sources_checked=14,
            sources_failed=["mcp-servers"],
            sources_empty=["cursor-changelog"],
            materials_collected=41,
            materials_filtered=23,
            cost_usd=0.27,
        ),
        score=87,
        score_rationale="вес вендора и близость даты отключения",
        rank=1,
        tier=Tier.LEAD,
        run_log_url="https://example.test/runs/run-1",
    )
    data.update(overrides)
    return Signal(**data)


def make_second_signal(**overrides) -> Signal:
    data = dict(
        signal_id="sig-second",
        run_id="run-1",
        signal_type=SignalType.DIGEST_ITEM,
        created_at=NOW,
        for_date=TODAY,
        headline="OpenAI поднимает лимиты Tier 1",
        summary="Потолок запросов в минуту вырастет вдвое.",
        change_type=ChangeType.LIMITS,
        vendor="openai",
        facts=[
            Fact(
                kind=FactKind.EFFECTIVE_DATE,
                value="2026-11-01",
                value_date=date(2026, 11, 1),
                subject="Tier 1",
                source_url="https://platform.openai.com/docs/guides/rate-limits",
                evidence="new limits take effect on November 1, 2026",
                evidence_verified=True,
            )
        ],
        primary_url="https://platform.openai.com/docs/guides/rate-limits",
        context_label=ContextLabel.NOT_FOUND_IN_CORPUS,
        rank=2,
        tier=Tier.STANDARD,
    )
    data.update(overrides)
    return Signal(**data)


def make_quiet_signal(**overrides) -> Signal:
    data = dict(
        signal_id="sig-quiet",
        run_id="run-2",
        signal_type=SignalType.QUIET_DAY,
        created_at=NOW,
        for_date=TODAY,
        headline="Сегодня в вашем стеке ничего не изменилось",
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                value_date=date(2026, 10, 15),
                subject="claude-3-opus",
                source_url="https://docs.claude.com/deprecations",
                evidence="claude-3-opus will be retired on October 15, 2026",
                evidence_verified=True,
            )
        ],
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
        ],
        run_summary=RunSummary(
            sources_checked=14,
            materials_collected=31,
            materials_filtered=23,
        ),
        rank=0,
    )
    data.update(overrides)
    return Signal(**data)


def make_failure_signal(**overrides) -> Signal:
    data = dict(
        signal_id="sig-failure",
        run_id="run-3",
        signal_type=SignalType.RUN_FAILURE,
        created_at=NOW,
        for_date=TODAY,
        headline="Прогон 17 августа не завершился",
        failure_reason="сбой на стадии обогащения",
        failure_stage="enrich",
        run_summary=RunSummary(
            sources_checked=14,
            materials_collected=34,
            materials_filtered=0,
            last_success_date=date(2026, 8, 16),
        ),
        rank=0,
    )
    data.update(overrides)
    return Signal(**data)


def make_run_view(**overrides) -> web.RunLogView:
    data = dict(
        run_id="run-1",
        for_date=TODAY,
        status="ok",
        started_at="2026-08-17T06:00:00+00:00",
        finished_at="2026-08-17T06:04:12+00:00",
        stages=[
            web.StageRow(
                name="collect",
                in_count=14,
                out_count=61,
                started_at="2026-08-17T06:00:01+00:00",
                duration_ms=8400,
            ),
            web.StageRow(
                name="filter",
                in_count=61,
                out_count=19,
                started_at="2026-08-17T06:00:12+00:00",
                duration_ms=41000,
                errors=["TimeoutError: батч 3 переспрошен"],
            ),
        ],
        sources=[
            web.SourceRow("anthropic-docs", "ok", items_count=12, latency_ms=430),
            web.SourceRow("cursor-changelog", "empty", items_count=0, latency_ms=310),
            web.SourceRow(
                "mcp-servers",
                "failed",
                items_count=0,
                latency_ms=None,
                error="HTTP 503",
            ),
        ],
        filtered=[
            web.FilteredRow(
                title=NASTY_TITLE,
                url="https://example.test/marketing-post",
                reason_code="маркетинг_без_фактов",
                stage="filter",
                reason_note="анонс вебинара",
            ),
            web.FilteredRow(
                title="Вчерашний релиз n8n",
                url="https://example.test/n8n",
                reason_code="дубль_вчерашнего",
                stage="filter",
            ),
            web.FilteredRow(
                title="Supabase меняет заголовки ответов",
                url="https://example.test/supabase#stmt-1",
                reason_code="vendor_unresolved",
                stage="enrich",
            ),
            web.FilteredRow(
                title="LangChain 0.3.9",
                url="https://example.test/langchain",
                reason_code="ниже_порога_публикации",
                stage="score",
                reason_note="оценка 31, порог 45",
            ),
        ],
        costs=[
            web.StageCost(
                "enrich", calls=6, tokens_in=41000, tokens_out=3100, usd=0.21
            ),
            web.StageCost("filter", calls=4, tokens_in=52000, tokens_out=900, usd=0.06),
        ],
        model_calls=10,
        tokens_in=93000,
        tokens_out=4000,
        usd=0.27,
        notes=["Источник mcp-servers отключён на два прогона"],
        delivery=[{"channel": "telegram", "status": "доставлено", "message_id": "42"}],
        history=[
            web.RunHistoryRow("run-1", TODAY, "ok", 10, 93000, 4000, 0.27),
            web.RunHistoryRow("run-0", date(2026, 8, 16), "ok", 9, 88000, 3800, 0.25),
        ],
    )
    data.update(overrides)
    return web.RunLogView(**data)


def make_corpus_view(**overrides) -> web.CorpusView:
    data = dict(
        statements=412,
        earliest=date(2026, 2, 1),
        latest=date(2026, 8, 16),
        cells=[
            web.CorpusCell("anthropic", "deprecation", 24),
            web.CorpusCell("anthropic", "release", 51),
            web.CorpusCell("openai", "limits", 9),
            web.CorpusCell("openai", "pricing", 2),
            web.CorpusCell("n8n", "release", 88),
        ],
        clusters=37,
        trends=6,
        signals=41,
        runs=4,
        dense_threshold=3,
        dense_vendors=["anthropic", "n8n", "openai"],
        ready=True,
    )
    data.update(overrides)
    return web.CorpusView(**data)


# -- html helpers ------------------------------------------------------

EXTERNAL_TAG_RE = re.compile(
    r"<(?:script|link|img|iframe|source|video|audio|embed|object|base)\b[^>]*>",
    re.IGNORECASE,
)
EMOJI_RE = re.compile("[\U0001f000-\U0001faff←-⇿⌀-⏿☀-➿️⬀-⯿]")


class DetailsChecker(HTMLParser):
    """`<details>` only works without JavaScript if it is written correctly."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.details_depth = 0
        self.details_seen = 0
        self.summaries = 0
        self.awaiting_summary: list[bool] = []
        self.problems: list[str] = []
        self.scripts = 0
        self.raw_script_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts += 1
        if tag == "details":
            self.details_depth += 1
            self.details_seen += 1
            self.awaiting_summary.append(True)
            return
        if tag == "summary":
            self.summaries += 1
            if not self.awaiting_summary:
                self.problems.append("summary outside details")
            elif not self.awaiting_summary[-1]:
                self.problems.append("second summary inside one details")
            else:
                self.awaiting_summary[-1] = False
            return
        if self.awaiting_summary and self.awaiting_summary[-1]:
            self.problems.append(f"<{tag}> before <summary> inside details")

    def handle_endtag(self, tag):
        if tag == "details":
            self.details_depth -= 1
            if self.awaiting_summary:
                if self.awaiting_summary.pop():
                    self.problems.append("details without summary")
            if self.details_depth < 0:
                self.problems.append("unbalanced </details>")


def check_details(html: str) -> DetailsChecker:
    parser = DetailsChecker()
    parser.feed(html)
    assert parser.details_depth == 0, "unclosed <details>"
    assert not parser.problems, parser.problems
    return parser


def page_text(html: str) -> str:
    """The words a reader sees, with the markup and the stylesheet removed."""
    body = re.sub(r"<head>.*?</head>", " ", html, flags=re.S)
    body = re.sub(r"<style.*?</style>", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return " ".join(unescape(body).split())


def assert_page_contract(html: str) -> None:
    """Every page, whatever it shows, obeys the same house rules."""
    assert html.startswith("<!DOCTYPE html>")
    assert '<html lang="ru">' in html
    assert '<meta charset="utf-8">' in html
    assert "prefers-color-scheme" in html
    assert "<script" not in html.lower()
    assert "@import" not in html
    assert "url(http" not in html
    assert not EMOJI_RE.search(html), "эмодзи на странице"
    for tag in EXTERNAL_TAG_RE.findall(html):
        assert "http://" not in tag and "https://" not in tag, tag
    check_details(html)


# -- digest page -------------------------------------------------------


def test_digest_lead_card_is_expanded_with_evidence():
    html = web.render_digest([make_lead_signal(), make_second_signal()], today=TODAY)
    assert_page_contract(html)
    assert "Anthropic отключает claude-3-opus" in html
    assert "15 октября, через 59 дней" in html
    assert "claude-3-opus will be retired on October 15, 2026" in html
    assert (
        'href="https://docs.claude.com/en/docs/about-claude/model-deprecations"' in html
    )
    assert "Почему это важно:" in html
    assert "sig-lead" in html
    lead_position = html.index("Anthropic отключает claude-3-opus")
    second_position = html.index("OpenAI поднимает лимиты Tier 1")
    assert lead_position < second_position


def test_secondary_items_are_one_line_with_a_disclosure():
    html = web.render_digest([make_lead_signal(), make_second_signal()], today=TODAY)
    checker = check_details(html)
    # one for precedents on the lead card, one for the second card body
    assert checker.details_seen >= 2
    assert (
        'OpenAI поднимает лимиты Tier 1</span> <span class="when">— 1 ноября'
        in html.replace("&quot;", '"')
        or "1 ноября, через 76 дней" in html
    )


def test_precedents_open_with_urls_and_dates():
    html = web.render_digest([make_lead_signal()], today=TODAY)
    assert "Показать три записи" in html
    assert "https://docs.claude.com/deprecations#claude-2-1" in html
    assert "15 мая, 94 дня назад" in html
    assert "21 июля, 27 дней назад" in html
    # DatePrecision.INFERRED must stay marked (voice 4)
    assert "год не указан в источнике" in html
    assert "stmt-001" in html


def test_hidden_fields_never_reach_the_page():
    """voice 8: no score, no rationale, no duplicate count, no empty label."""
    html = web.render_digest([make_lead_signal(), make_second_signal()], today=TODAY)
    assert "87" not in html
    assert "вес вендора" not in html
    assert "not_found_in_corpus" not in html
    assert "duplicates" not in html.lower()


def test_signal_without_precedents_shows_no_context_block():
    signal = make_lead_signal(
        precedents=[], context_label=ContextLabel.NOT_FOUND_IN_CORPUS, delta_note=None
    )
    html = web.render_digest([signal], today=TODAY)
    assert_page_contract(html)
    assert "Показать" not in html.replace("Показать статистику прогона", "")
    assert "precedents" not in html.replace(".precedents", "")


def test_signal_without_dates_admits_it_and_invents_nothing():
    signal = make_lead_signal(facts=[], precedents=[], context_label=None)
    html = web.render_digest([signal], today=TODAY)
    assert_page_contract(html)
    assert "Дата отключения в источнике не указана." in html
    assert "через" not in html.split('class="footer"')[0].replace(
        "через два месяца", ""
    )


def test_card_never_denies_a_date_its_own_quote_states():
    """The most expensive defect: two lines of one card contradicting.

    Records written before `value_date` existed carry the day in `value`
    alone. Reading only the parsed field printed «дата отключения в источнике
    не указана» directly above «Дата отключения: 2026-10-15».
    """
    signal = make_lead_signal(
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                value_date=None,
                subject="claude-3-opus",
                source_url="https://docs.claude.com/deprecations",
                evidence="claude-3-opus will be retired on October 15, 2026",
                evidence_verified=True,
            )
        ]
    )
    html = web.render_digest([signal], today=TODAY)
    text = page_text(html)

    assert "в источнике не указана" not in text.lower()
    assert "15 октября, через 59 дней" in text
    # And the fact block prints the day, not the machine form of it.
    assert "Дата отключения: 15 октября, через 59 дней — claude-3-opus" in text
    assert "2026-10-15" not in text


def test_a_date_only_in_the_value_still_carries_its_precision():
    signal = make_lead_signal(
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10",
                value_date=None,
                source_url="https://docs.claude.com/deprecations",
                evidence="retired in October 2026",
                evidence_verified=True,
            )
        ]
    )
    text = page_text(web.render_digest([signal], today=TODAY))

    assert "октябрь 2026" in text
    assert "через" not in text


def test_a_marked_precision_survives_the_fallback():
    signal = make_lead_signal(
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                value_date=None,
                date_precision=DatePrecision.INFERRED,
                source_url="https://docs.claude.com/deprecations",
                evidence="retired on October 15",
                evidence_verified=True,
            )
        ]
    )
    text = page_text(web.render_digest([signal], today=TODAY))

    assert "15 октября (год не указан в источнике)" in text
    assert "через 59 дней" not in text


def test_signal_without_dates_and_not_a_deprecation_stays_silent():
    signal = make_second_signal(facts=[], change_type=ChangeType.RELEASE, rank=1)
    html = web.render_digest([signal], today=TODAY)
    assert web.MISSING_SUNSET_DATE not in html


def test_escaping_of_every_field():
    signal = make_lead_signal(
        headline=NASTY_TITLE,
        summary=NASTY_TITLE,
        why_it_matters=NASTY_TITLE,
        delta_note=NASTY_TITLE,
        facts=[
            Fact(
                kind=FactKind.VERSION,
                value=NASTY_TITLE,
                source_url="https://example.test/a?x=1&y=2",
                evidence=NASTY_TITLE,
                evidence_verified=True,
            )
        ],
        precedents=[
            Precedent(
                statement_id=NASTY_TITLE,
                text=NASTY_TITLE,
                source_url="https://example.test/b?x=1&y=2",
                event_date=date(2026, 6, 1),
                vendor=NASTY_TITLE,
                change_type=ChangeType.DEPRECATION,
            ),
            Precedent(
                statement_id="stmt-x",
                text="вторая запись",
                source_url="https://example.test/c",
                event_date=date(2026, 6, 2),
                vendor=NASTY_TITLE,
                change_type=ChangeType.DEPRECATION,
            ),
        ],
    )
    html = web.render_digest([signal], today=TODAY)
    assert_page_contract(html)
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html
    assert "Tier 1 &amp; Tier 2" in html
    assert "&amp;y=2" in html
    assert "<b>" not in html


def test_javascript_url_is_not_a_link():
    signal = make_lead_signal(
        primary_url="javascript:alert(1)",
        facts=[
            Fact(
                kind=FactKind.VERSION,
                value="1.0",
                source_url="javascript:alert(2)",
                evidence="v1.0",
                evidence_verified=True,
            )
        ],
    )
    html = web.render_digest([signal], today=TODAY)
    assert 'href="javascript' not in html
    assert "<a href" not in html.split("Первоисточник:")[1].split("</p>")[0]
    assert_page_contract(html)


def test_unverified_fact_is_marked():
    signal = make_lead_signal(
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                source_url="https://docs.claude.com/deprecations",
                evidence="will be retired on October 15, 2026",
                evidence_verified=False,
            )
        ]
    )
    html = web.render_digest([signal], today=TODAY)
    assert "цитата не сверена" in html


def test_resolved_signal_closes_the_page():
    """The closing line names the story, and the story lives in the headline.

    `delta_note` on a resolved signal is the core's status word — "история
    закрыта" — so a footer that read the note first spent its last screen
    saying that something had closed without ever saying what.
    """
    resolved = make_second_signal(
        signal_id="sig-resolved",
        delta_status=DeltaStatus.RESOLVED,
        delta_note="история закрыта",
        days_tracked=12,
        rank=3,
    )
    html = web.render_digest([make_lead_signal(), resolved], today=TODAY)
    assert "Закрыто: OpenAI поднимает лимиты Tier 1, история велась 12 дней." in html
    assert "Закрыто: история закрыта" not in html


def test_every_closing_line_names_its_own_story():
    """Twenty-seven closures must read as twenty-seven different sentences."""
    resolved = [
        make_second_signal(
            signal_id=f"sig-resolved-{n}",
            headline=f"Вендор {n} отключил модель m-{n}",
            delta_status=DeltaStatus.RESOLVED,
            delta_note="история закрыта",
            days_tracked=4,
            rank=n + 3,
        )
        for n in range(5)
    ]
    html = web.render_digest([make_lead_signal(), *resolved], today=TODAY)
    closings = [line for line in html.splitlines() if "Закрыто:" in line]
    assert len(closings) == 5
    assert len(set(closings)) == 5
    for n in range(5):
        assert (
            f"Закрыто: Вендор {n} отключил модель m-{n}, история велась 4 дня." in html
        )


def test_a_closing_line_falls_back_to_the_note_without_a_headline():
    resolved = make_second_signal(
        signal_id="sig-resolved",
        headline="",
        delta_status=DeltaStatus.RESOLVED,
        delta_note="история закрыта",
        days_tracked=2,
        rank=3,
    )
    html = web.render_digest([make_lead_signal(), resolved], today=TODAY)
    assert "Закрыто: история закрыта, история велась 2 дня." in html


def test_a_headline_that_ends_in_a_period_does_not_double_it():
    resolved = make_second_signal(
        signal_id="sig-resolved",
        headline="OpenAI поднимает лимиты Tier 1.",
        delta_status=DeltaStatus.RESOLVED,
        delta_note="история закрыта",
        days_tracked=1,
        rank=3,
    )
    html = web.render_digest([make_lead_signal(), resolved], today=TODAY)
    assert "Закрыто: OpenAI поднимает лимиты Tier 1." in html
    assert ".." not in html


def test_the_web_and_telegram_close_a_story_with_the_same_sentence():
    """DR-10: one record, three faces. Two of them must not word it apart."""
    from radar.surfaces import telegram

    resolved = make_second_signal(
        signal_id="sig-resolved",
        delta_status=DeltaStatus.RESOLVED,
        delta_note="история закрыта",
        days_tracked=12,
        rank=3,
    )
    signals = [make_lead_signal(), resolved]
    html = web.render_digest(signals, today=TODAY)
    post = telegram.render_digest(signals, today=TODAY)
    line = "Закрыто: OpenAI поднимает лимиты Tier 1, история велась 12 дней."
    assert line in html
    assert line in (post if isinstance(post, str) else "\n".join(post))


# -- quiet day and failure ---------------------------------------------


def test_quiet_day_page():
    html = web.render_digest([make_quiet_signal()], today=TODAY)
    assert_page_contract(html)
    assert "Сегодня в вашем стеке ничего не изменилось" in html
    assert "Ближайшее" in html
    assert "15 октября, через 59 дней" in html
    assert "1 ноября, через 76 дней" in html
    assert "Проверено 14 источников, 23 материала отклонено." in html
    assert "sig-quiet" in html


def test_a_day_that_rejected_nothing_does_not_report_a_zero():
    """On a real quiet day `materials_filtered` is 0, and «0 материалов
    отклонено» reads as a counter that broke rather than as a calm day."""
    signal = make_quiet_signal(
        run_summary=RunSummary(sources_checked=5, materials_filtered=0)
    )
    html = web.render_digest([signal], today=TODAY)
    assert "Проверено 5 источников." in html
    assert "0 материалов" not in html


def test_quiet_day_without_deadlines_drops_the_whole_block():
    signal = make_quiet_signal(facts=[], precedents=[], upcoming=[])
    html = web.render_digest([signal], today=TODAY)
    assert_page_contract(html)
    assert "Ближайшее" not in html


def test_quiet_day_ignores_past_deadlines():
    signal = make_quiet_signal(
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-06-01",
                value_date=date(2026, 6, 1),
                source_url="https://example.test/old",
                evidence="retired on June 1, 2026",
                evidence_verified=True,
            )
        ],
        precedents=[],
        upcoming=[
            UpcomingDeadline(when=date(2026, 6, 1), what="прошедший срок"),
        ],
    )
    html = web.render_digest([signal], today=TODAY)
    assert "Ближайшее" not in html


def test_run_failure_page():
    html = web.render_digest([make_failure_signal()], today=TODAY)
    assert_page_contract(html)
    assert "Прогон 17 августа не завершился" in html
    assert "Сбой на стадии обогащения." in html
    assert "Собрано 34 материала, обработать не удалось." in html
    assert 'href="run-log.html"' in html


def test_empty_run_says_so_without_pretending():
    html = web.render_digest([], today=TODAY)
    assert_page_contract(html)
    assert "Записей за этот день нет" in html
    assert (
        "За этот день не записано ни изменений в вашем стеке, "
        "ни отметки о тихом дне." in html
    )
    assert "ничего не изменилось" not in html


def test_empty_run_speaks_about_the_day_and_says_it_once():
    """The fourth state got the voice of the other three (voice 2)."""
    html = web.render_digest([], today=TODAY)
    text = page_text(html)

    # Nothing from the plumbing: the reader has no storage and no run.
    assert "хранилище" not in text
    assert "Дата прогона" not in text
    # The day is stated once, in the masthead.
    assert text.count("17 августа, сегодня") == 1
    # And the state itself is stated once, not three times.
    assert text.count("Записей за этот день нет") == 1


def test_footer_reports_unanswered_sources_calmly():
    html = web.render_digest([make_lead_signal()], today=TODAY)
    assert "Один источник не ответил: Mcp servers." in html
    assert "Cursor changelog ответил, но ничего не отдал." in html
    assert "Лог прогона" in html


# -- run log page ------------------------------------------------------


def test_run_log_page_is_readable_prose_and_tables():
    html = web.render_run_log(make_run_view(), today=TODAY)
    assert_page_contract(html)
    # FR-8.1 stages
    assert "Сбор" in html and "Фильтр релевантности" in html
    assert "06:00:01" in html
    assert "8,4\u00a0с" in html
    # FR-8.2 sources
    assert "не ответил" in html and "HTTP 503" in html
    assert "ответил, ничего не отдал" in html
    assert "430\u00a0мс" in html
    # FR-8.3 filtered materials, with codes turned into Russian
    assert "маркетинг без фактов (анонс вебинара)" in html
    assert "вендор не опознан по словарю" in html
    assert "ниже порога публикации (оценка 31, порог 45)" in html
    assert "https://example.test/n8n" in html
    # FR-8.4 cost
    assert "Вызовов модели: 10." in html
    assert "$0.27" in html
    assert "93\u00a0000" in html
    # FR-9.4 history
    assert "Предыдущие прогоны" in html
    assert "16 августа, вчера" in html


def test_run_log_page_has_no_raw_json():
    html = web.render_run_log(make_run_view(), today=TODAY)
    body = html.split("</style>", 1)[1]
    assert '{"' not in body
    assert "log_json" not in body
    assert "payload" not in body


def test_run_log_escapes_material_titles():
    html = web.render_run_log(make_run_view(), today=TODAY)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_run_log_without_a_run():
    html = web.render_run_log(None, today=TODAY)
    assert_page_contract(html)
    assert "Логов прогонов нет" in html


def test_run_log_with_nothing_filtered():
    html = web.render_run_log(make_run_view(filtered=[]), today=TODAY)
    assert "Ни один материал не отклонён." in html


# -- a run that hangs is not a run that runs ---------------------------


UNFINISHED = dict(status="running", finished_at=None)


def test_a_run_that_never_finished_reads_as_stalled():
    """The page makes the distinction the product promises to make."""
    run = make_run_view(
        started_at="2026-08-17T06:00:00+00:00",
        history=[],
        **UNFINISHED,
    )
    html = web.render_run_log(
        run, today=TODAY, now=datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    )

    assert "<h1>Прогон завис</h1>" in html
    assert "Прогон выполняется" not in html
    assert "окончание не записано" in html


def test_a_fresh_run_without_an_end_is_still_running():
    run = make_run_view(
        started_at="2026-08-17T06:00:00+00:00",
        history=[],
        **UNFINISHED,
    )
    html = web.render_run_log(
        run, today=TODAY, now=datetime(2026, 8, 17, 6, 10, tzinfo=UTC)
    )

    assert "<h1>Прогон выполняется</h1>" in html
    assert "завис" not in html


def test_the_stall_window_is_half_an_hour():
    started = "2026-08-17T06:00:00+00:00"
    just_inside = datetime(2026, 8, 17, 6, 29, tzinfo=UTC)
    just_outside = datetime(2026, 8, 17, 6, 31, tzinfo=UTC)

    assert web.run_status_key("running", started, None, just_inside) == "running"
    assert web.run_status_key("running", started, None, just_outside) == "stalled"
    # A run that reported its end is never re-judged by the clock.
    assert (
        web.run_status_key("ok", started, "2026-08-17T06:04:00+00:00", just_outside)
        == "ok"
    )


def test_a_run_a_later_run_overtook_is_stalled_whatever_the_window():
    """One pipeline at a time: a newer run means this one is not running."""
    history = [
        web.RunHistoryRow(
            "run-2",
            TODAY,
            "running",
            started_at="2026-08-17T06:20:00+00:00",
            finished_at=None,
        ),
        web.RunHistoryRow(
            "run-1",
            TODAY,
            "running",
            started_at="2026-08-17T06:00:00+00:00",
            finished_at=None,
        ),
    ]
    html = web.render_run_log(
        make_run_view(
            run_id="run-2",
            started_at="2026-08-17T06:20:00+00:00",
            history=history,
            **UNFINISHED,
        ),
        today=TODAY,
    )

    # The run on the page started last and is reported as running; the one it
    # overtook is reported as hanging, in the same table.
    assert "<h1>Прогон выполняется</h1>" in html
    assert "Прогон завис" in html
    # Two rows, two different verdicts: the newest run and its own heading.
    assert html.count("Прогон выполняется") == 2
    assert html.count("Прогон завис") == 1


def test_the_reference_moment_comes_from_the_store_not_the_clock():
    run = make_run_view(
        started_at="2026-08-17T06:00:00+00:00",
        history=[
            web.RunHistoryRow(
                "run-1",
                TODAY,
                "running",
                started_at="2026-08-17T06:00:00+00:00",
                finished_at=None,
            )
        ],
        stages=[
            web.StageRow(
                name="collect",
                in_count=14,
                out_count=61,
                started_at="2026-08-17T06:00:01+00:00",
                duration_ms=7_200_000,
            )
        ],
        **UNFINISHED,
    )

    assert run.latest_moment == datetime(2026, 8, 17, 8, 0, 1, tzinfo=UTC)
    assert "<h1>Прогон завис</h1>" in web.render_run_log(run, today=TODAY)


# -- a label that contradicts the number next to it --------------------


def test_a_source_that_brought_back_less_is_not_called_empty():
    run = make_run_view(
        sources=[
            web.SourceRow("pinecone_release_notes", "empty", items_count=8),
            web.SourceRow("azure_model_retirement_schedule", "empty", items_count=0),
        ]
    )
    html = web.render_run_log(run, today=TODAY)
    text = page_text(html)

    assert "ответил меньше ожидаемого" in text
    assert text.count("ответил, ничего не отдал") == 1
    # The row with eight materials in it never says it returned nothing.
    row = re.search(r"<tr><td>Pinecone release notes</td><td>([^<]+)</td>", html)
    assert row and row.group(1) == "ответил меньше ожидаемого"


def test_the_sources_sentence_counts_the_two_faults_apart():
    run = make_run_view(
        sources_configured=None,
        sources=[
            web.SourceRow("anthropic_api_release_notes", "ok", items_count=12),
            web.SourceRow("pinecone_release_notes", "empty", items_count=8),
            web.SourceRow("fireworks_changelog", "empty", items_count=1),
            web.SourceRow("azure_model_retirement_schedule", "empty", items_count=0),
        ],
    )

    assert web.sources_sentence(run) == (
        "Опрошено 4 источника: 1 сообщил новое, 2 ответили меньше ожидаемого, "
        "1 ответил без записей."
    )


# -- machine names -----------------------------------------------------


THEME_FILE = """
theme:
  name: "Тема"
corpus:
  vendors:
    - id: anthropic
      label: "Anthropic"
      aliases: [Anthropic, claude, "Claude Code"]
    - id: openai
      label: "OpenAI"
      aliases: [OpenAI, "Open AI", gpt]
    - id: n8n
      label: "n8n"
      aliases: [n8n, "n8n.io"]
    - id: mcp
      label: "Model Context Protocol"
      aliases: [MCP, "MCP servers",
                modelcontextprotocol]
  change_types:
    - id: deprecation
      label: "Отключение"
"""


def theme_names(tmp_path: Path) -> web.Names:
    path = tmp_path / "theme.yaml"
    path.write_text(THEME_FILE, encoding="utf-8")
    return web.read_theme_names(path)


def test_names_are_read_out_of_the_theme_file(tmp_path):
    names = theme_names(tmp_path)

    assert names.labels["openai"] == "OpenAI"
    assert names.labels["mcp"] == "Model Context Protocol"
    # Change types live outside the vendors block and are not names here.
    assert "deprecation" not in names.labels
    # A word only ever seen inside a longer name proves nothing on its own.
    assert "context" not in names.words


def test_a_source_id_reaches_the_page_as_a_name(tmp_path):
    names = theme_names(tmp_path)
    run = make_run_view(
        sources=[
            web.SourceRow("mcp_spec_versioning", "ok", items_count=5),
            web.SourceRow("openai_deprecations", "ok", items_count=23),
            web.SourceRow("n8n_release_notes", "ok", items_count=39),
        ]
    )
    text = page_text(web.render_run_log(run, today=TODAY, names=names))

    assert "MCP spec versioning" in text
    assert "OpenAI deprecations" in text
    assert "n8n release notes" in text
    assert "mcp_spec_versioning" not in text
    assert "_" not in text


def test_the_corpus_spells_a_vendor_the_way_the_cards_do(tmp_path):
    names = theme_names(tmp_path)
    corpus = web.CorpusView(
        statements=33,
        cells=[
            web.CorpusCell("anthropic", "deprecation", 13),
            web.CorpusCell("openai", "limits", 1),
        ],
    )
    text = page_text(web.render_corpus(corpus, today=TODAY, names=names))

    # The card headline says «OpenAI»; the corpus page is two clicks away and
    # says the same thing.
    assert "Anthropic, OpenAI" in text
    assert "anthropic" not in text
    assert "openai" not in text


def test_a_name_is_never_invented_when_the_config_is_silent():
    assert web.human_name("cursor_changelog") == "Cursor changelog"
    assert web.human_name("n8n_release_notes") == "n8n release notes"
    assert web.human_name("") == ""


# -- corpus page -------------------------------------------------------


def test_corpus_page_shows_volume_depth_vendors_and_density():
    html = web.render_corpus(make_corpus_view(), today=TODAY)
    assert_page_contract(html)
    assert "412" in html
    assert "1 февраля, 197 дней назад" in html
    assert "16 августа, вчера" in html
    assert "Глубина 196 дней" in html
    # No config here, so the page falls back to a careful reading of the slug.
    assert "Anthropic" in html and "Openai" in html and "n8n" in html
    assert "отключения" in html and "лимиты" in html
    # column headers stay short enough not to wrap into the next column
    assert "ломающее изменение" not in html
    assert 'class="num dense">24' in html
    assert 'class="num empty">—' in html


def test_empty_corpus_page():
    html = web.render_corpus(
        web.CorpusView(statements=0, cells=[], ready=False), today=TODAY
    )
    assert_page_contract(html)
    assert "Корпус пуст." in html


# -- self-containment --------------------------------------------------


@pytest.mark.parametrize(
    "page",
    [
        lambda: web.render_digest(
            [make_lead_signal(), make_second_signal()], today=TODAY
        ),
        lambda: web.render_digest([make_quiet_signal()], today=TODAY),
        lambda: web.render_digest([make_failure_signal()], today=TODAY),
        lambda: web.render_run_log(make_run_view(), today=TODAY),
        lambda: web.render_corpus(make_corpus_view(), today=TODAY),
    ],
)
def test_every_page_is_self_contained(page):
    html = page()
    assert_page_contract(html)
    assert "<style>" in html, "стили должны быть внутри файла"
    assert 'rel="stylesheet"' not in html
    # anchors to sources survive; they are content, not page dependencies
    assert "<a href=" in html


def test_relative_navigation_only():
    html = web.render_digest([make_lead_signal()], today=TODAY)
    nav = html.split('<nav class="nav">')[1].split("</nav>")[0]
    assert "http" not in nav
    assert 'href="run-log.html"' in nav
    assert 'href="corpus.html"' in nav


# -- formatting units --------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (date(2026, 8, 17), "17 августа, сегодня"),
        (date(2026, 8, 18), "18 августа, завтра"),
        (date(2026, 8, 16), "16 августа, вчера"),
        (date(2026, 10, 15), "15 октября, через 59 дней"),
        (date(2026, 6, 1), "1 июня, 77 дней назад"),
        (date(2027, 1, 1), "1 января 2027, через 137 дней"),
    ],
)
def test_dates_always_carry_the_distance(value, expected):
    assert web.fmt_date(value, TODAY) == expected


def test_date_precision_is_respected():
    assert web.fmt_date(date(2026, 10, 1), TODAY, DatePrecision.MONTH) == "октябрь 2026"
    assert web.fmt_date(date(2026, 1, 1), TODAY, DatePrecision.YEAR) == "2026 год"
    # A guessed year makes "через 59 дней" false precision, so it is dropped.
    marked = web.fmt_date(date(2026, 10, 15), TODAY, DatePrecision.INFERRED)
    assert marked == "15 октября (год не указан в источнике)"
    assert "дней" not in marked


def test_plural_forms():
    assert web.count_phrase(1, web.DAY_FORMS) == "1 день"
    assert web.count_phrase(2, web.DAY_FORMS) == "2 дня"
    assert web.count_phrase(11, web.DAY_FORMS) == "11 дней"
    assert web.count_phrase(21, web.DAY_FORMS) == "21 день"
    assert web.spelled_count_phrase(3, web.RECORD_FORMS) == "три записи"
    assert web.spelled_count_phrase(12, web.RECORD_FORMS) == "12 записей"


def test_unparsable_fact_value_is_printed_verbatim():
    value, _ = web.parse_date_value("осенью 2026")
    assert value is None
    signal = make_lead_signal(
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="осенью 2026",
                source_url="https://example.test/x",
                evidence="this autumn",
                evidence_verified=True,
            )
        ]
    )
    html = web.render_digest([signal], today=TODAY)
    assert "осенью 2026" in html
    assert "Дата отключения в источнике не указана." in html


# -- architecture ------------------------------------------------------

ALLOWED_RADAR_MODULES = {"radar.models", "radar.db", "radar.journal"}


def test_surface_imports_nothing_from_the_pipeline():
    """SUR-2 as a test: the surface may read the store and the contract."""
    tree = ast.parse(WEB_SOURCE)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "относительные импорты скрывают зависимость"
            imported.append(node.module or "")
    for module in imported:
        root = module.split(".")[0]
        if root == "radar":
            assert module in ALLOWED_RADAR_MODULES, module
        else:
            assert root in sys.stdlib_module_names, module


def test_surface_does_not_reason_about_significance():
    """No score, no reranking, no model call, no trend arithmetic."""
    tree = ast.parse(WEB_SOURCE)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.keyword) and node.arg:
            used.add(node.arg)
    forbidden = {
        "score",
        "score_rationale",
        "duplicates_count",
        "retrieval",
        "trend_id",
        "embedding",
        "rerank",
        "sort_by_score",
    }
    assert not used & forbidden, used & forbidden


def test_surface_never_reads_the_system_clock():
    """The reference date is a parameter (relative dates must be reproducible)."""
    tree = ast.parse(WEB_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            qualified = f"{getattr(node.func.value, 'id', '')}.{node.func.attr}"
            assert qualified not in {"date.today", "datetime.now", "datetime.utcnow"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"today", "now"}


def test_surface_writes_nothing_to_the_store():
    lowered = WEB_SOURCE.lower()
    for statement in ("insert into", "update ", "delete from", "drop ", "commit()"):
        assert statement not in lowered, statement
    assert "read_only=True" in WEB_SOURCE


# -- end to end --------------------------------------------------------


def seed_store(path: Path) -> None:
    conn = init_db(path)
    publish_signals(conn, "run-1", [make_lead_signal(), make_second_signal()])
    conn.execute(
        "INSERT INTO runs (run_id, started_at, finished_at, status, for_date, cost_usd, "
        "model_calls, tokens_in, tokens_out, log_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run-1",
            "2026-08-17T06:00:00+00:00",
            "2026-08-17T06:04:12+00:00",
            "ok",
            TODAY.isoformat(),
            0.27,
            10,
            93000,
            4000,
            '{"stages": [{"stage": "collect", "in_count": 14, "out_count": 61, '
            '"started_at": "2026-08-17T06:00:01+00:00", "duration_ms": 8400, "errors": []}], '
            '"notes": ["источник mcp-servers отключён"], '
            '"delivery": [{"channel": "telegram", "status": "доставлено"}]}',
        ),
    )
    conn.execute(
        "INSERT INTO source_runs (run_id, source_id, status, items_count, latency_ms, error) "
        "VALUES ('run-1', 'mcp-servers', 'failed', 0, NULL, 'HTTP 503')"
    )
    conn.execute(
        "INSERT INTO filtered_items (run_id, url, title, reason_code, reason_note, stage) "
        "VALUES ('run-1', 'https://example.test/x', ?, 'маркетинг_без_фактов', NULL, 'filter')",
        (NASTY_TITLE,),
    )
    conn.execute(
        "INSERT INTO model_calls (call_id, run_id, stage, model, provider, tokens_in, "
        "tokens_out, cost_usd, cached, created_at) VALUES ('c1', 'run-1', 'enrich', "
        "'opus', 'anthropic', 41000, 3100, 0.21, 0, '2026-08-17T06:01:00+00:00')"
    )
    for index, (vendor, change_type) in enumerate(
        [
            ("anthropic", "deprecation"),
            ("anthropic", "deprecation"),
            ("anthropic", "deprecation"),
            ("openai", "limits"),
        ]
    ):
        conn.execute(
            "INSERT INTO event_statements (statement_id, text, vendor, change_type, event_date, "
            "source_url, statement_index, evidence, ingested_at, ingest_mode, extractor_model, "
            "prompt_version, raw_material_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"stmt-{index}",
                f"{vendor} {change_type}",
                vendor,
                change_type,
                f"2026-0{index + 2}-01",
                f"https://example.test/{index}",
                0,
                "quote",
                NOW.isoformat(),
                "backfill",
                "opus",
                "v1",
                f"ref-{index}",
            ),
        )
    conn.commit()
    conn.close()


def test_build_site_writes_three_linked_pages(tmp_path):
    db_path = tmp_path / "radar.db"
    seed_store(db_path)
    paths = web.build_site(db_path, tmp_path / "site", today=TODAY)

    assert set(paths) == {"digest", "run_log", "corpus"}
    for path in paths.values():
        assert path.exists()
        assert_page_contract(path.read_text(encoding="utf-8"))

    digest = paths["digest"].read_text(encoding="utf-8")
    run_log = paths["run_log"].read_text(encoding="utf-8")
    corpus = paths["corpus"].read_text(encoding="utf-8")

    # DR-10: the same record, visible on the surface next to the others
    assert "sig-lead" in digest
    assert 'href="run-log.html"' in digest
    assert "mcp-servers" in run_log and "HTTP 503" in run_log
    assert "$0.27" in run_log
    assert "Anthropic" in corpus
    assert 'href="digest.html"' in corpus


def test_build_site_leaves_the_store_untouched(tmp_path):
    db_path = tmp_path / "radar.db"
    seed_store(db_path)
    before = db_path.read_bytes()
    web.build_site(db_path, tmp_path / "site", today=TODAY)
    assert db_path.read_bytes() == before

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert count == 2


def test_one_source_failure_reads_as_russian():
    html = web.render_digest([make_lead_signal()], today=TODAY)
    assert "Один источник не ответил: Mcp servers." in html


def test_two_source_failures_read_as_russian():
    signal = make_lead_signal(
        run_summary=RunSummary(
            sources_checked=14, sources_failed=["mcp-servers", "cursor-changelog"]
        )
    )
    html = web.render_digest([signal], today=TODAY)
    assert "Два источника не ответили: Mcp servers, Cursor changelog." in html


def test_a_deadline_is_listed_once_even_with_two_records():
    """The upcoming entry and the fact describe the same day (voice 2)."""
    html = web.render_digest([make_quiet_signal()], today=TODAY)
    assert html.count("15 октября, через 59 дней") == 1
    assert "отключается claude-3-opus" in html
    # the verbatim quote does not get a second line of its own
    assert "will be retired on October 15" not in html


def test_upcoming_falls_back_to_facts_when_the_field_is_empty():
    signal = make_quiet_signal(upcoming=[])
    html = web.render_digest([signal], today=TODAY)
    assert "Ближайшее" in html
    assert "15 октября, через 59 дней" in html
    assert "claude-3-opus" in html


def test_sub_cent_cost_does_not_break_the_column():
    run = make_run_view(costs=[web.StageCost("cluster", calls=2, usd=0.004)])
    html = web.render_run_log(run, today=TODAY)
    assert "&lt; $0.01" in html or "< $0.01" in html
    assert "0.0040" not in html


def test_fact_block_shows_the_relative_date_not_the_raw_value():
    """voice 4 has no exception for the evidence block."""
    html = web.render_digest([make_lead_signal()], today=TODAY)
    assert "Дата отключения:" in html
    assert (
        '<span class="value">15 октября, через 59 дней — claude-3-opus</span>' in html
    )
    assert "2026-10-15" not in html


def test_inferred_fact_date_drops_the_day_count():
    signal = make_lead_signal(
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="15 октября",
                value_date=date(2026, 10, 15),
                date_precision=DatePrecision.INFERRED,
                source_url="https://docs.claude.com/deprecations",
                evidence="retired on October 15",
                evidence_verified=True,
            )
        ]
    )
    html = web.render_digest([signal], today=TODAY)
    assert "15 октября (год не указан в источнике)" in html
    assert "через 59 дней" not in html


def test_context_sentence_comes_from_the_core():
    html = web.render_digest([make_lead_signal()], today=TODAY)
    assert "Третий раз с мая Anthropic объявляет отключение" in html
    # the surface does not paraphrase the corpus on its own
    assert "Событие повторяется" not in html
    assert "повторяется третий раз" not in html


def test_without_a_context_note_only_the_disclosure_label_is_shown():
    signal = make_lead_signal(context_note=None)
    html = web.render_digest([signal], today=TODAY)
    assert "Показать три записи" in html
    assert (
        "Событие" not in html.split('<details class="ctx">')[1].split("</summary>")[0]
    )


def test_failure_page_names_the_stage_and_the_last_good_day():
    html = web.render_digest([make_failure_signal()], today=TODAY)
    assert "Стадия: Обогащение." in html
    assert "Последняя удачная сводка — 16 августа, вчера." in html
    assert "Собрано 34 материала, обработать не удалось." in html


def test_quiet_day_statistics_come_from_the_run_summary():
    html = web.render_digest([make_quiet_signal()], today=TODAY)
    assert "Проверено 14 источников, 23 материала отклонено." in html


def test_digest_reads_signals_and_nothing_else():
    """SUR-1: the page is a function of the published signals."""
    tree = ast.parse(WEB_SOURCE)
    render = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "render_digest"
    )
    params = {arg.arg for arg in render.args.args} | {
        arg.arg for arg in render.args.kwonlyargs
    }
    # `names` is presentation data, not a second source of truth: the page
    # still reads only published signals. Without it the footer printed
    # gh_google_gemini_gemini_cli to the reader while the neighbouring page
    # said "Google Gemini CLI".
    assert params == {"signals", "today", "links", "names"}
    footer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_digest_footer"
    )
    used = {node.attr for node in ast.walk(footer) if isinstance(node, ast.Attribute)}
    assert "run_summary" in used
    assert "source_id" not in used


# -- the run log has to add up -----------------------------------------


def test_cost_total_matches_the_rows_below_it():
    """The headline number is the sum of the itemized calls, not a counter."""
    html = web.render_run_log(make_run_view(), today=TODAY)
    assert "Вызовов модели: 10." in html
    assert "Счётчик прогона называет" not in html
    body = html.split("<h3>Стоимость</h3>", 1)[1]
    assert '<td class="num dense">10</td>' in body


def test_a_counter_disagreeing_with_the_records_is_named():
    run = make_run_view(model_calls=37, usd=0.48)
    html = web.render_run_log(run, today=TODAY)
    # the page counts what it can show
    assert "Вызовов модели: 10." in html
    assert "Счётчик прогона называет 37 вызовов" in html
    assert "На странице сложены подробные записи: 10 вызовов, $0.27." in html


def test_cost_falls_back_to_the_run_row_without_itemized_calls():
    run = make_run_view(costs=[])
    html = web.render_run_log(run, today=TODAY)
    assert "Вызовов модели: 10." in html
    assert "Счётчик прогона называет" not in html


def test_funnel_names_the_materials_dropped_without_a_reason():
    html = web.render_run_log(make_run_view(), today=TODAY)
    # filter 61 -> 19 drops 42, and four reasons are recorded in the fixture
    assert "Стадии отсеяли 42 материала, причины записаны у 4." in html
    assert "У 38 материалов причина не записана." in html


def test_funnel_closes_when_every_drop_has_a_reason():
    run = make_run_view(
        stages=[
            web.StageRow(
                name="filter",
                in_count=10,
                out_count=8,
                started_at="2026-08-17T06:00:12+00:00",
                duration_ms=1000,
            )
        ],
        filtered=[
            web.FilteredRow("A", "https://example.test/a", "слишком_общее", "filter"),
            web.FilteredRow("B", "https://example.test/b", "другое", "filter"),
        ],
    )
    html = web.render_run_log(run, today=TODAY)
    assert "Отсеяно 2 материала, причина записана у каждого." in html


def test_stage_table_shows_the_drop_and_the_recorded_reasons():
    html = web.render_run_log(make_run_view(), today=TODAY)
    assert '<th class="num">Отсеяно</th>' in html
    assert '<th class="num">Причин записано</th>' in html


def test_sources_line_names_configured_and_answered():
    run = make_run_view(sources_configured=14)
    html = web.render_run_log(run, today=TODAY)
    assert "В прогоне участвовало 14 источников, результат записан по 3" in html
    assert "1 сообщил новое, 1 не ответил, 1 ответил без записей." in html


def test_sources_line_without_a_configured_count():
    html = web.render_run_log(make_run_view(), today=TODAY)
    assert "Опрошено 3 источника:" in html


def test_no_reason_code_reaches_the_page_with_underscores():
    """FR-9.2: a code the reader has to decode is not a readable log."""
    for row in make_run_view().filtered:
        text = web._reason_text(row)
        assert "_" not in text, text
    # every code the pipeline can write has a Russian wording
    for code in (
        "не_относится_к_стеку",
        "маркетинг_без_фактов",
        "дубль_вчерашнего",
        "слишком_общее",
        "спекуляция_без_первоисточника",
        "другое",
        "vendor_unresolved",
        "unsupported_quantifier",
        "statement_unsupported",
        "ниже_порога_публикации",
    ):
        assert code in web.REASON_LABELS, code
        assert "_" not in web.REASON_LABELS[code]


def test_build_site_passes_the_source_count_from_the_contract(tmp_path):
    db_path = tmp_path / "radar.db"
    seed_store(db_path)
    paths = web.build_site(db_path, tmp_path / "site", today=TODAY)
    run_log = paths["run_log"].read_text(encoding="utf-8")
    # the lead signal carries run_summary.sources_checked = 14, one row exists
    assert "В прогоне участвовало 14 источников, результат записан по 1" in run_log


def test_cli_builds_from_the_store_without_arguments(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "radar.db"
    seed_store(db_path)
    out = tmp_path / "site"
    code = web.main(["--db", str(db_path), "--out", str(out)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "опорная дата: 2026-08-17" in printed
    for name in ("digest.html", "run-log.html", "corpus.html"):
        assert (out / name).exists()


def test_cli_reference_date_comes_from_the_data(tmp_path):
    db_path = tmp_path / "radar.db"
    seed_store(db_path)
    assert web.reference_date(db_path) == TODAY
    assert web.reference_date(db_path, override=date(2026, 1, 1)) == date(2026, 1, 1)


def test_cli_refuses_an_empty_store_instead_of_guessing_the_date(tmp_path):
    db_path = tmp_path / "empty.db"
    init_db(db_path).close()
    assert web.reference_date(db_path) is None
    with pytest.raises(SystemExit):
        web.main(["--db", str(db_path), "--out", str(tmp_path / "site")])


class TestTimesSayWhichClockTheyAreOn:
    """The store writes UTC and the page printed a bare wall clock.

    Everyone reads a bare time as local. On a machine two hours off UTC the
    run log said a run started at 02:51 while the person watching it start had
    05:51 on the wall — and a run that begins just after midnight UTC reads as
    belonging to the day before.
    """

    def test_a_time_carries_its_zone(self):
        assert web.fmt_time("2026-08-18T02:51:56+00:00") == "02:51:56 UTC"

    def test_the_readers_zone_comes_from_the_config(self):
        zone = web.display_zone({"delivery": {"timezone": "Europe/Moscow"}})
        assert web.fmt_time("2026-08-18T02:51:56+00:00", zone) == "05:51:56 MSK"

    def test_an_unknown_zone_falls_back_to_utc_rather_than_failing(self):
        assert web.display_zone(
            {"delivery": {"timezone": "Nowhere/Nothing"}}
        ) == ZoneInfo("UTC")
        assert web.display_zone(None) == ZoneInfo("UTC")

    def test_a_naive_timestamp_is_read_as_utc_not_as_local(self):
        # sqlite hands back strings; one written without an offset must not be
        # silently reinterpreted in whatever zone the machine happens to be in.
        assert web.fmt_time("2026-08-18T02:51:56") == "02:51:56 UTC"


class TestTheCardDoesNotSayItTwice:
    """The core stores the statement whole in headline and summary alike.

    That is right for the store: a surface with room for one line takes the
    headline, a channel with room for a paragraph takes the summary, and PUB-2
    puts the choice on the surface. But when the statement is a single
    sentence the two are the same string, and the card printed it as a title
    and again as its own body.
    """

    def test_a_repeated_sentence_is_printed_once(self):
        sentence = (
            "Anthropic сделал стандартной ценой Claude Sonnet 5 прежнее "
            "вводное предложение в размере $2 за миллион входных токенов"
        )
        signal = make_lead_signal(headline=sentence, summary=sentence)

        card = web.render_card(signal, date(2026, 8, 18), lead=True)

        assert card.count("Anthropic сделал стандартной ценой") == 1

    def test_a_summary_that_says_more_is_kept(self):
        signal = make_lead_signal(
            headline="Anthropic отключает claude-3-opus",
            summary=(
                "Anthropic отключает claude-3-opus. Замена — claude-opus-4-8, "
                "миграция описана в документации."
            ),
        )

        card = web.render_card(signal, date(2026, 8, 18), lead=True)

        assert "Замена" in card


class TestOnlyAKnownRunIsPublished:
    """Three times in one hour the site became "0 sources checked".

    `--out` defaulted to the same directory whatever database was open, so a
    run against a sandbox copy published over the real pages. The result is not
    obviously broken to a reader: it is a confident page saying nothing
    happened today, from a run_id that exists in no table.
    """

    def test_a_run_absent_from_the_store_is_refused(self, tmp_path):
        db = tmp_path / "empty.db"
        conn = init_db(db)
        conn.close()

        with pytest.raises(web.UnknownRun) as caught:
            web.build_site(
                db,
                tmp_path / "out",
                today=date(2026, 8, 18),
                run_id="20260818T064508-73e8a6",
            )

        assert "20260818T064508-73e8a6" in str(caught.value)
        assert not (tmp_path / "out" / "digest.html").exists()

    def test_a_run_in_the_store_publishes(self, tmp_path):
        db = tmp_path / "real.db"
        conn = init_db(db)
        signal = make_lead_signal(run_id="run-known")
        publish_signals(conn, "run-known", [signal])
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, for_date) "
            "VALUES ('run-known', '2026-08-18T06:00:00+00:00', 'ok', '2026-08-18')"
        )
        conn.commit()
        conn.close()

        paths = web.build_site(
            db, tmp_path / "out", today=date(2026, 8, 18), run_id="run-known"
        )

        assert paths["digest"].exists()
