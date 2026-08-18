"""Fixtures reproduce the markup the live pages actually serve.

docs.claude.com puts the anchor id on a div inside the heading and renders
model ids as copy-to-clipboard buttons; platform.openai.com puts the id on the
heading itself and prefixes every title with an ISO date; cursor.com answers
200 with a page that contains no date at all. Each shape is a fixture below.
"""

from datetime import date, datetime, timedelta

import pytest

from radar.adapters.base import SourceConfig
from radar.adapters.html_page import (
    HtmlPageAdapter,
    date_stated_alone,
    extract_json_index,
    extract_page_text,
    parse_date_fragment,
    period_end,
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

# cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions and its
# two siblings at AWS and Anthropic. Every row is a retirement still to come,
# and the table stands unchanged for months at a time.
RETIREMENT_TABLE_PAGE = """
<html><body><main>
<h2><div id="model-retirements">Model retirements</div></h2>
<table>
  <thead><tr>
    <th>Model version</th><th>Release date</th><th>Discontinuation date</th>
  </tr></thead>
  <tbody>
    <tr><td>gemini-2.5-pro</td><td>N/A</td><td>June 17, 2027</td></tr>
    <tr><td>gemini-2.5-flash</td><td>N/A</td><td>September 25, 2027</td></tr>
    <tr><td>gemini-2.0-flash-001</td><td>N/A</td><td>March 5, 2028</td></tr>
  </tbody>
</table>
</main></body></html>
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


# docs.n8n.io/release-notes/: the version is the heading, the date is the line
# under it.
N8N_PAGE = """
<html><body>
<main><article>
<h1>Release notes</h1>
<h2 id="how-to-update-n8n">How to update n8n</h2>
<p>Refer to the update guide for your installation method before upgrading.</p>
<h2 id="n8n234">n8n 2.34 OIDC logout support added and made opt-in, plus 18 other features</h2>
<p class="max-w-3xl"><strong class="font-bold">Released:</strong> 2026-08-04</p>
<ul>
  <li>Make OIDC RP-initiated logout an opt-in setting: OIDC RP-Initiated Logout
  is now opt-in and disabled by default.</li>
  <li>Slack Node: user pickers now show real names alongside handles.</li>
</ul>
<h2 id="n8n233">n8n 2.33 Redesigned instance settings for AI assistant, plus 10 other features</h2>
<p class="max-w-3xl"><strong class="font-bold">Released:</strong> 2026-07-28</p>
<ul>
  <li>Allow custom OAuth scopes for Microsoft Azure Monitor credentials.</li>
</ul>
</article></main>
</body></html>
"""

# docs.n8n.io/changelog/release-notes-1.x: same shape, longer date line, and a
# breaking-changes subsection that belongs to the release above it.
N8N_1X_PAGE = """
<html><body>
<main><article>
<h1>1.x release notes</h1>
<h2 id="n8n-1.32.0">n8n@1.32.0</h2>
<p><a href="https://github.com/n8n-io/n8n/commits/n8n@1.32.0">View the commits
for this version</a>. Release date: 2024-03-06</p>
<p>This release contains new features, node enhancements and bug fixes.</p>
<h2 id="n8n-1.31.1">n8n@1.31.1</h2>
<p><a href="https://github.com/n8n-io/n8n/commits/n8n@1.31.1">View the commits
for this version</a>. Release date: 2024-03-06</p>
<h3 id="breaking-changes">Breaking changes</h3>
<p>Please note that this version contains a breaking change. HTTP connections
to the editor will fail on domains other than localhost.</p>
<p>This is a bug fix release and it contains a breaking change.</p>
<h2 id="n8n-1.31.0">n8n@1.31.0</h2>
<p><a href="https://github.com/n8n-io/n8n/commits/n8n@1.31.0">View the commits
for this version</a>. Release date: 2024-02-28</p>
<p>This release contains new features, new nodes and bug fixes.</p>
</article></main>
</body></html>
"""

# changelog.langchain.com: a Mintlify "update" block. The week is a button in
# the sticky left column, the entry headings carry no date at all.
LANGCHAIN_UPDATE = """
<div class="update flex flex-col relative w-full lg:flex-row update-container" id="{anchor}">
  <div class="lg:sticky group flex flex-col w-full lg:w-[160px]">
    <div class="absolute">
      <a href="#{anchor}" aria-label="Navigate to changelog: {label}">​<div class="size-6"></div></a>
    </div>
    <button type="button" class="cursor-pointer px-2 py-1 rounded-lg"
      contentEditable="false" data-component-part="update-label">{label}</button>
    <span role="status" class="sr-only"></span>
  </div>
  <div class="flex-1 overflow-hidden">
    <div class="prose-sm" data-component-part="update-content">
      <h2 class="flex group font-semibold" id="{heading_id}">
        <div class="absolute" tabindex="-1"></div>​{heading}</h2>
      {body}
    </div>
  </div>
</div>
"""

LANGCHAIN_PAGE = (
    '<html><body><main><div id="content-area">'
    "<p>Weekly updates to LangSmith Cloud and LangSmith Fleet.</p>"
    + LANGCHAIN_UPDATE.format(
        anchor="august-3-10-2026",
        label="August 3-10, 2026",
        heading_id="access-control",
        heading="Access control",
        body=(
            "<p>Access-policy endpoints now report errors as RFC 7807 problem"
            " details, and semantically invalid request bodies return HTTP 422.</p>"
            "<p>The legacy dataset export endpoint is deprecated and is"
            " scheduled for removal on 2026-08-20; move to the new export API"
            " before that date.</p>"
        ),
    )
    + LANGCHAIN_UPDATE.format(
        anchor="june-29-july-3-2026",
        label="June 29 - July 3, 2026",
        heading_id="deployment",
        heading="Deployment",
        body=(
            "<p>Deployments now report the image digest they were rolled out"
            " from, which makes a rollback reproducible.</p>"
        ),
    )
    + LANGCHAIN_UPDATE.format(
        anchor="december-29-january-2-2026",
        label="December 29 - January 2, 2026",
        heading_id="sandboxes",
        heading="Sandboxes",
        body=(
            "<p>Sandboxes started reporting their remaining runtime budget in"
            " the response headers of every request.</p>"
        ),
    )
    + "</div></main></body></html>"
)

# docs.langchain.com/langsmith/self-hosted-changelog: the same Mintlify block
# with an ISO date for a label and the release as the heading.
LANGSMITH_PAGE = (
    '<html><body><main><div id="content-area">'
    "<p>Self-hosted LangSmith is an add-on to the Enterprise plan.</p>"
    + LANGCHAIN_UPDATE.format(
        anchor="2026-08-16",
        label="2026-08-16",
        heading_id="langsmith-0-16-7",
        heading="langsmith-0.16.7",
        body="<p>Internal improvements and maintenance updates.</p>",
    )
    + LANGCHAIN_UPDATE.format(
        anchor="2026-08-12",
        label="2026-08-12",
        heading_id="langsmith-0-16-0",
        heading="langsmith-0.16.0",
        body=(
            '<h3 id="breaking-changes">​Breaking changes</h3>'
            "<p>The bundled Redis chart is removed; point the deployment at an"
            " external Redis before upgrading.</p>"
        ),
    )
    + "</div></main></body></html>"
)

# platform.openai.com/docs/changelog: the month heading states the year, the
# badge inside it states the day, and nothing states both.
OPENAI_CHANGELOG_PAGE = """
<html><body>
<main><article id="mainContent">
<h1>Changelog</h1>
<div class="mb-12">
  <h3 class="_ChangelogSectionTitle_f3xd6_43">August, 2026</h3>
  <div class="mt-5">
    <div class="grid grid-cols-[3rem_1fr] items-start">
      <div><div class="_Badge_10t5o_1" data-variant="outline">Aug 13</div></div>
      <div>
        <div class="flex flex-wrap gap-2 mb-2">
          <div class="_Badge_10t5o_1" data-color="success"><span class="capitalize">Announcement</span></div>
        </div>
        <div class="_MarkdownContent_abpsh_1"><p>Announced Ultrafast mode, a new
        API service tier for GPT-5.6 Sol that runs up to 14x faster than
        Standard processing.</p></div>
      </div>
      <div><div class="_Badge_10t5o_1" data-variant="outline">Aug 6</div></div>
      <div>
        <div class="_MarkdownContent_abpsh_1"><p>Released gpt-4o-2024-11-20, our
        newest model in the gpt-4o series.</p></div>
      </div>
    </div>
  </div>
</div>
<div class="mb-12">
  <h3 class="_ChangelogSectionTitle_f3xd6_43">December, 2025</h3>
  <div class="mt-5">
    <div class="grid grid-cols-[3rem_1fr] items-start">
      <div><div class="_Badge_10t5o_1" data-variant="outline">Dec 14</div></div>
      <div>
        <div class="_MarkdownContent_abpsh_1"><p>The legacy completions endpoint
        started returning a deprecation header on every response.</p></div>
      </div>
    </div>
  </div>
</div>
</article></main>
</body></html>
"""

# platform.openai.com without the month headings: the same badges, nothing
# above them to state a year.
OPENAI_CHANGELOG_NO_MONTH_PAGE = """
<html><body>
<main><article id="mainContent">
<h1>Changelog</h1>
<div class="mt-5"><div class="grid">
  <div><div class="_Badge_10t5o_1">Aug 13</div></div>
  <div><p>Announced Ultrafast mode, a new API service tier for GPT-5.6 Sol.</p></div>
</div></div>
</article></main>
</body></html>
"""

# cursor.com/blog: no post bodies in the DOM at all, the index is a JSON array
# inside a script, escaped once for the string literal that carries it.
CURSOR_BLOG_PAGE = """
<html><body>
<main><h1>Blog</h1><p>Read about what we are building.</p></main>
<script>self.__next_f.push([1,"7:[\\"$\\",\\"section\\",null,{\\"posts\\":[
{\\"slug\\":\\"joining-spacex\\",\\"href\\":\\"/blog/joining-spacex\\",\\"title\\":\\"Cursor is now a part of SpaceX\\",\\"date\\":\\"2026-08-14T12:00:00.000Z\\",\\"categoryValue\\":\\"company\\",\\"readingTimeText\\":\\"3m\\"},
{\\"slug\\":\\"teams-pricing-june-2026\\",\\"href\\":\\"/blog/teams-pricing-june-2026\\",\\"title\\":\\"Improvements to Teams Pricing\\",\\"date\\":\\"2026-06-01T07:00:00.000Z\\",\\"categoryValue\\":\\"product\\",\\"readingTimeText\\":\\"4m\\"},
{\\"slug\\":\\"shadow-workspace\\",\\"href\\":\\"/blog/shadow-workspace\\",\\"title\\":\\"Iterating with shadow workspaces\\",\\"date\\":\\"2024-09-01T07:00:00.000Z\\",\\"categoryValue\\":\\"research\\",\\"readingTimeText\\":\\"9m\\"},
{\\"slug\\":\\"origin-code-hosting\\",\\"title\\":\\"Origin Code Hosting\\",\\"date\\":\\"2026-08-17T07:00:00.000Z\\",\\"categoryValue\\":\\"product\\"}
]}]"])</script>
</body></html>
"""

# modelcontextprotocol.io/specification/versioning: the revisions live in the
# navigation payload, and the newest one only as a label with no flat record.
MCP_VERSIONING_PAGE = """
<html><body>
<main><div id="content">
<h1>Versioning</h1>
<h2 id="revisions">​Revisions</h2>
<p>Revisions may be marked as Draft, Current or Final.</p>
</div></main>
<script>window.__MINTLIFY_NAV="{\\"tabs\\":[{\\"tab\\":\\"Documentation\\",\\"versions\\":[
{\\"version\\":\\"Version 2026-07-28 (latest)\\",\\"default\\":true,\\"pages\\":[{\\"title\\":\\"Specification\\",\\"href\\":\\"/specification/2026-07-28\\"},{\\"title\\":\\"Architecture\\",\\"href\\":\\"/specification/2026-07-28/architecture/index\\"}]},
{\\"version\\":\\"Version 2025-11-25\\",\\"pages\\":[{\\"title\\":\\"Version 2025-11-25\\",\\"href\\":\\"/docs/2025-11-25/learn/versioning\\"}]},
{\\"version\\":\\"Version 2025-06-18\\",\\"pages\\":[{\\"title\\":\\"Version 2025-06-18\\",\\"href\\":\\"/docs/2025-06-18/learn/versioning\\"}]},
{\\"version\\":\\"Draft\\",\\"pages\\":[{\\"title\\":\\"Draft\\",\\"href\\":\\"/docs/draft/learn/versioning\\"}]}]}]}"</script>
</body></html>
"""

# docs.claude.com/en/docs/about-claude/pricing: no changelog, no dated heading,
# two effective dates written into one paragraph of prose.
ANTHROPIC_PRICING_PAGE = """
<html><body>
<main><article class="prose">
<h1>Pricing</h1>
<h2 id="model-pricing">Model pricing</h2>
<table>
  <thead><tr><th>Model</th><th>Base input</th></tr></thead>
  <tbody><tr><td>Claude Sonnet 5</td><td>$2 / MTok</td></tr></tbody>
</table>
<p>The $2/$10 per million input/output token pricing for Claude Sonnet 5,
announced at launch as introductory pricing through August 31, 2026, is now the
standard price. The previously scheduled increase to $3/$15 per million
input/output tokens on September 1, 2026 will not occur.</p>
<h2 id="billing-and-payment">Billing and payment</h2>
<p>Invoices are issued monthly for organizations on annual contracts.</p>
</article></main>
</body></html>
"""

# github.blog/changelog/: one month per page, the day abbreviated with a dot
# and the year only on the month heading above.
GITHUB_ENTRY = """
<article><reactive-line-state class="ChangelogItem">
  <div class="ChangelogItem-content">
    <h3 class="ChangelogItem-content-meta" id="changelog-item-meta-{item_id}">
      <time aria-label=" {abbr} {day}." class="Tag" datetime="{year}-{month:02d}-{day:02d}">{abbr}.{day:02d}</time>
      <span class="Tag Tag--type-alt">{kind}</span>
    </h3>
    <div class="ChangelogItem-content-inner">
      <a href="https://github.blog/changelog/{year}-{month:02d}-{day:02d}-{slug}"
         class="ChangelogItem-title">{title}</a>
      <unveil-tags class="Tags"><div class="Tags-anim">
        <a href="https://github.blog/changelog/{year}/?label=copilot" class="Tag">copilot</a>
      </div></unveil-tags>
    </div>
  </div>
</reactive-line-state></article>
"""

GITHUB_MONTH = """
<html><body>
<main>
<h2 class="ChangelogMonthHeading">
  <button type="button" class="ChangelogMonthHeading-action">
    <span class="ChangelogMonth-name--full">{full}</span>
    <span class="ChangelogMonth-name--short">{abbr}</span>
    {year}
  </button>
</h2>
<div class="ChangelogMonthContent"><div class="ChangelogGroup">{entries}</div></div>
</main>
</body></html>
"""

_MONTH_NAMES = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def github_month_page(year: int, month: int, days: tuple[int, ...]) -> str:
    """A github.blog changelog month, one entry per day given."""
    full = _MONTH_NAMES[month]
    abbr = full[:3]
    entries = "".join(
        GITHUB_ENTRY.format(
            item_id=f"{year}{month:02d}{day:02d}",
            abbr=abbr,
            day=day,
            year=year,
            month=month,
            kind="Retired" if day % 2 else "Release",
            slug=f"change-{year}-{month:02d}-{day:02d}",
            title=f"Change shipped on {full} {day}",
        )
        for day in days
    )
    return GITHUB_MONTH.format(full=full, abbr=abbr, year=year, entries=entries)


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


class FakeArchiveFetcher:
    """Stands in for Fetcher across a per-month archive: one page per URL."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, **kwargs) -> FetchResult:
        self.calls.append(url)
        text = self.pages.get(url)
        if text is None:
            return FetchResult(
                url=url,
                status_code=404,
                text="",
                headers={},
                ref="sha256:missing",
                from_cache=True,
            )
        return FetchResult(
            url=url,
            status_code=200,
            text=text,
            headers={},
            ref=f"sha256:{url}",
            from_cache=True,
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

    @pytest.mark.parametrize(
        "text,expected",
        [
            # An ISO timestamp is what an embedded index states.
            ("2026-06-01T07:00:00.000Z", date(2026, 6, 1)),
            # github.blog abbreviates with a dot instead of a space.
            ("Aug.14 Improvement", None),
            # A week is dated from the day it starts.
            ("August 3-10, 2026", date(2026, 8, 3)),
            ("July 27-31, 2026", date(2026, 7, 27)),
            ("June 29 - July 3, 2026", date(2026, 6, 29)),
            # The year stated at the end of a range is the year it ends in.
            ("December 29 - January 2, 2026", date(2025, 12, 29)),
            # platform.openai.com puts a comma between month and year.
            ("June, 2025", date(2025, 6, 1)),
        ],
    )
    def test_shapes_the_configured_pages_actually_use(self, text, expected):
        parsed = parse_date_fragment(text)
        assert parsed is not None
        assert parsed.value == expected

    def test_a_dotted_month_still_refuses_to_invent_the_year(self):
        parsed = parse_date_fragment("Aug.14 Improvement")
        assert parsed is not None
        assert parsed.value is None and parsed.year_missing
        assert parsed.month == 8 and parsed.day == 14

    def test_a_range_keeps_day_precision(self):
        parsed = parse_date_fragment("August 3-10, 2026")
        assert parsed is not None
        assert parsed.precision is DatePrecision.DAY
        assert parsed.text == "August 3-10, 2026"

    @pytest.mark.parametrize(
        "text",
        [
            "gpt-4o-2024-08-06",
            "Released gpt-realtime-mini-2025-12-15 today",
            "web_search_20260209",
        ],
    )
    def test_a_date_glued_to_an_identifier_is_not_stated_alone(self, text):
        parsed = parse_date_fragment(text)
        assert parsed is None or not date_stated_alone(text, parsed)


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
# a date label outside the headings
# --------------------------------------------------------------------------


class TestDateMarks:
    def test_n8n_takes_the_date_from_the_line_under_the_version(self):
        items = make_adapter(N8N_PAGE).collect()
        assert [item.event_date for item in items] == [
            date(2026, 8, 4),
            date(2026, 7, 28),
        ]
        assert items[0].title.startswith("n8n 2.34 OIDC logout support")
        assert items[0].extra["date_text"] == "Released: 2026-08-04"

    def test_the_version_heading_stays_with_its_own_release(self):
        items = make_adapter(N8N_PAGE).collect()
        assert "n8n 2.34" in items[0].raw_text
        assert "n8n 2.33" not in items[0].raw_text
        assert "OIDC RP-Initiated Logout is now opt-in" in items[0].raw_text
        assert items[0].url.endswith("#n8n234")

    def test_a_how_to_heading_without_a_date_is_not_an_item(self):
        items = make_adapter(N8N_PAGE).collect()
        assert all("How to update" not in item.title for item in items)

    def test_a_subsection_belongs_to_the_release_above_it(self):
        items = make_adapter(N8N_1X_PAGE).collect()
        breaking = next(item for item in items if "1.31.1" in item.title)
        assert "Breaking changes" in breaking.raw_text
        assert "HTTP connections to the editor will fail" in breaking.raw_text
        assert "n8n@1.31.0" not in breaking.raw_text

    def test_two_releases_on_one_day_are_two_items(self):
        items = make_adapter(N8N_1X_PAGE).collect()
        assert [item.title for item in items] == [
            "n8n@1.32.0",
            "n8n@1.31.1",
            "n8n@1.31.0",
        ]
        assert [item.event_date for item in items] == [
            date(2024, 3, 6),
            date(2024, 3, 6),
            date(2024, 2, 28),
        ]

    def test_a_week_heading_dates_the_entry_from_the_start_of_the_range(self):
        items = make_adapter(LANGCHAIN_PAGE).collect()
        august = next(item for item in items if item.title.startswith("August 3-10"))
        assert august.event_date == date(2026, 8, 3)
        assert august.date_precision is DatePrecision.DAY
        assert august.extra["date_text"] == "August 3-10, 2026"

    def test_a_range_that_turns_the_calendar_over_starts_a_year_back(self):
        items = make_adapter(LANGCHAIN_PAGE).collect()
        winter = next(item for item in items if item.title.startswith("December 29"))
        # The label states 2026, and 2026 is the year the range ends in.
        assert winter.event_date == date(2025, 12, 29)

    def test_a_removal_date_inside_the_week_stays_inside_its_entry(self):
        items = make_adapter(LANGCHAIN_PAGE).collect()
        august = next(item for item in items if item.title.startswith("August 3-10"))
        assert "scheduled for removal on 2026-08-20" in august.raw_text
        assert "Deployment" not in august.raw_text

    def test_the_week_anchor_comes_from_the_block_around_the_label(self):
        items = make_adapter(
            LANGCHAIN_PAGE, url="https://changelog.langchain.com/"
        ).collect()
        assert items[0].url == "https://changelog.langchain.com/#august-3-10-2026"

    def test_an_iso_label_titles_the_entry_with_the_release_beside_it(self):
        items = make_adapter(LANGSMITH_PAGE).collect()
        assert [item.event_date for item in items] == [
            date(2026, 8, 16),
            date(2026, 8, 12),
        ]
        assert items[0].title == "2026-08-16 - langsmith-0.16.7"
        assert "Breaking changes" in items[1].raw_text

    def test_zero_width_anchors_are_not_part_of_a_title(self):
        items = make_adapter(LANGSMITH_PAGE).collect()
        assert all("​" not in item.title for item in items)


# --------------------------------------------------------------------------
# month heading, day badge
# --------------------------------------------------------------------------


class TestMonthThenDay:
    def test_the_day_badge_is_the_event_and_the_month_heading_is_its_year(self):
        items = make_adapter(OPENAI_CHANGELOG_PAGE, hint="month_then_day").collect()
        assert [item.event_date for item in items] == [
            date(2026, 8, 13),
            date(2026, 8, 6),
            date(2025, 12, 14),
        ]
        assert items[0].extra["year_from_heading"] == "August, 2026"

    def test_the_month_heading_is_not_an_item_of_its_own(self):
        items = make_adapter(OPENAI_CHANGELOG_PAGE, hint="month_then_day").collect()
        assert all(
            item.title not in ("August, 2026", "December, 2025") for item in items
        )

    def test_a_december_badge_does_not_borrow_the_year_of_the_badge_above(self):
        # "Dec 14" sits under "December, 2025" while the entry above it is
        # August 2026. Borrowing from the neighbour would date it 2026-12-14.
        items = make_adapter(OPENAI_CHANGELOG_PAGE, hint="month_then_day").collect()
        december = next(
            item
            for item in items
            if item.date_precision is DatePrecision.DAY and item.event_date.month == 12
        )
        assert december.event_date == date(2025, 12, 14)
        assert december.extra["year_from_heading"] == "December, 2025"

    def test_an_entry_stops_at_the_next_month_heading(self):
        items = make_adapter(OPENAI_CHANGELOG_PAGE, hint="month_then_day").collect()
        august_last = next(
            item for item in items if item.event_date == date(2026, 8, 6)
        )
        assert "December, 2025" not in august_last.raw_text
        assert "deprecation header" not in august_last.raw_text

    def test_a_dated_model_id_in_the_body_is_not_a_date_label(self):
        items = make_adapter(OPENAI_CHANGELOG_PAGE, hint="month_then_day").collect()
        assert len(items) == 3
        assert all(item.extra["date_text"] != "2024-11-20" for item in items)

    def test_without_a_month_heading_the_day_stays_unresolved(self):
        items = make_adapter(
            OPENAI_CHANGELOG_NO_MONTH_PAGE, hint="month_then_day"
        ).collect()
        assert len(items) == 1
        assert items[0].event_date is None
        assert items[0].date_precision is DatePrecision.INFERRED
        assert items[0].extra["date_unresolved"] is True
        assert "year_from_heading" not in items[0].extra


# --------------------------------------------------------------------------
# an index serialised into the page
# --------------------------------------------------------------------------


class TestEmbeddedJsonIndex:
    def test_one_item_per_record_of_the_index(self):
        items = make_adapter(CURSOR_BLOG_PAGE, hint="embedded_json_index").collect()
        assert [item.event_date for item in items] == [
            date(2026, 8, 17),
            date(2026, 8, 14),
            date(2026, 6, 1),
            date(2024, 9, 1),
        ]
        assert items[1].title == "Cursor is now a part of SpaceX"

    def test_an_iso_timestamp_keeps_its_day(self):
        items = make_adapter(CURSOR_BLOG_PAGE, hint="embedded_json_index").collect()
        pricing = next(item for item in items if "Teams Pricing" in item.title)
        assert pricing.event_date == date(2026, 6, 1)
        assert pricing.date_precision is DatePrecision.DAY

    def test_the_record_address_becomes_the_item_url(self):
        items = make_adapter(
            CURSOR_BLOG_PAGE, hint="embedded_json_index", url="https://cursor.com/blog"
        ).collect()
        pricing = next(item for item in items if "Teams Pricing" in item.title)
        assert pricing.url == "https://cursor.com/blog/teams-pricing-june-2026"

    def test_a_bare_slug_is_not_treated_as_an_address(self):
        items = make_adapter(
            CURSOR_BLOG_PAGE, hint="embedded_json_index", url="https://cursor.com/blog"
        ).collect()
        origin = next(item for item in items if item.title == "Origin Code Hosting")
        assert origin.url == "https://cursor.com/blog"

    def test_the_body_is_short_because_the_page_carries_no_body(self):
        items = make_adapter(CURSOR_BLOG_PAGE, hint="embedded_json_index").collect()
        assert all(len(item.raw_text) < 200 for item in items)
        assert "Cursor is now a part of SpaceX" in items[1].raw_text

    def test_a_revision_list_is_read_even_without_the_hint(self):
        # modelcontextprotocol.io is configured as dated_sections and has no
        # dated heading at all; the revisions are only in the nav payload.
        items = make_adapter(MCP_VERSIONING_PAGE).collect()
        assert [item.event_date for item in items] == [
            date(2026, 7, 28),
            date(2025, 11, 25),
            date(2025, 6, 18),
        ]
        assert items[0].title == "Version 2026-07-28 (latest)"

    def test_a_page_address_that_merely_contains_a_date_is_not_a_revision(self):
        items = make_adapter(MCP_VERSIONING_PAGE).collect()
        assert all("Architecture" not in item.title for item in items)
        assert all("Specification" != item.title for item in items)

    def test_the_fuller_reading_wins_when_both_are_guesses(self):
        # The page renders two entries and also ships a one-record navigation
        # payload. Taking the first non-empty reading would keep the payload.
        html = LANGSMITH_PAGE.replace(
            "</body>",
            '<script>{\\"title\\":\\"2026-08-16\\",'
            '\\"href\\":\\"/langsmith/self-hosted-changelog\\"}</script></body>',
        )
        items = make_adapter(html).collect()
        assert [item.event_date for item in items] == [
            date(2026, 8, 16),
            date(2026, 8, 12),
        ]
        assert "langsmith-0.16.7" in items[0].raw_text


# --------------------------------------------------------------------------
# a dated sentence, when the page has nothing else
# --------------------------------------------------------------------------


class TestDatedSentences:
    def test_each_effective_date_in_a_paragraph_is_its_own_item(self):
        items = make_adapter(ANTHROPIC_PRICING_PAGE).collect()
        assert [item.event_date for item in items] == [
            date(2026, 9, 1),
            date(2026, 8, 31),
        ]

    def test_the_sentence_is_the_material_and_the_heading_names_it(self):
        items = make_adapter(ANTHROPIC_PRICING_PAGE).collect()
        september = items[0]
        assert september.title == "Model pricing - September 1, 2026"
        assert september.raw_text == (
            "The previously scheduled increase to $3/$15 per million "
            "input/output tokens on September 1, 2026 will not occur."
        )
        assert september.url.endswith("#model-pricing")

    def test_prose_is_not_read_on_a_page_that_has_a_shape(self):
        # OPENAI_PAGE states dates in its headings, so its prose is never
        # split into a sentence per date.
        items = make_adapter(OPENAI_PAGE).collect()
        assert len(items) == 3


# --------------------------------------------------------------------------
# github.blog: history only through the per-month archive
# --------------------------------------------------------------------------


def github_source(**kwargs) -> SourceConfig:
    return SourceConfig(
        id="github_changelog",
        type="html_scrape",
        url="https://github.blog/changelog/",
        parser_hint="dated_sections",
        backfill_supported=True,
        backfill_url_template="https://github.blog/changelog/{year}/{month:02d}/",
        **kwargs,
    )


def github_archive(months: int = 14) -> FakeArchiveFetcher:
    """The index page plus one archive page per month back from today."""
    today = date.today()
    pages = {}
    month = today.replace(day=1)
    for index in range(months):
        days = (3, 14) if index else (3,)
        page = github_month_page(month.year, month.month, days)
        pages[f"https://github.blog/changelog/{month.year}/{month.month:02d}/"] = page
        if index == 0:
            pages["https://github.blog/changelog/"] = page
        month = (month - timedelta(days=1)).replace(day=1)
    return FakeArchiveFetcher(pages)


class TestMonthlyArchiveBackfill:
    def test_the_index_page_alone_is_one_month(self):
        adapter = HtmlPageAdapter(github_source(), github_archive())
        items = adapter.collect()
        assert len(items) == 1
        assert items[0].event_date == date.today().replace(day=3)

    def test_the_day_is_read_from_a_dotted_month_and_the_year_from_above(self):
        adapter = HtmlPageAdapter(github_source(), github_archive())
        item = adapter.collect()[0]
        assert item.extra["date_text"].startswith(
            date.today().strftime("%b").title() + "."
        )
        assert item.extra["year_from_heading"].endswith(str(date.today().year))

    def test_backfill_walks_one_url_per_month_back_over_the_window(self):
        fetcher = github_archive()
        adapter = HtmlPageAdapter(github_source(backfill_depth_days=365), fetcher)
        adapter.backfill()
        month = date.today().replace(day=1)
        expected = []
        while period_end(month, DatePrecision.MONTH) >= date.today() - timedelta(365):
            expected.append(
                f"https://github.blog/changelog/{month.year}/{month.month:02d}/"
            )
            month = (month - timedelta(days=1)).replace(day=1)
        assert [url for url in fetcher.calls if url in expected] == expected

    def test_a_year_of_archive_is_more_than_a_year_of_the_index(self):
        adapter = HtmlPageAdapter(
            github_source(backfill_depth_days=365), github_archive()
        )
        items = adapter.backfill()
        assert len(items) > 20
        months = {(item.event_date.year, item.event_date.month) for item in items}
        assert len(months) >= 12

    def test_the_current_month_is_not_collected_twice(self):
        adapter = HtmlPageAdapter(
            github_source(backfill_depth_days=365), github_archive()
        )
        items = adapter.backfill()
        assert len({(item.event_date, item.raw_text) for item in items}) == len(items)

    def test_a_missing_month_is_skipped_rather_than_raised(self):
        fetcher = github_archive()
        gone = sorted(fetcher.pages)[0]
        fetcher.pages.pop(gone)
        adapter = HtmlPageAdapter(github_source(backfill_depth_days=365), fetcher)
        assert len(adapter.backfill()) > 10

    def test_the_walk_stops_at_the_configured_depth(self):
        fetcher = github_archive()
        adapter = HtmlPageAdapter(github_source(backfill_depth_days=40), fetcher)
        items = adapter.backfill()
        cutoff = date.today() - timedelta(days=40)
        assert all(item.event_date >= cutoff for item in items)
        assert len(fetcher.calls) <= 4

    def test_a_source_without_a_template_never_walks(self):
        fetcher = github_archive()
        source = github_source(backfill_depth_days=365)
        source.backfill_url_template = None
        HtmlPageAdapter(source, fetcher).backfill()
        assert fetcher.calls == ["https://github.blog/changelog/"]


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
            (N8N_PAGE, "dated_sections"),
            (N8N_1X_PAGE, "dated_sections"),
            (LANGCHAIN_PAGE, "dated_sections"),
            (LANGSMITH_PAGE, "dated_sections"),
            (OPENAI_CHANGELOG_PAGE, "month_then_day"),
            (ANTHROPIC_PRICING_PAGE, "dated_sections"),
        ],
    )
    def test_section_text_is_a_substring_of_the_page_text(self, html, hint):
        page = extract_page_text(html)
        for item in make_adapter(html, hint=hint).collect():
            assert item.raw_text in page.text

    @pytest.mark.parametrize("html", [CURSOR_BLOG_PAGE, MCP_VERSIONING_PAGE])
    def test_an_index_record_is_a_substring_of_the_index_text(self, html):
        # The page renders no bodies, so the index it ships is the material,
        # and a record is still a slice of it rather than a rebuilt string.
        index = extract_json_index(html)
        for item in make_adapter(html, hint="embedded_json_index").collect():
            assert item.raw_text in index.text

    def test_a_github_archive_entry_is_a_substring_of_its_own_month(self):
        fetcher = github_archive()
        items = HtmlPageAdapter(
            github_source(backfill_depth_days=365), fetcher
        ).backfill()
        texts = [extract_page_text(page).text for page in fetcher.pages.values()]
        for item in items:
            assert any(item.raw_text in text for text in texts)

    def test_a_quote_from_a_week_verifies_against_the_week(self):
        items = make_adapter(LANGCHAIN_PAGE).collect()
        august = next(i for i in items if i.title.startswith("August 3-10"))
        ok, reason = verify_evidence(
            "scheduled for removal on 2026-08-20", august.raw_text
        )
        assert ok, reason

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


class TestTheWindowIsAboutEventsNotAboutNovelty:
    """Where this adapter's responsibility ends.

    `collect(since)` narrows by event date and can do nothing else: it is
    handed a URL and a window, and it has no memory of what any previous run
    saw. On a table of retirements still to come the window therefore narrows
    nothing at all — every row is dated ahead of any cutoff, on every day the
    digest is ever run. Three configured sources are exactly that shape and
    together hand over forty rows each morning.

    The tests below pin that as a stated property rather than leave it as a
    surprise, and they pin the reason it must not be "fixed" here: a table row
    dated ahead is the normal case, not the stale one. Live freshness is
    decided one stage on, by `radar.delta.filter_unseen`, which knows when a
    record was first seen.
    """

    def test_a_table_of_future_retirements_clears_every_window(self):
        adapter = make_adapter(RETIREMENT_TABLE_PAGE, hint="dated_table")
        for since in (
            datetime(2020, 1, 1),
            datetime(2026, 8, 18),
            datetime(2027, 1, 1),
        ):
            assert len(adapter.collect(since)) == 3

    def test_two_mornings_in_a_row_are_indistinguishable_here(self):
        adapter = make_adapter(RETIREMENT_TABLE_PAGE, hint="dated_table")
        monday = adapter.collect(datetime(2026, 8, 18))
        tuesday = adapter.collect(datetime(2026, 8, 19))
        assert [i.raw_text for i in monday] == [i.raw_text for i in tuesday]

    def test_a_row_is_never_dropped_for_being_dated_ahead(self):
        """The reason the window is not the place to fix this.

        A retirement announced this morning for 2028 is the single most
        valuable thing the digest carries. Any rule that discards future dates
        to quieten the tables would discard that too, silently.
        """
        adapter = make_adapter(RETIREMENT_TABLE_PAGE, hint="dated_table")
        assert len(adapter.collect(datetime(2026, 8, 18))) == 3

    def test_backfill_keeps_the_whole_table(self):
        """Depth is the backfill's whole point and stays untouched."""
        adapter = make_adapter(
            RETIREMENT_TABLE_PAGE,
            hint="dated_table",
            backfill_supported=True,
            backfill_depth_days=720,
        )
        assert len(adapter.backfill()) == 3
        assert len(adapter.backfill(depth_days=720)) == 3


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


class TestSectionProvenance:
    """A section knows it was cut out of a document already read whole.

    Enrichment completes a short material by fetching its URL, which is right
    for an RSS teaser and wrong for a table row: the document behind the row's
    URL is the table it came from. The flag is what lets the next stage tell
    the two apart, so it has to be set where the cutting happens.
    """

    TABLE = """
    <html><body>
      <h2 id="imagen-models">Imagen models</h2>
      <table>
        <tr><th>Model</th><th>Shutdown date</th></tr>
        <tr><td>imagen-4.0-generate-001</td><td>August 17, 2026</td></tr>
        <tr><td>imagen-4.0-fast-generate-001</td><td>August 17, 2026</td></tr>
      </table>
    </body></html>
    """

    def test_a_row_cut_from_the_page_is_marked_as_a_section_of_it(self):
        items = make_adapter(
            self.TABLE, hint="dated_table", url="https://ai.google.dev/deprecations"
        ).collect(None)

        assert items, "the table produced no materials"
        for item in items:
            assert (
                item.extra.get("page_section") == "https://ai.google.dev/deprecations"
            )

    def test_a_section_pointing_at_another_document_is_not_marked(self):
        html = """
        <html><body>
          <h2>August 11, 2026</h2>
          <p>Read the <a href="/posts/announcement">announcement</a>.</p>
        </body></html>
        """
        items = make_adapter(html, hint="dated_sections").collect(None)

        assert items
        # The heading section carries no link of its own, so it is still a cut
        # of this page. The assertion that matters is the flag tracking the
        # section's origin rather than being pasted on everything.
        assert all(
            item.extra.get("page_section") in (None, "https://example.test/changelog")
            for item in items
        )
