"""HTTP client with an on-disk cache.

Every fetch goes through the cache, so a backfill pays for the network once
and every later parse is free. `raw_material_ref` returned here is what an
EventStatement carries, and what evidence is later verified against.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from radar.cache import HttpCache, canonical_url

USER_AGENT = (
    "radar-daily-digest/0.1 (+https://github.com/vasilievyakov/radar-daily-digest)"
)


@dataclass(slots=True)
class FetchResult:
    url: str
    status_code: int
    text: str
    headers: dict[str, str]
    ref: str
    from_cache: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 300


class Fetcher:
    def __init__(
        self,
        cache_root: str | Path = "cache",
        timeout: float = 30.0,
        max_retries: int = 2,
        polite_delay: float = 1.0,
        client: httpx.Client | None = None,
        offline: bool = False,
    ) -> None:
        self.cache = HttpCache(cache_root)
        self.timeout = timeout
        self.max_retries = max_retries
        self.polite_delay = polite_delay
        # A miss must fail loudly rather than quietly reach the network:
        # a test claiming to run against the archive while fetching live
        # pages proves nothing about the archive.
        self.offline = offline
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        self._last_request_at: dict[str, float] = {}

    def _wait_for_domain(self, url: str) -> None:
        """Polite interval per domain (NFR-12)."""
        domain = httpx.URL(url).host or ""
        last = self._last_request_at.get(domain)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.polite_delay:
                time.sleep(self.polite_delay - elapsed)
        self._last_request_at[domain] = time.monotonic()

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        force: bool = False,
        cache_key_extra: Any = None,
    ) -> FetchResult:
        key = HttpCache.key_for(url, cache_key_extra)
        if not force:
            cached = self.cache.get(key)
            if cached is not None:
                return FetchResult(
                    url=cached["url"],
                    status_code=cached["status_code"],
                    text=cached["text"],
                    headers=cached.get("headers", {}),
                    ref=key,
                    from_cache=True,
                )

        if self.offline:
            return FetchResult(
                url=canonical_url(url),
                status_code=0,
                text="",
                headers={},
                ref=key,
                from_cache=False,
                error=f"offline: страницы нет в архиве ({canonical_url(url)})",
            )

        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self._wait_for_domain(url)
                response = self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code >= 500:
                    # Never archived: a cached 500 would replay a one-off
                    # outage from disk on every later run, and no amount of
                    # retrying inside a single run would notice.
                    last_error = f"HTTP {response.status_code}"
                    if attempt >= self.max_retries:
                        return FetchResult(
                            url=canonical_url(url),
                            status_code=response.status_code,
                            text=response.text,
                            headers=dict(response.headers),
                            ref=key,
                            from_cache=False,
                            error=last_error,
                        )
                else:
                    self.cache.put(
                        key,
                        {
                            "url": canonical_url(url),
                            "status_code": response.status_code,
                            "text": response.text,
                            "headers": dict(response.headers),
                        },
                    )
                    return FetchResult(
                        url=canonical_url(url),
                        status_code=response.status_code,
                        text=response.text,
                        headers=dict(response.headers),
                        ref=key,
                        from_cache=False,
                    )
            if attempt < self.max_retries:
                time.sleep(2**attempt)  # exponential backoff (FR-1.5)

        return FetchResult(
            url=canonical_url(url),
            status_code=0,
            text="",
            headers={},
            ref=key,
            from_cache=False,
            error=last_error or "unknown error",
        )

    def read_cached(self, ref: str) -> str | None:
        """Archived text behind a raw_material_ref, for evidence verification."""
        payload = self.cache.get(ref)
        return payload["text"] if payload else None

    def close(self) -> None:
        self._client.close()
