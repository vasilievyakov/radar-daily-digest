"""RSS and Atom feed adapter (`rss`).

A feed is the cheapest full-text source in the config: one request returns the
whole window a publisher is willing to expose, already split into dated
entries. Seven of the configured feeds behave that way; two addresses that
look like feeds are not feeds at all.

Three properties the rest of the pipeline leans on:

* `raw_text` is the entry body as text, never as markup. A fact's evidence is
  verified by substring against this text (FR-4.3), and `verify_evidence`
  only folds whitespace, dashes and quotes - it does not strip tags. An
  entry body left as HTML would make every quote inside it unverifiable.
* the fetch goes through `Fetcher`, never through feedparser's own network
  code. feedparser would bypass the cache, the polite delay and the retry
  policy, and the cache key doubles as `raw_material_ref`.
* the adapter never raises. HTTP 200 carrying an HTML page instead of a feed
  is a recorded outcome, not a crash: `docs.claude.com/rss.xml` answers with
  904 KB of HTML and `blog.langchain.com/rss/` with 202 KB of it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import feedparser

from radar.adapters.base import Adapter, CollectedItem, SourceConfig
from radar.adapters.html_page import extract_page_text
from radar.cache import canonical_url
from radar.fetch import Fetcher
from radar.models import DatePrecision

log = logging.getLogger(__name__)

_HTML_TYPES = ("text/html", "application/xhtml")
_HTML_MARKERS = ("<!doctype html", "<html")
_URL_SCHEMES = ("http://", "https://")
# Titles are display text, not evidence, so a long one is cut rather than kept.
MAX_TITLE_CHARS = 200


def html_to_text(html: str) -> str:
    """Entry body as readable text, with block boundaries kept as newlines.

    Reuses the page extractor so a feed body and a scraped page produce the
    same shape of text: `get_text()` would glue `<p>a</p><p>b</p>` into "ab"
    and `get_text(" ")` would break "Cursor's" into "Cursor 's", and either
    one silently turns a quotable phrase into an unverifiable one.
    """
    if not html or not html.strip():
        return ""
    try:
        return extract_page_text(html).text.strip()
    except Exception:
        # Text that cannot be parsed is still better evidence than nothing.
        return html.strip()


def plain_title(value: Any) -> str:
    """Titles arrive as escaped HTML often enough to always run them through."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "<" in text or "&" in text:
        text = html_to_text(text)
    text = " ".join(text.split())
    return text[:MAX_TITLE_CHARS].strip()


def is_html_type(media_type: Any) -> bool:
    return any(marker in str(media_type or "").lower() for marker in _HTML_TYPES)


def pick_body(entry: Any) -> tuple[str, str, str]:
    """Body of an entry as (markup, field, media type), first non-empty wins.

    Order is `content:encoded`, then `content`, then `summary`. feedparser
    folds `content:encoded` and a bare `<content>` into the same
    `entry.content` list and keeps no element name, so the media type is what
    separates them: `content:encoded` is html by contract while a plain
    `<content>` parses as text/plain. Atom entries land in the same list and
    take the same first branch.

    The order matters most where the two disagree. `github.blog` puts a
    453-character teaser in `<description>` and the 5 KB post in
    `content:encoded`; taking the summary would keep the teaser and lose
    every fact in the post.
    """
    contents = entry.get("content") or []
    for want_html in (True, False):
        for item in contents:
            value = str(item.get("value") or "").strip()
            if value and is_html_type(item.get("type")) is want_html:
                return value, "content", str(item.get("type") or "")
    summary = str(entry.get("summary") or "").strip()
    if summary:
        media = str((entry.get("summary_detail") or {}).get("type") or "")
        return summary, "summary", media
    return "", "", ""


def to_utc(parsed: Any) -> datetime | None:
    """feedparser hands back a UTC `struct_time`; this only re-tags it."""
    if not parsed:
        return None
    try:
        return datetime(*tuple(parsed)[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def entry_date(entry: Any) -> tuple[datetime | None, str]:
    """Published date, falling back to updated. Never invents one."""
    published = to_utc(entry.get("published_parsed"))
    if published is not None:
        return published, "published"
    updated = to_utc(entry.get("updated_parsed"))
    if updated is not None:
        return updated, "updated"
    return None, ""


def entry_link(entry: Any) -> str:
    """Canonical link of an entry, or "" when the feed states none."""
    link = str(entry.get("link") or "").strip()
    if link:
        return link
    for candidate in entry.get("links") or []:
        href = str(candidate.get("href") or "").strip()
        if href and str(candidate.get("rel") or "alternate") == "alternate":
            return href
    guid = str(entry.get("id") or "").strip()
    return guid if guid.startswith(_URL_SCHEMES) else ""


def canonical_key(url: str) -> str:
    """Canonical URL that keeps the fragment.

    `canonical_url` drops fragments, and the Anthropic feed tells all 130 of
    its entries apart by fragment alone - every one of them points at
    `release-notes/overview`. Deduping on the stripped form would collapse the
    whole feed into a single item.
    """
    fragment = urlsplit(url).fragment
    base = canonical_url(url)
    return f"{base}#{fragment}" if fragment else base


def looks_like_html(text: str, headers: dict[str, str]) -> bool:
    """An HTML page served where a feed was expected."""
    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value.lower()
            break
    if any(marker in content_type for marker in _HTML_TYPES):
        return True
    head = text[:2048].lstrip().lower()
    return head.startswith(_HTML_MARKERS)


class RssFeedAdapter(Adapter):
    """One feed, parsed once, newest entry first."""

    type = "rss"

    def __init__(self, source: SourceConfig, fetcher: Fetcher) -> None:
        super().__init__(source, fetcher)
        # Why an empty result is empty. An empty list cannot say it itself.
        self.extra: dict[str, Any] = {}

    def collect(self, since: datetime | None = None) -> list[CollectedItem]:
        items = self._parse()
        if since is None:
            return items
        cutoff = since if since.tzinfo else since.replace(tzinfo=UTC)
        return [item for item in items if _reaches(item, cutoff, inclusive=False)]

    def backfill(self, depth_days: int | None = None) -> list[CollectedItem]:
        """Everything the feed handed over.

        There is nothing to walk: a feed returns its whole window in one
        response, so depth only trims what already arrived. Every feed in the
        config carries `backfill_supported: false` and this stays a fallback.
        """
        items = self._parse()
        depth = self.source.backfill_depth_days if depth_days is None else depth_days
        if not depth or depth <= 0:
            return items
        cutoff = datetime.now(UTC) - timedelta(days=depth)
        return [item for item in items if _reaches(item, cutoff, inclusive=True)]

    def _parse(self) -> list[CollectedItem]:
        self.extra = {"entries_seen": 0}

        try:
            result = self.fetcher.get(self.source.url)
        except Exception as exc:  # a broken source is data, not a crash (FR-1.5)
            self.extra["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return []

        if not result.ok:
            self.extra["http_status"] = result.status_code
            self.extra["error"] = result.error or f"HTTP {result.status_code}"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return []

        text = result.text or ""
        self.extra["response_bytes"] = len(text)
        if "<" not in text:
            # feedparser fetches anything that parses as a URL, and it is only
            # ever handed document text here. Text without a tag is not a feed.
            self.extra["empty"] = True
            self.extra["error"] = "response body is not markup"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return []

        try:
            parsed = feedparser.parse(text)
        except Exception as exc:
            self.extra["error"] = f"feedparser failed: {type(exc).__name__}: {exc}"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return []

        entries = list(parsed.entries or [])
        self.extra["entries_seen"] = len(entries)
        self.extra["feed_version"] = str(parsed.get("version") or "")
        if parsed.get("bozo"):
            # Kept even when entries parsed: half the feeds trip a warning on
            # a stray entity and still hand over every entry.
            self.extra["bozo"] = str(parsed.get("bozo_exception") or "malformed")

        if not entries:
            self.extra["empty"] = True
            if looks_like_html(text, result.headers):
                self.extra["not_a_feed"] = True
                self.extra["error"] = (
                    f"HTTP 200 served an HTML page, not a feed ({len(text)} bytes)"
                )
            else:
                self.extra["error"] = "HTTP 200 with zero entries"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return []

        items: list[CollectedItem] = []
        seen: set[str] = set()
        duplicates = 0
        undated = 0
        for index, entry in enumerate(entries):
            item, key = self._to_item(entry, index, result.ref)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if item.published_at is None:
                undated += 1
            items.append(item)

        if duplicates:
            self.extra["duplicates_dropped"] = duplicates
        if undated:
            self.extra["entries_without_date"] = undated

        # Newest first; undated entries keep their place at the end rather
        # than being dropped, because a feed that stops stating dates is
        # exactly what a human has to see.
        items.sort(
            key=lambda item: (
                item.published_at is not None,
                item.published_at or datetime.min.replace(tzinfo=UTC),
            ),
            reverse=True,
        )
        return items

    def _to_item(self, entry: Any, index: int, ref: str) -> tuple[CollectedItem, str]:
        link = entry_link(entry)
        guid = str(entry.get("id") or "").strip()
        markup, body_field, body_type = pick_body(entry)
        raw_text = html_to_text(markup)
        published, date_field = entry_date(entry)
        title = plain_title(entry.get("title"))

        extra: dict[str, Any] = {
            "source_id": self.source.id,
            "entry_index": index,
            "body_field": body_field or "none",
        }
        if body_type:
            extra["body_type"] = body_type
        if guid and guid != link:
            extra["guid"] = guid
        if date_field == "updated":
            extra["date_from"] = "updated"
        if published is None:
            # An entry with no date at all is kept and marked: dropping it
            # hides the entry, and stamping today's date corrupts the corpus.
            extra["date_missing"] = True
        if not link:
            extra["link_missing"] = True
        if not raw_text:
            extra["body_missing"] = True

        item = CollectedItem(
            url=link or self.source.url,
            title=title or _first_line(raw_text) or link or self.source.id,
            raw_text=raw_text,
            published_at=published,
            event_date=published.date() if published else None,
            date_precision=(DatePrecision.DAY if published else DatePrecision.INFERRED),
            raw_material_ref=ref,
            extra=extra,
        )
        # Fragment-preserving URL, guid, then title: enough to tell two
        # entries apart in every feed in the config.
        key = canonical_key(link) if link else (guid or f"{title}#{index}")
        return item, key


def _reaches(item: CollectedItem, cutoff: datetime, inclusive: bool) -> bool:
    """Undated entries always pass: silence about them hides the gap."""
    if item.published_at is None:
        return True
    return item.published_at >= cutoff if inclusive else item.published_at > cutoff


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:MAX_TITLE_CHARS].strip()
    return ""
