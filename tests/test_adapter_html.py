"""Fixtures reproduce the markup the live pages actually serve.

docs.claude.com puts the anchor id on a div inside the heading and renders
model ids as copy-to-clipboard buttons; platform.openai.com puts the id on the
heading itself and prefixes every title with an ISO date; cursor.com answers
200 with a page that contains no date at all. Each shape is a fixture below.
"""

from datetime import date, datetime

import pytest

from radar.adapters.base import SourceConfig
from radar.adapters.html_page import (
    HtmlPageAdapter,
    extract_page_text,
    parse_date_fragment,
)
from radar.assertions import verify_evidence
from radar.fetch import FetchResult
from radar.models import DatePrecision

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

# docs.claude.com/en/docs/about-claude/model-deprecations
ANTHROPIC_PAGE = """
<html><body>
<nav><a href="/">Docs</a><a href="/pricing">March 1, 2020 pricing</a></nav>
<main><article class="prose">
<h1>Model deprecations</h1>
<h2><div class="group" id="model-status">
  <button aria-label="Copy link to clipboard"><span aria-hidden="true"></span></button>
  Model status</div></h2>
<p>The table below shows the current state of every model.</p>
<table>
  <thead><tr>
    <th>API model name</th><th>Current state</th>
    <th>Deprecated</th><th>Tentative retirement date</th>
  </tr></thead>
  <tbody>
    <tr>
      <td><button aria-label="Copy model ID claude-opus-5">claude-opus-5</button></td>
      <td>Active</td><td>N/A</td><td>Not sooner than July 24, 2027</td>
    </tr>
    <tr>
      <td><button>claude-3-7-sonnet-20250219</button></td>
      <td>Retired</td><td>October 28, 2025</td><td>February 19, 2026</td>
    </tr>
  </tbody>
</table>
<h2><div id="deprecation-history">Deprecation history</div></h2>
<h3><div class="group" id="2026-06-05-claude-opus-4-1-model">
  <button aria-label="Copy link to clipboard"><span aria-hidden="true"></span></button>
  2026-06-05: Claude Opus 4.1 model</div></h3>
<aside role="note"><p>This model was retired August 5, 2026.</p></aside>
<p>On June 5, 2026, Anthropic notified developers using Claude Opus 4.1 of its
upcoming retirement on the Claude API.</p>
<table>
  <thead><tr>
    <th>Retirement date</th><th>Deprecated model</th><th>Recommended replacement</th>
  </tr></thead>
  <tbody><tr>
    <td>August 5, 2026</td>
    <td><code>claude-opus-4-1-20250805</code></td>
    <td><code>claude-opus-4-8</code></td>
  </tr></tbody>
</table>
<h3><div class="group" id="2026-02-19-claude-haiku-3-model">
  2026-02-19: Claude Haiku 3 model</div></h3>
<p>On February 19, 2026, Anthropic notified developers using Claude Haiku 3 of
its upcoming retirement.</p>
<table>
  <thead><tr>
    <th>Retirement date</th><th>Deprecated model</th><th>Recommended replacement</th>
  </tr></thead>
  <tbody><tr>
    <td>April 20, 2026</td>
    <td><code>claude-3-haiku-20240307</code></td>
    <td><code>claude-haiku-4-5-20251001</code></td>
  </tr></tbody>
</table>
</article></main>
<footer>Copyright 2026 Anthropic</footer>
</body></html>
"""

# platform.openai.com/docs/deprecations
OPENAI_PAGE = """
<html><body>
<main><article id="mainContent" class="prose">
<h1>Deprecations</h1>
<h2 id="overview">Overview</h2>
<p>As we launch safer and more capable models, we regularly retire older ones.</p>
<h2 id="upcoming-deprecations">Upcoming deprecations</h2>
<h3 id="2026-06-11-gpt-5-and-o3-model-deprecations">2026-06-11: GPT-5 and o3 model deprecations</h3>
<p>On June 11, 2026, we notified developers using older GPT-5 and o3 model
snapshots of their deprecation and removal from the API in three months.</p>
<h3 id="update-to-openais-self-serve-fine-tuning">Update to OpenAI self-serve fine-tuning</h3>
<p>Self-serve fine-tuning of legacy models is no longer offered in the dashboard.</p>
<h3 id="2026-03-24-sora-2-video-generation-models">2026-03-24: Sora 2 video generation models</h3>
<p>On March 24, 2026, we notified developers using Sora 2 of its deprecation.</p>
<h2 id="past-deprecations">Past deprecations</h2>
<h3 id="2025-04-28-o1-preview-and-o1-mini">2025-04-28: o1-preview and o1-mini</h3>
<p>On April 28, 2025, we notified developers of the removal of o1-preview and
o1-mini from the API.</p>
</article></main>
</body></html>
"""

# docs.cursor.com/changelog: 200, real markup, not one date on the page.
CURSOR_PAGE = """
<html><body>
<nav><a href="/docs">Get Started</a><a href="/docs/agent">Agent</a></nav>
<main>
<h1>Cursor Documentation</h1>
<h2 id="start-here">Start here</h2>
<p>Install Cursor and open your first project to see what the agent can do for
you in an existing codebase.</p>
<h2 id="models">Models</h2>
<table><thead><tr><th>Model</th><th>Context</th></tr></thead>
<tbody><tr><td>Composer</td><td>Large context window</td></tr></tbody></table>
<h2 id="more-resources">More resources</h2>
<p>Browse the guides, the CLI reference and the SDK documentation.</p>
</main>
</body></html>
"""

# A changelog that groups entries under a year and drops the year from the
# entry headings, listed newest first across a year boundary.
YEAR_GROUPED_PAGE = """
<html><body>
<main><article>
<h1>Changelog</h1>
<h2 id="y2026">2026</h2>
<h3 id="jan-5">Jan 5</h3>
<p>The rate limit on the batch endpoint doubled for all organizations.</p>
<h2 id="y2025">2025</h2>
<h3 id="dec-14">Dec 14</h3>
<p>The legacy completions endpoint started returning a deprecation header.</p>
</article></main>
</body></html>
"""

# Same shape, but nothing above the entry states a year.
NO_YEAR_PAGE = """
<html><body>
<main><article>
<h1>Changelog</h1>
<h3 id="mar-14">Mar 14</h3>
<p>The CLI now refuses to start when the config file has an unknown key.</p>
<h3 id="feb-2">Feb 2</h3>
<p>Streaming responses gained a keepalive event every fifteen seconds.</p>
</article></main>
</body></html>
"""

MIXED_PRECISION_PAGE = """
<html><body>
<main><article>
<h1>Release notes</h1>
<h3 id="a">2026-05-01</h3>
<p>Prompt caching became generally available on every model in the family.</p>
<h3 id="b">May 2026</h3>
<p>Pricing for the batch tier changed for organizations on annual contracts.</p>
<h3 id="c">2026-03</h3>
<p>The legacy SDK stopped receiving security patches from this month on.</p>
<h3 id="d">February 2, 2026</h3>
<p>A new endpoint for listing organization members entered public beta.</p>
</article></main>
</body></html>
"""


class FakeFetcher:
    """Stands in for radar.fetch.Fetcher: no network, one canned response."""

    def __init__(
        self, text: str = "", status_code: int = 200, error: str | None = None
    ):
        self.text = text
        self.status_code = status_code
        self.error = error
        self.calls: list[str] = []

    def get(self, url: str, **kwargs) -> FetchResult:
        self.calls.append(url)
        return FetchResult(
            url=url,
            status_code=self.status_code,
            text=self.text,
            headers={},
            ref="sha256:fixture",
            from_cache=True,
            error=self.error,
        )


def make_adapter(
    html: str, hint: str | None = "dated_sections", **kwargs
) -> HtmlPageAdapter:
    source = SourceConfig(
        id=kwargs.pop("source_id", "test_source"),
        type="html_scrape",
        url=kwargs.pop("url", "https://example.test/changelog"),
        parser_hint=hint,
        **kwargs,
    )
    return HtmlPageAdapter(source, FakeFetcher(html))


# --------------------------------------------------------------------------
# date parsing
# --------------------------------------------------------------------------


class TestParseDateFragment:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("2026-05-01", date(2026, 5, 1)),
            ("May 1, 2026", date(2026, 5, 1)),
            ("May 1 2026", date(2026, 5, 1)),
            ("1 May 2026", date(2026, 5, 1)),
            ("May 10th, 2024", date(2024, 5, 10)),
            ("2026-06-11: GPT-5 and o3 model deprecations", date(2026, 6, 11)),
            ("Not sooner than July 24, 2027", date(2027, 7, 24)),
        ],
    )
    def test_day_precision(self, text, expected):
        parsed = parse_date_fragment(text)
        assert parsed is not None
        assert parsed.value == expected
        assert parsed.precision is DatePrecision.DAY

    @pytest.mark.parametrize("text", ["May 2026", "2026-05"])
    def test_month_precision(self, text):
        parsed = parse_date_fragment(text)
        assert parsed is not None
        assert parsed.value == date(2026, 5, 1)
        assert parsed.precision is DatePrecision.MONTH

    def test_leftmost_date_wins_over_a_dated_model_name(self):
        parsed = parse_date_fragment("2025-06-10: gpt-4o-realtime-preview-2024-10-01")
        assert parsed is not None and parsed.value == date(2025, 6, 10)

    def test_month_and_day_without_a_year_stay_undated(self):
        parsed = parse_date_fragment("Mar 14")
        assert parsed is not None
        assert parsed.value is None
        assert parsed.year_missing
        assert parsed.month == 3 and parsed.day == 14

    def test_no_date_at_all(self):
        assert parse_date_fragment("Model status") is None
        assert parse_date_fragment("claude-3-5-haiku-20241022") is None


# --------------------------------------------------------------------------
# dated sections
# --------------------------------------------------------------------------


class TestDatedSections:
    def test_one_item_per_dated_heading(self):
        items = make_adapter(OPENAI_PAGE).collect()
        titles = [item.title for item in items]
        assert titles == [
            "2026-06-11: GPT-5 and o3 model deprecations",
            "2026-03-24: Sora 2 video generation models",
            "2025-04-28: o1-preview and o1-mini",
        ]
        assert [item.event_date for item in items] == [
            date(2026, 6, 11),
            date(2026, 3, 24),
            date(2025, 4, 28),
        ]

    def test_body_belongs_to_its_own_section(self):
        items = make_adapter(OPENAI_PAGE).collect()
        first = items[0]
        assert "older GPT-5 and o3 model snapshots" in first.raw_text
        assert "Sora 2" not in first.raw_text
        assert "As we launch safer" not in first.raw_text

    def test_undated_heading_does_not_start_a_section(self):
        items = make_adapter(OPENAI_PAGE).collect()
        assert all("self-serve fine-tuning" not in item.title for item in items)

    def test_anchor_is_taken_from_the_heading(self):
        items = make_adapter(
            OPENAI_PAGE, url="https://platform.openai.com/docs/deprecations"
        ).collect()
        assert items[0].url == (
            "https://platform.openai.com/docs/deprecations"
            "#2026-06-11-gpt-5-and-o3-model-deprecations"
        )

    def test_anchor_is_found_on_a_div_inside_the_heading(self):
        items = make_adapter(ANTHROPIC_PAGE).collect()
        assert items[0].url.endswith("#2026-06-05-claude-opus-4-1-model")

    def test_icon_glyphs_and_copy_buttons_stay_out_of_the_title(self):
        items = make_adapter(ANTHROPIC_PAGE).collect()
        assert items[0].title == "2026-06-05: Claude Opus 4.1 model"

    def test_raw_material_ref_comes_from_the_fetch_result(self):
        items = make_adapter(ANTHROPIC_PAGE).collect()
        assert all(item.raw_material_ref == "sha256:fixture" for item in items)

    def test_navigation_and_footer_are_not_a_section(self):
        items = make_adapter(ANTHROPIC_PAGE).collect()
        assert all("pricing" not in item.title for item in items)
        assert all("Copyright" not in item.raw_text for item in items)

    def test_precision_follows_what_the_page_states(self):
        items = make_adapter(MIXED_PRECISION_PAGE).collect()
        by_title = {item.title: item for item in items}
        assert by_title["2026-05-01"].date_precision is DatePrecision.DAY
        assert by_title["May 2026"].date_precision is DatePrecision.MONTH
        assert by_title["May 2026"].event_date == date(2026, 5, 1)
        assert by_title["2026-03"].date_precision is DatePrecision.MONTH
        assert by_title["February 2, 2026"].event_date == date(2026, 2, 2)

    def test_items_are_newest_first(self):
        items = make_adapter(MIXED_PRECISION_PAGE).collect()
        dates = [item.event_date for item in items]
        assert dates == sorted(dates, reverse=True)

    def test_a_large_page_is_split_instead_of_returned_whole(self):
        body = "".join(
            f'<h3 id="d{day}">August {day}, 2026</h3><p>{"filler text " * 40}</p>'
            for day in range(1, 29)
        )
        items = make_adapter(
            f"<html><body><main><article><h1>Notes</h1>{body}</article></main></body></html>"
        ).collect()
        page = extract_page_text(
            f"<html><body><main><article><h1>Notes</h1>{body}</article></main></body></html>"
        )
        assert len(items) == 28
        assert all(len(item.raw_text) < len(page.text) / 10 for item in items)


# --------------------------------------------------------------------------
# dated table
# --------------------------------------------------------------------------


class TestDatedTable:
    def test_one_item_per_dated_row(self):
        items = make_adapter(ANTHROPIC_PAGE, hint="dated_table").collect()
        assert len(items) == 4
        assert [item.event_date for item in items] == [
            date(2027, 7, 24),
            date(2026, 8, 5),
            date(2026, 4, 20),
            date(2025, 10, 28),
        ]

    def test_announcement_column_wins_over_retirement_column(self):
        items = make_adapter(ANTHROPIC_PAGE, hint="dated_table").collect()
        retired = next(i for i in items if "claude-3-7-sonnet" in i.title)
        # The row states both "October 28, 2025" (Deprecated) and
        # "February 19, 2026" (Tentative retirement date).
        assert retired.event_date == date(2025, 10, 28)

    def test_row_keeps_the_model_id_rendered_as_a_button(self):
        items = make_adapter(ANTHROPIC_PAGE, hint="dated_table").collect()
        assert any("claude-opus-5" in item.raw_text for item in items)

    def test_title_carries_the_enclosing_heading(self):
        items = make_adapter(ANTHROPIC_PAGE, hint="dated_table").collect()
        assert items[0].title == "Model status - claude-opus-5"
        assert items[0].url.endswith("#model-status")

    def test_rows_without_a_date_are_skipped(self):
        items = make_adapter(CURSOR_PAGE, hint="dated_table").collect()
        assert items == []

    def test_hint_falls_back_when_the_hinted_shape_yields_nothing(self):
        # dated_table on a page whose events live in headings still collects.
        items = make_adapter(OPENAI_PAGE, hint="dated_table").collect()
        assert len(items) == 3


# --------------------------------------------------------------------------
# the year is never invented
# --------------------------------------------------------------------------


class TestMissingYear:
    def test_year_comes_from_the_enclosing_heading(self):
        items = make_adapter(YEAR_GROUPED_PAGE).collect()
        by_title = {item.title: item for item in items}
        assert by_title["Jan 5"].event_date == date(2026, 1, 5)
        assert by_title["Jan 5"].date_precision is DatePrecision.DAY
        assert by_title["Jan 5"].extra["year_from_heading"] == "2026"

    def test_a_neighbour_across_the_year_boundary_is_not_the_source(self):
        # "Dec 14" sits under the 2025 heading while the entry above it is
        # January 2026. Borrowing from the neighbour would date it 2026-12-14.
        items = make_adapter(YEAR_GROUPED_PAGE).collect()
        december = next(item for item in items if item.title == "Dec 14")
        assert december.event_date == date(2025, 12, 14)

    def test_no_enclosing_year_leaves_the_date_unresolved(self):
        items = make_adapter(NO_YEAR_PAGE).collect()
        assert len(items) == 2
        for item in items:
            assert item.event_date is None
            assert item.date_precision is DatePrecision.INFERRED
            assert item.extra["date_unresolved"] is True
        assert items[0].extra["date_text"] == "Mar 14"

    def test_grouping_heading_is_not_an_item_of_its_own(self):
        items = make_adapter(YEAR_GROUPED_PAGE).collect()
        assert {item.title for item in items} == {"Jan 5", "Dec 14"}


# --------------------------------------------------------------------------
# raw_text is quotable
# --------------------------------------------------------------------------


class TestRawTextIsVerbatim:
    @pytest.mark.parametrize(
        "html,hint",
        [
            (ANTHROPIC_PAGE, "dated_sections"),
            (ANTHROPIC_PAGE, "dated_table"),
            (OPENAI_PAGE, "dated_sections"),
            (MIXED_PRECISION_PAGE, "dated_sections"),
            (YEAR_GROUPED_PAGE, "dated_sections"),
        ],
    )
    def test_section_text_is_a_substring_of_the_page_text(self, html, hint):
        page = extract_page_text(html)
        for item in make_adapter(html, hint=hint).collect():
            assert item.raw_text in page.text

    def test_a_quote_taken_from_a_section_verifies_against_it(self):
        items = make_adapter(ANTHROPIC_PAGE).collect()
        section = items[0]
        ok, reason = verify_evidence("retired August 5, 2026", section.raw_text)
        assert ok, reason

    def test_a_quote_from_another_section_does_not_verify(self):
        items = make_adapter(ANTHROPIC_PAGE).collect()
        ok, reason = verify_evidence("Claude Haiku 3", items[0].raw_text)
        assert not ok and reason == "evidence_not_in_source"


# --------------------------------------------------------------------------
# windows
# --------------------------------------------------------------------------


class TestSinceAndBackfill:
    def test_since_keeps_only_newer_sections(self):
        items = make_adapter(OPENAI_PAGE).collect(since=datetime(2026, 1, 1))
        assert [item.event_date for item in items] == [
            date(2026, 6, 11),
            date(2026, 3, 24),
        ]

    def test_since_is_inclusive_of_the_day_itself(self):
        items = make_adapter(OPENAI_PAGE).collect(since=datetime(2026, 6, 11, 23, 0))
        assert [item.event_date for item in items] == [date(2026, 6, 11)]

    def test_a_month_precision_section_survives_until_the_month_ends(self):
        items = make_adapter(MIXED_PRECISION_PAGE).collect(since=datetime(2026, 5, 20))
        # "May 2026" can still mean May 31, so it is not older than May 20.
        assert [item.title for item in items] == ["May 2026"]

    def test_an_unresolved_date_is_never_filtered_out(self):
        items = make_adapter(NO_YEAR_PAGE).collect(since=datetime(2026, 8, 1))
        assert len(items) == 2

    def test_backfill_returns_everything_when_no_depth_is_configured(self):
        items = make_adapter(OPENAI_PAGE).backfill()
        assert len(items) == 3

    def test_backfill_stops_at_the_configured_depth(self):
        adapter = make_adapter(
            OPENAI_PAGE, backfill_supported=True, backfill_depth_days=1
        )
        assert adapter.backfill() == []
        assert len(adapter.backfill(depth_days=365 * 50)) == 3


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------


class TestFailuresAreEmptyNotExceptions:
    def test_a_client_rendered_page_yields_nothing(self):
        # HTTP 200, real markup, zero extractable dates: the caller compares
        # this against min_expected_items and records the source as empty.
        assert make_adapter(CURSOR_PAGE).collect() == []

    def test_a_network_error_yields_nothing(self):
        source = SourceConfig(id="s", type="html_scrape", url="https://example.test/x")
        adapter = HtmlPageAdapter(
            source, FakeFetcher("", status_code=0, error="ConnectError")
        )
        assert adapter.collect() == []

    def test_an_http_error_yields_nothing(self):
        source = SourceConfig(id="s", type="html_scrape", url="https://example.test/x")
        adapter = HtmlPageAdapter(source, FakeFetcher("<html></html>", status_code=404))
        assert adapter.collect() == []

    def test_a_raising_fetcher_does_not_escape(self):
        class Exploding:
            def get(self, url, **kwargs):
                raise RuntimeError("boom")

        source = SourceConfig(id="s", type="html_scrape", url="https://example.test/x")
        assert HtmlPageAdapter(source, Exploding()).collect() == []

    def test_empty_body_yields_nothing(self):
        assert make_adapter("").collect() == []
