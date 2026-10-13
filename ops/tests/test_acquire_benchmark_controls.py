import copy
import hashlib
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

from ops import acquire_benchmark_controls as adapter


PDF = b"%PDF-1.4\nmock candidate bytes\n"


def selection():
    rows = [{"document_id": "acl:doc" + str(n), "queue_id": "flagged_text:acl_" + str(n),
             "pdf_acquisition_required": False} for n in range(80)]
    for n in range(20):
        identifier = "2501." + str(10000 + n)
        qid = "flagged_text:" + identifier
        source = {"kind": "html", "scope": "saved_original", "original_path": "/never-read/" + identifier + ".html",
                  "size": 17, "declared_bytes": 17, "expected_sha256": hashlib.sha256(identifier.encode()).hexdigest(),
                  "eligible_for_sealing": True, "declared_binding_consistent": True}
        original = {"canonical_id": identifier, "queue_id": qid, "aliases": [qid], "identity_status": "matched",
                    "is_document_representative": True, "receipt_source_url": "https://arxiv.org/html/" + identifier,
                    "representative_source": source["original_path"], "originals": [source], "version_known": None,
                    "status_provenance": {"status": "NO_VERIFIED_TEXT_CHANGE", "sha256_verified": True}}
        rows.append({"document_id": identifier, "queue_id": qid, "pdf_acquisition_required": True,
                     "cohort": "html_potential_control", "source_uri": original["receipt_source_url"],
                     "pdf_url_candidate": "https://arxiv.org/pdf/" + identifier,
                     "source_record": source, "inventory_record": original})
    return {"candidates": rows}


class MockFetcher:
    def __init__(self, behavior=None):
        self.calls = []
        self.behavior = behavior

    def fetch(self, url, kind, directory, receipt):
        self.calls.append((url, kind, directory))
        if self.behavior:
            return self.behavior(url, directory, receipt)
        receipt["requests"].append({"status": 200, "url": url})
        target = directory / "original.pdf"
        target.write_bytes(PDF)
        receipt["source"] = {"kind": "pdf", "url": url, "bytes": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest()}
        return target


class AcquireBenchmarkControlsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = selection()
        self.selection_path = self.root / "selection.json"
        self.selection_path.write_bytes(adapter.catalog.encoded(self.plan))
        self.selection_sha = hashlib.sha256(self.selection_path.read_bytes()).hexdigest()
        self.item = self.plan["candidates"][80]
        self.directory = self.root / "document"
        self.directory.mkdir()

    def one(self, fetcher):
        return adapter.acquire_one(fetcher, self.item, self.directory, self.selection_sha)

    def run_mocked(self, fetcher, output=None):
        bindings = {"fetcher": {"sha256": adapter.REVIEWED_FETCHER_SHA256},
                    "exporter": {"sha256": adapter.REVIEWED_EXPORTER_SHA256}}
        with mock.patch.object(adapter, "load_fetcher", return_value=(fetcher, bindings)), \
                mock.patch.object(adapter.time, "sleep") as spacing, \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("test network forbidden")):
            summary = adapter.run(self.selection_path, self.selection_sha, self.root / "batch_repair_v1.py",
                                  adapter.REVIEWED_FETCHER_SHA256, adapter.REVIEWED_EXPORTER_SHA256,
                                  output or self.root / "output")
        return summary, spacing

    def test_only_twenty_controls_once_spacing_and_durable_receipts(self):
        fetcher = MockFetcher()
        summary, spacing = self.run_mocked(fetcher)
        self.assertEqual(len(fetcher.calls), 20)
        self.assertEqual([c[0] for c in fetcher.calls], [r["pdf_url_candidate"] for r in self.plan["candidates"][80:]])
        self.assertEqual(spacing.call_args_list, [mock.call(3)] * 19)
        self.assertEqual(summary["saved_candidates"], 20)
        self.assertEqual(summary["attempted"], 20)
        self.assertTrue(summary["all_attempts_recorded"])
        self.assertEqual(summary["remaining"], 0)
        self.assertEqual(summary["identity_accepted"], 0)
        self.assertEqual(summary["proven_healthy"], 0)
        for result in summary["results"]:
            raw = (self.root / "output" / result["receipt"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), result["receipt_sha256"])
            receipt = json.loads(raw)
            self.assertEqual(receipt["status"], adapter.SAVED)
            self.assertEqual(receipt["attempts"], 1)
            self.assertEqual(receipt["selection_sha256"], self.selection_sha)
            self.assertEqual(receipt["edition"], "UNKNOWN")
            self.assertIsNone(receipt["version_known"])
            self.assertFalse(receipt["identity_accepted"] or receipt["source_available"])
            self.assertEqual(receipt["healthy_status"], "unreviewed_not_proven")
            self.assertEqual(receipt["selection_record"]["source_record"]["kind"], "html")
        events = [json.loads(line) for line in (self.root / "output/progress.jsonl").read_bytes().splitlines()]
        self.assertEqual(sum(e["event"] == "attempt_finished" for e in events), 20)
        self.assertEqual(events[-1]["event"], "end")
        saved_summary = (self.root / "output/summary.json").read_bytes()
        self.assertEqual(hashlib.sha256(saved_summary).hexdigest(), events[-1]["summary_sha256"])

    def test_failed_and_partial_all_retained_no_retries(self):
        def unavailable(url, directory, receipt):
            receipt["requests"].append({"status": 200, "url": url})
            (directory / "original.pdf").write_bytes(b"%PDF-partial")
            raise http.client.IncompleteRead(b"partial", 20)
        fetcher = MockFetcher(unavailable)
        summary, _ = self.run_mocked(fetcher)
        self.assertEqual(len(fetcher.calls), 20)
        self.assertEqual(summary["counts"], {"temporarily_unavailable": 20})
        self.assertEqual(summary["saved_candidates"], 0)
        self.assertEqual(summary["failed"], 20)
        for result in summary["results"]:
            receipt = json.loads((self.root / "output" / result["receipt"]).read_bytes())
            self.assertEqual(receipt["error"]["type"], "IncompleteRead")
            self.assertIsNotNone(receipt["retained_file"])
            self.assertFalse(receipt["saved_sha256_verified"])

    def test_http_status_taxonomy(self):
        for status, expected in [(404, "acquisition_pending"), (429, "temporarily_unavailable"),
                                 (500, "temporarily_unavailable"), (503, "temporarily_unavailable"),
                                 (403, "acquisition_pending")]:
            with self.subTest(status=status):
                receipt = {"requests": [{"url": self.item["pdf_url_candidate"], "status": status}]}
                self.assertEqual(adapter.failure_status(ValueError("HTTP status/encoding rejected"), receipt,
                                                        self.item["pdf_url_candidate"]), expected)

    def test_connection_timeout_and_dns_unavailable(self):
        for error in (TimeoutError("request timeout"), ConnectionError("closed"), socket.gaierror("DNS failure")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(adapter.failure_status(error, {"requests": []}, self.item["pdf_url_candidate"]),
                                 "temporarily_unavailable")

    def test_unsafe_redirects_and_nonpublic_dns_identity_unresolved(self):
        for message in ("off-origin redirect rejected", "redirect changes exact identity", "private/nonpublic DNS rejected"):
            with self.subTest(message=message):
                self.assertEqual(adapter.failure_status(ValueError(message), {"requests": []}, self.item["pdf_url_candidate"]),
                                 "identity_unresolved")

    def test_missing_output_failure(self):
        def missing(url, directory, receipt):
            receipt["requests"].append({"url": url, "status": 200})
            receipt["source"] = {"url": url, "kind": "pdf", "bytes": 1, "sha256": "0" * 64}
            return directory / "original.pdf"
        receipt = self.one(MockFetcher(missing))
        self.assertEqual(receipt["status"], "acquisition_error")
        self.assertFalse(receipt["saved_sha256_verified"])

    def test_saved_hash_mismatch_retained_failure(self):
        def mismatch(url, directory, receipt):
            target = MockFetcher().fetch(url, "pdf", directory, receipt)
            receipt["source"]["sha256"] = "0" * 64
            return target
        receipt = self.one(MockFetcher(mismatch))
        self.assertEqual(receipt["status"], "acquisition_error")
        self.assertEqual(receipt["retained_file"]["sha256"], hashlib.sha256(PDF).hexdigest())

    def test_unexpected_error_retained_and_other_documents_attempted(self):
        def unexpected(url, directory, receipt):
            raise RuntimeError("unexpected fixture error")
        summary, _ = self.run_mocked(MockFetcher(unexpected))
        self.assertEqual(summary["counts"], {"acquisition_error": 20})
        self.assertEqual(len(summary["results"]), 20)

    def test_wrong_selection_sha_before_loader_or_output(self):
        with mock.patch.object(adapter, "load_fetcher") as loader:
            with self.assertRaisesRegex(ValueError, "bound_sha_mismatch"):
                adapter.run(self.selection_path, "0" * 64, self.root / "batch_repair_v1.py",
                            adapter.REVIEWED_FETCHER_SHA256, adapter.REVIEWED_EXPORTER_SHA256, self.root / "output")
            loader.assert_not_called()
        self.assertFalse((self.root / "output").exists())

    def test_invalid_urls_ids_flags_counts_aliases_rejected(self):
        cases = [("pdf_url_candidate", "http://arxiv.org/pdf/2501.10000"),
                 ("pdf_url_candidate", "https://arxiv.org/pdf/2501.10001"),
                 ("pdf_url_candidate", "https://evil.invalid/pdf/2501.10000"),
                 ("pdf_url_candidate", "https://arxiv.org/pdf/2501.10000?download=1"),
                 ("document_id", "2501.10000v2"), ("document_id", "acl:2501.10000"),
                 ("pdf_acquisition_required", "true")]
        for key, value in cases:
            plan = copy.deepcopy(self.plan)
            plan["candidates"][80][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                adapter.controls_from_selection(plan)
        for change in (lambda p: p["candidates"].pop(),
                       lambda p: p["candidates"][80].update(pdf_acquisition_required=False),
                       lambda p: p["candidates"][80]["inventory_record"].update(aliases=["wrong:alias"])):
            plan = copy.deepcopy(self.plan)
            change(plan)
            with self.assertRaises(ValueError):
                adapter.controls_from_selection(plan)

    def test_output_exclusive_no_fetcher_loaded(self):
        (self.root / "output").mkdir()
        with mock.patch.object(adapter, "load_fetcher") as loader:
            with self.assertRaisesRegex(ValueError, "output_must_be_new"):
                adapter.run(self.selection_path, self.selection_sha, self.root / "batch_repair_v1.py",
                            adapter.REVIEWED_FETCHER_SHA256, adapter.REVIEWED_EXPORTER_SHA256, self.root / "output")
            loader.assert_not_called()

    def test_pinned_modules_both_verified_before_exec_no_bytecode(self):
        exporter = self.root / "export_repair_sources_v1.py"
        exporter.write_bytes(b"TOKEN = 'verified-sibling'\n")
        fetcher = self.root / "batch_repair_v1.py"
        fetcher.write_bytes(b"from export_repair_sources_v1 import TOKEN\nPDF_CAP=32*1024*1024\nclass Fetcher:\n    token=TOKEN\n")
        fetcher_sha = hashlib.sha256(fetcher.read_bytes()).hexdigest()
        exporter_sha = hashlib.sha256(exporter.read_bytes()).hexdigest()
        previous = types.ModuleType("previous-sibling")
        with mock.patch.object(adapter, "REVIEWED_FETCHER_SHA256", fetcher_sha), \
                mock.patch.object(adapter, "REVIEWED_EXPORTER_SHA256", exporter_sha), \
                mock.patch.dict(sys.modules, {"export_repair_sources_v1": previous}):
            loaded, bindings = adapter.load_fetcher(fetcher, fetcher_sha, exporter_sha)
            self.assertEqual(loaded.token, "verified-sibling")
            self.assertEqual(bindings["exporter"]["sha256"], exporter_sha)
            self.assertIs(sys.modules["export_repair_sources_v1"], previous)
            exporter.write_bytes(b"raise AssertionError('unverified module executed')\n")
            with self.assertRaisesRegex(ValueError, "bound_sha_mismatch"):
                adapter.load_fetcher(fetcher, fetcher_sha, exporter_sha)
            self.assertIs(sys.modules["export_repair_sources_v1"], previous)
        self.assertFalse((self.root / "__pycache__").exists())

    def test_unreviewed_pin_rejected(self):
        with self.assertRaisesRegex(ValueError, "unreviewed_fetcher_or_exporter_pin"):
            adapter.load_fetcher(self.root / "batch_repair_v1.py", "0" * 64, adapter.REVIEWED_EXPORTER_SHA256)


if __name__ == "__main__":
    unittest.main()
