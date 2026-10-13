"""Compare done-paper caches with the frozen FTS ID rowmap; never writes corpus data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import document_cache, extractor

SCHEMA_VERSION = 1
NAMESPACE = uuid.NAMESPACE_URL


def _readonly(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"database missing: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return json.dumps([str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns], separators=(",", ":"))


def _complete(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _rowmap_groups(conn):
    cursor = conn.execute("SELECT arxiv_id,point_id FROM rows ORDER BY arxiv_id,rowid")
    current_id, point_ids = None, []
    for aid, point_id in cursor:
        if current_id is not None and aid != current_id:
            yield current_id, point_ids
            point_ids = []
        current_id = aid
        point_ids.append(point_id)
    if current_id is not None:
        yield current_id, point_ids


def _cache_map(cache, paper_id):
    if not isinstance(cache, list) or not cache or any(not isinstance(c, dict) for c in cache):
        return None, "malformed_cache"
    indices = [c.get("chunk_index") for c in cache]
    if any(type(i) is not int or i < 0 for i in indices) or len(set(indices)) != len(indices):
        return None, "invalid_chunk_indices"
    ids = {}
    for chunk in cache:
        index = chunk["chunk_index"]
        point_id = str(uuid.uuid5(NAMESPACE, f"{paper_id}#{index}"))
        ids[point_id] = (index, bool(extractor.should_embed(chunk)))
    return ids, "valid"


def _analyze(paper_id, fts_ids, cache_info, cache_dir):
    row = {"paper_id": paper_id, "census_cache_state": cache_info["cache_state"],
           "census_cache_sha256": cache_info["cache_sha256"],
           "census_fingerprint": cache_info["input_fingerprint"],
           "chunk_count": cache_info["chunk_count"], "cache_state": "unavailable",
           "cache_sha256": None, "fts_row_count": len(fts_ids),
           "fts_unique_point_count": len(set(fts_ids)),
           "fts_duplicate_point_id_count": len(fts_ids) - len(set(fts_ids)),
           "fts_cache_mapped_row_count": 0, "fts_unmapped_row_count": 0,
           "fts_current_expected_row_count": 0, "fts_currently_filtered_row_count": 0,
           "cache_current_expected_count": 0, "cache_currently_filtered_count": 0,
           "cache_backed_missing_current_count": 0, "cache_backed_missing_current_indices_json": "[]",
           "current_filter_excluded_absent_count": 0,
           "current_filter_excluded_absent_indices_json": "[]",
           "fts_unmapped_point_ids_json": "[]", "projection_status": "not_checked"}
    if cache_info["cache_state"] != "present":
        row["projection_status"] = "census_cache_unavailable"
        return row
    name = document_cache.safe_id(paper_id) + ".json"
    path = Path(cache_dir) / name
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        row["cache_state"] = "unsafe_path"
        row["projection_status"] = "cache_unsafe"
        return row
    try:
        raw = path.read_bytes()
    except OSError as exc:
        row["cache_state"] = "unreadable"
        row["projection_status"] = "cache_unreadable"
        return row
    cache_hash = hashlib.sha256(raw).hexdigest()
    row["cache_sha256"] = cache_hash
    if cache_hash != cache_info["cache_sha256"]:
        row["cache_state"] = "changed_since_census"
        row["projection_status"] = "cache_changed_since_census"
        return row
    try:
        cache = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        row["cache_state"] = "malformed_json"
        row["projection_status"] = "cache_malformed"
        return row
    all_ids, identity_status = _cache_map(cache, paper_id)
    if all_ids is None:
        row["cache_state"] = identity_status
        row["projection_status"] = "cache_identity_invalid"
        return row
    row["cache_state"] = "verified"
    row["cache_current_expected_count"] = sum(1 for _, include in all_ids.values() if include)
    row["cache_currently_filtered_count"] = len(all_ids) - row["cache_current_expected_count"]
    unique_fts = set(fts_ids)
    mapped = unique_fts & set(all_ids)
    current = {pid for pid, (_index, include) in all_ids.items() if include}
    filtered = set(all_ids) - current
    row["fts_cache_mapped_row_count"] = sum(1 for pid in fts_ids if pid in all_ids)
    row["fts_unmapped_row_count"] = len(fts_ids) - row["fts_cache_mapped_row_count"]
    row["fts_current_expected_row_count"] = sum(1 for pid in fts_ids if pid in current)
    row["fts_currently_filtered_row_count"] = sum(1 for pid in fts_ids if pid in filtered)
    missing = current - unique_fts
    filtered_absent = filtered - unique_fts
    row["cache_backed_missing_current_count"] = len(missing)
    row["cache_backed_missing_current_indices_json"] = json.dumps(
        sorted(all_ids[pid][0] for pid in missing), separators=(",", ":"))
    row["current_filter_excluded_absent_count"] = len(filtered_absent)
    row["current_filter_excluded_absent_indices_json"] = json.dumps(
        sorted(all_ids[pid][0] for pid in filtered_absent), separators=(",", ":"))
    unmapped = sorted(unique_fts - set(all_ids))
    row["fts_unmapped_point_ids_json"] = json.dumps(unmapped, separators=(",", ":"))
    if row["fts_duplicate_point_id_count"] or row["fts_unmapped_row_count"]:
        row["projection_status"] = "duplicate_or_unmapped_fts_ids"
    elif missing:
        row["projection_status"] = "cache_backed_current_projection_missing"
    elif unique_fts & filtered:
        row["projection_status"] = "fts_contains_currently_filtered_cache_points"
    else:
        row["projection_status"] = "aligned_with_current_filter"
    return row


def _report_db(path, census_id, rowmap_id):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS papers(
        paper_id TEXT PRIMARY KEY,census_fingerprint TEXT NOT NULL,
        census_cache_state TEXT,census_cache_sha256 TEXT,chunk_count INTEGER,
        cache_state TEXT NOT NULL,cache_sha256 TEXT,fts_row_count INTEGER NOT NULL,
        fts_unique_point_count INTEGER NOT NULL,fts_duplicate_point_id_count INTEGER NOT NULL,
        fts_cache_mapped_row_count INTEGER NOT NULL,fts_unmapped_row_count INTEGER NOT NULL,
        fts_current_expected_row_count INTEGER NOT NULL,fts_currently_filtered_row_count INTEGER NOT NULL,
        cache_current_expected_count INTEGER NOT NULL,cache_currently_filtered_count INTEGER NOT NULL,
        cache_backed_missing_current_count INTEGER NOT NULL,
        cache_backed_missing_current_indices_json TEXT NOT NULL,
        current_filter_excluded_absent_count INTEGER NOT NULL,
        current_filter_excluded_absent_indices_json TEXT NOT NULL,
        fts_unmapped_point_ids_json TEXT NOT NULL,
        projection_status TEXT NOT NULL,scanned_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS projection_status_idx ON papers(projection_status);
    """)
    existing = dict(conn.execute("SELECT key,value FROM meta"))
    for key, value in (("schema_version", "1"), ("census_identity", census_id), ("rowmap_identity", rowmap_id)):
        if key in existing and existing[key] != value:
            if key != "schema_version":
                conn.close()
                raise ValueError(f"report is bound to a different {key}")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value))
    conn.commit()
    return conn


def _summary(conn, scanned, reused, done, rowmap_orphan_rows, rowmap_orphan_papers, complete):
    statuses = {r[0]: r[1] for r in conn.execute(
        "SELECT projection_status,COUNT(*) FROM papers GROUP BY projection_status ORDER BY projection_status")}
    totals = conn.execute("SELECT COUNT(*),COALESCE(SUM(fts_row_count),0),"
                          "COALESCE(SUM(cache_backed_missing_current_count),0),"
                          "COALESCE(SUM(current_filter_excluded_absent_count),0),"
                          "COALESCE(SUM(fts_unmapped_row_count),0),"
                          "COALESCE(SUM(fts_duplicate_point_id_count),0) FROM papers").fetchone()
    return {"complete": bool(complete), "done_papers_in_census": done,
            "reported_papers": totals[0], "scanned_this_run": scanned, "reused_this_run": reused,
            "rowmap_orphan_papers": rowmap_orphan_papers, "rowmap_orphan_rows": rowmap_orphan_rows,
            "projection_statuses": statuses, "fts_rows_for_done_papers": totals[1],
            "cache_backed_missing_current_projection": totals[2],
            "current_filter_excluded_and_absent_from_fts": totals[3],
            "unmapped_fts_rows": totals[4], "duplicate_fts_rows": totals[5],
            "qdrant_checked": False}


def run(data_dir, census_path, rowmap_path, report_path, *, chunk_dir=None, limit=None,
        commit_every=500):
    if (limit is not None and limit < 1) or commit_every < 1:
        raise ValueError("limit and commit_every must be positive")
    data_dir = Path(data_dir).resolve()
    census_path, rowmap_path, report_path = map(lambda p: Path(p).resolve(),
                                                (census_path, rowmap_path, report_path))
    chunk_dir = Path(chunk_dir).resolve() if chunk_dir else data_dir / "chunk_cache"
    if report_path in {census_path, rowmap_path, data_dir / "state.db", data_dir / "fts.db"}:
        raise ValueError("report must be separate from source databases")
    census = _readonly(census_path)
    rowmap = _readonly(rowmap_path)
    if _complete(census, "run_complete") != "1":
        raise ValueError("the whole-corpus census is not complete")
    if _complete(rowmap, "complete") != "1":
        raise ValueError("the FTS rowmap is not complete")
    census_id, rowmap_id = _identity(census_path), _identity(rowmap_path)
    report = _report_db(report_path, census_id, rowmap_id)
    scanned = reused = done = 0
    orphan_rows = orphan_papers = 0
    complete = False
    try:
        stream = iter(_rowmap_groups(rowmap))
        fts_group = next(stream, None)
        papers = census.execute("SELECT paper_id,input_fingerprint,cache_state,cache_sha256,chunk_count "
                                "FROM papers ORDER BY paper_id")
        for paper in papers:
            aid = paper["paper_id"]
            while fts_group is not None and fts_group[0] < aid:
                orphan_papers += 1
                orphan_rows += len(fts_group[1])
                fts_group = next(stream, None)
            fts_ids = []
            if fts_group is not None and fts_group[0] == aid:
                fts_ids = fts_group[1]
                fts_group = next(stream, None)
            done += 1
            info = {"input_fingerprint": paper["input_fingerprint"],
                    "cache_state": paper["cache_state"], "cache_sha256": paper["cache_sha256"],
                    "chunk_count": paper["chunk_count"]}
            item = _analyze(aid, fts_ids, info, chunk_dir)
            prior = report.execute("SELECT census_fingerprint,cache_state,cache_sha256 FROM papers WHERE paper_id=?",
                                   (aid,)).fetchone()
            if (prior and prior[0] == paper["input_fingerprint"]
                    and prior[1] == item["cache_state"] and prior[2] == item["cache_sha256"]):
                reused += 1
            else:
                item["census_fingerprint"] = paper["input_fingerprint"]
                item["scanned_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                columns = list(item)
                report.execute(f"INSERT OR REPLACE INTO papers({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                               [item[c] for c in columns])
                scanned += 1
            if done % commit_every == 0:
                report.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('cursor',?)", (aid,))
                report.commit()
            if limit is not None and done >= limit:
                break
        else:
            complete = True
        if complete:
            while fts_group is not None:
                orphan_papers += 1
                orphan_rows += len(fts_group[1])
                fts_group = next(stream, None)
        report.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('run_complete',?)", ("1" if complete else "0",))
        report.commit()
        return _summary(report, scanned, reused, done, orphan_rows, orphan_papers, complete)
    finally:
        report.close()
        census.close()
        rowmap.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--census", required=True)
    parser.add_argument("--rowmap", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--chunk-dir")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    result = run(args.data_dir, args.census, args.rowmap, args.report,
                 chunk_dir=args.chunk_dir, limit=args.limit)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
