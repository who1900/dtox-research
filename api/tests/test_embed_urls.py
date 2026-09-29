import pathlib
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

_PIPELINE_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

if "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fcntl")
        stub.flock = lambda *a, **kw: None
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        sys.modules["fcntl"] = stub

import requests  # noqa: E402
from pipeline import service  # noqa: E402

A = "http://a.local/embed_batch"
B = "http://b.local/embed_batch"


def _batch(n, start=0):
    return [("p1", {"text": f"t{start + i}"}) for i in range(n)]


class FakeSession:
    """Records (url, n_texts); per-URL behaviour via handlers[url](texts)."""

    def __init__(self, handlers, delay=0.0):
        self.handlers = handlers
        self.delay = delay
        self.calls = []
        self.lock = threading.Lock()

    def post(self, url, json=None, timeout=None):
        with self.lock:
            self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        out = self.handlers[url](json["texts"])
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"vectors": out})
        return resp


def ok(texts):
    return [[1.0, 0.0] for _ in texts]


def boom(texts):
    raise requests.ConnectionError("down")


class EmbedUrlTests(unittest.TestCase):
    def setUp(self):
        service._url_down_until.clear()

    def test_single_url_default_keeps_old_path(self):
        self.assertEqual(service.EMBED_BATCH_URLS, [service.EMBED_BATCH_URL])
        s = FakeSession({service.EMBED_BATCH_URL: ok})
        b, v = service._run_embed_batch(s, _batch(3))
        self.assertEqual(len(v), 3)
        self.assertEqual(s.calls, [service.EMBED_BATCH_URL])

    def test_slots_first_url_gets_embed_workers(self):
        slots = service._embed_slots([A, B])
        got = [slots.get() for _ in range(slots.qsize())]
        self.assertEqual(got.count(A), service.EMBED_WORKERS)
        self.assertEqual(got.count(B), service.EMBED_EXTRA_URL_WORKERS)

    def test_batches_spread_over_both_urls(self):
        s = FakeSession({A: ok, B: ok}, delay=0.02)
        with patch.object(service, "EMBED_BATCH_URLS", [A, B]):
            slots = service._embed_slots()
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=slots.qsize()) as pool:
                res = list(pool.map(lambda i: service._run_embed_batch(s, _batch(2, i), slots), range(12)))
        self.assertEqual(len(res), 12)
        self.assertTrue(all(len(v) == 2 for _, v in res))
        self.assertGreater(s.calls.count(A), 0)
        self.assertGreater(s.calls.count(B), 0)
        self.assertEqual(len(s.calls), 12)

    def test_failing_url_falls_back_and_batch_is_kept(self):
        s = FakeSession({A: ok, B: boom})
        with patch.object(service, "EMBED_BATCH_URLS", [A, B]):
            vecs = service.embed_texts_failover(s, ["x", "y"], B)
        self.assertEqual(len(vecs), 2)
        self.assertEqual(s.calls, [B, A])

    def test_recently_failed_url_is_tried_last(self):
        s = FakeSession({A: ok, B: boom})
        with patch.object(service, "EMBED_BATCH_URLS", [A, B]):
            service.embed_texts_failover(s, ["x"], B)
            s.calls.clear()
            service.embed_texts_failover(s, ["x"], B)
        self.assertEqual(s.calls, [A])

    def test_all_urls_down_raises_request_exception(self):
        s = FakeSession({A: boom, B: boom})
        with patch.object(service, "EMBED_BATCH_URLS", [A, B]):
            with self.assertRaises(requests.RequestException):
                service.embed_texts_failover(s, ["x"], A)
        self.assertEqual(sorted(s.calls), [A, B])

    def test_wrong_vector_count_counts_as_failure(self):
        s = FakeSession({A: lambda t: [[1.0]], B: ok})
        with patch.object(service, "EMBED_BATCH_URLS", [A, B]):
            vecs = service.embed_texts_failover(s, ["x", "y"], A)
        self.assertEqual(len(vecs), 2)
        self.assertEqual(s.calls, [A, B])

    def test_slot_returned_even_on_total_failure(self):
        s = FakeSession({A: boom, B: boom})
        with patch.object(service, "EMBED_BATCH_URLS", [A, B]):
            slots = service._embed_slots()
            n = slots.qsize()
            with self.assertRaises(requests.RequestException):
                service._run_embed_batch(s, _batch(1), slots)
            self.assertEqual(slots.qsize(), n)


if __name__ == "__main__":
    unittest.main()
