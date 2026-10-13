import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from ops import audit_projection_coverage as audit
from pipeline import document_cache


class ProjectionCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data = self.base / "data"
        self.data.mkdir()
        self.cache_dir = self.data / "chunk_cache"
        self.cache_dir.mkdir()
        self.census = self.base / "census.sqlite"
        self.rowmap = self.base / "rowmap.sqlite"
        self.report = self.base / "projection.sqlite"
        self.paper_id = "fixture:paper"
        self.chunks = [
            {"chunk_index": 0, "text": "Method", "section_title": "Method", "section_type": "method",
             "element_type": "prose", "has_math": False, "has_algorithm": False},
            {"chunk_index": 1, "text": "Old figure", "section_title": "References", "section_type": "appendix",
             "element_type": "figure", "has_math": False, "has_algorithm": False},
            {"chunk_index": 2, "text": "Result", "section_title": "Results", "section_type": "experiments",
             "element_type": "prose", "has_math": False, "has_algorithm": False},
        ]
        self.cache_bytes = json.dumps(self.chunks).encode()
        path = self.cache_dir / (document_cache.safe_id(self.paper_id) + ".json")
        path.write_bytes(self.cache_bytes)
        conn = sqlite3.connect(self.census)
        conn.execute("CREATE TABLE meta(key TEXT,value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('run_complete','1')")
        conn.execute("CREATE TABLE papers(paper_id TEXT,input_fingerprint TEXT,cache_state TEXT,"
                     "cache_sha256 TEXT,chunk_count INTEGER)")
        conn.execute("INSERT INTO papers VALUES (?,?,?,?,?)", (
            self.paper_id, "fp-1", "present", hashlib.sha256(self.cache_bytes).hexdigest(), len(self.chunks)))
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.rowmap)
        conn.execute("CREATE TABLE meta(key TEXT,value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('complete','1')")
        conn.execute("CREATE TABLE rows(rowid INTEGER PRIMARY KEY,point_id TEXT,arxiv_id TEXT)")
        conn.execute("CREATE INDEX rows_paper ON rows(arxiv_id)")
        conn.commit()
        conn.close()

    @staticmethod
    def point_id(paper_id, index):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{paper_id}#{index}"))

    def add_fts(self, *point_ids):
        conn = sqlite3.connect(self.rowmap)
        conn.executemany("INSERT INTO rows(point_id,arxiv_id) VALUES (?,?)",
                         [(point_id, self.paper_id) for point_id in point_ids])
        conn.commit()
        conn.close()

    def test_reports_cache_backed_missing_projection_separately_from_filter_exclusions(self):
        expected0 = self.point_id(self.paper_id, 0)
        filtered1 = self.point_id(self.paper_id, 1)
        foreign = str(uuid.uuid4())
        self.add_fts(expected0, expected0, filtered1, foreign)

        summary = audit.run(self.data, self.census, self.rowmap, self.report, chunk_dir=self.cache_dir)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["qdrant_checked"], False)
        conn = sqlite3.connect(self.report)
        row = conn.execute("SELECT fts_row_count,fts_duplicate_point_id_count,fts_unmapped_row_count,"
                           "cache_current_expected_count,cache_currently_filtered_count,"
                           "cache_backed_missing_current_indices_json,projection_status "
                           "FROM papers WHERE paper_id=?", (self.paper_id,)).fetchone()
        conn.close()
        self.assertEqual(row, (4, 1, 1, 2, 1, "[2]", "duplicate_or_unmapped_fts_ids"))
        self.assertEqual(summary["cache_backed_missing_current_projection"], 1)

    def test_missing_currently_filtered_index_is_not_a_recovery_candidate(self):
        self.add_fts(self.point_id(self.paper_id, 0), self.point_id(self.paper_id, 2))
        audit.run(self.data, self.census, self.rowmap, self.report, chunk_dir=self.cache_dir)
        conn = sqlite3.connect(self.report)
        row = conn.execute("SELECT cache_backed_missing_current_count,current_filter_excluded_absent_count,"
                           "projection_status FROM papers WHERE paper_id=?", (self.paper_id,)).fetchone()
        conn.close()
        self.assertEqual(row, (0, 1, "aligned_with_current_filter"))

    def test_changed_cache_after_census_is_not_compared(self):
        path = self.cache_dir / (document_cache.safe_id(self.paper_id) + ".json")
        path.write_bytes(self.cache_bytes + b" ")
        audit.run(self.data, self.census, self.rowmap, self.report, chunk_dir=self.cache_dir)
        conn = sqlite3.connect(self.report)
        row = conn.execute("SELECT cache_state,cache_backed_missing_current_count "
                           "FROM papers WHERE paper_id=?", (self.paper_id,)).fetchone()
        conn.close()
        self.assertEqual(row, ("changed_since_census", 0))


if __name__ == "__main__":
    unittest.main()
