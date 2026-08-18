import pytest

from radar.config import ConfigError, ThemeConfig
from radar.models import ChangeType
from radar.normalize import Normalizer, subject_identity, model_identifiers

CONFIG_PATH = "config/ai-tools.yaml"


@pytest.fixture(scope="module")
def theme() -> ThemeConfig:
    return ThemeConfig.load(CONFIG_PATH)


@pytest.fixture(scope="module")
def normalizer(theme: ThemeConfig) -> Normalizer:
    return Normalizer.from_config(theme.vendors, theme.change_types)


class TestThemeConfig:
    def test_shipped_config_loads(self, theme):
        assert theme.name
        assert theme.vendor_ids
        assert theme.change_type_ids

    def test_every_source_has_a_known_type(self, theme):
        known = {"rss", "github_releases", "html_scrape", "telegram_channel"}
        assert {s.type for s in theme.sources} <= known

    def test_enabled_sources_can_be_filtered_by_type(self, theme):
        assert all(
            s.type == "github_releases"
            for s in theme.enabled_sources("github_releases")
        )

    def test_backfillable_sources_are_a_subset(self, theme):
        assert set(s.id for s in theme.backfillable_sources()) <= set(
            s.id for s in theme.enabled_sources()
        )

    def test_thresholds_come_from_config(self, theme):
        assert theme.scoring["publish_threshold"] > theme.scoring["digest_threshold"]

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError):
            ThemeConfig.load("config/does-not-exist.yaml")

    def test_missing_section_raises(self):
        with pytest.raises(ConfigError):
            ThemeConfig({"theme": {}, "corpus": {}})

    def test_duplicate_source_id_raises(self):
        data = {
            "theme": {},
            "corpus": {"vendors": [{"id": "a"}], "change_types": [{"id": "release"}]},
            "sources": [
                {"id": "x", "type": "rss", "url": "u"},
                {"id": "x", "type": "rss", "url": "u2"},
            ],
        }
        with pytest.raises(ConfigError, match="duplicate source id"):
            ThemeConfig(data)

    def test_empty_vendor_dictionary_raises(self):
        """Retrieval filters are mandatory, so an empty dictionary is fatal."""
        data = {
            "theme": {},
            "corpus": {"vendors": [], "change_types": [{"id": "release"}]},
            "sources": [],
        }
        with pytest.raises(ConfigError, match="vendors"):
            ThemeConfig(data)


class TestVendorNormalization:
    @pytest.mark.parametrize(
        "raw",
        [
            "anthropic",
            "Anthropic",
            "ANTHROPIC",
            "claude",
            "Claude",
            "Claude Code",
            "claude-code",
        ],
    )
    def test_all_spellings_collapse_to_one_id(self, normalizer, raw):
        assert normalizer.vendor(raw) == "anthropic"

    def test_distinct_vendors_stay_distinct(self, normalizer):
        assert normalizer.vendor("OpenAI") == "openai"
        assert normalizer.vendor("Cursor") == "cursor"
        assert normalizer.vendor("OpenAI") != normalizer.vendor("Cursor")

    def test_alias_inside_a_longer_phrase_resolves(self, normalizer):
        assert normalizer.vendor("Anthropic Claude Code CLI") == "anthropic"

    def test_unknown_vendor_is_reported_not_invented(self, normalizer):
        fresh = Normalizer.from_config(
            [{"id": "anthropic", "label": "Anthropic", "aliases": ["claude"]}],
            [{"id": "release"}],
        )
        assert fresh.vendor("Perplexity") is None
        assert fresh.report_unknown() == [("Perplexity", 1)]

    def test_unknown_vendors_are_counted(self, normalizer):
        fresh = Normalizer.from_config([{"id": "anthropic"}], [{"id": "release"}])
        fresh.vendor("Perplexity")
        fresh.vendor("Perplexity")
        assert fresh.report_unknown()[0] == ("Perplexity", 2)

    def test_empty_input_is_none_without_being_reported(self, normalizer):
        fresh = Normalizer.from_config([{"id": "anthropic"}], [{"id": "release"}])
        assert fresh.vendor("") is None
        assert fresh.vendor(None) is None
        assert fresh.report_unknown() == []

    def test_url_fallback_resolves_the_vendor(self, normalizer):
        assert (
            normalizer.vendor_from_url("https://docs.claude.com/en/release-notes/api")
            == "anthropic"
        )
        assert (
            normalizer.vendor_from_url("https://platform.openai.com/docs/deprecations")
            == "openai"
        )

    def test_url_fallback_returns_none_for_a_stranger(self, normalizer):
        assert normalizer.vendor_from_url("https://example.test/blog") is None


class TestChangeTypeNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("deprecation", ChangeType.DEPRECATION),
            ("Deprecated", ChangeType.DEPRECATION),
            ("sunset", ChangeType.DEPRECATION),
            ("retirement", ChangeType.DEPRECATION),
            ("breaking_change", ChangeType.BREAKING_CHANGE),
            ("Breaking Change", ChangeType.BREAKING_CHANGE),
            ("rate limits", ChangeType.LIMITS),
            ("quota", ChangeType.LIMITS),
            ("price", ChangeType.PRICING),
            ("CVE", ChangeType.SECURITY),
        ],
    )
    def test_synonyms_map_to_the_dictionary(self, normalizer, raw, expected):
        assert normalizer.change_type(raw) == expected

    def test_unknown_falls_back_to_other_never_to_empty(self, normalizer):
        """FR-5.16 forbids an empty change_type; the filter would silently miss it."""
        assert normalizer.change_type("никогда такого не было") is ChangeType.OTHER
        assert normalizer.change_type(None) is ChangeType.OTHER
        assert normalizer.change_type("") is ChangeType.OTHER

    def test_every_config_change_type_round_trips(self, theme, normalizer):
        for change_type_id in theme.change_type_ids:
            assert normalizer.change_type(change_type_id) == ChangeType(change_type_id)


class TestVendorFromSource:
    """Vendor comes from the source config, not from guessing at body text."""

    def test_every_first_party_source_declares_its_vendor(self, theme):
        undeclared = [s.id for s in theme.sources if s.priority <= 3 and not s.vendor]
        assert undeclared == []

    def test_declared_vendors_exist_in_the_dictionary(self, theme):
        known = set(theme.vendor_ids)
        assert all(s.vendor in known for s in theme.sources if s.vendor)

    def test_aggregators_declare_no_vendor(self, theme):
        """Media speaks for no single vendor; the model decides per item."""
        aggregators = [s for s in theme.sources if s.priority >= 4]
        assert aggregators
        assert all(s.vendor is None for s in aggregators)

    def test_a_release_body_linking_to_github_is_not_filed_under_github(self, normalizer):
        body = (
            "Zed 0.150 fixes a crash in the terminal. See "
            "https://github.com/zed-industries/zed/pull/12345 for details and "
            "the full list of changes in this release."
        )
        assert normalizer.vendor(body) is None

    def test_a_short_name_still_resolves(self, normalizer):
        assert normalizer.vendor("Claude Code") == "anthropic"
        assert normalizer.vendor("GitHub Copilot") == "github"

    def test_a_long_document_is_not_reported_as_an_unknown_vendor(self):
        """Reporting whole documents would drown the dictionary gap report."""
        fresh = Normalizer.from_config([{"id": "anthropic"}], [{"id": "release"}])
        fresh.vendor("a fairly long sentence of body text that names nobody at all here")
        assert fresh.report_unknown() == []


class TestModelNamesWrittenAsProse:
    """The extractor fills `product` from the sentence as often as from the API.

    Two cards about one price change carried "Claude Sonnet 5" and
    "claude-sonnet-5" and were therefore two subjects, so the digest opened
    with the same announcement twice, worded from two sides.
    """

    def test_a_spaced_name_is_the_same_subject_as_the_api_name(self):
        assert subject_identity("anthropic", "pricing", "Claude Sonnet 5") == \
               subject_identity("anthropic", "pricing", "claude-sonnet-5")

    def test_a_snapshot_stays_distinct_from_its_family(self):
        family = subject_identity("anthropic", "deprecation", "Claude Sonnet 5")
        snapshot = subject_identity(
            "anthropic", "deprecation", "claude-sonnet-5-20250929"
        )
        assert family != snapshot

    def test_prose_without_a_version_is_not_mistaken_for_a_model(self):
        # "Claude" alone names the product line, not a model: it must not
        # collapse two different announcements into one subject.
        one = subject_identity("anthropic", "pricing", "Claude", None, "цены выросли")
        two = subject_identity("anthropic", "pricing", "Claude", None, "лимиты сняты")
        assert one != two

    def test_two_changes_on_one_page_stay_two_subjects(self):
        # GitHub's GraphQL changelog names fields, not models. Nothing here
        # looks like a model identifier, and the fallback must keep them apart.
        one = subject_identity(
            "github", "breaking_change", None, None,
            "GitHub удалил значение enum SECURITY_KEY из ProofOfPresenceRequirement",
        )
        two = subject_identity(
            "github", "breaking_change", None, None,
            "GitHub удалит поле User.viewerRelevantRepositories в GraphQL API",
        )
        assert one != two


class TestASubjectIsAModelNotADate:
    """`event_identity` decides what counts as one event; it was reading dates.

    `_hyphenate` turns "February 12" into "february-12" so a prose name like
    "Claude Sonnet 5" can be recognised — and the identifier pattern then
    accepted the month as a model. One Google deprecation was keyed
    `google|deprecation|2024-02-12|february-12`: the event identified by its own
    date. Fourteen corpus records carried a month as their subject.

    The pattern also demanded a digit in the last segment, so `claude-3-opus`
    matched as `claude-3` — two different Opus models sharing a subject.
    """

    def test_a_month_is_never_a_subject(self):
        found = model_identifiers("multimodalembedding@001 | February 12, 2024 | April 1, 2027")
        assert found == ["multimodalembedding@001"]

    def test_a_name_is_not_cut_at_its_last_digit(self):
        assert model_identifiers("claude-3-opus | June 5, 2026") == ["claude-3-opus"]

    def test_two_models_of_one_family_stay_apart(self):
        assert subject_identity("anthropic", "deprecation", "claude-3-opus") != \
               subject_identity("anthropic", "deprecation", "claude-3-haiku")

    def test_the_vertex_at_sign_form_is_recognised(self):
        assert model_identifiers("multimodalembedding@001") == ["multimodalembedding@001"]

    def test_a_date_only_row_yields_no_subject_at_all(self):
        """Better no subject than the wrong one: with none, identity falls back
        to the statement and two records stay apart, which costs a duplicate.
        With a month, unrelated events merge and one of them disappears."""
        assert model_identifiers("February 12, 2024 | April 1, 2027") == []


class TestIdentityIsCheckedWhereItCanFail:
    """«Назовите тест, который упал бы, если бы event_identity сломался завтра.»

    The one that claimed to was a tautology: it asked the corpus for duplicate
    `event_key` values while a unique index on that column made the answer
    empty by construction. It could not fail, and meanwhile the live corpus
    held fourteen records whose subject was a month.

    These run the function against the shapes the corpus actually contains, so
    a change to the rule shows up here rather than in production three commits
    later.
    """

    ROWS = [
        # (source row, expected subject) — taken from the live corpus.
        ("claude-opus-4-5-20251101 | Active | N/A | Not sooner than November 24, 2026",
         "claude-opus-4-5-20251101"),
        ("multimodalembedding@001 | February 12, 2024 | April 1, 2027",
         "multimodalembedding@001"),
        ("text-embedding-004 | June 24, 2025 | April 1, 2027", "text-embedding-004"),
        ("veo-3.1-fast-generate-001 | November 17, 2025", "veo-3.1-fast-generate-001"),
        ("gpt-4.1-nano | 2025-04-14 | 2027-10-14", "gpt-4.1-nano"),
        ("Anthropic сохраняет цену Claude Sonnet 5", "claude-sonnet-5"),
    ]

    @pytest.mark.parametrize("row,expected", ROWS)
    def test_the_subject_is_the_model_the_row_is_about(self, row, expected):
        assert model_identifiers(row)[:1] == [expected]

    def test_two_readings_of_one_row_agree(self):
        """The case the guard exists for: the same shutdown, extracted twice in
        different words, must key the same."""
        first = subject_identity(
            "google", "deprecation", "text-embedding-004", None,
            "Google отключает модель text-embedding-004 1 апреля 2027 года.",
        )
        second = subject_identity(
            "google", "deprecation", None, "text-embedding-004 | April 1, 2027",
            "Модель text-embedding-004 будет выведена из обслуживания.",
        )
        assert first == second

    def test_the_stamped_version_matches_the_rule_in_force(self):
        """A corpus outlives the function that keyed it. When the rule changes
        without the stamp changing, the migration has nothing to detect."""
        from radar.normalize import IDENTITY_VERSION

        assert IDENTITY_VERSION == "identity-v2"
