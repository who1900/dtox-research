import unittest
import importlib.util
from contextlib import ExitStack
from unittest.mock import patch

if importlib.util.find_spec("httpx") is None:
    TestClient = None
else:
    from fastapi.testclient import TestClient

from api import main


def _hit(paper_id, score, channel="paper"):
    return {"id": paper_id, "score": score, "payload": {
        "arxiv_id": paper_id, "title": f"Title {paper_id}",
        "text": "A substantive indexed fragment.", "section_type": "method",
        "section_title": "Methods", "layers": ["llm-slm"],
    }}


class EmptySearchFallbackTests(unittest.TestCase):
    def setUp(self):
        main.search_cache.data.clear()
        main.qdrant_result_cache.data.clear()

    def _run(self, *, strict=False, min_score=None, partial=False, discovery=False,
             hits_override=None, **body_fields):
        hits = hits_override or [_hit(f"candidate-{i}", 0.70 - i * 0.01) for i in range(6)]
        extra = {
            "paper_scores": {f"candidate-{i}": 0.68 - i * 0.01 for i in range(6)},
            "found_via": {f"candidate-{i}": "paper graph" for i in range(6)},
            "fusion_ranks": {f"candidate-{i}": {"graph": i + 1} for i in range(6)},
            "fusion_scores": {f"candidate-{i}": 0.02 - i * 0.001 for i in range(6)},
            "global_channel": "used", "global_extra": [],
        }
        body = main.SearchBody(
            query="saving computer memory when processing very long texts by throwing away unimportant previous parts",
            layer="llm-slm", hybrid=True, dedupe=True, strict=strict,
            min_score=min_score, limit=10, diagnose=False, auto_relax=False,
            **body_fields)
        with (patch.object(main, "SOURCE_DISCOVERY", discovery),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main.requests, "post",
                           side_effect=AssertionError("unexpected HTTP request")),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "_hier_run", return_value=(
                  hits, [], ["global"] if partial else [], extra)) as hier,
              patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_hydrate_evidence",
                           side_effect=lambda rows, evidence_rows=None: rows),
              patch.object(main, "_evidence_rows", return_value={}),
              patch.object(main, "_inject_exact", side_effect=lambda rows, *args: rows),
              patch.object(main, "_lookup_note", return_value=None)):
            payload = main._run_search(body)
        return payload, hier

    def _run_soft_partner(self, partner_mode):
        candidates = [{"arxiv_id": f"requested-lead-{index}", "score": 0.2,
                       "retrieval_role": "source_candidate", "verification_required": True}
                      for index in range(3)]
        partner_hit = {"arxiv_id": "partner-primary", "score": 0.95,
                       "title": "Partner primary", "text": "Substantive result."}
        requested_payload = {
            "results": [], "count": 0, "source_candidates": candidates,
            "source_candidate_count": len(candidates), "discovery_note": "fixture leads",
            "retrieval_summary": {"auxiliary_candidate_count": len(candidates),
                                  "source_candidates_returned_count": len(candidates)},
        }
        partner_payload = {"results": [partner_hit], "count": 1,
                           "retrieval_summary": {"returned_unique_count": 1}}
        real_run_search = main._run_search

        def run_search(*args, **kwargs):
            top = kwargs.get("_top", args[5] if len(args) > 5 else True)
            if top:
                return real_run_search(*args, **kwargs)
            body = args[0]
            if body.layer == "ai-agents":
                return requested_payload
            if partner_mode == "failure":
                raise main.HTTPException(status_code=503, detail="fixture retrieval failure")
            if partner_mode == "primary":
                return partner_payload
            return {"results": [], "count": 0,
                    "retrieval_summary": {"returned_unique_count": 0}}

        body = main.SearchBody(query=f"soft partner fixture {partner_mode}",
                               layer="ai-agents", hybrid=True, dedupe=True,
                               limit=4, diagnose=False, auto_relax=False)
        with (patch.object(main, "SOURCE_DISCOVERY", False),
              patch.object(main, "_soft_partner", return_value="llm-slm"),
              patch.object(main, "_run_search", side_effect=run_search),
              patch.object(main.requests, "post",
                           side_effect=AssertionError("unexpected HTTP request")),
              patch.object(main, "_resolve_terms", return_value=([], [])),
              patch.object(main, "_hydrate_search_payload"),
              patch.object(main, "_inject_exact", side_effect=lambda rows, *args: rows)):
            return main._run_search(body)

    def test_empty_primary_has_at_most_three_separate_unverified_candidates(self):
        payload, hier = self._run()
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["count"], 0)
        self.assertFalse(payload.get("partial", False))
        self.assertEqual(len(payload["source_candidates"]), 3)
        self.assertEqual(payload["source_candidate_count"], 3)
        self.assertEqual(payload["retrieval_summary"]["auxiliary_candidate_count"], 3)
        self.assertTrue(all(row["retrieval_role"] == "source_candidate"
                            and row["verification_required"]
                            and row["candidate_reason"] == "related_source_after_empty_primary"
                            for row in payload["source_candidates"]))
        self.assertTrue(all(row["found_via"] == "paper graph"
                            and "graph" in row["channels"]
                            for row in payload["source_candidates"]))
        self.assertIn("not relevance-verified evidence", payload["discovery_note"])
        self.assertEqual(hier.call_count, 1)

    def test_fallback_uses_only_finite_nonboilerplate_rows_and_keeps_active_facets(self):
        body = main.SearchBody(query="facet fixture", layer="llm-slm",
                               year_from=2020, year_to=2023, terms=["cache"],
                               section_type="method", element_type="prose")

        def row(paper_id, score=0.1, **changes):
            value = {"arxiv_id": paper_id, "score": score, "title": paper_id,
                     "text": "Substantive content", "layers": ["llm-slm"],
                     "year": 2022, "terms": ["cache"], "section_type": "method",
                     "element_type": "prose"}
            value.update(changes)
            return value

        rows = [row("valid-low-score"), row("nan", float("nan")),
                row("infinite", float("inf")), row("boolean", True),
                row("oversized-number", 10 ** 1000),
                row("boilerplate", text="References", section_title="References",
                    section_type="references"),
                row("wrong-year", year=2019), row("unknown-year", year=None),
                row("wrong-term", terms=["other"]), row("missing-terms", terms=None),
                row("wrong-section", section_type="results"),
                row("wrong-element", element_type="equation"),
                row("wrong-layer", layers=["web3"])]
        candidates = main._empty_primary_source_candidates(rows, 10, 0.79, body, ["cache"])
        self.assertEqual([item["arxiv_id"] for item in candidates], ["valid-low-score"])
        self.assertFalse(candidates[0]["passes_relevance_floor"])
        self.assertEqual(candidates[0]["relevance_floor"], 0.79)
        self.assertTrue(candidates[0]["verification_required"])
        self.assertNotIn("evidence", candidates[0]["candidate_reason"])

    def test_aliases_are_canonical_deduped_and_nonempty_primary_order_is_unchanged(self):
        body = main.SearchBody(query="alias fixture", layer="llm-slm")
        rows = [_hit("alias-a", 0.2)["payload"] | {"score": 0.2},
                _hit("alias-b", 0.1)["payload"] | {"score": 0.1}]
        with patch.object(main, "canonical_id",
                          side_effect=lambda pid: "canonical" if pid.startswith("alias-") else pid):
            candidates = main._empty_primary_source_candidates(rows, 10, 0.79, body, [])
        self.assertEqual(len(candidates), 1)

        primary, _ = self._run(hits_override=[
            _hit("primary-a", 0.93), _hit("weak-candidate", 0.3),
            _hit("primary-b", 0.90),
        ])
        self.assertEqual([row["arxiv_id"] for row in primary["results"]],
                         ["primary-a", "primary-b"])
        self.assertEqual(primary["count"], 2)
        self.assertNotIn("source_candidates", primary)

    def test_candidate_helper_uses_production_legacy_selector_signature(self):
        body = main.SearchBody(query="signature fixture", layer="llm-slm")
        rows = [_hit(f"candidate-{index}", 0.1 - index * 0.001)["payload"] |
                {"score": 0.1 - index * 0.001} for index in range(6)]
        production_selector = main.select_source_candidates
        observed_limits = []

        def legacy_selector(hits, primary_results, limit, floor, canonical_id,
                            result_from_hit, is_boilerplate, fusion_ranks=None):
            observed_limits.append(limit)
            return production_selector(hits, primary_results, limit, floor, canonical_id,
                                       result_from_hit, is_boilerplate, fusion_ranks)

        with patch.object(main, "select_source_candidates", side_effect=legacy_selector):
            candidates = main._empty_primary_source_candidates(rows, 10, 0.79, body, [])
        self.assertEqual(observed_limits, [3])
        self.assertEqual(len(candidates), 3)

    def test_out_of_scope_search_hides_auxiliary_candidates_and_uncertain_scope_warns(self):
        body = main.SearchBody(query="scope fixture")
        payload = {"results": [], "count": 0,
                   "source_candidates": [{"arxiv_id": "lead"}],
                   "source_candidate_count": 1,
                   "discovery_note": "Inspect leads.",
                   "retrieval_summary": {"auxiliary_candidate_count": 1,
                                         "source_candidates_returned_count": 1}}
        with (patch.object(main, "auth_and_limit"),
              patch.object(main, "_run_search", return_value=payload),
              patch.object(main, "_query_scope", return_value={"in_scope": False})):
            out = main.search(body)
        self.assertNotIn("source_candidates", out)
        self.assertEqual(out["retrieval_summary"]["auxiliary_candidate_count"], 0)
        self.assertEqual(payload["retrieval_summary"]["auxiliary_candidate_count"], 1)

        with (patch.object(main, "auth_and_limit"),
              patch.object(main, "_run_search", return_value=payload),
              patch.object(main, "_query_scope", return_value={"in_scope": None})):
            out = main.search(body)
        self.assertIn("source_candidates", out)
        self.assertIn("scope is uncertain", out["discovery_note"])

    def test_empty_fallback_is_off_for_strict_explicit_floor_partial_and_flagged_mode(self):
        cases = (
            {"strict": True}, {"min_score": 0.95}, {"partial": True},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                payload, _ = self._run(**fields)
                self.assertNotIn("source_candidates", payload)
                self.assertEqual(payload["results"], [])
                self.assertEqual(payload["count"], 0)

    def test_existing_discovery_flag_keeps_its_separate_candidate_contract(self):
        payload, _ = self._run(discovery=True)
        self.assertTrue(payload["source_candidates"])
        self.assertTrue(all(row["candidate_reason"] != "related_source_after_empty_primary"
                            for row in payload["source_candidates"]))

    def test_soft_partner_primary_results_suppress_requested_auxiliary_candidates(self):
        payload = self._run_soft_partner("primary")
        self.assertTrue(payload["results"])
        self.assertNotIn("source_candidates", payload)
        self.assertNotIn("source_candidate_count", payload)
        self.assertNotIn("discovery_note", payload)
        self.assertEqual(payload["retrieval_summary"]["auxiliary_candidate_count"], 0)
        self.assertEqual(payload["retrieval_summary"]["source_candidates_returned_count"], 0)

    def test_soft_partner_failure_marks_partial_without_requested_auxiliary_candidates(self):
        payload = self._run_soft_partner("failure")
        self.assertTrue(payload["partial"])
        self.assertNotIn("source_candidates", payload)
        self.assertNotIn("source_candidate_count", payload)
        self.assertNotIn("discovery_note", payload)
        self.assertEqual(payload["retrieval_summary"]["auxiliary_candidate_count"], 0)
        self.assertEqual(payload["retrieval_summary"]["source_candidates_returned_count"], 0)

    def test_soft_partner_both_empty_keeps_only_bounded_requested_candidates(self):
        payload = self._run_soft_partner("empty")
        self.assertEqual(payload["results"], [])
        self.assertFalse(payload.get("partial", False))
        self.assertLessEqual(len(payload.get("source_candidates", [])), 3)


@unittest.skipIf(TestClient is None, "httpx is unavailable for FastAPI TestClient")
class ExactSectionNavigationTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        stack.enter_context(patch.object(main, "auth_and_limit"))
        stack.enter_context(patch.object(
            main.requests, "post", side_effect=AssertionError("live request")))
        self.scroll = stack.enter_context(patch.object(main, "qdrant_scroll_by_arxiv", return_value=[
            {"payload": {"title": "Paper", "section_title": "Methods",
                         "section_type": "method", "chunk_index": 0, "text": "body"}},
            {"payload": {"title": "Paper", "section_title": "Results",
                         "section_type": "experiments", "chunk_index": 1, "text": "results"}},
        ]))

    def test_missing_exact_title_returns_structured_outline_not_substitution(self):
        response = self.client.get("/v1/paper/2401.1/section", params={"title": "Method"})
        self.assertEqual(response.status_code, 404)
        detail = response.json()["detail"]
        self.assertEqual(detail["requested"]["title"], "Method")
        self.assertIn("no semantic substitution", detail["message"])
        self.assertEqual([row["section_title"] for row in detail["available_sections"]],
                         ["Methods", "Results"])
        self.assertIn("exactly", detail["selection_hints"]["exact_title"])

    def test_missing_exact_type_returns_structured_outline_and_unknown_paper_stays_404(self):
        response = self.client.get("/v1/paper/2401.1/section",
                                   params={"section_type": "methodology"})
        self.assertEqual(response.status_code, 404)
        detail = response.json()["detail"]
        self.assertEqual(detail["requested"]["section_type"], "methodology")
        self.assertEqual(len(detail["available_sections"]), 2)

        self.scroll.return_value = []
        unknown = self.client.get("/v1/paper/9999.1/section", params={"title": "Methods"})
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["detail"], "paper not found")


class DirectSectionNavigationTests(unittest.TestCase):
    def test_missing_exact_title_raises_structured_404_without_testclient(self):
        points = [{"payload": {"title": "Paper", "section_title": "Methods",
                               "section_type": "method", "chunk_index": 0, "text": "body"}}]
        with (patch.object(main, "auth_and_limit"),
              patch.object(main, "qdrant_scroll_by_arxiv", return_value=points),
              patch.object(main.requests, "post",
                           side_effect=AssertionError("unexpected HTTP request"))):
            with self.assertRaises(main.HTTPException) as caught:
                main.paper_section("2401.1", title="Method")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail["available_sections"][0]["section_title"],
                         "Methods")


if __name__ == "__main__":
    unittest.main()
