"""Data contracts for the pipeline.

The Signal type (PRD 5.7) is the only thing surfaces ever see. It is frozen
before backfill starts: a signal designed narrowly makes later surfaces
unbuildable on data already collected.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

SIGNAL_SCHEMA_VERSION = 1


class ChangeType(StrEnum):
    RELEASE = "release"
    BREAKING_CHANGE = "breaking_change"
    DEPRECATION = "deprecation"
    PRICING = "pricing"
    LIMITS = "limits"
    SECURITY = "security"
    OTHER = "other"


class FactKind(StrEnum):
    VERSION = "version"
    EFFECTIVE_DATE = "effective_date"
    SUNSET_DATE = "sunset_date"
    PRICE = "price"
    LIMIT = "limit"
    AFFECTED_PRODUCT = "affected_product"


class DeltaStatus(StrEnum):
    NEW = "new"
    CONTINUING = "continuing"
    UPDATED = "updated"
    RESOLVED = "resolved"


class ContextLabel(StrEnum):
    # "not_found_in_corpus" rather than "isolated": absence of precedents is an
    # artifact of corpus coverage as much as a property of the event, and the
    # label must not claim more than the corpus can support.
    NOT_FOUND_IN_CORPUS = "not_found_in_corpus"
    RECURRING = "recurring"
    TREND_MEMBER = "trend_member"
    ESCALATION = "escalation"


class Trajectory(StrEnum):
    EMERGING = "emerging"
    STEADY = "steady"
    ACCELERATING = "accelerating"
    DORMANT = "dormant"
    CLOSED = "closed"


class SignalType(StrEnum):
    DIGEST_ITEM = "digest_item"
    QUIET_DAY = "quiet_day"
    RUN_FAILURE = "run_failure"


class Tier(StrEnum):
    """Channel-independent importance band.

    Surfaces are forbidden to filter by significance (SUR-2), yet the config
    holds per-channel thresholds, which leaks knowledge of Telegram into the
    core. The core assigns a tier; each surface maps its own capacity onto it.
    """

    LEAD = "lead"
    STANDARD = "standard"
    BACKGROUND = "background"


class DatePrecision(StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    # Changelogs routinely write "Mar 14" with no year, and a model asked for a
    # date will supply the current one. Inferred dates must stay marked.
    INFERRED = "inferred"


class SourceStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    # HTTP 200 carrying zero extractable items. Client-rendered pages fail this
    # way, and FR-1.4 would otherwise record them as successful.
    EMPTY = "empty"
    # Checked, answered with its usual contents, nothing of it inside the
    # window. A repository that shipped no release today is not a fault, and
    # the product exists to tell that day apart from a broken one.
    QUIET = "quiet"


class Fact(BaseModel):
    """PRD 5.3. A fact without verbatim evidence is never published (FR-4.3)."""

    kind: FactKind
    value: str
    source_url: str
    evidence: str = Field(description="verbatim quote from the source, <= 15 words")
    confidence: Literal["high", "medium", "low"] = "medium"
    # Set by verify_evidence(), not by the model.
    evidence_verified: bool = False
    # Parsed form of `value` for date kinds. Without it every surface reparses
    # a free-form string to say "in 59 days", and each does it differently.
    value_date: date | None = None
    # A date recovered from context must stay marked as such: showing
    # "in 59 days" for a guessed year is false precision.
    date_precision: DatePrecision = DatePrecision.DAY
    # What the date is about, e.g. "claude-3-opus". Needed to render the
    # upcoming-deadlines block without inventing the subject.
    subject: str | None = None


class Precedent(BaseModel):
    """A corpus record backing a context label.

    Denormalized on purpose: SIG-3 requires a signal to render whole without
    reaching into other stores, so an id alone would break every surface.
    """

    statement_id: str
    text: str
    source_url: str
    event_date: date | None = None
    date_precision: DatePrecision = DatePrecision.DAY
    vendor: str
    change_type: ChangeType


class RawItem(BaseModel):
    """PRD 5.1."""

    id: str
    source_id: str
    url: str
    title: str
    published_at: datetime | None = None
    collected_at: datetime
    raw_text: str = ""
    seen_in: list[str] = Field(default_factory=list)


class EventStatement(BaseModel):
    """PRD 5.5. The unit of the corpus: one event, one normalized statement.

    Provenance fields are mandatory. The corpus is append-only and made of
    judgments from one model on one evening; without them it cannot be audited
    or selectively rebuilt later.
    """

    statement_id: str
    cluster_id: str | None = None
    text: str
    vendor: str
    product: str | None = None
    change_type: ChangeType
    event_date: date | None = None
    date_precision: DatePrecision = DatePrecision.DAY
    version: str | None = None
    source_url: str
    evidence: str
    ingested_at: datetime
    ingest_mode: Literal["live", "backfill"]
    extractor_model: str
    prompt_version: str
    raw_material_ref: str = Field(description="cache key of the archived source text")
    embedding: list[float] | None = None


class Trend(BaseModel):
    """PRD 5.4. Built on top of the grouping query, never in place of it."""

    trend_id: str
    label: str
    vendor: str | None = None
    change_types: list[ChangeType] = Field(default_factory=list)
    member_statement_ids: list[str] = Field(default_factory=list)
    first_observed: date | None = None
    last_observed: date | None = None
    cadence_days: float | None = None
    trajectory: Trajectory = Trajectory.EMERGING
    evidence_refs: list[str] = Field(default_factory=list)


class RetrievalReport(BaseModel):
    """Both counts travel to the run log.

    The strict conjunctive filter turns a miss into a confident
    "no precedents found". Recording the relaxed count makes the miss visible.
    """

    strict_hits: int = 0
    relaxed_hits: int = 0
    # Everything the strict filter matched, counted by the corpus. `shown` is
    # how many of them fit the page. A published number must come from
    # `total_found`; taking it from the list length reports `max_results`.
    total_found: int = 0
    shown: int = 0
    # The oldest strict match, from the same aggregate as `total_found`. The
    # page is capped and ordered by relevance, so its own oldest record is
    # younger than the corpus's whenever the cap bites — pairing the true
    # count with the visible date would claim a span nothing backs.
    earliest_event_date: date | None = None
    windows_days: list[int] = Field(default_factory=list)


class RunSummary(BaseModel):
    """Facts about the run itself, carried on every signal.

    Surfaces are forbidden to read `source_runs` (SUR-1), so the names of
    sources that failed have to travel inside the contract. Counters alone
    make the footer of S5 and the case of DR-5 unrenderable.
    """

    sources_checked: int = 0
    sources_failed: list[str] = Field(default_factory=list)
    # HTTP 200 with nothing extractable: a different fault, named separately.
    # Faults only. A source checked with nothing new in the window carries
    # SourceStatus.QUIET and appears in neither list: naming it here would
    # make a digest accuse seventy working sources on an ordinary morning.
    sources_empty: list[str] = Field(default_factory=list)
    materials_collected: int = 0
    materials_filtered: int = 0
    last_success_date: date | None = None
    cost_usd: float = 0.0


class UpcomingDeadline(BaseModel):
    """A dated obligation already extracted, shown when the day is quiet.

    Silence is filled with what the reader planned to forget, and every field
    here comes from a verified fact rather than from a new inference.
    """

    when: date
    what: str
    vendor: str | None = None
    source_url: str | None = None
    date_precision: DatePrecision = DatePrecision.DAY


class Signal(BaseModel):
    """PRD 5.7. The contract between the core and every surface.

    Rules that hold at write time: no channel markup in any field, headline and
    summary stored unabridged, truncation is a surface operation.
    """

    schema_version: int = SIGNAL_SCHEMA_VERSION
    signal_id: str
    run_id: str
    signal_type: SignalType
    created_at: datetime
    # The day the digest is about, distinct from the moment it was produced.
    for_date: date

    headline: str = ""
    summary: str = ""
    why_it_matters: str = ""
    change_type: ChangeType | None = None
    vendor: str | None = None
    product: str | None = None

    facts: list[Fact] = Field(default_factory=list)
    primary_url: str | None = None
    duplicates_count: int = 0

    delta_status: DeltaStatus | None = None
    delta_note: str | None = None
    # A storyline seen before with nothing new to say. FR-5.3 puts these in a
    # folded section rather than the main list: a reader who saw the card
    # yesterday should not have to re-read it to find out nothing changed.
    # The core decides — a surface may not judge significance (SUR-2) — and
    # `DeltaOutcome.is_publishable` is where the judgement already lived,
    # written, tested and called by nobody.
    in_progress: bool = False
    days_tracked: int = 0
    context_label: ContextLabel | None = None
    trend_id: str | None = None
    precedents: list[Precedent] = Field(default_factory=list)
    retrieval: RetrievalReport | None = None
    # The sentence a reader sees above the precedent list, composed by the
    # core. A surface deriving it would be recomputing the corpus, which SUR-2
    # forbids, and three surfaces would word the same claim three ways.
    # Every number in it must be backed by `precedents`.
    context_note: str | None = None

    # The date the card leads with, chosen once. Three consumers used to pick
    # it three ways — the page took the first dated fact in list order, scoring
    # took the nearest future one, the core took a third — and they disagreed
    # on fifteen of thirty-four cards. A card announcing today's news carried
    # "expired 649 days ago" because one of them read a neighbouring row.
    due_date: date | None = None
    due_precision: DatePrecision = DatePrecision.DAY

    score: int = 0
    score_rationale: str = ""
    rank: int = 0
    tier: Tier = Tier.STANDARD

    # Carried on every signal type, not just quiet_day: the footer listing
    # unreachable sources belongs on an ordinary day too.
    run_summary: RunSummary | None = None
    # Populated on quiet_day: what is coming that the reader filed away.
    upcoming: list[UpcomingDeadline] = Field(default_factory=list)

    # Free-form counters beyond RunSummary. Kept for extension; surfaces must
    # not depend on ad-hoc keys appearing here.
    stats: dict[str, int] = Field(default_factory=dict)
    failure_reason: str | None = None
    failure_stage: str | None = None

    run_log_url: str | None = None
