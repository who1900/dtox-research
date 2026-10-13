"""Bounded stream/CAS fixtures only; no network, document parser or real import."""
from contextlib import closing
from dataclasses import replace
import errno
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pipeline import originals as m


PDF = b"%PDF-1.7\nraw stream fixture\n%%EOF"
RESERVE = 10 * 1024**3


class StreamOriginalsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = m.OriginalStore.create(self.root / "store")
        self.provenance = m.Provenance("paper:one", "https://example.org/one", m.UNKNOWN,
            "PDF", m.UNKNOWN, m.UNKNOWN, hashlib.sha256(PDF).hexdigest())
        self.space = patch.object(m.shutil, "disk_usage", return_value=type("Usage", (), {"free": 100 * RESERVE})())
        self.space.start()
        self.addCleanup(self.space.stop)

    def ingest(self, data=PDF, provenance=None, **changes):
        options = dict(max_bytes=1000, expected_bytes=len(data), source_name="original.pdf")
        options.update(changes)
        return self.store.ingest_stream(io.BytesIO(data), provenance or self.provenance, **options)

    def reconcile(self, provenance=None, **changes):
        options = dict(max_bytes=1000, expected_bytes=len(PDF), source_name="original.pdf")
        options.update(changes)
        return self.store.add_ref_from_verified_blob(provenance or self.provenance, **options)

    def counts(self):
        with closing(sqlite3.connect(self.store.db_path)) as db:
            return tuple(db.execute("SELECT count(*) FROM " + t).fetchone()[0]
                         for t in ("blobs", "originals", "acquisition_queue", "transitions"))

    def partials(self):
        return list((self.store.root / "private").iterdir())

    def test_stream_success_without_landing_and_default_ingest_unchanged(self):
        first = self.ingest()
        self.assertEqual(self.store.verify_blob(self.provenance.raw_sha256).read_bytes(), PDF)
        self.assertEqual(self.counts(), (1, 1, 0, 0))
        self.assertEqual(self.partials(), [])
        source = self.root / "source.pdf"
        source.write_bytes(PDF)
        self.assertEqual(self.store.ingest_file(source, self.provenance, max_bytes=1000), first)
        self.assertEqual(source.read_bytes(), PDF)

    def test_duplicate_streams_and_logical_provenance_refs(self):
        first = self.ingest()
        self.assertEqual(self.ingest(), first)
        second = self.ingest(provenance=replace(self.provenance, canonical_id="paper:alias"))
        third = self.reconcile(replace(self.provenance, source_url="https://mirror.example/one", edition="v2"))
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(self.counts(), (1, 3, 0, 0))
        self.assertEqual(len(list((self.store.root / "blobs").iterdir())), 1)
        self.assertEqual(self.partials(), [])

    def test_eof_after_exact_length_is_consumed_and_trailer_failure_blocks(self):
        class Reader:
            def __init__(self):
                self.calls = 0
            def read(self, size):
                self.calls += 1
                if self.calls == 1:
                    return PDF
                raise OSError("source trailer SHA/stat mismatch")
        reader = Reader()
        with self.assertRaisesRegex(OSError, "trailer") as error:
            self.store.ingest_stream(reader, self.provenance, max_bytes=1000,
                                     expected_bytes=len(PDF), source_name="a.pdf")
        self.assertEqual(reader.calls, 2)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.partials()[0].read_bytes(), PDF)
        self.assertIn("private evidence", error.exception.__notes__[0])

    def test_read_exception_and_short_stream_retain_partial(self):
        class Reader:
            def __init__(self):
                self.first = True
            def read(self, size):
                if self.first:
                    self.first = False
                    return PDF[:8]
                raise InterruptedError("transport interrupted")
        with self.assertRaises(InterruptedError):
            self.store.ingest_stream(Reader(), self.provenance, max_bytes=1000,
                                     expected_bytes=len(PDF), source_name="a.pdf")
        with self.assertRaises(m.OriginalError):
            self.ingest(PDF[:8], expected_bytes=len(PDF))
        self.assertEqual(sorted(p.read_bytes() for p in self.partials()), [PDF[:8], PDF[:8]])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_wrong_hash_size_and_kind_never_publish(self):
        for data, provenance, options in (
            (PDF, replace(self.provenance, raw_sha256="0" * 64), {}),
            (PDF + b"x", self.provenance, {"expected_bytes": len(PDF)}),
            (b"not pdf", replace(self.provenance, raw_sha256=hashlib.sha256(b"not pdf").hexdigest()), {})):
            with self.subTest(data=data, options=options), self.assertRaises(m.OriginalError):
                self.ingest(data, provenance, **options)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(len(self.partials()), 3)
        self.assertEqual(list((self.store.root / "blobs").iterdir()), [])

    def test_explicit_byte_bounds_provenance_and_basename(self):
        for options in ({"expected_bytes": 0}, {"expected_bytes": True}, {"max_bytes": 1},
                        {"max_bytes": False}, {"source_name": "../a.pdf"}, {"source_name": "C:a.pdf"},
                        {"reserve_bytes": RESERVE - 1}, {"reserve_bytes": True}):
            with self.subTest(options=options), self.assertRaises(m.OriginalError):
                self.ingest(**options)
        with self.assertRaises(m.OriginalError):
            self.ingest(provenance=replace(self.provenance, source_url="file:///secret"))
        self.assertEqual(self.partials(), [])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_nonbytes_and_oversized_read_response_rejected(self):
        for response in (None, "text", b"x" * 10000):
            reader = type("Reader", (), {"read": lambda _, n: response})()
            with self.subTest(response_type=type(response).__name__), self.assertRaises(m.OriginalError):
                self.store.ingest_stream(reader, self.provenance, max_bytes=1000,
                                         expected_bytes=len(PDF), source_name="a.pdf")
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_fresh_reserve_preallocation_and_precommit(self):
        usage = type("Usage", (), {"free": RESERVE})()
        with patch.object(m.shutil, "disk_usage", return_value=usage), self.assertRaises(m.OriginalError):
            self.ingest()
        self.assertEqual(self.partials(), [])
        answers = [type("Usage", (), {"free": RESERVE + len(PDF)})(), type("Usage", (), {"free": RESERVE - 1})()]
        with patch.object(m.shutil, "disk_usage", side_effect=answers), self.assertRaises(m.OriginalError):
            self.ingest()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.partials()[0].read_bytes(), PDF)

    def test_exdev_no_copy_fallback_or_reference(self):
        with patch.object(m.os, "link", side_effect=OSError(errno.EXDEV, "different filesystem")):
            with self.assertRaises(OSError) as error:
                self.ingest()
        self.assertEqual(error.exception.errno, errno.EXDEV)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.partials()[0].read_bytes(), PDF)

    def test_target_collision_corruption_never_overwritten(self):
        target = self.store.blob_path(self.provenance.raw_sha256)
        target.write_bytes(b"X" * len(PDF))
        with self.assertRaises(m.OriginalError):
            self.ingest()
        with self.assertRaises(m.OriginalError):
            self.reconcile()
        self.assertEqual(target.read_bytes(), b"X" * len(PDF))
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_crash_after_link_reconcile_without_new_source_body(self):
        publish = self.store._publish
        def crash(private, target):
            publish(private, target)
            raise InterruptedError("after link before manifest")
        with patch.object(self.store, "_publish", side_effect=crash), self.assertRaises(InterruptedError):
            self.ingest()
        target = self.store.verify_blob(self.provenance.raw_sha256, len(PDF))
        evidence = self.partials()[0]
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), (evidence.stat().st_dev, evidence.stat().st_ino))
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        original_id = self.reconcile()
        self.assertEqual(self.reconcile(), original_id)
        self.assertEqual(self.counts(), (1, 1, 0, 0))
        self.assertEqual(evidence.read_bytes(), PDF)

    def test_failure_after_commit_idempotently_reconciles(self):
        unlink = Path.unlink
        def crash(path, *args, **kwargs):
            if path.parent == self.store.root / "private":
                raise InterruptedError("after commit before cleanup")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", crash), self.assertRaises(InterruptedError):
            self.ingest()
        self.assertEqual(self.counts(), (1, 1, 0, 0))
        first = self.reconcile()
        self.assertEqual(self.reconcile(), first)
        self.assertEqual(len(self.partials()), 1)

    def test_fsync_failure_no_reference_and_missing_reconcile_blocks(self):
        with patch.object(m.os, "fsync", side_effect=OSError("flush failed")), self.assertRaises(OSError):
            self.ingest()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        with self.assertRaises(FileNotFoundError):
            self.reconcile()

    def test_reference_transaction_failure_keeps_verified_orphan(self):
        with self.store._db() as db:
            db.execute("INSERT INTO blobs VALUES (?,?)", (self.provenance.raw_sha256, len(PDF) + 1))
        with self.assertRaisesRegex(m.OriginalError, "manifest size"):
            self.ingest()
        self.assertEqual(self.counts(), (1, 0, 0, 0))
        self.assertEqual(self.store.verify_blob(self.provenance.raw_sha256).read_bytes(), PDF)
        self.assertEqual(self.partials()[0].read_bytes(), PDF)

    def test_symlink_target_or_private_directory_rejected(self):
        source = self.root / "outside.pdf"
        source.write_bytes(PDF)
        target = self.store.blob_path(self.provenance.raw_sha256)
        try:
            target.symlink_to(source)
        except OSError as exc:
            self.skipTest("symlink privilege unavailable: " + str(exc))
        with self.assertRaises(m.OriginalError):
            self.ingest()
        with self.assertRaises(m.OriginalError):
            self.reconcile()
        self.assertEqual(source.read_bytes(), PDF)
        target.unlink()
        self.store = m.OriginalStore.create(self.root / "second-store")
        private = self.store.root / "private"
        private.rmdir()
        private.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(m.OriginalError):
            self.ingest()

    def test_create_only_private_and_reparse_guard(self):
        fixed = type("UUID", (), {"hex": "fixed"})()
        private = self.store.root / "private" / "fixed.partial"
        private.write_bytes(b"prior failure evidence")
        with patch.object(m.uuid, "uuid4", return_value=fixed), self.assertRaises(FileExistsError):
            self.ingest()
        self.assertEqual(private.read_bytes(), b"prior failure evidence")
        real = Path.lstat
        def reparse(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if path == self.store.root / "private":
                return type("Stat", (), {"st_mode": result.st_mode, "st_file_attributes": 0x400})()
            return result
        with patch.object(Path, "lstat", reparse), self.assertRaises(m.OriginalError):
            self.ingest()

    def test_bounded_read_size_and_no_caller_reader_close(self):
        body = PDF + b"x" * 140000
        provenance = replace(self.provenance, raw_sha256=hashlib.sha256(body).hexdigest())
        class Reader(io.BytesIO):
            def read(self, size):
                if not 0 < size <= 65536:
                    raise AssertionError("unbounded read")
                return super().read(size)
        reader = Reader(body)
        self.store.ingest_stream(reader, provenance, max_bytes=len(body), expected_bytes=len(body), source_name="a.pdf")
        self.assertFalse(reader.closed)
        self.assertEqual(self.store.verify_blob(provenance.raw_sha256).stat().st_size, len(body))

    def test_existing_queue_and_transitions_are_not_promoted(self):
        with self.store._db() as db:
            db.execute("INSERT INTO acquisition_queue VALUES(?,?,?,?,NULL)",
                (self.provenance.canonical_id, "a" * 64, "{}", "acquisition_pending"))
        before = self.counts()
        self.ingest()
        with self.store._db() as db:
            row = dict(db.execute("SELECT * FROM acquisition_queue").fetchone())
        self.assertEqual(row["status"], "acquisition_pending")
        self.assertIsNone(row["original_id"])
        self.assertEqual(row["imported_row"], "{}")
        self.assertEqual(self.counts()[2:], before[2:])


if __name__ == "__main__":
    unittest.main()
