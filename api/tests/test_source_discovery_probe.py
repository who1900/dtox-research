import json
import tempfile
import unittest
from pathlib import Path

from eval import source_discovery_probe as probe


class SourceDiscoveryProbeTests(unittest.TestCase):
    def test_aliases_are_normalized_and_fixture_target_is_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_path = Path(tmp) / "fixture.json"
            fixture_path.write_text(json.dumps({"groups": [{"id": "hotstuff", "targets": [
                {"canonical_id": "1803.05069", "aliases": ["oa:W2964159515"]}],
                "hard_negatives": [{"id": "wp:bitcoin", "aliases": ["WP:BITCOIN"]}],
                "phrases": [{"id": "p", "query": "q"}]}]}), encoding="utf-8")
            items = [{"fixture": "fixture.json", "group_id": "hotstuff",
                      "phrase_id": "p", "query": "q"}]
            oracle = probe._fixture_oracles((fixture_path,), items)["q"]
        self.assertEqual(oracle["targets"], ["1803.05069", "oa:w2964159515"])
        self.assertEqual(oracle["hard_negatives"], ["wp:bitcoin"])
        self.assertEqual(oracle["canonical_alias_map"]["oa:w2964159515"], "1803.05069")

    def test_primary_and_discovery_reach_are_separate(self):
        payload = {"results": [{"arxiv_id": "primary"}],
                   "source_candidates": [{"arxiv_id": "OA:W2964159515", "retrieval_role": "source_candidate",
                                          "candidate_channel": "citation_graph",
                                          "candidate_reason": "fragment_below_relevance_floor",
                                          "relevance_floor": 0.7, "score": 0.6,
                                          "url": "https://arxiv.org/abs/1.2",
                                          "fusion_ranks": {"graph": 3},
                                          "passes_relevance_floor": False,
                                          "verification_required": True}]}
        oracle = {"targets": ["1803.05069"], "hard_negatives": [],
                  "canonical_alias_map": {"oa:w2964159515": "1803.05069", "1803.05069": "1803.05069"}}
        row = probe._variant_summary(payload, 12, oracle)
        self.assertFalse(row["primary_hit_at_10"])
        self.assertTrue(row["discovery_reach_hit"])
        self.assertTrue(row["source_candidates"][0]["verification_required"])
        self.assertTrue(row["candidate_integrity"]["valid"])
        self.assertEqual(row["candidate_channel_counts"]["citation_graph"], 1)
        self.assertEqual(row["source_candidates"][0]["fusion_ranks"]["graph"], 3)

    def test_partial_is_neither_complete_nor_a_tie_and_is_excluded_from_summary(self):
        oracle = {"targets": [], "hard_negatives": [], "canonical_alias_map": {}}
        complete = probe._variant_summary({"results": [{"arxiv_id": "x", "title": "one"}]}, 1, oracle)
        same_ids_different_result = probe._variant_summary(
            {"results": [{"arxiv_id": "x", "title": "two"}]}, 2, oracle)
        exact = probe._pair_metrics(complete, same_ids_different_result)
        self.assertTrue(exact["primary_ids_exact_equal"])
        self.assertFalse(exact["primary_results_exact_equal"])
        partial = probe._variant_summary({"results": [{"arxiv_id": "x"}], "partial": True}, 2, oracle)
        metrics = probe._pair_metrics(complete, partial)
        self.assertFalse(metrics["complete"])
        self.assertIsNone(metrics["primary_ids_exact_equal"])
        summary = probe._summarize([{"metrics": metrics}])
        self.assertEqual(summary["complete_pairs"], 0)
        self.assertEqual(summary["incomplete_pairs"], 1)

    def test_probe_protocol_limits_and_environment(self):
        config = probe._environment(Path("/opt/dtox/api"))
        self.assertEqual(config["HIER_CHUNK_LEXICAL"], "0")
        self.assertEqual(config["HIER_GROUPED_SEARCH"], "0")
        self.assertEqual(config["HIER_RANK_CHANNELS"], "0")
        self.assertEqual(config["RERANK_ENABLED"], "0")
        self.assertEqual(config["HIER_GRAPH"], "1")
        self.assertEqual(config["HIER_SEARCH"], "1")
        self.assertEqual(config["PAPER_LEXICAL"], "1")
        self.assertEqual(config["PAPER_LEXICAL_SOURCE_BALANCE"], "1")
        self.assertEqual(config["EXACT_MATCH"], "1")
        self.assertEqual(config["HIER_FUSION"], "rrf")
        self.assertEqual(config["HIER_W_DENSE"], "2")
        self.assertEqual(config["HIER_W_LEX"], "0.8")
        self.assertEqual(config["HIER_W_GLOBAL"], "1")
        self.assertEqual(probe.MAX_BUDGET_SECONDS, 180)
        self.assertEqual(probe.MAX_QUERY_SECONDS, 12)

    def test_partial_payload_not_serialized_as_complete_results(self):
        with self.assertRaises(ValueError):
            probe._variant_summary([], 1, [])
        row = probe._variant_summary({"results": [], "source_candidates": [], "partial": True}, 3,
                                     {"targets": [], "hard_negatives": [], "canonical_alias_map": {}})
        self.assertFalse(row["complete"])
        self.assertGreater(row["output_chars"], 0)

    def test_primary_snapshots_are_optional_and_limited_to_ten(self):
        oracle = {"targets": [], "hard_negatives": [], "canonical_alias_map": {}}
        payload = {"results": [{"arxiv_id": str(i), "title": f"paper-{i}"} for i in range(12)]}
        compact = probe._variant_summary(payload, 1, oracle)
        audited = probe._variant_summary(payload, 1, oracle, include_primary_snapshot=True)
        self.assertNotIn("primary_results_snapshot", compact)
        self.assertEqual(len(audited["primary_results_snapshot"]), 10)
        self.assertEqual(audited["primary_results_snapshot"][0], payload["results"][0])

    def test_candidate_integrity_and_output_size_summary(self):
        oracle = {"targets": [], "hard_negatives": ["neg"], "canonical_alias_map": {}}
        bad = {"arxiv_id": "p", "retrieval_role": "other", "candidate_reason": "made_up",
               "candidate_channel": "unknown",
               "relevance_floor": 0.7, "score": 0.8, "passes_relevance_floor": False,
               "verification_required": False, "url": ""}
        variant = probe._variant_summary({"results": [{"arxiv_id": "p"}],
                                          "source_candidates": [bad]}, 4, oracle)
        self.assertFalse(variant["candidate_integrity"]["valid"])
        self.assertEqual(variant["candidate_integrity"]["errors"][:2], [
            "candidate_0_overlaps_primary_canonical_id", "candidate_0_verification_not_required"])
        base = probe._variant_summary({"results": [], "source_candidates": []}, 1, oracle)
        treatment = probe._variant_summary({"results": [], "source_candidates": [bad]}, 2, oracle)
        pair = {"baseline": base, "treatment": treatment,
                "metrics": probe._pair_metrics(base, treatment)}
        summary = probe._summarize([pair])
        self.assertEqual(summary["additional_json_chars_p50"], treatment["output_chars"] - base["output_chars"])
        self.assertEqual(summary["additional_json_chars_p95"], treatment["output_chars"] - base["output_chars"])
        self.assertEqual(summary["average_treatment_candidate_count"], 1)
        self.assertGreater(summary["additional_json_chars_ratio_to_baseline"], 0)


if __name__ == "__main__":
    unittest.main()
