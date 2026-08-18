"""Stage 1: collection.

One failing source never stops a run (FR-1.4). Beyond that, this stage draws a
distinction the PRD does not: a source answering HTTP 200 with nothing
extractable is neither a success nor a network failure. Two of the configured
pages behave exactly that way, and recording them as `ok` would quietly shrink
the corpus while every dashboard stayed green.

The same distinction has a third side, and it is the one the product sells. On
an ordinary morning most watched repositories ship nothing, and a source with
no news is not a source with no answer. `quiet` is that state: checked, it
answered, none of what it holds is new. Only a source that handed back nothing
to filter is still `empty`.
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


# How many records the source handed over before the window was applied. The
# feed, release and channel adapters already count them for the run log; the
# page adapter keeps no diagnostics, and its sources are read a second time.
RECORDS_SEEN_KEYS = ("entries_seen", "releases_seen", "posts_seen")


def _diagnostics(adapter: Adapter) -> dict[str, object]:
    extra = getattr(adapter, "extra", None)
    return extra if isinstance(extra, dict) else {}


def records_seen(adapter: Adapter) -> int | None:
    """Records the adapter read off the response, or None if it does not say."""
    extra = _diagnostics(adapter)
    for key in RECORDS_SEEN_KEYS:
        value = extra.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        return value
    return None


def reread_unfiltered(adapter: Adapter) -> tuple[int, str | None]:
    """Second read of the same response, with no window applied.

    Only reached for adapters that keep no diagnostics of their own — the page
    adapter — and only when the windowed read came back empty. The response is
    already in the HTTP cache, so this costs a parse and no request. Adapters
    that page the network (releases, channels) never get here: they report
    `releases_seen` and `posts_seen` themselves.
    """
    try:
        return len(adapter.collect(None)), None
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def classify_silence(adapter: Adapter) -> tuple[SourceStatus, str | None]:
    """Nothing new is not the same as nothing at all.

    The central claim of the product is that a quiet day and a broken pipe are
    different states, and the first place it has to hold is here. A source is
    `quiet` only when it can be shown to have answered with its usual contents
    and none of them fell inside the window; a source that handed back nothing
    to filter stays `empty`, which is the fault the status was made for.
    """
    extra = _diagnostics(adapter)
    error = extra.get("error")
    if error:
        return SourceStatus.EMPTY, str(error)

    seen = records_seen(adapter)
    if seen is None:
        seen, failure = reread_unfiltered(adapter)
        if failure is not None:
            return SourceStatus.EMPTY, failure

    if seen > 0:
        return (
            SourceStatus.QUIET,
            f"проверен, новых записей за окно нет; всего на источнике {seen}",
        )
    return SourceStatus.EMPTY, "источник ответил, но не отдал ни одной записи"


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

    # `min_expected_items` is a depth check written for the backfill: "this
    # page must hand over at least N dated records over `backfill_depth_days`".
    # Asked of a 26-hour window it means nothing, and it answered that a
    # repository with no release today was broken.
    if mode == "backfill":
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
        return SourceOutcome(
            source.id, SourceStatus.OK, items=items, latency_ms=latency
        )

    if items:
        return SourceOutcome(
            source.id, SourceStatus.OK, items=items, latency_ms=latency
        )

    status, note = classify_silence(adapter)
    return SourceOutcome(source.id, status, latency_ms=latency, error=note)


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
    summary = {"ok": 0, "quiet": 0, "empty": 0, "failed": 0, "items": 0}
    for outcome in outcomes:
        summary[str(outcome.status)] = summary.get(str(outcome.status), 0) + 1
        summary["items"] += outcome.count
    return summary
