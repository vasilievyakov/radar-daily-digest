"""Adapter contract.

Every adapter returns the same shape regardless of source type (FR-1.2), so
stages downstream never learn where a material came from. Adding a source is
a config edit (SRC-3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from radar.fetch import Fetcher
from radar.models import DatePrecision


@dataclass(slots=True)
class CollectedItem:
    """One material as an adapter sees it, before any model touches it."""

    url: str
    title: str
    raw_text: str
    published_at: datetime | None = None
    event_date: date | None = None
    date_precision: DatePrecision = DatePrecision.DAY
    raw_material_ref: str = ""
    vendor_hint: str | None = None
    version_hint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceConfig:
    id: str
    type: str
    url: str
    priority: int = 5
    enabled: bool = True
    parser_hint: str | None = None
    backfill_supported: bool = False
    backfill_depth_days: int = 0
    min_expected_items: int = 1
    # Vendor this source speaks for. Authoritative: we know that
    # docs.mistral.ai is Mistral, and guessing it from body text is how a Zed
    # release ends up filed under GitHub because it links to a pull request.
    vendor: str | None = None
    # Human name for the run log and the digest footer. Without it a slug is
    # all any surface can print, however carefully it capitalises.
    label: str | None = None
    # Some archives are frozen: worth backfilling, pointless to poll daily.
    live_collect: bool = True
    # Pages that only expose history through a per-month URL.
    backfill_url_template: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceConfig:
        known = {f for f in cls.__slots__}
        return cls(**{k: v for k, v in data.items() if k in known})


class Adapter(ABC):
    """Adapters never raise on a bad source: the collector isolates failures."""

    type: str = ""

    def __init__(self, source: SourceConfig, fetcher: Fetcher) -> None:
        self.source = source
        self.fetcher = fetcher

    @abstractmethod
    def collect(self, since: datetime | None = None) -> list[CollectedItem]:
        """Items for the daily run, newest first."""

    def backfill(self, depth_days: int | None = None) -> list[CollectedItem]:
        """Full history the source is willing to give. Defaults to collect()."""
        return self.collect()
