"""Bounded local corpus census and durable additive prose-repair orchestration."""

import argparse
import contextlib
import copy
import datetime
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import document_cache


COLLECTION = "papers_fulltext"
EMBED_URL = "http://127.0.0.1:8006/embed_batch"
VECTOR_SIZE = 384
MAX_NEW_CHUNKS = 20
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PLAN_BYTES = 256 * 1024
APPLICATION_ID = 0x44505243
SCHEMA_VERSION = 1
PARSER_SECONDS = 20
PARSER_MEMORY_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 10 * 1024 ** 3
MAX_LEDGER_BYTES = 256 * 1024 ** 2
MAX_JOURNAL_BYTES = 2 * 1024 ** 3
MAX_ATTEMPTS = 3
UNINSPECTED_STATES = ("skipped_missing_source", "skipped_missing_cache", "skipped_unsupported_id")


def completion_counts(counts):
    uninspected = sum(counts.get(state, 0) for state in UNINSPECTED_STATES)
    unresolved = uninspected + sum(counts.get(state, 0) for state in
                                  ("manual_attention", "retry_exhausted", "blocked", "applied_partial"))
    return {"unresolved_count": unresolved, "uninspected_count": uninspected,
            "safe_outside_scope_count": counts.get("skipped_outside_latex", 0)}


class Blocked(RuntimeError):
    pass


class Retryable(RuntimeError):
    pass


class DeadlineExpired(Retryable):
    pass


class ContractError(Blocked):
    pass


class Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def check(self):
        if self.remaining() <= 0:
            raise DeadlineExpired("run deadline reached")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or (os.name != "nt" and path.stat().st_mode & 0o077):
        raise Blocked("journal directory must be private (0700)")


@contextlib.contextmanager
def runner_lock(path):
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise Retryable("another corpus runner holds the ledger lock") from exc
    try:
        yield
    finally:
        os.close(fd)


def readonly_db(path):
    if not Path(path).is_file():
        raise Blocked("required database is missing")
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


class Ledger:
    def __init__(self, path, root, journal_dir, readonly=False):
        path = Path(path)
        if readonly:
            self.conn = readonly_db(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                fd = None
            if fd is not None:
                os.close(fd)
            if os.name != "nt" and path.stat().st_mode & 0o077:
                raise Blocked("ledger must be private (0600)")
            self.conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)
            self.conn.row_factory = sqlite3.Row
        try:
            app_id = self.conn.execute("PRAGMA application_id").fetchone()[0]
            tables = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id != APPLICATION_ID and (tables or readonly):
                raise Blocked("file is not a corpus-repair ledger")
            if not readonly:
                with self.conn:
                    self.conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.conn.execute("PRAGMA synchronous=FULL")
                    self.conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                    self.conn.execute("CREATE TABLE IF NOT EXISTS entries("
                                      "paper_id TEXT PRIMARY KEY,state TEXT NOT NULL,phase TEXT NOT NULL,"
                                      "plan_status TEXT,source_sha TEXT,cache_sha TEXT,additions_json TEXT,"
                                      "partial_scope INTEGER NOT NULL DEFAULT 0,unresolved_json TEXT,"
                                      "reason TEXT,journal_path TEXT,journal_expected INTEGER NOT NULL DEFAULT 0,"
                                      "attempts INTEGER NOT NULL DEFAULT 0,retry_after REAL NOT NULL DEFAULT 0,"
                                      "journal_bytes INTEGER NOT NULL DEFAULT 0,"
                                      "updated_at REAL NOT NULL)")
                    self.conn.execute("CREATE INDEX IF NOT EXISTS entries_work ON entries(phase,state,retry_after)")
                    self.conn.execute("CREATE TABLE IF NOT EXISTS universe(paper_id TEXT PRIMARY KEY,fulltext_source TEXT)")
                    for key, value in (("schema_version", str(SCHEMA_VERSION)), ("root", str(root)),
                                       ("journal_dir", str(journal_dir)), ("scan_cursor", "")):
                        self.conn.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
                if self.meta("universe_frozen") is None:
                    source_db = root / "state.db"
                    if not source_db.is_file():
                        raise Blocked("state database is missing")
                    self.conn.execute("ATTACH DATABASE ? AS corpus_snapshot",
                                      (source_db.as_uri() + "?mode=ro",))
                    try:
                        with self.conn:
                            self.conn.execute("INSERT INTO universe SELECT arxiv_id,fulltext_source "
                                              "FROM corpus_snapshot.papers WHERE status='done'")
                            count = self.conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
                            for key, value in (("universe_count", str(count)),
                                               ("universe_frozen", datetime.datetime.now(datetime.timezone.utc).isoformat())):
                                self.conn.execute("INSERT INTO meta VALUES (?,?)", (key, value))
                    finally:
                        self.conn.execute("DETACH DATABASE corpus_snapshot")
            if self.meta("schema_version") != str(SCHEMA_VERSION):
                raise Blocked("unsupported ledger version")
            if not readonly and (self.meta("root") != str(root) or self.meta("journal_dir") != str(journal_dir)):
                raise Blocked("ledger is bound to a different root or journal directory")
        except BaseException:
            self.conn.close()
            raise

    def meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def record(self, paper_id, state, phase, cursor=None, **values):
        old = self.conn.execute("SELECT attempts FROM entries WHERE paper_id=?", (paper_id,)).fetchone()
        attempts = 0 if phase == "apply" else (old[0] if old else 0) + 1
        if state == "retry" and attempts >= MAX_ATTEMPTS:
            state = "retry_exhausted"
            values["reason"] = "retry_exhausted: " + values.get("reason", "")
        fields = {"paper_id": paper_id, "state": state, "phase": phase,
                  "plan_status": values.get("plan_status"), "source_sha": values.get("source_sha"),
                  "cache_sha": values.get("cache_sha"), "additions_json": values.get("additions_json"),
                  "partial_scope": int(bool(values.get("partial_scope"))),
                  "unresolved_json": values.get("unresolved_json"),
                  "reason": values.get("reason"), "journal_path": values.get("journal_path"),
                  "updated_at": time.time(), "retry_after": time.time() + 30 if state == "retry" else 0,
                  "attempts": attempts}
        columns = list(fields)
        update = ",".join(f"{key}=excluded.{key}" for key in columns if key != "paper_id")
        with self.conn:
            self.conn.execute(f"INSERT INTO entries({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                              f"ON CONFLICT(paper_id) DO UPDATE SET {update}", list(fields.values()))
            if cursor is not None:
                self.conn.execute("UPDATE meta SET value=? WHERE key='scan_cursor'", (cursor,))

    def begin_apply(self, paper_id):
        with self.conn:
            self.conn.execute("UPDATE entries SET journal_expected=1,attempts=attempts+1,updated_at=? "
                              "WHERE paper_id=?", (time.time(), paper_id))

    def finish_apply(self, paper_id, state, reason=None):
        plan = self.conn.execute("SELECT partial_scope,unresolved_json,reason FROM entries WHERE paper_id=?",
                                 (paper_id,)).fetchone()
        if state == "applied" and plan[0]:
            state = "applied_partial"
            if reason is None:
                reason = plan[1] or plan[2]
        attempts = self.conn.execute("SELECT attempts FROM entries WHERE paper_id=?", (paper_id,)).fetchone()[0]
        journal = self.conn.execute("SELECT journal_path FROM entries WHERE paper_id=?", (paper_id,)).fetchone()[0]
        if state == "blocked" and journal and Path(journal).is_file():
            state, reason = "manual_attention", "unfinished_journal: " + (reason or "")
        if state == "retry" and attempts >= MAX_ATTEMPTS:
            state = "manual_attention" if journal and Path(journal).is_file() else "retry_exhausted"
            reason = "retry_exhausted: " + (reason or "")
        with self.conn:
            self.conn.execute("UPDATE entries SET state=?,reason=?,retry_after=?,updated_at=? WHERE paper_id=?",
                              (state, reason, time.time() + 30 if state == "retry" else 0,
                               time.time(), paper_id))

    def footprint_gate(self, path):
        if path.stat().st_size >= MAX_LEDGER_BYTES:
            raise Blocked("ledger footprint cap reached")
        if self.conn.execute("SELECT COALESCE(SUM(journal_bytes),0) FROM entries").fetchone()[0] >= MAX_JOURNAL_BYTES:
            raise Blocked("journal footprint cap reached")

    def journal_size(self, paper_id, path):
        if path.is_file():
            with self.conn:
                self.conn.execute("UPDATE entries SET journal_bytes=? WHERE paper_id=?",
                                  (path.stat().st_size, paper_id))

    def summary(self):
        counts = {row[0]: row[1] for row in self.conn.execute("SELECT state,COUNT(*) FROM entries GROUP BY state")}
        return {"schema_version": SCHEMA_VERSION, "scan_cursor": self.meta("scan_cursor"),
                "universe_count": int(self.meta("universe_count")), "universe_frozen": self.meta("universe_frozen"),
                "counts": counts, **completion_counts(counts)}


def paper_paths(root, paper_id):
    old_arxiv = bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]*/[0-9]{7}", paper_id))
    if not (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,180}", paper_id) or old_arxiv):
        raise Blocked("unsafe or unsupported paper ID")
    name = paper_id.replace(":", "_").replace("/", "_")
    source = root / "latex_cache" / name / "source.tex"
    cache = root / "chunk_cache" / (name + ".json")
    if old_arxiv:
        candidates = ((source, root / "latex_cache" / paper_id / "source.tex"),
                      (cache, root / "chunk_cache" / (paper_id + ".json")))
        selected = []
        for alternatives in candidates:
            found = [path for path in alternatives if path.is_file()]
            if len(found) > 1:
                raise Blocked("ambiguous flat and hierarchical old-arXiv cache paths")
            selected.append(found[0] if found else alternatives[0])
        source, cache = selected
    for path in (source, cache):
        relative = path.relative_to(root)
        component = root
        for part in relative.parts:
            component = component / part
            if component.is_symlink():
                raise Blocked("symlinked cache paths are refused")
        subtree = root / relative.parts[0]
        if not path.resolve().is_relative_to(subtree):
            raise Blocked("cache path escapes the corpus root")
    return source, cache


def read_bounded(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise Blocked("source/cache exceeds the per-paper byte bound")
    return data


def _planner_child(connection, kwargs):
    try:
        if sys.platform.startswith("linux"):
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (PARSER_MEMORY_BYTES, PARSER_MEMORY_BYTES))
        from ops.plan_missing_prose_repair import build_plan
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            plan = build_plan(**kwargs)
        encoded = json.dumps(plan, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_PLAN_BYTES:
            encoded = b'{"worker_error":"planner_payload_too_large"}'
        connection.send_bytes(encoded)
    except (MemoryError, TimeoutError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        connection.send_bytes(json.dumps({"worker_error": type(exc).__name__}).encode())
    finally:
        connection.close()


def bounded_plan(budget, timeout=PARSER_SECONDS, worker=_planner_child, **kwargs):
    budget.check()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(child, kwargs), daemon=True)
    process.start()
    child.close()
    wait_seconds = min(timeout, budget.remaining())
    try:
        if not parent.poll(wait_seconds):
            if wait_seconds < timeout:
                raise DeadlineExpired("run deadline reached during parse")
            raise Blocked("parser_timeout")
        try:
            output = parent.recv_bytes(MAX_PLAN_BYTES)
        except (EOFError, OSError) as exc:
            raise Blocked("parser_child_failed") from exc
        plan = json.loads(output)
        if isinstance(plan, dict) and plan.get("worker_error"):
            raise Blocked("parser_child_failed: " + plan["worker_error"])
        return plan
    finally:
        parent.close()
        process.join(timeout=0.1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        process.close()


def local_health(root, ledger_path, journal_dir):
    if hasattr(os, "getloadavg") and os.getloadavg()[0] > 0.75 * max(1, os.cpu_count() or 1):
        raise Retryable("host load gate is closed")
    if sys.platform.startswith("linux"):
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        if int(memory["MemAvailable"].split()[0]) < 256 * 1024:
            raise Retryable("available RAM gate is closed")
    for path in (root, ledger_path.parent, journal_dir):
        existing = path
        while not existing.exists():
            existing = existing.parent
        usage = shutil.disk_usage(existing)
        if usage.free < MIN_FREE_BYTES:
            raise Retryable("disk free-space gate is closed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Blocked("loopback HTTP redirects are refused")


def http_json(method, url, budget, body=None, request_deadline=math.inf, max_timeout=5):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.fragment):
        raise Blocked("HTTP endpoint must be credential-free loopback")
    budget.check()
    deadline = min(budget.deadline, request_deadline)
    if deadline <= time.monotonic():
        raise DeadlineExpired("HTTP operation time budget exhausted")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=min(max_timeout, deadline - time.monotonic())) as response:
            pieces, count = [], 0
            while True:
                budget.check()
                if time.monotonic() >= deadline:
                    raise DeadlineExpired("HTTP operation time budget exhausted")
                piece = response.read1(65536)
                if not piece:
                    break
                count += len(piece)
                if count > 16 * 1024 * 1024:
                    raise Blocked("HTTP response exceeds the per-request byte bound")
                pieces.append(piece)
    except urllib.error.HTTPError as exc:
        if exc.code >= 500 or exc.code in {408, 429}:
            raise Retryable(f"loopback service HTTP {exc.code}") from exc
        raise Blocked(f"loopback service HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise Retryable("loopback service transport failed") from exc
    try:
        return json.loads(b"".join(pieces))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Blocked("loopback service returned invalid JSON") from exc


def make_backend(engine, url, budget):
    class BoundedQdrant(engine.QdrantHTTP):
        def _request(self, method, path, body=None):
            deadlines = [value for value in (getattr(self, "_operation_deadline", None),
                                             getattr(self, "_snapshot_deadline", None)) if value is not None]
            deadline = min(deadlines, default=math.inf)
            result = http_json(method, self.base_url + path, budget, body,
                               request_deadline=deadline, max_timeout=min(5, self.timeout))
            if not isinstance(result, dict) or result.get("status") != "ok":
                raise Retryable("Qdrant returned non-ok status")
            return result
    return BoundedQdrant(url, collection=COLLECTION, timeout=5)


def qdrant_health(backend):
    data = backend._request("GET", f"/collections/{COLLECTION}")
    result = data.get("result") or {}
    if result.get("status") != "green" or result.get("optimizer_status") != "ok":
        raise Retryable("Qdrant collection/optimizer health gate is closed")
    vector_config = result.get("config", {}).get("params", {}).get("vectors", {})
    if vector_config.get("size") != VECTOR_SIZE:
        raise Blocked("Qdrant collection must use the existing unnamed 384-dimensional vector")


def embed_new(texts, budget):
    response = http_json("POST", EMBED_URL, budget, {"texts": texts}, max_timeout=20)
    vectors = response.get("vectors") if isinstance(response, dict) else None
    if (not isinstance(vectors, list) or len(vectors) != len(texts)
            or any(not isinstance(vector, list) or len(vector) != VECTOR_SIZE
                   or any(isinstance(value, bool) or not isinstance(value, (float, int))
                          or not math.isfinite(value) for value in vector) for vector in vectors)):
        raise Blocked("embedding response must contain one finite 384-vector per new prose chunk")
    return vectors


def require_done(conn, paper_id):
    row = conn.execute("SELECT status FROM papers WHERE arxiv_id=?", (paper_id,)).fetchone()
    if not row or row[0] != "done":
        raise Retryable("paper is no longer done")
    if conn.execute("SELECT 1 FROM paper_index_sync WHERE arxiv_id=? LIMIT 1", (paper_id,)).fetchone():
        raise Retryable("paper has pending index sync")


def scan_entry(root, journal_dir, paper_id, budget, planner, fulltext_source="latex"):
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", paper_id) or ".." in paper_id
            or paper_id.endswith(".") or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", paper_id)):
        return "skipped_unsupported_id", {"reason": "unsupported engine paper ID"}
    if fulltext_source != "latex":
        return "skipped_outside_latex", {"reason": "fulltext_source=" + str(fulltext_source)}
    source_path, cache_path = paper_paths(root, paper_id)
    if not source_path.is_file():
        return "skipped_missing_source", {}
    if not cache_path.is_file():
        return "skipped_missing_cache", {}
    source, cache = read_bounded(source_path), read_bounded(cache_path)
    source_hash, cache_hash = sha(source), sha(cache)
    chunks = json.loads(cache)
    plan = planner(paper_id=paper_id, source=source.decode("utf-8"), old_chunks=chunks,
                   section=None, max_new_chunks=MAX_NEW_CHUNKS,
                   source_sha256=source_hash, cache_sha256=cache_hash,
                   source_bytes=source, cache_bytes=cache)
    budget.check()
    if sha(read_bounded(source_path)) != source_hash or sha(read_bounded(cache_path)) != cache_hash:
        raise Retryable("source/cache changed during census")
    if not isinstance(plan, dict) or plan.get("status") not in {"dry_run_only", "no_change", "blocked"}:
        raise ContractError("planner returned an unsupported status")
    values = {"plan_status": plan["status"], "source_sha": source_hash, "cache_sha": cache_hash}
    if plan.get("partial_scope") or plan.get("unresolved_sections"):
        unresolved = json.dumps({"partial_scope": bool(plan.get("partial_scope")),
                                 "unresolved_sections": plan.get("unresolved_sections", [])},
                                ensure_ascii=False, sort_keys=True)
        if len(unresolved.encode("utf-8")) > MAX_PLAN_BYTES:
            raise Blocked("unresolved plan markers exceed the ledger payload bound")
        values.update(partial_scope=True, unresolved_json=unresolved, reason=unresolved)
    if plan["status"] == "dry_run_only":
        additions = plan.get("additions")
        if (not isinstance(additions, list) or not 1 <= len(additions) <= MAX_NEW_CHUNKS
                or any(not isinstance(chunk, dict) or chunk.get("element_type") != "prose"
                       or not isinstance(chunk.get("text"), str) or not chunk["text"].strip()
                       for chunk in additions)):
            raise Blocked("planner did not supply bounded new prose additions")
        manifest = plan.get("section_manifest")
        payload = additions
        if manifest is not None:
            expected_cache = json.dumps(chunks + additions, ensure_ascii=False, sort_keys=True).encode("utf-8")
            document_cache.validate_section_manifest(manifest, paper_id, expected_cache, source)
            payload = {"additions": additions, "section_manifest": manifest}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_PLAN_BYTES:
            raise Blocked("additions exceed the ledger payload bound")
        values.update(additions_json=encoded, journal_path=str(journal_dir / (sha(paper_id.encode()) + ".json")))
        return "pending", values
    values.setdefault("reason", json.dumps(plan.get("blockers", []), ensure_ascii=False)[:4096])
    return plan["status"], values


def apply_entry(entry, root, journal_dir, budget, engine, backend, embedder):
    paper_id = entry["paper_id"]
    source_path, cache_path = paper_paths(root, paper_id)
    with contextlib.closing(readonly_db(root / "state.db")) as state:
        require_done(state, paper_id)
    path = Path(entry["journal_path"])
    expected_path = journal_dir / (sha(paper_id.encode()) + ".json")
    if path != expected_path or not path.resolve().is_relative_to(journal_dir):
        raise Blocked("journal path does not match this ledger entry")
    payload = json.loads(entry["additions_json"])
    manifest = payload.get("section_manifest") if isinstance(payload, dict) else None
    additions = payload["additions"] if isinstance(payload, dict) else payload
    if path.exists():
        journal = engine._load_journal(path)
        if (journal.get("paper_id") != paper_id or journal.get("collection") != COLLECTION
                or journal.get("source_sha256") != entry["source_sha"]
                or journal.get("cache_before_sha256") != entry["cache_sha"]
                or journal.get("additions") != additions
                or journal.get("section_manifest") != manifest):
            raise Blocked("existing journal does not match its census entry")
        budget.check()
        return engine.apply_or_resume(path, source_path, cache_path, root / "fts.db", backend,
                                      state_db_path=root / "state.db", mode="resume")
    if hasattr(engine, "_allowed"):
        engine._allowed(paper_id, COLLECTION)
    source, cache = read_bounded(source_path), read_bounded(cache_path)
    if sha(source) != entry["source_sha"] or sha(cache) != entry["cache_sha"]:
        raise Blocked("source/cache hashes changed since census")
    old_points = backend.scroll(paper_id)
    if not old_points or not all(isinstance(point.get("payload"), dict) for point in old_points):
        raise Blocked("existing Qdrant snapshot lacks paper metadata")
    vectors = embedder([chunk["text"] for chunk in additions], budget)
    new_points = []
    for chunk, vector in zip(additions, vectors):
        seed = next((point for point in old_points
                     if point["payload"].get("section_title") == chunk.get("section_title")), old_points[0])
        payload = copy.deepcopy(seed["payload"])
        for key in ("part_index", "part_total", "has_math", "has_algorithm"):
            payload.pop(key, None)
        payload.update(part_index=0, part_total=1, has_math=False, has_algorithm=False)
        payload.update(chunk)
        payload["arxiv_id"] = paper_id
        new_points.append({"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{paper_id}#{chunk['chunk_index']}")),
                           "payload": payload, "vector": vector})
    budget.check()
    if sha(read_bounded(source_path)) != entry["source_sha"] or sha(read_bounded(cache_path)) != entry["cache_sha"]:
        raise Blocked("source/cache changed before journal preparation")
    with contextlib.closing(readonly_db(root / "state.db")) as state:
        require_done(state, paper_id)
    kwargs = {}
    if manifest is not None:
        _, section_path = document_cache.document_paths(root, paper_id)
        before = document_cache.read_bounded(section_path) if section_path.exists() else None
        kwargs = {"section_manifest": manifest, "section_manifest_before_bytes": before}
    engine.prepare_journal(paper_id, source, cache, additions, old_points, new_points, path,
                           collection=COLLECTION, **kwargs)
    budget.check()
    return engine.apply_or_resume(path, source_path, cache_path, root / "fts.db", backend,
                                  state_db_path=root / "state.db", mode="apply")


def validate_args(args):
    if args.mode not in {"scan", "apply", "status", "cycle", "worker"}:
        raise Blocked("unsupported mode")
    if type(args.limit) is not int or not 1 <= args.limit <= 100:
        raise Blocked("limit must be between 1 and 100 papers")
    if not math.isfinite(args.max_seconds) or not 0 < args.max_seconds <= 60:
        raise Blocked("max-seconds must be positive and at most 60")
    if not math.isfinite(args.pause) or not 0 <= args.pause <= 10:
        raise Blocked("pause must be between 0 and 10 seconds")
    if type(args.apply_limit) is not int or not 1 <= args.apply_limit <= 10:
        raise Blocked("apply-limit must be between 1 and 10 papers")
    if not math.isfinite(args.worker_hours) or not 0 < args.worker_hours <= 24:
        raise Blocked("worker-hours must be positive and at most 24")
    if not math.isfinite(args.cycle_pause) or not 1 <= args.cycle_pause <= 60:
        raise Blocked("cycle-pause must be between 1 and 60 seconds")
    root, ledger, journals = Path(args.root).resolve(), Path(args.ledger).resolve(), Path(args.journal_dir).resolve()
    if ledger in {root / "state.db", root / "fts.db"} or any(
            ledger.is_relative_to(root / folder) or journals.is_relative_to(root / folder)
            for folder in ("latex_cache", "chunk_cache")):
        raise Blocked("ledger/journals must not overwrite corpus databases or caches")
    return root, ledger, journals


def worker_loop(args, **dependencies):
    worker_deadline = time.monotonic() + args.worker_hours * 3600
    cycles, last = 0, None
    while time.monotonic() < worker_deadline:
        cycle_args = copy.copy(args)
        cycle_args.mode = "cycle"
        cycle_args.max_seconds = min(args.max_seconds, worker_deadline - time.monotonic())
        if cycle_args.max_seconds <= 0:
            break
        try:
            last = run(cycle_args, **dependencies)
        except Retryable as exc:
            last = {"mode": "cycle", "status": "retry", "reason": str(exc)}
        except Blocked as exc:
            print(json.dumps({"event": "corpus_cycle", "cycle": cycles + 1,
                              "status": "blocked", "reason": str(exc)}, sort_keys=True), flush=True)
            return {"mode": "worker", "status": "blocked", "reason": str(exc), "cycles": cycles}
        cycles += 1
        print(json.dumps({"event": "corpus_cycle", "cycle": cycles, **last}, sort_keys=True), flush=True)
        if last.get("frontier_complete") and not any(last["counts"].get(state, 0) for state in ("pending", "retry")):
            coverage = completion_counts(last["counts"])
            return {"mode": "worker", "status": "stopped",
                    "reason": "frontier_drained_with_unresolved" if coverage["unresolved_count"] else "frontier_drained",
                    **coverage,
                    "cycles": cycles, "last_cycle": last}
        time.sleep(min(args.cycle_pause, max(0, worker_deadline - time.monotonic())))
    return {"mode": "worker", "status": "stopped", "reason": "worker_time_limit",
            "cycles": cycles, "last_cycle": last}


def run(args, planner=None, engine=None, backend=None, embedder=embed_new, gate=None):
    root, ledger_path, journal_dir = validate_args(args)
    if args.mode == "worker":
        return worker_loop(args, planner=planner, engine=engine, backend=backend,
                           embedder=embedder, gate=gate)
    if args.mode == "status":
        with contextlib.closing(Ledger(ledger_path, root, journal_dir, readonly=True).conn) as conn:
            counts = {row[0]: row[1] for row in conn.execute("SELECT state,COUNT(*) FROM entries GROUP BY state")}
            cursor = conn.execute("SELECT value FROM meta WHERE key='scan_cursor'").fetchone()[0]
            meta = dict(conn.execute("SELECT key,value FROM meta"))
            return {"mode": "status", "schema_version": SCHEMA_VERSION, "counts": counts,
                    "scan_cursor": cursor, "universe_count": int(meta["universe_count"]),
                    "universe_frozen": meta["universe_frozen"], **completion_counts(counts)}
    budget = Budget(args.max_seconds)
    health = gate or (lambda: local_health(root, ledger_path, journal_dir))
    health()
    ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with runner_lock(ledger_path):
        ledger = Ledger(ledger_path, root, journal_dir)
        try:
            processed = 0
            scanned = applied = 0
            backlog = ledger.conn.execute("SELECT COUNT(*) FROM entries WHERE phase='apply' "
                                          "AND state IN ('pending','retry')").fetchone()[0]
            drain_backlog = args.mode == "cycle" and backlog >= max(args.limit, args.apply_limit * 4)
            if args.mode == "scan" or (args.mode == "cycle" and not drain_backlog):
                scan_budget = Budget(min(budget.remaining(), args.max_seconds / 2)) if args.mode == "cycle" else budget
                planner = planner or (lambda **kwargs: bounded_plan(scan_budget, **kwargs))
                with contextlib.closing(readonly_db(root / "state.db")) as state:
                    fresh = [(row[0], row[1]) for row in ledger.conn.execute(
                        "SELECT paper_id,fulltext_source FROM universe WHERE paper_id>? "
                        "ORDER BY paper_id LIMIT ?", (ledger.meta("scan_cursor"), args.limit))]
                    retry_slots = min(2, args.limit // 3) if fresh else args.limit
                    retries = [row[0] for row in ledger.conn.execute(
                        "SELECT paper_id FROM entries WHERE phase='scan' AND state='retry' AND retry_after<=? "
                        "ORDER BY paper_id LIMIT ?", (time.time(), retry_slots))]
                    fresh = fresh[:args.limit - len(retries)]
                    retry_rows = []
                    for pid in retries:
                        row = state.execute("SELECT status,fulltext_source FROM papers WHERE arxiv_id=?", (pid,)).fetchone()
                        if row and row[0] == "done":
                            retry_rows.append((pid, None, row[1]))
                        else:
                            ledger.record(pid, "retry", "scan", reason="paper is no longer done")
                work = retry_rows + [(pid, pid, kind) for pid, kind in fresh]
                for paper_id, cursor, kind in work:
                    if not scan_budget.remaining():
                        break
                    ledger.footprint_gate(ledger_path)
                    health()
                    try:
                        state_name, values = scan_entry(root, journal_dir, paper_id, scan_budget, planner, kind)
                        phase = "apply" if state_name == "pending" else "scan"
                    except ContractError:
                        raise
                    except Retryable as exc:
                        state_name, phase, values = "retry", "scan", {"reason": str(exc)}
                    except (Blocked, OSError, UnicodeError, json.JSONDecodeError) as exc:
                        state_name, phase, values = "blocked", "scan", {"reason": str(exc)}
                    ledger.record(paper_id, state_name, phase, cursor=cursor, **values)
                    processed += 1
                    scanned += 1
                    time.sleep(min(args.pause, scan_budget.remaining()))
            if args.mode in {"apply", "cycle"} and budget.remaining():
                if engine is None:
                    from ops import apply_missing_prose_repair as engine
                private_directory(journal_dir)
                if backend is None:
                    backend = make_backend(engine, args.qdrant_url, budget)
                states = "('pending','retry','manual_attention')" if args.mode == "apply" else "('pending','retry')"
                work = ledger.conn.execute(
                    f"SELECT * FROM entries WHERE phase='apply' AND state IN {states} AND retry_after<=? "
                    "ORDER BY journal_expected DESC,paper_id LIMIT ?",
                    (time.time(), args.apply_limit if args.mode == "cycle" else args.limit)).fetchall()
                for entry in work:
                    if not budget.remaining():
                        break
                    ledger.footprint_gate(ledger_path)
                    health()
                    if gate is None:
                        qdrant_health(backend)
                    ledger.begin_apply(entry["paper_id"])
                    try:
                        result = apply_entry(entry, root, journal_dir, budget, engine, backend, embedder)
                        if not isinstance(result, dict) or result.get("status") != "done":
                            raise Blocked("engine did not confirm completion")
                        ledger.finish_apply(entry["paper_id"], "applied")
                    except Retryable as exc:
                        ledger.finish_apply(entry["paper_id"], "retry", str(exc))
                    except engine.RepairBlocked as exc:
                        transient = getattr(exc, "retryable", False)
                        ledger.finish_apply(entry["paper_id"], "retry" if transient else "blocked", str(exc))
                    except (Blocked, OSError, UnicodeError, json.JSONDecodeError) as exc:
                        ledger.finish_apply(entry["paper_id"], "blocked", str(exc))
                    except sqlite3.OperationalError as exc:
                        number = getattr(exc, "sqlite_errorcode", None)
                        transient = number is not None and (number & 255) in {
                            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT}
                        state_name = "retry" if transient else "blocked"
                        ledger.finish_apply(entry["paper_id"], state_name, str(exc))
                    finally:
                        ledger.journal_size(entry["paper_id"], Path(entry["journal_path"]))
                    processed += 1
                    applied += 1
                    time.sleep(min(args.pause, budget.remaining()))
            fresh_exists = ledger.conn.execute("SELECT 1 FROM universe WHERE paper_id>? LIMIT 1",
                                               (ledger.meta("scan_cursor"),)).fetchone()
            return {"mode": args.mode, "processed": processed, "scanned": scanned,
                    "apply_attempts": applied, "frontier_complete": fresh_exists is None,
                    "drain_backlog": drain_backlog,
                    **ledger.summary()}
        finally:
            ledger.conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/opt/dtox-research", type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--journal-dir", required=True, type=Path)
    parser.add_argument("--limit", default=10, type=int)
    parser.add_argument("--max-seconds", default=60, type=float)
    parser.add_argument("--pause", default=0.5, type=float)
    parser.add_argument("--mode", choices=("scan", "apply", "status", "cycle", "worker"), default="scan")
    parser.add_argument("--apply-limit", default=2, type=int)
    parser.add_argument("--worker-hours", default=24, type=float)
    parser.add_argument("--cycle-pause", default=5, type=float)
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    args = parser.parse_args(argv)
    try:
        result = run(args)
        print(json.dumps(result, sort_keys=True), flush=True)
        if result.get("reason") == "worker_time_limit":
            return 75
        if result.get("status") == "blocked" or result.get("reason") == "frontier_drained_with_unresolved":
            return 2
        return 0
    except (Blocked, Retryable, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "blocked" if isinstance(exc, Blocked) else "retry",
                          "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
