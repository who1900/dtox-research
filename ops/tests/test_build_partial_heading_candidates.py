import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ops import build_partial_heading_candidates as build
from pipeline import document_cache


class PartialHeadingCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache_dir = self.root / "chunk_cache"
        self.fulltext_dir = self.root / "fulltext_cache"
        self.latex_dir = self.root / "latex_cache"
        for path in (self.cache_dir, self.fulltext_dir, self.latex_dir):
            path.mkdir()
        self.paper_id = "acl:2025.test-1.1"
        self.source = ("# Methodology\nAlpha unique\n"
                       "# Results\nBeta unique\n"
                       "# Analysis\nRepeat\n"
                       "# Conclusion\nRepeat").encode()
        self.chunks = [
            self._chunk(0, "old heading", "Alpha unique", "other"),
            self._chunk(1, "Old ambiguous", "Repeat", "other"),
            self._chunk(2, "Lost heading", "not present in source", "other"),
        ]
        self.cache_bytes = json.dumps(self.chunks).encode()
        name = document_cache.safe_id(self.paper_id)
        (self.cache_dir / (name + ".json")).write_bytes(self.cache_bytes)
        (self.fulltext_dir / (name + ".tex")).write_bytes(self.source)
        self.census = self.root / "census.sqlite"
        conn = sqlite3.connect(self.census)
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('run_complete','1')")
        conn.execute("CREATE TABLE papers(paper_id TEXT PRIMARY KEY,input_fingerprint TEXT,"
                      "candidate_status TEXT,heading_label_status TEXT,cache_sha256 TEXT,"
                      "source_sha256 TEXT,source_location TEXT,fulltext_source TEXT)")
        conn.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?)", (
            self.paper_id, "fingerprint", "identity_or_text_changed",
            "unmatched_or_ambiguous_cached_heading", hashlib.sha256(self.cache_bytes).hexdigest(),
            hashlib.sha256(self.source).hexdigest(), "fulltext_cache", "acl-pdf"))
        conn.commit()
        conn.close()
        self.report = self.root / "partial.sqlite"

    @staticmethod
    def _chunk(index, title, text, section_type):
        return {"chunk_index": index, "section_title": title, "section_type": section_type,
                "text": text, "element_type": "prose", "has_math": False, "has_algorithm": False}

    def _run(self):
        return build.run(self.census, self.report, cache_dir=self.cache_dir,
                         fulltext_cache_dir=self.fulltext_dir, latex_cache_dir=self.latex_dir)

    def test_partial_candidate_changes_only_uniquely_anchored_chunk_and_keeps_complete_map(self):
        result = self._run()
        self.assertTrue(result["complete"])
        self.assertEqual(result["candidates"], 1)
        resumed = self._run()
        self.assertEqual((resumed["processed"], resumed["reused"], resumed["candidates"]), (0, 1, 1))
        conn = sqlite3.connect(self.report)
        row = conn.execute("SELECT candidate_status,exact_text_ids,labels_source_verified,"
                           "partial_label_scope,mapped_chunk_count,ambiguous_chunk_count,"
                           "unmatched_chunk_count,current_changed_chunk_count,"
                           "candidate_changes_json,candidate_label_map_json FROM papers").fetchone()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
        conn.close()
        self.assertNotIn("text", columns)
        self.assertEqual(row[:8], ("heading_label_candidate", 0, 1, 1, 1, 1, 1, 1))
        self.assertEqual(json.loads(row[8]), [
            {"chunk_index": 0, "section_title": "Methodology", "section_type": "method"}])
        full = json.loads(row[9])
        self.assertEqual(full, [
            {"chunk_index": 0, "section_title": "Methodology", "section_type": "method"},
            {"chunk_index": 1, "section_title": "Old ambiguous", "section_type": "other"},
            {"chunk_index": 2, "section_title": "Lost heading", "section_type": "other"},
        ])

    def test_ambiguous_and_unmatched_only_chunks_are_not_candidates(self):
        # The unique chunk already has the source label, leaving only refusal cases.
        self.chunks[0]["section_title"] = "Methodology"
        self.chunks[0]["section_type"] = "method"
        self.cache_bytes = json.dumps(self.chunks).encode()
        name = document_cache.safe_id(self.paper_id)
        (self.cache_dir / (name + ".json")).write_bytes(self.cache_bytes)
        conn = sqlite3.connect(self.census)
        conn.execute("UPDATE papers SET cache_sha256=?", (hashlib.sha256(self.cache_bytes).hexdigest(),))
        conn.commit()
        conn.close()

        result = self._run()
        self.assertEqual(result["candidates"], 0)
        conn = sqlite3.connect(self.report)
        row = conn.execute("SELECT candidate_status,ambiguous_chunk_count,unmatched_chunk_count,"
                           "labels_source_verified FROM papers").fetchone()
        conn.close()
        self.assertEqual(row, ("partial_no_label_changes", 1, 1, 0))

    def test_hash_mismatch_is_not_source_anchored(self):
        conn = sqlite3.connect(self.census)
        conn.execute("UPDATE papers SET source_sha256=?", ("0" * 64,))
        conn.commit()
        conn.close()
        result = self._run()
        self.assertEqual(result["candidates"], 0)
        conn = sqlite3.connect(self.report)
        row = conn.execute("SELECT candidate_status,labels_source_verified,heading_label_status "
                           "FROM papers").fetchone()
        conn.close()
        self.assertEqual(row, ("census_hash_mismatch", 0, "census_hash_mismatch"))


if __name__ == "__main__":
    unittest.main()
