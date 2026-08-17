"""Backfill: the one paid, one-off fill of the corpus (PRD 6).

Four properties decide whether the money spent here is recoverable.

* **A ceiling is mandatory** (FR-6.8). There is no default and no fallback to
  "unlimited": a run without a limit is refused before the first fetch. The
  limit is checked before every call, not recorded after it.
* **The unit of work is a source, not a material.** One worker owns one source
  and walks its materials in order; several sources run at once. That makes a
  checkpoint mean something ("this source is done, or it was never started"),
  so resuming never has to remember a position inside a source; it keeps the
  polite per-domain interval intact, because nobody else is touching that
  domain; and it takes the races out of `statement_index`.
* **It resumes.** Checkpoints are written per source, so a run killed on the
  two hundredth material continues from the first unfinished source. An
  unfinished source restarts whole, which the model cache makes nearly free.
  Spend already recorded on the run carries into the resumed run's budget, so
  the limit covers the whole effort rather than each attempt separately.
* **It is idempotent** (FR-6.7). The corpus key is the canonical source URL
  plus the position of the event inside its material, enforced by the UNIQUE
  index rather than by a lookup: `INSERT OR IGNORE` cannot race. Materials
  already in the corpus are never sent to the model, so a repeat run neither
  duplicates nor pays twice.

Enrichment is reached only through the `Enricher` protocol from
radar.contracts. This module never imports radar.enrich: stage 4 is written in
parallel with this one, and the two meet at the contract or not at all. Model
routing (`models.enrich` versus `models.enrich_critical`) belongs to that
stage; backfill only reports what each model cost.

Backfill runs a shorter pipeline than the daily run: collect, enrich, index,
then one trend pass over the whole corpus (FR-6.9). No relevance filter, no
scoring, no publication, no delivery (FR-6.3, FR-6.4).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
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
STAGE_ENRICH = "enrich"
STAGE_TRENDS = "trends"
# Checkpoint stage per source: "backfill:github_changelog".
SOURCE_STAGE_PREFIX = "backfill:"

DEFAULT_CONCURRENCY = 8

# Shape of one extraction call, used to price a run before it starts and to
# reserve budget before each call. Deliberately generous: an estimate that
# under-reserves lets the concurrent walk overshoot the ceiling.
PROMPT_OVERHEAD_TOKENS = 1200
OUTPUT_TOKENS_PER_MATERIAL = 700
CHARS_PER_TOKEN = 4
MAX_MATERIAL_TOKENS = 24000

# List prices in USD per million tokens (input, output), August 2026. Kept
# here rather than in the theme config because it is a property of the
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
    """Number of sources processed at once, from `llm.concurrency`."""
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
    priorities: list[int] | None = None
    concurrency: int | None = None
    resume: bool = False
    dry_run: bool = False
    min_trend_members: int | None = None


@dataclass(slots=True)
class SourcePlan:
    source_id: str
    priority: int = 5
    status: str = "ok"
    collected: int = 0
    pending: int = 0
    processed: int = 0
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
    sources_unfinished: int = 0
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
    interrupted: bool = False
    cost_by_model: dict[str, dict[str, float]] = field(default_factory=dict)
    plans: list[SourcePlan] = field(default_factory=list)
    cells: list[dict[str, Any]] = field(default_factory=list)
    readiness: dict[str, Any] = field(default_factory=dict)
    trends_accepted: list[dict[str, Any]] = field(default_factory=list)
    trends_rejected: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def complete(self) -> bool:
        return not (
            self.budget_exhausted or self.interrupted or self.sources_unfinished
        )


# -- helpers ----------------------------------------------------------


def source_stage(source_id: str) -> str:
    return f"{SOURCE_STAGE_PREFIX}{source_id}"


def select_sources(
    config: ThemeConfig,
    source_ids: list[str] | None = None,
    priorities: list[int] | None = None,
) -> list[SourceConfig]:
    """Named sources are honoured as asked; otherwise every backfillable one.

    FR-6.1 draws the line by source type, and the config already encodes it:
    media and aggregators are not `backfill_supported`. `--priority` narrows
    further, which is how one evening's run is aimed at the registries.
    """
    if source_ids:
        chosen: list[SourceConfig] = []
        for sid in source_ids:
            source = config.source(sid)
            if source is None:
                raise ValueError(f"источник не найден в конфиге: {sid}")
            chosen.append(source)
    else:
        chosen = list(config.backfillable_sources())
    if priorities:
        wanted = set(priorities)
        chosen = [s for s in chosen if s.priority in wanted]
    return chosen


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
    """Write one source's harvest to the corpus. Returns (inserted, ignored).

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
                        # Stamped by the writer rather than trusted from the
                        # enricher: the row's provenance belongs to whoever
                        # puts it in the corpus (FR-6.4).
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


def candidate_row(candidate: TrendCandidate) -> dict[str, Any]:
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


@dataclass(slots=True)
class _SourceWork:
    source: SourceConfig
    items: list[CollectedItem]
    estimates: list[float]


@dataclass(slots=True)
class _SourceResult:
    source_id: str
    results: list[EnrichResult] = field(default_factory=list)
    stopped: bool = False
    exhausted: bool = False
    error: str | None = None


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

    sources = select_sources(config, options.source_ids, options.priorities)

    if options.dry_run:
        report = BackfillReport(
            run_id="dry-run",
            limit_usd=limit_usd,
            dry_run=True,
            concurrency=concurrency,
            sources_total=len(sources),
        )
        return _dry_run(conn, config, fetcher, sources, report, started)

    resumed_id = find_unfinished_run(conn) if options.resume and not run_id else None
    run_id = run_id or resumed_id or f"{RUN_PREFIX}{new_run_id()}"
    journal = journal or Journal(conn, log_dir=log_dir, run_id=run_id)
    # Spend already recorded on this run carries over, so --limit-usd bounds
    # the whole backfill and not each restart of it.
    prior_cost = _prior_cost(conn, run_id) if resumed_id else 0.0

    report = BackfillReport(
        run_id=run_id,
        limit_usd=limit_usd,
        resumed=resumed_id is not None,
        concurrency=concurrency,
        sources_total=len(sources),
        spent_usd=prior_cost,
    )
    if not sources:
        report.notes.append("нет источников, пригодных для бэкфилла")
        report.duration_s = time.monotonic() - started
        return report

    run_log = RunLog(conn, run_id, for_date or datetime.now(UTC).date())
    run_log.cost_usd = prior_cost
    run_log.note(f"backfill, лимит {limit_usd:.2f} USD, источников разом {concurrency}")
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
    pending_sources = [s for s in sources if source_stage(s.id) not in done_stages]
    report.sources_skipped = len(sources) - len(pending_sources)
    report.sources_completed = report.sources_skipped
    if report.sources_skipped:
        emit(f"Пропускаю {report.sources_skipped} источников: уже есть чекпоинт.")

    plans: dict[str, SourcePlan] = {
        s.id: SourcePlan(source_id=s.id, priority=s.priority) for s in sources
    }
    for source in sources:
        if source_stage(source.id) in done_stages:
            plans[source.id].completed = True
            state = done_stages[source_stage(source.id)]
            plans[source.id].statements_added = int(
                state.get("state", {}).get("statements", 0) or 0
            )

    # -- collection: one pass, so the enrichment stage can be handed whole
    # sources sorted by size.
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

    grouped: dict[str, _SourceWork] = {
        sid: _SourceWork(source=source, items=[], estimates=[])
        for sid, source in by_id.items()
    }
    for item in items:
        source_id = str(item.extra.get("source_id", ""))
        work = grouped.get(source_id)
        if work is None:
            continue
        plan = plans[source_id]
        if canonical_url(item.url, keep_fragment=True) in known_urls:
            # Already in the corpus: never sent to the model, which is what
            # makes a repeat run free rather than merely deduplicated.
            plan.already_ingested += 1
            report.materials_already_ingested += 1
            continue
        estimate = estimate_material_usd(item, price)
        work.items.append(item)
        work.estimates.append(estimate)
        plan.pending += 1
        plan.estimated_usd += estimate

    # Longest first: the naive order leaves seven workers idle while one drags
    # an 871-material changelog to the end of the run.
    works = sorted(
        (w for w in grouped.values() if w.items), key=lambda w: -len(w.items)
    )
    report.materials_pending = sum(len(w.items) for w in works)
    report.estimated_usd = sum(plan.estimated_usd for plan in plans.values())
    emit(
        f"К обработке {report.materials_pending} материалов из "
        f"{report.materials_collected} собранных, источников {len(works)}; "
        f"оценка {report.estimated_usd:.2f} USD при лимите {limit_usd:.2f}."
    )

    stop = threading.Event()

    def process_source(work: _SourceWork) -> _SourceResult:
        """One worker, one source, materials in order."""
        outcome = _SourceResult(source_id=work.source.id)
        for item, estimate in zip(work.items, work.estimates, strict=True):
            if stop.is_set():
                outcome.stopped = True
                return outcome
            try:
                budget.reserve(estimate)
            except BudgetExceeded as exc:
                # Not a crash: the ceiling did its job. Everything already
                # extracted goes back for writing.
                stop.set()
                outcome.stopped = True
                outcome.exhausted = True
                outcome.error = str(exc)
                return outcome
            try:
                result = enricher.enrich(item, work.source)  # type: ignore[union-attr]
            except Exception as exc:  # the protocol forbids it; survive anyway
                budget.settle(estimate, 0.0)
                result = EnrichResult(
                    source_id=work.source.id,
                    url=item.url,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                budget.settle(estimate, result.cost_usd)
            outcome.results.append(result)
        return outcome

    consumed: set[str] = set()

    def consume(outcome: _SourceResult) -> None:
        consumed.add(outcome.source_id)
        plan = plans[outcome.source_id]
        for result in outcome.results:
            plan.processed += 1
            plan.spent_usd += result.cost_usd
            report.materials_processed += 1
            if result.cached:
                report.cache_hits += 1
            model = _model_of(result) or config.models.get("enrich", "unknown")
            row = report.cost_by_model.setdefault(model, {"calls": 0, "cost_usd": 0.0})
            row["calls"] += 1
            row["cost_usd"] += result.cost_usd
            run_log.model_call(
                stage=STAGE_ENRICH,
                model=model,
                cost_usd=result.cost_usd,
                cached=result.cached,
            )
            if result.error:
                report.materials_failed += 1
                plan.failed += 1
                journal.record(
                    EventKind.STAGE_FAILED,
                    actor=STAGE_ENRICH,
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
                    actor=STAGE_ENRICH,
                    target=result.url,
                    outcome=Outcome.SKIPPED,
                    kind=rejected.kind,
                    value=rejected.value,
                    reason=rejected.reason,
                )

        if outcome.results:
            inserted, duplicate = persist_statements(conn, outcome.results)
            plan.statements_added += inserted
            plan.statements_duplicate += duplicate
            report.statements_added += inserted
            report.statements_duplicate += duplicate

        if outcome.exhausted and not report.budget_exhausted:
            report.budget_exhausted = True
            report.notes.append(outcome.error or "бюджет исчерпан")
            run_log.note(f"бюджет исчерпан: {outcome.error}")
            journal.record(
                EventKind.BUDGET_EXCEEDED,
                actor="backfill",
                target=outcome.source_id,
                outcome=Outcome.FAILED,
                spent_usd=round(budget.spent_usd, 6),
                limit_usd=limit_usd,
            )

        if outcome.stopped:
            report.sources_unfinished += 1
            emit(
                f"[!] {outcome.source_id}: источник не закончен, чекпоинт не ставлю; "
                f"при следующем запуске он начнётся заново"
            )
            return

        plan.completed = True
        report.sources_completed += 1
        journal.checkpoint(
            source_stage(outcome.source_id),
            item_count=plan.processed,
            statements=plan.statements_added,
            duplicates=plan.statements_duplicate,
            cost_usd=round(plan.spent_usd, 6),
        )
        emit(
            f"[{report.sources_completed}/{report.sources_total}] {outcome.source_id}: "
            f"материалов {plan.processed}, записей +{plan.statements_added}, "
            f"дублей {plan.statements_duplicate}, потрачено {budget.spent_usd:.4f}, "
            f"остаток {budget.remaining_usd:.4f} USD"
        )

    futures: dict[Future[_SourceResult], str] = {}
    with run_log.stage(STAGE_ENRICH, in_count=report.materials_pending) as record:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        try:
            futures = {
                pool.submit(process_source, work): work.source.id for work in works
            }
            try:
                for future in as_completed(futures):
                    consume(future.result())
            except KeyboardInterrupt:
                # Ctrl-C is a supported way to stop: workers finish the call
                # in flight, everything already extracted is written, and the
                # completed sources keep their checkpoints.
                report.interrupted = True
                stop.set()
                emit("Прерывание: сохраняю уже полученное и закрываю прогон.")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        # Anything that finished while the pool was draining.
        for future, source_id in futures.items():
            if source_id in consumed or future.cancelled() or not future.done():
                continue
            try:
                consume(future.result())
            except Exception as exc:  # a worker that died takes only its source
                plans[source_id].error = f"{type(exc).__name__}: {exc}"
                report.sources_unfinished += 1
        record["out_count"] = report.statements_added

    report.spent_usd = budget.spent_usd

    # -- trends over the whole corpus, then the readiness verdict
    with run_log.stage(STAGE_TRENDS):
        accepted, rejected, saved = refresh_trends(
            conn, config, options.min_trend_members
        )
    journal.checkpoint(STAGE_TRENDS, item_count=saved)
    report.trends_accepted = [candidate_row(c) for c in accepted]
    report.trends_rejected = [candidate_row(c) for c in rejected]

    report.cells = corpus_cells(conn)
    report.readiness = corpus_readiness(conn, config.data)
    report.plans = sorted(plans.values(), key=lambda p: -p.statements_added)
    report.duration_s = time.monotonic() - started

    run_log.finish("ok" if report.complete else "partial")
    journal.record(
        EventKind.RUN_FINISHED,
        actor="backfill",
        target=run_id,
        outcome=Outcome.OK if report.complete else Outcome.PARTIAL,
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
    started: float,
) -> BackfillReport:
    """Collect and price the work without opening a single model call."""
    items, outcomes = collect_all(
        config, fetcher, mode="backfill", sources=sources, max_workers=6
    )
    plans = {s.id: SourcePlan(source_id=s.id, priority=s.priority) for s in sources}
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
        plan = plans.get(str(item.extra.get("source_id", "")))
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
    return report
