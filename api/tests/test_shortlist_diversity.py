import copy
import sqlite3
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from api import main
from api.tests.test_global_metadata_filter import matches


def hit(aid, score, point=None, **fields):
    return {"id": point or aid, "score": score, "payload": {
        "arxiv_id": aid, "title": aid, "text": "method body",
        "layers": ["web3"], "section_type": "method", "element_type": "prose",
        "year": 2022, "terms": ["dependency"], **fields}}


class ShortlistDiversityTests(unittest.TestCase):
    def setUp(self):
        self.publication_filter = main._filter_publication_points
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("HIER_GROUPED_SEARCH", False), ("canonical_id", lambda x: x),
                            ("_filter_publication_points", lambda rows: rows)):
            self.stack.enter_context(patch.object(main, name, value))
        self.stack.enter_context(patch("requests.sessions.Session.request",
                                      side_effect=AssertionError("network forbidden")))
        self.must = [
            {"key": "layers", "match": {"any": ["builder-tech"]}},
            {"key": "section_type", "match": {"value": "method"}},
            {"key": "element_type", "match": {"value": "prose"}},
            {"key": "year", "range": {"gte": 2020, "lte": 2025}},
            {"key": "terms", "match": {"any": ["dependency"]}},
        ]
        self.ids = ["collector", "dependency-method", "runtime-oracles", "empty", "wrong-layer"]
        self.metadata = {aid: {"arxiv_id": aid, "layers": ["builder-tech"]}
                         for aid in self.ids[:-1]}
        self.metadata["wrong-layer"] = {"arxiv_id": "wrong-layer", "layers": ["web3"]}
        self.monopoly = [hit("collector", .96, f"collector-{i}") for i in range(120)]
        self.target = hit("dependency-method", .86,
                          text="Prune dependent call sequences to reach multi-step state bugs.")
        self.negative = hit("runtime-oracles", .61,
                            text="Generate ABI calls and inspect runtime oracles, without dependency pruning.")
        self.corpus = [*self.monopoly, self.target, self.negative,
                       hit("dependency-method", .99, "wrong-section", section_type="reference"),
                       hit("runtime-oracles", .99, "wrong-year", year=2010),
                       hit("wrong-layer", .99, layers=["builder-tech"]),
                       hit("outsider", .99, layers=["builder-tech"])]
        self.calls = []

    def post(self, url, json, timeout):
        self.assertTrue(url.endswith("/points/search/batch"))
        self.calls.append((copy.deepcopy(json), timeout))
        results = []
        for search in json["searches"]:
            eligible = [row for row in self.corpus if matches(search["filter"], row["payload"])]
            results.append(sorted(eligible, key=lambda row: -row["score"])[:search["limit"]])
        response = Mock()
        response.json.return_value = {"result": results}
        return response

    def stage(self, pool=None, post=None, ids=None):
        pool = self.monopoly if pool is None else pool
        ids = self.ids if ids is None else ids
        with (patch.object(main, "_evidence_rows", return_value=self.metadata) as metadata,
              patch.object(main, "two_phase_dense_search",
                           return_value=(pool[:15], pool[:15], pool[15:], [])),
              patch.object(main.requests, "post", side_effect=post or self.post)):
            result = main._hier_stage_b_hits([.1], self.must, ids, 120, 5)
        metadata.assert_called_once_with(list(dict.fromkeys(ids)))
        return result

    def test_monopoly_cannot_starve_original_shortlist(self):
        before = copy.deepcopy((self.must, self.ids, self.corpus))
        result = self.stage()
        self.assertEqual(result[:15], self.monopoly[:15])
        self.assertEqual({row["payload"]["arxiv_id"] for row in result},
                         {"collector", "dependency-method", "runtime-oracles"})
        self.assertEqual(result[-2]["score"], .86)
        self.assertEqual(result[-1]["score"], .61)
        self.assertEqual(result.trace["rescued_source_count"], 2)
        self.assertEqual(result.trace["source_status"]["empty"], "no_visible_chunk_returned")
        self.assertNotIn("wrong-layer", result.trace["source_status"])
        self.assertEqual(result.partial, [])
        self.assertEqual((self.must, self.ids, self.corpus), before)
        self.assertEqual(len(self.calls), 1)
        self.assertLessEqual(self.calls[0][1], 5)
        self.assertGreater(self.calls[0][1], 0)
        for search in self.calls[0][0]["searches"]:
            self.assertEqual(search["limit"], 1)
            self.assertTrue(search["with_payload"])
            self.assertFalse(search["with_vector"])
            self.assertEqual(search["filter"]["must"][:4], self.must[1:])

    def test_unknown_metadata_retains_chunk_layer_and_all_facets(self):
        self.ids.append("unknown")
        self.corpus.extend([hit("unknown", .99, "stale"),
                            hit("unknown", .84, "matching", layers=["builder-tech"]),
                            hit("unknown", .98, "wrong-element", layers=["builder-tech"], element_type="code"),
                            hit("unknown", .98, "wrong-term", layers=["builder-tech"], terms=["oracle"])])
        result = self.stage()
        self.assertEqual(next(row["id"] for row in result if row["payload"]["arxiv_id"] == "unknown"),
                         "matching")

    def test_unsaturated_pool_without_leftovers_issues_no_batch(self):
        result = self.stage(pool=[hit("collector", .9)])
        self.assertEqual(len(result), 1)
        self.assertEqual(self.calls, [])

    def test_rescue_publication_visibility_is_checked_once_and_fails_closed(self):
        self.target["payload"]["publication_revision"] = "unpublished"
        with (patch.object(main, "_filter_publication_points", wraps=self.publication_filter) as guard,
              patch.object(main, "_ro_conn", side_effect=sqlite3.OperationalError("journal unavailable"))):
            result = self.stage()
        self.assertNotIn("dependency-method", [row["payload"]["arxiv_id"] for row in result])
        self.assertEqual(result.trace["source_status"]["dependency-method"], "no_visible_chunk_returned")
        guard.assert_called_once()

    def test_unhydrated_tail_triggers_diversity_even_below_global_cap(self):
        result = self.stage(pool=self.monopoly[:20])
        self.assertIn("dependency-method", [row["payload"]["arxiv_id"] for row in result])
        self.assertEqual(len(self.calls), 1)

    def test_canonical_twins_share_one_rescue_query(self):
        self.ids.append("dependency-copy")
        self.metadata["dependency-copy"] = {"arxiv_id": "dependency-copy", "layers": ["builder-tech"]}
        with patch.object(main, "canonical_id", side_effect=lambda aid: (
                "dependency-method" if aid == "dependency-copy" else aid)):
            result = self.stage()
        self.assertEqual(result.trace["rescue_query_count"], 3)
        groups = [search["filter"]["must"][-1]["match"]["any"]
                  for search in self.calls[0][0]["searches"]]
        self.assertIn(["dependency-method", "dependency-copy"], groups)

    def test_http_failure_keeps_pooled_hits_and_marks_partial(self):
        result = self.stage(post=Mock(side_effect=main.requests.Timeout("bounded timeout")))
        self.assertEqual(result, self.monopoly[:15])
        self.assertEqual(result.partial, ["stage_b_diversity"])
        self.assertEqual(set(result.trace["source_status"].values()), {"batch_failed"})

    def test_malformed_batch_or_untrusted_score_never_promoted(self):
        for response_value in ({"result": []}, {"result": [[{"id": "x", "score": .99}]]},
                               {"result": [[hit("dependency-method", float("nan"))], [], []]},
                               {"result": [[hit("outsider", .99)], [], []]}):
            with self.subTest(response=response_value):
                response = Mock()
                response.json.return_value = response_value
                result = self.stage(post=Mock(return_value=response))
                self.assertEqual(result, self.monopoly[:15])
                self.assertIn("stage_b_diversity", result.partial)

    def test_bounded_queries_and_expired_deadline_do_not_expand_source_set(self):
        filt = {"must": [{"key": "arxiv_id", "match": {"any": ["a", "b", "c"]}}]}
        response = Mock()
        response.json.return_value = {"result": [[], []]}
        with patch.object(main.requests, "post", return_value=response) as post:
            result = main._shortlist_diversity_hits([.1], filt, ["a", "b", "c"], [], 2, 5)
        self.assertEqual(len(post.call_args.kwargs["json"]["searches"]), 2)
        self.assertIn("stage_b_diversity_budget", result.partial)
        self.assertEqual(result.trace["source_status"]["c"], "not_attempted")
        with patch.object(main.requests, "post") as post:
            result = main._shortlist_diversity_hits([.1], filt, ["a"], [], 120, 0)
        post.assert_not_called()
        self.assertIn("stage_b_diversity_timeout", result.partial)

    def search(self, strict=False, failure=False, trace=False):
        main.search_cache.data.clear()
        settings = {"HIER_SEARCH": True, "HIER_FUSION": "rrf", "HIER_W_DENSE": 2.,
                    "HIER_W_LEX": .8, "HIER_W_GLOBAL": 1., "HIER_CHUNK_LEXICAL": False,
                    "HIER_RANK_CHANNELS": False, "HIER_GRAPH": False, "HIER_GRAPH_QUOTA": False,
                    "PAPER_LEXICAL": True, "SOURCE_DISCOVERY": False}
        body = main.SearchBody(query="dependency-guided transaction sequence pruning",
                               layer="builder-tech", hybrid=True, dedupe=True, strict=strict,
                               section_type="method", element_type="prose", terms=["dependency"],
                               year_from=2020, year_to=2025,
                               min_score=.8 if strict else None,
                               diagnose=False, auto_relax=False, limit=10)
        def dense(_vector, filt, *_args, **_kwargs):
            if any(item.get("key") == "arxiv_id" for item in (filt or {}).get("must", [])):
                return self.monopoly[:15], self.monopoly[:15], self.monopoly[15:], []
            return [], [], [], []
        with ExitStack() as stack:
            for name, value in settings.items():
                stack.enter_context(patch.object(main, name, value))
            for name, value in (("_evidence_rows", self.metadata), ("embed_query", [.1]),
                                ("_coarse_paper_search", [{"arxiv_id": aid, "paper_score": .9}
                                                         for aid in self.ids]),
                                ("_paper_bm25", []), ("_paper_facts", {}), ("_lookup_note", None)):
                stack.enter_context(patch.object(main, name, return_value=value))
            stack.enter_context(patch.object(main, "two_phase_dense_search", side_effect=dense))
            stack.enter_context(patch.object(main, "_resolve_terms", return_value=(["dependency"], [])))
            stack.enter_context(patch.object(main, "_inject_exact", side_effect=lambda rows, *a: rows))
            stack.enter_context(patch.object(main, "_hydrate_search_payload", side_effect=lambda p: p))
            stack.enter_context(patch.object(main.requests, "post", side_effect=(
                main.requests.Timeout("batch failed") if failure else self.post)))
            if trace:
                from eval.trace_shortlist_diversity import trace_search
                return trace_search(main, body)
            return main._run_search(body)

    def test_real_hierarchy_retains_qualified_source_but_not_similar_word_negative(self):
        payload = self.search()
        self.assertEqual({row["arxiv_id"] for row in payload["results"]},
                         {"collector", "dependency-method"})
        self.assertNotIn("source_candidates", payload)
        self.assertFalse(payload.get("partial", False))
        self.assertGreaterEqual(payload["retrieval_summary"]["observed_candidate_count"], 1)

    def test_strict_scores_are_unchanged_and_discovery_is_not_evidence(self):
        payload = self.search(strict=True)
        self.assertEqual({row["arxiv_id"] for row in payload["results"]},
                         {"collector", "dependency-method"})
        self.assertTrue(all(row["score"] >= .8 for row in payload["results"]))
        self.assertNotIn("source_candidates", payload)
        self.assertEqual(payload["retrieval_summary"]["observed_candidate_count"], 0)

    def test_batch_failure_is_operationally_partial_and_not_cached(self):
        payload = self.search(failure=True)
        self.assertTrue(payload["partial"])
        self.assertIn("stage_b_diversity", payload["retrieval_warning"])
        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["collector"])
        self.assertEqual(len(main.search_cache.data), 0)
