"""Restore a missing legacy cache tail from unchanged Qdrant/FTS projections.

This operation only appends existing payload text/metadata to the document
cache. It never writes Qdrant, FTS, paper state, vectors, or section manifests.
"""

import base64
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from ops import repair_section_labels as labels
from pipeline import document_cache


RepairError = labels.RepairError
REQUIRED_FIELDS = ("chunk_index", "section_title", "section_type", "element_type",
                   "has_math", "has_algorithm", "text")
MAX_CACHE_BYTES = document_cache.MAX_BYTES
MAX_POINTS = 3000


def _regular_file(path, *, under=None, limit=MAX_CACHE_BYTES):
    path = Path(path)
    if path.is_symlink():
        raise RepairError("symlinked files are outside the cache-tail input contract")
    path = path.resolve(strict=True)
    if under is not None and path.parent != Path(under).resolve(strict=True):
        raise RepairError("file is outside its explicitly resolved directory")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise RepairError("expected a bounded regular file")
    return path


def _read(path, *, under=None, limit=MAX_CACHE_BYTES):
    path = _regular_file(path, under=under, limit=limit)
    data = path.read_bytes()
    if len(data) != path.stat().st_size:
        raise RepairError("file changed while reading")
    return path, data


def _sqlite_identity(path):
    path = Path(path).resolve(strict=True)
    st = path.stat()
    return [str(path), st.st_dev, st.st_ino]


def _rowmap_rows(path, fts_path, paper_id):
    conn = labels._connect(path, readonly=True)
    try:
        meta = dict(conn.execute("SELECT key,value FROM meta"))
        if meta.get("complete") != "1" or json.loads(meta.get("source_identity", "null")) != _sqlite_identity(fts_path):
            raise RepairError("complete frozen FTS rowmap identity is required")
        if [r[1] for r in conn.execute("PRAGMA table_info(rows)")] != ["rowid", "point_id", "arxiv_id"]:
            raise RepairError("unexpected FTS rowmap schema")
        indexes = conn.execute("PRAGMA index_list(rows)").fetchall()
        if not any([r[2] for r in conn.execute(
                'PRAGMA index_info("' + item[1].replace('"', '""') + '")')][:1] == ["arxiv_id"]
                   for item in indexes):
            raise RepairError("FTS rowmap must have an arxiv_id-leading index")
        rows = conn.execute("SELECT rowid,point_id,arxiv_id FROM rows WHERE arxiv_id=? ORDER BY rowid",
                            (paper_id,)).fetchall()
        if (any(type(row[0]) is not int or row[0] <= 0 or not row[1] or row[2] != paper_id for row in rows)
                or len({row[0] for row in rows}) != len(rows)
                or len({row[1] for row in rows}) != len(rows)):
            raise RepairError("invalid or duplicate FTS rowmap entry")
        return [list(row) for row in rows]
    finally:
        conn.close()


def _paper_state(conn, paper_id):
    if not labels._exists(conn, "papers"):
        raise RepairError("state DB has no papers table")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (paper_id,)).fetchone()
    if row is None or row["status"] != "done":
        raise RepairError("paper must remain an existing done legacy record")
    for table in ("paper_active", "paper_publications"):
        if labels._exists(conn, table) and conn.execute(
                "SELECT 1 FROM " + table + " WHERE arxiv_id=? LIMIT 1", (paper_id,)).fetchone():
            raise RepairError("managed or publication-owned papers cannot use legacy cache-tail restore")
    return dict(row)


def _qdrant_points(qdrant, collection, paper_id):
    points, offset, seen = [], None, set()
    while True:
        args = {"collection_name": collection,
                "scroll_filter": {"must": [{"key": "arxiv_id", "match": {"value": paper_id}}]},
                "limit": 200, "with_payload": True, "with_vectors": False}
        if offset is not None:
            args["offset"] = offset
        result = qdrant.scroll(**args)
        page, next_offset = result if isinstance(result, tuple) else (result, None)
        for point in page:
            payload = labels._payload(point)
            if not isinstance(payload, dict):
                raise RepairError("Qdrant returned a malformed payload")
            points.append({"id": labels._point_id(point), "payload": json.loads(
                json.dumps(payload, ensure_ascii=False, allow_nan=False))})
        if len(points) > MAX_POINTS:
            raise RepairError("paper exceeds the bounded legacy point snapshot")
        if next_offset is None:
            break
        marker = labels._json_bytes(next_offset)
        if marker in seen:
            raise RepairError("Qdrant scroll repeated an offset")
        seen.add(marker)
        offset = next_offset
    if len({point["id"] for point in points}) != len(points):
        raise RepairError("Qdrant point IDs are duplicated")
    return sorted(points, key=lambda point: point["id"])


def _field(payload, name):
    if name in payload:
        return payload[name]
    if name == "element_type":
        return "prose"
    if name in ("has_math", "has_algorithm"):
        return False
    raise RepairError("Qdrant payload lacks a field required by the cache schema")


def _fts_rows(fts_path, rowmap_rows, point_map, paper_id):
    conn = labels._connect(fts_path, readonly=True)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        required = {"text", "point_id", "arxiv_id", "section_type", "element_type", "layers", "year"}
        if not required <= columns:
            raise RepairError("FTS chunks table lacks required legacy projection columns")
        rows = []
        for rowid, point_id, arxiv_id in rowmap_rows:
            observed = conn.execute("SELECT text,point_id,arxiv_id,section_type,element_type,layers,year "
                                   "FROM chunks WHERE rowid=?", (rowid,)).fetchone()
            point = point_map.get(point_id)
            if observed is None or point is None or tuple(observed[1:3]) != (point_id, paper_id):
                raise RepairError("FTS row identity does not map to the complete Qdrant paper")
            payload = point["payload"]
            expected = (payload.get("text"), point_id, paper_id, payload.get("section_type"),
                        payload.get("element_type", "prose"), str(payload.get("layers") or ""),
                        payload.get("year"))
            if tuple(observed) != expected:
                raise RepairError("FTS row content/metadata differs from its Qdrant point")
            rows.append({"rowid": rowid, "values": list(observed)})
        if len(rows) != len(point_map) or {r["values"][1] for r in rows} != set(point_map):
            raise RepairError("complete FTS and Qdrant point sets differ")
        return rows
    finally:
        conn.close()


def _validate_inventory(cache, points, paper_id):
    if not isinstance(cache, list) or not cache or len(cache) >= len(points):
        raise RepairError("a non-empty cache prefix and at least one live tail point are required")
    keys = set(cache[0]) if isinstance(cache[0], dict) else set()
    if not set(REQUIRED_FIELDS) <= keys or any(not isinstance(c, dict) or set(c) != keys for c in cache):
        raise RepairError("cache chunks must share a schema containing all seven projection fields")
    indices = [c.get("chunk_index") for c in cache]
    if indices != list(range(len(cache))):
        raise RepairError("cache must be a contiguous zero-based prefix")
    by_index = {}
    point_map = {}
    for point in points:
        pid, payload = point["id"], point["payload"]
        idx = payload.get("chunk_index")
        if (payload.get("arxiv_id") != paper_id or type(idx) is not int or idx in by_index
                or not isinstance(payload.get("text"), str) or "publication_revision" in payload):
            raise RepairError("legacy Qdrant inventory has invalid identity or publication marker")
        expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{paper_id}#{idx}"))
        if pid != expected_id:
            raise RepairError("Qdrant point ID is not the deterministic legacy UUID")
        by_index[idx] = point
        point_map[pid] = point
    if set(by_index) != set(range(len(points))):
        raise RepairError("live point indices must be one contiguous zero-based prefix")
    for chunk in cache:
        point = by_index.get(chunk["chunk_index"])
        if point is None:
            raise RepairError("cached chunk is missing from live Qdrant")
        payload = point["payload"]
        for key in keys:
            if chunk[key] != _field(payload, key):
                raise RepairError(f"cached prefix differs from Qdrant payload field {key}")
    for idx in range(len(cache), len(points)):
        payload = by_index[idx]["payload"]
        chunk = {key: _field(payload, key) for key in keys}
        if chunk["chunk_index"] != idx:
            raise RepairError("tail cache index mapping is ambiguous")
        if (not document_cache._label(chunk["section_title"])
                or not isinstance(chunk["section_type"], str)
                or chunk["section_type"] not in document_cache.SECTION_TYPES
                or not isinstance(chunk["element_type"], str)
                or type(chunk["has_math"]) is not bool
                or type(chunk["has_algorithm"]) is not bool
                or not isinstance(chunk["text"], str) or not chunk["text"].strip()):
            raise RepairError("tail payload fields are invalid for the existing cache schema")
    return by_index, point_map, keys


def _source_proof(source, by_index, cache_count):
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return {"exact_unique_text_matches": False, "tail": [], "reason": "source_not_utf8"}
    details = []
    valid = True
    for index in range(cache_count, len(by_index)):
        fragment = by_index[index]["payload"].get("text", "")
        count = text.count(fragment) if fragment else 0
        offset = text.find(fragment) if count else -1
        details.append({"chunk_index": index, "occurrences": count, "offset": offset})
        valid = valid and count == 1
    return {"exact_unique_text_matches": bool(valid), "tail": details,
            "meaning": "unique literal matches only; does not certify source order, edition, completeness, or quality"}


def _manifest_path(section_cache_dir, paper_id):
    root = Path(section_cache_dir).resolve(strict=True)
    if not root.is_dir():
        raise RepairError("resolved section-cache directory is required")
    path = root / (document_cache.safe_id(paper_id) + ".json")
    if path.exists() or path.is_symlink():
        raise RepairError("existing section manifest is outside cache-tail recovery")
    return path


def prepare_plan(paper_id, *, state_db, fts_db, rowmap_db, cache_dir,
                 section_cache_dir, source_path, journal_path, qdrant,
                 collection="papers_fulltext", expected_cache_sha256=None,
                 expected_source_sha256=None):
    """Freeze and journal an exact legacy cache-tail append without live writes."""
    journal_path = Path(journal_path)
    if collection != "papers_fulltext" or journal_path.exists() or journal_path.is_symlink():
        raise RepairError("fresh journal and legacy fulltext collection required")
    name = document_cache.safe_id(paper_id) + ".json"
    cache_root = Path(cache_dir).resolve(strict=True)
    section_root = Path(section_cache_dir).resolve(strict=True)
    cache_path, before_cache = _read(cache_root / name, under=cache_root)
    source_path, source = _read(source_path)
    cache_sha, source_sha = labels._sha(before_cache), labels._sha(source)
    if expected_cache_sha256 is not None and cache_sha != expected_cache_sha256:
        raise RepairError("cache differs from the frozen expected hash")
    if expected_source_sha256 is not None and source_sha != expected_source_sha256:
        raise RepairError("source differs from the frozen expected hash")
    manifest_path = _manifest_path(section_root, paper_id)
    try:
        chunks = json.loads(before_cache)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RepairError("cache is not valid JSON") from exc
    if not isinstance(chunks, list):
        raise RepairError("cache must be a JSON chunk list")

    state = labels._connect(state_db, readonly=True)
    try:
        state_before = _paper_state(state, paper_id)
    finally:
        state.close()
    points = _qdrant_points(qdrant, collection, paper_id)
    by_index, point_map, _keys = _validate_inventory(chunks, points, paper_id)
    rowmap_rows = _rowmap_rows(rowmap_db, fts_db, paper_id)
    fts_before = _fts_rows(fts_db, rowmap_rows, point_map, paper_id)
    tails = [{key: _field(by_index[idx]["payload"], key) for key in chunks[0]}
             for idx in range(len(chunks), len(points))]
    after_chunks = chunks + tails
    after_cache = labels._json_bytes(after_chunks)
    if len(after_cache) > MAX_CACHE_BYTES:
        raise RepairError("appended cache exceeds the existing document-cache byte bound")
    plan = {"schema": 1, "kind": "restore-cache-tail", "phase": "prepared",
            "paper_id": paper_id, "collection": collection,
            "state_db": str(Path(state_db).resolve()), "fts_db": str(Path(fts_db).resolve()),
            "rowmap_db": str(Path(rowmap_db).resolve()), "cache_dir": str(cache_root),
            "cache_path": str(cache_path),
            "source_path": str(source_path), "manifest_path": str(manifest_path),
            "source_sha256": source_sha, "source_size": len(source),
            "cache_before_sha256": cache_sha, "cache_after_sha256": labels._sha(after_cache),
            "state_before": state_before,
            "cache_before": base64.b64encode(before_cache).decode("ascii"),
            "cache_after": base64.b64encode(after_cache).decode("ascii"),
            "qdrant_before": points, "fts_before": fts_before,
            "rowmap_rows": rowmap_rows,
            "source_proof": _source_proof(source, by_index, len(chunks)),
            "cache_prefix_count": len(chunks), "tail_count": len(tails),
            "point_count": len(points)}
    _verify(plan, qdrant, "before")
    labels._atomic(journal_path, labels._json_bytes(plan))
    return plan


def _load(path):
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if plan.get("schema") != 1 or plan.get("kind") != "restore-cache-tail":
        raise RepairError("invalid cache-tail journal")
    if (labels._sha(base64.b64decode(plan.get("cache_before", ""))) != plan.get("cache_before_sha256")
            or labels._sha(base64.b64decode(plan.get("cache_after", ""))) != plan.get("cache_after_sha256")):
        raise RepairError("cache-tail journal before/after image hashes are invalid")
    return plan


def _verify(plan, qdrant, wanted):
    aid = plan["paper_id"]
    source_path, source = _read(plan["source_path"])
    if str(source_path) != plan["source_path"]:
        raise RepairError("source path resolution changed after preparation")
    if len(source) != plan["source_size"] or labels._sha(source) != plan["source_sha256"]:
        raise RepairError("source changed after cache-tail preparation")
    cache_path = _regular_file(plan["cache_path"], under=plan["cache_dir"], limit=MAX_CACHE_BYTES)
    current_cache = cache_path.read_bytes()
    before = base64.b64decode(plan["cache_before"])
    after = base64.b64decode(plan["cache_after"])
    expected_cache = before if wanted == "before" else after
    if current_cache != expected_cache:
        raise RepairError("cache differs from the expected journal image")
    if Path(plan["manifest_path"]).exists() or Path(plan["manifest_path"]).is_symlink():
        raise RepairError("a section manifest appeared during cache-tail repair")

    state = labels._connect(plan["state_db"], readonly=True)
    try:
        if _paper_state(state, aid) != plan["state_before"]:
            raise RepairError("paper state or managed ownership changed")
    finally:
        state.close()
    points = _qdrant_points(qdrant, plan["collection"], aid)
    if points != plan["qdrant_before"]:
        raise RepairError("Qdrant payload inventory changed concurrently")
    if _rowmap_rows(plan["rowmap_db"], plan["fts_db"], aid) != plan["rowmap_rows"]:
        raise RepairError("frozen FTS rowmap entries changed")
    point_map = {point["id"]: point for point in points}
    rows = _fts_rows(plan["fts_db"], plan["rowmap_rows"], point_map, aid)
    if rows != plan["fts_before"]:
        raise RepairError("FTS rows changed concurrently")
    return True


def _phase(path, plan, phase):
    plan["phase"] = phase
    labels._atomic(path, labels._json_bytes(plan))


def _restore(path, plan, qdrant):
    _verify_partial(plan, qdrant)
    before = base64.b64decode(plan["cache_before"])
    current = Path(plan["cache_path"]).read_bytes()
    if current != before:
        labels._atomic(plan["cache_path"], before)
    _phase(path, plan, "rolled_back")
    _verify(plan, qdrant, "before")


def _verify_partial(plan, qdrant):
    current = _regular_file(plan["cache_path"], limit=MAX_CACHE_BYTES).read_bytes()
    if current not in (base64.b64decode(plan["cache_before"]), base64.b64decode(plan["cache_after"])):
        raise RepairError("cache is neither exact before-image nor after-image; refusing compensation")
    _verify(plan, qdrant, "before" if current == base64.b64decode(plan["cache_before"]) else "after")


def apply_plan(journal_path, *, qdrant):
    plan = _load(journal_path)
    if plan["phase"] != "prepared":
        raise RepairError("cache-tail journal is not prepared")
    _verify(plan, qdrant, "before")
    _phase(journal_path, plan, "applying")
    try:
        labels._atomic(plan["cache_path"], base64.b64decode(plan["cache_after"]))
        _phase(journal_path, plan, "cache_written")
        _verify(plan, qdrant, "after")
        _phase(journal_path, plan, "applied")
    except Exception:
        _restore(journal_path, plan, qdrant)
        raise
    return plan


def rollback_plan(journal_path, *, qdrant):
    plan = _load(journal_path)
    if plan["phase"] not in {"applying", "cache_written", "applied"}:
        raise RepairError("journal has no in-progress or completed cache-tail apply")
    _restore(journal_path, plan, qdrant)
    return plan
