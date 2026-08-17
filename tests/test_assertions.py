from datetime import date

import pytest

from radar.assertions import (
    filter_verified_facts,
    find_unsupported_quantifiers,
    resolve_context_label,
    validate_precedents,
    verify_evidence,
)
from radar.models import ChangeType, ContextLabel, Fact, FactKind, Precedent

SOURCE = (
    "Deprecation notice\n"
    "On January 5, 2026 we announced the deprecation of Claude 2.1.\n"
    "The model will be  retired  on\nOctober 15, 2026 for all API users.\n"
    "Pricing for Claude Opus 4 stays at $15 per million input tokens."
)


def make_fact(evidence: str, kind: FactKind = FactKind.SUNSET_DATE) -> Fact:
    return Fact(
        kind=kind,
        value="2026-10-15",
        source_url="https://example.test/deprecations",
        evidence=evidence,
    )


class TestVerifyEvidence:
    def test_exact_quote_passes(self):
        ok, reason = verify_evidence("retired on October 15, 2026", SOURCE)
        assert ok, reason

    def test_whitespace_and_newlines_are_folded(self):
        ok, reason = verify_evidence("will be retired on October 15, 2026", SOURCE)
        assert ok, reason

    def test_typographic_quotes_and_dashes_are_folded(self):
        ok, _ = verify_evidence(
            "Claude 2.1", "announced the deprecation of Claude 2.1."
        )
        assert ok

    def test_plausible_but_absent_quote_is_rejected(self):
        ok, reason = verify_evidence("retired on November 15, 2026", SOURCE)
        assert not ok
        assert reason == "evidence_not_in_source"

    def test_empty_evidence_is_rejected(self):
        ok, reason = verify_evidence("   ", SOURCE)
        assert not ok
        assert reason == "evidence_empty"

    def test_missing_source_is_rejected(self):
        ok, reason = verify_evidence("retired on October 15, 2026", "")
        assert not ok
        assert reason == "source_text_missing"

    def test_quote_over_fifteen_words_is_rejected(self):
        long_quote = " ".join(["word"] * 16)
        ok, reason = verify_evidence(long_quote, long_quote)
        assert not ok
        assert reason.startswith("evidence_too_long")

    def test_exactly_fifteen_words_is_allowed(self):
        quote = " ".join(f"w{i}" for i in range(15))
        ok, reason = verify_evidence(quote, f"prefix {quote} suffix")
        assert ok, reason


class TestFilterVerifiedFacts:
    def test_splits_and_marks_verified(self):
        kept, rejected = filter_verified_facts(
            [
                make_fact("retired on October 15, 2026"),
                make_fact("retired on July 1, 2026"),
            ],
            SOURCE,
        )
        assert len(kept) == 1
        assert kept[0].evidence_verified is True
        assert len(rejected) == 1
        assert rejected[0][1] == "evidence_not_in_source"

    def test_original_facts_are_not_mutated(self):
        fact = make_fact("retired on October 15, 2026")
        filter_verified_facts([fact], SOURCE)
        assert fact.evidence_verified is False


def make_precedent(
    statement_id: str,
    vendor: str = "anthropic",
    change_type: ChangeType = ChangeType.DEPRECATION,
) -> Precedent:
    return Precedent(
        statement_id=statement_id,
        text="Anthropic announced a model retirement.",
        source_url=f"https://example.test/{statement_id}",
        event_date=date(2026, 5, 1),
        vendor=vendor,
        change_type=change_type,
    )


class TestContextLabel:
    def test_single_precedent_forces_not_found(self):
        label = resolve_context_label(ContextLabel.RECURRING, [make_precedent("s1")])
        assert label is ContextLabel.NOT_FOUND_IN_CORPUS

    def test_no_precedents_forces_not_found(self):
        assert (
            resolve_context_label(ContextLabel.TREND_MEMBER, [])
            is ContextLabel.NOT_FOUND_IN_CORPUS
        )

    def test_two_precedents_allow_the_proposed_label(self):
        label = resolve_context_label(
            ContextLabel.ESCALATION, [make_precedent("s1"), make_precedent("s2")]
        )
        assert label is ContextLabel.ESCALATION

    def test_enough_precedents_without_a_proposal_defaults_to_recurring(self):
        label = resolve_context_label(
            None, [make_precedent("s1"), make_precedent("s2")]
        )
        assert label is ContextLabel.RECURRING


class TestValidatePrecedents:
    def test_mismatched_vendor_is_dropped(self):
        kept, rejected = validate_precedents(
            [make_precedent("s1"), make_precedent("s2", vendor="openai")],
            vendor="anthropic",
            change_type=ChangeType.DEPRECATION,
        )
        assert [p.statement_id for p in kept] == ["s1"]
        assert rejected == [("s2", "vendor_mismatch")]

    def test_mismatched_change_type_is_dropped(self):
        kept, rejected = validate_precedents(
            [make_precedent("s1", change_type=ChangeType.RELEASE)],
            vendor="anthropic",
            change_type=ChangeType.DEPRECATION,
        )
        assert kept == []
        assert rejected == [("s1", "change_type_mismatch")]

    def test_duplicates_are_dropped(self):
        kept, rejected = validate_precedents(
            [make_precedent("s1"), make_precedent("s1")],
            vendor="anthropic",
            change_type=ChangeType.DEPRECATION,
        )
        assert len(kept) == 1
        assert rejected == [("s1", "duplicate")]

    def test_padding_precedents_cannot_reach_the_threshold(self):
        """A model that pads the list to two must not buy itself a label."""
        kept, _ = validate_precedents(
            [make_precedent("s1"), make_precedent("s2", vendor="openai")],
            vendor="anthropic",
            change_type=ChangeType.DEPRECATION,
        )
        assert (
            resolve_context_label(ContextLabel.RECURRING, kept)
            is ContextLabel.NOT_FOUND_IN_CORPUS
        )


class TestQuantifiers:
    @pytest.mark.parametrize(
        "text",
        [
            "Вендор всё чаще меняет лимиты без предупреждения.",
            "Наблюдается тенденция к сокращению сроков поддержки.",
            "Как правило, отключение объявляют заранее.",
            "Компания регулярно переносит сроки.",
            "В последнее время правки приходят пачками.",
        ],
    )
    def test_quantifier_without_a_number_is_caught(self, text):
        assert find_unsupported_quantifiers(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Третий раз с мая, вот три записи корпуса.",
            "Вендор регулярно, 4 раза за полгода, менял лимиты.",
            "Anthropic объявил отключение 15 октября 2026 года.",
        ],
    )
    def test_number_in_the_same_sentence_makes_it_allowed(self, text):
        assert find_unsupported_quantifiers(text) == []

    def test_only_the_offending_sentence_matters(self):
        text = "Anthropic отключает модель 15 октября. Вендор всё чаще так делает."
        assert find_unsupported_quantifiers(text) == ["всё чаще"]

    def test_empty_text_is_clean(self):
        assert find_unsupported_quantifiers("") == []
