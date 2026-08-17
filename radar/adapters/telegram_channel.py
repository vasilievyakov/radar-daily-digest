"""Telegram channel adapter (`telegram_channel`).

Telegram exposes a public channel as an ordinary web page at `t.me/s/NAME`:
no API, no token, roughly twenty posts per request and a `?before=` link that
walks the history backwards. That page is the whole source.

Two things this adapter states and one it deliberately does not know:

* `raw_text` is the post as text. A post body is small enough that the whole
  of it is evidence, and `verify_evidence` checks a quote by substring
  against exactly this string, so the markup Telegram wraps around it -
  `<b>`, `<br/>`, link anchors, emoji spans - has to be gone before it lands.
* every item carries `extra["requires_corroboration"] = True`. SRC-2 forbids
  a priority-5 material from being the sole basis for a fact. The adapter
  knows nothing about priorities or about the rule; it only marks what it
  produced so the stages that do know can act on it.

Not every channel has a public preview. `t.me/s/openai`, `t.me/s/cursor_ide`
and `t.me/s/n8n_ru` all answer HTTP 200 with a 10 KB page carrying no posts -
a user, and two groups. That is a recorded outcome, not a failure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from radar.adapters.base import Adapter, CollectedItem, SourceConfig
from radar.adapters.html_page import extract_page_text
from radar.fetch import Fetcher
from radar.models import DatePrecision

log = logging.getLogger(__name__)

TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
MESSAGE_SELECTOR = "div.tgme_widget_message[data-post]"
TEXT_CLASS = "tgme_widget_message_text"
HISTORY_SELECTOR = ".tgme_channel_history"
MAX_TITLE_CHARS = 200


def parse_channel(url: str) -> str | None:
    """Channel name out of whatever form the config carries."""
    text = (url or "").strip()
    if not text:
        return None
    parts = urlsplit(text if "://" in text else f"https://{text}")
    host = parts.netloc.lower().rsplit("@", 1)[-1].split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    if host and host not in TELEGRAM_HOSTS:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if segments and segments[0] == "s":
        segments = segments[1:]  # /s/NAME is the preview form of /NAME
    if not segments:
        return None
    name = segments[0].lstrip("@").strip()
    return name or None


def preview_url(channel: str, before: int | None = None) -> str:
    base = f"https://t.me/s/{channel}"
    return f"{base}?before={before}" if before else base


def message_text(node: Tag) -> str:
    """Post body as text.

    Telegram nests a second element of the same class inside the first on
    long posts, so only the outermost blocks count - reading all of them
    would duplicate the whole message.
    """
    blocks = [
        block
        for block in node.select(f".{TEXT_CLASS}")
        if block.find_parent(class_=TEXT_CLASS) is None
        and block.find_parent(class_="tgme_widget_message_reply") is None
    ]
    parts: list[str] = []
    for block in blocks:
        try:
            text = extract_page_text(str(block)).text.strip()
        except Exception:
            text = block.get_text("\n").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def message_id(node: Tag) -> int | None:
    post = str(node.get("data-post") or "")
    tail = post.rsplit("/", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def message_datetime(node: Tag) -> datetime | None:
    stamp = node.select_one("a.tgme_widget_message_date time[datetime]")
    if stamp is None:
        stamp = node.select_one("time[datetime]")
    if stamp is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp.get("datetime") or "").strip())
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def message_link(node: Tag, channel: str, ident: int | None) -> str:
    anchor = node.select_one("a.tgme_widget_message_date[href]")
    href = str(anchor.get("href") or "").strip() if anchor is not None else ""
    if href.startswith(("http://", "https://")):
        return href
    post = str(node.get("data-post") or "").strip()
    if post:
        return f"https://t.me/{post}"
    return f"https://t.me/{channel}/{ident}" if ident else f"https://t.me/{channel}"


def next_before(soup: BeautifulSoup) -> int | None:
    """Message id to page back from.

    A `?before=` page carries two of these links, one back and one forward;
    only the one with `data-before` walks into history.
    """
    for anchor in soup.select("a.tme_messages_more[data-before]"):
        try:
            return int(str(anchor.get("data-before")))
        except ValueError:
            continue
    return None


class TelegramChannelAdapter(Adapter):
    """Public web preview of one channel."""

    type = "telegram_channel"

    # Twenty posts per page: the cap bounds a walk that never meets its
    # stop rule, and every page is a separate request paid for once.
    max_pages = 40

    def __init__(self, source: SourceConfig, fetcher: Fetcher) -> None:
        super().__init__(source, fetcher)
        self.channel = parse_channel(source.url)
        self.extra: dict[str, Any] = {}
        self._last_ref = ""

    def collect(self, since: datetime | None = None) -> list[CollectedItem]:
        cutoff = None
        if since is not None:
            cutoff = since if since.tzinfo else since.replace(tzinfo=UTC)
        return self._walk(cutoff=cutoff, inclusive=False, paginate=False)

    def backfill(self, depth_days: int | None = None) -> list[CollectedItem]:
        """Walk `?before=` backwards until the depth is covered.

        Every channel in the config carries `backfill_supported: false`, so
        this is the fallback path, not the daily one.
        """
        depth = self.source.backfill_depth_days if depth_days is None else depth_days
        cutoff = (
            datetime.now(UTC) - timedelta(days=depth) if depth and depth > 0 else None
        )
        return self._walk(cutoff=cutoff, inclusive=True, paginate=True)

    def _walk(
        self, cutoff: datetime | None, inclusive: bool, paginate: bool
    ) -> list[CollectedItem]:
        self.extra = {"pages_fetched": 0, "posts_seen": 0}
        if self.channel is None:
            self.extra["error"] = f"not a telegram channel url: {self.source.url!r}"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return []
        self.extra["channel"] = self.channel

        items: list[CollectedItem] = []
        seen: set[int] = set()
        seen_pages: set[int] = set()
        without_text = 0
        posts_seen = 0
        before: int | None = None

        for page in range(1, self.max_pages + 1):
            result = self._page(preview_url(self.channel, before))
            if result is None:
                break
            soup, page_bytes = result
            self.extra["pages_fetched"] = page
            if page == 1:
                self.extra["response_bytes"] = page_bytes
                # Present on a channel that has a preview, absent on the
                # 10 KB "view in Telegram" page a group or a user answers with.
                self.extra["has_history"] = (
                    soup.select_one(HISTORY_SELECTOR) is not None
                )

            nodes = soup.select(MESSAGE_SELECTOR)
            posts_seen += len(nodes)
            page_has_new = False
            reached_cutoff = False

            for node in nodes:
                ident = message_id(node)
                if ident is not None and ident in seen:
                    continue
                text = message_text(node)
                if not text:
                    # A photo with no caption carries no evidence, and an item
                    # with an empty raw_text can only produce unverifiable facts.
                    without_text += 1
                    continue
                published = message_datetime(node)
                if published is not None and cutoff is not None:
                    if published < cutoff or (not inclusive and published == cutoff):
                        reached_cutoff = True
                        continue
                if ident is not None:
                    seen.add(ident)
                page_has_new = True
                items.append(self._to_item(node, ident, text, published))

            if not paginate:
                break
            before = next_before(soup)
            if before is None or before in seen_pages:
                break  # end of history, or a page that points back at itself
            seen_pages.add(before)
            if reached_cutoff or not nodes:
                break
            if not page_has_new:
                break
        else:
            self.extra["truncated_at_max_pages"] = True

        self.extra["posts_seen"] = posts_seen
        if without_text:
            self.extra["posts_without_text"] = without_text
        if posts_seen == 0:
            self._diagnose_empty()

        items.sort(
            key=lambda item: (
                item.published_at is not None,
                item.published_at or datetime.min.replace(tzinfo=UTC),
            ),
            reverse=True,
        )
        return items

    def _page(self, url: str) -> tuple[BeautifulSoup, int] | None:
        try:
            result = self.fetcher.get(url)
        except Exception as exc:  # a broken source is data, not a crash (FR-1.5)
            self.extra["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return None
        if not result.ok:
            self.extra["http_status"] = result.status_code
            self.extra["error"] = result.error or f"HTTP {result.status_code}"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return None
        text = result.text or ""
        try:
            soup = BeautifulSoup(text, "lxml")
        except Exception as exc:
            self.extra["error"] = f"unparseable preview page: {type(exc).__name__}"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return None
        # Items carry the ref of the page they were read from, so evidence is
        # verified against the exact archived response.
        self._last_ref = result.ref
        return soup, len(text)

    def _diagnose_empty(self) -> None:
        """Zero posts is valid, but never silent (FR-1.4)."""
        if "error" in self.extra:
            return
        self.extra["empty"] = True
        size = self.extra.get("response_bytes", 0)
        if self.extra.get("has_history"):
            self.extra["error"] = f"preview page carries no posts ({size} bytes)"
        else:
            # No history block at all: a private channel, a group, or a user.
            self.extra["no_public_preview"] = True
            self.extra["error"] = (
                f"no public web preview for @{self.channel} (HTTP 200, {size} bytes)"
            )
        log.warning("%s: %s", self.source.id, self.extra["error"])

    def _to_item(
        self,
        node: Tag,
        ident: int | None,
        text: str,
        published: datetime | None,
    ) -> CollectedItem:
        channel = self.channel or ""
        extra: dict[str, Any] = {
            "source_id": self.source.id,
            "channel": channel,
            # SRC-2 is enforced upstream of this adapter; the mark is what
            # lets it be enforced at all.
            "requires_corroboration": True,
        }
        if ident is not None:
            extra["message_id"] = ident
        if published is None:
            extra["date_missing"] = True
        views = node.select_one(".tgme_widget_message_views")
        if views is not None:
            extra["views"] = views.get_text(strip=True)
        forwarded = node.select_one(".tgme_widget_message_forwarded_from_name")
        if forwarded is not None:
            extra["forwarded_from"] = forwarded.get_text(" ", strip=True)

        return CollectedItem(
            url=message_link(node, channel, ident),
            title=_first_line(text) or f"@{channel}",
            raw_text=text,
            published_at=published,
            event_date=published.date() if published else None,
            date_precision=(DatePrecision.DAY if published else DatePrecision.INFERRED),
            raw_material_ref=self._last_ref,
            extra=extra,
        )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if stripped:
            return stripped[:MAX_TITLE_CHARS].strip()
    return ""
