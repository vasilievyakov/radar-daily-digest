import httpx
import pytest

from radar import fetch as fetch_module
from radar.fetch import Fetcher


class FakeClock:
    """Stands in for radar.fetch.time: backoff and polite delays cost no wall time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def responder(*steps):
    """MockTransport handler: one step per attempt, the last step repeats.

    A step is either (status_code, text) or an exception instance to raise.
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        step = steps[min(len(calls) - 1, len(steps) - 1)]
        if isinstance(step, Exception):
            raise step
        status, text = step
        return httpx.Response(status, text=text, headers={"x-source": "mock"})

    handler.calls = calls
    return handler


def forbidden(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected network call to {request.url}")


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(fetch_module, "time", fake)
    return fake


@pytest.fixture
def make_fetcher(tmp_path, clock):
    created: list[Fetcher] = []

    def factory(handler, polite_delay=0.0, max_retries=2, cache_root=None):
        fetcher = Fetcher(
            cache_root=cache_root or tmp_path / "cache",
            max_retries=max_retries,
            polite_delay=polite_delay,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        created.append(fetcher)
        return fetcher

    yield factory
    for fetcher in created:
        fetcher.close()


class TestCaching:
    def test_a_first_fetch_reaches_the_network(self, make_fetcher):
        handler = responder((200, "<html>hello</html>"))
        result = make_fetcher(handler).get("https://example.test/a")
        assert result.ok
        assert result.status_code == 200
        assert result.text == "<html>hello</html>"
        assert result.from_cache is False
        assert len(handler.calls) == 1

    def test_a_second_fetch_is_served_from_disk(self, make_fetcher):
        handler = responder((200, "<html>hello</html>"))
        fetcher = make_fetcher(handler)
        first = fetcher.get("https://example.test/a")
        second = fetcher.get("https://example.test/a")
        assert len(handler.calls) == 1
        assert second.from_cache is True
        assert second.text == first.text
        assert second.ref == first.ref

    def test_a_cached_result_keeps_status_and_headers(self, make_fetcher):
        fetcher = make_fetcher(responder((200, "body")))
        fetcher.get("https://example.test/a")
        cached = fetcher.get("https://example.test/a")
        assert cached.status_code == 200
        assert cached.headers["x-source"] == "mock"

    def test_the_cache_outlives_the_fetcher(self, make_fetcher, tmp_path):
        root = tmp_path / "shared"
        ref = (
            make_fetcher(responder((200, "archived")), cache_root=root)
            .get("https://example.test/a")
            .ref
        )
        # A later run opens the same root and must not need the network again.
        reopened = make_fetcher(forbidden, cache_root=root)
        assert reopened.get("https://example.test/a").from_cache is True
        assert reopened.read_cached(ref) == "archived"

    def test_two_spellings_of_one_url_are_fetched_once(self, make_fetcher):
        handler = responder((200, "body"))
        fetcher = make_fetcher(handler)
        fetcher.get("https://www.example.test/a/?utm_source=hn")
        second = fetcher.get("https://example.test/a")
        assert len(handler.calls) == 1
        assert second.from_cache is True

    def test_force_bypasses_the_cache(self, make_fetcher):
        handler = responder((200, "old"), (200, "new"))
        fetcher = make_fetcher(handler)
        fetcher.get("https://example.test/a")
        refetched = fetcher.get("https://example.test/a", force=True)
        assert len(handler.calls) == 2
        assert refetched.from_cache is False
        assert refetched.text == "new"

    def test_force_overwrites_what_is_stored(self, make_fetcher):
        handler = responder((200, "old"), (200, "new"))
        fetcher = make_fetcher(handler)
        fetcher.get("https://example.test/a")
        fetcher.get("https://example.test/a", force=True)
        assert fetcher.get("https://example.test/a").text == "new"

    def test_cache_key_extra_separates_entries(self, make_fetcher):
        handler = responder((200, "page one"), (200, "page two"))
        fetcher = make_fetcher(handler)
        first = fetcher.get("https://example.test/list", cache_key_extra="page=1")
        second = fetcher.get("https://example.test/list", cache_key_extra="page=2")
        assert first.ref != second.ref
        assert second.text == "page two"


class TestRetries:
    def test_a_server_error_is_retried_and_then_returned(self, make_fetcher):
        handler = responder((500, "boom"))
        result = make_fetcher(handler, max_retries=2).get("https://example.test/a")
        assert len(handler.calls) == 3  # first attempt plus two retries
        assert result.status_code == 500
        # An exhausted 5xx is a failure the collector must be able to see.
        assert result.error == "HTTP 500"
        assert result.ok is False
        assert result.ok is False

    def test_backoff_grows_between_attempts(self, make_fetcher, clock):
        make_fetcher(responder((503, "boom")), max_retries=2).get(
            "https://example.test/a"
        )
        assert clock.slept == [1, 2]

    def test_a_recovered_server_stops_the_retries(self, make_fetcher):
        handler = responder((502, "boom"), (200, "recovered"))
        result = make_fetcher(handler).get("https://example.test/a")
        assert len(handler.calls) == 2
        assert result.ok
        assert result.text == "recovered"

    def test_a_client_error_is_not_retried(self, make_fetcher):
        handler = responder((404, "not found"))
        result = make_fetcher(handler).get("https://example.test/gone")
        assert len(handler.calls) == 1
        assert result.status_code == 404
        assert result.error is None
        assert result.ok is False

    def test_a_network_error_becomes_a_result(self, make_fetcher):
        handler = responder(httpx.ConnectError("name resolution failed"))
        result = make_fetcher(handler).get("https://example.test/a")
        assert result.error is not None
        assert result.error.startswith("ConnectError")
        assert result.status_code == 0
        assert result.text == ""
        assert result.ok is False

    def test_a_network_error_is_retried_too(self, make_fetcher):
        handler = responder(httpx.ReadTimeout("timed out"))
        make_fetcher(handler, max_retries=2).get("https://example.test/a")
        assert len(handler.calls) == 3

    def test_a_timeout_recovers_on_a_later_attempt(self, make_fetcher):
        handler = responder(httpx.ConnectTimeout("slow"), (200, "recovered"))
        result = make_fetcher(handler).get("https://example.test/a")
        assert result.ok
        assert result.text == "recovered"

    def test_a_failed_fetch_still_carries_a_canonical_url(self, make_fetcher):
        handler = responder(httpx.ConnectError("down"))
        result = make_fetcher(handler).get("https://WWW.Example.test/a/")
        assert result.url == "https://example.test/a"

    def test_a_server_error_is_not_archived(self, make_fetcher):
        fetcher = make_fetcher(responder((500, "boom")))
        result = fetcher.get("https://example.test/a")
        assert fetcher.cache.get(result.ref) is None


class TestArchivedMaterial:
    def test_read_cached_returns_the_text_behind_a_ref(self, make_fetcher):
        page = "Anthropic will retire claude-3-opus on October 15, 2026."
        fetcher = make_fetcher(responder((200, page)))
        result = fetcher.get("https://example.test/deprecations")
        # This is the text a quote is later verified against.
        assert fetcher.read_cached(result.ref) == page
        assert "retire claude-3-opus" in fetcher.read_cached(result.ref)

    def test_read_cached_is_none_for_an_unknown_ref(self, make_fetcher):
        fetcher = make_fetcher(forbidden)
        assert fetcher.read_cached("0" * 64) is None

    def test_a_ref_from_a_cache_hit_points_at_the_same_material(self, make_fetcher):
        fetcher = make_fetcher(responder((200, "material")))
        first = fetcher.get("https://example.test/a")
        second = fetcher.get("https://example.test/a")
        assert fetcher.read_cached(second.ref) == fetcher.read_cached(first.ref)


class TestPoliteDelay:
    def test_a_second_request_to_one_domain_waits(self, make_fetcher, clock):
        fetcher = make_fetcher(responder((200, "body")), polite_delay=1.5)
        fetcher.get("https://example.test/a")
        fetcher.get("https://example.test/b")
        assert clock.slept == [1.5]

    def test_another_domain_is_not_delayed(self, make_fetcher, clock):
        fetcher = make_fetcher(responder((200, "body")), polite_delay=1.5)
        fetcher.get("https://example.test/a")
        fetcher.get("https://other.test/a")
        assert clock.slept == []

    def test_only_the_remaining_interval_is_waited_out(self, make_fetcher, clock):
        fetcher = make_fetcher(responder((200, "body")), polite_delay=2.0)
        fetcher.get("https://example.test/a")
        clock.advance(1.5)
        fetcher.get("https://example.test/b")
        assert clock.slept == [pytest.approx(0.5)]

    def test_a_long_gap_needs_no_wait(self, make_fetcher, clock):
        fetcher = make_fetcher(responder((200, "body")), polite_delay=1.0)
        fetcher.get("https://example.test/a")
        clock.advance(60)
        fetcher.get("https://example.test/b")
        assert clock.slept == []

    def test_a_cache_hit_costs_no_delay(self, make_fetcher, clock):
        fetcher = make_fetcher(responder((200, "body")), polite_delay=5.0)
        fetcher.get("https://example.test/a")
        fetcher.get("https://example.test/a")
        assert clock.slept == []
