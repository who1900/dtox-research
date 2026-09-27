import unittest
from unittest.mock import MagicMock, patch

from api import main
from api.tests.test_hier_search import FakeHierQdrant, _body, _chunk_hit, _coarse_hit


class CoarseEmbedUrlTests(unittest.TestCase):
    """COARSE_EMBED_URL: stage A (papers_coarse) embeds the query with its own
    (bge-base) endpoint instead of reusing the bge-small vector chunk search
    already computed. Off by default (COARSE_EMBED_URL=""), which must leave
    HierSearchTests in test_hier_search.py byte-for-byte unaffected.
    """

    COARSE_EMBED_URL = "http://coarse-embed.local/embed"

    def setUp(self):
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        main.coarse_embedding_cache.data.clear()
        main.embedding_cache.data.clear()
        self.small_vector = [0.1, 0.2, 0.3]
        patcher = patch.object(main, "embed_query", return_value=self.small_vector)
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
        url_patch = patch.object(main, "COARSE_EMBED_URL", self.COARSE_EMBED_URL)
        url_patch.start()
        self.addCleanup(url_patch.stop)

    def _run(self, body, fake, coarse_embed_side_effect):
        def post(url, json=None, timeout=None):
            if url == self.COARSE_EMBED_URL:
                return coarse_embed_side_effect(json, timeout)
            return fake.post(url, json=json, timeout=timeout)
        with patch.object(main.requests, "post", side_effect=post):
            return main._run_search(body)

    def test_coarse_endpoint_called_with_query_true_and_its_vector_feeds_only_coarse_search(self):
        coarse_vector = [0.9, 0.8, 0.7]
        coarse = [_coarse_hit("p1", 0.80)]
        chunks = [_chunk_hit("c1", "p1", 0.85)]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        calls = []

        def coarse_embed(json_body, timeout):
            calls.append((json_body, timeout))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"vector": coarse_vector})
            return resp

        self._run(_body(), fake, coarse_embed)

        # the coarse endpoint was hit exactly once, asking for a query embedding
        self.assertEqual(len(calls), 1)
        body, timeout = calls[0]
        self.assertEqual(body["query"], True)
        self.assertEqual(body["text"], "hierarchical search test")
        self.assertEqual(timeout, main.COARSE_EMBED_TIMEOUT)

        # its vector is what reached papers_coarse (Stage A)...
        coarse_call = next(
            j for (u, j) in fake.calls
            if u.endswith(f"/collections/{main.COARSE_COLLECTION}/points/search")
        )
        self.assertEqual(coarse_call["vector"], coarse_vector)

        # ...while chunk search (Stage B, papers_fulltext) still got the old
        # bge-small vector -- the two must never be swapped or mixed.
        stage_b_call = next(
            j for (u, j) in fake.calls
            if u.endswith("/points/search")
            and any(c.get("key") == "arxiv_id" for c in (j.get("filter") or {}).get("must", []))
        )
        self.assertEqual(stage_b_call["vector"], self.small_vector)

    def test_coarse_endpoint_result_is_cached_by_text(self):
        coarse_vector = [0.5, 0.5, 0.5]
        coarse = [_coarse_hit("p1", 0.80)]
        chunks = [_chunk_hit("c1", "p1", 0.85)]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)
        calls = []

        def coarse_embed(json_body, timeout):
            calls.append(json_body)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"vector": coarse_vector})
            return resp

        self._run(_body(), fake, coarse_embed)
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        self._run(_body(), fake, coarse_embed)

        self.assertEqual(len(calls), 1, "second call should be served from coarse_embedding_cache")

    def test_coarse_embed_timeout_falls_back_to_old_path_with_warning(self):
        old_hits = [_chunk_hit("o1", "old1", 0.90), _chunk_hit("o2", "old2", 0.85)]
        points = {h["id"]: h for h in old_hits}
        fake = FakeHierQdrant([], [], old_hits, points)

        def coarse_embed(json_body, timeout):
            raise main.requests.Timeout("coarse embed service timed out")

        with patch.object(main.log, "warning") as warn_mock:
            result = self._run(_body(), fake, coarse_embed)

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(ids, ["old1", "old2"])
        self.assertNotIn("global_channel", result)
        for r in result["results"]:
            self.assertNotIn("paper_score", r)
            self.assertNotEqual(r.get("found_via"), "paper")
        warn_mock.assert_called_once()
        self.assertIn("stage A/B failed", warn_mock.call_args[0][0])

    def test_coarse_embed_error_response_falls_back_to_old_path(self):
        old_hits = [_chunk_hit("o1", "old1", 0.90)]
        points = {h["id"]: h for h in old_hits}
        fake = FakeHierQdrant([], [], old_hits, points)

        def coarse_embed(json_body, timeout):
            resp = MagicMock()
            resp.raise_for_status = MagicMock(
                side_effect=main.requests.HTTPError("500 server error"))
            return resp

        result = self._run(_body(), fake, coarse_embed)

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(ids, ["old1"])


if __name__ == "__main__":
    unittest.main()
