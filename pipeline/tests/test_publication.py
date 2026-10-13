"""Bounded, offline checks for the first-publication journal."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from pipeline.publication import (PublicationBlocked, PublicationRetry,
                                  ensure_publication_schema, prepare_snapshot,
                                  publish_snapshot, stage_source)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT,parse_status TEXT,"
                                "title TEXT,year INTEGER,layers TEXT,source_url TEXT);"
                                "CREATE TABLE completion_history(arxiv_id TEXT PRIMARY KEY,first_known INTEGER,"
                                "completed INTEGER); CREATE TABLE completion_totals(singleton INTEGER PRIMARY KEY,"
                                "first_completed INTEGER DEFAULT 0);")
        self.conn.execute("INSERT INTO completion_totals VALUES(1,0)")
        self.conn.execute("INSERT INTO papers VALUES('p','chunked','ok','Title',2026,'web3','url')")
        self.conn.execute("INSERT INTO completion_history VALUES('p',1,0)")
        ensure_publication_schema(self.conn)
        (self.root / "chunk_cache").mkdir()
        self.cache = self.root / "chunk_cache" / "chunks.json"
        self.cache.write_text('[{"chunk_index":0,"text":"hello"}]', encoding="utf-8")
        self.conn.commit()

    def job(self):
        pt = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "p#0")), "vector": [0.2, 0.3],
              "payload": {"arxiv_id": "p", "chunk_index": 0, "text": "hello"}}
        return prepare_snapshot(self.conn, self.root, "p", [pt], self.cache), pt

    def test_ready_is_not_published_and_success_persists_reader_record(self):
        job, original = self.job()
        self.assertEqual(self.conn.execute("SELECT state FROM paper_publications").fetchone()[0], "ready")
        self.assertNotIn("publication_revision", original["payload"])
        self.assertEqual(original["payload"]["arxiv_id"], "p")
        ack = []
        def complete(paper_id, commit=False):
            self.conn.execute("UPDATE papers SET status='done' WHERE arxiv_id=?", (paper_id,))
            self.conn.execute("UPDATE completion_history SET completed=1 WHERE arxiv_id=?", (paper_id,))
            self.conn.execute("UPDATE completion_totals SET first_completed=first_completed+1")
            return True
        def preflight(_job, _points):
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(json.loads(self.conn.execute(
                "SELECT completed FROM paper_publications").fetchone()[0]), [])
            return True
        def qdrant_write(_job, points):
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(json.loads(self.conn.execute(
                "SELECT completed FROM paper_publications").fetchone()[0]), ["preflight"])
            ack.append(points)
            return True
        self.assertTrue(publish_snapshot(self.conn, job,
            qdrant_write, lambda _job, points: True, complete, preflight))
        self.assertEqual(self.conn.execute("SELECT state FROM paper_publications").fetchone()[0], "published")
        self.assertEqual(self.conn.execute("SELECT status FROM papers").fetchone()[0], "done")
        self.assertEqual(self.conn.execute("SELECT first_completed FROM completion_totals").fetchone()[0], 1)
        marker = ack[0][0]["payload"]
        self.assertEqual(marker["arxiv_id"], "p")
        self.assertEqual(marker["publication_revision"], job["revision"])
        with self.assertRaises(PublicationBlocked):
            prepare_snapshot(self.conn, self.root, "p", ack[0], self.cache)

    def test_fts_ack_then_completion_sql_failure_keeps_ready_and_counters(self):
        job, _ = self.job()
        calls = []
        def fail_complete(paper_id, commit=False):
            self.conn.execute("UPDATE completion_totals SET first_completed=99")
            raise sqlite3.OperationalError("simulated finalization failure")
        with self.assertRaises(PublicationRetry) as caught:
            publish_snapshot(self.conn, job, lambda *_: True,
                             lambda *_: calls.append("fts") or True, fail_complete, lambda *_: True)
        self.assertEqual(caught.exception.code, "completion_failed")
        self.assertEqual(calls, ["fts"])
        row = self.conn.execute("SELECT state,completed FROM paper_publications").fetchone()
        self.assertEqual(row["state"], "ready")
        self.assertEqual(json.loads(row["completed"]), ["fts", "preflight", "qdrant"])
        self.assertEqual(self.conn.execute("SELECT status FROM papers").fetchone()[0], "chunked")
        self.assertEqual(self.conn.execute("SELECT first_completed FROM completion_totals").fetchone()[0], 0)

    def test_false_fts_is_not_acknowledged_or_completed(self):
        job, _ = self.job()
        with self.assertRaises(PublicationRetry):
            publish_snapshot(self.conn, job, lambda *_: True, lambda *_: False,
                             lambda *_: self.fail("must not finalize"), lambda *_: True)
        self.assertEqual(self.conn.execute("SELECT state FROM paper_publications").fetchone()[0], "ready")
        self.assertEqual(self.conn.execute("SELECT status FROM papers").fetchone()[0], "chunked")

    def test_legacy_unknown_completed_and_cache_drift_block_before_callbacks(self):
        self.conn.execute("UPDATE completion_history SET first_known=0 WHERE arxiv_id='p'")
        self.conn.commit()
        with self.assertRaises(PublicationBlocked):
            self.job()
        self.conn.execute("UPDATE completion_history SET first_known=1,completed=0 WHERE arxiv_id='p'")
        self.conn.commit()
        job, _ = self.job()
        self.cache.write_text("changed", encoding="utf-8")
        with self.assertRaises(PublicationBlocked):
            publish_snapshot(self.conn, job, lambda *_: self.fail("Qdrant called"),
                             lambda *_: self.fail("FTS called"), lambda *_: self.fail("completed"),
                             lambda *_: self.fail("preflight called"))

    def test_null_first_known_is_not_proof_of_new_admission(self):
        self.conn.execute("UPDATE completion_history SET first_known=NULL WHERE arxiv_id='p'")
        self.conn.commit()
        with self.assertRaises(PublicationBlocked):
            self.job()

    def test_source_staging_does_not_touch_done_readable_state(self):
        self.conn.execute("UPDATE papers SET status='done' WHERE arxiv_id='p'")
        self.conn.commit()
        before = tuple(self.conn.execute("SELECT status,title FROM papers WHERE arxiv_id='p'").fetchone())
        result = stage_source(self.conn, self.root, "p", b"source text", {"origin": "fixture"})
        self.assertEqual(result["status"], "staged")
        self.assertEqual(tuple(self.conn.execute("SELECT status,title FROM papers WHERE arxiv_id='p'").fetchone()), before)
        self.assertIsNone(self.conn.execute("SELECT kind,state FROM paper_publications WHERE kind='snapshot'").fetchone())
        self.assertEqual(hashlib.sha256(b"source text").hexdigest(),
                         self.conn.execute("SELECT cache_sha256 FROM paper_publications WHERE kind='source'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
