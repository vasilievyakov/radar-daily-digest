import json
from datetime import date

import pytest

from radar.cache import CacheStore, HttpCache, ModelCache, canonical_url, digest


@pytest.fixture
def store(tmp_path):
    return CacheStore(tmp_path / "cache", "unit")


class TestCanonicalUrl:
    def test_scheme_and_host_are_lowercased(self):
        assert canonical_url("HTTPS://Example.COM/x") == "https://example.com/x"

    def test_path_case_survives(self):
        # Hosts are case-insensitive, paths are not: /Blog and /blog may differ.
        assert canonical_url("https://example.com/Blog/Post") == (
            "https://example.com/Blog/Post"
        )

    def test_www_prefix_is_dropped(self):
        assert canonical_url("https://www.example.com/x") == "https://example.com/x"

    def test_default_ports_are_dropped(self):
        assert canonical_url("https://example.com:443/x") == "https://example.com/x"
        assert canonical_url("http://example.com:80/x") == "http://example.com/x"

    def test_a_non_default_port_is_kept(self):
        assert canonical_url("https://example.com:8443/x") == (
            "https://example.com:8443/x"
        )

    def test_trailing_slash_is_dropped_but_root_keeps_it(self):
        assert canonical_url("https://example.com/a/b/") == "https://example.com/a/b"
        assert canonical_url("https://example.com/") == "https://example.com/"
        assert canonical_url("https://example.com") == "https://example.com/"

    def test_query_is_sorted(self):
        assert canonical_url("https://example.com/x?b=2&a=1") == (
            "https://example.com/x?a=1&b=2"
        )

    def test_tracking_parameters_are_dropped(self):
        url = (
            "https://example.com/x?utm_source=hn&utm_medium=social&fbclid=1&gclid=2"
            "&mc_cid=3&mc_eid=4&ref_src=twsrc&id=7"
        )
        assert canonical_url(url) == "https://example.com/x?id=7"

    def test_fragment_is_dropped(self):
        assert canonical_url("https://example.com/x?a=1#section-2") == (
            "https://example.com/x?a=1"
        )

    def test_surrounding_whitespace_is_ignored(self):
        assert canonical_url("  https://example.com/x  ") == "https://example.com/x"

    def test_two_spellings_of_one_address_share_a_key(self):
        a = " HTTPS://WWW.Example.com:443/Blog/Post/?b=2&a=1&utm_source=hn#top "
        b = "https://example.com/Blog/Post?a=1&b=2"
        assert canonical_url(a) == canonical_url(b)
        assert HttpCache.key_for(a) == HttpCache.key_for(b)

    def test_different_addresses_stay_different(self):
        pairs = [
            ("https://example.com/a", "https://example.com/b"),
            ("https://example.com/a", "https://other.com/a"),
            ("https://example.com/a?x=1", "https://example.com/a?x=2"),
            ("http://example.com/a", "https://example.com/a"),
        ]
        for left, right in pairs:
            assert canonical_url(left) != canonical_url(right)
            assert HttpCache.key_for(left) != HttpCache.key_for(right)

    def test_a_parameter_that_merely_starts_with_ref_is_kept(self):
        assert canonical_url("https://example.com/x?reference=42") == (
            "https://example.com/x?reference=42"
        )
        assert canonical_url("https://example.com/x?reference=42") != canonical_url(
            "https://example.com/x?reference=43"
        )

    def test_a_schemeless_url_is_completed_to_https(self):
        assert canonical_url("example.com/path") == "https://example.com/path"


class TestDigest:
    def test_same_input_gives_the_same_hash(self):
        assert digest("model", "prompt") == digest("model", "prompt")

    def test_argument_order_matters(self):
        assert digest("a", "b") != digest("b", "a")

    def test_argument_boundaries_are_not_blurred(self):
        # The NUL separator is what keeps ("ab", "c") apart from ("a", "bc").
        assert digest("ab", "c") != digest("a", "bc")

    def test_dict_key_order_does_not_matter(self):
        assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})

    def test_dict_values_do_matter(self):
        assert digest({"a": 1}) != digest({"a": 2})

    def test_values_json_cannot_encode_still_hash(self):
        # default=str is the fallback: a date inside params must not break a key.
        assert digest({"since": date(2026, 8, 17)}) == digest(
            {"since": date(2026, 8, 17)}
        )
        assert digest({"since": date(2026, 8, 17)}) != digest(
            {"since": date(2026, 8, 18)}
        )


class TestCacheStore:
    def test_empty_store_misses(self, store):
        assert store.get("deadbeef") is None
        assert store.stats() == {"hits": 0, "misses": 1}

    def test_value_survives_a_put(self, store):
        store.put("deadbeef", {"text": "hello", "status_code": 200})
        payload = store.get("deadbeef")
        assert payload["text"] == "hello"
        assert payload["status_code"] == 200

    def test_put_stamps_the_write_time(self, store):
        store.put("deadbeef", {"text": "hello"})
        assert isinstance(store.get("deadbeef")["cached_at"], float)

    def test_put_returns_the_key(self, store):
        assert store.put("deadbeef", {"text": "hello"}) == "deadbeef"

    def test_hits_and_misses_are_counted_separately(self, store):
        store.get("aa11")
        store.put("aa11", {"text": "x"})
        store.get("aa11")
        store.get("aa11")
        store.get("bb22")
        assert store.stats() == {"hits": 2, "misses": 2}

    def test_a_finished_put_leaves_no_temporary_file(self, store):
        store.put("deadbeef", {"text": "hello"})
        assert list(store.root.rglob("*.tmp")) == []
        assert store.path_for("deadbeef").exists()

    def test_a_truncated_file_reads_as_a_miss(self, store):
        path = store.path_for("deadbeef")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"text": "hel', encoding="utf-8")
        assert store.get("deadbeef") is None
        assert store.stats()["misses"] == 1

    def test_binary_garbage_reads_as_a_miss(self, store):
        path = store.path_for("deadbeef")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe\x00garbage")
        assert store.get("deadbeef") is None

    def test_a_corrupt_entry_can_be_overwritten(self, store):
        path = store.path_for("deadbeef")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        store.put("deadbeef", {"text": "hello"})
        assert store.get("deadbeef")["text"] == "hello"

    def test_entries_are_nested_by_key_prefix(self, store):
        store.put("abcdef", {"text": "x"})
        path = store.path_for("abcdef")
        assert path.parent.name == "ab"
        assert path.name == "abcdef.json"
        assert path.parent.parent == store.root

    def test_namespaces_do_not_collide(self, tmp_path):
        http = CacheStore(tmp_path / "cache", "http")
        model = CacheStore(tmp_path / "cache", "model")
        http.put("aa11", {"text": "from http"})
        assert model.get("aa11") is None

    def test_unicode_is_stored_readable(self, store):
        store.put("aa11", {"text": "Anthropic отключает claude-3-opus"})
        raw = store.path_for("aa11").read_text(encoding="utf-8")
        assert "отключает" in raw
        assert json.loads(raw)["text"] == "Anthropic отключает claude-3-opus"


class TestModelCacheKeys:
    def test_same_input_gives_the_same_key(self):
        a = ModelCache.key_for("sonnet", "extract", {"type": "object"}, {"temp": 0})
        b = ModelCache.key_for("sonnet", "extract", {"type": "object"}, {"temp": 0})
        assert a == b

    def test_a_different_model_gives_a_different_key(self):
        assert ModelCache.key_for("sonnet", "p") != ModelCache.key_for("haiku", "p")

    def test_a_different_prompt_gives_a_different_key(self):
        assert ModelCache.key_for("m", "p1") != ModelCache.key_for("m", "p2")

    def test_a_different_schema_gives_a_different_key(self):
        assert ModelCache.key_for("m", "p", {"a": 1}) != ModelCache.key_for(
            "m", "p", {"a": 2}
        )

    def test_different_params_give_a_different_key(self):
        assert ModelCache.key_for("m", "p", None, {"temp": 0}) != ModelCache.key_for(
            "m", "p", None, {"temp": 1}
        )

    def test_param_order_does_not_change_the_key(self):
        assert ModelCache.key_for(
            "m", "p", None, {"temp": 0, "top_p": 1}
        ) == ModelCache.key_for("m", "p", None, {"top_p": 1, "temp": 0})

    def test_the_key_is_usable_as_a_store_key(self, tmp_path):
        cache = ModelCache(tmp_path / "cache")
        key = ModelCache.key_for("sonnet", "extract")
        cache.put(key, {"response": "{}"})
        assert cache.get(key)["response"] == "{}"


class TestWriteIsActuallyAtomic:
    """The existing test asserted "no .tmp remains after put", which is equally
    true when there is no temporary file at all. Removing atomicity entirely
    left the whole suite green. These assert the property, not its residue.
    """

    def test_a_crash_mid_write_leaves_no_partial_entry(self, tmp_path):
        """A reader must see the old value or the new one, never half of one.

        A truncated cache entry is worse than a miss: `raw_material_ref` points
        at the archived text that evidence is verified against, and half a
        document silently fails quotes that are genuinely in the source.
        """
        from radar.cache import CacheStore

        store = CacheStore(tmp_path, "http")
        store.put("k" * 64, {"text": "полный документ"})

        original = __import__("pathlib").Path.write_text

        def explode(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise OSError("диск кончился на середине")

        __import__("pathlib").Path.write_text = explode
        try:
            with __import__("pytest").raises(OSError):
                store.put("k" * 64, {"text": "новый документ, записанный наполовину"})
        finally:
            __import__("pathlib").Path.write_text = original

        # The previous value survived intact.
        assert store.get("k" * 64)["text"] == "полный документ"

    def test_the_write_goes_through_a_separate_file_first(self, tmp_path, monkeypatch):
        """Directly asserts the mechanism: the final path is never the one
        being written to."""
        from pathlib import Path

        from radar.cache import CacheStore

        store = CacheStore(tmp_path, "http")
        final = store.path_for("a" * 64)
        written_to: list[Path] = []
        original = Path.write_text

        def record(self, *args, **kwargs):
            written_to.append(Path(self))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", record)
        store.put("a" * 64, {"text": "x"})

        assert written_to, "put did not write anything"
        assert final not in written_to, "final path was written to directly"
        assert final.exists()
