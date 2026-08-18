"""HTTP client with an on-disk cache that is not allowed to impersonate it.

The cache stays: a backfill pays for the network once, tests replay the
archive offline, and `raw_material_ref` — the key returned here — is what
evidence is later verified against.

What changed is what the cache is permitted to answer. It used to answer
everything. `get` looked on disk first, found yesterday's bytes, and returned
them as though a source had been checked: eight sources answering in under six
milliseconds, two hundred and thirty files on disk, not one of them written by
today's run. Downstream that reads as a working morning — the first-sighting
gate sees nothing new, every source is `quiet`, the digest is empty — and it
would read that way tomorrow too. A radar whose job is to notice that a page
changed had been built so that it cannot notice.

So the archive now answers only when it is entitled to:

* inside `cache_ttl` it answers without a request, and says so (`ARCHIVE`);
* past it the fetcher asks the server, conditionally when the entry carries
  `ETag` or `Last-Modified`. HTTP 304 is a real answer from the network, not a
  skipped request, and it comes back as `REVALIDATED` with the archived body
  and the archived status — 304 is a statement about the copy, not a failure,
  and adapters must keep seeing the 200 they parse;
* a body that actually moved comes back as `NETWORK`;
* `offline=True` is unchanged: archive or a loud miss, never a socket.

Every result states which of those happened, and `record()` lets the collector
add them up per source so the run log can say whether the network was involved
at all. A log that cannot tell a checked source from a replayed one is a log
that reported eight successful checks on a day nothing was checked.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from radar.cache import HttpCache, canonical_url

USER_AGENT = (
    "radar-daily-digest/0.1 (+https://github.com/vasilievyakov/radar-daily-digest)"
)

# How long an archived response may be served without asking anyone.
#
# Thirty seconds, and the shortness is the whole policy. The window exists for
# exactly one job: collapsing the repeated reads of the same URL that a single
# collection makes — the collector re-reads a page unwindowed when the windowed
# read came back empty, an adapter re-parses after a retry — into one request.
# Those happen within the same second, so the window only has to survive a slow
# page, not a working day.
#
# What it must never be is long enough to span two collections. A window that
# does turns "check this source" back into "read the disk", which is the defect
# itself: at an hour, a re-run five minutes later reports every source as
# checked with no packet leaving the machine. Cheapness is not a reason to skip
# the question — a conditional request that ends in 304 costs a few hundred
# bytes and buys the one fact the product sells.
DEFAULT_CACHE_TTL = 30.0

# HTTP 304 without a body, the answer a conditional request hopes for.
NOT_MODIFIED = 304


class FetchOrigin(StrEnum):
    """Where the bytes in a result came from, and whether anyone was asked."""

    # Downloaded now. The body may or may not differ from the archived one;
    # what is certain is that a server produced it during this run.
    NETWORK = "network"
    # Asked, and the server answered 304: the archived body is current. The
    # body is old, the *answer* is not, and the difference is the product.
    REVALIDATED = "revalidated"
    # Read from disk with no request made. Legitimate — inside the TTL, or
    # deliberately offline — and never to be reported as a check.
    ARCHIVE = "archive"
    # Offline and not in the archive: no body, no request, a loud failure.
    OFFLINE = "offline"


# Per-source verdicts, in the order they outrank each other.
NETWORK_FRESH = "fresh"
NETWORK_REVALIDATED = "revalidated"
NETWORK_FAILED = "failed"
NETWORK_ARCHIVE = "archive"
NETWORK_OFFLINE = "offline"
NETWORK_NONE = ""


@dataclass(slots=True)
class FetchResult:
    url: str
    status_code: int
    text: str
    headers: dict[str, str]
    ref: str
    # "the body came off the disk". Kept because adapters and their tests were
    # written against it; `origin` is the field that carries the whole truth,
    # and the two are reconciled below so neither can drift.
    from_cache: bool = False
    error: str | None = None
    origin: FetchOrigin = FetchOrigin.NETWORK
    # What the wire actually said, when that differs from `status_code`. A
    # revalidated page reports 200 to its adapter and 304 here: the first is
    # what the body is, the second is what happened.
    network_status: int | None = None

    def __post_init__(self) -> None:
        if self.from_cache and self.origin is FetchOrigin.NETWORK:
            # A caller that only knows about `from_cache` means the archive.
            self.origin = FetchOrigin.ARCHIVE
        self.from_cache = self.origin in (
            FetchOrigin.ARCHIVE,
            FetchOrigin.REVALIDATED,
        )

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 300

    @property
    def requested(self) -> bool:
        """True when a request left the process, whatever came back."""
        return self.origin in (FetchOrigin.NETWORK, FetchOrigin.REVALIDATED)

    @property
    def unchanged(self) -> bool:
        """The network was asked and said the archived copy still stands."""
        return self.origin is FetchOrigin.REVALIDATED


@dataclass(slots=True)
class FetchTally:
    """What the network did across a group of fetches — one source, usually."""

    fresh: int = 0
    revalidated: int = 0
    archive: int = 0
    offline: int = 0
    failed: int = 0

    def add(self, result: FetchResult) -> None:
        if result.origin is FetchOrigin.OFFLINE:
            self.offline += 1
        elif result.origin is FetchOrigin.ARCHIVE:
            self.archive += 1
        elif result.origin is FetchOrigin.REVALIDATED:
            self.revalidated += 1
        elif result.error is not None:
            self.failed += 1
        else:
            self.fresh += 1

    def merge(self, other: FetchTally) -> None:
        self.fresh += other.fresh
        self.revalidated += other.revalidated
        self.archive += other.archive
        self.offline += other.offline
        self.failed += other.failed

    @property
    def requests(self) -> int:
        """Requests that left the process, successful or not."""
        return self.fresh + self.revalidated + self.failed

    @property
    def reads(self) -> int:
        return self.requests + self.archive + self.offline

    @property
    def label(self) -> str:
        """The strongest thing that can honestly be said about this group.

        Ordered by how much of an answer it is. One fresh download makes the
        source checked whatever else happened; a source that only ever read
        the disk was not checked, and no count of milliseconds should suggest
        otherwise.
        """
        if self.fresh:
            return NETWORK_FRESH
        if self.revalidated:
            return NETWORK_REVALIDATED
        if self.failed:
            return NETWORK_FAILED
        if self.archive:
            return NETWORK_ARCHIVE
        if self.offline:
            return NETWORK_OFFLINE
        return NETWORK_NONE


class Fetcher:
    def __init__(
        self,
        cache_root: str | Path = "cache",
        timeout: float = 30.0,
        max_retries: int = 2,
        polite_delay: float = 1.0,
        client: httpx.Client | None = None,
        offline: bool = False,
        cache_ttl: float | None = DEFAULT_CACHE_TTL,
    ) -> None:
        self.cache = HttpCache(cache_root)
        self.timeout = timeout
        self.max_retries = max_retries
        self.polite_delay = polite_delay
        # A miss must fail loudly rather than quietly reach the network:
        # a test claiming to run against the archive while fetching live
        # pages proves nothing about the archive.
        self.offline = offline
        # None disables expiry entirely — the old behaviour, kept only for a
        # caller that explicitly wants a frozen archive.
        self.cache_ttl = cache_ttl
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        self._last_request_at: dict[str, float] = {}
        # Thread-local: `collect_all` runs a dozen sources through one fetcher
        # in a pool, and a shared counter would attribute another source's
        # download to whoever asked first.
        self._recording = threading.local()

    # -- accounting --------------------------------------------------------

    @contextmanager
    def record(self) -> Iterator[FetchTally]:
        """Tally every fetch made by this thread inside the block.

        The collector wraps one source in it, so the run log can say whether
        that source was checked or replayed. Nested blocks all see the fetches
        made inside them.
        """
        tally = FetchTally()
        stack = getattr(self._recording, "stack", None)
        if stack is None:
            stack = []
            self._recording.stack = stack
        stack.append(tally)
        try:
            yield tally
        finally:
            stack.pop()

    def _observe(self, result: FetchResult) -> FetchResult:
        for tally in getattr(self._recording, "stack", ()):
            tally.add(result)
        return result

    # -- politeness --------------------------------------------------------

    def _wait_for_domain(self, url: str) -> None:
        """Polite interval per domain (NFR-12)."""
        domain = httpx.URL(url).host or ""
        last = self._last_request_at.get(domain)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.polite_delay:
                time.sleep(self.polite_delay - elapsed)
        self._last_request_at[domain] = time.monotonic()

    # -- results built from the archive ------------------------------------

    @staticmethod
    def _archived(
        entry: Mapping[str, Any], key: str, origin: FetchOrigin, wire: int | None = None
    ) -> FetchResult:
        return FetchResult(
            url=str(entry.get("url") or ""),
            status_code=int(entry.get("status_code") or 0),
            text=str(entry.get("text") or ""),
            headers=dict(entry.get("headers") or {}),
            ref=key,
            origin=origin,
            network_status=wire,
        )

    def _serve_from_archive(self, entry: Mapping[str, Any], key: str) -> FetchResult:
        return self._archived(entry, key, FetchOrigin.ARCHIVE)

    @staticmethod
    def _still_current(entry: Mapping[str, Any] | None, ttl: float | None) -> bool:
        """May this entry answer without anyone being asked?

        `ttl=None` means the archive never expires, which is what the fetcher
        did for every entry before and is now an opt-in. An entry that cannot
        say how old it is is stale: one extra conditional request is cheaper
        than a day of unseen news.
        """
        if entry is None:
            return False
        if ttl is None:
            return True
        age = HttpCache.age(entry)
        return age is not None and age < ttl

    # -- the fetch ---------------------------------------------------------

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        force: bool = False,
        cache_key_extra: Any = None,
        max_age: float | None = None,
    ) -> FetchResult:
        """Fetch `url`, stating honestly whether the network was involved.

        `max_age` overrides the fetcher's TTL for one call; `max_age=0` means
        "ask, however recently we looked". `force=True` goes further and drops
        the validators too, downloading the page whatever the server thinks.
        """
        key = HttpCache.key_for(url, cache_key_extra)
        entry = None if force else self.cache.get(key)

        if self.offline:
            if entry is not None:
                return self._observe(self._serve_from_archive(entry, key))
            return self._observe(
                FetchResult(
                    url=canonical_url(url),
                    status_code=0,
                    text="",
                    headers={},
                    ref=key,
                    origin=FetchOrigin.OFFLINE,
                    error=f"offline: страницы нет в архиве ({canonical_url(url)})",
                )
            )

        ttl = self.cache_ttl if max_age is None else max_age
        if self._still_current(entry, ttl):
            assert entry is not None  # narrowed by _still_current
            return self._observe(self._serve_from_archive(entry, key))

        request_headers = dict(headers or {})
        for name, value in HttpCache.validators(entry).items():
            # setdefault: a caller that built its own conditional request
            # knows something we do not.
            request_headers.setdefault(name, value)

        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self._wait_for_domain(url)
                response = self._client.get(url, headers=request_headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == NOT_MODIFIED:
                    if entry is None:
                        # 304 to a request that carried no validator. Nothing
                        # to serve, and retrying with the same headers would
                        # get the same nothing.
                        return self._observe(
                            FetchResult(
                                url=canonical_url(url),
                                status_code=response.status_code,
                                text="",
                                headers=dict(response.headers),
                                ref=key,
                                origin=FetchOrigin.NETWORK,
                                network_status=response.status_code,
                                error="HTTP 304 без архивной копии",
                            )
                        )
                    refreshed = self.cache.revalidate(
                        key, entry, dict(response.headers)
                    )
                    return self._observe(
                        self._archived(
                            refreshed,
                            key,
                            FetchOrigin.REVALIDATED,
                            wire=response.status_code,
                        )
                    )
                if 400 <= response.status_code < 500:
                    # Not retried, and — this is the change — not archived
                    # either. A cached 404 is worse than a cached 500 because
                    # it is stable and therefore invisible: Azure moved its
                    # retirement schedule, the 404 went into the cache, and
                    # the source then reported a one-millisecond answer with
                    # no records, which on the page looks exactly like a
                    # source that is simply quiet today.
                    return self._observe(
                        FetchResult(
                            url=canonical_url(url),
                            status_code=response.status_code,
                            text=response.text,
                            headers=dict(response.headers),
                            ref=key,
                            origin=FetchOrigin.NETWORK,
                            network_status=response.status_code,
                            error=f"HTTP {response.status_code}",
                        )
                    )
                if response.status_code >= 500:
                    # Never archived: a cached 500 would replay a one-off
                    # outage from disk on every later run, and no amount of
                    # retrying inside a single run would notice.
                    last_error = f"HTTP {response.status_code}"
                    if attempt >= self.max_retries:
                        return self._observe(
                            FetchResult(
                                url=canonical_url(url),
                                status_code=response.status_code,
                                text=response.text,
                                headers=dict(response.headers),
                                ref=key,
                                origin=FetchOrigin.NETWORK,
                                network_status=response.status_code,
                                error=last_error,
                            )
                        )
                else:
                    self.cache.store(
                        key,
                        canonical_url(url),
                        response.status_code,
                        response.text,
                        dict(response.headers),
                    )
                    return self._observe(
                        FetchResult(
                            url=canonical_url(url),
                            status_code=response.status_code,
                            text=response.text,
                            headers=dict(response.headers),
                            ref=key,
                            origin=FetchOrigin.NETWORK,
                            network_status=response.status_code,
                        )
                    )
            if attempt < self.max_retries:
                time.sleep(2**attempt)  # exponential backoff (FR-1.5)

        # Every attempt failed. The archive is not a fallback here: a source
        # that did not answer is a source that did not answer, and handing back
        # yesterday's page under a green status is the failure this module was
        # rewritten to stop.
        return self._observe(
            FetchResult(
                url=canonical_url(url),
                status_code=0,
                text="",
                headers={},
                ref=key,
                origin=FetchOrigin.NETWORK,
                error=last_error or "unknown error",
            )
        )

    def read_cached(self, ref: str) -> str | None:
        """Archived text behind a raw_material_ref, for evidence verification."""
        payload = self.cache.get(ref)
        return payload["text"] if payload else None

    def close(self) -> None:
        self._client.close()
