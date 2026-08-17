"""Stage 6 — scoring and ranking (PRD 4.6).

No model is called here. FR-6.3 puts the factor weights in the theme config,
and a weight stops being a weight the moment a language model is the one
"applying" it: the same input then yields a different number on the next run
and the config edit that was supposed to change the order changes nothing.
The acceptance criterion of 4.6 — the user opens the ranking and sees which
factors put an item at the top — is only honest when every point is
arithmetic the code can show its work for.

Two consequences run through the module. `score_rationale` (FR-6.2) is
generated from the same breakdown that produced the number, so the sentence
can never disagree with the score. And urgency is measured against a
reference date passed by the caller, never against the system clock, so
replaying an old run reproduces the old ranking.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from radar.assertions import find_unsupported_quantifiers
from radar.models import DeltaStatus, FactKind, Signal, Tier

# Fallbacks used only when the corresponding config key is absent. A key that
# is present is used exactly as written, including a partial map: an author who
# removed a change type from the weights meant it to be worth nothing.
DEFAULT_CHANGE_TYPE_WEIGHTS: dict[str, float] = {
    "breaking_change": 30,
    "deprecation": 30,
    "security": 25,
    "limits": 18,
    "pricing": 18,
    "release": 10,
    "other": 4,
}
DEFAULT_NOVELTY_WEIGHTS: dict[str, float] = {
    "new": 15,
    "updated": 12,
    "continuing": 4,
    "resolved": 6,
}
DEFAULT_STACK_WEIGHT = 20.0
DEFAULT_URGENCY_WEIGHT = 20.0
DEFAULT_AUTHORITY_WEIGHT = 15.0

DEFAULT_PUBLISH_THRESHOLD = 55.0
DEFAULT_DIGEST_THRESHOLD = 35.0

DEFAULT_URGENCY_HORIZON_DAYS = 90.0
DEFAULT_URGENCY_FACT_KINDS: tuple[str, ...] = (
    FactKind.SUNSET_DATE.value,
    FactKind.EFFECTIVE_DATE.value,
)

DEFAULT_RATIONALE_MAX_FACTORS = 3
# A factor worth less than this share of the ceiling is filler in a one-line
# explanation, so it is scored but not named.
DEFAULT_RATIONALE_MIN_SHARE = 0.05

# Ties are broken by source priority; a signal whose source is unknown sorts
# after every signal whose source is known.
UNRANKED_SOURCE_PRIORITY = 99

FACTOR_CHANGE_TYPE = "change_type"
FACTOR_STACK = "stack_overlap"
FACTOR_URGENCY = "urgency"
FACTOR_AUTHORITY = "source_authority"
FACTOR_NOVELTY = "novelty"

# Fixed order, used as the last tie-break so equal contributions always render
# in the same sequence.
FACTOR_ORDER: tuple[str, ...] = (
    FACTOR_CHANGE_TYPE,
    FACTOR_URGENCY,
    FACTOR_STACK,
    FACTOR_AUTHORITY,
    FACTOR_NOVELTY,
)

FACTOR_LABELS: dict[str, str] = {
    FACTOR_CHANGE_TYPE: "Тип изменения",
    FACTOR_STACK: "Пересечение с активным стеком",
    FACTOR_URGENCY: "Срочность",
    FACTOR_AUTHORITY: "Авторитетность источника",
    FACTOR_NOVELTY: "Новизна",
}

FALLBACK_CHANGE_TYPE_LABELS: dict[str, str] = {
    "release": "Релиз",
    "breaking_change": "Ломающее изменение",
    "deprecation": "Объявление об отключении",
    "pricing": "Изменение цены",
    "limits": "Изменение лимитов",
    "security": "Изменение в безопасности",
    "other": "Прочее изменение",
}

NOVELTY_DETAILS: dict[str, str] = {
    DeltaStatus.NEW.value: "первое появление в сводке",
    DeltaStatus.UPDATED.value: "история обновилась",
    DeltaStatus.CONTINUING.value: "история продолжается",
    DeltaStatus.RESOLVED.value: "история закрыта",
}

EMPTY_RATIONALE = "Ни один фактор важности не набрал баллов"


@dataclass(frozen=True, slots=True)
class FactorContribution:
    """One line of the "why this is first" screen."""

    key: str
    label: str
    points: float
    max_points: float
    detail: str
    # False when the signal carries no data for the factor. Such a factor adds
    # nothing and takes nothing away (FR-4.4 applied to scoring: a missing date
    # is a missing date, not a low-urgency date).
    applied: bool

    @property
    def share(self) -> float:
        return self.points / self.max_points if self.max_points > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "points": round(self.points, 2),
            "max_points": round(self.max_points, 2),
            "detail": self.detail,
            "applied": self.applied,
        }


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """The score with its arithmetic attached."""

    score: int
    rationale: str
    factors: tuple[FactorContribution, ...]
    raw_points: float
    max_points: float
    source_priority: int | None = None
    due_date: date | None = None
    days_until_due: int | None = None

    def by_key(self, key: str) -> FactorContribution:
        for factor in self.factors:
            if factor.key == key:
                return factor
        raise KeyError(key)

    def top_factors(self, limit: int = 3) -> list[FactorContribution]:
        return _ordered_contributors(self.factors)[:limit]

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "rationale": self.rationale,
            "raw_points": round(self.raw_points, 2),
            "max_points": round(self.max_points, 2),
            "source_priority": self.source_priority,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "days_until_due": self.days_until_due,
            "factors": [factor.as_dict() for factor in self.factors],
        }


@dataclass(frozen=True, slots=True)
class ScoredSignal:
    """A ranked signal next to the breakdown that ranked it."""

    signal: Signal
    breakdown: ScoreBreakdown


def _cfg(config: Mapping[str, Any]) -> Mapping[str, Any]:
    return config.get("scoring") or {}


def _weights(config: Mapping[str, Any]) -> Mapping[str, Any]:
    return _cfg(config).get("weights") or {}


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _weight_map(weights: Mapping[str, Any], key: str, default: dict[str, float]) -> dict[str, float]:
    raw = weights.get(key)
    if not isinstance(raw, Mapping):
        return dict(default)
    return {str(name): _number(value, 0.0) for name, value in raw.items()}


def _normalize(value: str | None) -> str:
    return (value or "").strip().casefold()


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _plural_days(count: int) -> str:
    count = abs(count)
    if 11 <= count % 100 <= 14:
        return "дней"
    last = count % 10
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дня"
    return "дней"


def change_type_labels(config: Mapping[str, Any]) -> dict[str, str]:
    """Human labels for change types, taken from the theme config when present."""
    labels = dict(FALLBACK_CHANGE_TYPE_LABELS)
    for entry in (config.get("corpus") or {}).get("change_types") or []:
        if isinstance(entry, Mapping) and entry.get("id") and entry.get("label"):
            labels[str(entry["id"])] = str(entry["label"])
    return labels


def vendor_labels(config: Mapping[str, Any]) -> dict[str, str]:
    """Display names for vendor ids, so the rationale reads "OpenAI", not "openai"."""
    labels: dict[str, str] = {}
    for entry in (config.get("corpus") or {}).get("vendors") or []:
        if isinstance(entry, Mapping) and entry.get("id") and entry.get("label"):
            labels[_normalize(str(entry["id"]))] = str(entry["label"])
    return labels


def _url_key(url: str | None) -> str:
    text = (url or "").strip().casefold()
    for scheme in ("https://", "http://"):
        if text.startswith(scheme):
            text = text[len(scheme):]
            break
    if text.startswith("www."):
        text = text[4:]
    return text.rstrip("/")


def _host(url_key: str) -> str:
    return url_key.split("/", 1)[0]


def resolve_source_priority(
    config: Mapping[str, Any],
    *,
    url: str | None = None,
    source_id: str | None = None,
) -> int | None:
    """Find the configured priority of the source a signal came from.

    Returns None when the source is not in the config: an unrecognized source
    has no known authority, and inventing one would be a guess.
    """
    sources = config.get("sources") or []
    if source_id:
        for entry in sources:
            if entry.get("id") == source_id and entry.get("priority") is not None:
                return int(entry["priority"])
        return None

    target = _url_key(url)
    if not target:
        return None

    # Longest configured URL that prefixes the signal URL wins; a bare host
    # match is the fallback, since one host carries several sources.
    prefix_hit: int | None = None
    prefix_len = -1
    host_hit: int | None = None
    for entry in sources:
        if entry.get("priority") is None:
            continue
        base = _url_key(entry.get("url"))
        if not base:
            continue
        priority = int(entry["priority"])
        if target == base or target.startswith(base + "/"):
            if len(base) > prefix_len:
                prefix_len, prefix_hit = len(base), priority
        elif _host(target) == _host(base):
            host_hit = priority if host_hit is None else min(host_hit, priority)
    return prefix_hit if prefix_hit is not None else host_hit


def _parse_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def nearest_due_date(
    signal: Signal, kinds: Sequence[str], as_of: date
) -> tuple[date | None, int | None]:
    """Earliest sunset or effective date on the signal, relative to `as_of`.

    An unparsable value yields no date at all; the urgency factor then sits out
    the calculation instead of being handed a plausible number.
    """
    wanted = {str(kind) for kind in kinds}
    found = [
        parsed
        for fact in signal.facts
        if str(fact.kind) in wanted
        and (parsed := fact.value_date or _parse_date(fact.value)) is not None
    ]
    if not found:
        return None, None

    # The nearest deadline still ahead, not the earliest date on the card. A
    # deprecations registry carries its whole history, so a single material
    # can hold dates from two years back; taking the minimum produced "срок
    # истёк 649 дней назад" under a headline about today's announcement.
    ahead = [d for d in found if d >= as_of]
    if ahead:
        due = min(ahead)
        return due, (due - as_of).days

    # Everything is in the past: the obligation has already come due, and the
    # most recent one is what still matters. Urgency of a closed deadline is
    # not a reason to rank a signal higher.
    due = max(found)
    return due, (due - as_of).days


def _stack_match(signal: Signal, stack: Iterable[str]) -> tuple[str, str] | None:
    """Return (role, name) of the first stack hit, vendor before product."""
    wanted = {_normalize(item) for item in stack}
    wanted.discard("")
    if not wanted:
        return None
    for role, candidate in (("вендор", signal.vendor), ("продукт", signal.product)):
        if candidate and _normalize(candidate) in wanted:
            return role, candidate
    return None


def _urgency_detail(days_until: int) -> str:
    if days_until > 0:
        return f"дата через {days_until} {_plural_days(days_until)}"
    if days_until == 0:
        return "дата наступает сегодня"
    overdue = abs(days_until)
    return f"срок истёк {overdue} {_plural_days(overdue)} назад"


def _novelty_detail(status: DeltaStatus, days_tracked: int) -> str:
    if status is DeltaStatus.CONTINUING and days_tracked > 1:
        return f"история ведётся {days_tracked}-й день"
    return NOVELTY_DETAILS.get(str(status), "")


def _ordered_contributors(
    factors: Sequence[FactorContribution],
) -> list[FactorContribution]:
    position = {key: index for index, key in enumerate(FACTOR_ORDER)}
    named = [f for f in factors if f.applied and f.detail and f.points > 0]
    named.sort(key=lambda f: (-f.points, position.get(f.key, len(position))))
    return named


def build_rationale(
    factors: Sequence[FactorContribution],
    config: Mapping[str, Any],
    max_points: float,
) -> str:
    """One Russian sentence naming the two or three heaviest factors (FR-6.2)."""
    cfg = _cfg(config)
    limit = int(_number(cfg.get("rationale_max_factors"), DEFAULT_RATIONALE_MAX_FACTORS))
    min_share = _number(cfg.get("rationale_min_share"), DEFAULT_RATIONALE_MIN_SHARE)

    named = _ordered_contributors(factors)
    if not named:
        return EMPTY_RATIONALE
    floor = max_points * min_share
    significant = [f for f in named if f.points >= floor]
    chosen = (significant or named[:1])[: max(1, limit)]

    sentence = _upper_first(", ".join(f.detail for f in chosen))
    # FR-6.18 covers generated prose as well. The phrasings below carry no
    # bare quantifiers, so this guard is a tripwire on future edits.
    if find_unsupported_quantifiers(sentence):
        return EMPTY_RATIONALE
    return sentence


def score_signal(
    signal: Signal,
    config: Mapping[str, Any],
    *,
    as_of: date,
    stack: Iterable[str] | None = None,
    source_id: str | None = None,
) -> ScoreBreakdown:
    """Score one signal 0-100 over the FR-6.1 factors.

    `as_of` is the reference date of the run. Urgency is measured from it and
    never from the system clock, so a replay of an old run reproduces the old
    score.
    """
    cfg = _cfg(config)
    weights = _weights(config)

    change_type_weights = _weight_map(weights, "change_type", DEFAULT_CHANGE_TYPE_WEIGHTS)
    novelty_weights = _weight_map(weights, "novelty", DEFAULT_NOVELTY_WEIGHTS)
    stack_weight = _number(weights.get("stack_overlap"), DEFAULT_STACK_WEIGHT)
    urgency_weight = _number(weights.get("urgency"), DEFAULT_URGENCY_WEIGHT)
    authority_weight = _number(weights.get("source_authority"), DEFAULT_AUTHORITY_WEIGHT)

    max_change_type = max(change_type_weights.values(), default=0.0)
    max_novelty = max(novelty_weights.values(), default=0.0)

    labels = change_type_labels(config)
    vendors = vendor_labels(config)
    factors: list[FactorContribution] = []

    # Change type. Breaking changes and deprecations carry the top weight.
    if signal.change_type is not None:
        key = str(signal.change_type)
        label = _lower_first(labels.get(key, key))
        detail = f"{label} в {signal.product}" if signal.product else label
        factors.append(
            FactorContribution(
                key=FACTOR_CHANGE_TYPE,
                label=FACTOR_LABELS[FACTOR_CHANGE_TYPE],
                points=change_type_weights.get(key, 0.0),
                max_points=max_change_type,
                detail=detail,
                applied=True,
            )
        )
    else:
        factors.append(
            FactorContribution(
                FACTOR_CHANGE_TYPE, FACTOR_LABELS[FACTOR_CHANGE_TYPE], 0.0,
                max_change_type, "", False,
            )
        )

    # Urgency, measured from the run date.
    horizon = _number(cfg.get("urgency_horizon_days"), DEFAULT_URGENCY_HORIZON_DAYS)
    kinds = cfg.get("urgency_fact_kinds") or DEFAULT_URGENCY_FACT_KINDS
    due, days_until = nearest_due_date(signal, kinds, as_of)
    if due is None or horizon <= 0:
        factors.append(
            FactorContribution(
                FACTOR_URGENCY, FACTOR_LABELS[FACTOR_URGENCY], 0.0,
                urgency_weight, "", False,
            )
        )
    else:
        # Linear ramp: the horizon and beyond is worth nothing, today and
        # anything overdue is worth the full weight.
        ratio = min(1.0, max(0.0, (horizon - days_until) / horizon))
        factors.append(
            FactorContribution(
                key=FACTOR_URGENCY,
                label=FACTOR_LABELS[FACTOR_URGENCY],
                points=urgency_weight * ratio,
                max_points=urgency_weight,
                detail=_urgency_detail(days_until),
                applied=True,
            )
        )

    # Overlap with the stack the user actually runs on.
    active_stack = stack if stack is not None else cfg.get("active_stack") or ()
    matched = _stack_match(signal, active_stack)
    if matched is None:
        stack_detail = ""
    else:
        role, name = matched
        # A common noun leads the phrase, so sentence-initial capitalization
        # never mangles a name that is deliberately lowercase, such as n8n.
        stack_detail = f"{role} {vendors.get(_normalize(name), name)} в активном стеке"
    factors.append(
        FactorContribution(
            key=FACTOR_STACK,
            label=FACTOR_LABELS[FACTOR_STACK],
            points=stack_weight if matched else 0.0,
            max_points=stack_weight,
            detail=stack_detail,
            applied=matched is not None,
        )
    )

    # Source authority, read off the source priority in the config.
    priority = resolve_source_priority(config, url=signal.primary_url, source_id=source_id)
    if priority is None:
        factors.append(
            FactorContribution(
                FACTOR_AUTHORITY, FACTOR_LABELS[FACTOR_AUTHORITY], 0.0,
                authority_weight, "", False,
            )
        )
    else:
        factors.append(
            FactorContribution(
                key=FACTOR_AUTHORITY,
                label=FACTOR_LABELS[FACTOR_AUTHORITY],
                points=authority_weight * _priority_factor(cfg, priority),
                max_points=authority_weight,
                detail=(
                    "официальный первоисточник"
                    if priority == 1
                    else f"источник приоритета {priority}"
                ),
                applied=True,
            )
        )

    # Novelty: new outranks updated outranks continuing.
    if signal.delta_status is not None:
        factors.append(
            FactorContribution(
                key=FACTOR_NOVELTY,
                label=FACTOR_LABELS[FACTOR_NOVELTY],
                points=novelty_weights.get(str(signal.delta_status), 0.0),
                max_points=max_novelty,
                detail=_novelty_detail(signal.delta_status, signal.days_tracked),
                applied=True,
            )
        )
    else:
        factors.append(
            FactorContribution(
                FACTOR_NOVELTY, FACTOR_LABELS[FACTOR_NOVELTY], 0.0,
                max_novelty, "", False,
            )
        )

    factors.sort(key=lambda f: FACTOR_ORDER.index(f.key))
    raw_points = sum(f.points for f in factors)
    # The ceiling stays fixed whatever a given signal happens to carry, so an
    # absent date leaves the other factors' contributions untouched.
    max_points = (
        max_change_type + urgency_weight + stack_weight + authority_weight + max_novelty
    )
    if max_points > 0:
        score = int(math.floor(100.0 * raw_points / max_points + 0.5))
    else:
        score = 0
    score = max(0, min(100, score))

    return ScoreBreakdown(
        score=score,
        rationale=build_rationale(factors, config, max_points),
        factors=tuple(factors),
        raw_points=raw_points,
        max_points=max_points,
        source_priority=priority,
        due_date=due,
        days_until_due=days_until,
    )


def _priority_factor(cfg: Mapping[str, Any], priority: int) -> float:
    """Share of the authority weight a source of this priority earns."""
    table = cfg.get("source_priority_factor") or {}
    raw = table.get(priority, table.get(str(priority)))
    if raw is not None:
        return min(1.0, max(0.0, _number(raw, 0.0)))
    return 1.0 / priority if priority > 0 else 1.0


def assign_tier(score: int, config: Mapping[str, Any]) -> Tier:
    """Map a score onto a channel-independent band (FR-6.4 without the channel).

    The config thresholds are minimums: a score equal to `publish_threshold`
    reaches it. The core stops here; each surface maps its own capacity onto
    the tier.
    """
    cfg = _cfg(config)
    publish = _number(cfg.get("publish_threshold"), DEFAULT_PUBLISH_THRESHOLD)
    digest = _number(cfg.get("digest_threshold"), DEFAULT_DIGEST_THRESHOLD)
    if score >= publish:
        return Tier.LEAD
    if score >= digest:
        return Tier.STANDARD
    return Tier.BACKGROUND


def rank_signals(
    signals: Iterable[Signal],
    config: Mapping[str, Any],
    *,
    as_of: date,
    stack: Iterable[str] | None = None,
    source_ids: Mapping[str, str] | None = None,
) -> list[ScoredSignal]:
    """Score, order and tier a run's signals.

    Sorting is total and deterministic: score descending, then source priority
    ascending, then `signal_id`. Two runs over the same input produce the same
    ranks whatever order the input arrived in.
    """
    source_ids = source_ids or {}
    stack = list(stack) if stack is not None else None

    scored = [
        (
            signal,
            score_signal(
                signal,
                config,
                as_of=as_of,
                stack=stack,
                source_id=source_ids.get(signal.signal_id),
            ),
        )
        for signal in signals
    ]
    scored.sort(
        key=lambda pair: (
            -pair[1].score,
            pair[1].source_priority
            if pair[1].source_priority is not None
            else UNRANKED_SOURCE_PRIORITY,
            pair[0].signal_id,
        )
    )

    ranked: list[ScoredSignal] = []
    for position, (signal, breakdown) in enumerate(scored, start=1):
        updated = signal.model_copy(
            update={
                "score": breakdown.score,
                "score_rationale": breakdown.rationale,
                "rank": position,
                "tier": assign_tier(breakdown.score, config),
            }
        )
        ranked.append(ScoredSignal(signal=updated, breakdown=breakdown))
    return ranked


def apply_ranking(
    signals: Iterable[Signal],
    config: Mapping[str, Any],
    *,
    as_of: date,
    stack: Iterable[str] | None = None,
    source_ids: Mapping[str, str] | None = None,
) -> list[Signal]:
    """`rank_signals` for callers that only need the signals (stage 7)."""
    return [
        scored.signal
        for scored in rank_signals(
            signals, config, as_of=as_of, stack=stack, source_ids=source_ids
        )
    ]
