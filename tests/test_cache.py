"""Tests for ChiefWiggum TTLCache."""

import threading
import time as time_module

from chiefwiggum.cache import TTLCache


def test_ttl_expiry(monkeypatch):
    cache = TTLCache(default_ttl=5.0)

    frozen = time_module.time()
    monkeypatch.setattr(time_module, "time", lambda: frozen)
    cache.set("key", "value")

    # Advance past TTL
    monkeypatch.setattr(time_module, "time", lambda: frozen + 10.0)
    assert cache.get("key") is None


def test_get_returns_value_before_expiry(monkeypatch):
    cache = TTLCache(default_ttl=5.0)

    frozen = time_module.time()
    monkeypatch.setattr(time_module, "time", lambda: frozen)
    cache.set("key", "value")

    # Advance within TTL
    monkeypatch.setattr(time_module, "time", lambda: frozen + 2.0)
    assert cache.get("key") == "value"


def test_get_missing_key_returns_none():
    cache = TTLCache()
    assert cache.get("no_such_key") is None


def test_invalidate_removes_entry():
    cache = TTLCache(default_ttl=60.0)
    cache.set("key", "value")
    assert cache.get("key") == "value"

    cache.invalidate("key")
    assert cache.get("key") is None


def test_invalidate_nonexistent_key_no_error():
    cache = TTLCache()
    cache.invalidate("missing")  # Should not raise


def test_invalidate_all_clears_cache():
    cache = TTLCache(default_ttl=60.0)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.invalidate_all()
    assert cache.get("a") is None
    assert cache.get("b") is None


def test_invalidate_pattern():
    cache = TTLCache(default_ttl=60.0)
    cache.set("task:1", "v1")
    cache.set("task:2", "v2")
    cache.set("instance:1", "v3")

    cache.invalidate_pattern("task:")

    assert cache.get("task:1") is None
    assert cache.get("task:2") is None
    assert cache.get("instance:1") == "v3"


def test_invalidate_pattern_no_match_leaves_entries():
    cache = TTLCache(default_ttl=60.0)
    cache.set("alpha", "x")
    cache.set("beta", "y")

    cache.invalidate_pattern("gamma")

    assert cache.get("alpha") == "x"
    assert cache.get("beta") == "y"


def test_set_with_custom_ttl(monkeypatch):
    cache = TTLCache(default_ttl=5.0)

    frozen = time_module.time()
    monkeypatch.setattr(time_module, "time", lambda: frozen)
    cache.set("key", "value", ttl=1.0)

    # Within custom TTL
    monkeypatch.setattr(time_module, "time", lambda: frozen + 0.5)
    assert cache.get("key") == "value"

    # Past custom TTL
    monkeypatch.setattr(time_module, "time", lambda: frozen + 2.0)
    assert cache.get("key") is None


def test_thread_safe_concurrent_access():
    cache = TTLCache(default_ttl=60.0)
    errors = []

    def worker(thread_id):
        try:
            for i in range(20):
                key = f"key-{thread_id}-{i}"
                cache.set(key, thread_id * 100 + i)
                val = cache.get(key)
                # Value may have been expired or overwritten but should not raise
                assert val is None or isinstance(val, int)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"
