"""CFG-1 on a second theme: everything the pipeline can do without a model.

The claim under test is narrow and checkable: `config/cloud-infra.yaml`
describes a domain with no overlap with AI tooling, and every stage that runs
without a language model runs on it with no edit to `radar/`. Nothing here
imports a patched module, a shim or a domain switch — the only difference
between this file and the AI-theme tests is which YAML is loaded.

Three tests deliberately assert a limitation rather than a capability
(`TestVocabularyIsCode`). They are green because they state what the code does
today, not what the config appears to promise: the change-type vocabulary and
the fact-kind vocabulary live in `radar/models.py` as enums, so a config can
rename them but cannot extend them. That boundary is the honest edge of CFG-1
and it is written down in `docs/portability.md`.

Network tests carry the `integration` marker and are skipped unless
RADAR_NETWORK_INTEGRATION=1. They read through the on-disk HTTP cache, so a
second run costs no requests.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from radar.adapters.base import CollectedItem
from radar.backfill import refresh_trends
from radar.cluster import cluster_items, make_cluster_id, title_signature
from radar.collect import ADAPTERS, build_adapter, collect_source
from radar.config import ThemeConfig
from radar.db import corpus_readiness, init_db
from radar.enrich import cache_prefix_for
from radar.fetch import Fetcher
from radar.filter import build_cache_prefix
from radar.models import (
    ChangeType,
    ContextLabel,
    DeltaStatus,
    Fact,
    FactKind,
    Signal,
    SignalType,
    Tier,
    Trajectory,
)
from radar.normalize import Normalizer
from radar.retrieval import CorpusRetriever
from radar.scoring import rank_signals, resolve_source_priority, score_signal
from radar.trends import find_candidates

ROOT = Path(__file__).resolve().parents[1]
THEME_PATH = ROOT / "config" / "cloud-infra.yaml"
AI_THEME_PATH = ROOT / "config" / "ai-tools.yaml"

NETWORK_ENV = "RADAR_NETWORK_INTEGRATION"
network = pytest.mark.skipif(
    os.environ.get(NETWORK_ENV) != "1",
    reason=f"set {NETWORK_ENV}=1 to reach the configured sources over the network",
)

# Fixed reference date. Urgency, windows and trajectories are all measured
# against it, never against the system clock, so this file reproduces.
TODAY = date(2026, 8, 17)
NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

LAMBDA_URL = "https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html"
PG_URL = "https://www.postgresql.org/support/versioning/"


@pytest.fixture(scope="module")
def config() -> ThemeConfig:
    return ThemeConfig.load(THEME_PATH)


@pytest.fixture(scope="module")
def ai_config() -> ThemeConfig:
    """The first theme, read only. Used to show the two configs disagree."""
    return ThemeConfig.load(AI_THEME_PATH)


@pytest.fixture(scope="module")
def normalizer(config: ThemeConfig) -> Normalizer:
    return Normalizer.from_config(config.vendors, config.change_types)


@pytest.fixture(scope="module")
def fetcher(config: ThemeConfig) -> Fetcher:
    """Shares the project cache, and reads every timeout from the theme."""
    collection = config.collection
    return Fetcher(
        cache_root=ROOT / "cache",
        timeout=float(collection.get("timeout_seconds", 30)),
        max_retries=int(collection.get("max_retries", 2)),
        polite_delay=float(collection.get("polite_delay_seconds", 1.0)),
    )


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    yield conn
    conn.close()


def make_signal(
    signal_id: str,
    *,
    change_type: ChangeType | None = ChangeType.DEPRECATION,
    vendor: str | None = "aws",
    product: str | None = None,
    url: str | None = LAMBDA_URL,
    sunset_in_days: int | None = None,
    delta_status: DeltaStatus | None = DeltaStatus.NEW,
    headline: str = "",
) -> Signal:
    facts: list[Fact] = []
    if sunset_in_days is not None:
        facts.append(
            Fact(
                kind=FactKind.SUNSET_DATE,
                value=(TODAY + timedelta(days=sunset_in_days)).isoformat(),
                source_url=url or "",
                evidence="deprecation date Jun 30, 2027",
            )
        )
    return Signal(
        signal_id=signal_id,
        run_id="run-portability",
        signal_type=SignalType.DIGEST_ITEM,
        created_at=NOW,
        for_date=TODAY,
        headline=headline or signal_id,
        change_type=change_type,
        vendor=vendor,
        product=product,
        facts=facts,
        primary_url=url,
        delta_status=delta_status,
    )


def add_statement(
    conn,
    sid: str,
    vendor: str,
    change_type: str,
    event_date: date | None,
    *,
    index: int = 0,
    text: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO event_statements (statement_id, text, vendor, change_type, "
        "event_date, source_url, statement_index, evidence, ingested_at, ingest_mode, "
        "extractor_model, prompt_version, raw_material_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            text or f"{vendor}: {change_type} announced with a dated deadline",
            vendor,
            change_type,
            event_date.isoformat() if event_date else None,
            f"https://example.test/{sid}",
            index,
            "support ends November 13, 2025",
            NOW.isoformat(),
            "backfill",
            "test-model",
            "v1",
            "cache/ref",
        ),
    )
    conn.commit()


# -- 1. The config itself ---------------------------------------------------


class TestConfigLoads:
    def test_the_second_theme_passes_the_same_validation(self, config):
        assert config.name
        assert config.description.strip()
        assert config.relevance_criteria.strip()
        assert config.exclusion_criteria.strip()

    def test_every_section_a_stage_reads_is_present(self, config):
        for section in (
            "collection",
            "retrieval",
            "trends",
            "scoring",
            "filter",
            "enrichment",
            "delivery",
            "models",
            "budget",
        ):
            assert config.section(section), f"empty section: {section}"

    def test_the_dictionary_is_the_new_domain_and_not_the_old_one(
        self, config, ai_config
    ):
        shared = set(config.vendor_ids) & set(ai_config.vendor_ids)
        # One id survives the move, and it does not survive intact: the AI
        # theme means Bedrock by `aws`, this one means the platform.
        assert shared == {"aws"}
        labels = {v["id"]: v["label"] for v in config.vendors}
        ai_labels = {v["id"]: v["label"] for v in ai_config.vendors}
        assert labels["aws"] != ai_labels["aws"]
        assert len(set(config.vendor_ids) - shared) >= 10

    def test_sources_are_unique_enabled_and_adapter_backed(self, config, tmp_path):
        sources = config.enabled_sources()
        assert len(sources) >= 6
        assert len({s.id for s in sources}) == len(sources)
        fetcher = Fetcher(cache_root=tmp_path / "cache")
        for source in sources:
            assert source.type in ADAPTERS, source.id
            assert build_adapter(source, fetcher) is not None, source.id

    def test_all_three_adapter_kinds_are_exercised(self, config):
        """A domain proven on one adapter proves one adapter, not the pipeline."""
        kinds = {s.type for s in config.enabled_sources()}
        assert kinds == {"html_scrape", "rss", "github_releases"}

    def test_source_authority_resolves_from_the_new_source_list(self, config):
        assert resolve_source_priority(config.data, url=LAMBDA_URL) == 1
        assert resolve_source_priority(config.data, source_id="gh_kubernetes") == 2
        # A URL from the other theme has no authority under this config.
        assert (
            resolve_source_priority(
                config.data, url="https://platform.openai.com/docs/deprecations"
            )
            is None
        )


# -- 2. Normalization on the new dictionary ---------------------------------


class TestNormalizationOnTheNewDictionary:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("AWS", "aws"),
            ("Amazon Web Services", "aws"),
            ("AWS Lambda", "aws"),
            ("Postgres", "postgresql"),
            ("PostgreSQL 18", "postgresql"),
            ("GKE", "google_cloud"),
            ("Google Kubernetes Engine", "google_cloud"),
            ("Terraform", "hashicorp"),
            ("Cloudflare Workers", "cloudflare"),
            ("stripe-node", "stripe"),
            ("Valkey", "redis"),
        ],
    )
    def test_aliases_fold_onto_one_id(self, normalizer, raw, expected):
        assert normalizer.vendor(raw) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (LAMBDA_URL, "aws"),
            (PG_URL, "postgresql"),
            ("https://docs.stripe.com/changelog", "stripe"),
            ("https://developers.cloudflare.com/changelog/rss.xml", "cloudflare"),
            ("https://shopify.dev/changelog/feed.xml", "shopify"),
        ],
    )
    def test_the_domain_alone_identifies_the_vendor(self, normalizer, url, expected):
        assert normalizer.vendor_from_url(url) == expected

    def test_a_vendor_of_the_previous_theme_is_now_unknown(self, config):
        """`unknown_vendors` accumulates, so this normalizer is not shared."""
        local = Normalizer.from_config(config.vendors, config.change_types)
        assert local.vendor("Anthropic") is None
        assert dict(local.report_unknown()) == {"Anthropic": 1}

    def test_every_source_speaks_for_a_vendor_the_dictionary_knows(
        self, config, normalizer
    ):
        """`SourceConfig.vendor` is authoritative downstream, so a source whose
        vendor is missing from the dictionary would file its whole history
        under an id retrieval can never match."""
        for source in config.enabled_sources():
            assert source.vendor, source.id
            assert normalizer.vendor(source.vendor) == source.vendor, source.id

    def test_labels_come_from_the_config(self, normalizer):
        assert normalizer.label("google_cloud") == "Google Cloud"
        assert normalizer.label("rabbitmq") == "RabbitMQ"

    def test_the_configured_change_types_all_normalize(self, config, normalizer):
        for entry in config.change_types:
            assert str(normalizer.change_type(entry["id"])) == entry["id"]


# -- 3. Where the config stops and the code starts --------------------------


class TestVocabularyIsCode:
    """The one part of the theme that a config cannot actually replace.

    `corpus.change_types` looks like a dictionary the theme owns. It is not:
    `ChangeType` in radar/models.py is a closed enum, and four call sites
    construct it from a corpus value. A config may relabel the seven members
    and may drop some; it cannot add an eighth.
    """

    def test_an_id_outside_the_enum_survives_validation_and_fails_in_use(self):
        data = {
            "theme": {"name": "boundary probe"},
            "corpus": {
                "vendors": [{"id": "aws", "label": "AWS"}],
                # The natural eighth type for this domain: support ended, the
                # date is in the past, the action is due now. Distinct from an
                # announced retirement, and unrepresentable.
                "change_types": [{"id": "end_of_life", "label": "Конец поддержки"}],
            },
            "sources": [{"id": "s1", "type": "rss", "url": "https://example.test"}],
        }
        config = ThemeConfig(data)  # load-time validation lets it through
        assert config.change_type_ids == ["end_of_life"]

        normalizer = Normalizer.from_config(config.vendors, config.change_types)
        with pytest.raises(ValueError, match="end_of_life"):
            normalizer.change_type("end_of_life")

    def test_a_domain_phrase_the_hardcoded_synonyms_miss_becomes_other(
        self, normalizer
    ):
        """The synonym table in radar/normalize.py is not reachable from config."""
        # Words the table happens to know, because they are also AI-tooling words.
        assert normalizer.change_type("sunset") is ChangeType.DEPRECATION
        assert normalizer.change_type("retirement") is ChangeType.DEPRECATION
        # Words this domain uses that the table does not know.
        for phrase in ("end of life", "mandatory upgrade", "region closure"):
            assert normalizer.change_type(phrase) is ChangeType.OTHER

    def test_the_miss_is_silent_in_the_corpus_and_loud_in_the_ranking(
        self, config, normalizer
    ):
        """No error is raised, so the cost shows up only as a score gap."""
        as_deprecation = score_signal(
            make_signal("a", change_type=ChangeType.DEPRECATION),
            config.data,
            as_of=TODAY,
        )
        as_other = score_signal(
            make_signal("b", change_type=normalizer.change_type("end of life")),
            config.data,
            as_of=TODAY,
        )
        assert as_deprecation.score - as_other.score >= 25

    def test_fact_kinds_are_bounded_by_code_the_same_way(self, config):
        """`enrichment.fact_kinds` is a selection from FactKind, not a vocabulary.

        The enricher keeps only the configured kinds it recognizes and falls
        back to the whole enum when it recognizes none, so an invented kind
        raises nothing and changes nothing.
        """
        known = {str(kind) for kind in FactKind}
        assert set(config.enrichment["fact_kinds"]) <= known
        assert "migration_deadline" not in known
        assert "affected_region" not in known


# -- 3b. The two stages that do call a model, up to the call ----------------


class TestPromptsAreBuiltFromTheTheme:
    """Stages 3 and 4 cannot be run here, but their theme-shaped half can.

    Both build a cache prefix out of the config and nothing else. If a domain
    word were hardcoded in a prompt, it would show up in these strings.
    """

    def test_the_filter_prefix_carries_this_theme_and_only_this_theme(
        self, config, ai_config
    ):
        prefix = build_cache_prefix(config)
        assert config.name in prefix
        assert "Cloudflare" in prefix and "PostgreSQL" in prefix
        assert "квот" in prefix  # exclusion and relevance criteria, verbatim
        for vendor_id in config.vendor_ids:
            assert vendor_id in prefix
        for stale in ("anthropic", "cursor", "llamaindex"):
            assert stale not in prefix
        assert build_cache_prefix(ai_config) != prefix

    def test_the_enrichment_prefix_carries_the_configured_fact_kinds(self, config):
        prefix = cache_prefix_for(config)
        assert config.name in prefix
        for kind in config.enrichment["fact_kinds"]:
            assert kind in prefix
        for change_type in config.change_type_ids:
            assert change_type in prefix

    def test_the_prefix_is_byte_stable_across_calls(self, config):
        """A prefix that varies turns every cached read into a paid write."""
        assert build_cache_prefix(config) == build_cache_prefix(config)
        assert cache_prefix_for(config) == cache_prefix_for(config)


# -- 4. Clustering -----------------------------------------------------------


def make_item(url: str, title: str, source_id: str, vendor: str) -> CollectedItem:
    return CollectedItem(
        url=url,
        title=title,
        raw_text=title,
        published_at=NOW,
        event_date=TODAY,
        vendor_hint=vendor,
        extra={"source_id": source_id},
    )


class TestClusteringIsStable:
    def test_the_same_input_yields_the_same_ids_twice(self):
        items = [
            make_item(
                LAMBDA_URL + "#nodejs20",
                "Node.js 20 runtime deprecation",
                "aws_lambda_runtime_deprecations",
                "aws",
            ),
            make_item(
                PG_URL + "#v13",
                "PostgreSQL 13 reaches final release",
                "postgresql_release_support",
                "postgresql",
            ),
        ]
        first = [c.cluster_id for c in cluster_items(items)]
        second = [c.cluster_id for c in cluster_items(items)]
        assert first == second
        assert len(set(first)) == 2

    def test_input_order_does_not_move_the_ids(self):
        items = [
            make_item(f"{LAMBDA_URL}#r{i}", f"Runtime {i} deprecation", "s", "aws")
            for i in range(5)
        ]
        forward = [c.cluster_id for c in cluster_items(items)]
        backward = [c.cluster_id for c in cluster_items(list(reversed(items)))]
        assert forward == backward

    def test_two_sources_on_one_event_collapse_into_one_cluster(self):
        url = "https://azure.microsoft.com/updates?id=568344"
        items = [
            make_item(
                url,
                "Azure Service Bus premium retirement",
                "azure_updates_feed",
                "azure",
            ),
            make_item(
                url, "Azure Service Bus premium retirement", "gh_terraform", "azure"
            ),
        ]
        clusters = cluster_items(
            items, priority_of={"azure_updates_feed": 1, "gh_terraform": 2}
        )
        assert len(clusters) == 1
        assert clusters[0].duplicates_count == 1
        assert clusters[0].seen_in == ["azure_updates_feed", "gh_terraform"]
        # FR-2.2: the authoritative source becomes the primary.
        assert clusters[0].primary.extra["source_id"] == "azure_updates_feed"

    def test_two_versions_of_one_engine_stay_apart(self):
        items = [
            make_item(
                f"{PG_URL}#13", "PostgreSQL 13.23 final release", "s", "postgresql"
            ),
            make_item(
                f"{PG_URL}#14", "PostgreSQL 14.24 final release", "s", "postgresql"
            ),
        ]
        clusters = cluster_items(items)
        assert len({c.cluster_id for c in clusters}) == 2

    def test_vendor_and_change_type_both_take_part_in_the_key(self):
        """Same headline, different classification, different history."""
        signature = title_signature("Python 3.12 runtime deprecation")
        deprecation = make_cluster_id("aws", "deprecation", signature)
        release = make_cluster_id("aws", "release", signature)
        other_vendor = make_cluster_id("azure", "deprecation", signature)
        assert len({deprecation, release, other_vendor}) == 3
        assert deprecation == make_cluster_id("aws", "deprecation", signature)


# -- 5. Scoring on the new weights ------------------------------------------


class TestScoringOnTheNewWeights:
    def test_the_number_is_arithmetic_the_config_can_explain(self, config):
        breakdown = score_signal(
            make_signal("s1", sunset_in_days=150, product="AWS Lambda"),
            config.data,
            as_of=TODAY,
        )
        weights = config.scoring["weights"]
        ceiling = (
            max(weights["change_type"].values())
            + weights["urgency"]
            + weights["stack_overlap"]
            + weights["source_authority"]
            + max(weights["novelty"].values())
        )
        assert breakdown.max_points == pytest.approx(ceiling)
        assert (
            breakdown.by_key("change_type").points
            == weights["change_type"]["deprecation"]
        )
        assert breakdown.by_key("stack_overlap").points == weights["stack_overlap"]
        assert (
            breakdown.by_key("source_authority").points == weights["source_authority"]
        )
        assert breakdown.days_until_due == 150

    def test_the_rationale_speaks_the_new_domain_labels(self, config):
        breakdown = score_signal(
            make_signal("s1", sunset_in_days=30, product="AWS Lambda"),
            config.data,
            as_of=TODAY,
        )
        assert "вывод из эксплуатации" in breakdown.rationale.lower()
        assert breakdown.rationale != ""
        # FR-6.2: the sentence is built from the same factors as the number.
        named = {f.detail for f in breakdown.top_factors(3)}
        assert named <= {f.detail for f in breakdown.factors if f.applied}

    def test_quotas_outrank_vulnerabilities_here_and_the_reverse_holds_there(
        self, config, ai_config
    ):
        """One weight table swap, one order flip, no code involved."""
        signals = [
            make_signal("limits", change_type=ChangeType.LIMITS, url=None),
            make_signal("security", change_type=ChangeType.SECURITY, url=None),
        ]
        here = [
            s.signal.signal_id for s in rank_signals(signals, config.data, as_of=TODAY)
        ]
        there = [
            s.signal.signal_id
            for s in rank_signals(signals, ai_config.data, as_of=TODAY)
        ]
        assert here == ["limits", "security"]
        assert there == ["security", "limits"]

    def test_the_longer_horizon_is_what_makes_an_annual_warning_visible(
        self, config, ai_config
    ):
        """The demo signal: a PostgreSQL end of support 170 days out."""
        signal = make_signal(
            "pg-eol",
            vendor="postgresql",
            product="PostgreSQL 14",
            url=PG_URL,
            sunset_in_days=170,
        )

        here = rank_signals([signal], config.data, as_of=TODAY)[0]
        there = rank_signals([signal], ai_config.data, as_of=TODAY)[0]

        assert here.breakdown.by_key("urgency").applied
        assert here.breakdown.by_key("urgency").points > 0
        # 90-day horizon: the same date scores nothing at all.
        assert there.breakdown.by_key("urgency").points == 0
        assert here.signal.score - there.signal.score >= 20
        # The same record, the same code, two verdicts about whether it leads.
        assert here.signal.tier is Tier.LEAD
        assert there.signal.tier is Tier.STANDARD

    def test_a_signal_carrying_no_date_loses_nothing(self, config):
        """FR-4.4 inside scoring: a missing date is missing, not urgent-zero."""
        breakdown = score_signal(make_signal("s1"), config.data, as_of=TODAY)
        urgency = breakdown.by_key("urgency")
        assert urgency.applied is False
        assert urgency.points == 0
        assert breakdown.by_key("change_type").points > 0

    def test_the_ranking_is_total_and_replayable(self, config):
        signals = [
            make_signal("a", change_type=ChangeType.RELEASE, url=None),
            make_signal("b", change_type=ChangeType.DEPRECATION, sunset_in_days=10),
            make_signal("c", change_type=ChangeType.PRICING, url=None),
        ]
        first = [
            s.signal.signal_id for s in rank_signals(signals, config.data, as_of=TODAY)
        ]
        shuffled = [signals[2], signals[0], signals[1]]
        second = [
            s.signal.signal_id for s in rank_signals(shuffled, config.data, as_of=TODAY)
        ]
        assert first == second == ["b", "c", "a"]


# -- 6. Retrieval and trends on a synthetic corpus of the new domain --------


class TestRetrievalOnTheNewCorpus:
    def test_the_wider_windows_are_what_find_an_annual_precedent(
        self, db, config, ai_config
    ):
        """Retirement cycles here are annual; a 180-day window sees nothing."""
        for i, days_back in enumerate((420, 700)):
            add_statement(
                db,
                f"aws{i}",
                "aws",
                "deprecation",
                TODAY - timedelta(days=days_back),
                index=i,
            )

        here = CorpusRetriever(db, config.retrieval)
        there = CorpusRetriever(db, ai_config.retrieval)

        found = here.find_precedents("aws", "deprecation", TODAY)
        missed = there.find_precedents("aws", "deprecation", TODAY)

        assert len(found.hits) == 2
        assert here.label_for(found) is ContextLabel.RECURRING
        assert missed.hits == []
        assert there.label_for(missed) is ContextLabel.NOT_FOUND_IN_CORPUS

    def test_both_filters_stay_mandatory_on_the_new_theme(self, db, config):
        add_statement(db, "s1", "stripe", "breaking_change", TODAY - timedelta(days=30))
        retriever = CorpusRetriever(db, config.retrieval)
        assert retriever.find_precedents(None, "breaking_change", TODAY).hits == []
        assert retriever.find_precedents("stripe", None, TODAY).hits == []

    def test_the_relaxed_pair_of_this_domain_makes_a_near_miss_countable(
        self, db, config
    ):
        """A quota change filed as pricing must not read as no precedents."""
        add_statement(db, "l1", "cloudflare", "limits", TODAY - timedelta(days=90))
        add_statement(
            db, "p1", "cloudflare", "pricing", TODAY - timedelta(days=200), index=1
        )
        add_statement(
            db, "p2", "cloudflare", "pricing", TODAY - timedelta(days=300), index=2
        )

        result = CorpusRetriever(db, config.retrieval).find_precedents(
            "cloudflare", "limits", TODAY
        )
        assert result.report.strict_hits == 1
        assert result.report.relaxed_hits == 3
        assert {h.statement_id for h in result.relaxed_only} == {"p1", "p2"}
        # A relaxed hit is counted, never published.
        assert [p.statement_id for p in result.precedents] == ["l1"]

    def test_precedents_render_whole_under_the_new_theme(self, db, config):
        for i in range(2):
            add_statement(
                db,
                f"pg{i}",
                "postgresql",
                "deprecation",
                TODAY - timedelta(days=200 + i * 200),
                index=i,
            )
        precedents = (
            CorpusRetriever(db, config.retrieval)
            .find_precedents("postgresql", "deprecation", TODAY)
            .precedents
        )
        assert len(precedents) == 2
        assert all(p.vendor == "postgresql" for p in precedents)
        assert all(p.change_type is ChangeType.DEPRECATION for p in precedents)
        assert all(p.text and p.source_url for p in precedents)


class TestTrendsOnTheNewCorpus:
    def test_a_yearly_retirement_line_is_accepted_and_labelled_in_domain_words(
        self, db, config
    ):
        for i, days_back in enumerate((700, 430, 160)):
            add_statement(
                db,
                f"pg{i}",
                "postgresql",
                "deprecation",
                TODAY - timedelta(days=days_back),
                index=i,
            )
        accepted, _, saved = refresh_trends(db, config)

        assert saved == 1
        assert [(c.vendor, c.change_type) for c in accepted] == [
            ("postgresql", "deprecation")
        ]
        label = db.execute("SELECT label FROM trends").fetchone()["label"]
        assert label.startswith("PostgreSQL: Вывод из эксплуатации")
        assert "3 события" in label

    def test_the_longer_dormancy_window_keeps_an_annual_line_alive(
        self, db, config, ai_config
    ):
        for i, days_back in enumerate((700, 430, 160)):
            add_statement(
                db,
                f"pg{i}",
                "postgresql",
                "deprecation",
                TODAY - timedelta(days=days_back),
                index=i,
            )
        candidate = find_candidates(db, min_members=config.trends["min_members"])[0][0]

        here = candidate.trajectory(TODAY, config.trends["dormant_after_days"])
        there = candidate.trajectory(TODAY, ai_config.trends["dormant_after_days"])
        assert here is Trajectory.EMERGING
        assert there is Trajectory.DORMANT

    def test_a_vendor_that_ships_daily_still_does_not_produce_a_trend(self, db, config):
        """Cloudflare posts several changelog entries a day; recurrence there
        carries no information, and the guard is domain independent."""
        for i in range(12):
            add_statement(
                db,
                f"cf{i}",
                "cloudflare",
                "breaking_change",
                TODAY - timedelta(days=i),
                index=i,
            )
        accepted, rejected, _ = refresh_trends(db, config)
        assert accepted == []
        assert any("постоянно" in (c.rejected_reason or "") for c in rejected)

    def test_readiness_is_measured_against_the_new_dense_cells(self, db, config):
        dense = config.readiness["dense_cell_change_types"]
        assert dense == ["deprecation", "breaking_change", "limits"]
        for vendor in ("aws", "stripe", "cloudflare"):
            for i in range(3):
                add_statement(
                    db,
                    f"{vendor}{i}",
                    vendor,
                    "deprecation",
                    TODAY - timedelta(days=100 * (i + 1)),
                    index=i,
                )
        report = corpus_readiness(db, config.data)
        assert report["required_events_per_cell"] == 3
        assert report["vendors_with_dense_cell"] == ["aws", "cloudflare", "stripe"]
        # Density is there — three vendors with a dense cell — and the verdict
        # is still no, because the theme also asks for a minimum size and depth
        # and nine statements do not meet them. Those two numbers used to sit
        # in the config without deciding anything.
        assert report["ready_for_trend_demo"] is False
        assert report["total_statements"] < report["required_statements"]


# -- 7. The sources themselves ----------------------------------------------


@pytest.mark.integration
@network
class TestSourcesAnswerInThisDomain:
    """Real requests through the project cache. First run pays, later ones do not."""

    @pytest.mark.parametrize(
        "source_id",
        [
            "aws_lambda_runtime_deprecations",
            "postgresql_release_support",
            "gke_deprecations",
            "stripe_api_changelog",
            "azure_updates_feed",
            "shopify_changelog_feed",
        ],
    )
    def test_a_configured_source_yields_dated_material(
        self, config, fetcher, source_id
    ):
        source = config.source(source_id)
        outcome = collect_source(source, fetcher, mode="live")

        assert outcome.status.value == "ok", outcome.error
        assert outcome.count >= source.min_expected_items
        # FR-5.17: the date of the event, not the date the page was fetched.
        dated = [i for i in outcome.items if i.event_date is not None]
        assert len(dated) >= source.min_expected_items
        # Not every entry carries a body: the Shopify feed publishes a handful
        # of title-only items. They stay in, and FR-4.3 drops them later, since
        # a material with no text can never yield a verifiable quote.
        with_text = [i for i in outcome.items if i.raw_text.strip()]
        assert len(with_text) >= 0.95 * outcome.count

    def test_the_release_adapter_works_on_a_non_ai_repository(self, config, fetcher):
        source = config.source("gh_rabbitmq")
        outcome = collect_source(
            source, fetcher, mode="live", since=datetime.now(UTC) - timedelta(days=365)
        )
        assert outcome.status.value == "ok", outcome.error
        assert all(i.event_date is not None for i in outcome.items)

    def test_a_retirement_table_carries_a_future_date_and_a_quotable_line(
        self, config, fetcher
    ):
        """The epistemic shape the theme depends on, taken from the live page."""
        source = config.source("aws_lambda_runtime_deprecations")
        items = collect_source(source, fetcher, mode="live").items

        future = [i for i in items if i.event_date and i.event_date > TODAY]
        assert future, "a deprecation registry with no future dates is not one"
        sample = future[0]
        assert len(sample.raw_text.split()) >= 5
        assert sample.url.startswith("https://docs.aws.amazon.com/")

    def test_the_collected_vendor_matches_the_configured_one(
        self, config, normalizer, fetcher
    ):
        for source_id in ("postgresql_release_support", "stripe_api_changelog"):
            source = config.source(source_id)
            outcome = collect_source(source, fetcher, mode="live")
            assert normalizer.vendor_from_url(outcome.items[0].url) == source.vendor
