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


class Fact(BaseModel):
    """PRD 5.3. A fact without verbatim evidence is never published (FR-4.3)."""

    kind: FactKind
    value: str
    source_url: str
    evidence: str = Field(description="verbatim quote from the source, <= 15 words")
    confidence: Literal["high", "medium", "low"] = "medium"
    # Set by verify_evidence(), not by the model.
    evidence_verified: bool = False


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
    total_found: int = 0
    shown: int = 0
    windows_days: list[int] = Field(default_factory=list)


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
    days_tracked: int = 0
    context_label: ContextLabel | None = None
    trend_id: str | None = None
    precedents: list[Precedent] = Field(default_factory=list)
    retrieval: RetrievalReport | None = None

    score: int = 0
    score_rationale: str = ""
    rank: int = 0
    tier: Tier = Tier.STANDARD

    # Populated on quiet_day and run_failure; harmless elsewhere.
    stats: dict[str, int] = Field(default_factory=dict)
    failure_reason: str | None = None

    run_log_url: str | None = None
