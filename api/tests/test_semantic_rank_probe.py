import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import semantic_rank_probe as probe
from api.semantic_rank_experiment import build_candidates, rerank_candidates


class SemanticRankProbeTests(unittest.TestCase):
    def test_below_floor_hit_is_kept_only_as_unverified_candidate(self):
        hits = [{"id": "chunk", "score": 0.21, "payload": {"arxiv_id": "p1",
                "title": "Origin", "text": "Useful evidence passage"}}]
        candidates = build_candidates(hits, lambda value: value, lambda _hit: False,
                                      fusion_ranks={"p1": {"dense": 27}}, limit=20)
        self.assertEqual(candidates[0]["channel_ranks"]["dense"], 27)
        self.assertTrue(candidates[0]["verification_required"])
        self.assertEqual(candidates[0]["retrieval_role"], "unverified_source_candidate")

    def test_rerank_fallback_and_invalid_scores_preserve_baseline(self):
        candidate = {"arxiv_id": "p", "title": "Paper", "passages": [{"text": "passage"}]}
        baseline = [candidate]
        for scorer in (lambda *_: (_ for _ in ()).throw(TimeoutError()), lambda *_: [float("nan")]):
            report = rerank_candidates("query", baseline, scorer)
            self.assertEqual(report["status"], "fallback")
            self.assertIs(report["candidates"], baseline)
            self.assertFalse(report["evidence"])

    def test_same_pool_run_restores_wrappers_and_does_not_pass_oracle_to_selector(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            api_dir = root / "api"
            api_dir.mkdir()
            main_file = api_dir / "main.py"
            main_file.write_text("fixture main", encoding="utf-8")
            fixture = root / "fixture.json"
            fixture.write_text(json.dumps({"groups": [{"id": "g", "layer": "web3",
                "targets": [{"id": "target"}], "hard_negatives": [],
                "phrases": [{"id": "p", "query": "q"}, {"id": "p2", "query": "q2"}]}]}), encoding="utf-8")
            vectors = root / "vectors.json"
            vectors.write_text(json.dumps({"q": [0.0] * 384, "q2": [0.0] * 384}), encoding="utf-8")
            base_vectors = root / "base.json"
            base_vectors.write_text(json.dumps({"q": [0.0] * 768, "q2": [0.0] * 768}), encoding="utf-8")
            output_path = root / "out.json"
            class Body:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs
                def model_copy(self, **_kwargs):
                    return self
            app = types.ModuleType("main")
            app.SearchBody = Body
            app.SOURCE_DISCOVERY = True
            app.TITLE_BUILD_PRIORITY = False
            for name, value in {"HIER_SEARCH": True, "HIER_CHUNK_LEXICAL": True,
                "HIER_GRAPH": False, "HIER_GROUPED_SEARCH": True, "HIER_RANK_CHANNELS": True,
                "RERANK_ENABLED": True, "PAPER_LEXICAL": False,
                "PAPER_LEXICAL_SOURCE_BALANCE": False, "HIER_FUSION": "mix",
                "HIER_W_DENSE": 1, "HIER_W_LEX": 1, "HIER_W_GLOBAL": 1}.items():
                setattr(app, name, value)
            app.embed_query = lambda *_a, **_k: []
            app.embed_query_coarse = lambda *_a, **_k: []
            app.canonical_id = lambda value: value
            app._hit_is_boilerplate = lambda _hit: False
            app.MIN_RELEVANCE_BY_LAYER = {"web3": 0.7}
            app.MIN_RELEVANCE_SCORE = 0.5
            hit = {"id": "h", "score": 0.3, "payload": {"arxiv_id": "target", "title": "Origin",
                   "text": "A substantial relevant paragraph", "relevance_floor": 0.8}}
            seen_pool = []
            def hier(*_args, **_kwargs):
                return [hit], [], [], {"fusion_ranks": {"target": {"dense": 1}}}
            app._hier_run = hier
            def search(*_args, **_kwargs):
                body = _args[0]
                app._hier_run(None, None, None, body, query=body.kwargs["query"])
                return {"results": [], "partial": False}
            app._run_search = search
            args = types.SimpleNamespace(fixtures=[fixture], small_queries=vectors,
                base_queries=base_vectors, api_dir=api_dir,
                expected_main_sha256=hashlib.sha256(b"fixture main").hexdigest(),
                budget_seconds=10, per_query_seconds=3, max_queries=2,
                rerank_url="http://localhost/rerank", output=output_path)
            import eval.splade_candidate_probe as helpers
            old_mod = sys.modules.get("main")
            scorer_calls = []
            def broken_scorer(_query, _pairs):
                scorer_calls.append(1)
                raise ConnectionError("response detail must never be persisted")
            def capture_build(hits_, *rest, **kwargs):
                seen_pool.extend(hits_)
                return build_candidates(hits_, *rest, **kwargs)
            with patch.dict(sys.modules, {"main": app}), \
                 patch.object(helpers, "_load_query_vectors", side_effect=lambda _p, _q, dim, _l: {q: [0.0] * dim for q in ("q", "q2")}), \
                 patch.object(probe, "_score_post", return_value=broken_scorer), \
                 patch("api.semantic_rank_experiment.build_candidates", side_effect=capture_build):
                result = probe.run(args)
            if old_mod is None:
                sys.modules.pop("main", None)
            else:
                sys.modules["main"] = old_mod
            self.assertEqual(len(seen_pool), 2, result)
            self.assertEqual(result["records"][0]["candidate_count"], 1)
            self.assertEqual(len(scorer_calls), 1)
            self.assertEqual(result["records"][0]["scorer_error"], "ConnectionError")
            self.assertEqual(result["records"][1]["scorer_error"], "scorer_disabled_after_failure")
            self.assertFalse(result["records"][1]["ce_complete"])
            self.assertEqual(result["summary"]["attempted_queries"], 2)
            self.assertEqual(result["records"][0]["baseline_primary10"], [])
            self.assertTrue(result["records"][0]["source_rank_provenance"][0]["verification_required"])
            self.assertTrue(app.SOURCE_DISCOVERY)
            self.assertFalse(app.TITLE_BUILD_PRIORITY)
            self.assertIs(app._hier_run, hier)
            self.assertTrue(output_path.is_file())

    def test_runtime_flags_and_environment_protocol(self):
        config = probe._environment()
        self.assertEqual(config["TITLE_BUILD_PRIORITY"], "1")
        self.assertEqual(config["SOURCE_DISCOVERY"], "0")
        self.assertEqual(config["API_KEYS_PATH"], "/__codex_probe_no_api_keys__")
        self.assertEqual(probe.MAX_BUDGET_SECONDS, 180)
        self.assertEqual(probe.MAX_QUERY_SECONDS, 12)

    def test_scorer_uses_passages_protocol_and_batches_at_twelve(self):
        calls = []
        class Response:
            def __init__(self, count):
                self.count = count
            def raise_for_status(self):
                return None
            def json(self):
                return {"scores": [float(i) for i in range(self.count)]}
        fake_requests = types.SimpleNamespace(post=lambda url, json, timeout: (
            calls.append((url, json, timeout)) or Response(len(json["passages"]))))
        pairs = [{"title": f"title-{i}", "passage": f"body-{i}"} for i in range(25)]
        with patch.dict(sys.modules, {"requests": fake_requests}):
            scorer = probe._score_post("http://127.0.0.1:8006/rerank", 3)
            scores = scorer("q", pairs)
        self.assertEqual(len(scores), 25)
        self.assertEqual([len(call[1]["passages"]) for call in calls], [12, 12, 1])
        self.assertEqual(set(calls[0][1]), {"query", "passages"})
        self.assertNotIn("pairs", calls[0][1])
        self.assertEqual(calls[0][1]["passages"][0], "title-0\nbody-0")
        self.assertTrue(all(call[2] <= 3 for call in calls))
        self.assertEqual(len(calls), 3)
        self.assertEqual(scorer.last_batches, [12, 12, 1])

    def test_scorer_deadline_stops_before_next_batch(self):
        calls = []
        class Response:
            def raise_for_status(self):
                return None
            def json(self):
                return {"scores": [0.5] * 12}
        fake_requests = types.SimpleNamespace(post=lambda *_a, **_kw: (calls.append(1) or Response()))
        pairs = [{"title": "t", "passage": str(i)} for i in range(24)]
        with patch.dict(sys.modules, {"requests": fake_requests}), \
             patch.object(probe.time, "monotonic", side_effect=[0.0, 0.0, 4.0]):
            scorer = probe._score_post("http://127.0.0.1:8006/rerank", 3)
            with self.assertRaises(TimeoutError):
                scorer("q", pairs)
        self.assertEqual(len(calls), 1)
        self.assertEqual(scorer.last_batches, [12])

    def test_fixture_alias_and_result_alias_share_canonical_rank(self):
        normalize = probe._canonical_normalizer(lambda value: value, {
            "legacy:paper": "canonical-paper", "canonical-paper": "canonical-paper"})
        target = normalize("canonical-paper")
        result = normalize("legacy:paper")
        metrics = probe._rank_metrics([result], {target}, set())
        self.assertTrue(metrics["hit"])
        self.assertEqual(metrics["mrr"], 1.0)

    def test_round_robin_selection_covers_groups_before_prefix(self):
        rows = [{"fixture": "f", "group_id": group, "phrase_id": str(i)}
                for group in ("a", "b", "c") for i in range(3)]
        selected = probe._select_queries(rows, 3)
        self.assertEqual({row["group_id"] for row in selected}, {"a", "b", "c"})

    def test_unattempted_fixture_groups_are_reported_as_failed_not_vacuous_success(self):
        groups = {("f", "g1"): {"phrase_count": 3}, ("f", "g2"): {"phrase_count": 3}}
        summary = probe._phrase_group_summary(groups, [{"fixture": "f", "group_id": "g1",
            "ce_complete": True, "cross_encoder20": {"hit": True}}])
        self.assertEqual(set(summary), {"f:g1", "f:g2"})
        self.assertFalse(summary["f:g1"]["success"])
        self.assertEqual(summary["f:g2"]["attempted"], 0)
        self.assertFalse(summary["f:g2"]["success"])


if __name__ == "__main__":
    unittest.main()
