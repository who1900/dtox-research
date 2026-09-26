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
    """Stands in for both papers_coarse and papers_fulltext Qdrant traffic.

    coarse_hits: hits papers_coarse answers with (with_payload=True, single call).
    stage_b_hits: chunk hits answered when the search filter carries an
        arxiv_id clause (stage B, restricted to the stage-A shortlist).
    global_hits: chunk hits answered when it does not (stage C, the old
        unrestricted per-layer/global search).
    global_delay: seconds to sleep before answering a stage-C search, to
        simulate it missing the HIER_GLOBAL_GRACE window.
    """

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
    defaults = dict(query="hierarchical search test", layer="web3", hybrid=False,
                    diagnose=False, dedupe=True, min_score=0.0, limit=8)
    defaults.update(kwargs)
    return main.SearchBody(**defaults)


class HierSearchTests(unittest.TestCase):
    def setUp(self):
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        self.vector = [0.1, 0.2, 0.3]
        patcher = patch.object(main, "embed_query", return_value=self.vector)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(main, "_paper_facts", return_value={})
        patcher2.start()
        self.addCleanup(patcher2.stop)
        hier_on = patch.object(main, "HIER_SEARCH", True)
        hier_on.start()
        self.addCleanup(hier_on.stop)
        grace = patch.object(main, "HIER_GLOBAL_GRACE", 0.2)
        grace.start()
        self.addCleanup(grace.stop)

    def _run(self, body, fake):
        with patch.object(main.requests, "post", side_effect=fake.post):
            return main._run_search(body)

    def test_stage_a_then_b_merge_can_reorder_stage_a(self):
        # p1 leads stage A on paper_score alone; p2's stronger chunk evidence
        # in stage B should overtake it once mixed (HIER_MIX default 0.5).
        coarse = [_coarse_hit("p1", 0.80), _coarse_hit("p2", 0.78)]
        chunks = [_chunk_hit("c1", "p1", 0.70), _chunk_hit("c2", "p2", 0.95)]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        result = self._run(_body(), fake)

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(ids, ["p2", "p1"])
        for r in result["results"]:
            self.assertEqual(r["found_via"], "paper")
            self.assertIn("paper", r["channels"])
            self.assertIsInstance(r["paper_score"], float)
        self.assertNotIn("partial", result)
        self.assertNotIn("global_channel", result)

    def test_citation_prior_can_outrank_higher_cosine(self):
        # p1: lower cosine but heavily cited in-corpus (full prior).
        # p2: higher cosine, zero in-corpus citations.
        # Equal chunk evidence isolates the prior's effect on the final mix.
        coarse = [_coarse_hit("p2", 0.81, in_citations=0),
                  _coarse_hit("p1", 0.80, in_citations=1000)]
        chunks = [_chunk_hit("c1", "p1", 0.85), _chunk_hit("c2", "p2", 0.85)]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        result = self._run(_body(), fake)

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(ids[0], "p1", "in-corpus citation prior should flip a near-tied cosine")

    def test_stage_b_filters_to_stage_a_shortlist(self):
        coarse = [_coarse_hit("p1", 0.80), _coarse_hit("p2", 0.79)]
        chunks = [_chunk_hit("c1", "p1", 0.90), _chunk_hit("c2", "p2", 0.88)]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        self._run(_body(), fake)

        stage_b_calls = [
            j for (u, j) in fake.calls
            if u.endswith("/points/search")
            and any(c.get("key") == "arxiv_id" for c in (j.get("filter") or {}).get("must", []))
        ]
        self.assertEqual(len(stage_b_calls), 1)
        arxiv_clause = next(c for c in stage_b_calls[0]["filter"]["must"] if c["key"] == "arxiv_id")
        self.assertEqual(set(arxiv_clause["match"]["any"]), {"p1", "p2"})

    def test_global_channel_marked_skipped_when_it_misses_the_grace_window(self):
        coarse = [_coarse_hit("p1", 0.85)]
        chunks = [_chunk_hit("c1", "p1", 0.85)]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [_chunk_hit("g1", "gpaper", 0.90)], points,
                              global_delay=1.0)

        result = self._run(_body(), fake)

        self.assertEqual(result.get("global_channel"), "skipped")
        self.assertNotIn("partial", result)
        self.assertEqual([r["arxiv_id"] for r in result["results"]], ["p1"])

    def test_global_channel_adds_up_to_three_extra_papers_in_tail(self):
        coarse = [_coarse_hit("p1", 0.85)]
        chunks = [_chunk_hit("c1", "p1", 0.85)]
        global_extra = [_chunk_hit(f"g{i}", f"gpaper{i}", 0.9 - i * 0.01) for i in range(5)]
        points = {h["id"]: h for h in chunks + global_extra}
        fake = FakeHierQdrant(coarse, chunks, global_extra, points, global_delay=0.0)

        result = self._run(_body(), fake)

        self.assertNotIn("global_channel", result)
        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(ids[0], "p1")
        extras = ids[1:]
        self.assertEqual(extras, ["gpaper0", "gpaper1", "gpaper2"], "capped at HIER_GLOBAL_EXTRA=3")
        for r in result["results"][1:]:
            self.assertNotEqual(r.get("found_via"), "paper")
            self.assertEqual(r["channels"], ["dense"])

    def test_stage_a_failure_falls_back_to_old_path(self):
        old_hits = [_chunk_hit("o1", "old1", 0.90), _chunk_hit("o2", "old2", 0.85)]
        points = {h["id"]: h for h in old_hits}
        fake = FakeHierQdrant([], [], old_hits, points, coarse_should_fail=True)

        result = self._run(_body(), fake)

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(ids, ["old1", "old2"])
        self.assertNotIn("global_channel", result)
        for r in result["results"]:
            self.assertNotIn("paper_score", r)
            self.assertNotEqual(r.get("found_via"), "paper")
            self.assertEqual(r["channels"], ["dense"])

    def test_hier_search_off_matches_old_single_tier_path(self):
        with patch.object(main, "HIER_SEARCH", False):
            hits = [_chunk_hit("a", "arx-a", 0.95), _chunk_hit("b", "arx-b", 0.90)]
            points = {h["id"]: h for h in hits}
            fake = FakeHierQdrant([], [], hits, points)

            with patch.object(main, "_hier_run") as spy:
                result = self._run(_body(), fake)
                spy.assert_not_called()

            ids = [r["arxiv_id"] for r in result["results"]]
            self.assertEqual(ids, ["arx-a", "arx-b"])
            for r in result["results"]:
                self.assertNotIn("paper_score", r)
                self.assertEqual(r["channels"], ["dense"])
            self.assertNotIn("global_channel", result)


if __name__ == "__main__":
    unittest.main()
