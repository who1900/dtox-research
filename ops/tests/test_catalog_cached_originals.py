"""Small owned fixtures only; no service imports or production input."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ops import catalog_cached_originals as catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.bundle = self.root / "bundle"
        self.baselines = self.root / "data" / "fulltext_cache"
        self.latex = self.baselines.parent / "latex_cache"
        for path in (self.runtime, self.bundle, self.baselines, self.latex):
            path.mkdir(parents=True)
        self.plan = {"schema": "dtox-cached-repair-r3-v1", "runtime": str(self.runtime),
                     "baselines": str(self.baselines), "r2_provenance": {"bundle_path": str(self.bundle)},
                     "inputs": {}}
        self.plan_path = self.root / "plan.json"
        self.output = self.root / "inventory"

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = catalog.encoded(value)
        path.write_bytes(raw)
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def fixture(self, identifier="2401.12345", kind="pdf", prefix="flagged_text", receipt=True):
        stem = identifier.replace("/", "_").replace(":", "_")
        qid = prefix + ":" + stem
        directory = self.runtime / "documents" / hashlib.sha256(qid.encode()).hexdigest()
        directory.mkdir(parents=True)
        meta = {"arxiv_id": identifier, "fulltext_source": "acl-pdf" if identifier.startswith("acl:") else "latex",
                "source_url": None, "status": "done", "title": "Real fixture identity"}
        metadata = {"schema": "repair_source_v1", "stem": stem, "metadata": [meta], "files": {}, "errors": []}
        item = {"old_status": "candidate_unchanged",
                "metadata": self.save(self.bundle / stem / "metadata.json", metadata)}
        value = {"queue_id": qid, "stem": stem, "status": "candidate_unchanged", "errors": [],
                 "identity": {"arxiv_id": identifier, "forward_mapping_verified": True, "metadata": meta}}
        if kind:
            raw = b"%PDF-1.7 fixture original" if kind == "pdf" else b"<article>fixture original</article>"
            path = directory / ("original." + kind)
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            value["source"] = {"kind": kind, "sha256": digest, "bytes": len(raw),
                               "url": "https://aclanthology.org/2020.osact-1.16.pdf" if kind == "pdf"
                               else "https://arxiv.org/html/2401.12345"}
            if kind == "pdf":
                value["core_identity"] = {"pdf": {"canonical_id": identifier, "source_sha256": digest,
                    "baseline_sha256": "a" * 64, "caller_verified": True, "worker_verified": True,
                    "historical_identity_proven": False}}
            value["requests"] = [{"status": 200, "url": value["source"]["url"], "utc": "2026-10-11T00:00:00Z"}]
        if receipt:
            item["receipt"] = self.save(directory / "receipt.v1.json", value)
        self.plan["inputs"][qid] = item
        return qid, item, value, metadata, directory

    def run_catalog(self, **kwargs):
        self.plan["scope"] = {"total": len(self.plan["inputs"])}
        self.save(self.plan_path, self.plan)
        summary = catalog.catalog(self.plan_path, self.output, **kwargs)
        filename = "missing.jsonl" if kwargs.get("export_missing") else "originals.jsonl"
        rows = [json.loads(line) for line in (self.output / filename).read_bytes().splitlines()]
        self.assertEqual(sum(summary["counts_by_availability"].values()), summary["input_count"])
        return summary, rows

    def ledger(self, values):
        path = self.root / "ledger.sqlite3"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE results(queue_id TEXT PRIMARY KEY,payload BLOB,sha TEXT)")
            conn.execute("CREATE TABLE fonts(program_sha TEXT,payload BLOB,sha TEXT)")
            for qid, status, corrupt in values:
                raw = catalog.encoded({"queue_id": qid, "status": status})
                conn.execute("INSERT INTO results VALUES(?,?,?)", (qid, raw,
                    "0" * 64 if corrupt else hashlib.sha256(raw).hexdigest()))
            conn.commit()
        return path

    def test_actual_pdf_receipt_and_no_original_content_read(self):
        qid, _, _, _, directory = self.fixture("acl:2020.osact-1.16")
        with patch.object(catalog, "read_json", wraps=catalog.read_json) as reader:
            summary, rows = self.run_catalog()
        self.assertTrue(summary["complete"])
        self.assertFalse(any(str(call.args[0]).endswith("original.pdf") for call in reader.call_args_list))
        row = rows[0]
        self.assertEqual(row["canonical_id"], "acl:2020.osact-1.16")
        self.assertEqual(row["availability"], "source_available")
        self.assertEqual(row["representative_source"], str(directory / "original.pdf"))
        self.assertEqual(row["originals"][0]["verification_status"], "declared_binding_unsealed")
        self.assertTrue(row["originals"][0]["eligible_for_sealing"])
        self.assertFalse(row["originals"][0]["usable"])
        self.assertEqual(row["aliases"], [qid])

    def test_html_receipt(self):
        self.fixture(kind="html")
        _, rows = self.run_catalog()
        self.assertEqual(rows[0]["originals"][0]["kind"], "html")
        self.assertEqual(rows[0]["source_url_provenance"], "bound_receipt.source.url")

    def test_unknown_version_and_id_version_not_pdf_proof(self):
        self.fixture("2401.12345v2")
        _, rows = self.run_catalog()
        self.assertEqual(rows[0]["id_version"], "v2")
        self.assertIsNone(rows[0]["version_known"])
        self.assertIsNone(rows[0]["originals"][0]["version_known"])

    def test_candidate_url_does_not_become_actual_url(self):
        self.fixture(kind=None)
        _, rows = self.run_catalog()
        self.assertIsNone(rows[0]["source_url"])
        self.assertEqual(rows[0]["candidate_source_url"], "https://arxiv.org/pdf/2401.12345")

    def test_cached_tex_is_assembled_not_raw(self):
        _, item, _, _, _ = self.fixture(kind=None)
        path = self.latex / "2401.12345" / "source.tex"
        path.parent.mkdir()
        raw = b"\\documentclass{article} assembled source"
        path.write_bytes(raw)
        item["cached_tex"] = {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        summary, rows = self.run_catalog()
        source = rows[0]["derived_sources"][0]
        self.assertEqual(source["scope"], "assembled_source_derived")
        self.assertEqual(source["kind"], "assembled_tex")
        self.assertFalse(source["content_sha256_verified"])
        self.assertFalse(source["eligible_for_sealing"])
        self.assertTrue(source["declared_binding_consistent"])
        self.assertEqual(rows[0]["originals"], [])
        self.assertIsNone(rows[0]["representative_source"])
        self.assertEqual(rows[0]["availability"], "acquisition_pending")
        self.assertEqual(summary["unique_represented_documents"], 0)
        self.assertEqual(summary["derived_rows_available"], 1)
        self.assertEqual(summary["counts_by_derived_kind"], {"assembled_tex": 1})
        self.assertEqual(summary["counts_by_availability"]["source_available"], 0)

    def test_wrong_receipt_hash_before_parse_preserves_row(self):
        _, item, _, _, _ = self.fixture()
        item["receipt"]["sha256"] = "0" * 64
        summary, rows = self.run_catalog()
        self.assertEqual(summary["input_count"], 1)
        self.assertEqual(rows[0]["originals"][0]["verification_status"], "unverified_binding")
        self.assertFalse(rows[0]["originals"][0]["eligible_for_sealing"])
        self.assertTrue(any(e["code"] == "small_file_sha_mismatch" for e in rows[0]["errors"]))

    def test_wrong_metadata_hash_preserves_unresolved_denominator(self):
        _, item, _, _, _ = self.fixture()
        item["metadata"]["sha256"] = "0" * 64
        summary, rows = self.run_catalog()
        self.assertEqual(summary["counts_by_availability"]["identity_unresolved"], 1)
        self.assertIsNone(rows[0]["canonical_id"])

    def test_duplicate_metadata_identity_is_unresolved(self):
        _, item, _, metadata, _ = self.fixture()
        metadata["metadata"] *= 2
        item["metadata"] = self.save(Path(item["metadata"]["path"]), metadata)
        _, rows = self.run_catalog()
        self.assertEqual(rows[0]["identity_status"], "unresolved")

    def test_duplicate_json_keys_fail_closed(self):
        self.plan_path.write_text('{"inputs":{},"inputs":{}}', encoding="utf-8")
        with self.assertRaisesRegex(catalog.InventoryError, "duplicate_json_key"):
            catalog.catalog(self.plan_path, self.output)
        self.assertFalse(self.output.exists())

    def test_shared_identical_bindings_keep_aliases_and_representative(self):
        qid, first, _, _, _ = self.fixture()
        other, second, _, _, _ = self.fixture(prefix="empty_chunk_cache")
        first["metadata"] = second["metadata"]
        summary, rows = self.run_catalog()
        self.assertEqual(summary["input_count"], 2)
        self.assertEqual(summary["unique_represented_documents"], 1)
        self.assertEqual(set(rows[0]["aliases"]), {qid, other})
        self.assertEqual(rows[1]["duplicate_document_of"], rows[0]["queue_id"])
        self.assertFalse(rows[1]["is_document_representative"])
        self.assertIsNone(rows[1]["representative_source"])

    def test_conflicting_shared_bindings_fail_both_rows(self):
        _, first, _, _, _ = self.fixture()
        _, second, _, _, _ = self.fixture(prefix="empty_chunk_cache")
        second["metadata"] = dict(first["metadata"], sha256="0" * 64)
        summary, rows = self.run_catalog()
        self.assertEqual(summary["rows_with_errors"], 2)
        self.assertTrue(all(row["identity_status"] == "unresolved" for row in rows))

    def test_unverified_original_and_missing_receipt(self):
        self.fixture(receipt=False)
        summary, rows = self.run_catalog()
        self.assertEqual(summary["input_count"], 1)
        self.assertTrue(summary["complete"])
        self.assertFalse(summary["provenance_complete"])
        self.assertEqual(summary["rows_missing_receipt_binding"], 1)
        self.assertEqual(rows[0]["originals"][0]["verification_status"], "unverified_binding")
        self.assertIsNone(rows[0]["representative_source"])

    def test_hash_without_explicit_bytes_is_unverified(self):
        _, item, receipt, _, directory = self.fixture()
        del receipt["source"]["bytes"]
        item["receipt"] = self.save(directory / "receipt.v1.json", receipt)
        _, rows = self.run_catalog()
        self.assertFalse(rows[0]["originals"][0]["eligible_for_sealing"])

    def test_conflicting_core_origin_hash(self):
        _, item, receipt, _, directory = self.fixture()
        receipt["core_identity"]["pdf"]["source_sha256"] = "0" * 64
        item["receipt"] = self.save(directory / "receipt.v1.json", receipt)
        _, rows = self.run_catalog()
        self.assertIsNone(rows[0]["representative_source"])
        self.assertTrue(any(e["code"] == "conflicting_origin_bindings" for e in rows[0]["errors"]))

    def test_404_is_not_permanent_and_old_unchanged_is_not_healthy(self):
        _, item, receipt, _, directory = self.fixture(kind=None)
        receipt["requests"] = [{"status": 404, "url": "https://arxiv.org/pdf/2401.12345"}]
        item["receipt"] = self.save(directory / "receipt.v1.json", receipt)
        summary, rows = self.run_catalog()
        self.assertEqual(rows[0]["availability"], "acquisition_pending")
        self.assertEqual(rows[0]["old_r2_status"], "candidate_unchanged")
        self.assertFalse(summary["no_change_implies_healthy"])

    def test_temporary_status(self):
        _, item, receipt, _, directory = self.fixture(kind=None)
        receipt["requests"] = [{"status": 503}]
        item["receipt"] = self.save(directory / "receipt.v1.json", receipt)
        _, rows = self.run_catalog()
        self.assertEqual(rows[0]["availability"], "temporarily_unavailable")

    def test_unsafe_binding_and_output_paths(self):
        _, item, _, _, _ = self.fixture()
        item["receipt"]["path"] = str(self.runtime / ".." / "secret.json")
        _, rows = self.run_catalog()
        self.assertTrue(any(e["code"] == "unsafe_path" for e in rows[0]["errors"]))
        with self.assertRaisesRegex(catalog.InventoryError, "output_inside_source_root"):
            catalog.catalog(self.plan_path, self.runtime / "out")

    def test_linked_original_rejected(self):
        _, _, _, _, directory = self.fixture(kind=None)
        target = self.root / "outside.pdf"
        target.write_bytes(b"outside")
        try:
            (directory / "original.pdf").symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable")
        _, rows = self.run_catalog()
        self.assertTrue(any(e["code"] == "linked_path" for e in rows[0]["errors"]))

    def test_output_is_exclusive_and_sources_unchanged(self):
        self.fixture()
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                  for root in (self.runtime, self.bundle) for path in root.rglob("*") if path.is_file()}
        self.run_catalog()
        after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                 for root in (self.runtime, self.bundle) for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        with self.assertRaises(FileExistsError):
            catalog.catalog(self.plan_path, self.output)

    def test_export_missing_uses_verified_v3_status_not_r2(self):
        qid, _, _, _, _ = self.fixture(kind=None)
        other, _, _, _, _ = self.fixture("2401.54321", kind=None)
        ledger = self.ledger([(qid, "CACHED_SOURCE_UNAVAILABLE", False), (other, "NO_VERIFIED_TEXT_CHANGE", False)])
        before = ledger.read_bytes()
        summary, rows = self.run_catalog(ledger=ledger, export_missing=True, expected_missing_count=1)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["input_count"], 2)
        self.assertEqual(summary["verified_v3_missing_count"], 1)
        self.assertEqual([row["queue_id"] for row in rows], [qid])
        self.assertTrue(rows[0]["status_provenance"]["sha256_verified"])
        self.assertEqual(before, ledger.read_bytes())

    def test_export_missing_count_mismatch_and_corrupt_payload_are_not_hidden(self):
        qid, _, _, _, _ = self.fixture(kind=None)
        ledger = self.ledger([(qid, "CACHED_SOURCE_UNAVAILABLE", True)])
        summary, rows = self.run_catalog(ledger=ledger, export_missing=True, expected_missing_count=1)
        self.assertFalse(summary["complete"])
        self.assertFalse(summary["missing_count_matches"])
        self.assertEqual(summary["verified_v3_missing_count"], 0)
        self.assertEqual(rows[0]["export_reason"], "verification_error")

    def test_export_missing_requires_explicit_actual_ledger_and_count(self):
        self.fixture()
        self.save(self.plan_path, self.plan)
        with self.assertRaisesRegex(catalog.InventoryError, "export_missing_requires"):
            catalog.catalog(self.plan_path, self.output, export_missing=True)

    def test_declared_original_size_mismatch_is_not_usable(self):
        _, item, receipt, _, directory = self.fixture()
        receipt["source"]["bytes"] += 1
        item["receipt"] = self.save(directory / "receipt.v1.json", receipt)
        summary, rows = self.run_catalog()
        self.assertFalse(summary["complete"])
        self.assertEqual(rows[0]["originals"][0]["verification_status"], "size_mismatch")
        self.assertFalse(rows[0]["originals"][0]["eligible_for_sealing"])

    def test_metadata_bytes_binding_mismatch_before_parse(self):
        _, item, _, _, _ = self.fixture()
        item["metadata"]["bytes"] += 1
        _, rows = self.run_catalog()
        self.assertTrue(any(e["code"] == "invalid_small_file_binding" for e in rows[0]["errors"]))

    def test_explicit_request_binding_can_complete_core_hash(self):
        _, item, receipt, _, directory = self.fixture()
        source = receipt.pop("source")
        receipt["requests"] = [dict(source, status=200)]
        item["receipt"] = self.save(directory / "receipt.v1.json", receipt)
        _, rows = self.run_catalog()
        self.assertTrue(rows[0]["originals"][0]["eligible_for_sealing"])
        self.assertIsNone(rows[0]["source_url"])

    def test_receipt_wrong_forward_id_is_unresolved(self):
        _, item, receipt, _, directory = self.fixture()
        receipt["identity"]["arxiv_id"] = "2401.99999"
        item["receipt"] = self.save(directory / "receipt.v1.json", receipt)
        _, rows = self.run_catalog()
        self.assertEqual(rows[0]["availability"], "identity_unresolved")

    def test_ledger_count_mismatch_without_corruption(self):
        qid, _, _, _, _ = self.fixture(kind=None)
        ledger = self.ledger([(qid, "CACHED_SOURCE_UNAVAILABLE", False)])
        summary, rows = self.run_catalog(ledger=ledger, export_missing=True, expected_missing_count=2)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["verified_v3_missing_count"], 1)
        self.assertEqual(len(rows), 1)

    def test_original_binary_is_never_opened_by_inventory(self):
        self.fixture()
        with patch.object(catalog.os, "open", wraps=catalog.os.open) as opener:
            self.run_catalog()
        self.assertFalse(any(str(call.args[0]).endswith(("original.pdf", "original.html", "source.tex"))
                             for call in opener.call_args_list))

    def cached_tex(self, item, stem="2401.12345"):
        path = self.latex / stem / "source.tex"
        path.parent.mkdir(exist_ok=True)
        raw = b"\\documentclass{article} assembled source"
        path.write_bytes(raw)
        item["cached_tex"] = {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        return path

    def test_pdf_and_derived_tex_have_separate_representatives_and_counts(self):
        _, item, _, _, directory = self.fixture()
        tex = self.cached_tex(item)
        summary, rows = self.run_catalog()
        row = rows[0]
        self.assertEqual(row["availability"], "source_available")
        self.assertEqual(row["representative_source"], str(directory / "original.pdf"))
        self.assertEqual(row["representative_derived_source"], str(tex))
        self.assertEqual(summary["counts_by_kind"], {"pdf": 1})
        self.assertEqual(summary["counts_by_derived_kind"], {"assembled_tex": 1})
        self.assertEqual(summary["unique_represented_documents"], 1)
        self.assertEqual(summary["unique_represented_derived_documents"], 1)

    def test_derived_aliases_do_not_inflate_raw_documents(self):
        qid, first, _, _, _ = self.fixture(kind=None)
        other, second, _, _, _ = self.fixture(kind=None, prefix="empty_chunk_cache")
        first["metadata"] = second["metadata"]
        self.cached_tex(first)
        second["cached_tex"] = first["cached_tex"]
        summary, rows = self.run_catalog()
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["unique_represented_documents"], 0)
        self.assertEqual(summary["unique_represented_derived_documents"], 1)
        self.assertEqual(summary["derived_rows_available"], 2)
        self.assertTrue(all(set(row["aliases"]) == {qid, other} for row in rows))
        self.assertTrue(all(row["representative_source"] is None for row in rows))
        self.assertEqual(rows[1]["duplicate_derived_document_of"], rows[0]["queue_id"])
        self.assertIsNone(rows[1]["representative_derived_source"])

    def test_export_missing_keeps_actual_rows_with_expected_missing_receipts(self):
        first, item, _, _, _ = self.fixture(kind=None, receipt=False)
        self.cached_tex(item)
        second, _, _, _, _ = self.fixture("2401.54321", kind=None, receipt=False)
        other, _, _, _, _ = self.fixture("2401.11111", kind=None, receipt=False)
        ledger = self.ledger([(first, "CACHED_SOURCE_UNAVAILABLE", False),
                              (second, "CACHED_SOURCE_UNAVAILABLE", False),
                              (other, "NO_VERIFIED_TEXT_CHANGE", False)])
        summary, rows = self.run_catalog(ledger=ledger, export_missing=True, expected_missing_count=2)
        self.assertTrue(summary["complete"])
        self.assertFalse(summary["provenance_complete"])
        self.assertEqual(summary["input_count"], 3)
        self.assertEqual(summary["rows_missing_receipt_binding"], 3)
        self.assertEqual(summary["rows_with_blocking_errors"], 0)
        self.assertEqual(summary["verified_v3_missing_count"], 2)
        self.assertEqual({row["queue_id"] for row in rows}, {first, second})
        self.assertTrue(all(row["export_reason"] == "actual_v3_cached_source_unavailable" for row in rows))
        self.assertTrue(all(row["availability"] == "acquisition_pending" for row in rows))

    def test_live_r3_plan_root_layout_is_supported(self):
        plan = {"runtime": str(self.root / "opt/dtox-repair-20261010/runs/r2/runtime"),
                "baselines": str(self.root / "opt/dtox-research/fulltext_cache"),
                "r2_provenance": {"bundle_path": str(self.root / "opt/dtox-repair-20261010/runs/r2/direct-metadata")},
                "inputs": {}}
        runtime, bundle, latex = catalog.roots_for(plan)
        self.assertEqual(runtime, Path(plan["runtime"]))
        self.assertEqual(bundle, Path(plan["r2_provenance"]["bundle_path"]))
        self.assertEqual(latex, self.root / "opt/dtox-research/latex_cache")


if __name__ == "__main__":
    unittest.main()
