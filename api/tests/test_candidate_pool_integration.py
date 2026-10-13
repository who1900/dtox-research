import copy
import unittest
from unittest.mock import patch

from api import main


def _hit(paper_id, score, text="method body"):
    return {"id": paper_id, "score": score, "payload": {
        "arxiv_id": paper_id, "title": paper_id, "text": text,
        "section_type": "method", "section_title": "Methods",
        "layers": ["web3"],
    }}


class CandidatePoolIntegrationTests(unittest.TestCase):
    def setUp(self):
        main.search_cache.data.clear()

    def _run(self, hits, exact=None, extra=None, discovery=True, **body_fields):
        body = main.SearchBody(query="candidate pool regression", layer="web3",
                               hybrid=True, dedupe=True, diagnose=False,
                               auto_relax=False, limit=1, **body_fields)

        def inject_exact(results, *args):
            return exact(results) if exact else results

        with (patch.object(main, "SOURCE_DISCOVERY", discovery),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "embed_query", return_value=[0.1]),
              patch.object(main, "_hier_run", return_value=(hits, [], [], extra or {
                  "paper_scores": {}, "global_channel": "used", "global_extra": []})),
              patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_hydrate_evidence", side_effect=lambda rows, *a: rows),
              patch.object(main, "_evidence_rows", return_value={}),
              patch.object(main, "_inject_exact", side_effect=inject_exact),
              patch.object(main, "HIER_GRAPH_QUOTA_POS", 2),
              patch.object(main, "_lookup_note", return_value=None)):
            return main._run_search(body)

    def test_late_exact_remains_auxiliary_and_primary_output_is_unchanged(self):
        hits = [_hit("primary", 0.95), _hit("late", 0.71)]
        exact_row = {"arxiv_id": "exact", "score": None, "text": "exact title",
                     "channels": ["exact"]}
        before = copy.deepcopy(exact_row)
        payload = self._run(hits, exact=lambda rows: [exact_row, *rows])

        self.assertEqual(payload["results"][0], before)
        self.assertEqual(payload["results"][1]["arxiv_id"], "primary")
        self.assertIn("late", [row["arxiv_id"] for row in payload["source_candidates"]])
        self.assertEqual(payload["retrieval_summary"]["returned_unique_count"], 2)

    def test_graph_quota_chunk_after_primary_cut_is_in_ledger(self):
        hits = [_hit("main", 0.95)]
        quota_hit = _hit("quota", 0.83, "quota snippet")
        extra = {"paper_scores": {}, "global_channel": "used", "global_extra": [],
                 "graph_quota": {"quota": "cited by 3"},
                 "quota_chunks": {"quota": quota_hit},
                 "fusion_ranks": {"quota": {"graph": 1}}}
        original_hits = copy.deepcopy((hits, extra))
        payload = self._run(hits, extra=extra)

        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["main"])
        candidate = next(row for row in payload["source_candidates"]
                         if row["arxiv_id"] == "quota")
        self.assertEqual(candidate["text"], "quota snippet")
        self.assertEqual(candidate["fusion_ranks"], {"graph": 1})
        self.assertEqual((hits, extra), original_hits)

    def test_floor_lead_survives_and_invalid_raw_score_stays_unscored(self):
        payload = self._run([_hit("weak", 0.2)])
        by_id = {row["arxiv_id"]: row for row in payload["source_candidates"]}
        self.assertIn("weak", by_id)
        self.assertFalse(by_id["weak"]["passes_relevance_floor"])

    def test_summary_is_active_with_discovery_flag_off_without_exposing_leads(self):
        payload = self._run([_hit("primary", 0.95), _hit("lead", 0.2)], discovery=False)
        self.assertIn("retrieval_summary", payload)
        self.assertNotIn("source_candidates", payload)
        self.assertEqual(payload["retrieval_summary"]["observed_candidate_count"], 1)

    def test_strict_summary_omits_raw_ineligible_leads(self):
        payload = self._run([_hit("primary", 0.95), _hit("weak", 0.2)], strict=True)
        self.assertEqual(payload["retrieval_summary"]["observed_candidate_count"], 0)
        self.assertNotIn("source_candidates", payload)

    def test_soft_layer_search_reports_real_per_pass_ledgers(self):
        body = main.SearchBody(query="agentic retrieval", layer="ai-agents",
                               hybrid=True, dedupe=True, diagnose=False,
                               auto_relax=False, limit=2)
        shared = _hit("shared", 0.91)
        requested_only = _hit("requested-only", 0.86)
        requested_tail = _hit("requested-tail", 0.85)
        unique = _hit("partner-only", 0.83)
        partner_tail = _hit("partner-tail", 0.82)

        def hier(_vector, _qfilter, _must, request_body, *_args, **_kwargs):
            found = ([shared, requested_only, requested_tail]
                     if request_body.layer == "ai-agents"
                     else [shared, unique, partner_tail])
            return found, [], [], {"paper_scores": {}, "global_channel": "used",
                                   "global_extra": []}

        with (patch.object(main, "SOURCE_DISCOVERY", True),
              patch.object(main, "SOFT_LAYERS", True),
              patch.object(main, "_soft_partner", return_value="llm-slm"),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "embed_query", return_value=[0.1]),
              patch.object(main, "_hier_run", side_effect=hier),
              patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_hydrate_evidence", side_effect=lambda rows, *a: rows),
              patch.object(main, "_evidence_rows", return_value={}),
              patch.object(main, "_inject_exact", side_effect=lambda rows, *a: rows),
              patch.object(main, "_lookup_note", return_value=None)):
            payload = main._run_search(body)

        summary = payload["retrieval_summary"]
        self.assertEqual(set(summary["passes"]), {"requested", "partner"})
        self.assertEqual(summary["ordering_policy"],
                         "per-pass summaries; no cross-pass candidate-count sum")
        self.assertEqual(summary["returned_unique_count"], 2)
        requested_count = summary["passes"]["requested"]["observed_candidate_count"]
        partner_count = summary["passes"]["partner"]["observed_candidate_count"]
        self.assertGreaterEqual(requested_count, 1)
        self.assertGreaterEqual(partner_count, 1)
        self.assertNotIn("observed_candidate_count", summary)

    def test_rerank_uses_bounded_runtime_and_emits_relevance_only_wording(self):
        main._rerank_runtime = main.RerankRuntime(max_pairs=8, timeout=3,
                                                   cooldown=60, cache_size=128,
                                                   cache_ttl=300)

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"scores": [0.4]}

        with patch.object(main.requests, "post", return_value=Response()) as post:
            scores = main._rerank("query", [{"text": "passage"}])
        self.assertEqual(scores, [0.4])
        self.assertEqual(post.call_args.kwargs["timeout"], 3)
        self.assertEqual(main._rerank_runtime.snapshot()["counters"]["successes"], 1)
        self.assertEqual(
            main._corroborated({"cross_top": True, "cross_score": 0.2}),
            "cross-encoder ranked this passage as relevant; this is not claim verification")

    def test_health_exposes_rerank_guard_snapshot_without_network(self):
        with patch.object(main.requests, "post") as post:
            payload = main.health()
        self.assertEqual(payload["optional_reranker"]["enabled"], main.RERANK_ENABLED)
        self.assertIn("counters", payload["optional_reranker"])
        self.assertIn("cooldown_remaining", payload["optional_reranker"])
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
