"""Isolated regressions: no service imports, credentials, network or corpus access."""
import ast
import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import types
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import Mock, patch


def load_functions(*names, **globals_):
    source = Path(__file__).resolve().parents[1] / "service.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    selected = [node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(selected) == len(names)
    namespace = {"sqlite3": sqlite3, "json": json, "uuid": uuid, "time": time,
                 "log": Mock(),
                 **globals_}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return types.SimpleNamespace(**namespace)


class StopLoop(BaseException):
    pass


class NoLegacyScanConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("select") and any(
                f"from {table} where arxiv_id" in normalized for table in ("chunks", "papers_fts")):
            raise AssertionError("unindexed legacy FTS scan is forbidden")
        return super().execute(sql, *args, **kwargs)


class StageRegressionTests(unittest.TestCase):
    def test_insert_exception_rolls_back_and_releases_second_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE items (id INTEGER)")
            conn.commit()
            second = sqlite3.connect(path, timeout=0.1)

            def fail(connection):
                connection.execute("INSERT INTO items VALUES (1)")
                raise RuntimeError("stage failed after INSERT")

            def stop(_):
                self.assertFalse(conn.in_transaction)
                second.execute("INSERT INTO items VALUES (2)")
                second.commit()
                raise StopLoop()

            service = load_functions("_stage_loop", get_conn=lambda: conn,
                                     requests=Mock(), stage_heartbeat=Mock(),
                                     time=types.SimpleNamespace(sleep=stop),
                                     STAGE_BUSY_SLEEP=0, STAGE_IDLE_SLEEP=0, STAGE_ERROR_SLEEP=0)
            with self.assertRaises(StopLoop):
                service._stage_loop("fixture", fail, False)
            self.assertEqual(second.execute("SELECT id FROM items").fetchall(), [(2,)])
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
            second.close()

    def test_failed_rollback_reopens_connection_and_closes_session(self):
        broken, fresh, session = Mock(), Mock(), Mock()
        broken.rollback.side_effect = sqlite3.OperationalError("closed connection")
        service = load_functions("_stage_loop", get_conn=Mock(side_effect=[broken, fresh]),
                                 requests=types.SimpleNamespace(Session=lambda: session),
                                 stage_heartbeat=Mock(), time=Mock(),
                                 STAGE_BUSY_SLEEP=0, STAGE_IDLE_SLEEP=0, STAGE_ERROR_SLEEP=0)
        service.time.sleep.side_effect = StopLoop()
        with self.assertRaises(StopLoop):
            service._stage_loop("fixture", Mock(side_effect=RuntimeError("failure")), True)
        broken.close.assert_called_once()
        fresh.close.assert_called_once()
        session.close.assert_called_once()

    def heartbeat_service(self):
        clock = Mock()
        clock.time.return_value = 0
        return load_functions("stage_heartbeat", "stalled_stages", time=clock,
                              _heartbeats={}, _heartbeats_lock=threading.Lock(),
                              WATCHDOG_STALL_SECONDS=100)

    def test_fresh_error_heartbeats_do_not_hide_error_stall(self):
        service = self.heartbeat_service()
        service.stage_heartbeat("stage", "error")
        service.time.time.return_value = 101
        service.stage_heartbeat("stage", "start")
        service.stage_heartbeat("stage", "error")
        self.assertIn("stage", service.stalled_stages(now=101))
        service.stage_heartbeat("stage", "start")
        self.assertIn("stage", service.stalled_stages(now=101))

    def test_successful_idle_clears_errors_without_faking_progress(self):
        service = self.heartbeat_service()
        service.stage_heartbeat("stage", "error")
        service.time.time.return_value = 101
        service.stage_heartbeat("stage", "end", 0)
        self.assertEqual(service.stalled_stages(now=101), {})
        self.assertNotIn("last_progress_at", service._heartbeats["stage"])
        self.assertNotIn("error_since_at", service._heartbeats["stage"])

    def test_progress_is_separate_from_success_and_stuck_call(self):
        service = self.heartbeat_service()
        service.stage_heartbeat("stage", "end", 2)
        service.time.time.return_value = 99
        service.stage_heartbeat("stage", "end", 0)
        self.assertEqual(service._heartbeats["stage"]["last_progress_at"], 0)
        service.stage_heartbeat("stage", "start")
        self.assertEqual(service.stalled_stages(now=200), {"stage": "stuck call"})


class StateRegressionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        niche = Mock()
        niche.niche_score.return_value = {"web3": 6}
        niche.admitted_layers.return_value = ["web3"]
        niche.primary_layer.return_value = ("web3", 6)
        niche.all_matched_terms.return_value = ["blockchain"]
        self.service = load_functions(
            "init_db", "upsert_discovered", "_queue_index_sync", "_complete_paper", "_fresh_fts_ids",
            "hal_seed_cursors", "pick_next_query", "get_cursor", "_sched_get", "_sched_set",
            niche_filter=niche, forced_web3=lambda _: False, now_iso=lambda: "2026-10-02T12:00:00Z",
            HAL_QUERIES=["blockchain"], HAL_CURSOR_PREFIX="hal-v2@", HAL_REFRESH_SECONDS=86400,
            ALL_QUERY_KEYS=[("web3", "kw:owned")], LAYER_QUERY_GROUPS=[("web3", [])])
        self.service.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def paper(self, paper_id):
        return self.conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (paper_id,)).fetchone()

    def test_schema_initialization_is_idempotent_without_backfill(self):
        self.conn.execute("INSERT INTO papers(arxiv_id,status) VALUES ('legacy','done')")
        self.conn.commit()
        self.service.init_db(self.conn)
        self.service.init_db(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM completion_history").fetchone()[0], 0)
        self.assertEqual(tuple(self.conn.execute("SELECT first_completed,reprocessed,unknown FROM completion_totals").fetchone()), (0, 0, 0))

    def test_graph_placeholder_gets_metadata_and_passes_unchanged_gate(self):
        self.conn.execute("INSERT INTO papers(arxiv_id,title,layers,status) VALUES ('p','','','graph_unresolved')")
        self.service.upsert_discovered(self.conn, "p", "Blockchain paper", 2026, "web3")
        row = self.paper("p")
        self.assertEqual((row["title"], row["year"], row["status"], row["layers"]),
                         ("Blockchain paper", 2026, "discovered", "web3"))

    def test_graph_placeholder_off_niche_is_not_admitted(self):
        self.conn.execute("INSERT INTO papers(arxiv_id,status) VALUES ('p','graph_unresolved')")
        self.service.niche_filter.admitted_layers.return_value = []
        self.service.upsert_discovered(self.conn, "p", "Other topic", 2026, "web3")
        self.assertEqual(self.paper("p")["status"], "off_niche")
        self.assertEqual(self.paper("p")["layers"], "")

    def test_done_rediscovery_marks_changed_indexes_without_reopening_paper(self):
        self.service.upsert_discovered(self.conn, "p", "Old blockchain title", 2025, "web3", abstract="old")
        self.conn.execute("UPDATE papers SET status='done' WHERE arxiv_id='p'")
        self.conn.commit()
        self.service.upsert_discovered(self.conn, "p", "New blockchain title", 2026, "web3", abstract="new")
        row = self.paper("p")
        self.assertEqual((row["title"], row["year"], row["abstract"], row["status"]),
                         ("New blockchain title", 2026, "new", "done"))
        revision = self.conn.execute("SELECT revision FROM paper_index_sync").fetchone()[0]
        self.service.upsert_discovered(self.conn, "p", "New blockchain title", 2026, "web3", abstract="new")
        self.assertEqual(self.conn.execute("SELECT revision FROM paper_index_sync").fetchone()[0], revision)

    def test_metadata_marker_and_state_roll_back_together(self):
        self.conn.execute("INSERT INTO papers(arxiv_id,title,layers,status) VALUES ('p','Old','web3','done')")
        self.conn.commit()
        self.service.upsert_discovered(self.conn, "p", "Blockchain", 2026, "web3", commit=False)
        self.conn.rollback()
        self.assertEqual(self.paper("p")["title"], "Old")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 0)

    def test_hal_is_not_dispatched_as_arxiv(self):
        self.service.get_cursor(self.conn, "kw:owned", "web3")
        self.conn.execute("UPDATE harvest_cursor SET done=1 WHERE query_key='kw:owned'")
        self.service.hal_seed_cursors(self.conn)
        self.assertIsNone(self.service.pick_next_query(self.conn))

    def test_hal_refresh_is_bounded_and_preserves_inflight_offsets(self):
        self.service.hal_seed_cursors(self.conn)
        now = time.time()
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10))
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 86410))
        self.conn.execute("UPDATE harvest_cursor SET done=1,next_start=200,last_run_at=?", (recent,))
        self.service.hal_seed_cursors(self.conn)
        self.assertEqual(tuple(self.conn.execute("SELECT done,next_start FROM harvest_cursor").fetchone()), (1, 200))
        self.conn.execute("UPDATE harvest_cursor SET last_run_at=?", (old,))
        self.service.hal_seed_cursors(self.conn)
        self.assertEqual(tuple(self.conn.execute("SELECT done,next_start FROM harvest_cursor").fetchone()), (0, 0))
        self.conn.execute("UPDATE harvest_cursor SET next_start=100,last_run_at=?", (old,))
        self.service.hal_seed_cursors(self.conn)
        self.assertEqual(tuple(self.conn.execute("SELECT done,next_start FROM harvest_cursor").fetchone()), (0, 100))

    def test_first_completion_reprocessing_and_unknown_are_separate_and_atomic(self):
        self.service.upsert_discovered(self.conn, "new", "Blockchain", 2026, "web3")
        self.conn.execute("UPDATE papers SET status='chunked' WHERE arxiv_id='new'")
        self.assertEqual(self.service._fresh_fts_ids(self.conn, "new"), ["new"])
        self.assertTrue(self.service._complete_paper(self.conn, "new"))
        self.assertFalse(self.service._complete_paper(self.conn, "new"))
        self.conn.execute("UPDATE papers SET status='chunked' WHERE arxiv_id='new'")
        self.assertEqual(self.service._fresh_fts_ids(self.conn, "new"), [])
        self.service._complete_paper(self.conn, "new")
        self.conn.execute("INSERT INTO papers(arxiv_id,status) VALUES ('legacy','chunked')")
        self.service._complete_paper(self.conn, "legacy")
        self.assertEqual(tuple(self.conn.execute("SELECT first_completed,reprocessed,unknown FROM completion_totals").fetchone()), (1, 1, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 2)
        self.assertFalse(self.conn.in_transaction)

    def test_observed_done_cannot_be_counted_as_a_new_completion(self):
        self.service.upsert_discovered(self.conn, "p", "Blockchain", 2026, "web3")
        self.conn.execute("UPDATE papers SET status='done' WHERE arxiv_id='p'")
        self.service.upsert_discovered(self.conn, "p", "Blockchain", 2026, "web3")
        self.conn.execute("UPDATE papers SET status='chunked' WHERE arxiv_id='p'")
        self.service._complete_paper(self.conn, "p")
        self.assertEqual(tuple(self.conn.execute("SELECT first_completed,reprocessed,unknown FROM completion_totals").fetchone()), (0, 1, 0))

    def test_completion_failure_can_roll_back_state_journal_and_marker_together(self):
        self.service.upsert_discovered(self.conn, "p", "Blockchain", 2026, "web3")
        self.conn.execute("UPDATE papers SET status='chunked' WHERE arxiv_id='p'")
        self.conn.execute("CREATE TRIGGER reject_counter BEFORE UPDATE ON completion_totals "
                          "BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.service._complete_paper(self.conn, "p")
        self.conn.rollback()
        self.assertEqual(self.paper("p")["status"], "chunked")
        self.assertEqual(self.conn.execute("SELECT completed FROM completion_history WHERE arxiv_id='p'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 0)

    def reconciliation_service(self, fts_path, coarse=None, paper_fts=None, chunk_fts=None, embed_filter=True):
        def outside_transaction(*args):
            self.assertFalse(self.conn.in_transaction)
            return True
        connect = sqlite3.connect
        guarded_sqlite = types.SimpleNamespace(
            connect=lambda *a, **kw: connect(*a, **kw, factory=NoLegacyScanConnection),
            Error=sqlite3.Error, OperationalError=sqlite3.OperationalError)
        chunk = load_functions("_sync_chunk_fts_owned", "_fts_rowids", "fts_write", FTS_DB_PATH=fts_path,
                               CHUNK_DIR=Path(fts_path).parent, safe_id=lambda value: value,
                               extractor=types.SimpleNamespace(should_embed=lambda _: embed_filter),
                               NAMESPACE_URL=uuid.NAMESPACE_URL, sqlite3=guarded_sqlite)
        return load_functions("_reconcile_index_sync", "_sync_qdrant_metadata", QDRANT_URL="http://fixture",
                              COLLECTION_NAME="chunks", STAGE_ERROR_SLEEP=1,
                              _sync_coarse_index=coarse or outside_transaction,
                              _sync_chunk_fts_owned=chunk_fts or chunk._sync_chunk_fts_owned,
                              _sync_paper_fts_owned=paper_fts or outside_transaction)

    def test_reconciliation_updates_chunk_metadata_and_http_runs_without_writer_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "fts.db")
            with contextlib.closing(sqlite3.connect(path)) as fts, fts:
                fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,arxiv_id UNINDEXED,layers UNINDEXED,year UNINDEXED)")
                catalog = load_functions("_fts_rowids")
                catalog._fts_rowids(fts, "chunks", "p", fresh=True)
                rowid = fts.execute("INSERT INTO chunks VALUES ('original','p','old',2025)").lastrowid
                fts.execute("INSERT INTO pipeline_fts_rows VALUES ('chunks','p',?)", (rowid,))
            self.conn.execute("INSERT INTO papers(arxiv_id,title,year,layers,status,matched_terms) "
                              "VALUES ('p','Updated',2026,'web3','done','[]')")
            self.service._queue_index_sync(self.conn, "p")
            self.conn.commit()
            session = Mock()

            def post(*args, **kwargs):
                self.assertFalse(self.conn.in_transaction)
                self.assertEqual(kwargs["json"]["payload"]["title"], "Updated")
                return Mock()

            session.post.side_effect = post
            worker = self.reconciliation_service(path)
            self.assertEqual(worker._reconcile_index_sync(self.conn, session), 1)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 0)
            with contextlib.closing(sqlite3.connect(path)) as fts, fts:
                self.assertEqual(fts.execute("SELECT text,layers,year FROM chunks").fetchall(), [("original", "['web3']", 2026)])

    def test_legacy_fts_defer_does_not_block_qdrant_or_coarse_and_successes_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "fts.db")
            with contextlib.closing(sqlite3.connect(path)) as fts, fts:
                fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,arxiv_id UNINDEXED,layers UNINDEXED,year UNINDEXED)")
            self.conn.execute("INSERT INTO papers(arxiv_id,status,matched_terms) VALUES ('p','done','[]')")
            self.service._queue_index_sync(self.conn, "p")
            self.conn.commit()
            session = Mock()
            coarse, paper_fts = Mock(return_value=True), Mock(return_value=True)
            worker = self.reconciliation_service(path, coarse=coarse, paper_fts=paper_fts)
            self.assertEqual(worker._reconcile_index_sync(self.conn, session), 0)
            session.post.assert_called_once()
            coarse.assert_called_once()
            paper_fts.assert_called_once()
            completed = json.loads(self.conn.execute("SELECT completed FROM paper_index_sync_progress").fetchone()[0])
            self.assertEqual(set(completed), {"qdrant", "coarse", "paper_fts"})
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 1)
            with contextlib.closing(sqlite3.connect(path)) as fts, fts:
                load_functions("_fts_rowids")._fts_rowids(fts, "chunks", "p", fresh=True)
            self.conn.execute("UPDATE paper_index_sync SET retry_after=0")
            self.conn.commit()
            self.assertEqual(worker._reconcile_index_sync(self.conn, session), 1)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 0)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync_progress").fetchone()[0], 0)
            coarse.assert_called_once()
            paper_fts.assert_called_once()
            session.post.assert_called_once()

    def test_concurrent_rediscovery_does_not_lose_newer_pending_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "fts.db")
            with contextlib.closing(sqlite3.connect(path)) as fts, fts:
                fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,arxiv_id UNINDEXED,layers UNINDEXED,year UNINDEXED)")
                load_functions("_fts_rowids")._fts_rowids(fts, "chunks", "p", fresh=True)
            self.conn.execute("INSERT INTO papers(arxiv_id,status,matched_terms) VALUES ('p','done','[]')")
            self.service._queue_index_sync(self.conn, "p")
            self.conn.commit()
            session = Mock()

            def post(*args, **kwargs):
                self.assertFalse(self.conn.in_transaction)
                self.service._queue_index_sync(self.conn, "p")
                self.conn.commit()
                return Mock()

            session.post.side_effect = post
            self.reconciliation_service(path)._reconcile_index_sync(self.conn, session)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 1)

    def test_paper_fts_replay_is_addressed_and_legacy_without_catalog_is_deferred(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "papers_fts.db")
            with contextlib.closing(sqlite3.connect(path)) as fts, fts:
                fts.execute("CREATE VIRTUAL TABLE papers_fts USING fts5(arxiv_id UNINDEXED,title,abstract,layers UNINDEXED,year UNINDEXED)")
            self.service.upsert_discovered(self.conn, "new", "Blockchain", 2026, "web3", abstract="first")
            self.conn.execute("UPDATE papers SET status='done' WHERE arxiv_id='new'")
            self.conn.execute("INSERT INTO papers(arxiv_id,title,status) VALUES ('old','Legacy','done')")
            self.conn.commit()
            worker = load_functions("_sync_paper_fts_owned", "_fts_rowids", PAPER_FTS_ENABLED=True)
            connect = sqlite3.connect
            with patch.dict("sys.modules", {"build_paper_fts": types.SimpleNamespace(PAPER_FTS_PATH=path)}), \
                    patch.object(sqlite3, "connect", side_effect=lambda *a, **kw: connect(*a, **kw, factory=NoLegacyScanConnection)):
                self.assertTrue(worker._sync_paper_fts_owned(self.conn, ["new"]))
                self.conn.execute("UPDATE papers SET title='Updated' WHERE arxiv_id='new'")
                self.conn.commit()
                self.assertTrue(worker._sync_paper_fts_owned(self.conn, ["new"]))
                self.assertFalse(worker._sync_paper_fts_owned(self.conn, ["old"]))
            with contextlib.closing(sqlite3.connect(path)) as fts:
                self.assertEqual(fts.execute("SELECT arxiv_id,title FROM papers_fts").fetchall(), [("new", "Updated")])

    def test_coarse_sync_failure_is_not_acknowledged(self):
        worker = load_functions("_sync_coarse_index", COARSE_INDEX_ENABLED=True)
        with patch.dict("sys.modules", {"coarse_index": types.SimpleNamespace(upsert_papers=Mock(return_value=0))}):
            self.assertFalse(worker._sync_coarse_index(self.conn, ["p"]))
        with patch.dict("sys.modules", {"coarse_index": types.SimpleNamespace(upsert_papers=Mock(side_effect=RuntimeError("offline")))}):
            self.assertFalse(worker._sync_coarse_index(self.conn, ["p"]))

    def test_component_failures_are_independent_and_only_pending_components_retry(self):
        self.conn.execute("INSERT INTO papers(arxiv_id,status,matched_terms) VALUES ('p','done','[]')")
        self.service._queue_index_sync(self.conn, "p")
        self.conn.commit()
        coarse = Mock(side_effect=[False, True])
        chunk = Mock(side_effect=[False, False, True])
        paper = Mock(return_value=True)
        worker = self.reconciliation_service("unused-fixture", coarse=coarse, chunk_fts=chunk, paper_fts=paper)
        session = Mock()
        for expected in (0, 0, 1):
            self.conn.execute("UPDATE paper_index_sync SET retry_after=0")
            self.conn.commit()
            self.assertEqual(worker._reconcile_index_sync(self.conn, session), expected)
        self.assertEqual(coarse.call_count, 2)
        self.assertEqual(chunk.call_count, 3)
        paper.assert_called_once()
        session.post.assert_called_once()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 0)

    def legacy_completion_case(self, embeddable):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "fts.db")
            with contextlib.closing(sqlite3.connect(path)) as fts, fts:
                fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,"
                            "section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED)")
                fts.executemany("INSERT INTO chunks(text,point_id,arxiv_id) VALUES (?,?,?)",
                                [("old", "old-id", "legacy"), ("tail", "tail-id", "legacy")])
            chunks = [{"chunk_index": 0, "text": "current", "section_type": "method", "section_title": "Method"}]
            (Path(directory) / "legacy.json").write_text(json.dumps(chunks), encoding="utf-8")
            self.conn.execute("INSERT INTO papers(arxiv_id,status) VALUES ('legacy','chunked')")
            self.conn.commit()
            coarse = Mock(return_value=True)
            reconciler = self.reconciliation_service(path, coarse=coarse, embed_filter=embeddable)
            connect = sqlite3.connect
            guarded_sqlite = types.SimpleNamespace(
                connect=lambda *a, **kw: connect(*a, **kw, factory=NoLegacyScanConnection),
                Error=sqlite3.Error, OperationalError=sqlite3.OperationalError)
            fts_writer = load_functions("fts_write", "_fts_rowids", FTS_DB_PATH=path, sqlite3=guarded_sqlite)
            batch_embed = Mock(side_effect=lambda session, batch, slots: (batch, [[0.1] for _ in batch]))
            worker = load_functions("embed_step", "_upsert_chunk_snapshot", "_fresh_fts_ids", "_complete_paper", "_queue_index_sync",
                                    EMBED_CYCLE_LIMIT=1, _reconcile_index_sync=reconciler._reconcile_index_sync,
                                    CHUNK_DIR=Path(directory), safe_id=lambda value: value,
                                    extractor=types.SimpleNamespace(should_embed=lambda _: embeddable),
                                    fts_write=fts_writer.fts_write, UPSERT_BATCH_SIZE=1,
                                    QDRANT_URL="http://fixture", COLLECTION_NAME="chunks",
                                    requests=types.SimpleNamespace(RequestException=RuntimeError),
                                    now_iso=lambda: "2026-10-03T00:00:00Z", EMBED_BATCH_TEXTS=8,
                                    EMBED_BATCH_URLS=["fixture"], EMBED_WORKERS=1,
                                    ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed,
                                    _run_embed_batch=batch_embed, NAMESPACE_URL=uuid.NAMESPACE_URL,
                                    read_latex_cache=lambda _: None)
            session = Mock()

            def http(*args, **kwargs):
                self.assertFalse(self.conn.in_transaction)
                return Mock()

            session.put.side_effect = http
            session.post.side_effect = http
            self.assertEqual(worker.embed_step(self.conn, session), 1)
            self.assertEqual(self.paper("legacy")["status"], "done")
            self.assertEqual(worker.embed_step(self.conn, session), 0)
            self.assertEqual(session.put.call_count, 1)
            self.assertEqual(batch_embed.call_count, 1 if embeddable else 0)
            coarse.assert_called_once()
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 1)
            self.assertEqual(tuple(self.conn.execute("SELECT first_completed,reprocessed,unknown FROM completion_totals").fetchone()), (0, 0, 1))
            with contextlib.closing(sqlite3.connect(path)) as fts, fts:
                self.assertEqual(fts.execute("SELECT text FROM chunks").fetchall(), [("old",), ("tail",)])
                fts_writer._fts_rowids(fts, "chunks", "legacy", fresh=True)
                fts.executemany("INSERT INTO pipeline_fts_rows VALUES ('chunks','legacy',?)", [(1,), (2,)])
            self.conn.execute("UPDATE paper_index_sync SET retry_after=0")
            self.conn.commit()
            self.assertEqual(worker.embed_step(self.conn, session), 1)
            self.assertEqual(session.put.call_count, 1)
            self.assertEqual(batch_embed.call_count, 1 if embeddable else 0)
            coarse.assert_called_once()
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_index_sync").fetchone()[0], 0)
            with contextlib.closing(sqlite3.connect(path)) as fts:
                self.assertEqual(fts.execute("SELECT text FROM chunks").fetchall(), [("current",)] if embeddable else [])

    def test_unmapped_legacy_completes_once_without_repeated_embedding_and_fts_retries(self):
        self.legacy_completion_case(embeddable=True)

    def test_empty_chunk_legacy_completes_once_even_when_fts_is_deferred(self):
        self.legacy_completion_case(embeddable=False)


class FtsRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "fts.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,"
                          "section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED)")
        connect = sqlite3.connect
        guarded_sqlite = types.SimpleNamespace(
            connect=lambda *a, **kw: connect(*a, **kw, factory=NoLegacyScanConnection),
            Error=sqlite3.Error, OperationalError=sqlite3.OperationalError)
        self.service = load_functions("fts_write", "_fts_rowids", FTS_DB_PATH=self.path, sqlite3=guarded_sqlite)

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def point(self, point_id, text="new", paper="p", year=2026):
        return {"id": point_id, "payload": {"arxiv_id": paper, "text": text, "year": year}}

    def test_replay_deduplicates_point_ids_and_removes_old_version_tail(self):
        self.conn.executemany("INSERT INTO chunks(text,point_id,arxiv_id) VALUES (?,?,?)",
                              [("stale", "1", "p"), ("duplicate", "1", "p"), ("tail", "2", "p"), ("other", "3", "other")])
        self.conn.commit()
        self.service._fts_rowids(self.conn, "chunks", "p", fresh=True)
        self.conn.executemany("INSERT INTO pipeline_fts_rows VALUES ('chunks','p',?)", [(1,), (2,), (3,)])
        self.conn.commit()
        points = [self.point("1", "earlier"), self.point("1", "replacement")]
        self.assertTrue(self.service.fts_write(points))
        self.assertTrue(self.service.fts_write(points))
        self.assertEqual(self.conn.execute("SELECT point_id,text FROM chunks WHERE arxiv_id='p'").fetchall(), [("1", "replacement")])
        self.assertEqual(self.conn.execute("SELECT text FROM chunks WHERE arxiv_id='other'").fetchall(), [("other",)])

    def test_empty_snapshot_clears_only_one_paper(self):
        self.assertTrue(self.service.fts_write([self.point("1"), self.point("2", paper="other")], fresh_ids=["p", "other"]))
        self.assertTrue(self.service.fts_write([], ["p"]))
        self.assertEqual(self.conn.execute("SELECT arxiv_id FROM chunks").fetchall(), [("other",)])

    def test_failed_insert_restores_old_snapshot_atomically(self):
        self.assertTrue(self.service.fts_write([self.point("1", "original")], fresh_ids=["p"]))
        self.assertFalse(self.service.fts_write([self.point("1", year=object())]))
        self.assertEqual(self.conn.execute("SELECT text FROM chunks").fetchall(), [("original",)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pipeline_fts_rows").fetchone()[0], 1)

    def test_unmapped_legacy_is_deferred_without_scan_but_fresh_papers_and_replays_progress(self):
        self.conn.executemany("INSERT INTO chunks(text,point_id,arxiv_id) VALUES ('x',?,?)",
                              [(str(i), f"old-{i}") for i in range(8000)])
        self.conn.commit()
        self.assertFalse(self.service.fts_write([self.point("new-id")]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 8000)
        self.assertTrue(self.service.fts_write([self.point("new-id")], fresh_ids=["p"]))
        self.assertTrue(self.service.fts_write([self.point("new-id", "replay")]))
        self.assertEqual(self.conn.execute("SELECT text FROM chunks WHERE arxiv_id='p'").fetchall(), [("replay",)])

    def test_sidecatalog_lookup_uses_an_index(self):
        self.assertTrue(self.service.fts_write([self.point("id")], fresh_ids=["p"]))
        plan = self.conn.execute("EXPLAIN QUERY PLAN SELECT row_id FROM pipeline_fts_rows "
                                 "WHERE table_name=? AND arxiv_id=?", ("chunks", "p")).fetchall()
        self.assertTrue(any("SEARCH" in row[3] and "pipeline_fts_rows_paper" in row[3] for row in plan))


class VectorAndLoggingRegressionTests(unittest.TestCase):
    def vector_service(self):
        return load_functions("_upsert_chunk_snapshot", QDRANT_URL="http://fixture", COLLECTION_NAME="chunks",
                              UPSERT_BATCH_SIZE=1, requests=types.SimpleNamespace(RequestException=RuntimeError))

    def test_tail_prune_follows_all_acknowledged_upserts_and_is_paper_scoped(self):
        service, session, events = self.vector_service(), Mock(), []
        session.put.side_effect = lambda *a, **kw: events.append("upsert") or Mock()
        session.post.side_effect = lambda *a, **kw: events.append("prune") or Mock()
        points = [{"payload": {"chunk_index": index}} for index in (0, 1)]
        self.assertTrue(service._upsert_chunk_snapshot(session, "p", points))
        self.assertEqual(events, ["upsert", "upsert", "prune"])
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["filter"]["must"], [
            {"key": "arxiv_id", "match": {"value": "p"}}, {"key": "chunk_index", "range": {"gt": 1}}])
        self.assertEqual(session.put.call_args.kwargs["params"], {"wait": "true"})

    def test_failed_upsert_does_not_prune_and_failed_prune_does_not_complete(self):
        service, session = self.vector_service(), Mock()
        points = [{"payload": {"chunk_index": 0}}]
        session.put.return_value.raise_for_status.side_effect = RuntimeError("upsert failed")
        self.assertFalse(service._upsert_chunk_snapshot(session, "p", points))
        session.post.assert_not_called()
        session.put.return_value.raise_for_status.side_effect = None
        session.post.return_value.raise_for_status.side_effect = RuntimeError("prune failed")
        self.assertFalse(service._upsert_chunk_snapshot(session, "p", points))
        self.assertFalse(service._upsert_chunk_snapshot(session, "p", []))

    def test_empty_snapshot_prunes_only_after_acknowledged_empty_upsert(self):
        service, session, events = self.vector_service(), Mock(), []
        session.put.side_effect = lambda *a, **kw: events.append("upsert") or Mock()
        session.post.side_effect = lambda *a, **kw: events.append("prune") or Mock()
        self.assertTrue(service._upsert_chunk_snapshot(session, "p", []))
        self.assertEqual(events, ["upsert", "prune"])
        self.assertEqual(session.put.call_args.kwargs["json"], {"points": []})
        self.assertEqual(session.post.call_args.kwargs["json"], {"filter": {"must": [
            {"key": "arxiv_id", "match": {"value": "p"}}]}})

    def test_stdout_to_same_log_file_uses_one_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.log"
            with path.open("a", encoding="utf-8") as stream:
                service = load_functions("_pipeline_log_handlers", LOG_FILE=path, logging=logging, os=os,
                                         sys=types.SimpleNamespace(stdout=stream))
                handlers = service._pipeline_log_handlers()
                self.assertEqual(len(handlers), 1)
                for handler in handlers:
                    handler.close()

    def test_separate_stdout_keeps_console_and_file_handlers(self):
        with tempfile.TemporaryDirectory() as directory:
            service = load_functions("_pipeline_log_handlers", LOG_FILE=Path(directory) / "pipeline.log",
                                     logging=logging, os=os, sys=types.SimpleNamespace(stdout=Mock(fileno=Mock(side_effect=OSError()))))
            handlers = service._pipeline_log_handlers()
            self.assertEqual(len(handlers), 2)
            for handler in handlers:
                handler.close()


class PaperFtsCompatibilityTests(unittest.TestCase):
    def test_single_argument_hook_calls_existing_adapter(self):
        adapter = Mock()
        service = load_functions("_sync_paper_fts", PAPER_FTS_ENABLED=True)
        with patch.dict("sys.modules", {"build_paper_fts": types.SimpleNamespace(upsert_papers=adapter)}):
            service._sync_paper_fts(["2401.00001"])
        adapter.assert_called_once_with(["2401.00001"])

    def test_single_argument_hook_logs_adapter_failure(self):
        service = load_functions("_sync_paper_fts", PAPER_FTS_ENABLED=True)
        with patch.dict("sys.modules", {"build_paper_fts": types.SimpleNamespace(upsert_papers=Mock(side_effect=RuntimeError("fixture")))}):
            service._sync_paper_fts(["2401.00001"])
        service.log.warning.assert_called_once()

    def test_disabled_and_empty_hooks_do_not_import_adapter(self):
        disabled = load_functions("_sync_paper_fts", PAPER_FTS_ENABLED=False)
        enabled = load_functions("_sync_paper_fts", PAPER_FTS_ENABLED=True)
        with patch("builtins.__import__") as importer:
            disabled._sync_paper_fts(["2401.00001"])
            enabled._sync_paper_fts([])
        importer.assert_not_called()

    def test_live_reconciler_never_calls_legacy_compatibility_hook(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8"))
        worker = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_reconcile_index_sync")
        calls = [node.func.id for node in ast.walk(worker)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        self.assertIn("_sync_paper_fts_owned", calls)
        self.assertNotIn("_sync_paper_fts", calls)


if __name__ == "__main__":
    unittest.main()
