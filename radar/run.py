"""The daily run: stages one through eight, in order.

Every stage is written and tested on its own; this is the thread that joins
them. Three properties matter more here than anywhere else, because this is
where they either hold end to end or quietly do not.

A crash on stage four leaves stages one through three recorded (NFR-4), so
each stage is bracketed by the journal and the run log rather than wrapped in
one try around everything. A run that dies still publishes a `run_failure`
record: a daily agent gone silent looks from outside exactly like a quiet day,
and that is the one confusion this product cannot afford. And the run ends by
writing signals and nothing else — delivery is a surface's business, so this
module imports no surface at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from radar.cluster import cluster_items
from radar.collect import collect_all
from radar.config import ThemeConfig
from radar.contracts import Enricher
from radar.db import publish_signals
from radar.delta import compute_delta, prune_state, resolve_expired, save_state
from radar.fetch import Fetcher
from radar.journal import EventKind, Journal, Outcome
from radar.models import Fact, Signal, Tier
from radar.publish import (
    build_quiet_day,
    build_run_failure,
    build_run_summary,
    build_signal,
)
from radar.retrieval import CorpusRetriever
from radar.runlog import Budget, BudgetExceeded, RunLog, new_run_id
from radar.scoring import assign_tier, rank_signals


@dataclass(slots=True)
class RunResult:
    run_id: str
    for_date: date
    signals: list[Signal] = field(default_factory=list)
    collected: int = 0
    clusters: int = 0
    relevant: int = 0
    enriched: int = 0
    facts_kept: int = 0
    facts_rejected: int = 0
    cost_usd: float = 0.0
    quiet: bool = False
    failed_stage: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "for_date": self.for_date.isoformat(),
            "signals": len(self.signals),
            "collected": self.collected,
            "clusters": self.clusters,
            "relevant": self.relevant,
            "enriched": self.enriched,
            "facts_kept": self.facts_kept,
            "facts_rejected": self.facts_rejected,
            "cost_usd": round(self.cost_usd, 4),
            "quiet": self.quiet,
            "failed_stage": self.failed_stage,
            "error": self.error,
        }


class DailyRun:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: ThemeConfig,
        fetcher: Fetcher,
        enricher: Enricher,
        relevance_filter: Any = None,
        run_id: str | None = None,
        for_date: date | None = None,
        log_dir: str = "logs",
    ) -> None:
        self.conn = conn
        self.config = config
        self.fetcher = fetcher
        self.enricher = enricher
        self.relevance_filter = relevance_filter
        self.for_date = for_date or datetime.now(UTC).date()
        self.run_id = run_id or new_run_id()
        self.log = RunLog(conn, self.run_id, self.for_date)
        self.journal = Journal(conn, log_dir=log_dir, run_id=self.run_id)
        self.budget = Budget(
            float(config.section("budget").get("max_usd_per_run", 0.5))
        )
        self.retriever = CorpusRetriever(conn, config.retrieval)

    def execute(self) -> RunResult:
        result = RunResult(run_id=self.run_id, for_date=self.for_date)
        self.journal.record(EventKind.RUN_STARTED, actor="pipeline", target=self.run_id)
        try:
            self._pipeline(result)
        except BudgetExceeded as exc:
            # Not a crash: the ceiling did its job. Whatever was produced up to
            # this point still gets published.
            result.error = str(exc)
            result.failed_stage = result.failed_stage or "budget"
            self._publish_failure(result, "budget", str(exc))
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            self._publish_failure(
                result, result.failed_stage or "unknown", result.error
            )
        finally:
            result.cost_usd = self.log.cost_usd
            self.log.finish("ok" if result.ok else "failed")
            self.journal.record(
                EventKind.RUN_FINISHED if result.ok else EventKind.RUN_FAILED,
                actor="pipeline",
                target=self.run_id,
                outcome=Outcome.OK if result.ok else Outcome.FAILED,
                **result.as_dict(),
            )
        return result

    def _pipeline(self, result: RunResult) -> None:
        priority_of = {s.id: s.priority for s in self.config.sources}
        name_of = {s.id: s.id for s in self.config.sources}

        result.failed_stage = "collect"
        with self.log.stage("collect") as record:
            items, outcomes = collect_all(self.config, self.fetcher, self.log)
            record["out_count"] = len(items)
        result.collected = len(items)
        self.journal.checkpoint("collect", item_count=len(items))

        result.failed_stage = "cluster"
        with self.log.stage("cluster", in_count=len(items)) as record:
            vendor_of = {
                i.url: next(
                    (
                        s.vendor
                        for s in self.config.sources
                        if s.id == i.extra.get("source_id")
                    ),
                    None,
                )
                for i in items
            }
            clusters = cluster_items(items, priority_of, vendor_of)
            record["out_count"] = len(clusters)
        result.clusters = len(clusters)
        self.journal.checkpoint("cluster", item_count=len(clusters))

        result.failed_stage = "filter"
        with self.log.stage("filter", in_count=len(clusters)) as record:
            relevant = self._filter(clusters)
            record["out_count"] = len(relevant)
        result.relevant = len(relevant)
        self.journal.checkpoint("filter", item_count=len(relevant))

        result.failed_stage = "enrich"
        enriched: list[tuple[Any, list[Fact]]] = []
        with self.log.stage("enrich", in_count=len(relevant)) as record:
            for cluster in relevant:
                source = self.config.source(
                    str(cluster.primary.extra.get("source_id", ""))
                )
                outcome = self.enricher.enrich(cluster.primary, source)
                if not outcome.ok:
                    self.journal.record(
                        EventKind.MODEL_CALLED,
                        actor="enrich",
                        target=cluster.cluster_id,
                        outcome=Outcome.FAILED,
                        error=outcome.error,
                    )
                    continue
                result.facts_kept += len(outcome.facts)
                result.facts_rejected += len(outcome.rejected_facts)
                enriched.append((cluster, outcome.facts))
            record["out_count"] = len(enriched)
        result.enriched = len(enriched)
        self.journal.checkpoint("enrich", item_count=len(enriched))

        result.failed_stage = "contextualize"
        with self.log.stage("contextualize", in_count=len(enriched)) as record:
            contexts = []
            for cluster, facts in enriched:
                delta = compute_delta(
                    self.conn, cluster, facts, self.run_id, self.for_date
                )
                retrieval = self.retriever.find_precedents(
                    cluster.vendor,
                    cluster.change_type,
                    self.for_date,
                    text=cluster.title,
                    exclude_ids={cluster.cluster_id},
                )
                contexts.append((cluster, facts, delta, retrieval))
                save_state(self.conn, cluster, facts, delta, self.run_id, self.for_date)
            resolve_expired(self.conn, self.for_date)
            record["out_count"] = len(contexts)
        self.journal.checkpoint("contextualize", item_count=len(contexts))

        result.failed_stage = "score"
        summary = build_run_summary(
            outcomes,
            result.collected,
            result.clusters - result.relevant,
            cost_usd=self.log.cost_usd,
            name_of=name_of,
        )
        with self.log.stage("score", in_count=len(contexts)) as record:
            signals = self._assemble_and_rank(contexts, summary)
            record["out_count"] = len(signals)
        self.journal.checkpoint("score", item_count=len(signals))

        result.failed_stage = "publish"
        with self.log.stage("publish", in_count=len(signals)) as record:
            if not signals:
                # PUB-4: silence is delivered as a record, not as nothing.
                signals = [
                    build_quiet_day(self.conn, self.run_id, self.for_date, summary)
                ]
                result.quiet = True
            publish_signals(self.conn, self.run_id, signals)
            record["out_count"] = len(signals)
        result.signals = signals
        result.failed_stage = None
        self.journal.checkpoint("publish", item_count=len(signals))
        prune_state(self.conn, self.for_date)

    def _filter(self, clusters: list[Any]) -> list[Any]:
        """A filter that breaks lets material through rather than dropping it.

        Losing a real change to an outage is worse than letting noise past.
        """
        if self.relevance_filter is None:
            return clusters
        try:
            return self.relevance_filter.keep(
                clusters, run_log=self.log, budget=self.budget
            )
        except BudgetExceeded:
            raise
        except Exception as exc:
            self.log.note(f"фильтр не отработал, материалы пропущены дальше: {exc}")
            return clusters

    def _assemble_and_rank(self, contexts: list[tuple], summary: Any) -> list[Signal]:
        """Build signals first, then score and rank them.

        Scoring takes a whole Signal rather than loose fields, and that is the
        right way round: the factors of FR-6.1 live on the contract, so one
        function ranks a live run and a replayed one identically.
        """
        drafts: list[Signal] = []
        source_ids: dict[str, str] = {}
        for cluster, facts, delta, retrieval in contexts:
            signal = build_signal(
                self.run_id,
                self.for_date,
                cluster,
                facts,
                delta,
                retrieval,
                score=0,
                rationale="",
                tier=Tier.STANDARD,
                rank=0,
                headline=cluster.title,
                summary=cluster.primary.raw_text[:2000],
                vendor_label=cluster.vendor or "",
                change_type_label=cluster.change_type or "",
                run_summary=summary,
            )
            drafts.append(signal)
            source_ids[signal.signal_id] = str(
                cluster.primary.extra.get("source_id", "")
            )

        ranked = rank_signals(
            drafts, self.config.data, as_of=self.for_date, source_ids=source_ids
        )
        out: list[Signal] = []
        for position, scored in enumerate(ranked, 1):
            tier = assign_tier(scored.breakdown.score, self.config.data)
            if tier is Tier.BACKGROUND:
                # Below the digest threshold the core still knows about it; it
                # simply does not become something a surface will show.
                continue
            out.append(
                scored.signal.model_copy(
                    update={
                        "score": scored.breakdown.score,
                        "score_rationale": scored.breakdown.rationale,
                        "rank": position,
                        "tier": tier,
                    }
                )
            )
        return out

    def _publish_failure(self, result: RunResult, stage: str, reason: str) -> None:
        """A run that died says so, in the same store every surface reads."""
        try:
            summary = build_run_summary(
                [], result.collected, 0, cost_usd=self.log.cost_usd
            )
            signal = build_run_failure(
                self.run_id, self.for_date, stage, reason, summary
            )
            publish_signals(self.conn, self.run_id, [signal])
            result.signals = [signal]
        except Exception as exc:  # never mask the original failure
            self.log.note(f"не удалось опубликовать отчёт о падении: {exc}")
