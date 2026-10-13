import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))
import splade_compare as runner


class FakeEncoder:
    def __init__(self):
        self.calls = 0

    def passage_embed(self, texts):
        self.calls += len(texts)
        return [{"indices": [1], "values": [float(len(text))]} for text in texts]


class SpladeCompareTests(unittest.TestCase):
    def test_sparse_exact_dot_and_dense_alias(self):
        rows = [
            {"chunk_id": "a", "paper_id": "paper-a", "text": "", "sparse_vector": {"indices": [1, 3], "values": [2, 1]}, "dense": [1, 0]},
            {"chunk_id": "b", "paper_id": "paper-b", "text": "", "sparse_vector": {"indices": [2], "values": [4]}, "dense_vector": [0, 1]},
        ]
        self.assertEqual(runner.sparse_dot({"indices": [1, 2], "values": [3, 1]}, rows), {"a": 6.0, "b": 4.0})
        self.assertEqual(runner.dense_cosine([1, 0], rows), {"a": 1.0, "b": 0.0})

    def test_unknown_bm25_query_has_no_hits_or_rrf_vote(self):
        rows = [
            {"chunk_id": "c1", "paper_id": "p1", "text": "known vocabulary", "title": ""},
            {"chunk_id": "c2", "paper_id": "p2", "text": "other terms", "title": ""},
        ]
        scores = runner.bm25_scores("zzzxxyy", rows)
        paper_scores, _, order = runner._collapse_paper_scores(scores, rows)
        bm25_ranking = runner._rank_papers(paper_scores, order)
        self.assertEqual(scores, {})
        self.assertEqual(bm25_ranking, [])
        self.assertEqual(runner.reciprocal_rank_fusion(["p1"], bm25_ranking), {"p1": 1 / 61})

    def test_evaluation_deduplicates_papers_before_fusion_and_reports_aliases(self):
        corpus = [
            {"chunk_id": "t-1", "paper_id": "target-alias", "text": "needle target", "layers": ["x"], "dense": [1, 0],
             "sparse_vector": {"indices": [1], "values": [2]}},
            {"chunk_id": "t-2", "paper_id": "target-alias", "text": "needle target", "layers": ["x"], "dense": [1, 0],
             "sparse_vector": {"indices": [1], "values": [2]}},
            {"chunk_id": "n-1", "paper_id": "negative", "text": "other", "layers": ["y"], "dense": [0, 1],
             "sparse_vector": {"indices": [2], "values": [3]}},
        ]
        fixtures = [("frozen.json", {"groups": [{"id": "g", "targets": [{"canonical_id": "target", "aliases": ["target-alias"]}],
            "hard_negatives": [{"id": "negative"}], "phrases": [{"id": "p", "query": "needle", "dense": [1, 0]}]}]})]
        result = runner.evaluate(corpus, fixtures, lambda query, **kwargs: {
            "sparse": {"indices": [1], "values": [1]}, "elapsed": {"splade": 0.01}})
        phrase = result["phrases"][0]
        self.assertEqual(len(phrase["raw_top_ids"]["dense"]), 2)
        self.assertEqual(phrase["raw_top_ids"]["dense"][0], "target-alias")
        self.assertEqual(phrase["best_chunk_ids"]["dense"]["target-alias"], "t-1")
        self.assertEqual(result["corpus_coverage"]["frozen.json"]["covered_count"], 1)
        self.assertEqual(result["fixture_summary"]["frozen.json"]["phrases"], 1)
        self.assertIn("bm25_simple", result["summary"])
        self.assertIn("rrf_dense_bm25_simple_splade", result["summary"])
        self.assertEqual(len(result["summary"]), 6)
        self.assertNotIn("bm25", result["summary"])

    def test_encode_checkpoint_resume_and_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus_path, output_path, checkpoint = root / "corpus.jsonl", root / "encoded.jsonl", root / "checkpoint.json"
            rows = [{"chunk_id": f"c{i}", "paper_id": f"p{i}", "text": f"text {i}", "layers": [], "title": ""}
                    for i in range(4)]
            corpus_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            model = FakeEncoder()
            partial = runner.encode_corpus(corpus_path, output_path, checkpoint, model=model, max_seconds=0.000001)
            self.assertFalse(partial["complete"])
            first_count = model.calls
            resumed = runner.encode_corpus(corpus_path, output_path, checkpoint, model=model)
            self.assertTrue(resumed["complete"])
            self.assertEqual(model.calls, 4)
            self.assertEqual(len(runner._jsonl(output_path)), 4)
            self.assertGreaterEqual(first_count, 0)
            changed = [dict(rows[0], text="changed"), *rows[1:]]
            corpus_path.write_text("".join(json.dumps(row) + "\n" for row in changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalidated"):
                runner.encode_corpus(corpus_path, output_path, checkpoint, model=model)

    def test_dense_query_export_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queries.json"
            vector = [0.0] * 384
            path.write_text(json.dumps({"query text": vector}), encoding="utf-8")
            self.assertEqual(runner._dense_query_map(path)["query text"], vector)
            path.write_text(json.dumps([{"query": "query text", "vector": vector}]), encoding="utf-8")
            self.assertEqual(runner._dense_query_map(path)["query text"], vector)


if __name__ == "__main__":
    unittest.main()
