"""Owned small fixtures only; no network, production originals or model execution."""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ops import backfill_cached_originals as backfill
from pipeline.originals import OriginalError, OriginalStore, Provenance, UNKNOWN


PDF = b"%PDF-1.7 owned fixture only"
HTML = b"<!doctype html><html>owned fixture</html>"


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "mapped"
        self.inputs.mkdir()
        self.store = OriginalStore.create(self.root / "cas")
        self.output = self.root / "journal"
        self.inventory = self.root / "inventory.jsonl"
        self.plan_dir = self.root / "planned"
        self.mapping = self.root / "mapping.json"
        self.limits = dict(max_items=20, max_files=20, max_file_bytes=1000, max_total_bytes=10000)
        disk = patch.object(backfill.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**12, 0, 10**12))
        disk.start()
        self.addCleanup(disk.stop)

    def row(self, name="a.pdf", canonical="paper:a", raw=PDF, **changes):
        path = self.inputs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(raw)
        row = {"canonical_id": canonical, "queue_id": canonical, "aliases": ["historical:" + canonical],
               "availability": "source_available", "identity_status": "matched", "source_url": "https://example.org/" + name,
               "historical_version": None, "errors": [], "derived_sources": [],
               "originals": [{"original_path": "/declared/originals/" + name,
                   "size": len(raw), "declared_bytes": len(raw), "expected_sha256": hashlib.sha256(raw).hexdigest(),
                   "kind": "html" if name.endswith(".html") else "pdf", "scope": "saved_original",
                   "eligible_for_sealing": True, "declared_binding_consistent": True,
                   "content_sha256_verified": False, "file_metadata": backfill.metadata(path)}]}
        row.update(changes)
        return row

    def prepare(self, rows, raw=None):
        data = raw if raw is not None else b"".join(backfill.encoded(r) + b"\n" for r in rows)
        self.inventory.write_bytes(data)
        self.plan_sha = backfill.plan(self.inventory, self.plan_dir, expected_sha256=backfill.sha(data),
                                      source_root="/declared/originals", max_bytes=100000, max_rows=30)
        self.plan_file = self.plan_dir / "plan.json"
        self.plan = json.loads(self.plan_file.read_bytes())["payload"]
        return self.plan

    def bind(self, **changes):
        files = {}
        for item in self.plan["items"]:
            if not item["reasons"]:
                rel = item["relative_path"]
                files[rel] = {"relative_path": rel, "expected_sha256": item["expected_sha256"],
                              "metadata": backfill.metadata(self.inputs / rel)}
        self.receipt = {"schema": backfill.BINDING, "plan_sha256": self.plan_sha,
                        "inventory_sha256": self.plan["inventory_sha256"],
                        "source_root": "/declared/originals", "source_identity": {"host": "fixture-source", "generation": "fixture-boot"},
                        "source_mount_root": "/declared", "source_mount_identity": {"dev": 1, "ino": 2},
                        "target_identity": backfill.identity(), "local_root": str(self.inputs),
                        "other_reserved_bytes": 0,
                        "mount_root": str(self.inputs), "target_mount_identity": backfill.directory_identity(self.inputs),
                        "files": list(files.values())}
        self.receipt.update(changes)
        self.save_receipt()

    def save_receipt(self):
        raw = backfill.encoded(self.receipt)
        self.mapping.write_bytes(raw)
        self.receipt_sha = backfill.sha(raw)

    def ingest(self, **kwargs):
        return backfill.import_local(self.plan_file, self.mapping, self.output, self.store.root,
                                     plan_sha256=self.plan_sha, receipt_sha256=self.receipt_sha,
                                     max_json_bytes=100000, limits=self.limits, **kwargs)

    def events(self):
        return [json.loads(p.read_bytes()) for p in sorted((self.output / "events").glob("*.json"))]

    def count(self, table):
        with closing(sqlite3.connect(self.store.db_path)) as db:
            return db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def test_plan_deterministic_dedup_and_unknown_provenance(self):
        rows = [self.row(), self.row("b.pdf", "paper:b"),
                {"canonical_id": "pending", "availability": "acquisition_pending", "originals": []},
                {"canonical_id": None, "availability": "identity_unresolved", "originals": [], "errors": ["ambiguous"]}]
        p = self.prepare(rows)
        self.assertEqual(p["counts"]["rows"], 4)
        self.assertEqual(p["counts"]["expected_unique_blobs"], 1)
        self.assertEqual(p["source_host_identity"], UNKNOWN)
        self.assertEqual(p["full_corpus_coverage"], UNKNOWN)
        self.assertEqual(p["items"][0]["provenance"]["edition"], UNKNOWN)
        self.assertEqual(p["items"][0]["provenance"]["retrieved_at"], UNKNOWN)
        self.assertFalse(p["items"][0]["content_verified"])
        second = backfill.plan(self.inventory, self.root / "second", expected_sha256=p["inventory_sha256"],
                               source_root="/declared/originals", max_bytes=100000, max_rows=30)
        self.assertEqual(self.plan_sha, second)

    def test_plan_does_not_read_originals(self):
        row = self.row()
        (self.inputs / "a.pdf").unlink()
        self.prepare([row])
        self.assertEqual(self.plan["counts"]["eligible_items"], 1)

    def test_duplicate_json_quarantined_not_omitted(self):
        p = self.prepare([], raw=b'{"canonical_id":"a","canonical_id":"b"}\n')
        self.assertEqual(p["counts"]["rows"], 1)
        self.assertIn("duplicate JSON", p["rows"][0]["reasons"][0])
        self.assertIn("raw_line_hex", p["rows"][0])

    def test_bad_paths_quarantined(self):
        rows = []
        for name in ["/declared/originals/../a.pdf", "/elsewhere/a.pdf", "C:\\bad.pdf", "//host/a.pdf"]:
            row = self.row()
            row["originals"][0]["original_path"] = name
            rows.append(row)
        p = self.prepare(rows)
        self.assertEqual(p["counts"]["quarantined_items"], 4)

    def test_link_metadata_quarantined(self):
        row = self.row()
        row["originals"][0]["file_metadata"]["symlink"] = True
        self.assertEqual(self.prepare([row])["counts"]["quarantined_items"], 1)

    def test_hash_size_conflict_quarantines_every_reference(self):
        a, b = self.row(), self.row("b.pdf", "paper:b", raw=PDF + b"x")
        b["originals"][0]["expected_sha256"] = a["originals"][0]["expected_sha256"]
        p = self.prepare([a, b])
        self.assertEqual(p["counts"]["quarantined_items"], 2)
        self.assertEqual(p["blobs"], {})

    def test_canonical_conflict_quarantined(self):
        a, b = self.row(), self.row("b.pdf", raw=PDF + b"x")
        b["source_url"] = a["source_url"]
        self.assertEqual(self.prepare([a, b])["counts"]["quarantined_items"], 2)

    def test_derived_tex_not_raw(self):
        row = self.row()
        row["originals"][0]["kind"] = "assembled_tex"
        self.assertEqual(self.prepare([row])["counts"]["quarantined_items"], 1)

    def test_inventory_sha_and_limits(self):
        self.inventory.write_bytes(b'{}\n')
        for expected, cap, count in [("0" * 64, 100, 10), (backfill.sha(b'{}\n'), 2, 10),
                                      (backfill.sha(b'{}\n'), 100, 0)]:
            with self.subTest(cap=cap, count=count), self.assertRaises(ValueError):
                backfill.plan(self.inventory, self.plan_dir, expected_sha256=expected,
                              source_root="/declared/originals", max_bytes=cap, max_rows=count)

    def test_success_ledger_denominators_dedup_no_queue_or_embedding_changes(self):
        a, b = self.row(), self.row("b.pdf", "paper:b")
        pending = {"canonical_id": "pending", "availability": "acquisition_pending", "originals": []}
        self.prepare([a, b, pending]); self.bind()
        embedding = self.root / "embedding.bin"
        embedding.write_bytes(b"untouched vectors")
        before = backfill.sha(embedding.read_bytes())
        result = self.ingest()
        self.assertEqual(result["rows"], 3)
        self.assertEqual(result["ingested_items"], 2)
        self.assertEqual(result["availability_promotions"], 0)
        self.assertEqual(self.count("blobs"), 1)
        self.assertEqual(self.count("originals"), 2)
        self.assertEqual(self.count("acquisition_queue"), 0)
        self.assertEqual(self.count("transitions"), 0)
        self.assertEqual(before, backfill.sha(embedding.read_bytes()))
        self.assertEqual(self.events()[-1]["status"], "COMPLETE")
        self.assertTrue(self.events()[1]["existing_blob_verified"])
        self.assertEqual(self.events()[0]["aliases"], a["aliases"])

    def test_unknown_host_blocks_import(self):
        self.prepare([self.row()]); self.bind(source_identity={"host": UNKNOWN, "generation": "boot"})
        with self.assertRaisesRegex(ValueError, "UNKNOWN"):
            self.ingest()
        self.assertEqual(self.count("originals"), 0)

    def test_wrong_target_identity_blocks(self):
        self.prepare([self.row()]); self.bind(target_identity={"hostname": "other", "platform": "other"})
        with self.assertRaisesRegex(ValueError, "target host"):
            self.ingest()

    def test_mapping_binding_mismatch(self):
        self.prepare([self.row()]); self.bind(plan_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "bound to plan"):
            self.ingest()

    def test_duplicate_mapping_json_rejected(self):
        self.prepare([self.row()]); self.bind()
        raw = b'{"schema":"a","schema":"b"}'
        self.mapping.write_bytes(raw); self.receipt_sha = backfill.sha(raw)
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            self.ingest()

    def test_extra_missing_and_path_escape_mapping(self):
        self.prepare([self.row()]); self.bind()
        original = deepcopy(self.receipt)
        for files in [[], [dict(original["files"][0], relative_path="../a.pdf")],
                      original["files"] + original["files"]]:
            self.receipt = dict(original, files=files); self.save_receipt()
            with self.subTest(files=len(files)), self.assertRaises(ValueError):
                self.ingest()

    def test_unmapped_file_rejected(self):
        self.prepare([self.row()]); self.bind()
        (self.inputs / "unexpected").write_bytes(b"not mapped")
        with self.assertRaisesRegex(ValueError, "mapped file set"):
            self.ingest()

    def test_changed_file_metadata_rejected(self):
        self.prepare([self.row()]); self.bind()
        (self.inputs / "a.pdf").write_bytes(PDF + b"changed")
        with self.assertRaisesRegex(ValueError, "mapped file set"):
            self.ingest()

    def test_wrong_hash_failure_private_and_ledger_preserved(self):
        row = self.row()
        row["originals"][0]["expected_sha256"] = "0" * 64
        self.prepare([row]); self.bind()
        with self.assertRaisesRegex(ValueError, "raw hash mismatch"):
            self.ingest()
        self.assertEqual(self.events()[-1]["status"], "FAILED")
        self.assertEqual(self.count("originals"), 0)
        self.assertEqual(len(list((self.store.root / "private").iterdir())), 1)
        self.assertEqual((self.inputs / "a.pdf").read_bytes(), PDF)
        with self.assertRaisesRegex(ValueError, "retry-failed"):
            self.ingest(resume=True)

    def test_existing_corrupt_CAS_never_skipped(self):
        self.prepare([self.row()]); self.bind()
        h = self.plan["items"][0]["expected_sha256"]
        self.store.blob_path(h).write_bytes(b"x" * len(PDF))
        with self.assertRaisesRegex(ValueError, "corrupt existing blob"):
            self.ingest()
        self.assertEqual(self.events()[-1]["status"], "BLOCKED")
        self.assertEqual(self.count("originals"), 0)

    def test_bounds_count_bytes_and_reserve(self):
        self.prepare([self.row()]); self.bind()
        for key in ["max_file_bytes", "max_total_bytes"]:
            self.limits[key] = 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "byte caps"):
                self.ingest()
            self.limits[key] = 10000
        with patch.object(backfill.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 0, 100)):
            with self.assertRaisesRegex(ValueError, "reserve"):
                self.ingest()
        self.assertEqual(self.events(), [])
        self.assertEqual(self.count("originals"), 0)

    def test_explicit_resume_completed_counts_without_reingest(self):
        self.prepare([self.row()]); self.bind(); self.ingest()
        with patch.object(OriginalStore, "ingest_file", side_effect=AssertionError("must not reingest")):
            result = self.ingest(resume=True)
        self.assertEqual(result["ingested_items"], 1)
        self.assertEqual(self.count("originals"), 1)
        with self.assertRaises(FileExistsError):
            self.ingest()

    def test_resume_changed_limits_rejected(self):
        self.prepare([self.row()]); self.bind(); self.ingest()
        self.limits["max_items"] += 1
        with self.assertRaisesRegex(ValueError, "resume binding changed"):
            self.ingest(resume=True)

    def test_plan_tamper_rejected(self):
        self.prepare([self.row()]); self.bind()
        obj = json.loads(self.plan_file.read_bytes())
        obj["payload"]["counts"]["eligible_items"] = 99
        raw = backfill.encoded(obj); self.plan_file.write_bytes(raw); self.plan_sha = backfill.sha(raw)
        with self.assertRaisesRegex(ValueError, "seal mismatch"):
            self.ingest()

    def test_symlink_refused(self):
        self.prepare([self.row()]); self.bind()
        path = self.inputs / "a.pdf"
        target = self.root / "outside.pdf"
        target.write_bytes(PDF); path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("host does not permit fixture symlinks")
        with self.assertRaises(ValueError):
            self.ingest()

    def test_existing_references_and_queue_unchanged(self):
        prior = self.root / "prior.pdf"
        prior.write_bytes(PDF)
        provenance = Provenance("prior", "https://example.org/prior", UNKNOWN,
                                "PDF", UNKNOWN, UNKNOWN, backfill.sha(PDF))
        original_id = self.store.ingest_file(prior, provenance, max_bytes=1000)
        q = self.root / "queue.jsonl"
        raw = backfill.encoded({"canonical_id": "pending", "binding_sha256": "a" * 64,
                                "reason": "fixture", "observed_at": "2026-10-12T00:00:00Z"}) + b"\n"
        q.write_bytes(raw)
        self.store.import_queue(q, expected_jsonl_sha256=backfill.sha(raw), max_bytes=1000, max_rows=2)
        old_refs = self.store.references("prior")
        old_queue = dict(self.store.queue_item("pending", "a" * 64))
        self.prepare([self.row()]); self.bind(); self.ingest()
        self.assertEqual(old_refs, self.store.references("prior"))
        self.assertEqual(old_queue, dict(self.store.queue_item("pending", "a" * 64)))
        self.assertEqual(self.count("transitions"), 1)
        self.assertEqual(old_refs[0]["id"], original_id)

    def test_file_and_reference_count_caps(self):
        self.prepare([self.row(), self.row("b.pdf", "paper:b")]); self.bind()
        for key in ["max_items", "max_files"]:
            self.limits[key] = 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "count cap"):
                self.ingest()
            self.limits[key] = 20

    def test_journal_chain_tamper_rejected(self):
        self.prepare([self.row()]); self.bind(); self.ingest()
        event_path = self.output / "events" / "00000000.json"
        event = json.loads(event_path.read_bytes())
        event["previous_sha256"] = "0" * 64
        event_path.write_bytes(backfill.encoded(event))
        with self.assertRaisesRegex(ValueError, "chain/binding"):
            self.ingest(resume=True)

    def test_other_reservations_are_explicit_and_enforced(self):
        self.prepare([self.row()]); self.bind()
        self.receipt.pop("other_reserved_bytes"); self.save_receipt()
        with self.assertRaisesRegex(ValueError, "other-reservations"):
            self.ingest()
        self.receipt["other_reserved_bytes"] = 10**13; self.save_receipt()
        with self.assertRaisesRegex(ValueError, "reserve"):
            self.ingest()

    def test_journal_reserve_rechecked_before_each_event(self):
        self.prepare([self.row()]); self.bind()
        calls = 0
        def disk(path):
            nonlocal calls
            if Path(path) == self.output:
                calls += 1
                if calls > 1:
                    return shutil._ntuple_diskusage(100, 0, 100)
            return shutil._ntuple_diskusage(10**12, 0, 10**12)
        with patch.object(backfill.shutil, "disk_usage", side_effect=disk):
            with self.assertRaisesRegex(ValueError, "journal disk reserve"):
                self.ingest()
        self.assertFalse(any(e["status"] == "COMPLETE" for e in self.events()))
        self.assertEqual((self.inputs / "a.pdf").read_bytes(), PDF)
        self.assertEqual(self.count("originals"), 1)


if __name__ == "__main__":
    unittest.main()
