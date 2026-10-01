import unittest
from unittest.mock import ANY, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api import main
from api.research_bundle import BUNDLE_CHUNK_LIMIT, ResearchBundleBody


class ResearchBundleTests(unittest.TestCase):
    def _run(self, hits=None, points=None, **kwargs):
        hits = hits if hits is not None else [{"arxiv_id": "acl:x", "title": "Paper", "score": 0.9}]
        rows = {h["arxiv_id"]: {"fulltext_source": "acl-pdf", "source_url": "https://example.test/p.pdf"} for h in hits}
        points = points if points is not None else [
            {"payload": {"text": "table 123", "element_type": "table", "chunk_index": 2}},
            {"payload": {"text": "x=1", "element_type": "equation", "chunk_index": 0}},
            {"payload": {"text": "Do X", "element_type": "algorithm", "chunk_index": 1}},
            {"payload": {"text": "Limited", "section_type": "limitations", "chunk_index": 3}}]
        with (patch.object(main, "auth_and_limit"),
              patch.object(main, "_run_search", return_value={"results": hits, "partial": kwargs.pop("partial", False)}) as search,
              patch.object(main, "_paper_rows", return_value=rows) as metadata,
              patch.object(main, "qdrant_scroll_by_arxiv", return_value=points) as scroll):
            result = main.research_bundle(ResearchBundleBody(query="method", **kwargs))
        return result, search, metadata, scroll

    def test_default_strict_and_single_retrieval_per_unique_paper(self):
        hits = [{"arxiv_id": "acl:x", "title": "Paper", "score": 0.9}] * 3
        out, search, metadata, scroll = self._run(hits, limit=2)
        self.assertTrue(search.call_args.args[0].strict)
        self.assertEqual(len(out["papers"]), 1)
        metadata.assert_called_once_with(["acl:x"])
        scroll.assert_called_once_with("acl:x", limit=BUNDLE_CHUNK_LIMIT, page=BUNDLE_CHUNK_LIMIT,
                                       timeout=3, deadline=ANY)
        for category in ("equations", "algorithms", "tables", "limitations"):
            self.assertEqual(len(out[category]), 1)
            self.assertFalse(out[category][0]["evidence"]["exact_latex"])
        self.assertFalse(out["generated"])

    def test_deterministic_order_and_character_budget(self):
        points = [{"payload": {"text": "y" * 700, "element_type": "table", "chunk_index": 9}},
                  {"payload": {"text": "x" * 300, "element_type": "equation", "chunk_index": 2}}]
        first, *_ = self._run(points=points, max_chars=500)
        second, *_ = self._run(points=list(reversed(points)), max_chars=500)
        self.assertEqual(first, second)
        self.assertEqual(first["budgets"]["chars_returned"], 500)
        self.assertTrue(first["partial"])
        table = first["tables"][0]
        self.assertEqual(table["evidence"]["fragment_chars"], 700)
        self.assertEqual(table["evidence"]["returned_chars"], 250)
        self.assertTrue(table["clipped"])

    def test_equation_flood_cannot_bury_limitations_or_second_paper(self):
        hits = [{"arxiv_id": pid, "title": pid, "fulltext": True,
                 "evidence": {"acquisition_type": "latex"}} for pid in ("p1", "p2")]
        flood = [{"payload": {"text": f"equation {i} " + "x" * 3000, "element_type": "equation", "chunk_index": i}}
                 for i in range(10)]
        flood.append({"payload": {"text": "Important limitation", "section_type": "limitations", "chunk_index": 99}})
        second = [{"payload": {"text": "Second paper algorithm", "element_type": "algorithm"}}]
        with (patch.object(main, "auth_and_limit"),
              patch.object(main, "_run_search", return_value={"results": hits}),
              patch.object(main, "qdrant_scroll_by_arxiv", side_effect=[flood, second]) as scroll):
            out = main.research_bundle(ResearchBundleBody(query="q", limit=2, max_chars=12000))
        self.assertLessEqual(out["budgets"]["chars_returned"], 12000)
        self.assertEqual(out["limitations"][0]["text"], "Important limitation")
        self.assertEqual(out["algorithms"][0]["paper_id"], "p2")
        self.assertEqual(scroll.call_count, 2)
        self.assertTrue(out["partial"])

    def test_bounds(self):
        for kwargs in ({"limit": 6}, {"limit": 0}, {"max_chars": 499}, {"max_chars": 20001}):
            with self.assertRaises(ValidationError):
                ResearchBundleBody(query="method", **kwargs)

    def test_code_only_protocol_specs_return_separate_code_evidence(self):
        for pid in ("eip:4337", "simd:0297"):
            hit = {"arxiv_id": pid, "title": "Spec", "score": 0.9, "fulltext": True,
                   "completeness": "fulltext_acquired",
                   "evidence": {"acquisition_type": "spec-markdown", "extraction_type": "markdown_source"}}
            points = [{"payload": {"arxiv_id": pid, "text": "function validateUserOp() {}",
                                   "element_type": "code", "section_title": "Architecture"}}]
            out, *_ = self._run(hits=[hit], points=points)
            self.assertEqual(len(out["code"]), 1)
            self.assertEqual(out["code"][0]["paper_id"], pid)
            self.assertEqual(out["code"][0]["evidence"]["extraction_type"], "markdown_source")
            self.assertEqual(out["algorithms"], [])
            self.assertFalse(out["missing"]["code"])
            self.assertNotIn("code", out["papers"][0]["missing"])

    def test_partial_search_stays_partial(self):
        out, *_ = self._run(partial=True)
        self.assertTrue(out["partial"])
        self.assertTrue(out["search_partial"])

    def test_empty_search_has_no_retrieval_and_explicit_missing(self):
        out, _, metadata, scroll = self._run(hits=[])
        metadata.assert_not_called()
        scroll.assert_not_called()
        self.assertTrue(out["missing"]["papers"])
        self.assertEqual(out["budgets"]["chars_returned"], 0)

    def test_abstract_missing_is_not_full_document_absence(self):
        hit = {"arxiv_id": "oa:1", "title": "Abstract", "score": 0.9,
               "fulltext": False, "completeness": "abstract_only",
               "evidence": {"acquisition_type": "openalex-abstract", "extraction_type": "abstract_text"}}
        out, _, metadata, _ = self._run(hits=[hit], points=[])
        metadata.assert_not_called()
        self.assertTrue(out["partial"])
        self.assertEqual(out["papers"][0]["missing"]["equations"], "abstract_only")

    def test_retrieval_failure_and_chunk_cap_are_partial(self):
        for response in (HTTPException(502, "failed"), [{"payload": {}}] * BUNDLE_CHUNK_LIMIT):
            with (patch.object(main, "auth_and_limit"),
                  patch.object(main, "_run_search", return_value={"results": [{"arxiv_id": "1", "fulltext": True, "evidence": {}}]}),
                  patch.object(main, "qdrant_scroll_by_arxiv", side_effect=response if isinstance(response, Exception) else None,
                               return_value=response if isinstance(response, list) else None)):
                out = main.research_bundle(ResearchBundleBody(query="q"))
            self.assertTrue(out["partial"])

    def test_post_route_and_validation_without_startup_or_live_io(self):
        client = TestClient(main.app)
        with (patch.object(main, "auth_and_limit"),
              patch.object(main, "_run_search", return_value={"results": []}) as search):
            response = client.post("/v1/research/bundle", json={"query": "method"})
            bad = client.post("/v1/research/bundle", json={"query": "method", "limit": 6})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(search.call_count, 1)

    def test_five_retrievals_have_bounded_timeouts_without_waiting(self):
        hits = [{"arxiv_id": str(i), "fulltext": True, "evidence": {}} for i in range(5)]
        with (patch.object(main, "auth_and_limit"),
              patch.object(main, "_run_search", return_value={"results": hits}),
              patch.object(main.time, "monotonic", side_effect=[0, 4, 7, 10, 13, 16]),
              patch.object(main, "qdrant_scroll_by_arxiv", return_value=[]) as scroll):
            out = main.research_bundle(ResearchBundleBody(query="q", limit=5))
        timeouts = [call.kwargs["timeout"] for call in scroll.call_args_list]
        self.assertEqual(timeouts, [3] * 5)
        self.assertLessEqual(sum(timeouts), 15)
        self.assertEqual(out["budgets"]["paper_retrieval_calls"], 5)

    def test_deadline_includes_search_and_stops_all_later_requests(self):
        hits = [{"arxiv_id": str(i), "fulltext": True, "evidence": {}} for i in range(5)]
        for ticks, expected_timeouts in (([0, 18, 20, 21, 22, 23], [2]),
                                         ([0, 20, 21, 22, 23, 24], [])):
            with (patch.object(main, "auth_and_limit"),
                  patch.object(main, "_run_search", return_value={"results": hits}),
                  patch.object(main.time, "monotonic", side_effect=ticks),
                  patch.object(main, "qdrant_scroll_by_arxiv", return_value=[]) as scroll):
                out = main.research_bundle(ResearchBundleBody(query="q", limit=5))
            self.assertEqual([call.kwargs["timeout"] for call in scroll.call_args_list], expected_timeouts)
            self.assertTrue(out["partial"])
            self.assertEqual(out["papers"][-1]["missing"]["code"], "time_budget")
            self.assertEqual(out["budgets"]["paper_retrieval_calls"], len(expected_timeouts))

    def test_scroll_default_timeout_remains_30_and_override_is_forwarded(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": {"points": [], "next_page_offset": None}}

        with patch.object(main.requests, "post", return_value=Response()) as post:
            main.qdrant_scroll_by_arxiv("1")
            self.assertEqual(post.call_args.kwargs["timeout"], 30)
            main.qdrant_scroll_by_arxiv("1", timeout=2)
            self.assertEqual(post.call_args.kwargs["timeout"], 2)

    def test_paginated_scroll_cannot_start_another_request_after_deadline(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": {"points": [{"payload": {}}], "next_page_offset": "next"}}

        for deadline, ticks in ((20, [0, 0, 3]), (2, [0, 0, 2])):
            with (patch.object(main.requests, "post", return_value=Response()) as post,
                  patch.object(main.time, "monotonic", side_effect=ticks)):
                with self.assertRaises(TimeoutError):
                    main.qdrant_scroll_by_arxiv("1", limit=200, page=200, timeout=3, deadline=deadline)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["timeout"], min(3, deadline))


if __name__ == "__main__":
    unittest.main()
