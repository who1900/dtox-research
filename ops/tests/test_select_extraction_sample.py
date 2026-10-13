import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ops import select_extraction_sample as selector


def fixture(identity, kind="pdf", status="NEEDS_GLYPH_VERIFICATION", salt=""):
    qid = "flagged_text:" + identity.replace(":", "_").replace("/", "_") + salt
    host = "arxiv.org" if kind == "html" else "raw.githubusercontent.com"
    source = {"kind": kind, "scope": "saved_original", "eligible_for_sealing": True,
              "declared_binding_consistent": True, "declared_duplicate_of": None,
              "expected_sha256": hashlib.sha256((identity + salt).encode()).hexdigest(),
              "size": 12, "declared_bytes": 12, "original_path": "/never-open/" + qid + "/original." + kind,
              "verification_status": "declared_binding_unsealed", "version_known": None}
    return {"queue_id": qid, "canonical_id": identity, "aliases": [qid], "identity_status": "matched",
            "errors": [], "is_document_representative": True, "duplicate_document_of": None,
            "representative_source": source["original_path"], "originals": [source], "derived_sources": [],
            "receipt_source_url": "https://" + host + "/html/" + identity,
            "source_url": "https://" + host + "/html/" + identity,
            "status_provenance": {"status": status, "sha256_verified": True}, "version_known": None}


def pool():
    rows = []
    for prefix, group in selector.NAMESPACES.items():
        for number in range(selector.DEFAULT_CONFIG["pdf_host_quotas"][group] + 3):
            status = ("INPUT_OR_ANALYSIS_UNRESOLVED" if prefix == "acl" and number < 6
                      else "STRUCTURAL_REEXTRACT_REQUIRED" if prefix == "pmlr" and number == 0
                      else "NEEDS_GLYPH_VERIFICATION")
            rows.append(fixture(prefix + ":doc" + str(number), status=status))
    rows.extend(fixture("2501." + str(10000 + number), "html", "NO_VERIFIED_TEXT_CHANGE")
                for number in range(25))
    return rows


class SelectExtractionSampleTests(unittest.TestCase):
    def test_quotas_mandatory_and_rank(self):
        rows = pool()
        selected, summary, _ = selector.select_candidates(rows)
        self.assertEqual(len(selected), 100)
        self.assertEqual(summary["cached_pdf_challenges"], 80)
        self.assertEqual(summary["html_potential_controls"], 20)
        self.assertEqual(summary["mandatory_pdf_counts"], selector.MANDATORY)
        self.assertEqual({r["queue_id"] for r in selected if r["mandatory_status"]},
                         {r["queue_id"] for r in rows if r["status_provenance"]["status"] in selector.MANDATORY})
        for group, quota in selector.DEFAULT_CONFIG["pdf_host_quotas"].items():
            items = [r for r in selected if r["quota_group"] == group]
            self.assertEqual(len(items), quota)
            self.assertEqual(items, sorted(items, key=selector.rank))
            eligible = sorted((r for r in rows if r["canonical_id"].startswith(
                next(k for k, v in selector.NAMESPACES.items() if v == group) + ":")
                and r["status_provenance"]["status"] not in selector.MANDATORY),
                key=lambda r: hashlib.sha256(r["canonical_id"].encode()).hexdigest())
            optional = [r["queue_id"] for r in items if not r["mandatory_status"]]
            self.assertEqual(set(optional), {r["queue_id"] for r in eligible[:len(optional)]})
        self.assertEqual(summary["excluded_count"] + 100, len(rows))

    def test_determinism_and_no_input_mutation(self):
        rows = pool()
        snapshot = copy.deepcopy(rows)
        first = selector.select_candidates(rows)
        self.assertEqual(first, selector.select_candidates(list(reversed(rows))))
        self.assertEqual(rows, snapshot)

    def test_duplicate_hash_and_identity(self):
        rows = pool()
        duplicate_hash = fixture("acl:duplicate-content")
        duplicate_hash["originals"][0]["expected_sha256"] = rows[0]["originals"][0]["expected_sha256"]
        duplicate_id = fixture("acl:doc0", salt="other-queue")
        rows.extend([duplicate_hash, duplicate_id])
        selected, summary, _ = selector.select_candidates(rows)
        self.assertEqual(len({r["document_id"] for r in selected}), 100)
        self.assertEqual(len({r["source_record"]["expected_sha256"] for r in selected}), 100)
        self.assertEqual(summary["excluded_by_reason"]["duplicate_expected_content_or_identity"], 2)

    def test_mandatory_cannot_be_deduplicated_away(self):
        rows = pool()
        rows[1]["originals"][0]["expected_sha256"] = rows[0]["originals"][0]["expected_sha256"]
        with self.assertRaisesRegex(ValueError, "mandatory_duplicate"):
            selector.select_candidates(rows)

    def test_missing_quota_fails_without_autofill(self):
        rows = [r for r in pool() if not r["canonical_id"].startswith("author:")]
        with self.assertRaisesRegex(ValueError, "quota_unavailable:author"):
            selector.select_candidates(rows)

    def test_missing_mandatory_fails(self):
        rows = pool()[1:]
        with self.assertRaisesRegex(ValueError, "mandatory_pool_counts_mismatch"):
            selector.select_candidates(rows)

    def test_controls_not_healthy_and_no_alias(self):
        rows = pool()
        rows[-1]["aliases"].append("alias:other")
        selected, summary, _ = selector.select_candidates(rows)
        controls = [r for r in selected if r["cohort"] == "html_potential_control"]
        self.assertEqual(len(controls), 20)
        self.assertEqual(summary["excluded_by_reason"]["html_alias_not_control"], 1)
        for item in controls:
            self.assertEqual(item["healthy_status"], "unreviewed_not_proven")
            self.assertTrue(item["pdf_acquisition_required"])
            self.assertTrue(item["visual_review_required"])
            self.assertEqual(item["edition"], "UNKNOWN")
            self.assertEqual(item["pdf_url_status"], "CANDIDATE")
            self.assertEqual(item["pdf_url_candidate"], "https://arxiv.org/pdf/" + item["document_id"])
            self.assertEqual(item["source_uri"], item["inventory_record"]["receipt_source_url"])
            self.assertEqual(item["source_record"]["kind"], "html")
            self.assertIsNone(item["inventory_record"]["version_known"])
            self.assertFalse(item["sealed"] or item["quality_accepted"])

    def test_out_of_scope_derived_and_unverified(self):
        rows = pool()
        extra = [fixture("other:1"), fixture("acl:unverified"), fixture("2501.99999", "html", "OTHER")]
        extra[1]["status_provenance"]["sha256_verified"] = False
        derived = fixture("2501.99998")
        derived["derived_sources"], derived["originals"] = derived["originals"], []
        extra.append(derived)
        selected, summary, _ = selector.select_candidates(rows + extra)
        self.assertEqual(len(selected), 100)
        self.assertEqual(summary["out_of_scope_count"], 2)
        self.assertEqual(summary["derived_source_rows"], 1)
        self.assertEqual(summary["excluded_count"] + 100, len(rows + extra))

    def write_inventory(self, directory, rows):
        path = directory / "originals.jsonl"
        raw = b"".join(selector.catalog.encoded(row) + b"\n" for row in rows)
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_bound_inventory_and_exclusive_output_no_original_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = pool()
            path, digest = self.write_inventory(root, rows)
            config = root / "quotas.json"
            config.write_bytes(selector.catalog.encoded(selector.DEFAULT_CONFIG))
            summary = selector.run(path, digest, root / "new-output", config)
            selection = json.loads((root / "new-output/selection.json").read_bytes())
            self.assertEqual(selection["input_inventory"]["sha256"], digest)
            self.assertEqual(selection["candidates"][0]["inventory_record"],
                             next(r for r in rows if r["queue_id"] == selection["candidates"][0]["queue_id"]))
            self.assertEqual(summary, json.loads((root / "new-output/summary.json").read_bytes()))
            self.assertEqual(summary["protocol"]["config_sha256"], hashlib.sha256(config.read_bytes()).hexdigest())
            self.assertFalse(summary["protocol"]["original_bytes_read"])
            with self.assertRaises(FileExistsError):
                selector.run(path, digest, root / "new-output")

    def test_wrong_hash_no_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, _ = self.write_inventory(root, pool())
            with self.assertRaisesRegex(ValueError, "inventory_sha_mismatch"):
                selector.run(path, "0" * 64, root / "output")
            self.assertFalse((root / "output").exists())

    def test_missing_quota_no_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, digest = self.write_inventory(root, [r for r in pool() if not r["canonical_id"].startswith("hal:")])
            with self.assertRaisesRegex(ValueError, "quota_unavailable:HAL"):
                selector.run(path, digest, root / "output")
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
