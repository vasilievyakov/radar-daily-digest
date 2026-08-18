"""Backfill: the one paid, one-off fill of the corpus (PRD 6).

Four properties decide whether the money spent here is recoverable.

* **A ceiling is mandatory** (FR-6.8). There is no default and no fallback to
  "unlimited": a run without a limit is refused before the first fetch. The
  limit is checked before every call, not recorded after it.
* **The unit of work is a batch of one source's materials**, walked in order
  by a single worker, with several batches running at once. Whole sources
  would be the tidier unit, but the distribution forbids it: at priority 1 one
  changelog holds 36 percent of all materials, so a per-source walk finishes
  when that one source finishes and seven workers idle meanwhile. Batching
  keeps everything a per-source walk was for — order inside a source is
  preserved, so `statement_index` stays sequential and race-free — while the
  tail disappears. Enrichment makes no network requests (the HTTP cache is
  warmed by collection), so splitting a source across workers cannot break the
  polite per-domain interval; the only shared external resource is the model,
  whose limits are global rather than per domain.
* **It resumes.** A checkpoint is written per finished batch, so an
  interruption loses at most one batch of work rather than a whole source.
  Spend already recorded on the run carries into the resumed run's budget, so
  the limit covers the whole effort rather than each attempt separately.
* **It is idempotent** (FR-6.7). The corpus key is the canonical source URL
  plus the position of the event inside its material, enforced by the UNIQUE
  index rather than by a lookup: `INSERT OR IGNORE` cannot race. One caveat
  the PRD did not foresee: a page whose headings carry no anchor hands over a
  hundred materials under one address, so the key also carries the material's
  slot on that URL (see `corpus_key`). Without it a hundred materials would
  overwrite each other at position zero. Materials already in the corpus are
  never sent to the model, so a repeat run neither duplicates nor pays twice —
  and a source whose parser later returns more materials simply contributes
  the new ones on the next run.
* **The prefix cache is warmed before the fan-out.** The Claude CLI prepends
  a system prefix of some sixteen thousand tokens and caches it; eight workers
  starting together all miss that cache and each pay to create it, measured at
  five times the warm price per material. One call goes first, alone.

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
from radar.cache import canonical_url, digest
from radar.collect import collect_all
from radar.config import ThemeConfig
from radar.contracts import EnrichResult, Enricher
from radar.db import corpus_readiness
from radar.fetch import Fetcher
from radar.normalize import IDENTITY_VERSION, event_identity
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
# Checkpoint stage per batch: "backfill:github_changelog:3".
STAGE_PREFIX = "backfill:"

DEFAULT_CONCURRENCY = 8
DEFAULT_BATCH_SIZE = 100

# One URL can carry many materials: a changelog page whose headings have no
# anchor gives every dated section the same address, and the collector keys a
# material by URL plus content for exactly that reason. The corpus key has to
# survive it, so the stored index is the material's slot on its URL times this
# number, plus the position of the event inside that material. Both halves are
# recomputed identically on every run, which is what keeps FR-6.7 true.
EVENTS_PER_MATERIAL = 10_000

# Cost profiles. Token arithmetic is right for OpenRouter, and wrong for the
# Claude CLI: the CLI carries a system prefix of some sixteen thousand tokens
# that the price list knows nothing about, and bills its own session. Measured
# in August 2026 on this project: 0.0079 USD per call with the prefix cached,
# about 0.039 USD for the call that creates the cache.
COST_PROFILE_TOKENS = "tokens"
COST_PROFILE_CLI = "claude-cli"
CLI_WARM_CALL_USD = 0.008
CLI_COLD_CALL_USD = 0.039

# Shape of one extraction call, used to price a run before it starts and to
# reserve budget before each call. Deliberately generous: an estimate that
# under-reserves lets the concurrent walk overshoot the ceiling.
PROMPT_OVERHEAD_TOKENS = 1200
OUTPUT_TOKENS_PER_MATERIAL = 700
CHARS_PER_TOKEN = 4
MAX_MATERIAL_TOKENS = 24000

# A batch of a hundred materials takes ten minutes; without a heartbeat the
# terminal is indistinguishable from a hung process for that whole time.
HEARTBEAT_SECONDS = 30.0

# Measured shape of one extraction call end to end. Only used to tell the
# operator how long a run will take before they start it.
SECONDS_PER_MATERIAL = 6.0

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
    """How many batches run at once, from `llm.concurrency`."""
    if concurrency is None:
        concurrency = int(config.section("llm").get("concurrency", DEFAULT_CONCURRENCY))
    return max(1, int(concurrency))


def price_for(model: str, config: ThemeConfig | None = None) -> tuple[float, float]:
    overrides = (config.section("llm").get("pricing") or {}) if config else {}
    row = overrides.get(model)
    if isinstance(row, dict):
        return float(row.get("input", 0.0)), float(row.get("output", 0.0))
    return PRICE_PER_MTOK.get(model, DEFAULT_PRICE_PER_MTOK)


def estimate_material_usd(
    item: CollectedItem,
    price: tuple[float, float],
    profile: str = COST_PROFILE_TOKENS,
) -> float:
    if profile == COST_PROFILE_CLI:
        # The material's length barely moves the needle next to a 16k prefix.
        return CLI_WARM_CALL_USD
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

    # Below this many settled calls the average is noise, and one cheap cache
    # hit would let the run reserve nothing.
    MIN_CALLS_FOR_AVERAGE = 3

    def __init__(self, limit_usd: float) -> None:
        super().__init__(limit_usd)
        self._lock = threading.Lock()
        self._settled_calls = 0
        self._settled_usd = 0.0

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
            self._settled_calls += 1
            self._settled_usd += actual_usd

    @property
    def observed_average_usd(self) -> float:
        """What a call is really costing, once there is evidence.

        A price table cannot know what a backend charges: the Claude CLI bills
        its own session and comes out well above the token list price. Holding
        the observed average keeps the ceiling meaningful whichever backend the
        run happens to use.
        """
        with self._lock:
            if self._settled_calls < self.MIN_CALLS_FOR_AVERAGE:
                return 0.0
            return self._settled_usd / self._settled_calls


@dataclass(slots=True)
class BackfillOptions:
    limit_usd: float | None = None
    source_ids: list[str] | None = None
    priorities: list[int] | None = None
    concurrency: int | None = None
    batch_size: int = DEFAULT_BATCH_SIZE
    cost_profile: str = COST_PROFILE_TOKENS
    warm_prefix: bool = True
    # Off when the model backend writes its own `model_calls` rows: those
    # carry tokens, which `EnrichResult` does not, and two writers would
    # count the same call twice.
    log_model_calls: bool = True
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
    batches: int = 0
    batches_done: int = 0
    batches_skipped: int = 0
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
    batch_size: int = DEFAULT_BATCH_SIZE
    duration_s: float = 0.0
    sources_total: int = 0
    sources_completed: int = 0
    sources_unfinished: int = 0
    batches_total: int = 0
    batches_done: int = 0
    batches_skipped: int = 0
    longest_batch: int = 0
    eta_seconds: float = 0.0
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
    warmup_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_usd: float = 0.0
    # Same materials priced on `models.enrich_critical`: the ceiling if every
    # one of them escalated. The real figure lands between the two.
    estimated_max_usd: float = 0.0
    model_base: str = ""
    model_critical: str = ""
    cache_hits: int = 0
    budget_exhausted: bool = False
    interrupted: bool = False
    cost_profile: str = COST_PROFILE_TOKENS
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


def batch_stage(source_id: str, batch_index: int) -> str:
    return f"{STAGE_PREFIX}{source_id}:{batch_index}"


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


def material_slots(items: list[CollectedItem]) -> list[int]:
    """Position of each material among the materials sharing its URL.

    Document order, so the same page parsed again yields the same slots and
    the corpus key does not move under a resumed run.
    """
    seen: dict[str, int] = defaultdict(int)
    slots: list[int] = []
    for item in items:
        url = canonical_url(item.url, keep_fragment=True)
        slots.append(seen[url])
        seen[url] += 1
    return slots


def corpus_key(url: str, slot: int, position: int) -> tuple[str, int, str]:
    """(source_url, statement_index, statement_id) for one event.

    The id is derived from the key rather than taken from the enricher: two
    materials on one URL both produce an event at position zero, and stage 4
    has no way to tell them apart. The writer does, because it knows the slot.
    """
    index = slot * EVENTS_PER_MATERIAL + position
    return url, index, f"{digest(url)[:16]}-{index:06d}"


def ingested_slots(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    """(canonical URL, slot) pairs the corpus already holds."""
    return {
        (row["source_url"], int(row["statement_index"]) // EVENTS_PER_MATERIAL)
        for row in conn.execute(
            "SELECT DISTINCT source_url, statement_index FROM event_statements"
        )
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
    conn: sqlite3.Connection,
    harvest: list[tuple[int, EnrichResult]],
    ingest_mode: str = INGEST_MODE,
) -> tuple[int, int]:
    """Write one batch's harvest to the corpus. Returns (inserted, ignored).

    Each result arrives with the slot of the material it came from, so the key
    is (canonical URL, slot, position) computed the same way on every run:
    stable across restarts, and unique even when a page hands over a hundred
    materials under one address.
    """
    inserted = 0
    duplicate = 0
    now = datetime.now(UTC)
    with conn:
        for slot, result in harvest:
            for position, statement in enumerate(result.statements):
                url, index, statement_id = corpus_key(
                    canonical_url(
                        statement.source_url or result.url, keep_fragment=True
                    ),
                    slot,
                    position,
                )
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO event_statements ("
                    "statement_id, cluster_id, text, vendor, product, change_type, "
                    "event_date, date_precision, version, source_url, statement_index, "
                    "evidence, ingested_at, ingest_mode, extractor_model, prompt_version, "
                    "raw_material_ref, embedding, supersedes, identity_version, event_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        statement_id,
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
                        # puts it in the corpus (FR-6.4). The daily run passes
                        # "live", and the distinction matters: a corpus that
                        # cannot say which records came from a nightly agent
                        # and which from one evening's backfill cannot be
                        # selectively rebuilt.
                        ingest_mode,
                        statement.extractor_model,
                        statement.prompt_version,
                        statement.raw_material_ref,
                        None,
                        None,
                        # One event, one record (PRD 5.5). The unique index on
                        # this column is what makes the sentence true: without
                        # it a page printing the same retirement in two tables
                        # put two precedents behind one event, and the card
                        # said "the eighth time since May" about one row read
                        # eight times.
                        IDENTITY_VERSION,
                        event_identity(
                            statement.vendor,
                            str(statement.change_type),
                            statement.event_date,
                            statement.product,
                            statement.evidence,
                            statement.text,
                        ),
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


def estimate_eta_seconds(batch_sizes: list[int], concurrency: int) -> float:
    """Wall clock a run will take: whichever binds, the queue or the longest batch."""
    if not batch_sizes:
        return 0.0
    total = sum(batch_sizes) * SECONDS_PER_MATERIAL
    longest = max(batch_sizes) * SECONDS_PER_MATERIAL
    return max(total / max(1, concurrency), longest)


# -- the run ----------------------------------------------------------


@dataclass(slots=True)
class _Task:
    source: SourceConfig
    batch_index: int
    items: list[CollectedItem]
    estimates: list[float]
    slots: list[int]
    warmup: bool = False

    @property
    def stage(self) -> str:
        if self.warmup:
            return f"{STAGE_PREFIX}{self.source.id}:warmup"
        return batch_stage(self.source.id, self.batch_index)


@dataclass(slots=True)
class _TaskResult:
    source_id: str
    batch_index: int
    stage: str
    harvest: list[tuple[int, EnrichResult]] = field(default_factory=list)
    warmup: bool = False
    stopped: bool = False
    exhausted: bool = False
    error: str | None = None


def build_tasks(
    source: SourceConfig,
    items: list[CollectedItem],
    *,
    known: set[tuple[str, int]],
    price: tuple[float, float],
    profile: str,
    batch_size: int,
    done_stages: dict[str, Any],
    plan: SourcePlan,
) -> list[_Task]:
    """Slice one source into batches, dropping what the corpus already has.

    Batches are cut over the collected list, not over the pending one, so a
    batch index means the same slice of the source on a resumed run even
    though the pending set has shrunk in between.
    """
    slots = material_slots(items)
    tasks: list[_Task] = []
    for offset in range(0, len(items), batch_size):
        batch_index = offset // batch_size
        pending: list[CollectedItem] = []
        estimates: list[float] = []
        pending_slots: list[int] = []
        for position in range(offset, min(offset + batch_size, len(items))):
            item = items[position]
            slot = slots[position]
            if (canonical_url(item.url, keep_fragment=True), slot) in known:
                # Already in the corpus: never sent to the model, which is
                # what makes a repeat run free rather than merely deduplicated.
                plan.already_ingested += 1
                continue
            pending.append(item)
            estimates.append(estimate_material_usd(item, price, profile))
            pending_slots.append(slot)
        if not pending:
            continue
        if batch_stage(source.id, batch_index) in done_stages:
            plan.batches_skipped += 1
            continue
        plan.batches += 1
        plan.pending += len(pending)
        plan.estimated_usd += sum(estimates)
        tasks.append(_Task(source, batch_index, pending, estimates, pending_slots))
    return tasks


def carve_warmup(tasks: list[_Task]) -> _Task | None:
    """Take the first material out of the biggest batch as a warm-up call.

    The Claude CLI prefixes every call with a system prompt of some sixteen
    thousand tokens and caches it. Eight workers starting at once all miss
    that cache and each pay to create it, which measured five times the warm
    price per material. One call first, then the fan-out.
    """
    if not tasks:
        return None
    head = tasks[0]
    warmup = _Task(
        source=head.source,
        batch_index=head.batch_index,
        items=[head.items.pop(0)],
        estimates=[head.estimates.pop(0)],
        slots=[head.slots.pop(0)],
        warmup=True,
    )
    if not head.items:
        tasks.pop(0)
    return warmup


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
    batch_size = max(1, int(options.batch_size or DEFAULT_BATCH_SIZE))
    if not options.dry_run and enricher is None:
        raise ValueError("нужен обогатитель: без него запускается только --dry-run")

    sources = select_sources(config, options.source_ids, options.priorities)

    if options.dry_run:
        report = BackfillReport(
            run_id="dry-run",
            limit_usd=limit_usd,
            dry_run=True,
            concurrency=concurrency,
            batch_size=batch_size,
            cost_profile=options.cost_profile,
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
        batch_size=batch_size,
        sources_total=len(sources),
        spent_usd=prior_cost,
    )
    if not sources:
        report.notes.append("нет источников, пригодных для бэкфилла")
        report.duration_s = time.monotonic() - started
        return report

    run_log = RunLog(conn, run_id, for_date or datetime.now(UTC).date())
    run_log.cost_usd = prior_cost
    run_log.note(
        f"backfill, лимит {limit_usd:.2f} USD, параллель {concurrency}, "
        f"пачка {batch_size}"
    )
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

    # -- collection: one pass, so batches can be cut and sorted by size.
    with run_log.stage(STAGE_COLLECT, in_count=len(sources)) as record:
        items, outcomes = collect_all(
            config,
            fetcher,
            run_log=run_log,
            mode="backfill",
            sources=sources,
            max_workers=min(concurrency, 8),
        )
        record["out_count"] = len(items)
    journal.checkpoint(STAGE_COLLECT, item_count=len(items))
    report.materials_collected = len(items)

    plans: dict[str, SourcePlan] = {
        s.id: SourcePlan(source_id=s.id, priority=s.priority) for s in sources
    }
    for outcome in outcomes:
        plan = plans.setdefault(
            outcome.source_id, SourcePlan(source_id=outcome.source_id)
        )
        plan.status = str(outcome.status)
        plan.collected = outcome.count
        plan.error = outcome.error

    by_source: dict[str, list[CollectedItem]] = defaultdict(list)
    for item in items:
        by_source[str(item.extra.get("source_id", ""))].append(item)

    known = ingested_slots(conn)
    report.model_base = config.models.get("enrich", "") or ""
    report.model_critical = config.models.get("enrich_critical", "") or ""
    report.cost_profile = options.cost_profile
    price = price_for(report.model_base, config)

    tasks: list[_Task] = []
    for source in sources:
        tasks.extend(
            build_tasks(
                source,
                by_source.get(source.id, []),
                known=known,
                price=price,
                profile=options.cost_profile,
                batch_size=batch_size,
                done_stages=done_stages,
                plan=plans[source.id],
            )
        )

    # Longest first: the naive order leaves seven workers idle while one drags
    # an 871-material changelog to the end of the run.
    tasks.sort(key=lambda t: -len(t.items))
    warmup_task = carve_warmup(tasks) if options.warm_prefix else None
    report.batches_total = len(tasks)
    report.batches_skipped = sum(p.batches_skipped for p in plans.values())
    report.materials_already_ingested = sum(p.already_ingested for p in plans.values())
    report.materials_pending = sum(len(t.items) for t in tasks) + (
        len(warmup_task.items) if warmup_task else 0
    )
    report.estimated_usd = sum(p.estimated_usd for p in plans.values())
    report.longest_batch = max((len(t.items) for t in tasks), default=0)
    report.eta_seconds = estimate_eta_seconds(
        [len(t.items) for t in tasks], concurrency
    )

    remaining_batches: dict[str, int] = defaultdict(int)
    for task in tasks:
        remaining_batches[task.source.id] += 1
    if warmup_task is not None:
        remaining_batches[warmup_task.source.id] += 1
    for source in sources:
        if remaining_batches[source.id] == 0:
            plans[source.id].completed = True
            report.sources_completed += 1

    emit(
        f"К обработке {report.materials_pending} материалов из "
        f"{report.materials_collected} собранных: {len(tasks)} заданий по "
        f"{batch_size}, параллель {concurrency}. Оценка "
        f"{report.estimated_usd:.2f} USD при лимите {limit_usd:.2f}, "
        f"примерно {report.eta_seconds / 60:.0f} минут."
    )

    stop = threading.Event()
    ticked = threading.Event()
    counter_lock = threading.Lock()
    counted = [0]

    def heartbeat() -> None:
        while not ticked.wait(HEARTBEAT_SECONDS):
            with counter_lock:
                done = counted[0]
            emit(
                f"  ... {done} из {report.materials_pending} материалов, "
                f"потрачено {budget.spent_usd:.4f}, "
                f"остаток {budget.remaining_usd:.4f} USD"
            )

    def process_batch(task: _Task) -> _TaskResult:
        """One worker, one batch, materials in order."""
        outcome = _TaskResult(
            task.source.id, task.batch_index, task.stage, warmup=task.warmup
        )
        for item, estimate, slot in zip(
            task.items, task.estimates, task.slots, strict=True
        ):
            if stop.is_set():
                outcome.stopped = True
                return outcome
            hold = max(estimate, budget.observed_average_usd)
            try:
                budget.reserve(hold)
            except BudgetExceeded as exc:
                # Not a crash: the ceiling did its job. Everything already
                # extracted goes back for writing.
                stop.set()
                outcome.stopped = True
                outcome.exhausted = True
                outcome.error = str(exc)
                return outcome
            try:
                result = enricher.enrich(item, task.source)  # type: ignore[union-attr]
            except Exception as exc:  # the protocol forbids it; survive anyway
                budget.settle(hold, 0.0)
                result = EnrichResult(
                    source_id=task.source.id,
                    url=item.url,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except BaseException:
                # Ctrl-C lands here: release the hold before unwinding, or the
                # resumed run inherits a charge for a call that never happened.
                budget.settle(hold, 0.0)
                stop.set()
                raise
            else:
                budget.settle(hold, result.cost_usd)
            outcome.harvest.append((slot, result))
            with counter_lock:
                counted[0] += 1
        return outcome

    consumed: set[str] = set()

    def consume(outcome: _TaskResult) -> None:
        consumed.add(outcome.stage)
        plan = plans[outcome.source_id]
        for _slot, result in outcome.harvest:
            plan.processed += 1
            plan.spent_usd += result.cost_usd
            report.materials_processed += 1
            if result.cached:
                report.cache_hits += 1
            model = _model_of(result) or config.models.get("enrich", "unknown")
            row = report.cost_by_model.setdefault(model, {"calls": 0, "cost_usd": 0.0})
            row["calls"] += 1
            row["cost_usd"] += result.cost_usd
            # Tokens are read off the result when stage 4 reports them:
            # `EnrichResult` does not carry them today, and FR-8.4 wants the
            # log to hold both money and tokens the moment it does.
            tokens_in = int(getattr(result, "tokens_in", 0) or 0)
            tokens_out = int(getattr(result, "tokens_out", 0) or 0)
            report.tokens_in += tokens_in
            report.tokens_out += tokens_out
            if options.log_model_calls:
                run_log.model_call(
                    stage=STAGE_ENRICH,
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
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
                    fact_kind=rejected.kind,
                    fact_value=rejected.value,
                    reason=rejected.reason,
                )

        if outcome.warmup:
            report.warmup_usd = sum(r.cost_usd for _s, r in outcome.harvest)

        if outcome.harvest:
            inserted, duplicate = persist_statements(conn, outcome.harvest)
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
            emit(
                f"[!] {outcome.source_id} #{outcome.batch_index}: пачка не закончена, "
                "чекпоинт не ставлю — при продолжении она начнётся заново"
            )
            return

        # Checkpoint carries what the batch produced and what it cost, so a
        # resumed run can report the whole effort and not just its last leg.
        journal.checkpoint(
            outcome.stage,
            item_count=len(outcome.harvest),
            statements=plan.statements_added,
            duplicates=plan.statements_duplicate,
            cost_usd=round(plan.spent_usd, 6),
        )
        remaining_batches[outcome.source_id] -= 1
        if remaining_batches[outcome.source_id] == 0:
            plan.completed = True
            report.sources_completed += 1
        if outcome.warmup:
            emit(
                f"Прогрев кэша префикса: {report.warmup_usd:.4f} USD, "
                f"остаток {budget.remaining_usd:.4f} USD"
            )
            return
        report.batches_done += 1
        plan.batches_done += 1
        emit(
            f"[{report.batches_done}/{report.batches_total}] {outcome.source_id} "
            f"#{outcome.batch_index}: материалов {len(outcome.harvest)}, "
            f"записей +{plan.statements_added}, дублей {plan.statements_duplicate}, "
            f"потрачено {budget.spent_usd:.4f}, остаток {budget.remaining_usd:.4f} USD"
        )

    if warmup_task is not None:
        # One call first, then the fan-out: eight workers starting together
        # all miss the prefix cache and each pay to create it, which measured
        # five times the warm price per material.
        emit("Прогреваю кэш системного префикса одним вызовом.")
        try:
            warmup_result = process_batch(warmup_task)
        except KeyboardInterrupt:
            # Ctrl-C during the very first call: nothing to write, but the run
            # still has to close cleanly so it can be resumed.
            report.interrupted = True
            stop.set()
            emit("Прерывание на прогреве, задания не раздаю.")
        else:
            consume(warmup_result)
            failed_warmup = [r for _s, r in warmup_result.harvest if r.error]
            if failed_warmup or not warmup_result.harvest:
                report.notes.append(
                    "прогрев кэша префикса не удался: стоимость будет выше расчётной"
                )
                emit(report.notes[-1])

    futures: dict[Future[_TaskResult], _Task] = {}
    ticker = threading.Thread(target=heartbeat, daemon=True)
    if progress is not None and tasks:
        ticker.start()
    with run_log.stage(STAGE_ENRICH, in_count=report.materials_pending) as record:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        try:
            futures = {pool.submit(process_batch, task): task for task in tasks}
            try:
                for future in as_completed(futures):
                    consume(future.result())
            except KeyboardInterrupt:
                # Ctrl-C is a supported way to stop: workers drop out between
                # materials, everything already extracted is written, and the
                # finished batches keep their checkpoints.
                report.interrupted = True
                stop.set()
                emit("Прерывание: сохраняю уже полученное и закрываю прогон.")
        finally:
            ticked.set()
            pool.shutdown(wait=True, cancel_futures=True)
        # Anything that finished while the pool was draining. Read the
        # exception rather than re-raising it: one dead batch must not take
        # the run's bookkeeping with it, and a Ctrl-C in flight is already
        # handled above.
        for future, task in futures.items():
            if task.stage in consumed or future.cancelled() or not future.done():
                continue
            failure = future.exception()
            if failure is not None:
                plans[task.source.id].error = f"{type(failure).__name__}: {failure}"
                if isinstance(failure, KeyboardInterrupt):
                    report.interrupted = True
                continue
            consume(future.result())
        record["out_count"] = report.statements_added

    report.spent_usd = budget.spent_usd
    report.sources_unfinished = sum(
        1 for count in remaining_batches.values() if count > 0
    )

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

    # The run row's cost is the budget's number whoever wrote the call rows:
    # it is what a resumed run reads back to know what has been spent.
    run_log.cost_usd = report.spent_usd
    run_log.model_calls = max(run_log.model_calls, report.materials_processed)
    run_log.tokens_in = max(run_log.tokens_in, report.tokens_in)
    run_log.tokens_out = max(run_log.tokens_out, report.tokens_out)
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


def _price_ratio(critical: tuple[float, float], base: tuple[float, float]) -> float:
    """How much dearer the critical model is, weighted the way a call is."""
    base_cost = base[0] * PROMPT_OVERHEAD_TOKENS + base[1] * OUTPUT_TOKENS_PER_MATERIAL
    if base_cost <= 0:
        return 1.0
    critical_cost = (
        critical[0] * PROMPT_OVERHEAD_TOKENS + critical[1] * OUTPUT_TOKENS_PER_MATERIAL
    )
    return max(1.0, critical_cost / base_cost)


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

    by_source: dict[str, list[CollectedItem]] = defaultdict(list)
    for item in items:
        by_source[str(item.extra.get("source_id", ""))].append(item)

    known = ingested_slots(conn)
    report.model_base = config.models.get("enrich", "") or ""
    report.model_critical = config.models.get("enrich_critical", "") or ""
    price = price_for(report.model_base, config)
    critical_price = price_for(report.model_critical or report.model_base, config)
    escalation_factor = (
        _price_ratio(critical_price, price) if report.model_critical else 1.0
    )
    batch_sizes: list[int] = []
    for source in sources:
        tasks = build_tasks(
            source,
            by_source.get(source.id, []),
            known=known,
            price=price,
            profile=report.cost_profile,
            batch_size=report.batch_size,
            done_stages={},
            plan=plans[source.id],
        )
        batch_sizes.extend(len(t.items) for t in tasks)

    report.materials_collected = len(items)
    report.materials_already_ingested = sum(p.already_ingested for p in plans.values())
    report.materials_pending = sum(p.pending for p in plans.values())
    report.estimated_usd = sum(p.estimated_usd for p in plans.values())
    if report.cost_profile == COST_PROFILE_CLI and report.materials_pending:
        # The first call pays to create the prefix cache; the rest read it.
        report.estimated_usd += CLI_COLD_CALL_USD - CLI_WARM_CALL_USD
    report.estimated_max_usd = report.estimated_usd * escalation_factor
    report.batches_total = len(batch_sizes)
    report.longest_batch = max(batch_sizes, default=0)
    report.eta_seconds = estimate_eta_seconds(batch_sizes, report.concurrency)
    report.plans = sorted(plans.values(), key=lambda p: -p.pending)
    report.cells = corpus_cells(conn)
    report.readiness = corpus_readiness(conn, config.data)
    report.duration_s = time.monotonic() - started
    if report.estimated_usd > report.limit_usd:
        report.notes.append(
            f"оценка {report.estimated_usd:.2f} USD превышает лимит "
            f"{report.limit_usd:.2f} USD: прогон остановится, не закончив"
        )
    return report


__all__ = [
    "BackfillOptions",
    "BackfillReport",
    "BudgetNotSet",
    "SharedBudget",
    "SourcePlan",
    "batch_stage",
    "build_tasks",
    "corpus_cells",
    "estimate_eta_seconds",
    "estimate_material_usd",
    "find_unfinished_run",
    "ingested_slots",
    "material_slots",
    "corpus_key",
    "persist_statements",
    "price_for",
    "refresh_trends",
    "resolve_concurrency",
    "resolve_limit",
    "run_backfill",
    "select_sources",
]
