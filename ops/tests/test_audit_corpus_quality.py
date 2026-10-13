import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ops import audit_corpus_quality as census
from pipeline import document_cache, extractor


class CorpusQualityCensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.data = base / "data"
        self.data.mkdir()
        self.chunks = self.data / "chunk_cache"
        self.sources = self.data / "fulltext_cache"
        self.chunks.mkdir()
        self.sources.mkdir()
        self.report = base / "audit" / "quality.sqlite"
        conn = sqlite3.connect(self.data / "state.db")
        conn.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT,fulltext_source TEXT,"
                     "parser_mode TEXT,parser_revision INTEGER,extractor_version INTEGER,parse_status TEXT,updated_at TEXT)")
        conn.execute("CREATE TABLE paper_publications(arxiv_id TEXT,revision TEXT,kind TEXT,state TEXT)")
        conn.executemany("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?)", [
            ("acl:paper", "done", "acl-pdf", "markdown", 4, 5, "ok", "today"),
            ("pmlr:broken", "done", "oa-mirror-pdf", "markdown", 4, 5, "ok", "today"),
            ("missing:source", "done", None, None, None, None, None, "today"),
            ("abstract:fallback", "done", "abstract-fallback", "markdown", 4, 5, "ok", "today"),
            ("latex:fallback", "done", "latex", "pylatexenc", 4, 5, "ok", "today"),
            ("queued:paper", "chunked", "latex", "pylatexenc", 4, 5, "ok", "today"),
        ])
        conn.execute("INSERT INTO paper_publications VALUES (?,?,?,?)",
                     ("acl:paper", "rev-1", "snapshot", "published"))
        conn.commit()
        conn.close()

    @staticmethod
    def _write_doc(data_dir, paper_id, text, *, old_spec_labels=False):
        safe = document_cache.safe_id(paper_id)
        source = text.encode("utf-8")
        (data_dir / "fulltext_cache" / f"{safe}.tex").write_bytes(source)
        if old_spec_labels:
            chunks, _ = extractor.build_chunks_markdown(text)
        else:
            chunks, _ = extractor.build_chunks_markdown(text, document_kind="paper")
        (data_dir / "chunk_cache" / f"{safe}.json").write_text(
            json.dumps(chunks, ensure_ascii=False), encoding="utf-8")

    def test_census_classifies_all_done_and_flags_label_only_candidate(self):
        paper = ("# Paper Title\nAuthor One\n## Abstract\nSummary.\n"
                 "## **4. Methodology**\nA method.\n")
        self._write_doc(self.data, "acl:paper", paper, old_spec_labels=True)
        broken_id = document_cache.safe_id("pmlr:broken")
        (self.chunks / f"{broken_id}.json").write_text("{bad json", encoding="utf-8")
        (self.sources / f"{broken_id}.tex").write_text("Broken \ufffd source", encoding="utf-8")
        latex_dir = self.data / "latex_cache" / "latex_fallback"
        latex_dir.mkdir(parents=True)
        (latex_dir / "source.tex").write_text("\\section{Abstract}Fallback source", encoding="utf-8")

        summary = census.run_census(self.data, self.report)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["reported_papers"], 5)
        self.assertEqual(summary["candidate_statuses"]["candidate"], 1)
        self.assertEqual(summary["cache_states"]["missing"], 3)
        self.assertEqual(summary["cache_states"]["malformed_json"], 1)
        self.assertEqual(summary["u_fffd_source"], 1)

        conn = sqlite3.connect(self.report)
        candidate = conn.execute("SELECT candidate_status,exact_text_ids,label_change_count,"
                                 "candidate_changes_json,candidate_label_map_json,source_class,publication_revision_count "
                                 "FROM papers WHERE paper_id='acl:paper'").fetchone()
        missing = conn.execute("SELECT source_class,source_state FROM papers WHERE paper_id='missing:source'").fetchone()
        abstract = conn.execute("SELECT source_class FROM papers WHERE paper_id='abstract:fallback'").fetchone()
        latex_fallback = conn.execute("SELECT source_location,source_state FROM papers WHERE paper_id='latex:fallback'").fetchone()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
        conn.close()
        self.assertEqual(candidate[:3], ("candidate", 1, 1))
        self.assertEqual(candidate[5:], ("fulltext", 1))
        self.assertIn('"section_type":"method"', candidate[3])
        self.assertNotIn("A method", candidate[3])
        candidate_labels = json.loads(candidate[4])
        self.assertEqual(len(candidate_labels), 3)
        self.assertEqual(candidate_labels[-1]["section_type"], "method")
        self.assertEqual(missing, ("unknown", "missing"))
        self.assertEqual(abstract, ("abstract_only",))
        self.assertEqual(latex_fallback, ("latex_cache", "present"))
        self.assertNotIn("text", columns)

    def test_duplicate_indices_block_candidate_and_metrics_are_retained(self):
        text = "# Paper\nTitle text.\n## Abstract\nText \ufffd.\n"
        self._write_doc(self.data, "acl:paper", text)
        path = self.chunks / "acl_paper.json"
        chunks = json.loads(path.read_text(encoding="utf-8"))
        chunks[0]["chunk_index"] = chunks[1]["chunk_index"]
        path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")

        census.run_census(self.data, self.report)
        conn = sqlite3.connect(self.report)
        row = conn.execute("SELECT candidate_status,duplicate_index_count,noncontiguous_index,"
                           "cache_replacement_count FROM papers WHERE paper_id='acl:paper'").fetchone()
        conn.close()
        self.assertEqual(row, ("identity_or_text_changed", 1, 1, 1))

    def test_resumes_unchanged_inputs_and_rechecks_changed_source(self):
        self._write_doc(self.data, "acl:paper", "# Paper\nTitle.\n## Abstract\nText.\n")
        first = census.run_census(self.data, self.report)
        second = census.run_census(self.data, self.report)
        self.assertEqual(first["scanned_this_run"], 5)
        self.assertEqual(second["scanned_this_run"], 0)
        self.assertEqual(second["reused_this_run"], 5)
        source = self.sources / "acl_paper.tex"
        source.write_text("# Paper\n## Abstract\nChanged text.\n", encoding="utf-8")
        third = census.run_census(self.data, self.report)
        self.assertEqual(third["scanned_this_run"], 1)


if __name__ == "__main__":
    unittest.main()
