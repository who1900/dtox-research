import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from ops import restore_cache_tails as restore
from ops.repair_section_labels import _sha


class FakeQdrant:
    def __init__(self, points):
        self.points = {point["id"]: json.loads(json.dumps(point)) for point in points}

    def scroll(self, **kwargs):
        aid = kwargs["scroll_filter"]["must"][0]["match"]["value"]
        return ([point for point in self.points.values()
                 if point["payload"].get("arxiv_id") == aid], None)


class RestoreCacheTailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache_dir = self.root / "chunk_cache"
        self.section_dir = self.root / "section_cache"
        self.cache_dir.mkdir()
        self.section_dir.mkdir()
        self.aid = "simd:0047"
        self.cache_path = self.cache_dir / "simd_0047.json"
        self.manifest_path = self.section_dir / "simd_0047.json"
        self.source_path = self.root / "source.md"
        self.journal = self.root / "journal.json"
        self.state_path = self.root / "state.db"
        self.fts_path = self.root / "fts.db"
        self.rowmap_path = self.root / "rowmap.db"
        self.chunks = [self.chunk(i, f"Existing text {i}.") for i in range(2)]
        self.tail = self.chunk(2, "A unique source-backed tail sentence.")
        self.cache_path.write_bytes(restore.labels._json_bytes(self.chunks))
        self.cache_before = self.cache_path.read_bytes()
        self.source_path.write_text("# Specification\n\n" + self.tail["text"] + "\n", encoding="utf-8")

        self.state_path.touch()
        with closing(sqlite3.connect(self.state_path)) as conn, conn:
            conn.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT,fulltext_source TEXT)")
            conn.execute("INSERT INTO papers VALUES(?,?,?)", (self.aid, "done", "spec-markdown"))
            conn.execute("CREATE TABLE paper_active(arxiv_id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE paper_publications(arxiv_id TEXT,revision TEXT)")

        self.points = [self.point(chunk) for chunk in self.chunks + [self.tail]]
        self.qdrant = FakeQdrant(self.points)
        with closing(sqlite3.connect(self.fts_path)) as conn, conn:
            conn.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,"
                         "section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED)")
            for point in self.points:
                payload = point["payload"]
                conn.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type,layers,year) "
                             "VALUES(?,?,?,?,?,?,?)", (payload["text"], point["id"], self.aid,
                             payload["section_type"], payload["element_type"], payload["layers"], payload["year"]))
        st = self.fts_path.stat()
        with closing(sqlite3.connect(self.rowmap_path)) as conn, conn:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.executemany("INSERT INTO meta VALUES(?,?)", [
                ("complete", "1"),
                ("source_identity", json.dumps([str(self.fts_path.resolve()), st.st_dev, st.st_ino]))])
            conn.execute("CREATE TABLE rows(rowid INTEGER PRIMARY KEY,point_id TEXT NOT NULL,arxiv_id TEXT NOT NULL)")
            conn.execute("CREATE INDEX rows_paper ON rows(arxiv_id)")
            for rowid, point in enumerate(self.points, 1):
                conn.execute("INSERT INTO rows VALUES(?,?,?)", (rowid, point["id"], self.aid))

    def tearDown(self):
        self.temp.cleanup()

    def chunk(self, index, text):
        return {"chunk_index": index, "section_title": "Specification", "section_type": "method",
                "element_type": "prose", "has_math": False, "has_algorithm": False,
                "part_index": 0, "part_total": 1, "text": text}

    def point(self, chunk):
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.aid}#{chunk['chunk_index']}"))
        payload = dict(chunk, arxiv_id=self.aid, layers="crypto", year=2025)
        return {"id": pid, "payload": payload}

    def prepare(self):
        return restore.prepare_plan(self.aid, state_db=self.state_path, fts_db=self.fts_path,
            rowmap_db=self.rowmap_path, cache_dir=self.cache_dir, section_cache_dir=self.section_dir,
            source_path=self.source_path, journal_path=self.journal, qdrant=self.qdrant,
            expected_cache_sha256=_sha(self.cache_before),
            expected_source_sha256=_sha(self.source_path.read_bytes()))

    def test_prefix_mismatch_blocks_prepare(self):
        self.qdrant.points[self.points[0]["id"]]["payload"]["section_title"] = "Changed title"
        with self.assertRaises(restore.RepairError):
            self.prepare()
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.cache_path.read_bytes(), self.cache_before)

    def test_append_uses_existing_payload_and_preserves_prefix_then_rolls_back(self):
        plan = self.prepare()
        self.assertTrue(plan["source_proof"]["exact_unique_text_matches"])
        restore.apply_plan(self.journal, qdrant=self.qdrant)
        after = json.loads(self.cache_path.read_bytes())
        self.assertEqual(after[:2], self.chunks)
        self.assertEqual(after[2], self.tail)
        self.assertEqual(self.qdrant.points, {point["id"]: point for point in self.points})
        restore.rollback_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(self.cache_path.read_bytes(), self.cache_before)

    def test_ambiguous_source_anchor_is_recorded_without_rejecting_payload_restore(self):
        self.source_path.write_text(self.tail["text"] + "\n" + self.tail["text"], encoding="utf-8")
        plan = self.prepare()
        self.assertFalse(plan["source_proof"]["exact_unique_text_matches"])
        self.assertEqual(plan["source_proof"]["tail"][0]["occurrences"], 2)
        self.assertFalse(self.manifest_path.exists())


if __name__ == "__main__":
    unittest.main()
