"""Offline parser state and cache safety, using injected AST dependencies."""
import copy
import contextlib
import json
import os
import sqlite3
import tempfile
import types
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import Mock, patch

from pipeline.document_cache import ManifestInvalid
from pipeline.tests.test_pipeline_audit_fixes import load_functions


class ParseSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "state.db"
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.clock = 1000.0
        self.paper_id = "2401.00001"
        self.chunks = [{"chunk_index": 0, "section_type": "method", "section_title": "Method",
                        "element_type": "equation", "has_math": True, "has_algorithm": False,
                        "part_index": 0, "part_total": 1, "text": r"\frac{a}{b}=\alpha",
                        "custom_payload": {"keep": [1, 2]}}]
        self.canonical_sections = [{"section_title": "Method", "section_type": "method",
                                    "text": self.chunks[0]["text"]}]

        def parsed(mode, section_sink):
            if section_sink is not None:
                section_sink.extend(copy.deepcopy(self.canonical_sections))
            return copy.deepcopy(self.chunks), mode

        self.extractor = types.SimpleNamespace(
            build_chunks=Mock(side_effect=lambda *a, section_sink=None, **k: parsed("pylatexenc", section_sink)),
            build_chunks_markdown=Mock(side_effect=lambda *a, section_sink=None, **k: parsed("markdown", section_sink)),
            should_embed=lambda _: True,
        )
        self.manifests = types.ModuleType("pipeline.document_cache")
        self.manifests.ManifestInvalid = ManifestInvalid
        self.manifests.make_section_manifest = Mock(return_value={"paper_id": self.paper_id})
        self.manifests.publish_section_manifest = Mock(
            side_effect=lambda path, manifest, **kwargs: path.write_bytes(json.dumps(manifest).encode("utf-8")))
        self.upsert = Mock(return_value=True)
        self.worker = load_functions(
            "init_db", "safe_id", "_record_parse_failure", "_write_parse_cache", "_make_embed_fn",
            "chunk_step", "_complete_paper", "_queue_index_sync", "_preflight_chunk_snapshot", "embed_step",
            __package__="pipeline", BASE_DIR=self.root, PUBLICATION_PATHS={}, CHUNK_DIR=self.root / "chunk_cache",
            FULLTEXT_DIR=self.root / "fulltext_cache", EXTRACTOR_VERSION=4,
            PARSE_RETRY_BASE_SECONDS=60, PARSE_RETRY_MAX_SECONDS=3600,
            CHUNK_CYCLE_LIMIT=10, extractor=self.extractor, os=os, tempfile=tempfile, Path=Path,
            time=types.SimpleNamespace(time=lambda: self.clock), now_iso=lambda: "fixture-time",
            is_spec_source=lambda paper: paper.startswith("eip:"), ACL_ID_PREFIX="acl:",
            OPENALEX_ID_PREFIX="openalex:", PMLR_ID_PREFIX="pmlr:", HAL_ID_PREFIX="hal:",
            embed_texts_batch=Mock(), _reconcile_index_sync=Mock(return_value=0),
            _upsert_chunk_snapshot=self.upsert, fts_write=Mock(return_value=True),
            QDRANT_URL="http://fixture-qdrant", COLLECTION_NAME="papers_fulltext",
            _fresh_fts_ids=Mock(return_value=[]), EMBED_CYCLE_LIMIT=10, EMBED_BATCH_TEXTS=8,
            EMBED_BATCH_URLS=["fixture"], EMBED_WORKERS=1, NAMESPACE_URL=uuid.NAMESPACE_URL,
            ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed,
            _run_embed_batch=lambda session, batch, slots: (batch, [[0.1] for _ in batch]),
            read_latex_cache=lambda _: None,
        )
        self.worker.init_db(self.conn)
        self.conn.execute("INSERT INTO papers(arxiv_id,status,fulltext_source,extractor_version,layers) "
                          "VALUES (?,'fulltext_fetched','latex',2,'web3')", (self.paper_id,))
        self.conn.execute("INSERT INTO completion_history(arxiv_id,first_known,completed) VALUES (?,1,0)",
                          (self.paper_id,))
        self.conn.commit()
        for name in ("fulltext_cache", "chunk_cache", "section_cache"):
            (self.root / name).mkdir()
        self.source_path = self.root / "fulltext_cache" / f"{self.paper_id}.tex"
        self.source_bytes = b"\\section{Method}\n\\frac{a}{b}=\\alpha"
        self.source_path.write_bytes(self.source_bytes)
        self.cache_path = self.root / "chunk_cache" / f"{self.paper_id}.json"
        self.old_cache = b'[{"text":"previous good text"}]'
        self.cache_path.write_bytes(self.old_cache)
        self.section_path = self.root / "section_cache" / self.cache_path.name
        self.section_path.write_bytes(b'{"previous":"section"}')
        self.session = Mock()

    def paper(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (self.paper_id,)).fetchone()

    def parse(self):
        with patch.dict("sys.modules", {"pipeline.document_cache": self.manifests,
                                        "document_cache": self.manifests}):
            return self.worker.chunk_step(self.conn, self.session)

    def assert_preserved(self):
        self.assertEqual(self.paper()["status"], "fulltext_fetched")
        self.assertEqual(self.paper()["extractor_version"], 2)
        self.assertEqual(self.cache_path.read_bytes(), self.old_cache)
        self.assertEqual(self.section_path.read_bytes(), b'{"previous":"section"}')
        self.assertEqual(self.source_path.read_bytes(), self.source_bytes)
        self.assertEqual(self.session.mock_calls, [])
        self.upsert.assert_not_called()

    def test_additive_migration_is_repeatable_and_preserves_old_data(self):
        self.worker.init_db(self.conn)
        self.worker.init_db(self.conn)
        self.assertEqual(self.paper()["extractor_version"], 2)
        self.assertEqual(self.paper()["parse_attempts"], 0)
        self.assertEqual(self.paper()["parse_retry_after"], 0)
        self.assertIsNone(self.paper()["parse_status"])

    def test_parser_exception_persists_failure_without_overwrite_or_advance(self):
        self.extractor.build_chunks.side_effect = RuntimeError("parser failed")
        self.assertEqual(self.parse(), 0)
        self.assert_preserved()
        self.assertEqual(self.paper()["parse_status"], "failed")
        self.assertEqual(self.paper()["parse_error"], "parser_exception:RuntimeError")
        self.assertEqual(self.paper()["parser_revision"], 4)
        self.assertEqual(self.paper()["parse_retry_after"], 1060)
        self.manifests.make_section_manifest.assert_not_called()

    def test_empty_output_cannot_succeed(self):
        self.extractor.build_chunks.side_effect = lambda *a, **k: ([], "pylatexenc")
        self.assertEqual(self.parse(), 0)
        self.assert_preserved()
        self.assertEqual(self.paper()["parse_error"], "empty_or_invalid_parser_output")

    def test_whitespace_only_output_cannot_succeed(self):
        self.extractor.build_chunks.side_effect = lambda *a, **k: ([{"text": " \n "}], "pylatexenc")
        self.assertEqual(self.parse(), 0)
        self.assert_preserved()

    def test_empty_source_is_persisted_without_invoking_parser(self):
        self.source_bytes = b" \n "
        self.source_path.write_bytes(self.source_bytes)
        self.assertEqual(self.parse(), 0)
        self.assert_preserved()
        self.extractor.build_chunks.assert_not_called()
        self.assertEqual(self.paper()["parse_error"], "empty_or_invalid_source")

    def test_missing_source_records_failure_and_preserves_existing_chunk_cache(self):
        self.source_path.unlink()
        self.assertEqual(self.parse(), 0)
        self.assertEqual(self.cache_path.read_bytes(), self.old_cache)
        self.assertEqual(self.paper()["status"], "fulltext_fetched")
        self.assertEqual(self.paper()["parse_error"], "source_unavailable:FileNotFoundError")
        self.extractor.build_chunks.assert_not_called()
        self.assertEqual(self.session.mock_calls, [])

    def test_legacy_mode_is_degraded_and_never_completes(self):
        self.extractor.build_chunks.side_effect = lambda *a, **k: (copy.deepcopy(self.chunks), "legacy")
        self.assertEqual(self.parse(), 0)
        self.assert_preserved()
        self.assertEqual(self.paper()["parse_status"], "degraded")
        self.assertEqual(self.paper()["parser_mode"], "legacy")
        self.assertEqual(self.paper()["parser_revision"], 4)
        self.conn.execute("UPDATE papers SET status='chunked' WHERE arxiv_id=?", (self.paper_id,))
        self.conn.commit()
        self.assertFalse(self.worker._complete_paper(self.conn, self.paper_id))
        self.assertEqual(self.worker.embed_step(self.conn, self.session), 0)
        self.upsert.assert_not_called()

    def test_retry_survives_reopen_and_backoff_is_bounded(self):
        self.extractor.build_chunks.side_effect = RuntimeError("parser failed")
        self.assertEqual(self.parse(), 0)
        with contextlib.closing(sqlite3.connect(self.db)) as other:
            other.row_factory = sqlite3.Row
            self.assertEqual(self.worker.chunk_step(other, self.session), 0)
        self.assertEqual(self.extractor.build_chunks.call_count, 1)
        delays = []
        for _ in range(9):
            self.clock = self.paper()["parse_retry_after"]
            self.assertEqual(self.parse(), 0)
            delays.append(self.paper()["parse_retry_after"] - self.clock)
        self.assertEqual(delays[:3], [120, 240, 480])
        self.assertEqual(delays[-1], 3600)
        self.assertLessEqual(max(delays), 3600)
        self.assert_preserved()

    def test_healthy_parse_keeps_chunk_list_and_formulas_and_publishes_manifest(self):
        with patch("os.fsync", wraps=os.fsync) as fsync, patch("os.replace", wraps=os.replace) as replace:
            self.assertEqual(self.parse(), 1)
        fsync.assert_called_once()
        replace.assert_called_once()
        self.assertEqual(json.loads(self.cache_path.read_bytes()), self.chunks)
        self.assertEqual(self.paper()["status"], "chunked")
        self.assertEqual(self.paper()["parse_status"], "ok")
        self.assertEqual(self.paper()["parser_mode"], "pylatexenc")
        self.assertEqual(self.paper()["parser_revision"], 4)
        self.manifests.make_section_manifest.assert_called_once_with(
            self.paper_id, self.canonical_sections, self.cache_path.read_bytes(), self.source_bytes, "pylatexenc", 4)
        self.manifests.publish_section_manifest.assert_called_once_with(
            self.section_path, {"paper_id": self.paper_id}, cache_dir=self.section_path.parent)
        self.assertEqual(list(self.cache_path.parent.glob("*.tmp")), [])

    def test_standalone_manifest_import_uses_same_contract(self):
        self.worker.chunk_step.__globals__["__package__"] = None
        self.assertEqual(self.parse(), 1)
        self.manifests.publish_section_manifest.assert_called_once()

    def test_real_shared_helper_publishes_manifest_bound_to_unchanged_chunk_structure(self):
        from pipeline import document_cache
        self.assertEqual(self.worker.chunk_step(self.conn, self.session), 1)
        manifest = json.loads(self.section_path.read_bytes())
        document_cache.validate_section_manifest(
            manifest, self.paper_id, self.cache_path.read_bytes(), self.source_bytes)
        self.assertEqual(manifest["sections"][0]["text"], self.chunks[0]["text"])
        self.assertEqual(manifest["parser_mode"], "pylatexenc")
        self.assertEqual(json.loads(self.cache_path.read_bytes()), self.chunks)

    def test_unverified_projection_still_atomically_publishes_healthy_chunks(self):
        self.manifests.make_section_manifest.side_effect = ManifestInvalid("ambiguous protected split")
        with patch("os.fsync", wraps=os.fsync) as fsync, patch("os.replace", wraps=os.replace) as replace:
            self.assertEqual(self.parse(), 1)
        fsync.assert_called_once()
        replace.assert_called_once()
        self.assertEqual(self.cache_path.read_bytes(), json.dumps(self.chunks).encode("utf-8"))
        self.assertEqual(self.section_path.read_bytes(), b'{"previous":"section"}')
        self.manifests.publish_section_manifest.assert_not_called()
        self.assertEqual(self.paper()["status"], "chunked")
        self.assertEqual(self.paper()["parse_status"], "ok")
        self.assertIsNone(self.paper()["parse_error"])
        self.assertEqual(self.paper()["parse_attempts"], 0)
        self.assertEqual(self.paper()["parse_retry_after"], 0)
        self.worker.log.warning.assert_called_once_with(
            "chunk: %s degraded read view (ManifestInvalid); canonical assembly unverified", self.paper_id)
        self.assertEqual(list(self.cache_path.parent.glob("*.tmp")), [])
        self.assertEqual(self.session.mock_calls, [])
        self.upsert.assert_not_called()

    def test_standalone_import_tolerates_only_manifest_invalid(self):
        self.worker.chunk_step.__globals__["__package__"] = None
        self.manifests.make_section_manifest.side_effect = ManifestInvalid("unverified assembly")
        self.assertEqual(self.parse(), 1)
        self.assertEqual(self.paper()["parse_status"], "ok")
        self.assertEqual(json.loads(self.cache_path.read_bytes()), self.chunks)
        self.assertEqual(self.section_path.read_bytes(), b'{"previous":"section"}')
        self.manifests.publish_section_manifest.assert_not_called()

    def test_real_invalid_manifest_preserves_old_projection_and_reader_rejects_stale_hash(self):
        from pipeline import document_cache
        old_manifest = document_cache.make_section_manifest(
            self.paper_id, self.chunks, self.old_cache, self.source_bytes, "pylatexenc", 4)
        document_cache.publish_section_manifest(self.section_path, old_manifest)
        old_manifest_bytes = self.section_path.read_bytes()
        # Long headings are valid; an embedded control character is not.
        self.chunks[0]["section_title"] = "M" * 513 + "\x00"
        self.canonical_sections[0]["section_title"] = self.chunks[0]["section_title"]
        with self.assertRaises(document_cache.ManifestInvalid):
            document_cache.make_section_manifest(
                self.paper_id, self.chunks, json.dumps(self.chunks).encode("utf-8"),
                self.source_bytes, "pylatexenc", 4)
        self.assertEqual(self.worker.chunk_step(self.conn, self.session), 1)
        self.assertEqual(json.loads(self.cache_path.read_bytes()), self.chunks)
        self.assertEqual(self.section_path.read_bytes(), old_manifest_bytes)
        self.assertEqual(self.paper()["parse_status"], "ok")
        self.assertEqual(self.paper()["parse_retry_after"], 0)
        with self.assertRaisesRegex(document_cache.ManifestInvalid, "hash mismatch"):
            document_cache.load_section_manifest(self.root, self.paper_id)

    def test_arbitrary_manifest_builder_errors_are_not_treated_as_optional_projection(self):
        for error in (ValueError("schema bug"), RuntimeError("implementation bug"), OSError("read failure")):
            with self.subTest(error=type(error).__name__):
                self.manifests.make_section_manifest.side_effect = error
                with self.assertRaises(type(error)):
                    with patch.dict("sys.modules", {"pipeline.document_cache": self.manifests}):
                        self.worker._write_parse_cache(self.paper_id, self.chunks, self.source_bytes, "pylatexenc",
                                                       canonical_sections=self.canonical_sections)
                self.assertEqual(self.parse(), 0)
                self.assert_preserved()
                self.assertEqual(self.paper()["parse_status"], "failed")
                self.assertEqual(self.paper()["parse_error"], f"cache_publish_failed:{type(error).__name__}")
                self.clock = self.paper()["parse_retry_after"]
        self.manifests.publish_section_manifest.assert_not_called()

    def test_migrated_section_directory_resolves_but_document_symlinks_are_rejected(self):
        from pipeline import document_cache
        directory = self.root / "section_cache"
        migrated = self.root / "migrated_sections"
        directory.rename(migrated)
        try:
            directory.symlink_to(migrated, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        self.worker._write_parse_cache.__globals__['PUBLICATION_PATHS'] = {'section_cache_dir': migrated}
        with patch.dict("sys.modules", {"pipeline.document_cache": document_cache}):
            self.worker._write_parse_cache(self.paper_id, self.chunks, self.source_bytes, "pylatexenc",
                                           canonical_sections=self.canonical_sections)
        self.assertTrue((migrated / self.cache_path.name).is_file())
        target = migrated / self.cache_path.name
        saved = migrated / "saved.json"
        target.rename(saved)
        target.symlink_to(saved)
        before = saved.read_bytes()
        with patch.dict("sys.modules", {"pipeline.document_cache": document_cache}):
            with self.assertRaisesRegex(ManifestInvalid, "symlinked document path"):
                self.worker._write_parse_cache(self.paper_id, self.chunks, self.source_bytes, "pylatexenc",
                                               canonical_sections=self.canonical_sections)
        self.assertEqual(saved.read_bytes(), before)

    def test_cache_replace_failure_with_invalid_projection_still_preserves_old_cache(self):
        self.manifests.make_section_manifest.side_effect = ManifestInvalid("unverified assembly")
        with patch("os.replace", side_effect=OSError("cache publication failed")):
            self.assertEqual(self.parse(), 0)
        self.assert_preserved()
        self.assertEqual(self.paper()["parse_status"], "failed")
        self.assertEqual(self.paper()["parse_error"], "cache_publish_failed:OSError")
        self.manifests.publish_section_manifest.assert_not_called()
        self.assertEqual(list(self.cache_path.parent.glob("*.tmp")), [])

    def test_unsupported_parser_mode_cannot_bypass_manifest_validation(self):
        self.extractor.build_chunks.side_effect = lambda *a, **k: (copy.deepcopy(self.chunks), "unknown")
        self.manifests.make_section_manifest.side_effect = ManifestInvalid("unsupported mode")
        self.assertEqual(self.parse(), 0)
        self.assert_preserved()
        self.assertEqual(self.paper()["parse_error"], "unsupported_parser_mode")
        self.manifests.make_section_manifest.assert_not_called()

    def test_missing_collector_keeps_healthy_chunks_without_guessing_canonical_overlap(self):
        self.extractor.build_chunks.side_effect = lambda *a, **k: (copy.deepcopy(self.chunks), "pylatexenc")
        self.assertEqual(self.parse(), 1)
        self.assertEqual(self.paper()["parse_status"], "ok")
        self.assertEqual(json.loads(self.cache_path.read_bytes()), self.chunks)
        self.assertEqual(self.section_path.read_bytes(), b'{"previous":"section"}')
        self.manifests.make_section_manifest.assert_not_called()
        self.manifests.publish_section_manifest.assert_not_called()

    def test_real_markdown_collector_publishes_unsplit_repeating_code(self):
        from pipeline import document_cache, extractor
        code = "```python\n" + "  emit(value)\n" * 220 + "```"
        source = "# Method\nBefore\n\n" + code + "\n\nAfter"
        expected_chunks, _mode = extractor.build_chunks_markdown(source, document_kind="paper")
        self.source_path.write_bytes(source.encode("utf-8"))
        self.conn.execute("UPDATE papers SET fulltext_source='oa-mirror-pdf'")
        self.conn.commit()
        self.worker.chunk_step.__globals__["extractor"] = extractor
        with patch.object(extractor, "build_chunks_markdown", wraps=extractor.build_chunks_markdown) as parse:
            self.assertEqual(self.worker.chunk_step(self.conn, self.session), 1)
        parse.assert_called_once()
        self.assertEqual(parse.call_args.kwargs["document_kind"], "paper")
        manifest = document_cache.load_section_manifest(self.root, self.paper_id)
        self.assertEqual(manifest["sections"][0]["text"], "Before\n\n" + code + "\n\nAfter")
        self.assertEqual(json.loads(self.cache_path.read_bytes()), expected_chunks)
        self.assertEqual(self.paper()["parse_status"], "ok")

    def test_markdown_mode_is_preserved(self):
        self.conn.execute("UPDATE papers SET fulltext_source='oa-mirror-pdf'")
        self.conn.commit()
        self.assertEqual(self.parse(), 1)
        self.extractor.build_chunks.assert_not_called()
        self.assertEqual(self.paper()["parser_mode"], "markdown")
        self.assertEqual(self.paper()["fulltext_source"], "oa-mirror-pdf")

    def test_markdown_spec_chunk_records_spec_acquisition(self):
        self.worker.chunk_step.__globals__["is_spec_source"] = lambda _paper: True
        self.assertEqual(self.parse(), 1)
        self.assertEqual(self.paper()["parser_mode"], "markdown")
        self.assertEqual(self.paper()["fulltext_source"], "spec-markdown")
        self.assertEqual(self.extractor.build_chunks_markdown.call_args.kwargs["document_kind"], "spec")

    def test_fsync_failure_preserves_previous_cache_and_does_not_advance(self):
        with patch("os.fsync", side_effect=OSError("disk failure")):
            self.assertEqual(self.parse(), 0)
        self.assert_preserved()
        self.assertEqual(self.paper()["parse_error"], "cache_publish_failed:OSError")
        self.assertEqual(list(self.cache_path.parent.glob("*.tmp")), [])

    def test_manifest_failure_preserves_previous_chunk_cache(self):
        self.manifests.publish_section_manifest.side_effect = OSError("manifest failure")
        self.assertEqual(self.parse(), 0)
        self.assert_preserved()

    def test_success_after_retry_clears_error_and_attempts(self):
        self.extractor.build_chunks.side_effect = RuntimeError("parser failed")
        self.assertEqual(self.parse(), 0)
        self.clock = self.paper()["parse_retry_after"]
        self.extractor.build_chunks.side_effect = lambda *a, **k: (copy.deepcopy(self.chunks), "pylatexenc")
        self.assertEqual(self.parse(), 1)
        self.assertIsNone(self.paper()["parse_error"])
        self.assertEqual(self.paper()["parse_retry_after"], 0)
        self.assertEqual(self.paper()["parse_attempts"], 0)

    def test_unknown_and_completed_sources_are_filtered_and_completion_drift_preserves_cache(self):
        fetch = Mock(side_effect=AssertionError("ineligible source must not be fetched"))
        worker = load_functions("fulltext_step", FULLTEXT_CYCLE_LIMIT=10, FULLTEXT_WORKERS=1,
                                FULLTEXT_DIR=self.root / "fulltext_cache", safe_id=lambda value: value,
                                _fetch_one_fulltext=fetch, ThreadPoolExecutor=ThreadPoolExecutor,
                                as_completed=as_completed, os=os, now_iso=lambda: "fixture-time")
        self.conn.execute("UPDATE papers SET status='quality_checked'")
        self.conn.commit()
        for history in (None, (0, 0), (1, 1)):
            with self.subTest(history=history):
                self.conn.execute("DELETE FROM completion_history WHERE arxiv_id=?", (self.paper_id,))
                if history:
                    self.conn.execute("INSERT INTO completion_history VALUES (?,?,?)", (self.paper_id, *history))
                self.conn.commit()
                self.assertEqual(worker.fulltext_step(self.conn, limit=1), 0)
                fetch.assert_not_called()
                self.assertEqual(self.source_path.read_bytes(), self.source_bytes)
                self.assertEqual(self.paper()["status"], "quality_checked")
        self.conn.execute("UPDATE completion_history SET completed=0 WHERE arxiv_id=?", (self.paper_id,))
        self.conn.commit()
        def completed_during_fetch(paper, _abstract, _url):
            with contextlib.closing(sqlite3.connect(self.db)) as other, other:
                other.execute("UPDATE completion_history SET completed=1 WHERE arxiv_id=?", (paper,))
            return paper, "changed after completion", "latex"
        fetch.side_effect = completed_during_fetch
        self.assertEqual(worker.fulltext_step(self.conn, limit=1), 0)
        fetch.assert_called_once()
        self.assertEqual(self.source_path.read_bytes(), self.source_bytes)
        self.assertEqual(self.paper()["status"], "quality_checked")
        self.assertEqual(list(self.source_path.parent.glob("*.tmp")), [])

    def test_unknown_and_completed_backlog_cannot_starve_parser_limit(self):
        self.conn.execute("UPDATE papers SET updated_at='2026'")
        for paper, stamp in (("unknown", "2000"), ("completed", "2001")):
            self.conn.execute("INSERT INTO papers(arxiv_id,status,fulltext_source,updated_at) "
                              "VALUES (?,'fulltext_fetched','latex',?)", (paper, stamp))
        self.conn.execute("INSERT INTO completion_history VALUES ('completed',1,1)")
        self.conn.commit()
        with patch.dict("sys.modules", {"pipeline.document_cache": self.manifests,
                                       "document_cache": self.manifests}):
            self.assertEqual(self.worker.chunk_step(self.conn, self.session, limit=1), 1)
        self.extractor.build_chunks.assert_called_once()
        self.assertEqual(self.paper()["status"], "chunked")
        self.assertEqual(self.conn.execute("SELECT status FROM papers WHERE arxiv_id='unknown'").fetchone()[0], "fulltext_fetched")
        self.assertEqual(self.conn.execute("SELECT status FROM papers WHERE arxiv_id='completed'").fetchone()[0], "fulltext_fetched")

    def test_embedding_requires_explicit_ok_parse_status_before_any_batch(self):
        batch = Mock(side_effect=AssertionError("ineligible parse must not embed"))
        self.worker.embed_step.__globals__["_run_embed_batch"] = batch
        for parse_status in (None, "unexpected"):
            with self.subTest(parse_status=parse_status):
                self.conn.execute("UPDATE papers SET status='chunked',parse_status=?", (parse_status,))
                self.conn.commit()
                self.assertEqual(self.worker.embed_step(self.conn, self.session), 0)
                batch.assert_not_called()
                self.upsert.assert_not_called()
                self.worker.fts_write.assert_not_called()
                self.assertEqual(self.session.mock_calls, [])

    def test_embedding_preserves_formula_identity_and_adds_parse_metadata(self):
        self.assertEqual(self.parse(), 1)
        retrieve = Mock()
        retrieve.json.return_value = {"result": []}
        scroll = Mock()
        scroll.json.return_value = {"result": {"points": []}}
        self.session.post.side_effect = [retrieve, scroll]

        def assert_preflight_before_write(*_args, **_kwargs):
            self.assertEqual(self.session.post.call_count, 2)
            self.assertEqual(self.session.post.call_args_list[-1].args[0],
                             "http://fixture-qdrant/collections/papers_fulltext/points/scroll")
            return True

        self.upsert.side_effect = assert_preflight_before_write
        self.assertEqual(self.worker.embed_step(self.conn, self.session), 1)
        points = self.upsert.call_args.args[2]
        self.assertEqual(points[0]["id"], str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.paper_id}#0")))
        payload = points[0]["payload"]
        self.assertEqual(payload["text"], self.chunks[0]["text"])
        self.assertTrue(payload["has_math"])
        self.assertEqual(payload["element_type"], "equation")
        self.assertEqual(payload["parser_mode"], "pylatexenc")
        self.assertEqual(payload["parser_revision"], 4)
        self.assertEqual(payload["parse_status"], "ok")


if __name__ == "__main__":
    unittest.main()
