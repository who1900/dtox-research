import unittest
from unittest.mock import patch

from api import main


def _hit(paper_id, score, text="A useful technical passage describing the method."):
    return {"score": score, "payload": {
        "arxiv_id": paper_id, "title": f"Paper {paper_id}", "text": text,
        "section_title": "Method", "section_type": "method",
        "element_type": "prose", "layers": ["web3"],
    }}


class HybridFinalCutTests(unittest.TestCase):
    def setUp(self):
        main.search_cache.data.clear()

    def _run(self, *, hybrid, lexical, hits, strict=False, dedupe=True,
             boilerplate_filter=True):
        body = main.SearchBody(query="bounded lexical candidates", layer="web3",
                               hybrid=hybrid, strict=strict, diagnose=False, dedupe=dedupe,
                               limit=10, min_score=0.5)
        with patch.object(main, "embed_query", return_value=[0.1, 0.2]), \
             patch.object(main, "two_phase_dense_search",
                          return_value=(hits, [], [], [])) as dense_search, \
             patch.object(main, "HIER_SEARCH", False), \
             patch.object(main, "PAPER_LEXICAL", False), \
             patch.object(main, "_bm25_candidates", return_value=lexical) as lexical_search, \
             patch.object(main, "BOILERPLATE_FILTER", boilerplate_filter), \
             patch.object(main, "MIN_RELEVANCE_BY_LAYER", {"web3": 0.5}), \
             patch.object(main, "_score_specific_papers", return_value=[]) as score_specific, \
             patch.object(main, "_paper_facts", return_value={}), \
             patch.object(main, "_exact_matches", return_value=[]), \
             patch.object(main, "_lookup_note", return_value=None), \
             patch.object(main, "_dedupe_preferring_content",
                          wraps=main._dedupe_preferring_content) as content_dedupe:
            result = main._run_search(body)
        return result, dense_search, lexical_search, score_specific, content_dedupe

    def test_rank_eleven_lexical_hit_survives_fusion_and_final_cut(self):
        hits = [_hit(f"arxiv:2401.{i:05d}", 0.99 - i * 0.01,
                     "Davide Mancino _University of Milano-Bicocca_ Milano, Italy "
                     "davide.mancino@unimib.it Marco Rossi _University of Turin_ Turin, Italy "
                     "marco.rossi@unito.it")
                if i == 3 else _hit(f"arxiv:2401.{i:05d}", 0.99 - i * 0.01)
                for i in range(1, 11)]
        rank_eleven = "arxiv:2401.00011"
        hits.append(_hit(rank_eleven, 0.89))
        # A later content chunk replaces rank 3's boilerplate chunk on dedupe.
        hits.append(_hit("arxiv:2401.00003", 0.86))
        hits.append(_hit("arxiv:2401.00013", 0.1))  # below the relevance floor

        result, dense, lexical, scored, dedupe = self._run(
            hybrid=True, lexical=[rank_eleven], hits=hits, strict=True)

        rows = result["results"]
        ids = [row["arxiv_id"] for row in rows]
        self.assertEqual(len(rows), 10)
        self.assertIn(rank_eleven, ids)
        self.assertEqual(ids.count("arxiv:2401.00003"), 1)
        self.assertEqual(next(r for r in rows if r["arxiv_id"] == "arxiv:2401.00003")["text"],
                         hits[11]["payload"]["text"])
        self.assertNotIn("arxiv:2401.00013", ids)
        self.assertTrue(all(row["score"] >= 0.5 for row in rows))
        dense.assert_called_once()
        lexical.assert_called_once()
        scored.assert_not_called()
        dedupe.assert_called_once_with(hits, 0.5, set(), 13)

    def test_nonhybrid_and_empty_lexical_keep_requested_dense_cut(self):
        hits = [_hit(f"arxiv:2402.{i:05d}", 0.99 - i * 0.01) for i in range(1, 12)]
        for hybrid, lexical in ((False, ["arxiv:2402.00011"]), (True, [])):
            with self.subTest(hybrid=hybrid, lexical=bool(lexical)):
                main.search_cache.data.clear()
                result, _, _, _, dedupe = self._run(hybrid=hybrid, lexical=lexical, hits=hits)
                self.assertEqual(result["count"], 10)
                self.assertNotIn("arxiv:2402.00011",
                                 [row["arxiv_id"] for row in result["results"]])
                dedupe.assert_called_once_with(hits, 0.5, set(), 10)

    def test_candidate_cap_is_120_and_boilerplate_path_stays_bounded(self):
        hits = [_hit(f"arxiv:2403.{i:05d}", 0.99 - i * 0.001)
                for i in range(1, 123)]
        rank_120 = "arxiv:2403.00120"
        # Boilerplate at dense rank 3 has a later content chunk within the cap.
        hits[2]["payload"]["text"] = (
            "Davide Mancino _University of Milano-Bicocca_ Milano, Italy "
            "davide.mancino@unimib.it Marco Rossi _University of Turin_ Turin, Italy "
            "marco.rossi@unito.it")
        hits[118] = _hit("arxiv:2403.00003", 0.86)
        hits[121] = _hit("arxiv:2403.00122", 0.1)

        result, dense, lexical, scored, dedupe = self._run(
            hybrid=True, lexical=[rank_120], hits=hits)

        ids = [row["arxiv_id"] for row in result["results"]]
        self.assertEqual(len(ids), 10)
        self.assertIn(rank_120, ids)
        self.assertNotIn("arxiv:2403.00121", ids)
        self.assertNotIn("arxiv:2403.00122", ids)
        self.assertEqual(ids.count("arxiv:2403.00003"), 1)
        self.assertEqual(next(r for r in result["results"]
                              if r["arxiv_id"] == "arxiv:2403.00003")["text"],
                         hits[118]["payload"]["text"])
        dense.assert_called_once()
        lexical.assert_called_once()
        scored.assert_not_called()
        dedupe.assert_called_once_with(hits, 0.5, set(), 120)

    def test_plain_dedupe_keeps_lexical_rank_eleven_through_final_cut(self):
        hits = [_hit(f"arxiv:2404.{i:05d}", 0.99 - i * 0.01)
                for i in range(1, 12)]
        rank_eleven = "arxiv:2404.00011"
        hits[-1]["score"] = 0.9
        hits.append(_hit("arxiv:2404.00012", 0.1))

        result, dense, lexical, scored, dedupe = self._run(
            hybrid=True, lexical=[rank_eleven], hits=hits, dedupe=False,
            boilerplate_filter=False)

        ids = [row["arxiv_id"] for row in result["results"]]
        self.assertEqual(len(ids), 10)
        self.assertIn(rank_eleven, ids)
        self.assertNotIn("arxiv:2404.00012", ids)
        dense.assert_called_once()
        lexical.assert_called_once()
        scored.assert_not_called()
        dedupe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
