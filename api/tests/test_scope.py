import unittest
from unittest.mock import patch

from api import main
from api.tests.test_explore import ROWS, TWINS

IN_SCOPE = ["MEV", "maximal extractable value", "account abstraction", "KV cache",
            "coding agents", "durable nonce", "GRPO", "LLM agent for CRISPR design"]
OUT_OF_SCOPE = ["CRISPR gene editing", "AlphaFold", "perovskite solar cells",
                "football tactics", "sourdough bread", "dark matter MeV photons"]


class QueryScopeTest(unittest.TestCase):
    def setUp(self):
        main._scope_cache.data.clear()

    def test_indexed_topics_are_in_scope_without_a_probe(self):
        with patch.object(main, "_scope_dense_probe") as probe:
            for q in IN_SCOPE:
                got = main._query_scope(q)
                self.assertTrue(got["in_scope"], q)
                self.assertIsNone(got["note"], q)
            probe.assert_not_called()

    def test_foreign_domains_are_out_with_a_note(self):
        with patch.object(main, "_scope_dense_probe", return_value=None):
            for q in OUT_OF_SCOPE:
                got = main._query_scope(q)
                self.assertFalse(got["in_scope"], q)
                self.assertIn("looks outside", got["note"])
                self.assertIn("web3, AI agents, LLMs and developer tooling", got["note"])

    def test_mev_is_case_aware(self):
        with patch.object(main, "_scope_dense_probe", return_value=None):
            self.assertTrue(main._query_scope("MEV")["in_scope"])
            self.assertFalse(main._query_scope("MeV photons")["in_scope"])

    def test_dense_probe_decides_unlisted_subjects(self):
        with patch.object(main, "_scope_dense_probe", return_value=0.52):
            far = main._query_scope("medieval pottery glazes")
        # distance alone is never "out": web3 topics with a thin shelf sit there too
        self.assertIsNone(far["in_scope"])
        self.assertEqual(far["nearest_similarity"], 0.52)
        self.assertIn("thin", far["note"])
        with patch.object(main, "_scope_dense_probe", return_value=0.75):
            near = main._query_scope("sybil resistance in markets")
        self.assertTrue(near["in_scope"])
        self.assertEqual(near["confidence"], "low")

    def test_unmeasurable_query_is_not_flagged(self):
        with patch.object(main, "_scope_dense_probe", return_value=None):
            got = main._query_scope("medieval pottery glazes")
        self.assertTrue(got["in_scope"])
        self.assertEqual(got["reason"], "not measured")

    def test_result_is_cached_and_callers_cannot_corrupt_it(self):
        with patch.object(main, "_scope_dense_probe", return_value=0.5) as probe:
            first = main._query_scope("medieval pottery glazes")
            first["in_scope"] = True
            second = main._query_scope("medieval pottery glazes")
        self.assertEqual(probe.call_count, 1)
        self.assertFalse(second["in_scope"])


class ScopeInEndpointsTest(unittest.TestCase):
    def setUp(self):
        main._scope_cache.data.clear()
        main.search_cache.data.clear()

    def _search(self, query):
        payload = {"results": [], "count": 0, "usage": "u"}
        body = main.SearchBody(query=query)
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_run_search", return_value=payload) as run, \
             patch.object(main, "_scope_dense_probe", return_value=None):
            return main.search(body), payload, run

    def test_search_carries_scope_and_keeps_results(self):
        out, _, _ = self._search("CRISPR gene editing")
        self.assertFalse(out["scope"]["in_scope"])
        self.assertEqual(out["count"], 0)
        out, _, _ = self._search("MEV")
        self.assertTrue(out["scope"]["in_scope"])

    def test_search_does_not_write_scope_into_the_cached_payload(self):
        out, payload, _ = self._search("AlphaFold")
        self.assertNotIn("scope", payload)
        self.assertIsNot(out, payload)

    def test_real_search_cache_still_serves_second_call(self):
        body = main.SearchBody(query="AlphaFold")
        cached = {"results": [{"arxiv_id": "x"}], "count": 1, "usage": "u"}
        key = ("AlphaFold", None, None, None, (), True, None, None, None, 8, True)
        main.search_cache.set(key, cached)
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_scope_dense_probe", return_value=None):
            out = main.search(body)
        self.assertEqual(out["results"], [{"arxiv_id": "x"}])
        self.assertFalse(out["scope"]["in_scope"])
        self.assertNotIn("scope", cached)

    def test_compact_search_gets_scope_too(self):
        payload = {"results": [], "count": 0, "usage": "u", "licenses": {}}
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_run_search", return_value=payload), \
             patch.object(main, "_scope_dense_probe", return_value=None):
            out = main.search(main.SearchBody(query="football tactics", compact=True))
        self.assertFalse(out["scope"]["in_scope"])

    def test_papers_carries_scope(self):
        pool = [dict(ROWS["2403.02691"], relevance_rank=1)]
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_rank_papers", return_value=(pool, None)), \
             patch.object(main, "_twins", return_value=TWINS), \
             patch.object(main, "_scope_dense_probe", return_value=None):
            bad = main.find_papers(main.PapersBody(query="sourdough bread"))
            good = main.find_papers(main.PapersBody(query="prompt injection"))
        self.assertFalse(bad["scope"]["in_scope"])
        self.assertEqual(len(bad["papers"]), 1)
        self.assertTrue(good["scope"]["in_scope"])

    def test_facets_carry_scope_with_and_without_query(self):
        rows = [("2403.02691", 2024, "ACL", "ai-agents", "[]")]
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_facet_source", return_value=rows), \
             patch.object(main, "_rank_papers", return_value=([dict(ROWS["2403.02691"])], None)), \
             patch.object(main, "_twins", return_value=TWINS), \
             patch.object(main, "_scope_dense_probe", return_value=None):
            q = main.paper_facets(main.FacetsBody(query="perovskite solar cells"))
            none = main.paper_facets(main.FacetsBody(layer="web3"))
        self.assertFalse(q["scope"]["in_scope"])
        self.assertIn("counted_over", q)
        self.assertTrue(none["scope"]["in_scope"])


class DenseProbeTest(unittest.TestCase):
    def test_probe_is_off_without_the_coarse_index_and_survives_errors(self):
        with patch.object(main, "COARSE_EMBED_URL", ""):
            self.assertIsNone(main._scope_dense_probe("x"))
        with patch.object(main, "COARSE_EMBED_URL", "http://e"), \
             patch.object(main, "HIER_SEARCH", True), \
             patch.object(main, "embed_query_coarse", side_effect=main.HTTPException(502, "x")):
            self.assertIsNone(main._scope_dense_probe("x"))

    def test_probe_averages_the_five_nearest(self):
        hits = [{"score": s} for s in (0.9, 0.8, 0.7, 0.6, 0.5, 0.1)]
        with patch.object(main, "COARSE_EMBED_URL", "http://e"), \
             patch.object(main, "HIER_SEARCH", True), \
             patch.object(main, "embed_query_coarse", return_value=[0.0]), \
             patch.object(main, "qdrant_search", return_value=hits):
            self.assertAlmostEqual(main._scope_dense_probe("x"), 0.7)


if __name__ == "__main__":
    unittest.main()
