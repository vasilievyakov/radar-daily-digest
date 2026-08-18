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

# Above this, an input is a document rather than a vendor name.
MAX_TOKENS_FOR_SUBSTRING_MATCH = 6


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
        # Substring matching only for short inputs — a name, not a document.
        # Folding strips separators, so on a full release body "github.com" in
        # any link matches the vendor id "github". Long text must get its
        # vendor from the source config instead of from a guess.
        if len(raw.split()) <= MAX_TOKENS_FOR_SUBSTRING_MATCH:
            candidates = [a for a in self.vendor_by_alias if a and a in key]
            if candidates:
                return self.vendor_by_alias[max(candidates, key=len)]
            # A short unrecognized name is a dictionary gap worth reporting.
            # A whole document is not: it would fill the report with noise.
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


# --------------------------------------------------------------------------
# event identity
# --------------------------------------------------------------------------

# A model identifier as vendors print it: a name with a version or a date
# glued on. Two rows of the same page describing one retirement — one in the
# status table, one in the deprecation table — agree on this and on nothing
# else, so it is what identity has to be built from.
_MODEL_IDENT = re.compile(
    r"\b[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*-(?:\d[\w.]*|[a-z]+-?\d[\w.]*)\b", re.I
)


# The same model written the way a sentence writes it: "Claude Sonnet 5",
# "Gemini 2.5 Pro". The extractor fills `product` from the prose as often as
# from the API name, and two cards about one price change carried
# "Claude Sonnet 5" and "claude-sonnet-5" as different subjects.
_SPACED_NAME = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\s+(\d[\w.]*)\b"
)


def _hyphenate(text: str | None) -> str:
    """Turn "Claude Sonnet 5" into "claude-sonnet-5", leave the rest alone."""
    if not text:
        return ""
    return _SPACED_NAME.sub(
        lambda m: (m.group(1) + "-" + m.group(2)).replace(" ", "-"), text
    )


def model_identifiers(*texts: str | None) -> list[str]:
    """Model names in the order they appear, folded, without repeats.

    Order carries meaning and sorting destroys it. A deprecation table reads
    "April 20, 2026 | claude-3-haiku-20240307 | claude-haiku-4-5-20251001":
    the first name is what is being retired, the second is what to move to.
    Sorted, the retirement of one model and the retirement of another become
    indistinguishable from each other.
    """
    found: list[str] = []
    for text in texts:
        for match in _MODEL_IDENT.findall(_hyphenate(text)):
            token = match.casefold()
            if token not in found:
                found.append(token)
    return found


def subject_identity(
    vendor: str,
    change_type: str,
    product: str | None = None,
    evidence: str | None = None,
    text: str | None = None,
) -> str:
    """What the reader counts as one thing happening.

    Weaker than `event_identity` by one field, and the field is the date. A
    lifecycle table states two milestones for one retirement — Anthropic's
    page says `claude-3-haiku-20240307` was deprecated on February 19 and
    retired on April 20 — and both are true, so the corpus keeps both. A
    digest that prints both prints one model twice.

    So: the corpus is unique by event, the digest is unique by subject, and
    the precedent count is a count of subjects. Otherwise "the fourth time
    since February" counts the milestones of one deprecation.
    """
    named = (
        model_identifiers(product)
        or model_identifiers(evidence)
        or model_identifiers(text)
    )
    subject = named[0] if named else _fold(text or "")[:120]
    return "|".join((_fold(vendor), _fold(change_type), subject))


def event_identity(
    vendor: str,
    change_type: str,
    event_date: Any,
    product: str | None = None,
    evidence: str | None = None,
    text: str | None = None,
) -> str:
    """What makes two statements the same event, and nothing more.

    The corpus is append-only and its records are how the context label counts
    ("the third time since May"). A page that prints one retirement twice —
    Anthropic's status table and its deprecation table both name
    `claude-3-haiku-20240307` — put two precedents behind one event, and the
    number on the card counted rows rather than events.

    Identity is the named subject, not the wording: two extractions of the
    same row differ in every word and agree on the model name. When no model
    name can be found the statement's own text is used, which never merges two
    events by accident — a duplicate slipping through costs one repeated card,
    a false merge silently deletes an event.
    """
    # The subject is the first name in the most specific field that has one:
    # the extractor's own `product` when it filled it, otherwise the quote,
    # otherwise the statement. Only the first — the names after it are
    # replacements and successors, which differ between two readings of one
    # row and would split the event in two.
    named = (
        model_identifiers(product)
        or model_identifiers(evidence)
        or model_identifiers(text)
    )
    subject = named[0] if named else _fold(text or "")[:120]
    when = getattr(event_date, "isoformat", lambda: str(event_date or ""))()
    return "|".join((_fold(vendor), _fold(change_type), when, subject))
