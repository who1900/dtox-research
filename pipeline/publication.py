"""Durable, first-publication journal for pipeline paper snapshots."""
import hashlib
import json
import math
import os
import errno
import sqlite3
import stat
import tempfile
import uuid
import re
from contextlib import contextmanager
from pathlib import Path


class PublicationBlocked(Exception):
    retryable = False

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class PublicationRetry(Exception):
    retryable = True

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _bounded_read(path, root, maximum):
    root = Path(root).resolve()
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise PublicationBlocked("unsafe_path", "file is outside its owned directory") from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise PublicationBlocked("unsafe_path", "symlinked parent directory")
    if absolute.is_symlink():
        raise PublicationBlocked("unsafe_path", "symlinked file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(absolute, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise PublicationBlocked("invalid_file", "file is not regular or exceeds size limit")
            data = bytearray()
            while len(data) <= maximum:
                part = os.read(fd, min(1024 * 1024, maximum + 1 - len(data)))
                if not part:
                    break
                data.extend(part)
            if len(data) > maximum:
                raise PublicationBlocked("file_too_large", "file exceeds size limit")
            return bytes(data)
        finally:
            os.close(fd)
    except PublicationBlocked:
        raise
    except OSError as exc:
        raise PublicationBlocked("file_unavailable", "owned regular file could not be read") from exc


def ensure_publication_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS paper_publications ("
                 "arxiv_id TEXT NOT NULL,revision TEXT NOT NULL,kind TEXT NOT NULL,"
                 "artifact_path TEXT NOT NULL,cache_sha256 TEXT,state TEXT NOT NULL,"
                 "completed TEXT NOT NULL DEFAULT '[]',retry_after REAL NOT NULL DEFAULT 0,"
                 "error_code TEXT,PRIMARY KEY(arxiv_id,revision))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_kind_state_retry "
                 "ON paper_publications(kind,state,retry_after)")


def _safe_dir(data_dir):
    root = Path(data_dir).resolve()
    target = root / "publication_cache"
    if target.is_symlink():
        raise PublicationBlocked("unsafe_path", "publication_cache is a symlink")
    target.mkdir(parents=True, exist_ok=True)
    if target.resolve().parent != root:
        raise PublicationBlocked("unsafe_path", "publication cache escaped data directory")
    return target


def _atomic(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
            temporary = Path(out.name)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
                    raise
            finally:
                os.close(fd)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _store(conn, data_dir, paper_id, revision, kind, value, cache_sha=None):
    if conn.in_transaction:
        raise PublicationBlocked("transaction_active", "publication staging requires idle connection")
    ensure_publication_schema(conn)
    path = _safe_dir(data_dir) / f"{hashlib.sha256(paper_id.encode()).hexdigest()}-{revision}.json"
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if len(encoded) > (64 * 1024 * 1024 if kind == "snapshot" else 8 * 1024 * 1024):
        raise PublicationBlocked("artifact_too_large", "publication artifact exceeds limit")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if kind == "snapshot" and conn.execute(
                "SELECT 1 FROM paper_publications WHERE arxiv_id=? AND kind='snapshot' "
                "AND revision<>? LIMIT 1", (paper_id, revision)).fetchone():
            raise PublicationBlocked("snapshot_conflict", "paper already owns another snapshot revision")
        prior = conn.execute("SELECT artifact_path,state FROM paper_publications WHERE arxiv_id=? AND revision=?",
                             (paper_id, revision)).fetchone()
        if prior:
            if prior[1] == "published" and kind == "snapshot":
                raise PublicationBlocked("already_published", "published point IDs cannot be replaced")
            if _bounded_read(prior[0], Path(data_dir) / "publication_cache", 64 * 1024 * 1024) != encoded:
                raise PublicationBlocked("revision_conflict", "revision already has different artifact")
            path = Path(prior[0])
        else:
            if path.exists():
                if _bounded_read(path, Path(data_dir) / "publication_cache", 64 * 1024 * 1024) != encoded:
                    raise PublicationBlocked("artifact_conflict", "immutable artifact path contains other data")
            else:
                _atomic(path, encoded)
            conn.execute("INSERT INTO paper_publications(arxiv_id,revision,kind,artifact_path,cache_sha256,state) "
                         "VALUES (?,?,?,?,?,'staged')", (paper_id, revision, kind, str(path), cache_sha))
        conn.commit()
        return str(path)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def stage_source(conn, data_dir, paper_id, source_bytes, metadata):
    if conn.in_transaction:
        raise PublicationBlocked("transaction_active", "source staging requires idle connection")
    row = conn.execute("SELECT status FROM papers WHERE arxiv_id=?", (paper_id,)).fetchone()
    if row is None or row[0] != "done":
        raise PublicationBlocked("paper_not_done", "source-only publication requires a done paper")
    if not isinstance(source_bytes, bytes) or not source_bytes or len(source_bytes) > 8 * 1024 * 1024:
        raise PublicationBlocked("invalid_source", "source must be nonempty bytes up to 8 MiB")
    try:
        text = source_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise PublicationBlocked("invalid_source", "source is not UTF-8") from exc
    if not text.strip() or "\x00" in text or not isinstance(metadata, dict):
        raise PublicationBlocked("invalid_source", "source or metadata is invalid")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    try:
        metadata_bytes = json.dumps(metadata, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise PublicationBlocked("invalid_metadata", "metadata must be finite JSON") from exc
    revision = hashlib.sha256(source_bytes + b"\0" + metadata_bytes).hexdigest()
    artifact = {"source": text, "metadata": metadata}
    path = _store(conn, data_dir, paper_id, revision, "source", artifact, source_hash)
    return {"status": "staged", "paper_id": paper_id, "revision": revision,
            "job_kind": "source", "artifact_path": path}


def prepare_snapshot(conn, data_dir, paper_id, points, cache_path):
    if conn.in_transaction:
        raise PublicationBlocked("transaction_active", "snapshot staging requires idle connection")
    row = conn.execute("SELECT status,parse_status,title,year,layers,source_url FROM papers WHERE arxiv_id=?",
                       (paper_id,)).fetchone()
    history = conn.execute("SELECT first_known,completed FROM completion_history WHERE arxiv_id=?",
                           (paper_id,)).fetchone()
    if row is None or row[0] != "chunked" or row[1] != "ok":
        raise PublicationBlocked("paper_not_publishable", "paper is not successfully parsed and chunked")
    if history is None or not history[0] or history[1]:
        raise PublicationBlocked("legacy_or_completed", "only genuinely new papers may be published")
    cache = Path(cache_path)
    cache_root = Path(data_dir).resolve() / "chunk_cache"
    cache_bytes = _bounded_read(cache, cache_root, 8 * 1024 * 1024)
    cache_hash = hashlib.sha256(cache_bytes).hexdigest()
    try:
        cache_records = json.loads(cache_bytes, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError) as exc:
        raise PublicationBlocked("invalid_cache", "chunk cache is not finite JSON") from exc
    if not isinstance(cache_records, list):
        raise PublicationBlocked("invalid_cache", "chunk cache must be a JSON list")
    cache_by_index = {}
    for record in cache_records:
        if not isinstance(record, dict) or isinstance(record.get("chunk_index"), bool) or not isinstance(record.get("chunk_index"), int):
            raise PublicationBlocked("invalid_cache", "chunk records require integer chunk_index")
        if record["chunk_index"] in cache_by_index:
            raise PublicationBlocked("invalid_cache", "duplicate chunk index in cache")
        cache_by_index[record["chunk_index"]] = record
    if not isinstance(points, list) or len(points) > 3000:
        raise PublicationBlocked("invalid_points", "points must be a list of at most 3000")
    try:
        cloned = json.loads(json.dumps(points, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise PublicationBlocked("invalid_points", "points are not finite JSON") from exc
    seen = set()
    dim = None
    for point in cloned:
        payload = point.get("payload") if isinstance(point, dict) else None
        if not isinstance(payload, dict) or payload.get("arxiv_id") != paper_id:
            raise PublicationBlocked("paper_mismatch", "point paper id mismatch")
        index = payload.get("chunk_index")
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{paper_id}#{index}")) if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None
        if expected is None or expected in seen or str(point.get("id")) != expected:
            raise PublicationBlocked("invalid_point_id", "chunk index or stable point id invalid")
        seen.add(expected)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PublicationBlocked("invalid_text", "point text must be nonempty")
        if index not in cache_by_index or cache_by_index[index].get("text") != text:
            raise PublicationBlocked("cache_point_mismatch", "point index/text differs from chunk cache")
        vector = point.get("vector")
        if not isinstance(vector, list) or not vector or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
            raise PublicationBlocked("invalid_vector", "vectors must be finite nonempty arrays")
        if dim is None:
            dim = len(vector)
        elif dim != len(vector):
            raise PublicationBlocked("invalid_vector", "vector dimensions differ")
    published = conn.execute("SELECT 1 FROM paper_publications WHERE arxiv_id=? AND kind='snapshot' "
                             "AND state='published' LIMIT 1", (paper_id,)).fetchone()
    if published:
        raise PublicationBlocked("already_published", "first publication is immutable")
    metadata = {key: row[key] for key in ("title", "year", "layers", "source_url")}
    revision_material = json.dumps({"points": cloned, "metadata": metadata}, sort_keys=True,
                                  separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    revision = hashlib.sha256((cache_hash + revision_material).encode()).hexdigest()
    for point in cloned:
        point.setdefault("payload", {}).update(arxiv_id=paper_id, publication_revision=revision)
    path = _store(conn, data_dir, paper_id, revision, "snapshot",
                   {"points": cloned, "cache_path": str(cache.resolve()), "metadata": metadata}, cache_hash)
    state = conn.execute("SELECT state FROM paper_publications WHERE arxiv_id=? AND revision=?",
                         (paper_id, revision)).fetchone()[0]
    if state == "blocked":
        raise PublicationBlocked("job_blocked", "snapshot journal entry is blocked")
    if state != "published":
        conn.execute("UPDATE paper_publications SET state='ready' WHERE arxiv_id=? AND revision=?", (paper_id, revision))
        conn.commit()
    return {"status": "ready", "paper_id": paper_id, "revision": revision,
            "job_kind": "snapshot", "artifact_path": path}


def publish_snapshot(conn, job, qdrant_write, fts_write, complete, preflight):
    if not callable(preflight):
        raise TypeError("snapshot publication requires a Qdrant preflight callback")
    if conn.in_transaction:
        raise PublicationBlocked("transaction_active", "publisher requires idle connection")
    row = conn.execute("SELECT * FROM paper_publications WHERE arxiv_id=? AND revision=?",
                       (job["paper_id"], job["revision"])).fetchone()
    if row is None or row["kind"] != "snapshot":
        raise PublicationBlocked("job_missing", "snapshot journal entry not found")
    if row["state"] == "published":
        return True
    if row["state"] not in ("ready", "staged"):
        raise PublicationBlocked("job_not_ready", "journal entry is not publishable")
    if conn.execute("SELECT 1 FROM paper_publications WHERE arxiv_id=? AND kind='snapshot' "
                    "AND revision<>? LIMIT 1", (job["paper_id"], job["revision"])).fetchone():
        raise PublicationBlocked("snapshot_conflict", "paper owns conflicting snapshot revisions")
    artifact_path = Path(row["artifact_path"])
    if artifact_path.parent.name != "publication_cache" or artifact_path.parent.is_symlink():
        raise PublicationBlocked("unsafe_path", "artifact is outside publication_cache")
    raw = _bounded_read(artifact_path, artifact_path.parent, 64 * 1024 * 1024)
    try:
        artifact = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, TypeError) as exc:
        raise PublicationBlocked("invalid_artifact", "staged artifact is invalid JSON") from exc
    def assert_metadata_current():
        current = conn.execute("SELECT title,year,layers,source_url FROM papers WHERE arxiv_id=?",
                               (job["paper_id"],)).fetchone()
        expected = artifact.get("metadata") or {}
        if current is None or any(current[key] != expected.get(key)
                                  for key in ("title", "year", "layers", "source_url")):
            raise PublicationBlocked("metadata_drift", "paper metadata changed after snapshot staging")

    def assert_eligible():
        paper = conn.execute("SELECT status,parse_status FROM papers WHERE arxiv_id=?",
                             (job["paper_id"],)).fetchone()
        history = conn.execute("SELECT first_known,completed FROM completion_history WHERE arxiv_id=?",
                               (job["paper_id"],)).fetchone()
        if (paper is None or paper["status"] != "chunked" or paper["parse_status"] != "ok"
                or history is None or not history[0] or history[1]):
            raise PublicationBlocked("eligibility_changed", "paper no longer eligible for publication")

    assert_eligible()
    assert_metadata_current()
    points = artifact["points"]
    original_points = json.loads(json.dumps(points))
    for point in original_points:
        point.get("payload", {}).pop("publication_revision", None)
    expected_revision = hashlib.sha256((row["cache_sha256"] + json.dumps(
        {"points": original_points, "metadata": artifact.get("metadata")}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False)).encode()).hexdigest()
    if expected_revision != row["revision"]:
        raise PublicationBlocked("artifact_hash_mismatch", "staged artifact integrity check failed")
    cache_path = Path(artifact["cache_path"])
    def assert_cache_current():
        if (cache_path.parent.name != "chunk_cache" or cache_path.parent.is_symlink()
                or cache_path.parent.parent.resolve() != artifact_path.parent.parent.resolve()
                or hashlib.sha256(_bounded_read(cache_path, cache_path.parent.parent,
                                                8 * 1024 * 1024)).hexdigest() != row["cache_sha256"]):
            raise PublicationBlocked("cache_hash_mismatch", "chunk cache changed after snapshot staging")

    assert_cache_current()
    if any(point.get("payload", {}).get("publication_revision") != row["revision"]
           or point.get("payload", {}).get("arxiv_id") != job["paper_id"] for point in points):
        raise PublicationBlocked("artifact_marker_mismatch", "point publication marker does not match journal")
    done = set(json.loads(row["completed"] or "[]"))
    for name, callback in (("preflight", preflight), ("qdrant", qdrant_write), ("fts", fts_write)):
        if name in done and not (name == "preflight" and "qdrant" not in done):
            continue
        assert_eligible()
        assert_metadata_current()
        assert_cache_current()
        if conn.in_transaction:
            raise PublicationBlocked("transaction_active", "external callback inside SQLite transaction")
        try:
            if callback(job, points) is not True:
                raise PublicationRetry(f"{name}_not_acknowledged", f"{name} write not acknowledged")
        except PublicationBlocked:
            raise
        except PublicationRetry:
            raise
        except Exception as exc:
            raise PublicationRetry(f"{name}_failed", str(exc)) from exc
        done.add(name)
        conn.execute("UPDATE paper_publications SET completed=? WHERE arxiv_id=? AND revision=?",
                     (json.dumps(sorted(done)), job["paper_id"], job["revision"]))
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT state FROM paper_publications WHERE arxiv_id=? AND revision=?",
                               (job["paper_id"], job["revision"])).fetchone()
        if current is None:
            raise PublicationBlocked("job_missing", "journal entry disappeared before finalization")
        if current[0] == "published":
            conn.commit()
            return True
        if current[0] not in ("ready", "staged"):
            raise PublicationBlocked("job_not_ready", "journal entry is not publishable")
        assert_eligible()
        assert_metadata_current()
        assert_cache_current()
        result = complete(job["paper_id"], commit=False)
        if result is not True:
            conn.rollback()
            raise PublicationRetry("completion_not_acknowledged", "paper completion was not acknowledged")
        conn.execute("UPDATE paper_publications SET state='published',error_code=NULL "
                     "WHERE arxiv_id=? AND revision=?", (job["paper_id"], job["revision"]))
        conn.commit()
        return True
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise PublicationRetry("completion_failed", str(exc)) from exc
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def reconcile_publications(conn, data_dir, qdrant_write, fts_write, complete, preflight, limit=1,
                           *, on_success=None, paper_ids=None):
    if not callable(preflight):
        raise TypeError("publication reconciliation requires a Qdrant preflight callback")
    scope = tuple(sorted(set(paper_ids))) if paper_ids is not None else None
    if scope == ():
        return 0
    clause = (" AND arxiv_id IN (" + ",".join("?" for _ in scope) + ")") if scope else ""
    rows = conn.execute("SELECT * FROM paper_publications WHERE kind='snapshot' AND state IN ('ready','staged') "
                        "AND retry_after<=?" + clause + " ORDER BY retry_after LIMIT ?",
                        (__import__('time').time(), *(scope or ()), limit)).fetchall()
    count = 0
    for row in rows:
        job = {"paper_id": row["arxiv_id"], "revision": row["revision"], "job_kind": "snapshot"}
        try:
            published = bool(publish_snapshot(conn, job, qdrant_write, fts_write, complete, preflight))
        except PublicationRetry as exc:
            conn.execute("UPDATE paper_publications SET retry_after=?,error_code=? WHERE arxiv_id=? AND revision=?",
                         (__import__('time').time() + 30, exc.code, row["arxiv_id"], row["revision"]))
            conn.commit()
        except PublicationBlocked as exc:
            conn.execute("UPDATE paper_publications SET state='blocked',error_code=? WHERE arxiv_id=? AND revision=?",
                         (exc.code, row["arxiv_id"], row["revision"]))
            conn.commit()
        else:
            count += published
            if published and on_success is not None:
                try:
                    on_success(job)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).warning("publication progress observer failed: %s", type(exc).__name__)
    return count


def ensure_revision_schema(conn):
    """Explicit setup only; existing first-publication callers do not invoke this."""
    if conn.in_transaction:
        raise PublicationBlocked("transaction_active", "revision setup requires idle connection")
    ensure_publication_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS paper_active (arxiv_id TEXT PRIMARY KEY,"
                 "revision TEXT NOT NULL,generation INTEGER NOT NULL CHECK(generation>0))")
    conn.execute("CREATE TABLE IF NOT EXISTS publication_epoch (singleton INTEGER PRIMARY KEY "
                 "CHECK(singleton=1),epoch INTEGER NOT NULL CHECK(epoch>=0))")
    conn.execute("INSERT OR IGNORE INTO publication_epoch VALUES(1,0)")
    conn.execute("CREATE TABLE IF NOT EXISTS publication_vector_claims (point_id TEXT PRIMARY KEY,"
                 "collection TEXT NOT NULL,family TEXT NOT NULL,arxiv_id TEXT NOT NULL,revision TEXT NOT NULL)")
    conn.commit()


def _revision_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _revision_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@contextmanager
def revision_writer(state_path, chunk_fts_path, paper_fts_path, *, timeout,
                    storage_admission_sha256):
    """Local attached writer; admission pin represents external VFS/capacity approval."""
    if (not _revision_digest(storage_admission_sha256) or type(timeout) not in (int, float)
            or not math.isfinite(timeout) or timeout < 0):
        raise PublicationBlocked("storage_not_admitted", "explicit storage admission and timeout required")
    paths = [Path(os.path.abspath(p)) for p in (state_path, chunk_fts_path, paper_fts_path)]
    identities = set()
    for path in paths:
        if str(path).startswith("\\\\"):
            raise PublicationBlocked("unsafe_path", "network filesystem is not admitted")
        for part in (path, *path.parents):
            info = part.lstat()
            if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PublicationBlocked("unsafe_path", "linked/reparse database path")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PublicationBlocked("unsafe_path", "database must be a single regular file")
        identities.add((info.st_dev, info.st_ino))
        with path.open("rb") as stream:
            header = stream.read(100)
        if header[:16] != b"SQLite format 3\0" or header[18:20] != b"\x01\x01":
            raise PublicationBlocked("rollback_mode_required", "WAL/unknown file format is not converted")
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise PublicationBlocked("rollback_mode_required", "WAL sidecars require offline resolution")
    if len(identities) != 3:
        raise PublicationBlocked("database_alias", "three distinct disk databases required")
    conn = sqlite3.connect(paths[0].as_uri() + "?mode=rw", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        for name, path in zip(("chunk_fts", "paper_fts"), paths[1:]):
            conn.execute(f"ATTACH DATABASE ? AS {name}", (path.as_uri() + "?mode=rw",))
        for name in ("main", "chunk_fts", "paper_fts"):
            if conn.execute(f"PRAGMA {name}.journal_mode").fetchone()[0] not in ("delete", "truncate", "persist"):
                raise PublicationBlocked("rollback_mode_required", "durable rollback journal required")
            conn.execute(f"PRAGMA {name}.synchronous=FULL")
        _revision_connection(conn)
        yield conn
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _revision_connection(conn):
    databases = {row[1]: row[2] for row in conn.execute("PRAGMA database_list") if row[1] != "temp"}
    if set(databases) != {"main", "chunk_fts", "paper_fts"} or not all(databases.values()):
        raise PublicationBlocked("attached_disks_required", "state and both FTS disks must share one connection")
    for schema in databases:
        if (conn.execute(f"PRAGMA {schema}.journal_mode").fetchone()[0] not in ("delete", "truncate", "persist")
                or conn.execute(f"PRAGMA {schema}.synchronous").fetchone()[0] != 2):
            raise PublicationBlocked("durability_changed", "rollback/FULL regime changed")
        if conn.execute(f"SELECT 1 FROM {schema}.sqlite_schema WHERE type IN ('trigger','view') LIMIT 1").fetchone():
            raise PublicationBlocked("unsupported_schema", "unreviewed triggers/views are not supported")
    definitions = {
        "chunk_fts": ("chunks", "text,point_id UNINDEXED,arxiv_id UNINDEXED,section_type UNINDEXED,"
                      "element_type UNINDEXED,layers UNINDEXED,year UNINDEXED"),
        "paper_fts": ("papers_fts", "arxiv_id UNINDEXED,title,abstract,layers UNINDEXED,year UNINDEXED"),
    }
    normalize = lambda sql: re.sub(r"\s+", "", sql.lower()).replace("ifnotexists", "")
    for schema, (table, columns) in definitions.items():
        row = conn.execute(f"SELECT sql FROM {schema}.sqlite_schema WHERE name=? AND type='table'", (table,)).fetchone()
        expected = f"CREATE VIRTUAL TABLE {table} USING fts5({columns},tokenize='porter unicode61')"
        if row is None or normalize(row[0]) != normalize(expected):
            raise PublicationBlocked("unsupported_fts", "only reviewed internal-content porter unicode61 FTS5 supported")
        for table_name, names in (("pipeline_fts_papers", ["table_name", "arxiv_id"]),
                                  ("pipeline_fts_rows", ["table_name", "arxiv_id", "row_id"])):
            if [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table_name})")] != names:
                raise PublicationBlocked("unsupported_catalog", "existing ownership catalog required")
    if [r[1] for r in conn.execute("PRAGMA chunk_fts.table_info(pipeline_fts_points)")] != ["point_id", "arxiv_id", "row_id"]:
        raise PublicationBlocked("unsupported_catalog", "exact point ownership catalog required")
    scope = conn.execute("SELECT complete FROM chunk_fts.pipeline_fts_scope WHERE singleton=1").fetchone()
    if scope is None or scope[0] != 1:
        raise PublicationBlocked("ownership_incomplete", "legacy ownership must be reconciled before revisions")
    for table, required in (("papers", {"arxiv_id", "status", "title", "abstract", "year", "layers", "source_url"}),
                            ("paper_active", {"arxiv_id", "revision", "generation"}),
                            ("publication_epoch", {"singleton", "epoch"}),
                            ("publication_vector_claims", {"point_id", "collection", "family", "arxiv_id", "revision"}),
                            ("paper_publications", {"arxiv_id", "revision", "kind", "artifact_path",
                                                    "cache_sha256", "state", "completed", "error_code"})):
        if not required.issubset({r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")}):
            raise PublicationBlocked("unsupported_state", "explicit revision schema setup required")
    for schema, table, key in (
            ("main", "papers", ["arxiv_id"]), ("main", "paper_active", ["arxiv_id"]),
            ("main", "publication_epoch", ["singleton"]),
            ("main", "publication_vector_claims", ["point_id"]),
            ("main", "paper_publications", ["arxiv_id", "revision"]),
            ("chunk_fts", "pipeline_fts_points", ["point_id"]),
            ("chunk_fts", "pipeline_fts_scope", ["singleton"]),
            *((schema, table, key) for schema in ("chunk_fts", "paper_fts")
              for table, key in (("pipeline_fts_papers", ["table_name", "arxiv_id"]),
                                 ("pipeline_fts_rows", ["table_name", "arxiv_id", "row_id"])))):
        columns = list(conn.execute(f"PRAGMA {schema}.table_info({table})"))
        primary = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
        if primary != key:
            raise PublicationBlocked("unsupported_schema", "reviewed primary-key ownership required")


def _revision_projection(projection):
    def require(condition):
        if not condition:
            raise ValueError("invalid projection field")
    try:
        value = json.loads(_revision_bytes(projection))
        require(isinstance(value, dict) and set(value) == {"metadata", "chunks"})
        meta = value["metadata"]
        require(isinstance(meta, dict) and set(meta) == {"title", "abstract", "layers", "year", "source_url"})
        require(all(isinstance(meta[k], str) for k in ("title", "abstract", "layers")))
        require(meta["year"] is None or type(meta["year"]) is int)
        require(meta["source_url"] is None or isinstance(meta["source_url"], str))
        require(isinstance(value["chunks"], list) and 0 < len(value["chunks"]) <= 3000)
        ids = []
        for chunk in value["chunks"]:
            require(isinstance(chunk, dict) and set(chunk) == {"point_id", "text", "section_type", "element_type"})
            require(isinstance(chunk["point_id"], str) and str(uuid.UUID(chunk["point_id"])) == chunk["point_id"])
            require(isinstance(chunk["text"], str) and bool(chunk["text"].strip()))
            require(all(chunk[k] is None or isinstance(chunk[k], str) for k in ("section_type", "element_type")))
            ids.append(chunk["point_id"])
        require(len(ids) == len(set(ids)))
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise PublicationBlocked("invalid_projection", "bounded canonical projection required") from exc
    value["chunks"].sort(key=lambda c: c["point_id"])
    return value


def _revision_coarse_input(projection, recipe, bindings):
    required = {"source_sha256", "abstract_char_limit", "min_abstract_len", "fallback_text", "fallback_source_sha256"}
    if (not isinstance(recipe, dict) or set(recipe) != required or not _revision_digest(recipe["source_sha256"])
            or any(type(recipe[k]) is not int or recipe[k] < 1 for k in ("abstract_char_limit", "min_abstract_len"))):
        raise PublicationBlocked("invalid_vector_recipe", "explicit pinned coarse recipe required")
    title = projection["metadata"]["title"].strip()
    abstract = projection["metadata"]["abstract"].strip()
    if len(abstract) >= recipe["min_abstract_len"]:
        if recipe["fallback_text"] is not None or recipe["fallback_source_sha256"] is not None:
            raise PublicationBlocked("invalid_vector_recipe", "unused fallback is not admitted")
        body = abstract
    else:
        body = recipe["fallback_text"]
        pin = recipe["fallback_source_sha256"]
        if (not isinstance(body, str) or not body.strip() or not _revision_digest(pin)
                or bindings.get("coarse_fallback") != pin):
            raise PublicationBlocked("coarse_fallback_unestablished", "short abstract needs explicitly bound resolved fallback")
        body = body.strip()
    limit = recipe["abstract_char_limit"]
    if len(body) > limit:
        cut = body[:limit]
        best = max(cut.rfind(sep) for sep in (". ", ".\n", "! ", "? "))
        if best > limit * 0.4:
            body = cut[:best + 1].strip()
        else:
            word = cut.rfind(" ")
            body = (cut[:word] if word > 0 else cut).strip()
    return f"{title}. {body}" if title else body


REVISION_API_SOURCE_SHA256 = "886b70d920901059566f532767c5b62b41a9d4c8822eb91fbd14d4ea7fb9dd66"
REVISION_PAYLOAD_EXCLUSIONS = ("api_payload_sha256", "publication_revision", "visibility_intervals")


def _revision_payload_bytes(payload):
    if not isinstance(payload, dict) or any(not isinstance(k, str) for k in payload):
        raise PublicationBlocked("api_payload_invalid", "JSON object required")
    try:
        return _revision_bytes({k: v for k, v in payload.items() if k not in REVISION_PAYLOAD_EXCLUSIONS})
    except (TypeError, ValueError) as exc:
        raise PublicationBlocked("api_payload_invalid", "finite canonical JSON required") from exc


def _revision_payload_snapshot(artifact):
    snapshot = artifact.get("api_payload_snapshot")
    if (artifact.get("schema") != "dtox-publication-revision-2" or not isinstance(snapshot, dict)
            or set(snapshot) != {"schema", "api_source_sha256", "projection_sha256", "exclusions", "families"}
            or snapshot["schema"] != "dtox-api-payload-snapshot-1"
            or snapshot["api_source_sha256"] != REVISION_API_SOURCE_SHA256
            or snapshot["exclusions"] != list(REVISION_PAYLOAD_EXCLUSIONS)
            or snapshot["projection_sha256"] != hashlib.sha256(_revision_bytes(artifact["projection"])).hexdigest()
            or not isinstance(snapshot["families"], dict) or set(snapshot["families"]) != {"fulltext", "coarse"}):
        raise PublicationBlocked("api_payload_snapshot_version", "explicit admitted v2 payload snapshot required; no legacy fallback")
    for name, family in artifact["vector_families"].items():
        rows = snapshot["families"][name]
        if not isinstance(rows, dict) or set(rows) != {p["point_id"] for p in family["points"]}:
            raise PublicationBlocked("api_payload_coverage", "exact family point coverage required")
        for row in rows.values():
            if (not isinstance(row, dict) or set(row) != {"payload", "sha256"} or not isinstance(row["payload"], dict)
                    or any(k in row["payload"] for k in REVISION_PAYLOAD_EXCLUSIONS)
                    or hashlib.sha256(_revision_payload_bytes(row["payload"])).hexdigest() != row["sha256"]):
                raise PublicationBlocked("api_payload_snapshot_hash", "complete semantic payload/hash required")
    if artifact["artifact_bindings"].get("api_payload_snapshot") != hashlib.sha256(_revision_bytes(snapshot)).hexdigest():
        raise PublicationBlocked("api_payload_snapshot_binding", "snapshot SHA must be journal bound")
    return snapshot


def _revision_vectors(artifact):
    families = artifact.get("vector_families")
    if not isinstance(families, dict) or set(families) != {"fulltext", "coarse"}:
        raise PublicationBlocked("vector_coverage", "both fulltext and coarse families are mandatory")
    coarse = _revision_coarse_input(artifact["projection"], artifact.get("coarse_recipe"), artifact["artifact_bindings"])
    if not _revision_digest(artifact["artifact_bindings"].get("vector_ownership")):
        raise PublicationBlocked("vector_ownership_unestablished", "sealed ownership admission for both collections required")
    ids, collections = set(), set()
    for name, family in families.items():
        if (not isinstance(family, dict) or set(family) != {"collection", "model_id", "model_sha256", "dimension", "recipe_sha256", "points"}
                or any(not isinstance(family[k], str) or not family[k] for k in ("collection", "model_id"))
                or not _revision_digest(family["model_sha256"]) or not _revision_digest(family["recipe_sha256"])
                or type(family["dimension"]) is not int or family["dimension"] < 1
                or not isinstance(family["points"], list)):
            raise PublicationBlocked("vector_profile", "closed collection/model/dimension/recipe pins required")
        if family["collection"] in collections:
            raise PublicationBlocked("vector_collision", "families must use distinct collections")
        collections.add(family["collection"])
        if name == "coarse":
            recipe = artifact["coarse_recipe"]
            fixed = {k: recipe[k] for k in ("source_sha256", "abstract_char_limit", "min_abstract_len")}
            if family["recipe_sha256"] != hashlib.sha256(_revision_bytes(fixed)).hexdigest():
                raise PublicationBlocked("vector_profile", "coarse recipe parameters must match their pin")
        expected = ({c["point_id"]: hashlib.sha256(c["text"].encode("utf-8")).hexdigest()
                     for c in artifact["projection"]["chunks"]} if name == "fulltext" else None)
        if len(family["points"]) != (len(expected) if expected is not None else 1):
            raise PublicationBlocked("vector_coverage", "no extra or omitted declared vectors")
        for point in family["points"]:
            if (not isinstance(point, dict) or set(point) != {"point_id", "input_text_sha256", "vector_sha256", "reuse_from"}
                    or not isinstance(point["point_id"], str)
                    or not _revision_digest(point["input_text_sha256"]) or not _revision_digest(point["vector_sha256"])):
                raise PublicationBlocked("vector_point", "exact point/input/vector binding required")
            try:
                valid = str(uuid.UUID(point["point_id"])) == point["point_id"]
            except ValueError:
                valid = False
            if not valid or point["point_id"] in ids:
                raise PublicationBlocked("vector_collision", "globally distinct canonical UUIDs required")
            ids.add(point["point_id"])
            text_hash = expected.get(point["point_id"]) if expected is not None else hashlib.sha256(coarse.encode("utf-8")).hexdigest()
            if text_hash != point["input_text_sha256"]:
                raise PublicationBlocked("vector_input", "vector input differs from exact projection recipe")
            reuse = point["reuse_from"]
            if reuse is not None and (not isinstance(reuse, dict) or set(reuse) != {"revision", "collection", "point_id"}
                                      or not _revision_digest(reuse["revision"])):
                raise PublicationBlocked("vector_lineage", "explicit parent vector reference required")
    _revision_payload_snapshot(artifact)
    return families


def _revision_vector_transition(target, prior, parent_revision):
    families, old = _revision_vectors(target), _revision_vectors(prior)
    old_ids = {p["point_id"] for f in old.values() for p in f["points"]}
    for name, family in families.items():
        profile = {k: v for k, v in family.items() if k != "points"}
        if profile != {k: v for k, v in old[name].items() if k != "points"}:
            raise PublicationBlocked("vector_profile_changed", "model/collection/recipe changes are outside revision scope")
        for point in family["points"]:
            if point["point_id"] in old_ids:
                raise PublicationBlocked("vector_parent_overwrite", "all target points must be revision-isolated")
            matches = [p for p in old[name]["points"] if p["input_text_sha256"] == point["input_text_sha256"]]
            reuse = point["reuse_from"]
            if matches:
                source = next((p for p in matches if reuse == {"revision": parent_revision,
                    "collection": family["collection"], "point_id": p["point_id"]}), None)
                if source is None or source["vector_sha256"] != point["vector_sha256"]:
                    raise PublicationBlocked("vector_lineage", "unchanged input must reuse exact pinned parent vector")
            elif reuse is not None:
                raise PublicationBlocked("vector_lineage", "changed input cannot reuse parent embedding")


def _claim_revision_vectors(conn, paper_id, revision, artifact, *, reserve=False):
    for name, family in _revision_vectors(artifact).items():
        for point in family["points"]:
            claim = (point["point_id"], family["collection"], name, paper_id, revision)
            if reserve:
                conn.execute("INSERT OR IGNORE INTO publication_vector_claims VALUES(?,?,?,?,?)", claim)
            row = conn.execute("SELECT point_id,collection,family,arxiv_id,revision FROM publication_vector_claims WHERE point_id=?",
                               (point["point_id"],)).fetchone()
            if row is None or tuple(row) != claim:
                raise PublicationBlocked("vector_collision", "journal vector claim missing or owned by another revision/paper/collection")


def revision_readiness_expected(paper_id, revision, artifact):
    """Receipt schema only, NOT a verifier; HTTP adapter must derive every readback."""
    families = json.loads(_revision_bytes(_revision_vectors(artifact)))
    metadata_sha = hashlib.sha256(_revision_bytes(artifact["projection"]["metadata"])).hexdigest()
    for name, family in families.items():
        for point in family["points"]:
            point.update(owner_paper_id=paper_id, owner_revision=revision, metadata_sha256=metadata_sha,
                         api_payload_sha256=artifact["api_payload_snapshot"]["families"][name][point["point_id"]]["sha256"],
                         reuse_readback_sha256=point["vector_sha256"] if point["reuse_from"] is not None else None)
    return {"paper_id": paper_id, "revision": revision, "artifact_sha256": revision,
            "ownership_admission_sha256": artifact["artifact_bindings"]["vector_ownership"],
            "collision_scope": sorted(f["collection"] for f in families.values()),
            "vector_families": families, "durable": True, "revision_isolated": True, "verified": True}


def prepare_revision(conn, data_dir, paper_id, projection, *, expected_revision,
                     expected_generation, artifact_bindings, vector_families, coarse_recipe, api_payload_snapshot=None):
    """Stage only an immutable journal artifact; never insert candidate FTS rows."""
    if conn.in_transaction:
        raise PublicationBlocked("transaction_active", "staging requires idle connection")
    active = conn.execute("SELECT revision,generation FROM paper_active WHERE arxiv_id=?", (paper_id,)).fetchone()
    if (not _revision_digest(expected_revision) or type(expected_generation) is not int
            or expected_generation < 1 or active is None or tuple(active) != (expected_revision, expected_generation)):
        raise PublicationBlocked("parent_changed", "only an explicitly managed current parent can be revised")
    if (not isinstance(artifact_bindings, dict) or not artifact_bindings
            or any(not isinstance(k, str) or not k or not _revision_digest(v) for k, v in artifact_bindings.items())):
        raise PublicationBlocked("invalid_bindings", "validated immutable artifact SHA bindings required")
    artifact = {"schema": "dtox-publication-revision-2", "paper_id": paper_id, "parent_revision": expected_revision,
                "parent_generation": expected_generation, "projection": _revision_projection(projection),
                "artifact_bindings": artifact_bindings, "vector_families": vector_families,
                "coarse_recipe": coarse_recipe, "api_payload_snapshot": api_payload_snapshot}
    artifact = json.loads(_revision_bytes(artifact))
    _, prior = _load_revision(conn, data_dir, paper_id, expected_revision)
    _revision_vector_transition(artifact, prior, expected_revision)
    raw = _revision_bytes(artifact)
    revision = hashlib.sha256(raw).hexdigest()
    path = _store(conn, data_dir, paper_id, revision, "revision", artifact, revision)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _claim_revision_vectors(conn, paper_id, revision, artifact, reserve=True)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"paper_id": paper_id, "revision": revision, "job_kind": "revision", "artifact_path": path}


def _load_revision(conn, data_dir, paper_id, revision):
    row = conn.execute("SELECT * FROM paper_publications WHERE arxiv_id=? AND revision=?",
                       (paper_id, revision)).fetchone()
    if row is None or row["kind"] != "revision" or row["state"] not in ("staged", "published"):
        raise PublicationBlocked("revision_not_ready", "managed revision journal artifact required")
    raw = _bounded_read(row["artifact_path"], Path(data_dir) / "publication_cache", 8 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != revision or row["cache_sha256"] != revision:
        raise PublicationBlocked("artifact_hash_mismatch", "revision bytes changed")
    artifact = json.loads(raw)
    if artifact["paper_id"] != paper_id or _revision_projection(artifact["projection"]) != artifact["projection"]:
        raise PublicationBlocked("invalid_projection", "revision projection is not canonical")
    _revision_vectors(artifact)
    return row, artifact


def _read_revision_projection(conn, paper_id):
    row = conn.execute("SELECT title,abstract,layers,year,source_url,status FROM main.papers WHERE arxiv_id=?",
                       (paper_id,)).fetchone()
    if row is None or row["status"] != "done":
        raise PublicationBlocked("active_not_done", "active metadata must remain done")
    meta = {k: row[k] for k in ("title", "abstract", "layers", "year", "source_url")}
    def year_matches(value):
        if meta["year"] is None:
            return value is None
        return type(value) in (int, str) and value in (meta["year"], str(meta["year"]))
    chunks = []
    for schema, table in (("chunk_fts", "chunks"), ("paper_fts", "papers_fts")):
        if conn.execute(f"SELECT 1 FROM {schema}.pipeline_fts_papers WHERE table_name=? AND arxiv_id=?",
                        (table, paper_id)).fetchone() is None:
            raise PublicationBlocked("unowned_projection", "legacy adoption is not implicit")
        owned = [r[0] for r in conn.execute(f"SELECT row_id FROM {schema}.pipeline_fts_rows WHERE table_name=? AND arxiv_id=?",
                                           (table, paper_id))]
        actual = conn.execute(f"SELECT rowid,* FROM {schema}.{table} WHERE arxiv_id=?", (paper_id,)).fetchall()
        if len(owned) != len(set(owned)) or set(owned) != {r["rowid"] for r in actual}:
            raise PublicationBlocked("ownership_incomplete", "all selected-paper rows must be owned")
        if schema == "chunk_fts":
            mapped = {r["point_id"]: r["row_id"] for r in conn.execute(
                "SELECT point_id,row_id FROM chunk_fts.pipeline_fts_points WHERE arxiv_id=?", (paper_id,))}
            if mapped != {r["point_id"]: r["rowid"] for r in actual}:
                raise PublicationBlocked("ownership_incomplete", "exact UUID ownership mismatch")
            for r in actual:
                if r["layers"] != meta["layers"] or not year_matches(r["year"]):
                    raise PublicationBlocked("projection_drift", "chunk metadata differs from active state")
                chunks.append({k: r[k] for k in ("point_id", "text", "section_type", "element_type")})
        elif (len(actual) != 1 or tuple(actual[0][k] for k in ("title", "abstract", "layers")) != (
                meta["title"], meta["abstract"], meta["layers"]) or not year_matches(actual[0]["year"])):
            raise PublicationBlocked("projection_drift", "paper FTS metadata differs from active state")
    return _revision_projection({"metadata": meta, "chunks": chunks})


def _replace_revision_fts(conn, paper_id, projection, fault):
    meta = projection["metadata"]
    for schema, table in (("chunk_fts", "chunks"), ("paper_fts", "papers_fts")):
        ids = conn.execute(f"SELECT row_id FROM {schema}.pipeline_fts_rows WHERE table_name=? AND arxiv_id=?",
                           (table, paper_id)).fetchall()
        conn.executemany(f"DELETE FROM {schema}.{table} WHERE rowid=?", ids)
        conn.execute(f"DELETE FROM {schema}.pipeline_fts_rows WHERE table_name=? AND arxiv_id=?", (table, paper_id))
        if table == "chunks":
            conn.execute("DELETE FROM chunk_fts.pipeline_fts_points WHERE arxiv_id=?", (paper_id,))
            for chunk in projection["chunks"]:
                if conn.execute("SELECT 1 FROM chunk_fts.pipeline_fts_points WHERE point_id=?", (chunk["point_id"],)).fetchone():
                    raise PublicationBlocked("point_collision", "candidate UUID belongs to another paper")
                cursor = conn.execute("INSERT INTO chunk_fts.chunks(text,point_id,arxiv_id,section_type,element_type,layers,year) "
                                      "VALUES (?,?,?,?,?,?,?)", (chunk["text"], chunk["point_id"], paper_id,
                                      chunk["section_type"], chunk["element_type"], meta["layers"], meta["year"]))
                conn.execute("INSERT INTO chunk_fts.pipeline_fts_points VALUES (?,?,?)", (chunk["point_id"], paper_id, cursor.lastrowid))
                conn.execute("INSERT INTO chunk_fts.pipeline_fts_rows VALUES ('chunks',?,?)", (paper_id, cursor.lastrowid))
        else:
            cursor = conn.execute("INSERT INTO paper_fts.papers_fts(arxiv_id,title,abstract,layers,year) VALUES (?,?,?,?,?)",
                                  (paper_id, meta["title"], meta["abstract"], meta["layers"], meta["year"]))
            conn.execute("INSERT INTO paper_fts.pipeline_fts_rows VALUES ('papers_fts',?,?)", (paper_id, cursor.lastrowid))
        fault("after_" + schema)


def _commit_revision(conn, data_dir, paper_id, revision, expected_revision, expected_generation,
                     qdrant_ready, *, rollback, fault):
    if conn.in_transaction:
        raise PublicationBlocked("transaction_active", "revision commit requires idle connection")
    if (not _revision_digest(expected_revision) or not _revision_digest(revision)
            or type(expected_generation) is not int or expected_generation < 1):
        raise PublicationBlocked("invalid_parent", "explicit revision and positive generation required")
    _revision_connection(conn)
    target_row, target = _load_revision(conn, data_dir, paper_id, revision)
    prior_row, prior = _load_revision(conn, data_dir, paper_id, expected_revision)
    if prior_row["state"] != "published":
        raise PublicationBlocked("invalid_parent", "active parent must have a committed journal record")
    _claim_revision_vectors(conn, paper_id, revision, target)
    _claim_revision_vectors(conn, paper_id, expected_revision, prior)
    if rollback:
        if prior["parent_revision"] != revision or target_row["state"] != "published":
            raise PublicationBlocked("invalid_rollback", "only the retained direct parent may be restored")
    elif target["parent_revision"] != expected_revision or target["parent_generation"] != expected_generation:
        raise PublicationBlocked("parent_changed", "candidate was staged against another parent")
    if not rollback:
        _revision_vector_transition(target, prior, expected_revision)
    if not rollback and {c["point_id"] for c in target["projection"]["chunks"]} & {c["point_id"] for c in prior["projection"]["chunks"]}:
        raise PublicationBlocked("revision_id_collision", "candidate vectors must not overwrite parent IDs")
    receipt = qdrant_ready({"paper_id": paper_id, "revision": revision}, json.loads(_revision_bytes(target)))
    expected = revision_readiness_expected(paper_id, revision, target)
    if (not isinstance(receipt, dict) or _revision_bytes(receipt) != _revision_bytes(expected)
            or any(receipt.get(k) is not True for k in ("durable", "revision_isolated", "verified"))):
        raise PublicationBlocked("qdrant_not_ready", "exact durable revision-isolated readback receipt required")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _revision_connection(conn)
        current = conn.execute("SELECT revision,generation FROM main.paper_active WHERE arxiv_id=?", (paper_id,)).fetchone()
        if current is None or tuple(current) != (expected_revision, expected_generation):
            raise PublicationBlocked("parent_changed", "active CAS failed")
        target_row, reloaded = _load_revision(conn, data_dir, paper_id, revision)
        if reloaded != target or _load_revision(conn, data_dir, paper_id, expected_revision)[1] != prior:
            raise PublicationBlocked("artifact_hash_mismatch", "journal drift after readback")
        _claim_revision_vectors(conn, paper_id, revision, target)
        _claim_revision_vectors(conn, paper_id, expected_revision, prior)
        if _read_revision_projection(conn, paper_id) != prior["projection"]:
            raise PublicationBlocked("projection_drift", "active projections do not match retained parent")
        _replace_revision_fts(conn, paper_id, target["projection"], fault)
        meta = target["projection"]["metadata"]
        conn.execute("UPDATE main.papers SET title=?,abstract=?,layers=?,year=?,source_url=? WHERE arxiv_id=?",
                     (*(meta[k] for k in ("title", "abstract", "layers", "year", "source_url")), paper_id))
        fault("after_metadata")
        if _read_revision_projection(conn, paper_id) != target["projection"]:
            raise PublicationBlocked("projection_drift", "new projections failed readback")
        epoch_row = conn.execute("SELECT epoch FROM main.publication_epoch WHERE singleton=1").fetchone()
        if epoch_row is None or type(epoch_row[0]) is not int or epoch_row[0] < 0:
            raise PublicationBlocked("invalid_epoch", "publication epoch missing/invalid")
        epoch = epoch_row[0] + 1
        generation = expected_generation + 1
        events = json.loads(target_row["completed"])
        if not isinstance(events, list):
            raise PublicationBlocked("invalid_journal", "revision event list required")
        events.append({"operation": "rollback" if rollback else "activate", "from_revision": expected_revision,
                       "generation": generation, "epoch": epoch, "qdrant_readiness": receipt})
        evidence = getattr(qdrant_ready, "readback_evidence", None)
        if evidence is not None:
            events[-1]["qdrant_readback_evidence"] = json.loads(_revision_bytes(evidence))
        conn.execute("UPDATE main.paper_publications SET state='published',completed=?,error_code=NULL WHERE arxiv_id=? AND revision=?",
                     (_revision_bytes(events).decode(), paper_id, revision))
        conn.execute("UPDATE main.paper_active SET revision=?,generation=? WHERE arxiv_id=?", (revision, generation, paper_id))
        conn.execute("UPDATE main.publication_epoch SET epoch=? WHERE singleton=1", (epoch,))
        fault("before_commit")
        interval_guard = getattr(qdrant_ready, "assert_interval", None)
        if interval_guard is not None:
            interval_guard({"paper_id": paper_id, "revision": revision}, target)
        conn.commit()
        fault("after_commit")
        return {"revision": revision, "generation": generation, "epoch": epoch}
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def activate_revision(conn, data_dir, job, qdrant_ready, *, _fault=lambda _: None):
    _, artifact = _load_revision(conn, data_dir, job["paper_id"], job["revision"])
    return _commit_revision(conn, data_dir, job["paper_id"], job["revision"], artifact["parent_revision"],
                            artifact["parent_generation"], qdrant_ready, rollback=False, fault=_fault)


def rollback_revision(conn, data_dir, paper_id, *, expected_revision, expected_generation,
                      target_revision, qdrant_ready, _fault=lambda _: None):
    return _commit_revision(conn, data_dir, paper_id, target_revision, expected_revision,
                            expected_generation, qdrant_ready, rollback=True, fault=_fault)
