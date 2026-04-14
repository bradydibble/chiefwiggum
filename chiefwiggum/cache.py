"""
Caching infrastructure for ChiefWiggum TUI performance optimization.

Provides TTL-based caching to avoid redundant subprocess calls, file I/O,
and database queries during UI rendering.
"""

import time
from threading import Lock
from typing import Any, Callable, Dict, Optional


class TTLCache:
    """Thread-safe cache with time-to-live (TTL) expiration."""

    def __init__(self, default_ttl: float = 5.0):
        """
        Initialize TTL cache.

        Args:
            default_ttl: Default time-to-live in seconds
        """
        self.default_ttl = default_ttl
        self._cache: Dict[str, tuple[Any, float]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Optional[Any]:
        """
        Get cached value if not expired.

        Args:
            key: Cache key

        Returns:
            Cached value or None if expired/missing
        """
        with self._lock:
            if key not in self._cache:
                return None

            value, expiry = self._cache[key]
            if time.time() > expiry:
                del self._cache[key]
                return None

            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """
        Set cached value with TTL.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Optional custom TTL (uses default_ttl if None)
        """
        ttl = ttl if ttl is not None else self.default_ttl
        expiry = time.time() + ttl

        with self._lock:
            self._cache[key] = (value, expiry)

    def invalidate(self, key: str) -> None:
        """
        Invalidate a specific cache entry.

        Args:
            key: Cache key to invalidate
        """
        with self._lock:
            self._cache.pop(key, None)

    def invalidate_all(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()

    def invalidate_pattern(self, pattern: str) -> None:
        """
        Invalidate all keys containing pattern.

        Args:
            pattern: String pattern to match in keys
        """
        with self._lock:
            keys_to_delete = [k for k in self._cache.keys() if pattern in k]
            for key in keys_to_delete:
                del self._cache[key]


# Global cache instances
process_health_cache = TTLCache(default_ttl=5.0)  # 5s TTL for process health
error_indicator_cache = TTLCache(default_ttl=5.0)  # 5s TTL for error indicators
progress_data_cache = TTLCache(default_ttl=2.0)   # 2s TTL for progress data
search_results_cache = TTLCache(default_ttl=10.0) # 10s TTL for search results


def cached(cache: TTLCache, key_fn: Callable[..., str], ttl: Optional[float] = None):
    """
    Decorator to cache function results with TTL.

    Args:
        cache: TTLCache instance to use
        key_fn: Function that generates cache key from args/kwargs
        ttl: Optional custom TTL

    Returns:
        Decorated function
    """
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            # Generate cache key
            cache_key = key_fn(*args, **kwargs)

            # Try to get from cache
            cached_value = cache.get(cache_key)
            if cached_value is not None:
                return cached_value

            # Compute and cache
            result = func(*args, **kwargs)
            cache.set(cache_key, result, ttl=ttl)
            return result

        return wrapper
    return decorator
