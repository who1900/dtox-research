import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api import main

ROWS = {
    "2403.02691": {"arxiv_id": "2403.02691", "title": "InjecAgent", "year": 2024, "venue": "ACL",
                   "citation_count": 300, "influential_citations": 40, "layers": ["ai-agents"],
                   "abstract": "Indirect prompt injection benchmark. " * 30},
    "2406.13352": {"arxiv_id": "2406.13352", "title": "AgentDojo", "year": 2024, "venue": "NeurIPS",
                   "citation_count": 150, "influential_citations": 20, "layers": ["ai-agents"],
                   "abstract": "Dynamic environment."},
    "2605.17986": {"arxiv_id": "2605.17986", "title": "LivePI", "year": 2026, "venue": None,
                   "citation_count": 2, "influential_citations": 0, "layers": ["ai-agents"],
                   "abstract": "Realistic benchmarking."},
}
TWINS = {"checked": 1e18, "mtime": 1, "members": {"2403.02691": ["2403.02691", "acl:x"]},
         "canonical": {"2403.02691": "2403.02691", "acl:x": "2403.02691"}}


def _find(sort, dense, lexical):
    body = main.PapersBody(query="prompt injection benchmark", sort=sort, limit=10)
    with patch.object(main, "auth_and_limit"), \
         patch.object(main, "COARSE_EMBED_URL", "http://embed"), \
         patch.object(main, "_coarse_paper_search", return_value=[{"arxiv_id": p} for p in dense]), \
         patch.object(main, "_paper_bm25", return_value=lexical), \
         patch.object(main, "_paper_rows", side_effect=lambda ids: {i: dict(ROWS[i]) for i in ids if i in ROWS}), \
         patch.object(main, "_twins", return_value=TWINS):
        return main.find_papers(body)


class FindPapersTest(unittest.TestCase):
    def test_relevance_order_and_one_row_per_paper(self):
        out = _find("relevance", ["2605.17986", "acl:x", "2403.02691"], ["2406.13352"])
        ids = [p["id"] for p in out["papers"]]
        self.assertEqual(ids[0], "2605.17986")
        self.assertEqual(len([i for i in ids if i in ("acl:x", "2403.02691")]), 1)

    def test_citations_resorts_the_relevant_pool(self):
        out = _find("citations", ["2605.17986", "2406.13352", "2403.02691"], [])
        self.assertEqual([p["id"] for p in out["papers"]], ["2403.02691", "2406.13352", "2605.17986"])
        self.assertEqual(out["papers"][0]["relevance_rank"], 3)
        self.assertIn("note_sort", out)

    def test_recent_puts_newest_first(self):
        out = _find("recent", ["2403.02691", "2605.17986"], [])
        self.assertEqual(out["papers"][0]["id"], "2605.17986")

    def test_abstract_is_trimmed_and_twins_named(self):
        out = _find("relevance", ["2403.02691"], [])
        card = out["papers"][0]
        self.assertLessEqual(len(card["abstract"]), main.EXPLORE_ABSTRACT_CHARS + 3)
        self.assertEqual(card["twins"], ["acl:x"])

    def test_foundational_adds_cited_papers_wording_missed(self):
        class Conn:
            def execute(self, sql, params):
                class R:
                    def fetchall(self_inner):
                        return [("1904.05234", 5), ("2406.13352", 3)]
                return R()
        classic = {"arxiv_id": "1904.05234", "title": "Flash Boys 2.0", "year": 2019, "venue": "S&P",
                   "citation_count": 900, "influential_citations": 90, "layers": ["ai-agents"], "abstract": ""}
        rows = dict(ROWS, **{"1904.05234": classic})
        body = main.PapersBody(query="mev", sort="foundational")
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "COARSE_EMBED_URL", "http://embed"), \
             patch.object(main, "_coarse_paper_search", return_value=[{"arxiv_id": "2605.17986"}, {"arxiv_id": "2406.13352"}]), \
             patch.object(main, "_paper_bm25", return_value=[]), \
             patch.object(main, "_paper_rows", side_effect=lambda ids: {i: dict(rows[i]) for i in ids if i in rows}), \
             patch.object(main, "_ro_conn", return_value=Conn()), \
             patch.object(main, "_twins", return_value=TWINS):
            out = main.find_papers(body)
        self.assertEqual([p["id"] for p in out["papers"]][:2], ["1904.05234", "2406.13352"])
        self.assertIsNone(out["papers"][0]["relevance_rank"])
        self.assertEqual(out["papers"][0]["cited_by_pool"], 5)

    def test_bad_sort_and_layer_are_refused(self):
        with patch.object(main, "auth_and_limit"):
            with self.assertRaises(HTTPException):
                main.find_papers(main.PapersBody(query="mev", sort="popular"))
            with self.assertRaises(HTTPException):
                main.find_papers(main.PapersBody(query="mev", layer="biology"))


class OutlineAndSectionTest(unittest.TestCase):
    PAYLOADS = [
        {"chunk_index": 2, "section_title": "Method", "section_type": "method", "element_type": "equation", "text": "E=mc2"},
        {"chunk_index": 0, "section_title": "Introduction", "section_type": "introduction", "text": "Intro text."},
        {"chunk_index": 1, "section_title": "Method", "section_type": "method", "element_type": "prose", "text": "We do X."},
    ]

    def test_outline_is_in_paper_order_with_counts(self):
        outline = main._outline(self.PAYLOADS)
        self.assertEqual([s["section_title"] for s in outline], ["Introduction", "Method"])
        self.assertEqual(outline[1]["chunks"], 2)
        self.assertEqual(outline[1]["elements"], {"prose": 1, "equation": 1})

    def test_outline_needs_no_text_and_reports_no_chars(self):
        bare = [{k: v for k, v in p.items() if k != "text"} for p in self.PAYLOADS]
        outline = main._outline(bare)
        self.assertEqual(outline, main._outline(self.PAYLOADS))
        self.assertTrue(all("chars" not in s for s in outline))

    def test_card_scrolls_metadata_only(self):
        points = [{"payload": {k: v for k, v in p.items() if k != "text"}} for p in self.PAYLOADS]
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_paper_rows", side_effect=lambda ids: {i: dict(ROWS[i]) for i in ids if i in ROWS}), \
             patch.object(main, "_neighbours", return_value=([], 0)), \
             patch.object(main, "qdrant_scroll_by_arxiv", return_value=points) as scroll:
            card = main.paper_card("2403.02691")
        self.assertEqual(scroll.call_args.kwargs["payload_fields"], main.OUTLINE_FIELDS)
        self.assertEqual(scroll.call_args.kwargs["page"], main.OUTLINE_PAGE)
        self.assertEqual(card["indexed_chunks"], 3)
        self.assertEqual([s["section_title"] for s in card["outline"]], ["Introduction", "Method"])

    def test_scroll_asks_qdrant_for_include_list_only_when_narrowed(self):
        sent = []

        class Resp:
            def raise_for_status(self): pass
            def json(self): return {"result": {"points": [], "next_page_offset": None}}
        with patch.object(main.requests, "post", side_effect=lambda url, json, timeout: sent.append(json) or Resp()):
            main.qdrant_scroll_by_arxiv("x", payload_fields=["a"], page=500)
            main.qdrant_scroll_by_arxiv("x")
        self.assertEqual(sent[0]["with_payload"], {"include": ["a"]})
        self.assertEqual(sent[0]["limit"], 500)
        self.assertIs(sent[1]["with_payload"], True)
        self.assertEqual(sent[1]["limit"], 200)

    def test_section_is_paged(self):
        points = [{"payload": p} for p in self.PAYLOADS]
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "qdrant_scroll_by_arxiv", return_value=points):
            first = main.paper_section("2401.1", title="Method", max_chars=500)
            self.assertEqual(first["text"], "We do X.\n\nE=mc2")
            self.assertIsNone(first["next_offset"])
            with self.assertRaises(HTTPException):
                main.paper_section("2401.1", title="Nope")


class CompactSearchTest(unittest.TestCase):
    LIC_A, LIC_B = {"spdx": "CC-BY-4.0"}, {"spdx": "none"}

    def _full(self):
        return {"results": [
            {"arxiv_id": "1", "source": "arXiv", "url": "u1", "arxiv_url": "u1", "license": self.LIC_A, "text": "t1"},
            {"arxiv_id": "2", "source": "arXiv", "url": "u2", "arxiv_url": "u2", "license": self.LIC_A, "text": "t2"},
            {"arxiv_id": "3", "source": "IACR", "url": "u3", "arxiv_url": "u3", "license": self.LIC_B, "text": "t3"},
        ], "count": 3, "usage": "n"}

    def _search(self, compact):
        full = self._full()
        with patch.object(main.search_cache, "get", return_value=full):
            out = main._run_search(main.SearchBody(query="q", compact=compact))
        return full, out

    def test_default_shape_is_untouched(self):
        full, out = self._search(False)
        self.assertIs(out, full)
        self.assertNotIn("licenses", out)
        self.assertIn("license", out["results"][0])
        self.assertIn("arxiv_url", out["results"][0])

    def test_compact_drops_per_hit_fields_and_lists_licenses_once(self):
        _, out = self._search(True)
        for r in out["results"]:
            self.assertNotIn("license", r)
            self.assertNotIn("arxiv_url", r)
        self.assertEqual(out["licenses"], {"arXiv": self.LIC_A, "IACR": self.LIC_B})
        self.assertEqual([r["text"] for r in out["results"]], ["t1", "t2", "t3"])
        self.assertEqual(out["count"], 3)

    def test_compact_does_not_corrupt_the_cached_payload(self):
        full, _ = self._search(True)
        self.assertEqual(full, self._full())

    def test_only_seen_sources_get_a_license(self):
        full = self._full()
        full["results"] = full["results"][:2]
        self.assertEqual(main._compact_search(full)["licenses"], {"arXiv": self.LIC_A})


class FacetsTest(unittest.TestCase):
    # id, year, venue, layers, matched_terms
    DB = [("2403.02691", 2024, "ACL", "ai-agents", '["prompt injection", "agent"]'),
          ("acl:x", 2024, "ACL", "ai-agents", '["prompt injection"]'),
          ("2406.13352", 2024, "NeurIPS", "ai-agents,llm-slm", '["agent"]'),
          ("2605.17986", 2026, None, "ai-agents", "not json")]

    def _run(self, body, rank_rows=None):
        seen = {}

        def source(b, ids):
            seen["ids"] = ids
            return self.DB
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_facet_source", side_effect=source), \
             patch.object(main, "_rank_papers", return_value=(rank_rows or [], None)) as rank, \
             patch.object(main, "_twins", return_value=TWINS):
            return main.paper_facets(body), seen, rank

    def test_without_query_counts_everything_and_folds_twins(self):
        out, seen, rank = self._run(main.FacetsBody(layer="ai-agents", year_from=2024))
        rank.assert_not_called()
        self.assertIsNone(seen["ids"])
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["by_year"], {2024: 2, 2026: 1})
        self.assertEqual(out["by_layer"], {"ai-agents": 3, "llm-slm": 1})
        self.assertEqual(out["top_venues"][0], {"venue": "ACL", "papers": 1})
        self.assertEqual(out["top_terms"][0], {"term": "agent", "papers": 2})
        self.assertNotIn("top_papers", out)
        self.assertNotIn("sampled", out)

    def test_with_query_uses_the_ranked_pool_and_returns_top_papers(self):
        pool = [dict(ROWS[i], relevance_rank=n) for n, i in enumerate(ROWS, 1)]
        out, seen, rank = self._run(main.FacetsBody(query="prompt injection"), pool)
        rank.assert_called_once_with("prompt injection", None, None, None, main.FACET_POOL)
        self.assertEqual(seen["ids"], list(ROWS))
        self.assertEqual([p["id"] for p in out["top_papers"]], list(ROWS))
        self.assertEqual(out["counted_over"], "every paper the query reached")

    def test_terms_are_sampled_when_the_filter_is_huge(self):
        with patch.object(main, "FACET_TERMS_SAMPLE", 2):
            out, _, _ = self._run(main.FacetsBody())
        self.assertEqual(out["sampled"], 2)

    def test_bad_layer_is_refused(self):
        with patch.object(main, "auth_and_limit"):
            with self.assertRaises(HTTPException):
                main.paper_facets(main.FacetsBody(layer="biology"))


class RankPapersTest(unittest.TestCase):
    def test_pool_size_reaches_both_retrievers(self):
        with patch.object(main, "COARSE_EMBED_URL", "http://embed"), \
             patch.object(main, "_coarse_paper_search", return_value=[]) as dense, \
             patch.object(main, "_paper_bm25", return_value=["2406.13352"]) as lex, \
             patch.object(main, "_paper_rows", side_effect=lambda ids: {i: dict(ROWS[i]) for i in ids if i in ROWS}), \
             patch.object(main, "_twins", return_value=TWINS):
            pool, err = main._rank_papers("mev", "web3", 2020, None, 200)
        self.assertEqual(dense.call_args.args[4], 200)
        self.assertEqual(lex.call_args.kwargs["limit"], 200)
        self.assertEqual(pool[0]["relevance_rank"], 1)
        self.assertIsNone(err)


class SimilarTest(unittest.TestCase):
    def _run(self, hits, scroll_points=("pt-1",), layer=None, paper_id="2403.02691"):
        calls = []

        class Resp:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                pass

            def json(self):
                return {"result": self.data}

        def post(url, json=None, timeout=None):
            calls.append((url, json))
            if url.endswith("/scroll"):
                return Resp({"points": [{"id": p} for p in scroll_points]})
            return Resp(hits)
        with patch.object(main, "auth_and_limit"), \
             patch.object(main.requests, "post", side_effect=post), \
             patch.object(main, "_paper_rows", side_effect=lambda ids: {i: dict(ROWS[i]) for i in ids if i in ROWS}), \
             patch.object(main, "_twins", return_value=TWINS):
            out = main.similar_papers(paper_id, limit=10, layer=layer)
        return out, calls

    def test_excludes_self_and_twins_and_reports_similarity(self):
        hits = [{"score": 0.9, "payload": {"arxiv_id": "acl:x"}},
                {"score": 0.8, "payload": {"arxiv_id": "2406.13352"}},
                {"score": 0.7, "payload": {"arxiv_id": "2406.13352"}},
                {"score": 0.6, "payload": {"arxiv_id": "2605.17986"}},
                {"score": 0.5, "payload": {"arxiv_id": "unknown"}}]
        out, calls = self._run(hits)
        self.assertEqual([p["id"] for p in out["papers"]], ["2406.13352", "2605.17986"])
        self.assertEqual(out["papers"][0]["similarity"], 0.8)
        self.assertEqual(calls[-1][1]["positive"], ["pt-1"])
        self.assertNotIn("filter", calls[-1][1])

    def test_layer_filter_is_passed_to_qdrant(self):
        _, calls = self._run([], layer="web3")
        self.assertEqual(calls[-1][1]["filter"], {"must": [{"key": "layers", "match": {"any": ["web3"]}}]})

    def test_missing_paper_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run([], scroll_points=(), paper_id="9999.99999")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_bad_layer_is_refused(self):
        with patch.object(main, "auth_and_limit"):
            with self.assertRaises(HTTPException):
                main.similar_papers("2403.02691", layer="biology")

    def test_route_is_ahead_of_the_catch_all(self):
        paths = [r.path for r in main.app.routes]
        self.assertLess(paths.index("/v1/paper/{paper_id:path}/similar"), paths.index("/v1/paper/{paper_id:path}"))
        self.assertIn("/v1/paper/{arxiv_id:path}/spec", paths)


if __name__ == "__main__":
    unittest.main()
