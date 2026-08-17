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
from html.parser import HTMLParser
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
    RetrievalReport,
    Signal,
    SignalType,
    Tier,
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
        delta_note="Третий раз с мая Anthropic объявляет отключение с двухмесячным предупреждением.",
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
                source_url="https://docs.claude.com/deprecations",
                evidence="claude-3-opus will be retired on October 15, 2026",
                evidence_verified=True,
            )
        ],
        precedents=[
            Precedent(
                statement_id="stmt-100",
                text="новые лимиты Tier 1 в OpenAI API",
                source_url="https://platform.openai.com/docs/guides/rate-limits",
                event_date=date(2026, 11, 1),
                vendor="openai",
                change_type=ChangeType.LIMITS,
            )
        ],
        stats={"sources_checked": 14, "items_filtered": 23, "items_collected": 31},
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
        stats={"items_collected": 34, "sources_checked": 14},
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
            web.RunSummary("run-1", TODAY, "ok", 10, 93000, 4000, 0.27),
            web.RunSummary("run-0", date(2026, 8, 16), "ok", 9, 88000, 3800, 0.25),
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
    resolved = make_second_signal(
        signal_id="sig-resolved",
        delta_status=DeltaStatus.RESOLVED,
        delta_note="миграция на Responses API завершена",
        days_tracked=12,
        rank=3,
    )
    html = web.render_digest([make_lead_signal(), resolved], today=TODAY)
    assert (
        "Закрыто: миграция на Responses API завершена, история велась 12 дней." in html
    )


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


def test_quiet_day_without_deadlines_drops_the_whole_block():
    signal = make_quiet_signal(facts=[], precedents=[])
    html = web.render_digest([signal], today=TODAY)
    assert_page_contract(html)
    assert "Ближайшее" not in html


def test_quiet_day_ignores_past_deadlines():
    signal = make_quiet_signal(
        facts=[
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-06-01",
                source_url="https://example.test/old",
                evidence="retired on June 1, 2026",
                evidence_verified=True,
            )
        ],
        precedents=[],
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
    assert "В хранилище нет сигналов за этот прогон." in html
    assert "ничего не изменилось" not in html


def test_footer_reports_unanswered_sources_calmly():
    html = web.render_digest([make_lead_signal()], today=TODAY, run=make_run_view())
    assert "не ответил: mcp-servers." in html
    assert "cursor-changelog ответил, но ничего не отдал." in html
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
    # FR-8.3 filtered materials
    assert "маркетинг без фактов (анонс вебинара)" in html
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


# -- corpus page -------------------------------------------------------


def test_corpus_page_shows_volume_depth_vendors_and_density():
    html = web.render_corpus(make_corpus_view(), today=TODAY)
    assert_page_contract(html)
    assert "412" in html
    assert "1 февраля, 197 дней назад" in html
    assert "16 августа, вчера" in html
    assert "Глубина 196 дней" in html
    assert "anthropic" in html and "openai" in html and "n8n" in html
    assert "отключение" in html and "лимиты" in html
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
    marked = web.fmt_date(date(2026, 10, 15), TODAY, DatePrecision.INFERRED)
    assert marked == "15 октября, через 59 дней (год не указан в источнике)"


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
    assert "anthropic" in corpus
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
    run = make_run_view(
        sources=[web.SourceRow("mcp-servers", "failed", 0, None, "HTTP 503")]
    )
    html = web.render_digest([make_lead_signal()], today=TODAY, run=run)
    assert "Один источник не ответил: mcp-servers." in html


def test_two_source_failures_read_as_russian():
    run = make_run_view(
        sources=[
            web.SourceRow("mcp-servers", "failed", 0, None, "HTTP 503"),
            web.SourceRow("cursor-changelog", "failed", 0, None, "HTTP 500"),
        ]
    )
    html = web.render_digest([make_lead_signal()], today=TODAY, run=run)
    assert "Два источника не ответили: cursor-changelog, mcp-servers." in html or (
        "Два источника не ответили: mcp-servers, cursor-changelog." in html
    )


def test_a_deadline_is_listed_once_even_with_two_records():
    """The corpus record and the quote describe the same day (voice 2)."""
    html = web.render_digest([make_quiet_signal()], today=TODAY)
    assert html.count("15 октября, через 59 дней") == 1


def test_sub_cent_cost_does_not_break_the_column():
    run = make_run_view(costs=[web.StageCost("cluster", calls=2, usd=0.004)])
    html = web.render_run_log(run, today=TODAY)
    assert "&lt; $0.01" in html or "< $0.01" in html
    assert "0.0040" not in html
