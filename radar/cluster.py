"""Stage 2: clustering and deduplication.

No model is involved. A model asked to group materials regroups them
differently tomorrow, and `cluster_id` has to be stable across runs: the whole
of pass A (`days_tracked`, `delta_status`) is a join on that key. A drifting
key does not raise an error, it quietly reports "third day running" about a
story it met an hour ago.

The key is therefore derived from the content: canonical URL first, and a
normalized title signature when two sources cover one event under different
headlines.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from radar.adapters.base import CollectedItem
from radar.cache import canonical_url, digest

# Words that carry no identity: dropping them lets "Anthropic releases Claude
# 4.5" and "Claude 4.5 released" collapse onto one signature.
_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "to",
    "of",
    "in",
    "on",
    "for",
    "and",
    "or",
    "with",
    "from",
    "at",
    "by",
    "as",
    "it",
    "its",
    "new",
    "now",
    "announces",
    "announced",
    "announcement",
    "release",
    "released",
    "releases",
    "update",
    "updated",
    "updates",
    "introducing",
    "introduces",
    "launch",
    "launches",
    "и",
    "в",
    "на",
    "с",
    "для",
    "от",
    "по",
    "о",
    "об",
    "не",
    "что",
    "как",
    "это",
    "анонс",
    "релиз",
    "выпуск",
    "обновление",
    "новый",
    "новая",
    "новое",
}

_VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+)+(?:[-.][0-9a-z]+)?\b", re.IGNORECASE)


def _tokens(title: str) -> list[str]:
    text = unicodedata.normalize("NFKC", title).casefold()
    text = re.sub(r"[^\w\s.-]", " ", text)
    raw = [t.strip(".-") for t in text.split()]
    return [t for t in raw if t and t not in _STOPWORDS and len(t) > 1]


def title_signature(title: str) -> str:
    """Order-independent fingerprint of the meaningful words in a headline.

    A version number is kept verbatim when present: it is usually the single
    most identifying token, and it keeps 1.62.0 from merging with 1.63.0.
    """
    tokens = _tokens(title)
    versions = sorted(set(_VERSION_RE.findall(title.casefold())))
    significant = sorted(set(tokens))[:8]
    return digest("|".join(versions), "|".join(significant))


def make_cluster_id(vendor: str | None, change_type: str | None, signature: str) -> str:
    """Deterministic across runs: same inputs always yield the same id."""
    return digest(vendor or "", change_type or "", signature)[:24]


@dataclass(slots=True)
class Cluster:
    cluster_id: str
    dedup_key: str
    items: list[CollectedItem] = field(default_factory=list)
    primary_index: int = 0
    vendor: str | None = None
    change_type: str | None = None
    seen_in: list[str] = field(default_factory=list)

    @property
    def primary(self) -> CollectedItem:
        return self.items[self.primary_index]

    @property
    def title(self) -> str:
        return self.primary.title

    @property
    def duplicates_count(self) -> int:
        """Materials collapsed into this one, excluding the primary (FR-2.4)."""
        return max(0, len(self.items) - 1)


def choose_primary(items: list[CollectedItem], priority_of: dict[str, int]) -> int:
    """Lowest source priority number wins (FR-2.2), earliest publication breaks ties."""

    def sort_key(pair: tuple[int, CollectedItem]) -> tuple[int, float, str]:
        index, item = pair
        priority = priority_of.get(str(item.extra.get("source_id", "")), 5)
        published = item.published_at.timestamp() if item.published_at else float("inf")
        return (priority, published, item.url)

    return min(enumerate(items), key=sort_key)[0]


def cluster_items(
    items: list[CollectedItem],
    priority_of: dict[str, int] | None = None,
    vendor_of: dict[str, str | None] | None = None,
    change_type_of: dict[str, str | None] | None = None,
) -> list[Cluster]:
    """Group materials describing one event.

    Two passes, both deterministic: identical canonical URLs merge first
    (FR-1.6), then titles sharing a signature within the same vendor and
    change type merge on top of that.
    """
    priority_of = priority_of or {}
    vendor_of = vendor_of or {}
    change_type_of = change_type_of or {}

    by_url: dict[str, list[CollectedItem]] = defaultdict(list)
    for item in items:
        by_url[canonical_url(item.url, keep_fragment=True)].append(item)

    groups: dict[tuple[str | None, str | None, str], list[CollectedItem]] = defaultdict(
        list
    )
    for url, url_items in by_url.items():
        head = url_items[0]
        vendor = vendor_of.get(url, head.vendor_hint)
        change_type = change_type_of.get(url)
        groups[(vendor, change_type, title_signature(head.title))].extend(url_items)

    clusters: list[Cluster] = []
    for (vendor, change_type, signature), group in groups.items():
        cluster_id = make_cluster_id(vendor, change_type, signature)
        seen = sorted(
            {
                str(i.extra.get("source_id", ""))
                for i in group
                if i.extra.get("source_id")
            }
        )
        clusters.append(
            Cluster(
                cluster_id=cluster_id,
                dedup_key=signature,
                items=group,
                primary_index=choose_primary(group, priority_of),
                vendor=vendor,
                change_type=change_type,
                seen_in=seen,
            )
        )
    # Stable output order so a rerun produces the same sequence.
    clusters.sort(key=lambda c: c.cluster_id)
    return clusters
