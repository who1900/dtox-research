import contextlib
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pipeline import build_fts


def point(identifier, text="fixture text"):
    return {"id": identifier, "payload": {"text": text, "arxiv_id": "paper:fixture"}}


def page(points, offset=None):
    return {"result": {"points": points, "next_page_offset": offset}}


class FakeQdrant:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, path, body):
        if path != "/points/scroll":
            raise AssertionError(f"unexpected Qdrant path: {path}")
        self.calls.append(copy.deepcopy(body))
        offset = body.get("offset")
        if offset not in self.pages:
            raise AssertionError(f"unexpected Qdrant offset: {offset}")
        response = self.pages[offset]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class CrashAfterCommit:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        result = self.connection.__exit__(exc_type, exc, traceback)
        if exc_type is None:
            raise RuntimeError("fixture crash after durable commit")
        return result


class BuildFtsSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "fts.db"
        path_patch = patch.object(build_fts, "FTS_DB", str(database))
        path_patch.start()
        self.addCleanup(path_patch.stop)
        network_patch = patch.object(build_fts.urllib.request, "urlopen", side_effect=AssertionError("network forbidden"))
        network_patch.start()
        self.addCleanup(network_patch.stop)

    def run_builder(self, qdrant, arguments=(), connection=None):
        connection = connection if connection is not None else build_fts.connect()
        try:
            with patch.object(build_fts, "connect", return_value=connection), \
                    patch.object(build_fts, "post", qdrant), \
                    patch.object(build_fts.sys, "argv", ["build_fts.py", *arguments]), \
                    contextlib.redirect_stdout(io.StringIO()):
                return build_fts.main()
        finally:
            connection.close()

    def state(self):
        connection = build_fts.connect()
        try:
            return {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM build_state")}
        finally:
            connection.close()

    def ids(self):
        connection = build_fts.connect()
        try:
            return [row[0] for row in connection.execute("SELECT point_id FROM chunks ORDER BY rowid")]
        finally:
            connection.close()

    def seed_state(self, values):
        connection = build_fts.connect()
        try:
            with connection:
                connection.executemany("INSERT INTO build_state(key, value) VALUES (?, ?)",
                                       [(key, json.dumps(value)) for key, value in values.items()])
        finally:
            connection.close()

    def assert_completed_no_work(self):
        before = self.ids()
        connection = build_fts.connect()
        statements = []
        connection.set_trace_callback(statements.append)
        qdrant = FakeQdrant({})
        self.assertEqual(self.run_builder(qdrant, connection=connection), 0)
        self.assertEqual(qdrant.calls, [])
        self.assertEqual([sql for sql in statements if sql.lstrip().upper().startswith("INSERT")], [])
        self.assertEqual(self.ids(), before)

    def test_first_run_missing_offset_starts_and_preserves_zero_cursor(self):
        qdrant = FakeQdrant({None: page([point(1)], 0), 0: page([point(2)])})
        self.assertEqual(self.run_builder(qdrant), 0)
        self.assertNotIn("offset", qdrant.calls[0])
        self.assertEqual(qdrant.calls[1]["offset"], 0)
        self.assertEqual(self.ids(), ["1", "2"])
        self.assertEqual(self.state(), {"offset": None, "completed": True})

    def test_completed_rerun_makes_zero_qdrant_calls_and_inserts(self):
        self.run_builder(FakeQdrant({None: page([point("final")])}))
        self.assert_completed_no_work()

    def test_explicit_completion_is_distinct_from_an_old_cursor(self):
        self.seed_state({"offset": "old-cursor", "completed": True})
        self.assert_completed_no_work()

    def test_legacy_json_null_offset_is_completed_without_any_inserts(self):
        self.seed_state({"offset": None})
        self.assert_completed_no_work()
        self.assertEqual(self.state(), {"offset": None})

    def test_empty_first_final_page_commits_completion(self):
        qdrant = FakeQdrant({None: page([])})
        self.run_builder(qdrant)
        self.assertEqual(len(qdrant.calls), 1)
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.state(), {"offset": None, "completed": True})
        self.assert_completed_no_work()

    def test_empty_final_page_after_nonempty_page_commits_completion(self):
        qdrant = FakeQdrant({None: page([point("first")], "empty-final"), "empty-final": page([])})
        self.run_builder(qdrant)
        self.assertEqual([call.get("offset") for call in qdrant.calls], [None, "empty-final"])
        self.assertEqual(self.ids(), ["first"])
        self.assertEqual(self.state(), {"offset": None, "completed": True})
        self.assert_completed_no_work()

    def test_empty_nonfinal_page_advances_its_cursor(self):
        qdrant = FakeQdrant({None: page([], "next"), "next": page([point("final")])})
        self.run_builder(qdrant)
        self.assertEqual([call.get("offset") for call in qdrant.calls], [None, "next"])
        self.assertEqual(self.ids(), ["final"])
        self.assert_completed_no_work()

    def test_final_page_without_indexable_text_still_completes(self):
        self.run_builder(FakeQdrant({None: page([point("empty", ""), point("none", None)])}))
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.state(), {"offset": None, "completed": True})
        self.assert_completed_no_work()

    def test_crash_after_nonfinal_commit_resumes_without_reinserting_page(self):
        qdrant = FakeQdrant({None: page([point("first")], "next")})
        with self.assertRaisesRegex(RuntimeError, "after durable commit"):
            self.run_builder(qdrant, connection=CrashAfterCommit(build_fts.connect()))
        self.assertEqual(self.ids(), ["first"])
        self.assertEqual(self.state(), {"offset": "next", "completed": False})
        resumed = FakeQdrant({"next": page([point("final")])})
        self.run_builder(resumed)
        self.assertEqual([call.get("offset") for call in resumed.calls], ["next"])
        self.assertEqual(self.ids(), ["first", "final"])
        self.assert_completed_no_work()

    def test_crash_after_final_commit_cannot_repeat_final_page(self):
        with self.assertRaisesRegex(RuntimeError, "after durable commit"):
            self.run_builder(FakeQdrant({None: page([point("final")])}),
                             connection=CrashAfterCommit(build_fts.connect()))
        self.assertEqual(self.ids(), ["final"])
        self.assertEqual(self.state(), {"offset": None, "completed": True})
        self.assert_completed_no_work()

    def test_failed_page_keeps_last_committed_cursor_and_not_completion(self):
        qdrant = FakeQdrant({None: page([point("first")], "next"), "next": RuntimeError("fixture scroll failure")})
        with self.assertRaisesRegex(RuntimeError, "scroll failure"):
            self.run_builder(qdrant)
        self.assertEqual(self.ids(), ["first"])
        self.assertEqual(self.state(), {"offset": "next", "completed": False})
        self.run_builder(FakeQdrant({"next": page([point("final")])}))
        self.assertEqual(self.ids(), ["first", "final"])

    def test_failed_first_page_does_not_create_completed_state(self):
        with self.assertRaisesRegex(RuntimeError, "scroll failure"):
            self.run_builder(FakeQdrant({None: RuntimeError("fixture scroll failure")}))
        self.assertEqual(self.state(), {})
        self.run_builder(FakeQdrant({None: page([point("final")])}))
        self.assertEqual(self.ids(), ["final"])

    def test_malformed_page_does_not_insert_or_mark_completed(self):
        for response in [{}, {"result": {}}, {"result": {"points": [point("uncommitted")]}},
                         {"result": {"points": {}, "next_page_offset": None}}]:
            with self.subTest(response=response):
                with self.assertRaises((KeyError, ValueError)):
                    self.run_builder(FakeQdrant({None: response}))
                self.assertEqual(self.ids(), [])
                self.assertEqual(self.state(), {})

    def test_completion_write_failure_rolls_back_rows_and_cursor_together(self):
        connection = build_fts.connect()
        connection.execute("""CREATE TRIGGER reject_completion BEFORE INSERT ON build_state
            WHEN NEW.key='completed' AND NEW.value='true'
            BEGIN SELECT RAISE(ABORT, 'fixture completion failure'); END""")
        connection.commit()
        qdrant = FakeQdrant({None: page([point("first")], "next"), "next": page([point("final")])})
        with self.assertRaisesRegex(sqlite3.IntegrityError, "completion failure"):
            self.run_builder(qdrant, connection=connection)
        self.assertEqual(self.ids(), ["first"])
        self.assertEqual(self.state(), {"offset": "next", "completed": False})
        connection = build_fts.connect()
        connection.execute("DROP TRIGGER reject_completion")
        connection.commit()
        self.run_builder(FakeQdrant({"next": page([point("final")])}), connection=connection)
        self.assertEqual(self.ids(), ["first", "final"])
        self.assert_completed_no_work()

    def test_topup_remains_available_after_build_completed(self):
        self.run_builder(FakeQdrant({None: page([point("existing")])}))
        before = self.state()
        for _ in range(2):
            qdrant = FakeQdrant({None: page([point("existing"), point("new")])})
            self.assertEqual(self.run_builder(qdrant, ("--topup",)), 0)
            self.assertEqual(len(qdrant.calls), 1)
            self.assertEqual(self.ids(), ["existing", "new"])
            self.assertEqual(self.state(), before)

    def test_partial_legacy_blocks_build_and_topup_before_qdrant(self):
        connection = build_fts.connect()
        connection.execute("INSERT INTO chunks(text,point_id,arxiv_id) VALUES ('legacy','old-id','old-paper')")
        connection.commit()
        connection.close()
        for args in ((), ("--topup",)):
            qdrant = FakeQdrant({})
            with self.assertRaises(build_fts.CatalogBlocked):
                self.run_builder(qdrant, args)
            self.assertEqual(qdrant.calls, [])

    def test_completed_legacy_build_returns_before_catalog_or_qdrant(self):
        connection = build_fts.connect()
        connection.execute("INSERT INTO chunks(text,point_id,arxiv_id) VALUES ('legacy','old-id','old-paper')")
        connection.execute("INSERT INTO build_state(key,value) VALUES ('completed','true')")
        connection.commit()
        statements = []
        connection.set_trace_callback(statements.append)
        qdrant = FakeQdrant({})
        self.assertEqual(self.run_builder(qdrant, connection=connection), 0)
        self.assertEqual(qdrant.calls, [])
        self.assertFalse(any("pipeline_fts_scope" in sql for sql in statements))


if __name__ == "__main__":
    unittest.main()
