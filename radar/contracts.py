"""Contracts between stage 4 and everything that calls it.

Written before the stages themselves so that enrichment, backfill and the
daily run can be built against one shape instead of three guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from radar.adapters.base import CollectedItem, SourceConfig
from radar.models import ChangeType, EventStatement, Fact

# Bumped whenever the extraction prompt changes in a way that would alter
# output. Recorded on every EventStatement so the corpus stays auditable and
# can be selectively rebuilt rather than wholesale.
# Bumped on 2026-08-17 after a live run exposed three extraction defects:
# one announcement retiring nine snapshots produced nine near-identical
# events; lifecycle tables without an announcement date had the collection
# date substituted; and the date guard, written against sonnet, zeroed valid
# dates once the stage moved to haiku. Records made before the fixes are a
# different generation and must be distinguishable from the corpus itself.
EXTRACTION_PROMPT_VERSION = "extract-v2"


@dataclass(slots=True)
class RejectedFact:
    """A fact the model produced and the verifier refused to publish."""

    kind: str
    value: str
    evidence: str
    reason: str


@dataclass(slots=True)
class EnrichResult:
    """Outcome of enriching exactly one collected material."""

    source_id: str
    url: str
    statements: list[EventStatement] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    rejected_facts: list[RejectedFact] = field(default_factory=list)
    change_type: ChangeType | None = None
    cost_usd: float = 0.0
    cached: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def kept_ratio(self) -> float:
        total = len(self.facts) + len(self.rejected_facts)
        return len(self.facts) / total if total else 0.0


class Enricher(Protocol):
    """Stage 4. Never raises: a broken material becomes a recorded outcome."""

    def enrich(self, item: CollectedItem, source: SourceConfig) -> EnrichResult: ...
