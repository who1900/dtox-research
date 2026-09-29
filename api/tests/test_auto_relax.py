"""Auto-relaxing hard filters, closed-vocabulary term matching and the cap on
gh: framework documentation in /v1/search. All mocked: no network, no DBs."""
import unittest
from unittest.mock import patch

from api import main


def _hit(aid, score=0.9, **extra):
    payload = {"arxiv_id": aid, "title": f"Title {aid}", "section_type": "method"}
    payload.update(extra)
    return {"score": score, "payload": payload}


class _Base(unittest.TestCase):
    def setUp(self):
        main.search_cache._data.clear() if hasattr(main.search_cache, "_data") else None
        self.calls = []

    def run_search(self, body, dense):
        """dense(qfilter) -> list of hits; records every Qdrant filter used."""
        def fake_dense(vector, qfilter, dense_limit, limit, timeout, layers=None):
            self.calls.append(qfilter)
            return dense(qfilter), [], [], []
        with (patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "HIER_SEARCH", False),
              patch.object(main, "PAPER_LEXICAL", False),
              patch.object(main, "two_phase_dense_search", side_effect=fake_dense),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_lookup_note", return_value=None)):
            return main._run_search(body)

    @staticmethod
    def keys(qfilter):
        return {c["key"] for c in (qfilter or {}).get("must", [])}


class TermNormalisation(unittest.TestCase):
    def test_case_hyphen_space_and_plural_are_equivalent(self):
        for raw in ("KV Cache", "kv-cache", "kv caches", "  KV_cache "):
            self.assertEqual(main._resolve_terms([raw]), (["kv cache"], []), raw)

    def test_one_term_expands_to_every_stored_spelling(self):
        known, unknown = main._resolve_terms(["Chain of Thoughts"])
        self.assertEqual(unknown, [])
        self.assertIn("chain-of-thought", known)
        self.assertIn("chain of thought", known)

    def test_unknown_terms_are_reported_not_silently_kept(self):
        known, unknown = main._resolve_terms(["EIP-1559", "rollups", "PDA"])
        self.assertEqual(known, ["rollup"])
        self.assertEqual(unknown, ["EIP-1559", "PDA"])

    def test_no_vocabulary_passes_terms_through_lowercased(self):
        with patch.object(main, "_TERM_VOCAB", {}):
            self.assertEqual(main._resolve_terms(["EIP-1559"]), (["eip-1559"], []))


class TermsInSearch(_Base):
    def test_unknown_term_is_dropped_from_filter_and_named(self):
        body = main.SearchBody(query="nt-unknown-term", terms=["EIP-1559"], limit=3)
        out = self.run_search(body, lambda f: [_hit("1"), _hit("2"), _hit("3")])
        self.assertNotIn("terms", self.keys(self.calls[0]))
        self.assertEqual(out["terms_resolution"]["unknown"], ["EIP-1559"])
        self.assertNotIn("relaxed", out)
        self.assertEqual(out["count"], 3)

    def test_plural_term_reaches_the_filter_in_stored_form(self):
        body = main.SearchBody(query="nt-plural", terms=["Rollups"], limit=3)
        self.run_search(body, lambda f: [_hit("1"), _hit("2"), _hit("3")])
        terms_clause = [c for c in self.calls[0]["must"] if c["key"] == "terms"][0]
        self.assertEqual(terms_clause["match"]["any"], ["rollup"])


class AutoRelax(_Base):
    def dense(self, blocked):
        """Filters named in `blocked` produce nothing; otherwise 5 hits."""
        def fn(qfilter):
            if self.keys(qfilter) & blocked:
                return []
            return [_hit(str(i)) for i in range(5)]
        return fn

    def test_empty_terms_search_is_relaxed_and_flagged(self):
        body = main.SearchBody(query="ar-terms", terms=["rollup"], limit=5)
        out = self.run_search(body, self.dense({"terms"}))
        self.assertEqual(out["count"], 5)
        self.assertEqual(out["relaxed"]["dropped"], ["terms"])
        self.assertEqual(out["relaxed"]["exact_matches"], 0)
        self.assertTrue(out["relaxed"]["note"].startswith("No result matched terms="))
        self.assertTrue(all(r["matches_filters"] is False for r in out["results"]))
        self.assertEqual(len(self.calls), 2)          # strict + one relaxed
        self.assertNotIn("why_empty", out)

    def test_order_is_terms_then_section_then_element_then_year(self):
        body = main.SearchBody(query="ar-order", terms=["rollup"], section_type="method",
                               element_type="prose", year_from=2020, limit=5)
        out = self.run_search(body, self.dense({"terms", "section_type", "element_type", "year"}))
        # first extra search drops terms only, the second drops everything left
        self.assertEqual(self.keys(self.calls[1]), {"section_type", "element_type", "year"})
        self.assertEqual(self.keys(self.calls[2]), set())
        self.assertEqual(out["relaxed"]["dropped"],
                         ["terms", "section_type", "element_type", "year"])

    def test_at_most_two_extra_searches(self):
        body = main.SearchBody(query="ar-limit", terms=["rollup"], section_type="method",
                               element_type="prose", year_from=2020, limit=5)
        out = self.run_search(body, lambda f: [])
        self.assertLessEqual(len(self.calls), 3)
        self.assertNotIn("relaxed", out)
        self.assertEqual(out["count"], 0)

    def test_stops_at_first_relaxation_that_works(self):
        body = main.SearchBody(query="ar-first", terms=["rollup"], section_type="method", limit=5)
        out = self.run_search(body, self.dense({"terms"}))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(out["relaxed"]["dropped"], ["terms"])

    def test_partial_matches_stay_first_and_are_topped_up(self):
        def dense(qfilter):
            if "terms" in self.keys(qfilter):
                return [_hit("exact")]
            return [_hit("exact"), _hit("a"), _hit("b"), _hit("c")]
        body = main.SearchBody(query="ar-partial", terms=["rollup"], limit=5)
        out = self.run_search(body, dense)
        ids = [r["arxiv_id"] for r in out["results"]]
        self.assertEqual(ids, ["exact", "a", "b", "c"])
        self.assertTrue(out["results"][0]["matches_filters"])
        self.assertFalse(out["results"][1]["matches_filters"])
        self.assertEqual(out["relaxed"]["exact_matches"], 1)
        self.assertIn("Only 1 result matched", out["relaxed"]["note"])

    def test_enough_results_means_no_relaxing(self):
        body = main.SearchBody(query="ar-enough", terms=["rollup"], limit=5)
        out = self.run_search(body, self.dense(set()))
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("relaxed", out)

    def test_flag_off_keeps_strict_behaviour_and_why_empty(self):
        body = main.SearchBody(query="ar-off", terms=["rollup"], limit=5)
        with patch.object(main, "AUTO_RELAX", False):
            out = self.run_search(body, self.dense({"terms"}))
        self.assertEqual(out["count"], 0)
        self.assertNotIn("relaxed", out)
        self.assertEqual(out["why_empty"]["relax_one_of"][0]["drop"], "terms")

    def test_per_request_opt_out(self):
        body = main.SearchBody(query="ar-optout", terms=["rollup"], limit=5, auto_relax=False)
        out = self.run_search(body, self.dense({"terms"}))
        self.assertEqual(out["count"], 0)
        self.assertNotIn("relaxed", out)

    def test_layer_is_never_relaxed(self):
        body = main.SearchBody(query="ar-layer", layer="web3", limit=5, diagnose=False)
        out = self.run_search(body, lambda f: [])
        self.assertNotIn("relaxed", out)
        self.assertEqual(len(self.calls), 1)

    def test_diagnose_probes_run_only_for_unrelaxable_filters(self):
        body = main.SearchBody(query="ar-diag", terms=["rollup"], min_score=0.99, limit=5)
        out = self.run_search(body, lambda f: [])
        self.assertEqual(out["count"], 0)
        self.assertNotIn("terms", [r["drop"] for r in out["why_empty"]["relax_one_of"]])


class GhDocCap(unittest.TestCase):
    def results(self, ids):
        return [{"arxiv_id": i} for i in ids]

    def test_caps_framework_docs_to_two_of_ten(self):
        ids = [f"gh:anchor:{n}" for n in range(6)] + ["1", "2"]
        out, dropped = main._cap_gh_docs(self.results(ids), "how do PDAs work", 10)
        self.assertEqual([r["arxiv_id"] for r in out].count("1"), 1)
        self.assertEqual(sum(r["arxiv_id"].startswith("gh:") for r in out), 2)
        self.assertEqual(dropped, 4)

    def test_named_framework_is_not_capped(self):
        ids = [f"gh:anchor:{n}" for n in range(6)]
        out, dropped = main._cap_gh_docs(self.results(ids), "Anchor account constraints", 10)
        self.assertEqual((len(out), dropped), (6, 0))

    def test_cap_counts_only_unnamed_projects(self):
        ids = ["gh:anchor:1", "gh:anchor:2", "gh:anchor:3", "gh:jupiter:1", "gh:jupiter:2", "gh:jupiter:3"]
        out, dropped = main._cap_gh_docs(self.results(ids), "jupiter swap routing", 10)
        self.assertEqual([r["arxiv_id"] for r in out],
                         ["gh:anchor:1", "gh:anchor:2", "gh:jupiter:1", "gh:jupiter:2", "gh:jupiter:3"])
        self.assertEqual(dropped, 1)

    def test_cap_scales_with_limit_and_can_be_disabled(self):
        ids = [f"gh:anchor:{n}" for n in range(8)]
        self.assertEqual(len(main._cap_gh_docs(self.results(ids), "pda", 20)[0]), 4)
        with patch.object(main, "GH_DOC_CAP", 0):
            self.assertEqual(len(main._cap_gh_docs(self.results(ids), "pda", 10)[0]), 8)

    def test_papers_are_untouched(self):
        ids = ["1", "2", "3", "eip:1559", "simd:0085"]
        out, dropped = main._cap_gh_docs(self.results(ids), "fees", 10)
        self.assertEqual((len(out), dropped), (5, 0))

    def test_generic_word_does_not_name_a_project(self):
        self.assertFalse(main._query_names_gh_source("consensus in blockchains", "eth-consensus"))
        self.assertTrue(main._query_names_gh_source("beacon chain finality", "eth-consensus"))


if __name__ == "__main__":
    unittest.main()
