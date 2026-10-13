from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from api import main
from api.evidence import provenance
from pipeline import document_cache as dc


class CanonicalSectionReadTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.pid = "fixture"
        self.old = {"arxiv_id": self.pid, "title": "Paper", "chunk_index": 0,
                    "section_title": "Method", "section_type": "method", "text": "before\n\nafter"}
        self.payloads = [self.old, dict(self.old, chunk_index=1, text="middle")]
        self.cache = json.dumps(self.payloads).encode()
        cache_path, self.target = dc.document_paths(self.root, self.pid)
        cache_path.parent.mkdir()
        cache_path.write_bytes(self.cache)
        self.cache_path = cache_path
        self.state = sqlite3.connect(self.root / "state.db")
        self.stack.callback(self.state.close)
        self.state.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT)")
        self.state.execute("INSERT INTO papers VALUES (?, 'done')", (self.pid,))
        self.state.commit()
        self.manifest = dc.make_section_manifest(self.pid, [dict(self.old, text="before\n\nmiddle\n\nafter")],
                                                 self.cache, b"source", "pylatexenc", "rev")
        dc.publish_section_manifest(self.target, self.manifest)
        self.stack.enter_context(patch.dict(main.os.environ, {"DTOX_DATA_DIR": str(self.root)}))
        self.stack.enter_context(patch.object(main, "STATE_DB_PATH", str(self.root / "state.db")))
        self.stack.enter_context(patch.object(main, "_ro_conn", return_value=self.state))
        self.stack.enter_context(patch.object(main, "auth_and_limit", return_value=None))
        self.stack.enter_context(patch.object(main, "qdrant_scroll_by_arxiv",
                                            side_effect=lambda *a, **k: [{"payload": p} for p in self.payloads]))
        self.stack.enter_context(patch.object(main, "_evidence_rows", return_value={
            self.pid: {"fulltext_source": "latex", "source_url": "https://example.test/paper"}}))
        self.stack.enter_context(patch.object(main.requests, "get", side_effect=AssertionError("offline")))
        self.stack.enter_context(patch.object(main.requests, "post", side_effect=AssertionError("offline")))

    def read(self, **kwargs):
        return main.paper_section(self.pid, x_api_key="fixture", **kwargs)

    def test_middle_gap_within_old_chunk_reads_canonical_source(self):
        result = self.read(title="Method")
        self.assertEqual(result["text"], "before\n\nmiddle\n\nafter")
        self.assertTrue(result["section_order"]["verified"])
        self.assertEqual(result["section_order"]["scope"], "parsed_source")
        self.assertEqual(result["section_order"]["cache_sha256"], dc.sha256(self.cache))
        self.assertEqual(self.cache_path.read_bytes(), self.cache)

    def test_provenance_and_page_offsets_cover_returned_text(self):
        text = "before middle after " * 70
        self.manifest["sections"][0]["text"] = text
        dc.publish_section_manifest(self.target, self.manifest)
        result = self.read(section_type="method", offset=11, max_chars=500)
        self.assertEqual(result["text"], text[11:511])
        self.assertEqual(result["next_offset"], 511)
        self.assertEqual(result["total_chars"], len(text))
        self.assertEqual(result["evidence"]["offset"], 11)
        self.assertEqual(result["evidence"]["fragment_scope"], "returned_section_page")
        fragment = {"section_title": None, "section_type": "method", "text": result["text"].strip()}
        self.assertEqual(result["evidence"]["fragment_hash"],
                         provenance(self.pid, fragment)["fragment_hash"])

    def test_title_and_type_are_exact(self):
        for request in ({"title": "method"}, {"section_type": "Method"}):
            with self.subTest(request=request), self.assertRaises(HTTPException) as caught:
                self.read(**request)
            self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.read(title="Method", section_type="other")["text"],
                         "before\n\nmiddle\n\nafter")

    def assert_degraded(self, reason=None):
        result = self.read(title="Method")
        self.assertEqual(result["text"], "before\n\nafter\n\nmiddle")
        self.assertFalse(result["section_order"]["verified"])
        self.assertFalse(result["evidence"]["order_verified"])
        if reason:
            self.assertEqual(result["section_order"]["reason"], reason)

    def test_not_done_falls_back_without_source_order_claim(self):
        self.state.execute("UPDATE papers SET status='fulltext_fetched'")
        self.state.commit()
        self.assert_degraded("paper_not_done")

    def test_changed_cache_falls_back(self):
        self.cache_path.write_bytes(self.cache + b" ")
        self.assert_degraded("canonical_manifest_unavailable_or_invalid")

    def test_explicit_physical_cache_directories_are_used(self):
        physical = self.root / "physical"
        chunks, sections = physical / "chunks", physical / "sections"
        chunks.mkdir(parents=True)
        sections.mkdir()
        (chunks / self.cache_path.name).write_bytes(self.cache)
        (sections / self.target.name).write_bytes(self.target.read_bytes())
        self.target.unlink()
        self.cache_path.unlink()
        with patch.dict(main.os.environ, {"DTOX_CHUNK_CACHE_DIR": str(chunks),
                                         "DTOX_SECTION_CACHE_DIR": str(sections)}):
            result = self.read(title="Method")
        self.assertTrue(result["section_order"]["verified"])
        self.assertEqual(result["text"], "before\n\nmiddle\n\nafter")

    def test_absent_malformed_and_symlinked_manifest_degrade(self):
        self.target.unlink()
        self.assert_degraded()
        self.target.write_bytes(b"not JSON")
        self.assert_degraded()
        with patch.object(dc.Path, "is_symlink", return_value=True):
            self.assert_degraded()

    def test_partial_view_type_request_requires_all_known_sections(self):
        self.manifest["coverage"] = "examined_sections"
        dc.publish_section_manifest(self.target, self.manifest)
        self.payloads.append(dict(self.old, section_title="Other method", text="unexamined"))
        self.assertTrue(self.read(title="Method")["section_order"]["verified"])
        result = self.read(section_type="method")
        self.assertFalse(result["section_order"]["verified"])
        self.assertEqual(result["section_order"]["reason"], "section_not_fully_examined")

    def test_auth_runs_before_file_reads(self):
        with patch.object(main, "auth_and_limit", side_effect=HTTPException(401)), \
                patch.object(dc, "load_section_manifest") as load:
            with self.assertRaises(HTTPException):
                self.read(title="Method")
            load.assert_not_called()
