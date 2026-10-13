"""Offline JSONL producer. Only explicit packets; no corpus/publication authority."""
import argparse
from collections import Counter, OrderedDict
from contextlib import contextmanager
import hashlib
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zlib
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import source_representations as catalog
from api.source_representations import search_catalog
from pipeline.citation_contexts import extract_citation_contexts, stream_citation_contexts

MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_MATCHED_TERMS_BYTES = 65536
PAYLOAD_TERMS_LIMIT = 12
SOURCE_FIELDS = {"source_id", "title", "year", "layers", "status", "publication_revision"}
DOCUMENT_FIELDS = {"source_id", "text", "bibliography_files", "origin_publication_revision"}
PASSAGE_FIELDS = {"source_id", "origin_id", "text", "artifact_sha256", "chunk_index",
                  "locator", "origin_publication_revision"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+~-]{0,255}\Z")
ARXIV = re.compile(r"(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|[a-z]+(?:-[a-z]+)?"
                   r"(?:\.[a-z]{2})?/\d{2}(?:0[1-9]|1[0-2])\d{3})(?:v[1-9]\d*)?\Z", re.I)


def canonical_id(value):
    doi = isinstance(value, str) and re.fullmatch(r'doi:10\.\d{4,9}/[^\s{}<>"\\]+', value)
    if not isinstance(value, str) or len(value) > 256 or (not doi and not ID.fullmatch(value)):
        raise ValueError("invalid source identifier")
    candidate = value[6:] if value.lower().startswith("arxiv:") else value
    if ARXIV.fullmatch(candidate):
        return re.sub(r"v[1-9]\d*$", "", candidate.lower())
    if value.lower().startswith("arxiv:"):
        raise ValueError("invalid arXiv identifier")
    return value


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def revision(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
        raise ValueError("publication revision must be SHA-256 or null")


def matched_terms(value):
    if (not isinstance(value, list) or len(value) > 4096 or len(encoded(value)) > MAX_MATCHED_TERMS_BYTES or
            any(not isinstance(term, str) or not term.strip() or len(term) > 256 or
                any(ord(char) < 32 for char in term) for term in value)):
        raise ValueError("invalid matched_terms annotation")
    return value


def terms_provenance(record):
    value = record.get("terms_provenance")
    if value is None:
        return
    if not isinstance(value, dict) or canonical_id(value.get("source_id")) != canonical_id(record["source_id"]):
        raise ValueError("terms annotation must belong to the same source")
    kind = value.get("kind")
    fields = {"kind", "source_id"}
    if kind in {"frozen_papers_matched_terms", "unavailable"}:
        fields |= {"metadata_snapshot_sha256"}
        revision(value.get("metadata_snapshot_sha256"))
        if value.get("metadata_snapshot_sha256") is None:
            raise ValueError("terms annotation requires frozen metadata digest")
        if kind == "frozen_papers_matched_terms":
            fields |= {"payload_limit", "publication_verified"}
            if (type(value.get("payload_limit")) is not int or value["payload_limit"] != PAYLOAD_TERMS_LIMIT
                    or value.get("publication_verified") is not False):
                raise ValueError("own metadata annotation is not a publication certificate")
        else:
            fields |= {"reason"}
            if value.get("reason") not in {"column_missing", "null", "invalid", "bounds"}:
                raise ValueError("invalid unavailable terms reason")
    elif record["type"] == "passage" and kind in {"publication_snapshot_payload", "chunk_cache_payload"}:
        fields |= {"artifact_sha256"}
        if value.get("artifact_sha256") != record["artifact_sha256"]:
            raise ValueError("terms annotation artifact mismatch")
        if kind == "publication_snapshot_payload":
            fields |= {"publication_revision"}
            if value.get("publication_revision") is None or value["publication_revision"] != record["origin_publication_revision"]:
                raise ValueError("terms annotation publication mismatch")
    else:
        raise ValueError("invalid terms annotation provenance")
    if set(value) != fields:
        raise ValueError("invalid terms annotation fields")


def packet(record):
    if not isinstance(record, dict) or record.get("type") not in ("source", "document", "passage"):
        raise ValueError("expected source/document/passage packet")
    fields = {"source": SOURCE_FIELDS, "document": DOCUMENT_FIELDS, "passage": PASSAGE_FIELDS}[record["type"]]
    optional = {"source": {"source_url", "abstract", "aliases", "matched_terms", "terms_provenance"}, "document": set(),
                "passage": {"section_type", "element_type", "terms", "terms_provenance"}}[record["type"]]
    if not fields <= set(record) or set(record) - fields - optional - {"type"}:
        raise ValueError("invalid packet fields")
    canonical_id(record["source_id"])
    revision(record["publication_revision"] if record["type"] == "source" else record["origin_publication_revision"])
    terms_provenance(record)
    if record["type"] == "source":
        if ("matched_terms" in record) != ("terms_provenance" in record):
            raise ValueError("matched_terms requires explicit provenance")
        if "matched_terms" in record:
            matched_terms(record["matched_terms"])
            if record["terms_provenance"]["kind"] == "unavailable" and record["matched_terms"]:
                raise ValueError("unavailable annotation cannot supply terms")
    if record["type"] == "passage":
        if canonical_id(record["origin_id"]) != canonical_id(record["source_id"]):
            raise ValueError("own passage origin must equal target")
        revision(record["artifact_sha256"])
        if record["artifact_sha256"] is None or type(record["chunk_index"]) is not int or record["chunk_index"] < 0:
            raise ValueError("passage requires artifact SHA and nonnegative chunk index")
        text, locator = record["text"], record["locator"]
        if (not isinstance(text, str) or not text.strip() or len(text) > 65536 or
                any(ord(c) < 32 and c not in "\r\n\t" for c in text)):
            raise ValueError("invalid passage text")
        if (not isinstance(locator, dict) or set(locator) != {"start", "end", "unit", "artifact"} or
                type(locator["start"]) is not int or type(locator["end"]) is not int or
                locator["start"] != 0 or locator["end"] != len(text) or locator["unit"] != "unicode_codepoint" or
                not isinstance(locator["artifact"], str) or not locator["artifact"].strip() or
                len(locator["artifact"]) > 256 or any(ord(c) < 32 for c in locator["artifact"])):
            raise ValueError("passage locator must address its exact decoded chunk text field")
        for field in ("section_type", "element_type"):
            value = record.get(field)
            if field in record and (not isinstance(value, str) or not value.strip() or len(value) > 256 or
                                    any(ord(c) < 32 for c in value)):
                raise ValueError("invalid passage tag")
        terms = record.get("terms", [])
        if (not isinstance(terms, list) or len(terms) > 32 or
                any(not isinstance(v, str) or not v.strip() or len(v) > 256 or
                    any(ord(c) < 32 for c in v) for v in terms)):
            raise ValueError("invalid passage terms")
    elif record["type"] == "document":
        files = record["bibliography_files"]
        if not isinstance(record["text"], str) or not isinstance(files, list):
            raise ValueError("invalid document text/bibliography")
        seen = {}
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "text"} or not all(isinstance(v, str) for v in item.values()):
                raise ValueError("invalid bibliography file")
            if item["path"] in seen and seen[item["path"]] != item["text"]:
                raise ValueError("conflicting bibliography path")
            seen[item["path"]] = item["text"]
    else:
        if not isinstance(record.get("abstract", ""), str) or not isinstance(record.get("aliases", []), list):
            raise ValueError("invalid abstract/aliases")
        for alias in record.get("aliases", []):
            canonical_id(alias)
        for key in ("title", "status"):
            if not isinstance(record[key], str) or not record[key].strip():
                raise ValueError("invalid " + key)
        if (record["year"] is not None and (type(record["year"]) is not int or not 1 <= record["year"] <= 9999)
                or not isinstance(record["layers"], list) or not all(isinstance(v, str) and v.strip() for v in record["layers"])):
            raise ValueError("invalid year/layers")
        url = record.get("source_url")
        if url is not None:
            if not isinstance(url, str):
                raise ValueError("invalid source_url")
            parsed = urlsplit(url)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or any(c.isspace() for c in url)):
                raise ValueError("invalid source_url")
            parsed.port
    encoded(record)
    return record


class _FrozenInput:
    def __init__(self, stream, size):
        self.stream, self.size = stream, size

    def read(self, size=-1):
        remaining = self.size - self.stream.tell()
        requested = remaining if size < 0 else min(size, remaining)
        raw = self.stream.read(requested)
        if len(raw) != requested:
            raise ValueError("input truncated while reading")
        return raw


def records(path, digest, *, offset=0, position=None):
    if not Path(path).is_file():
        raise ValueError("input must be a finite regular JSONL file")
    with Path(path).open("rb") as stream:
        initial = os.fstat(stream.fileno())
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError("input must be a regular file")
        compressed = Path(path).name.endswith(".jsonl.gz")
        reader = gzip.GzipFile(fileobj=_FrozenInput(stream, initial.st_size), mode="rb") if compressed else stream
        try:
            remaining = offset
            while remaining:
                raw = reader.read(min(1024 * 1024, remaining))
                if not raw:
                    raise ValueError("input truncated before checkpoint")
                digest.update(raw)
                remaining -= len(raw)
            line = 0
            while compressed or stream.tell() < initial.st_size:
                maximum = MAX_LINE_BYTES + 1 if compressed else min(MAX_LINE_BYTES + 1, initial.st_size - stream.tell())
                raw = reader.readline(maximum)
                if not raw:
                    if compressed:
                        break
                    raise ValueError("input truncated while reading")
                line += 1
                if len(raw) > MAX_LINE_BYTES:
                    raise ValueError(f"line {line}: exceeds 8 MiB decoded")
                digest.update(raw)
                if position is not None:
                    position[0] += len(raw)
                try:
                    yield packet(json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object))
                except (ValueError, TypeError, RecursionError) as exc:
                    raise ValueError(f"line {line}: {exc}") from exc
        except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
            raise ValueError("invalid or truncated gzip input") from exc
        finally:
            if compressed:
                reader.close()
        final = os.fstat(stream.fileno())
        current = Path(path).stat()
        if ((initial.st_size, initial.st_mtime_ns, initial.st_ino) !=
                (final.st_size, final.st_mtime_ns, final.st_ino) or
                (current.st_size, current.st_mtime_ns, current.st_ino) !=
                (initial.st_size, initial.st_mtime_ns, initial.st_ino)):
            raise ValueError("input changed while reading its frozen finite file")
        if stream.read(1):
            raise ValueError("input grew while reading")


class _Pause(Exception):
    pass


@contextmanager
def _locked_work(root, resume):
    if root.is_symlink():
        raise ValueError("work directory cannot be a symlink")
    if not resume:
        root.mkdir()
    elif not root.is_dir():
        raise ValueError("resume requires an existing owned work directory")
    lock_path = root / "build.lock"
    if lock_path.is_symlink():
        raise ValueError("invalid work lock")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _file_hash(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("identity input must be a finite regular file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("identity input must be a finite regular file")
        remaining = before.st_size
        while remaining:
            raw = handle.read(min(1024 * 1024, remaining))
            if not raw:
                raise ValueError("input changed during hash guard")
            digest.update(raw)
            remaining -= len(raw)
        after = os.fstat(handle.fileno())
        current = path.stat()
        identity = lambda s: (s.st_size, s.st_mtime_ns, s.st_ino, s.st_dev)
        if handle.read(1) or identity(before) != identity(after) or identity(before) != identity(current):
            raise ValueError("input changed during hash guard")
    return digest.hexdigest()


class _DiskMap:
    def __init__(self, conn, table, *, cache_limit=0):
        self.conn, self.table, self.cache_limit = conn, table, cache_limit
        self.cache = OrderedDict()

    def __getitem__(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        row = self.conn.execute(f"SELECT value FROM work.{self.table} WHERE key=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        value = json.loads(row[0]) if self.table == "metadata" else row[0]
        if self.cache_limit and len(row[0]) <= 65536:
            self.cache[key] = value
            while len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)
        return value

    def __contains__(self, key):
        return self.conn.execute(f"SELECT 1 FROM work.{self.table} WHERE key=?", (key,)).fetchone() is not None

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class _Work:
    def __init__(self, root, identity, resume, deadline, disk_budget, reserve):
        self.root, self.identity, self.deadline = root, identity, deadline
        self.disk_budget, self.reserve = disk_budget, reserve
        self.conn = None
        self.batch_rows = self.batch_bytes = 0
        self.staged = root / "catalog.sqlite"
        work_db = root / "work.sqlite"
        if resume and any(p.is_symlink() or not p.is_file() or p.stat().st_nlink != 1
                          for p in (work_db,)):
            raise ValueError("invalid owned work database")
        if resume and (self.staged.is_symlink() or not self.staged.is_file()):
            raise ValueError("invalid staged catalog")
        if root.stat().st_dev != Path(identity["output"]).parent.stat().st_dev:
            raise ValueError("work directory must be on publication filesystem")
        self.guard_disk()
        conn = sqlite3.connect(self.staged)
        self.conn = conn
        try:
            catalog._configure_builder(conn)
            conn.execute("ATTACH DATABASE ? AS work", (str(work_db),))
            conn.execute("PRAGMA work.journal_mode=DELETE")
            conn.execute("PRAGMA work.synchronous=FULL")
            conn.execute("PRAGMA work.cache_size=-8192")
            conn.execute("PRAGMA work.mmap_size=0")
            if resume:
                self.state = json.loads(conn.execute("SELECT value FROM work.state WHERE singleton=1").fetchone()[0])
                if self.state["identity"] != identity:
                    raise ValueError("resume identity mismatch: input/report/code/options/output changed")
                if self.staged.stat().st_nlink != 1 and not (
                        self.state["phase"] == "ready" and os.path.exists(identity["output"])
                        and os.path.samefile(self.staged, identity["output"])):
                    raise ValueError("staged catalog has an unexpected hard link")
            else:
                conn.execute(f"PRAGMA application_id={catalog._APP_ID}")
                conn.execute(f"PRAGMA user_version={catalog._VERSION}")
                conn.executescript(catalog._SCHEMA)
                conn.executescript("""
                    CREATE TABLE work.state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),value TEXT NOT NULL);
                    CREATE TABLE work.metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL,abstract TEXT NOT NULL);
                    CREATE TABLE work.aliases(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE work.packets(id INTEGER PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE work.documents(identity TEXT PRIMARY KEY,signature TEXT NOT NULL);
                    CREATE TABLE work.passages(identity TEXT PRIMARY KEY,signature TEXT NOT NULL);
                """)
                self.state = dict(identity=identity, phase="ingest", offset=0, cursor=0)
                self.save()
                conn.commit()
        except BaseException:
            conn.close()
            self.conn = None
            raise

    def guard_disk(self):
        used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        if self.disk_budget is not None and used > self.disk_budget:
            raise ValueError("disk budget exceeded; no catalog published")
        if shutil.disk_usage(self.root).free < self.reserve:
            raise ValueError("disk reserve exhausted; no catalog published")

    def check(self):
        self.guard_disk()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise _Pause()

    def save(self):
        self.conn.execute("INSERT OR REPLACE INTO work.state VALUES (1,?)", (encoded(self.state).decode(),))

    def checkpoint(self):
        self.save()
        self.guard_disk()
        self.conn.commit()
        self.batch_rows = self.batch_bytes = 0
        self.check()

    def tick(self, size=0):
        self.batch_rows += 1
        self.batch_bytes += size
        if (self.batch_rows >= 128 or self.batch_bytes >= 4 * 1024 * 1024 or
                self.deadline is not None and time.monotonic() >= self.deadline):
            self.checkpoint()


RECIPE_VERSION = 2


def produce(input_path, output_path, max_documents=None, input_report=None, *, work_dir=None,
            resume=False, max_seconds=None, disk_budget_bytes=None, reserve_bytes=64 * 1024 * 1024):
    """Cooperative deadlines: finite hash guards and one bounded packet may overrun.

    DELETE/FULL journals atomically commit the catalog and attached work progress.
    Context/lead checks never commit inside a packet. Pause/crash rolls back the
    uncommitted document and batch: a whole document may replay from the saved cursor.
    Work storage retains decoded packets; recipe-v1 checkpoints cannot be resumed.
    """
    if resume and work_dir is None:
        raise ValueError("resume requires --work-dir")
    if max_seconds is not None and (type(max_seconds) not in (int, float) or
                                    not 0 < max_seconds < float("inf")):
        raise ValueError("max_seconds must be positive and finite")
    for name, value in (("disk_budget_bytes", disk_budget_bytes), ("reserve_bytes", reserve_bytes)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(name + " must be a nonnegative integer")
    if max_documents is not None and (type(max_documents) is not int or max_documents < 1):
        raise ValueError("max_documents must be positive")
    if max_seconds is not None and work_dir is None:
        raise ValueError("deadline pause requires --work-dir")
    if reserve_bytes is None:
        raise ValueError("reserve_bytes must be a nonnegative integer")
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None
    target = Path(output_path).absolute()
    target = target.parent.resolve(strict=True) / target.name
    if os.path.lexists(target) and not resume:
        raise FileExistsError("output already exists; choose a new path")
    identity = dict(version=RECIPE_VERSION, input=str(Path(input_path).resolve(strict=True)),
                    input_file_sha256=_file_hash(input_path),
                    report=str(Path(input_report).resolve(strict=True)) if input_report is not None else None,
                    report_sha256=_file_hash(input_report) if input_report is not None else None,
                    output=str(target), max_documents=max_documents,
                    disk_budget_bytes=disk_budget_bytes, reserve_bytes=reserve_bytes,
                    runtime=[sys.version, sqlite3.sqlite_version],
                    code=[_file_hash(p) for p in (Path(__file__), Path(catalog.__file__),
                          Path(sys.modules[stream_citation_contexts.__module__].__file__))])
    temporary = tempfile.TemporaryDirectory(prefix=".source-passages-", dir=target.parent) if work_dir is None else None
    root = Path(temporary.name) / "owned" if temporary is not None else Path(work_dir).absolute()
    work = None
    try:
        with _locked_work(root, resume):
            work = _Work(root, identity, resume, deadline, disk_budget_bytes, reserve_bytes)
            work.keep = work_dir is not None
            if os.path.lexists(target) and (work.state["phase"] != "ready" or not os.path.samefile(work.staged, target)):
                raise FileExistsError("output already exists; choose a new path")
            try:
                work.check()
                return _produce(input_path, target, max_documents, input_report, work)
            except (_Pause, sqlite3.OperationalError) as exc:
                if isinstance(exc, sqlite3.OperationalError) and not (
                        str(exc) == "interrupted" and deadline is not None and time.monotonic() >= deadline):
                    raise
                work.conn.set_progress_handler(None, 0)
                work.conn.rollback()
                saved = json.loads(work.conn.execute("SELECT value FROM work.state WHERE singleton=1").fetchone()[0])
                return dict(status="paused", phase=saved["phase"], work_dir=str(root), output=str(target),
                            counts=saved.get("counts", {}), diagnostics=saved.get("diagnostics", {}),
                            partial=True, complete_generation=False)
            finally:
                if work.conn is not None:
                    work.conn.close()
                    work.conn = None
    finally:
        if work is not None and work.conn is not None:
            work.conn.close()
        if temporary is not None:
            temporary.cleanup()


def _produce(input_path, output_path, max_documents, input_report, work):
    if max_documents is not None and (type(max_documents) is not int or max_documents < 1):
        raise ValueError("max_documents must be positive")
    if input_report is None and Path(input_path).name in ("packets.jsonl", "packets.jsonl.gz") and Path(input_path).with_name("report.json").exists():
        raise ValueError("export bundle requires explicit --input-report so shard coverage is not lost")
    export_report = None
    export_report_sha256 = None
    if input_report is not None:
        with Path(input_report).open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LINE_BYTES:
                raise ValueError("input report must be a bounded regular JSON file")
            raw_report = stream.read(info.st_size + 1)
            final = os.fstat(stream.fileno())
            if (len(raw_report) != info.st_size or
                    (info.st_size, info.st_mtime_ns, info.st_ino) !=
                    (final.st_size, final.st_mtime_ns, final.st_ino)):
                raise ValueError("export report changed while reading")
            export_report = json.loads(raw_report, object_pairs_hook=unique_object)
            export_report_sha256 = hashlib.sha256(raw_report).hexdigest()
        if (not isinstance(export_report, dict) or export_report.get("schema") != 1 or
                export_report.get("mode") != "export" or type(export_report.get("partial")) is not bool or
                not isinstance(export_report.get("packets_sha256"), str)):
            raise ValueError("invalid export report")
        revision(export_report["packets_sha256"])
    counts = Counter(records=0, source_packets=0, document_packets=0, duplicate_sources=0,
                     duplicate_documents=0, sources=0, documents_selected=0, documents_omitted=0,
                     documents_extracted=0, skipped_source_status=0, skipped_document_status=0,
                     skipped_target_status=0, publication_mismatch=0, missing_metadata=0,
                     empty_abstracts=0, own_summary=0, citation_context=0, duplicate_context_targets=0,
                     passage_packets=0, own_passage=0, duplicate_passages=0, skipped_passage_status=0,
                     skipped_owner_metadata=0)
    counts.update(work.state.get("counts", {}))
    diagnostics = work.state.get("diagnostics", {
        "extractor": {}, "bindings": {"samples": [], "total_count": 0, "truncated": False}})
    diagnostics["extractor"] = Counter(diagnostics["extractor"])
    work.state.update(counts=counts, diagnostics=diagnostics)
    metadata = _DiskMap(work.conn, "metadata", cache_limit=16)
    aliases = _DiskMap(work.conn, "aliases")
    if work.state["phase"] == "ingest":
        digest = hashlib.sha256()
        position = [work.state["offset"]]
        for record in records(input_path, digest, offset=position[0], position=position):
            counts["records"] += 1
            counts[record["type"] + "_packets"] += 1
            if record["type"] != "source":
                work.conn.execute("INSERT INTO work.packets VALUES (?,?)", (counts["records"], encoded(record).decode()))
            else:
                source = {**record, "source_id": canonical_id(record["source_id"]),
                          "aliases": sorted(set(record.get("aliases", []))), "abstract": record.get("abstract", ""),
                          "source_url": record.get("source_url"), "layers": sorted(set(record["layers"]))}
                abstract = source.pop("abstract")
                source["abstract_sha256"] = hashlib.sha256(abstract.encode("utf-8")).hexdigest()
                key = source["source_id"]
                if key in metadata:
                    if source != metadata[key]:
                        raise ValueError("conflicting canonical source metadata: " + key)
                    counts["duplicate_sources"] += 1
                else:
                    work.conn.execute("INSERT INTO work.metadata VALUES (?,?,?)", (key, encoded(source).decode(), abstract))
                for alias in [key, *source["aliases"]]:
                    alias = canonical_id(alias)
                    if alias in aliases and aliases[alias] != key:
                        raise ValueError("conflicting explicit alias: " + alias)
                    work.conn.execute("INSERT OR IGNORE INTO work.aliases VALUES (?,?)", (alias, key))
            size = position[0] - work.state["offset"]
            work.state["offset"] = position[0]
            work.tick(size)
        input_sha256 = digest.hexdigest()
        if export_report is not None and export_report["packets_sha256"] != input_sha256:
            raise ValueError("export report packet hash mismatch")
        work.state.update(input_sha256=input_sha256, phase="sources", cursor="")
        work.checkpoint()
    input_sha256 = work.state["input_sha256"]

    def representation(kind, target, owner, sha, text, start, end, artifact, binding, provenance):
        row = dict(kind=kind, source_id=target, origin_id=owner, origin_revision=sha,
                   locator=dict(start=start, end=end, unit="unicode_codepoint", artifact=artifact),
                   text=text, binding=binding, binding_status="resolved")
        row["representation_id"] = fingerprint([row, metadata[owner]["publication_revision"],
                                                metadata[target]["publication_revision"], provenance])
        counts[kind] += 1
        return row

    def summary(source, abstract):
        owner = source["source_id"]
        if source["status"] == "done":
            if abstract.strip():
                row = representation("own_summary", owner, owner, hashlib.sha256(abstract.encode("utf-8")).hexdigest(),
                                     abstract, 0, len(abstract), "metadata_abstract", None, source.get("terms_provenance"))
                if "matched_terms" in source:
                    row["terms"] = source["matched_terms"][:PAYLOAD_TERMS_LIMIT]
                    row["representation_id"] = fingerprint([row, source["terms_provenance"]])
                yield row
            else:
                counts["empty_abstracts"] += 1
    def representations(passage_index, packets):
        for document in packets:
            if document["type"] == "passage":
                owner = aliases.get(canonical_id(document["source_id"]), canonical_id(document["source_id"]))
                identity = encoded([owner, document["artifact_sha256"], document["chunk_index"]]).decode("utf-8")
                normalized = dict(document, source_id=owner, origin_id=owner)
                signature = fingerprint(normalized)
                prior = passage_index.execute("SELECT signature FROM work.passages WHERE identity=?", (identity,)).fetchone()
                if prior:
                    if prior[0] != signature:
                        raise ValueError("conflicting passage packet: " + owner)
                    counts["duplicate_passages"] += 1
                    continue
                passage_index.execute("INSERT INTO work.passages VALUES (?,?)", (identity, signature))
                if owner not in metadata:
                    counts["missing_metadata"] += 1
                    counts["skipped_owner_metadata"] += 1
                    continue
                if metadata[owner]["status"] != "done":
                    counts["skipped_passage_status"] += 1
                    continue
                if document["origin_publication_revision"] != metadata[owner]["publication_revision"]:
                    counts["publication_mismatch"] += 1
                    continue
                annotation = document.get("terms_provenance")
                if annotation is not None and annotation["kind"] == "frozen_papers_matched_terms":
                    if (annotation != metadata[owner].get("terms_provenance") or
                            document.get("terms") != metadata[owner].get("matched_terms", [])[:PAYLOAD_TERMS_LIMIT]):
                        raise ValueError("passage own annotation does not match frozen source metadata")
                row = dict(kind="own_passage", source_id=owner, origin_id=owner,
                           origin_revision=document["artifact_sha256"], locator=document["locator"],
                           text=document["text"], binding=None, binding_status="resolved")
                row.update({key: document[key] for key in ("section_type", "element_type", "terms") if key in document})
                row["representation_id"] = fingerprint([row, metadata[owner]["publication_revision"]])
                if annotation is not None:
                    row["representation_id"] = fingerprint([row, annotation])
                    counts["own_passage_terms_" + annotation["kind"]] += 1
                counts["own_passage"] += 1
                yield row
                continue
            if document["type"] != "document":
                continue
            owner = aliases.get(canonical_id(document["source_id"]), canonical_id(document["source_id"]))
            document = {**document, "source_id": owner,
                        "bibliography_files": sorted(document["bibliography_files"], key=lambda f: (f["path"], f["text"]))}
            signature = fingerprint(document)
            prior = passage_index.execute("SELECT signature FROM work.documents WHERE identity=?", (owner,)).fetchone()
            if prior is not None:
                if prior[0] != signature:
                    raise ValueError("conflicting document packet: " + owner)
                counts["duplicate_documents"] += 1
                continue
            passage_index.execute("INSERT INTO work.documents VALUES (?,?)", (owner, signature))
            if max_documents is not None and counts["documents_selected"] >= max_documents:
                counts["documents_omitted"] += 1
                continue
            counts["documents_selected"] += 1
            if owner not in metadata:
                counts["missing_metadata"] += 1
                counts["skipped_owner_metadata"] += 1
                continue
            if metadata[owner]["status"] != "done":
                counts["skipped_document_status"] += 1
                continue
            if document["origin_publication_revision"] != metadata[owner]["publication_revision"]:
                counts["publication_mismatch"] += 1
                continue
            result = stream_citation_contexts(document["text"], [(f["path"], f["text"]) for f in document["bibliography_files"]])
            counts["documents_extracted"] += 1
            emitted = set()
            ordinal = 0
            for context in result:
                work.check()
                if document["text"][context["start"]:context["end"]] != context["raw"]:
                    raise ValueError("extractor span does not address raw source")
                if not isinstance(context.get("search_text"), str):
                    raise ValueError("extractor public context.search_text unavailable")
                for lead in context["resolved"]:
                    work.check()
                    catalog._insert_provenance_occurrence(work.conn, owner, result.source_sha256,
                        result.bibliography_sha256, context, lead, ordinal)
                    ordinal += 1
                    requested = canonical_id(lead["target_id"])
                    target = aliases.get(requested, requested)
                    if target not in metadata:
                        counts["missing_metadata"] += 1
                        continue
                    if metadata[target]["status"] != "done":
                        counts["skipped_target_status"] += 1
                        continue
                    context_target = (owner, result.source_sha256, context["start"], context["end"], target)
                    if context_target in emitted:
                        counts["duplicate_context_targets"] += 1
                        continue
                    emitted.add(context_target)
                    binding = {"exact_key": lead["key"], "identifier": target}
                    if target == owner:
                        binding["self_citation"] = True
                    row = representation("citation_context", target, owner, result.source_sha256, context["search_text"],
                                         context["start"], context["end"], "latex_source", binding,
                                         [result.bibliography_sha256, lead])
                    bindings = diagnostics["bindings"]
                    bindings["total_count"] += 1
                    bindings["truncated"] = bindings["total_count"] > 32
                    if len(bindings["samples"]) < 32:
                        first = {k: v for k, v in lead["provenance"][0].items() if k != "entry_raw"}
                        clipped = any(isinstance(v, str) and len(v) > 1024 for v in first.values())
                        provenance = {k: v[:1024] if isinstance(v, str) else v for k, v in first.items()}
                        bindings["samples"].append(dict(representation_id=row["representation_id"], origin_id=owner,
                            target_id=target, source_sha256=result.source_sha256, bibliography_sha256=result.bibliography_sha256,
                            provenance=[provenance], provenance_total_count=len(lead["provenance"]),
                            provenance_truncated=clipped or len(lead["provenance"]) > 1))
                    yield row
            work.check()
            diagnostics["extractor"].update(item["code"] for item in result.diagnostics)

    def coverage_record():
        complete = bool(export_report is not None and export_report.get("full_export") is True
                        and export_report.get("complete_generation") is True and export_report["partial"] is False
                        and max_documents is None and not any(counts[key] for key in (
                            "documents_omitted", "skipped_source_status", "skipped_document_status",
                            "skipped_passage_status", "skipped_owner_metadata", "publication_mismatch")))
        return dict(version=1, scope="full_snapshot" if complete else "explicit_input_only", partial=not complete,
                    input_sha256=input_sha256, export_report_sha256=export_report_sha256)

    if work.state["phase"] == "sources":
        for key, value, abstract in work.conn.execute(
                "SELECT key,value,abstract FROM work.metadata WHERE key>? ORDER BY key", (work.state["cursor"],)):
            source = json.loads(value)
            if source["status"] == "done":
                catalog._insert_source(work.conn, {field: source[field] for field in SOURCE_FIELDS | {"source_url"}})
                counts["sources"] += 1
            else:
                counts["skipped_source_status"] += 1
            work.state["cursor"] = key
            work.tick(len(value) + len(abstract))
        work.state.update(phase="summaries", cursor="")
        work.checkpoint()
    if work.state["phase"] == "summaries":
        for key, value, abstract in work.conn.execute(
                "SELECT key,value,abstract FROM work.metadata WHERE key>? ORDER BY key", (work.state["cursor"],)):
            for row in summary(json.loads(value), abstract):
                catalog._insert_representation(work.conn, row)
            work.state["cursor"] = key
            work.tick(len(value) + len(abstract))
        work.state.update(phase="representations", cursor=0)
        work.checkpoint()
    if work.state["phase"] == "representations":
        for index, value in work.conn.execute(
                "SELECT id,value FROM work.packets WHERE id>? ORDER BY id", (work.state["cursor"],)):
            for row in representations(work.conn, [json.loads(value)]):
                catalog._insert_representation(work.conn, row)
            work.state["cursor"] = index
            work.tick(len(value))
        work.state.update(phase="finalize", cursor=0)
        work.checkpoint()
    if work.state["phase"] == "finalize":
        work.check()
        work.conn.set_progress_handler(
            lambda: int(work.deadline is not None and time.monotonic() >= work.deadline), 10000)
        try:
            built = catalog._finish_catalog(work.conn,
                generation_id=fingerprint(dict(input_sha256=input_sha256, max_documents=max_documents, version=RECIPE_VERSION)),
                coverage=coverage_record, check=work.check)
        finally:
            work.conn.set_progress_handler(None, 0)
        work.state.update(phase="ready", built=built)
        work.checkpoint()
    built = work.state["built"]
    if _file_hash(input_path) != work.identity["input_file_sha256"] or (
            input_report is not None and _file_hash(input_report) != work.identity["report_sha256"]):
        raise ValueError("input changed before publication; generation not published")
    work.check()
    work.conn.close()
    work.conn = None
    if not os.path.lexists(output_path):
        catalog._publish_catalog(work.staged, Path(output_path), keep=work.keep)
    return dict(status=built["status"], generation=built["generation"], input_sha256=input_sha256,
                output=str(Path(output_path).absolute()), counts=dict(counts), diagnostics=diagnostics,
                partial=built["coverage"]["partial"], complete_generation=not built["coverage"]["partial"],
                catalog_coverage=built["coverage"], max_documents=max_documents,
                input_coverage=export_report.get("coverage") if export_report else "explicit_input_only",
                coverage=built["coverage"]["scope"], evidence_state="not_read")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="finite JSONL or .jsonl.gz; decoded lines bounded to 8 MiB")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-documents", type=int, help="explicit first-N unique document subset; reported as partial")
    parser.add_argument("--input-report", type=Path, help="export report.json; checks packet SHA and preserves partial coverage")
    parser.add_argument("--work-dir", type=Path, help="new owned durable directory on output filesystem")
    parser.add_argument("--resume", action="store_true", help="resume only identical input/report/code/options/output")
    parser.add_argument("--max-seconds", type=float, help="cooperative checkpoint pause; hash guards/one packet may overrun; requires --work-dir")
    parser.add_argument("--disk-budget-bytes", type=int, help="maximum owned staging bytes, checked at batches")
    parser.add_argument("--reserve-bytes", type=int, default=64 * 1024 * 1024, help="minimum filesystem free bytes")
    parser.add_argument("--query")
    parser.add_argument("--query-only", type=Path, metavar="CATALOG")
    args = parser.parse_args(argv)
    try:
        if args.query_only is not None:
            if (args.query is None or args.input is not None or args.output is not None or args.max_documents is not None
                    or args.input_report is not None or args.work_dir is not None or args.resume
                    or args.max_seconds is not None or args.disk_budget_bytes is not None
                    or args.reserve_bytes != 64 * 1024 * 1024):
                raise ValueError("query-only requires --query and forbids build options")
            report = search_catalog(args.query_only, args.query)
        else:
            if args.input is None or args.output is None:
                raise ValueError("build requires --input and --output")
            report = produce(args.input, args.output, args.max_documents, args.input_report,
                             work_dir=args.work_dir, resume=args.resume, max_seconds=args.max_seconds,
                             disk_budget_bytes=args.disk_budget_bytes, reserve_bytes=args.reserve_bytes)
            if args.query is not None and report["status"] == "ready":
                report["search"] = search_catalog(args.output, args.query)
        print(encoded(report).decode("utf-8"))
        return 0 if report["status"] == "ready" else 3 if report["status"] == "paused" else 1
    except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error) as exc:
        print(encoded(dict(status="error", diagnostics={"error": str(exc)})).decode("utf-8"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
