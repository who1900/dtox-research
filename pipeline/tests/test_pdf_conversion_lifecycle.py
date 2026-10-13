"""Bounded local lifecycle QA: fake converter, real child; no native model/ingest."""
import ast
import gc
import os
import sqlite3
import subprocess
import tempfile
import types
import unittest
import weakref
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import Mock, patch

from pipeline import extractor


def load_service_functions(*names, **namespace):
    namespace.setdefault("stage_heartbeat", Mock())
    path = Path(__file__).resolve().parents[1] / "service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return types.SimpleNamespace(**namespace)


FAKE_CONVERTER = """
import pathlib, sys, types
assert not any(name in sys.modules for name in
               ('service', 'pipeline.service', 'extractor', 'pipeline.extractor'))
def to_markdown(path, **kwargs):
    assert kwargs == {'show_progress': False}
    with pathlib.Path(path).open(encoding='utf-8', newline='') as source:
        return source.read()
module = types.ModuleType('pymupdf4llm')
module.to_markdown = to_markdown
sys.modules['pymupdf4llm'] = module
guard = types.ModuleType('pdf_memory')
guard.bind_parent_lifetime = lambda expected_pid: None
guard.verify_native_build = lambda root: None
sys.modules['pdf_memory'] = guard
"""


class PDFConversionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pdf = Path(self.temp.name) / "input.pdf"
        self.pdf.write_text("fixture", encoding="utf-8")
        self.children = []
        self.output_dirs = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            self.output_dirs.append(Path(kwargs['cwd']))
            process = real_popen(*args, **kwargs)
            self.children.append(process)
            return process

        self.launch = patch("subprocess.Popen", side_effect=launch)
        self.launch.start()
        self.addCleanup(self.launch.stop)

    def assert_reaped(self):
        self.assertTrue(self.children)
        self.assertTrue(all(child.poll() is not None for child in self.children))
        self.assertTrue(all(not directory.exists() for directory in self.output_dirs))

    def test_full_utf8_markdown_and_defaults_without_service_import(self):
        text = ("# Method\n$$x^2 + y^2 = z^2$$\n```algorithm\nreturn α\n```\n"
                "| A | B |\n|---|---|\n| OCR | 表 |\n# Limitations\nОграничения é Ω\r\n") * 16000
        self.pdf.write_bytes(text.encode("utf-8"))
        with patch.object(extractor, "_PDF_CONVERSION_SCRIPT",
                          FAKE_CONVERTER + extractor._PDF_CONVERSION_SCRIPT):
            self.assertEqual(extractor.convert_pdf_isolated(self.pdf, 5), text)
        self.assert_reaped()

    def test_fresh_process_per_document(self):
        script = "import pathlib,sys,os; pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))"
        with patch.object(extractor, "_PDF_CONVERSION_SCRIPT", script):
            first = extractor.convert_pdf_isolated(self.pdf, 5)
            second = extractor.convert_pdf_isolated(self.pdf, 5)
        self.assertNotEqual(first, second)
        self.assert_reaped()

    def test_timeout_discards_partial_output_and_reaps(self):
        script = ("import pathlib,sys,time; "
                  "pathlib.Path(sys.argv[2]).write_text('partial'); time.sleep(60)")
        with patch.object(extractor, "_PDF_CONVERSION_SCRIPT", script):
            with self.assertRaises(extractor.PDFConversionError) as caught:
                extractor.convert_pdf_isolated(self.pdf, 0.5)
        self.assertEqual(caught.exception.reason, "conversion_timeout")
        self.assert_reaped()

    def test_nonzero_exit_never_returns_partial_markdown(self):
        script = ("import pathlib,sys; pathlib.Path(sys.argv[2]).write_text('partial'); "
                  "raise SystemExit(7)")
        with patch.object(extractor, "_PDF_CONVERSION_SCRIPT", script):
            with self.assertRaises(extractor.PDFConversionError) as caught:
                extractor.convert_pdf_isolated(self.pdf, 5)
        self.assertEqual((caught.exception.reason, caught.exception.returncode),
                         ("conversion_failed", 7))
        self.assert_reaped()

    def test_unverified_build_is_retryable_and_never_publishes_output(self):
        with patch.object(extractor, "_PDF_CONVERSION_SCRIPT", "raise SystemExit(86)"):
            with self.assertRaises(extractor.PDFConversionError) as caught:
                extractor.convert_pdf_isolated(self.pdf, 5)
        self.assertEqual(caught.exception.reason, "converter_build_unverified")
        self.assert_reaped()

    def test_invalid_utf8_is_failure_not_lossy_success(self):
        script = "import pathlib,sys; pathlib.Path(sys.argv[2]).write_bytes(bytes([255]))"
        with patch.object(extractor, "_PDF_CONVERSION_SCRIPT", script):
            with self.assertRaises(extractor.PDFConversionError) as caught:
                extractor.convert_pdf_isolated(self.pdf, 5)
        self.assertEqual(caught.exception.reason, "invalid_output")
        self.assert_reaped()

    def test_missing_output_is_failure(self):
        with patch.object(extractor, "_PDF_CONVERSION_SCRIPT", "pass"):
            with self.assertRaises(extractor.PDFConversionError) as caught:
                extractor.convert_pdf_isolated(self.pdf, 5)
        self.assertEqual(caught.exception.reason, "invalid_output")
        self.assert_reaped()

    def test_parent_interruption_reaps_before_propagating(self):
        real_popen = subprocess.Popen.side_effect

        def interrupt(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            real_wait = process.wait
            process.wait = Mock(side_effect=lambda *a, **kw: (
                (_ for _ in ()).throw(KeyboardInterrupt()) if kw else real_wait(*a, **kw))
            )
            return process

        with patch.object(extractor, "_PDF_CONVERSION_SCRIPT", "import time; time.sleep(60)"), \
                patch("subprocess.Popen", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                extractor.convert_pdf_isolated(self.pdf, 5)
        self.assert_reaped()

    def test_spawn_error_is_retryable_and_temp_directory_removed(self):
        with patch("subprocess.Popen", side_effect=OSError("fixture spawn failure")):
            with self.assertRaises(extractor.PDFConversionError) as caught:
                extractor.convert_pdf_isolated(self.pdf, 5)
        self.assertEqual(caught.exception.reason, "conversion_io_failure")

    def test_temp_directory_failure_is_retryable(self):
        with patch("tempfile.TemporaryDirectory", side_effect=OSError("fixture temp failure")):
            with self.assertRaises(extractor.PDFConversionError) as caught:
                extractor.convert_pdf_isolated(self.pdf, 5)
        self.assertEqual(caught.exception.reason, "conversion_io_failure")

    @unittest.skipUnless(os.name == "posix", "POSIX process-group teardown")
    def test_timeout_kills_descendant_group(self):
        script = ("import pathlib,sys,subprocess,time; "
                  "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                  "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)")
        with patch.object(extractor, "_PDF_CONVERSION_SCRIPT", script):
            with self.assertRaises(extractor.PDFConversionError):
                extractor.convert_pdf_isolated(self.pdf, 1)
        pid = int(self.pdf.read_text())
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            self.assertEqual(stat.read_text().split(") ", 1)[1].split()[0], "Z")
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assert_reaped()


class PDFFetchIntegrationTests(unittest.TestCase):
    def service(self, converter):
        self.paths = []

        def convert(path, timeout):
            self.paths.append((Path(path), timeout))
            self.assertTrue(Path(path).is_file())
            return converter(path, timeout)

        self.session = Mock()
        self.session.get.return_value = types.SimpleNamespace(status_code=200, content=b"%PDFfixture")
        return load_service_functions(
            "_fetch_acl_fulltext", "_fetch_openalex_fulltext",
            extractor=types.SimpleNamespace(convert_pdf_isolated=convert,
                                            PDFConversionError=extractor.PDFConversionError),
            tempfile=tempfile, os=os, log=Mock(), ACL_ID_PREFIX="acl:", ACL_PDF_BASE="fixture:",
            ARXIV_CONTACT_UA="fixture", get_fulltext_session=lambda: self.session)

    def test_both_fetch_paths_use_isolation_and_keep_full_markdown(self):
        text = "# Method\n$$α=表$$\n# Limitations\n" + "full body " * 100
        service = self.service(lambda _path, _timeout: text)
        self.assertEqual(service._fetch_acl_fulltext("acl:fixture", "abstract"),
                         ("acl:fixture", text, "acl-pdf"))
        self.assertEqual(service._fetch_openalex_fulltext("oa:fixture", "fixture:", "abstract"),
                         ("oa:fixture", text, "openalex-oa-pdf"))
        self.assertEqual([timeout for _, timeout in self.paths], [90, 120])
        self.assertTrue(all(not path.exists() for path, _ in self.paths))

    def test_conversion_failures_remain_retryable_despite_stored_abstract(self):
        for reason in ("conversion_timeout", "conversion_failed", "invalid_output", "conversion_io_failure"):
            with self.subTest(reason=reason):
                service = self.service(Mock(side_effect=extractor.PDFConversionError(reason)))
                self.assertEqual(service._fetch_acl_fulltext("acl:fixture", "abstract"),
                                 ("acl:fixture", None, None))
                self.assertEqual(service._fetch_openalex_fulltext("oa:fixture", "fixture:", "abstract"),
                                 ("oa:fixture", None, None))
                self.assertTrue(all(not path.exists() for path, _ in self.paths))

    def test_existing_absent_source_and_short_text_fallbacks_preserved(self):
        service = self.service(lambda _path, _timeout: "")
        self.assertEqual(service._fetch_acl_fulltext("acl:fixture", "abstract"),
                         ("acl:fixture", "\\section{Abstract}\nabstract", "acl-abstract"))
        self.assertEqual(service._fetch_openalex_fulltext("oa:fixture", "fixture:", "abstract"),
                         ("oa:fixture", "# Abstract\n\nabstract", "openalex-abstract"))
        self.assertEqual(service._fetch_openalex_fulltext("oa:fixture", None, "abstract"),
                         ("oa:fixture", None, None))
        self.session.get.return_value.status_code = 404
        self.assertEqual(service._fetch_acl_fulltext("acl:fixture", "abstract")[2], "acl-abstract")


class FulltextWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,abstract TEXT,source_url TEXT,
                                status TEXT,fulltext_source TEXT,updated_at TEXT);
            CREATE TABLE completion_history(arxiv_id TEXT,first_known INTEGER,completed INTEGER);
        """)
        self.ids = [f"fixture-{i}" for i in range(17)]
        self.conn.executemany("INSERT INTO papers VALUES (?, 'abstract', 'fixture:', 'quality_checked', NULL, NULL)",
                              [(paper,) for paper in self.ids])
        self.conn.executemany("INSERT INTO completion_history VALUES (?,1,0)", [(paper,) for paper in self.ids])
        self.conn.commit()

    def service(self, fetch, pool=ThreadPoolExecutor, completed=as_completed):
        return load_service_functions(
            "fulltext_step", FULLTEXT_CYCLE_LIMIT=150, FULLTEXT_WORKERS=4,
            FULLTEXT_DIR=self.root, safe_id=lambda paper: paper, _fetch_one_fulltext=fetch,
            ThreadPoolExecutor=pool, as_completed=completed, os=os, log=Mock(), now_iso=lambda: "fixture")

    def test_pending_and_retained_results_bounded_without_dropping_batch(self):
        refs = []
        peaks = []

        class ImmediatePool:
            def __init__(self, max_workers):
                self.max_workers = max_workers
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def submit(self, function, *args):
                future = Future()
                future.set_result(function(*args))
                refs.append(weakref.ref(future))
                return future

        def completed(pending):
            gc.collect()
            peaks.append((len(pending), sum(ref() is not None for ref in refs)))
            return iter(pending)

        fetch = Mock(side_effect=lambda paper, *_args: (paper, "fulltext 表", "fixture"))
        service = self.service(fetch, ImmediatePool, completed)
        self.assertEqual(service.fulltext_step(self.conn), len(self.ids))
        self.assertEqual(fetch.call_count, len(self.ids))
        self.assertTrue(all(pending <= 4 and retained <= 5 for pending, retained in peaks), peaks)
        self.assertEqual(len(list(self.root.glob("*.tex"))), len(self.ids))

    def test_retry_preserves_existing_cache_and_then_recovers_all_documents(self):
        path = self.root / (self.ids[0] + ".tex")
        path.write_text("previous durable fulltext", encoding="utf-8")
        fetch = Mock(return_value=(self.ids[0], None, None))
        service = self.service(fetch)
        self.assertEqual(service.fulltext_step(self.conn, limit=1), 0)
        self.assertEqual(path.read_text(), "previous durable fulltext")
        self.assertEqual(self.conn.execute("SELECT status FROM papers WHERE arxiv_id=?", (self.ids[0],)).fetchone()[0],
                         "quality_checked")
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        fetch.side_effect = lambda paper, *_args: (paper, "recovered fulltext", "fixture")
        self.assertEqual(service.fulltext_step(self.conn), len(self.ids))
        self.assertEqual(path.read_text(), "recovered fulltext")

    def test_worker_error_keeps_retry_queue_and_other_jobs_progress(self):
        def fetch(paper, *_args):
            if paper == self.ids[0]:
                raise extractor.PDFConversionError("conversion_failed", -9)
            return paper, "fulltext", "fixture"

        service = self.service(fetch)
        self.assertEqual(service.fulltext_step(self.conn), len(self.ids) - 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM papers WHERE status='quality_checked'").fetchone()[0], 1)
        self.assertFalse((self.root / (self.ids[0] + ".tex")).exists())


if __name__ == "__main__":
    unittest.main()
