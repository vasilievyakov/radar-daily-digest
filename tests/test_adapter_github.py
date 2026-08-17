import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from radar.adapters.base import SourceConfig
from radar.adapters.github_releases import (
    GitHubReleasesAdapter,
    parse_repo,
    resolve_token,
)
from radar.cache import HttpCache
from radar.fetch import FetchResult
from radar.models import DatePrecision

REPO_URL = "https://github.com/anthropics/claude-code"
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def release(
    tag: str,
    published: datetime | str | None,
    *,
    name: str | None = None,
    body: str = "## What's changed\n\n- something",
    draft: bool = False,
    prerelease: bool = False,
    release_id: int = 1,
) -> dict:
    published_at = (
        published.strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(published, datetime)
        else published
    )
    return {
        "id": release_id,
        "tag_name": tag,
        "name": tag if name is None else name,
        "body": body,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": published_at,
        "created_at": published_at,
        "html_url": f"{REPO_URL}/releases/tag/{tag}",
    }


def page_ok(releases: list[dict]) -> dict:
    return {"status": 200, "text": json.dumps(releases)}


def page_status(code: int, headers: dict[str, str] | None = None, text: str = "{}"):
    return {"status": code, "text": text, "headers": headers or {}}


def page_error(message: str) -> dict:
    return {"status": 0, "text": "", "error": message}


class FakeFetcher:
    """Fetcher stand-in keyed by the `page` query parameter."""

    def __init__(self, pages: dict[int, dict], missing: dict | None = None) -> None:
        self.pages = pages
        self.missing = missing if missing is not None else page_ok([])
        self.calls: list[dict] = []

    def get(self, url, headers=None, force=False, cache_key_extra=None):
        page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        self.calls.append(
            {
                "url": url,
                "page": page,
                "headers": dict(headers or {}),
                "cache_key_extra": cache_key_extra,
            }
        )
        spec = self.pages.get(page, self.missing)
        return FetchResult(
            url=url,
            status_code=spec.get("status", 200),
            text=spec.get("text", ""),
            headers=spec.get("headers", {}),
            ref=HttpCache.key_for(url),
            from_cache=False,
            error=spec.get("error"),
        )

    @property
    def pages_requested(self) -> list[int]:
        return [call["page"] for call in self.calls]


def make_source(**overrides) -> SourceConfig:
    data = {
        "id": "gh_anthropics_claude_code",
        "type": "github_releases",
        "url": REPO_URL,
        "priority": 3,
        "backfill_supported": True,
        "backfill_depth_days": 360,
    }
    data.update(overrides)
    return SourceConfig(**data)


def make_adapter(pages, source=None) -> tuple[GitHubReleasesAdapter, FakeFetcher]:
    fetcher = FakeFetcher(pages)
    return GitHubReleasesAdapter(source or make_source(), fetcher), fetcher


@pytest.fixture(autouse=True)
def token_in_env(monkeypatch):
    """Keeps the default test path away from the real `gh` binary."""
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")


@pytest.fixture
def small_pages(monkeypatch):
    monkeypatch.setattr(GitHubReleasesAdapter, "per_page", 2)


# --- URL parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/anthropics/claude-code", ("anthropics", "claude-code")),
        ("https://github.com/anthropics/claude-code/", ("anthropics", "claude-code")),
        ("http://github.com/openai/openai-python", ("openai", "openai-python")),
        ("https://www.github.com/n8n-io/n8n", ("n8n-io", "n8n")),
        ("https://GitHub.com/n8n-io/n8n", ("n8n-io", "n8n")),
        (
            "https://github.com/langchain-ai/langchain.git",
            ("langchain-ai", "langchain"),
        ),
        (
            "https://github.com/modelcontextprotocol/servers/releases",
            ("modelcontextprotocol", "servers"),
        ),
        (
            "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v0.122.0",
            ("anthropics", "anthropic-sdk-python"),
        ),
        (
            "https://api.github.com/repos/anthropics/claude-code/releases",
            ("anthropics", "claude-code"),
        ),
        ("git@github.com:anthropics/claude-code.git", ("anthropics", "claude-code")),
        ("anthropics/claude-code", ("anthropics", "claude-code")),
        ("  https://github.com/openai/openai-python  ", ("openai", "openai-python")),
    ],
)
def test_parse_repo_accepts_every_url_form(url, expected):
    assert parse_repo(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://github.com/anthropics",
        "https://gitlab.com/owner/repo",
        "https://example.com/anthropics/claude-code",
        "not a url",
    ],
)
def test_parse_repo_rejects_non_repo_urls(url):
    assert parse_repo(url) is None


def test_unparseable_url_yields_empty_list_not_an_exception():
    adapter, fetcher = make_adapter(
        {}, source=make_source(url="https://gitlab.com/a/b")
    )

    assert adapter.collect() == []
    assert fetcher.calls == []
    assert "not a github repository url" in adapter.extra["error"]


# --- item mapping --------------------------------------------------------


def test_release_maps_onto_collected_item():
    body = "## What's changed\n\n- Added `--worktree`\n- **Breaking**: removed `-x`"
    pages = {
        1: page_ok(
            [
                release(
                    "v2.1.233",
                    datetime(2026, 8, 14, 22, 20, 57, tzinfo=UTC),
                    name="v2.1.233",
                    body=body,
                    release_id=370762090,
                )
            ]
        )
    }
    adapter, fetcher = make_adapter(pages)

    (item,) = adapter.collect()

    assert item.title == "v2.1.233"
    assert item.raw_text == body  # verbatim markdown, nothing normalised
    assert item.url == f"{REPO_URL}/releases/tag/v2.1.233"
    assert item.version_hint == "v2.1.233"
    assert item.published_at == datetime(2026, 8, 14, 22, 20, 57, tzinfo=UTC)
    assert item.event_date == datetime(2026, 8, 14).date()
    assert item.date_precision is DatePrecision.DAY
    assert item.vendor_hint == "anthropics"
    assert item.raw_material_ref == HttpCache.key_for(fetcher.calls[0]["url"])
    assert item.extra["repo"] == "anthropics/claude-code"
    assert item.extra["release_id"] == 370762090
    assert "prerelease" not in item.extra


def test_title_falls_back_to_tag_when_release_is_unnamed():
    pages = {
        1: page_ok(
            [
                {**release("v1.0.0", NOW), "name": None},
                {**release("v0.9.0", NOW), "name": ""},
            ]
        )
    }
    adapter, _ = make_adapter(pages)

    assert [item.title for item in adapter.collect()] == ["v1.0.0", "v0.9.0"]


def test_release_without_published_at_falls_back_to_created_at():
    raw = release("v1.0.0", None)
    raw["created_at"] = "2026-08-10T10:00:00Z"
    adapter, _ = make_adapter({1: page_ok([raw])})

    (item,) = adapter.collect()

    assert item.published_at == datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    assert item.extra["date_from"] == "created_at"


# --- pagination ----------------------------------------------------------


def test_collect_walks_every_full_page(small_pages):
    pages = {
        1: page_ok([release("v3", NOW), release("v2", NOW - timedelta(days=1))]),
        2: page_ok(
            [
                release("v1", NOW - timedelta(days=2)),
                release("v0", NOW - timedelta(days=3)),
            ]
        ),
        3: page_ok([release("v-old", NOW - timedelta(days=4))]),
    }
    adapter, fetcher = make_adapter(pages)

    items = adapter.collect()

    assert [item.version_hint for item in items] == ["v3", "v2", "v1", "v0", "v-old"]
    assert fetcher.pages_requested == [1, 2, 3]  # a short page ends the walk
    assert adapter.extra["pages_fetched"] == 3
    assert adapter.extra["releases_seen"] == 5


def test_default_page_size_is_the_api_maximum():
    adapter, fetcher = make_adapter({1: page_ok([])})
    adapter.collect()

    query = parse_qs(urlsplit(fetcher.calls[0]["url"]).query)
    assert query["per_page"] == ["100"]
    assert query["page"] == ["1"]


def test_each_page_is_its_own_cache_entry(small_pages):
    pages = {
        1: page_ok([release("v3", NOW), release("v2", NOW)]),
        2: page_ok([release("v1", NOW)]),
    }
    adapter, fetcher = make_adapter(pages)
    adapter.collect()

    refs = {HttpCache.key_for(call["url"]) for call in fetcher.calls}
    assert len(refs) == len(fetcher.calls)  # the page number reaches the cache key
    assert all(call["cache_key_extra"] is None for call in fetcher.calls)


def test_collect_stops_once_a_page_is_wholly_older_than_since(small_pages):
    since = datetime(2026, 8, 1, tzinfo=UTC)
    pages = {
        1: page_ok(
            [
                release("v3", datetime(2026, 8, 14, tzinfo=UTC)),
                release("v2", datetime(2026, 8, 5, tzinfo=UTC)),
            ]
        ),
        2: page_ok(
            [
                release("v1", datetime(2026, 8, 2, tzinfo=UTC)),
                release("v0", datetime(2026, 7, 20, tzinfo=UTC)),
            ]
        ),
        3: page_ok(
            [
                release("v-old-1", datetime(2026, 7, 10, tzinfo=UTC)),
                release("v-old-2", datetime(2026, 7, 1, tzinfo=UTC)),
            ]
        ),
        4: page_ok([release("v-ancient", datetime(2026, 6, 1, tzinfo=UTC))]),
    }
    adapter, fetcher = make_adapter(pages)

    items = adapter.collect(since=since)

    assert [item.version_hint for item in items] == ["v3", "v2", "v1"]
    assert fetcher.pages_requested == [
        1,
        2,
        3,
    ]  # page 3 proves the boundary, 4 is never paid for
    assert adapter.extra["pages_fetched"] == 3


def test_collect_treats_since_as_exclusive():
    since = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    pages = {
        1: page_ok([release("v2", since), release("v1", since - timedelta(hours=1))])
    }
    adapter, _ = make_adapter(pages)

    assert adapter.collect(since=since) == []


def test_naive_since_is_read_as_utc():
    pages = {
        1: page_ok(
            [
                release("v2", datetime(2026, 8, 14, tzinfo=UTC)),
                release("v1", datetime(2026, 7, 1, tzinfo=UTC)),
            ]
        )
    }
    adapter, _ = make_adapter(pages)

    items = adapter.collect(since=datetime(2026, 8, 1))

    assert [item.version_hint for item in items] == ["v2"]


# --- backfill ------------------------------------------------------------


def test_backfill_stops_at_depth(small_pages):
    now = datetime.now(UTC)
    pages = {
        1: page_ok(
            [
                release("v4", now - timedelta(days=2)),
                release("v3", now - timedelta(days=10)),
            ]
        ),
        2: page_ok(
            [
                release("v2", now - timedelta(days=25)),
                release("v1", now - timedelta(days=40)),
            ]
        ),
        3: page_ok(
            [
                release("v0", now - timedelta(days=200)),
                release("v-1", now - timedelta(days=400)),
            ]
        ),
        4: page_ok([release("v-2", now - timedelta(days=500))]),
    }
    adapter, fetcher = make_adapter(pages)

    items = adapter.backfill(depth_days=30)

    assert [item.version_hint for item in items] == ["v4", "v3", "v2"]
    assert fetcher.pages_requested == [1, 2, 3]


def test_backfill_defaults_to_configured_depth(small_pages):
    now = datetime.now(UTC)
    pages = {
        1: page_ok(
            [
                release("v3", now - timedelta(days=100)),
                release("v2", now - timedelta(days=170)),
            ]
        ),
        2: page_ok(
            [
                release("v1", now - timedelta(days=200)),
                release("v0", now - timedelta(days=400)),
            ]
        ),
        3: page_ok([release("v-1", now - timedelta(days=500))]),
    }
    source = make_source(backfill_depth_days=180)
    adapter, fetcher = make_adapter(pages, source=source)

    items = adapter.backfill()

    assert [item.version_hint for item in items] == ["v3", "v2"]
    assert fetcher.pages_requested == [1, 2]


def test_backfill_without_depth_walks_the_whole_history(small_pages):
    now = datetime.now(UTC)
    pages = {
        1: page_ok(
            [
                release("v2", now - timedelta(days=400)),
                release("v1", now - timedelta(days=900)),
            ]
        ),
        2: page_ok([release("v0", now - timedelta(days=1500))]),
    }
    source = make_source(backfill_depth_days=0)
    adapter, fetcher = make_adapter(pages, source=source)

    assert len(adapter.backfill()) == 3
    assert fetcher.pages_requested == [1, 2]


def test_walk_stops_at_max_pages(monkeypatch):
    monkeypatch.setattr(GitHubReleasesAdapter, "per_page", 1)
    monkeypatch.setattr(GitHubReleasesAdapter, "max_pages", 3)
    fetcher = FakeFetcher({}, missing=page_ok([release("v", NOW)]))
    adapter = GitHubReleasesAdapter(make_source(), fetcher)

    items = adapter.collect()

    assert len(items) == 3
    assert adapter.extra["truncated_at_max_pages"] is True


# --- drafts and prereleases ----------------------------------------------


def test_drafts_are_skipped():
    pages = {
        1: page_ok(
            [
                release("v3-draft", NOW, draft=True),
                release("v2", NOW - timedelta(days=1)),
            ]
        )
    }
    adapter, _ = make_adapter(pages)

    items = adapter.collect()

    assert [item.version_hint for item in items] == ["v2"]
    assert adapter.extra["drafts_skipped"] == 1


def test_prereleases_are_kept_and_marked():
    pages = {
        1: page_ok(
            [
                release("v2.0.0-rc.1", NOW, prerelease=True),
                release("v1.9.0", NOW - timedelta(days=1)),
            ]
        )
    }
    adapter, _ = make_adapter(pages)

    items = adapter.collect()

    assert [item.version_hint for item in items] == ["v2.0.0-rc.1", "v1.9.0"]
    assert items[0].extra["prerelease"] is True
    assert "prerelease" not in items[1].extra


# --- failure modes -------------------------------------------------------


def test_repo_without_releases_is_empty_not_failed():
    adapter, fetcher = make_adapter({1: page_ok([])})

    assert adapter.collect() == []
    assert adapter.extra["empty"] is True
    assert "error" not in adapter.extra
    assert fetcher.pages_requested == [1]


def test_network_error_returns_empty_list_and_records_the_reason():
    adapter, _ = make_adapter({1: page_error("ConnectTimeout: timed out")})

    assert adapter.collect() == []
    assert adapter.extra["error"] == "ConnectTimeout: timed out"
    assert "empty" not in adapter.extra


def test_http_404_returns_empty_list_and_records_the_status():
    adapter, _ = make_adapter({1: page_status(404, text='{"message": "Not Found"}')})

    assert adapter.collect() == []
    assert adapter.extra["http_status"] == 404
    assert adapter.extra["error"] == "HTTP 404"


def test_malformed_json_does_not_raise():
    adapter, _ = make_adapter({1: {"status": 200, "text": "<html>nope</html>"}})

    assert adapter.collect() == []
    assert "not valid JSON" in adapter.extra["error"]


def test_rate_limit_keeps_what_was_already_collected(small_pages):
    pages = {
        1: page_ok([release("v3", NOW), release("v2", NOW - timedelta(days=1))]),
        2: page_status(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1786998664"},
            text='{"message": "API rate limit exceeded"}',
        ),
        3: page_ok([release("v1", NOW - timedelta(days=2))]),
    }
    adapter, fetcher = make_adapter(pages)

    items = adapter.collect()

    assert [item.version_hint for item in items] == ["v3", "v2"]
    assert fetcher.pages_requested == [1, 2]  # paging stops, nothing is retried
    assert adapter.extra["rate_limited"] is True
    assert "rate limited on page 2" in adapter.extra["error"]


def test_secondary_rate_limit_429_also_stops_paging(small_pages):
    pages = {
        1: page_ok([release("v3", NOW), release("v2", NOW - timedelta(days=1))]),
        2: page_status(429, headers={"Retry-After": "60"}),
    }
    adapter, _ = make_adapter(pages)

    assert len(adapter.collect()) == 2
    assert adapter.extra["rate_limited"] is True


def test_403_that_is_not_a_rate_limit_is_a_plain_failure():
    pages = {1: page_status(403, headers={"X-RateLimit-Remaining": "4321"})}
    adapter, _ = make_adapter(pages)

    assert adapter.collect() == []
    assert "rate_limited" not in adapter.extra
    assert adapter.extra["error"] == "HTTP 403"


# --- token handling ------------------------------------------------------


def test_env_token_travels_in_the_header_only(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    adapter, fetcher = make_adapter({1: page_ok([release("v1", NOW)])})

    adapter.collect()

    headers = fetcher.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer ghp_secret"
    assert headers["Accept"] == "application/vnd.github+json"
    # NFR-11: the source entry is committed to the repo, the token is not.
    source_values = [getattr(adapter.source, field) for field in SourceConfig.__slots__]
    assert "ghp_secret" not in json.dumps(source_values)
    assert not any("token" in field for field in SourceConfig.__slots__)


def test_gh_cli_supplies_the_token_when_the_env_does_not(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="gho_from_cli\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter, fetcher = make_adapter({1: page_ok([release("v1", NOW)])})

    adapter.collect()

    assert calls == [["gh", "auth", "token"]]
    assert fetcher.calls[0]["headers"]["Authorization"] == "Bearer gho_from_cli"


def test_token_is_resolved_once_per_adapter(monkeypatch, small_pages):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="gho_from_cli\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    pages = {
        1: page_ok([release("v3", NOW), release("v2", NOW)]),
        2: page_ok([release("v1", NOW)]),
    }
    adapter, _ = make_adapter(pages)

    adapter.collect()

    assert len(calls) == 1  # one subprocess, not one per page


def test_missing_token_is_logged_and_the_walk_continues(monkeypatch, caplog):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("gh")),
    )
    adapter, fetcher = make_adapter({1: page_ok([release("v1", NOW)])})

    with caplog.at_level(logging.WARNING, logger="radar.adapters.github_releases"):
        items = adapter.collect()

    assert len(items) == 1
    assert "Authorization" not in fetcher.calls[0]["headers"]
    assert "60 requests/hour" in caplog.text


def test_resolve_token_prefers_env_over_gh(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "  ghp_env  ")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: pytest.fail("gh must not be called")
    )

    assert resolve_token() == "ghp_env"


def test_resolve_token_returns_none_when_gh_is_not_logged_in(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="no auth"
        ),
    )

    assert resolve_token() is None
