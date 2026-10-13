"""Offline fail-closed recovery planning preserves published availability."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ops import reprocess_lost_body as reprocess
from pipeline.tests.test_pipeline_audit_fixes import load_functions


class CachedReprocessSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "state.db"
        self.conn = reprocess.connect(self.db)
        self.addCleanup(self.conn.close)
        schema = load_functions("init_db")
        schema.init_db(self.conn)
        self.paper_id = "2401.00001"
        self.conn.execute("INSERT INTO papers(arxiv_id,status,fulltext_source,extractor_version, "
                          "parser_mode,parser_revision) VALUES (?,'done','latex',2,'pylatexenc',2)",
                          (self.paper_id,))
        self.conn.commit()
        self.args = reprocess.parse_args([
            "--data-dir", str(self.root), "--db", str(self.db),
            "--fts", str(self.root / "fts.db"), "--list", str(self.root / "list.json"),
            "--progress", str(self.root / "progress.jsonl"), "--apply", "--target", "5",
            "--lock", str(self.root / "planner.lock"),
        ])
        self.paths = {
            self.root / "fulltext_cache" / f"{self.paper_id}.tex": b"\\section{Method}\nCached text",
            self.root / "latex_cache" / self.paper_id / "source.tex": b"Original source",
            self.root / "chunk_cache" / f"{self.paper_id}.json": b'[{"text":"good chunk"}]',
        }
        for path, data in self.paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with contextlib.closing(sqlite3.connect(self.args.fts)) as fts, fts:
            fts.execute("CREATE TABLE chunks(text,point_id,arxiv_id)")
            fts.execute("INSERT INTO chunks VALUES ('good chunk','stable-id',?)", (self.paper_id,))
        Path(self.args.list).write_text(json.dumps({"items": [
            {"id": self.paper_id, "citations": 2, "chunks": 1}]}), encoding="utf-8")
        self.session = Mock()
        self.messages = []

    def paper(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (self.paper_id,)).fetchone()

    def assert_no_losses(self):
        for path, data in self.paths.items():
            self.assertEqual(path.read_bytes(), data)
        with contextlib.closing(sqlite3.connect(self.args.fts)) as fts:
            self.assertEqual(fts.execute("SELECT * FROM chunks").fetchall(),
                             [("good chunk", "stable-id", self.paper_id)])
        self.assertEqual(self.session.mock_calls, [])

    def assert_available(self, before):
        self.assertEqual(dict(self.paper()), before)
        self.assertIsNotNone(self.conn.execute(
            "SELECT arxiv_id FROM papers WHERE arxiv_id=? AND status='done'",
            (self.paper_id,)).fetchone())
        self.assertEqual(reprocess.queue_size(self.conn), 0)
        self.assertFalse(Path(self.args.progress).exists())
        self.assert_no_losses()

    def cli_args(self, mode="--feed"):
        return [mode, "--db", str(self.db), "--data-dir", str(self.root),
                "--list", self.args.list, "--progress", self.args.progress,
                "--lock", self.args.lock]

    def test_reset_signature_refuses_without_deletes_or_status_change(self):
        before = dict(self.paper())
        with patch.object(reprocess, "qdrant_delete") as qdrant_delete, \
                patch.object(reprocess, "fts_delete") as fts_delete, \
                patch.object(reprocess, "remove_caches") as remove, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(reprocess.reset_one(self.conn, self.session, self.args, self.paper_id, [1]))
        qdrant_delete.assert_not_called()
        fts_delete.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {
            "status": "retired", "error": "revision-aware publication required", "id": self.paper_id})
        self.assert_available(before)
        self.assertFalse(self.conn.in_transaction)

    def test_duplicate_refusal_does_not_reset_metadata_or_backoff(self):
        self.conn.execute("UPDATE papers SET parse_status='failed',parse_retry_after=1234,parse_attempts=3")
        self.conn.commit()
        before = dict(self.paper())
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                self.assertFalse(reprocess.reset_one(self.conn, self.session, self.args, self.paper_id, []))
        self.assert_available(before)

    def test_missing_input_refusal_does_not_write_missing_source(self):
        path = self.root / "fulltext_cache" / f"{self.paper_id}.tex"
        path.unlink()
        del self.paths[path]
        before = dict(self.paper())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(reprocess.reset_one(self.conn, self.session, self.args, self.paper_id, []))
        self.assert_available(before)

    def test_direct_reset_never_touches_dependencies_even_without_apply(self):
        self.args.apply = False
        conn = Mock()
        with patch.object(reprocess, "_cached_reparse_input") as read_source, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(reprocess.reset_one(conn, self.session, self.args, self.paper_id, [1]))
        read_source.assert_not_called()
        self.assertEqual(conn.mock_calls, [])
        self.assertEqual(self.session.mock_calls, [])

    def test_main_apply_refuses_before_lock_database_session_or_run(self):
        before = dict(self.paper())
        files_before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for mode in ("--feed", "--build-list"):
            with self.subTest(mode=mode), \
                    patch.object(reprocess, "single_instance") as lock, \
                    patch.object(reprocess.sqlite3, "connect") as connect, \
                    patch.object(reprocess.requests, "Session") as session, \
                    patch.object(reprocess, "run") as run, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(reprocess.main(self.cli_args(mode) + ["--apply"]), 2)
            self.assertEqual(json.loads(output.getvalue()), {
                "status": "retired", "error": "revision-aware publication required"})
            for dependency in (lock, connect, session, run):
                dependency.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
                         files_before)
        self.assert_available(before)

    def test_apply_refuses_even_when_all_paths_are_missing(self):
        missing = self.root / "absent"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(reprocess.main(["--apply", "--db", str(missing / "state.db"),
                                             "--lock", str(missing / "lock")]), 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "revision-aware publication required")
        self.assertFalse(missing.exists())

    def test_direct_feed_apply_refuses_before_reading_plan_or_queue(self):
        before = dict(self.paper())
        with patch.object(reprocess, "load_progress") as progress, \
                patch.object(reprocess, "queue_size") as queue, \
                patch("builtins.open") as open_file:
            self.assertEqual(reprocess.feed(self.conn, self.session, self.args, log=self.messages.append), 2)
        for dependency in (progress, queue, open_file):
            dependency.assert_not_called()
        self.assertEqual(json.loads(self.messages[0])["status"], "retired")
        self.assert_available(before)

    def test_direct_run_apply_refuses_build_list_and_feed(self):
        before = dict(self.paper())
        for build in (False, True):
            self.args.build_list = build
            with patch.object(reprocess, "build_list") as build_list, \
                    patch.object(reprocess, "feed") as feed, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reprocess.run(self.conn, self.session, self.args), 2)
            build_list.assert_not_called()
            feed.assert_not_called()
        self.assert_available(before)

    def test_dry_run_validates_input_without_state_or_cache_changes(self):
        self.args.apply = False
        before = dict(self.paper())
        self.assertEqual(reprocess.feed(self.conn, self.session, self.args, log=self.messages.append), 1)
        self.assertTrue(any("ready candidates 1" in message for message in self.messages))
        self.assertFalse(any("schedule" in message for message in self.messages))
        self.assert_available(before)

    def test_dry_run_missing_source_has_zero_ready_without_persisting(self):
        self.args.apply = False
        path = self.root / "fulltext_cache" / f"{self.paper_id}.tex"
        path.unlink()
        del self.paths[path]
        before = dict(self.paper())
        self.assertEqual(reprocess.feed(self.conn, self.session, self.args, log=self.messages.append), 0)
        self.assert_available(before)
        self.assertTrue(any("refused" in message for message in self.messages))

    def test_dry_run_empty_and_invalid_input_are_not_ready(self):
        self.args.apply = False
        path = self.root / "fulltext_cache" / f"{self.paper_id}.tex"
        before = dict(self.paper())
        for data in (b" \n ", b"\xff\xfe"):
            with self.subTest(data=data):
                self.paths[path] = data
                path.write_bytes(data)
                self.assertEqual(reprocess.feed(self.conn, self.session, self.args, log=self.messages.append), 0)
                self.assert_available(before)

    def test_main_planning_opens_readonly_database_without_creating_lock(self):
        before = dict(self.paper())

        def check_readonly(conn, session, args):
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE papers SET status='fulltext_fetched'")
            return 0

        with patch.object(reprocess, "run", side_effect=check_readonly), \
                patch.object(reprocess.requests, "Session", return_value=self.session):
            self.session.__enter__ = Mock(return_value=self.session)
            self.session.__exit__ = Mock(return_value=False)
            self.assertEqual(reprocess.main(self.cli_args()), 0)
        self.session.reset_mock()
        self.assertFalse(Path(self.args.lock).exists())
        self.assert_available(before)

    def test_build_list_preserves_state_caches_and_only_writes_plan(self):
        self.args.apply = False
        self.args.build_list = True
        before = dict(self.paper())
        with patch.object(reprocess, "qdrant_count", return_value=1) as count, \
                contextlib.redirect_stdout(io.StringIO()):
            self.args.pause = 0
            self.assertEqual(reprocess.run(self.conn, self.session, self.args), 0)
        count.assert_called_once()
        self.assertEqual(json.loads(Path(self.args.list).read_text())["items"], [
            {"id": self.paper_id, "citations": 0, "chunks": 1}])
        self.assert_available(before)

    def test_historical_progress_is_read_but_never_extended(self):
        self.args.apply = False
        progress = Path(self.args.progress)
        data = json.dumps({"id": self.paper_id, "at": "historical"}) + "\n"
        progress.write_text(data, encoding="utf-8")
        before = dict(self.paper())
        self.assertEqual(reprocess.feed(self.conn, self.session, self.args, log=self.messages.append), 0)
        self.assertEqual(progress.read_text(encoding="utf-8"), data)
        self.assertEqual(dict(self.paper()), before)
        self.assert_no_losses()


if __name__ == "__main__":
    unittest.main()
