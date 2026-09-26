import time
import unittest
from unittest.mock import MagicMock, patch

from api import main


def _hit(point_id, score, payload=None):
    return {"id": point_id, "score": score, "payload": payload or {}}


def _coarse_hit(arxiv_id, cosine, in_citations=0, layers=("web3",)):
    return _hit(
        f"coarse:{arxiv_id}", cosine,
        {"arxiv_id": arxiv_id, "title": f"paper {arxiv_id}", "in_citations": in_citations,
         "layers": list(layers)},
    )


def _chunk_hit(point_id, arxiv_id, score):
    return _hit(point_id, score, {"arxiv_id": arxiv_id, "title": f"paper {arxiv_id}", "text": "body"})


class FakeHierQdrant:
    """Same fake as test_hier_search.py's -- stands in for papers_coarse and
    papers_fulltext Qdrant traffic (coarse / stage-B / stage-C=global)."""

    def __init__(self, coarse_hits, stage_b_hits, global_hits, points_by_id,
                coarse_should_fail=False, global_delay=0.0):
        self.coarse_hits = coarse_hits
        self.stage_b_hits = stage_b_hits
        self.global_hits = global_hits
        self.points_by_id = points_by_id
        self.coarse_should_fail = coarse_should_fail
        self.global_delay = global_delay
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
            if has_arxiv_filter:
                hits = self.stage_b_hits
            else:
                if self.global_delay:
                    time.sleep(self.global_delay)
                hits = self.global_hits
            resp.json = MagicMock(return_value={"result": hits[: json["limit"]]})
            return resp
        if url.endswith("/points"):
            ids = json["ids"]
            points = [self.points_by_id[i] for i in ids if i in self.points_by_id]
            resp.json = MagicMock(return_value={"result": points})
            return resp
        raise AssertionError(f"unexpected URL {url}")


def _body(**kwargs):
    defaults = dict(query="hier fusion test", layer="web3", hybrid=False,
                    diagnose=False, dedupe=True, min_score=0.0, limit=8)
    defaults.update(kwargs)
    return main.SearchBody(**defaults)


class HierRrfFusionTests(unittest.TestCase):
    """HIER_FUSION="rrf" fuses dense(mix)/lexical/global paper rankings by
    weighted RRF instead of mix's blend + tail splice. Default stays "mix",
    byte-for-byte the pre-existing behavior (see test_hier_search.py)."""

    def setUp(self):
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        self.vector = [0.1, 0.2, 0.3]
        for name, value in (("HIER_SEARCH", True), ("HIER_GLOBAL_GRACE", 0.2)):
            p = patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(main, "embed_query", return_value=self.vector)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(main, "_paper_facts", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def _run(self, body, fake):
        with patch.object(main.requests, "post", side_effect=fake.post):
            return main._run_search(body)

    def test_default_fusion_is_mix_and_unchanged(self):
        # Same scenario as test_hier_search's stage-A/B reorder case: p2's
        # stronger stage-B chunk should overtake p1 under the HIER_MIX blend,
        # exactly like before HIER_FUSION existed.
        self.assertEqual(main.HIER_FUSION, "mix")
        coarse = [_coarse_hit("p1", 0.80), _coarse_hit("p2", 0.78)]
        chunks = [_chunk_hit("c1", "p1", 0.70), _chunk_hit("c2", "p2", 0.95)]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        result = self._run(_body(), fake)

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(ids, ["p2", "p1"])
        for r in result["results"]:
            self.assertEqual(r["found_via"], "paper")
            self.assertNotIn("fusion_ranks", r)

    def test_rrf_promotes_lex_top_rank_with_weak_dense(self):
        # "lexpaper" is bm25-rank-1 (exact title/acronym match) but only a
        # weak stage-B chunk; two coarse-dense papers both score higher on
        # the mix blend alone. Under weighted RRF the lexical top rank should
        # still be enough to land it in the top 3.
        coarse = [_coarse_hit("p1", 0.90), _coarse_hit("p2", 0.88)]
        chunks = [
            _chunk_hit("c1", "p1", 0.90),
            _chunk_hit("c2", "p2", 0.88),
            _chunk_hit("c3", "lexpaper", 0.50),
        ]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        with patch.object(main, "PAPER_LEXICAL", True), \
             patch.object(main, "HIER_FUSION", "rrf"), \
             patch.object(main, "_paper_bm25", return_value=["lexpaper", "p1", "p2"]):
            result = self._run(_body(min_score=0.0), fake)

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertIn("lexpaper", ids[:3], f"expected lexpaper in top 3, got {ids}")
        lexpaper = next(r for r in result["results"] if r["arxiv_id"] == "lexpaper")
        self.assertEqual(lexpaper["found_via"], "paper lexical")
        self.assertEqual(lexpaper["fusion_ranks"]["lex"], 1)
        self.assertIsNone(lexpaper["fusion_ranks"]["global"])

    def test_global_only_paper_enters_the_pool(self):
        # "gpaper" is found only by the old global chunk channel (stage C) --
        # never shortlisted by stage A, never in the lexical list -- and
        # should still surface via HIER_FUSION="rrf" fusion (unlike "mix",
        # which only tail-splices up to HIER_GLOBAL_EXTRA of these).
        coarse = [_coarse_hit("p1", 0.85)]
        chunks = [_chunk_hit("c1", "p1", 0.85)]
        global_hits = [_chunk_hit("g1", "gpaper", 0.80)]
        points = {h["id"]: h for h in chunks + global_hits}
        fake = FakeHierQdrant(coarse, chunks, global_hits, points)

        with patch.object(main, "HIER_FUSION", "rrf"):
            result = self._run(_body(min_score=0.0), fake)

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertIn("gpaper", ids)
        gpaper = next(r for r in result["results"] if r["arxiv_id"] == "gpaper")
        self.assertEqual(gpaper["fusion_ranks"]["global"], 1)
        self.assertIsNone(gpaper["fusion_ranks"]["dense"])
        self.assertIsNone(gpaper["fusion_ranks"]["lex"])

    def test_lex_top_three_survives_the_relevance_floor(self):
        # "lexpaper"'s only chunk evidence scores under the relevance floor;
        # under "mix" it would be dropped. Under "rrf" a lex rank <=3 is its
        # own sufficient evidence and must survive regardless.
        coarse = [_coarse_hit("p1", 0.90)]
        chunks = [
            _chunk_hit("c1", "p1", 0.90),
            _chunk_hit("c2", "lexpaper", 0.05),  # well under any relevance floor
        ]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        with patch.object(main, "PAPER_LEXICAL", True), \
             patch.object(main, "HIER_FUSION", "rrf"), \
             patch.object(main, "_paper_bm25", return_value=["lexpaper"]):
            result = self._run(_body(min_score=0.5), fake)

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertIn("lexpaper", ids, "lex rank<=3 must survive the floor")
        lexpaper = next(r for r in result["results"] if r["arxiv_id"] == "lexpaper")
        self.assertEqual(lexpaper["found_via"], "paper lexical")

        # a lexical rank *below* 3 gets no floor exemption. Give four other
        # papers their own real (above-floor) chunk evidence and rank them
        # ahead of "lexpaper" in the bm25 order, pushing it to lex rank 5 --
        # L_lex only contains ids with stage-B chunk evidence, so without
        # real competing evidence "lexpaper" would stay rank 1 regardless.
        chunks2 = chunks + [_chunk_hit(f"co{i}", f"other{i}", 0.90) for i in range(1, 5)]
        points2 = {h["id"]: h for h in chunks2}
        fake2 = FakeHierQdrant(coarse, chunks2, [], points2)
        with patch.object(main, "PAPER_LEXICAL", True), \
             patch.object(main, "HIER_FUSION", "rrf"), \
             patch.object(main, "_paper_bm25",
                          return_value=["other1", "other2", "other3", "other4", "lexpaper"]):
            result2 = self._run(_body(min_score=0.5, query="hier fusion test 2"), fake2)
        ids2 = [r["arxiv_id"] for r in result2["results"]]
        self.assertNotIn("lexpaper", ids2, f"lex rank 5 must not survive the floor, got {ids2}")

    def test_fusion_weights_from_env_change_order(self):
        # Two papers: "densewin" leads on the dense/mix channel, "lexwin"
        # leads (rank 1) on the lexical channel with a middling dense rank.
        # Cranking HIER_W_LEX far above HIER_W_DENSE should flip the order.
        coarse = [_coarse_hit("densewin", 0.95), _coarse_hit("lexwin", 0.70)]
        chunks = [
            _chunk_hit("c1", "densewin", 0.95),
            _chunk_hit("c2", "lexwin", 0.70),
        ]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        with patch.object(main, "PAPER_LEXICAL", True), \
             patch.object(main, "HIER_FUSION", "rrf"), \
             patch.object(main, "_paper_bm25", return_value=["lexwin", "densewin"]), \
             patch.object(main, "HIER_W_DENSE", 1.0), \
             patch.object(main, "HIER_W_LEX", 0.1), \
             patch.object(main, "HIER_W_GLOBAL", 0.5):
            default_weighted = self._run(_body(min_score=0.0), fake)
        ids_default = [r["arxiv_id"] for r in default_weighted["results"]]
        self.assertEqual(ids_default[0], "densewin")

        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        with patch.object(main, "PAPER_LEXICAL", True), \
             patch.object(main, "HIER_FUSION", "rrf"), \
             patch.object(main, "_paper_bm25", return_value=["lexwin", "densewin"]), \
             patch.object(main, "HIER_W_DENSE", 0.1), \
             patch.object(main, "HIER_W_LEX", 5.0), \
             patch.object(main, "HIER_W_GLOBAL", 0.5):
            lex_weighted = self._run(_body(min_score=0.0), fake)
        ids_lex = [r["arxiv_id"] for r in lex_weighted["results"]]
        self.assertEqual(ids_lex[0], "lexwin", "a dominant lex weight should flip the ranking")


if __name__ == "__main__":
    unittest.main()
