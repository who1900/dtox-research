import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api import main


def point(pid, kind, text="payload"):
    return {"payload": {"arxiv_id": pid, "element_type": kind, "text": text,
                        "section_type": "method", "section_title": kind}}


def twins():
    return {"canonical": {"pdf:one": "latex:one", "latex:one": "latex:one"},
            "members": {"latex:one": ["latex:one", "pdf:one", "alt:one", "alt2:one", "alt3:one"]}}


class AliasExtractionTests(unittest.TestCase):
    def test_unknown_id_does_not_guess_alias(self):
        with (patch.object(main, "_twins", return_value={"canonical": {}, "members": {}}) as twin_mock,
              patch.object(main, "qdrant_scroll_by_arxiv", return_value=[]),
              patch.object(main, "auth_and_limit")):
            with self.assertRaises(HTTPException) as error:
                main.paper_spec("unknown")
        self.assertEqual(error.exception.status_code, 404)
        twin_mock.assert_called_once()

    def test_spec_alias_rescues_equation_and_algorithm_with_actual_provenance(self):
        metadata = {"latex:one": {"fulltext_source": "latex", "source_url": "https://example.test/latex"},
                    "pdf:one": {"fulltext_source": "pdf", "source_url": "https://example.test/pdf"}}
        scroll = lambda pid, **kwargs: [point(pid, "equation", "x^2=1"), point(pid, "algorithm")] if pid == "latex:one" else []
        with (patch.object(main, "_twins", side_effect=twins),
              patch.object(main, "auth_and_limit"),
              patch.object(main, "qdrant_scroll_by_arxiv", side_effect=scroll),
              patch.object(main, "_evidence_rows", side_effect=lambda ids: {pid: metadata[pid] for pid in ids})):
            result = main.paper_spec("pdf:one")
        self.assertEqual({row["element_type"] for row in result["sections"]}, {"equation", "algorithm"})
        self.assertEqual({row["source_paper_id"] for row in result["sections"]}, {"latex:one"})
        eq = next(row for row in result["sections"] if row["element_type"] == "equation")
        self.assertEqual(eq["evidence"]["source_paper_id"], "latex:one")
        self.assertEqual(eq["evidence"]["source_url"], "https://example.test/latex")
        canonical_payload = point("latex:one", "equation", "x^2=1")["payload"]
        expected = main.provenance("latex:one", canonical_payload, metadata["latex:one"],
                                   "https://example.test/latex", scope="indexed_chunk")
        self.assertEqual(eq["evidence"]["fragment_hash"], expected["fragment_hash"])
        self.assertEqual(eq["evidence"]["exact_latex"], expected["exact_latex"])
        self.assertTrue(eq["evidence"]["exact_latex"])
        self.assertEqual(result["arxiv_id"], "pdf:one")

    def test_wrongpaper_rejected_and_at_most_two_aliases(self):
        seen = []
        def scroll(pid, **kwargs):
            seen.append(pid)
            return [point("wrongpaper", "equation")] if pid == "pdf:one" else []
        with (patch.object(main, "_twins", side_effect=twins), patch.object(main, "auth_and_limit"),
              patch.object(main, "qdrant_scroll_by_arxiv", side_effect=scroll)):
            with self.assertRaises(HTTPException):
                main.paper_spec("pdf:one")
        self.assertEqual(seen, ["pdf:one", "latex:one", "alt:one"])

    def test_alias_deadline_preserves_requested_sections(self):
        ticks = iter([0.0, 0.0, 30.0])
        with (patch.object(main, "_twins", side_effect=twins), patch.object(main, "auth_and_limit"),
              patch.object(main.time, "monotonic", side_effect=lambda: next(ticks)),
              patch.object(main, "qdrant_scroll_by_arxiv", return_value=[point("pdf:one", "equation")]),
              patch.object(main, "_evidence_rows", return_value={})):
            result = main.paper_spec("pdf:one")
        self.assertEqual(result["sections"][0]["source_paper_id"], "pdf:one")
        self.assertTrue(result["source_resolution"]["partial"])
        self.assertTrue(result["source_resolution"]["time_budget_exhausted"])

    def test_normal_bundle_path_stays_plain_and_ranking_unchanged(self):
        hit = {"arxiv_id": "requested", "evidence": {}}
        points = [point("requested", "equation"), point("requested", "algorithm"),
                  point("requested", "table"), point("requested", "code"),
                  {"payload": {"arxiv_id": "requested", "section_type": "limitations", "text": "limits"}}]
        with (patch.object(main, "auth_and_limit"), patch.object(main, "_run_search", return_value={"results": [hit]}),
              patch.object(main, "qdrant_scroll_by_arxiv", return_value=points),
              patch.object(main, "build_bundle", side_effect=lambda body, search, selected, retrieve: retrieve("requested"))):
            result = main.research_bundle(main.ResearchBundleBody(query="q"))
        self.assertEqual(result, points)
        self.assertEqual(main.SPEC_PRIORITY, {"algorithm": 0, "equation": 1, "code": 2, "prose": 3, "table": 4})


if __name__ == "__main__":
    unittest.main()
