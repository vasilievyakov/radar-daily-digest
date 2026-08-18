"""Stage 1 has to tell a quiet source from a broken one.

The product sells one distinction: today nothing happened, versus today the
machine did not look. Until this file existed the collector could not make it
about itself. It compared what came back through a 26-hour window against
`min_expected_items`, a threshold written for a 540-day backfill, and on an
ordinary morning it marked 69 of 77 working sources as faulty.

What is asserted here is that distinction and nothing else:

* a source that answered with its usual contents and had nothing new in the
  window is `quiet`, and never appears in the fault lists a digest renders;
* a source that handed back nothing to filter is still `empty` — the fault
  the status was made for, and the one a client-rendered page produces;
* `min_expected_items` is asked only in backfill, where it means something.

Every test runs the real adapters against canned responses. No collector
internals are patched: what is checked is the status the pipeline writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from radar.adapters.base import SourceConfig
from radar.collect import collect_source, summarize
from radar.fetch import FetchResult
from radar.models import SourceStatus
from radar.publish import build_run_summary
from radar.surfaces import web

NOW = datetime(2026, 8, 18, 6, 0, tzinfo=UTC)
WINDOW = NOW - timedelta(hours=26)


class FakeFetcher:
    """Stands in for radar.fetch.Fetcher: one canned response, no network."""

    def __init__(
        self,
        text: str = "",
        status_code: int = 200,
        error: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.error = error
        self.headers = headers or {}
        self.calls: list[str] = []

    def get(self, url: str, **kwargs) -> FetchResult:
        self.calls.append(url)
        return FetchResult(
            url=url,
            status_code=self.status_code,
            text=self.text,
            headers=self.headers,
            ref="sha256:fixture",
            from_cache=True,
            error=self.error,
        )


def rss(*published: datetime) -> str:
    entries = "\n".join(
        f"<item><title>Release {i}</title>"
        f"<link>https://vendor.test/notes/{i}</link>"
        f"<description>The batch endpoint returns a deprecation header.</description>"
        f"<pubDate>{when.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for i, when in enumerate(published)
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>Vendor release notes</title>{entries}</channel></rss>"
    )


def releases(*published: datetime) -> str:
    return json.dumps(
        [
            {
                "id": i,
                "tag_name": f"v1.{i}",
                "name": f"v1.{i}",
                "body": "The batch endpoint returns a deprecation header.",
                "published_at": when.isoformat().replace("+00:00", "Z"),
                "html_url": f"https://github.com/vendor/tool/releases/tag/v1.{i}",
            }
            for i, when in enumerate(published)
        ]
    )


# A real page with dated headings, all of them old.
DATED_PAGE = """
<html><body><main><article>
<h1>Changelog</h1>
<h3 id="2026-06-11-batch">2026-06-11: batch endpoint deprecation notice</h3>
<p>On June 11, 2026, the batch endpoint began returning a deprecation header.</p>
<h3 id="2026-03-24-sora">2026-03-24: video generation models retired</h3>
<p>On March 24, 2026, the video generation models were retired from the API.</p>
<h3 id="2025-04-28-o1">2025-04-28: o1-preview removed</h3>
<p>On April 28, 2025, o1-preview and o1-mini were removed from the API.</p>
</article></main></body></html>
"""

# HTTP 200, real markup, not one date anywhere: the client-rendered page.
CLIENT_RENDERED_PAGE = """
<html><body>
<nav><a href="/docs">Get Started</a><a href="/docs/agent">Agent</a></nav>
<main>
<h1>Product documentation</h1>
<h2 id="start-here">Start here</h2>
<p>Install the tool and open your first project to see what it can do for you
in an existing codebase.</p>
<h2 id="more-resources">More resources</h2>
<p>Browse the guides, the CLI reference and the SDK documentation.</p>
</main>
</body></html>
"""


def source(**overrides) -> SourceConfig:
    data = dict(
        id="vendor_notes",
        type="rss",
        url="https://vendor.test/feed.xml",
        min_expected_items=10,
    )
    data.update(overrides)
    return SourceConfig(**data)


# -- nothing new is not a fault ----------------------------------------


class TestASourceWithNothingNewIsNotBroken:
    def test_a_feed_whose_entries_all_predate_the_window_is_quiet(self):
        fetcher = FakeFetcher(rss(NOW - timedelta(days=9), NOW - timedelta(days=30)))

        outcome = collect_source(source(), fetcher, mode="live", since=WINDOW)

        assert outcome.status is SourceStatus.QUIET
        assert outcome.count == 0
        # The reason line is an explanation, not an accusation, and it carries
        # the number that proves the source answered.
        assert outcome.error is not None
        assert "всего на источнике 2" in outcome.error

    def test_a_repository_that_shipped_nothing_today_is_quiet(self):
        fetcher = FakeFetcher(
            releases(NOW - timedelta(days=5), NOW - timedelta(days=8))
        )
        repo = source(
            id="gh_vendor_tool",
            type="github_releases",
            url="https://github.com/vendor/tool",
            min_expected_items=3,
        )

        outcome = collect_source(repo, fetcher, mode="live", since=WINDOW)

        assert outcome.status is SourceStatus.QUIET

    def test_a_page_whose_sections_are_all_old_is_quiet(self):
        """The page adapter keeps no diagnostics, so the page is read again.

        The response is already in the HTTP cache: the second read costs a
        parse and no request, which is what the call count asserts.
        """
        fetcher = FakeFetcher(DATED_PAGE)
        page = source(
            id="vendor_changelog",
            type="html_scrape",
            url="https://vendor.test/changelog",
            min_expected_items=20,
        )

        outcome = collect_source(page, fetcher, mode="live", since=WINDOW)

        assert outcome.status is SourceStatus.QUIET
        assert outcome.error is not None and "всего на источнике 3" in outcome.error
        assert fetcher.calls == [page.url, page.url]

    def test_the_quiet_ones_are_never_named_as_faults_in_the_contract(self):
        """SUR-1: surfaces read the names off the signal, so this is the seam
        where 69 working sources used to be reported as broken."""
        fetcher = FakeFetcher(rss(NOW - timedelta(days=9)))
        outcome = collect_source(source(), fetcher, mode="live", since=WINDOW)

        summary = build_run_summary(
            [outcome], materials_collected=0, materials_filtered=0
        )

        assert outcome.status is SourceStatus.QUIET
        assert summary.sources_empty == []
        assert summary.sources_failed == []
        assert summary.sources_checked == 1


# -- and answering with nothing still is --------------------------------


class TestASourceThatAnsweredWithNothingIsStillEmpty:
    def test_a_client_rendered_page_is_empty(self):
        fetcher = FakeFetcher(CLIENT_RENDERED_PAGE)
        page = source(
            id="cursor_docs",
            type="html_scrape",
            url="https://cursor.test/changelog",
            min_expected_items=20,
        )

        outcome = collect_source(page, fetcher, mode="live", since=WINDOW)

        assert outcome.status is SourceStatus.EMPTY
        assert outcome.error == "источник ответил, но не отдал ни одной записи"

    def test_a_dead_url_behind_a_page_source_is_empty(self):
        """learn.microsoft.com answers 404 with a rendered 404 page: the
        adapter swallows it, and only the absence of records shows it."""
        fetcher = FakeFetcher("<html><body>404 - Content not found</body></html>", 404)
        page = source(
            id="azure_model_retirement_schedule",
            type="html_scrape",
            url="https://learn.test/retirements",
            min_expected_items=20,
        )

        outcome = collect_source(page, fetcher, mode="live", since=WINDOW)

        assert outcome.status is SourceStatus.EMPTY

    def test_an_html_page_served_where_a_feed_was_expected_is_empty(self):
        fetcher = FakeFetcher(
            "<!doctype html><html><body><p>Blog</p></body></html>",
            headers={"content-type": "text/html"},
        )

        outcome = collect_source(source(), fetcher, mode="live", since=WINDOW)

        assert outcome.status is SourceStatus.EMPTY
        assert outcome.error is not None and "not a feed" in outcome.error

    def test_a_repository_that_publishes_no_releases_at_all_is_empty(self):
        fetcher = FakeFetcher("[]")
        repo = source(
            id="gh_tags_only",
            type="github_releases",
            url="https://github.com/vendor/tags-only",
            min_expected_items=3,
        )

        outcome = collect_source(repo, fetcher, mode="live", since=WINDOW)

        assert outcome.status is SourceStatus.EMPTY

    def test_a_transport_error_the_adapter_swallowed_is_not_called_quiet(self):
        fetcher = FakeFetcher("", status_code=0, error="ConnectError")

        outcome = collect_source(source(), fetcher, mode="live", since=WINDOW)

        assert outcome.status is SourceStatus.EMPTY
        assert outcome.error == "ConnectError"


# -- the threshold is asked only where it means something ---------------


class TestMinExpectedItemsBelongsToBackfill:
    def test_a_live_source_below_the_threshold_is_ok(self):
        """openai_news_feed brought three items back against a threshold of
        five and was recorded as faulty. Three new materials is a good day."""
        fetcher = FakeFetcher(rss(NOW - timedelta(hours=3)))

        outcome = collect_source(source(), fetcher, mode="live", since=WINDOW)

        assert outcome.status is SourceStatus.OK
        assert outcome.count == 1 < source().min_expected_items

    def test_a_shallow_backfill_still_fails_the_threshold(self):
        fetcher = FakeFetcher(DATED_PAGE)
        page = source(
            id="vendor_changelog",
            type="html_scrape",
            url="https://vendor.test/changelog",
            min_expected_items=10,
            backfill_supported=True,
        )

        outcome = collect_source(page, fetcher, mode="backfill")

        assert outcome.status is SourceStatus.EMPTY
        assert outcome.error is not None and "3 вместо 10" in outcome.error

    def test_a_deep_enough_backfill_passes_it(self):
        fetcher = FakeFetcher(DATED_PAGE)
        page = source(
            id="vendor_changelog",
            type="html_scrape",
            url="https://vendor.test/changelog",
            min_expected_items=3,
            backfill_supported=True,
        )

        outcome = collect_source(page, fetcher, mode="backfill")

        assert outcome.status is SourceStatus.OK


# -- and the difference survives to the screen --------------------------


class TestTheRunLogSaysWhichItWas:
    def test_the_counters_hold_the_three_states_apart(self):
        outcomes = [
            collect_source(
                source(),
                FakeFetcher(rss(NOW - timedelta(hours=2))),
                mode="live",
                since=WINDOW,
            ),
            collect_source(
                source(),
                FakeFetcher(rss(NOW - timedelta(days=9))),
                mode="live",
                since=WINDOW,
            ),
            collect_source(
                source(id="c", type="html_scrape", url="https://c.test/x"),
                FakeFetcher(CLIENT_RENDERED_PAGE),
                mode="live",
                since=WINDOW,
            ),
        ]

        assert summarize(outcomes) == {
            "ok": 1,
            "quiet": 1,
            "empty": 1,
            "failed": 0,
            "items": 1,
        }

    def test_the_page_counts_the_quiet_ones_apart_from_the_broken_one(self):
        run = web.RunLogView(
            run_id="run-1",
            sources=[web.SourceRow(f"gh_{i}", "quiet") for i in range(69)]
            + [web.SourceRow(f"ok_{i}", "ok", items_count=2) for i in range(8)]
            + [web.SourceRow("azure_model_retirement_schedule", "empty")],
        )

        assert web.sources_sentence(run) == (
            "Опрошено 78 источников: 8 сообщили новое, "
            "69 проверены, нового нет, 1 ответил без записей."
        )

    def test_a_quiet_row_is_not_worded_as_a_failure(self):
        row = web.SourceRow("gh_vendor_tool", "quiet")

        assert row.answer == "проверен, нового нет"
        assert "проверен, нового нет" in web.render_run_log(
            web.RunLogView(run_id="run-1", sources=[row]), today=NOW.date()
        )
