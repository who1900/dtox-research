"""Narrow, journaled label-only repair for legacy full-text papers.

Classification is deliberately external: callers supply the approved labels.
This module changes only section_title/section_type in cache, manifest, FTS,
and existing Qdrant payloads. It never writes vectors or publication state.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

from pipeline import document_cache
from pipeline import extractor


class RepairError(RuntimeError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _atomic(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(parent_fd)
            finally: os.close(parent_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("schema") != 1 or value.get("kind") != "section-label-repair":
        raise RepairError("invalid repair journal")
    return value


def _exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()


def _connect(path, readonly=False):
    if readonly:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=30)
    return sqlite3.connect(path, timeout=30)


def _stable_payload(payload):
    return _sha(_json_bytes({k: v for k, v in payload.items()
                             if k not in {"section_title", "section_type"}}))


def _fts_tuple(conn, rowid):
    return conn.execute("SELECT text,point_id,arxiv_id,section_type,element_type,layers,year "
                        "FROM chunks WHERE rowid=?", (rowid,)).fetchone()


def _fts_stable(row):
    return _sha(_json_bytes([row[0], row[1], row[2], "<section_type>", row[4], row[5], row[6]]))


def _qdrant_points(qdrant, collection, arxiv_id):
    """Fetch every page for one paper by payload filter."""
    if hasattr(qdrant, "scroll"):
        points, offset, seen = [], None, set()
        while True:
            args = {"collection_name": collection,
                    "scroll_filter": {"must": [{"key": "arxiv_id", "match": {"value": arxiv_id}}]},
                    "limit": 10000, "with_payload": True, "with_vectors": False}
            if offset is not None:
                args["offset"] = offset
            try:
                result = qdrant.scroll(**args)
            except TypeError:
                if offset is not None:
                    raise RepairError("legacy scroll adapter cannot paginate")
                return list(qdrant.scroll(arxiv_id))
            page, next_offset = result if isinstance(result, tuple) else (result, None)
            points.extend(page)
            if next_offset is None:
                return points
            marker = _json_bytes(next_offset)
            if marker in seen:
                raise RepairError("Qdrant scroll repeated a pagination offset")
            seen.add(marker)
            offset = next_offset
    raise RepairError("Qdrant client must provide scroll")


def _point_id(p):
    return str(getattr(p, "id", p.get("id") if isinstance(p, dict) else ""))


def _payload(p):
    return getattr(p, "payload", p.get("payload", {}) if isinstance(p, dict) else {}) or {}


def _write_payload_grouped(qdrant, collection, changes, labels):
    """Batch label-identical points while keeping payload updates label-only."""
    groups = {}
    for change in changes:
        pair = labels(change)
        groups.setdefault(pair, []).append(change["point_id"])
    for (title, kind), point_ids in groups.items():
        qdrant.set_payload(collection_name=collection,
                           payload={"section_title": title, "section_type": kind},
                           points=point_ids, wait=True)


def _verified_point_map(qdrant, collection, aid, expected_indices):
    points = _qdrant_points(qdrant, collection, aid)
    point_map = {_point_id(p): p for p in points}
    indices = [_payload(p).get("chunk_index") for p in points]
    if len(point_map) != len(points) or len(set(indices)) != len(indices) or set(indices) != set(expected_indices):
        raise RepairError("Qdrant point set changed from the prepared indexed chunk set")
    return point_map


_SOURCE_TEXT_FIELDS = ("chunk_index", "text", "element_type")


def _source_sections(source_bytes, route):
    """Parse source once into canonical sections and the exact split chunks."""
    if not isinstance(source_bytes, bytes) or len(source_bytes) > document_cache.MAX_BYTES:
        raise RepairError("manifest source snapshot is invalid or exceeds the bounded source size")
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepairError("manifest source is not valid UTF-8") from exc
    sections = []
    if route == "latex":
        chunks, mode = extractor.build_chunks(source_text, section_sink=sections)
    elif route in {"paper_markdown", "spec_markdown"}:
        chunks, mode = extractor.build_chunks_markdown(
            source_text, section_sink=sections,
            document_kind="paper" if route == "paper_markdown" else "spec")
    else:
        raise RepairError("unsupported manifest source route")
    if mode == "legacy" or not chunks or not sections:
        raise RepairError("source parser did not produce canonical sections")
    for section in sections:
        if (not document_cache._label(section.get("section_title"))
                or section.get("section_type") not in document_cache.SECTION_TYPES
                or not isinstance(section.get("text"), str) or not section["text"]):
            raise RepairError("source parser produced an invalid canonical section")
    return chunks, sections


def _manifest_after(raw, arxiv_id, cache_before, cache_after, *, source_bytes,
                    expected_source_sha256, source_route):
    """Relabel a manifest only after exact source, chunk, and section alignment."""
    manifest = json.loads(raw)
    document_cache.validate_section_manifest(manifest, arxiv_id, cache_before)
    source_sha = _sha(source_bytes)
    if (not isinstance(expected_source_sha256, str) or source_sha != expected_source_sha256
            or manifest["source_sha256"] != source_sha):
        raise RepairError("manifest source hash does not match the frozen source")
    old_chunks, new_chunks = json.loads(cache_before), json.loads(cache_after)
    parsed_chunks, source_sections = _source_sections(source_bytes, source_route)
    if len(old_chunks) != len(parsed_chunks) or len(new_chunks) != len(parsed_chunks):
        raise RepairError("manifest source and cache chunk counts differ")
    for old, parsed, new in zip(old_chunks, parsed_chunks, new_chunks):
        if any(old.get(field) != parsed.get(field) for field in _SOURCE_TEXT_FIELDS):
            raise RepairError("manifest source does not reproduce cached chunk text and identity exactly")
        if ((new.get("section_title", ""), new.get("section_type", "other"))
                != (parsed.get("section_title", ""), parsed.get("section_type", "other"))):
            raise RepairError("repaired cache labels do not match source-derived labels")

    # Existing manifests group adjacent cache chunks by their before-image labels.
    runs = []
    for chunk in old_chunks:
        pair = (chunk.get("section_title", ""), chunk.get("section_type", "other"))
        if not runs or runs[-1]["pair"] != pair:
            runs.append({"pair": pair})
    sections = manifest["sections"]
    if manifest.get("coverage") != "parsed_source" or len(runs) != len(sections):
        raise RepairError("manifest does not cover the exact ordered cache label runs")
    for run, section in zip(runs, sections):
        if (section["section_title"], section["section_type"]) != run["pair"]:
            raise RepairError("manifest/cache label-run order differs")

    # Map each existing manifest section to a consecutive source-section range.
    # Matching is byte-for-byte after the same CRLF normalization used by the
    # manifest builder; split chunks are never joined (their overlap is irrelevant).
    cursor = 0
    for section in sections:
        expected = section["text"].replace("\r\n", "\n").replace("\r", "\n")
        assembled = ""
        mapped = []
        while cursor < len(source_sections) and len(assembled) < len(expected):
            part = source_sections[cursor]["text"].replace("\r\n", "\n").replace("\r", "\n")
            assembled += ("\n\n" if assembled and part else "") + part
            mapped.append(source_sections[cursor])
            cursor += 1
            if not expected.startswith(assembled):
                raise RepairError("manifest section text does not align to source-ordered unsplit sections")
        if assembled != expected or not mapped:
            raise RepairError("manifest section text is not an exact source-section sequence")
        pairs = {(item["section_title"], item["section_type"]) for item in mapped}
        if len(pairs) != 1:
            raise RepairError("source sections require different labels inside one stored manifest section")
        section["section_title"], section["section_type"] = next(iter(pairs))
    if cursor != len(source_sections):
        raise RepairError("manifest omits source-ordered canonical sections")
    manifest["cache_sha256"] = _sha(cache_after)
    document_cache.validate_section_manifest(manifest, arxiv_id, cache_after, source_bytes)
    return _json_bytes(manifest)


def prepare_plan(changes, *, state_db, fts_db, cache_dir, section_cache_dir, journal_path,
                 qdrant, collection="papers_fulltext", manifest=True,
                 cache_labels_by_paper=None, expected_indexed_indices_by_paper=None,
                 complete_fts_rowids_by_paper=None,
                 preserve_invalid_unchanged_before_image=False,
                 unresolved_chunks_by_paper=None, source_bytes_by_paper=None,
                 expected_source_sha256_by_paper=None, source_route_by_paper=None):
    """Prepare a durable before-image plan. `changes` are externally classified labels.

    Each item: arxiv_id, rowid, point_id, section_title, section_type.
    """
    if not changes and (cache_labels_by_paper is None or expected_indexed_indices_by_paper is None):
        raise RepairError("empty changes plan")
    grouped = {}
    for c in changes:
        required = {"arxiv_id", "rowid", "point_id", "section_title", "section_type"}
        if not required <= c.keys() or type(c["rowid"]) is not int or c["rowid"] <= 0:
            raise RepairError("invalid change record")
        if ((not isinstance(c["section_type"], str) or c["section_type"] not in document_cache.SECTION_TYPES
             or not document_cache._label(c["section_title"]))
                and not preserve_invalid_unchanged_before_image):
            raise RepairError("invalid supplied section label")
        grouped.setdefault(c["arxiv_id"], []).append(dict(c))
    mapped_scope = None
    for mapping in (cache_labels_by_paper, expected_indexed_indices_by_paper, complete_fts_rowids_by_paper):
        if mapping is not None:
            if mapped_scope is None: mapped_scope = set(mapping)
            elif set(mapping) != mapped_scope: raise RepairError("paper scopes in supplied maps differ")
    if mapped_scope is not None:
        if grouped and set(grouped) != mapped_scope:
            raise RepairError("supplied maps must contain exactly selected papers")
        grouped = {aid: grouped.get(aid, []) for aid in mapped_scope}
    state = _connect(state_db, readonly=True); fts = _connect(fts_db, readonly=True)
    papers = []
    try:
        for aid, items in grouped.items():
            if not _exists(state, "papers"):
                raise RepairError("state DB lacks papers table")
            status = state.execute("SELECT status FROM papers WHERE arxiv_id=?", (aid,)).fetchone()
            if not status or status[0] != "done":
                raise RepairError("paper status must be done for a label-only repair")
            if _exists(state, "paper_active") and state.execute("SELECT 1 FROM paper_active WHERE arxiv_id=?", (aid,)).fetchone():
                raise RepairError("paper_active exists; managed writer owns this document")
            if _exists(state, "paper_publications"):
                cols = {r[1] for r in state.execute("PRAGMA table_info(paper_publications)")}
                if {"arxiv_id", "revision"} <= cols and state.execute(
                    "SELECT 1 FROM paper_publications WHERE arxiv_id=? AND revision IS NOT NULL LIMIT 1", (aid,)).fetchone():
                    raise RepairError("publication_revision exists; managed writer owns this document")
                if "arxiv_id" in cols and state.execute("SELECT 1 FROM paper_publications WHERE arxiv_id=? LIMIT 1", (aid,)).fetchone():
                    raise RepairError("paper_publications entry exists; managed writer owns this document")
            # Caller supplies the resolved migration payload directories; production
            # cache roots may intentionally be symlinked transport mounts.
            safe_name = document_cache.safe_id(aid) + ".json"
            cache_path = Path(cache_dir).resolve() / safe_name
            manifest_path = Path(section_cache_dir).resolve() / safe_name
            if cache_path.parent != Path(cache_dir).resolve() or not cache_path.is_file() or cache_path.is_symlink():
                raise RepairError("cache file is not a regular file in the supplied resolved directory")
            before_cache = document_cache.read_bounded(cache_path)
            chunks = json.loads(before_cache)
            by_index = {c.get("chunk_index"): c for c in chunks}
            if len(by_index) != len(chunks):
                raise RepairError("cache chunk_index values are not unique")
            supplied_labels = None
            if cache_labels_by_paper is not None:
                supplied_labels = {}
                for label in cache_labels_by_paper[aid]:
                    index = label.get("chunk_index")
                    matches_before = (preserve_invalid_unchanged_before_image and type(index) is int
                                      and index in by_index
                                      and label.get("section_title") == by_index[index].get("section_title", "")
                                      and label.get("section_type") == by_index[index].get("section_type", "other"))
                    valid_label = (document_cache._label(label.get("section_title"))
                                   and isinstance(label.get("section_type"), str)
                                   and label.get("section_type") in document_cache.SECTION_TYPES)
                    if (type(index) is not int or index in supplied_labels
                            or (not valid_label and not matches_before)):
                        raise RepairError("invalid or duplicate complete cache label record")
                    supplied_labels[index] = (label["section_title"], label["section_type"])
                if set(supplied_labels) != set(by_index):
                    raise RepairError("complete cache labels must cover every cache chunk index")
            expected_indices = None
            if expected_indexed_indices_by_paper is not None:
                raw_indices = expected_indexed_indices_by_paper[aid]
                if (not isinstance(raw_indices, (list, tuple)) or any(type(i) is not int for i in raw_indices)
                        or len(set(raw_indices)) != len(raw_indices)):
                    raise RepairError("expected indexed chunk indices must be unique integers")
                expected_indices = set(raw_indices)
                if not expected_indices <= set(by_index):
                    raise RepairError("expected indexed set contains a chunk absent from cache")
            frozen_rowids = None
            if complete_fts_rowids_by_paper is not None:
                frozen_rowids = complete_fts_rowids_by_paper[aid]
                if (not isinstance(frozen_rowids, (list, tuple))
                        or any(type(rid) is not int or rid <= 0 for rid in frozen_rowids)
                        or len(set(frozen_rowids)) != len(frozen_rowids)):
                    raise RepairError("complete FTS row IDs must be unique positive integers")
            points = _qdrant_points(qdrant, collection, aid)
            points_by_id = {_point_id(p): p for p in points}
            if len(points_by_id) != len(points):
                raise RepairError("duplicate Qdrant point IDs for paper")
            selected = {}
            seen_rowids = set()
            for item in items:
                if item["rowid"] in seen_rowids or item["point_id"] in selected:
                    raise RepairError("duplicate rowid or point id in supplied plan")
                seen_rowids.add(item["rowid"]); selected[item["point_id"]] = item
                row = fts.execute("SELECT text,point_id,arxiv_id,section_type FROM chunks WHERE rowid=?", (item["rowid"],)).fetchone()
                if not row or (row[1], row[2]) != (item["point_id"], aid):
                    raise RepairError("prepared FTS row identity mismatch")
                point = points_by_id.get(item["point_id"])
                if point is None or _payload(point).get("text") != row[0]:
                    raise RepairError("Qdrant point missing or text differs from FTS")
                if "publication_revision" in _payload(point):
                    raise RepairError("Qdrant publication_revision exists; managed writer owns this document")
                idx = _payload(point).get("chunk_index")
                chunk = by_index.get(idx)
                if chunk is None or chunk.get("text") != row[0]:
                    raise RepairError("Qdrant point does not map to cache chunk")
                if (row[3] != chunk.get("section_type", "other")
                        or _payload(point).get("section_type") != row[3]
                        or _payload(point).get("section_title") != chunk.get("section_title", "")):
                    raise RepairError("current cache, FTS, and Qdrant labels disagree")
                item["chunk_index"] = idx
                valid_label = (document_cache._label(item["section_title"])
                               and isinstance(item["section_type"], str)
                               and item["section_type"] in document_cache.SECTION_TYPES)
                if (not valid_label and (not preserve_invalid_unchanged_before_image
                        or (item["section_title"], item["section_type"])
                        != (chunk.get("section_title", ""), chunk.get("section_type", "other")))):
                    raise RepairError("invalid changed section label")
                if supplied_labels is not None and supplied_labels.get(idx) != (item["section_title"], item["section_type"]):
                    raise RepairError("live change disagrees with complete cache labels")
                item["before"] = {"section_title": chunk.get("section_title", ""),
                                  "section_type": chunk.get("section_type", "other"),
                                  "fts_section_type": row[3],
                                  "qdrant_section_title": _payload(point).get("section_title"),
                                  "qdrant_section_type": _payload(point).get("section_type"),
                                  "qdrant_stable_sha256": _stable_payload(_payload(point)),
                                  "fts_stable_sha256": _fts_stable(_fts_tuple(fts, item["rowid"]))}
            # The complete Qdrant point set must map uniquely to the cached chunk list.
            point_map = {_point_id(p): _payload(p) for p in points}
            point_indices = [p.get("chunk_index") for p in point_map.values()]
            exact_indices = expected_indices if expected_indices is not None else set(by_index)
            if (len(point_map) != len(points) or len(set(point_indices)) != len(point_indices)
                    or set(point_indices) != exact_indices
                    or any(p.get("text") is None or by_index[p.get("chunk_index")].get("text") != p.get("text")
                           for p in point_map.values())):
                raise RepairError("Qdrant paper points do not map uniquely to cache")
            if any("publication_revision" in p for p in point_map.values()):
                raise RepairError("Qdrant publication_revision exists; managed writer owns this document")
            if frozen_rowids is not None:
                if len(frozen_rowids) != len(points):
                    raise RepairError("complete FTS row count differs from Qdrant point count")
                fts_by_id = {}
                inventory = []
                for rowid in frozen_rowids:
                    row = _fts_tuple(fts, rowid)
                    if not row or row[2] != aid or row[1] not in point_map or row[1] in fts_by_id:
                        raise RepairError("complete FTS row identity map differs from Qdrant")
                    payload = point_map[row[1]]
                    idx = payload.get("chunk_index")
                    if (idx not in by_index or by_index[idx].get("text") != row[0]
                            or payload.get("text") != row[0] or row[3] != payload.get("section_type")):
                        raise RepairError("complete FTS text/type does not match cache and Qdrant")
                    fts_by_id[row[1]] = rowid
                    inventory.append({"rowid": rowid, "point_id": row[1], "chunk_index": idx,
                                      "section_type": row[3], "stable_sha256": _fts_stable(row)})
                if set(fts_by_id) != set(point_map):
                    raise RepairError("complete FTS point set differs from Qdrant")
                changed_by_pid = {item["point_id"]: item for item in items}
                if supplied_labels is not None:
                    for pid, payload in point_map.items():
                        idx = payload["chunk_index"]
                        target = supplied_labels[idx]
                        if ((payload.get("section_title"), payload.get("section_type")) != target
                                or _fts_tuple(fts, fts_by_id[pid])[3] != target[1]):
                            item = changed_by_pid.get(pid)
                            if item is None or (item["section_title"], item["section_type"]) != target:
                                raise RepairError("live label difference missing from sparse changes")
            else:
                inventory = []
            after_chunks = json.loads(before_cache)
            after_by_index = {c.get("chunk_index"): c for c in after_chunks}
            if supplied_labels is not None:
                for index, labels in supplied_labels.items():
                    after_by_index[index]["section_title"], after_by_index[index]["section_type"] = labels
            for item in items:
                chunk = after_by_index[item["chunk_index"]]
                chunk["section_title"] = item["section_title"]
                chunk["section_type"] = item["section_type"]
            after_cache = _json_bytes(after_chunks)
            before_manifest = after_manifest = None
            if manifest and manifest_path.exists():
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    raise RepairError("manifest is not a regular file in the supplied resolved directory")
                before_manifest = document_cache.read_bounded(manifest_path)
                if (source_bytes_by_paper is None or aid not in source_bytes_by_paper
                        or expected_source_sha256_by_paper is None or aid not in expected_source_sha256_by_paper
                        or source_route_by_paper is None or aid not in source_route_by_paper):
                    raise RepairError("existing manifest repair requires frozen source bytes, hash, and route")
                after_manifest = _manifest_after(before_manifest, aid, before_cache, after_cache,
                    source_bytes=source_bytes_by_paper[aid],
                    expected_source_sha256=expected_source_sha256_by_paper[aid],
                    source_route=source_route_by_paper[aid])
            unresolved = (unresolved_chunks_by_paper or {}).get(aid, [])
            papers.append({"arxiv_id": aid, "cache_path": str(cache_path), "manifest_path": str(manifest_path) if before_manifest is not None else None,
                "cache_before": base64.b64encode(before_cache).decode(), "cache_after": base64.b64encode(after_cache).decode(),
                "manifest_before": base64.b64encode(before_manifest).decode() if before_manifest is not None else None,
                "manifest_after": base64.b64encode(after_manifest).decode() if after_manifest is not None else None,
                "unresolved_chunks": unresolved,
                "changes": items, "expected_indexed_indices": sorted(exact_indices),
                "fts_inventory": inventory,
                "qdrant_inventory": [{"point_id": pid, "chunk_index": payload.get("chunk_index"),
                    "section_title": payload.get("section_title"), "section_type": payload.get("section_type"),
                    "stable_sha256": _stable_payload(payload)} for pid, payload in point_map.items()]})
        plan = {"schema": 1, "kind": "section-label-repair", "id": str(uuid.uuid4()), "phase": "prepared",
                "state_db": str(state_db), "fts_db": str(fts_db), "collection": collection, "papers": papers}
        _atomic(journal_path, _json_bytes(plan))
        return plan
    finally:
        state.close(); fts.close()


def _verify(plan, qdrant, expected_phase):
    state = _connect(plan["state_db"], readonly=True); fts = _connect(plan["fts_db"], readonly=True)
    try:
        for paper in plan["papers"]:
            aid = paper["arxiv_id"]
            status = state.execute("SELECT status FROM papers WHERE arxiv_id=?", (aid,)).fetchone()
            if not status or status[0] != "done":
                raise RepairError("paper status changed from done during repair")
            if _exists(state, "paper_active") and state.execute("SELECT 1 FROM paper_active WHERE arxiv_id=?", (aid,)).fetchone():
                raise RepairError("paper became managed during repair")
            if _exists(state, "paper_publications") and state.execute("SELECT 1 FROM paper_publications WHERE arxiv_id=? LIMIT 1", (aid,)).fetchone():
                raise RepairError("publication entry appeared during repair")
            want_after = expected_phase == "rollback"
            for key in ("cache", "manifest"):
                path = paper.get(key + "_path")
                if path:
                    wanted = base64.b64decode(paper[key + ("_after" if want_after else "_before")])
                    if Path(path).read_bytes() != wanted:
                        raise RepairError(f"{key} changed concurrently")
            pts = _verified_point_map(qdrant, plan["collection"], aid,
                                      paper["expected_indexed_indices"])
            change_by_pid = {c["point_id"]: c for c in paper["changes"]}
            for original in paper["qdrant_inventory"]:
                p = pts.get(original["point_id"])
                if not p or _payload(p).get("chunk_index") != original["chunk_index"]:
                    raise RepairError("Qdrant inventory identity changed")
                payload = _payload(p)
                item = change_by_pid.get(original["point_id"])
                expected_labels = ((item["section_title"], item["section_type"]) if want_after and item
                                   else (original["section_title"], original["section_type"]))
                if (payload.get("section_title"), payload.get("section_type")) != expected_labels:
                    raise RepairError("Qdrant inventory labels changed")
                if _stable_payload(payload) != original["stable_sha256"]:
                    raise RepairError("Qdrant inventory payload changed")
            if paper["fts_inventory"]:
                for original in paper["fts_inventory"]:
                    row = _fts_tuple(fts, original["rowid"])
                    item = change_by_pid.get(original["point_id"])
                    expected_type = item["section_type"] if want_after and item else original["section_type"]
                    if (not row or (row[1], row[2], row[3]) !=
                            (original["point_id"], aid, expected_type)
                            or _fts_stable(row) != original["stable_sha256"]):
                        raise RepairError("complete FTS inventory changed")
            for c in paper["changes"]:
                want = c["section_type"] if want_after else c["before"]["fts_section_type"]
                row = _fts_tuple(fts, c["rowid"])
                if (not row or (row[1], row[2], row[3]) != (c["point_id"], aid, want)
                        or _fts_stable(row) != c["before"]["fts_stable_sha256"]):
                    raise RepairError("FTS row changed concurrently")
                p = pts.get(c["point_id"])
                if not p:
                    raise RepairError("Qdrant point missing")
                payload = _payload(p)
                exp_title = c["section_title"] if want_after else c["before"]["qdrant_section_title"]
                exp_type = c["section_type"] if want_after else c["before"]["qdrant_section_type"]
                if (payload.get("section_title"), payload.get("section_type")) != (exp_title, exp_type):
                    raise RepairError("Qdrant labels changed concurrently")
                if _stable_payload(payload) != c["before"]["qdrant_stable_sha256"]:
                    raise RepairError("Qdrant payload changed concurrently")
    finally:
        state.close(); fts.close()


def apply_plan(journal_path, *, qdrant):
    plan = _load(journal_path)
    if plan["phase"] != "prepared":
        raise RepairError("journal is not in prepared phase")
    _verify(plan, qdrant, "apply")
    plan["journal_path"] = str(journal_path)
    plan["phase"] = "applying"
    _atomic(journal_path, _json_bytes(plan))
    fts = _connect(plan["fts_db"])
    failure = None
    try:
        for paper in plan["papers"]:
            _write_payload_grouped(qdrant, plan["collection"], paper["changes"],
                                   lambda c: (c["section_title"], c["section_type"]))
            plan["phase"] = "qdrant_applied"; _atomic(journal_path, _json_bytes(plan))
            for c in paper["changes"]:
                cur = fts.execute("UPDATE chunks SET section_type=? WHERE rowid=? AND point_id=? AND arxiv_id=? AND section_type=?",
                            (c["section_type"], c["rowid"], c["point_id"], paper["arxiv_id"], c["before"]["fts_section_type"]))
                if cur.rowcount != 1: raise RepairError("FTS exact-row update did not affect one row")
            fts.commit()
            plan["phase"] = "fts_applied"; _atomic(journal_path, _json_bytes(plan))
            _atomic(paper["cache_path"], base64.b64decode(paper["cache_after"]))
            if paper["manifest_path"]:
                _atomic(paper["manifest_path"], base64.b64decode(paper["manifest_after"]))
            plan["phase"] = "cache_applied"; _atomic(journal_path, _json_bytes(plan))
        plan["phase"] = "applied"; _atomic(journal_path, _json_bytes(plan))
    except Exception:
        failure = True
        raise
    finally:
        fts.close()
        if failure:
            _verify_partial(plan, qdrant)
            _restore(plan, qdrant)
    _verify(plan, qdrant, "rollback")
    return plan


def _restore(plan, qdrant):
    fts = _connect(plan["fts_db"])
    try:
        for paper in plan["papers"]:
            _write_payload_grouped(qdrant, plan["collection"], paper["changes"],
                lambda c: (c["before"]["qdrant_section_title"], c["before"]["qdrant_section_type"]))
            for c in paper["changes"]:
                old = c["before"]
                cur = fts.execute("UPDATE chunks SET section_type=? WHERE rowid=? AND point_id=? AND arxiv_id=?",
                            (old["fts_section_type"], c["rowid"], c["point_id"], paper["arxiv_id"]))
                if cur.rowcount != 1: raise RepairError("FTS exact-row rollback did not affect one row")
            fts.commit()
            _atomic(paper["cache_path"], base64.b64decode(paper["cache_before"]))
            if paper["manifest_path"]:
                _atomic(paper["manifest_path"], base64.b64decode(paper["manifest_before"]))
        plan["phase"] = "rolled_back"; _atomic(plan["journal_path"], _json_bytes(plan))
    finally:
        fts.close()


def rollback_plan(journal_path, *, qdrant):
    plan = _load(journal_path)
    if plan["phase"] not in {"applying", "applied", "cache_applied", "fts_applied", "qdrant_applied"}:
        raise RepairError("journal has no in-progress or completed apply phase")
    # Validate each surface against either its before or after-image so a process
    # interrupted between phase writes can still be safely compensated.
    _verify_partial(plan, qdrant)
    plan["journal_path"] = str(journal_path)
    _restore(plan, qdrant)
    return plan


def _verify_partial(plan, qdrant):
    state = _connect(plan["state_db"], readonly=True); fts = _connect(plan["fts_db"], readonly=True)
    try:
        for paper in plan["papers"]:
            aid = paper["arxiv_id"]
            status = state.execute("SELECT status FROM papers WHERE arxiv_id=?", (aid,)).fetchone()
            if not status or status[0] != "done":
                raise RepairError("paper status changed from done during repair")
            if _exists(state, "paper_active") and state.execute("SELECT 1 FROM paper_active WHERE arxiv_id=?", (aid,)).fetchone():
                raise RepairError("paper became managed during repair")
            if _exists(state, "paper_publications") and state.execute("SELECT 1 FROM paper_publications WHERE arxiv_id=? LIMIT 1", (aid,)).fetchone():
                raise RepairError("publication entry appeared during repair")
            for key in ("cache", "manifest"):
                path = paper.get(key + "_path")
                if path:
                    current = Path(path).read_bytes()
                    if current not in (base64.b64decode(paper[key + "_before"]), base64.b64decode(paper[key + "_after"])):
                        raise RepairError(f"{key} changed concurrently")
            pts = _verified_point_map(qdrant, plan["collection"], aid,
                                      paper["expected_indexed_indices"])
            change_by_pid = {c["point_id"]: c for c in paper["changes"]}
            for original in paper["qdrant_inventory"]:
                point = pts.get(original["point_id"])
                if not point or _payload(point).get("chunk_index") != original["chunk_index"]:
                    raise RepairError("Qdrant inventory identity changed")
                payload = _payload(point)
                item = change_by_pid.get(original["point_id"])
                before = (original["section_title"], original["section_type"])
                after = (item["section_title"], item["section_type"]) if item else before
                if (payload.get("section_title"), payload.get("section_type")) not in {before, after}:
                    raise RepairError("Qdrant inventory labels changed concurrently")
                if _stable_payload(payload) != original["stable_sha256"]:
                    raise RepairError("Qdrant inventory payload changed concurrently")
            if paper["fts_inventory"]:
                for original in paper["fts_inventory"]:
                    row = _fts_tuple(fts, original["rowid"])
                    item = change_by_pid.get(original["point_id"])
                    allowed_types = {original["section_type"]}
                    if item: allowed_types.add(item["section_type"])
                    if (not row or row[1:3] != (original["point_id"], aid)
                            or row[3] not in allowed_types
                            or _fts_stable(row) != original["stable_sha256"]):
                        raise RepairError("complete FTS inventory changed concurrently")
            for c in paper["changes"]:
                row = _fts_tuple(fts, c["rowid"])
                before_type, after_type = c["before"]["fts_section_type"], c["section_type"]
                if (not row or row[1:3] != (c["point_id"], aid) or row[3] not in (before_type, after_type)
                        or _fts_stable(row) != c["before"]["fts_stable_sha256"]):
                    raise RepairError("FTS row changed concurrently")
                point = pts.get(c["point_id"])
                if not point: raise RepairError("Qdrant point missing")
                p = _payload(point)
                before = (c["before"]["qdrant_section_title"], c["before"]["qdrant_section_type"])
                after = (c["section_title"], c["section_type"])
                if (p.get("section_title"), p.get("section_type")) not in (before, after):
                    raise RepairError("Qdrant labels changed concurrently")
                if _stable_payload(p) != c["before"]["qdrant_stable_sha256"]:
                    raise RepairError("Qdrant payload changed concurrently")
    finally:
        state.close(); fts.close()
