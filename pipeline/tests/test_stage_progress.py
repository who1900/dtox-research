"""Bounded offline watchdog/commit checks; no service import or network/native."""
import sqlite3
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from pipeline.tests import test_parse_safety as parse_fixture
from pipeline.tests import test_pdf_conversion_lifecycle as fulltext_fixture
from pipeline.tests.test_pipeline_audit_fixes import load_functions


class StageProgressTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock()
        self.clock.time.return_value = 0
        self.service = load_functions("stage_heartbeat", "stalled_stages", time=self.clock,
                                      _heartbeats={}, _heartbeats_lock=threading.Lock(),
                                      WATCHDOG_STALL_SECONDS=2700)
        self.events = []
        for target in ("socket.create_connection", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def begin(self, name):
        self.service.stage_heartbeat(name, "error")
        self.service.stage_heartbeat(name, "start")
        self.clock.time.return_value = 3000

    def record(self, name):
        return self.service._heartbeats[name]

    def observe(self, function, conn, acknowledged=lambda: None):
        def heartbeat(name, event, processed):
            self.assertEqual(event, "progress")
            self.assertFalse(conn.in_transaction)
            acknowledged()
            self.events.append((name, event, processed))
            self.service.stage_heartbeat(name, event, processed)
        function.__globals__["stage_heartbeat"] = heartbeat

    def fulltext(self):
        fixture = fulltext_fixture.FulltextWindowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        worker = fixture.service(lambda paper, *_: (paper, "durable fulltext", "fixture"))
        return fixture, worker

    def parser(self):
        fixture = parse_fixture.ParseSafetyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def embedding(self):
        fixture = self.parser()
        self.assertEqual(fixture.parse(), 1)
        def response(url, **_):
            value = {"points": []} if url.endswith("/scroll") else []
            return Mock(json=Mock(return_value={"result": value}))
        fixture.session.post.side_effect = response
        return fixture

    def assert_failed_without_progress(self, name):
        self.assertEqual(self.events, [])
        self.assertEqual(self.record(name)["error_since_at"], 0)
        self.assertIn(name, self.service.stalled_stages(now=3000))

    def test_long_busy_current_cycle_progress_prevents_false_stall(self):
        self.begin("fulltext")
        self.service.stage_heartbeat("fulltext", "progress", 1)
        self.assertEqual(self.record("fulltext")["phase"], "busy")
        self.assertEqual(self.record("fulltext")["started_at"], 0)
        self.assertNotIn("error_since_at", self.record("fulltext"))
        self.assertEqual(self.service.stalled_stages(now=4000), {})
        self.assertEqual(self.service.stalled_stages(now=5701), {"fulltext": "stuck call"})

    def test_previous_cycle_does_not_mask_new_hang_even_same_start_timestamp(self):
        self.service.stage_heartbeat("embed", "start")
        self.clock.time.return_value = 3000
        self.service.stage_heartbeat("embed", "progress", 1)
        self.service.stage_heartbeat("embed", "end", 1)
        self.service.stage_heartbeat("embed", "start")
        self.assertNotIn("cycle_progress_at", self.record("embed"))
        self.assertEqual(self.record("embed")["last_progress_at"], 3000)
        self.assertEqual(self.service.stalled_stages(now=5701), {"embed": "stuck call"})

    def test_start_errors_and_empty_progress_do_not_clear_error_history(self):
        self.begin("chunk")
        for event, count in (("progress", 0), ("progress", -1), ("start", None),
                             ("error", None), ("start", None)):
            self.service.stage_heartbeat("chunk", event, count)
            self.assertEqual(self.record("chunk")["error_since_at"], 0)
        self.assertIn("chunk", self.service.stalled_stages(now=3000))
        self.service.stage_heartbeat("chunk", "progress", 1)
        self.assertNotIn("error_since_at", self.record("chunk"))

    def test_progress_outside_busy_cycle_is_ignored(self):
        self.service.stage_heartbeat("embed", "error")
        before = dict(self.record("embed"))
        self.clock.time.return_value = 3000
        self.service.stage_heartbeat("embed", "progress", 1)
        self.assertEqual(self.record("embed"), before)

    def test_progress_without_registered_cycle_does_not_create_empty_record(self):
        self.service.stage_heartbeat("unknown", "progress", 1)
        self.assertEqual(self.service._heartbeats, {})
        self.assertEqual(self.service.stalled_stages(now=3000), {})

    def test_fulltext_reports_only_after_real_status_and_file_commit(self):
        fixture, worker = self.fulltext()
        self.begin("fulltext")
        def acknowledged():
            row = fixture.conn.execute("SELECT status FROM papers WHERE arxiv_id=?", (fixture.ids[0],)).fetchone()
            self.assertEqual(row[0], "fulltext_fetched")
            self.assertEqual((fixture.root / (fixture.ids[0] + ".tex")).read_text(), "durable fulltext")
        self.observe(worker.fulltext_step, fixture.conn, acknowledged)
        self.assertEqual(worker.fulltext_step(fixture.conn, limit=1), 1)
        self.assertEqual(self.events, [("fulltext", "progress", 1)])
        self.assertEqual(self.service.stalled_stages(now=3001), {})

    def test_fulltext_failure_and_rollback_never_emit_progress(self):
        fixture, worker = self.fulltext()
        self.begin("fulltext")
        self.observe(worker.fulltext_step, fixture.conn)
        proxy = Mock(wraps=fixture.conn)
        proxy.commit.side_effect = sqlite3.OperationalError("commit failed")
        with self.assertRaises(sqlite3.OperationalError):
            worker.fulltext_step(proxy, limit=1)
        fixture.conn.rollback()
        self.assertEqual(fixture.conn.execute("SELECT status FROM papers LIMIT 1").fetchone()[0], "quality_checked")
        self.assert_failed_without_progress("fulltext")

    def test_fulltext_stale_transition_and_retry_do_not_emit_progress(self):
        fixture, worker = self.fulltext()
        self.begin("fulltext")
        self.observe(worker.fulltext_step, fixture.conn)
        proxy = Mock(wraps=fixture.conn)
        def execute(sql, *args):
            if sql.startswith("UPDATE papers SET status='fulltext_fetched'"):
                return Mock(rowcount=0)
            return fixture.conn.execute(sql, *args)
        proxy.execute.side_effect = execute
        self.assertEqual(worker.fulltext_step(proxy, limit=1), 0)
        worker.fulltext_step.__globals__["_fetch_one_fulltext"] = lambda paper, *_: (paper, None, None)
        self.assertEqual(worker.fulltext_step(fixture.conn, limit=1), 0)
        self.assert_failed_without_progress("fulltext")

    def test_chunk_committed_cache_and_status_emit_progress(self):
        fixture = self.parser()
        self.begin("chunk")
        def acknowledged():
            self.assertEqual(fixture.paper()["status"], "chunked")
            self.assertNotEqual(fixture.cache_path.read_bytes(), fixture.old_cache)
        self.observe(fixture.worker.chunk_step, fixture.conn, acknowledged)
        self.assertEqual(fixture.parse(), 1)
        self.assertEqual(self.events, [("chunk", "progress", 1)])
        self.assertEqual(self.service.stalled_stages(now=3001), {})

    def test_chunk_parser_retry_commit_is_not_durable_completion(self):
        fixture = self.parser()
        self.begin("chunk")
        self.observe(fixture.worker.chunk_step, fixture.conn)
        fixture.extractor.build_chunks.side_effect = RuntimeError("parser failed")
        self.assertEqual(fixture.parse(), 0)
        self.assertEqual(fixture.paper()["parse_attempts"], 1)
        self.assert_failed_without_progress("chunk")

    def test_chunk_commit_failure_and_rollback_do_not_emit_progress(self):
        fixture = self.parser()
        self.begin("chunk")
        self.observe(fixture.worker.chunk_step, fixture.conn)
        proxy = Mock(wraps=fixture.conn)
        proxy.commit.side_effect = sqlite3.OperationalError("commit failed")
        with patch.dict("sys.modules", {"pipeline.document_cache": fixture.manifests}):
            with self.assertRaises(sqlite3.OperationalError):
                fixture.worker.chunk_step(proxy, fixture.session)
        fixture.conn.rollback()
        self.assertEqual(fixture.paper()["status"], "fulltext_fetched")
        self.assert_failed_without_progress("chunk")

    def test_chunk_cache_failure_and_stale_update_do_not_emit_progress(self):
        fixture = self.parser()
        self.begin("chunk")
        self.observe(fixture.worker.chunk_step, fixture.conn)
        fixture.manifests.publish_section_manifest.side_effect = OSError("cache failed")
        self.assertEqual(fixture.parse(), 0)
        self.assert_failed_without_progress("chunk")
        fixture.conn.execute("UPDATE papers SET parse_retry_after=0")
        fixture.conn.commit()
        fixture.manifests.publish_section_manifest.side_effect = None
        proxy = Mock(wraps=fixture.conn)
        def execute(sql, *args):
            if sql.startswith("UPDATE papers SET status='chunked'"):
                return Mock(rowcount=0)
            return fixture.conn.execute(sql, *args)
        proxy.execute.side_effect = execute
        with patch.dict("sys.modules", {"pipeline.document_cache": fixture.manifests}):
            self.assertEqual(fixture.worker.chunk_step(proxy, fixture.session), 0)
        self.assert_failed_without_progress("chunk")

    def test_embed_acknowledged_complete_publication_emits_progress(self):
        fixture = self.embedding()
        self.begin("embed")
        def acknowledged():
            self.assertEqual(fixture.paper()["status"], "done")
            self.assertEqual(fixture.conn.execute("SELECT state FROM paper_publications").fetchone()[0], "published")
            self.assertEqual(fixture.conn.execute("SELECT completed FROM completion_history").fetchone()[0], 1)
        self.observe(fixture.worker.embed_step, fixture.conn, acknowledged)
        self.assertEqual(fixture.worker.embed_step(fixture.conn, fixture.session), 1)
        self.assertEqual(self.events, [("embed", "progress", 1)])
        self.assertEqual(self.service.stalled_stages(now=3001), {})

    def test_embed_qdrant_ack_without_fts_publication_is_not_progress(self):
        fixture = self.embedding()
        self.begin("embed")
        self.observe(fixture.worker.embed_step, fixture.conn)
        fixture.worker.fts_write.return_value = False
        self.assertEqual(fixture.worker.embed_step(fixture.conn, fixture.session), 0)
        fixture.upsert.assert_called_once()
        self.assertEqual(fixture.paper()["status"], "chunked")
        self.assert_failed_without_progress("embed")

    def test_embed_completion_failure_rolls_back_without_progress(self):
        fixture = self.embedding()
        self.begin("embed")
        self.observe(fixture.worker.embed_step, fixture.conn)
        fixture.worker.embed_step.__globals__["_complete_paper"] = Mock(return_value=False)
        self.assertEqual(fixture.worker.embed_step(fixture.conn, fixture.session), 0)
        fixture.upsert.assert_called_once()
        fixture.worker.fts_write.assert_called_once()
        self.assertEqual(fixture.paper()["status"], "chunked")
        self.assertFalse(fixture.conn.in_transaction)
        self.assert_failed_without_progress("embed")

    def test_embed_empty_vector_publication_is_real_completion(self):
        fixture = self.embedding()
        fixture.extractor.should_embed = lambda _: False
        self.begin("embed")
        self.observe(fixture.worker.embed_step, fixture.conn)
        self.assertEqual(fixture.worker.embed_step(fixture.conn, fixture.session), 1)
        self.assertEqual(fixture.paper()["status"], "done")
        self.assertEqual(self.events, [("embed", "progress", 1)])

    def test_recovered_publications_emit_per_committed_unit_while_still_busy(self):
        from pipeline.publication import prepare_snapshot
        fixture = self.embedding()
        for paper in (fixture.paper_id, "second"):
            if paper == "second":
                fixture.conn.execute("INSERT INTO papers(arxiv_id,status,parse_status) VALUES (?,'chunked','ok')", (paper,))
                fixture.conn.execute("INSERT INTO completion_history VALUES (?,1,0)", (paper,))
                fixture.conn.commit()
                (fixture.root / "chunk_cache/second.json").write_bytes(fixture.cache_path.read_bytes())
            point = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, paper + "#0")), "vector": [0.1],
                     "payload": {"arxiv_id": paper, "chunk_index": 0, "text": fixture.chunks[0]["text"]}}
            prepare_snapshot(fixture.conn, fixture.root, paper, [point], fixture.root / "chunk_cache" / (paper + ".json"))
        self.begin("embed")
        self.observe(fixture.worker.embed_step, fixture.conn)
        def acknowledge(*_):
            if fixture.upsert.call_count == 2:
                self.assertEqual(len(self.events), 1)
                self.assertEqual(self.record("embed")["phase"], "busy")
                self.clock.time.return_value = 4000
                self.assertEqual(self.service.stalled_stages(now=4000), {})
            return True
        fixture.upsert.side_effect = acknowledge
        self.assertEqual(fixture.worker.embed_step(fixture.conn, fixture.session, limit=2), 2)
        self.assertEqual(self.events, [("embed", "progress", 1)] * 2)

    def test_idle_embed_step_selects_reconciliation_batch_once(self):
        fixture = self.embedding()
        fixture.conn.execute("UPDATE papers SET status='done'")
        fixture.conn.commit()
        statements = []
        fixture.conn.set_trace_callback(statements.append)
        self.assertEqual(fixture.worker.embed_step(fixture.conn, fixture.session, limit=20), 0)
        selections = [sql for sql in statements if sql.startswith("SELECT * FROM paper_publications WHERE kind='snapshot'")]
        self.assertEqual(len(selections), 1)
        self.assertIn("LIMIT 20", selections[0])

    def test_index_sync_partial_retry_is_not_progress_but_complete_commit_is(self):
        fixture = self.embedding()
        fixture.conn.execute("UPDATE papers SET status='done'")
        fixture.worker._queue_index_sync(fixture.conn, fixture.paper_id)
        fixture.conn.commit()
        gate = Mock(return_value=False)
        worker = load_functions("_reconcile_index_sync", time=self.clock, STAGE_ERROR_SLEEP=1,
                                _sync_qdrant_metadata=lambda *_: True, _sync_coarse_index=lambda *_: True,
                                _sync_chunk_fts_owned=lambda *_: True, _sync_paper_fts_owned=gate)
        self.begin("embed")
        self.observe(worker._reconcile_index_sync, fixture.conn)
        self.assertEqual(worker._reconcile_index_sync(fixture.conn, fixture.session), 0)
        self.assert_failed_without_progress("embed")
        self.clock.time.return_value = 3001
        gate.return_value = True
        self.assertEqual(worker._reconcile_index_sync(fixture.conn, fixture.session), 1)
        self.assertEqual(self.events, [("embed", "progress", 1)])
        self.assertEqual(fixture.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 0)
        self.assertNotIn("error_since_at", self.record("embed"))
