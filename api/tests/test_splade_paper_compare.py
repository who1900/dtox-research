import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
import splade_paper_compare as compare


def vector(index, size=768):
    values = [0.0] * size
    values[index] = 1.0
    return values


class SpladePaperCompareTests(unittest.TestCase):
    def fixture(self):
        return [("frozen.json", {"groups": [{
            "id": "g", "layer": "web3",
            "targets": [{"canonical_id": "target", "aliases": ["target-alias"]}],
            "hard_negatives": [{"id": "negative"}],
            "phrases": [{"id": "known", "query": "oracle feeds"},
                        {"id": "empty", "query": "the and"}],
        }]})]

    def corpus(self):
        return [
            {"paper_id": "target", "aliases": ["target-alias"], "layers": ["web3"],
             "title": "Oracle Feeds", "abstract": "Decentralized oracle feeds deliver data.",
             "dense_vector": vector(0), "sparse_vector": {"indices": [3], "values": [2]}},
            {"paper_id": "negative", "layers": ["web3"], "title": "Oracle Feeds Comparator",
             "abstract": "A close but different oracle mechanism.", "dense_vector": vector(1),
             "sparse_vector": {"indices": [4], "values": [1]}},
            {"paper_id": "other-layer", "layers": ["ai-agents"], "title": "Oracle feeds",
             "abstract": "Must be excluded by layer.", "dense_vector": vector(2),
             "sparse_vector": {"indices": [3], "values": [100]}},
            {"paper_id": "target", "aliases": ["another-twin"], "layers": ["web3"],
             "title": "Duplicate twin must not vote", "abstract": "Oracle feeds",
             "dense_vector": vector(5), "sparse_vector": {"indices": [3], "values": [999]}},
        ]

    def test_layer_filter_dedupe_sparse_zero_and_exact_fts_empty(self):
        dense = {"oracle feeds": vector(0), "the and": vector(0)}
        sparse = {"oracle feeds": {"indices": [3], "values": [1]},
                  "the and": {"indices": [999], "values": [1]}}
        report = compare.evaluate(self.corpus(), self.fixture(), dense, sparse)
        known, empty = report["phrases"]
        self.assertEqual(report["corpus"]["canonical_papers"], 3)
        self.assertEqual(known["eligible_count"], 2)
        self.assertNotIn("other-layer", known["ranked_ids"]["fts5"])
        self.assertEqual(known["ranked_ids"]["dense"].count("target"), 1)
        self.assertEqual(known["ranked_ids"]["splade"], ["target"])
        self.assertEqual(empty["ranked_ids"]["fts5"], [])
        self.assertEqual(empty["ranked_ids"]["splade"], [])
        self.assertFalse(empty["methods"]["fts5"]["hit@40"])
        self.assertTrue(known["methods"]["fts5"]["hit@10"])
        self.assertEqual(report["summary"]["dense"]["denominator"], 2)

    def test_empty_abstract_has_no_fake_fts_and_equal_channel_budget(self):
        rows = []
        for i in range(46):
            rows.append({"paper_id": f"p{i:02}", "layers": ["web3"],
                "title": f"Paper {i}", "abstract": "" if i == 0 else "token oracle",
                "text": "token oracle fulltext for zero-abstract row", "dense_vector": vector(i),
                "sparse_vector": {"indices": [7], "values": [float(46 - i)]}})
        fixture = [("one.json", {"groups": [{"id": "g", "layer": "web3",
            "targets": [{"canonical_id": "p00"}], "phrases": [{"query": "fulltext"}]}]})]
        report = compare.evaluate(rows, fixture, {"fulltext": vector(0)},
                                  {"fulltext": {"indices": [7], "values": [1]}})
        phrase = report["phrases"][0]
        self.assertNotIn("p00", phrase["ranked_ids"]["fts5"])
        self.assertEqual(phrase["ranked_ids"]["splade"][0], "p00")
        for channel in ("dense", "fts5", "splade"):
            self.assertLessEqual(len(phrase["ranked_ids"][channel]), compare.CHANNEL_BUDGET)
        self.assertEqual(phrase["eligible_count"], 46)
        self.assertEqual(report["corpus"]["abstract_missing_or_short_lt80_count"], 46)
        self.assertEqual(phrase["production_stage_a"]["baseline_candidate_count"], 40)
        self.assertGreaterEqual(phrase["production_stage_a"]["with_splade_candidate_count"], 40)

    def test_non_768_dense_queries_rejected_and_multi_target_union_is_not_incremental(self):
        fixture = [("one.json", {"groups": [{"id": "g", "layer": "web3",
            "targets": [{"canonical_id": "a"}, {"canonical_id": "b"}],
            "phrases": [{"query": "q"}]}]})]
        with self.assertRaises(ValueError):
            compare.evaluate(self.corpus(), fixture, {"q": [0.0] * 384},
                             {"q": {"indices": [], "values": []}})
        self.assertFalse(compare._incremental_target_hit({"a", "b"}, {"a"}, {"a", "b"}))

    def test_citation_prior_resorts_only_dense_raw_top40(self):
        high_cited = [0.8, 0.6] + [0.0] * 766
        higher_cosine = [0.81, (1 - 0.81 ** 2) ** 0.5] + [0.0] * 766
        rows = [
            {"paper_id": "cited", "in_citations": 1000, "layers": ["web3"],
             "title": "", "abstract": "", "dense_vector": high_cited,
             "sparse_vector": {"indices": [], "values": []}},
            {"paper_id": "cosine", "in_citations": 0, "layers": ["web3"],
             "title": "", "abstract": "", "dense_vector": higher_cosine,
             "sparse_vector": {"indices": [], "values": []}},
        ]
        fixture = [("prior.json", {"groups": [{"id": "g", "layer": "web3",
            "targets": [{"canonical_id": "cited"}], "phrases": [{"query": "query term"}]}]})]
        report = compare.evaluate(rows, fixture, {"query term": vector(0)},
                                  {"query term": {"indices": [], "values": []}})
        self.assertEqual(report["phrases"][0]["ranked_ids"]["dense"][0], "cited")
        self.assertEqual(report["citation_prior"]["weight"], 0.03)

    def test_absent_target_stays_in_all_method_denominators(self):
        fixture = [("missing.json", {"groups": [{"id": "missing", "layer": "web3",
            "targets": [{"canonical_id": "not-in-corpus"}],
            "phrases": [{"query": "unmatched"}]}]})]
        report = compare.evaluate(self.corpus(), fixture, {"unmatched": vector(0)},
                                  {"unmatched": {"indices": [999], "values": [1]}})
        phrase = report["phrases"][0]
        self.assertEqual(phrase["missing_target_ids"], ["not-in-corpus"])
        self.assertFalse(phrase["target_present"])
        self.assertEqual(report["summary"]["rrf_all"]["denominator"], 1)
        self.assertEqual(report["summary"]["rrf_all"]["hit@40"], 0.0)

    def test_empty_abstract_never_indexes_truncated_text_fallback(self):
        conn = compare._build_fts([{"paper_id": "p", "layers": ["web3"], "title": "",
                                    "abstract": "", "text": "hidden truncated body token"}])
        self.addCleanup(conn.close)
        self.assertEqual(compare._fts_rank(conn, "truncated body", "web3"), [])

    def test_full_cli_accepts_plain_dense_map_and_parity_sparse_envelope(self):
        query = "needle target"
        fixture = {"groups": [{"id": "g", "layer": "web3",
            "targets": [{"canonical_id": "paper-one"}],
            "phrases": [{"id": "p", "query": query}]}]}
        corpus_row = {"paper_id": "paper-one", "layers": ["web3"],
            "title": "Needle", "abstract": "Needle target paper",
            "dense_vector": vector(0),
            "sparse_vector": {"indices": [1, 2, 3], "values": [1, 2, 3]}}
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            corpus_path = temp / "corpus.jsonl"
            fixture_path = temp / "fixture.json"
            dense_path = temp / "dense.json"
            sparse_path = temp / "sparse.json"
            output_path = temp / "comparison.json"
            corpus_path.write_text(json.dumps(corpus_row) + "\n", encoding="utf-8")
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            dense_path.write_text(json.dumps({query: vector(0)}), encoding="utf-8")
            sparse_path.write_text(json.dumps({"query_vectors": {query: {
                "indices": [1, 2, 3], "values": [1, 2, 3]}}}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = compare.main(["--corpus", str(corpus_path), "--fixture", str(fixture_path),
                    "--dense-queries", str(dense_path), "--sparse-queries", str(sparse_path),
                    "--output", str(output_path)])
            result = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(result["phrases"][0]["ranked_ids"]["splade"], ["paper-one"])
        self.assertTrue(result["phrases"][0]["methods"]["splade"]["hit@40"])


if __name__ == "__main__":
    unittest.main()
