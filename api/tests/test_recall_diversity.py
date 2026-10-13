import sqlite3
import unittest
from unittest.mock import patch

from api import main


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeFtsConnection:
    def __init__(self, rows, interrupt=False):
        self.rows = rows
        self.interrupt = interrupt
        self.calls = []
        self.handlers = []
        self.handler = None

    def set_progress_handler(self, callback, steps):
        self.handlers.append((callback, steps))
        self.handler = callback

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if self.interrupt and self.handler and self.handler():
            raise sqlite3.OperationalError("interrupted")
        return Rows(self.rows[:params[-1]])


def _ranked_rows(count=120):
    rows = [(f"arxiv:{i}", float(i)) for i in range(count)]
    rows[50] = ("wp:chainlink-v2", 50.0)
    rows[70] = ("wp:other", 70.0)
    rows[80] = ("eip:7702", 80.0)
    rows[90] = ("simd:0123", 90.0)
    return rows


class PaperLexicalSourceBalanceTests(unittest.TestCase):
    def _search(self, layer="web3", limit=15, year_from=2020, year_to=2025,
                enabled=True, rows=None):
        conn = FakeFtsConnection(rows if rows is not None else _ranked_rows())
        with (patch.object(main, "_ro_conn", return_value=conn),
              patch.object(main, "PAPER_LEXICAL_SOURCE_BALANCE", enabled)):
            result = main._paper_bm25("oracle event", layer, year_from, year_to, limit)
        return result, conn

    def test_balanced_overfetch_is_one_bounded_query_and_returns_requested_cap(self):
        result, conn = self._search()
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(conn.calls[0][1][-1], 120)
        self.assertEqual(len(result), 15)
        self.assertIn("wp:chainlink-v2", result)
        self.assertIn("eip:7702", result)
        self.assertIn("simd:0123", result)
        self.assertEqual(conn.handlers[-1], (None, 0))

    def test_flag_off_preserves_global_bm25_order(self):
        result, conn = self._search(enabled=False)
        self.assertEqual(result, [f"arxiv:{i}" for i in range(15)])
        self.assertEqual(conn.calls[0][1][-1], 15)

    def test_non_web3_preserves_global_order_and_requested_limit(self):
        result, conn = self._search(layer="ai-agents")
        self.assertEqual(result, [f"arxiv:{i}" for i in range(15)])
        self.assertEqual(conn.calls[0][1][-1], 15)

    def test_layer_and_year_filters_remain_in_the_single_sql(self):
        _, conn = self._search()
        sql, params = conn.calls[0]
        self.assertIn("layers LIKE ?", sql)
        self.assertIn("CAST(year AS INTEGER) >= ?", sql)
        self.assertIn("CAST(year AS INTEGER) <= ?", sql)
        self.assertEqual(params[1:], ["%web3%", 2020, 2025, 120])

    def test_requests_above_overfetch_bound_keep_the_requested_limit(self):
        rows = _ranked_rows(130)
        result, conn = self._search(limit=125, rows=rows)
        self.assertEqual(conn.calls[0][1][-1], 125)
        self.assertEqual(len(result), 125)

    def test_stale_connection_retries_once_with_the_same_deadline(self):
        stale = FakeFtsConnection([])
        stale.execute = lambda sql, params: (_ for _ in ()).throw(
            sqlite3.OperationalError("database disk image is malformed"))
        fresh = FakeFtsConnection([("wp:chainlink-v2", -1.0)])
        with (patch.object(main, "_ro_conn", side_effect=[stale, fresh]),
              patch.object(main, "_ro_conn_drop") as drop,
              patch.object(main, "PAPER_LEXICAL_SOURCE_BALANCE", True),
              patch.object(main.time, "monotonic", return_value=0)):
            result = main._paper_bm25("oracle event", "web3", limit=15)
        self.assertEqual(result, ["wp:chainlink-v2"])
        drop.assert_called_once_with(main.PAPER_FTS_PATH)
        self.assertEqual(fresh.calls[0][1][-1], 120)
        self.assertEqual(fresh.handlers[-1], (None, 0))

    def test_query_timeout_is_propagated_and_progress_handler_is_cleared(self):
        conn = FakeFtsConnection([], interrupt=True)
        with (patch.object(main, "_ro_conn", return_value=conn),
              patch.object(main, "PAPER_LEXICAL_SOURCE_BALANCE", True),
              patch.object(main, "LEXICAL_QUERY_TIMEOUT", 1),
              patch.object(main.time, "monotonic", side_effect=[0, 2, 3])):
            with self.assertRaises(main.FuturesTimeoutError):
                main._paper_bm25("oracle event", "web3", limit=15)
        self.assertEqual(conn.handlers[-1], (None, 0))


class SearchPayloadRecallMetadataTests(unittest.TestCase):
    def setUp(self):
        main.search_cache.data.clear()
        self.hit = {
            "id": "point-1", "score": 0.9,
            "payload": {"arxiv_id": "1234.5678", "title": "Dense result",
                        "section_type": "method", "element_type": "prose",
                        "layers": ["web3"], "year": 2024, "text": "evidence"},
        }

    def _patch_search(self, paper_bm25, source_balance=True):
        return (
            patch.object(main, "HIER_SEARCH", False),
            patch.object(main, "PAPER_LEXICAL", True),
            patch.object(main, "PAPER_LEXICAL_SOURCE_BALANCE", source_balance),
            patch.object(main, "HIER_CHUNK_LEXICAL", True),
            patch.object(main, "embed_query", return_value=[0.1, 0.2]),
            patch.object(main, "_bm25_candidates", return_value=[]),
            patch.object(main, "_paper_bm25", side_effect=paper_bm25),
            patch.object(main, "two_phase_dense_search",
                         return_value=([self.hit], [], [], [])),
            patch.object(main, "_score_specific_papers", return_value=[]),
            patch.object(main, "_lexical_fallback_results", return_value=[]),
            patch.object(main, "_paper_facts", return_value={}),
            patch.object(main, "_lookup_note", return_value=None),
            patch.object(main, "_hydrate_evidence", side_effect=lambda rows: rows),
            patch.object(main, "_collapse_twins", side_effect=lambda rows: rows),
        )

    def _run_with_patches(self, body, paper_bm25, source_balance=True):
        patchers = self._patch_search(paper_bm25, source_balance)
        for p in patchers:
            p.start()
        try:
            return main._run_search(body)
        finally:
            for p in reversed(patchers):
                p.stop()

    def test_source_balance_metadata_requires_paper_lexical_hits_on_web3(self):
        body = main.SearchBody(query="oracle event", layer="web3", hybrid=True,
                               min_score=0.0, dedupe=False, auto_relax=False)
        result = self._run_with_patches(body, lambda *args, **kwargs: ["wp:chainlink-v2"])
        self.assertEqual(result.get("lexical_ranking"), "source_balanced")

    def test_source_balance_metadata_absent_when_flag_off_or_layer_is_not_web3(self):
        body = main.SearchBody(query="oracle event", layer="web3", hybrid=True,
                               min_score=0.0, dedupe=False, auto_relax=False)
        result = self._run_with_patches(body, lambda *args, **kwargs: ["wp:chainlink-v2"],
                                        source_balance=False)
        self.assertNotIn("lexical_ranking", result)
        main.search_cache.data.clear()
        other_layer = body.model_copy(update={"layer": "ai-agents"})
        result = self._run_with_patches(other_layer,
                                        lambda *args, **kwargs: ["wp:chainlink-v2"])
        self.assertNotIn("lexical_ranking", result)

    def test_paper_lexical_timeout_marks_result_incomplete(self):
        body = main.SearchBody(query="oracle event", layer="web3", hybrid=True,
                               min_score=0.0, dedupe=False, auto_relax=False)

        def timeout(*args, **kwargs):
            raise main.FuturesTimeoutError()

        result = self._run_with_patches(body, timeout)
        self.assertTrue(result.get("partial"))
        self.assertEqual(result.get("lexical_channel"), "skipped")
        self.assertNotIn("lexical_ranking", result)

    def test_sql_progress_timeout_marks_parent_partial_and_does_not_cache(self):
        main.search_cache.data.clear()
        body = main.SearchBody(query="progress timeout probe", layer="web3", hybrid=True,
                               min_score=0.0, dedupe=False, auto_relax=False)
        conn = FakeFtsConnection([], interrupt=True)
        patchers = (
            patch.object(main, "HIER_SEARCH", False),
            patch.object(main, "PAPER_LEXICAL", True),
            patch.object(main, "PAPER_LEXICAL_SOURCE_BALANCE", True),
            patch.object(main, "HIER_CHUNK_LEXICAL", True),
            patch.object(main, "LEXICAL_QUERY_TIMEOUT", 1),
            patch.object(main, "embed_query", return_value=[0.1, 0.2]),
            patch.object(main, "_bm25_candidates", return_value=[]),
            patch.object(main, "_ro_conn", return_value=conn),
            patch.object(main.time, "monotonic", side_effect=[0, 2, 3]),
            patch.object(main, "two_phase_dense_search",
                         return_value=([self.hit], [], [], [])),
            patch.object(main, "_paper_facts", return_value={}),
            patch.object(main, "_lookup_note", return_value=None),
            patch.object(main, "_hydrate_evidence", side_effect=lambda rows: rows),
            patch.object(main, "_collapse_twins", side_effect=lambda rows: rows),
            patch.object(main, "canonical_id", side_effect=lambda paper_id: paper_id),
            patch.object(main, "_inject_exact", side_effect=lambda rows, *args: rows),
        )
        for p in patchers:
            p.start()
        try:
            result = main._run_search(body)
        finally:
            for p in reversed(patchers):
                p.stop()
        self.assertTrue(result.get("partial"))
        self.assertEqual(result.get("lexical_channel"), "skipped")
        self.assertNotIn("lexical_ranking", result)
        self.assertEqual(len(main.search_cache.data), 0)
        self.assertEqual(conn.handlers[-1], (None, 0))

    def test_hierarchical_lexical_timeout_keeps_coarse_stage_b_results_partial(self):
        main.search_cache.data.clear()
        body = main.SearchBody(query="hierarchical lexical timeout", layer="web3",
                               hybrid=True, min_score=0.0, dedupe=False,
                               auto_relax=False)
        chunk = {
            "id": "stage-b-point", "score": 0.9,
            "payload": {"arxiv_id": "wp:coarse-candidate", "title": "Coarse candidate",
                        "section_type": "method", "element_type": "prose",
                        "layers": ["web3"], "year": 2024, "text": "stage B evidence"},
        }

        def timeout(*args, **kwargs):
            raise main.FuturesTimeoutError()

        with (patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "HIER_FUSION", "mix"),
              patch.object(main, "HIER_GRAPH", False),
              patch.object(main, "PAPER_LEXICAL", True),
              patch.object(main, "PAPER_LEXICAL_SOURCE_BALANCE", True),
              patch.object(main, "HIER_CHUNK_LEXICAL", False),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "_paper_bm25", side_effect=timeout),
              patch.object(main, "_coarse_paper_search",
                           return_value=[{"arxiv_id": "wp:coarse-candidate",
                                          "paper_score": 0.9}]),
              patch.object(main, "_hier_stage_b_hits", return_value=[chunk]),
              patch.object(main, "two_phase_dense_search", return_value=([], [], [], [])),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_lookup_note", return_value=None),
              patch.object(main, "_result_from_hit",
                           side_effect=lambda hit: {"arxiv_id": hit["payload"]["arxiv_id"],
                                                   "score": hit["score"],
                                                   "title": hit["payload"]["title"]}),
              patch.object(main, "_hydrate_evidence", side_effect=lambda rows: rows),
              patch.object(main, "_collapse_twins", side_effect=lambda rows: rows),
              patch.object(main, "_inject_exact", side_effect=lambda rows, *args: rows)):
            result = main._run_search(body)

        self.assertIn("wp:coarse-candidate", [r["arxiv_id"] for r in result["results"]])
        self.assertTrue(result.get("partial"))
        self.assertEqual(result.get("lexical_channel"), "skipped")
        self.assertNotIn("lexical_ranking", result)
        self.assertEqual(len(main.search_cache.data), 0)

    def test_strict_min_score_still_drops_below_floor_title_candidates(self):
        body = main.SearchBody(query="Generative Agents memory stream reflection planning",
                               layer="ai-agents", strict=True, hybrid=False,
                               min_score=0.8, dedupe=False, auto_relax=False)
        title_hit = {"arxiv_id": "2304.03442", "title": "Generative Agents",
                     "score": 0.7, "channels": ["exact"]}
        with (patch.object(main, "HIER_SEARCH", False),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "two_phase_dense_search",
                           return_value=([self.hit], [], [], [])),
              patch.object(main, "_exact_matches",
                           return_value=[("2304.03442", "title match")]),
              patch.object(main, "_score_specific_papers", return_value=[title_hit]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_lookup_note", return_value=None),
              patch.object(main, "_hydrate_evidence", side_effect=lambda rows: rows),
              patch.object(main, "_collapse_twins", side_effect=lambda rows: rows)):
            result = main._run_search(body)
        self.assertNotIn("2304.03442", [r["arxiv_id"] for r in result["results"]])


if __name__ == "__main__":
    unittest.main()
