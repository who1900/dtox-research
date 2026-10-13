"""Bounded synchronous runtime guard for an optional passage reranker.

Scores are relative relevance signals only; they are not probabilities or
evidence. The request callback owns transport details. Its timeout argument is
passed through as a client timeout, while elapsed time is checked on return.
"""

from collections import OrderedDict
import hashlib
import math
import numbers
import threading
import time


MAX_QUERY_CHARS = 4096
MAX_PASSAGE_CHARS = 1200


class RerankRuntime:
    def __init__(self, max_pairs=8, timeout=3, cooldown=60, cache_size=128,
                 cache_ttl=300, clock=None):
        if not isinstance(max_pairs, int) or isinstance(max_pairs, bool) or max_pairs < 1:
            raise ValueError("max_pairs must be a positive integer")
        for name, value in (("timeout", timeout), ("cooldown", cooldown),
                            ("cache_ttl", cache_ttl)):
            try:
                finite_value = (float(value) if isinstance(value, numbers.Real)
                                and not isinstance(value, bool) else math.nan)
            except (OverflowError, TypeError, ValueError):
                finite_value = math.nan
            if not math.isfinite(finite_value) or finite_value <= 0:
                raise ValueError(name + " must be finite and positive")
        if not isinstance(cache_size, int) or isinstance(cache_size, bool) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")
        self.max_pairs = max_pairs
        self.timeout = float(timeout)
        self.cooldown = float(cooldown)
        self.cache_size = cache_size
        self.cache_ttl = float(cache_ttl)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._admission = threading.BoundedSemaphore(1)
        self._cache = OrderedDict()
        self._breaker_until = 0.0
        self._counters = {
            "requests": 0, "successes": 0, "cache_hits": 0,
            "busy_rejections": 0, "cooldown_rejections": 0,
            "input_rejections": 0, "request_failures": 0,
            "invalid_results": 0, "elapsed_rejections": 0,
        }
        self._last_status = "idle"
        self._last_reason = "not_called"

    @staticmethod
    def _cache_key(query, passages):
        digest = hashlib.sha256()
        for value in (query, *passages):
            encoded = value.encode("utf-8", errors="surrogatepass")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.digest()

    def _record(self, status, reason, counter=None):
        if counter:
            self._counters[counter] += 1
        self._last_status = status
        self._last_reason = reason

    def _failure(self, reason, counter, now=None):
        self._record("fallback", reason, counter)
        self._breaker_until = (self._clock() if now is None else now) + self.cooldown

    def score(self, query, passages, request):
        """Return one finite relevance score per passage, or None on fallback."""
        if not isinstance(query, str) or not query or len(query) > MAX_QUERY_CHARS:
            with self._lock:
                self._record("fallback", "invalid_input", "input_rejections")
            return None
        if not isinstance(passages, (list, tuple)) or not passages:
            with self._lock:
                self._record("fallback", "empty", "input_rejections")
            return None
        if (len(passages) > self.max_pairs
                or any(not isinstance(item, str) or not item
                       or len(item) > MAX_PASSAGE_CHARS for item in passages)
                or not callable(request)):
            with self._lock:
                reason = "too_many_pairs" if len(passages) > self.max_pairs else "invalid_input"
                self._record("fallback", reason, "input_rejections")
            return None

        safe_passages = tuple(passages)
        key = self._cache_key(query, safe_passages)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                expires, values = cached
                if now < expires:
                    self._cache.move_to_end(key)
                    self._counters["cache_hits"] += 1
                    self._last_status, self._last_reason = "ok", "cache_hit"
                    return list(values)
                del self._cache[key]
            if now < self._breaker_until:
                self._record("fallback", "cooldown", "cooldown_rejections")
                return None
            if not self._admission.acquire(blocking=False):
                self._record("fallback", "busy", "busy_rejections")
                return None
            self._counters["requests"] += 1

        started = self._clock()
        try:
            try:
                raw_scores = request(query, list(safe_passages), self.timeout)
            except Exception:
                with self._lock:
                    self._failure("request_failure", "request_failures")
                return None

            finished = self._clock()
            if finished - started > self.timeout:
                with self._lock:
                    self._failure("elapsed_timeout", "elapsed_rejections", finished)
                return None
            if not isinstance(raw_scores, (list, tuple)) or len(raw_scores) != len(safe_passages):
                with self._lock:
                    self._failure("invalid_scores", "invalid_results", finished)
                return None
            scores = []
            for value in raw_scores:
                try:
                    score = (float(value) if isinstance(value, numbers.Real)
                             and not isinstance(value, bool) else math.nan)
                except (OverflowError, TypeError, ValueError):
                    score = math.nan
                if not math.isfinite(score):
                    with self._lock:
                        self._failure("invalid_scores", "invalid_results", finished)
                    return None
                scores.append(score)

            with self._lock:
                self._counters["successes"] += 1
                self._last_status, self._last_reason = "ok", "scored"
                if self.cache_size:
                    self._cache[key] = (finished + self.cache_ttl, tuple(scores))
                    self._cache.move_to_end(key)
                    while len(self._cache) > self.cache_size:
                        self._cache.popitem(last=False)
            return list(scores)
        finally:
            self._admission.release()

    def snapshot(self):
        """Return counters and nonsensitive state; never includes request data."""
        with self._lock:
            now = self._clock()
            return {
                "status": "open" if now < self._breaker_until else "closed",
                "cooldown_remaining": max(0.0, self._breaker_until - now),
                "cache_entries": len(self._cache),
                "counters": dict(self._counters),
                "last_status": self._last_status,
                "last_reason": self._last_reason,
            }
