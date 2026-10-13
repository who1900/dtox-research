import unittest
from unittest.mock import patch

from api import main


def _hit(arxiv_id, score):
    return {"id": arxiv_id, "score": score,
            "payload": {"arxiv_id": arxiv_id, "title": arxiv_id, "text": "body"}}


def _body(**kwargs):
    values = dict(query="single fusion invariant", layer="web3", hybrid=True,
                  diagnose=False, dedupe=False, min_score=0.0, limit=8)
    values.update(kwargs)
    return main.SearchBody(**values)


class HierSingleFusionTests(unittest.TestCase):
    def setUp(self):
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        self.patches = [
            patch.object(main, "embed_query", return_value=[0.1, 0.2]),
            patch.object(main, "_paper_facts", return_value={}),
            patch.object(main, "_inject_exact", side_effect=lambda results, *args: results),
            patch.object(main, "HIER_SEARCH", True),
            patch.object(main, "HIER_CHUNK_LEXICAL", False),
            patch.object(main, "PAPER_LEXICAL", True),
            patch.object(main, "HIER_FUSION", "rrf"),
            patch.object(main, "HIER_GRAPH", False),
            patch.object(main, "HIER_W_LEX", 3.0),
            patch.object(main, "HIER_GLOBAL_GRACE", 0.2),
            patch.object(main, "_coarse_paper_search", return_value=[
                {"arxiv_id": "p1", "paper_score": 0.9},
                {"arxiv_id": "p2", "paper_score": 0.8},
            ]),
            patch.object(main, "_hier_stage_b_hits", return_value=[
                _hit("p1", 0.9), _hit("p2", 0.8),
            ]),
            patch.object(main, "two_phase_dense_search", return_value=([], [], [], [])),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_hier_uses_one_bm25_list_and_keeps_its_fused_order(self):
        with patch.object(main, "_paper_bm25",
                          side_effect=[["p2", "p1"], ["p1", "p2"]]) as paper_bm25:
            result = main._run_search(_body())

        self.assertEqual(paper_bm25.call_count, 1)
        self.assertEqual([r["arxiv_id"] for r in result["results"]], ["p2", "p1"])
        self.assertEqual(result["results"][0]["channels"], ["paper", "lexical"])
        expected = main.reciprocal_rank_fusion(
            ["p1", "p2"], ["p2", "p1"], [],
            weights=[main.HIER_W_DENSE, main.HIER_W_LEX, main.HIER_W_GLOBAL])
        self.assertEqual(result["results"][0]["fusion_score"], round(expected["p2"], 6))

    def test_timed_out_hier_lexical_is_partial_and_not_cached(self):
        with patch.object(main, "_paper_bm25", side_effect=main.FuturesTimeoutError):
            result = main._run_search(_body())

        self.assertTrue(result["partial"])
        self.assertEqual(result["lexical_channel"], "skipped")
        self.assertFalse(main.search_cache.data)

    def test_strict_floor_still_drops_low_score_hier_result(self):
        with patch.object(main, "_paper_bm25", return_value=["p2", "p1"]):
            result = main._run_search(_body(strict=True, min_score=0.85))

        self.assertEqual([r["arxiv_id"] for r in result["results"]], ["p1"])

    def test_nonhierarchical_outer_rrf_is_unchanged(self):
        dense_hits = [_hit("p1", 0.9), _hit("p2", 0.8)]
        lexical_extra = {"arxiv_id": "p3", "score": 0.7}
        with patch.object(main, "HIER_SEARCH", False), \
             patch.object(main, "PAPER_LEXICAL", False), \
             patch.object(main, "_bm25_candidates", return_value=["p3", "p2", "p1"]), \
             patch.object(main, "two_phase_dense_search",
                          return_value=(dense_hits, [], [], [])), \
             patch.object(main, "_score_specific_papers", return_value=[lexical_extra]):
            result = main._run_search(_body(query="nonhier outer rrf"))

        self.assertEqual([r["arxiv_id"] for r in result["results"]], ["p1", "p2", "p3"])
        self.assertEqual(result["results"][2]["channels"], ["lexical"])
        self.assertGreater(result["results"][2]["fusion_score"], 0)


if __name__ == "__main__":
    unittest.main()
