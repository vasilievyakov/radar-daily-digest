"""Disk caches for network and model calls.

Both exist from the first line of code rather than as an optimization added
later. Network is paid for once, so re-parsing costs nothing and a rate limit
cannot interrupt a backfill twice. Model responses are keyed by their full
input, so fixing a bug downstream replays the backfill in seconds instead of
tens of dollars.

The cache key of a fetched document doubles as `raw_material_ref` on every
EventStatement: the archived text is what evidence is verified against, so it
has to stay addressable.
"""

from __future__ import annotations

import hashlib
import json
import time
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
