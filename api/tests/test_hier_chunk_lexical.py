"""HIER_CHUNK_LEXICAL: when hierarchical search actually succeeds (not the
stage A/B fallback), its own paper-level BM25 channel (PAPER_LEXICAL) already
covers exact-term matches, so HIER_CHUNK_LEXICAL=0 skips the chunk-level
_bm25_candidates query and the _score_specific_papers/_lexical_fallback_results
extra-scoring the old hybrid block did on top of it. Default "1" must leave
that path byte-for-byte unchanged, and a stage A/B fallback must always get
the chunk channel regardless of the flag.

Reuses the FakeHierQdrant shape from test_paper_lexical.py.
"""
import unittest
from unittest.mock import MagicMock, patch

from api import main


def _hit(point_id, score, payload=None):
    return {"id": point_id, "score": score, "payload": payload or {}}


def _coarse_hit(arxiv_id, cosine, in_citations=0, layers=("web3",)):
    return _hit(f"coarse:{arxiv_id}", cosine,
               {"arxiv_id": arxiv_id, "title": f"paper {arxiv_id}", "in_citations": in_citations,
                "layers": list(layers)})


def _chunk_hit(point_id, arxiv_id, score):
    return _hit(point_id, score, {"arxiv_id": arxiv_id, "title": f"paper {arxiv_id}", "text": "body"})


class FakeHierQdrant:
    def __init__(self, coarse_hits, stage_b_hits, global_hits, points_by_id,
                coarse_should_fail=False):
        self.coarse_hits = coarse_hits
        self.stage_b_hits = stage_b_hits
        self.global_hits = global_hits
        self.points_by_id = points_by_id
        self.coarse_should_fail = coarse_should_fail
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith(f"/collections/{main.COARSE_COLLECTION}/points/search"):
            if self.coarse_should_fail:
                raise main.requests.RequestException("papers_coarse unavailable")
            resp.json = MagicMock(return_value={"result": self.coarse_hits[: json["limit"]]})
            return resp
        if url.endswith("/points/search"):
            must = (json.get("filter") or {}).get("must") or []
            has_arxiv_filter = any(c.get("key") == "arxiv_id" for c in must)
            hits = self.stage_b_hits if has_arxiv_filter else self.global_hits
            resp.json = MagicMock(return_value={"result": hits[: json["limit"]]})
            return resp
        if url.endswith("/points"):
            ids = json["ids"]
            points = [self.points_by_id[i] for i in ids if i in self.points_by_id]
            resp.json = MagicMock(return_value={"result": points})
            return resp
        raise AssertionError(f"unexpected URL {url}")


class HierChunkLexicalTests(unittest.TestCase):
    def setUp(self):
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        self.vector = [0.1, 0.2, 0.3]
        for name, value in (("HIER_SEARCH", True), ("PAPER_LEXICAL", True),
                           ("HIER_GLOBAL_GRACE", 0.2)):
            p = patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(main, "embed_query", return_value=self.vector)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(main, "_paper_facts", return_value={})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(main, "_lookup_note", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def _body(self, **kwargs):
        defaults = dict(query="chunk lexical flag test", layer="web3", hybrid=True,
                        diagnose=False, dedupe=True, min_score=0.0, limit=8)
        defaults.update(kwargs)
        return main.SearchBody(**defaults)

    def _success_fake(self):
        coarse = [_coarse_hit("p1", 0.80)]
        chunks = [_chunk_hit("c1", "p1", 0.85)]
        points = {h["id"]: h for h in chunks}
        return FakeHierQdrant(coarse, chunks, [], points)

    def test_flag_off_default_skips_chunk_bm25_when_hier_succeeds(self):
        fake = self._success_fake()
        with (patch.object(main, "HIER_CHUNK_LEXICAL", False),
              patch.object(main, "_paper_bm25", return_value=["p1"]),
              patch.object(main, "_bm25_candidates") as bm25_mock,
              patch.object(main, "_score_specific_papers") as score_mock,
              patch.object(main, "_lexical_fallback_results") as fallback_mock,
              patch.object(main.requests, "post", side_effect=fake.post)):
            result = main._run_search(self._body())

        bm25_mock.assert_not_called()
        score_mock.assert_not_called()
        fallback_mock.assert_not_called()
        self.assertEqual(result.get("lexical_channel"), "paper")
        self.assertIn("p1", [r["arxiv_id"] for r in result["results"]])

    def test_flag_on_default_still_calls_chunk_bm25_when_hier_succeeds(self):
        fake = self._success_fake()
        with (patch.object(main, "HIER_CHUNK_LEXICAL", True),
              patch.object(main, "_paper_bm25", return_value=["p1"]),
              patch.object(main, "_bm25_candidates", return_value=[]) as bm25_mock,
              patch.object(main.requests, "post", side_effect=fake.post)):
            result = main._run_search(self._body())

        bm25_mock.assert_called_once()
        self.assertEqual(result.get("lexical_channel"), "paper+chunk")

    def test_flag_off_falls_back_to_chunk_bm25_when_hier_falls_back(self):
        # stage A (papers_coarse) fails -> old single-tier path, which must
        # get the chunk-level channel exactly like before the flag existed.
        old_hits = [_chunk_hit("o1", "old1", 0.90)]
        points = {h["id"]: h for h in old_hits}
        fake = FakeHierQdrant([], [], old_hits, points, coarse_should_fail=True)

        with (patch.object(main, "HIER_CHUNK_LEXICAL", False),
              patch.object(main, "_paper_bm25", return_value=[]),
              patch.object(main, "_bm25_candidates", return_value=["old1"]) as bm25_mock,
              patch.object(main.requests, "post", side_effect=fake.post)):
            result = main._run_search(self._body())

        bm25_mock.assert_called_once()
        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertIn("old1", ids)

    def test_flag_off_without_paper_lexical_does_not_skip_chunk_bm25(self):
        # the optimization only applies when PAPER_LEXICAL is also on -- with
        # it off, hier search carries no lexical signal to cover the gap.
        fake = self._success_fake()
        with (patch.object(main, "HIER_CHUNK_LEXICAL", False),
              patch.object(main, "PAPER_LEXICAL", False),
              patch.object(main, "_bm25_candidates", return_value=[]) as bm25_mock,
              patch.object(main.requests, "post", side_effect=fake.post)):
            main._run_search(self._body())

        bm25_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
