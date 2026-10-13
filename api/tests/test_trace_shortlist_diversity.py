import copy
import unittest
from unittest.mock import Mock, patch

from api import main
from api.tests import test_shortlist_diversity as synthetic
from eval import trace_shortlist_diversity as observer


class TraceShortlistDiversityTests(unittest.TestCase):
    def setUp(self):
        self.case = synthetic.ShortlistDiversityTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_actual_flow_separates_discovery_passage_floor_and_absence(self):
        original = {name: getattr(main, name) for name in (
            "_coarse_paper_search", "_lexical_paper_shortlist", "_hier_stage_b_hits", "_hier_run", "search_cache")}
        outcome = self.case.search(trace=True)
        self.assertIsNone(outcome["error"])
        trace = outcome["trace"]
        self.assertFalse(trace["config"]["HIER_GROUPED_SEARCH"])
        self.assertFalse(trace["config"]["HIER_RANK_CHANNELS"])
        self.assertEqual(trace["config"]["HIER_W_DENSE"], 2.)
        self.assertEqual(trace["config"]["HIER_W_LEX"], .8)
        self.assertEqual(trace["config"]["HIER_W_GLOBAL"], 1.)
        self.assertEqual(len(trace["passes"]), 1)
        flow = trace["passes"][0]
        self.assertEqual(flow["stage_b_calls"][0]["diversity"]["pooled_candidate_count"], 120)
        rows = {row["arxiv_id"]: row for row in flow["sources"]}
        self.assertTrue(rows["dependency-method"]["stage_a"])
        self.assertTrue(rows["dependency-method"]["primary_returned"])
        self.assertEqual(rows["dependency-method"]["chunk_score"], .86)
        self.assertEqual(rows["runtime-oracles"]["loss_stage"], "passage_floor")
        self.assertFalse(rows["runtime-oracles"]["passage_qualified"])
        self.assertEqual(rows["empty"]["loss_stage"], "chunk_not_observed")
        self.assertTrue(all(row["verification_required"] for row in rows.values()))
        self.assertEqual(len(self.case.calls), 1)
        for name, value in original.items():
            self.assertIs(getattr(main, name), value)

    def test_trace_failure_is_partial_not_quality_pass(self):
        outcome = self.case.search(failure=True, trace=True)
        self.assertTrue(outcome["payload"]["partial"])
        self.assertIn("stage_b_diversity", outcome["trace"]["passes"][0]["partial"])

    def test_absent_anchor_is_not_claimed_unindexed_or_chunk_starved(self):
        trace = self.case.search(trace=True)["trace"]
        group = {"targets": [{"canonical_id": "dependency-method", "aliases": []},
                             {"canonical_id": "not-in-stage-a", "aliases": []}]}
        paths = observer.anchor_paths(group, trace, lambda aid: aid)
        self.assertTrue(paths[0]["stage_a_observed"])
        self.assertFalse(paths[1]["stage_a_observed"])
        self.assertEqual(paths[1]["observations"], [])
        self.assertEqual(paths[1]["index_coverage"], "not_measured")

    def test_gate_scoring_does_not_count_auxiliary_candidates_as_hits(self):
        fixture = {"preregistration": {"status": "frozen_before_live", "query_count": 2,
                                       "retrieval_limit": 10, "strict": False},
                   "groups": [{"id": "g", "layer": "builder-tech",
                               "targets": [{"canonical_id": "target", "aliases": []}],
                               "hard_negatives": [{"id": "negative"}],
                               "phrases": [{"id": "one", "query": "first synthetic mechanism"},
                                           {"id": "two", "query": "second synthetic mechanism"}]}]}
        before = copy.deepcopy(fixture)
        application = Mock(SEARCH_QDRANT_TIMEOUT=6, SEARCH_EMBED_WAIT_TIMEOUT=2,
                           SEARCH_EMBED_HTTP_TIMEOUT=4, SearchBody=main.SearchBody,
                           canonical_id=lambda aid: aid)
        outcomes = [
            {"payload": {"results": [{"arxiv_id": "negative"}],
                         "source_candidates": [{"arxiv_id": "target"}]},
             "trace": {"passes": []}, "error": None},
            {"payload": {"results": [{"arxiv_id": "target"}], "partial": True},
             "trace": {"passes": []}, "error": None},
        ]
        with patch.object(observer, "trace_search", side_effect=outcomes):
            report = observer.run_fixture(application, fixture)
        self.assertFalse(report["summary"]["all_groups_pass"])
        self.assertEqual(report["summary"]["operational_invalid_count"], 1)
        self.assertEqual(report["summary"]["negative_flag_count"], 1)
        self.assertFalse(report["summary"]["phrases"][0]["hit@10"])
        self.assertEqual(fixture, before)

    def test_nonfrozen_fixture_rejected_before_runtime(self):
        with patch.object(observer, "trace_search") as search:
            with self.assertRaises(ValueError):
                observer.run_fixture(Mock(), {"preregistration": {"status": "mutable", "query_count": 0},
                                               "groups": []})
        search.assert_not_called()

    def test_retention_comparison_separates_partial_from_lost_complete_hit(self):
        fixture = {"groups": [{"id": "g", "targets": [{"canonical_id": "target"}],
                               "phrases": [{"id": str(index)} for index in range(4)]}]}
        def record(index, target=True, partial=False):
            return {"group_id": "g", "phrase_id": str(index), "partial": partial,
                    "results": [{"arxiv_id": "target" if target else "other"}]}
        before = {"fixture": copy.deepcopy(fixture), "fixture_sha256": "serialization-hash",
                  "raw_results": [record(0), record(1), record(2), record(3, partial=True)]}
        after = {"records": [record(0), record(1, target=False), record(2, partial=True), record(3)]}
        result = observer.compare_complete_hits(fixture, before, after)
        self.assertEqual(result["baseline_complete_count"], 3)
        self.assertEqual(result["candidate_complete_count"], 3)
        self.assertEqual(result["complete_pair_count"], 2)
        self.assertEqual(result["baseline_known_hits_on_complete_pairs"], 2)
        self.assertEqual(result["candidate_known_hits_on_complete_pairs"], 1)
        self.assertEqual(result["retained_complete_hits"], [{"group_id": "g", "phrase_id": "0"}])
        self.assertEqual(result["lost_complete_hits"], [{"group_id": "g", "phrase_id": "1"}])
        self.assertEqual(result["unconfirmed_prior_hits_due_to_incomplete_candidate"],
                         [{"group_id": "g", "phrase_id": "2"}])
        before["fixture"]["groups"][0]["targets"][0]["canonical_id"] = "changed"
        with self.assertRaises(ValueError):
            observer.compare_complete_hits(fixture, before, after)


if __name__ == "__main__":
    unittest.main()
