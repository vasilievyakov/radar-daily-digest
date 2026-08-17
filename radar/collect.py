"""Stage 1: collection.

One failing source never stops a run (FR-1.4). Beyond that, this stage draws a
distinction the PRD does not: a source answering HTTP 200 with nothing
extractable is neither a success nor a network failure. Two of the configured
pages behave exactly that way, and recording them as `ok` would quietly shrink
the corpus while every dashboard stayed green.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from radar.adapters.base import Adapter, CollectedItem, SourceConfig
from radar.adapters.github_releases import GitHubReleasesAdapter
from radar.adapters.html_page import HtmlPageAdapter
from radar.adapters.rss_feed import RssFeedAdapter
from radar.adapters.telegram_channel import TelegramChannelAdapter
from radar.config import ThemeConfig
from radar.fetch import Fetcher
from radar.models import SourceStatus
from radar.runlog import RunLog

ADAPTERS: dict[str, type[Adapter]] = {
    "html_scrape": HtmlPageAdapter,
    "github_releases": GitHubReleasesAdapter,
    "rss": RssFeedAdapter,
    "telegram_channel": TelegramChannelAdapter,
}


@dataclass(slots=True)
class SourceOutcome:
    source_id: str
    status: SourceStatus
    items: list[CollectedItem] = field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.items)


def build_adapter(source: SourceConfig, fetcher: Fetcher) -> Adapter | None:
    adapter_cls = ADAPTERS.get(source.type)
    return adapter_cls(source, fetcher) if adapter_cls else None


def collect_source(
    source: SourceConfig,
    fetcher: Fetcher,
    mode: str = "live",
    since: datetime | None = None,
) -> SourceOutcome:
    """Never raises: a source that breaks becomes a recorded outcome."""
    started = time.monotonic()
    adapter = build_adapter(source, fetcher)
    if adapter is None:
        return SourceOutcome(
            source.id,
            SourceStatus.FAILED,
            error=f"нет адаптера для типа источника {source.type}",
        )

    try:
        items = (
            adapter.backfill(source.backfill_depth_days)
            if mode == "backfill"
            else adapter.collect(since)
        )
    except Exception as exc:
        return SourceOutcome(
            source.id,
            SourceStatus.FAILED,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )

    for item in items:
        item.extra.setdefault("source_id", source.id)
        item.extra.setdefault("source_priority", source.priority)

    latency = int((time.monotonic() - started) * 1000)
    if len(items) < source.min_expected_items:
        return SourceOutcome(
            source.id,
            SourceStatus.EMPTY,
            items=items,
            latency_ms=latency,
            error=(
                f"источник ответил, но записей меньше ожидаемого: "
                f"{len(items)} вместо {source.min_expected_items}"
            ),
        )
    return SourceOutcome(source.id, SourceStatus.OK, items=items, latency_ms=latency)


def dedupe_key(item: CollectedItem) -> str:
    """Identity of a material.

    URL alone is not enough. A page whose headings carry no `id` gives every
    section the same address: the OpenAI changelog yields 103 dated sections
    on one URL, and keying by URL collapses them into a single material. So
    content participates in the key, and two sources publishing the identical
    text under one address still merge as they should (FR-1.6).
    """
    from radar.cache import canonical_url, digest

    return digest(canonical_url(item.url, keep_fragment=True), item.raw_text[:2000])


def dedupe_by_url(outcomes: list[SourceOutcome]) -> list[CollectedItem]:
    """One URL seen through two sources is one material with two `seen_in` (FR-1.6)."""
    merged: dict[str, CollectedItem] = {}
    for outcome in outcomes:
        for item in outcome.items:
            key = dedupe_key(item)
            existing = merged.get(key)
            if existing is None:
                item.extra.setdefault("seen_in", [outcome.source_id])
                merged[key] = item
                continue
            seen = existing.extra.setdefault("seen_in", [])
            if outcome.source_id not in seen:
                seen.append(outcome.source_id)
    return list(merged.values())


def collect_all(
    config: ThemeConfig,
    fetcher: Fetcher,
    run_log: RunLog | None = None,
    mode: str = "live",
    sources: list[SourceConfig] | None = None,
    max_workers: int = 6,
) -> tuple[list[CollectedItem], list[SourceOutcome]]:
    """Walk every enabled source concurrently and merge the results.

    Concurrency matters more than it looks: a serial walk over a dozen sources
    with retries and polite delays is most of the ten minute budget (NFR-2).
    The fetcher keeps its own per-domain interval, so politeness survives.
    """
    if sources is None:
        sources = (
            config.backfillable_sources()
            if mode == "backfill"
            # Frozen archives carry depth but never change: polling them every
            # morning spends budget to re-read the same page.
            else [s for s in config.enabled_sources() if s.live_collect]
        )

    window_hours = int(config.collection.get("window_hours", 26))
    since = (
        None
        if mode == "backfill"
        else datetime.now(UTC) - timedelta(hours=window_hours)
    )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        outcomes = list(
            pool.map(
                lambda s: collect_source(s, fetcher, mode=mode, since=since), sources
            )
        )

    if run_log is not None:
        for outcome in outcomes:
            run_log.source_result(
                outcome.source_id,
                outcome.status,
                items_count=outcome.count,
                latency_ms=outcome.latency_ms,
                error=outcome.error,
            )

    return dedupe_by_url(outcomes), outcomes


def summarize(outcomes: list[SourceOutcome]) -> dict[str, int]:
    summary = {"ok": 0, "empty": 0, "failed": 0, "items": 0}
    for outcome in outcomes:
        summary[str(outcome.status)] = summary.get(str(outcome.status), 0) + 1
        summary["items"] += outcome.count
    return summary
