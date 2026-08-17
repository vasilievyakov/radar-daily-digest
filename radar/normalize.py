"""Metadata normalization against the config dictionary.

Second irreversible decision in the agent notes: `anthropic`, `Anthropic` and
`claude` arriving as three distinct values break retrieval filters (FR-6.13),
and the damage only shows up once the corpus is already collected. Every path
into the corpus goes through here.

FR-5.16 makes vendor and change_type mandatory. An unrecognized vendor is
therefore not silently dropped: it is reported so the dictionary can be
extended before the backfill runs, not after.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from radar.models import ChangeType


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9а-яё]+", "", value)


@dataclass(slots=True)
class Normalizer:
    vendor_by_alias: dict[str, str] = field(default_factory=dict)
    vendor_labels: dict[str, str] = field(default_factory=dict)
    change_types: set[str] = field(default_factory=set)
    unknown_vendors: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(
        cls, vendors: list[dict[str, Any]], change_types: list[dict[str, Any]]
    ):
        by_alias: dict[str, str] = {}
        labels: dict[str, str] = {}
        for vendor in vendors:
            vid = vendor["id"]
            labels[vid] = vendor.get("label", vid)
            for alias in [vid, vendor.get("label", vid), *vendor.get("aliases", [])]:
                if alias:
                    by_alias[_fold(str(alias))] = vid
        return cls(
            vendor_by_alias=by_alias,
            vendor_labels=labels,
            change_types={c["id"] for c in change_types},
        )

    def vendor(self, raw: str | None) -> str | None:
        """Map any spelling to a dictionary id, or record it as unknown."""
        if not raw or not raw.strip():
            return None
        key = _fold(raw)
        if not key:
            return None
        if key in self.vendor_by_alias:
            return self.vendor_by_alias[key]
        # Longest alias contained in the string, so "Anthropic Claude Code"
        # resolves without needing an entry of its own.
        candidates = [alias for alias in self.vendor_by_alias if alias and alias in key]
        if candidates:
            return self.vendor_by_alias[max(candidates, key=len)]
        self.unknown_vendors[raw.strip()] = self.unknown_vendors.get(raw.strip(), 0) + 1
        return None

    def vendor_from_url(self, url: str) -> str | None:
        """Fallback when the text names no vendor but the domain does."""
        host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
        parts = [
            p
            for p in host.split(".")
            if p
            not in {"www", "com", "org", "io", "ai", "dev", "docs", "platform", "api"}
        ]
        for part in parts:
            resolved = self.vendor_by_alias.get(_fold(part))
            if resolved:
                return resolved
        return None

    def label(self, vendor_id: str | None) -> str:
        return self.vendor_labels.get(vendor_id or "", vendor_id or "")

    def change_type(self, raw: str | None) -> ChangeType:
        """Unknown values fall back to `other` rather than to an empty field."""
        if not raw:
            return ChangeType.OTHER
        key = _fold(raw)
        for known in self.change_types:
            if _fold(known) == key:
                return ChangeType(known)
        synonyms = {
            "breaking": ChangeType.BREAKING_CHANGE,
            "breakingchange": ChangeType.BREAKING_CHANGE,
            "deprecated": ChangeType.DEPRECATION,
            "deprecate": ChangeType.DEPRECATION,
            "sunset": ChangeType.DEPRECATION,
            "retirement": ChangeType.DEPRECATION,
            "price": ChangeType.PRICING,
            "prices": ChangeType.PRICING,
            "cost": ChangeType.PRICING,
            "ratelimit": ChangeType.LIMITS,
            "ratelimits": ChangeType.LIMITS,
            "quota": ChangeType.LIMITS,
            "quotas": ChangeType.LIMITS,
            "limit": ChangeType.LIMITS,
            "cve": ChangeType.SECURITY,
            "vulnerability": ChangeType.SECURITY,
            "patch": ChangeType.SECURITY,
            "released": ChangeType.RELEASE,
            "version": ChangeType.RELEASE,
        }
        return synonyms.get(key, ChangeType.OTHER)

    def report_unknown(self) -> list[tuple[str, int]]:
        """Unrecognized spellings, most frequent first.

        Written to the run log so the dictionary can be widened before the
        corpus is built rather than after filtering silently misses records.
        """
        return sorted(self.unknown_vendors.items(), key=lambda kv: -kv[1])
