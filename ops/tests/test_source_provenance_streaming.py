"""Local contracts for lossless streaming and atomic whole-packet replay, not semantic QA."""
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from api import source_representations as catalog
from ops import build_source_representations as producer
from ops.tests.test_build_source_representations import document, passage, source


class ProvenanceStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "packets.jsonl"

    def packets(self, records):
        self.input.write_bytes(b"".join(producer.encoded(row) + b"\n" for row in records))

    def test_stream_exceeds_legacy_emission_limit_without_losing_any_lead_or_primary_identity(self):
        entry = "@misc{k,url={https://arxiv.org/abs/2401.12345v2},note={引用}}"
        bibliography = "\r\n".join([entry] * 110 + [
            "@misc{z,url={https://arxiv.org/abs/2409.12345}}",
            "@misc{wait,url={https://arxiv.org/abs/2408.12345}}"])
        doc = document(text="\r\n\r\n".join(["First \\cite{k,k,z,wait}."] +
                       ["Context \\cite{k}." for _ in range(100)] + ["Last \\cite{missing}."]),
                       bibliography_files=[dict(path="refs.bib", text=bibliography)])
        files = [("refs.bib", bibliography)]
        with self.assertRaisesRegex(ValueError, "emitted_provenance"):
            producer.extract_citation_contexts(doc["text"], files)
        stream = producer.stream_citation_contexts(doc["text"], files)
        contexts = list(stream)
        expected = [(context, lead) for context in contexts for lead in context["resolved"]]
        unavailable = source("2408.12345")
        unavailable["status"] = "pending"
        self.packets([source("2402.12345"), source("2401.12345"), unavailable, doc])
        output = self.root / "catalog.sqlite"
        report = producer.produce(self.input, output)
        self.assertEqual(report["counts"]["citation_context"], 101)
        self.assertEqual(report["counts"]["duplicate_context_targets"], 1)
        self.assertEqual(report["counts"]["missing_metadata"], 1)
        self.assertEqual(report["counts"]["skipped_target_status"], 1)
        self.assertEqual(report["diagnostics"]["extractor"]["unknown_reference"], 1)
        self.assertTrue(report["diagnostics"]["bindings"]["samples"][0]["provenance_truncated"])
        with contextlib.closing(sqlite3.connect(output)) as conn:
            rows = conn.execute("SELECT o.origin_id,o.origin_revision,o.context_start,o.context_end,"
                "o.occurrence_ordinal,o.requested_target,o.exact_key,o.citation_start,o.citation_end,"
                "p.provenance_id,p.bibliography_sha256,p.payload,o.bibliography_sha256 FROM provenance_occurrences o "
                "JOIN provenance_payloads p USING(provenance_id) ORDER BY o.occurrence_ordinal").fetchall()
            self.assertEqual(len(rows), len(expected))
            self.assertEqual(conn.execute("SELECT count(*) FROM provenance_payloads").fetchone()[0], 3)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            for ordinal, (row, (context, lead)) in enumerate(zip(rows, expected)):
                self.assertEqual(row[:9], ("2402.12345", stream.source_sha256, context["start"], context["end"],
                    ordinal, lead["target_id"], lead["key"], lead["citation_start"], lead["citation_end"]))
                self.assertEqual(row[9], producer.fingerprint([
                    stream.source_sha256, stream.bibliography_sha256, lead["provenance"]]))
                self.assertEqual(row[10], stream.bibliography_sha256)
                self.assertEqual(json.loads(row[11]), lead["provenance"])
                self.assertEqual(row[12], stream.bibliography_sha256)
            context, lead = expected[0]
            primary = dict(kind="citation_context", source_id="2401.12345", origin_id="2402.12345",
                origin_revision=stream.source_sha256, locator=dict(start=context["start"], end=context["end"],
                unit="unicode_codepoint", artifact="latex_source"), text=context["search_text"],
                binding=dict(exact_key="k", identifier="2401.12345"), binding_status="resolved")
            expected_id = producer.fingerprint([primary, "a" * 64, "a" * 64, [stream.bibliography_sha256, lead]])
            self.assertEqual(conn.execute("SELECT representation_id FROM representations WHERE kind='citation_context' "
                "ORDER BY id LIMIT 1").fetchone()[0], expected_id)
            self.assertEqual(conn.execute("SELECT count(*) FROM representations WHERE source_id IN (?,?)",
                ("2409.12345", "2408.12345")).fetchone()[0], 0)

    def test_same_artifact_distinct_bibliographies_retain_both_with_canonical_digest(self):
        text = "Shared source \\cite{k,k}."
        bibliography = "@misc{k,url={https://arxiv.org/abs/2401.12345v2}}"

        def build(order):
            with contextlib.closing(sqlite3.connect(":memory:")) as conn:
                catalog._configure_builder(conn)
                conn.executescript(catalog._SCHEMA)
                owner = source("2402.12345")
                catalog._insert_source(conn, {k: v for k, v in owner.items() if k in producer.SOURCE_FIELDS})
                for name in order:
                    stream = producer.stream_citation_contexts(text, [(name, bibliography)])
                    ordinal = 0
                    for context in stream:
                        for lead in context["resolved"]:
                            catalog._insert_provenance_occurrence(conn, owner["source_id"], stream.source_sha256,
                                stream.bibliography_sha256, context, lead, ordinal)
                            ordinal += 1
                rows = conn.execute("SELECT o.origin_revision,o.bibliography_sha256,o.occurrence_ordinal,"
                    "p.bibliography_sha256,p.payload FROM provenance_occurrences o "
                    "JOIN provenance_payloads p USING(provenance_id) "
                    "ORDER BY o.bibliography_sha256,o.occurrence_ordinal").fetchall()
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                return catalog._finish_catalog(conn)["generation"], rows

        generation, rows = build(["a.bib", "b.bib"])
        self.assertEqual((generation, rows), build(["b.bib", "a.bib"]))
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row[0] for row in rows}), 1)
        self.assertEqual(len({row[1] for row in rows}), 2)
        self.assertEqual([row[2] for row in rows], [0, 1, 0, 1])
        self.assertTrue(all(row[1] == row[3] for row in rows))
        self.assertEqual({json.loads(row[4])[0]["path"] for row in rows}, {"a.bib", "b.bib"})
        self.assertNotEqual(generation, build(["a.bib"])[0])
        self.packets([source("2402.12345"), source("2401.12345")] + [
            document(text=text, bibliography_files=[dict(path=name, text=bibliography)])
            for name in ("a.bib", "b.bib")])
        output = self.root / "conflicting-bibliographies.sqlite"
        with self.assertRaisesRegex(ValueError, "conflicting document packet"):
            producer.produce(self.input, output)
        self.assertFalse(output.exists())

    def test_pause_and_crash_roll_back_document_and_batch_then_replay_without_loss_or_duplicates(self):
        docs = [document(text="First \\cite{k,k}.\n\nSecond \\cite{k}."),
                document("2403.12345", text="Other \\cite{k,k}.\n\nLast \\cite{k}.")]
        self.packets([source("2401.12345"), source("2402.12345"), source("2403.12345")] +
            [passage(chunk_index=i, locator=dict(start=0, end=len(passage()["text"]), unit="unicode_codepoint",
             artifact=f"chunk_cache/fixture.json#chunk_index={i}/text")) for i in range(130)] + docs)
        baseline = producer.produce(self.input, self.root / "baseline.sqlite")
        for mode in ("pause", "crash"):
            with self.subTest(mode=mode):
                work_dir, output = self.root / mode, self.root / (mode + ".sqlite")
                original_check = producer._Work.check
                original_insert = catalog._insert_provenance_occurrence

                def check(work):
                    original_check(work)
                    if work.state["phase"] == "representations" and work.conn.execute(
                            "SELECT count(*) FROM provenance_occurrences WHERE origin_id='2403.12345'").fetchone()[0]:
                        raise producer._Pause()

                def insert(conn, owner, *args):
                    original_insert(conn, owner, *args)
                    if owner == "2403.12345" and args[-1] == 1:
                        raise RuntimeError("simulated uncommitted document crash")

                if mode == "pause":
                    with patch.object(producer._Work, "check", check):
                        paused = producer.produce(self.input, output, work_dir=work_dir)
                    self.assertEqual((paused["status"], paused["phase"]), ("paused", "representations"))
                else:
                    with patch.object(catalog, "_insert_provenance_occurrence", insert), self.assertRaisesRegex(
                            RuntimeError, "uncommitted document"):
                        producer.produce(self.input, output, work_dir=work_dir)
                self.assertFalse(output.exists())
                with contextlib.closing(sqlite3.connect(work_dir / "catalog.sqlite")) as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM provenance_occurrences").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT count(*) FROM provenance_payloads").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT count(*) FROM representations WHERE kind='own_passage'").fetchone()[0], 128)
                with contextlib.closing(sqlite3.connect(work_dir / "work.sqlite")) as conn:
                    saved = json.loads(conn.execute("SELECT value FROM state").fetchone()[0])
                    self.assertEqual(saved["cursor"], 131)
                    self.assertEqual(saved["identity"]["version"], 2)
                    self.assertEqual(conn.execute("SELECT count(*) FROM documents").fetchone()[0], 0)
                resumed = producer.produce(self.input, output, work_dir=work_dir, resume=True)
                for key in ("generation", "counts", "diagnostics"):
                    self.assertEqual(resumed[key], baseline[key])
                with contextlib.closing(sqlite3.connect(output)) as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM provenance_occurrences").fetchone()[0], 6)
                    self.assertEqual(conn.execute("SELECT count(*) FROM provenance_payloads").fetchone()[0], 2)
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_payload_and_occurrences_change_digest_empty_extension_is_stable_and_v1_resume_is_rejected(self):
        def generation(raw=None, key="k", reverse=False):
            with contextlib.closing(sqlite3.connect(":memory:")) as conn:
                catalog._configure_builder(conn)
                conn.executescript(catalog._SCHEMA)
                owner = source("2402.12345")
                catalog._insert_source(conn, {k: v for k, v in owner.items() if k in producer.SOURCE_FIELDS})
                if raw is not None:
                    for ordinal in ([1, 0] if reverse else [0, 1]):
                        catalog._insert_provenance_occurrence(conn, owner["source_id"], "b" * 64, "c" * 64,
                            dict(start=0, end=20), dict(target_id="2409.12345", key=key, citation_start=2,
                            citation_end=8, provenance=[dict(entry_raw=raw, requested_id="arxiv:2409.12345v2")]), ordinal)
                return catalog._finish_catalog(conn)["generation"]

        self.assertEqual(catalog.PROVENANCE_SCHEMA_VERSION, 1)
        self.assertEqual(generation(), generation())
        self.assertEqual(generation("full raw 引用\r\n"), generation("full raw 引用\r\n", reverse=True))
        self.assertNotEqual(generation("full raw 引用\r\n"), generation("changed raw 引用\r\n"))
        self.assertNotEqual(generation("same raw"), generation("same raw", key="other"))
        self.packets([source("2402.12345"), document()])
        output, work_dir = self.root / "v1.sqlite", self.root / "v1-work"
        original = producer._Work.checkpoint

        def checkpoint(work):
            original(work)
            if work.state["phase"] == "sources":
                raise producer._Pause()

        with patch.object(producer._Work, "checkpoint", checkpoint):
            self.assertEqual(producer.produce(self.input, output, work_dir=work_dir)["status"], "paused")
        with contextlib.closing(sqlite3.connect(work_dir / "work.sqlite")) as conn:
            state = json.loads(conn.execute("SELECT value FROM state").fetchone()[0])
            state["identity"]["version"] = 1
            conn.execute("UPDATE state SET value=?", (producer.encoded(state).decode(),))
            conn.commit()
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            producer.produce(self.input, output, work_dir=work_dir, resume=True)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
