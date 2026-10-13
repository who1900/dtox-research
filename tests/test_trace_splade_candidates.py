import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from eval.trace_splade_candidates import _instrument, _set_env


def _hit(aid, score, text="private full text"):
    return {"id": f"chunk-{aid}", "score": score,
            "payload": {"arxiv_id": aid, "title": f"Title {aid}",
                        "section_type": "method", "text": text}}


def fake_app():
    calls = []
    app = SimpleNamespace(
        HIER_CHUNK_LEXICAL=False, HIER_SEARCH=True, HIER_CHUNKS=120,
        MIN_RELEVANCE_SCORE=.79, MIN_RELEVANCE_BY_LAYER={"web3": .7},
        HIER_FUSION="rrf", HIER_W_DENSE=2, HIER_W_LEX=.8,
        HIER_W_GLOBAL=1, HIER_GRAPH=True, HIER_GROUPED_SEARCH=False,
        HIER_RANK_CHANNELS=False, BOILERPLATE_FILTER=True, SOFT_LAYERS=True,
        _hit_is_boilerplate=lambda hit: "boilerplate" in hit["payload"].get("text", ""),
    )

    def stage_b(vector, must, arxiv_ids, dense_limit, timeout):
        calls.append("stage_b")
        return [_hit(aid, score) for aid, score in (("a", .4), ("b", .5), ("c", .7))]

    def hier_run(vector, qfilter, must, body, limit, dense_limit, dense_timeout,
                 layers, query="", paper_lexical_future=None):
        calls.append("hier")
        hits = app._hier_stage_b_hits(vector, must, ["a", "b", "c"], dense_limit, dense_timeout)
        return hits, [], [], {"paper_scores": {"a": .1},
                              "fusion_ranks": {"a": {"dense": 1}},
                              "fusion_scores": {"a": .2}, "lex_exempt": ["a"]}

    def dedupe(hits, floor, exempt, limit):
        calls.append("dedupe")
        return [{"arxiv_id": hit["payload"]["arxiv_id"], "score": hit["score"],
                 "title": hit["payload"]["title"], "section_type": "method"}
                for hit in hits if hit["score"] >= floor or hit["payload"]["arxiv_id"] in exempt][:limit]

    app._hier_stage_b_hits = stage_b
    app._hier_run = hier_run
    app._dedupe_preferring_content = dedupe
    app._collapse_twins = lambda results: results
    app._cap_gh_docs = lambda results, query, limit: (results, 0)
    app._inject_exact = lambda results, query, constraints, limit, timeout: results
    app._merge_soft_layers = lambda primary, partner, limit: primary["results"][:limit]

    def run_search(body, x_api_key=None, dense_timeout=1, embed_wait_timeout=1,
                   embed_http_timeout=1, _top=True):
        calls.append("run_search")
        if not _top:
            app._hier_stage_b_hits(None, [], ["a", "b", "c"], 120, 1)
            return {"results": []}
        hits, _, _, _extra = app._hier_run(None, None, [], body, 10, 120, 1,
                                           None, query=body.query)
        results = app._dedupe_preferring_content(hits, .6, {"a"}, 10)
        app._collapse_twins(results)
        app._cap_gh_docs(results, body.query, 10)
        app._inject_exact(results, body.query, [], 10, 1)
        app._merge_soft_layers({"results": results}, {"results": []}, 10)
        return {"results": results}

    app._run_search = run_search
    app.calls = calls
    return app


class TraceSpladeCandidatesTests(unittest.TestCase):
    def test_trace_observes_candidates_without_mutating_order_or_scores(self):
        app = fake_app()
        _config, _originals = _instrument(app)
        body = SimpleNamespace(query="sample query", layer="web3")
        payload = app._run_search(body)

        self.assertEqual(app.calls, ["run_search", "hier", "stage_b", "dedupe"])
        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["a", "c"])
        self.assertEqual([row["score"] for row in payload["results"]], [.4, .7])
        events = payload["diagnostic_trace"]["events"]
        dedupe = next(event for event in events if event["stage"] == "dedupe_preferring_content")
        self.assertEqual(dedupe["floor"], .6)
        self.assertEqual(dedupe["lex_exempt"], ["a"])
        hier = next(event for event in events if event["stage"] == "hier_run")
        self.assertEqual([hit["arxiv_id"] for hit in hier["ordered_hits"]["items"]], ["a", "b", "c"])
        self.assertNotIn("text", hier["ordered_hits"]["items"][0])
        self.assertEqual([hit["score"] for hit in hier["ordered_hits"]["items"]], [.4, .5, .7])

    def test_nested_search_is_logged_under_outer_trace_only(self):
        app = fake_app()
        inner_original = app._run_search

        def run_search(body, x_api_key=None, dense_timeout=1, embed_wait_timeout=1,
                       embed_http_timeout=1, _top=True):
            if not _top:
                app._hier_stage_b_hits(None, [], ["a"], 120, 1)
                return {"results": []}
            worker = threading.Thread(target=lambda: app._run_search(body, _top=False),
                                      name="nested-search")
            worker.start()
            worker.join()
            return {"results": []}

        app._run_search = run_search
        _instrument(app)
        payload = app._run_search(SimpleNamespace(query="threaded", layer="web3"))
        events = payload["diagnostic_trace"]["events"]
        self.assertTrue(any(event["stage"] == "stage_b" and event["thread"] == "nested-search"
                            for event in events))
        self.assertTrue(all(event["trace_id"] == events[0]["trace_id"] for event in events))
        self.assertIn("stage_b", app.calls)
        self.assertIsNot(inner_original, app._run_search)

    def test_explicit_production_config_is_zero(self):
        app = fake_app()
        config, _ = _instrument(app)
        self.assertIs(config["HIER_CHUNK_LEXICAL"], False)
        payload = app._run_search(SimpleNamespace(query="config", layer="web3"))
        self.assertIs(payload["diagnostic_trace"]["config"]["HIER_CHUNK_LEXICAL"], False)

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_set_env("unused", lambda _path: {"base": True}), {"base": True})
            self.assertEqual(__import__("os").environ["HIER_CHUNK_LEXICAL"], "0")


if __name__ == "__main__":
    unittest.main()
