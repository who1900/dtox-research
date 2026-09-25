import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api import main
from api.search_core import fts_query, merge_layer_hits, reciprocal_rank_fusion


class SearchCoreTests(unittest.TestCase):
    def test_fts_query_keeps_exact_protocol_terms(self):
        self.assertEqual(fts_query("durable nonce Solana"),
                         '"durable" AND "nonce" AND "Solana"')

    def test_rrf_rewards_agreement_between_channels(self):
        scores = reciprocal_rank_fusion(["dense", "both"], ["both", "lexical"],
                                        weights=[1.0, 0.6])
        self.assertGreater(scores["both"], scores["dense"])
        self.assertGreater(scores["dense"], scores["lexical"])

    def test_layer_fusion_deduplicates_shared_points(self):
        merged = merge_layer_hits([
            [{"id": "shared", "score": 0.81}, {"id": "a", "score": 0.90}],
            [{"id": "shared", "score": 0.84}, {"id": "b", "score": 0.88}],
        ], 3)
        self.assertEqual([item["id"] for item in merged][0], "shared")
        self.assertEqual(sum(item["id"] == "shared" for item in merged), 1)
        self.assertEqual(merged[0]["score"], 0.84)

    def test_search_returns_marked_lexical_fallback_when_dense_times_out(self):
        fallback = {
            "arxiv_id": "simd:0297", "score": None, "niche_score": 4,
            "title": "Durable Transaction Nonces", "channels": ["lexical"],
        }
        body = main.SearchBody(query="unit-test durable nonce", layer="web3", limit=3)
        with (patch.object(main, "_bm25_candidates", return_value=["simd:0297"]),
              patch.object(main, "embed_query", side_effect=HTTPException(503, "busy")),
              patch.object(main, "_lexical_fallback_results", return_value=[fallback]),
              patch.object(main, "_paper_facts", return_value={"simd:0297": 4}),
              patch.object(main, "_lookup_note", return_value=None)):
            result = main._run_search(body)
        self.assertTrue(result["partial"])
        self.assertEqual(result["results"][0]["arxiv_id"], "simd:0297")
        self.assertIsNone(result["results"][0]["score"])

    def test_audit_refuses_unscored_partial_results(self):
        with self.assertRaises(HTTPException) as error:
            main._scored_results_for_audit({
                "partial": True,
                "results": [{"arxiv_id": "simd:0297", "score": None}],
            })
        self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
