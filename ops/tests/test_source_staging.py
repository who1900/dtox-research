"""Offline guarantees for non-destructive source staging entry points."""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

if "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fcntl")
        stub.flock = lambda *args, **kwargs: None
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        sys.modules["fcntl"] = stub

ROOT = pathlib.Path(__file__).resolve().parents[2]
for directory in (ROOT / "pipeline", ROOT / "ops"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from pipeline import service
import iacr_oa_fulltext as iacr
import refresh_moved_eips as eips
import reprocess_lost_body as recovery


BODY = "Fake Paper Title On Something\n\n" + "validated text " * 350
ERC = ("---\ntitle: ERC Example\nstatus: Draft\ncreated: 2021-01-01\n---\n\n"
       "## Abstract\n" + "An ERC specification. " * 30)


class SourceStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.db = self.root / "state.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        service.init_db(self.conn)
        self.conn.execute("INSERT INTO papers(arxiv_id,title,layers,status,fulltext_source,abstract) "
                          "VALUES('iacr:2020/1','Fake Paper Title On Something','web3','done',"
                          "'iacr-abstract','old abstract')")
        self.conn.execute("INSERT INTO papers(arxiv_id,title,layers,status,fulltext_source) "
                          "VALUES('eip:4337','old ERC','web3','done','spec-markdown')")
        self.conn.commit()
        self.cache = self.root / "fulltext_cache"
        self.chunks = self.root / "chunk_cache"
        self.latex = self.root / "latex_cache" / "eip_4337" / "source.tex"
        for path, data in ((self.cache / "iacr_2020_1.tex", b"old IACR source"),
                           (self.chunks / "iacr_2020_1.json", b"old chunks"),
                           (self.cache / "eip_4337.tex", b"old EIP source"),
                           (self.chunks / "eip_4337.json", b"old EIP chunks"),
                           (self.latex, b"old latex source")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.old_files = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and p != self.db}
        self.old_rows = {pid: dict(self.conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (pid,)).fetchone())
                         for pid in ("iacr:2020/1", "eip:4337")}

    def tearDown(self):
        self.conn.close()

    def assert_published_unchanged(self):
        for path, data in self.old_files.items():
            self.assertEqual(path.read_bytes(), data)
        for pid, row in self.old_rows.items():
            self.assertEqual(dict(self.conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (pid,)).fetchone()), row)

    def test_iacr_stages_after_title_check_without_mutating_published_content(self):
        args = iacr.parse_args(["--data-dir", str(self.root), "--skip-fts"])
        with patch.object(iacr, "stage_source", return_value={"status": "staged", "paper_id": "iacr:2020/1",
                                                                "revision": "r1", "job_kind": "source",
                                                                "artifact_path": "publication_cache/x"}) as stage:
            outcome, _ = iacr.apply_pdf(self.conn, MagicMock(), MagicMock(pdf=MagicMock(return_value=b"%PDF")),
                args, {"id": "iacr:2020/1", "title": "Fake Paper Title On Something", "copies": [
                    {"kind": "pdf", "pdf": True, "url": "https://mirror.test/p.pdf", "host": "mirror.test"}]},
                [], lambda _: BODY, log=lambda *_: None)
        self.assertEqual(outcome, "staged")
        self.assertEqual(stage.call_args.kwargs["conn"], self.conn)
        self.assertEqual(stage.call_args.kwargs["data_dir"], str(self.root))
        self.assertEqual(stage.call_args.kwargs["paper_id"], "iacr:2020/1")
        self.assertEqual(stage.call_args.kwargs["source_bytes"], BODY.encode("utf-8"))
        self.assert_published_unchanged()

    def test_failed_stage_leaves_old_content_and_state_intact(self):
        args = iacr.parse_args(["--data-dir", str(self.root)])
        with patch.object(iacr, "stage_source", side_effect=OSError("stage failed")):
            with self.assertRaises(OSError):
                iacr.apply_pdf(self.conn, MagicMock(), MagicMock(pdf=MagicMock(return_value=b"%PDF")),
                    args, {"id": "iacr:2020/1", "title": "Fake Paper Title On Something", "copies": [
                        {"kind": "pdf", "pdf": True, "url": "https://mirror.test/p.pdf", "host": "mirror.test"}]},
                    [], lambda _: BODY, log=lambda *_: None)
        self.assert_published_unchanged()

    def test_real_publication_stage_is_immutable_and_idempotent(self):
        first = iacr.stage_source(conn=self.conn, data_dir=str(self.root), paper_id="iacr:2020/1",
                                  source_bytes=BODY.encode("utf-8"), metadata={"source": "test"})
        again = iacr.stage_source(conn=self.conn, data_dir=str(self.root), paper_id="iacr:2020/1",
                                  source_bytes=BODY.encode("utf-8"), metadata={"source": "test"})
        self.assertEqual(first, again)
        artifact = pathlib.Path(first["artifact_path"])
        self.assertTrue(artifact.is_file())
        self.assertEqual(json.loads(artifact.read_text(encoding="utf-8"))["source"], BODY)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_publications WHERE arxiv_id=?",
                                           ("iacr:2020/1",)).fetchone()[0], 1)
        self.assert_published_unchanged()

    def test_eip_refresh_only_stages_and_reports_staged(self):
        args = eips.parse_args(["--data-dir", str(self.root)])
        with patch.object(eips, "stage_source", return_value={"status": "staged"}) as stage, \
                patch.object(recovery, "qdrant_delete") as qdelete, \
                patch.object(recovery, "fts_delete") as fdelete:
            outcome = eips.refresh_one(self.conn, MagicMock(), args, "eip:4337", ERC, [])
        self.assertEqual(outcome, "staged")
        self.assertEqual(stage.call_args.kwargs["source_bytes"], ERC.encode("utf-8"))
        qdelete.assert_not_called()
        fdelete.assert_not_called()
        self.assert_published_unchanged()

    def test_legacy_destructive_helpers_fail_closed_before_side_effects(self):
        session = MagicMock()
        for fn, args in ((recovery.qdrant_delete, (session, "http://qdrant", "c", "paper")),
                         (recovery.fts_delete, (str(self.root / "fts.db"), [1])),
                         (recovery.remove_caches, (str(self.root), "iacr:2020/1"))):
            with self.subTest(helper=fn.__name__), self.assertRaises(recovery.RetiredDestructiveHelper):
                fn(*args)
        session.post.assert_not_called()
        self.assert_published_unchanged()

    def test_arxiv_twin_insert_records_first_known_atomically(self):
        iacr.queue_arxiv(self.conn, {"id": "iacr:2020/1", "arxiv_id": "2001.12345",
                                    "title": "Twin", "year": 2020})
        row = self.conn.execute("SELECT first_known, completed FROM completion_history WHERE arxiv_id=?",
                                ("2001.12345",)).fetchone()
        self.assertEqual(tuple(row), (1, 0))
        iacr.queue_arxiv(self.conn, {"id": "iacr:2020/1", "arxiv_id": "2001.12345",
                                    "title": "Twin", "year": 2020})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM completion_history WHERE arxiv_id=?",
                                           ("2001.12345",)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
