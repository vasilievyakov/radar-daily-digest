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


def canonical_url(url: str) -> str:
    """Strip the parts that do not change what a page says."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if (scheme == "https" and netloc.endswith(":443")) or (
        scheme == "http" and netloc.endswith(":80")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    path = parts.path.rstrip("/") or "/"
    tracking = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "ref")
    query = "&".join(
        q
        for q in sorted(parts.query.split("&"))
        if q and not any(q.lower().startswith(t) for t in tracking)
    )
    return urlunsplit((scheme, netloc, path, query, ""))


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


class HttpCache(CacheStore):
    def __init__(self, root: str | Path) -> None:
        super().__init__(root, "http")

    @staticmethod
    def key_for(url: str) -> str:
        return digest(canonical_url(url))


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
        return digest(model, prompt, schema or "", params or {})
