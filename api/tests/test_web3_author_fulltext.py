import builtins
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "ops" / "web3_author_fulltext.py"
_SPEC = importlib.util.spec_from_file_location("web3_author_fulltext", _SCRIPT)
wf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wf)

TITLE = "Paper Title With Enough Meaningful Words"
TEXT = TITLE + "\n\n" + "substantive paper text " * 400
PDF = b"%PDF-1.4 sample"


class Response:
    status_code = 200
    headers = {}
    text = "User-agent: *\nDisallow: /private"
    def iter_content(self, _):
        yield PDF
    def close(self): pass


class Session:
    def __init__(self, url="https://safe.example/p.pdf"):
        self.url, self.calls = url, []
    def get(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith("robots.txt"):
            return Response()
        return Response()


class AuthorFulltext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.db = self.root / "state.db"
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY,title TEXT,year INTEGER,layers TEXT,status TEXT,abstract TEXT,niche_score REAL,matched_terms TEXT,fulltext_source TEXT,source_url TEXT,twin_of TEXT,updated_at TEXT)")
        c.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("iacr:2020/1", TITLE, 2020, "web3", "done", "abstract", 7, "term", "iacr-abstract", None, None, "now"))
        c.commit(); c.close()
        self.item = {"originaliacr_id": "2020/1", "title": TITLE, "copy_url": "https://safe.example/p.pdf", "license": "unknown", "identity_source": "https://identity.example/", "copy_publication_year": 2020, "copy_version": "publisher final version"}
        self.catalog = [dict(self.item, id="iacr:2020/1")]

    def artifacts(self, corrupt=False):
        d = self.root / "artifacts"; d.mkdir(exist_ok=True)
        stem = wf.safe_id("iacr:2020/1")
        (d / (stem + ".pdf")).write_bytes(PDF)
        (d / (stem + ".md")).write_text(("Wrong Title\n" if corrupt else TEXT), encoding="utf-8")
        m = {"url": self.item["copy_url"], "original_id": "iacr:2020/1", "paper_title": TITLE, "length": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest(), "text_sha256": hashlib.sha256(("Wrong Title\n" if corrupt else TEXT).encode()).hexdigest(), "pdf_magic": "%PDF", "first_page_title_valid": True, "first_page_author_valid": True, "license": "unknown"}
        (d / (stem + ".json")).write_text(json.dumps(m), encoding="utf-8")
        return str(d)

    def test_native_parser_processes_whole_document_and_closes(self):
        doc = Mock()
        mupdf = SimpleNamespace(open=Mock(return_value=doc))
        markdown = SimpleNamespace(use_layout=Mock(), to_markdown=Mock(return_value=TEXT))
        with patch.dict("sys.modules", {"pymupdf": mupdf, "pymupdf4llm": markdown}):
            self.assertEqual(wf.default_to_text(PDF), TEXT)
            markdown.use_layout.assert_called_once_with(False)
            mupdf.open.assert_called_once_with(stream=PDF, filetype="pdf")
            markdown.to_markdown.assert_called_once_with(doc, show_progress=False, write_images=False)
            doc.close.assert_called_once_with()
            del markdown.use_layout
            markdown.to_markdown.side_effect = RuntimeError("parser failed")
            with self.assertRaises(RuntimeError): wf.default_to_text(PDF)
            self.assertEqual(doc.close.call_count, 2)

    def test_module_import_and_audit_do_not_import_parser_or_onnx(self):
        original_import = builtins.__import__
        forbidden = {"pymupdf", "pymupdf4llm", "whitepapers", "service", "onnx", "onnxruntime", "pymupdf_layout"}
        def guard(name, *args, **kwargs):
            if name.split(".")[0] in forbidden:
                raise AssertionError("unexpected heavy import: " + name)
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guard):
            module = importlib.util.module_from_spec(_SPEC)
            _SPEC.loader.exec_module(module)
            conn = module.open_db(str(self.db))
            try: self.assertEqual(module.audit(conn, self.catalog)["plan"][0]["action"], "stage")
            finally: conn.close()

    def test_download_injected_parser_manifest_and_flushed_progress(self):
        fetcher, parser = Mock(), Mock(return_value=TEXT)
        fetcher.pdf.return_value = PDF
        with patch("builtins.print") as progress:
            result = wf.download(self.catalog, str(self.root / "staged"), 3, fetcher, parser)
        self.assertEqual(result[0]["action"], "staged")
        parser.assert_called_once_with(PDF)
        manifest = json.loads((self.root / "staged" / "iacr_2020_1.json").read_text())
        self.assertEqual(manifest["parser"], "pymupdf4llm-native-markdown")
        self.assertIs(manifest["exact_latex"], False)
        self.assertEqual((manifest["identity_source"], manifest["copy_publication_year"], manifest["copy_version"]), (self.item["identity_source"], 2020, "publisher final version"))
        self.assertTrue(progress.call_args.kwargs["flush"])
        self.assertIs(progress.call_args.kwargs["file"], wf.sys.stderr)
        self.assertEqual(json.loads(progress.call_args.args[0]), result[0])

    def test_parser_error_continues_without_plaintext_fallback(self):
        fetcher, parser = Mock(), Mock(side_effect=[RuntimeError("private error details"), TEXT])
        fetcher.pdf.return_value = PDF
        catalog = self.catalog + [dict(self.catalog[0], id="iacr:2020/2")]
        before = self.db.read_bytes()
        with patch("builtins.print") as progress:
            result = wf.download(catalog, str(self.root / "staged"), 3, fetcher, parser)
        self.assertEqual(result[0], {"id": "iacr:2020/1", "action": "error", "error": "RuntimeError"})
        self.assertEqual(result[1]["action"], "staged")
        self.assertEqual(progress.call_count, 2)
        self.assertFalse((self.root / "staged" / "iacr_2020_1.md").exists())
        self.assertEqual(self.db.read_bytes(), before)

    def test_fetcher_obeys_robots_and_blocked_hosts(self):
        session = Session()
        fetcher = wf.io_.Fetcher(session, sleep=lambda _: None)
        with self.assertRaises(ValueError): fetcher.pdf("https://safe.example/private/p.pdf")
        self.assertFalse(any("iacr.org" in call for call in session.calls))
        with self.assertRaises(ValueError): fetcher.pdf("https://eprint.iacr.org/p.pdf")

    def test_audit_ro_and_apply_rejects_bad_text_without_db_write(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        conn = wf.open_db(str(self.db))
        try:
            wf.audit(conn, self.catalog)
            with self.assertRaises(sqlite3.OperationalError): conn.execute("DELETE FROM papers")
        finally: conn.close()
        conn = wf.open_db(str(self.db), True)
        try: result = wf.apply_artifacts(conn, self.catalog, self.artifacts(True), str(self.root), 3)
        finally: conn.close()
        self.assertEqual(result[0]["action"], "reject_artifact")
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_insert_is_new_only_and_idempotent(self):
        d = self.artifacts()
        for _ in range(2):
            conn = wf.open_db(str(self.db), True)
            try: result = wf.apply_artifacts(conn, self.catalog, d, str(self.root), 3)
            finally: conn.close()
        self.assertEqual(result[0]["action"], "skip_known")
        c = sqlite3.connect(self.db)
        rows = c.execute("SELECT arxiv_id,title,status,fulltext_source,twin_of FROM papers ORDER BY arxiv_id").fetchall()
        c.close()
        self.assertEqual(len(rows), 2)
        added = next(r for r in rows if r[0].startswith("author:"))
        self.assertEqual((added[1], added[2], added[3], added[4]), (TITLE, "fulltext_fetched", "oa-mirror-pdf", "iacr:2020/1"))
        expected_cache = self.root / "fulltext_cache" / (wf.safe_id(added[0]) + ".tex")
        self.assertTrue(expected_cache.is_file())
        self.assertEqual(expected_cache.read_text(encoding="utf-8"), TEXT)

    def test_declared_title_alias_is_exact_only(self):
        item = dict(self.item, original_title_aliases=["Recursive Proof Composition without a Trusted Setup"])
        self.assertTrue(wf.catalog_title_matches("Recursive Proof Composition without a Trusted Setup", item))
        self.assertFalse(wf.catalog_title_matches("Recursive Proof Composition without Trusted Setup", item))

    def test_matching_or_mismatching_orphan_cache(self):
        d = self.artifacts()
        conn = wf.open_db(str(self.db), True)
        try: first = wf.apply_artifacts(conn, self.catalog, d, str(self.root), 3)
        finally: conn.close()
        new_id = first[0]["new_id"]
        conn = sqlite3.connect(self.db); conn.execute("DELETE FROM papers WHERE arxiv_id=?", (new_id,)); conn.commit(); conn.close()
        conn = wf.open_db(str(self.db), True)
        try: retry = wf.apply_artifacts(conn, self.catalog, d, str(self.root), 3)
        finally: conn.close()
        self.assertEqual(retry[0]["action"], "inserted")
        conn = sqlite3.connect(self.db); conn.execute("DELETE FROM papers WHERE arxiv_id=?", (new_id,)); conn.commit(); conn.close()
        cache = self.root / "fulltext_cache" / (wf.safe_id(new_id) + ".tex")
        cache.write_text("unrelated cached text", encoding="utf-8")
        conn = wf.open_db(str(self.db), True)
        try: refused = wf.apply_artifacts(conn, self.catalog, d, str(self.root), 3)
        finally: conn.close()
        self.assertEqual(refused[0]["action"], "skip_cache_mismatch")
        conn = sqlite3.connect(self.db)
        self.assertIsNone(conn.execute("SELECT 1 FROM papers WHERE arxiv_id=?", (new_id,)).fetchone())
        self.assertEqual(conn.execute("SELECT title,status,fulltext_source FROM papers WHERE arxiv_id='iacr:2020/1'").fetchone(), (TITLE, "done", "iacr-abstract"))
        conn.close()

    def standalone_item(self, original=None):
        item = {"curated_standalone": True, "doi": "10.4230/LIPIcs.ICALP.2018.14",
                "title": "Polynomial Commitments for Efficient Proofs", "authors": ["Alice Smith"],
                "year": 2018, "identity_source": "https://drops.example/record",
                "copy_url": "https://drops.example/paper.pdf"}
        if original:
            item["originaliacr_id"] = original
        return wf.load_catalog_from_items([item])[0]

    def test_standalone_approval_requires_curated_verified_doi_metadata(self):
        base = {"curated_standalone": True, "doi": "10.4230/LIPIcs.ICALP.2018.14",
                "title": "Polynomial Commitments for Efficient Proofs", "authors": ["Alice Smith"],
                "year": 2018, "identity_source": "https://drops.example/record",
                "copy_url": "https://drops.example/paper.pdf"}
        items = wf.load_catalog_from_items([base, dict(base, curated_standalone=False), dict(base, doi="not-doi"), dict(base, authors=[])])
        self.assertEqual(items[0]["id"], "doi:10.4230/lipics.icalp.2018.14")
        self.assertTrue(items[0]["standalone"])
        self.assertTrue(all(item["id"] is None and not item["standalone"] for item in items[1:]))

    def test_standalone_doi_insert_new_only_and_no_fake_twin(self):
        item = self.standalone_item()
        catalog = [item]
        stem = wf.safe_id(item["id"])
        directory = self.root / "artifacts"; directory.mkdir()
        (directory / (stem + ".pdf")).write_bytes(PDF)
        (directory / (stem + ".md")).write_text("Polynomial Commitments for Efficient Proofs\nAlice Smith\n" + "substantive paper text " * 400)
        text = (directory / (stem + ".md")).read_text()
        manifest = {"url": item["copy_url"], "original_id": item["id"], "paper_title": item["title"],
                    "length": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest(),
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "pdf_magic": "%PDF",
                    "first_page_title_valid": True, "first_page_author_valid": True, "standalone": True,
                    "doi": item["doi"], "year": item["year"]}
        (directory / (stem + ".json")).write_text(json.dumps(manifest))
        conn = wf.open_db(str(self.db), True)
        try: result = wf.apply_artifacts(conn, catalog, str(directory), str(self.root), 3)
        finally: conn.close()
        self.assertEqual(result[0]["action"], "inserted")
        self.assertEqual(result[0]["new_id"], "author:doi:10_4230_lipics_icalp_2018_14:" + manifest["sha256"][:12])
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT title,year,layers,status,abstract,niche_score,matched_terms,fulltext_source,twin_of FROM papers WHERE arxiv_id=?", (result[0]["new_id"],)).fetchone()
        self.assertEqual(row, (item["title"], 2018, "web3", "fulltext_fetched", None, None, "[]", "oa-mirror-pdf", None))
        conn.close()
        conn = wf.open_db(str(self.db), True)
        try: again = wf.apply_artifacts(conn, catalog, str(directory), str(self.root), 3)
        finally: conn.close()
        self.assertEqual(again[0]["action"], "skip_known")

    def test_standalone_existing_iacr_id_without_row_uses_identifier_without_twin(self):
        item = self.standalone_item("2010/009")
        self.assertEqual(item["id"], "iacr:2010/009")
        self.assertTrue(item["standalone"])

    def test_actual_catalog_kzg_without_doi_is_standalone(self):
        items = wf.load_catalog(wf.CATALOG)
        kzg = next(item for item in items if item.get("originaliacr_id") == "2010/009")
        self.assertTrue(kzg["standalone"])
        self.assertEqual(kzg["id"], "iacr:2010/009")
        self.assertNotIn("doi", kzg)

    def test_audit_doi_standalone_reports_exact_title_duplicates(self):
        item = self.standalone_item()
        item["title"] = TITLE
        conn = wf.open_db(str(self.db))
        try: result = wf.audit(conn, [item])
        finally: conn.close()
        self.assertEqual(result["plan"][0]["action"], "standalone_candidate")
        self.assertEqual(result["plan"][0]["exact_title_matches"], ["iacr:2020/1"])

    def test_standalone_manifest_identity_mismatch_is_rejected(self):
        item = self.standalone_item()
        directory = self.root / "bad-artifacts"; directory.mkdir()
        stem = wf.safe_id(item["id"])
        text = "Polynomial Commitments for Efficient Proofs\nAlice Smith\n" + "substantive paper text " * 400
        (directory / (stem + ".pdf")).write_bytes(PDF)
        (directory / (stem + ".md")).write_text(text)
        (directory / (stem + ".json")).write_text(json.dumps({
            "url": item["copy_url"], "original_id": item["id"], "paper_title": item["title"],
            "length": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest(),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "pdf_magic": "%PDF",
            "first_page_title_valid": True, "first_page_author_valid": True, "standalone": True,
            "doi": "10.9999/another.paper", "year": item["year"]}))
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        conn = wf.open_db(str(self.db), True)
        try: result = wf.apply_artifacts(conn, [item], str(directory), str(self.root), 3)
        finally: conn.close()
        self.assertEqual(result[0]["action"], "reject_artifact")
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_nfkc_validation_does_not_change_preserved_text(self):
        ligature_title = "Efficient ﬁeld Commitments for Proofs"
        self.assertTrue(wf.title_opens("Efficient field Commitments for Proofs\nAlice Smith", ligature_title))
        raw = "math: x ≠ y; ﬁ"
        self.assertEqual(raw, "math: x ≠ y; ﬁ")


if __name__ == "__main__":
    unittest.main()
