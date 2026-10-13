import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from api import main


def _hit(arxiv_id, score):
    return {"id": f"chunk:{arxiv_id}", "score": score,
            "payload": {"arxiv_id": arxiv_id, "title": arxiv_id, "text": "body"}}


def _body():
    return main.SearchBody(query="rank channel fixture", layer="web3", hybrid=False,
                           diagnose=False, dedupe=False, min_score=0.0, limit=10)


def _run_hier(coarse, chunks, lexical, global_hits=None, graph_lists=None,
              rank_channels=True, graph=False, weights=None):
    graph_lists = graph_lists or ([], {})
    weights = weights or {}
    graph_counts = {}

    def graph_channel(*args, counts_out=None, **kwargs):
        counts_out.update(graph_counts)
        return graph_lists

    with ExitStack() as stack:
        for name, value in (
            ("HIER_FUSION", "rrf"), ("HIER_RANK_CHANNELS", rank_channels),
            ("HIER_GRAPH", graph), ("HIER_GRAPH_QUOTA", False),
            ("HIER_GLOBAL_GRACE", 0.2), ("HIER_W_DENSE", weights.get("dense", 2.0)),
            ("HIER_W_LEX", weights.get("lex", 0.6)),
            ("HIER_W_GLOBAL", weights.get("global", 0.4)),
            ("HIER_W_GRAPH", weights.get("graph", 0.8)),
            ("PAPER_LEXICAL", bool(lexical)),
        ):
            stack.enter_context(patch.object(main, name, value))
        stack.enter_context(patch.object(main, "_coarse_paper_search", return_value=coarse))
        stack.enter_context(patch.object(main, "_paper_bm25", return_value=lexical))
        stack.enter_context(patch.object(main, "_hier_stage_b_hits", return_value=chunks))
        stack.enter_context(patch.object(main, "two_phase_dense_search",
                                          return_value=(global_hits or [], [], [], [])))
        stack.enter_context(patch.object(main, "_graph_channel", side_effect=graph_channel))
        return main._hier_run([0.1], None, [], _body(), 10, 10, 1.0, None,
                              query="rank channel fixture")


class HierRankChannelTests(unittest.TestCase):
    def test_already_fused_search_obeys_requested_result_limit(self):
        hits = [_hit(f"paper-{i}", 0.9 - i / 1000) for i in range(30)]
        ids = [hit["payload"]["arxiv_id"] for hit in hits]
        extra = {
            "paper_scores": {aid: 0.9 for aid in ids},
            "paper_lexical_used": True,
            "paper_lexical_skipped": False,
            "global_channel": None,
            "global_extra": [],
            "fusion_ranks": {aid: {"dense": i + 1, "lex": None}
                             for i, aid in enumerate(ids)},
            "fusion_scores": {aid: 1.0 - i / 1000 for i, aid in enumerate(ids)},
            "lex_exempt": set(),
            "found_via": {aid: "paper" for aid in ids},
            "graph_quota": {},
            "graph_channel": None,
        }
        main.search_cache.data.clear()
        with patch.object(main, "HIER_SEARCH", True), \
             patch.object(main, "HIER_CHUNK_LEXICAL", False), \
             patch.object(main, "PAPER_LEXICAL", True), \
             patch.object(main, "HIER_FUSION", "rrf"), \
             patch.object(main, "embed_query", return_value=[0.1]), \
             patch.object(main, "_hier_run", return_value=(hits, [], [], extra)), \
             patch.object(main, "_paper_bm25", return_value=[]), \
             patch.object(main, "_paper_facts", return_value={}), \
             patch.object(main, "_rank_score", return_value=0.5), \
             patch.object(main, "_inject_exact", side_effect=lambda results, *args: results):
            result = main._run_search(main.SearchBody(
                query="limit 10 already fused", layer="web3", hybrid=True,
                diagnose=False, dedupe=False, limit=10, min_score=0.0))

        self.assertEqual(result["count"], 10)
        self.assertEqual(len(result["results"]), 10)
        self.assertEqual([r["arxiv_id"] for r in result["results"]], ids[:10])

    def test_coarse_score_scale_does_not_change_rank_channel_fusion(self):
        chunks = [_hit("p1", 0.7), _hit("p2", 0.95), _hit("lex", 0.8)]
        first = _run_hier([
            {"arxiv_id": "p1", "paper_score": 0.99},
            {"arxiv_id": "p2", "paper_score": 0.98},
        ], chunks, ["lex", "p2"])
        second = _run_hier([
            {"arxiv_id": "p1", "paper_score": 0.41},
            {"arxiv_id": "p2", "paper_score": 0.40},
        ], chunks, ["lex", "p2"])

        self.assertEqual([h["payload"]["arxiv_id"] for h in first[0]],
                         [h["payload"]["arxiv_id"] for h in second[0]])
        self.assertEqual(first[3]["fusion_scores"], second[3]["fusion_scores"])

    def test_lexical_only_paper_is_not_in_paper_dense_channel(self):
        hits, _, _, extra = _run_hier([
            {"arxiv_id": "p1", "paper_score": 0.9},
            {"arxiv_id": "p2", "paper_score": 0.8},
        ], [_hit("p1", 0.7), _hit("p2", 0.8), _hit("lex-only", 0.95)],
            ["lex-only", "p1"])

        self.assertIsNone(extra["fusion_ranks"]["lex-only"]["dense"])
        self.assertEqual(extra["fusion_ranks"]["lex-only"]["lex"], 1)
        self.assertIsNotNone(extra["fusion_ranks"]["lex-only"]["chunk"])
        self.assertEqual(extra["fusion_ranks"]["p1"]["dense"], 1)
        self.assertTrue(hits)

    def test_independent_channel_weights_and_best_real_global_chunk(self):
        global_p1 = _hit("p1", 0.99)
        graph_z = _hit("z", 0.6)
        hits, _, _, extra = _run_hier([
            {"arxiv_id": "p1", "paper_score": 0.9},
            {"arxiv_id": "p2", "paper_score": 0.8},
        ], [_hit("p1", 0.7), _hit("p2", 0.95), _hit("lex", 0.8)],
            ["lex", "p1"], global_hits=[global_p1, _hit("g", 0.75)],
            graph_lists=(["g", "z"], {"z": graph_z}), graph=True,
            weights={"dense": 2.0, "lex": 0.6, "global": 0.4, "graph": 0.8})

        expected = main.reciprocal_rank_fusion(
            ["p1", "p2"], ["p2", "lex", "p1"], ["lex", "p1"],
            ["p1", "g"], ["g", "z"], weights=[1.0, 1.0, 0.6, 0.4, 0.8])
        self.assertEqual(extra["fusion_scores"],
                         {aid: round(score, 6) for aid, score in expected.items()})
        self.assertEqual(extra["fusion_ranks"]["p1"]["dense"], 1)
        self.assertEqual(extra["fusion_ranks"]["p1"]["chunk"], 3)
        self.assertEqual(next(h["score"] for h in hits
                              if h["payload"]["arxiv_id"] == "p1"), 0.99)

    def test_legacy_rrf_keeps_mixed_dense_order_and_rank_shape(self):
        hits, _, _, extra = _run_hier([
            {"arxiv_id": "p1", "paper_score": 0.9},
            {"arxiv_id": "p2", "paper_score": 0.8},
        ], [_hit("p1", 0.1), _hit("p2", 0.99)], [], rank_channels=False)

        self.assertEqual(extra["fusion_ranks"]["p2"]["dense"], 1)
        self.assertNotIn("chunk", extra["fusion_ranks"]["p2"])
        self.assertEqual(hits[0]["payload"]["arxiv_id"], "p2")

    def test_grouped_stage_b_deduplicates_ids_and_preserves_filters(self):
        response = MagicMock()
        response.json.return_value = {"result": {"groups": [
            {"hits": [_hit("p1", 0.9)]}, {"hits": [_hit("p2", 0.8)]},
        ]}}
        must = [{"key": "layers", "match": {"any": ["web3"]}},
                {"key": "section_type", "match": {"value": "method"}}]
        with patch.object(main, "HIER_GROUPED_SEARCH", True), \
             patch.object(main.requests, "post", return_value=response) as post:
            hits = main._hier_stage_b_hits([0.1], must, ["p1", "p1", "p2"], 50, 2.0)

        self.assertEqual([h["payload"]["arxiv_id"] for h in hits], ["p1", "p2"])
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].endswith("/points/search/groups"))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["group_size"], 1)
        self.assertEqual(body["filter"]["must"][:2], must)
        self.assertEqual(body["filter"]["must"][2]["match"]["any"], ["p1", "p2"])

    def test_malformed_grouped_response_falls_back_as_partial_without_retry(self):
        response = MagicMock()
        response.json.return_value = {"result": []}
        with patch.object(main, "HIER_GROUPED_SEARCH", True), \
             patch.object(main, "HIER_FUSION", "rrf"), \
             patch.object(main, "_coarse_paper_search", return_value=[
                 {"arxiv_id": "p1", "paper_score": 0.9}]), \
             patch.object(main, "two_phase_dense_search",
                          return_value=([_hit("fallback", 0.8)], [], [], [])) as fallback, \
             patch.object(main.requests, "post", return_value=response) as post:
            hits, _, partial, extra = main._hier_run(
                [0.1], None, [], _body(), 5, 5, 1.0, None,
                query="malformed grouped fixture")

        self.assertEqual(post.call_count, 1)
        self.assertEqual(fallback.call_count, 1)
        self.assertIsNone(extra)
        self.assertIn("coarse_fallback", partial)
        self.assertEqual(hits[0]["payload"]["arxiv_id"], "fallback")


if __name__ == "__main__":
    unittest.main()
