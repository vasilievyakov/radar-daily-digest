"""Tests for the Telegram surface.

Two kinds of check live here. Most are about the text a person reads on the
morning of the demo. One is architectural: the surface is parsed as source and
must not import a single pipeline stage (SUR-2). That test is the executable
form of the requirement, and it fails the day someone reaches for the scorer.
"""

from __future__ import annotations

import ast
import json
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
    RunSummary,
    Signal,
    SignalType,
    Tier,
    UpcomingDeadline,
)
from radar.surfaces import telegram

TODAY = date(2026, 8, 17)
RUN_ID = "20260817T060000-ab12cd"
RUN_LOG = "https://radar.local/runs/20260817T060000-ab12cd"
DEPRECATION_URL = "https://docs.claude.com/en/docs/about-claude/model-deprecations"


# --------------------------------------------------------------------------
# Fixtures shaped like a real run
# --------------------------------------------------------------------------


def make_signal(
    signal_id: str = "sig-1",
    signal_type: SignalType = SignalType.DIGEST_ITEM,
    **kwargs,
) -> Signal:
    payload = {
        "signal_id": signal_id,
        "run_id": RUN_ID,
        "signal_type": signal_type,
        "created_at": datetime(2026, 8, 17, 6, 0, tzinfo=UTC),
        "for_date": TODAY,
        "run_log_url": RUN_LOG,
    }
    payload.update(kwargs)
    return Signal(**payload)


def lead_signal(**kwargs) -> Signal:
    payload = {
        "signal_id": "sig-anthropic-opus-sunset",
        "headline": "Anthropic отключает claude-3-opus",
        "summary": (
            "Запросы к модели начнут возвращать ошибку; "
            "на замену предложен claude-opus-4."
        ),
        "why_it_matters": "модель используется в двух ваших проектах",
        "change_type": ChangeType.DEPRECATION,
        "vendor": "anthropic",
        "product": "claude-3-opus",
        "primary_url": DEPRECATION_URL,
        "facts": [
            Fact(
                kind=FactKind.SUNSET_DATE,
                value="2026-10-15",
                source_url=DEPRECATION_URL,
                evidence="claude-3-opus will be retired on October 15, 2026",
                confidence="high",
                evidence_verified=True,
            )
        ],
        "context_label": ContextLabel.RECURRING,
        "precedents": [
            Precedent(
                statement_id="st-1",
                text="Anthropic объявил об отключении claude-2.1",
                source_url=DEPRECATION_URL,
                event_date=date(2026, 5, 12),
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            ),
            Precedent(
                statement_id="st-2",
                text="Anthropic объявил об отключении claude-instant-1.2",
                source_url=DEPRECATION_URL,
                event_date=date(2026, 6, 30),
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            ),
            Precedent(
                statement_id="st-3",
                text="Anthropic объявил об отключении claude-3-sonnet",
                source_url=DEPRECATION_URL,
                event_date=date(2026, 7, 21),
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            ),
        ],
        "score": 91,
        "rank": 1,
        "tier": Tier.LEAD,
        "duplicates_count": 8,
        "score_rationale": "ломающее изменение у вендора из активного стека",
        "stats": {
            "sources_checked": 14,
            "sources_failed": 2,
            "source_failed:Cursor changelog": 1,
            "source_failed:MCP servers": 1,
        },
    }
    payload.update(kwargs)
    return make_signal(**payload)


def digest_run() -> list[Signal]:
    return [
        lead_signal(),
        make_signal(
            signal_id="sig-openai-tier1-limits",
            headline="OpenAI поднимает лимиты Tier 1",
            summary="Потолок запросов в минуту вырастет вдвое для всех проектов.",
            change_type=ChangeType.LIMITS,
            vendor="openai",
            primary_url="https://platform.openai.com/docs/guides/rate-limits",
            facts=[
                Fact(
                    kind=FactKind.EFFECTIVE_DATE,
                    value="2026-11-01",
                    source_url="https://platform.openai.com/docs/guides/rate-limits",
                    evidence="new Tier 1 limits take effect on November 1, 2026",
                    evidence_verified=True,
                )
            ],
            score=74,
            rank=2,
            tier=Tier.STANDARD,
        ),
        make_signal(
            signal_id="sig-n8n-1-62-0",
            headline="n8n 1.62.0",
            summary="Исправлена потеря данных в Webhook-ноде. Обновление рекомендовано.",
            change_type=ChangeType.RELEASE,
            vendor="n8n",
            primary_url="https://github.com/n8n-io/n8n/releases/tag/n8n%401.62.0",
            score=61,
            rank=3,
            tier=Tier.STANDARD,
        ),
        make_signal(
            signal_id="sig-cursor-config-format",
            headline="Cursor меняет формат файла конфигурации",
            summary="Старый .cursorrules перестанет читаться после обновления.",
            change_type=ChangeType.BREAKING_CHANGE,
            vendor="cursor",
            primary_url="https://cursor.com/changelog",
            facts=[
                Fact(
                    kind=FactKind.EFFECTIVE_DATE,
                    value="2026-08-18",
                    source_url="https://cursor.com/changelog",
                    evidence=".cursorrules is removed in the next release",
                    evidence_verified=True,
                )
            ],
            score=58,
            rank=4,
            tier=Tier.STANDARD,
        ),
        make_signal(
            signal_id="sig-mcp-sdk-0-9-2",
            headline="MCP SDK 0.9.2",
            summary="Закрыта уязвимость в обработке ответа сервера.",
            change_type=ChangeType.SECURITY,
            vendor="mcp",
            primary_url="https://github.com/modelcontextprotocol/python-sdk/releases",
            score=55,
            rank=5,
            tier=Tier.STANDARD,
        ),
        make_signal(
            signal_id="sig-langchain-docs",
            headline="LangChain переписал раздел документации по агентам",
            summary="Структура разделов изменилась, ссылки на старые якоря не работают.",
            change_type=ChangeType.OTHER,
            vendor="langchain",
            primary_url="https://python.langchain.com/docs/",
            score=31,
            rank=6,
            tier=Tier.BACKGROUND,
        ),
    ]


def quiet_signal(**kwargs) -> Signal:
    payload = {
        "signal_id": "sig-quiet-2026-08-17",
        "signal_type": SignalType.QUIET_DAY,
        "stats": {"sources_checked": 14, "items_rejected": 23},
        "precedents": [
            Precedent(
                statement_id="st-1",
                text="отключается claude-3-opus",
                source_url=DEPRECATION_URL,
                event_date=date(2026, 10, 15),
                vendor="anthropic",
                change_type=ChangeType.DEPRECATION,
            ),
            Precedent(
                statement_id="st-2",
                text="новые лимиты Tier 1 в OpenAI API",
                source_url="https://platform.openai.com/docs/guides/rate-limits",
                event_date=date(2026, 11, 1),
                vendor="openai",
                change_type=ChangeType.LIMITS,
            ),
        ],
    }
    payload.update(kwargs)
    return make_signal(**payload)


def failure_signal(**kwargs) -> Signal:
    payload = {
        "signal_id": "sig-failure-2026-08-17",
        "signal_type": SignalType.RUN_FAILURE,
        "failure_reason": "сбой на стадии обогащения",
        "stats": {"items_collected": 34, "last_success_days_ago": 1},
    }
    payload.update(kwargs)
    return make_signal(**payload)


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.add(tag)


def tags_of(text: str) -> set[str]:
    collector = _TagCollector()
    collector.feed(text)
    return collector.tags


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


class TestDates:
    def test_future_date_carries_distance(self):
        assert (
            telegram.format_date(date(2026, 10, 15), TODAY)
            == "15 октября, через 59 дней"
        )

    def test_past_date_carries_distance(self):
        assert telegram.format_date(date(2026, 6, 1), TODAY) == "1 июня, 77 дней назад"

    def test_today(self):
        assert telegram.format_date(TODAY, TODAY) == "сегодня"

    def test_tomorrow(self):
        assert telegram.format_date(date(2026, 8, 18), TODAY) == "завтра"

    def test_yesterday(self):
        assert telegram.format_date(date(2026, 8, 16), TODAY) == "вчера"

    def test_day_plural_forms(self):
        assert telegram.format_date(date(2026, 8, 19), TODAY).endswith("через 2 дня")
        assert telegram.format_date(date(2026, 8, 22), TODAY).endswith("через 5 дней")
        assert telegram.format_date(date(2026, 9, 7), TODAY).endswith("через 21 день")

    def test_inferred_year_is_marked_and_carries_no_distance(self):
        phrase = telegram.format_date(date(2026, 10, 15), TODAY, DatePrecision.INFERRED)
        assert phrase == "15 октября (год не указан в источнике)"
        assert "через" not in phrase

    def test_month_precision(self):
        assert (
            telegram.format_date(date(2026, 10, 1), TODAY, DatePrecision.MONTH)
            == "октябрь 2026"
        )

    def test_missing_date_is_said_out_loud(self):
        assert (
            telegram.missing_date_phrase(ChangeType.DEPRECATION)
            == "дата отключения в источнике не указана"
        )
        assert (
            telegram.missing_date_phrase(ChangeType.PRICING)
            == "дата вступления в силу в источнике не указана"
        )
        assert telegram.missing_date_phrase(ChangeType.RELEASE) is None

    def test_reference_date_is_a_parameter_not_the_clock(self):
        replay = telegram.render_digest(digest_run(), today=date(2026, 9, 1))
        assert "через 44 дня" in replay
        assert "через 59 дней" not in replay

    def test_deprecation_without_date_says_so(self):
        signal = lead_signal(facts=[])
        text = telegram.render_digest([signal], TODAY)
        assert "Дата отключения в источнике не указана." in text


# --------------------------------------------------------------------------
# The three message types
# --------------------------------------------------------------------------


class TestDigest:
    def test_digest_renders_headline_date_evidence_and_source(self):
        text = telegram.render_digest(digest_run(), TODAY)
        assert "<b>Anthropic отключает claude-3-opus</b>" in text
        assert "15 октября, через 59 дней." in text
        assert "Почему это важно: модель используется в двух ваших проектах." in text
        assert "«claude-3-opus will be retired on October 15, 2026»" in text
        assert DEPRECATION_URL in text
        assert len(text) <= telegram.MAX_MESSAGE_CHARS

    def test_lead_gets_a_spread_and_the_rest_one_line_each(self):
        text = telegram.render_digest(digest_run(), TODAY)
        blocks = text.split("\n\n")
        rows_index = next(i for i, b in enumerate(blocks) if "n8n 1.62.0" in b)
        # Headline, date paragraph, why, evidence with the link, context.
        assert rows_index >= 5

        rows_block = blocks[rows_index]
        rows = rows_block.split("\n")
        assert len(rows) == 4
        for row in rows:
            assert "\n" not in row
            assert len(row) < 200
        assert "OpenAI поднимает лимиты Tier 1</a> — с 1 ноября, через 76 дней" in text
        assert "n8n 1.62.0</a> — исправлена потеря данных в Webhook-ноде" in text

    def test_rows_link_to_the_primary_source(self):
        text = telegram.render_digest(digest_run(), TODAY)
        assert 'href="https://cursor.com/changelog"' in text
        assert 'href="https://github.com/n8n-io/n8n/releases/tag/n8n%401.62.0"' in text

    def test_relative_date_for_tomorrow_inside_a_row(self):
        text = telegram.render_digest(digest_run(), TODAY)
        assert "Cursor меняет формат файла конфигурации</a> — завтра" in text

    def test_context_sentence_is_the_one_the_core_wrote(self):
        """DR-10: one record, three faces, and one wording of the claim."""
        note = "Anthropic: отключения, третий раз с 12 мая."
        text = telegram.render_digest([lead_signal(context_note=note)], TODAY)

        assert note in text
        # The label is gone: it named the block instead of saying anything.
        assert "Повторяется" not in text
        assert "самая ранняя запись" not in text
        assert f'<a href="{RUN_LOG}">Показать три записи.</a>' in text

    def test_without_the_field_the_surface_states_only_what_it_can_show(self):
        text = telegram.render_digest(digest_run(), TODAY)

        assert "Самая ранняя запись в корпусе — 12 мая, 97 дней назад." in text
        assert "Повторяется" not in text
        assert f'<a href="{RUN_LOG}">Показать три записи.</a>' in text

    def test_precedents_without_dates_leave_the_navigation_alone(self):
        signal = lead_signal(
            precedents=[
                Precedent(
                    statement_id="st-1",
                    text="Anthropic объявил об отключении claude-2.1",
                    source_url=DEPRECATION_URL,
                    event_date=None,
                    vendor="anthropic",
                    change_type=ChangeType.DEPRECATION,
                )
            ]
        )
        text = telegram.render_digest([signal], TODAY)

        assert f'<a href="{RUN_LOG}">Показать одну запись.</a>' in text
        assert "Повторяется" not in text

    def test_signal_without_precedents_renders_whole(self):
        signal = lead_signal(
            precedents=[], context_label=ContextLabel.NOT_FOUND_IN_CORPUS
        )
        text = telegram.render_digest([signal], TODAY)
        assert "<b>Anthropic отключает claude-3-opus</b>" in text
        assert "15 октября, через 59 дней." in text
        assert "Показать" not in text
        assert "Повторяется" not in text
        # A label that changes nothing in the reading never reaches the screen.
        assert "not_found_in_corpus" not in text

    def test_updated_delta_note_is_shown(self):
        signal = lead_signal(
            delta_status=DeltaStatus.UPDATED,
            delta_note="вчера этого не было, сегодня названа дата отключения: 2026-10-15",
        )
        text = telegram.render_digest([signal], TODAY)
        assert (
            "Вчера этого не было, сегодня названа дата отключения: 2026-10-15." in text
        )

    def test_footer_names_unavailable_sources(self):
        text = telegram.render_digest(digest_run(), TODAY)
        assert "Два источника не ответили: Cursor changelog, MCP servers." in text
        assert f'<a href="{RUN_LOG}">Лог прогона.</a>' in text

    def test_source_that_answered_with_nothing_is_named_apart(self):
        signal = lead_signal(
            stats={"sources_checked": 14, "source_empty:Cursor changelog": 1}
        )
        text = telegram.render_digest([signal], TODAY)
        assert "Cursor changelog ответил, но ничего не отдал." in text

    def test_last_line_closes_a_story_when_one_closed(self):
        run = digest_run()
        run.append(
            make_signal(
                signal_id="sig-responses-api-migration",
                headline="Миграция на Responses API завершена",
                delta_status=DeltaStatus.RESOLVED,
                days_tracked=12,
                tier=Tier.STANDARD,
                rank=7,
            )
        )
        text = telegram.render_digest(run, TODAY)
        last = text.split("\n\n")[-2]
        assert (
            last
            == "Закрыто: Миграция на Responses API завершена, история велась 12 дней."
        )

    def test_run_log_is_reachable_when_no_source_failed(self):
        signal = lead_signal(stats={})
        text = telegram.render_digest([signal], TODAY)
        assert f'<a href="{RUN_LOG}">Лог прогона.</a>' in text

    def test_signal_id_sits_in_the_basement(self):
        text = telegram.render_digest(digest_run(), TODAY)
        assert text.endswith("<code>sig-anthropic-opus-sunset</code>")

    def test_screen_never_shows_the_score_or_the_duplicate_count(self):
        text = telegram.render_digest(digest_run(), TODAY)
        assert "91" not in text
        assert "ломающее изменение у вендора" not in text
        # duplicates_count is 8 for the lead and interests only the builder.
        assert "8 перепечаток" not in text
        assert "duplicates" not in text


class TestQuietDay:
    def test_quiet_day_speaks_about_the_reader_first(self):
        text = telegram.render_quiet_day(quiet_signal(), TODAY)
        assert text.startswith("Сегодня в вашем стеке ничего не изменилось.")
        assert (
            "Ближайшее:\n15 октября, через 59 дней — отключается claude-3-opus" in text
        )
        assert "1 ноября, через 76 дней — новые лимиты Tier 1 в OpenAI API" in text
        assert "Проверено 14 источников, 23 материала отклонено." in text
        assert f'<a href="{RUN_LOG}">Лог прогона.</a>' in text
        assert text.endswith("<code>sig-quiet-2026-08-17</code>")

    def test_the_quiet_day_block_reads_the_field_the_core_fills(self):
        """A quiet-day signal written by the core carries `upcoming` and no
        precedents at all, so a surface reading precedents shows nothing."""
        signal = quiet_signal(
            precedents=[],
            upcoming=[
                UpcomingDeadline(
                    when=date(2026, 10, 15),
                    what="отключается claude-3-opus",
                    vendor="anthropic",
                    source_url=DEPRECATION_URL,
                ),
                UpcomingDeadline(
                    when=date(2026, 11, 1),
                    what="новые лимиты Tier 1 в OpenAI API",
                    vendor="openai",
                    source_url="https://platform.openai.com/docs/guides/rate-limits",
                ),
            ],
        )
        text = telegram.render_quiet_day(signal, TODAY)

        assert (
            "Ближайшее:\n15 октября, через 59 дней — отключается claude-3-opus" in text
        )
        assert "1 ноября, через 76 дней — новые лимиты Tier 1 в OpenAI API" in text

    def test_quiet_day_without_deadlines_drops_the_heading(self):
        text = telegram.render_quiet_day(quiet_signal(precedents=[]), TODAY)
        assert "Ближайшее" not in text
        assert "Проверено 14 источников" in text

    def test_the_proof_line_survives_a_signal_the_core_actually_writes(self):
        """`stats` is a free-form extension nothing in the core fills.

        A live quiet_day arrives with `stats == {}` and a populated
        `run_summary`, and reading only `stats` left the message with no
        numbers under it — which is what an agent gone silent looks like.
        """
        signal = quiet_signal(
            stats={},
            precedents=[],
            run_summary=RunSummary(
                sources_checked=5,
                sources_empty=["worldmonitor"],
                materials_collected=0,
                materials_filtered=0,
            ),
        )
        text = telegram.render_quiet_day(signal, TODAY)
        assert "Проверено 5 источников." in text
        assert "worldmonitor ответил, но ничего не отдал." in text

    def test_past_deadlines_do_not_reach_the_upcoming_block(self):
        stale = Precedent(
            statement_id="st-old",
            text="отключается claude-2.0",
            source_url=DEPRECATION_URL,
            event_date=date(2026, 5, 1),
            vendor="anthropic",
            change_type=ChangeType.DEPRECATION,
        )
        text = telegram.render_quiet_day(
            quiet_signal(precedents=[stale, *quiet_signal().precedents]), TODAY
        )
        assert "claude-2.0" not in text
        assert "отключается claude-3-opus" in text

    def test_inferred_date_stays_marked_in_the_upcoming_block(self):
        precedent = Precedent(
            statement_id="st-inferred",
            text="отключается claude-3-opus",
            source_url=DEPRECATION_URL,
            event_date=date(2026, 10, 15),
            date_precision=DatePrecision.INFERRED,
            vendor="anthropic",
            change_type=ChangeType.DEPRECATION,
        )
        text = telegram.render_quiet_day(quiet_signal(precedents=[precedent]), TODAY)
        assert (
            "15 октября (год не указан в источнике) — отключается claude-3-opus" in text
        )

    def test_a_run_with_no_digest_items_renders_the_quiet_day(self):
        text = telegram.render([quiet_signal()], TODAY)
        assert text.startswith("Сегодня в вашем стеке ничего не изменилось.")

    def test_empty_run_still_produces_a_message(self):
        text = telegram.render([], TODAY)
        assert text == "Сегодня в вашем стеке ничего не изменилось."


class TestRunFailure:
    def test_failure_reports_itself(self):
        text = telegram.render_run_failure(failure_signal(), TODAY)
        assert text.startswith(
            "Прогон 17 августа не завершился: сбой на стадии обогащения."
        )
        assert "Собрано 34 материала, обработать не удалось." in text
        assert "Последняя удачная сводка — 16 августа." in text
        assert f'<a href="{RUN_LOG}">Лог прогона.</a>' in text
        assert text.endswith("<code>sig-failure-2026-08-17</code>")

    def test_failure_takes_precedence_over_the_digest(self):
        text = telegram.render([*digest_run(), failure_signal()], TODAY)
        assert text.startswith("Прогон 17 августа не завершился")

    def test_failure_without_statistics_still_reads(self):
        text = telegram.render_run_failure(
            failure_signal(stats={}, failure_reason=None), TODAY
        )
        assert text.startswith("Прогон 17 августа не завершился.")


class TestNotificationLine:
    def test_normal_day_carries_the_main_fact(self):
        line = telegram.notification_line(digest_run(), TODAY)
        assert line == "Anthropic отключает claude-3-opus 15 окт"
        assert len(line) <= telegram.NOTIFICATION_CHARS

    def test_quiet_day(self):
        assert (
            telegram.notification_line([quiet_signal()], TODAY)
            == "Сегодня без изменений"
        )

    def test_run_failure(self):
        assert (
            telegram.notification_line([failure_signal()], TODAY)
            == "Прогон не завершился"
        )

    def test_long_headline_is_cut_on_a_word_boundary(self):
        signal = lead_signal(
            headline="Anthropic объявляет об отключении семейства моделей claude-3"
        )
        line = telegram.notification_line([signal], TODAY)
        assert len(line) <= telegram.NOTIFICATION_CHARS
        assert line == "Anthropic объявляет об отключении"

    def test_notification_never_names_the_product_or_the_run(self):
        line = telegram.notification_line(digest_run(), TODAY)
        for forbidden in ("дайджест", "радар", RUN_ID):
            assert forbidden.lower() not in line.lower()


# --------------------------------------------------------------------------
# Markup and escaping
# --------------------------------------------------------------------------


class TestEscaping:
    def test_special_characters_and_cyrillic_survive(self):
        signal = lead_signal(
            headline='Claude 3.5 <Sonnet> & "Opus" (2026) — обновление',
            summary="Ломает вызов tool_use, если параметр <input> пуст & не задан.",
            why_it_matters="в проектах используется связка A&B (v1.2-beta)",
        )
        text = telegram.render_digest([signal], TODAY)
        assert "&lt;Sonnet&gt;" in text
        assert "&amp;" in text
        assert "<Sonnet>" not in text
        assert "обновление" in text
        assert "Ломает вызов tool_use" in text
        assert tags_of(text) <= {"b", "i", "a", "code"}

    def test_url_with_query_and_ampersand_is_escaped_in_the_attribute(self):
        signal = lead_signal(
            primary_url="https://example.com/changelog?tag=v1&from=rss",
            facts=[],
        )
        text = telegram.render_digest([signal], TODAY)
        assert 'href="https://example.com/changelog?tag=v1&amp;from=rss"' in text
        assert tags_of(text) <= {"b", "i", "a", "code"}

    def test_markup_stays_wellformed_for_the_whole_run(self):
        text = telegram.render_digest(digest_run(), TODAY)
        opened = text.count("<a ")
        closed = text.count("</a>")
        assert opened == closed
        assert tags_of(text) <= {"b", "i", "a", "code"}


# --------------------------------------------------------------------------
# Capacity and truncation
# --------------------------------------------------------------------------


class TestCapacity:
    def test_capacity_comes_from_the_tier(self):
        text = telegram.render_digest(digest_run(), TODAY)
        assert "LangChain переписал раздел документации" not in text

    def test_a_low_scoring_lead_still_leads(self):
        # Score is the core's business and never reaches this surface: a lead
        # tier with a low number renders exactly like any other lead.
        signal = lead_signal(score=3)
        text = telegram.render_digest([signal], TODAY)
        assert "<b>Anthropic отключает claude-3-opus</b>" in text

    def test_a_high_scoring_background_item_stays_out(self):
        background = make_signal(
            signal_id="sig-background",
            headline="Фоновая запись",
            score=99,
            rank=2,
            tier=Tier.BACKGROUND,
        )
        text = telegram.render_digest([lead_signal(), background], TODAY)
        assert "Фоновая запись" not in text

    def test_no_more_than_five_items(self):
        many = [lead_signal()]
        for index in range(2, 12):
            many.append(
                make_signal(
                    signal_id=f"sig-{index}",
                    headline=f"Позиция номер {index}",
                    summary="Короткое описание изменения.",
                    rank=index,
                    tier=Tier.STANDARD,
                )
            )
        text = telegram.render_digest(many, TODAY)
        assert "Позиция номер 5" in text
        assert "Позиция номер 6" not in text

    def test_rank_one_leads_whatever_the_input_order(self):
        run = digest_run()
        shuffled = [run[3], run[1], run[0], run[2]]
        text = telegram.render_digest(shuffled, TODAY)
        assert text.startswith("<b>Anthropic отключает claude-3-opus</b>")


class TestTruncation:
    def test_long_message_fits_and_keeps_the_date_and_the_link(self):
        sentences = " ".join(
            f"Подробность номер {i} про поведение модели после отключения."
            for i in range(200)
        )
        run = digest_run()
        run[0] = lead_signal(summary=sentences)
        text = telegram.render_digest(run, TODAY)

        assert len(text) <= telegram.MAX_MESSAGE_CHARS
        assert "15 октября, через 59 дней." in text
        assert DEPRECATION_URL in text
        assert text.endswith("<code>sig-anthropic-opus-sunset</code>")

    def test_truncation_lands_on_a_sentence_boundary(self):
        sentences = " ".join(
            f"Подробность номер {i} про поведение модели после отключения."
            for i in range(200)
        )
        text = telegram.render_digest([lead_signal(summary=sentences)], TODAY)
        paragraph = text.split("\n\n")[1]
        assert paragraph.endswith("после отключения.")

    def test_summary_without_sentence_boundaries_is_dropped_whole(self):
        wall = "слово " * 2000
        text = telegram.render_digest([lead_signal(summary=wall)], TODAY)
        assert len(text) <= telegram.MAX_MESSAGE_CHARS
        assert "15 октября, через 59 дней." in text
        assert DEPRECATION_URL in text

    def test_giant_headline_is_cut_on_a_word_boundary(self):
        text = telegram.render_digest(
            [lead_signal(headline="Отключение " * 800, summary="")], TODAY
        )
        assert len(text) <= telegram.MAX_MESSAGE_CHARS
        assert "15 октября, через 59 дней." in text
        assert DEPRECATION_URL in text


# --------------------------------------------------------------------------
# Architecture: the surface is not allowed to know the pipeline exists
# --------------------------------------------------------------------------


FORBIDDEN_MODULES = {
    "radar.enrich",
    "radar.filter",
    "radar.scoring",
    "radar.retrieval",
    "radar.trends",
    "radar.collect",
    "radar.cluster",
    "radar.delta",
    "radar.llm",
    "radar.llm_cli",
    "radar.fetch",
    "radar.backfill",
    "radar.normalize",
    "radar.config",
    "radar.supervisor",
    "radar.journal",
    "radar.runlog",
    "radar.scout",
    "radar.cache",
    "radar.adapters",
    "radar.contracts",
    "radar.assertions",
}
ALLOWED_RADAR_MODULES = {"radar.models", "radar.db"}


def imported_modules() -> set[str]:
    tree = ast.parse(Path(telegram.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


class TestArchitecture:
    def test_no_pipeline_stage_is_imported(self):
        assert not imported_modules() & FORBIDDEN_MODULES

    def test_only_the_signal_contract_and_the_store_are_imported(self):
        radar_imports = {m for m in imported_modules() if m.split(".")[0] == "radar"}
        assert radar_imports <= ALLOWED_RADAR_MODULES

    def test_everything_else_is_the_standard_library(self):
        outside = {
            module.split(".")[0]
            for module in imported_modules()
            if module.split(".")[0] != "radar"
        }
        assert outside <= sys.stdlib_module_names

    def test_the_surface_never_reads_the_score(self):
        # Capacity is expressed in tiers. An attribute access to `score` would
        # mean a threshold is back in the surface (SUR-2).
        tree = ast.parse(Path(telegram.__file__).read_text(encoding="utf-8"))
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert not attributes & {"score", "score_rationale", "duplicates_count"}

    def test_the_store_is_opened_read_only(self, tmp_path):
        db_path = tmp_path / "radar.db"
        conn = init_db(db_path)
        publish_signals(conn, RUN_ID, digest_run())
        conn.close()

        signals = telegram.load_signals(db_path)
        assert [s.signal_id for s in signals][0] == "sig-anthropic-opus-sunset"
        text = telegram.render_digest(signals, TODAY)
        assert "<b>Anthropic отключает claude-3-opus</b>" in text


# --------------------------------------------------------------------------
# Delivery. No socket is ever opened here.
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")


@pytest.fixture
def no_network(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the surface must not open a socket in tests")

    monkeypatch.setattr(telegram, "urlopen", explode)


class TestDelivery:
    def test_send_posts_to_the_bot_api(self, monkeypatch, credentials):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeResponse({"ok": True, "result": {"message_id": 42}})

        monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
        result = telegram.send("<b>текст</b>")

        assert result.ok is True
        assert result.message_id == 42
        assert (
            captured["url"]
            == "https://api.telegram.org/bot123456:test-token/sendMessage"
        )
        assert captured["payload"]["chat_id"] == "-1001234567890"
        assert captured["payload"]["parse_mode"] == "HTML"
        assert captured["payload"]["text"] == "<b>текст</b>"

    def test_missing_token_returns_a_result_and_opens_nothing(
        self, monkeypatch, no_network
    ):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
        result = telegram.send("текст")

        assert result.ok is False
        assert "TELEGRAM_BOT_TOKEN" in (result.error or "")

    def test_missing_chat_id_is_reported_too(self, monkeypatch, no_network):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        result = telegram.send("текст")

        assert result.ok is False
        assert "TELEGRAM_CHAT_ID" in (result.error or "")

    def test_network_failure_is_returned_not_raised(self, monkeypatch, credentials):
        from urllib.error import URLError

        def fake_urlopen(request, timeout=None):
            raise URLError("connection refused")

        monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
        result = telegram.send("текст")

        assert result.ok is False
        assert "connection refused" in (result.error or "")

    def test_http_error_is_returned_not_raised(self, monkeypatch, credentials):
        from io import BytesIO
        from urllib.error import HTTPError

        def fake_urlopen(request, timeout=None):
            raise HTTPError(
                request.full_url, 429, "Too Many Requests", {}, BytesIO(b"retry later")
            )

        monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
        result = telegram.send("текст")

        assert result.ok is False
        assert result.status == 429

    def test_bot_api_rejection_is_returned(self, monkeypatch, credentials):
        def fake_urlopen(request, timeout=None):
            return _FakeResponse({"ok": False, "description": "chat not found"})

        monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
        result = telegram.send("текст")

        assert result.ok is False
        assert result.error == "chat not found"

    def test_the_lock_screen_line_is_produced_on_the_delivery_path(
        self, monkeypatch, credentials
    ):
        """voice.md section 1 wants «Прогон не завершился» on a failed run.

        The message itself opens with section 6, which names the day and the
        stage. Building the preview out of the head of the message therefore
        gets the failed run wrong, and the line has to be built where the
        message is sent, not only in a test.
        """
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["payload"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"ok": True, "result": {"message_id": 11}})

        monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
        result = telegram.send_digest([failure_signal()], TODAY)

        assert result.ok is True
        assert result.notification == "Прогон не завершился"
        assert len(result.notification) <= telegram.NOTIFICATION_CHARS
        # The message keeps its own opening, which is not the preview.
        assert sent["payload"]["text"].startswith("Прогон 17 августа не завершился")

    def test_an_ordinary_day_carries_the_main_fact_to_the_lock_screen(
        self, monkeypatch, credentials
    ):
        def fake_urlopen(request, timeout=None):
            return _FakeResponse({"ok": True, "result": {"message_id": 12}})

        monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
        result = telegram.send_digest(digest_run(), TODAY)

        assert result.notification == "Anthropic отключает claude-3-opus 15 окт"

    def test_a_refused_send_still_reports_what_the_reader_would_have_seen(
        self, monkeypatch, no_network
    ):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        result = telegram.send_digest([quiet_signal()], TODAY)

        assert result.ok is False
        assert result.notification == "Сегодня без изменений"

    def test_quiet_day_is_delivered_like_any_other_day(self, monkeypatch, credentials):
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["payload"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"ok": True, "result": {"message_id": 7}})

        monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
        result = telegram.send_digest([quiet_signal()], TODAY)

        assert result.ok is True
        assert sent["payload"]["text"].startswith(
            "Сегодня в вашем стеке ничего не изменилось."
        )

    def test_a_run_with_nothing_at_all_still_sends(self, monkeypatch, credentials):
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["payload"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"ok": True, "result": {"message_id": 8}})

        monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
        result = telegram.send_digest([], TODAY)

        assert result.ok is True
        assert "ничего не изменилось" in sent["payload"]["text"]

    def test_delivery_failure_does_not_stop_the_process(self, monkeypatch, credentials):
        def fake_urlopen(request, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
        result = telegram.send_digest(digest_run(), TODAY)

        assert result.ok is False
        assert result.chars > 0
