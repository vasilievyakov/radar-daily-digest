"""Fixtures reproduce the markup the live feeds and channels actually serve.

platform.claude.com tells all 130 of its entries apart by URL fragment alone
and carries the body in `<description>`; cursor.com carries it in
`content:encoded` and puts a one-line teaser in `<description>`;
simonwillison.net is Atom. `docs.claude.com/rss.xml` and
`blog.langchain.com/rss/` answer HTTP 200 with an HTML page and no feed at
all. Telegram nests a second copy of the message text inside the first on
long posts, and a channel without a public preview answers 200 with a 10 KB
page. Each shape is a fixture below.

No test touches the network: `Fetcher` is replaced by a stand-in that serves
these fixtures and records every URL it was asked for.
"""

from datetime import UTC, datetime

import pytest

from radar.adapters.base import SourceConfig
from radar.adapters.rss_feed import RssFeedAdapter, html_to_text
from radar.adapters.telegram_channel import TelegramChannelAdapter, parse_channel
from radar.assertions import verify_evidence
from radar.cache import HttpCache
from radar.fetch import FetchResult
from radar.models import DatePrecision

# --------------------------------------------------------------------------
# feed fixtures
# --------------------------------------------------------------------------

# platform.claude.com/docs/en/release-notes/feed.xml
ANTHROPIC_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>
<title>Claude Platform release notes</title>
<link>https://platform.claude.com/docs/en/release-notes/overview</link>
<description>Updates to the Claude Platform.</description>
<item><title>Claude Platform release notes &#8212; August 11, 2026</title>
<link>https://platform.claude.com/docs/en/release-notes/overview#august-11-2026</link>
<guid isPermaLink="true">https://platform.claude.com/docs/en/release-notes/overview#august-11-2026</guid>
<pubDate>Tue, 11 Aug 2026 00:00:00 GMT</pubDate>
<description>
&lt;ul&gt;
&lt;li&gt;The &lt;a href=&quot;https://platform.claude.com/docs/en/compliance-api&quot;&gt;Compliance API&lt;/a&gt; now returns transcripts of Cowork and Claude Code sessions, in beta for Enterprise organizations.&lt;/li&gt;
&lt;li&gt;&lt;code&gt;claude-opus-4-1&lt;/code&gt; is retired on the Claude API.&lt;/li&gt;
&lt;/ul&gt;
</description></item>
<item><title>Claude Platform release notes &#8212; August 5, 2026</title>
<link>https://platform.claude.com/docs/en/release-notes/overview#august-5-2026</link>
<guid isPermaLink="true">https://platform.claude.com/docs/en/release-notes/overview#august-5-2026</guid>
<pubDate>Wed, 05 Aug 2026 00:00:00 GMT</pubDate>
<description>&lt;p&gt;The batch API limit rises to 200,000 requests.&lt;/p&gt;</description></item>
<item><title>Claude Platform release notes &#8212; May 10, 2024</title>
<link>https://platform.claude.com/docs/en/release-notes/overview#may-10-2024</link>
<guid isPermaLink="true">https://platform.claude.com/docs/en/release-notes/overview#may-10-2024</guid>
<pubDate>Fri, 10 May 2024 00:00:00 GMT</pubDate>
<description>&lt;p&gt;Tool use is generally available.&lt;/p&gt;</description></item>
</channel></rss>
"""

# cursor.com/changelog/rss.xml: teaser in description, post in content:encoded.
CURSOR_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Cursor Changelog</title>
    <link>https://cursor.com/changelog</link>
    <item>
      <title>Origin Code Hosting</title>
      <link>https://cursor.com/changelog/origin-code-hosting</link>
      <guid isPermaLink="true">https://cursor.com/changelog/origin-code-hosting</guid>
      <pubDate>Mon, 17 Aug 2026 00:00:00 GMT</pubDate>
      <description>Cursor can now host your code.</description>
      <content:encoded><![CDATA[<p>Cursor can now host your code.</p>
<p>Origin begins rolling out today in early beta on all paid plans.</p>
<h2>Origin Repos</h2>
<p>The new <b>Codebase</b> tab is home for Origin repos.</p>]]></content:encoded>
    </item>
  </channel>
</rss>
"""

# An item carrying all three body elements at once, to pin the order down.
THREE_BODIES_FEED = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>t</title><link>https://example.com</link>
<item><title>All three</title><link>https://example.com/a</link>
  <pubDate>Mon, 17 Aug 2026 00:00:00 GMT</pubDate>
  <description>SUMMARY BODY</description>
  <content>PLAIN CONTENT BODY</content>
  <content:encoded><![CDATA[<p>ENCODED BODY</p>]]></content:encoded>
</item>
<item><title>Content and summary</title><link>https://example.com/b</link>
  <pubDate>Mon, 17 Aug 2026 00:00:00 GMT</pubDate>
  <description>SUMMARY BODY B</description>
  <content>PLAIN CONTENT BODY B</content>
</item>
<item><title>Summary only</title><link>https://example.com/c</link>
  <pubDate>Mon, 17 Aug 2026 00:00:00 GMT</pubDate>
  <description>SUMMARY BODY C</description>
</item>
</channel></rss>
"""

# simonwillison.net/tags/llms.atom
ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xml:lang="en-us" xmlns="http://www.w3.org/2005/Atom">
<title>Simon Willison's Weblog: llms</title>
<link href="http://simonwillison.net/" rel="alternate"/>
<id>http://simonwillison.net/</id>
<updated>2026-08-16T22:00:39+00:00</updated>
<entry><title>Qwen 3.8 27B is excellent</title>
<link href="https://simonwillison.net/2026/Aug/16/qwen-38-27b/" rel="alternate"/>
<published>2026-08-16T22:00:39+00:00</published>
<updated>2026-08-16T22:00:39+00:00</updated>
<id>https://simonwillison.net/2026/Aug/16/qwen-38-27b/</id>
<summary type="html">&lt;p&gt;Friday's big release was &lt;a href="https://huggingface.co/Qwen/Qwen3.8-27B"&gt;Qwen 3.8 27B&lt;/a&gt;, an Apache 2 licensed 27B parameter vision-capable LLM.&lt;/p&gt;</summary>
</entry>
<entry><title>Updated only</title>
<link href="https://simonwillison.net/2026/Aug/12/updated-only/" rel="alternate"/>
<updated>2026-08-12T09:30:00+00:00</updated>
<id>https://simonwillison.net/2026/Aug/12/updated-only/</id>
<content type="html">&lt;p&gt;An entry that states no publication date.&lt;/p&gt;</content>
</entry>
</feed>
"""

UNDATED_FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title><link>https://example.com</link>
<item><title>Dated</title><link>https://example.com/dated</link>
  <pubDate>Mon, 17 Aug 2026 00:00:00 GMT</pubDate>
  <description>&lt;p&gt;Has a date.&lt;/p&gt;</description></item>
<item><title>No date at all</title><link>https://example.com/undated</link>
  <description>&lt;p&gt;States no date whatsoever.&lt;/p&gt;</description></item>
</channel></rss>
"""

DEDUP_FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title><link>https://example.com</link>
<item><title>First</title>
  <link>https://cursor.com/changelog/origin-code-hosting</link>
  <pubDate>Mon, 17 Aug 2026 00:00:00 GMT</pubDate>
  <description>&lt;p&gt;First copy.&lt;/p&gt;</description></item>
<item><title>Same post, syndicated again</title>
  <link>https://www.cursor.com/changelog/origin-code-hosting/?utm_source=rss</link>
  <pubDate>Mon, 17 Aug 2026 00:00:00 GMT</pubDate>
  <description>&lt;p&gt;Second copy.&lt;/p&gt;</description></item>
<item><title>Release notes August 11</title>
  <link>https://platform.claude.com/docs/en/release-notes/overview#august-11-2026</link>
  <pubDate>Tue, 11 Aug 2026 00:00:00 GMT</pubDate>
  <description>&lt;p&gt;August 11.&lt;/p&gt;</description></item>
<item><title>Release notes August 5</title>
  <link>https://platform.claude.com/docs/en/release-notes/overview#august-5-2026</link>
  <pubDate>Wed, 05 Aug 2026 00:00:00 GMT</pubDate>
  <description>&lt;p&gt;August 5.&lt;/p&gt;</description></item>
</channel></rss>
"""

EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Nothing here yet</title>
<link>https://example.com/blog</link>
<description>An empty but well formed feed.</description>
</channel></rss>
"""

# docs.claude.com/rss.xml and blog.langchain.com/rss/ both answer like this.
HTML_INSTEAD_OF_FEED = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><title>Claude Docs</title>
<link rel="alternate" type="application/rss+xml" href="/release-notes/feed.xml"/>
</head><body><div id="__next"><nav>Docs</nav>
<main><h1>Welcome to Claude</h1><p>Build with Claude.</p></main>
</div><script>window.__DATA__={"a":1<2}</script></body></html>
"""

# --------------------------------------------------------------------------
# telegram fixtures
# --------------------------------------------------------------------------

# t.me/s/data_secrets. Post 9705 shows the real quirk: Telegram wraps a long
# message in a second element of the same class, so the text appears twice.
TG_TEXT_9705 = (
    '<div class="tgme_widget_message_text js-message_text" dir="auto">'
    '<div class="tgme_widget_message_text js-message_text" dir="auto">'
    "<b>Google выпустили Gemini 3.7 "
    "Flash</b><br/><br/>"
    "Теперь это сама"
    "я сильная модел"
    "ь Google. <br/><br/>"
    "До конца года "
    "<b>модель</b> будет "
    "стоить вдвое "
    "меньше: $0.75/1M input и $3.75/1M output."
    '<br/><br/><a href="https://blog.google/gemini-3-7-flash/" rel="noopener" '
    'target="_blank">https://blog.google/gemini-3-7-flash/</a>'
    "</div></div>"
)


def tg_message(post: str, stamp: str, body: str = "", views: str = "19.4K") -> str:
    """One message node the way t.me/s/ renders it."""
    return f"""<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message text_not_supported_wrap js-widget_message"
     data-post="{post}" data-view="eyJjIjotMX0=">
  <div class="tgme_widget_message_bubble">
    <div class="tgme_widget_message_author accent_color">
      <a class="tgme_widget_message_owner_name" href="https://t.me/{post.split("/")[0]}">
      <span dir="auto">Data Secrets</span></a></div>
    {body}
    <div class="tgme_widget_message_footer compact js-message_footer">
      <div class="tgme_widget_message_info short js-message_info">
        <span class="tgme_widget_message_views">{views}</span>
        <a class="tgme_widget_message_date" href="https://t.me/{post}">
          <time datetime="{stamp}" class="time">17:48</time></a>
      </div>
    </div>
  </div>
</div></div>"""


def tg_page(messages: str, before: int | None = None, after: int | None = None) -> str:
    links = ""
    if before is not None:
        links += (
            '<div class="tgme_widget_message_centered js-messages_more_wrap">'
            f'<a href="/s/data_secrets?before={before}" '
            f'class="tme_messages_more js-messages_more" data-before="{before}"></a>'
            "</div>"
        )
    if after is not None:
        links += (
            '<div class="tgme_widget_message_centered js-messages_more_wrap">'
            f'<a href="/s/data_secrets?after={after}" '
            f'class="tme_messages_more js-messages_more" data-after="{after}"></a>'
            "</div>"
        )
    return f"""<!DOCTYPE html><html><head><title>Data Secrets</title></head>
<body class="tgme_body_wrap"><div class="tgme_page_wrap">
<main class="tgme_main"><div class="tgme_channel_history js-message_history">
{links}{messages}
</div></main></div></body></html>"""


TG_MEDIA_ONLY = (
    '<div class="media_supported_cont">'
    '<a class="tgme_widget_message_photo_wrap" '
    'href="https://t.me/data_secrets/9706" '
    "style=\"background-image:url('https://cdn4.telesco.pe/file/x.jpg')\">"
    '<div class="tgme_widget_message_photo" style="padding-top:56.25%"></div>'
    "</a></div>"
)

TG_PLAIN = (
    '<div class="tgme_widget_message_text js-message_text" dir="auto">'
    "OpenAI подняли лимит"
    "ы Tier 4 до 10M токенов "
    "в минуту.</div>"
)

TG_PAGE_LATEST = tg_page(
    tg_message("data_secrets/9705", "2026-08-13T17:48:12+00:00", TG_TEXT_9705)
    + tg_message("data_secrets/9706", "2026-08-14T06:46:03+00:00", TG_MEDIA_ONLY)
    + tg_message("data_secrets/9707", "2026-08-17T09:00:00+00:00", TG_PLAIN),
    before=9705,
)

TG_PAGE_OLDER = tg_page(
    tg_message("data_secrets/9700", "2026-07-09T15:09:11+00:00", TG_PLAIN)
    + tg_message("data_secrets/9701", "2026-07-10T11:00:00+00:00", TG_PLAIN),
    before=9700,
    after=9705,
)

TG_PAGE_OLDEST = tg_page(
    tg_message("data_secrets/9698", "2026-06-01T10:00:00+00:00", TG_PLAIN),
    after=9700,
)

# t.me/s/openai: not a channel, so there is no history block at all.
TG_NO_PREVIEW = """<!DOCTYPE html><html><head><title>Telegram: View @openai</title>
</head><body class="no_transition"><div class="tgme_page_wrap">
<div class="tgme_body_wrap"><div class="tgme_page">
<div class="tgme_page_icon"></div>
<div class="tgme_page_description">If you have <a href="//telegram.org/">Telegram</a>,
you can view posts by <b>@openai</b> right away.</div>
<div class="tgme_page_action"><a class="tgme_action_button_new"
href="tg://resolve?domain=openai">View in Telegram</a></div>
</div></div></div></body></html>
"""

# A channel whose preview renders but holds no posts.
TG_EMPTY_HISTORY = tg_page("")


# --------------------------------------------------------------------------
# fetcher stand-in
# --------------------------------------------------------------------------


class FakeFetcher:
    """Serves fixtures by URL and records every URL it was asked for.

    Its presence is half the test: an adapter that let feedparser or requests
    reach the network would never show up here.
    """

    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url, headers=None, force=False, cache_key_extra=None):
        self.calls.append(url)
        page = self.pages.get(url)
        if page is None:
            return FetchResult(
                url=url,
                status_code=404,
                text="",
                headers={},
                ref=HttpCache.key_for(url),
                from_cache=False,
            )
        return FetchResult(
            url=url,
            status_code=page.get("status", 200),
            text=page.get("text", ""),
            headers=page.get("headers", {"content-type": "application/rss+xml"}),
            ref=HttpCache.key_for(url),
            from_cache=False,
            error=page.get("error"),
        )


FEED_URL = "https://example.test/feed.xml"
TG_URL = "https://t.me/s/data_secrets"


def rss_adapter(text: str, *, headers: dict | None = None, **kwargs):
    fetcher = FakeFetcher(
        {
            FEED_URL: {
                "text": text,
                "headers": headers or {"content-type": "application/rss+xml"},
            }
        }
    )
    source = SourceConfig(id="feed_under_test", type="rss", url=FEED_URL, **kwargs)
    return RssFeedAdapter(source, fetcher), fetcher


def tg_adapter(pages: dict[str, str], *, url: str = TG_URL, **kwargs):
    fetcher = FakeFetcher(
        {
            page_url: {"text": body, "headers": {"content-type": "text/html"}}
            for page_url, body in pages.items()
        }
    )
    source = SourceConfig(
        id="tg_under_test", type="telegram_channel", url=url, **kwargs
    )
    return TelegramChannelAdapter(source, fetcher), fetcher


# --------------------------------------------------------------------------
# rss: parsing
# --------------------------------------------------------------------------


def test_rss20_entries_parsed_newest_first():
    adapter, fetcher = rss_adapter(ANTHROPIC_FEED)
    items = adapter.collect()

    assert [item.event_date.isoformat() for item in items] == [
        "2026-08-11",
        "2026-08-05",
        "2024-05-10",
    ]
    assert items[0].published_at == datetime(2026, 8, 11, tzinfo=UTC)
    assert items[0].date_precision is DatePrecision.DAY
    assert items[0].title == "Claude Platform release notes — August 11, 2026"
    assert items[0].url.endswith("#august-11-2026")
    assert adapter.extra["entries_seen"] == 3
    assert adapter.extra["feed_version"] == "rss20"
    # One request, through the Fetcher, and nowhere else.
    assert fetcher.calls == [FEED_URL]
    assert items[0].raw_material_ref == HttpCache.key_for(FEED_URL)


def test_atom_entries_and_updated_fallback():
    adapter, _ = rss_adapter(ATOM_FEED)
    items = adapter.collect()

    assert adapter.extra["feed_version"] == "atom10"
    assert len(items) == 2
    assert items[0].url == "https://simonwillison.net/2026/Aug/16/qwen-38-27b/"
    assert items[0].published_at == datetime(2026, 8, 16, 22, 0, 39, tzinfo=UTC)
    assert "date_from" not in items[0].extra
    # Second entry states only <updated>, and says so.
    assert items[1].published_at == datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
    assert items[1].extra["date_from"] == "updated"


def test_body_prefers_content_encoded_then_content_then_summary():
    adapter, _ = rss_adapter(THREE_BODIES_FEED)
    by_title = {item.title: item for item in adapter.collect()}

    assert by_title["All three"].raw_text == "ENCODED BODY"
    assert by_title["Content and summary"].raw_text == "PLAIN CONTENT BODY B"
    assert by_title["Summary only"].raw_text == "SUMMARY BODY C"
    assert by_title["All three"].extra["body_field"] == "content"
    assert by_title["Summary only"].extra["body_field"] == "summary"


def test_full_post_wins_over_the_teaser_in_description():
    adapter, _ = rss_adapter(CURSOR_FEED)
    (item,) = adapter.collect()

    # <description> holds only the first sentence; content:encoded holds the post.
    assert "Origin begins rolling out today" in item.raw_text
    assert "Origin Repos" in item.raw_text
    assert item.extra["body_field"] == "content"


def test_entry_without_a_date_is_kept_and_marked():
    adapter, _ = rss_adapter(UNDATED_FEED)
    items = adapter.collect()

    undated = [item for item in items if item.title == "No date at all"]
    assert len(undated) == 1
    assert undated[0].published_at is None
    assert undated[0].event_date is None
    assert undated[0].date_precision is DatePrecision.INFERRED
    assert undated[0].extra["date_missing"] is True
    assert adapter.extra["entries_without_date"] == 1
    # Undated entries sort last rather than being dropped.
    assert items[-1].title == "No date at all"


def test_raw_text_carries_no_markup():
    adapter, _ = rss_adapter(ANTHROPIC_FEED)
    item = adapter.collect()[0]

    for marker in ("<ul>", "<li>", "<a ", "<code>", "&lt;", "&quot;"):
        assert marker not in item.raw_text
    # The list item reads as one sentence, not as glued-together fragments.
    assert "returns transcripts of Cowork and Claude Code sessions" in item.raw_text


def test_html_to_text_keeps_blocks_apart_and_inline_runs_together():
    text = html_to_text("<p>First block.</p><p>Second block.</p>")
    assert text == "First block.\nSecond block."
    assert html_to_text("<p>The <b>Codebase</b> tab</p>") == "The Codebase tab"


# --------------------------------------------------------------------------
# rss: filtering, dedup, failure modes
# --------------------------------------------------------------------------


def test_collect_filters_by_since_and_backfill_does_not():
    adapter, _ = rss_adapter(ANTHROPIC_FEED)
    since = datetime(2026, 8, 6, tzinfo=UTC)

    fresh = adapter.collect(since)
    assert [item.event_date.isoformat() for item in fresh] == ["2026-08-11"]
    # backfill returns the whole window the feed handed over.
    assert len(adapter.backfill()) == 3


def test_since_without_a_timezone_is_read_as_utc():
    adapter, _ = rss_adapter(ANTHROPIC_FEED)
    naive = datetime(2026, 8, 6)
    assert len(adapter.collect(naive)) == 1


def test_undated_entry_survives_the_since_filter():
    adapter, _ = rss_adapter(UNDATED_FEED)
    items = adapter.collect(datetime(2026, 8, 17, 12, tzinfo=UTC))
    assert [item.title for item in items] == ["No date at all"]


def test_backfill_depth_trims_the_window():
    adapter, _ = rss_adapter(
        ANTHROPIC_FEED, backfill_supported=True, backfill_depth_days=3650
    )
    assert len(adapter.backfill()) == 3
    assert len(adapter.backfill(depth_days=0)) == 3


def test_dedup_is_canonical_but_keeps_the_fragment():
    adapter, _ = rss_adapter(DEDUP_FEED)
    items = adapter.collect()

    urls = {item.url for item in items}
    # www, the trailing slash and utm_source are the same post.
    assert len(items) == 3
    assert adapter.extra["duplicates_dropped"] == 1
    assert "https://cursor.com/changelog/origin-code-hosting" in urls
    # Two entries on one page, told apart by fragment alone, stay two.
    assert sum(1 for url in urls if "release-notes/overview#" in url) == 2


def test_html_page_served_instead_of_a_feed():
    adapter, _ = rss_adapter(
        HTML_INSTEAD_OF_FEED, headers={"content-type": "text/html; charset=utf-8"}
    )
    assert adapter.collect() == []
    assert adapter.extra["not_a_feed"] is True
    assert adapter.extra["empty"] is True
    assert adapter.extra["entries_seen"] == 0
    assert "HTML page" in adapter.extra["error"]


def test_html_page_recognised_without_a_helpful_content_type():
    adapter, _ = rss_adapter(
        HTML_INSTEAD_OF_FEED, headers={"content-type": "application/octet-stream"}
    )
    assert adapter.collect() == []
    assert adapter.extra["not_a_feed"] is True


def test_valid_feed_with_zero_entries_is_empty_not_broken():
    adapter, _ = rss_adapter(EMPTY_FEED)
    assert adapter.collect() == []
    assert adapter.extra["empty"] is True
    assert adapter.extra["entries_seen"] == 0
    assert "not_a_feed" not in adapter.extra
    assert adapter.extra["error"] == "HTTP 200 with zero entries"


def test_http_failure_is_recorded_not_raised():
    fetcher = FakeFetcher({})
    source = SourceConfig(id="gone", type="rss", url=FEED_URL)
    adapter = RssFeedAdapter(source, fetcher)

    assert adapter.collect() == []
    assert adapter.extra["http_status"] == 404
    assert "404" in adapter.extra["error"]


def test_response_that_is_not_markup_never_reaches_feedparser():
    # feedparser fetches anything that parses as a URL; a body like this one
    # must not become a second, uncached request.
    adapter, fetcher = rss_adapter("https://example.test/somewhere-else.xml")
    assert adapter.collect() == []
    assert adapter.extra["error"] == "response body is not markup"
    assert fetcher.calls == [FEED_URL]


# --------------------------------------------------------------------------
# rss: evidence
# --------------------------------------------------------------------------


def test_quote_from_raw_text_verifies_against_it():
    adapter, _ = rss_adapter(ANTHROPIC_FEED)
    item = adapter.collect()[0]

    quote = "now returns transcripts of Cowork and Claude Code sessions"
    assert quote in item.raw_text
    assert verify_evidence(quote, item.raw_text) == (True, "")


def test_quote_spanning_an_inline_tag_verifies():
    adapter, _ = rss_adapter(CURSOR_FEED)
    (item,) = adapter.collect()

    # In the feed this reads `The new <b>Codebase</b> tab`.
    assert verify_evidence("The new Codebase tab is home", item.raw_text) == (True, "")


def test_a_phrase_the_feed_never_stated_is_rejected():
    adapter, _ = rss_adapter(ANTHROPIC_FEED)
    item = adapter.collect()[0]
    ok, reason = verify_evidence("the Compliance API is deprecated", item.raw_text)
    assert (ok, reason) == (False, "evidence_not_in_source")


# --------------------------------------------------------------------------
# telegram
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://t.me/s/tsingular", "tsingular"),
        ("https://t.me/tsingular", "tsingular"),
        ("t.me/s/pwnai", "pwnai"),
        ("https://telegram.me/s/data_secrets", "data_secrets"),
        ("https://t.me/s/data_secrets?before=9705", "data_secrets"),
        ("https://example.com/s/nope", None),
        ("https://t.me/", None),
        ("", None),
    ],
)
def test_parse_channel(url, expected):
    assert parse_channel(url) == expected


def test_posts_parsed_from_the_web_preview():
    adapter, fetcher = tg_adapter({TG_URL: TG_PAGE_LATEST})
    items = adapter.collect()

    assert fetcher.calls == [TG_URL]
    assert [item.extra["message_id"] for item in items] == [9707, 9705]
    newest = items[0]
    assert newest.url == "https://t.me/data_secrets/9707"
    assert newest.published_at == datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    assert newest.date_precision is DatePrecision.DAY
    assert newest.extra["channel"] == "data_secrets"
    assert newest.extra["views"] == "19.4K"
    assert newest.raw_material_ref == HttpCache.key_for(TG_URL)
    assert adapter.extra["posts_seen"] == 3


def test_post_text_is_plain_and_not_doubled():
    adapter, _ = tg_adapter({TG_URL: TG_PAGE_LATEST})
    post = next(i for i in adapter.collect() if i.extra["message_id"] == 9705)

    # Telegram nests a second copy of the text inside the first.
    assert post.raw_text.count("Gemini 3.7 Flash") == 1
    assert "<br" not in post.raw_text and "<b>" not in post.raw_text
    assert post.title.startswith("Google выпусти")


def test_media_only_post_is_skipped_and_counted():
    adapter, _ = tg_adapter({TG_URL: TG_PAGE_LATEST})
    items = adapter.collect()

    assert 9706 not in {item.extra["message_id"] for item in items}
    assert adapter.extra["posts_without_text"] == 1


def test_every_post_is_marked_as_needing_corroboration():
    adapter, _ = tg_adapter({TG_URL: TG_PAGE_LATEST})
    items = adapter.collect()

    assert items
    # SRC-2: a priority-5 material can never stand alone behind a fact.
    assert all(item.extra["requires_corroboration"] is True for item in items)


def test_collect_filters_posts_by_since():
    adapter, _ = tg_adapter({TG_URL: TG_PAGE_LATEST})
    items = adapter.collect(datetime(2026, 8, 14, tzinfo=UTC))
    assert [item.extra["message_id"] for item in items] == [9707]


def test_quote_from_a_post_verifies_against_its_raw_text():
    adapter, _ = tg_adapter({TG_URL: TG_PAGE_LATEST})
    post = next(i for i in adapter.collect() if i.extra["message_id"] == 9705)

    # Reads `<b>модель</b> будет стоить` in the markup and has to survive it.
    quote = "модель будет стоить вдвое меньше: $0.75/1M input и $3.75/1M output"
    assert verify_evidence(quote, post.raw_text) == (True, "")


def test_channel_without_a_public_preview():
    adapter, _ = tg_adapter(
        {"https://t.me/s/openai": TG_NO_PREVIEW}, url="https://t.me/s/openai"
    )
    assert adapter.collect() == []
    assert adapter.extra["empty"] is True
    assert adapter.extra["no_public_preview"] is True
    assert adapter.extra["has_history"] is False
    assert adapter.extra["posts_seen"] == 0
    assert "@openai" in adapter.extra["error"]


def test_preview_that_renders_but_holds_no_posts():
    adapter, _ = tg_adapter({TG_URL: TG_EMPTY_HISTORY})
    assert adapter.collect() == []
    assert adapter.extra["empty"] is True
    assert adapter.extra["has_history"] is True
    assert "no_public_preview" not in adapter.extra
    assert "carries no posts" in adapter.extra["error"]


def test_url_that_is_not_a_telegram_channel():
    adapter, fetcher = tg_adapter({}, url="https://github.com/anthropics/claude-code")
    assert adapter.collect() == []
    assert fetcher.calls == []
    assert "not a telegram channel url" in adapter.extra["error"]


def test_http_failure_on_the_preview_is_recorded():
    fetcher = FakeFetcher({})  # every URL answers 404
    source = SourceConfig(id="tg_gone", type="telegram_channel", url=TG_URL)
    adapter = TelegramChannelAdapter(source, fetcher)

    assert adapter.collect() == []
    assert adapter.extra["http_status"] == 404


def test_backfill_walks_back_through_before():
    pages = {
        TG_URL: TG_PAGE_LATEST,
        f"{TG_URL}?before=9705": TG_PAGE_OLDER,
        f"{TG_URL}?before=9700": TG_PAGE_OLDEST,
    }
    adapter, fetcher = tg_adapter(pages, backfill_supported=True)
    items = adapter.backfill()

    assert fetcher.calls == [TG_URL, f"{TG_URL}?before=9705", f"{TG_URL}?before=9700"]
    assert [item.extra["message_id"] for item in items] == [
        9707,
        9705,
        9701,
        9700,
        9698,
    ]
    assert adapter.extra["pages_fetched"] == 3


def test_backfill_stops_once_the_depth_is_covered():
    pages = {
        TG_URL: TG_PAGE_LATEST,
        f"{TG_URL}?before=9705": TG_PAGE_OLDER,
        f"{TG_URL}?before=9700": TG_PAGE_OLDEST,
    }
    adapter, fetcher = tg_adapter(pages, backfill_supported=True)
    # Depth reaching back to 2026-08-01: the second page is already older, so
    # the third is never paid for.
    depth = (datetime.now(UTC) - datetime(2026, 8, 1, tzinfo=UTC)).days
    items = adapter.backfill(depth_days=depth)

    assert fetcher.calls == [TG_URL, f"{TG_URL}?before=9705"]
    assert [item.extra["message_id"] for item in items] == [9707, 9705]


def test_collect_never_paginates():
    pages = {TG_URL: TG_PAGE_LATEST, f"{TG_URL}?before=9705": TG_PAGE_OLDER}
    adapter, fetcher = tg_adapter(pages)
    adapter.collect()
    assert fetcher.calls == [TG_URL]
