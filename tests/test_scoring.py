from __future__ import annotations

import copy
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from radar.assertions import find_unsupported_quantifiers
from radar.models import (
    ChangeType,
    DeltaStatus,
    Fact,
    FactKind,
    Signal,
    SignalType,
    Tier,
)
from radar.scoring import (
    EMPTY_RATIONALE,
    FACTOR_AUTHORITY,
    FACTOR_CHANGE_TYPE,
    FACTOR_NOVELTY,
    FACTOR_STACK,
    FACTOR_URGENCY,
    apply_ranking,
    assign_tier,
    rank_signals,
    resolve_source_priority,
    score_signal,
)

RUN_DATE = date(2026, 8, 17)
CREATED_AT = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)

CONFIG: dict = {
    "corpus": {
        "vendors": [
            {"id": "anthropic", "label": "Anthropic"},
            {"id": "openai", "label": "OpenAI"},
            {"id": "cursor", "label": "Cursor"},
            {"id": "n8n", "label": "n8n"},
            {"id": "langchain", "label": "LangChain"},
        ],
        "change_types": [
            {"id": "release", "label": "Релиз"},
            {"id": "breaking_change", "label": "Ломающее изменение"},
            {"id": "deprecation", "label": "Объявление об отключении"},
            {"id": "pricing", "label": "Изменение цены"},
            {"id": "limits", "label": "Изменение лимитов"},
            {"id": "security", "label": "Безопасность"},
            {"id": "other", "label": "Прочее"},
        ]
    },
    "sources": [
        {
            "id": "anthropic_model_deprecations",
            "url": "https://docs.claude.com/en/docs/about-claude/model-deprecations",
            "priority": 1,
        },
        {
            "id": "anthropic_api_release_notes",
            "url": "https://docs.claude.com/en/release-notes/api",
            "priority": 1,
        },
        {
            "id": "gh_openai_python",
            "url": "https://github.com/openai/openai-python",
            "priority": 3,
        },
    ],
    "scoring": {
        "weights": {
            "change_type": {
                "breaking_change": 30,
                "deprecation": 30,
                "security": 25,
                "limits": 18,
                "pricing": 18,
                "release": 10,
                "other": 4,
            },
            "stack_overlap": 20,
            "urgency": 20,
            "source_authority": 15,
            "novelty": {"new": 15, "updated": 12, "continuing": 4, "resolved": 6},
        },
        "publish_threshold": 55,
        "digest_threshold": 35,
        "active_stack": ["anthropic", "openai", "cursor", "n8n"],
    },
}

DEPRECATIONS_URL = "https://docs.claude.com/en/docs/about-claude/model-deprecations"
GITHUB_URL = "https://github.com/openai/openai-python/releases/tag/v2.0.0"


def make_signal(
    signal_id: str = "sig-1",
    *,
    change_type: ChangeType | None = None,
    vendor: str | None = None,
    product: str | None = None,
    delta_status: DeltaStatus | None = None,
    days_tracked: int = 0,
    primary_url: str | None = None,
    sunset_date: str | None = None,
    effective_date: str | None = None,
) -> Signal:
    facts: list[Fact] = []
    if sunset_date is not None:
        facts.append(
            Fact(
                kind=FactKind.SUNSET_DATE,
                value=sunset_date,
                source_url=primary_url or "https://example.test/x",
                evidence="will be retired",
            )
        )
    if effective_date is not None:
        facts.append(
            Fact(
                kind=FactKind.EFFECTIVE_DATE,
                value=effective_date,
                source_url=primary_url or "https://example.test/x",
                evidence="takes effect",
            )
        )
    return Signal(
        signal_id=signal_id,
        run_id="run-1",
        signal_type=SignalType.DIGEST_ITEM,
        created_at=CREATED_AT,
        for_date=RUN_DATE,
        headline="H",
        change_type=change_type,
        vendor=vendor,
        product=product,
        delta_status=delta_status,
        days_tracked=days_tracked,
        primary_url=primary_url,
        facts=facts,
    )


def maximal_signal(signal_id: str = "sig-max") -> Signal:
    return make_signal(
        signal_id,
        change_type=ChangeType.BREAKING_CHANGE,
        vendor="anthropic",
        product="Claude API",
        delta_status=DeltaStatus.NEW,
        primary_url=DEPRECATIONS_URL,
        sunset_date=RUN_DATE.isoformat(),
    )


def with_weights(**overrides) -> dict:
    config = copy.deepcopy(CONFIG)
    config["scoring"]["weights"].update(overrides)
    return config


class TestBounds:
    def test_empty_signal_scores_zero(self):
        breakdown = score_signal(make_signal(), CONFIG, as_of=RUN_DATE)
        assert breakdown.score == 0
        assert all(not f.applied for f in breakdown.factors)

    def test_every_factor_at_maximum_scores_one_hundred(self):
        breakdown = score_signal(maximal_signal(), CONFIG, as_of=RUN_DATE)
        assert breakdown.score == 100

    def test_overdue_date_does_not_push_past_one_hundred(self):
        signal = make_signal(
            change_type=ChangeType.DEPRECATION,
            vendor="anthropic",
            delta_status=DeltaStatus.NEW,
            primary_url=DEPRECATIONS_URL,
            sunset_date="2020-01-01",
        )
        breakdown = score_signal(signal, CONFIG, as_of=RUN_DATE)
        assert 0 <= breakdown.score <= 100
        assert breakdown.score == 100

    @pytest.mark.parametrize(
        "change_type",
        list(ChangeType),
    )
    def test_score_stays_in_range_for_every_change_type(self, change_type):
        signal = make_signal(
            change_type=change_type,
            vendor="anthropic",
            delta_status=DeltaStatus.UPDATED,
            primary_url=GITHUB_URL,
            sunset_date="2026-12-01",
        )
        breakdown = score_signal(signal, CONFIG, as_of=RUN_DATE)
        assert 0 <= breakdown.score <= 100

    def test_zero_weights_do_not_divide_by_zero(self):
        config = copy.deepcopy(CONFIG)
        config["scoring"]["weights"] = {
            "change_type": {},
            "stack_overlap": 0,
            "urgency": 0,
            "source_authority": 0,
            "novelty": {},
        }
        breakdown = score_signal(maximal_signal(), config, as_of=RUN_DATE)
        assert breakdown.score == 0


class TestFactors:
    def test_imminent_deprecation_outranks_routine_release(self):
        deprecation = score_signal(
            make_signal(
                "dep",
                change_type=ChangeType.DEPRECATION,
                vendor="anthropic",
                product="Claude API",
                delta_status=DeltaStatus.NEW,
                primary_url=DEPRECATIONS_URL,
                sunset_date="2026-08-29",
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        release = score_signal(
            make_signal(
                "rel",
                change_type=ChangeType.RELEASE,
                vendor="anthropic",
                delta_status=DeltaStatus.NEW,
                primary_url="https://docs.claude.com/en/release-notes/api",
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert deprecation.score > release.score

    def test_stack_overlap_is_worth_its_configured_weight(self):
        inside = score_signal(
            make_signal(change_type=ChangeType.RELEASE, vendor="anthropic"),
            CONFIG,
            as_of=RUN_DATE,
        )
        outside = score_signal(
            make_signal(change_type=ChangeType.RELEASE, vendor="langchain"),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert inside.score - outside.score == 20
        assert not outside.by_key(FACTOR_STACK).applied

    def test_stack_can_be_overridden_per_call(self):
        signal = make_signal(change_type=ChangeType.RELEASE, vendor="langchain")
        breakdown = score_signal(signal, CONFIG, as_of=RUN_DATE, stack=["langchain"])
        assert breakdown.by_key(FACTOR_STACK).applied

    def test_product_also_matches_the_stack(self):
        signal = make_signal(
            change_type=ChangeType.RELEASE, vendor="unknown-vendor", product="Cursor"
        )
        factor = score_signal(signal, CONFIG, as_of=RUN_DATE).by_key(FACTOR_STACK)
        assert factor.applied
        assert factor.detail == "продукт Cursor в активном стеке"

    def test_vendor_label_from_the_config_is_used_verbatim(self):
        """A name that is deliberately lowercase must survive the sentence start."""
        breakdown = score_signal(
            make_signal(change_type=ChangeType.RELEASE, vendor="n8n"),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert breakdown.by_key(FACTOR_STACK).detail == "вендор n8n в активном стеке"
        assert "n8n" in breakdown.rationale
        assert "N8n" not in breakdown.rationale

    def test_novelty_orders_new_above_updated_above_continuing(self):
        scores = [
            score_signal(
                make_signal(
                    change_type=ChangeType.RELEASE, vendor="anthropic", delta_status=status
                ),
                CONFIG,
                as_of=RUN_DATE,
            ).score
            for status in (DeltaStatus.NEW, DeltaStatus.UPDATED, DeltaStatus.CONTINUING)
        ]
        assert scores == sorted(scores, reverse=True)
        assert len(set(scores)) == 3

    def test_source_authority_follows_configured_priority(self):
        first = score_signal(
            make_signal(change_type=ChangeType.RELEASE, primary_url=DEPRECATIONS_URL),
            CONFIG,
            as_of=RUN_DATE,
        )
        third = score_signal(
            make_signal(change_type=ChangeType.RELEASE, primary_url=GITHUB_URL),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert first.by_key(FACTOR_AUTHORITY).points > third.by_key(FACTOR_AUTHORITY).points

    def test_unknown_source_leaves_the_authority_factor_out(self):
        breakdown = score_signal(
            make_signal(change_type=ChangeType.RELEASE, primary_url="https://blog.test/post"),
            CONFIG,
            as_of=RUN_DATE,
        )
        factor = breakdown.by_key(FACTOR_AUTHORITY)
        assert not factor.applied
        assert factor.points == 0
        assert breakdown.source_priority is None

    def test_source_id_resolves_authority_without_a_url(self):
        assert (
            resolve_source_priority(CONFIG, source_id="anthropic_model_deprecations") == 1
        )
        assert resolve_source_priority(CONFIG, source_id="nope") is None
        assert resolve_source_priority(CONFIG, url=None) is None


class TestMissingDate:
    def test_absent_date_neither_penalizes_nor_rewards(self):
        without = score_signal(
            make_signal(
                change_type=ChangeType.DEPRECATION,
                vendor="anthropic",
                delta_status=DeltaStatus.NEW,
                primary_url=DEPRECATIONS_URL,
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        far_away = score_signal(
            make_signal(
                change_type=ChangeType.DEPRECATION,
                vendor="anthropic",
                delta_status=DeltaStatus.NEW,
                primary_url=DEPRECATIONS_URL,
                sunset_date="2030-01-01",
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert without.score == far_away.score
        assert without.by_key(FACTOR_URGENCY).points == 0
        assert not without.by_key(FACTOR_URGENCY).applied
        assert without.due_date is None

    def test_unparsable_date_is_treated_as_absent(self):
        breakdown = score_signal(
            make_signal(
                change_type=ChangeType.DEPRECATION,
                primary_url=DEPRECATIONS_URL,
                sunset_date="дата в источнике не указана",
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert breakdown.due_date is None
        assert not breakdown.by_key(FACTOR_URGENCY).applied

    def test_other_factors_keep_their_full_contribution(self):
        breakdown = score_signal(
            make_signal(
                change_type=ChangeType.BREAKING_CHANGE,
                vendor="anthropic",
                delta_status=DeltaStatus.NEW,
                primary_url=DEPRECATIONS_URL,
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        # 30 + 20 + 15 + 15 out of a ceiling of 100 that still counts urgency.
        assert breakdown.score == 80


class TestReferenceDate:
    def test_urgency_grows_as_the_run_date_approaches_the_deadline(self):
        signal = make_signal(
            change_type=ChangeType.DEPRECATION,
            vendor="anthropic",
            delta_status=DeltaStatus.NEW,
            primary_url=DEPRECATIONS_URL,
            sunset_date="2026-10-15",
        )
        scores = [
            score_signal(signal, CONFIG, as_of=as_of).score
            for as_of in (date(2026, 5, 1), date(2026, 8, 17), date(2026, 10, 10))
        ]
        assert scores[0] < scores[1] < scores[2]

    def test_beyond_the_horizon_contributes_nothing(self):
        signal = make_signal(
            change_type=ChangeType.DEPRECATION,
            primary_url=DEPRECATIONS_URL,
            sunset_date="2026-10-15",
        )
        far = score_signal(signal, CONFIG, as_of=date(2026, 1, 1))
        assert far.by_key(FACTOR_URGENCY).points == 0
        assert far.by_key(FACTOR_URGENCY).applied
        assert far.days_until_due == (date(2026, 10, 15) - date(2026, 1, 1)).days

    def test_horizon_is_configurable(self):
        signal = make_signal(
            change_type=ChangeType.DEPRECATION,
            primary_url=DEPRECATIONS_URL,
            sunset_date="2026-11-15",
        )
        narrow = score_signal(signal, CONFIG, as_of=RUN_DATE)
        config = copy.deepcopy(CONFIG)
        config["scoring"]["urgency_horizon_days"] = 365
        wide = score_signal(signal, config, as_of=RUN_DATE)
        assert wide.by_key(FACTOR_URGENCY).points > narrow.by_key(FACTOR_URGENCY).points

    def test_reference_date_is_not_the_system_clock(self):
        signal = make_signal(
            change_type=ChangeType.DEPRECATION,
            primary_url=DEPRECATIONS_URL,
            sunset_date="2026-08-29",
        )
        replayed = score_signal(signal, CONFIG, as_of=date(2026, 8, 17))
        again = score_signal(signal, CONFIG, as_of=date(2026, 8, 17))
        assert replayed.score == again.score
        assert replayed.days_until_due == 12
        assert score_signal(signal, CONFIG, as_of=date(2026, 8, 18)).score > replayed.score

    def test_earliest_of_several_dates_wins(self):
        signal = make_signal(
            change_type=ChangeType.DEPRECATION,
            primary_url=DEPRECATIONS_URL,
            sunset_date="2026-12-01",
            effective_date="2026-09-01",
        )
        assert score_signal(signal, CONFIG, as_of=RUN_DATE).due_date == date(2026, 9, 1)


class TestConfigDrivesOrder:
    def test_reweighting_change_types_reorders_the_run(self):
        """FR-6.3 read literally: the config edit alone flips the ranking."""
        pricing = make_signal(
            "a-pricing", change_type=ChangeType.PRICING, vendor="anthropic"
        )
        release = make_signal(
            "b-release", change_type=ChangeType.RELEASE, vendor="anthropic"
        )

        base = apply_ranking([pricing, release], CONFIG, as_of=RUN_DATE)
        assert [s.signal_id for s in base] == ["a-pricing", "b-release"]

        flipped_config = with_weights(
            change_type={
                "breaking_change": 30,
                "deprecation": 30,
                "security": 25,
                "limits": 18,
                "pricing": 5,
                "release": 40,
                "other": 4,
            }
        )
        flipped = apply_ranking([pricing, release], flipped_config, as_of=RUN_DATE)
        assert [s.signal_id for s in flipped] == ["b-release", "a-pricing"]

    def test_zeroing_the_stack_weight_removes_its_effect(self):
        inside = make_signal("a", change_type=ChangeType.RELEASE, vendor="anthropic")
        outside = make_signal("b", change_type=ChangeType.RELEASE, vendor="langchain")
        config = with_weights(stack_overlap=0)
        scores = {
            s.signal_id: s.score for s in apply_ranking([inside, outside], config, as_of=RUN_DATE)
        }
        assert scores["a"] == scores["b"]

    def test_missing_weight_key_falls_back_to_the_module_default(self):
        config = copy.deepcopy(CONFIG)
        del config["scoring"]["weights"]["stack_overlap"]
        inside = score_signal(
            make_signal(change_type=ChangeType.RELEASE, vendor="anthropic"),
            config,
            as_of=RUN_DATE,
        )
        assert inside.by_key(FACTOR_STACK).points == 20

    def test_explicit_priority_factor_table_is_honoured(self):
        config = copy.deepcopy(CONFIG)
        config["scoring"]["source_priority_factor"] = {1: 1.0, 3: 1.0}
        first = score_signal(
            make_signal(change_type=ChangeType.RELEASE, primary_url=DEPRECATIONS_URL),
            config,
            as_of=RUN_DATE,
        )
        third = score_signal(
            make_signal(change_type=ChangeType.RELEASE, primary_url=GITHUB_URL),
            config,
            as_of=RUN_DATE,
        )
        assert first.score == third.score


class TestRanking:
    def test_ranks_run_from_one_without_gaps(self):
        signals = [
            make_signal("s1", change_type=ChangeType.RELEASE, vendor="anthropic"),
            make_signal("s2", change_type=ChangeType.DEPRECATION, vendor="anthropic"),
            make_signal("s3", change_type=ChangeType.PRICING),
            make_signal("s4"),
        ]
        ranked = apply_ranking(signals, CONFIG, as_of=RUN_DATE)
        assert [s.rank for s in ranked] == [1, 2, 3, 4]
        assert [s.score for s in ranked] == sorted((s.score for s in ranked), reverse=True)

    def test_equal_scores_break_on_source_priority_then_id(self):
        config = copy.deepcopy(CONFIG)
        # Equal authority points, different priorities: the tie-break is visible.
        config["scoring"]["source_priority_factor"] = {1: 1.0, 3: 1.0}
        low_priority = make_signal(
            "aaa", change_type=ChangeType.RELEASE, primary_url=GITHUB_URL
        )
        high_priority = make_signal(
            "zzz", change_type=ChangeType.RELEASE, primary_url=DEPRECATIONS_URL
        )
        ranked = apply_ranking([low_priority, high_priority], config, as_of=RUN_DATE)
        assert [s.signal_id for s in ranked] == ["zzz", "aaa"]
        assert ranked[0].score == ranked[1].score

    def test_order_is_independent_of_input_order(self):
        signals = [
            make_signal(f"sig-{i}", change_type=ChangeType.RELEASE, vendor="anthropic")
            for i in range(6)
        ]
        forward = [s.signal_id for s in apply_ranking(signals, CONFIG, as_of=RUN_DATE)]
        backward = [
            s.signal_id for s in apply_ranking(list(reversed(signals)), CONFIG, as_of=RUN_DATE)
        ]
        assert forward == backward == [f"sig-{i}" for i in range(6)]

    def test_repeated_runs_produce_identical_output(self):
        signals = [
            maximal_signal("m1"),
            make_signal("m2", change_type=ChangeType.LIMITS, vendor="openai"),
            make_signal("m3", change_type=ChangeType.OTHER),
        ]
        first = [(s.signal_id, s.rank, s.score, s.tier) for s in apply_ranking(signals, CONFIG, as_of=RUN_DATE)]
        second = [(s.signal_id, s.rank, s.score, s.tier) for s in apply_ranking(signals, CONFIG, as_of=RUN_DATE)]
        assert first == second

    def test_ranking_does_not_mutate_the_input(self):
        signal = maximal_signal()
        apply_ranking([signal], CONFIG, as_of=RUN_DATE)
        assert signal.score == 0
        assert signal.rank == 0
        assert signal.score_rationale == ""

    def test_source_ids_override_url_matching(self):
        signal = make_signal(
            "s1", change_type=ChangeType.RELEASE, primary_url="https://mirror.test/x"
        )
        ranked = rank_signals(
            [signal],
            CONFIG,
            as_of=RUN_DATE,
            source_ids={"s1": "anthropic_model_deprecations"},
        )
        assert ranked[0].breakdown.source_priority == 1


class TestTiers:
    def test_thresholds_are_minimums(self):
        assert assign_tier(56, CONFIG) is Tier.LEAD
        assert assign_tier(55, CONFIG) is Tier.LEAD
        assert assign_tier(54, CONFIG) is Tier.STANDARD
        assert assign_tier(35, CONFIG) is Tier.STANDARD
        assert assign_tier(34, CONFIG) is Tier.BACKGROUND
        assert assign_tier(0, CONFIG) is Tier.BACKGROUND

    def test_thresholds_come_from_the_config(self):
        config = copy.deepcopy(CONFIG)
        config["scoring"]["publish_threshold"] = 90
        config["scoring"]["digest_threshold"] = 80
        assert assign_tier(85, config) is Tier.STANDARD
        assert assign_tier(85, CONFIG) is Tier.LEAD

    def test_tier_is_assigned_during_ranking(self):
        signals = [
            maximal_signal("lead"),
            make_signal("standard", change_type=ChangeType.PRICING, vendor="anthropic", delta_status=DeltaStatus.UPDATED),
            make_signal("background", change_type=ChangeType.OTHER),
        ]
        tiers = {s.signal_id: s.tier for s in apply_ranking(signals, CONFIG, as_of=RUN_DATE)}
        assert tiers["lead"] is Tier.LEAD
        assert tiers["standard"] is Tier.STANDARD
        assert tiers["background"] is Tier.BACKGROUND


class TestRationale:
    def test_names_the_heaviest_factors(self):
        breakdown = score_signal(
            make_signal(
                change_type=ChangeType.DEPRECATION,
                vendor="anthropic",
                product="Claude API",
                delta_status=DeltaStatus.NEW,
                primary_url=DEPRECATIONS_URL,
                sunset_date="2026-08-29",
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert breakdown.rationale
        assert breakdown.rationale.startswith("Объявление об отключении в Claude API")
        assert "дата через 12 дней" in breakdown.rationale
        assert "вендор Anthropic в активном стеке" in breakdown.rationale
        assert breakdown.rationale.count(",") <= 2

    def test_every_named_factor_actually_contributed(self):
        breakdown = score_signal(maximal_signal(), CONFIG, as_of=RUN_DATE)
        named = [f for f in breakdown.factors if f.detail and f.detail in breakdown.rationale]
        assert named
        assert all(f.points > 0 and f.applied for f in named)

    def test_top_factor_is_always_mentioned(self):
        breakdown = score_signal(
            make_signal(
                change_type=ChangeType.SECURITY,
                vendor="openai",
                delta_status=DeltaStatus.UPDATED,
                primary_url=GITHUB_URL,
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        # The leading factor opens the sentence, so it arrives capitalized.
        assert breakdown.top_factors(1)[0].detail in breakdown.rationale.lower()

    def test_absent_factors_are_never_mentioned(self):
        breakdown = score_signal(
            make_signal(change_type=ChangeType.RELEASE, vendor="langchain"),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert "активном стеке" not in breakdown.rationale
        assert "дата" not in breakdown.rationale
        assert "источник" not in breakdown.rationale

    def test_signal_with_no_factors_gets_an_explicit_sentence(self):
        breakdown = score_signal(make_signal(), CONFIG, as_of=RUN_DATE)
        assert breakdown.rationale == EMPTY_RATIONALE

    def test_days_tracked_is_spelled_out_for_continuing(self):
        breakdown = score_signal(
            make_signal(
                change_type=ChangeType.RELEASE,
                vendor="anthropic",
                delta_status=DeltaStatus.CONTINUING,
                days_tracked=3,
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert "3-й день" in breakdown.by_key(FACTOR_NOVELTY).detail

    def test_overdue_deadline_is_phrased_in_the_past(self):
        breakdown = score_signal(
            make_signal(
                change_type=ChangeType.DEPRECATION,
                primary_url=DEPRECATIONS_URL,
                sunset_date="2026-08-15",
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        assert "срок истёк 2 дня назад" in breakdown.rationale

    @pytest.mark.parametrize("days_tracked", [1, 2, 3, 5, 11, 21, 101])
    @pytest.mark.parametrize("change_type", list(ChangeType))
    @pytest.mark.parametrize("delta_status", list(DeltaStatus))
    def test_generated_rationales_pass_the_quantifier_check(
        self, days_tracked, change_type, delta_status
    ):
        """FR-6.18 applies to prose the code writes, not only to prose a model writes."""
        for offset in (-14, -1, 0, 1, 12, 89, 400, None):
            sunset = (
                None
                if offset is None
                else date.fromordinal(RUN_DATE.toordinal() + offset).isoformat()
            )
            breakdown = score_signal(
                make_signal(
                    change_type=change_type,
                    vendor="anthropic",
                    product="Claude Code",
                    delta_status=delta_status,
                    days_tracked=days_tracked,
                    primary_url=DEPRECATIONS_URL,
                    sunset_date=sunset,
                ),
                CONFIG,
                as_of=RUN_DATE,
            )
            assert breakdown.rationale
            assert breakdown.rationale != EMPTY_RATIONALE
            assert find_unsupported_quantifiers(breakdown.rationale) == []

    def test_rationale_lands_on_the_signal(self):
        ranked = apply_ranking([maximal_signal()], CONFIG, as_of=RUN_DATE)
        assert ranked[0].score_rationale
        assert find_unsupported_quantifiers(ranked[0].score_rationale) == []


class TestBreakdownExport:
    def test_breakdown_serializes_every_factor(self):
        breakdown = score_signal(maximal_signal(), CONFIG, as_of=RUN_DATE)
        payload = breakdown.as_dict()
        keys = {f["key"] for f in payload["factors"]}
        assert keys == {
            FACTOR_CHANGE_TYPE,
            FACTOR_URGENCY,
            FACTOR_STACK,
            FACTOR_AUTHORITY,
            FACTOR_NOVELTY,
        }
        assert payload["score"] == 100
        assert payload["due_date"] == RUN_DATE.isoformat()

    def test_contributions_add_up_to_the_score(self):
        breakdown = score_signal(
            make_signal(
                change_type=ChangeType.LIMITS,
                vendor="openai",
                delta_status=DeltaStatus.UPDATED,
                primary_url=GITHUB_URL,
                sunset_date="2026-09-01",
            ),
            CONFIG,
            as_of=RUN_DATE,
        )
        total = sum(f.points for f in breakdown.factors)
        assert total == pytest.approx(breakdown.raw_points)
        assert breakdown.score == round(100 * total / breakdown.max_points)


class TestShippedConfig:
    def test_real_theme_config_scores_within_range(self):
        path = Path(__file__).resolve().parents[1] / "config" / "ai-tools.yaml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        signals = [
            maximal_signal("m"),
            make_signal("r", change_type=ChangeType.RELEASE, primary_url=GITHUB_URL),
        ]
        ranked = apply_ranking(signals, config, as_of=RUN_DATE, stack=["anthropic"])
        assert [s.rank for s in ranked] == [1, 2]
        assert all(0 <= s.score <= 100 for s in ranked)
        assert all(s.score_rationale for s in ranked)
        assert all(find_unsupported_quantifiers(s.score_rationale) == [] for s in ranked)


class TestClusterVendorSurvives:
    """The vendor must reach the cluster: without it retrieval matches nothing."""

    def test_vendor_lookup_uses_the_item_url_not_the_group_key(self):
        from radar.adapters.base import CollectedItem
        from radar.cluster import cluster_items

        item = CollectedItem(
            url="https://docs.claude.com/deprecations#opus",
            title="Anthropic отключает claude-3-opus",
            raw_text="body",
        )
        [cluster] = cluster_items([item], vendor_of={item.url: "anthropic"})
        assert cluster.vendor == "anthropic"

    def test_vendor_can_also_arrive_on_the_item(self):
        from radar.adapters.base import CollectedItem
        from radar.cluster import cluster_items

        item = CollectedItem(
            url="https://example.test/a", title="Заголовок", raw_text="body",
            extra={"vendor": "openai"},
        )
        [cluster] = cluster_items([item])
        assert cluster.vendor == "openai"


class TestDeadlineLooksForward:
    """A deprecations registry carries its whole history on one page, so a
    single material can hold dates two years old. Taking the earliest of them
    printed "срок истёк 649 дней назад" under a headline about an
    announcement made today."""

    def _signal(self, dates):
        from datetime import date as _date

        from radar.models import Fact, FactKind, Signal, SignalType

        return Signal(
            signal_id="s1", run_id="r1", signal_type=SignalType.DIGEST_ITEM,
            created_at=datetime(2026, 8, 17, tzinfo=UTC), for_date=_date(2026, 8, 17),
            headline="Anthropic отключает модель",
            facts=[
                Fact(kind=FactKind.SUNSET_DATE, value=d.isoformat(), source_url="u",
                     evidence="q", value_date=d)
                for d in dates
            ],
        )

    def test_the_nearest_future_date_wins_over_an_older_one(self):
        from datetime import date as _date

        from radar.scoring import nearest_due_date

        signal = self._signal([_date(2024, 11, 6), _date(2026, 10, 15), _date(2027, 5, 1)])
        due, days = nearest_due_date(signal, ["sunset_date"], _date(2026, 8, 17))
        assert due == _date(2026, 10, 15)
        assert days == 59

    def test_all_dates_past_reports_the_most_recent(self):
        from datetime import date as _date

        from radar.scoring import nearest_due_date

        signal = self._signal([_date(2024, 11, 6), _date(2025, 7, 21)])
        due, days = nearest_due_date(signal, ["sunset_date"], _date(2026, 8, 17))
        assert due == _date(2025, 7, 21)
        assert days < 0

    def test_the_parsed_field_is_preferred_over_the_string(self):
        from datetime import date as _date

        from radar.models import Fact, FactKind, Signal, SignalType
        from radar.scoring import nearest_due_date

        signal = Signal(
            signal_id="s", run_id="r", signal_type=SignalType.DIGEST_ITEM,
            created_at=datetime(2026, 8, 17, tzinfo=UTC), for_date=_date(2026, 8, 17),
            facts=[Fact(kind=FactKind.SUNSET_DATE, value="15 октября 2026",
                        source_url="u", evidence="q", value_date=_date(2026, 10, 15))],
        )
        due, days = nearest_due_date(signal, ["sunset_date"], _date(2026, 8, 17))
        assert due == _date(2026, 10, 15)
