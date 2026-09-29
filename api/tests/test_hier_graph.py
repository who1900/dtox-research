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


import sqlite3

def _db(edges):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE citations (src TEXT, dst TEXT)")
    conn.executemany("INSERT INTO citations VALUES (?, ?)", edges)
    return conn


class StrictFake(FakeHierQdrant):
    """Stage-B answers only for the arxiv_ids actually asked for."""

    def post(self, url, json=None, timeout=None):
        must = ((json or {}).get("filter") or {}).get("must") or []
        wanted = next((c["match"]["any"] for c in must if c.get("key") == "arxiv_id"), None)
        if wanted is not None and url.endswith("/points/search")                 and not url.endswith(f"/collections/{main.COARSE_COLLECTION}/points/search"):
            self.calls.append((url, json))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"result": [
                h for h in self.stage_b_hits if h["payload"]["arxiv_id"] in wanted][: json["limit"]]})
            return resp
        return super().post(url, json=json, timeout=timeout)


class HierGraphChannelTests(unittest.TestCase):
    """HIER_GRAPH adds a fourth RRF channel: papers cited by several of the top
    fused candidates, each still needing stage-B chunk evidence."""

    def setUp(self):
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        for name, value in (("HIER_SEARCH", True), ("HIER_GLOBAL_GRACE", 0.2),
                            ("HIER_FUSION", "rrf"), ("HIER_GRAPH", True),
                            ("HIER_GRAPH_MIN", 2)):
            p = patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, rv in (("embed_query", [0.1, 0.2, 0.3]), ("_paper_facts", {})):
            p = patch.object(main, name, return_value=rv)
            p.start()
            self.addCleanup(p.stop)
        self.coarse = [_coarse_hit("p1", 0.90), _coarse_hit("p2", 0.88), _coarse_hit("p3", 0.86)]
        self.chunks = [_chunk_hit("c1", "p1", 0.90), _chunk_hit("c2", "p2", 0.88),
                       _chunk_hit("c3", "p3", 0.86), _chunk_hit("cc", "canon", 0.80)]
        self.edges = [("p1", "canon"), ("p2", "canon"), ("p3", "other")]

    def _run(self, body, edges=None, chunks=None, coarse_only=True):
        chunks = self.chunks if chunks is None else chunks
        points = {h["id"]: h for h in chunks}
        fake = StrictFake(self.coarse, chunks, [], points)
        conn = _db(self.edges if edges is None else edges)
        with patch.object(main.requests, "post", side_effect=fake.post),              patch.object(main, "_ro_conn", return_value=conn):
            return main._run_search(body)

    def _body(self, **kw):
        kw.setdefault("min_score", 0.0)
        return _body(**kw)

    def test_cited_paper_is_added_with_graph_rank(self):
        # canon is not in the coarse shortlist but is cited by p1 and p2
        result = self._run(self._body())
        by = {r["arxiv_id"]: r for r in result["results"]}
        self.assertIn("canon", by)
        self.assertEqual(by["canon"]["fusion_ranks"]["graph"], 1)
        self.assertEqual(by["canon"]["found_via"], "paper graph")
        self.assertIn("graph", by["canon"]["channels"])
        self.assertIsNone(by["p1"]["fusion_ranks"]["graph"])
        self.assertNotIn("graph", by["p1"]["channels"])

    def test_below_threshold_not_added(self):
        result = self._run(self._body(), edges=[("p1", "canon"), ("p3", "other")])
        self.assertNotIn("canon", [r["arxiv_id"] for r in result["results"]])

    def test_needs_chunk_evidence_and_floor(self):
        # no chunk for canon under the filters: not shown
        result = self._run(self._body(), chunks=self.chunks[:3])
        self.assertNotIn("canon", [r["arxiv_id"] for r in result["results"]])
        # chunk below the relevance floor: not shown either
        weak = self.chunks[:3] + [_chunk_hit("cc", "canon", 0.10)]
        result = self._run(self._body(min_score=0.5, query="floor test"), chunks=weak)
        self.assertNotIn("canon", [r["arxiv_id"] for r in result["results"]])

    def test_stage_b_call_carries_filters(self):
        points = {h["id"]: h for h in self.chunks}
        fake = StrictFake(self.coarse, self.chunks, [], points)
        with patch.object(main.requests, "post", side_effect=fake.post),              patch.object(main, "_ro_conn", return_value=_db(self.edges)):
            main._run_search(self._body(year_from=2020))
        graph_calls = [j for u, j in fake.calls
                       if u.endswith("/points/search")
                       and any(c.get("key") == "arxiv_id" and c["match"]["any"] == ["canon"]
                               for c in (j.get("filter") or {}).get("must", []))]
        self.assertTrue(graph_calls)
        keys = {c["key"] for c in graph_calls[0]["filter"]["must"]}
        self.assertTrue({"layers", "year"} <= keys)

    def test_twins_are_merged(self):
        state = {"canonical": {"canon": "canon", "canon-v2": "canon"},
                 "members": {"canon": ["canon", "canon-v2"]}}
        edges = [("p1", "canon"), ("p2", "canon-v2")]
        chunks = self.chunks + [_chunk_hit("cv", "canon-v2", 0.80)]
        with patch.object(main, "_twins", return_value=state),              patch.object(main, "canonical_id", side_effect=lambda a: state["canonical"].get(a, a)):
            result = self._run(self._body(), edges=edges, chunks=chunks)
        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(sum(i.startswith("canon") for i in ids), 1)

    def test_graph_off_is_identical_to_three_channels(self):
        with patch.object(main, "HIER_GRAPH", False):
            off = self._run(self._body())
        self.assertNotIn("canon", [r["arxiv_id"] for r in off["results"]])
        for r in off["results"]:
            self.assertNotIn("graph", r["fusion_ranks"])
            self.assertNotIn("graph", r["channels"])
            self.assertEqual(r["found_via"], "paper")

    def test_db_error_does_not_break_search(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)  # no citations table
        points = {h["id"]: h for h in self.chunks}
        fake = StrictFake(self.coarse, self.chunks, [], points)
        with patch.object(main.requests, "post", side_effect=fake.post),              patch.object(main, "_ro_conn", return_value=conn):
            result = main._run_search(self._body())
        self.assertEqual([r["arxiv_id"] for r in result["results"]], ["p1", "p2", "p3"])
        with patch.object(main.requests, "post", side_effect=fake.post),              patch.object(main, "_ro_conn", side_effect=RuntimeError("boom")):
            main.search_cache.data.clear(); main.qdrant_result_cache.data.clear()
            result = main._run_search(self._body())
        self.assertEqual(len(result["results"]), 3)


if __name__ == "__main__":
    unittest.main()
