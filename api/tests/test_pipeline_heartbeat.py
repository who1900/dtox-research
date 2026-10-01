"""Load only heartbeat functions: no pipeline imports, log files, DB or network."""
import ast
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch


class StopLoop(BaseException):
    pass


def heartbeat_functions(directory):
    path = Path(__file__).resolve().parents[2] / "pipeline" / "service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"stage_heartbeat", "newest_heartbeat", "process_start_ticks", "heartbeat_snapshot", "persist_stage_heartbeats",
             "stalled_stages", "_stage_loop", "_reporter_loop", "run_stages_parallel", "start_watchdog"}
    selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names],
                          type_ignores=[])
    namespace = {"time": types.SimpleNamespace(time=Mock(return_value=1000), sleep=Mock()),
                 "os": os, "Path": Path, "tempfile": tempfile, "json": json,
                 "threading": threading, "BASE_DIR": directory,
                 "STAGE_HEARTBEAT_FILE": directory / "stage_heartbeats.json",
                 "_heartbeats": {}, "_heartbeats_lock": threading.Lock(), "log": Mock(),
                 "get_conn": Mock(), "requests": Mock(), "status_counts": Mock(return_value={}),
                 "REPORT_INTERVAL": 300, "STAGE_IDLE_SLEEP": 20, "STAGE_BUSY_SLEEP": 1,
                 "STAGE_ERROR_SLEEP": 30, "WATCHDOG_STALL_SECONDS": 2700,
                 "WATCHDOG_POLL_SECONDS": 60, "logging": Mock()}
    exec(compile(selected, str(path), "exec"), namespace)
    namespace["process_start_ticks"] = Mock(return_value=100)
    return namespace


class PipelineHeartbeatTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.ns = heartbeat_functions(self.directory)

    def test_start_end_zero_processed_and_busy_states(self):
        beat = self.ns["stage_heartbeat"]
        beat("embed")
        beat("embed", "start")
        record = self.ns["_heartbeats"]["embed"]
        self.assertEqual(record["phase"], "busy")
        self.ns["time"].time.return_value = 1100
        beat("embed", "end", 0)
        self.assertEqual(record["phase"], "idle")
        self.assertEqual(record["heartbeat_at"], 1100)
        self.assertEqual(record["last_success_at"], 1100)
        beat("embed", "start")
        beat("embed", "end", 2)
        self.assertEqual(record["phase"], "waiting")
        beat("embed", "error")
        self.assertEqual(record["phase"], "backoff")

    def test_atomic_snapshot_replacement_and_no_sensitive_fields(self):
        self.ns["stage_heartbeat"]("embed", "start")
        destination = self.ns["STAGE_HEARTBEAT_FILE"]
        with patch.object(os, "replace", wraps=os.replace) as replace:
            self.ns["persist_stage_heartbeats"]()
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(replace.call_args.args[1], destination)
        snapshot = json.loads(destination.read_text())
        self.assertEqual(set(snapshot), {"version", "pid", "process_start_ticks", "written_at", "stages"})
        self.assertEqual(snapshot["pid"], os.getpid())
        self.assertEqual(snapshot["process_start_ticks"], 100)
        self.assertEqual(snapshot["stages"]["embed"]["phase"], "busy")
        self.assertEqual(list(self.directory.iterdir()), [destination])

    def test_failed_replace_preserves_previous_snapshot_and_cleans_temporary(self):
        self.ns["stage_heartbeat"]("embed")
        self.ns["persist_stage_heartbeats"]()
        before = self.ns["STAGE_HEARTBEAT_FILE"].read_text()
        with patch.object(os, "replace", side_effect=OSError("mock failure")):
            with self.assertRaises(OSError):
                self.ns["persist_stage_heartbeats"]()
        self.assertEqual(self.ns["STAGE_HEARTBEAT_FILE"].read_text(), before)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_reporter_cannot_mask_stuck_stage_and_idle_stage_ticks(self):
        beat = self.ns["stage_heartbeat"]
        beat("fulltext", "start")
        self.ns["time"].time.return_value = 3801
        beat("reporter", "end", 0)
        beat("harvest", "end", 0)
        self.assertEqual(self.ns["newest_heartbeat"](), 3801)
        self.assertEqual(self.ns["stalled_stages"](), {"fulltext": "stuck call"})
        self.ns["time"].time.return_value = 6702
        self.assertEqual(self.ns["stalled_stages"]()["harvest"], "stale heartbeat")

    def test_zero_processed_loop_emits_start_and_end_before_idle_sleep(self):
        self.ns["time"].sleep.side_effect = StopLoop

        def process(conn):
            self.assertEqual(self.ns["_heartbeats"]["chunk"]["phase"], "busy")
            return 0

        with self.assertRaises(StopLoop):
            self.ns["_stage_loop"]("chunk", process, False)
        self.assertEqual(self.ns["_heartbeats"]["chunk"]["phase"], "idle")
        self.ns["time"].sleep.assert_called_once_with(20)

    def test_error_loop_emits_error_heartbeat(self):
        self.ns["time"].sleep.side_effect = StopLoop
        with self.assertRaises(StopLoop):
            self.ns["_stage_loop"]("chunk", Mock(side_effect=ValueError("mock failure")), False)
        self.assertEqual(self.ns["_heartbeats"]["chunk"]["phase"], "backoff")
        self.ns["time"].sleep.assert_called_once_with(30)

    def test_reporter_persists_each_cycle(self):
        self.ns["time"].sleep.side_effect = [None, StopLoop]
        with self.assertRaises(StopLoop):
            self.ns["_reporter_loop"]()
        snapshot = json.loads(self.ns["STAGE_HEARTBEAT_FILE"].read_text())
        self.assertEqual(snapshot["stages"]["reporter"]["phase"], "idle")

    def test_initial_snapshot_before_monitoring_loop_and_failure_is_nonfatal(self):
        self.ns["STAGES"] = [("chunk", Mock(), False), ("embed", Mock(), True)]
        self.ns["time"].sleep.side_effect = StopLoop
        with patch.object(threading, "Thread"):
            with self.assertRaises(StopLoop):
                self.ns["run_stages_parallel"]()
        snapshot = json.loads(self.ns["STAGE_HEARTBEAT_FILE"].read_text())
        self.assertEqual(set(snapshot["stages"]), {"chunk", "embed", "reporter"})
        self.ns["persist_stage_heartbeats"] = Mock(side_effect=OSError("mock permission"))
        with patch.object(threading, "Thread"):
            with self.assertRaises(StopLoop):
                self.ns["run_stages_parallel"]()
        self.ns["log"].warning.assert_called_once()

    def test_stage_names_and_existing_scheduling_constants_unchanged(self):
        from ops import monitor
        path = Path(__file__).resolve().parents[2] / "pipeline" / "service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {n.targets[0].id: n.value for n in tree.body if isinstance(n, ast.Assign)
                     and isinstance(n.targets[0], ast.Name)}
        names = [ast.literal_eval(entry.elts[0]) for entry in constants["STAGES"].elts]
        self.assertEqual(set(names) | {"reporter"}, set(monitor.STAGES))
        for name, value in (("STAGE_BUSY_SLEEP", 1), ("STAGE_IDLE_SLEEP", 20),
                            ("STAGE_ERROR_SLEEP", 30), ("REPORT_INTERVAL", 300)):
            self.assertEqual(ast.literal_eval(constants[name]), value)


if __name__ == "__main__":
    unittest.main()
