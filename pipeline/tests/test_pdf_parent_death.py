"""No native imports: mocked binding plus a <=5s Linux parent-death harness."""
import ctypes
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from pipeline import extractor, pdf_memory


class ParentDeathBindingTests(unittest.TestCase):
    def bind(self, parents, result=0):
        libc = Mock()
        libc.prctl.return_value = result
        @contextmanager
        def linux():
            with patch("sys.platform", "linux"), patch.object(signal, "SIGKILL", 9, create=True):
                yield
        return (linux(),
                patch("os.getppid", side_effect=parents),
                patch("ctypes.CDLL", return_value=libc), libc)

    def test_non_linux_has_no_effect(self):
        for platform in ("win32", "darwin"):
            with patch("sys.platform", platform), patch("ctypes.CDLL") as load, \
                    patch("os.getppid") as parent:
                pdf_memory.bind_parent_lifetime(-1)
                load.assert_not_called()
                parent.assert_not_called()

    def test_linux_binds_sigkill_and_checks_parent_twice(self):
        platform, parent, load, libc = self.bind([42, 42])
        with platform, parent as pid, load as dll:
            pdf_memory.bind_parent_lifetime(42)
            self.assertEqual(pid.call_count, 2)
            dll.assert_called_once_with(None, use_errno=True)
        libc.prctl.assert_called_once_with(1, 9, 0, 0, 0)
        self.assertEqual(libc.prctl.argtypes, [ctypes.c_int] + [ctypes.c_ulong] * 4)

    def test_parent_dead_before_binding_never_loads_libc(self):
        platform, parent, load, _ = self.bind([1])
        with platform, parent, load as dll, self.assertRaises(SystemExit) as caught:
            pdf_memory.bind_parent_lifetime(42)
        self.assertEqual(caught.exception.code, 87)
        dll.assert_not_called()

    def test_parent_death_during_binding_fails_closed(self):
        platform, parent, load, libc = self.bind([42, 1])
        with platform, parent, load, self.assertRaises(SystemExit) as caught:
            pdf_memory.bind_parent_lifetime(42)
        self.assertEqual(caught.exception.code, 87)
        libc.prctl.assert_called_once()

    def test_prctl_failure_is_not_ignored(self):
        platform, parent, load, _ = self.bind([42], -1)
        with platform, parent, load, patch("ctypes.get_errno", return_value=errno.EPERM):
            with self.assertRaises(OSError) as caught:
                pdf_memory.bind_parent_lifetime(42)
        self.assertEqual(caught.exception.errno, errno.EPERM)

    def test_actual_bootstrap_binds_before_guard_and_conversion(self):
        events = []
        guard = Mock()
        guard.bind_parent_lifetime.side_effect = lambda pid: events.append(("bind", pid))
        guard.verify_native_build.side_effect = lambda root: events.append(("verify", root))
        converter = Mock()
        converter.to_markdown.side_effect = lambda *a, **kw: events.append(("native", kw)) or "complete"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.md"
            with patch.dict("sys.modules", {"pdf_memory": guard, "pymupdf4llm": converter}), \
                    patch.object(sys, "path", list(sys.path)), \
                    patch.object(sys, "argv", ["-c", "input.pdf", str(output), "helper", "fork", "42"]):
                exec(extractor._PDF_CONVERSION_SCRIPT, {})
            self.assertEqual(output.read_text(), "complete")
        self.assertEqual(events, [("bind", 42), ("verify", "fork"),
                                  ("native", {"show_progress": False})])

    def test_parent_pid_passed_without_preexec_fn(self):
        script = ("import os,pathlib,sys; assert int(sys.argv[5]) == os.getppid(); "
                  "pathlib.Path(sys.argv[2]).write_text(sys.argv[5])")
        real_launch = subprocess.Popen
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.pdf"
            path.write_text("fixture")
            with patch.object(extractor, "_PDF_CONVERSION_SCRIPT", script), \
                    patch("subprocess.Popen", wraps=real_launch) as launch:
                self.assertEqual(extractor.convert_pdf_isolated(path, 2), str(os.getpid()))
            self.assertNotIn("preexec_fn", launch.call_args.kwargs)

    @unittest.skipUnless(sys.platform == "linux", "real Linux prctl/subreaper test; Windows cannot verify")
    def test_real_linux_parent_death_sigkill_within_five_seconds(self):
        # An isolated observer subreaps the orphan to verify SIGKILL, not just PID disappearance.
        observer = r'''
import ctypes, os, select, signal, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from pdf_memory import bind_parent_lifetime
bind_parent_lifetime(int(sys.argv[2]))
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER, test observer only
deadline = time.monotonic() + 3.5
child_code = "import os,sys,time; sys.path.insert(0,sys.argv[1]); from pdf_memory import bind_parent_lifetime; bind_parent_lifetime(int(sys.argv[2])); print(os.getpid(),flush=True); time.sleep(4)"
parent_code = "import os,subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],str(os.getpid())],start_new_session=True); time.sleep(4)"
parent = subprocess.Popen([sys.executable, '-c', parent_code, child_code, sys.argv[1]], stdout=subprocess.PIPE)
child_pid = None
try:
    assert select.select([parent.stdout], [], [], max(0,deadline-time.monotonic()))[0], 'child readiness timeout'
    child_pid = int(parent.stdout.readline())
    parent.kill()
    parent.wait(timeout=max(0.01,deadline-time.monotonic()))
    while time.monotonic() < deadline:
        pid, status = os.waitpid(child_pid, os.WNOHANG)
        if pid:
            assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status
            child_pid = None
            print('parent-death SIGKILL verified; orphan reaped', flush=True)
            break
        time.sleep(0.01)
    else:
        raise AssertionError('orphan survived parent death')
finally:
    if parent.poll() is None:
        parent.kill()
    parent.wait(timeout=0.2)
    if child_pid is not None:
        try:
            os.killpg(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        until = time.monotonic()+0.2
        while time.monotonic() < until:
            try:
                if os.waitpid(child_pid, os.WNOHANG)[0]:
                    break
            except ChildProcessError:
                break
            time.sleep(0.01)
'''
        helper = str(Path(pdf_memory.__file__).resolve().parent)
        process = subprocess.Popen([sys.executable, "-c", observer, helper, str(os.getpid())],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=4.5)
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            self.assertIn(b"SIGKILL verified; orphan reaped", stdout)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=0.4)
