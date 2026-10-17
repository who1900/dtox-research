import concurrent.futures
from contextlib import closing
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from pipeline.originals import OriginalError, OriginalStore, Provenance, UNKNOWN


STAMP = "2026-10-12T09:00:00+05:00"
PDF = b"%PDF-1.7\nraw fixture\n%%EOF"
HTML = b"<!doctype html><html><body>raw fixture</body></html>"
XML = b'<?xml version="1.0"?><article>raw fixture</article>'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class OriginalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = OriginalStore.create(self.base / "originals")
        self.provenance = Provenance("arxiv:1234.5678", "https://example.org/paper",
                                     "https://example.org/paper.pdf", "PDF", UNKNOWN, STAMP, sha(PDF))

    def fixture(self, name="input.pdf", data=PDF):
        path = self.base / name
        path.write_bytes(data)
        return path

    def ingest(self, data=PDF, name="input.pdf", provenance=None, **kwargs):
        return self.store.ingest_file(self.fixture(name, data), provenance or self.provenance,
                                      max_bytes=kwargs.get("max_bytes", 10000))

    def count(self, table):
        with closing(sqlite3.connect(self.store.db_path)) as db:
            return db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def queue(self, rows=None):
        if rows is None:
            rows = [{"canonical_id": self.provenance.canonical_id, "binding_sha256": sha(b"historical document row"),
                     "reason": "actual-v3 missing raw original", "observed_at": STAMP}]
        data = b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"
        path = self.fixture("queue.jsonl", data)
        self.store.import_queue(path, expected_jsonl_sha256=sha(data), max_bytes=10000, max_rows=100)
        return rows[0]

    def identity(self, row, **changes):
        evidence = {"canonical_id": row["canonical_id"], "binding_sha256": row["binding_sha256"],
                    "raw_sha256": self.provenance.raw_sha256, "identity_checked": True,
                    "method": "manually checked source identity"}
        return dict(evidence, **changes)

    def attach(self, row, original_id, evidence=None):
        self.store.attach_original(row["canonical_id"], row["binding_sha256"], original_id,
                                   identity_evidence=evidence or self.identity(row), reason="identity checked", observed_at=STAMP)

    def test_raw_formats_and_representations(self):
        for kind, data in (("PDF", PDF), ("HTML", HTML), ("XML", XML)):
            with self.subTest(kind=kind):
                p = replace(self.provenance, original_kind=kind, raw_sha256=sha(data))
                self.ingest(data, kind + ".raw", p)
                self.assertEqual(self.store.verify_blob(sha(data)).read_bytes(), data)
        self.assertEqual(len(self.store.references(self.provenance.canonical_id)), 3)

    def test_duplicate_bytes_and_reference_idempotency(self):
        first = self.ingest()
        second = self.ingest(name="different.pdf")
        self.assertEqual(first, second)
        self.assertEqual(self.count("blobs"), 1)
        self.assertEqual(self.count("originals"), 1)
        self.assertEqual(len(list((self.store.root / "blobs").iterdir())), 1)
        self.assertEqual(list((self.store.root / "private").iterdir()), [])

    def test_layout_accepts_journal_unlinked_during_metadata_lookup(self):
        journal = Path(str(self.store.db_path) + "-journal")
        journal.write_bytes(b"transient rollback journal")
        original_lstat = Path.lstat
        original_stat = Path.stat
        journal_lookups = 0

        def disappearing_lstat(path, *args, **kwargs):
            nonlocal journal_lookups
            info = original_lstat(path, *args, **kwargs)
            if path == journal:
                journal_lookups += 1
                if journal_lookups > 1:  # after the shared path/symlink check
                    path.unlink(missing_ok=True)
            return info

        def disappearing_stat(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path == journal and kwargs.get("follow_symlinks", True):
                path.unlink(missing_ok=True)
            return info

        # Model SQLite unlinking a journal immediately after a successful lookup.
        # Both pathname and non-following lookups see the same interleaving.
        with patch.object(Path, "stat", disappearing_stat), patch.object(Path, "lstat", disappearing_lstat):
            self.store._layout()
        self.assertFalse(journal.exists())

    def test_layout_rejects_unsafe_manifest_and_requires_primary(self):
        journal = Path(str(self.store.db_path) + "-journal")
        for kind in ("directory", "hardlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    journal.mkdir()
                else:
                    os.link(self.store.db_path, journal)
                try:
                    with self.assertRaisesRegex(OriginalError, "unsafe manifest file"):
                        self.store._layout()
                finally:
                    journal.rmdir() if kind == "directory" else journal.unlink()
        self.store.db_path.unlink()
        with self.assertRaisesRegex(OriginalError, "missing manifest"):
            self.store._layout()

    def test_versions_and_sources_share_blob(self):
        self.ingest()
        self.ingest(provenance=replace(self.provenance, edition="v2", source_url="https://mirror.example/paper"))
        self.ingest(provenance=replace(self.provenance, canonical_id="doi:other", edition="v1"))
        refs = self.store.references(self.provenance.canonical_id)
        self.assertEqual([r["edition"] for r in refs], [UNKNOWN, "v2"])
        self.assertEqual(self.count("originals"), 3)
        self.assertEqual(self.count("blobs"), 1)

    def test_raw_tar_and_zip(self):
        for mode in ("tar", "tar.gz", "zip"):
            with self.subTest(mode=mode):
                buffer = io.BytesIO()
                if mode == "zip":
                    with zipfile.ZipFile(buffer, "w") as archive:
                        archive.writestr("source.tex", "\\documentclass{article}")
                else:
                    with tarfile.open(fileobj=buffer, mode="w:gz" if mode.endswith("gz") else "w") as archive:
                        entry = tarfile.TarInfo("source.tex")
                        entry.size = 5
                        archive.addfile(entry, io.BytesIO(b"hello"))
                data = buffer.getvalue()
                p = replace(self.provenance, original_kind="rawTexarchive", raw_sha256=sha(data))
                self.ingest(data, "source." + mode, p, max_bytes=100000)
        self.assertEqual(self.count("blobs"), 3)

    def test_assembled_tex_refused_even_with_archive_kind(self):
        data = b"\\documentclass{article}\nassembled content"
        p = replace(self.provenance, original_kind="rawTexarchive", raw_sha256=sha(data))
        for name in ("assembledsource.tex", "disguised.tar.gz"):
            with self.subTest(name=name), self.assertRaises(OriginalError):
                self.ingest(data, name, p)
        self.assertEqual(self.count("originals"), 0)

    def test_corrupted_existing_blob_not_overwritten(self):
        self.ingest()
        target = self.store.blob_path(sha(PDF))
        target.write_bytes(b"X" * len(PDF))
        with self.assertRaises(OriginalError):
            self.ingest(name="copy.pdf")
        self.assertEqual(target.read_bytes(), b"X" * len(PDF))
        self.assertEqual(self.count("originals"), 1)
        self.assertTrue(list((self.store.root / "private").iterdir()))

    def test_wrong_hash_preserves_input_and_evidence(self):
        path = self.fixture()
        before = path.stat()
        with self.assertRaises(OriginalError):
            self.store.ingest_file(path, replace(self.provenance, raw_sha256="0" * 64), max_bytes=100)
        self.assertEqual(path.read_bytes(), PDF)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.count("blobs"), 0)
        self.assertEqual(len(list((self.store.root / "private").iterdir())), 1)

    def test_oversize_and_explicit_bound(self):
        path = self.fixture()
        for limit in (1, 0, -1, True):
            with self.subTest(limit=limit), self.assertRaises(OriginalError):
                self.store.ingest_file(path, self.provenance, max_bytes=limit)
        with self.assertRaises(TypeError):
            self.store.ingest_file(path, self.provenance)
        self.assertEqual(self.count("originals"), 0)

    def test_traversal_path_rejected_ids_never_used_as_paths(self):
        path = self.fixture()
        with self.assertRaises(OriginalError):
            self.store.ingest_file(path.parent / ".." / path.parent.name / path.name, self.provenance, max_bytes=100)
        malicious_id = "../../CON:arbitrary/id"
        self.ingest(provenance=replace(self.provenance, canonical_id=malicious_id))
        self.assertEqual(len(self.store.references(malicious_id)), 1)
        self.assertEqual([p.name for p in (self.store.root / "blobs").iterdir()], [sha(PDF)])
        with self.assertRaises(OriginalError):
            self.store.blob_path("../manifest.sqlite3")

    def symlink(self, target, link, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest("symlink privilege unavailable: " + str(exc))

    def test_symlink_input_rejected(self):
        path = self.fixture()
        link = self.base / "link.pdf"
        self.symlink(path, link)
        with self.assertRaises(OriginalError):
            self.store.ingest_file(link, self.provenance, max_bytes=100)

    def test_symlink_output_escape_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        blobs = self.store.root / "blobs"
        blobs.rmdir()
        self.symlink(outside, blobs, True)
        with self.assertRaises(OriginalError):
            self.ingest()
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_blob_rejected(self):
        path = self.fixture()
        self.symlink(path, self.store.blob_path(sha(PDF)))
        with self.assertRaises(OriginalError):
            self.ingest(name="copy.pdf")
        self.assertEqual(path.read_bytes(), PDF)

    def test_reparse_attribute_rejected(self):
        real = Path.lstat

        def reparse(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if path == self.base / "input.pdf":
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
            return result

        path = self.fixture()
        with patch.object(Path, "lstat", reparse), self.assertRaises(OriginalError):
            self.store.ingest_file(path, self.provenance, max_bytes=100)

    def test_interrupted_before_publication_leaves_no_reference(self):
        with patch.object(self.store, "_publish", side_effect=InterruptedError("before publication")):
            with self.assertRaises(InterruptedError):
                self.ingest()
        self.assertEqual(self.count("originals"), 0)
        self.assertEqual(self.count("blobs"), 0)
        self.assertFalse(self.store.blob_path(sha(PDF)).exists())
        self.assertTrue(list((self.store.root / "private").iterdir()))

    def test_interrupted_after_publication_orphan_is_safe_and_reusable(self):
        publish = self.store._publish

        def interrupted(private, target):
            publish(private, target)
            raise InterruptedError("after publication, before manifest")

        with patch.object(self.store, "_publish", side_effect=interrupted):
            with self.assertRaises(InterruptedError):
                self.ingest()
        self.assertEqual(self.count("originals"), 0)
        self.assertEqual(self.count("blobs"), 0)
        self.assertEqual(self.store.verify_blob(sha(PDF)).read_bytes(), PDF)
        self.ingest(name="retry.pdf")
        self.assertEqual(self.count("originals"), 1)
        self.assertEqual(len(list((self.store.root / "private").iterdir())), 1)

    def test_fsync_failure_does_not_advertise_original(self):
        with patch("pipeline.originals.os.fsync", side_effect=OSError("flush failed")):
            with self.assertRaises(OSError):
                self.ingest()
        self.assertEqual(self.count("originals"), 0)
        self.assertFalse(self.store.blob_path(sha(PDF)).exists())

    def test_concurrent_duplicate_ingestion(self):
        paths = [self.fixture(str(i) + ".pdf") for i in range(6)]

        def ingest(path):
            return OriginalStore(self.store.root).ingest_file(path, self.provenance, max_bytes=100)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(ingest, paths))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.count("blobs"), 1)
        self.assertEqual(self.count("originals"), 1)
        self.assertEqual(list((self.store.root / "private").iterdir()), [])

    def test_credentials_and_bad_provenance_rejected(self):
        changes = ({"source_url": "https://user:password@example.org/paper"},
                   {"retrieval_url": "https://user@example.org/raw"},
                   {"retrieval_url": "file:///secret"}, {"retrieved_at": "2026-10-12"},
                   {"edition": ""}, {"raw_sha256": "not a hash"}, {"original_kind": "assembledTex"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(OriginalError):
                self.ingest(provenance=replace(self.provenance, **change))
        self.assertEqual(self.count("originals"), 0)

    def test_known_retrieval_provenance_preserved(self):
        for timestamp in ("2026-10-12T04:00:00Z", STAMP):
            with self.subTest(timestamp=timestamp):
                self.ingest(provenance=replace(self.provenance, retrieved_at=timestamp))
        refs = self.store.references(self.provenance.canonical_id)
        self.assertEqual([r["retrieved_at"] for r in refs], ["2026-10-12T04:00:00Z", STAMP])
        self.assertTrue(all(r["retrieval_url"] == self.provenance.retrieval_url for r in refs))

    def test_unknown_retrieval_fields_preserved_independently(self):
        variants = ({"retrieved_at": UNKNOWN}, {"retrieval_url": UNKNOWN},
                    {"retrieved_at": UNKNOWN, "retrieval_url": UNKNOWN})
        for changes in variants:
            with self.subTest(changes=changes):
                p = replace(self.provenance, **changes)
                original_id = self.ingest(provenance=p)
                self.assertEqual(self.ingest(name="duplicate.pdf", provenance=p), original_id)
                row = next(r for r in self.store.references(p.canonical_id) if r["id"] == original_id)
                self.assertEqual(row["retrieved_at"], p.retrieved_at)
                self.assertEqual(row["retrieval_url"], p.retrieval_url)
                self.assertEqual(row["source_url"], p.source_url)
        self.assertEqual(self.count("originals"), 3)
        self.assertEqual(self.count("blobs"), 1)

    def test_unknown_url_does_not_allow_invalid_retrieval_timestamp(self):
        for timestamp in ("unknown", "", None, "2026-10-12", "2026-10-12T04:00:00", "not-time"):
            with self.subTest(timestamp=timestamp), self.assertRaises(OriginalError):
                self.ingest(provenance=replace(self.provenance, retrieval_url=UNKNOWN, retrieved_at=timestamp))

    def test_unknown_timestamp_does_not_allow_credentials_or_unknown_source(self):
        for url in ("https://user:password@example.org/raw", "https://user@example.org/raw",
                    "file:///raw", "unknown", "", None):
            with self.subTest(url=url), self.assertRaises(OriginalError):
                self.ingest(provenance=replace(self.provenance, retrieved_at=UNKNOWN, retrieval_url=url))
        for source in (UNKNOWN, "https://user:password@example.org/paper"):
            with self.subTest(source=source), self.assertRaises(OriginalError):
                self.ingest(provenance=replace(self.provenance, retrieved_at=UNKNOWN,
                                              retrieval_url=UNKNOWN, source_url=source))

    def test_transition_observations_still_require_timezone_timestamp(self):
        row = self.queue()
        original_id = self.ingest(provenance=replace(self.provenance, retrieved_at=UNKNOWN, retrieval_url=UNKNOWN))
        for timestamp in (UNKNOWN, "2026-10-12T04:00:00", "2026-10-12"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(OriginalError):
                    self.store.transition(row["canonical_id"], row["binding_sha256"], "identity_unresolved",
                                          reason="review", observed_at=timestamp, evidence={"observation": "review required"})
                with self.assertRaises(OriginalError):
                    self.store.attach_original(row["canonical_id"], row["binding_sha256"], original_id,
                                               identity_evidence=self.identity(row), reason="checked", observed_at=timestamp)
        self.store.transition(row["canonical_id"], row["binding_sha256"], "identity_unresolved",
                              reason="review", observed_at="2026-10-12T04:00:00Z", evidence={"observation": "review required"})
        with self.assertRaises(OriginalError):
            self.store.transition(row["canonical_id"], row["binding_sha256"], "permanently_unavailable",
                                  reason="absence", observed_at=STAMP,
                                  evidence={"scope": "document", "kind": "authoritative_document_absence",
                                            "authority_url": UNKNOWN, "statement": "absence claimed"})
        self.assertEqual(self.count("transitions"), 2)

    def test_queue_import_observation_cannot_be_unknown(self):
        for timestamp in (UNKNOWN, "2026-10-12T04:00:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(OriginalError):
                self.queue([{"canonical_id": "id", "binding_sha256": sha(b"row"),
                             "reason": "missing", "observed_at": timestamp}])
        self.assertEqual(self.count("acquisition_queue"), 0)

    def test_dedicated_root_and_schema_guard(self):
        with self.assertRaises(FileExistsError):
            OriginalStore.create(self.store.root)
        prod = self.base / "prod"
        prod.mkdir()
        db_path = prod / "manifest.sqlite3"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("CREATE TABLE production (id INTEGER)")
        before = db_path.read_bytes()
        with self.assertRaises(OriginalError):
            OriginalStore(prod)
        self.assertEqual(db_path.read_bytes(), before)
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(OriginalError):
            OriginalStore(self.store.root)

    def test_queue_import_idempotent_and_hash_bound(self):
        row = self.queue()
        self.queue()
        self.assertEqual(self.count("acquisition_queue"), 1)
        self.assertEqual(self.count("transitions"), 1)
        self.assertEqual(self.store.queue_item(row["canonical_id"], row["binding_sha256"])["status"], "acquisition_pending")
        with self.assertRaises(OriginalError):
            self.store.import_queue(self.base / "queue.jsonl", expected_jsonl_sha256="0" * 64, max_bytes=10000, max_rows=100)

    def test_queue_import_cannot_promote_and_rolls_back_batch(self):
        row = {"canonical_id": "id", "binding_sha256": sha(b"row"), "reason": "missing", "observed_at": STAMP}
        with self.assertRaises(OriginalError):
            self.queue([row, dict(row, canonical_id="other", status="source_available")])
        self.assertEqual(self.count("acquisition_queue"), 0)

    def test_queue_conflicting_binding_import_refused(self):
        row = self.queue()
        with self.assertRaises(OriginalError):
            self.queue([dict(row, reason="changed")])
        self.assertEqual(self.count("transitions"), 1)

    def test_single_endpoint_404_not_permanent_absence(self):
        row = self.queue()
        evidence = {"endpoint": "https://example.org/raw", "http_status": 404}
        self.store.transition(row["canonical_id"], row["binding_sha256"], "temporarily_unavailable",
                              reason="single endpoint 404", observed_at=STAMP, evidence=evidence)
        with self.assertRaises(OriginalError):
            self.store.transition(row["canonical_id"], row["binding_sha256"], "permanently_unavailable",
                                  reason="404", observed_at=STAMP, evidence=evidence)
        self.assertEqual(self.store.queue_item(row["canonical_id"], row["binding_sha256"])["status"], "temporarily_unavailable")

    def test_unavailable_and_unresolved_transitions_require_evidence(self):
        row = self.queue()
        for status in ("acquisition_pending", "temporarily_unavailable", "identity_unresolved"):
            self.store.transition(row["canonical_id"], row["binding_sha256"], status,
                                  reason="review", observed_at=STAMP, evidence={"observation": "review required"})
        for reason, timestamp, evidence in (("", STAMP, {"a": 1}), ("review", "bad", {"a": 1}), ("review", STAMP, {})):
            with self.assertRaises(OriginalError):
                self.store.transition(row["canonical_id"], row["binding_sha256"], "identity_unresolved",
                                      reason=reason, observed_at=timestamp, evidence=evidence)

    def test_document_wide_absence_requires_authority(self):
        row = self.queue()
        self.store.transition(row["canonical_id"], row["binding_sha256"], "permanently_unavailable",
                              reason="authoritative absence", observed_at=STAMP,
                              evidence={"scope": "document", "kind": "authoritative_document_absence",
                                        "authority_url": "https://publisher.example/notice", "statement": "document withdrawn and unavailable"})
        self.assertEqual(self.store.queue_item(row["canonical_id"], row["binding_sha256"])["status"], "permanently_unavailable")

    def test_availability_requires_explicit_matching_identity_and_blob(self):
        row = self.queue()
        original_id = self.ingest()
        self.assertEqual(self.store.queue_item(row["canonical_id"], row["binding_sha256"])["status"], "acquisition_pending")
        with self.assertRaises(OriginalError):
            self.store.transition(row["canonical_id"], row["binding_sha256"], "source_available",
                                  reason="string promotion", observed_at=STAMP, evidence={"raw_sha256": sha(PDF)})
        for change in ({"identity_checked": False}, {"binding_sha256": "0" * 64},
                       {"raw_sha256": "0" * 64}, {"canonical_id": "other"}, {"method": ""}):
            with self.subTest(change=change), self.assertRaises(OriginalError):
                self.attach(row, original_id, self.identity(row, **change))
        with self.assertRaises(OriginalError):
            self.attach(row, 99999)
        self.attach(row, original_id)
        self.assertEqual(self.store.queue_item(row["canonical_id"], row["binding_sha256"])["status"], "source_available")

    def test_attachment_cannot_use_other_document_or_unknown_binding(self):
        row = self.queue()
        other_id = self.ingest(provenance=replace(self.provenance, canonical_id="other"))
        with self.assertRaises(OriginalError):
            self.attach(row, other_id)
        original_id = self.ingest(name="copy.pdf")
        wrong = dict(row, binding_sha256="1" * 64)
        with self.assertRaises(OriginalError):
            self.attach(wrong, original_id)
        self.assertEqual(self.count("transitions"), 1)

    def test_available_read_and_attachment_reverify_blob(self):
        row = self.queue()
        original_id = self.ingest()
        self.attach(row, original_id)
        target = self.store.blob_path(sha(PDF))
        target.write_bytes(b"bad")
        with self.assertRaises(OriginalError):
            self.store.queue_item(row["canonical_id"], row["binding_sha256"])
        with self.assertRaises(OriginalError):
            self.attach(row, original_id)


if __name__ == "__main__":
    unittest.main()
