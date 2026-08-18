"""Disk caches for network and model calls.

Both exist from the first line of code rather than as an optimization added
later. Network is paid for once, so re-parsing costs nothing and a rate limit
cannot interrupt a backfill twice. Model responses are keyed by their full
input, so fixing a bug downstream replays the backfill in seconds instead of
tens of dollars.

The cache key of a fetched document doubles as `raw_material_ref` on every
EventStatement: the archived text is what evidence is verified against, so it
has to stay addressable.

What the HTTP half of the cache is *not* allowed to do is stand in for the
network. A product whose whole claim is "this page changed today" cannot
answer that question from yesterday's bytes: without an expiry and without
validators, `get` returned the archive forever, the first-sighting gate said
"seen", and every morning was quiet by construction. So an archived response
carries the two things that make a later request conditional — `etag` and
`last_modified` — plus the moment it was written, and the fetcher asks the
server whether the copy still stands rather than assuming it does.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


# Exact parameter names, plus one prefix family. Matching on a bare "ref"
# prefix would also eat reference= and refresh=, collapsing two different
# documents onto one cache key and therefore onto one raw_material_ref.
_TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "yclid", "igshid", "_ga"}
)
_TRACKING_PREFIXES = ("utm_",)


def canonical_url(url: str, keep_fragment: bool = False) -> str:
    """Strip the parts that do not change what a page says.

    `keep_fragment` matters more than it looks. One fetched page can carry
    dozens of distinct events, each addressed by its own anchor: every entry
    of the Anthropic release-notes feed differs only by `#august-11-2026`, and
    section-level items from html_page are addressed the same way. Dropping
    the fragment is right for the HTTP cache, where the whole page is one
    request, and wrong for deduplication, where it would collapse a hundred
    events into one material.
    """
    url = url.strip()
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc and parts.path:
        # "example.com/path" would otherwise keep the host inside path and
        # urlunsplit would emit "https:example.com/path".
        parts = urlsplit(f"https://{url}")
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if (scheme == "https" and netloc.endswith(":443")) or (
        scheme == "http" and netloc.endswith(":80")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    path = parts.path.rstrip("/") or "/"

    def is_tracking(pair: str) -> bool:
        name = pair.split("=", 1)[0].lower()
        return name in _TRACKING_PARAMS or name.startswith(_TRACKING_PREFIXES)

    query = "&".join(
        q for q in sorted(parts.query.split("&")) if q and not is_tracking(q)
    )
    fragment = parts.fragment if keep_fragment else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def header_value(headers: Mapping[str, Any] | None, name: str) -> str:
    """One header by name, whatever case the server or an older run used.

    httpx lowercases what it hands over, but the archive on disk is years of
    accumulated writes and nothing guarantees every entry in it was written
    by today's code. A validator missed because of a capital E is a
    conditional request that never happens.
    """
    if not headers:
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value).strip()
    return ""


def digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(
            (
                part
                if isinstance(part, str)
                else json.dumps(part, sort_keys=True, default=str)
            ).encode()
        )
        h.update(b"\x00")
    return h.hexdigest()


class CacheStore:
    """Content-addressed JSON store on disk."""

    def __init__(self, root: str | Path, namespace: str) -> None:
        self.root = Path(root) / namespace
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A truncated file from an interrupted write is a miss, not a crash.
            self.misses += 1
            return None
        self.hits += 1
        return payload

    def put(self, key: str, value: dict[str, Any]) -> str:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"cached_at": time.time(), **value}, ensure_ascii=False, default=str
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
        return key

    @staticmethod
    def age(entry: Mapping[str, Any] | None) -> float | None:
        """Seconds since the entry was written, or None if it cannot say.

        An entry that cannot say how old it is counts as stale everywhere it
        is asked. That is the safe direction: the cost of being wrong is one
        conditional request, and the cost of the other direction is a day of
        news nobody saw.
        """
        if not entry:
            return None
        stamp = entry.get("cached_at")
        if not isinstance(stamp, (int, float, str)):
            return None
        try:
            return max(0.0, time.time() - float(stamp))
        except (TypeError, ValueError):
            return None

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses}


# Stable across processes: an object() sentinel would serialize to its
# memory address and change the key on every run.
_ABSENT = "\x00absent"


class HttpCache(CacheStore):
    def __init__(self, root: str | Path) -> None:
        super().__init__(root, "http")

    @staticmethod
    def key_for(url: str, extra: Any = None) -> str:
        # `extra` is a separate digest component rather than a URL fragment:
        # canonical_url drops fragments, so folding it into the URL made
        # paginated requests share one key and silently re-read page one.
        return digest(canonical_url(url), _ABSENT if extra is None else extra)

    @staticmethod
    def validators(entry: Mapping[str, Any] | None) -> dict[str, str]:
        """Headers that turn the next request into a question, not a download.

        Read from the top-level fields first and from the stored response
        headers second: entries written before this existed have no top-level
        fields, and there are two hundred of them on disk.
        """
        if not entry:
            return {}
        headers = entry.get("headers") or {}
        conditional: dict[str, str] = {}
        etag = str(entry.get("etag") or "").strip() or header_value(headers, "etag")
        if etag:
            conditional["If-None-Match"] = etag
        modified = str(entry.get("last_modified") or "").strip() or header_value(
            headers, "last-modified"
        )
        if modified:
            conditional["If-Modified-Since"] = modified
        return conditional

    @staticmethod
    def has_validators(entry: Mapping[str, Any] | None) -> bool:
        return bool(HttpCache.validators(entry))

    def store(
        self,
        key: str,
        url: str,
        status_code: int,
        text: str,
        headers: Mapping[str, str],
    ) -> str:
        """Archive a fresh response together with what revalidates it later."""
        return self.put(
            key,
            {
                "url": url,
                "status_code": status_code,
                "text": text,
                "headers": dict(headers),
                "etag": header_value(headers, "etag"),
                "last_modified": header_value(headers, "last-modified"),
            },
        )

    def revalidate(
        self, key: str, entry: Mapping[str, Any], headers: Mapping[str, str]
    ) -> dict[str, Any]:
        """Record a 304: the body stands, the clock restarts.

        The server was asked and said the copy is current, so the entry is as
        good as one downloaded this second — and it has to be stamped that way,
        or the next call would ask again a minute later. Validators can rotate
        on a 304, and headers that carry state of their own (rate limits, for
        one) are the fresh ones.
        """
        merged = {**(entry.get("headers") or {}), **dict(headers)}
        refreshed = dict(entry)
        refreshed.pop("cached_at", None)  # `put` stamps it; a leftover would win
        refreshed["headers"] = merged
        refreshed["etag"] = (
            header_value(headers, "etag")
            or str(entry.get("etag") or "")
            or header_value(entry.get("headers") or {}, "etag")
        )
        refreshed["last_modified"] = (
            header_value(headers, "last-modified")
            or str(entry.get("last_modified") or "")
            or header_value(entry.get("headers") or {}, "last-modified")
        )
        self.put(key, refreshed)
        refreshed["cached_at"] = time.time()
        return refreshed


class ModelCache(CacheStore):
    def __init__(self, root: str | Path) -> None:
        super().__init__(root, "model")

    @staticmethod
    def key_for(
        model: str,
        prompt: str,
        schema: Any = None,
        params: dict[str, Any] | None = None,
    ) -> str:
        # None, "" and {} must not collapse onto one key: an absent schema
        # and an empty one are different requests.
        return digest(
            model,
            prompt,
            _ABSENT if schema is None else schema,
            _ABSENT if params is None else params,
        )
