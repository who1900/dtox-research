"""Crash-resumable, additions-only prose repair from authoritative cached snapshots."""

import argparse
import base64
import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import document_cache


MAX_NEW_CHUNKS = 20
COLLECTION_ALLOWLIST = {"papers_fulltext"}
MAX_SNAPSHOT_POINTS = 10000
SCROLL_PAGE_SIZE = 200
MAX_SNAPSHOT_PAGES = 100
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
SNAPSHOT_SECONDS = 60
REPAIR_SECONDS = 30
SECTION_TYPES = {"abstract", "introduction", "related_work", "method", "experiments",
                 "analysis", "limitations", "conclusion", "appendix", "other"}
VECTOR_SIZE = 384
JOURNAL_VERSION = 2


class RepairBlocked(RuntimeError):
    def __init__(self, message, code="repair_invalid", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def sqlite_failure(exc, namespace="fts"):
    number = getattr(exc, "sqlite_errorcode", 0) & 255
    retryable = number in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT}
    return RepairBlocked(str(exc), code=namespace + ("_busy" if retryable else "_database_error"), retryable=retryable)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _point_id(paper_id, index):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{paper_id}#{index}"))


def _allowed(paper_id, collection):
    if (not isinstance(paper_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", paper_id)
            or ".." in paper_id or paper_id.endswith(".")
            or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", paper_id)):
        raise RepairBlocked("paper ID must be a safe ASCII identifier without paths or traversal")
    if collection not in COLLECTION_ALLOWLIST:
        raise RepairBlocked("Qdrant collection is not allowlisted")


def validate_loopback_url(url):
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in
            {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise RepairBlocked("URL must be a credential-free loopback origin")
    return url.rstrip("/")


def _vector_values(vector):
    values = []
    def visit(value):
        if isinstance(value, dict):
            for key in sorted(value):
                visit(value[key])
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            values.append(float(value))
        else:
            raise RepairBlocked("vector contains a non-finite or non-numeric value")
    visit(vector)
    if len(values) != VECTOR_SIZE:
        raise RepairBlocked(f"expected {VECTOR_SIZE}-dimensional vectors")
    return values


def _same_vector(left, right):
    try:
        a, b = _vector_values(left), _vector_values(right)
    except RepairBlocked:
        return False
    return all(abs(x - y) <= 1e-6 for x, y in zip(a, b))


def _same_point(left, right):
    return (str(left.get("id")) == str(right.get("id"))
            and left.get("payload") == right.get("payload")
            and _same_vector(left.get("vector"), right.get("vector")))


def _text_key(text):
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def _validate_chunks(paper_id, old_chunks, additions, old_points, new_points):
    if not isinstance(old_chunks, list) or not old_chunks or not all(isinstance(c, dict) for c in old_chunks):
        raise RepairBlocked("old cache must contain chunk objects")
    indices = [c.get("chunk_index") for c in old_chunks]
    if any(type(i) is not int or i < 0 for i in indices) or len(indices) != len(set(indices)):
        raise RepairBlocked("old cache chunk indices are invalid or duplicated")
    old_by_id = _validate_old_points(paper_id, old_chunks, old_points)
    if not isinstance(additions, list) or not 1 <= len(additions) <= MAX_NEW_CHUNKS:
        raise RepairBlocked(f"new chunk count must be between 1 and {MAX_NEW_CHUNKS}")
    if len(old_points) + len(additions) > MAX_SNAPSHOT_POINTS:
        raise RepairBlocked("old and added points exceed the complete snapshot bound")
    start = max(indices, default=-1) + 1
    expected_new = {}
    text_keys = {_text_key(c["text"]) for c in old_chunks if isinstance(c.get("text"), str)}
    for offset, chunk in enumerate(additions):
        index = start + offset
        if (not isinstance(chunk, dict) or type(chunk.get("chunk_index")) is not int
                or chunk["chunk_index"] != index or chunk.get("element_type") != "prose"
                or not isinstance(chunk.get("section_title"), str)
                or not 1 <= len(chunk["section_title"].strip()) <= 512
                or any(ord(c) < 32 or ord(c) == 127 for c in chunk["section_title"])
                or not isinstance(chunk.get("section_type"), str)
                or chunk["section_type"] not in SECTION_TYPES
                or not isinstance(chunk.get("text"), str) or not chunk["text"].strip()
                or "\x00" in chunk["text"]
                or any(key in chunk and type(chunk[key]) is not bool for key in ("has_math", "has_algorithm"))
                or chunk.get("has_algorithm", False)):
            raise RepairBlocked("additions must be sequential, non-empty prose chunks after the old cache")
        if "arxiv_id" in chunk and chunk["arxiv_id"] != paper_id:
            raise RepairBlocked("added cache chunk belongs to another paper")
        text_key = _text_key(chunk["text"])
        if text_key in text_keys:
            raise RepairBlocked("added prose duplicates existing or other added chunk text")
        text_keys.add(text_key)
        expected_new[_point_id(paper_id, index)] = chunk
    new_by_id = _validate_points(paper_id, expected_new, new_points)
    if len(_json_bytes(old_points + new_points)) > MAX_SNAPSHOT_BYTES:
        raise RepairBlocked("complete Qdrant snapshot exceeds its byte bound")
    if set(old_by_id) & set(new_by_id):
        raise RepairBlocked("new point UUID collides with an old point")
    old_dimensions = {_vector_values(p["vector"]).__len__() for p in old_by_id.values()}
    new_dimensions = {_vector_values(p["vector"]).__len__() for p in new_by_id.values()}
    if old_dimensions != {VECTOR_SIZE} or new_dimensions != {VECTOR_SIZE}:
        raise RepairBlocked("old and new point vectors must use the existing 384-dimension model")
    return old_by_id, new_by_id


def _validate_old_points(paper_id, chunks, points):
    by_id = {_point_id(paper_id, c["chunk_index"]): c for c in chunks}
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_SNAPSHOT_POINTS:
        raise RepairBlocked("observed old Qdrant snapshot must contain 1..10000 points")
    if len(_json_bytes(points)) > MAX_SNAPSHOT_BYTES:
        raise RepairBlocked("observed old Qdrant snapshot exceeds its byte bound")
    found = {}
    for point in points:
        if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
            raise RepairBlocked("malformed old Qdrant point")
        point_id = str(point.get("id"))
        chunk = by_id.get(point_id)
        if chunk is None or point_id in found:
            raise RepairBlocked("old Qdrant snapshot has an unknown or duplicate UUID")
        payload = point["payload"]
        if (payload.get("arxiv_id") != paper_id or type(payload.get("chunk_index")) is not int
                or payload.get("chunk_index") != chunk["chunk_index"]
                or payload.get("text") != chunk.get("text")
                or payload.get("section_type") != chunk.get("section_type")
                or payload.get("element_type", "prose") != chunk.get("element_type", "prose")):
            raise RepairBlocked("old Qdrant point does not match its authoritative cache chunk")
        _vector_values(point.get("vector"))
        found[point_id] = point
    structured = {_point_id(paper_id, c["chunk_index"]) for c in chunks
                  if c.get("element_type") in {"equation", "algorithm", "code"}
                  or c.get("has_algorithm") is True}
    if not structured.issubset(found):
        raise RepairBlocked("one or more cached equation/algorithm/code points are absent from Qdrant snapshot")
    return found


def _validate_points(paper_id, expected_chunks, points):
    if not isinstance(points, list) or len(points) != len(expected_chunks):
        raise RepairBlocked("Qdrant point snapshot is incomplete or has unexpected points")
    found = {}
    for point in points:
        if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
            raise RepairBlocked("malformed Qdrant point")
        point_id = str(point.get("id"))
        if point_id in found or point_id not in expected_chunks:
            raise RepairBlocked("duplicate or unexpected Qdrant point UUID")
        chunk = expected_chunks[point_id]
        payload = point["payload"]
        if (payload.get("arxiv_id") != paper_id or type(payload.get("chunk_index")) is not int
                or payload.get("chunk_index") != chunk["chunk_index"]
                or payload.get("text") != chunk.get("text")
                or payload.get("section_type") != chunk.get("section_type")
                or payload.get("element_type", "prose") != chunk.get("element_type", "prose")
                or ("section_title" in payload and payload["section_title"] != chunk["section_title"])):
            raise RepairBlocked("Qdrant payload does not match its cache chunk")
        _vector_values(point.get("vector"))
        found[point_id] = point
    if set(found) != set(expected_chunks):
        raise RepairBlocked("Qdrant snapshot does not cover expected cache chunk UUIDs")
    return found


def _new_cache_bytes(old_cache_bytes, additions):
    old_chunks = json.loads(old_cache_bytes)
    return json.dumps(old_chunks + additions, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _immutable_digest(journal):
    fields = {key: value for key, value in journal.items()
              if key not in {"digest", "stage", "fts_row_ids"}}
    return _sha(_json_bytes(fields))


def prepare_journal(paper_id, source_bytes, old_cache_bytes, additions, old_points, new_points,
                    journal_path, collection="papers_fulltext", write=True,
                    section_manifest=None, section_manifest_before_bytes=None):
    _allowed(paper_id, collection)
    try:
        old_chunks = json.loads(old_cache_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairBlocked("old cache is not valid UTF-8 JSON") from exc
    old_by_id, new_by_id = _validate_chunks(paper_id, old_chunks, additions, old_points, new_points)
    new_cache = _new_cache_bytes(old_cache_bytes, additions)
    repair_id = hashlib.sha256((paper_id + _sha(source_bytes) + _sha(old_cache_bytes)).encode()).hexdigest()
    journal = {
        "version": JOURNAL_VERSION if section_manifest is not None else 1,
        "repair_id": repair_id, "paper_id": paper_id,
        "collection": collection, "source_sha256": _sha(source_bytes),
        "cache_before_sha256": _sha(old_cache_bytes), "cache_after_sha256": _sha(new_cache),
        "old_cache_backup_b64": base64.b64encode(old_cache_bytes).decode("ascii"),
        "old_points": list(old_by_id.values()), "new_points": list(new_by_id.values()),
        "additions": additions, "stage": "prepared",
    }
    if section_manifest is not None:
        try:
            document_cache.validate_section_manifest(section_manifest, paper_id, new_cache, source_bytes)
        except (ValueError, TypeError) as exc:
            raise RepairBlocked("invalid canonical section manifest", "manifest_invalid") from exc
        journal["section_manifest"] = json.loads(document_cache.manifest_bytes(section_manifest))
        journal["section_manifest_before_sha256"] = (
            _sha(section_manifest_before_bytes) if section_manifest_before_bytes is not None else None)
    journal["digest"] = _immutable_digest(journal)
    if write:
        _write_new_journal(Path(journal_path), journal)
    return journal


def _fsync_dir(path):
    if os.name != "nt":
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_json(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise RepairBlocked("journal already exists")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive and path.exists():
            raise RepairBlocked("journal already exists")
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_new_journal(path, journal):
    path.parent.mkdir(parents=True, exist_ok=True)
    with journal_lock(path):
        _atomic_json(path, journal, exclusive=True)


@contextlib.contextmanager
def journal_lock(path):
    lock_path = str(path) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_journal(path):
    raw = document_cache.read_bounded(path, MAX_SNAPSHOT_BYTES * 2)
    if os.name != "nt" and stat.S_IMODE(Path(path).stat().st_mode) != 0o600:
        raise RepairBlocked("journal permissions must be 0600")
    journal = json.loads(raw)
    if journal.get("version") not in {1, JOURNAL_VERSION} or journal.get("digest") != _immutable_digest(journal):
        raise RepairBlocked("journal version or immutable digest mismatch")
    if journal.get("stage") not in {"prepared", "qdrant_applied", "cache_applied", "fts_applied", "done"}:
        raise RepairBlocked("unknown journal stage", "journal_invalid")
    return journal


@contextlib.contextmanager
def _state_guard(state_path, paper_id):
    path = Path(state_path)
    if not path.is_file():
        raise RepairBlocked("state database does not exist; refusing to create one")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=1)
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"papers", "paper_index_sync"}.issubset(tables):
            raise RepairBlocked("state database lacks required paper/sync tables")
        paper = conn.execute("SELECT status FROM papers WHERE arxiv_id=?", (paper_id,)).fetchone()
        if not paper or paper[0] != "done":
            raise RepairBlocked("paper must remain done before repair", "paper_not_done", True)
        if conn.execute("SELECT 1 FROM paper_index_sync WHERE arxiv_id=? LIMIT 1", (paper_id,)).fetchone():
            raise RepairBlocked("paper has pending index sync", "index_sync_pending", True)
        yield
    except sqlite3.Error as exc:
        raise sqlite_failure(exc, "state") from exc
    finally:
        conn.rollback()
        conn.close()


class QdrantHTTP:
    def __init__(self, base_url, collection="papers_fulltext", timeout=5):
        self.base_url = validate_loopback_url(base_url)
        if collection not in COLLECTION_ALLOWLIST:
            raise RepairBlocked("Qdrant collection is not allowlisted")
        self.collection = collection
        self.timeout = timeout

    def _request(self, method, path, body=None):
        data = None if body is None else _json_bytes(body)
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            deadlines = [d for d in (getattr(self, "_snapshot_deadline", None),
                                     getattr(self, "_operation_deadline", None)) if d is not None]
            timeout = self.timeout if not deadlines else min(self.timeout, min(deadlines) - time.monotonic())
            if timeout <= 0:
                raise RepairBlocked("Qdrant snapshot exceeded its time budget", "backend_timeout", True)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RepairBlocked("Qdrant response exceeded its byte bound")
                self._last_response_bytes = len(raw)
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict) or result.get("status") != "ok":
                    raise RepairBlocked("Qdrant returned a non-ok status")
                return result
        except urllib.error.HTTPError as exc:
            raise RepairBlocked(f"Qdrant HTTP {exc.code}", "backend_http",
                                exc.code >= 500 or exc.code in {408, 429}) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RepairBlocked(f"Qdrant request failed: {type(exc).__name__}", "backend_transport", True) from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RepairBlocked("Qdrant returned invalid JSON", "backend_invalid_response") from exc

    def scroll(self, paper_id):
        _allowed(paper_id, self.collection)
        points, seen_ids, seen_offsets = [], set(), set()
        offset, total_bytes = None, 0
        self._snapshot_deadline = min(time.monotonic() + SNAPSHOT_SECONDS,
                                      getattr(self, "_operation_deadline", math.inf))
        try:
            for _ in range(MAX_SNAPSHOT_PAGES):
                if time.monotonic() >= self._snapshot_deadline:
                    raise RepairBlocked("Qdrant snapshot exceeded its time budget", "backend_timeout", True)
                body = {
                    "filter": {"must": [{"key": "arxiv_id", "match": {"value": paper_id}}]},
                    "limit": min(SCROLL_PAGE_SIZE, MAX_SNAPSHOT_POINTS - len(points)),
                    "with_payload": True, "with_vector": True,
                }
                if offset is not None:
                    body["offset"] = offset
                self._last_response_bytes = 0
                response = self._request("POST", f"/collections/{self.collection}/points/scroll", body)
                total_bytes += self._last_response_bytes or len(_json_bytes(response))
                if total_bytes > MAX_SNAPSHOT_BYTES or time.monotonic() >= self._snapshot_deadline:
                    raise RepairBlocked("Qdrant snapshot exceeded its byte or time budget", "backend_budget", True)
                result = response.get("result", {})
                page = result.get("points") if isinstance(result, dict) else None
                if not isinstance(page, list) or len(page) > body["limit"]:
                    raise RepairBlocked("Qdrant scroll page is malformed or exceeds its point bound")
                for point in page:
                    if (not isinstance(point, dict) or not isinstance(point.get("payload"), dict)
                            or point["payload"].get("arxiv_id") != paper_id):
                        raise RepairBlocked("Qdrant scroll returned a malformed or foreign paper point")
                    point_id = str(point.get("id"))
                    try:
                        uuid.UUID(point_id)
                    except ValueError as exc:
                        raise RepairBlocked("Qdrant paper snapshot contains an invalid UUID") from exc
                    if point_id in seen_ids:
                        raise RepairBlocked("Qdrant scroll returned duplicate point IDs")
                    _vector_values(point.get("vector"))
                    seen_ids.add(point_id)
                points.extend(page)
                offset = result.get("next_page_offset")
                if offset is None:
                    return points
                if not page or len(points) >= MAX_SNAPSHOT_POINTS:
                    raise RepairBlocked("Qdrant snapshot is incomplete or exceeds 10000 points")
                if type(offset) is int:
                    valid_offset = offset >= 0
                elif isinstance(offset, str):
                    try:
                        valid_offset = str(uuid.UUID(offset)) == offset
                    except ValueError:
                        valid_offset = False
                else:
                    valid_offset = False
                if not valid_offset or offset in seen_offsets:
                    raise RepairBlocked("Qdrant scroll cursor is invalid or repeats")
                seen_offsets.add(offset)
            raise RepairBlocked("Qdrant snapshot exceeded its page bound")
        finally:
            del self._snapshot_deadline

    def retrieve(self, point_ids):
        result = self._request("POST", f"/collections/{self.collection}/points", {
            "ids": point_ids, "with_payload": True, "with_vector": True,
        }).get("result")
        if not isinstance(result, list):
            raise RepairBlocked("Qdrant retrieve response is malformed")
        return result

    def upsert_additions(self, points):
        response = self._request("PUT", f"/collections/{self.collection}/points?wait=true", {"points": points})
        if response.get("result", {}).get("status") != "completed":
            raise RepairBlocked("Qdrant did not complete additive upsert")


def _check_journal_content(journal, source_path, cache_path):
    _allowed(journal.get("paper_id"), journal.get("collection"))
    source_bytes = document_cache.read_bounded(source_path)
    if _sha(source_bytes) != journal["source_sha256"]:
        raise RepairBlocked("source hash changed since prepare")
    old_cache = base64.b64decode(journal["old_cache_backup_b64"], validate=True)
    if _sha(old_cache) != journal["cache_before_sha256"]:
        raise RepairBlocked("journal old-cache backup hash mismatch")
    _validate_chunks(journal["paper_id"], json.loads(old_cache), journal["additions"],
                     journal["old_points"], journal["new_points"])
    expected_new = _new_cache_bytes(old_cache, journal["additions"])
    if _sha(expected_new) != journal["cache_after_sha256"]:
        raise RepairBlocked("journal new-cache hash mismatch")
    if journal["version"] == 2:
        try:
            document_cache.validate_section_manifest(journal["section_manifest"], journal["paper_id"],
                                                     expected_new, source_bytes)
        except (ValueError, TypeError, KeyError) as exc:
            raise RepairBlocked("journal manifest hash/contract mismatch", "manifest_invalid") from exc
    current_hash = _check_paths(journal, source_path, cache_path)
    return old_cache, expected_new, current_hash


def _check_paths(journal, source_path, cache_path):
    if _sha(document_cache.read_bounded(source_path)) != journal["source_sha256"]:
        raise RepairBlocked("source hash changed since prepare")
    current_hash = _sha(document_cache.read_bounded(cache_path))
    if current_hash not in {journal["cache_before_sha256"], journal["cache_after_sha256"]}:
        raise RepairBlocked("cache is neither the recorded old nor new snapshot")
    return current_hash


def _verify_live_qdrant(journal, backend, allow_intended_new):
    if getattr(backend, "collection", None) != journal["collection"]:
        raise RepairBlocked("Qdrant backend collection does not match journal")
    paper_id = journal["paper_id"]
    old = {str(p["id"]): p for p in journal["old_points"]}
    new = {str(p["id"]): p for p in journal["new_points"]}
    live = backend.scroll(paper_id)
    if not isinstance(live, list) or not all(isinstance(p, dict) for p in live):
        raise RepairBlocked("Qdrant paper snapshot is malformed")
    live_by_id = {str(p.get("id")): p for p in live}
    if (len(live) > MAX_SNAPSHOT_POINTS or len(_json_bytes(live)) > MAX_SNAPSHOT_BYTES
            or len(live_by_id) != len(live) or set(live_by_id) - (set(old) | set(new))):
        raise RepairBlocked("Qdrant paper state contains duplicate or unknown points")
    for point_id, baseline in old.items():
        if point_id not in live_by_id or not _same_point(live_by_id[point_id], baseline):
            raise RepairBlocked("old Qdrant point changed or disappeared")
    retrieved_points = backend.retrieve(list(new))
    if not isinstance(retrieved_points, list) or not all(isinstance(p, dict) for p in retrieved_points):
        raise RepairBlocked("Qdrant retrieve response is malformed")
    retrieved = {str(p.get("id")): p for p in retrieved_points}
    if len(retrieved) != len(retrieved_points) or set(retrieved) - set(new):
        raise RepairBlocked("Qdrant retrieve returned duplicate IDs")
    present = set()
    for point_id, intended in new.items():
        actual = retrieved.get(point_id)
        if actual is not None:
            if not allow_intended_new or not _same_point(actual, intended):
                raise RepairBlocked("new Qdrant UUID already exists with unknown or unapproved state")
            if point_id not in live_by_id:
                raise RepairBlocked("Qdrant scroll/retrieve disagree for a new point")
            present.add(point_id)
        if point_id in live_by_id and (actual is None or not _same_point(live_by_id[point_id], intended)):
            raise RepairBlocked("Qdrant scroll/retrieve disagree for a new point")
    return [point for point_id, point in new.items() if point_id not in present]


def _atomic_cache_replace(cache_path, expected_bytes):
    path = Path(cache_path)
    current_mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, current_mode)
        else:
            os.chmod(temp_name, current_mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(expected_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _fts_expected_row(point):
    payload = point["payload"]
    return (payload["text"], str(point["id"]), payload["arxiv_id"],
            payload.get("section_type"), payload.get("element_type", "prose"),
            str(payload.get("layers") or ""),
            None if payload.get("year") is None else str(payload["year"]))


def _receipt_rows(conn, journal):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='dtox_prose_repair_receipts' AND type='table'").fetchone():
        return []
    columns = list(conn.execute("PRAGMA table_info(dtox_prose_repair_receipts)"))
    if ({row[1] for row in columns} != {"repair_id", "digest", "row_ids_json"}
            or [row[1] for row in columns if row[5]] != ["repair_id"]):
        raise RepairBlocked("FTS receipt schema lacks an exact primary key", "fts_receipt_invalid")
    row = conn.execute("SELECT digest,row_ids_json FROM dtox_prose_repair_receipts WHERE repair_id=?",
                       (journal["repair_id"],)).fetchone()
    if not row:
        return []
    if row[0] != journal["digest"]:
        raise RepairBlocked("FTS receipt digest mismatch", "fts_receipt_invalid")
    if not isinstance(row[1], str) or len(row[1]) > 65536:
        raise RepairBlocked("FTS receipt exceeds its bound", "fts_receipt_invalid")
    try:
        records = json.loads(row[1])
    except (TypeError, ValueError) as exc:
        raise RepairBlocked("FTS receipt JSON is invalid", "fts_receipt_invalid") from exc
    _validate_receipt_rows(conn, journal, records)
    return records


def _validate_receipt_rows(conn, journal, row_ids):
    points = {str(p["id"]): p for p in journal["new_points"] if p["payload"].get("text")}
    if not isinstance(row_ids, list) or len(row_ids) != len(points):
        raise RepairBlocked("FTS receipt row IDs do not cover new indexed points", "fts_receipt_invalid")
    seen = set()
    seen_points = set()
    expected = {_fts_expected_row(p)[1]: _fts_expected_row(p) for p in points.values()}
    for record in row_ids:
        if not isinstance(record, dict) or type(record.get("row_id")) is not int:
            raise RepairBlocked("FTS receipt row ID record is malformed", "fts_receipt_invalid")
        row_id = record["row_id"]
        if row_id in seen:
            raise RepairBlocked("FTS receipt contains duplicate row IDs", "fts_receipt_invalid")
        seen.add(row_id)
        row = conn.execute("SELECT rowid,text,point_id,arxiv_id,section_type,element_type,layers,year "
                           "FROM chunks WHERE rowid=?", (row_id,)).fetchone()
        point_id = record.get("point_id")
        if (row is None or point_id not in expected or point_id in seen_points
                or tuple(row[1:]) != expected[point_id]
                or record.get("text_sha256") != _sha(str(row[1]).encode("utf-8"))):
            raise RepairBlocked("FTS receipt does not match its indexed row", "fts_receipt_invalid")
        seen_points.add(point_id)
    if seen_points != set(expected):
        raise RepairBlocked("FTS receipt is incomplete", "fts_receipt_invalid")


@contextlib.contextmanager
def _fts_preflight(fts_path, journal, deadline=None):
    path = Path(fts_path)
    if not path.is_file():
        raise RepairBlocked("FTS database does not exist; refusing to create one")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("BEGIN")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        required = {"text", "point_id", "arxiv_id", "section_type", "element_type", "layers", "year"}
        if not required.issubset(columns):
            raise RepairBlocked("FTS chunks schema is incomplete")
        receipt = _receipt_rows(conn, journal)
        if not receipt:
            raise RepairBlocked("no prior FTS receipt; certified identity migration contract is unavailable",
                                "fts_identity_unknown")
        conn.rollback()
        yield {"digest": journal["digest"], "fts_path": path.resolve(),
               "deadline": deadline, "receipt": receipt}
    except sqlite3.Error as exc:
        raise sqlite_failure(exc) from exc
    finally:
        conn.close()


def _fts_additions(fts_path, journal, preflight=None):
    """Receipt-only resume. New FTS writes require a separate certified writer contract."""
    if preflight is None:
        with _fts_preflight(fts_path, journal) as verified:
            return _fts_additions(fts_path, journal, verified)
    path = Path(fts_path)
    if not path.is_file():
        raise RepairBlocked("FTS database does not exist; refusing to create one")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("BEGIN")
        _verify_preflight(fts_path, journal, preflight, conn)
        return _receipt_rows(conn, journal)
    except sqlite3.Error as exc:
        raise sqlite_failure(exc) from exc
    finally:
        conn.close()


def _verify_preflight(fts_path, journal, preflight, conn=None):
    if preflight["digest"] != journal["digest"] or preflight["fts_path"] != Path(fts_path).resolve():
        raise RepairBlocked("FTS preflight belongs to another repair", "fts_preflight_invalid")
    if conn is None:
        with contextlib.closing(sqlite3.connect(Path(fts_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as current:
            return _verify_preflight(fts_path, journal, preflight, current)
    if _receipt_rows(conn, journal) != preflight["receipt"]:
        raise RepairBlocked("repair FTS receipt changed after preflight", "fts_revision_conflict")


def _require_time(deadline):
    if time.monotonic() >= deadline:
        raise RepairBlocked("repair operation exceeded its time budget; resume after resolving the delay", "repair_timeout", True)


@contextlib.contextmanager
def _backend_budget(backend, deadline):
    previous = getattr(backend, "_operation_deadline", None)
    backend._operation_deadline = deadline
    try:
        yield
    finally:
        if previous is None:
            del backend._operation_deadline
        else:
            backend._operation_deadline = previous


def apply_or_resume(journal_path, source_path, cache_path, fts_path, backend, state_db_path,
                    mode="apply", fault_hook=None):
    if mode not in {"apply", "resume"}:
        raise RepairBlocked("mode must be apply or resume")
    with journal_lock(Path(journal_path)):
        journal = _load_journal(journal_path)
        _allowed(journal.get("paper_id"), journal.get("collection"))
        if journal["version"] != 1 or journal["stage"] != "done":
            raise RepairBlocked("repair mutation/publication requires single-owner exact FTS mapping migration",
                                "fts_identity_unknown")
        if mode != "resume":
            raise RepairBlocked("done legacy journals support read-only resume validation only",
                                "journal_requires_resume")
        deadline = time.monotonic() + REPAIR_SECONDS
        _, _, current_hash = _check_journal_content(journal, source_path, cache_path)
        if current_hash != journal["cache_after_sha256"]:
            raise RepairBlocked("done journal cache is not its recorded completed snapshot", "cache_revision_conflict")
        with _state_guard(state_db_path, journal["paper_id"]):
            _check_paths(journal, source_path, cache_path)
        with _fts_preflight(fts_path, journal, deadline) as preflight, _backend_budget(backend, deadline):
            _require_time(deadline)
            if _verify_live_qdrant(journal, backend, allow_intended_new=True):
                raise RepairBlocked("done journal Qdrant additions are missing; validation cannot recreate them",
                                    "qdrant_snapshot_conflict")
            with _state_guard(state_db_path, journal["paper_id"]):
                _check_paths(journal, source_path, cache_path)
                _verify_preflight(fts_path, journal, preflight)
            _require_time(deadline)
            return {"status": "done", "repair_id": journal["repair_id"], "fts_rows": preflight["receipt"],
                    "verification_scope": "recorded_cache_qdrant_and_new_fts_receipt",
                    "legacy_fts_baseline_verified": False, "read_only": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Additive, crash-resumable prose repair from supplied plans")
    parser.add_argument("--mode", choices=("prepare", "dry-run", "apply", "resume"), default="prepare")
    parser.add_argument("--paper-id")
    parser.add_argument("--collection", default="papers_fulltext")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--old-points", type=Path)
    parser.add_argument("--new-points", type=Path)
    parser.add_argument("--fts-db", type=Path)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--qdrant-url")
    args = parser.parse_args(argv)
    try:
        if args.mode in {"prepare", "dry-run"}:
            _allowed(args.paper_id, args.collection)
            if not args.old_points or not args.new_points:
                raise RepairBlocked("prepare requires supplied old/new point snapshots; no network call is made")
            new_input = json.loads(args.new_points.read_text(encoding="utf-8"))
            journal = prepare_journal(args.paper_id, args.source.read_bytes(), args.cache.read_bytes(),
                                      new_input['additions'],
                                      json.loads(args.old_points.read_text(encoding="utf-8")),
                                      new_input['points'], args.journal, args.collection,
                                      write=args.mode == "prepare")
            print(json.dumps({"status": "prepared" if args.mode == "prepare" else "dry_run",
                              "repair_id": journal["repair_id"], "digest": journal["digest"],
                              "stage": journal["stage"], "journal": str(args.journal)}, indent=2))
            return 0
        if not args.fts_db or not args.state_db or not args.qdrant_url:
            raise RepairBlocked("apply/resume requires state/FTS database paths and loopback Qdrant URL")
        if args.paper_id is not None and _load_journal(args.journal).get("paper_id") != args.paper_id:
            raise RepairBlocked("CLI paper ID does not match the journal")
        backend = QdrantHTTP(args.qdrant_url, args.collection)
        result = apply_or_resume(args.journal, args.source, args.cache, args.fts_db, backend,
                                 args.state_db, args.mode)
        print(json.dumps(result, indent=2))
        return 0
    except (RepairBlocked, OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc),
                          "code": getattr(exc, "code", "repair_invalid"),
                          "retryable": getattr(exc, "retryable", False)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
