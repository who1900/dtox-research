import hashlib
import gzip
import io
import tempfile
import tarfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from ops.corpus_quality import fresh_sources


class _Response:
    status_code = 200
    url = "https://aclanthology.org/2023.evalita-1.3.pdf"

    def __init__(self, content):
        self.content = content
        self.closed = False

    def iter_content(self, size):
        yield self.content[:5]
        yield self.content[5:]

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response = response
        self.call = None

    def get(self, url, **kwargs):
        self.call = (url, kwargs)
        return self.response


class FreshSourceTests(unittest.TestCase):
    def test_acl_pdf_is_bound_to_id_and_raw_hash(self):
        raw = b"%PDF-1.7\nsmall fixture"
        response = _Response(raw)
        session = _Session(response)
        artifact = fresh_sources.fetch_fresh_artifact(
            "acl:2023.evalita-1.3", original_kind="PDF",
            edition="acl:2023.evalita-1.3", session=session,
            retrieved_at="2026-10-14T00:00:00Z")

        self.assertEqual(session.call[0], response.url)
        self.assertTrue(session.call[1]["stream"])
        self.assertTrue(response.closed)
        self.assertEqual(artifact.raw_bytes, raw)
        self.assertEqual(artifact.raw_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(artifact.retrieval_url, response.url)
        self.assertEqual(artifact.edition, "sha256:" + artifact.raw_sha256)

    def test_html_must_match_original_store_raw_html_contract(self):
        response = _Response(b"<!-- mirror wrapper --><article>Paper</article>")
        response.url = "https://arxiv.org/html/2408.11826v1"
        with self.assertRaisesRegex(fresh_sources.FreshSourceError, "not raw HTML"):
            fresh_sources.fetch_fresh_artifact(
                "2408.11826", original_kind="HTML", edition="v1",
                session=_Session(response))

    def test_html_parser_limit_is_per_source_so_route_fallback_can_continue(self):
        from pipeline.structured_html import HTMLLimitError
        raw = b"<html><body>arXiv:2408.11826v1<article><h1>Paper</h1></article></body></html>"
        artifact = fresh_sources.FreshArtifact(
            canonical_id="2408.11826", source_url="https://arxiv.org/html/2408.11826v1",
            request_url="https://arxiv.org/html/2408.11826v1",
            retrieval_url="https://arxiv.org/html/2408.11826v1", original_kind="HTML",
            edition="v1", retrieved_at="2026-10-14T00:00:00Z",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw), raw_bytes=raw)
        with patch("pipeline.structured_html.extract_html",
                   side_effect=HTMLLimitError("HTML node cap exceeded")):
            with self.assertRaisesRegex(fresh_sources.FreshSourceError, "parser limits"):
                fresh_sources.parse_fresh_artifact(artifact)

    def test_generic_pdf_uses_exact_url_and_hash_edition(self):
        raw = b"%PDF-1.7\nsource fixture"
        response = _Response(raw)
        response.url = "https://open.example/paper.pdf"
        artifact = fresh_sources.fetch_fresh_artifact(
            "openalex:W123", original_kind="PDF", edition="sha256:pending",
            source_url="https://open.example/paper.pdf", session=_Session(response))
        self.assertEqual(artifact.source_url, response.url)
        self.assertEqual(artifact.edition, "sha256:" + artifact.raw_sha256)

    def test_arxiv_resolver_binds_explicit_current_version_and_title(self):
        atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                b'<id>http://arxiv.org/abs/2408.11826v1</id>'
                b'<title>Research title</title></entry></feed>')
        response = _Response(atom)
        response.url = "https://export.arxiv.org/api/query?id_list=2408.11826"
        resolved = fresh_sources.resolve_arxiv_version(
            "2408.11826", session=_Session(response))
        self.assertEqual(resolved["fresh_version"], "v1")
        self.assertEqual(resolved["title"], "Research title")

    def test_html_alttext_projects_without_placeholder_text(self):
        raw = (b'<html><body>arXiv:2408.11826v1<article><h1>Paper</h1><p>Claim '
               b'<math alttext="x squared"><mi>x</mi></math> is tested.</p>'
               b'<p>Display: <math display="block" alttext="sum of x"><mi>x</mi></math></p>'
               b'<h2>Conclusion</h2><p>Results support the claim.</p>'
               b'</article></body></html>')
        artifact = fresh_sources.FreshArtifact(
            canonical_id="2408.11826", source_url="https://arxiv.org/html/2408.11826v1",
            request_url="https://arxiv.org/html/2408.11826v1",
            retrieval_url="https://arxiv.org/html/2408.11826v1", original_kind="HTML",
            edition="v1", retrieved_at="2026-10-14T00:00:00Z",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw), raw_bytes=raw)

        text, diagnostics = fresh_sources.parse_fresh_artifact(artifact)

        self.assertIn("x squared", text)
        self.assertIn("$x squared$", text)
        self.assertIn("$$\nsum of x\n$$", text)
        self.assertNotIn("UNSUPPORTED MATH", text)
        self.assertEqual(diagnostics["math_from_alttext"], ["math-1", "math-2"])
        self.assertEqual(diagnostics["replacement_characters"], 0)

    def test_html_projection_uses_article_title_and_preserves_source_table(self):
        raw = (b'<html><body>arXiv:2408.11826v1<h1>Report GitHub Issue</h1><article>'
               b'<h1>Verified Paper Title</h1><h2>Method</h2><p>Body.</p>'
               b'<table><tr><th scope="col">Name</th><th scope="col">Value</th></tr>'
               b'<tr><td>A</td><td>1</td></tr></table>'
               b'<h2>References</h2><p>Reference text remains.</p>'
               b'<h2>Appendix</h2><p>Appendix text remains.</p></article></body></html>')
        artifact = fresh_sources.FreshArtifact(
            canonical_id="2408.11826", source_url="https://arxiv.org/html/2408.11826v1",
            request_url="https://arxiv.org/html/2408.11826v1",
            retrieval_url="https://arxiv.org/html/2408.11826v1", original_kind="HTML",
            edition="v1", retrieved_at="2026-10-14T00:00:00Z",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw), raw_bytes=raw)
        text, diagnostics = fresh_sources.parse_fresh_artifact(
            artifact, expected_title="Verified Paper Title")
        self.assertNotIn("Report GitHub Issue", text)
        self.assertIn("<table>", text)
        self.assertIn("<th scope=\"col\">Name</th>", text)
        self.assertIn("Reference text remains.", text)
        self.assertIn("Appendix text remains.", text)
        self.assertEqual(diagnostics["tables"], diagnostics["article_tables_rendered_count"])

    def test_html_projection_preserves_valid_table_row_and_column_spans(self):
        raw = (b'<html><body>arXiv:2408.11826v1<article><h1>Spanned Table Paper</h1>'
               b'<h2>Results</h2><p>Source rows follow.</p><table>'
               b'<thead><tr><th rowspan="2" scope="col">Key</th>'
               b'<th colspan="2" scope="col">Values</th></tr>'
               b'<tr><th scope="col">Alpha</th><th scope="col">Beta</th></tr></thead>'
               b'<tbody><tr><th scope="row">Item</th><td>First</td><td>Second</td></tr></tbody>'
               b'</table></article></body></html>')
        artifact = fresh_sources.FreshArtifact(
            canonical_id="2408.11826", source_url="https://arxiv.org/html/2408.11826v1",
            request_url="https://arxiv.org/html/2408.11826v1",
            retrieval_url="https://arxiv.org/html/2408.11826v1", original_kind="HTML",
            edition="v1", retrieved_at="2026-10-14T00:00:00Z",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw), raw_bytes=raw)
        text, diagnostics = fresh_sources.parse_fresh_artifact(
            artifact, expected_title="Spanned Table Paper")
        self.assertIn('<th scope="col" rowspan="2">Key</th>', text)
        self.assertIn('<th scope="col" colspan="2">Values</th>', text)
        for value in ("Key", "Values", "Alpha", "Beta", "Item", "First", "Second"):
            self.assertEqual(text.count(value), 1)
        self.assertEqual(diagnostics["tables"], diagnostics["article_tables_rendered_count"])

    def test_rejects_id_version_mismatch(self):
        with self.assertRaises(fresh_sources.FreshSourceError):
            fresh_sources.expected_source("2408.11826v2", "HTML", "v1")

    def test_existing_original_blob_gets_fresh_pdf_provenance_ref(self):
        raw = b"%PDF-1.7\nverified fixture"
        sha = hashlib.sha256(raw).hexdigest()
        artifact = fresh_sources.FreshArtifact(
            canonical_id="acl:2023.evalita-1.3",
            source_url="https://aclanthology.org/2023.evalita-1.3.pdf",
            request_url="https://aclanthology.org/2023.evalita-1.3.pdf",
            retrieval_url="https://aclanthology.org/2023.evalita-1.3.pdf",
            original_kind="PDF", edition="acl:2023.evalita-1.3",
            retrieved_at="2026-10-14T00:00:00Z", raw_sha256=sha,
            raw_size_bytes=len(raw), raw_bytes=raw)

        class Store:
            def __init__(self, root): self.calls = []
            def blob_path(self, digest): return Path(blob_path)
            def add_ref_from_verified_blob(self, provenance, **kwargs):
                self.calls.append((provenance, kwargs))
            def verify_blob(self, digest, size): return Path(blob_path)
            def ingest_file(self, *args, **kwargs): raise AssertionError("must deduplicate exact raw SHA")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "existing-originals"
            store_root.mkdir()
            (store_root / "manifest.sqlite3").write_bytes(b"manifest marker")
            blob_path = store_root / "blobs" / sha
            blob_path.parent.mkdir()
            blob_path.write_bytes(raw)
            local_raw = Path(tmp) / "fresh.pdf"
            local_raw.write_bytes(raw)
            with patch("pipeline.originals.OriginalStore", Store):
                receipt = fresh_sources._register_original(artifact, local_raw, store_root)

        self.assertEqual(receipt["storage"], "existing_verified_blob_reference")
        self.assertEqual(receipt["edition"], "sha256:" + sha)

    def test_eprint_uses_existing_path_resolving_assembler_without_service_import(self):
        archive_bytes = io.BytesIO()
        members = {"main.tex": b"\\documentclass{article}\n\\begin{document}\n\\input{sections/body}\n\\end{document}",
                   "sections/body.tex": b"\\section{Method}\nA source-bound body."}
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        raw = archive_bytes.getvalue()
        artifact = fresh_sources.FreshArtifact(
            canonical_id="2408.11826", source_url="https://arxiv.org/e-print/2408.11826v1",
            request_url="https://arxiv.org/e-print/2408.11826v1",
            retrieval_url="https://arxiv.org/e-print/2408.11826v1", original_kind="rawTexarchive",
            edition="v1", retrieved_at="2026-10-14T00:00:00Z",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw), raw_bytes=raw)

        text, diagnostics = fresh_sources.parse_fresh_artifact(artifact)

        self.assertIn("A source-bound body.", text)
        self.assertEqual(diagnostics["archive_member_count"], 2)
        self.assertEqual(diagnostics["parse_status"], "ok")

    def test_single_file_gzip_tex_is_source_unsupported_not_an_archive(self):
        raw = gzip.compress(
            b"\\documentclass{article}\n\\begin{document}Text.\\end{document}")
        artifact = fresh_sources.FreshArtifact(
            canonical_id="2408.11826", source_url="https://arxiv.org/e-print/2408.11826v1",
            request_url="https://arxiv.org/e-print/2408.11826v1",
            retrieval_url="https://arxiv.org/e-print/2408.11826v1", original_kind="rawTexarchive",
            edition="v1", retrieved_at="2026-10-14T00:00:00Z",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw), raw_bytes=raw)
        with self.assertRaisesRegex(fresh_sources.FreshSourceError, "single-file gzip TeX"):
            fresh_sources._parse_tex_archive(artifact)

    def test_truncated_and_invalid_deflate_eprints_are_per_source_errors(self):
        source = b"\\documentclass{article}\\begin{document}" + b"A" * 2000 + b"\\end{document}"
        valid = gzip.compress(source)
        for raw in (valid[:-4], valid[:20] + b"\xff\xff\xff\xff" + valid[24:]):
            artifact = fresh_sources.FreshArtifact(
                canonical_id="2408.11826", source_url="https://arxiv.org/e-print/2408.11826v1",
                request_url="https://arxiv.org/e-print/2408.11826v1",
                retrieval_url="https://arxiv.org/e-print/2408.11826v1", original_kind="rawTexarchive",
                edition="v1", retrieved_at="2026-10-14T00:00:00Z",
                raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw), raw_bytes=raw)
            with self.subTest(size=len(raw)):
                with self.assertRaises(fresh_sources.FreshSourceError):
                    fresh_sources._parse_tex_archive(artifact)

    def test_pdf_timeout_is_per_source_but_unverified_build_remains_dependency_error(self):
        from pipeline.extractor import PDFConversionError
        raw = b"%PDF-1.7\nsource fixture"
        artifact = fresh_sources.FreshArtifact(
            canonical_id="acl:2023.evalita-1.3", source_url="https://aclanthology.org/2023.evalita-1.3.pdf",
            request_url="https://aclanthology.org/2023.evalita-1.3.pdf",
            retrieval_url="https://aclanthology.org/2023.evalita-1.3.pdf", original_kind="PDF",
            edition="acl:2023.evalita-1.3", retrieved_at="2026-10-14T00:00:00Z",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw), raw_bytes=raw)
        with patch("pipeline.extractor.convert_pdf_isolated",
                   side_effect=PDFConversionError("conversion_timeout")):
            with self.assertRaisesRegex(fresh_sources.FreshSourceError, "timed out"):
                fresh_sources._parse_pdf(artifact, 1)
        with patch("pipeline.extractor.convert_pdf_isolated",
                   side_effect=PDFConversionError("converter_build_unverified")):
            with self.assertRaisesRegex(PDFConversionError, "converter_build_unverified"):
                fresh_sources._parse_pdf(artifact, 1)

    def test_original_format_rejection_is_per_source_but_store_failure_is_dependency(self):
        from pipeline.originals import OriginalError

        raw = b"\\documentclass{article}\\begin{document}Text\\end{document}"
        artifact = fresh_sources.FreshArtifact(
            canonical_id="2408.11826", source_url="https://arxiv.org/e-print/2408.11826v1",
            request_url="https://arxiv.org/e-print/2408.11826v1",
            retrieval_url="https://arxiv.org/e-print/2408.11826v1", original_kind="rawTexarchive",
            edition="v1", retrieved_at="2026-10-14T00:00:00Z",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_size_bytes=len(raw), raw_bytes=raw)

        for message, expected in (
                ("not a raw TeX tar/zip archive", fresh_sources.FreshSourceError),
                ("fresh ORIGINAL disk reserve failed", fresh_sources.FreshSourceDependencyError)):
            class Store:
                def __init__(self, root): pass
                def blob_path(self, digest): return Path(tmp) / "missing-blob"
                def ingest_file(self, *args, **kwargs): raise OriginalError(message)

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "originals"
                root.mkdir()
                (root / "manifest.sqlite3").write_bytes(b"manifest marker")
                raw_path = Path(tmp) / "fresh-source"
                raw_path.write_bytes(raw)
                with patch("pipeline.originals.OriginalStore", Store):
                    with self.assertRaises(expected):
                        fresh_sources._register_original(artifact, raw_path, root)


if __name__ == "__main__":
    unittest.main()
