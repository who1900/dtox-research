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
        self.assertEqual([p["id"] for p in out["papers"]], ["1904.05234", "2406.13352"])
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

    def test_section_is_paged(self):
        points = [{"payload": p} for p in self.PAYLOADS]
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "qdrant_scroll_by_arxiv", return_value=points):
            first = main.paper_section("2401.1", title="Method", max_chars=500)
            self.assertEqual(first["text"], "We do X.\n\nE=mc2")
            self.assertIsNone(first["next_offset"])
            with self.assertRaises(HTTPException):
                main.paper_section("2401.1", title="Nope")


if __name__ == "__main__":
    unittest.main()
