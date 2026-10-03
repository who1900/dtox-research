"""Offline backup and observability regressions against temporary fixtures."""
import ast
import contextlib
import json
import math
import os
import pathlib
import sqlite3
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch


def load_functions(filename, *names, **globals_):
    source = pathlib.Path(__file__).resolve().parents[1] / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(selected) == len(names)
    namespace = {"sqlite3": sqlite3, "time": time, "contextlib": contextlib, "math": math,
                 "json": json, **globals_}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return types.SimpleNamespace(**namespace)


class BackupRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.backup = load_functions("backup.py", "verify", "backup_files", "prune", DEST=self.root)

    def tearDown(self):
        self.directory.cleanup()

    def database(self, name, schema="CREATE TABLE required(id INTEGER)"):
        path = self.root / name
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.executescript(schema)
            conn.commit()
        return path

    def test_latest_and_retention_follow_mtime_not_legacy_name(self):
        legacy = self.database("state-pre-oai.db")
        latest = self.database("state-20261002T120000Z.db")
        os.utime(legacy, (100, 100))
        os.utime(latest, (200, 200))
        self.assertEqual(self.backup.backup_files("state"), [latest, legacy])
        self.assertEqual(self.backup.prune("state", 1), 1)
        self.assertTrue(latest.exists())
        self.assertFalse(legacy.exists())

    def test_missing_required_table_is_not_ok(self):
        path = self.database("missing.db", "CREATE TABLE other(id INTEGER)")
        ok, info = self.backup.verify(path, ["required"])
        self.assertFalse(ok)
        self.assertIn("schema", info)

    def test_view_does_not_substitute_for_required_table(self):
        path = self.database("view.db", "CREATE VIEW required AS SELECT 1 AS id")
        self.assertFalse(self.backup.verify(path, ["required"])[0])

    def test_unreadable_required_table_and_integrity_error_are_not_ok(self):
        connection = Mock()
        connection.execute.side_effect = [iter([("ok",)]), Mock(fetchone=lambda: ("table",)),
                                          sqlite3.DatabaseError("table read failed")]
        with patch.object(sqlite3, "connect", return_value=connection):
            self.assertFalse(self.backup.verify("fixture", ["required"])[0])
        connection = Mock()
        connection.execute.return_value = iter([("ok",), ("page corrupt",)])
        with patch.object(sqlite3, "connect", return_value=connection):
            self.assertFalse(self.backup.verify("fixture", ["required"])[0])

    def test_valid_empty_schema_is_ok_and_foreign_key_violation_is_bad(self):
        valid = self.database("valid.db")
        self.assertEqual(self.backup.verify(valid, ["required"]), (True, {"required": 0}))
        invalid = self.database("fk.db", "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                                "CREATE TABLE required(id INTEGER REFERENCES parent(id));"
                                "INSERT INTO required VALUES (1);")
        self.assertFalse(self.backup.verify(invalid, ["required"])[0])

    def test_corrupt_and_missing_backups_are_not_ok(self):
        missing = self.root / "missing.db"
        self.assertFalse(self.backup.verify(missing, ["required"])[0])
        self.assertFalse(missing.exists())
        corrupt = self.database("corrupt.db")
        with corrupt.open("r+b") as stream:
            stream.write(b"not a sqlite database")
        self.assertFalse(self.backup.verify(corrupt, ["required"])[0])

    def test_monitor_selects_newest_mtime(self):
        old = self.database("judgments-pre-oai.db")
        fresh = self.database("judgments-20261002T120000Z.db")
        os.utime(old, (100, 100))
        os.utime(fresh, (999, 999))
        monitor = load_functions("monitor.py", "check_backups", BACKUP_DIR=self.root,
                                 BACKUP_MAX_AGE_H=1, time=types.SimpleNamespace(time=lambda: 1000))
        self.assertTrue(monitor.check_backups()["backup:judgments"][0])


class MonitorRegressionTests(unittest.TestCase):
    def test_absent_completion_history_is_unknown_not_zero_or_rate(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as conn:
            monitor = load_functions("monitor.py", "completion_checks")
            checks = monitor.completion_checks(conn)
            self.assertIsNone(checks["pipeline:new_completed"][0])
            self.assertIsNone(checks["pipeline:reprocessing"][0])

    def test_observed_counters_are_separate_and_no_rate_is_invented(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript("CREATE TABLE completion_totals(singleton,started_at,first_completed,reprocessed,unknown);"
                               "INSERT INTO completion_totals VALUES (1,100,2,5,3);"
                               "CREATE TABLE paper_index_sync(arxiv_id TEXT);"
                               "INSERT INTO paper_index_sync VALUES ('pending');")
            monitor = load_functions("monitor.py", "completion_checks")
            checks = monitor.completion_checks(conn)
            self.assertIn("2 observed first", checks["pipeline:new_completed"][1])
            self.assertIn("5 observed reprocessing", checks["pipeline:reprocessing"][1])
            self.assertIn("rate unknown", checks["pipeline:new_completed"][1])
            self.assertIsNone(checks["pipeline:completion_unknown"][0])
            self.assertIsNone(checks["pipeline:index_sync"][0])

    def test_invalid_counters_are_unknown(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript("CREATE TABLE completion_totals(singleton,started_at,first_completed,reprocessed,unknown);"
                               "INSERT INTO completion_totals VALUES (1,100,-1,0,0);"
                               "CREATE TABLE paper_index_sync(arxiv_id TEXT);")
            monitor = load_functions("monitor.py", "completion_checks")
            self.assertIsNone(monitor.completion_checks(conn)["pipeline:new_completed"][0])

    def test_live_error_heartbeats_fail_while_successful_idle_is_healthy(self):
        snapshot = {"version": 1, "pid": 123, "written_at": 200, "stages": {
            "errors": {"phase": "busy", "started_at": 199, "heartbeat_at": 199, "error_since_at": 0},
            "idle": {"phase": "idle", "heartbeat_at": 199, "last_success_at": 199}}}
        heartbeat = Mock()
        heartbeat.open.return_value.__enter__ = Mock(return_value=types.SimpleNamespace(read=lambda _: json.dumps(snapshot)))
        heartbeat.open.return_value.__exit__ = Mock(return_value=False)
        monitor = load_functions("monitor.py", "check_stage_heartbeats", HEARTBEAT_FILE=heartbeat,
                                 STAGES=["errors", "idle"], STAGE_STUCK_SECONDS=100,
                                 HEARTBEAT_MAX_AGE_SECONDS=100, time=types.SimpleNamespace(time=lambda: 200),
                                 validate_snapshot_process=lambda _: (True, "verified"), journal_stage_activity=Mock())
        checks = monitor.check_stage_heartbeats()
        self.assertFalse(checks["pipeline:stage:errors"][0])
        self.assertTrue(checks["pipeline:stage:idle"][0])


if __name__ == "__main__":
    unittest.main()
