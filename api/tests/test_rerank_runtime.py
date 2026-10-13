import math
import threading
import unittest

from api.rerank_runtime import RerankRuntime


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class RerankRuntimeTests(unittest.TestCase):
    def test_busy_call_returns_immediately_and_cache_is_available_while_busy(self):
        entered = threading.Event()
        release = threading.Event()
        runtime = RerankRuntime()
        calls = []

        def request(query, passages, timeout):
            calls.append(query)
            if query == "cold":
                entered.set()
                release.wait(2)
            return [0.0] * len(passages)

        self.assertEqual(runtime.score("cached", ["p"], request), [0.0])
        worker = threading.Thread(target=lambda: runtime.score("cold", ["p"], request))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertIsNone(runtime.score("busy", ["p"], request))
        self.assertEqual(runtime.score("cached", ["p"], request), [0.0])
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, ["cached", "cold"])
        self.assertEqual(runtime.snapshot()["counters"]["busy_rejections"], 1)

    def test_request_failure_opens_breaker_without_retry_or_sensitive_metadata(self):
        clock = FakeClock()
        runtime = RerankRuntime(cooldown=60, clock=clock)
        calls = []

        def fail(query, passages, timeout):
            calls.append(query)
            raise RuntimeError("private passage text")

        self.assertIsNone(runtime.score("secret query", ["private passage"], fail))
        self.assertIsNone(runtime.score("other", ["other passage"], fail))
        snapshot = runtime.snapshot()
        self.assertEqual(calls, ["secret query"])
        self.assertEqual(snapshot["last_reason"], "cooldown")
        self.assertEqual(snapshot["counters"]["request_failures"], 1)
        self.assertEqual(snapshot["counters"]["cooldown_rejections"], 1)
        self.assertNotIn("private", repr(snapshot))
        clock.now += 61
        self.assertEqual(runtime.score("other", ["other passage"], lambda *_: [-2]), [-2.0])

    def test_ttl_lru_and_defensive_cache_copies(self):
        clock = FakeClock()
        runtime = RerankRuntime(cache_size=2, cache_ttl=5, clock=clock)
        calls = []

        def score(query, passages, timeout):
            calls.append(query)
            return [len(calls)]

        result = runtime.score("a", ["p"], score)
        result[0] = 999
        self.assertEqual(runtime.score("a", ["p"], score), [1.0])
        runtime.score("b", ["p"], score)
        runtime.score("a", ["p"], score)  # refresh a's LRU position
        runtime.score("c", ["p"], score)  # evict b
        self.assertEqual(runtime.score("b", ["p"], score), [4.0])
        clock.now += 6
        self.assertEqual(runtime.score("a", ["p"], score), [5.0])
        self.assertEqual(len(calls), 5)

    def test_accepts_tuple_and_zero_or_negative_logits(self):
        runtime = RerankRuntime()
        self.assertEqual(runtime.score("q", ["one", "two"],
                                       lambda *_: (0, -3.5)), [0.0, -3.5])

    def test_invalid_results_open_breaker(self):
        for result in ([True], [math.inf], [math.nan], [10 ** 10000],
                       ["1"], [], [1, 2]):
            runtime = RerankRuntime()
            self.assertIsNone(runtime.score("q", ["p"], lambda *_: result))
            self.assertEqual(runtime.snapshot()["last_reason"], "invalid_scores")
            called = []
            self.assertIsNone(runtime.score("q2", ["p"], lambda *_: called.append(1)))
            self.assertFalse(called)

    def test_caps_reject_without_truncation_and_empty_is_none(self):
        runtime = RerankRuntime(max_pairs=2)
        called = []
        request = lambda *args: called.append(args)
        self.assertIsNone(runtime.score("q", [], request))
        self.assertIsNone(runtime.score("q", ["1", "2", "3"], request))
        self.assertIsNone(runtime.score("q", ["x" * 1201], request))
        self.assertIsNone(runtime.score("q" * 4097, ["p"], request))
        self.assertEqual(called, [])
        self.assertEqual(runtime.snapshot()["last_reason"], "invalid_input")

    def test_slow_completion_is_rejected_and_opens_cooldown(self):
        clock = FakeClock()
        runtime = RerankRuntime(timeout=3, clock=clock)

        def slow(*_):
            clock.now += 3.01
            return [1]

        self.assertIsNone(runtime.score("q", ["p"], slow))
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["last_reason"], "elapsed_timeout")
        self.assertEqual(snapshot["counters"]["elapsed_rejections"], 1)
        self.assertEqual(snapshot["status"], "open")


if __name__ == "__main__":
    unittest.main()
