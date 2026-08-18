import httpx
import pytest

from radar import fetch as fetch_module
from radar.fetch import DEFAULT_CACHE_TTL, FetchOrigin, FetchResult, FetchTally, Fetcher


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

    def factory(
        handler,
        polite_delay=0.0,
        max_retries=2,
        cache_root=None,
        cache_ttl=DEFAULT_CACHE_TTL,
        offline=False,
    ):
        fetcher = Fetcher(
            cache_root=cache_root or tmp_path / "cache",
            max_retries=max_retries,
            polite_delay=polite_delay,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            offline=offline,
            cache_ttl=cache_ttl,
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
        assert result.ok is False
        # Carries a reason now. Without one a moved page reached the collector
        # as an answer with nothing in it, and the run log filed it next to the
        # repositories that simply had no release today.
        assert result.error == "HTTP 404"

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


class TestAMissingPageIsNotArchived:
    """A 404 is stable, and that is exactly what makes it dangerous to keep.

    Azure moved its model retirement schedule. The old address answered 404,
    the fetcher stored it like any non-5xx answer, and from then on the source
    reported a one-millisecond response carrying nothing — which on the run-log
    page is indistinguishable from a source that is quiet today. The page
    behind that URL had been alive again for weeks under a new address.
    """

    def test_a_404_is_not_written_to_the_cache(self, tmp_path):
        # First attempt 404, every attempt after it 200: the last step repeats.
        handler = responder((404, "gone"), (200, "<html>снова здесь</html>"))
        fetcher = Fetcher(
            cache_root=tmp_path / "cache",
            polite_delay=0.0,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        first = fetcher.get("https://example.test/moved")
        assert not first.ok
        assert first.status_code == 404

        # The page comes back at the same address: a stored 404 would hide it.
        second = fetcher.get("https://example.test/moved")

        assert second.ok, "the 404 was served from disk"
        assert "снова здесь" in second.text
        assert not second.from_cache


# -- conditional requests ---------------------------------------------------


class FakeServer:
    """A page that answers a validator, and can publish a new version.

    The point of the class is that it distinguishes the two questions the old
    fetcher collapsed into one: "do I have bytes for this URL" and "are the
    bytes I have still what the page says". Only the second one is a check.
    """

    def __init__(self, body="v1", etag='W/"1"', last_modified=None, extra=None):
        self.body = body
        self.etag = etag
        self.last_modified = last_modified
        self.extra = dict(extra or {})
        self.requests: list[httpx.Request] = []

    @property
    def conditional_requests(self) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if "if-none-match" in r.headers or "if-modified-since" in r.headers
        ]

    def publish(self, body, etag=None, last_modified=None) -> None:
        self.body = body
        if etag is not None:
            self.etag = etag
        if last_modified is not None:
            self.last_modified = last_modified

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self.etag:
            headers["etag"] = self.etag
        if self.last_modified:
            headers["last-modified"] = self.last_modified
        headers.update(self.extra)
        return headers

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        matched = self.etag and request.headers.get("if-none-match") == self.etag
        dated = (
            self.last_modified
            and request.headers.get("if-modified-since") == self.last_modified
        )
        if matched or dated:
            return httpx.Response(304, headers=self._headers())
        return httpx.Response(200, text=self.body, headers=self._headers())


class TestTheArchiveDoesNotImpersonateTheNetwork:
    """The defect this module was rewritten for.

    Eight sources answered in under six milliseconds, two hundred and thirty
    files sat on disk and not one had been written that day. Nothing in the
    result said so, so the run log recorded eight checks that never happened
    and the digest was empty for a reason no page could state.
    """

    def test_a_fresh_download_says_it_came_from_the_network(self, make_fetcher):
        server = FakeServer(body="hello")
        result = make_fetcher(server).get("https://example.test/a")
        assert result.origin is FetchOrigin.NETWORK
        assert result.requested is True
        assert result.from_cache is False
        assert result.network_status == 200

    def test_inside_the_ttl_the_archive_answers_and_admits_it(self, make_fetcher):
        server = FakeServer()
        fetcher = make_fetcher(server)
        fetcher.get("https://example.test/a")
        second = fetcher.get("https://example.test/a")
        assert len(server.requests) == 1
        assert second.origin is FetchOrigin.ARCHIVE
        # The old code returned exactly this result and called it a check.
        assert second.requested is False

    def test_past_the_ttl_the_server_is_asked(self, make_fetcher):
        server = FakeServer()
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")
        fetcher.get("https://example.test/a")
        assert len(server.requests) == 2

    def test_the_second_request_carries_the_stored_etag(self, make_fetcher):
        server = FakeServer(etag='W/"abc"')
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")
        fetcher.get("https://example.test/a")
        assert server.requests[0].headers.get("if-none-match") is None
        assert server.requests[1].headers["if-none-match"] == 'W/"abc"'

    def test_a_page_with_only_a_date_sends_if_modified_since(self, make_fetcher):
        stamp = "Mon, 17 Aug 2026 20:03:01 GMT"
        server = FakeServer(etag=None, last_modified=stamp)
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")
        fetcher.get("https://example.test/a")
        assert server.requests[1].headers["if-modified-since"] == stamp

    def test_a_page_with_no_validators_is_downloaded_again(self, make_fetcher):
        """No ETag, no Last-Modified: the only honest check is a full read."""
        server = FakeServer(etag=None)
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")
        second = fetcher.get("https://example.test/a")
        assert server.conditional_requests == []
        assert second.origin is FetchOrigin.NETWORK

    def test_force_asks_for_the_whole_page(self, make_fetcher):
        server = FakeServer()
        fetcher = make_fetcher(server)
        fetcher.get("https://example.test/a")
        result = fetcher.get("https://example.test/a", force=True)
        assert server.conditional_requests == []
        assert result.origin is FetchOrigin.NETWORK

    def test_max_age_zero_overrides_the_ttl_for_one_call(self, make_fetcher):
        server = FakeServer()
        fetcher = make_fetcher(server, cache_ttl=3600)
        fetcher.get("https://example.test/a")
        result = fetcher.get("https://example.test/a", max_age=0)
        assert len(server.requests) == 2
        assert result.origin is FetchOrigin.REVALIDATED


class TestNotModifiedIsAnAnswer:
    """304 is the sentence the product needs and could not previously say.

    "Nothing changed" and "nobody looked" render identically on a page and
    differ completely in what they mean. A 304 is the first one, stated by the
    server, and it has to survive all the way into the run log.
    """

    def test_a_304_is_reported_as_a_network_answer(self, make_fetcher):
        server = FakeServer(body="page")
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")
        second = fetcher.get("https://example.test/a")
        assert second.origin is FetchOrigin.REVALIDATED
        assert second.requested is True
        assert second.unchanged is True
        assert second.network_status == 304

    def test_a_304_hands_the_adapter_the_archived_page(self, make_fetcher):
        """An adapter must never see 304: it parses bodies, not statuses."""
        server = FakeServer(body="<html>page</html>")
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")
        second = fetcher.get("https://example.test/a")
        assert second.ok is True
        assert second.status_code == 200
        assert second.text == "<html>page</html>"
        assert second.from_cache is True

    def test_a_304_keeps_the_material_reference(self, make_fetcher):
        server = FakeServer(body="quotable line")
        fetcher = make_fetcher(server, cache_ttl=0)
        first = fetcher.get("https://example.test/a")
        second = fetcher.get("https://example.test/a")
        assert second.ref == first.ref
        assert fetcher.read_cached(second.ref) == "quotable line"

    def test_a_304_restarts_the_clock(self, make_fetcher):
        """Confirmed current means current: the next read may skip the wire."""
        server = FakeServer()
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")
        fetcher.get("https://example.test/a")
        fetcher.cache_ttl = 3600
        third = fetcher.get("https://example.test/a")
        assert third.origin is FetchOrigin.ARCHIVE
        assert len(server.requests) == 2

    def test_fresh_headers_from_a_304_win(self, make_fetcher):
        """GitHub answers a conditional read with current rate-limit counters."""
        server = FakeServer(extra={"x-ratelimit-remaining": "4999"})
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://api.example.test/releases")
        server.extra["x-ratelimit-remaining"] = "12"
        second = fetcher.get("https://api.example.test/releases")
        assert second.headers["x-ratelimit-remaining"] == "12"

    def test_a_rotated_etag_is_stored(self, make_fetcher):
        server = FakeServer(etag='W/"1"')
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")
        server.extra = {}
        server.etag = 'W/"1"'
        fetcher.get("https://example.test/a")  # 304, same validator
        server.publish("v1", etag='W/"2"')  # the page is re-issued unchanged
        fetcher.get("https://example.test/a")  # 200, new validator
        fetcher.get("https://example.test/a")
        assert server.requests[-1].headers["if-none-match"] == 'W/"2"'

    def test_an_unsolicited_304_is_an_error_not_an_empty_page(self, make_fetcher):
        """No archive to serve, so there is nothing truthful to hand back."""

        def always_304(request: httpx.Request) -> httpx.Response:
            return httpx.Response(304)

        result = make_fetcher(always_304).get("https://example.test/a")
        assert result.ok is False
        assert result.error is not None


class TestAChangedPageIsSeen:
    """The property the product is sold on, asserted directly."""

    def test_a_new_body_arrives_as_a_fresh_download(self, make_fetcher):
        server = FakeServer(body="old", etag='W/"1"')
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")
        server.publish("new", etag='W/"2"')
        second = fetcher.get("https://example.test/a")
        assert second.origin is FetchOrigin.NETWORK
        assert second.text == "new"

    def test_the_archive_is_updated_to_the_new_body(self, make_fetcher):
        server = FakeServer(body="old", etag='W/"1"')
        fetcher = make_fetcher(server, cache_ttl=0)
        ref = fetcher.get("https://example.test/a").ref
        server.publish("new", etag='W/"2"')
        fetcher.get("https://example.test/a")
        assert fetcher.read_cached(ref) == "new"


class TestFailureIsNotPapersOverWithTheArchive:
    def test_an_unreachable_server_does_not_fall_back_to_disk(self, make_fetcher):
        """A source that did not answer is a source that did not answer."""
        server = FakeServer(body="yesterday")
        fetcher = make_fetcher(server, cache_ttl=0)
        fetcher.get("https://example.test/a")

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        fetcher._client = httpx.Client(transport=httpx.MockTransport(down))
        result = fetcher.get("https://example.test/a")
        assert result.ok is False
        assert result.text == ""
        assert result.from_cache is False


class TestOfflineIsUnchanged:
    def test_offline_serves_the_archive_however_stale(self, make_fetcher, tmp_path):
        root = tmp_path / "shared"
        make_fetcher(FakeServer(body="archived"), cache_root=root).get(
            "https://example.test/a"
        )
        offline = make_fetcher(forbidden, cache_root=root, offline=True, cache_ttl=0)
        result = offline.get("https://example.test/a")
        assert result.text == "archived"
        assert result.origin is FetchOrigin.ARCHIVE
        assert result.requested is False

    def test_an_offline_miss_stays_loud(self, make_fetcher):
        result = make_fetcher(forbidden, offline=True).get("https://example.test/gone")
        assert result.ok is False
        assert result.origin is FetchOrigin.OFFLINE
        assert "offline" in (result.error or "")


class TestWhatTheRunLogIsToldAboutTheNetwork:
    def test_record_counts_the_three_outcomes(self, make_fetcher):
        server = FakeServer()
        fetcher = make_fetcher(server, cache_ttl=0)
        with fetcher.record() as tally:
            fetcher.get("https://example.test/a")  # 200
            fetcher.get("https://example.test/a")  # 304
            fetcher.cache_ttl = 3600
            fetcher.get("https://example.test/a")  # archive
        assert (tally.fresh, tally.revalidated, tally.archive) == (1, 1, 1)
        assert tally.requests == 2

    def test_a_block_that_only_read_the_disk_reports_no_requests(self, make_fetcher):
        fetcher = make_fetcher(FakeServer())
        fetcher.get("https://example.test/a")
        with fetcher.record() as tally:
            fetcher.get("https://example.test/a")
        assert tally.requests == 0
        assert tally.label == "archive"

    def test_one_download_makes_the_whole_group_checked(self):
        tally = FetchTally(fresh=1, revalidated=2, archive=5)
        assert tally.label == "fresh"

    def test_all_304_is_reported_as_confirmed_unchanged(self):
        assert FetchTally(revalidated=3, archive=1).label == "revalidated"

    def test_an_empty_group_claims_nothing(self):
        assert FetchTally().label == ""

    def test_a_failed_request_outranks_a_disk_read(self):
        assert FetchTally(failed=1, archive=2).label == "failed"

    def test_recording_is_per_thread(self, make_fetcher):
        """collect_all runs a dozen sources through one fetcher in a pool."""
        import threading

        fetcher = make_fetcher(FakeServer(), cache_ttl=0)
        seen = {}

        def worker(name, url):
            with fetcher.record() as tally:
                fetcher.get(url)
            seen[name] = tally.requests

        threads = [
            threading.Thread(target=worker, args=(i, f"https://example.test/{i}"))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert seen == {0: 1, 1: 1, 2: 1, 3: 1}


class TestTheTwoFlagsCannotDisagree:
    def test_from_cache_alone_still_means_the_archive(self):
        """Adapters and their fixtures were written before `origin` existed."""
        result = FetchResult(
            url="u", status_code=200, text="t", headers={}, ref="r", from_cache=True
        )
        assert result.origin is FetchOrigin.ARCHIVE
        assert result.requested is False

    def test_a_revalidated_result_reads_as_cached(self):
        result = FetchResult(
            url="u",
            status_code=200,
            text="t",
            headers={},
            ref="r",
            origin=FetchOrigin.REVALIDATED,
        )
        assert result.from_cache is True
        assert result.requested is True
