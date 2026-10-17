"""Local ORIGINAL store, not a downloader or a document-identity resolver.

Create a NEW dedicated directory with OriginalStore.create(). Raw-byte hashes
are distinct from historical/operational hashes in imported queue rows. Callers
must check identity before attach_original(); ingestion alone never promotes a
queue item. UNKNOWN means no historical edition evidence.

Publication uses a fsynced private file and an exclusive hard link, then commits
the manifest. Failed attempts retain private files/unreferenced blobs for manual
inspection, never automatic deletion. A crash can leave an orphan, not a manifest
entry for a half-written blob. Directory fsync is used on POSIX; Python's stdlib
does not expose durable directory flush on Windows. Keep the dedicated root
private: this is not a sandbox against a hostile process replacing directories
between syscalls, nor a WORM filesystem. External corruption is detected on use.
"""

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tarfile
import uuid
import zipfile
from urllib.parse import urlsplit


UNKNOWN = "UNKNOWN"
KINDS = {"PDF", "HTML", "XML", "rawTexarchive"}
STATUSES = {"acquisition_pending", "temporarily_unavailable",
            "permanently_unavailable", "identity_unresolved", "source_available"}
APP_ID = 0x4F524947
SCHEMA = (
    "CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL CHECK(size>0))",
    "CREATE TABLE originals (id INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL, "
    "source_url TEXT NOT NULL, retrieval_url TEXT NOT NULL, original_kind TEXT NOT NULL, "
    "edition TEXT NOT NULL, retrieved_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL REFERENCES blobs(sha256), "
    "UNIQUE(canonical_id,source_url,retrieval_url,original_kind,edition,retrieved_at,raw_sha256))",
    "CREATE TABLE acquisition_queue (canonical_id TEXT NOT NULL, binding_sha256 TEXT NOT NULL, "
    "imported_row TEXT NOT NULL, status TEXT NOT NULL, original_id INTEGER REFERENCES originals(id), "
    "PRIMARY KEY(canonical_id,binding_sha256))",
    "CREATE TABLE transitions (id INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL, "
    "binding_sha256 TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, "
    "observed_at TEXT NOT NULL, evidence TEXT NOT NULL, original_id INTEGER REFERENCES originals(id), "
    "FOREIGN KEY(canonical_id,binding_sha256) REFERENCES acquisition_queue(canonical_id,binding_sha256))",
)


class OriginalError(ValueError):
    """Fail closed; filesystem evidence from failed ingestion is retained."""


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise OriginalError("invalid metadata label")
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise OriginalError("expected lowercase SHA256")
    return value


def _timestamp(value):
    _label(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise OriginalError("invalid timezone-aware timestamp") from exc
    return value


def _url(value):
    _label(value)
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or "@" in parsed.netloc or "\\" in value:
            raise ValueError("unsafe URL")
        parsed.port
    except ValueError as exc:
        raise OriginalError("URL must be HTTP(S), without credentials") from exc
    return value


def _plain(path):
    path = Path(path)
    if ".." in path.parts:
        raise OriginalError("path traversal")
    path = path.absolute()
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OriginalError("symlink/reparse path rejected")
    return path


def _regular(path):
    path = _plain(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        disk = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (disk.st_dev, disk.st_ino):
            raise OriginalError("not a stable regular file")
        _plain(path)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _sync_dir(path):
    _plain(path)
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class Provenance:
    canonical_id: str
    source_url: str
    retrieval_url: str
    original_kind: str
    edition: str
    retrieved_at: str
    raw_sha256: str

    def validate(self):
        _label(self.canonical_id)
        _url(self.source_url)
        if self.retrieval_url != UNKNOWN:
            _url(self.retrieval_url)
        _label(self.edition)
        if self.retrieved_at != UNKNOWN:
            _timestamp(self.retrieved_at)
        _hash(self.raw_sha256)
        if self.original_kind not in KINDS:
            raise OriginalError("unsupported original kind")


class OriginalStore:
    @classmethod
    def create(cls, root):
        root = _plain(root)
        root.mkdir(mode=0o700)  # Exclusive: never adopt a production/unknown DB.
        (root / "blobs").mkdir(mode=0o700)
        (root / "private").mkdir(mode=0o700)
        with closing(sqlite3.connect(root / "manifest.sqlite3")) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            for statement in SCHEMA:
                db.execute(statement)
            db.execute(f"PRAGMA application_id={APP_ID}")
            db.execute("PRAGMA user_version=1")
        _sync_dir(root)
        _sync_dir(root.parent)
        return cls(root)

    def __init__(self, root):
        self.root = _plain(root)
        self.db_path = self.root / "manifest.sqlite3"
        with self._db() as db:
            self._schema(db)

    def _layout(self):
        for name in ("", "blobs", "private"):
            path = _plain(self.root / name)
            if not path.is_dir():
                raise OriginalError("missing store directory")
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = _plain(str(self.db_path) + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                if not suffix:
                    raise OriginalError("missing manifest; use create on a new dedicated root")
                # SQLite can remove a rollback journal between layout checks.
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OriginalError("unsafe manifest file")

    @contextmanager
    def _db(self):
        self._layout()
        db = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            self._schema(db)
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _schema(db):
        if (db.execute("PRAGMA application_id").fetchone()[0] != APP_ID
                or db.execute("PRAGMA user_version").fetchone()[0] != 1):
            raise OriginalError("not an ORIGINAL manifest/schema")
        actual = {row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")}
        if actual != set(SCHEMA):
            raise OriginalError("unexpected ORIGINAL schema")

    def blob_path(self, raw_sha256):
        return _plain(self.root / "blobs" / _hash(raw_sha256))

    def verify_blob(self, raw_sha256, size=None):
        self._layout()
        path = self.blob_path(raw_sha256)
        digest = hashlib.sha256()
        with _regular(path) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size <= 0 or (size is not None and before.st_size != size):
                raise OriginalError("blob size mismatch")
            remaining = before.st_size
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise OriginalError("truncated blob")
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(stream.fileno())
            if stream.read(1) or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OriginalError("blob changed during verification")
        current = _plain(path).stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise OriginalError("blob path changed during verification")
        if digest.hexdigest() != raw_sha256:
            raise OriginalError("corrupt existing blob")
        return path

    @staticmethod
    def _check_kind(path, kind, source_name):
        with _regular(path) as stream:
            head = stream.read(512)
            stripped = head.lstrip(b"\xef\xbb\xbf \r\n\t").lower()
            if kind == "PDF" and not head.startswith(b"%PDF-"):
                raise OriginalError("not raw PDF")
            if kind == "HTML" and not stripped.startswith((b"<!doctype html", b"<html")):
                raise OriginalError("not raw HTML")
            if kind == "XML" and not stripped.startswith(b"<"):
                raise OriginalError("not raw XML")
            if kind == "rawTexarchive":
                if source_name.lower().endswith(".tex"):
                    raise OriginalError("assembled/derived TeX is not a raw archive")
                if head.startswith(b"PK\x03\x04"):
                    stream.seek(0)
                    if zipfile.is_zipfile(stream):
                        return
                    raise OriginalError("invalid raw zip archive")
                if head.startswith(b"\x1f\x8b"):
                    stream.seek(0)
                    with gzip.GzipFile(fileobj=stream) as archive:
                        head = archive.read(512)
                try:
                    tarfile.TarInfo.frombuf(head, "utf-8", "strict")
                except (tarfile.TarError, ValueError) as exc:
                    raise OriginalError("not a raw TeX tar/zip archive") from exc

    def _publish(self, private, target):
        self._layout()
        _plain(private)
        _plain(target)
        try:
            os.link(private, target, follow_symlinks=False)
        except FileExistsError:
            pass  # Always rehash a competing/existing publication below.
        _sync_dir(target.parent)

    def ingest_file(self, path, provenance, *, max_bytes):
        """Ingest raw bytes only. Return immutable reference ID, not queue availability."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise OriginalError("explicit positive max_bytes required")
        if not isinstance(provenance, Provenance):
            raise OriginalError("Provenance required")
        provenance.validate()
        path = _plain(path)
        if path.is_relative_to(self.root):
            raise OriginalError("source must be outside store")
        self._layout()
        private = _plain(self.root / "private" / (uuid.uuid4().hex + ".partial"))
        digest, total = hashlib.sha256(), 0
        with _regular(path) as source:
            before = os.fstat(source.fileno())
            if before.st_size > max_bytes or before.st_size <= 0:
                raise OriginalError("source empty or exceeds max_bytes")
            fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                while True:
                    data = source.read(min(65536, max_bytes - total + 1))
                    if not data:
                        break
                    total += len(data)
                    if total > max_bytes:
                        raise OriginalError("source exceeds max_bytes; private evidence retained")
                    digest.update(data)
                    out.write(data)
                out.flush()
                os.fsync(out.fileno())
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or total != before.st_size:
                raise OriginalError("source changed; private evidence retained")
        current = _plain(path).stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise OriginalError("source path changed; private evidence retained")
        if digest.hexdigest() != provenance.raw_sha256:
            raise OriginalError("declared raw hash mismatch; private evidence retained")
        self._check_kind(private, provenance.original_kind, path.name)
        target = self.blob_path(provenance.raw_sha256)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._publish(private, target)
            self.verify_blob(provenance.raw_sha256, total)
            db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?)", (provenance.raw_sha256, total))
            if db.execute("SELECT size FROM blobs WHERE sha256=?", (provenance.raw_sha256,)).fetchone()[0] != total:
                raise OriginalError("manifest size mismatch")
            values = tuple(asdict(provenance).values())
            db.execute("INSERT OR IGNORE INTO originals (canonical_id,source_url,retrieval_url,original_kind,"
                       "edition,retrieved_at,raw_sha256) VALUES (?,?,?,?,?,?,?)", values)
            row = db.execute("SELECT id FROM originals WHERE canonical_id=? AND source_url=? AND retrieval_url=? "
                             "AND original_kind=? AND edition=? AND retrieved_at=? AND raw_sha256=?", values).fetchone()
        # Only this attempt's verified private link is removed; failures leave evidence.
        private.unlink()
        _sync_dir(private.parent)
        return row[0]

    def _stream_bounds(self, provenance, max_bytes, expected_bytes, source_name, reserve_bytes):
        if not isinstance(provenance, Provenance):
            raise OriginalError("Provenance required")
        provenance.validate()
        if (type(max_bytes) is not int or type(expected_bytes) is not int
                or not 0 < expected_bytes <= max_bytes):
            raise OriginalError("explicit positive expected_bytes <= max_bytes required")
        _label(source_name)
        if (source_name in (".", "..") or "/" in source_name or "\\" in source_name
                or ":" in source_name):
            raise OriginalError("source_name must be a basename, not a path")
        if type(reserve_bytes) is not int or reserve_bytes < 10 * 1024**3:
            raise OriginalError("reserve_bytes must preserve at least 10 GiB")
        self._layout()

    def _stream_space(self, reserve_bytes, additional_bytes=0):
        if shutil.disk_usage(self.root).free < reserve_bytes + additional_bytes:
            raise OriginalError("fresh ORIGINAL disk reserve failed")

    def _commit_received(self, provenance, expected_bytes, source_name, reserve_bytes, private=None):
        target = self.blob_path(provenance.raw_sha256)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._stream_space(reserve_bytes)
            if private is not None:
                info = _plain(private).lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OriginalError("private inode is not exclusively controlled")
                self._publish(private, target)
            self.verify_blob(provenance.raw_sha256, expected_bytes)
            self._check_kind(target, provenance.original_kind, source_name)
            _sync_dir(target.parent)
            db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?)", (provenance.raw_sha256, expected_bytes))
            if db.execute("SELECT size FROM blobs WHERE sha256=?", (provenance.raw_sha256,)).fetchone()[0] != expected_bytes:
                raise OriginalError("manifest size mismatch")
            values = tuple(asdict(provenance).values())
            db.execute("INSERT OR IGNORE INTO originals (canonical_id,source_url,retrieval_url,original_kind,"
                       "edition,retrieved_at,raw_sha256) VALUES (?,?,?,?,?,?,?)", values)
            row = db.execute("SELECT id FROM originals WHERE canonical_id=? AND source_url=? AND retrieval_url=? "
                             "AND original_kind=? AND edition=? AND retrieved_at=? AND raw_sha256=?", values).fetchone()
            self._stream_space(reserve_bytes)
        return row[0]

    def ingest_stream(self, stream, provenance, *, max_bytes, expected_bytes, source_name,
                      reserve_bytes=10 * 1024**3):
        """Reader must raise on invalid source/trailer; only verified end is EOF.

        Consume through EOF even at expected_bytes. Commit one file, not a batch.
        Failed private files and any orphan CAS inode are retained; no retry here.
        Caller owns/closes the reader and enforces sealed-plan/count/total bounds.
        """
        self._stream_bounds(provenance, max_bytes, expected_bytes, source_name, reserve_bytes)
        if not callable(getattr(stream, "read", None)):
            raise OriginalError("bounded binary reader required")
        self._stream_space(reserve_bytes, expected_bytes)
        private = _plain(self.root / "private" / (uuid.uuid4().hex + ".partial"))
        fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        digest, total = hashlib.sha256(), 0
        try:
            _sync_dir(private.parent)
            with os.fdopen(fd, "wb") as out:
                fd = None
                try:
                    while True:
                        requested = min(65536, expected_bytes - total + 1)
                        data = stream.read(requested)
                        if not isinstance(data, bytes) or len(data) > requested:
                            raise OriginalError("reader violated bounded bytes contract")
                        if not data:
                            break
                        total += len(data)
                        if total > expected_bytes:
                            raise OriginalError("received bytes exceed declared size")
                        digest.update(data)
                        out.write(data)
                finally:
                    out.flush()
                    os.fsync(out.fileno())
            if total != expected_bytes or digest.hexdigest() != provenance.raw_sha256:
                raise OriginalError("received size/raw SHA mismatch")
            self._check_kind(private, provenance.original_kind, source_name)
            original_id = self._commit_received(provenance, expected_bytes, source_name, reserve_bytes, private)
            private.unlink()
            _sync_dir(private.parent)
            return original_id
        except BaseException as exc:
            if hasattr(exc, "add_note"):
                exc.add_note("ORIGINAL private evidence retained if present: " + str(private))
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def add_ref_from_verified_blob(self, provenance, *, max_bytes, expected_bytes, source_name,
                                   reserve_bytes=10 * 1024**3):
        """Explicit dedup/crash reconciliation: rehash CAS, never read a source body.

        Provenance still needs caller's sealed-plan identity binding. This is not
        an availability promotion, alias deletion, or automatic failed-item retry.
        """
        self._stream_bounds(provenance, max_bytes, expected_bytes, source_name, reserve_bytes)
        return self._commit_received(provenance, expected_bytes, source_name, reserve_bytes)

    def references(self, canonical_id):
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM originals WHERE canonical_id=? ORDER BY id", (_label(canonical_id),))]

    def import_queue(self, path, *, expected_jsonl_sha256, max_bytes, max_rows):
        """Rows require canonical_id and binding_sha256 (historical row/document binding).

        The separately supplied JSONL hash authenticates exact import bytes; neither
        hash is a raw original hash. Import is all-or-nothing, replay is idempotent.
        """
        _hash(expected_jsonl_sha256)
        if type(max_bytes) is not int or max_bytes <= 0 or type(max_rows) is not int or max_rows <= 0:
            raise OriginalError("explicit positive import bounds required")
        with _regular(path) as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes or hashlib.sha256(data).hexdigest() != expected_jsonl_sha256:
            raise OriginalError("queue byte bound/hash mismatch")
        rows = data.splitlines()
        if len(rows) > max_rows:
            raise OriginalError("queue row bound exceeded")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for line in rows:
                row = json.loads(line)
                canonical_id, binding = _label(row["canonical_id"]), _hash(row["binding_sha256"])
                reason, timestamp = _label(row["reason"]), _timestamp(row["observed_at"])
                encoded = json.dumps(row, sort_keys=True, ensure_ascii=True)
                previous = db.execute("SELECT imported_row FROM acquisition_queue WHERE canonical_id=? AND binding_sha256=?",
                                      (canonical_id, binding)).fetchone()
                if previous:
                    if previous[0] != encoded:
                        raise OriginalError("conflicting imported binding")
                    continue
                if row.get("status", "acquisition_pending") != "acquisition_pending":
                    raise OriginalError("import cannot promote status")
                db.execute("INSERT INTO acquisition_queue VALUES (?,?,?,'acquisition_pending',NULL)", (canonical_id, binding, encoded))
                self._transition(db, canonical_id, binding, "acquisition_pending", reason, timestamp, {"import_sha256": expected_jsonl_sha256})
        return len(rows)

    @staticmethod
    def _transition(db, canonical_id, binding, status, reason, observed_at, evidence, original_id=None):
        _label(reason)
        _timestamp(observed_at)
        if not isinstance(evidence, dict) or not evidence:
            raise OriginalError("explicit evidence required")
        encoded = json.dumps(evidence, sort_keys=True, allow_nan=False)
        if len(encoded) > 16384:
            raise OriginalError("evidence too large")
        cursor = db.execute("UPDATE acquisition_queue SET status=?, original_id=? WHERE canonical_id=? AND binding_sha256=?",
                            (status, original_id, canonical_id, binding))
        if cursor.rowcount != 1:
            raise OriginalError("unknown document binding")
        db.execute("INSERT INTO transitions (canonical_id,binding_sha256,status,reason,observed_at,evidence,original_id) "
                   "VALUES (?,?,?,?,?,?,?)", (canonical_id, binding, status, reason, observed_at, encoded, original_id))

    def transition(self, canonical_id, binding_sha256, status, *, reason, observed_at, evidence):
        _label(canonical_id)
        _hash(binding_sha256)
        if status not in STATUSES or status == "source_available":
            raise OriginalError("availability requires attach_original after identity check")
        if status == "permanently_unavailable":
            # Endpoint HTTP status, including 404/410, is not document-wide evidence.
            if (not isinstance(evidence, dict) or evidence.get("scope") != "document"
                    or evidence.get("kind") != "authoritative_document_absence"
                    or not evidence.get("authority_url") or not evidence.get("statement")):
                raise OriginalError("endpoint failure cannot establish permanent document absence")
            _url(evidence["authority_url"])
            _label(evidence["statement"])
        with self._db() as db:
            self._transition(db, canonical_id, binding_sha256, status, reason, observed_at, evidence)

    def attach_original(self, canonical_id, binding_sha256, original_id, *, identity_evidence, reason, observed_at):
        """Explicit caller identity attestation bound to both row hash and raw hash."""
        _label(canonical_id)
        _hash(binding_sha256)
        if type(original_id) is not int or not isinstance(identity_evidence, dict):
            raise OriginalError("reference ID and identity evidence required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT o.*, b.size FROM originals o JOIN blobs b ON b.sha256=o.raw_sha256 WHERE o.id=?", (original_id,)).fetchone()
            if (row is None or row["canonical_id"] != canonical_id
                    or identity_evidence.get("canonical_id") != canonical_id
                    or identity_evidence.get("binding_sha256") != binding_sha256
                    or identity_evidence.get("raw_sha256") != row["raw_sha256"]
                    or identity_evidence.get("identity_checked") is not True):
                raise OriginalError("identity evidence does not match stored document binding")
            _label(identity_evidence.get("method"))
            self.verify_blob(row["raw_sha256"], row["size"])
            self._transition(db, canonical_id, binding_sha256, "source_available", reason, observed_at, identity_evidence, original_id)

    def queue_item(self, canonical_id, binding_sha256):
        with self._db() as db:
            row = db.execute("SELECT * FROM acquisition_queue WHERE canonical_id=? AND binding_sha256=?", (canonical_id, binding_sha256)).fetchone()
            if row is None:
                raise OriginalError("unknown document binding")
            if row["status"] == "source_available":
                original = db.execute("SELECT o.raw_sha256,b.size FROM originals o JOIN blobs b ON b.sha256=o.raw_sha256 "
                                      "WHERE o.id=? AND o.canonical_id=?", (row["original_id"], canonical_id)).fetchone()
                if original is None:
                    raise OriginalError("missing original binding")
                self.verify_blob(original[0], original[1])
            return dict(row)
