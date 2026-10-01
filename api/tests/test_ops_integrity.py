import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from ops import qdrant_oom_guard as guard
from ops import monitor


def inspect_result(**changes):
    data = {"Name": "/qdrant", "Id": "a" * 64,
            "State": {"Running": True, "Paused": False, "Restarting": False,
                      "Dead": False, "Status": "running", "Pid": 42}}
    for name, value in changes.items():
        if name in data["State"]:
            data["State"][name] = value
        else:
            data[name] = value
    return subprocess.CompletedProcess([], 0, json.dumps(data), "")


class QdrantOOMGuardTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(guard.sys, "platform", "linux"))
        stack.enter_context(patch.object(guard.os, "O_DIRECTORY", 65536, create=True))
        stack.enter_context(patch.object(guard.os, "O_NOFOLLOW", 131072, create=True))
        self.inspect = stack.enter_context(patch.object(guard.subprocess, "run", return_value=inspect_result()))
        self.open = stack.enter_context(patch.object(guard.os, "open", side_effect=self.open_proc))
        self.close = stack.enter_context(patch.object(guard.os, "close"))
        self.read = stack.enter_context(patch.object(guard.os, "read", side_effect=self.read_proc))
        self.write = stack.enter_context(patch.object(guard.os, "write", return_value=5))
        stack.enter_context(patch.object(guard.os, "lseek"))
        self.comm = b"qdrant\n"
        self.started = 100
        self.score = 0
        self.adj_reads = 0
        self.child_comm = b"qdrant\n"
        self.child_ppid = 42
        self.children = b"43\n"
        self.child_started = 200
        self.child_group = "0::/system.slice/docker-" + "a" * 64 + ".scope\n"

    def open_proc(self, path, flags, **kwargs):
        if path == "/proc/42":
            return 10
        if path in ("/proc/43", "/proc/44"):
            return 20 if path.endswith("43") else 30
        directory = kwargs["dir_fd"]
        self.assertIn(directory, (10, 20, 30))
        return directory + {"comm": 1, "stat": 2, "oom_score_adj": 3,
                            "cgroup": 4, "task/42/children": 5}[path]

    def read_proc(self, fd, count):
        if fd in (11, 21, 31):
            return self.comm if fd == 11 else self.child_comm
        if fd in (12, 22, 32):
            child = fd != 12
            pid = 42 if not child else 43 if fd == 22 else 44
            comm = self.child_comm if child else self.comm
            fields = ["S", str(self.child_ppid if child else 2)] + ["0"] * 17
            fields.append(str(self.child_started if child else self.started))
            return (f"{pid} ({comm.decode().strip()}) " + " ".join(fields)).encode()
        if fd in (14, 24, 34):
            return (self.child_group if fd != 14 else "0::/system.slice/docker-" + "a" * 64 + ".scope\n").encode()
        if fd == 15:
            return self.children
        self.adj_reads += 1
        return str(self.score if self.adj_reads == 1 else -900).encode()

    def test_only_strict_local_inspect_and_verified_main_pid_written(self):
        self.assertEqual(guard.protect_qdrant(), 42)
        self.write.assert_called_once_with(13, b"-900\n")
        self.assertEqual(self.inspect.call_count, 2)
        for call in self.inspect.call_args_list:
            command = call.args[0]
            self.assertEqual(command[:5], ["/usr/bin/docker", "--host",
                             "unix:///var/run/docker.sock", "container", "inspect"])
            self.assertEqual(command[-1], "qdrant")
            self.assertNotIn(".Config", command[-2])
            self.assertNotIn("{{json .}}", command)
            self.assertEqual(call.kwargs["timeout"], 5)
        self.assertEqual(self.close.call_args_list[-2].args, (13,))
        self.assertEqual(self.close.call_args_list[-1].args, (10,))

    def test_already_protected_is_idempotent(self):
        self.score = -900
        guard.protect_qdrant()
        self.write.assert_not_called()

    def test_wrong_container_state_or_pid_no_proc_access(self):
        invalid = ({"Name": "/qdrant-other"}, {"Pid": 0}, {"Pid": 1}, {"Pid": "42"},
                   {"Pid": True}, {"Running": False}, {"Paused": True},
                   {"Restarting": True}, {"Dead": True}, {"Status": "exited"}, {"Id": "bad"})
        for changes in invalid:
            with self.subTest(changes=changes):
                self.inspect.return_value = inspect_result(**changes)
                with self.assertRaises(guard.GuardError):
                    guard.protect_qdrant()
        self.open.assert_not_called()
        self.write.assert_not_called()

    def test_wrapper_without_qdrant_child_rejected(self):
        self.comm = b"bash\n"
        self.child_comm = b"sleep\n"
        with self.assertRaisesRegex(guard.GuardError, "exactly one"):
            guard.protect_qdrant()
        self.write.assert_not_called()
        self.assertNotIn("oom_score_adj", [call.args[0] for call in self.open.call_args_list])

    def test_container_replaced_or_restarted_before_write(self):
        for changed in (inspect_result(Id="b" * 64), inspect_result(Pid=99)):
            self.inspect.side_effect = [inspect_result(), changed]
            with self.assertRaisesRegex(guard.GuardError, "changed"):
                guard.protect_qdrant()
        self.write.assert_not_called()

    def test_process_starttime_changed_before_write(self):
        with patch.object(guard, "process_identity", side_effect=[("qdrant", 2, 100, ()), ("qdrant", 2, 101, ())]):
            with self.assertRaisesRegex(guard.GuardError, "changed"):
                guard.protect_qdrant()
        self.write.assert_not_called()

    def test_missing_proc_and_permission_failure_fail_closed(self):
        self.open.side_effect = FileNotFoundError("process exited")
        with contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(guard.main(), 1)
        self.assertIn("guard failed", out.getvalue())
        self.write.assert_not_called()
        self.open.side_effect = self.open_proc
        self.write.side_effect = PermissionError("score permission denied")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main(), 1)
        self.assertEqual(self.close.call_args_list[-1].args, (10,))

    def test_inspect_error_timeout_and_malformed_no_write(self):
        for value in (subprocess.CompletedProcess([], 1, "", "do not echo"),
                      subprocess.CompletedProcess([], 0, "not json", "")):
            self.inspect.return_value = value
            with self.assertRaises(guard.GuardError):
                guard.protect_qdrant()
        self.inspect.side_effect = subprocess.TimeoutExpired("docker", 5)
        with self.assertRaisesRegex(guard.GuardError, "unavailable"):
            guard.protect_qdrant()
        self.write.assert_not_called()

    def test_dead_or_wrong_stat_process_and_verification_failure(self):
        with patch.object(guard, "read_proc", side_effect=["qdrant", "42 (qdrant) Z"]):
            with self.assertRaises(guard.GuardError):
                guard.protect_qdrant()
        self.write.assert_not_called()
        self.read.side_effect = lambda fd, count: b"0" if fd == 13 else self.read_proc(fd, count)
        with self.assertRaisesRegex(guard.GuardError, "verification failed"):
            guard.protect_qdrant()

    def test_wrapper_single_direct_qdrant_child_only_is_written(self):
        self.comm = b"bash\n"
        self.assertEqual(guard.protect_qdrant(), 43)
        self.write.assert_called_once_with(23, b"-900\n")
        self.assertEqual(self.inspect.call_count, 2)
        self.assertNotIn("/proc/99", [c.args[0] for c in self.open.call_args_list])

    def test_wrapper_multiple_qdrant_children_and_host_other_cgroup_rejected(self):
        self.comm = b"bash\n"
        self.children = b"43 44\n"
        with self.assertRaisesRegex(guard.GuardError, "exactly one"):
            guard.protect_qdrant()
        self.children = b"43\n"
        for group in ("0::/user.slice\n", "0::/system.slice/docker-" + "a" * 63 + "b.scope\n",
                      "0::/system.slice/docker-" + "a" * 64 + ".scope/other-container\n"):
            self.child_group = group
            with self.assertRaises(guard.GuardError):
                guard.protect_qdrant()
        self.write.assert_not_called()

    def test_child_wrong_ppid_and_pid_reuse_fail_before_write(self):
        self.comm = b"bash\n"
        self.child_ppid = 99
        with self.assertRaisesRegex(guard.GuardError, "PPID"):
            guard.protect_qdrant()
        self.child_ppid = 42
        original = self.read_proc
        child_stats = 0

        def reused(fd, count):
            nonlocal child_stats
            if fd == 22:
                child_stats += 1
                if child_stats > 1:
                    self.child_started = 201
            return original(fd, count)

        self.read.side_effect = reused
        with self.assertRaisesRegex(guard.GuardError, "child changed"):
            guard.protect_qdrant()
        self.write.assert_not_called()

    def test_main_host_cgroup_and_new_second_child_on_recheck_fail(self):
        with patch.object(guard, "read_proc", side_effect=["qdrant", "42 (qdrant) S 2 " + "0 " * 17 + "100", "0::/user.slice"]):
            with self.assertRaisesRegex(guard.GuardError, "cgroup"):
                guard.protect_qdrant()
        self.comm = b"bash\n"
        original = self.read_proc
        children_reads = 0

        def additional_child(fd, count):
            nonlocal children_reads
            if fd == 15:
                children_reads += 1
                return b"43\n" if children_reads == 1 else b"43 44\n"
            return original(fd, count)

        self.read.side_effect = additional_child
        with self.assertRaisesRegex(guard.GuardError, "exactly one"):
            guard.protect_qdrant()
        self.write.assert_not_called()

    def test_static_unit_limits_and_persistent_timer(self):
        root = Path(__file__).resolve().parents[2] / "ops" / "deploy" / "oracle"
        service = (root / "dtox-qdrant-oom-guard.service").read_text()
        timer = (root / "dtox-qdrant-oom-guard.timer").read_text()
        for directive in ("Type=oneshot", "User=root", "MemoryMax=64M", "CPUQuota=20%",
                          "TasksMax=16", "TimeoutStartSec=20", "CAP_SYS_RESOURCE"):
            self.assertIn(directive, service)
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnCalendar=", timer)
        self.assertNotIn("EnvironmentFile=", service)
        self.assertNotIn("Restart=", service)


class Response:
    def __init__(self, data, headers=None, status=200):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.headers = headers or {"Content-Type": "application/json"}
        self.status = status

    def read(self, size=None):
        return self.data if size is None else self.data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MonitorIntegrityTests(unittest.TestCase):
    def snapshot(self):
        return {"version": 1, "pid": 42, "process_start_ticks": 100, "written_at": 10000,
                "stages": {stage: {"phase": "idle", "heartbeat_at": 9900,
                                   "started_at": 9000, "ended_at": 9900}
                           for stage in monitor.STAGES}}

    def heartbeat_check(self, data, pid="42"):
        path = unittest.mock.Mock()
        path.open.return_value = io.StringIO(json.dumps(data))
        def proc_identity(process_pid):
            if process_pid == data["pid"] and str(process_pid) != pid:
                raise FileNotFoundError()
            return (2, 100, ("/system.slice/dtox-research.service",))

        properties = f"MainPID={pid}\nControlGroup=/system.slice/dtox-research.service\n"
        with (patch.object(monitor, "HEARTBEAT_FILE", path),
              patch.object(monitor.time, "time", return_value=10000),
              patch.object(monitor, "proc_process_identity", side_effect=proc_identity),
              patch.object(monitor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, properties, ""))):
            return monitor.check_stage_heartbeats()

    def test_wrapper_direct_child_same_exact_cgroup_accepted_and_only_safe_properties(self):
        snapshot = self.snapshot()
        properties = "MainPID=40\nControlGroup=/system.slice/dtox-research.service\n"
        identities = {40: (2, 50, ("/system.slice/dtox-research.service",)),
                      42: (40, 100, ("/system.slice/dtox-research.service",))}
        with (patch.object(monitor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, properties, "")) as run,
              patch.object(monitor, "proc_process_identity", side_effect=lambda pid: identities[pid]) as proc):
            self.assertIs(monitor.validate_snapshot_process(snapshot)[0], True)
        self.assertEqual(proc.call_count, 4)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ["systemctl", "show", "dtox-research.service",
                                                "--property=MainPID", "--property=ControlGroup"])

    def test_unrelated_wrong_group_or_ppid_rejected(self):
        group = "/system.slice/dtox-research.service"
        for target in ((99, 100, (group,)), (40, 100, (group + "/other",)),
                       (40, 100, ("/system.slice/unrelated.service",))):
            identities = {40: (2, 50, (group,)), 42: target}
            with (patch.object(monitor, "service_process_identity", return_value=(40, group)),
                  patch.object(monitor, "proc_process_identity", side_effect=lambda pid: identities[pid])):
                self.assertIs(monitor.validate_snapshot_process(self.snapshot())[0], False)

    def test_pid_reuse_restart_and_legacy_snapshot_are_unknown(self):
        group = "/system.slice/dtox-research.service"
        with (patch.object(monitor, "service_process_identity", return_value=(42, group)),
              patch.object(monitor, "proc_process_identity", return_value=(2, 101, (group,)))):
            self.assertIsNone(monitor.validate_snapshot_process(self.snapshot())[0])
        with (patch.object(monitor, "service_process_identity", side_effect=[(42, group), (99, group)]),
              patch.object(monitor, "proc_process_identity", return_value=(2, 100, (group,)))):
            self.assertIsNone(monitor.validate_snapshot_process(self.snapshot())[0])
        old = self.snapshot()
        del old["process_start_ticks"]
        with (patch.object(monitor, "service_process_identity", return_value=(42, group)),
              patch.object(monitor, "proc_process_identity", return_value=(2, 100, (group,)))):
            self.assertIsNone(monitor.validate_snapshot_process(old)[0])

    def test_proc_parser_stat_and_exact_v2_or_v1_systemd_group(self):
        fields = ["S", "40"] + ["0"] * 17 + ["100"]
        stat = "42 (python worker) " + " ".join(fields)
        for cgroup in ("0::/system.slice/dtox-research.service\n",
                       "1:name=systemd:/system.slice/dtox-research.service\n2:cpu:/different\n"):
            with patch.object(monitor.pathlib.Path, "open", side_effect=[io.StringIO(stat), io.StringIO(cgroup)]):
                self.assertEqual(monitor.proc_process_identity(42),
                                 (40, 100, ("/system.slice/dtox-research.service",)))

    def test_stuck_call_not_masked_by_reporter_or_empty_queue(self):
        data = self.snapshot()
        data["stages"]["fulltext"].update(phase="busy", started_at=7200, heartbeat_at=9999)
        checks = self.heartbeat_check(data)
        self.assertFalse(checks["pipeline:stage:fulltext"][0])
        self.assertIn("stuck call", checks["pipeline:stage:fulltext"][1])
        self.assertTrue(checks["pipeline:stage:reporter"][0])
        self.assertTrue(checks["pipeline:stage:harvest"][0])
        self.assertIn("idle", checks["pipeline:stage:harvest"][1])

    def test_old_process_snapshot_is_unknown_not_new_process_stuck(self):
        data = self.snapshot()
        data["written_at"] = 1
        checks = self.heartbeat_check(data, pid="99")
        self.assertIsNone(checks["pipeline:heartbeat_file"][0])
        self.assertIn("old snapshot PID", checks["pipeline:heartbeat_file"][1])
        self.assertFalse(any(ok is False for ok, _detail in checks.values()))

    def test_stale_snapshot_invalid_stage_and_error_backoff(self):
        data = self.snapshot()
        data["written_at"] = 9000
        checks = self.heartbeat_check(data)
        self.assertFalse(checks["pipeline:heartbeat_file"][0])
        self.assertIsNone(checks["pipeline:stage:embed"][0])
        data = self.snapshot()
        data["stages"]["embed"]["phase"] = "backoff"
        data["stages"]["chunk"]["heartbeat_at"] = float("nan")
        checks = self.heartbeat_check(data)
        self.assertFalse(checks["pipeline:stage:embed"][0])
        self.assertFalse(checks["pipeline:stage:chunk"][0])

    def test_journal_activity_not_fabricated_stage_heartbeat(self):
        path = unittest.mock.Mock()
        path.open.side_effect = FileNotFoundError()
        lines = [json.dumps({"MESSAGE": "stage embed: 12", "__REALTIME_TIMESTAMP": "9999000000"}),
                 json.dumps({"MESSAGE": "counts={'done': 42}", "__REALTIME_TIMESTAMP": "9999000000"})]
        with (patch.object(monitor, "HEARTBEAT_FILE", path),
              patch.object(monitor.time, "time", return_value=10000),
              patch.object(monitor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "\n".join(lines), "")) as run):
            checks = monitor.check_stage_heartbeats()
        self.assertIsNone(checks["pipeline:stage:embed"][0])
        self.assertIn("journal activity", checks["pipeline:stage:embed"][1])
        self.assertIsNone(checks["pipeline:stage:reporter"][0])
        self.assertEqual(len(checks), len(monitor.STAGES))
        self.assertIn("--lines=2000", run.call_args.args[0])

    def test_latency_floor_and_threshold_no_two_sample_green(self):
        for samples, p95, expected in ((2, 0.1, None), (19, 0.1, None),
                                       (20, 4, True), (20, 6, False), (20, float("nan"), None)):
            response = Response({"status": "ok", "search_latency_seconds": {"samples": samples, "p95": p95}})
            with patch.object(monitor.urllib.request, "urlopen", return_value=response):
                checks = monitor.check_api()
            self.assertIs(checks["api:search_p95"][0], expected)

    def test_mcp_health_initialize_and_list_read_only_no_key(self):
        responses = [Response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
                              {"Content-Type": "application/json", "Mcp-Session-Id": "mock-session"}),
                     Response({}, status=202),
                     Response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "get_paper"}]}})]
        with patch.object(monitor.urllib.request, "urlopen", side_effect=responses) as request:
            self.assertTrue(monitor.check_mcp()["mcp:port"][0])
        methods = []
        for call in request.call_args_list:
            req = call.args[0]
            self.assertEqual(req.full_url, "http://localhost:8011/mcp")
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(req.get_header("Host"), "localhost:8011")
            self.assertIsNone(req.get_header("X-api-key"))
            methods.append(json.loads(req.data)["method"])
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/list"])
        self.assertEqual(request.call_args_list[-1].args[0].get_header("Mcp-session-id"), "mock-session")

    def test_mcp_network_error_rpc_error_wrong_id_and_sse(self):
        with patch.object(monitor.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertFalse(monitor.check_mcp()["mcp:port"][0])
        for data in ({"jsonrpc": "2.0", "id": 1, "error": {}},
                     {"jsonrpc": "2.0", "id": 9, "result": {}}, {"arbitrary": "200 response"}):
            with patch.object(monitor.urllib.request, "urlopen", return_value=Response(data)):
                self.assertFalse(monitor.check_mcp()["mcp:port"][0])
        sse = b'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"get_paper"}]}}\n\n'
        result = monitor.mcp_response(Response(sse, {"Content-Type": "text/event-stream"}), 2)
        self.assertEqual(result["tools"][0]["name"], "get_paper")

    def test_rerank_removed_actual_embed_health_and_no_old_local_qdrant(self):
        with patch.object(monitor.urllib.request, "urlopen", return_value=Response({"status": "ok"})) as request:
            self.assertTrue(monitor.check_embed()["embed:health"][0])
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:8005/healthz")
        with patch.object(monitor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "embed-small\n", "")):
            self.assertEqual(set(monitor.check_containers()), {"container:embed-small"})

    def test_completion_updates_are_not_new_or_reprocessing_counts(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "state.db"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE papers (status TEXT, updated_at TEXT)")
            connection.execute("INSERT INTO papers VALUES ('done', '2026-10-02T12:00:00Z')")
            connection.execute("INSERT INTO papers VALUES ('chunked', '2026-10-02T12:00:00Z')")
            for status in ("discovered", "quality_checked", "fulltext_fetched", "deferred", "rejected"):
                connection.execute("INSERT INTO papers VALUES (?, '2026-10-02T12:00:00Z')", (status,))
            connection.commit()
        with patch.object(monitor, "STATE_DB", str(database)):
            checks, progress = monitor.check_pipeline()
        self.assertEqual(progress, (1, 4, "2026-10-02T12:00:00Z"))
        self.assertIsNone(checks["pipeline:new_completed"][0])
        self.assertIsNone(checks["pipeline:reprocessing"][0])
        with contextlib.closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 7)

    def test_nonchunked_pending_work_is_not_idle_healthy(self):
        checks = self.run_checks((1, 3, "2026-10-02T12:00:00Z"),
                                 {"done": 1, "done_at": 1, "completion_update": "2026-10-02T12:00:00Z"})
        self.assertFalse(checks["pipeline:progress"][0])
        self.assertNotIn("idle queue", checks["pipeline:progress"][1])

    def run_checks(self, progress, state):
        with (patch.object(monitor, "check_services", return_value={}),
              patch.object(monitor, "check_containers", return_value={}),
              patch.object(monitor, "check_api", return_value={}),
              patch.object(monitor, "check_mcp", return_value={}),
              patch.object(monitor, "check_embed", return_value={}),
              patch.object(monitor, "check_stage_heartbeats", return_value={}),
              patch.object(monitor, "check_pipeline", return_value=({}, progress)),
              patch.object(monitor, "check_backups", return_value={}),
              patch.object(monitor.urllib.request, "urlopen", return_value=Response({"result": {"status": "green"}})),
              patch.object(monitor.shutil, "disk_usage", return_value=types.SimpleNamespace(free=42 * 1024 ** 3)),
              patch.object(monitor.time, "time", return_value=10000)):
            return monitor.run_checks(state)

    def test_idle_queue_baseline_and_reprocessing_activity(self):
        checks = self.run_checks((2, 0, "same"), {"done": 2, "done_at": 1, "completion_update": "same"})
        self.assertTrue(checks["pipeline:progress"][0])
        self.assertIn("idle queue", checks["pipeline:progress"][1])
        checks = self.run_checks((2, 1, "same"), {})
        self.assertIsNone(checks["pipeline:progress"][0])
        checks = self.run_checks((2, 1, "2026-10-02T12:00:00Z"),
                                 {"done": 2, "done_at": 1, "completion_update": "2026-10-01T12:00:00Z"})
        self.assertTrue(checks["pipeline:progress"][0])
        self.assertIn("not new papers", checks["pipeline:progress"][1])
        self.assertNotIn("embed:rerank", checks)

    def test_report_no_alert_env_no_notification_no_state_write(self):
        path = unittest.mock.Mock()
        path.read_text.return_value = "{}"
        with (patch.object(monitor.sys, "argv", ["monitor.py", "--report"]),
              patch.object(monitor, "STATE_FILE", path),
              patch.object(monitor, "run_checks", return_value={"unknown": (None, "no data")}),
              patch.object(monitor, "load_env", side_effect=AssertionError("secret read forbidden")),
              patch.object(monitor, "telegram", side_effect=AssertionError("notifications forbidden")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            self.assertEqual(monitor.main(), 0)
        self.assertIn("UNKNOWN", out.getvalue())
        path.write_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
