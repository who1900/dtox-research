import unittest
from unittest.mock import patch, MagicMock

import requests
from fastapi import HTTPException

from api import main


def _hit(point_id, score):
    return {"id": point_id, "score": score}


def _point(point_id, arxiv_id):
    return {"id": point_id, "payload": {"arxiv_id": arxiv_id, "title": f"paper {arxiv_id}",
                                        "text": "body"}}


class FakeQdrant:
    """Stands in for the two Qdrant endpoints the search path calls.

    search_responses: {layer_or_None: [hit, ...]}  (already sorted best-first)
    points_by_id: {point_id: point}   used to answer the /points batch fetch
    """

    def __init__(self, search_responses, points_by_id):
        self.search_responses = search_responses
        self.points_by_id = points_by_id
        self.search_calls = []   # list of (url, json_body)
        self.points_calls = []   # list of json_body

    def post(self, url, json=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/points/search"):
            self.search_calls.append((url, json))
            must = (json.get("filter") or {}).get("must") or []
            layer = None
            for clause in must:
                if clause.get("key") == "layers":
                    layer = clause["match"]["any"][0]
            hits = self.search_responses.get(layer, [])
            resp.json = MagicMock(return_value={"result": hits[: json["limit"]]})
        elif url.endswith("/points"):
            self.points_calls.append(json)
            ids = json["ids"]
            points = [self.points_by_id[i] for i in ids if i in self.points_by_id]
            resp.json = MagicMock(return_value={"result": points})
        else:
            raise AssertionError(f"unexpected URL {url}")
        return resp


def _base_body(**kwargs):
    defaults = dict(query="two phase retrieval test", hybrid=False, diagnose=False,
                     dedupe=True, limit=4)
    defaults.update(kwargs)
    return main.SearchBody(**defaults)


class TwoPhaseSearchTests(unittest.TestCase):
    def setUp(self):
        # avoid stale cross-test hits in the module-level TTL caches
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        self.vector = [0.1, 0.2, 0.3]
        patcher = patch.object(main, "embed_query", return_value=self.vector)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(main, "_paper_facts", return_value={})
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def _run(self, body, fake):
        with patch.object(main.requests, "post", side_effect=fake.post):
            return main._run_search(body)

    def test_single_layer_phase1_requests_no_payload(self):
        hits = [_hit(f"p{i}", 0.9 - i * 0.01) for i in range(10)]
        points = {h["id"]: _point(h["id"], f"arx-{h['id']}") for h in hits}
        fake = FakeQdrant({"web3": hits}, points)

        body = _base_body(layer="web3", min_score=0.0, limit=3)
        result = self._run(body, fake)

        self.assertEqual(len(fake.search_calls), 1)
        _, sent = fake.search_calls[0]
        self.assertEqual(sent["with_payload"], False)
        self.assertEqual(len(fake.points_calls), 1)
        self.assertEqual(fake.points_calls[0]["with_payload"], True)
        self.assertEqual(result["count"], 3)

    def test_all_layers_merge_and_single_batch_hydrate(self):
        layers = sorted(main.ALLOWED_LAYERS)
        search_responses = {}
        points = {}
        for li, layer in enumerate(layers):
            layer_hits = [_hit(f"{layer}-{i}", 0.9 - i * 0.01) for i in range(10)]
            search_responses[layer] = layer_hits
            for h in layer_hits:
                points[h["id"]] = _point(h["id"], f"arx-{h['id']}")
        fake = FakeQdrant(search_responses, points)

        body = _base_body(min_score=0.0, limit=4)
        result = self._run(body, fake)

        # one phase-1 search per layer
        self.assertEqual(len(fake.search_calls), len(layers))
        for _, sent in fake.search_calls:
            self.assertEqual(sent["with_payload"], False)
        # phase 2 hydrates the merged top-K in exactly one batch call
        self.assertEqual(len(fake.points_calls), 1)
        requested_ids = fake.points_calls[0]["ids"]
        # K = limit*3, capped by however many phase-1 candidates exist
        self.assertEqual(len(requested_ids), min(4 * 3, 10 * len(layers)))
        self.assertEqual(result["count"], 4)

    def test_order_and_score_preserved_through_hydration(self):
        hits = [_hit("a", 0.95), _hit("b", 0.90), _hit("c", 0.85)]
        points = {
            "a": _point("a", "arx-a"), "b": _point("b", "arx-b"), "c": _point("c", "arx-c"),
        }
        fake = FakeQdrant({"web3": hits}, points)
        body = _base_body(layer="web3", min_score=0.0, limit=3)
        result = self._run(body, fake)

        ids_in_order = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(ids_in_order, ["arx-a", "arx-b", "arx-c"])
        scores = [r["score"] for r in result["results"]]
        self.assertEqual(scores, [0.95, 0.90, 0.85])

    def test_dedupe_triggers_one_extra_hydration_round(self):
        # top-K (K = limit*3 = 12) is dominated by duplicate arxiv_ids so
        # dedupe leaves fewer than `limit` distinct papers; the next slice of
        # phase-1 candidates should be hydrated in exactly one more batch call.
        limit = 4
        dup_hits = [_hit(f"dup{i}", 0.99 - i * 0.001) for i in range(12)]
        fresh_hits = [_hit(f"fresh{i}", 0.5 - i * 0.001) for i in range(8)]
        hits = dup_hits + fresh_hits
        points = {h["id"]: _point(h["id"], "arx-SAME") for h in dup_hits}
        for h in fresh_hits:
            points[h["id"]] = _point(h["id"], f"arx-{h['id']}")
        fake = FakeQdrant({"web3": hits}, points)

        body = _base_body(layer="web3", min_score=0.0, limit=limit, dedupe=True)
        result = self._run(body, fake)

        self.assertEqual(len(fake.points_calls), 2, "expected exactly one extra round")
        first_round_ids = set(fake.points_calls[0]["ids"])
        second_round_ids = set(fake.points_calls[1]["ids"])
        self.assertTrue(first_round_ids.issubset({h["id"] for h in dup_hits}))
        self.assertTrue(second_round_ids)
        arxiv_ids = [r["arxiv_id"] for r in result["results"]]
        self.assertEqual(len(arxiv_ids), len(set(arxiv_ids)), "deduped by arxiv_id")
        self.assertGreater(len(arxiv_ids), 1, "extra round supplied more distinct papers")

    def test_phase2_error_falls_back_to_dense_error_partial(self):
        hits = [_hit("a", 0.95)]

        def flaky_post(url, json=None, timeout=None):
            if url.endswith("/points/search"):
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                resp.json = MagicMock(return_value={"result": hits})
                return resp
            raise requests.RequestException("boom")

        lexical_hit = {
            "arxiv_id": "lex:1", "score": None, "niche_score": None,
            "title": "lexical only", "channels": ["lexical"],
        }
        body = _base_body(layer="web3", min_score=0.0, limit=3, hybrid=True)
        with (patch.object(main.requests, "post", side_effect=flaky_post),
              patch.object(main, "_bm25_candidates", return_value=["lex:1"]),
              patch.object(main, "_lexical_fallback_results", return_value=[lexical_hit]),
              patch.object(main, "_lookup_note", return_value=None)):
            result = main._run_search(body)

        self.assertTrue(result.get("partial"))
        self.assertIn("retrieval_warning", result)
        self.assertEqual(result["results"][0]["arxiv_id"], "lex:1")

    def test_phase2_error_raises_when_no_lexical_fallback(self):
        hits = [_hit("a", 0.95)]

        def flaky_post(url, json=None, timeout=None):
            if url.endswith("/points/search"):
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                resp.json = MagicMock(return_value={"result": hits})
                return resp
            raise requests.RequestException("boom")

        body = _base_body(layer="web3", min_score=0.0, limit=3, hybrid=False)
        with patch.object(main.requests, "post", side_effect=flaky_post):
            with self.assertRaises(HTTPException):
                main._run_search(body)


if __name__ == "__main__":
    unittest.main()
