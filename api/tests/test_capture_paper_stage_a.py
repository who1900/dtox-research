import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
import capture_paper_stage_a as capture


class CapturePaperStageATests(unittest.TestCase):
    def test_fts_timeout_matches_production_default(self):
        self.assertEqual(capture.FTS_SECONDS, 2)

    def fixture_files(self, root):
        paths = []
        for n in range(2):
            groups = [{"id": f"g{n}", "layer": "web3",
                "targets": [{"canonical_id": "target", "aliases": ["target-copy"]}],
                "hard_negatives": [{"id": "negative"}],
                "phrases": [{"id": f"p{n}-{i}", "query": f"query {n}-{i}"} for i in range(16 if n == 0 else 17)]}]
            path = root / f"fixture{n}.json"
            path.write_text(json.dumps({"groups": groups}), encoding="utf-8")
            paths.append(path)
        return paths

    def test_loads_33_queries_and_independent_canonical_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture_files(Path(tmp))
            rows, _ = capture.load_queries(paths, {"target-copy": "target", "negative-copy": "negative"})
            self.assertEqual(len(rows), 33)
            self.assertEqual(rows[0]["targets"], ["target"])
            self.assertEqual(rows[0]["hard_negatives"], ["negative"])

    def test_qdrant_request_and_coarse_citation_prior(self):
        session = Mock()
        response = Mock()
        response.json.return_value = {"result": [
            {"payload": {"arxiv_id": "copy", "in_citations": 1000}, "score": .7},
            {"payload": {"arxiv_id": "plain", "in_citations": 0}, "score": .74}]}
        session.post.return_value = response
        raw, prior, result, error = capture._dense(session, "http://127.0.0.1:16335", [0.0]*768,
                                                   "ai-agents", {"copy": "canonical"}, 1e20)
        self.assertIsNone(error)
        self.assertEqual(raw, ["copy", "plain"])
        self.assertEqual(prior, ["plain", "copy"])
        self.assertEqual(result, ["plain", "canonical"])
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["filter"]["must"][0]["match"], {"any": ["ai-agents"]})
        self.assertEqual(body["limit"], 40)
        self.assertEqual(body["with_payload"], ["arxiv_id", "title", "in_citations"])
        self.assertFalse(body["with_vector"])
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_fts_production_query_weights_filters_and_web3_balance(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE papers_fts USING fts5(arxiv_id UNINDEXED,title,abstract,layers UNINDEXED,year UNINDEXED,tokenize='porter unicode61')")
        rows = [(f"wp:{i}", "oracle oracle", "oracle", "|web3|", "2024") for i in range(3)]
        rows += [(f"eip:{i}", "oracle oracle", "oracle", "|web3|", "2024") for i in range(3)]
        rows += [(f"p:{i}", "oracle oracle", "oracle", "|web3|", "2024") for i in range(125)]
        conn.executemany("INSERT INTO papers_fts VALUES(?,?,?,?,?)", rows)
        try:
            result = capture._lexical(conn, "oracle the", "web3", {}, 1e20)
        finally:
            conn.close()
        self.assertEqual(len(result), 15)
        self.assertEqual(result[:2], ["wp:0", "wp:1"])
        self.assertEqual(capture.fts_query_any("oracle the"), '"oracle"')

    def test_lexical_cutoff_precedes_canonical_dedupe(self):
        class Cursor:
            def fetchall(self):
                return [(f"copy-{i}",) for i in range(15)] + [("next",)]
            def __iter__(self):
                return iter(self.fetchall())
        class Conn:
            def set_progress_handler(self, *_args):
                pass
            def execute(self, _sql, params):
                self.params = params
                return Cursor()
        conn = Conn()
        source_ids = capture._lexical(conn, "needle", "ai-agents", {}, 1e20)
        canonical = list(dict.fromkeys(capture._canon(x, {f"copy-{i}": "twin" for i in range(15)}) for x in source_ids))
        self.assertEqual(len(source_ids), 15)
        self.assertEqual(canonical, ["twin"])
        self.assertEqual(conn.params[-1], 15)

    def test_qdrant_is_loopback_only_on_port_16335(self):
        self.assertEqual(capture._validate_qdrant("http://localhost:16335/"), "http://localhost:16335")
        for url in ("http://127.0.0.1:6333", "http://example.com:16335", "http://127.0.0.1:16335@evil:16335"):
            with self.assertRaises(ValueError):
                capture._validate_qdrant(url)

    def test_query_vectors_are_checked_before_any_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures = self.fixture_files(root)
            (root / "twins.json").write_text('{"canonical":{}}', encoding="utf-8")
            (root / "queries.json").write_text(json.dumps({f"query 0-{i}": [0]*768 for i in range(16)}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing cached"):
                capture.capture(fixtures, root / "queries.json", root / "twins.json", root / "missing.db",
                                "http://127.0.0.1:16335", root / "out.json")


if __name__ == "__main__":
    unittest.main()
