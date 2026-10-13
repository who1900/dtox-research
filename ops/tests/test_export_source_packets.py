"""Small persisted fixtures only: never import service or open a production corpus."""
import contextlib
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from api.source_representations import search_catalog
from ops import build_source_representations as producer
from ops import export_source_packets as exporter
from pipeline.document_cache import make_section_manifest, safe_id


TEX = "\\documentclass{article}\r\n\\begin{document}\r\nForeign frobnicator \\cite{k}.\r\n\\end{document}\r\n"
INLINE = TEX.replace("\\end{document}", "\\begin{thebibliography}{9}\r\n\\bibitem{k} arXiv:2401.12345\r\n\\end{thebibliography}\r\n\\end{document}")
BIB = [{"path": "paper/refs.bib", "text": "@misc{k,url={https://arxiv.org/abs/2401.12345v2}}"}]


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "state.db"
        self.output = self.root / "export"
        self.paths = {name: self.root / name.replace("_", "-") for name in
                      ("latex_cache", "fulltext_cache", "chunks", "publication_cache", "section_cache")}
        for path in self.paths.values():
            path.mkdir()
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,title TEXT,year INTEGER,layers TEXT,"
                          "status TEXT,abstract TEXT,source_url TEXT,fulltext_source TEXT)")
        self.conn.execute("CREATE TABLE paper_publications(arxiv_id TEXT,revision TEXT,state TEXT,kind TEXT,"
                          "artifact_path TEXT,cache_sha256 TEXT,PRIMARY KEY(arxiv_id,revision))")
        self.conn.commit()

    def paper(self, identity="2401.12345", source="latex", status="done", abstract="  Abstract 引用\r\n", chunks=None):
        self.conn.execute("INSERT INTO papers VALUES(?,?,?,?,?,?,?,?)",
                          (identity, "Persisted " + identity, 2024, "crypto,web3", status, abstract, None, source))
        self.conn.commit()
        if chunks is None:
            chunks = [dict(chunk_index=0, text="Owned mechanismneedle \r\n", section_type="method",
                           element_type="prose", terms=["persistedterm"])]
        try:
            name = safe_id(identity)
        except ValueError:
            return identity
        (self.paths["chunks"] / (name + ".json")).write_bytes(producer.encoded(chunks))
        return identity

    def latex(self, identity="2401.12345", text=TEX, references=BIB, fulltext=True):
        name = safe_id(identity)
        path = self.paths["latex_cache"] / name / "source.tex"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        if fulltext:
            (self.paths["fulltext_cache"] / (name + ".tex")).write_bytes(text.encode("utf-8"))
        if references is not None:
            manifest = dict(version=1, source_sha256=exporter.sha(text.encode("utf-8")), files=references)
            manifest["manifest_sha256"] = exporter.sha(producer.encoded(manifest))
            path.with_name("references.json").write_bytes(producer.encoded(manifest))
        return path

    def snapshot(self, identity="2401.12345", state="published", changes=None):
        name = safe_id(identity)
        chunk_path = self.paths["chunks"] / (name + ".json")
        cache = chunk_path.read_bytes()
        chunks = json.loads(cache)
        row = self.conn.execute("SELECT title,year,layers,source_url FROM papers WHERE arxiv_id=?", (identity,)).fetchone()
        metadata = dict(zip(("title", "year", "layers", "source_url"), row))
        points = [dict(id=str(chunk["chunk_index"]), vector=[0.1], payload=dict(chunk, arxiv_id=identity)) for chunk in chunks]
        if changes:
            changes(points, metadata)
        cache_hash = exporter.sha(cache)
        revision = exporter.sha(cache_hash.encode() + producer.encoded(dict(points=points, metadata=metadata)))
        for point in points:
            point["payload"]["publication_revision"] = revision
        artifact = dict(points=points, metadata=metadata, cache_path=str(chunk_path))
        path = self.paths["publication_cache"] / (exporter.sha(identity.encode()) + "-" + revision + ".json")
        path.write_bytes(producer.encoded(artifact))
        self.conn.execute("INSERT INTO paper_publications VALUES(?,?,?,?,?,?)",
                          (identity, revision, state, "snapshot", str(path), cache_hash))
        self.conn.commit()
        return path, revision

    def section(self, identity="2401.12345", source=TEX):
        name = safe_id(identity)
        cache = (self.paths["chunks"] / (name + ".json")).read_bytes()
        chunks = json.loads(cache)
        manifest = make_section_manifest(identity, chunks, cache, source.encode(), "pylatexenc", 7)
        path = self.paths["section_cache"] / (name + ".json")
        path.write_bytes(producer.encoded(manifest))
        return path

    def run_export(self, **options):
        args = dict(db=self.db, **self.paths, output=self.output, max_sources=100, reserve_bytes=0)
        args.update(options)
        return exporter.export_packets(**args)

    def inventory(self, **options):
        return self.run_export(output=None, max_sources=None, inventory_only=True, **options)

    def packets(self):
        report = json.loads((self.output / "report.json").read_bytes())
        return list(producer.records(self.output / report["packet_file"], hashlib.sha256()))

    def test_round_trip_exact_packets_passages_foreign_context_and_read_only_inputs(self):
        self.paper()
        self.paper("2402.12345", source="latex")
        self.latex("2402.12345")
        self.snapshot("2402.12345")
        self.section("2402.12345")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        report = self.run_export()
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertTrue(report["partial"])
        self.assertEqual(report["counts"]["passage_packets"], 2)
        self.assertEqual(report["counts"]["document_packets"], 1)
        records = self.packets()
        doc = next(r for r in records if r["type"] == "document")
        self.assertEqual(doc["text"], TEX)
        self.assertEqual(doc["bibliography_files"], BIB)
        owned = next(r for r in records if r["type"] == "passage")
        self.assertEqual(owned["artifact_sha256"], exporter.sha((self.paths["chunks"] / "2401.12345.json").read_bytes()))
        self.assertEqual(owned["locator"]["artifact"], "chunk_cache/2401.12345.json#chunk_index=0/text")
        with self.assertRaisesRegex(ValueError, "input-report"):
            producer.produce(self.output / "packets.jsonl", self.root / "catalog.sqlite")
        built = producer.produce(self.output / "packets.jsonl", self.root / "catalog.sqlite",
                                 input_report=self.output / "report.json")
        self.assertTrue(built["partial"])
        result = search_catalog(self.root / "catalog.sqlite", "mechanismneedle", section_type="method",
                                element_type="prose", terms=["persistedterm"], timeout=2)
        self.assertEqual(len(result["sources"]), 2)
        result = search_catalog(self.root / "catalog.sqlite", "frobnicator", timeout=2)
        self.assertEqual(result["sources"][0]["source_id"], "2401.12345")

    def test_inventory_and_plan_measure_only_sample_without_output(self):
        for identity in ("2401.12345", "2402.12345", "2403.12345"):
            self.paper(identity, source="spec-markdown")
        sampled = self.inventory(sample_size=1)
        self.assertTrue(sampled["partial"])
        self.assertEqual(sampled["counts"]["metadata_total"], 3)
        self.assertEqual(sampled["counts"]["owned_chunks_recoverable"], 1)
        self.assertEqual(sampled["counts"]["abstracts_recoverable"], 1)
        self.assertEqual(sampled["counts"]["source_packets"], 0)
        self.assertFalse(self.output.exists())
        full = self.inventory(full_scan=True)
        self.assertFalse(full["partial"])
        self.assertEqual(full["counts"]["owned_chunks_recoverable"], 3)
        exported = self.run_export()
        self.assertEqual(full["plan"]["measured_packet_bytes"], exported["counts"]["output_bytes"])
        self.assertFalse(full["plan"]["estimate_certified"])

    def test_explicit_caps_never_claim_full_generation_or_split_a_source(self):
        for identity in ("2401.12345", "2402.12345"):
            self.paper(identity)
            self.latex(identity)
        for number, options in enumerate((dict(max_sources=1), dict(limit=1), dict(max_documents=1),
                                          dict(max_output_bytes=1))):
            self.output = self.root / ("cap" + str(number))
            report = self.run_export(**options)
            self.assertTrue(report["partial"])
            if "max_output_bytes" in options:
                self.assertEqual(self.packets(), [])
                self.assertEqual(report["stopped_by"], "max_output_bytes")
            if "max_documents" in options:
                self.assertEqual(report["counts"]["documents_omitted"], 1)
                self.assertEqual(report["counts"]["passage_packets"], 2)

    def test_missing_stale_malformed_unsafe_references_count_without_losing_owned_chunks(self):
        self.paper()
        source = self.latex()
        sidecar = source.with_name("references.json")
        valid = sidecar.read_bytes()
        manifest = json.loads(valid)
        stale = dict(manifest, source_sha256="0" * 64)
        stale.pop("manifest_sha256")
        stale["manifest_sha256"] = exporter.sha(producer.encoded(stale))
        unsafe = dict(manifest, files=[dict(path="../escape.bib", text="x")])
        unsafe.pop("manifest_sha256")
        unsafe["manifest_sha256"] = exporter.sha(producer.encoded(unsafe))
        cases = [(None, "document_no_bibliography"), (b"{", "references_invalid"),
                 (b"\xff", "references_invalid"), (producer.encoded(stale), "references_stale"),
                 (producer.encoded(unsafe), "references_invalid"),
                 (valid.replace(b'"version":1', b'"version":1,"version":1'), "references_invalid")]
        for index, (raw, code) in enumerate(cases):
            with self.subTest(code=code, index=index):
                self.output = self.root / ("refs" + str(index))
                sidecar.unlink(missing_ok=True)
                if raw is not None:
                    sidecar.write_bytes(raw)
                report = self.run_export()
                self.assertEqual(report["counts"][code], 1)
                self.assertEqual(report["counts"]["passage_packets"], 1)
                self.assertEqual(report["counts"]["latex_recoverable"], 1)
                self.assertEqual(report["counts"]["document_packets"], 0)

    def test_inline_bibliography_recovered_but_comments_and_literals_are_not_bibliography(self):
        self.paper()
        self.latex(text=INLINE, references=None)
        report = self.run_export()
        self.assertEqual(report["counts"]["bibliography_inline"], 1)
        self.assertEqual(report["counts"]["references_missing"], 1)
        for index, fake in enumerate(("% \\bibitem{k} arXiv:2401.12345\n", r"\verb|\bibitem{k} arXiv:2401.12345|")):
            self.output = self.root / ("fake" + str(index))
            self.latex(text=TEX + fake, references=None)
            self.assertEqual(self.run_export()["counts"]["document_no_bibliography"], 1)

    def test_conflicting_copies_ambiguous_citations_and_finite_artifact_bounds(self):
        self.paper()
        source = self.latex()
        (self.paths["fulltext_cache"] / "2401.12345.tex").write_bytes(b"different")
        report = self.run_export()
        self.assertEqual(report["counts"]["source_copies_ambiguous"], 1)
        self.output = self.root / "ambiguity"
        refs = [dict(path="refs.bib", text="@misc{k,doi={10.1234/a},url={https://arxiv.org/abs/2401.12345}}")]
        self.latex(references=refs)
        inventory = self.inventory()
        self.assertEqual(inventory["counts"]["citation_ambiguous_identifiers"], 1)
        report = self.run_export()
        self.assertEqual(report["citation_binding_coverage"]["diagnostics_scope"], "deferred_to_producer")
        self.assertIsNone(report["citation_binding_coverage"]["ambiguous_bindings"])
        self.assertEqual(report["counts"]["document_packets"], 1)
        self.output = self.root / "oversized"
        source.write_bytes(b"x" * (producer.MAX_LINE_BYTES + 1))
        self.assertEqual(self.run_export()["counts"]["source_bounds"], 1)

    def test_all_niches_owned_chunks_do_not_require_latex_or_bibliography(self):
        for identity, source in (("eip:4000", "spec-markdown"), ("acl:fixture", "acl-pdf"),
                                 ("iacr:2024/1", "iacr-abstract")):
            self.paper(identity, source=source)
        report = self.run_export()
        self.assertEqual(report["counts"]["passage_packets"], 3)
        self.assertEqual(report["counts"]["document_not_raw_latex"], 3)
        self.assertTrue(all(r["origin_publication_revision"] is None for r in self.packets() if r["type"] == "passage"))

    def test_done_and_snapshot_state_revision_and_chunk_mismatches_fail_closed_metadata_update_survives(self):
        self.paper("2401.12345", status="chunked")
        self.paper("2402.12345")
        self.snapshot("2402.12345", state="ready")
        self.paper("2403.12345")
        path, _ = self.snapshot("2403.12345")
        artifact = json.loads(path.read_bytes())
        artifact["points"][0]["payload"]["publication_revision"] = "0" * 64
        path.write_bytes(producer.encoded(artifact))
        self.paper("2404.12345")
        path, _ = self.snapshot("2404.12345")
        artifact = json.loads(path.read_bytes())
        artifact["points"][0]["vector"] = [0.9]
        path.write_bytes(producer.encoded(artifact))
        self.paper("2405.12345")
        self.snapshot("2405.12345")
        (self.paths["chunks"] / "2405.12345.json").write_bytes(b"[]")
        self.paper("2406.12345")
        self.snapshot("2406.12345")
        self.conn.execute("UPDATE papers SET title='drift' WHERE arxiv_id='2406.12345'")
        self.conn.commit()
        report = self.run_export()
        for code in ("source_not_done", "publication_hidden", "publication_marker_mismatch",
                     "publication_revision_mismatch", "publication_chunks_mismatch", "publication_metadata_updated"):
            self.assertEqual(report["counts"][code], 1)
        sources = [r for r in self.packets() if r["type"] == "source"]
        self.assertEqual([r["source_id"] for r in sources], ["2406.12345"])
        self.assertEqual(sources[0]["title"], "drift")

    def test_metadata_updates_use_current_target_filters_and_immutable_snapshot_revision(self):
        self.paper()
        _, revision = self.snapshot()
        self.conn.execute("UPDATE papers SET title='New title',year=2025,layers='ai' WHERE arxiv_id='2401.12345'")
        self.conn.commit()
        report = self.run_export()
        source = next(r for r in self.packets() if r["type"] == "source")
        self.assertEqual((source["title"], source["year"], source["layers"]), ("New title", 2025, ["ai"]))
        self.assertEqual(source["publication_revision"], revision)
        self.assertEqual(report["counts"]["publication_metadata_updated"], 1)
        self.assertEqual(report["counts"]["passage_packets"], 1)
        catalog = self.root / "updated.sqlite"
        producer.produce(self.output / "packets.jsonl", catalog, input_report=self.output / "report.json")
        self.assertEqual(len(search_catalog(catalog, "mechanismneedle", layer="ai", year_from=2025, timeout=2)["sources"]), 1)
        self.assertEqual(search_catalog(catalog, "mechanismneedle", layer="crypto", timeout=2)["sources"], [])

    def test_full_export_is_complete_for_eligible_sources_independently_of_missing_bibliography(self):
        self.paper()
        self.latex(references=None)
        self.paper("eip:4000", source="spec-markdown")
        self.paper("2402.12345", status="chunked")
        report = self.run_export(full_export=True, max_sources=None)
        self.assertFalse(report["partial"])
        self.assertTrue(report["complete_generation"])
        self.assertEqual(report["expected_metadata_rows"], 2)
        self.assertEqual(report["expected_all_metadata_rows"], 3)
        self.assertEqual(report["source_coverage"]["exported_sources"], 2)
        self.assertEqual(report["counts"]["passage_packets"], 2)
        self.assertEqual(report["citation_binding_coverage"]["missing_bibliography"], 1)
        self.assertFalse(report["citation_binding_coverage"]["certified"])
        built = producer.produce(self.output / "packets.jsonl", self.root / "full.sqlite", input_report=self.output / "report.json")
        self.assertFalse(built["partial"])
        with self.assertRaises(ValueError):
            self.run_export(full_export=True, output=self.root / "invalid-full")
        with self.assertRaises(ValueError):
            self.run_export(full_export=True, max_sources=None, max_documents=1, output=self.root / "invalid-doc-cap")
        self.output = self.root / "budget-exhausted"
        with self.assertRaisesRegex(OSError, "budget"):
            self.run_export(full_export=True, max_sources=None, disk_budget_bytes=1)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".source-packets-*")), [])

    def test_full_export_with_unresolved_source_artifacts_is_not_complete(self):
        self.paper()
        path, _ = self.snapshot()
        path.unlink()
        report = self.run_export(full_export=True, max_sources=None)
        self.assertTrue(report["partial"])
        self.assertFalse(report["complete_generation"])
        self.assertEqual(report["source_coverage"]["unresolved_source_rows"], 1)

    def test_sqlite_snapshot_is_read_only_and_consistent_during_external_metadata_update(self):
        self.paper()
        self.conn.execute("PRAGMA journal_mode=WAL")
        original_read = exporter.read_artifact
        changed = False
        def reading(*args):
            nonlocal changed
            if not changed:
                changed = True
                self.conn.execute("UPDATE papers SET title='Concurrent update'")
                self.conn.commit()
            return original_read(*args)
        with patch.object(exporter, "read_artifact", side_effect=reading):
            self.run_export()
        source = next(r for r in self.packets() if r["type"] == "source")
        self.assertEqual(source["title"], "Persisted 2401.12345")
        self.assertEqual(self.conn.execute("SELECT title FROM papers").fetchone()[0], "Concurrent update")

    def test_snapshot_selects_only_published_chunks_and_persisted_payload_terms(self):
        chunks = [dict(chunk_index=i, text="mechanismneedle " + str(i), section_type="method", element_type="prose") for i in range(2)]
        self.paper(chunks=chunks)
        def subset(points, metadata):
            del points[1:]
            points[0]["payload"]["terms"] = ["snapshotterm"]
        self.snapshot(changes=subset)
        report = self.run_export()
        passages = [r for r in self.packets() if r["type"] == "passage"]
        self.assertEqual(report["counts"]["passage_packets"], 1)
        self.assertEqual(passages[0]["terms"], ["snapshotterm"])
        self.assertEqual(passages[0]["chunk_index"], 0)

    def test_marked_raw_source_requires_hash_binding_but_passages_survive_missing_sections(self):
        self.paper()
        self.latex()
        self.snapshot()
        report = self.run_export(section_cache=None)
        self.assertEqual(report["counts"]["publication_source_unbound"], 1)
        self.assertEqual(report["counts"]["passage_packets"], 1)
        self.output = self.root / "stale-sections"
        self.section(source=TEX + "old")
        report = self.run_export()
        self.assertEqual(report["counts"]["publication_source_mismatch"], 1)
        self.assertEqual(report["counts"]["document_packets"], 0)

    def test_unsafe_ids_filename_collisions_and_corrupt_chunks_are_visible(self):
        self.paper("aa:b")
        self.paper("aa_b")
        self.paper("x/../escape")
        self.paper("arxiv:2401.12345v2")
        self.paper("2401.12345")
        self.paper("2402.12345")
        (self.paths["chunks"] / "2402.12345.json").write_bytes(b"[NaN]")
        report = self.run_export()
        self.assertEqual(report["counts"]["document_filename_ambiguous"], 2)
        self.assertEqual(report["counts"]["document_unsafe_id"], 1)
        self.assertEqual(report["counts"]["source_identity_ambiguous"], 2)
        self.assertEqual(report["counts"]["chunks_invalid"], 1)

    def test_symlinks_root_containment_output_guard_and_no_fallback_around_unsafe_cache(self):
        self.paper()
        source = self.latex()
        outside = self.root / "outside.tex"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        try:
            source.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        report = self.run_export()
        self.assertEqual(report["counts"]["source_unsafe_or_unreadable"], 1)
        self.assertEqual(report["counts"]["document_packets"], 0)
        with self.assertRaises(ValueError):
            self.run_export(output=self.paths["chunks"] / "export")
        with self.assertRaisesRegex(exporter.Skip, "unsafe"):
            exporter.read_artifact(outside, self.paths["latex_cache"], 10000, "source")

    def test_existing_output_disk_guard_and_atomic_race_leave_no_published_partial_files(self):
        self.paper()
        self.output.mkdir()
        sentinel = self.output / "user.txt"
        sentinel.write_bytes(b"owned")
        with self.assertRaises(FileExistsError):
            self.run_export()
        self.assertEqual(sentinel.read_bytes(), b"owned")
        self.output = self.root / "low-disk"
        usage = type("Usage", (), {"free": 1})()
        with patch.object(exporter.shutil, "disk_usage", return_value=usage), self.assertRaises(OSError):
            self.run_export(reserve_bytes=1024)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".source-packets-*")), [])
        self.output = self.root / "race"
        real_rename = exporter.rename_new
        def racing(source, target):
            target.mkdir()
            real_rename(source, target)
        with patch.object(exporter, "rename_new", side_effect=racing), self.assertRaises(OSError):
            self.run_export()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.root.glob(".source-packets-*")), [])

    def test_bounded_metadata_schema_and_cli_plan_options(self):
        self.paper()
        self.conn.execute("UPDATE papers SET abstract=?", ("x" * (exporter.MAX_METADATA_BYTES + 1),))
        self.conn.commit()
        self.assertEqual(self.inventory()["counts"]["metadata_bounds"], 1)
        arguments = ["--db", str(self.db), "--inventory", "--sample-size", "1"]
        for name, path in self.paths.items():
            arguments += ["--" + name.replace("_", "-"), str(path)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(exporter.main(arguments), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "inventory")
        with self.assertRaises(ValueError):
            self.run_export(max_sources=None)
        with self.assertRaises(ValueError):
            self.inventory(full_scan=True, limit=1)
        with self.assertRaises(ValueError):
            self.run_export(max_sources=-1)

    def test_inventory_samples_only_done_without_loading_off_niche_bodies(self):
        self.paper("0000-off-niche", status="off_niche", abstract="x" * (exporter.MAX_METADATA_BYTES + 1))
        self.paper("2401.12345", source="spec-markdown")
        report = self.inventory(sample_size=1)
        self.assertEqual(report["counts"]["metadata_total"], 2)
        self.assertEqual(report["counts"]["metadata_done_total"], 1)
        self.assertEqual(report["counts"]["metadata_rows"], 1)
        self.assertEqual(report["counts"]["owned_chunks_recoverable"], 1)
        self.assertNotIn("metadata_bounds", report["counts"])
        self.assertFalse(report["partial"])
        self.assertEqual(report["artifact_scan_predicate"], "papers.status='done'")

    def test_primary_transaction_released_before_artifacts_and_frozen_metadata_and_journal(self):
        self.paper(source="spec-markdown")
        self.conn.execute("ALTER TABLE papers ADD COLUMN matched_terms TEXT")
        self.conn.execute("UPDATE papers SET matched_terms='[\"before\"]'")
        self.conn.commit()
        _, revision = self.snapshot()
        baseline = self.run_export(full_export=True, max_sources=None)
        self.output = self.root / "frozen"
        original_chunks = exporter.chunks_for

        def mutate_primary_then_read(*args):
            with contextlib.closing(sqlite3.connect(self.db, timeout=0, isolation_level=None)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE papers SET title='Later metadata',year=2025,status='pending',abstract='Later abstract',"
                               "matched_terms='[\"later\"]'")
                writer.execute("UPDATE paper_publications SET state='pending'")
                writer.execute("COMMIT")
            return original_chunks(*args)

        with patch.object(exporter, "chunks_for", side_effect=mutate_primary_then_read):
            report = self.run_export(full_export=True, max_sources=None)
        self.assertFalse(report["partial"])
        self.assertTrue(report["metadata_snapshot"]["primary_transaction_released"])
        self.assertEqual(report["metadata_snapshot_sha256"], baseline["metadata_snapshot_sha256"])
        source = next(record for record in self.packets() if record["type"] == "source")
        self.assertEqual((source["title"], source["year"], source["status"]), ("Persisted 2401.12345", 2024, "done"))
        self.assertEqual(source["publication_revision"], revision)
        self.assertEqual(source["abstract"], "  Abstract 引用\r\n")
        self.assertEqual(source["matched_terms"], ["before"])
        self.assertFalse(report["live_visibility_verified"])
        self.assertEqual(list(self.root.glob(".source-metadata-*")), [])

    def test_same_source_matched_terms_survive_source_and_passage_filters_without_foreign_inference(self):
        chunks = [dict(chunk_index=0, text="Owned mechanismneedle", section_type="method", element_type="prose")]
        self.paper("2401.12345", chunks=chunks)
        self.paper("2402.12345", chunks=chunks)
        self.latex("2402.12345")
        self.conn.execute("ALTER TABLE papers ADD COLUMN matched_terms TEXT")
        terms = ["targetterm", *[f"term{index}" for index in range(12)], "outside_payload_cap"]
        self.conn.execute("UPDATE papers SET matched_terms=? WHERE arxiv_id='2401.12345'", (json.dumps(terms),))
        self.conn.execute("UPDATE papers SET matched_terms='[\"ownerterm\"]' WHERE arxiv_id='2402.12345'")
        self.conn.commit()
        report = self.run_export(full_export=True, max_sources=None)
        packets = self.packets()
        source = next(item for item in packets if item["type"] == "source" and item["source_id"] == "2401.12345")
        passage = next(item for item in packets if item["type"] == "passage" and item["source_id"] == "2401.12345")
        self.assertEqual(source["matched_terms"], terms)
        self.assertEqual(passage["terms"], terms[:12])
        self.assertEqual(passage["terms_provenance"], source["terms_provenance"])
        self.assertFalse(source["terms_provenance"]["publication_verified"])
        self.assertEqual(source["terms_provenance"]["metadata_snapshot_sha256"], report["metadata_snapshot_sha256"])
        self.assertEqual(report["owned_passage_coverage"]["terms_from_own_metadata"], 2)
        catalog = self.root / "terms.sqlite"
        producer.produce(self.output / "packets.jsonl", catalog, input_report=self.output / "report.json")
        self.assertEqual([s["source_id"] for s in search_catalog(catalog, "Abstract", terms=["targetterm"], timeout=2)["sources"]], ["2401.12345"])
        self.assertEqual([s["source_id"] for s in search_catalog(catalog, "mechanismneedle", terms=["targetterm"], section_type="method", timeout=2)["sources"]], ["2401.12345"])
        self.assertEqual(search_catalog(catalog, "frobnicator", terms=["ownerterm"], timeout=2)["sources"], [])
        self.assertEqual([s["source_id"] for s in search_catalog(catalog, "frobnicator", terms=["targetterm"], timeout=2)["sources"]], ["2401.12345"])
        self.assertEqual(search_catalog(catalog, "mechanismneedle", terms=["outside_payload_cap"], timeout=2)["sources"], [])

    def test_current_matched_terms_do_not_rewrite_immutable_publication_terms_or_revision(self):
        self.paper(source="spec-markdown", chunks=[dict(chunk_index=0, text="Owned mechanismneedle", section_type="method")])
        _, revision = self.snapshot(changes=lambda points, metadata: points[0]["payload"].update(terms=["publishedterm"]))
        self.conn.execute("ALTER TABLE papers ADD COLUMN matched_terms TEXT")
        self.conn.execute("UPDATE papers SET matched_terms='[\"currentterm\"]',title='Current title',year=2025")
        self.conn.commit()
        report = self.run_export(full_export=True, max_sources=None)
        self.assertFalse(report["partial"])
        self.assertEqual(report["counts"]["publication_metadata_updated"], 1)
        self.assertEqual(report["counts"]["publication_terms_metadata_updated"], 1)
        source = next(item for item in self.packets() if item["type"] == "source")
        passage = next(item for item in self.packets() if item["type"] == "passage")
        self.assertEqual((source["title"], source["year"], source["matched_terms"]), ("Current title", 2025, ["currentterm"]))
        self.assertEqual(source["publication_revision"], revision)
        self.assertEqual(passage["terms"], ["publishedterm"])
        self.assertEqual(passage["terms_provenance"]["kind"], "publication_snapshot_payload")
        catalog = self.root / "publication-terms.sqlite"
        producer.produce(self.output / "packets.jsonl", catalog, input_report=self.output / "report.json")
        self.assertEqual(len(search_catalog(catalog, "Abstract", terms=["currentterm"], timeout=2)["sources"]), 1)
        self.assertEqual(len(search_catalog(catalog, "mechanismneedle", section_type="method", terms=["publishedterm"], year_from=2025, timeout=2)["sources"]), 1)
        self.assertEqual(search_catalog(catalog, "mechanismneedle", section_type="method", terms=["currentterm"], timeout=2)["sources"], [])

    def test_optional_missing_invalid_and_oversized_matched_terms_are_diagnostics_not_fake_facets(self):
        self.paper(source="spec-markdown", chunks=[dict(chunk_index=0, text="Owned mechanismneedle", section_type="method")])
        report = self.run_export(full_export=True, max_sources=None)
        self.assertEqual(report["counts"]["matched_terms_column_missing"], 1)
        self.assertEqual(next(item for item in self.packets() if item["type"] == "source")["matched_terms"], [])
        self.assertNotIn("terms", next(item for item in self.packets() if item["type"] == "passage"))
        self.conn.execute("ALTER TABLE papers ADD COLUMN matched_terms TEXT")
        for index, (value, reason) in enumerate(((None, "null"), ("not JSON", "invalid"),
                                               ('["foreign",2]', "invalid"), ("x" * 65537, "bounds"))):
            with self.subTest(reason=reason):
                self.output = self.root / f"missing-terms-{index}"
                self.conn.execute("UPDATE papers SET matched_terms=?", (value,))
                self.conn.commit()
                report = self.run_export(full_export=True, max_sources=None)
                self.assertFalse(report["partial"])
                self.assertEqual(report["counts"]["matched_terms_" + reason], 1)
                self.assertEqual(report["owned_passage_coverage"]["terms_unavailable"], 1)
                source = next(item for item in self.packets() if item["type"] == "source")
                self.assertEqual(source["matched_terms"], [])
                self.assertEqual(source["terms_provenance"]["reason"], reason)

    def test_export_defers_citation_parsing_to_producer_but_inventory_parses(self):
        self.paper()
        self.latex()
        with patch.object(exporter, "extract_citation_contexts", side_effect=AssertionError("export reparses")):
            report = self.run_export(full_export=True, max_sources=None)
        self.assertIsNone(report["citation_binding_coverage"]["unresolved_mentions"])
        with patch.object(producer, "extract_citation_contexts", wraps=producer.extract_citation_contexts) as parse:
            producer.produce(self.output / "packets.jsonl", self.root / "parsed.sqlite",
                             input_report=self.output / "report.json")
        self.assertEqual(parse.call_count, 1)
        with patch.object(exporter, "extract_citation_contexts", wraps=exporter.extract_citation_contexts) as parse:
            inventory = self.inventory()
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(inventory["citation_binding_coverage"]["diagnostics_scope"], "inventory_parsed")

    def test_frozen_journal_preserves_oversized_diagnostic_not_unpublished_exclusion(self):
        self.paper(source="spec-markdown")
        self.snapshot()
        self.conn.execute("UPDATE paper_publications SET artifact_path=?", ("x" * 17000,))
        self.conn.commit()
        report = self.run_export(full_export=True, max_sources=None)
        self.assertTrue(report["partial"])
        self.assertFalse(report["complete_generation"])
        self.assertEqual(report["counts"]["publication_journal_bounds"], 1)
        self.assertEqual(report["source_coverage"]["excluded_unpublished"], 0)

    def test_progress_counts_phase_timings_and_explicit_spool_cleanup(self):
        rows = [(f"spec:{index}", "Persisted", 2024, "crypto", "done", "Abstract", None, "spec-markdown")
                for index in range(1001)]
        self.conn.executemany("INSERT INTO papers VALUES(?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        spool_root = self.root / "spool"
        spool_root.mkdir()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(exporter, "chunks_for", side_effect=exporter.Skip("chunks_missing")):
            report = self.run_export(full_export=True, max_sources=None, spool_dir=spool_root)
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertTrue(any(event["phase"] == "artifacts" and event["rows"] == 1000 for event in events))
        self.assertTrue(any(event["phase"] == "primary_released" for event in events))
        self.assertEqual(events[-1]["phase"], "ready")
        self.assertTrue(all("text" not in event and "source_id" not in event for event in events))
        self.assertEqual(set(report["phase_elapsed"]), {"snapshot_seconds", "artifact_seconds", "finalization_seconds", "total_seconds"})
        self.assertGreater(report["metadata_snapshot"]["bytes"], 0)
        self.assertEqual(list(spool_root.iterdir()), [])

    def test_legacy_owned_chunks_reuse_existing_policy_without_boilerplate(self):
        chunks = [dict(chunk_index=0, text="We present a mechanismneedle and show how the method works.", section_type="method", element_type="prose"),
                  dict(chunk_index=1, text="Caption only", section_type="appendix", element_type="figure"),
                  dict(chunk_index=2, text="References to papers", section_title="References", section_type="other", element_type="prose")]
        self.paper(chunks=chunks)
        report = self.run_export()
        passages = [r for r in self.packets() if r["type"] == "passage"]
        self.assertEqual([r["chunk_index"] for r in passages], [0])
        self.assertEqual(report["counts"]["legacy_owned_chunks_policy_skipped"], 2)
        self.assertNotIn("terms", passages[0])
        self.assertFalse(report["legacy_owned_policy"]["publication_certified"])

    def test_gzip_direct_output_matches_decoded_sha_inventory_measurement_and_producer(self):
        self.paper()
        self.paper("2402.12345")
        self.latex("2402.12345")
        inventory = self.inventory(full_scan=True, gzip=True, gzip_level=1)
        self.assertFalse(self.output.exists())
        report = self.run_export(full_export=True, max_sources=None, gzip=True, gzip_level=1)
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["packets.jsonl.gz", "report.json"])
        path = self.output / "packets.jsonl.gz"
        decoded = gzip.decompress(path.read_bytes())
        self.assertEqual(exporter.sha(decoded), report["packets_sha256"])
        self.assertEqual(len(decoded), report["counts"]["output_bytes"])
        self.assertEqual(path.stat().st_size, report["counts"]["output_storage_bytes"])
        self.assertEqual(inventory["plan"]["measured_storage_bytes"], path.stat().st_size)
        catalog = self.root / "gzip.sqlite"
        built = producer.produce(path, catalog, input_report=self.output / "report.json")
        self.assertFalse(built["partial"])
        self.assertEqual(built["counts"]["own_passage"], 2)
        self.assertEqual(built["counts"]["citation_context"], 1)
        self.output = self.root / "gzip-budget"
        with self.assertRaisesRegex(OSError, "budget"):
            self.run_export(full_export=True, max_sources=None, gzip=True, disk_budget_bytes=1)
        self.assertFalse(self.output.exists())

    def durable_export(self, work, **options):
        return self.run_export(full_export=True, max_sources=None, gzip=True,
                               work_dir=work, checkpoint_rows=1, **options)

    def interrupt_second_source(self, work):
        original = exporter.chunks_for
        calls = []

        def interrupt(*args):
            calls.append(args)
            if len(calls) == 2:
                raise exporter.ExportInterrupted()
            return original(*args)

        with patch.object(exporter, "chunks_for", side_effect=interrupt):
            with self.assertRaises(exporter.ExportInterrupted):
                self.durable_export(work)
        self.assertFalse(self.output.exists())
        state = json.loads((work / "checkpoint.json").read_bytes())["payload"]
        self.assertEqual(state["last_row"], 1)
        return state

    def test_durable_resume_uses_frozen_metadata_and_removes_only_uncommitted_tail(self):
        for identity in ("2401.12345", "2402.12345", "2403.12345"):
            self.paper(identity)
        self.latex("2402.12345")
        baseline = self.run_export(full_export=True, max_sources=None, gzip=True)
        expected = gzip.decompress((self.output / "packets.jsonl.gz").read_bytes())
        self.output = self.root / "resumed"
        work = self.root / "durable"
        state = self.interrupt_second_source(work)
        packet_path = work / "payload" / "packets.jsonl.gz"
        with packet_path.open("ab") as tail:
            tail.write(b"uncommitted interrupted bytes")
        self.conn.execute("UPDATE papers SET title='Later metadata',status='pending'")
        self.conn.commit()
        with patch.object(exporter, "freeze_inputs", side_effect=AssertionError("must not resnapshot")):
            report = self.durable_export(work, resume=True)
        self.assertTrue(report["complete_generation"])
        self.assertEqual(gzip.decompress((self.output / "packets.jsonl.gz").read_bytes()), expected)
        for key in ("packets_sha256", "metadata_snapshot_sha256", "artifact_facts_sha256"):
            self.assertEqual(report[key], baseline[key])
        self.assertEqual(report["counts"]["metadata_rows"], 3)
        self.assertEqual(report["counts"]["source_packets"], 3)
        self.assertGreater((self.output / "packets.jsonl.gz").stat().st_size, state["storage_offset"])
        self.assertEqual(self.conn.execute("SELECT status FROM papers LIMIT 1").fetchone()[0], "pending")

    def test_durable_deadline_pauses_at_whole_source_then_resumes(self):
        self.paper()
        self.paper("2402.12345")
        work = self.root / "deadline"
        elapsed = [0.0]
        original = exporter.chunks_for

        def read_then_expire(*args):
            result = original(*args)
            elapsed[0] = 2.0
            return result

        with patch.object(exporter.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(exporter, "chunks_for", side_effect=read_then_expire):
            report = self.durable_export(work, max_seconds=1)
        self.assertEqual(report["status"], "paused")
        self.assertFalse(report["complete_generation"])
        self.assertEqual(report["committed_rows"], 1)
        self.assertFalse(self.output.exists())
        self.assertTrue(self.durable_export(work, resume=True)["complete_generation"])

    def test_durable_resume_rejects_changed_prefix_snapshot_and_options_without_truncation(self):
        for case in ("prefix", "snapshot", "options", "runtime"):
            with self.subTest(case=case):
                if case == "prefix":
                    self.paper()
                    self.paper("2402.12345")
                self.output = self.root / ("rejected-" + case)
                work = self.root / ("work-" + case)
                self.interrupt_second_source(work)
                path = work / "payload" / "packets.jsonl.gz"
                if case == "prefix":
                    raw = bytearray(path.read_bytes())
                    raw[10] ^= 1
                    path.write_bytes(raw)
                elif case == "snapshot":
                    with (work / "snapshot.sqlite").open("ab") as stream:
                        stream.write(b"changed")
                before = path.read_bytes()
                context = (patch.object(exporter, "runtime_identity", return_value={}) if case == "runtime"
                           else contextlib.nullcontext())
                with context, self.assertRaisesRegex(ValueError, "hash|identity"):
                    self.durable_export(work, resume=True, **({"gzip_level": 1} if case == "options" else {}))
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(self.output.exists())

    def test_durable_publish_retry_does_not_duplicate_packets_or_append_empty_members(self):
        self.paper()
        work = self.root / "publish"
        with patch.object(exporter, "rename_new", side_effect=OSError("interrupted publication")):
            with self.assertRaisesRegex(OSError, "interrupted publication"):
                self.durable_export(work)
        path = work / "payload" / "packets.jsonl.gz"
        before = path.read_bytes()
        saved_report = json.loads((work / "payload" / "report.json").read_bytes())
        result = self.durable_export(work, resume=True)
        self.assertEqual(result, saved_report)
        self.assertEqual((self.output / "packets.jsonl.gz").read_bytes(), before)
        self.assertEqual(result["counts"]["source_packets"], 1)

    def test_durable_work_directory_lock_rejects_a_second_writer(self):
        work = self.root / "locked"
        with exporter.work_lock(work, False):
            with self.assertRaisesRegex(OSError, "locked"):
                with exporter.work_lock(work, True):
                    self.fail("second writer acquired the lock")

    def test_durable_report_interruption_leaves_no_partial_final_report_and_can_resume(self):
        self.paper()
        work = self.root / "report-interruption"
        original = exporter.os.replace

        def interrupt_report(source, destination):
            if Path(destination).name == "report.json":
                raise exporter.ExportInterrupted()
            return original(source, destination)

        with patch.object(exporter.os, "replace", side_effect=interrupt_report):
            with self.assertRaises(exporter.ExportInterrupted):
                self.durable_export(work)
        self.assertFalse((work / "payload" / "report.json").exists())
        self.assertFalse(self.output.exists())
        self.assertTrue(self.durable_export(work, resume=True)["complete_generation"])

    def test_signal_after_atomic_publication_returns_owned_result_and_resume_is_idempotent(self):
        self.paper()
        work = self.root / "rename-interruption"
        original = exporter.rename_new

        def rename_then_interrupt(source, destination):
            original(source, destination)
            raise exporter.ExportInterrupted()

        with patch.object(exporter, "rename_new", side_effect=rename_then_interrupt):
            report = self.durable_export(work)
        self.assertTrue(report["complete_generation"])
        self.assertEqual(report, self.durable_export(work, resume=True))
        self.assertEqual(report, exporter.published_report(work, self.output))
        packet_path = self.output / "packets.jsonl.gz"
        original_packet = packet_path.read_bytes()
        altered = bytearray(original_packet)
        altered[10] ^= 1
        packet_path.write_bytes(altered)
        with self.assertRaisesRegex(ValueError, "hash.*owned checkpoint"):
            self.durable_export(work, resume=True)
        packet_path.write_bytes(original_packet)
        with (self.output / "packets.jsonl.gz").open("ab") as stream:
            stream.write(b"foreign tail")
        with self.assertRaisesRegex(ValueError, "owned checkpoint"):
            self.durable_export(work, resume=True)

    def test_optional_url_and_title_formatting_do_not_discard_valid_sources_or_change_inputs(self):
        self.paper()
        self.paper("2402.12345")
        self.conn.execute("UPDATE papers SET title='  Technical\n  title\t ',source_url='https://example.org/bad path'")
        self.conn.execute("UPDATE papers SET source_url='https://user:password@example.org/paper' WHERE arxiv_id='2402.12345'")
        self.conn.commit()
        before = self.db.read_bytes()
        report = self.run_export(full_export=True, max_sources=None)
        sources = [item for item in self.packets() if item["type"] == "source"]
        self.assertTrue(report["complete_generation"])
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(item["title"] == "Technical title" and item["source_url"] is None for item in sources))
        self.assertEqual(report["counts"]["metadata_title_whitespace_normalized"], 2)
        self.assertEqual(report["counts"]["invalid_source_url_omitted"], 2)
        self.assertNotIn(b"user:password", (self.output / "packets.jsonl").read_bytes())
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
