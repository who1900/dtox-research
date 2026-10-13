"""Independent, bounded offline failure checks for publication ownership."""
import copy
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from pipeline.publication import (
    PublicationBlocked, PublicationRetry, ensure_publication_schema,
    prepare_snapshot, publish_snapshot, reconcile_publications,
)


class PublicationFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "state.db"
        self.conn = sqlite3.connect(self.database)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(lambda: self.conn.close())
        self.conn.executescript(
            "CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT,parse_status TEXT,"
            "title TEXT,year INTEGER,layers TEXT,source_url TEXT);"
            "CREATE TABLE completion_history(arxiv_id TEXT PRIMARY KEY,first_known INTEGER,completed INTEGER);"
            "CREATE TABLE completion_totals(singleton INTEGER PRIMARY KEY,first_completed INTEGER DEFAULT 0);"
            "INSERT INTO completion_totals VALUES(1,0);"
        )
        ensure_publication_schema(self.conn)
        (self.root / "chunk_cache").mkdir()
        self.completions = []
        for target in ("urllib.request.urlopen", "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.add_paper("p")

    def add_paper(self, paper):
        self.conn.execute("INSERT INTO papers VALUES(?,'chunked','ok','Title',2026,'web3','url')", (paper,))
        self.conn.execute("INSERT INTO completion_history VALUES(?,1,0)", (paper,))
        self.cache(paper).write_text('[{"chunk_index":0,"text":"hello"}]', encoding="utf-8")
        self.conn.commit()

    def cache(self, paper="p"):
        return self.root / "chunk_cache" / f"{paper}.json"

    def point(self, paper="p"):
        return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{paper}#0")),
                "vector": [0.2, 0.3],
                "payload": {"arxiv_id": paper, "chunk_index": 0, "text": "hello"}}

    def job(self, paper="p"):
        return prepare_snapshot(self.conn, self.root, paper, [self.point(paper)], self.cache(paper))

    def complete(self, paper, commit=False):
        self.assertFalse(commit)
        self.completions.append(paper)
        self.conn.execute("UPDATE papers SET status='done' WHERE arxiv_id=?", (paper,))
        self.conn.execute("UPDATE completion_history SET completed=1 WHERE arxiv_id=?", (paper,))
        self.conn.execute("UPDATE completion_totals SET first_completed=first_completed+1")
        return True

    def journal(self, paper="p"):
        return self.conn.execute("SELECT * FROM paper_publications WHERE arxiv_id=?", (paper,)).fetchone()

    def assert_uncompleted(self, paper="p"):
        self.assertEqual(self.conn.execute("SELECT status FROM papers WHERE arxiv_id=?", (paper,)).fetchone()[0], "chunked")
        self.assertEqual(self.conn.execute("SELECT completed FROM completion_history WHERE arxiv_id=?", (paper,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT first_completed FROM completion_totals").fetchone()[0], 0)

    def no_callback(self, *_args, **_kwargs):
        self.fail("external or completion callback must not run")

    def test_resume_after_qdrant_ack_keeps_vectors_without_reembedding(self):
        job = self.job()
        qdrant, fts = [], []
        def write_qdrant(_job, points):
            qdrant.append(copy.deepcopy(points))
            return True
        def reject_fts(_job, points):
            fts.append(copy.deepcopy(points))
            return False
        with patch("time.time", return_value=1000):
            self.assertEqual(reconcile_publications(self.conn, self.root, write_qdrant, reject_fts, self.complete,
                                                    lambda *_: True), 0)
        self.assertEqual(json.loads(self.journal()["completed"]), ["preflight", "qdrant"])
        self.assert_uncompleted()
        self.conn.close()
        self.conn = sqlite3.connect(self.database)
        self.conn.row_factory = sqlite3.Row
        def accept_fts(_job, points):
            fts.append(copy.deepcopy(points))
            self.assertEqual(points, qdrant[0])
            self.assertEqual(points[0]["vector"], [0.2, 0.3])
            return True
        with patch("time.time", return_value=1031), \
                patch("pipeline.publication.prepare_snapshot", side_effect=AssertionError("no restaging/reembedding")):
            self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback, accept_fts, self.complete,
                                                    lambda *_: True), 1)
        self.assertEqual(len(qdrant), 1)
        self.assertEqual(len(fts), 2)
        self.assertTrue(publish_snapshot(self.conn, job, self.no_callback, self.no_callback, self.no_callback,
                                         lambda *_: True))
        self.assertEqual(self.completions, ["p"])
        self.assertEqual(self.conn.execute("SELECT first_completed FROM completion_totals").fetchone()[0], 1)

    def test_cache_tamper_during_fts_ack_blocks_final_completion(self):
        job = self.job()
        def tamper(_job, _points):
            self.cache().write_text("changed during FTS", encoding="utf-8")
            return True
        with self.assertRaises(PublicationBlocked) as caught:
            publish_snapshot(self.conn, job, lambda *_: True, tamper, self.no_callback, lambda *_: True)
        self.assertEqual(caught.exception.code, "cache_hash_mismatch")
        self.assertEqual(json.loads(self.journal()["completed"]), ["fts", "preflight", "qdrant"])
        self.assertEqual(self.journal()["state"], "ready")
        self.assert_uncompleted()

    def test_metadata_drift_blocks_before_callbacks(self):
        job = self.job()
        self.conn.execute("UPDATE papers SET title='changed' WHERE arxiv_id='p'")
        self.conn.commit()
        with self.assertRaises(PublicationBlocked) as caught:
            publish_snapshot(self.conn, job, self.no_callback, self.no_callback, self.no_callback, self.no_callback)
        self.assertEqual(caught.exception.code, "metadata_drift")

    def test_history_drift_blocks_before_callbacks(self):
        job = self.job()
        for first_known, completed in ((0, 0), (1, 1)):
            with self.subTest(first_known=first_known, completed=completed):
                self.conn.execute("UPDATE completion_history SET first_known=?,completed=? WHERE arxiv_id='p'", (first_known, completed))
                self.conn.commit()
                with self.assertRaises(PublicationBlocked) as caught:
                    publish_snapshot(self.conn, job, self.no_callback, self.no_callback, self.no_callback,
                                     self.no_callback)
                self.assertEqual(caught.exception.code, "eligibility_changed")

    def test_active_sql_transaction_rejects_callbacks(self):
        job = self.job()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(PublicationBlocked) as caught:
                publish_snapshot(self.conn, job, self.no_callback, self.no_callback, self.no_callback,
                                 self.no_callback)
            self.assertEqual(caught.exception.code, "transaction_active")
        finally:
            self.conn.rollback()

    def test_malicious_marker_and_artifact_text_tamper_are_rejected(self):
        job = self.job()
        path = Path(job["artifact_path"])
        original = json.loads(path.read_text(encoding="utf-8"))
        for field, value, code in (("publication_revision", "forged", "artifact_marker_mismatch"),
                                   ("text", "forged", "artifact_hash_mismatch")):
            with self.subTest(field=field):
                artifact = copy.deepcopy(original)
                artifact["points"][0]["payload"][field] = value
                path.write_text(json.dumps(artifact), encoding="utf-8")
                with self.assertRaises(PublicationBlocked) as caught:
                    publish_snapshot(self.conn, job, self.no_callback, self.no_callback, self.no_callback,
                                     self.no_callback)
                self.assertEqual(caught.exception.code, code)
        self.assert_uncompleted()

    def test_invalid_cache_json_rejected_without_journal(self):
        for content in ("{broken", '{"chunk_index":0}', '[{"chunk_index":0,"text":NaN}]'):
            with self.subTest(content=content):
                self.cache().write_text(content, encoding="utf-8")
                with self.assertRaises(PublicationBlocked) as caught:
                    self.job()
                self.assertEqual(caught.exception.code, "invalid_cache")
        self.assertIsNone(self.journal())

    def test_cache_point_text_mismatch_is_rejected(self):
        point = self.point()
        point["payload"]["text"] = "different"
        with self.assertRaises(PublicationBlocked) as caught:
            prepare_snapshot(self.conn, self.root, "p", [point], self.cache())
        self.assertEqual(caught.exception.code, "cache_point_mismatch")
        self.assertIsNone(self.journal())

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "mkfifo"), "FIFO fixture requires POSIX")
    def test_fifo_cache_is_rejected_without_blocking(self):
        self.cache().unlink()
        os.mkfifo(self.cache())
        with self.assertRaises(PublicationBlocked) as caught:
            self.job()
        self.assertEqual(caught.exception.code, "invalid_file")

    def test_oversized_cache_is_rejected_before_json_read(self):
        with self.cache().open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        with self.assertRaises(PublicationBlocked) as caught:
            self.job()
        self.assertEqual(caught.exception.code, "invalid_file")
        self.assertIsNone(self.journal())

    def test_persisted_backoff_skips_until_due(self):
        self.job()
        calls = []
        def reject(_job, _points):
            calls.append("qdrant")
            return False
        with patch("time.time", return_value=1000):
            self.assertEqual(reconcile_publications(self.conn, self.root, reject, self.no_callback, self.no_callback,
                                                    lambda *_: True), 0)
        self.assertEqual(self.journal()["retry_after"], 1030)
        self.assertEqual(self.journal()["error_code"], "qdrant_not_acknowledged")
        self.conn.close()
        self.conn = sqlite3.connect(self.database)
        self.conn.row_factory = sqlite3.Row
        with patch("time.time", return_value=1029):
            self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback, self.no_callback,
                                                    self.no_callback, self.no_callback), 0)
        with patch("time.time", return_value=1030):
            self.assertEqual(reconcile_publications(self.conn, self.root, reject, self.no_callback, self.no_callback,
                                                    lambda *_: True), 0)
        self.assertEqual(calls, ["qdrant", "qdrant"])
        self.assertEqual(self.journal()["retry_after"], 1060)

    def test_preflight_collision_blocks_before_qdrant_write(self):
        self.job()
        def collision(*_args):
            raise PublicationBlocked("legacy_uuid_collision", "legacy point owns candidate UUID")
        with patch("time.time", return_value=1000):
            self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback,
                                                    self.no_callback, self.no_callback, collision), 0)
        self.assertEqual(self.journal()["state"], "blocked")
        self.assertEqual(self.journal()["error_code"], "legacy_uuid_collision")
        self.assertEqual(json.loads(self.journal()["completed"]), [])
        self.assert_uncompleted()

    def test_preflight_timeout_is_typed_and_uses_persisted_backoff(self):
        self.job()
        def timeout(*_args):
            raise TimeoutError("simulated Qdrant timeout")
        with patch("time.time", return_value=1000):
            self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback,
                                                    self.no_callback, self.no_callback, timeout), 0)
        self.assertEqual(self.journal()["state"], "ready")
        self.assertEqual(self.journal()["completed"], "[]")
        self.assertEqual(self.journal()["error_code"], "preflight_failed")
        self.assertEqual(self.journal()["retry_after"], 1030)
        self.assert_uncompleted()

    def test_false_preflight_ack_is_typed_and_never_reaches_qdrant(self):
        self.job()
        with patch("time.time", return_value=1000):
            self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback,
                                                    self.no_callback, self.no_callback,
                                                    lambda *_: False), 0)
        self.assertEqual(self.journal()["state"], "ready")
        self.assertEqual(self.journal()["error_code"], "preflight_not_acknowledged")
        self.assertEqual(self.journal()["retry_after"], 1030)
        self.assert_uncompleted()

    def test_blocked_job_is_skipped_while_other_paper_progresses(self):
        self.job()
        self.add_paper("q")
        self.job("q")
        self.conn.execute("UPDATE papers SET title='drift' WHERE arxiv_id='p'")
        self.conn.commit()
        calls = []
        def acknowledge(job, _points):
            calls.append(job["paper_id"])
            return True
        self.assertEqual(reconcile_publications(self.conn, self.root, acknowledge, acknowledge, self.complete,
                                                lambda *_: True, limit=2), 1)
        self.assertEqual(self.journal()["state"], "blocked")
        self.assertEqual(self.journal()["error_code"], "metadata_drift")
        self.assertEqual(self.journal("q")["state"], "published")
        self.assertEqual(calls, ["q", "q"])
        self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback, self.no_callback,
                                                self.no_callback, self.no_callback, limit=2), 0)

    def test_final_sqlite_failure_is_retry_with_acks_retained_and_counter_rolled_back(self):
        job = self.job()
        def fail_complete(paper, commit=False):
            self.complete(paper, commit=commit)
            self.conn.execute("INSERT INTO missing_completion_fixture VALUES (1)")
        error = None
        try:
            publish_snapshot(self.conn, job, lambda *_: True, lambda *_: True, fail_complete, lambda *_: True)
        except Exception as exc:
            error = exc
        self.assert_uncompleted()
        self.assertEqual(self.journal()["state"], "ready")
        self.assertEqual(json.loads(self.journal()["completed"]), ["fts", "preflight", "qdrant"])
        self.assertIsInstance(error, PublicationRetry)
        self.assertEqual(error.code, "completion_failed")

    def test_direct_blocked_job_never_calls_external_writers(self):
        job = self.job()
        self.conn.execute("UPDATE paper_publications SET state='blocked',error_code='fixture_collision'")
        self.conn.commit()
        with self.assertRaises(PublicationBlocked) as caught:
            publish_snapshot(self.conn, job, self.no_callback, self.no_callback,
                             self.no_callback, self.no_callback)
        self.assertEqual(caught.exception.code, "job_not_ready")
        self.assert_uncompleted()

    def test_second_revision_cannot_reuse_pending_or_blocked_paper_uuids(self):
        job = self.job()
        changed = self.point()
        changed["vector"] = [0.8, 0.9]
        for state in ("ready", "blocked"):
            with self.subTest(state=state):
                self.conn.execute("UPDATE paper_publications SET state=?", (state,))
                self.conn.commit()
                with self.assertRaises(PublicationBlocked) as caught:
                    prepare_snapshot(self.conn, self.root, "p", [changed], self.cache())
                self.assertEqual(caught.exception.code, "snapshot_conflict")
                self.assertEqual(self.journal()["revision"], job["revision"])
                self.assertEqual(len(list((self.root / "publication_cache").glob("*.json"))), 1)

    def test_cancelled_completion_rolls_back_on_live_connection(self):
        job = self.job()
        def cancel(paper, commit=False):
            self.complete(paper, commit=commit)
            raise KeyboardInterrupt("fixture cancellation before visibility")
        with self.assertRaises(KeyboardInterrupt):
            publish_snapshot(self.conn, job, lambda *_: True, lambda *_: True,
                             cancel, lambda *_: True)
        self.assertFalse(self.conn.in_transaction)
        self.assert_uncompleted()
        self.assertEqual(self.journal()["state"], "ready")
        self.assertEqual(json.loads(self.journal()["completed"]), ["fts", "preflight", "qdrant"])

    def test_existing_conflicting_journal_revisions_block_before_callbacks(self):
        job = self.job()
        self.conn.execute("INSERT INTO paper_publications(arxiv_id,revision,kind,artifact_path,cache_sha256,state) "
                          "SELECT arxiv_id,'fixture-conflicting-revision',kind,artifact_path,cache_sha256,'ready' "
                          "FROM paper_publications")
        self.conn.commit()
        with self.assertRaises(PublicationBlocked) as caught:
            publish_snapshot(self.conn, job, self.no_callback, self.no_callback,
                             self.no_callback, self.no_callback)
        self.assertEqual(caught.exception.code, "snapshot_conflict")
        self.assert_uncompleted()


if __name__ == "__main__":
    unittest.main()
