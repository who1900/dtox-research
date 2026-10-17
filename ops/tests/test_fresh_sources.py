import hashlib
import gzip
import io
import tempfile
import tarfile
import unittest
import zlib
from types import SimpleNamespace
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
    def test_abs_mobile_breadcrumb_uses_explicit_submission_history_version(self):
        response = _Response(
            b'<meta name="citation_title" content="Paper">'
            b'<div class="header-breadcrumbs-mobile"><strong>arXiv:2606.27708</strong></div>'
            b'<div class="dateline">[Submitted on 26 Jun 2026]</div>'
            b'<div class="submission-history"><h2>Submission history</h2>'
            b'<strong>[v1]</strong> Fri, 26 Jun 2026 04:13:32 UTC</div>')
        response.url = 'https://arxiv.org/abs/2606.27708'
        result = fresh_sources._resolve_arxiv_abs_version(
            '2606.27708', session=_Session(response), timeout=1, started=0,
            fallback_reason='fixture')
        self.assertEqual(result['fresh_version'], 'v1')

    def test_abs_conflicting_dateline_and_history_versions_are_rejected(self):
        response = _Response(
            b'<meta name="citation_title" content="Paper">'
            b'<div class="header-breadcrumbs">arXiv:2606.27708</div>'
            b'<div class="dateline">[Submitted on 26 Jun 2026 (v1), '
            b'last revised 1 Jul 2026 (this version, v2)]</div>'
            b'<div class="submission-history"><strong>[v3]</strong></div>')
        response.url = 'https://arxiv.org/abs/2606.27708'
        with self.assertRaisesRegex(fresh_sources.FreshSourceError, 'versions disagree'):
            fresh_sources._resolve_arxiv_abs_version(
                '2606.27708', session=_Session(response), timeout=1, started=0,
                fallback_reason='fixture')

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

    def test_invalid_source_section_contract_is_per_route_fallback(self):
        sections = [{"section_title": "Result 1: First line\nSecond line",
                     "section_type": "experiments", "text": "Grounded source body."}]
        with self.assertRaisesRegex(fresh_sources.FreshSourceError,
                                    "canonical document-cache contract"):
            fresh_sources._validate_candidate_sections(
                "2606.07316", sections, "Grounded source body.", "pylatexenc_recovered", sections)

    def test_generic_pdf_uses_exact_url_and_hash_edition(self):
        raw = b"%PDF-1.7\nsource fixture"
        response = _Response(raw)
        response.url = "https://open.example/paper.pdf"
        artifact = fresh_sources.fetch_fresh_artifact(
            "openalex:W123", original_kind="PDF", edition="sha256:pending",
            source_url="https://open.example/paper.pdf", session=_Session(response))
        self.assertEqual(artifact.source_url, response.url)
        self.assertEqual(artifact.edition, "sha256:" + artifact.raw_sha256)

    def test_pmlr_http_pdf_url_normalizes_to_proceedings_https_pdf(self):
        paper_id = "pmlr:v318/litvin26a"
        declared = "http://proceedings.mlr.press/v318/litvin26a/litvin26a.pdf"
        official = "https://proceedings.mlr.press/v318/litvin26a/litvin26a.pdf"
        raw_github = "https://raw.githubusercontent.com/mlresearch/v318/main/assets/litvin26a/litvin26a.pdf"
        self.assertEqual(fresh_sources.canonical_source_url(paper_id, declared), official)
        self.assertEqual(fresh_sources.canonical_source_url(paper_id, raw_github), raw_github)
        self.assertEqual(fresh_sources.expected_source(paper_id, "PDF", "sha256:pending"), official)

        raw = b"%PDF-1.7\npmlr fixture"
        response = _Response(raw)
        response.url = raw_github
        session = _Session(response)
        artifact = fresh_sources.fetch_fresh_artifact(
            paper_id, original_kind="PDF", edition="sha256:pending",
            source_url=fresh_sources.canonical_source_url(paper_id, declared), session=session)

        self.assertEqual(session.call[0], official)
        self.assertEqual(artifact.canonical_id, paper_id)
        self.assertEqual(artifact.source_url, official)
        self.assertEqual(artifact.retrieval_url, raw_github)
        self.assertEqual(artifact.raw_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(artifact.edition, "sha256:" + artifact.raw_sha256)

    def test_pmlr_direct_proceedings_pdf_and_exact_raw_github_redirect(self):
        paper_id = "pmlr:v318/litvin26a"
        proceedings = fresh_sources.expected_source(paper_id, "PDF", "sha256:pending")
        raw_github = "https://raw.githubusercontent.com/mlresearch/v318/main/assets/litvin26a/litvin26a.pdf"
        raw = b"%PDF-1.7\npmlr fixture"

        direct = _Response(raw)
        direct.url = proceedings
        direct_session = _Session(direct)
        direct_artifact = fresh_sources.fetch_fresh_artifact(
            paper_id, original_kind="PDF", edition="sha256:pending", session=direct_session)
        self.assertEqual(direct_session.call[0], proceedings)
        self.assertEqual(direct_artifact.source_url, proceedings)
        self.assertEqual(direct_artifact.retrieval_url, proceedings)

        redirected = _Response(raw)
        redirected.url = raw_github
        redirect_session = _Session(redirected)
        redirect_artifact = fresh_sources.fetch_fresh_artifact(
            paper_id, original_kind="PDF", edition="sha256:pending", session=redirect_session)
        self.assertEqual(redirect_session.call[0], proceedings)
        self.assertEqual(redirect_artifact.source_url, proceedings)
        self.assertEqual(redirect_artifact.retrieval_url, raw_github)
        self.assertEqual(redirect_artifact.raw_sha256, hashlib.sha256(raw).hexdigest())

    def test_pmlr_rejects_non_id_derived_cross_host_redirect(self):
        paper_id = "pmlr:v318/litvin26a"
        response = _Response(b"%PDF-1.7\nfixture")
        response.url = "https://raw.githubusercontent.com/mlresearch/v318/main/assets/other/other.pdf"
        with self.assertRaisesRegex(fresh_sources.FreshSourceError, "different host"):
            fresh_sources.fetch_fresh_artifact(
                paper_id, original_kind="PDF", edition="sha256:pending",
                session=_Session(response))

    def test_pmlr_url_canonicalization_rejects_id_or_host_mismatch(self):
        with self.assertRaisesRegex(fresh_sources.FreshSourceError, "does not match canonical ID"):
            fresh_sources.canonical_source_url(
                "pmlr:v318/litvin26a",
                "http://proceedings.mlr.press/v318/other/other.pdf")
        with self.assertRaisesRegex(fresh_sources.FreshSourceError, "does not match canonical ID"):
            fresh_sources.canonical_source_url(
                "pmlr:v318/litvin26a", "http://example.org/litvin26a.pdf")

    def test_pmlr_acquisition_binding_keeps_declared_url_and_verified_route(self):
        paper_id = "pmlr:v265/eide25a"
        declared = "http://proceedings.mlr.press/v265/eide25a/eide25a.pdf"
        official = fresh_sources.canonical_source_url(paper_id, declared)
        raw = b"%PDF-1.7\nsource fixture"
        response = _Response(raw)
        response.url = official

        def build_chunks(_text, *, section_sink, element_sink, document_kind):
            self.assertEqual(document_kind, "paper")
            section_sink.append({"section_title": "Abstract", "section_type": "abstract", "text": "A source body."})
            return ([{"chunk_index": 0, "section_title": "Abstract", "section_type": "abstract",
                      "text": "A source body."}], "markdown")

        extractor = SimpleNamespace(build_chunks_markdown=build_chunks)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(fresh_sources, "_parse_pdf", return_value=(
                    "BoRA: Bayesian Hierarchical Low-Rank Adaption for Multi-task Large Language Models\n\nA source body.",
                    {"page_count": 1, "page_text_chars": 50, "unresolved_unicode_mappings": []})), \
                    patch.object(fresh_sources, "_pipeline_modules", return_value=(extractor, None)), \
                    patch.object(fresh_sources, "_validate_candidate_sections"), \
                    patch.object(fresh_sources, "_register_original", return_value={
                        "canonical_path": str(Path(tmp) / "original"),
                        "edition": "sha256:" + hashlib.sha256(raw).hexdigest()}):
                result = fresh_sources.acquire_and_parse(
                    {"paper_id": paper_id, "fresh_kind": "PDF", "fresh_url": official,
                     "declared_source_url": declared,
                     "title": "BoRA: Bayesian Hierarchical Low-Rank Adaption for Multi-task Large Language Models"},
                    Path(tmp) / "stage", session=_Session(response))

        binding = result["source_binding"]
        self.assertEqual(binding["declared_source_url"], declared)
        self.assertEqual(binding["request_url"], official)
        self.assertEqual(binding["retrieval_url"], official)
        self.assertEqual(binding["url"], official)
        self.assertEqual(binding["raw_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(binding["edition"], "sha256:" + hashlib.sha256(raw).hexdigest())

    def test_legacy_arxiv_id_uses_same_explicit_edition_binding(self):
        self.assertTrue(fresh_sources.is_arxiv_id("hep-th/0611328"))
        self.assertTrue(fresh_sources.is_arxiv_id("math-ph/0611021v2"))
        self.assertFalse(fresh_sources.is_arxiv_id("hep-th/061132x"))
        self.assertEqual(
            fresh_sources.expected_source("hep-th/0611328", "PDF", "v1"),
            "https://arxiv.org/pdf/hep-th/0611328v1")
        with self.assertRaisesRegex(fresh_sources.FreshSourceError, "version and edition disagree"):
            fresh_sources.expected_source("hep-th/0611328v2", "PDF", "v1")

    def test_generic_pdf_title_error_names_the_markdown_prefix_check(self):
        raw = b"%PDF-1.7\nsource fixture"
        artifact = fresh_sources.FreshArtifact(
            canonical_id="pmlr:v265/eide25a", source_url="https://example.org/paper.pdf",
            request_url="https://example.org/paper.pdf", retrieval_url="https://example.org/paper.pdf",
            original_kind="PDF", edition="sha256:" + hashlib.sha256(raw).hexdigest(),
            retrieved_at="2026-10-14T00:00:00Z", raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_size_bytes=len(raw), raw_bytes=raw)
        with patch.object(fresh_sources, "_parse_pdf", return_value=("unrelated content", {"page_count": 1})):
            with self.assertRaisesRegex(fresh_sources.FreshSourceError,
                                        "converted Markdown prefix does not contain expected title"):
                fresh_sources.parse_fresh_artifact(
                    artifact, expected_title="BoRA: Bayesian Hierarchical Low-Rank Adaption")

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

    def test_arxiv_resolver_uses_exact_publisher_history_if_export_times_out(self):
        import requests

        class _FallbackSession:
            def __init__(self):
                self.urls = []

            def get(self, url, **kwargs):
                self.urls.append(url)
                if "export.arxiv.org" in url:
                    raise requests.ReadTimeout("fixture timeout")
                response = _Response((
                    b'<html><head><meta name="citation_title" content="A &amp; B: Results"></head>'
                    b'<body><div class="header-breadcrumbs">arXiv:2606.07316 (cs)</div>'
                    b'<div class="dateline">[Submitted on 5 Jun 2026 ( v1 ), '
                    b'last revised 13 Aug 2026 (this version, v2)]</div></body></html>'))
                response.url = url
                return response

        session = _FallbackSession()
        resolved = fresh_sources.resolve_arxiv_version("2606.07316", session=session)
        self.assertEqual(session.urls, [
            "https://export.arxiv.org/api/query?id_list=2606.07316",
            "https://arxiv.org/abs/2606.07316"])
        self.assertEqual(resolved["fresh_version"], "v2")
        self.assertEqual(resolved["title"], "A & B: Results")
        self.assertEqual(resolved["resolution"], "publisher_abs_exact_id_version_history")

    def test_arxiv_abs_fallback_requires_exact_version_evidence(self):
        import requests

        class _IncompleteSession:
            def get(self, url, **kwargs):
                if "export.arxiv.org" in url:
                    raise requests.ReadTimeout("fixture timeout")
                response = _Response(
                    b'<html><meta name="citation_title" content="Title">'
                    b'<body>arXiv:2606.07316</body></html>')
                response.url = url
                return response

        with self.assertRaisesRegex(fresh_sources.FreshSourceError, "lacks exact ID"):
            fresh_sources.resolve_arxiv_version("2606.07316", session=_IncompleteSession())

    def test_arxiv_abs_fallback_does_not_take_identity_from_related_links(self):
        import requests

        class _RelatedIdSession:
            def get(self, url, **kwargs):
                if "export.arxiv.org" in url:
                    raise requests.ReadTimeout("fixture timeout")
                response = _Response(
                    b'<html><meta name="citation_title" content="Title"><body>'
                    b'<a href="/abs/2606.07316">Related paper</a>'
                    b'<div class="dateline">[Submitted on 5 Jun 2026 (v1)]</div>'
                    b'</body></html>')
                response.url = url
                return response

        with self.assertRaisesRegex(fresh_sources.FreshSourceError, "lacks exact ID"):
            fresh_sources.resolve_arxiv_version("2606.07316", session=_RelatedIdSession())

    def test_arxiv_abs_fallback_wont_assume_v1_if_revision_history_is_incomplete(self):
        import requests

        class _IncompleteHistorySession:
            def get(self, url, **kwargs):
                if "export.arxiv.org" in url:
                    raise requests.ReadTimeout("fixture timeout")
                response = _Response(
                    b'<html><meta name="citation_title" content="Title"><body>'
                    b'<div class="header-breadcrumbs">arXiv:2606.07316</div>'
                    b'<div class="dateline">[Submitted on 5 Jun 2026 (v1), '
                    b'last revised 13 Aug 2026]</div></body></html>')
                response.url = url
                return response

        with self.assertRaisesRegex(fresh_sources.FreshSourceError, "lacks exact ID"):
            fresh_sources.resolve_arxiv_version("2606.07316", session=_IncompleteHistorySession())

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
        with self.assertRaisesRegex(fresh_sources.FreshSourceError,
                                    "heading does not uniquely match"):
            fresh_sources.parse_fresh_artifact(
                artifact, expected_title="A metadata title that differs")

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
        with patch("pipeline.extractor.convert_pdf_isolated",
                   side_effect=PDFConversionError("conversion_failed", 1)):
            with self.assertRaisesRegex(fresh_sources.FreshSourceError, "PDF parser rejected") as caught:
                fresh_sources._parse_pdf(artifact, 1)
            self.assertIsInstance(caught.exception.__cause__, PDFConversionError)

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
