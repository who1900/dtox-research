import hashlib
import json
import base64
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ops.corpus_quality import prepare_label_boundaries as prepare
from pipeline import document_cache, extractor


class PrepareLabelBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache_dir = self.root / "chunk_cache"
        self.source_dir = self.root / "fulltext_cache"
        self.latex_dir = self.root / "latex_cache"
        for directory in (self.cache_dir, self.source_dir, self.latex_dir):
            directory.mkdir()
        self.aid = "pmlr:v306/fixture26a"
        self.source = b"# Methods\nFirst passage.\n# Results\nSecond passage."
        self.chunks, _ = extractor.build_chunks_markdown(
            self.source.decode(), document_kind="paper")
        for chunk in self.chunks:
            chunk["section_title"] = "Legacy title"
            chunk["section_type"] = "other"
        cache_bytes = json.dumps(self.chunks).encode()
        name = document_cache.safe_id(self.aid)
        (self.cache_dir / (name + ".json")).write_bytes(cache_bytes)
        (self.source_dir / (name + ".tex")).write_bytes(self.source)

        self.census = self.root / "census.sqlite"
        conn = sqlite3.connect(self.census)
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('run_complete','1')")
        conn.execute("CREATE TABLE papers(paper_id TEXT PRIMARY KEY,cache_sha256 TEXT,"
                      "source_sha256 TEXT,source_location TEXT,fulltext_source TEXT,parser_mode TEXT)")
        conn.execute("INSERT INTO papers VALUES (?,?,?,?,?,?)", (
            self.aid, hashlib.sha256(cache_bytes).hexdigest(),
            hashlib.sha256(self.source).hexdigest(), "fulltext_cache", "pmlr-html", "markdown"))
        conn.commit()
        conn.close()

        self.ledger = self.root / "label-run.sqlite"
        conn = sqlite3.connect(self.ledger)
        conn.execute("CREATE TABLE papers(paper_id TEXT PRIMARY KEY,status TEXT,attempt INTEGER,"
                      "journal_path TEXT,detail TEXT,updated_at TEXT)")
        conn.execute("INSERT INTO papers VALUES (?,?,?,?,?,?)",
                     (self.aid, "failed", 2, "/saved/attempt-0002.json", "prior failure", "now"))
        conn.commit()
        conn.close()
        self.output = self.root / "retry.sqlite"

    def test_prepares_only_failed_source_bound_row_without_mutating_inputs(self):
        census_before = self.census.read_bytes()
        ledger_before = self.ledger.read_bytes()

        result = prepare.prepare(self.census, self.ledger, self.output,
            cache_dir=self.cache_dir, fulltext_cache_dir=self.source_dir,
            latex_cache_dir=self.latex_dir, expected_failed_count=1)

        self.assertEqual(result["prepared"], 1)
        conn = sqlite3.connect(self.output)
        meta = dict(conn.execute("SELECT key,value FROM meta"))
        row = conn.execute("SELECT paper_id,candidate_status,exact_text_ids,"
            "labels_source_verified,candidate_changes_json,candidate_label_map_json FROM papers").fetchone()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
        conn.close()
        self.assertEqual(meta["run_complete"], "1")
        self.assertEqual(meta["selected_failed_count"], "1")
        self.assertEqual(row[:4], (self.aid, "candidate", 1, 1))
        self.assertEqual(len(json.loads(row[4])), 2)
        self.assertEqual(len(json.loads(row[5])), 2)
        self.assertEqual(self.census.read_bytes(), census_before)
        self.assertEqual(self.ledger.read_bytes(), ledger_before)

    def test_verified_unresolved_subset_is_source_bound_and_leaves_original_ledger_unchanged(self):
        original_cache = (self.cache_dir / (document_cache.safe_id(self.aid) + ".json")).read_bytes()
        current_chunks = json.loads(original_cache)
        parsed, _ = extractor.build_chunks_markdown(self.source.decode(), document_kind="paper")
        current_chunks[0]["section_title"] = parsed[0]["section_title"]
        current_chunks[0]["section_type"] = parsed[0]["section_type"]
        current_cache = json.dumps(current_chunks).encode()
        (self.cache_dir / (document_cache.safe_id(self.aid) + ".json")).write_bytes(current_cache)
        detail = json.dumps({"unresolved_chunks": [{"chunk_index": 0}]})
        parent = self.root / "prior-label.json"
        parent.write_text(json.dumps({"schema": 1, "kind": "section-label-repair", "phase": "applied",
            "papers": [{"arxiv_id": self.aid,
                "cache_before": base64.b64encode(original_cache).decode(),
                "cache_after": base64.b64encode(current_cache).decode()}]}), encoding="utf-8")
        conn = sqlite3.connect(self.ledger)
        conn.execute("UPDATE papers SET status='verified',journal_path=?,detail=? WHERE paper_id=?",
                     (str(parent), detail, self.aid))
        conn.commit()
        conn.close()
        census_before = self.census.read_bytes()
        ledger_before = self.ledger.read_bytes()

        result = prepare.prepare(self.census, self.ledger, self.output,
            cache_dir=self.cache_dir, fulltext_cache_dir=self.source_dir,
            latex_cache_dir=self.latex_dir, subset="verified-unresolved",
            expected_verified_count=1, expected_unresolved_count=1,
            expected_resolvable_count=1, expected_resolved_chunk_count=1)

        self.assertEqual(result["prepared"], 1)
        self.assertEqual(result["subset"], "verified-unresolved")
        conn = sqlite3.connect(self.output)
        meta = dict(conn.execute("SELECT key,value FROM meta"))
        row = conn.execute("SELECT paper_id,candidate_status,exact_text_ids,labels_source_verified,"
            "cache_sha256,frozen_cache_sha256,candidate_changes_json,candidate_label_map_json FROM papers").fetchone()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
        conn.close()
        self.assertEqual(meta["candidate_subset"], "verified-unresolved-label-retry")
        self.assertEqual(meta["source_verified_unresolved_count"], "1")
        self.assertEqual(meta["source_unresolved_chunk_count"], "1")
        self.assertEqual(meta["source_bound_selected_count"], "1")
        self.assertEqual(row[:4], (self.aid, "candidate", 1, 1))
        self.assertEqual(row[4], hashlib.sha256(current_cache).hexdigest())
        self.assertEqual(row[5], hashlib.sha256(original_cache).hexdigest())
        self.assertEqual(len(json.loads(row[6])), 1)
        self.assertEqual(len(json.loads(row[7])), 2)
        self.assertEqual(self.census.read_bytes(), census_before)
        self.assertEqual(self.ledger.read_bytes(), ledger_before)


if __name__ == "__main__":
    unittest.main()
