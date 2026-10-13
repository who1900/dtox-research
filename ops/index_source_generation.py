#!/usr/bin/env python3
"""Explicit offline plan/index of a derived catalog. Default mode is read-only.

Owned passages remain in their existing native 384 index, read-only at query time
after exact whole-text/owner-revision validation. They are NEVER copied/embedded.
Only summaries/citation contexts enter the new 768 collection. All their text is
covered by tokenizer-verified UTF-8 views. Locators remain in the owner's raw
artifact; view offsets address representation text, not that raw artifact.
"""
import argparse
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import source_representations as catalog
from api.source_generation import MODEL_ID, DIMENSION, MAX_LENGTH, QUERY_INSTRUCTION, collection_name


VIEW_BYTES = 4096
PAYLOAD_BYTES = 32768
DEFAULT_MAX_POINTS = 100000
DEFAULT_MAX_BYTES = 2 * 1024**3
OWNED_AFTER_CREATE = "created_verified_v1"


def text_views(text, max_bytes=VIEW_BYTES, tokenizer=None):
    if type(max_bytes) is not int or not 16 <= max_bytes <= VIEW_BYTES:
        raise ValueError("unsafe view size")
    if tokenizer is None:
        raise ValueError("tokenizer required for exact token-bounded views")
    start, size = 0, 0
    for index, char in enumerate(text):
        length = len(char.encode("utf-8"))
        if size + length > max_bytes:
            yield from _token_windows(text, start, index, tokenizer)
            start, size = index, 0
        size += length
    if start < len(text):
        yield from _token_windows(text, start, len(text), tokenizer)


def _token_windows(text, start, end, tokenizer):
    while start < end:
        stop = end
        if tokenizer is not None and len(tokenizer.encode(text[start:stop]).ids) > MAX_LENGTH:
            low, high = start + 1, stop
            while low < high:
                middle = (low + high + 1) // 2
                if len(tokenizer.encode(text[start:middle]).ids) <= MAX_LENGTH:
                    low = middle
                else:
                    high = middle - 1
            stop = low
            if len(tokenizer.encode(text[start:stop]).ids) > MAX_LENGTH:
                raise ValueError("tokenizer cannot encode one character within max length")
        yield start, stop, text[start:stop]
        start = stop


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def embedding_descriptor(model_sha256, tokenizer_sha256):
    return {"model_sha256": catalog._sha(model_sha256), "tokenizer_sha256": catalog._sha(tokenizer_sha256),
            "query_instruction": QUERY_INSTRUCTION, "dimension": DIMENSION,
            "max_length": MAX_LENGTH, "pooling": "CLS/L2"}


def load_tokenizer(path):
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(Path(path).resolve(strict=True)))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def offline_assets(model_dir, model_file="onnx/model_quantized.onnx"):
    root = Path(model_dir).resolve(strict=True)
    if Path(model_file).is_absolute() or ".." in Path(model_file).parts:
        raise ValueError("model-file must be relative to model-dir")
    model = (root / model_file).resolve(strict=True)
    tokenizer = (root / "tokenizer.json").resolve(strict=True)
    # HF snapshot symlinks may resolve to sibling cached blobs. Read-only, no fetch.
    descriptor = embedding_descriptor(_file_sha(model), _file_sha(tokenizer))
    return model, tokenizer, descriptor


def _offline_worker(pipe, model_path, tokenizer_path):
    try:
        # Dedicated low-priority process, one CPU, <=4 GiB address space on Linux.
        for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ[name] = "1"
        os.environ["RAYON_NUM_THREADS"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        if os.name == "posix":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
            os.nice(10)
        elif os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        import numpy as np
        import onnxruntime as ort
        tokenizer = load_tokenizer(tokenizer_path)
        tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        names = {item.name for item in session.get_inputs()}
        descriptor = embedding_descriptor(_file_sha(model_path), _file_sha(tokenizer_path))
        if session.get_outputs()[0].shape[-1] != DIMENSION:
            raise ValueError("offline model output dimension mismatch")
        pipe.send(("ready", descriptor))
        while True:
            texts = pipe.recv()
            if texts is None:
                break
            encoded = tokenizer.encode_batch(texts)
            if any(len(item.ids) > MAX_LENGTH for item in encoded):
                raise ValueError("silent tokenizer truncation forbidden")
            feed = {"input_ids": np.array([item.ids for item in encoded], dtype=np.int64),
                    "attention_mask": np.array([item.attention_mask for item in encoded], dtype=np.int64)}
            if "token_type_ids" in names:
                feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
            cls = session.run(None, feed)[0][:, 0]
            norms = np.linalg.norm(cls, axis=1, keepdims=True)
            if not np.all(np.isfinite(norms)) or np.any(norms == 0):
                raise ValueError("invalid CLS vector norms")
            pipe.send(("vectors", (cls / norms).astype(float).tolist()))
    except BaseException as exc:
        pipe.send(("error", type(exc).__name__ + ": " + str(exc)))
    finally:
        pipe.close()


class OfflineEmbedder:
    def __init__(self, model_path, tokenizer_path, deadline, descriptor):
        self.deadline = deadline
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_offline_worker, args=(child, model_path, tokenizer_path), daemon=True)
        self.process.start()
        child.close()
        try:
            kind, actual = self._receive()
            if kind != "ready" or actual != descriptor:
                raise ValueError("offline runtime not ready")
        except BaseException:
            self.close()
            raise

    def _receive(self):
        catalog._check_deadline(self.deadline)
        if not self.pipe.poll(min(60, self.deadline - time.monotonic())):
            raise TimeoutError("offline embedding worker timeout")
        kind, data = self.pipe.recv()
        if kind == "error":
            raise ValueError("offline embedding failed: " + data)
        return kind, data

    def encode(self, texts):
        self.pipe.send(texts)
        kind, vectors = self._receive()
        if kind != "vectors":
            raise ValueError("invalid offline runtime response")
        return vectors

    def close(self):
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=2)
        self.pipe.close()


def point_id(generation, representation_id, view_index):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dtox:source:{generation}:{representation_id}:{view_index}"))


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError("invalid " + name)
    return value


def _catalog_generation(path):
    conn = catalog._connect(Path(path).absolute())
    try:
        row = conn.execute("SELECT generation FROM catalog WHERE singleton=1").fetchone()
        if row is None:
            raise ValueError("missing catalog generation")
        return catalog._sha(row[0])
    finally:
        conn.close()


def plan_generation(catalog_path, *, max_representations=None, max_points=DEFAULT_MAX_POINTS,
                    max_bytes=DEFAULT_MAX_BYTES, timeout=120, tokenizer=None,
                    model_dir=None, model_file="onnx/model_quantized.onnx"):
    """No HTTP/writes. Counts and conservative disk/embedding bounds BEFORE create.

    Planning uses SQL aggregates under a deadline, not hydrated Python corpus copies.
    --max-representations limits NEW representations only and always means partial.
    estimated_disk_bytes is a conservative planning allowance, not a disk guarantee.
    """
    _positive(max_points, "max_points")
    _positive(max_bytes, "max_bytes")
    if max_representations is not None:
        _positive(max_representations, "max_representations")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid planning timeout")
    path = Path(catalog_path).resolve(strict=True)
    generation = _catalog_generation(path)
    descriptor = None
    if model_dir is not None:
        _, tokenizer_path, descriptor = offline_assets(model_dir, model_file)
        tokenizer = load_tokenizer(tokenizer_path)
    deadline = time.monotonic() + timeout
    with catalog.CatalogReader(path, generation, deadline=deadline) as reader:
        conn = reader.conn
        coverage = reader.coverage
        source_count = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM representations").fetchone()[0]
        native = conn.execute("SELECT count(*) FROM representations WHERE kind='own_passage'").fetchone()[0]
        native_legacy = conn.execute(
            "SELECT count(DISTINCT r.source_id) FROM representations r JOIN sources s ON s.source_id=r.source_id "
            "WHERE r.kind='own_passage' AND s.publication_revision IS NULL").fetchone()[0]
        count, text_bytes, point_bound = conn.execute(
            "SELECT count(*),coalesce(sum(n),0),coalesce(sum(chars),0) FROM "
            "(SELECT length(CAST(text AS BLOB)) n,length(text) chars FROM representations "
            "WHERE kind!='own_passage' ORDER BY id LIMIT ?)",
            (max_representations or -1,)).fetchone()
        counts_by_kind = dict(conn.execute("SELECT kind,count(*) FROM representations GROUP BY kind"))
        excessive_facets = conn.execute(
            "SELECT 1 FROM (SELECT DISTINCT source_id,kind,section_type,element_type,terms "
            "FROM representations WHERE origin_id=source_id AND kind IN ('own_summary','own_passage')) "
            "GROUP BY source_id HAVING count(*)>256 LIMIT 1").fetchone()
        exact_points = None
        if tokenizer is not None:
            exact_points, offset, consumed = 0, 0, 0
            while consumed < count:
                rows = conn.execute("SELECT id,text FROM representations WHERE id>? AND kind!='own_passage' ORDER BY id LIMIT ?",
                                    (offset, min(64, count - consumed))).fetchall()
                if not rows:
                    raise ValueError("catalog count changed")
                for row in rows:
                    catalog._check_deadline(deadline)
                    exact_points += sum(1 for _ in text_views(row["text"], tokenizer=tokenizer))
                    offset = row["id"]
                    consumed += 1
            point_bound = exact_points
    allowance = DIMENSION * 4 + 16 * 8 * 2 + PAYLOAD_BYTES + 1024
    estimate = point_bound * allowance
    reasons = []
    if point_bound > max_points:
        reasons.append("point_budget_exceeded")
    if estimate > max_bytes:
        reasons.append("disk_budget_exceeded")
    if excessive_facets:
        reasons.append("owned_facets_budget_exceeded")
    return {"version": 1, "generation": generation, "catalog_path": str(path),
            "collection": collection_name(generation), "model_id": MODEL_ID, "dimension": DIMENSION,
            "counts": {"sources": source_count, "representations": total,
                       "total_representations": total, "native_owned_representations": native,
                       "new_indexed_representations": total - native,
                       "selected_representations": count, "point_upper_bound": point_bound,
                       "views": exact_points},
            "counts_by_kind": counts_by_kind, "catalog_bytes": path.stat().st_size,
            "text_bytes": text_bytes, "estimated_disk_bytes": estimate,
            "embedding_bytes": text_bytes, "embedding_point_upper_bound": point_bound,
            "view_bytes": VIEW_BYTES, "max_representations": max_representations,
            "max_points": max_points, "max_bytes": max_bytes,
            "partial": max_representations is not None or not catalog.coverage_is_complete(coverage),
            "coverage": coverage, "complete": False,
            "allowed": not reasons, "reasons": reasons, "embedding": descriptor,
            "native": {"mode": "read_only_query_time_exact_payload_validation",
                       "legacy_unmarked_sources": native_legacy, "corpus_attested": False,
                       "vectors_copied": 0, "vectors_embedded": 0},
            "view_count_kind": "tokenizer_exact" if exact_points is not None else "unicode_codepoint_upper_bound",
            "reuse": "native_owned_exact_text_owner_revision_query_time"}


def _endpoint(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("explicit HTTP endpoint required, no credentials/query")
    return value.rstrip("/")


class HTTP:
    def __init__(self, deadline, request=None):
        if request is None:
            import requests
            request = requests.request
        self.request = request
        self.deadline = deadline

    def call(self, method, url, body=None, *, missing=False, retry=True):
        for attempt in range(3 if retry else 1):
            catalog._check_deadline(self.deadline)
            try:
                response = self.request(method, url, json=body,
                                        timeout=min(30, self.deadline - time.monotonic()))
                catalog._check_deadline(self.deadline)
                if missing and response.status_code == 404:
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    raise RuntimeError("retryable HTTP failure")
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get("status") == "error":
                    raise ValueError("remote operation failed")
                return data
            except (ValueError, catalog._Deadline):
                raise
            except Exception:
                if not retry or attempt == 2:
                    raise
        raise RuntimeError("HTTP retries exhausted")


def _result(data):
    if not isinstance(data, dict):
        raise ValueError("invalid HTTP response")
    return data.get("result", data)


def _check_config(info):
    config = _result(info)["config"]
    params = config["params"]
    vectors = params["vectors"]
    if (vectors.get("size") != DIMENSION or vectors.get("distance") != "Cosine"
            or vectors.get("on_disk") is not True or params.get("on_disk_payload") is not True
            or config["hnsw_config"].get("on_disk") is not True
            or type(config["hnsw_config"].get("max_indexing_threads")) is not int
            or config["hnsw_config"].get("max_indexing_threads") != 1
            or type(config.get("optimizer_config", {}).get("max_optimization_threads")) is not int
            or config.get("optimizer_config", {}).get("max_optimization_threads") != 1
            or config["hnsw_config"].get("m") != 16
            or config["hnsw_config"].get("ef_construct") != 128):
        raise ValueError("collection config mismatch")


def _ledger(path, fingerprint):
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                       "CREATE TABLE IF NOT EXISTS points (id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL);")
    prior = conn.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
    if prior is not None and prior[0] != fingerprint:
        conn.close()
        raise ValueError("checkpoint identity mismatch")
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('identity',?)", (fingerprint,))
    conn.commit()
    return conn


def _facets(reader, source_id):
    rows = reader.conn.execute(
        "SELECT DISTINCT kind,section_type,element_type,terms FROM representations "
        "WHERE source_id=? AND origin_id=source_id AND kind IN ('own_summary','own_passage') "
        "ORDER BY kind,section_type,element_type,terms LIMIT 257", (source_id,)).fetchall()
    if len(rows) > 256:
        raise ValueError("owned facet lookup exceeds bound")
    facets = []
    for row in rows:
        facet = {"kind": row["kind"], "terms": catalog._terms(json.loads(row["terms"]))}
        for field in ("section_type", "element_type"):
            if row[field] is not None:
                facet[field] = catalog._string(row[field], field)
        if facet not in facets:
            facets.append(facet)
    return facets


def _points(reader, plan, tokenizer):
    conn = reader.conn
    offset, seen, facets_cache = 0, 0, {}
    while True:
        catalog._check_deadline(reader.deadline)
        left = plan["counts"]["selected_representations"] - seen
        if left <= 0:
            break
        rows = conn.execute("SELECT * FROM representations WHERE id>? AND kind!='own_passage' ORDER BY id LIMIT ?",
                            (offset, min(64, left))).fetchall()
        if not rows:
            raise ValueError("catalog count changed")
        for row in rows:
            catalog._check_deadline(reader.deadline)
            record = catalog._record(row)
            target = reader.source(record["source_id"])
            owner = reader.source(record["origin_id"])
            if target is None or owner is None:
                raise ValueError("orphan representation")
            if record["source_id"] not in facets_cache:
                if len(facets_cache) >= 128:
                    facets_cache.clear()
                facets_cache[record["source_id"]] = _facets(reader, record["source_id"])
            digest = hashlib.sha256(record["text"].encode()).hexdigest()
            for index, (start, end, text) in enumerate(text_views(record["text"], tokenizer=tokenizer)):
                payload = {"generation": plan["generation"], "model_id": MODEL_ID, "dimension": DIMENSION,
                    "embedding": plan["embedding"],
                    "representation_id": record["representation_id"], "source_id": record["source_id"],
                    "origin_id": record["origin_id"], "origin_revision": record["origin_revision"],
                    "target_publication_revision": target["publication_revision"],
                    "owner_publication_revision": owner["publication_revision"],
                    "target_status": target["status"], "owner_status": owner["status"],
                    "target_year": target["year"], "target_layers": target["layers"],
                    "owned_facets": facets_cache[record["source_id"]], "kind": record["kind"],
                    "locator": record["locator"], "binding": record["binding"],
                    "representation_text_sha256": digest,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "view": {"index": index, "start": start, "end": end,
                             "unit": "unicode_codepoint", "coverage": "complete_partition"}}
                encoded = catalog._json(payload).encode()
                if len(encoded) > PAYLOAD_BYTES:
                    raise ValueError("payload exceeds planned allowance")
                yield {"id": point_id(plan["generation"], record["representation_id"], index),
                       "payload": payload, "text": text,
                       "payload_hash": hashlib.sha256(encoded).hexdigest()}
            offset = row["id"]
            seen += 1
        # Metadata is bounded per batch; no entire-corpus Python cache.
        reader.metadata.clear()


def _publish(path, document):
    path = Path(path).absolute()
    fd, temporary = tempfile.mkstemp(prefix=".source-manifest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(catalog._json(document) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # A generation manifest is new-only. A separate activation owner selects it.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _verify_remote(http, base, ledger, expected):
    if ledger.execute("SELECT count(*) FROM points").fetchone()[0] != expected:
        raise ValueError("ledger contains unexpected point IDs")
    actual = _result(http.call("POST", base + "/points/count", {"exact": True}))["count"]
    if actual != expected:
        raise ValueError("remote point count mismatch")
    cursor = ledger.execute("SELECT id,payload_hash FROM points ORDER BY id")
    while True:
        batch = cursor.fetchmany(64)
        if not batch:
            break
        rows = _result(http.call("POST", base + "/points", {
            "ids": [row[0] for row in batch], "with_payload": True, "with_vector": False}))
        if not isinstance(rows, list) or len(rows) != len(batch):
            raise ValueError("remote point ID count mismatch")
        found = {str(row["id"]): hashlib.sha256(catalog._json(row["payload"]).encode()).hexdigest() for row in rows}
        if found != dict(batch):
            raise ValueError("remote IDs/payload generation/hash mismatch")
    _check_config(http.call("GET", base))


def index_generation(catalog_path, *, manifest_path, checkpoint_path, embed_endpoint=None,
                     qdrant_endpoint, allow_collection, max_representations=None,
                     max_points=DEFAULT_MAX_POINTS, max_bytes=DEFAULT_MAX_BYTES,
                     batch_size=16, timeout=300, request=None, model_dir=None,
                     model_file="onnx/model_quantized.onnx", tokenizer_path=None,
                     tokenizer=None, descriptor=None):
    _positive(batch_size, "batch_size")
    if batch_size > 32:
        raise ValueError("batch exceeds bounded embedding batch")
    deadline = time.monotonic() + timeout
    runtime = None
    if model_dir is not None:
        model_path, tokenizer_path, descriptor = offline_assets(model_dir, model_file)
        tokenizer = load_tokenizer(tokenizer_path)
    elif tokenizer is None:
        if tokenizer_path is None:
            raise ValueError("HTTP indexing requires local tokenizer, verified against runtime health")
        tokenizer = load_tokenizer(tokenizer_path)
    plan = plan_generation(catalog_path, max_representations=max_representations,
                           max_points=max_points, max_bytes=max_bytes, timeout=timeout, tokenizer=tokenizer)
    plan["embedding"] = descriptor
    if not plan["allowed"]:
        raise ValueError("heavy generation refused: " + ",".join(plan["reasons"]))
    if allow_collection != plan["collection"]:
        raise ValueError("exact derived collection allowlist required")
    manifest_path = Path(manifest_path).absolute()
    checkpoint_path = Path(checkpoint_path).absolute()
    catalog_path = Path(plan["catalog_path"])
    if len({manifest_path.resolve(), checkpoint_path.resolve(), catalog_path.resolve()}) != 3:
        raise ValueError("catalog/manifest/checkpoint paths must differ")
    embed_endpoint = _endpoint(embed_endpoint) if model_dir is None else None
    qdrant_endpoint = _endpoint(qdrant_endpoint)
    http = HTTP(deadline, request)
    if model_dir is None:
        health = http.call("GET", embed_endpoint + "/healthz")
        actual = health.get("embedding")
        if not isinstance(actual, dict) or health.get("model") != MODEL_ID:
            raise ValueError("embedding runtime lacks actual loaded-file hashes")
        if descriptor is not None and descriptor != actual:
            raise ValueError("embedding model/hash/contract mismatch")
        descriptor = actual
        if tokenizer_path is not None and descriptor.get("tokenizer_sha256") != _file_sha(tokenizer_path):
            raise ValueError("runtime/local tokenizer hash mismatch")
    if descriptor != embedding_descriptor(descriptor["model_sha256"], descriptor["tokenizer_sha256"]):
        raise ValueError("invalid embedding descriptor")
    plan["embedding"] = descriptor
    identity = hashlib.sha256(catalog._json({**plan, "embed_endpoint": embed_endpoint,
                                           "qdrant_endpoint": qdrant_endpoint}).encode()).hexdigest()
    base = qdrant_endpoint + "/collections/" + plan["collection"]
    if manifest_path.exists():
        # Completed retries are read-only, still verify remote counts/IDs/config.
        with manifest_path.open("rb") as handle:
            prior_raw = handle.read(65537)
        if len(prior_raw) > 65536:
            raise FileExistsError("existing manifest is not owned")
        prior = json.loads(prior_raw)
        for key in ("version", "generation", "catalog_path", "collection", "model_id", "dimension", "embedding"):
            if prior.get(key) != plan.get(key):
                raise FileExistsError("existing manifest identity mismatch")
        if prior.get("partial") != plan["partial"] or prior.get("complete") != (not plan["partial"]):
            raise FileExistsError("existing manifest coverage mismatch")
        conn = sqlite3.connect(checkpoint_path.as_uri() + "?mode=ro", uri=True)
        try:
            checkpoint = conn.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            if checkpoint != (identity,):
                raise ValueError("checkpoint identity mismatch")
            if conn.execute("SELECT value FROM meta WHERE key='owned'").fetchone() != (OWNED_AFTER_CREATE,):
                raise ValueError("refusing pre-existing unowned collection; orphan requires separate ownership proof")
            _verify_remote(http, base, conn, prior["counts"]["points"])
        finally:
            conn.close()
        return prior
    ledger = _ledger(checkpoint_path, identity)
    try:
        if model_dir is not None:
            runtime = OfflineEmbedder(model_path, tokenizer_path, deadline, descriptor)
        info = http.call("GET", base, missing=True)
        owned = ledger.execute("SELECT value FROM meta WHERE key='owned'").fetchone()
        if info is not None and owned != (OWNED_AFTER_CREATE,):
            raise ValueError("refusing pre-existing unowned collection; orphan requires separate ownership proof")
        if info is None:
            if owned is not None or ledger.execute("SELECT 1 FROM points LIMIT 1").fetchone():
                raise ValueError("checkpoint collection disappeared")
            created = http.call("PUT", base, {"vectors": {"size": DIMENSION, "distance": "Cosine", "on_disk": True},
                      "on_disk_payload": True, "hnsw_config": {"m": 16, "ef_construct": 128,
                      "on_disk": True, "max_indexing_threads": 1},
                      "optimizers_config": {"max_optimization_threads": 1}}, retry=False)
            if _result(created) is not True:
                raise ValueError("collection creation not confirmed; refusing ownership")
            info = http.call("GET", base)
            _check_config(info)
            ledger.execute("INSERT INTO meta VALUES ('owned',?)", (OWNED_AFTER_CREATE,))
            ledger.commit()
        else:
            _check_config(info)
        for field, schema in (("generation", "keyword"), ("source_id", "keyword"),
                ("target_status", "keyword"), ("owner_status", "keyword"),
                ("target_layers", "keyword"), ("target_year", "integer"),
                ("owned_facets[].kind", "keyword"), ("owned_facets[].section_type", "keyword"),
                ("owned_facets[].element_type", "keyword"), ("owned_facets[].terms", "keyword")):
            http.call("PUT", base + "/index?wait=true", {"field_name": field, "field_schema": schema})
        ledger.execute("INSERT OR REPLACE INTO meta VALUES ('complete','false')")
        ledger.commit()
        pending, expected = [], 0

        def flush():
            if not pending:
                return
            texts = [p["text"] for p in pending]
            if runtime is None and http.call("GET", embed_endpoint + "/healthz").get("embedding") != descriptor:
                raise ValueError("embedding runtime changed during index")
            vectors = runtime.encode(texts) if runtime is not None else http.call(
                "POST", embed_endpoint + "/embed_batch", {"texts": texts})["vectors"]
            if runtime is None and http.call("GET", embed_endpoint + "/healthz").get("embedding") != descriptor:
                raise ValueError("embedding runtime changed during batch")
            if len(vectors) != len(pending) or any(not isinstance(v, list) or len(v) != DIMENSION
                    or any(type(x) not in (int, float) or not math.isfinite(x) for x in v) for v in vectors):
                raise ValueError("embedding count/dimension mismatch")
            http.call("PUT", base + "/points?wait=true", {"points": [
                {"id": p["id"], "vector": vector, "payload": p["payload"]}
                for p, vector in zip(pending, vectors)]})
            ledger.executemany("INSERT OR REPLACE INTO points VALUES (?,?)",
                               [(p["id"], p["payload_hash"]) for p in pending])
            ledger.commit()
            pending.clear()

        with catalog.CatalogReader(catalog_path, plan["generation"], deadline=deadline) as reader:
            for point in _points(reader, plan, tokenizer):
                expected += 1
                if expected > max_points:
                    raise ValueError("actual point budget exceeded")
                prior = ledger.execute("SELECT payload_hash FROM points WHERE id=?", (point["id"],)).fetchone()
                if prior is not None:
                    if prior[0] != point["payload_hash"]:
                        raise ValueError("checkpoint text/owner/config mismatch")
                    continue
                pending.append(point)
                if len(pending) >= batch_size:
                    flush()
            flush()
        _verify_remote(http, base, ledger, expected)
        counts = {"sources": plan["counts"]["sources"], "representations": plan["counts"]["representations"],
                  "total_representations": plan["counts"]["total_representations"],
                  "native_owned_representations": plan["counts"]["native_owned_representations"],
                  "new_indexed_representations": plan["counts"]["new_indexed_representations"],
                  "indexed_representations": plan["counts"]["selected_representations"], "points": expected}
        manifest = {"version": 1, "generation": plan["generation"], "catalog_path": str(catalog_path),
                    "collection": plan["collection"], "model_id": MODEL_ID, "dimension": DIMENSION,
                    "complete": not plan["partial"], "partial": plan["partial"], "counts": counts,
                    "view_bytes": VIEW_BYTES, "reuse": plan["reuse"], "embedding": descriptor,
                    "native": plan["native"],
                    "coverage": plan["coverage"],
                    "completion_scope": "derived_point_ids_verified;native_hits_verified_at_query_time_only"}
        catalog._check_deadline(deadline)
        _publish(manifest_path, manifest)
        ledger.execute("INSERT OR REPLACE INTO meta VALUES ('complete',?)", (json.dumps(manifest["complete"]),))
        ledger.commit()
        return manifest
    finally:
        if runtime is not None:
            runtime.close()
        ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--max-representations", type=int)
    parser.add_argument("--max-points", type=int, default=DEFAULT_MAX_POINTS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--manifest")
    parser.add_argument("--checkpoint")
    parser.add_argument("--embed-endpoint")
    parser.add_argument("--model-dir")
    parser.add_argument("--model-file", default="onnx/model_quantized.onnx")
    parser.add_argument("--tokenizer-path")
    parser.add_argument("--qdrant-endpoint")
    parser.add_argument("--allow-collection")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args(argv)
    common = dict(max_representations=args.max_representations, max_points=args.max_points,
                  max_bytes=args.max_bytes, timeout=args.timeout)
    try:
        if args.execute:
            if not all((args.manifest, args.checkpoint, args.qdrant_endpoint, args.allow_collection)) or not (
                    args.model_dir or args.embed_endpoint):
                parser.error("execute requires manifest/checkpoint/endpoints/exact allow-collection")
            result = index_generation(args.catalog, manifest_path=args.manifest, checkpoint_path=args.checkpoint,
                embed_endpoint=args.embed_endpoint, qdrant_endpoint=args.qdrant_endpoint,
                allow_collection=args.allow_collection, batch_size=args.batch_size,
                model_dir=args.model_dir, model_file=args.model_file,
                tokenizer_path=args.tokenizer_path, **common)
        else:
            result = plan_generation(args.catalog, model_dir=args.model_dir,
                                     model_file=args.model_file, **common)
        print(catalog._json(result))
        return 0 if result.get("allowed", True) else 2
    except (OSError, ValueError, KeyError, sqlite3.Error, catalog._Deadline) as exc:
        print(catalog._json({"complete": False, "status": "failed", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
