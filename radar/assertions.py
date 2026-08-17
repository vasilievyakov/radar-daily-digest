"""Publication guards.

The three prohibitions in the agent notes are enforced here rather than asked
for in a prompt. A model told to supply a quote will supply a plausible one;
a substring check against the archived source is what makes FR-4.3 a property
of the system instead of an intention.

Verifying evidence also closes much of the prompt-injection surface (NFR-13):
a value that does not occur in the fetched text cannot be published.
"""

from __future__ import annotations

import re
import unicodedata

from radar.models import ContextLabel, Fact, Precedent

MAX_EVIDENCE_WORDS = 15
MIN_PRECEDENTS_FOR_PATTERN = 2

# Quantifiers that assert frequency or direction. Permitted only in a sentence
# that also carries a number: "третий раз с мая" passes, "всё чаще" does not.
_QUANTIFIER_PATTERNS = [
    r"вс[её]\s+чаще",
    r"вс[её]\s+больше",
    r"вс[её]\s+реже",
    r"наблюдается\s+тенденци\w*",
    r"наметил\w*\s+тенденци\w*",
    r"прослеживается\s+тенденци\w*",
    r"есть\s+тенденци\w*",
    r"как\s+правило",
    r"зачастую",
    r"нередко",
    r"сплошь\s+и\s+рядом",
    r"из\s+раза\s+в\s+раз",
    r"в\s+последнее\s+время",
    r"в\s+последние\s+месяцы",
    r"систематическ\w+",
    r"регулярн\w+",
    r"постоянн\w+",
    r"неоднократн\w+",
    r"раз\s+за\s+разом",
    r"тренд\s+на\b",
    r"наметилась\s+динамика",
]
_QUANTIFIER_RE = re.compile("|".join(_QUANTIFIER_PATTERNS), re.IGNORECASE)
_DIGIT_RE = re.compile(r"\d")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {
    ord("‘"): "'",
    ord("’"): "'",
    ord("‚"): "'",
    ord("“"): '"',
    ord("”"): '"',
    ord("„"): '"',
    ord("«"): '"',
    ord("»"): '"',
    ord("′"): "'",
    ord("″"): '"',
}
_DROP = dict.fromkeys(map(ord, "­​‌‍﻿"), None)


def normalize_for_match(text: str) -> str:
    """Fold away the differences that survive copying text out of a page."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_DROP).translate(_DASHES).translate(_QUOTES)
    text = text.replace(" ", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def word_count(text: str) -> int:
    return len(text.split())


def verify_evidence(evidence: str, source_text: str) -> tuple[bool, str]:
    """Check that the quote occurs verbatim in the archived source.

    Returns (ok, reason). Reason is empty when the check passes.
    """
    if not evidence or not evidence.strip():
        return False, "evidence_empty"
    if word_count(evidence) > MAX_EVIDENCE_WORDS:
        return False, f"evidence_too_long:{word_count(evidence)}"
    if not source_text or not source_text.strip():
        return False, "source_text_missing"
    if normalize_for_match(evidence) not in normalize_for_match(source_text):
        return False, "evidence_not_in_source"
    return True, ""


def filter_verified_facts(
    facts: list[Fact], source_text: str
) -> tuple[list[Fact], list[tuple[Fact, str]]]:
    """Split facts into publishable and rejected, with a reason for each drop."""
    kept: list[Fact] = []
    rejected: list[tuple[Fact, str]] = []
    for fact in facts:
        ok, reason = verify_evidence(fact.evidence, source_text)
        if ok:
            kept.append(fact.model_copy(update={"evidence_verified": True}))
        else:
            rejected.append((fact, reason))
    return kept, rejected


def resolve_context_label(
    proposed: ContextLabel | None, precedents: list[Precedent]
) -> ContextLabel:
    """Force the label down to what the precedent list can carry (FR-5.9, FR-6.17).

    A claim of recurrence needs at least two records. The model proposes; the
    count decides.
    """
    if len(precedents) < MIN_PRECEDENTS_FOR_PATTERN:
        return ContextLabel.NOT_FOUND_IN_CORPUS
    if proposed is None or proposed is ContextLabel.NOT_FOUND_IN_CORPUS:
        return ContextLabel.RECURRING
    return proposed


def validate_precedents(
    precedents: list[Precedent], vendor: str | None, change_type: str | None
) -> tuple[list[Precedent], list[tuple[str, str]]]:
    """Drop precedents that do not match the claim they are supposed to support."""
    kept: list[Precedent] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for precedent in precedents:
        if precedent.statement_id in seen:
            rejected.append((precedent.statement_id, "duplicate"))
            continue
        if vendor and precedent.vendor != vendor:
            rejected.append((precedent.statement_id, "vendor_mismatch"))
            continue
        if change_type and precedent.change_type != change_type:
            rejected.append((precedent.statement_id, "change_type_mismatch"))
            continue
        seen.add(precedent.statement_id)
        kept.append(precedent)
    return kept, rejected


def find_unsupported_quantifiers(text: str) -> list[str]:
    """Return quantifier phrases used in a sentence that carries no number.

    FR-6.18: "вендор всё чаще" is banned, "третий раз с мая" is allowed.
    """
    if not text:
        return []
    offenders: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not sentence.strip() or _DIGIT_RE.search(sentence):
            continue
        offenders.extend(match.group(0) for match in _QUANTIFIER_RE.finditer(sentence))
    return offenders
