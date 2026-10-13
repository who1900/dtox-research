"""Offline functional boundary tests, not semantic-quality or live-publication gates."""
import contextlib
import hashlib
import gzip
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from api.source_representations import search_catalog, catalog_coverage
from ops import build_source_representations as producer


def source(source_id, **extra):
    return dict(type="source", source_id=source_id, title="Explicit " + source_id, year=2024,
                layers=["crypto"], status="done", publication_revision="a" * 64,
                abstract="Author abstract without the discovery term. 引用\r\n", **extra)


def document(source_id="2402.12345", **extra):
    result = dict(type="document", source_id=source_id, text="Heading\r\n\r\n  Foreign frobnicator \\cite{k}. \r\n\r\nTail",
                  bibliography_files=[dict(path="refs.bib", text="@misc{k,url={https://arxiv.org/abs/2401.12345v2}}")],
                  origin_publication_revision="a" * 64)
    return dict(result, **extra)


def passage(source_id="2402.12345", **extra):
    text = "Owned mechanismneedle 引用\r\n"
    result = dict(type="passage", source_id=source_id, origin_id=source_id,
                  text=text, artifact_sha256="c" * 64, chunk_index=3,
                  locator=dict(start=0, end=len(text), unit="unicode_codepoint", artifact="chunk_cache/fixture.json#chunk_index=3/text"),
                  origin_publication_revision="a" * 64, section_type="method", element_type="equation", terms=["persisted"])
    return dict(result, **extra)


class ProducerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "packets.jsonl"
        self.output = self.root / "catalog.sqlite"
        self.target = source("arxiv:2401.12345v2")
        self.owner = source("2402.12345")
        self.doc = document()

    def packets(self, records=None):
        raw = b"".join(producer.encoded(record) + b"\n" for record in
                       (records if records is not None else [self.doc, self.target, self.owner]))
        self.input.write_bytes(raw)
        return raw

    def build(self, records=None, limit=None):
        self.packets(records)
        return producer.produce(self.input, self.output, limit)

    def cli(self, arguments):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = producer.main(arguments)
        return code, json.loads(stdout.getvalue())

    def test_build_then_find_foreign_description_and_keep_owner_and_raw_spans(self):
        report = self.build()
        self.assertEqual(report["input_sha256"], hashlib.sha256(self.input.read_bytes()).hexdigest())
        result = search_catalog(self.output, "frobnicator", timeout=2)
        self.assertEqual(result["status"], "ready")
        self.assertEqual([r["source_id"] for r in result["sources"]], ["2401.12345"])
        found = result["sources"][0]
        self.assertIsNone(found["source_url"])
        self.assertEqual(found["evidence_state"], "not_read")
        foreign = next(r for r in found["representations"] if r["kind"] == "citation_context")
        own = next(r for r in found["representations"] if r["kind"] == "own_summary")
        self.assertEqual(foreign["origin_source_id"], "2402.12345")
        self.assertEqual(foreign["origin_revision"], hashlib.sha256(self.doc["text"].encode()).hexdigest())
        self.assertEqual(foreign["text"], self.doc["text"][foreign["locator"]["start"]:foreign["locator"]["end"]])
        self.assertEqual(foreign["binding"], {"exact_key": "k", "identifier": "2401.12345"})
        self.assertEqual(own["text"], self.target["abstract"])
        self.assertNotIn("frobnicator", own["text"])
        self.assertEqual(own["origin_revision"], hashlib.sha256(self.target["abstract"].encode()).hexdigest())
        self.assertEqual(own["locator"], dict(start=0, end=len(own["text"]), unit="unicode_codepoint", artifact="metadata_abstract"))
        provenance = report["diagnostics"]["bindings"]["samples"][0]["provenance"][0]
        self.assertEqual(provenance["requested_id"], "arxiv:2401.12345v2")
        self.assertEqual(provenance["identifier_raw"], "https://arxiv.org/abs/2401.12345v2")
        self.assertTrue(all(r["evidence_state"] == "not_read" for r in found["representations"]))

    def test_unknown_target_and_unknown_key_do_not_invent_metadata(self):
        unknown = document(text=r"Frobnicator \cite{k,missing}.")
        report = self.build([unknown, self.owner])
        self.assertEqual(report["counts"]["missing_metadata"], 1)
        self.assertEqual(report["diagnostics"]["extractor"]["unknown_reference"], 1)
        self.assertEqual(report["counts"]["citation_context"], 0)
        self.assertEqual(search_catalog(self.output, "frobnicator", timeout=2)["sources"], [])
        with contextlib.closing(sqlite3.connect(self.output.as_uri() + "?mode=ro", uri=True)) as conn:
            self.assertEqual(conn.execute("SELECT source_id FROM sources").fetchall(), [("2402.12345",)])

    def test_explicit_alias_only_and_multiple_identifiers_remain_ambiguous(self):
        self.target["aliases"] = ["doi:10.1234/verified"]
        bib = "@misc{k,doi={10.1234/verified}} @misc{near,doi={10.1234/unverified}}"
        doc = document(text=r"Frobnicator \cite{k,near}.", bibliography_files=[dict(path="refs.bib", text=bib)])
        report = self.build([self.target, self.owner, doc])
        self.assertEqual(report["counts"]["citation_context"], 1)
        self.assertEqual(report["counts"]["missing_metadata"], 1)
        self.assertEqual(report["diagnostics"]["bindings"]["samples"][0]["provenance"][0]["requested_id"], "doi:10.1234/verified")
        self.output = self.root / "ambiguous.sqlite"
        doc["bibliography_files"][0]["text"] = "@misc{k,doi={10.1234/verified},url={https://arxiv.org/abs/2401.12345}}"
        report = self.build([doc, self.target, self.owner])
        self.assertEqual(report["counts"]["citation_context"], 0)
        self.assertEqual(report["diagnostics"]["extractor"]["ambiguous_identifiers"], 1)

    def test_hidden_sources_unknown_origin_and_publication_mismatch_are_counted(self):
        hidden = dict(self.target, status="chunked")
        report = self.build([hidden, self.owner, self.doc, document("2403.12345")])
        self.assertEqual(report["counts"]["skipped_source_status"], 1)
        self.assertEqual(report["counts"]["skipped_target_status"], 1)
        self.assertEqual(report["counts"]["missing_metadata"], 1)
        self.output = self.root / "mismatch.sqlite"
        report = self.build([self.target, self.owner, document(origin_publication_revision="b" * 64)])
        self.assertEqual(report["counts"]["publication_mismatch"], 1)
        self.assertEqual(report["counts"]["documents_extracted"], 0)
        self.output = self.root / "hidden-owner.sqlite"
        report = self.build([self.target, dict(self.owner, status="pending"), self.doc])
        self.assertEqual(report["counts"]["skipped_document_status"], 1)

    def test_null_publication_and_full_abstract_are_not_rewritten(self):
        target = dict(self.target, publication_revision=None, abstract="  full\n\nabstract\t ")
        owner = dict(self.owner, publication_revision=None)
        report = self.build([document(origin_publication_revision=None), owner, target])
        self.assertEqual(report["counts"]["citation_context"], 1)
        found = search_catalog(self.output, "frobnicator", timeout=2)["sources"][0]
        self.assertEqual(found["publication_state"], "legacy_unmarked")
        self.assertEqual(next(r["text"] for r in found["representations"] if r["kind"] == "own_summary"), target["abstract"])

    def test_identical_duplicates_and_explicit_document_subset(self):
        report = self.build([self.target, self.owner, self.doc, self.doc,
                             dict(self.target, source_id="2401.12345"), document("2404.12345")], limit=1)
        self.assertEqual(report["counts"]["duplicate_sources"], 1)
        self.assertEqual(report["counts"]["duplicate_documents"], 1)
        self.assertEqual(report["counts"]["documents_selected"], 1)
        self.assertEqual(report["counts"]["documents_omitted"], 1)
        self.assertTrue(report["partial"])
        self.assertEqual(report["coverage"], "explicit_input_only")

    def test_conflicting_sources_documents_aliases_and_bibliography_fail_without_output(self):
        cases = [[self.target, dict(self.target, title="conflict")],
                 [self.target, self.owner, self.doc, dict(self.doc, text="conflict")],
                 [dict(self.target, aliases=["doi:10.1234/a"]), dict(self.owner, aliases=["doi:10.1234/a"])],
                 [self.target, self.owner, document(bibliography_files=[dict(path="x.bib", text="a"), dict(path="x.bib", text="b")])]]
        for records in cases:
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.build(records, limit=1)
            self.assertFalse(self.output.exists())
            self.assertEqual(list(self.root.glob(".source-representations-*")), [])

    def test_input_changes_after_ingest_prevent_publication(self):
        self.packets()
        real_records = producer.records

        def changing(path, digest, **kwargs):
            yield from real_records(path, digest, **kwargs)
            self.packets([self.target, self.owner, dict(self.doc, text="Changed \\cite{k}.")])

        with patch.object(producer, "records", side_effect=changing), self.assertRaisesRegex(ValueError, "input changed"):
            producer.produce(self.input, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".source-representations-*")), [])

    def test_line_bounds_utf8_duplicate_json_keys_and_bad_url_are_errors(self):
        for raw in (b"x" * (producer.MAX_LINE_BYTES + 1), b"\xff\n",
                    b'{"type":"source","type":"document"}\n', producer.encoded(dict(self.target, source_url="file:///x")) + b"\n"):
            self.input.write_bytes(raw)
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                producer.produce(self.input, self.output)
            self.assertFalse(self.output.exists())

    def test_generation_and_representation_ids_are_deterministic(self):
        first = self.build()
        second_path = self.root / "second.sqlite"
        second = producer.produce(self.input, second_path)
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(first["diagnostics"]["bindings"], second["diagnostics"]["bindings"])
        third_path = self.root / "reordered.sqlite"
        self.packets([self.owner, self.target, self.doc])
        third = producer.produce(self.input, third_path)
        self.assertEqual(first["diagnostics"]["bindings"], third["diagnostics"]["bindings"])

    def test_complete_coverage_requires_all_explicit_full_export_flags_and_no_producer_limit(self):
        raw = self.packets()
        report_path = self.root / "report.json"
        full = dict(schema=1, mode="export", partial=False, full_export=True, complete_generation=True,
                    packets_sha256=hashlib.sha256(raw).hexdigest(), coverage="db_addressed_artifacts_only")
        cases = [(full, None, True), ({**full, "partial": True}, None, False),
                 ({**full, "full_export": False}, None, False),
                 ({**full, "complete_generation": False}, None, False),
                 ({**full, "full_export": 1}, None, False),
                 ({key: value for key, value in full.items() if key != "complete_generation"}, None, False),
                 (full, 1, False), (None, None, False)]
        generations = []
        for index, (export_report, limit, expected_complete) in enumerate(cases):
            with self.subTest(case=index):
                if export_report is not None:
                    report_path.write_bytes(producer.encoded(export_report) + b"\n")
                else:
                    report_path.unlink(missing_ok=True)
                output = self.root / f"coverage-{index}.sqlite"
                built = producer.produce(self.input, output, max_documents=limit,
                                         input_report=report_path if export_report is not None else None)
                coverage = catalog_coverage(output)
                self.assertEqual(built["catalog_coverage"], coverage)
                self.assertEqual(built["complete_generation"], expected_complete)
                self.assertEqual(built["partial"], not expected_complete)
                self.assertEqual(coverage["scope"], "full_snapshot" if expected_complete else "explicit_input_only")
                self.assertEqual(coverage["input_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(coverage["export_report_sha256"], hashlib.sha256(report_path.read_bytes()).hexdigest()
                                 if export_report is not None else None)
                generations.append(built["generation"])
        self.assertNotEqual(generations[0], generations[1])

    def test_full_report_cannot_hide_skipped_owned_packets(self):
        for index, extra in enumerate((passage(origin_publication_revision="b" * 64), passage("missing-owner"))):
            with self.subTest(case=index):
                raw = self.packets([self.target, self.owner, self.doc, extra])
                report_path = self.root / "report.json"
                report_path.write_bytes(producer.encoded(dict(schema=1, mode="export", partial=False,
                    full_export=True, complete_generation=True, packets_sha256=hashlib.sha256(raw).hexdigest())))
                built = producer.produce(self.input, self.root / f"skipped-{index}.sqlite", input_report=report_path)
                self.assertTrue(built["partial"])
                self.assertEqual(built["catalog_coverage"]["scope"], "explicit_input_only")

    def test_outside_corpus_citation_target_is_not_owned_packet_coverage_loss(self):
        raw = self.packets([self.owner, document(bibliography_files=[
            dict(path="refs.bib", text="@misc{k,url={https://arxiv.org/abs/2409.12345}}")])])
        report_path = self.root / "report.json"
        report_path.write_bytes(producer.encoded(dict(schema=1, mode="export", partial=False,
            full_export=True, complete_generation=True, packets_sha256=hashlib.sha256(raw).hexdigest())))
        built = producer.produce(self.input, self.output, input_report=report_path)
        self.assertEqual(built["counts"]["missing_metadata"], 1)
        self.assertEqual(built["counts"]["skipped_owner_metadata"], 0)
        self.assertFalse(built["partial"])
        self.assertEqual(catalog_coverage(self.output)["scope"], "full_snapshot")

    def test_own_terms_annotation_requires_same_source_frozen_values_and_no_publication_claim(self):
        annotation = dict(kind="frozen_papers_matched_terms", source_id="2402.12345",
                          metadata_snapshot_sha256="d" * 64, payload_limit=12, publication_verified=False)
        owner = dict(self.owner, matched_terms=["ownedterm"], terms_provenance=annotation)
        for index, invalid in enumerate((
                passage(terms=["foreignterm"], terms_provenance=annotation),
                passage(terms=["ownedterm"], terms_provenance={**annotation, "metadata_snapshot_sha256": "e" * 64}),
                passage(terms=["ownedterm"], terms_provenance={**annotation, "source_id": "2401.12345"}),
                passage(terms=["ownedterm"], terms_provenance={**annotation, "publication_verified": True}))):
            with self.subTest(case=index), self.assertRaises(ValueError):
                self.packets([owner, invalid])
                producer.produce(self.input, self.root / f"invalid-annotation-{index}.sqlite")
            self.assertFalse((self.root / f"invalid-annotation-{index}.sqlite").exists())
        self.packets([owner, passage(terms=["ownedterm"], terms_provenance=annotation)])
        report = producer.produce(self.input, self.output)
        self.assertEqual(report["counts"]["own_passage_terms_frozen_papers_matched_terms"], 1)
        self.assertEqual(len(search_catalog(self.output, "mechanismneedle", terms=["ownedterm"], section_type="method", timeout=2)["sources"]), 1)

    def test_repeated_citation_key_is_counted_without_duplicate_index_row(self):
        report = self.build([self.target, self.owner, document(text=r"Foreign frobnicator \cite{k,k}.")])
        self.assertEqual(report["counts"]["citation_context"], 1)
        self.assertEqual(report["counts"]["duplicate_context_targets"], 1)
        self.assertEqual(report["diagnostics"]["bindings"]["total_count"], 1)

    def test_same_target_different_exact_keys_keep_first_binding_once(self):
        self.target["aliases"] = ["doi:10.1234/verified"]
        bib = "@misc{z,url={https://arxiv.org/abs/2401.12345}} @misc{a,doi={10.1234/verified}}"
        doc = document(text=r"Foreign frobnicator \cite{z,a,z}.", bibliography_files=[dict(path="refs.bib", text=bib)])
        report = self.build([self.target, self.owner, doc])
        self.assertEqual(report["counts"]["duplicate_context_targets"], 2)
        foreign = [r for r in search_catalog(self.output, "frobnicator", timeout=2)["sources"][0]["representations"]
                   if r["kind"] == "citation_context"]
        self.assertEqual(len(foreign), 1)
        self.assertEqual(foreign[0]["binding"]["exact_key"], "z")

    def test_diagnostics_are_bounded_without_clipping_search_contexts(self):
        text = "\n\n".join(f"Foreign probe{i} \\cite{{k}}." for i in range(40))
        bib = "@misc{k,url={https://arxiv.org/abs/2401.12345},note={ENTRY_ONLY_" + "b" * 50000 + "}}"
        report = self.build([self.target, self.owner, document(text=text, bibliography_files=[dict(path="refs.bib", text=bib)])])
        bindings = report["diagnostics"]["bindings"]
        self.assertEqual(len(bindings["samples"]), 32)
        self.assertEqual(bindings["total_count"], 40)
        self.assertTrue(bindings["truncated"])
        self.assertNotIn("entry_raw", producer.encoded(bindings).decode())
        self.assertNotIn("ENTRY_ONLY_", producer.encoded(bindings).decode())
        self.assertLess(len(producer.encoded(bindings)), 50000)
        self.assertEqual(report["counts"]["citation_context"], 40)
        self.assertEqual(search_catalog(self.output, "probe39", timeout=2)["sources"][0]["source_id"], "2401.12345")
        self.assertTrue(report["partial"])

    def test_public_search_text_excludes_comments_and_literals_but_keeps_raw_owner(self):
        text = "Foreign frobnicator \\cite{k}. % commentneedle\n" + r"\verb|literalneedle| \begin{verbatim}envneedle\end{verbatim}"
        doc = document(text=text)
        self.build([doc, self.target, self.owner])
        context = producer.extract_citation_contexts(text, [(f["path"], f["text"]) for f in doc["bibliography_files"]])["contexts"][0]
        self.assertEqual(context["raw"], text[context["start"]:context["end"]])
        found = search_catalog(self.output, "frobnicator", timeout=2)["sources"][0]
        foreign = next(r for r in found["representations"] if r["kind"] == "citation_context")
        self.assertEqual(foreign["text"], context["search_text"])
        self.assertEqual(foreign["origin_revision"], hashlib.sha256(text.encode()).hexdigest())
        for needle in ("commentneedle", "literalneedle", "envneedle"):
            self.assertEqual(search_catalog(self.output, needle, timeout=2)["sources"], [])

    def test_opaque_doi_parentheses_are_exact_not_fuzzy_equivalence(self):
        doi = "doi:10.1234/a(2024)"
        target = dict(self.target, source_id=doi)
        bib = "@misc{k,doi={10.1234/a(2024)}} @misc{near,doi={10.1234/a2024}}"
        report = self.build([target, self.owner, document(text=r"Foreign frobnicator \cite{k,near}.",
                            bibliography_files=[dict(path="refs.bib", text=bib)])])
        self.assertEqual(producer.canonical_id(doi), doi)
        self.assertEqual(report["counts"]["missing_metadata"], 1)
        self.assertEqual(search_catalog(self.output, "frobnicator", timeout=2)["sources"][0]["source_id"], doi)

    def test_canonical_arxiv_is_standard_only_and_oversized_abstract_is_not_truncated(self):
        self.assertEqual(producer.canonical_id("arxiv:math.GT/0301001v2"), "math.gt/0301001")
        self.assertEqual(producer.canonical_id("doi:10.1234/EXACT"), "doi:10.1234/EXACT")
        with self.assertRaises(ValueError):
            self.build([dict(self.target, abstract="x" * 65537), self.owner, self.doc])
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".source-representations-*")), [])

    def test_cli_build_query_query_only_and_existing_output_untouched(self):
        self.packets()
        script = Path(producer.__file__).resolve()
        result = subprocess.run([sys.executable, "-B", str(script), "--input", str(self.input),
                                 "--output", str(self.output), "--query", "frobnicator"],
                                cwd=self.root, capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["search"]["sources"][0]["source_id"], "2401.12345")
        before = self.output.read_bytes()
        code, report = self.cli(["--query-only", str(self.output), "--query", "frobnicator"])
        self.assertEqual(code, 0)
        self.assertEqual(report["sources"][0]["source_id"], "2401.12345")
        code, report = self.cli(["--input", str(self.input), "--output", str(self.output)])
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "error")
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["catalog.sqlite", "packets.jsonl"])

    def test_passage_stream_preserves_exact_artifact_locator_tags_and_origin(self):
        owned = passage()
        report = self.build([owned, self.owner, self.target, self.doc, owned])
        self.assertEqual(report["counts"]["own_passage"], 1)
        self.assertEqual(report["counts"]["duplicate_passages"], 1)
        result = search_catalog(self.output, "mechanismneedle", section_type="method", element_type="equation",
                                terms=["persisted"], timeout=2)
        representation = next(r for r in result["sources"][0]["representations"] if r["kind"] == "own_passage")
        self.assertEqual(representation["text"], owned["text"])
        self.assertEqual(representation["origin_revision"], owned["artifact_sha256"])
        self.assertEqual(representation["locator"], owned["locator"])
        self.assertEqual(representation["origin_source_id"], self.owner["source_id"])
        self.assertEqual(list(self.root.glob(".source-passages-*")), [])

    def test_passage_unknown_hidden_revision_mismatch_and_legacy_counts(self):
        report = self.build([self.owner, dict(self.target, status="pending"), passage(),
                             passage(self.target["source_id"]), passage("2404.12345"),
                             passage(chunk_index=4, origin_publication_revision="b" * 64)])
        self.assertEqual(report["counts"]["own_passage"], 1)
        self.assertEqual(report["counts"]["skipped_passage_status"], 1)
        self.assertEqual(report["counts"]["missing_metadata"], 1)
        self.assertEqual(report["counts"]["publication_mismatch"], 1)
        self.output = self.root / "legacy.sqlite"
        self.build([dict(self.owner, publication_revision=None), passage(origin_publication_revision=None)])
        found = search_catalog(self.output, "mechanismneedle", timeout=2)["sources"][0]
        self.assertEqual(found["publication_state"], "legacy_unmarked")

    def test_passage_conflicting_duplicate_bad_origin_hash_locator_and_tags_fail_atomically(self):
        for index, change in enumerate((dict(origin_id="2401.12345"), dict(artifact_sha256=None),
                                         dict(chunk_index=True), dict(terms="bad"), dict(section_type=""),
                                         dict(locator=dict(start=1, end=2, unit="chunk_index", artifact="x")))):
            self.output = self.root / ("invalid" + str(index) + ".sqlite")
            with self.assertRaises(ValueError):
                self.build([self.owner, passage(**change)])
            self.assertFalse(self.output.exists())
        self.output = self.root / "conflicting.sqlite"
        with self.assertRaisesRegex(ValueError, "conflicting passage"):
            self.build([self.owner, passage(), passage(terms=["different"])])
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".source-passages-*")), [])

    def test_gzip_input_stream_hashes_decoded_bytes_and_handles_concatenated_members(self):
        raw = self.packets([self.owner, passage()])
        self.input = self.root / "input.jsonl.gz"
        self.input.write_bytes(gzip.compress(raw, mtime=0))
        report = producer.produce(self.input, self.output)
        self.assertEqual(report["input_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(report["counts"]["own_passage"], 1)
        self.output = self.root / "concatenated.sqlite"
        self.input.write_bytes(gzip.compress(producer.encoded(self.owner) + b"\n", mtime=0) +
                               gzip.compress(producer.encoded(passage()) + b"\n", mtime=0))
        self.assertEqual(producer.produce(self.input, self.output)["counts"]["own_passage"], 1)

    def test_gzip_decoded_line_limit_corruption_utf8_and_truncation_are_atomic_failures(self):
        raw = producer.encoded(self.owner) + b"\n"
        self.input = self.root / "bad.jsonl.gz"
        for index, compressed in enumerate((gzip.compress(raw)[:-5], b"not gzip", gzip.compress(b"\xff\n"),
                                            gzip.compress(b"x" * (producer.MAX_LINE_BYTES + 1)))):
            self.output = self.root / ("bad-gzip" + str(index) + ".sqlite")
            self.input.write_bytes(compressed)
            with self.assertRaises(ValueError):
                producer.produce(self.input, self.output)
            self.assertFalse(self.output.exists())

    def test_gzip_input_growth_does_not_extend_the_frozen_compressed_stream(self):
        self.input = self.root / "growing.jsonl.gz"
        self.input.write_bytes(gzip.compress(producer.encoded(self.owner) + b"\n", mtime=0))
        real_read = producer._FrozenInput.read
        changed = False
        def growing(reader, size=-1):
            nonlocal changed
            if not changed:
                changed = True
                with self.input.open("ab") as stream:
                    stream.write(gzip.compress(producer.encoded(self.target) + b"\n", mtime=0))
            return real_read(reader, size)
        with patch.object(producer._FrozenInput, "read", growing), self.assertRaisesRegex(ValueError, "changed"):
            producer.produce(self.input, self.output)
        self.assertFalse(self.output.exists())

    def pause_at(self, phase, *, require_cursor=False, output=None, **options):
        original = producer._Work.checkpoint
        def checkpoint(work):
            original(work)
            if work.state["phase"] == phase and (not require_cursor or work.state["cursor"]):
                raise producer._Pause()
        with patch.object(producer._Work, "checkpoint", checkpoint):
            return producer.produce(self.input, output or self.output, **options)

    def test_owner_aware_context_dedup_retains_identical_text_from_two_owners(self):
        report = self.build([self.target, self.owner, source("2403.12345"), self.doc,
                             document("2403.12345"), self.doc])
        self.assertEqual(report["counts"]["citation_context"], 2)
        self.assertEqual(report["counts"]["duplicate_documents"], 1)
        with contextlib.closing(sqlite3.connect(self.output)) as conn:
            self.assertEqual(conn.execute("SELECT origin_id FROM representations WHERE kind='citation_context' "
                                          "ORDER BY origin_id").fetchall(), [("2402.12345",), ("2403.12345",)])

    def test_every_phase_resumes_with_identical_generation_counters_and_report_hash(self):
        raw = self.packets([self.target, self.owner, self.doc, passage(), self.doc, passage()])
        report_path = self.root / "report.json"
        report_path.write_bytes(producer.encoded(dict(schema=1, mode="export", partial=False,
            full_export=True, complete_generation=True, packets_sha256=hashlib.sha256(raw).hexdigest())))
        baseline = producer.produce(self.input, self.output, input_report=report_path)
        for phase in ("sources", "summaries", "representations", "finalize", "ready"):
            with self.subTest(phase=phase):
                output = self.root / (phase + ".sqlite")
                work_dir = self.root / (phase + "-work")
                options = dict(work_dir=work_dir, input_report=report_path)
                paused = self.pause_at(phase, output=output, **options)
                self.assertEqual((paused["status"], paused["phase"]), ("paused", phase))
                self.assertFalse(output.exists())
                with patch.object(producer, "records", side_effect=AssertionError("input reparsed")):
                    resumed = producer.produce(self.input, output, resume=True, **options)
                self.assertEqual(resumed["generation"], baseline["generation"])
                self.assertEqual(resumed["counts"], baseline["counts"])
                self.assertEqual(resumed["diagnostics"], baseline["diagnostics"])
                self.assertEqual(catalog_coverage(output), baseline["catalog_coverage"])
                before = output.read_bytes()
                again = producer.produce(self.input, output, resume=True, **options)
                self.assertEqual(again["generation"], baseline["generation"])
                self.assertEqual(output.read_bytes(), before)

    def test_mid_ingest_and_representation_batches_resume_gzip_without_replaying_committed_rows(self):
        packets = [self.owner] + [passage(chunk_index=i, locator=dict(start=0, end=len(passage()["text"]),
            unit="unicode_codepoint", artifact=f"chunk_cache/fixture.json#chunk_index={i}/text")) for i in range(270)]
        raw = self.packets(packets)
        self.input = self.root / "bounded.jsonl.gz"
        self.input.write_bytes(gzip.compress(raw, mtime=0))
        baseline = producer.produce(self.input, self.output)
        for phase in ("ingest", "representations"):
            with self.subTest(phase=phase):
                output = self.root / (phase + ".sqlite")
                work_dir = self.root / (phase + "-work")
                original = producer._Work.checkpoint
                def checkpoint(work):
                    original(work)
                    if work.state["phase"] == phase and (
                            work.state["offset"] if phase == "ingest" else work.state["cursor"]):
                        raise producer._Pause()
                with patch.object(producer._Work, "checkpoint", checkpoint):
                    paused = producer.produce(self.input, output, work_dir=work_dir)
                self.assertEqual(paused["phase"], phase)
                resumed = producer.produce(self.input, output, work_dir=work_dir, resume=True)
                self.assertEqual(resumed["generation"], baseline["generation"])
                self.assertEqual(resumed["counts"], baseline["counts"])

    def test_uncommitted_tail_and_finalization_are_rolled_back_without_losing_insert_phase(self):
        self.packets([self.target, self.owner, self.doc, passage()])
        original = producer.catalog._insert_representation
        work_dir = self.root / "tail-work"
        def interrupted(conn, record):
            original(conn, record)
            if record["kind"] == "citation_context":
                raise RuntimeError("simulated crash before checkpoint")
        with patch.object(producer.catalog, "_insert_representation", interrupted), self.assertRaises(RuntimeError):
            producer.produce(self.input, self.output, work_dir=work_dir)
        self.assertFalse(self.output.exists())
        with contextlib.closing(sqlite3.connect(work_dir / "catalog.sqlite")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM representations WHERE kind='citation_context'").fetchone(), (0,))
        finish = producer.catalog._finish_catalog
        def interrupted_finish(conn, **kwargs):
            finish(conn, **kwargs)
            raise producer._Pause()
        with patch.object(producer.catalog, "_finish_catalog", interrupted_finish):
            paused = producer.produce(self.input, self.output, work_dir=work_dir, resume=True)
        self.assertEqual(paused["phase"], "finalize")
        with contextlib.closing(sqlite3.connect(work_dir / "catalog.sqlite")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM catalog").fetchone(), (0,))
            self.assertEqual(conn.execute("SELECT count(*) FROM representations").fetchone(), (4,))
        with patch.object(producer.catalog, "_insert_representation", side_effect=AssertionError("reinserted")):
            resumed = producer.produce(self.input, self.output, work_dir=work_dir, resume=True)
        baseline = producer.produce(self.input, self.root / "baseline.sqlite")
        self.assertEqual(resumed["counts"], baseline["counts"])
        self.assertEqual(resumed["generation"], baseline["generation"])

    def test_resume_hash_guard_rejects_mutated_input_report_code_options_and_output(self):
        raw = self.packets()
        report_path = self.root / "report.json"
        report_raw = producer.encoded(dict(schema=1, mode="export", partial=False,
            full_export=True, complete_generation=True, packets_sha256=hashlib.sha256(raw).hexdigest()))
        report_path.write_bytes(report_raw)
        work_dir = self.root / "identity-work"
        options = dict(work_dir=work_dir, input_report=report_path)
        self.pause_at("sources", **options)
        cases = [dict(max_documents=1), dict(reserve_bytes=0), dict(disk_budget_bytes=10**9)]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "identity mismatch"):
                producer.produce(self.input, self.output, resume=True, **options, **extra)
        self.input.write_bytes(raw.replace(b"Heading", b"Changed"))
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            producer.produce(self.input, self.output, resume=True, **options)
        self.input.write_bytes(raw)
        report_path.write_bytes(report_raw + b" ")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            producer.produce(self.input, self.output, resume=True, **options)
        report_path.write_bytes(report_raw)
        original_hash = producer._file_hash
        with patch.object(producer, "_file_hash", side_effect=lambda path:
                "e" * 64 if Path(path) == Path(producer.__file__) else original_hash(path)):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                producer.produce(self.input, self.output, resume=True, **options)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            producer.produce(self.input, self.root / "other.sqlite", resume=True, **options)
        self.assertFalse(self.output.exists())

    def test_disk_budget_reserve_and_before_publish_fail_without_partial_publication(self):
        self.packets()
        for index, options in enumerate((dict(disk_budget_bytes=1), dict(reserve_bytes=2**63))):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "disk"):
                producer.produce(self.input, self.root / f"disk-{index}.sqlite",
                                 work_dir=self.root / f"disk-work-{index}", **options)
            self.assertFalse((self.root / f"disk-{index}.sqlite").exists())
        original = producer._Work.guard_disk
        def full_before_publish(work):
            original(work)
            if getattr(work, "state", {}).get("phase") == "ready":
                raise ValueError("disk reserve exhausted before publish")
        with patch.object(producer._Work, "guard_disk", full_before_publish), self.assertRaisesRegex(ValueError, "disk reserve"):
            producer.produce(self.input, self.output, work_dir=self.root / "publish-work")
        self.assertFalse(self.output.exists())

    def test_explicit_deadline_cli_pauses_and_resume_is_new_path_only(self):
        self.packets()
        work_dir = self.root / "deadline-work"
        code, paused = self.cli(["--input", str(self.input), "--output", str(self.output),
            "--work-dir", str(work_dir), "--max-seconds", "0.000000001"])
        self.assertEqual(code, 3)
        self.assertEqual(paused["status"], "paused")
        self.assertFalse(self.output.exists())
        self.output.write_bytes(b"unrelated original")
        with self.assertRaises(FileExistsError):
            producer.produce(self.input, self.output, work_dir=work_dir, resume=True)
        self.assertEqual(self.output.read_bytes(), b"unrelated original")

    def test_single_parse_disk_metadata_and_bounded_cache(self):
        self.packets([source("owner-" + str(i)) for i in range(150)])
        real_records = producer.records
        with patch.object(producer, "records", wraps=real_records) as parse:
            producer.produce(self.input, self.output, work_dir=self.root / "cache-work")
        self.assertEqual(parse.call_count, 1)
        with contextlib.closing(sqlite3.connect(self.root / "cache-work" / "work.sqlite")) as conn:
            conn.execute("ATTACH DATABASE ? AS work", (str(self.root / "cache-work" / "work.sqlite"),))
            metadata = producer._DiskMap(conn, "metadata", cache_limit=16)
            for i in range(150):
                self.assertEqual(metadata["owner-" + str(i)]["source_id"], "owner-" + str(i))
            self.assertLessEqual(len(metadata.cache), 16)

    def test_mid_source_and_summary_batches_are_not_counted_twice(self):
        self.packets([source("owner-" + str(i)) for i in range(135)])
        baseline = producer.produce(self.input, self.output)
        for phase in ("sources", "summaries"):
            output = self.root / (phase + ".sqlite")
            work_dir = self.root / (phase + "-work")
            paused = self.pause_at(phase, require_cursor=True, output=output, work_dir=work_dir)
            self.assertEqual(paused["phase"], phase)
            resumed = producer.produce(self.input, output, work_dir=work_dir, resume=True)
            self.assertEqual(resumed["generation"], baseline["generation"])
            self.assertEqual(resumed["counts"], baseline["counts"])

    def test_exclusive_work_lock_blocks_concurrent_resumer(self):
        self.packets()
        work_dir = self.root / "locked-work"
        self.pause_at("sources", work_dir=work_dir)
        with producer._locked_work(work_dir, True), self.assertRaises(OSError):
            producer.produce(self.input, self.output, work_dir=work_dir, resume=True)
        self.assertFalse(self.output.exists())
        self.assertEqual(producer.produce(self.input, self.output, work_dir=work_dir, resume=True)["status"], "ready")


if __name__ == "__main__":
    unittest.main()
