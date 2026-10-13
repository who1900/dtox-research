import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ops import extend_heading_candidates as extend
from pipeline import document_cache


class ExtendHeadingCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache_dir = self.root / "chunk_cache"
        self.fulltext_dir = self.root / "fulltext_cache"
        self.latex_dir = self.root / "latex_cache"
        self.cache_dir.mkdir()
        self.fulltext_dir.mkdir()
        self.latex_dir.mkdir()
        self.census = self.root / "census.sqlite"
        self.paper_id = "pmlr:v1/paper"
        self.source = b"# **4. Methodology**\nAlpha beta"
        self.cache = json.dumps([
            {"chunk_index": 0, "section_title": "4. Methodology", "section_type": "other",
             "text": "Alpha", "element_type": "prose", "has_math": False, "has_algorithm": False},
            {"chunk_index": 1, "section_title": "4. Methodology", "section_type": "other",
             "text": "beta", "element_type": "prose", "has_math": False, "has_algorithm": False},
        ]).encode()
        safe = document_cache.safe_id(self.paper_id)
        (self.cache_dir / (safe + ".json")).write_bytes(self.cache)
        (self.fulltext_dir / (safe + ".tex")).write_bytes(self.source)
        conn = sqlite3.connect(self.census)
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('run_complete','1')")
        conn.execute("CREATE TABLE papers(paper_id TEXT PRIMARY KEY,input_fingerprint TEXT,"
                      "cache_sha256 TEXT,source_sha256 TEXT,source_location TEXT,candidate_status TEXT,"
                      "exact_text_ids INTEGER,label_change_count INTEGER,candidate_changes_json TEXT,"
                      "candidate_label_map_json TEXT,diagnostic TEXT)")
        conn.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            self.paper_id, "fp", hashlib.sha256(self.cache).hexdigest(),
            hashlib.sha256(self.source).hexdigest(), "fulltext_cache", "identity_or_text_changed",
            0, 0, "[]", "[]", "original-diagnostic"))
        conn.commit()
        conn.close()

    def test_hash_guarded_extension_emits_complete_labels_without_claiming_text_identity(self):
        result = extend.run(self.census, cache_dir=self.cache_dir,
                            fulltext_cache_dir=self.fulltext_dir, latex_cache_dir=self.latex_dir)
        self.assertEqual((result["processed"], result["verified"]), (1, 1))
        conn = sqlite3.connect(self.census)
        row = conn.execute("SELECT candidate_status,exact_text_ids,labels_source_verified,"
                           "heading_label_status,diagnostic,candidate_changes_json,"
                           "candidate_label_map_json FROM papers WHERE paper_id=?", (self.paper_id,)).fetchone()
        conn.close()
        self.assertEqual(row[:5], ("heading_label_candidate", 0, 1, "candidate", "original-diagnostic"))
        self.assertEqual(json.loads(row[5]), [
            {"chunk_index": 0, "section_title": "Methodology", "section_type": "method"},
            {"chunk_index": 1, "section_title": "Methodology", "section_type": "method"},
        ])
        self.assertEqual(json.loads(row[6]), json.loads(row[5]))

    def test_changed_source_hash_is_reported_without_overwriting_original_diagnostic(self):
        conn = sqlite3.connect(self.census)
        conn.execute("UPDATE papers SET source_sha256=? WHERE paper_id=?", ("0" * 64, self.paper_id))
        conn.commit()
        conn.close()
        result = extend.run(self.census, cache_dir=self.cache_dir,
                            fulltext_cache_dir=self.fulltext_dir, latex_cache_dir=self.latex_dir)
        self.assertEqual(result["not_verified"], 1)
        conn = sqlite3.connect(self.census)
        row = conn.execute("SELECT candidate_status,labels_source_verified,heading_label_status,"
                           "diagnostic FROM papers WHERE paper_id=?", (self.paper_id,)).fetchone()
        conn.close()
        self.assertEqual(row, ("identity_or_text_changed", 0, "census_hash_mismatch", "original-diagnostic"))


if __name__ == "__main__":
    unittest.main()
