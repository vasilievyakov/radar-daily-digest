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

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from radar.backfill import persist_statements
from radar.cluster import cluster_items
from radar.collect import collect_all
from radar.config import ThemeConfig
from radar.contracts import Enricher, EnrichResult
from radar.db import publish_signals
from radar.delta import (
    compute_delta,
    filter_unseen,
    prune_state,
    resolve_expired,
    save_state,
)
from radar.fetch import Fetcher
from radar.journal import EventKind, Journal, Outcome
from radar.language import days as russian_days, sentence as russian_sentence
from radar.normalize import subject_identity
from radar.models import (
    DatePrecision,
    Fact,
    FactKind,
    Signal,
    SourceStatus,
    Tier,
)
from radar.publish import (
    MONTHS_GENITIVE,
    choose_due_date,
    build_quiet_day,
    build_run_failure,
    build_run_summary,
    build_signal,
)
from radar.retrieval import CorpusRetriever
from radar.runlog import Budget, BudgetExceeded, RunLog, new_run_id
from radar.supervisor import Supervisor
from radar.scoring import (
    assign_tier,
    change_type_labels,
    rank_signals,
    vendor_labels,
)


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


def _readable(slug: str) -> str:
    """Last-resort name for a source without a label in the config."""
    words = slug.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else slug


# A headline is a claim about the event, and the only place the pipeline can
# get one is the extracted statement: literary Russian, quantifier-checked,
# every fact behind it verified. Three numbers tune how much of that statement
# survives into the headline; nothing else in here is tunable.
_HEADLINE_MIN_CHARS = 15
# Above this a bracketed restatement of a model id is worth dropping: the id
# stays in `summary` and in the affected_product facts either way.
_HEADLINE_COMFORT_CHARS = 110
# Above this a trailing dependent clause is worth dropping too.
_HEADLINE_LONG_CHARS = 130

# The core writes Russian. A candidate with no Cyrillic in it did not come
# from the core's own formulation, which makes it raw source text — the name
# of a changelog page, a release tag — and never a statement about an event.
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)

# A full stop ends a sentence and a semicolon separates two complete clauses;
# both bound the first claim. The required whitespace after the mark is what
# keeps the dots inside "v5.0.0-beta.8" and "gemini-2.5-pro" from counting.
_CLAUSE_END_RE = re.compile(r"(?<=[.!?;])\s+")

_PARENTHETICAL_RE = re.compile(r"\s*\([^()]{0,120}\)")

# Gerund and participle endings, and the prepositions that open a dependent
# tail. A comma followed by one of these is a clause boundary: cutting there
# leaves a whole sentence standing. A comma followed by anything else — a bare
# model id, a conjunction — sits inside an enumeration, and cutting there
# would leave half a list.
_DEPENDENT_TAIL_RE = re.compile(
    r"^(?:[а-яё]{4,}(?:ая|яя|уя|юя|ые|ый|ое|ых)|с|со|для|при|без)\b",
    re.IGNORECASE,
)

# The change type rendered as a predicate. Not a new judgement: the type is
# the enricher's and already on the cluster, and this table only conjugates
# the label the config gives it. Nothing here claims more than the type does —
# "объявляет об отключении", not "отключает", because a deprecation is an
# announcement and the shutdown is still ahead.
_TYPE_PREDICATE: dict[str, tuple[str, str]] = {
    "deprecation": ("объявляет об отключении", ""),
    "breaking_change": ("вносит ломающее изменение", "в"),
    "pricing": ("меняет цену", "на"),
    "limits": ("меняет лимиты", "в"),
    "security": ("публикует изменение в безопасности", "для"),
    "release": ("выпускает", ""),
    "other": ("сообщает об изменении", "в"),
}

_DATED_FACT_KINDS = {FactKind.SUNSET_DATE.value, FactKind.EFFECTIVE_DATE.value}
# A year recovered from context is not a date a headline may state.
_FIRM_PRECISIONS = {DatePrecision.DAY.value, DatePrecision.MONTH.value}


def _reads_as_statement(text: str) -> bool:
    """True when the text is the core's own Russian, not raw source material."""
    return len(text) >= _HEADLINE_MIN_CHARS and bool(_CYRILLIC_RE.search(text))


def _first_clause(text: str) -> str:
    """The opening claim of a statement, whole."""
    head = _CLAUSE_END_RE.split(text.strip(), maxsplit=1)[0]
    return head.strip().strip(",;").rstrip(".").strip()


def _tighten(clause: str) -> str:
    """Drop what a headline can lose without losing the claim.

    Two edits, both structural, both leaving a grammatical sentence behind: a
    bracketed restatement of the id already named beside it, and a trailing
    dependent clause. Neither is truncation — nothing is cut mid-thought and
    no ellipsis is written. Everything dropped here is still in `summary` and
    in the facts, which is where a surface goes for it.
    """
    if len(clause) > _HEADLINE_COMFORT_CHARS and "(" in clause:
        stripped = _PARENTHETICAL_RE.sub("", clause).strip()
        if len(stripped) >= _HEADLINE_MIN_CHARS:
            clause = stripped
    if len(clause) > _HEADLINE_LONG_CHARS:
        head, separator, tail = clause.rpartition(", ")
        if separator and len(head) >= 40 and _DEPENDENT_TAIL_RE.match(tail):
            clause = head.rstrip(" ,")
    return clause


def _headline_subject(lead: Any, facts: list[Fact]) -> str:
    """What the change is about, taken from data and never guessed."""
    product = getattr(lead, "product", None) if lead is not None else None
    if product and str(product).strip():
        return str(product).strip()
    for fact in facts:
        if str(fact.kind) == FactKind.AFFECTED_PRODUCT.value and fact.value.strip():
            return fact.value.strip()
    for fact in facts:
        if fact.subject and fact.subject.strip():
            return fact.subject.strip()
    return ""


def _headline_date(facts: list[Fact], subject: str) -> str:
    """The deadline the subject carries, or nothing at all.

    A date reaches a headline only when a fact holds it at day or month
    precision, and only when it belongs to the subject the headline names.
    Two models retired on two dates under one announcement is the ordinary
    case in a deprecations registry, and lifting one of those dates onto a
    headline about the other invents a deadline.
    """
    dated: list[tuple[date, Fact]] = []
    for fact in facts:
        parsed = fact.value_date
        if parsed is None or str(fact.kind) not in _DATED_FACT_KINDS:
            continue
        if str(fact.date_precision) not in _FIRM_PRECISIONS:
            continue
        dated.append((parsed, fact))
    if not dated:
        return ""

    owned = [
        pair
        for pair in dated
        if subject and pair[1].subject and pair[1].subject.strip() == subject
    ]
    if owned:
        when, chosen = min(owned, key=lambda pair: pair[0])
    elif len({pair[0] for pair in dated}) == 1:
        when, chosen = dated[0]
    else:
        # Several deadlines and no way to tell which one is this subject's.
        return ""

    month = MONTHS_GENITIVE[when.month - 1]
    if str(chosen.date_precision) == DatePrecision.MONTH.value:
        return f"с {month} {when.year} года"
    return f"с {when.day} {month} {when.year} года"


def _composed_headline(
    cluster: Any,
    lead: Any,
    facts: list[Fact],
    vendor_names: dict[str, str],
    type_names: dict[str, str],
) -> str:
    """Who did what, with what — assembled from fields when no statement exists.

    Reached when stage four produced facts but no usable statement. Every part
    comes off the signal: the vendor and the change type off the cluster, the
    subject off the lead or the affected_product facts, the date off a
    verified fact.
    """
    change_type = str(cluster.change_type or "").strip()
    vendor_slug = str(cluster.vendor or "").strip()
    vendor = vendor_names.get(vendor_slug, "") or vendor_slug
    subject = _headline_subject(lead, facts)
    when = _headline_date(facts, subject)

    if vendor:
        predicate, preposition = _TYPE_PREDICATE.get(
            change_type, _TYPE_PREDICATE["other"]
        )
        words = [vendor, predicate]
        if subject:
            if preposition:
                words.append(preposition)
            words.append(subject)
        head = " ".join(words)
    elif subject and change_type:
        head = f"{type_names.get(change_type, change_type)}: {subject}"
    else:
        return ""

    return f"{head} {when}" if when else head


def _headline_for(
    cluster: Any,
    lead: Any,
    facts: list[Fact] | None = None,
    vendor_names: dict[str, str] | None = None,
    type_names: dict[str, str] | None = None,
) -> str:
    """A claim about the event, never the name of the page it was found on.

    A source page title names the whole changelog — "Model status", "Legacy
    and end-of-life (EOL) models", "Schema changes for 2026-08-17" — is
    identical for every event on it, and tells a reader deciding whether today
    concerns him nothing at all. The extracted statement names this change, so
    it goes first; with no statement the fields compose one. The title is
    reached only by a cluster carrying no statement, no vendor, no change type
    and no facts, which is a cluster with nothing in it.

    The gate here used to reject any statement whose first sentence ran past
    120 characters and fall straight through to the title. Sixteen of the
    thirty-four headlines in the run of 2026-08-18 were page names for that
    reason alone, and every one of them had a usable statement behind it.
    """
    facts = list(facts or [])
    vendor_names = vendor_names or {}
    type_names = type_names or {}

    if lead is not None and str(getattr(lead, "text", "")).strip():
        candidate = _tighten(_first_clause(str(lead.text)))
        if _reads_as_statement(candidate):
            return candidate

    composed = _composed_headline(cluster, lead, facts, vendor_names, type_names)
    if _reads_as_statement(composed):
        return composed

    # Nothing else exists on this cluster. A name is worse than a claim and
    # better than a blank card.
    return cluster.title


def _collapse_to_events(
    enriched: list[tuple], log: Any = None
) -> tuple[list[tuple], int]:
    """One card per event, however many places state it.

    Clustering runs before the model and can only compare raw text, so five
    pages announcing one Veo shutdown — a table row, a release note, two
    mirrors of the same document, a changelog line — stay five clusters and
    become five cards. What they have in common only exists after extraction:
    the vendor, the kind of change and the model being retired.

    The materials are merged rather than dropped, so `duplicates_count` counts
    them and the card can say it was seen in five places. Facts are unioned:
    a lifecycle table states the announcement date and the shutdown date in
    different rows, and the card needs both.
    """
    winners: dict[str, tuple] = {}
    order: list[str] = []
    collapsed = 0

    for cluster, facts, statements in enriched:
        lead = statements[0] if statements else None
        key = subject_identity(
            cluster.vendor or "",
            cluster.change_type or "",
            lead.product if lead else None,
            lead.evidence if lead else None,
            lead.text if lead else cluster.title,
        )
        if key not in winners:
            winners[key] = (cluster, list(facts), list(statements))
            order.append(key)
            continue

        kept_cluster, kept_facts, kept_statements = winners[key]
        # The richer extraction leads: more verified facts means more of the
        # event was actually read off the page.
        if len(facts) > len(kept_facts):
            kept_cluster, cluster = cluster, kept_cluster
            kept_facts, facts = list(facts), kept_facts
            kept_statements, statements = list(statements), kept_statements

        kept_cluster.items.extend(cluster.items)
        for source in cluster.seen_in:
            if source not in kept_cluster.seen_in:
                kept_cluster.seen_in.append(source)
        seen = {(f.kind, f.value) for f in kept_facts}
        for fact in facts:
            if (fact.kind, fact.value) not in seen:
                seen.add((fact.kind, fact.value))
                kept_facts.append(fact)
        for statement in statements:
            if statement.text not in {st.text for st in kept_statements}:
                kept_statements.append(statement)
        winners[key] = (kept_cluster, kept_facts, kept_statements)
        collapsed += 1
        if log is not None:
            log.filtered(
                url=cluster.primary.url,
                title=cluster.title,
                reason_code="то_же_событие",
                stage="collapse",
                note=f"уже описано другим материалом: {key}",
                # Five sections of one page share a URL. Without this the
                # collapse stage reported seven merges and could name two.
                item_key=cluster.cluster_id,
            )

    return [winners[key] for key in order], collapsed


def _why_it_matters(cluster: Any, facts: list[Fact], as_of: date) -> str:
    """Composed from verified facts, never from a second model call.

    Says why this deserves attention today, and every clause is backed by a
    fact that survived quote verification. Nothing here may claim more.
    """
    reasons: list[str] = []
    # The same choice the card leads with and scoring weighs. Three copies of
    # this arithmetic disagreed on fifteen cards of thirty-four.
    deadline, _precision = choose_due_date(facts, as_of)
    if deadline is not None:
        days = (deadline - as_of).days
        subject = next(
            (f.subject for f in facts if f.value_date == deadline and f.subject), None
        )
        what = f"{subject}: " if subject else ""
        reasons.append(
            f"{what}срок наступает через {russian_days(days)}"
            if days
            else f"{what}срок сегодня"
        )
    if cluster.change_type in {"deprecation", "breaking_change"}:
        reasons.append("работающий код перестанет работать без правки")
    elif cluster.change_type == "security":
        reasons.append("затрагивает безопасность")
    products = [f.value for f in facts if str(f.kind) == "affected_product"][:3]
    if products:
        reasons.append("затронуто: " + ", ".join(products))
    # Every clause, not only the first: joining with ". " and capitalising once
    # produced "Работающий код перестанет работать без правки. затронуто: …" on
    # the one line of the card written to make somebody get up and fix
    # something.
    return ". ".join(russian_sentence(reason) for reason in reasons if reason)


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
        progress: Any = None,
        sources: list[Any] | None = None,
    ) -> None:
        self.conn = conn
        self.config = config
        self.fetcher = fetcher
        self.enricher = enricher
        self.relevance_filter = relevance_filter
        self.for_date = for_date or datetime.now(UTC).date()
        self.run_id = run_id or new_run_id()
        self.log = RunLog(conn, self.run_id, self.for_date)
        # "Как это получено" was None on every signal ever produced, so the
        # one link that turns a claim into something checkable led nowhere.
        # Relative by default because the web surface writes both pages side
        # by side; a channel that leaves the machine needs the absolute form,
        # which is why it comes from config rather than being hardcoded here.
        base = str(config.delivery.get("run_log_url_base", "") or "").rstrip("/")
        self.run_log_url = f"{base}/run-log.html" if base else "run-log.html"
        if not base:
            # Said out loud rather than left to each surface to discover. The
            # relative path works on the site and is meaningless in a message:
            # email drops it silently, Telegram would carry a dead href.
            self.log.note(
                "delivery.run_log_url_base не задан: ссылка на лог прогона "
                "будет работать только на веб-странице"
            )
        self.journal = Journal(conn, log_dir=log_dir, run_id=self.run_id)
        self.budget = Budget(
            float(config.section("budget").get("max_usd_per_run", 0.5))
        )
        self.retriever = CorpusRetriever(conn, config.retrieval)
        # Injected rather than printing directly: the orchestrator stays
        # usable from a test and from a scheduler that wants silence.
        self.progress = progress
        self.sources = sources

    def execute(self) -> RunResult:
        # Yesterday's hung runs get their verdict written before today's
        # starts. Nothing had ever recorded a stall, so eight runs sat at
        # "running" into a second day and anything reading "the latest run"
        # picked up a zombie.
        try:
            Supervisor(self.conn, self.journal).close_stalled()
        except Exception as exc:  # never let housekeeping fail a run
            self.log.note(f"не удалось закрыть зависшие прогоны: {exc}")

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
            # Deliberately last: `finish()` freezes log_json, so anything
            # recorded after it never reaches the run-log page.
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
        # Real names, from the config. This used to map every slug to itself,
        # so the footer of the digest read "gh_google_gemini_gemini_cli
        # ответил, но ничего не отдал" while the neighbouring page said
        # "Google Gemini CLI" — the resolver existed and nobody called it.
        name_of = {s.id: (s.label or _readable(s.id)) for s in self.config.sources}
        vendor_names = vendor_labels(self.config.data)
        type_names = change_type_labels(self.config.data)

        result.failed_stage = "collect"
        with self.log.stage("collect") as record:
            items, outcomes = collect_all(
                self.config, self.fetcher, self.log, sources=self.sources
            )
            record["in_count"] = len(items)
            # A retirement table is dated entirely in the future, so a window
            # measured against the event date passes every row of it every
            # day, forever: forty rows arrived as today's news each morning
            # and a quiet day could never happen. Freshness is first sighting,
            # which only a stage with history can know.
            items = filter_unseen(self.conn, items, as_of=self.for_date)
            record["out_count"] = len(items)
        result.collected = len(items)

        # collect_all writes source_runs before the gate, so a table that
        # answered with seventeen familiar rows would be filed as "ok, 17" next
        # to an empty digest. The upsert lets the truth land after the gate.
        fresh = Counter(str(item.extra.get("source_id", "")) for item in items)
        for outcome in outcomes:
            if outcome.status is SourceStatus.OK and not fresh.get(outcome.source_id):
                self.log.source_result(
                    outcome.source_id,
                    SourceStatus.QUIET,
                    items_count=0,
                    latency_ms=outcome.latency_ms,
                    error=(
                        "проверен, новых записей нет; "
                        f"всего на источнике {outcome.count}"
                    ),
                )
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
        enriched: list[tuple[Any, list[Fact], list[Any]]] = []
        with self.log.stage("enrich", in_count=len(relevant)) as record:
            total = len(relevant)
            for position, cluster in enumerate(relevant, 1):
                # A call takes about a minute through the CLI backend. Silence
                # for that long is indistinguishable from a hung machine, on a
                # stage and in a terminal alike.
                if self.progress:
                    self.progress(
                        f"  обогащение {position} из {total}: {cluster.title[:56]}"
                    )
                source = self.config.source(
                    str(cluster.primary.extra.get("source_id", ""))
                )
                if source is None:
                    # An item whose source vanished from the config still has
                    # a vendor to lose: skip it loudly rather than enrich it
                    # against nothing.
                    self.log.filtered(
                        url=cluster.primary.url,
                        title=cluster.title,
                        reason_code="другое",
                        stage="enrich",
                        note="источник не найден в конфиге",
                    )
                    continue
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
                # The one judgement the model makes that every deterministic
                # stage below needs. Dropping it here cost 30 points of score
                # on every deprecation, made retrieval return nothing before
                # it even reached the corpus, and left the rationale talking
                # about plumbing instead of the news.
                if outcome.change_type is not None:
                    cluster.change_type = str(outcome.change_type)
                enriched.append((cluster, outcome.facts, outcome.statements))
            record["out_count"] = len(enriched)
        result.enriched = len(enriched)
        self.journal.checkpoint("enrich", item_count=len(enriched))

        # Before retrieval, so the corpus is not queried once per copy, and
        # before delta, so one event does not carry several histories.
        with self.log.stage("collapse", in_count=len(enriched)) as record:
            enriched, collapsed = _collapse_to_events(enriched, self.log)
            record["out_count"] = len(enriched)
            if collapsed:
                self.log.note(
                    f"{collapsed} материалов описывали событие, уже собранное "
                    f"из другого источника"
                )
        self.journal.checkpoint("collapse", item_count=len(enriched))

        # The loop closes here. Until this line the corpus held only what one
        # evening's backfill put in it: today's events never became tomorrow's
        # precedents, so "the third time since May" could only ever refer to
        # history loaded by hand. A daily agent that does not consolidate what
        # it learns is a daily agent with anterograde amnesia.
        harvest = [
            (index, EnrichResult(source_id="", url=cluster.primary.url,
                                 statements=list(statements)))
            for index, (cluster, _facts, statements) in enumerate(enriched)
        ]
        stored, already = persist_statements(self.conn, harvest, ingest_mode="live")
        self.log.note(
            f"в корпус записано {stored} событий сегодняшнего прогона"
            + (f", {already} уже там были" if already else "")
        )

        result.failed_stage = "contextualize"
        with self.log.stage("contextualize", in_count=len(enriched)) as record:
            contexts = []
            missed_by_type: list[int] = []
            for cluster, facts, statements in enriched:
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
                contexts.append((cluster, facts, delta, retrieval, statements))
                # Both counts reach the log. Every other system in the room
                # measures precision only; this line is what makes a miss
                # visible. A strict conjunctive filter turns a
                # misclassification into a confident "no precedents", and
                # without the relaxed number nobody can tell the difference.
                report = retrieval.report
                if report.relaxed_hits > report.strict_hits:
                    # Counted, not narrated. Fifteen near-identical lines of
                    # "strict 12, relaxed 27" were the last thing on the run-log
                    # page every morning: telemetry addressed to nobody. One
                    # sentence with the total says the same and can be acted on.
                    missed_by_type.append(report.relaxed_hits - report.strict_hits)
                    self.journal.record(
                        EventKind.LABEL_DOWNGRADED,
                        actor="contextualize",
                        target=cluster.cluster_id,
                        outcome=Outcome.PARTIAL,
                        strict_hits=report.strict_hits,
                        relaxed_hits=report.relaxed_hits,
                    )
                save_state(self.conn, cluster, facts, delta, self.run_id, self.for_date)
            if missed_by_type:
                self.log.note(
                    f"у {len(missed_by_type)} сюжетов расширенный поиск нашёл "
                    f"больше строгого: {sum(missed_by_type)} записей корпуса не "
                    f"попали под точный тип изменения"
                )
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
            signals = self._assemble_and_rank(
                contexts, summary, vendor_names, type_names
            )
            record["out_count"] = len(signals)
        self.journal.checkpoint("score", item_count=len(signals))

        result.failed_stage = "publish"
        with self.log.stage("publish", in_count=len(signals)) as record:
            if not signals and summary.sources_checked == 0:
                # Zero sources checked is not a quiet day, it is not knowing.
                # The two rendered identically — same headline, same calm tone,
                # with "0 sources checked" one line below in smaller type — so
                # a run that reached nothing said "nothing changed in your
                # stack" with complete confidence.
                signals = [
                    build_run_failure(
                        self.run_id, self.for_date, "collect",
                        "ни один источник не проверен: сказать о сегодняшнем "
                        "дне нечего",
                        summary, run_log_url=self.run_log_url,
                    )
                ]
                result.failed_stage = "collect"
            elif not signals:
                # PUB-4: silence is delivered as a record, not as nothing.
                signals = [
                    build_quiet_day(
                        self.conn, self.run_id, self.for_date, summary,
                        run_log_url=self.run_log_url,
                    )
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
            outcome = self.relevance_filter.run(clusters)
            for decision in outcome.unjudged:
                # Passed through without the model having judged it: worth
                # naming, because it is neither a keep nor a drop.
                self.log.note(f"{decision.url}: пропущен без решения модели")
            return outcome.clusters
        except BudgetExceeded:
            raise
        except (AttributeError, TypeError, NameError, ImportError):
            # Not an outage: a name that does not resolve, a signature that
            # does not match. Three times tonight such an error arrived as a
            # calm note about the outside world while the stage did nothing.
            raise
        except Exception as exc:
            self.log.note(f"фильтр не отработал, материалы пропущены дальше: {exc}")
            return clusters

    def _assemble_and_rank(
        self,
        contexts: list[tuple],
        summary: Any,
        vendor_names: dict[str, str] | None = None,
        type_names: dict[str, str] | None = None,
    ) -> list[Signal]:
        vendor_names = vendor_names or {}
        type_names = type_names or {}
        """Build signals first, then score and rank them.

        Scoring takes a whole Signal rather than loose fields, and that is the
        right way round: the factors of FR-6.1 live on the contract, so one
        function ranks a live run and a replayed one identically.
        """
        drafts: list[Signal] = []
        source_ids: dict[str, str] = {}
        for cluster, facts, delta, retrieval, statements in contexts:
            # The normalized statement is what the expensive stage produced:
            # one to three sentences in literary Russian, quantifier-checked,
            # every fact behind it verified. The page title and a slab of raw
            # changelog were standing in for it on the screen.
            lead = statements[0] if statements else None
            headline = _headline_for(
                cluster, lead, facts, vendor_names, type_names
            )
            # Named apart from `summary`, which is the RunSummary parameter of
            # this function: shadowing it silently replaced the run report
            # with a string and every signal failed validation.
            body = " ".join(st.text.strip() for st in statements[:3]).strip()
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
                headline=headline,
                summary=body or cluster.primary.raw_text[:2000],
                why_it_matters=_why_it_matters(cluster, facts, self.for_date),
                product=lead.product if lead else None,
                # Slugs used to travel into parameters named `label`, which
                # is how the context sentence read "anthropic: deprecation"
                # instead of "Anthropic: объявление об отключении".
                vendor_label=vendor_names.get(cluster.vendor or "", cluster.vendor or ""),
                change_type_label=type_names.get(
                    cluster.change_type or "", cluster.change_type or ""
                ),
                run_summary=summary,
                run_log_url=self.run_log_url,
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
                # Below the threshold, and therefore recorded. FR-8.3 asks for
                # every dropped material with a reason, and "fell under the
                # publication threshold" is a reason. Without this the funnel
                # on the run-log page stops adding up, and a reader checking
                # the arithmetic finds material that vanished without trace.
                self.log.filtered(
                    url=scored.signal.primary_url or scored.signal.signal_id,
                    title=scored.signal.headline,
                    reason_code="ниже_порога_публикации",
                    stage="score",
                    item_key=scored.signal.signal_id,
                    note=f"оценка {scored.breakdown.score}, порог "
                    f"{self.config.scoring.get('digest_threshold')}",
                )
                self.journal.record(
                    EventKind.ITEM_FILTERED,
                    actor="score",
                    target=scored.signal.signal_id,
                    outcome=Outcome.SKIPPED,
                    reason="ниже_порога_публикации",
                    score=scored.breakdown.score,
                )
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
                self.run_id, self.for_date, stage, reason, summary,
                run_log_url=self.run_log_url,
            )
            publish_signals(self.conn, self.run_id, [signal])
            result.signals = [signal]
        except Exception as exc:  # never mask the original failure
            self.log.note(f"не удалось опубликовать отчёт о падении: {exc}")
