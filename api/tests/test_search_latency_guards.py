"""Guards added after the cold-cache profile: an overrunning lexical (BM25)
thread must not block a /v1/search response that dense retrieval already
answered, and hot-path sqlite reads must not reconnect on every call.

All mocked -- no real fts.db/state.db and no network.
"""
import sqlite3
import time
import threading
import unittest
from unittest.mock import patch

from api import main


class LexicalTimeoutTests(unittest.TestCase):
    """_run_search must not wait past LEXICAL_QUERY_TIMEOUT on the BM25 thread."""

    def _dense_hit(self):
        return {
            "score": 0.9,
            "payload": {"arxiv_id": "1234.5678", "title": "Dense Hit",
                       "section_type": "method"},
        }

    def test_slow_lexical_thread_is_skipped_not_awaited(self):
        def slow_bm25(*args, **kwargs):
            time.sleep(0.2)  # much slower than the tiny timeout below
            return ["1234.5678"]

        body = main.SearchBody(query="unit-test slow lexical", limit=3)
        with (patch.object(main, "LEXICAL_QUERY_TIMEOUT", 0.01),
              patch.object(main, "_bm25_candidates", side_effect=slow_bm25),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "two_phase_dense_search",
                          return_value=([self._dense_hit()], [], [], [])),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_lookup_note", return_value=None)):
            started = time.monotonic()
            result = main._run_search(body)
            elapsed = time.monotonic() - started

        # answered near the (tiny) lexical timeout, not after the 0.2s sleep
        self.assertLess(elapsed, 0.2)
        self.assertEqual(result.get("lexical_channel"), "skipped")
        self.assertNotIn("partial", result)
        self.assertEqual(result["results"][0]["arxiv_id"], "1234.5678")

    def test_fast_lexical_thread_is_not_marked_skipped(self):
        body = main.SearchBody(query="unit-test fast lexical", limit=3)
        with (patch.object(main, "_bm25_candidates", return_value=["1234.5678"]),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "two_phase_dense_search",
                          return_value=([self._dense_hit()], [], [], [])),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_lookup_note", return_value=None)):
            result = main._run_search(body)

        self.assertNotIn("lexical_channel", result)
        self.assertNotIn("partial", result)


class ReadOnlyConnectionReuseTests(unittest.TestCase):
    """_ro_conn must open one sqlite3 connection per (thread, db path)."""

    def setUp(self):
        # tests run in whatever order unittest picks; make sure no connection
        # left behind by another test/module leaks into this one
        main._ro_conn_local.conns = {}

    def tearDown(self):
        main._ro_conn_local.conns = {}

    def test_reused_across_many_calls_on_the_same_thread(self):
        fake_conn = _FakeConnection()
        with patch.object(main.sqlite3, "connect", return_value=fake_conn) as connect:
            for _ in range(10):
                conn = main._ro_conn("/fake/state.db")
                conn.execute("SELECT 1")
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(fake_conn.execute_count, 10)

    def test_separate_connections_per_db_path(self):
        with patch.object(main.sqlite3, "connect",
                          side_effect=lambda *a, **k: _FakeConnection()) as connect:
            main._ro_conn("/fake/state.db")
            main._ro_conn("/fake/fts.db")
            main._ro_conn("/fake/state.db")
            self.assertEqual(connect.call_count, 2)

    def test_dropped_connection_reconnects_on_next_call(self):
        with patch.object(main.sqlite3, "connect",
                          side_effect=lambda *a, **k: _FakeConnection()) as connect:
            main._ro_conn("/fake/state.db")
            main._ro_conn_drop("/fake/state.db")
            main._ro_conn("/fake/state.db")
            self.assertEqual(connect.call_count, 2)

    def test_paper_facts_reconnects_once_after_operational_error(self):
        good_conn = _FakeConnection()
        bad_conn = _FakeConnection(raise_on_execute=sqlite3.OperationalError("locked"))
        with patch.object(main.sqlite3, "connect", side_effect=[bad_conn, good_conn]):
            facts = main._paper_facts(["1234.5678"])
        self.assertEqual(facts, {})  # good_conn returns no rows by default
        self.assertEqual(bad_conn.execute_count, 1)
        self.assertEqual(good_conn.execute_count, 1)

    def test_each_thread_gets_its_own_connection(self):
        seen = []
        with patch.object(main.sqlite3, "connect",
                          side_effect=lambda *a, **k: _FakeConnection()) as connect:
            def worker():
                seen.append(main._ro_conn("/fake/state.db"))

            threads = [threading.Thread(target=worker) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(connect.call_count, 3)
        self.assertEqual(len({id(c) for c in seen}), 3)


class _FakeConnection:
    def __init__(self, raise_on_execute=None):
        self.execute_count = 0
        self._raise_on_execute = raise_on_execute

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        if self._raise_on_execute is not None:
            raise self._raise_on_execute
        return _FakeCursor()

    def set_progress_handler(self, *args, **kwargs):
        pass

    def close(self):
        pass


class _FakeCursor:
    def fetchall(self):
        return []

    def fetchone(self):
        return None


class SourceUrlCacheTests(unittest.TestCase):
    def setUp(self):
        main._source_url_for_id.cache_clear()

    def test_repeated_calls_hit_the_cache(self):
        main.source_url("1234.5678")
        main.source_url("1234.5678")
        info = main._source_url_for_id.cache_info()
        self.assertEqual(info.hits, 1)
        self.assertEqual(info.misses, 1)

    def test_payload_url_bypasses_the_cache_entirely(self):
        main._source_url_for_id.cache_clear()
        url = main.source_url("wp:some-project", {"url": "https://example.com/x"})
        self.assertEqual(url, "https://example.com/x")
        info = main._source_url_for_id.cache_info()
        self.assertEqual(info.hits + info.misses, 0)

    def test_result_matches_uncached_formula(self):
        self.assertEqual(main.source_url("simd:0297"),
                         "https://github.com/solana-foundation/solana-improvement-documents"
                         "/blob/main/proposals/0297")
        self.assertEqual(main.source_url("1234.5678"),
                         "https://arxiv.org/abs/1234.5678")


if __name__ == "__main__":
    unittest.main()
