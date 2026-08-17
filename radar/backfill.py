"""Backfill: the one paid, one-off fill of the corpus (PRD 6).

Three properties decide whether the money spent here is recoverable.

* **A ceiling is mandatory** (FR-6.8). There is no default and no fallback to
  "unlimited": a run without a limit is refused before the first fetch. The
  limit is checked before every call, not recorded after it.
* **It resumes.** Checkpoints are written per source, not once for the whole
  process, so a run killed on the two hundredth material continues from the
  first unfinished source instead of from zero. Spend already recorded on the
  run is carried into the resumed run's budget, so the limit covers the whole
  effort rather than each attempt separately.
* **It is idempotent** (FR-6.7). The corpus key is the canonical source URL
  plus the position of the event inside its material, and it is enforced by
  the UNIQUE index rather than by a lookup: `INSERT OR IGNORE` cannot race.
  Materials already in the corpus are not sent to the model at all, so a
  repeat run neither duplicates nor pays twice.

Enrichment is reached only through the `Enricher` protocol from
radar.contracts. This module never imports radar.enrich: stage 4 is written in
parallel with this one, and the two meet at the contract or not at all.

Backfill deliberately runs a shorter pipeline than the daily run: collect,
enrich, index, then one trend pass over the whole corpus (FR-6.9). No
relevance filter, no scoring, no publication, no delivery (FR-6.3, FR-6.4).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Callable

from radar.adapters.base import CollectedItem, SourceConfig
from radar.cache import canonical_url
from radar.collect import collect_all
from radar.config import ThemeConfig
from radar.contracts import EnrichResult, Enricher
from radar.db import corpus_readiness
from radar.fetch import Fetcher
from radar.journal import EventKind, Journal, Outcome
from radar.runlog import Budget, BudgetExceeded, RunLog, new_run_id
from radar.trends import (
    DEFAULT_MIN_MEMBERS,
    TrendCandidate,
    find_candidates,
    save_trends,
)

INGEST_MODE = "backfill"
RUN_PREFIX = "backfill-"
STAGE_COLLECT = "collect"
SOURCE_STAGE_PREFIX = "source:"
STAGE_TRENDS = "trends"

# FR-6.1: v1 backfills priority 1-3 only. Media and aggregators fill the
# corpus by themselves over weeks of daily runs, and scraping their archives
# is the most reliable way to run out of both money and evening.
MAX_BACKFILL_PRIORITY = 3

DEFAULT_CONCURRENCY = 8

# Shape of one extraction call, used to price a run before it starts and to
# reserve budget before each call. Deliberately generous: an estimate that
# under-reserves lets the concurrent walk overshoot the ceiling.
PROMPT_OVERHEAD_TOKENS = 1200
OUTPUT_TOKENS_PER_MATERIAL = 700
CHARS_PER_TOKEN = 4
MAX_MATERIAL_TOKENS = 24000

# List prices in USD per million tokens (input, output), August 2026.
# Kept here rather than in the theme config because it is a property of the
# provider, not of the theme; `llm.pricing` in the config overrides any row.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "anthropic/claude-fable-5": (10.0, 50.0),
    "anthropic/claude-opus-5": (5.0, 25.0),
    "anthropic/claude-opus-4.8": (5.0, 25.0),
    "anthropic/claude-opus-4.7": (5.0, 25.0),
    "anthropic/claude-opus-4.6": (5.0, 25.0),
    "anthropic/claude-sonnet-5": (3.0, 15.0),
    "anthropic/claude-sonnet-4.6": (3.0, 15.0),
    "anthropic/claude-haiku-4.5": (1.0, 5.0),
}
DEFAULT_PRICE_PER_MTOK = (5.0, 25.0)

ProgressFn = Callable[[str], None]


class BudgetNotSet(RuntimeError):
    """No ceiling was given anywhere. Refusal, not a default (FR-6.8)."""


def _print_progress(line: str) -> None:
    print(line, flush=True)


def resolve_limit(limit_usd: float | None, config: ThemeConfig) -> float:
    """CLI value, then the config, then refusal. Never a default number."""
    if limit_usd is None:
        configured = config.section("budget").get("max_usd_per_backfill")
        limit_usd = None if configured is None else float(configured)
    if limit_usd is None:
        raise BudgetNotSet(
            "Лимит стоимости не задан. Укажите --limit-usd или budget.max_usd_per_backfill "
            "в конфиге: бэкфилл без лимита не запускается (FR-6.8)."
        )
    if float(limit_usd) <= 0:
        raise BudgetNotSet(
            f"Лимит стоимости должен быть больше нуля, получено {limit_usd}."
        )
    return float(limit_usd)


def resolve_concurrency(concurrency: int | None, config: ThemeConfig) -> int:
    if concurrency is None:
        concurrency = int(config.section("llm").get("concurrency", DEFAULT_CONCURRENCY))
    return max(1, int(concurrency))


def price_for(model: str, config: ThemeConfig | None = None) -> tuple[float, float]:
    overrides = (config.section("llm").get("pricing") or {}) if config else {}
    row = overrides.get(model)
    if isinstance(row, dict):
        return float(row.get("input", 0.0)), float(row.get("output", 0.0))
    return PRICE_PER_MTOK.get(model, DEFAULT_PRICE_PER_MTOK)


def estimate_material_usd(item: CollectedItem, price: tuple[float, float]) -> float:
    tokens_in = (
        min(len(item.raw_text) // CHARS_PER_TOKEN, MAX_MATERIAL_TOKENS)
        + PROMPT_OVERHEAD_TOKENS
    )
    return (
        tokens_in / 1_000_000 * price[0]
        + OUTPUT_TOKENS_PER_MATERIAL / 1_000_000 * price[1]
    )


class SharedBudget(Budget):
    """`Budget` for a concurrent walk.

    Reserve-then-settle rather than check-then-charge: eight workers can all
    pass the same `check` and then each spend the last dollar. The estimate is
    charged before the call and replaced by the real cost when it returns, so
    what is in flight is always counted against the ceiling.
    """

    def __init__(self, limit_usd: float) -> None:
        super().__init__(limit_usd)
        self._lock = threading.Lock()

    def check(self, estimated_usd: float = 0.0) -> None:
        with self._lock:
            super().check(estimated_usd)

    def charge(self, usd: float) -> None:
        with self._lock:
            super().charge(usd)

    def reserve(self, estimated_usd: float) -> None:
        """Atomic check plus hold. Raises BudgetExceeded instead of spending."""
        with self._lock:
            super().check(estimated_usd)
            self.spent_usd += estimated_usd

    def settle(self, estimated_usd: float, actual_usd: float) -> None:
        with self._lock:
            self.spent_usd = max(0.0, self.spent_usd - estimated_usd + actual_usd)


@dataclass(slots=True)
class BackfillOptions:
    limit_usd: float | None = None
    source_ids: list[str] | None = None
    concurrency: int | None = None
    resume: bool = False
    dry_run: bool = False
    min_trend_members: int | None = None


@dataclass(slots=True)
class SourcePlan:
    source_id: str
    status: str = "ok"
    collected: int = 0
    pending: int = 0
    already_ingested: int = 0
    estimated_usd: float = 0.0
    spent_usd: float = 0.0
    statements_added: int = 0
    statements_duplicate: int = 0
    failed: int = 0
    error: str | None = None
    completed: bool = False


@dataclass(slots=True)
class BackfillReport:
    run_id: str
    limit_usd: float
    dry_run: bool = False
    resumed: bool = False
    concurrency: int = DEFAULT_CONCURRENCY
    duration_s: float = 0.0
    sources_total: int = 0
    sources_completed: int = 0
    sources_skipped: int = 0
    materials_collected: int = 0
    materials_pending: int = 0
    materials_processed: int = 0
    materials_already_ingested: int = 0
    materials_failed: int = 0
    statements_added: int = 0
    statements_duplicate: int = 0
    facts_kept: int = 0
    facts_rejected: int = 0
    spent_usd: float = 0.0
    estimated_usd: float = 0.0
    cache_hits: int = 0
    budget_exhausted: bool = False
    plans: list[SourcePlan] = field(default_factory=list)
    cells: list[dict[str, Any]] = field(default_factory=list)
    readiness: dict[str, Any] = field(default_factory=dict)
    trends_accepted: list[dict[str, Any]] = field(default_factory=list)
    trends_rejected: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)


# -- helpers ----------------------------------------------------------


def _source_stage(source_id: str) -> str:
    return f"{SOURCE_STAGE_PREFIX}{source_id}"


def select_sources(
    config: ThemeConfig, source_ids: list[str] | None = None
) -> list[SourceConfig]:
    """Named sources are honoured as asked; the default set obeys FR-6.1."""
    if source_ids:
        chosen: list[SourceConfig] = []
        for sid in source_ids:
            source = config.source(sid)
            if source is None:
                raise ValueError(f"источник не найден в конфиге: {sid}")
            chosen.append(source)
        return chosen
    return [
        s for s in config.backfillable_sources() if s.priority <= MAX_BACKFILL_PRIORITY
    ]


def ingested_urls(conn: sqlite3.Connection) -> set[str]:
    return {
        row["source_url"]
        for row in conn.execute("SELECT DISTINCT source_url FROM event_statements")
    }


def find_unfinished_run(conn: sqlite3.Connection) -> str | None:
    """Latest backfill run that never reported a clean finish."""
    row = conn.execute(
        "SELECT run_id FROM runs WHERE run_id LIKE ? AND status <> 'ok' "
        "ORDER BY started_at DESC LIMIT 1",
        (f"{RUN_PREFIX}%",),
    ).fetchone()
    return row["run_id"] if row else None


def _prior_cost(conn: sqlite3.Connection, run_id: str) -> float:
    row = conn.execute(
        "SELECT cost_usd FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return float(row["cost_usd"] or 0.0) if row else 0.0


def persist_statements(
    conn: sqlite3.Connection, results: list[EnrichResult]
) -> tuple[int, int]:
    """Write a batch to the corpus. Returns (inserted, ignored as duplicate).

    The index is the position of the event inside its material and is
    recomputed the same way on every run, which is what makes the UNIQUE key
    stable across runs instead of shifting by the number of rows already
    stored.
    """
    inserted = 0
    duplicate = 0
    now = datetime.now(UTC)
    with conn:
        for result in results:
            per_url: dict[str, int] = defaultdict(int)
            for statement in result.statements:
                url = canonical_url(
                    statement.source_url or result.url, keep_fragment=True
                )
                index = per_url[url]
                per_url[url] += 1
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO event_statements ("
                    "statement_id, cluster_id, text, vendor, product, change_type, "
                    "event_date, date_precision, version, source_url, statement_index, "
                    "evidence, ingested_at, ingest_mode, extractor_model, prompt_version, "
                    "raw_material_ref, embedding, supersedes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        statement.statement_id,
                        statement.cluster_id,
                        statement.text,
                        statement.vendor,
                        statement.product,
                        str(statement.change_type),
                        statement.event_date.isoformat()
                        if statement.event_date
                        else None,
                        str(statement.date_precision),
                        statement.version,
                        url,
                        index,
                        statement.evidence,
                        (statement.ingested_at or now).isoformat(),
                        # Forced here rather than trusted from the enricher:
                        # the writer owns the provenance of its own rows
                        # (FR-6.4).
                        INGEST_MODE,
                        statement.extractor_model,
                        statement.prompt_version,
                        statement.raw_material_ref,
                        None,
                        None,
                    ),
                )
                if cursor.rowcount:
                    inserted += 1
                else:
                    duplicate += 1
    return inserted, duplicate


def refresh_trends(
    conn: sqlite3.Connection, config: ThemeConfig, min_members: int | None = None
) -> tuple[list[TrendCandidate], list[TrendCandidate], int]:
    """One pass over the whole corpus (FR-6.9, FR-6.10, FR-6.11)."""
    settings = config.trends
    members = min_members or int(settings.get("min_members", DEFAULT_MIN_MEMBERS))
    dormant = int(settings.get("dormant_after_days", 90))
    labels = {v["id"]: v.get("label", v["id"]) for v in config.vendors}
    labels.update({c["id"]: c.get("label", c["id"]) for c in config.change_types})
    accepted, rejected = find_candidates(conn, min_members=members)
    saved = save_trends(conn, accepted, dormant_after=dormant, labels=labels)
    return accepted, rejected, saved


def _candidate_row(candidate: TrendCandidate) -> dict[str, Any]:
    return {
        "vendor": candidate.vendor,
        "change_type": candidate.change_type,
        "members": candidate.members,
        "first_observed": candidate.first_observed,
        "last_observed": candidate.last_observed,
        "cadence_days": candidate.cadence_days(),
        "reason": candidate.rejected_reason,
    }


def corpus_cells(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT vendor, change_type, COUNT(*) AS n FROM event_statements "
            "GROUP BY vendor, change_type ORDER BY n DESC, vendor"
        )
    ]


# -- the run ----------------------------------------------------------


def run_backfill(
    conn: sqlite3.Connection,
    config: ThemeConfig,
    fetcher: Fetcher,
    enricher: Enricher | None = None,
    options: BackfillOptions | None = None,
    *,
    journal: Journal | None = None,
    log_dir: str = "logs",
    run_id: str | None = None,
    progress: ProgressFn | None = _print_progress,
    for_date: date | None = None,
) -> BackfillReport:
    """Fill the corpus with history. Returns what it did, at what cost."""
    options = options or BackfillOptions()
    emit: ProgressFn = progress or (lambda _line: None)
    started = time.monotonic()

    limit_usd = resolve_limit(options.limit_usd, config)
    concurrency = resolve_concurrency(options.concurrency, config)
    if not options.dry_run and enricher is None:
        raise ValueError("нужен обогатитель: без него запускается только --dry-run")

    resumed_id = find_unfinished_run(conn) if options.resume and not run_id else None
    run_id = run_id or resumed_id or f"{RUN_PREFIX}{new_run_id()}"
    journal = journal or Journal(conn, log_dir=log_dir, run_id=run_id)
    # Spend already recorded on this run carries over, so `--limit-usd` bounds
    # the whole backfill and not each restart of it.
    prior_cost = _prior_cost(conn, run_id) if resumed_id else 0.0

    report = BackfillReport(
        run_id=run_id,
        limit_usd=limit_usd,
        dry_run=options.dry_run,
        resumed=resumed_id is not None,
        concurrency=concurrency,
        spent_usd=prior_cost,
    )

    sources = select_sources(config, options.source_ids)
    report.sources_total = len(sources)
    if not sources:
        report.notes.append("нет источников, пригодных для бэкфилла")
        report.duration_s = time.monotonic() - started
        return report

    if options.dry_run:
        return _dry_run(conn, config, fetcher, sources, report, emit, started)

    run_log = RunLog(conn, run_id, for_date or datetime.now(UTC).date())
    run_log.cost_usd = prior_cost
    run_log.note(f"backfill, лимит {limit_usd:.2f} USD, параллель {concurrency}")
    budget = SharedBudget(limit_usd)
    budget.spent_usd = prior_cost

    journal.record(
        EventKind.RUN_RESUMED if report.resumed else EventKind.RUN_STARTED,
        actor="backfill",
        target=run_id,
        limit_usd=limit_usd,
        spent_usd=prior_cost,
    )
    if report.resumed:
        emit(
            f"Продолжаю прогон {run_id}: уже потрачено {prior_cost:.4f} из "
            f"{limit_usd:.2f} USD."
        )

    done_stages = journal.completed_stages(run_id)
    pending_sources = [s for s in sources if _source_stage(s.id) not in done_stages]
    report.sources_skipped = len(sources) - len(pending_sources)
    report.sources_completed = report.sources_skipped
    if report.sources_skipped:
        emit(f"Пропускаю {report.sources_skipped} источников: уже отмечены чекпоинтом.")

    # -- collect
    with run_log.stage(STAGE_COLLECT, in_count=len(pending_sources)) as record:
        items, outcomes = collect_all(
            config,
            fetcher,
            run_log=run_log,
            mode="backfill",
            sources=pending_sources,
            max_workers=min(concurrency, 8),
        )
        record["out_count"] = len(items)
    journal.checkpoint(STAGE_COLLECT, item_count=len(items))
    report.materials_collected = len(items)

    plans: dict[str, SourcePlan] = {
        s.id: SourcePlan(source_id=s.id) for s in pending_sources
    }
    for outcome in outcomes:
        plan = plans.setdefault(
            outcome.source_id, SourcePlan(source_id=outcome.source_id)
        )
        plan.status = str(outcome.status)
        plan.collected = outcome.count
        plan.error = outcome.error

    by_id = {s.id: s for s in pending_sources}
    known_urls = ingested_urls(conn)
    price = price_for(config.models.get("enrich", ""), config)

    work: list[tuple[SourceConfig, CollectedItem, float]] = []
    for item in items:
        source = by_id.get(str(item.extra.get("source_id", "")))
        if source is None:
            continue
        plan = plans[source.id]
        if canonical_url(item.url, keep_fragment=True) in known_urls:
            # Already in the corpus: not sent to the model at all, which is
            # what keeps a repeat run free rather than merely deduplicated.
            plan.already_ingested += 1
            report.materials_already_ingested += 1
            continue
        estimate = estimate_material_usd(item, price)
        plan.pending += 1
        plan.estimated_usd += estimate
        work.append((source, item, estimate))

    report.materials_pending = len(work)
    report.estimated_usd = sum(plan.estimated_usd for plan in plans.values())
    emit(
        f"К обработке {len(work)} материалов из {len(items)} собранных; "
        f"оценка {report.estimated_usd:.2f} USD при лимите {limit_usd:.2f}."
    )

    remaining: dict[str, int] = defaultdict(int)
    for source, _item, _estimate in work:
        remaining[source.id] += 1
    collected_results: dict[str, list[EnrichResult]] = defaultdict(list)

    stop = threading.Event()

    def enrich_one(
        source: SourceConfig, item: CollectedItem, estimate: float
    ) -> EnrichResult | None:
        if stop.is_set():
            return None
        budget.reserve(estimate)
        try:
            result = enricher.enrich(item, source)  # type: ignore[union-attr]
        except Exception as exc:  # the protocol forbids it; the run survives anyway
            budget.settle(estimate, 0.0)
            return EnrichResult(
                source_id=source.id,
                url=item.url,
                error=f"{type(exc).__name__}: {exc}",
            )
        budget.settle(estimate, result.cost_usd)
        return result

    def flush(source_id: str, completed: bool) -> None:
        results = collected_results.pop(source_id, [])
        plan = plans[source_id]
        if results:
            inserted, duplicate = persist_statements(conn, results)
            plan.statements_added += inserted
            plan.statements_duplicate += duplicate
            report.statements_added += inserted
            report.statements_duplicate += duplicate
            for result in results:
                run_log.model_call(
                    stage="enrich",
                    model=_model_of(result) or config.models.get("enrich", "unknown"),
                    cost_usd=result.cost_usd,
                    cached=result.cached,
                )
        if completed:
            plan.completed = True
            report.sources_completed += 1
            journal.checkpoint(
                _source_stage(source_id),
                item_count=plan.pending,
                statements=plan.statements_added,
                cost_usd=round(plan.spent_usd, 6),
            )
        emit(
            f"[{report.sources_completed}/{report.sources_total}] {source_id}: "
            f"материалов {plan.pending}, записей +{plan.statements_added}, "
            f"дублей {plan.statements_duplicate}, потрачено {budget.spent_usd:.4f}, "
            f"остаток бюджета {budget.remaining_usd:.4f} USD"
        )

    with run_log.stage("enrich", in_count=len(work)) as record:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(enrich_one, source, item, estimate): (source.id, estimate)
                for source, item, estimate in work
            }
            for future in as_completed(futures):
                source_id, _estimate = futures[future]
                try:
                    result = future.result()
                except BudgetExceeded as exc:
                    if not report.budget_exhausted:
                        report.budget_exhausted = True
                        stop.set()
                        report.notes.append(str(exc))
                        run_log.note(f"бюджет исчерпан: {exc}")
                        journal.record(
                            EventKind.BUDGET_EXCEEDED,
                            actor="backfill",
                            target=source_id,
                            outcome=Outcome.FAILED,
                            spent_usd=round(budget.spent_usd, 6),
                            limit_usd=limit_usd,
                        )
                    remaining[source_id] -= 1
                    continue
                remaining[source_id] -= 1
                if result is None:  # stopped before the call was made
                    continue
                plans[source_id].spent_usd += result.cost_usd
                report.materials_processed += 1
                if result.cached:
                    report.cache_hits += 1
                if result.error:
                    report.materials_failed += 1
                    plans[source_id].failed += 1
                    journal.record(
                        EventKind.STAGE_FAILED,
                        actor="enrich",
                        target=result.url,
                        outcome=Outcome.FAILED,
                        error=result.error,
                    )
                    continue
                report.facts_kept += len(result.facts)
                report.facts_rejected += len(result.rejected_facts)
                for rejected in result.rejected_facts:
                    journal.record(
                        EventKind.FACT_REJECTED,
                        actor="enrich",
                        target=result.url,
                        outcome=Outcome.SKIPPED,
                        kind=rejected.kind,
                        value=rejected.value,
                        reason=rejected.reason,
                    )
                collected_results[source_id].append(result)
                if remaining[source_id] == 0 and not stop.is_set():
                    flush(source_id, completed=True)
            if stop.is_set():
                for future in futures:
                    future.cancel()
        # Whatever a stopped run already extracted is written down: the point
        # of a ceiling is to stop spending, not to discard what was bought.
        for source_id in list(collected_results):
            flush(source_id, completed=False)
        record["out_count"] = report.statements_added

    report.spent_usd = budget.spent_usd

    # -- trends over the whole corpus, then the readiness verdict
    with run_log.stage(STAGE_TRENDS):
        accepted, rejected, saved = refresh_trends(
            conn, config, options.min_trend_members
        )
    journal.checkpoint(STAGE_TRENDS, item_count=saved)
    report.trends_accepted = [_candidate_row(c) for c in accepted]
    report.trends_rejected = [_candidate_row(c) for c in rejected]

    report.cells = corpus_cells(conn)
    report.readiness = corpus_readiness(conn, config.data)
    report.duration_s = time.monotonic() - started

    status = "partial" if report.budget_exhausted else "ok"
    run_log.finish(status)
    journal.record(
        EventKind.RUN_FINISHED,
        actor="backfill",
        target=run_id,
        outcome=Outcome.PARTIAL if report.budget_exhausted else Outcome.OK,
        statements=report.statements_added,
        cost_usd=round(report.spent_usd, 6),
    )
    return report


def _model_of(result: EnrichResult) -> str | None:
    for statement in result.statements:
        if statement.extractor_model:
            return statement.extractor_model
    return None


def _dry_run(
    conn: sqlite3.Connection,
    config: ThemeConfig,
    fetcher: Fetcher,
    sources: list[SourceConfig],
    report: BackfillReport,
    emit: ProgressFn,
    started: float,
) -> BackfillReport:
    """Collect and price the work without opening a single model call."""
    items, outcomes = collect_all(
        config, fetcher, mode="backfill", sources=sources, max_workers=6
    )
    plans = {s.id: SourcePlan(source_id=s.id) for s in sources}
    for outcome in outcomes:
        plan = plans.setdefault(
            outcome.source_id, SourcePlan(source_id=outcome.source_id)
        )
        plan.status = str(outcome.status)
        plan.collected = outcome.count
        plan.error = outcome.error

    known_urls = ingested_urls(conn)
    price = price_for(config.models.get("enrich", ""), config)
    for item in items:
        source_id = str(item.extra.get("source_id", ""))
        plan = plans.get(source_id)
        if plan is None:
            continue
        if canonical_url(item.url, keep_fragment=True) in known_urls:
            plan.already_ingested += 1
            report.materials_already_ingested += 1
            continue
        plan.pending += 1
        plan.estimated_usd += estimate_material_usd(item, price)

    report.materials_collected = len(items)
    report.materials_pending = sum(p.pending for p in plans.values())
    report.estimated_usd = sum(p.estimated_usd for p in plans.values())
    report.plans = sorted(plans.values(), key=lambda p: -p.estimated_usd)
    report.cells = corpus_cells(conn)
    report.readiness = corpus_readiness(conn, config.data)
    report.duration_s = time.monotonic() - started
    if report.estimated_usd > report.limit_usd:
        report.notes.append(
            f"оценка {report.estimated_usd:.2f} USD превышает лимит "
            f"{report.limit_usd:.2f} USD: прогон остановится, не закончив"
        )
    emit("")
    return report


def finish_plans(report: BackfillReport, plans: dict[str, SourcePlan]) -> None:
    report.plans = sorted(plans.values(), key=lambda p: -p.statements_added)
