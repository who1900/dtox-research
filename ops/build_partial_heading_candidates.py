"""Build cache-preserving, source-anchored partial label candidates."""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from ops import source_heading_labels
from pipeline import document_cache

MAX_SOURCE_BYTES = 128 * 1024 * 1024
PDF_SOURCES = {"openalex-oa-pdf", "acl-pdf"}


def _readonly(path):
    path = Path(path)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _identity(path):
    path = Path(path).resolve()
    st = path.stat()
    return json.dumps([str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns], separators=(",", ":"))


def _read(path, limit):
    path = Path(path)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("unsafe_path")
    st = path.stat()
    if not path.is_file():
        raise ValueError("not_regular_file")
    if st.st_size > limit:
        raise ValueError("too_large")
    data = path.read_bytes()
    if len(data) != st.st_size:
        raise ValueError("changed_during_read")
    return data


def _paths(row, cache_dir, fulltext_dir, latex_dir):
    name = document_cache.safe_id(row["paper_id"])
    cache = Path(cache_dir) / (name + ".json")
    if row["source_location"] == "fulltext_cache":
        source = Path(fulltext_dir) / (name + ".tex")
    elif row["source_location"] == "latex_cache":
        source = Path(latex_dir) / name / "source.tex"
    else:
        raise ValueError("unsupported_source_location")
    return cache, source


def _propose(cache, source_text):
    sections = source_heading_labels._sections(source_text, "paper")
    labels = []
    changes = []
    mapped = ambiguous = unmatched = changed = 0
    for chunk in cache:
        text = chunk["text"]
        targets = {(section["section_title"], section["section_type"])
                   for section in sections if text and text in section["text"]}
        old_title = chunk.get("section_title", "")
        old_type = chunk.get("section_type", "other")
        if len(targets) == 1:
            title, kind = next(iter(targets))
            mapped += 1
            if (title, kind) != (old_title, old_type):
                changed += 1
                changes.append({"chunk_index": chunk["chunk_index"],
                                "section_title": title, "section_type": kind})
        elif targets:
            ambiguous += 1
            title, kind = old_title, old_type
        else:
            unmatched += 1
            title, kind = old_title, old_type
        labels.append({"chunk_index": chunk["chunk_index"],
                       "section_title": title, "section_type": kind})
    return {"full_labels": labels, "changes": changes, "mapped": mapped,
            "ambiguous": ambiguous, "unmatched": unmatched, "changed": changed}


def _setup_report(path, census_identity):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS papers(
        paper_id TEXT PRIMARY KEY,source_location TEXT NOT NULL,
        cache_sha256 TEXT,source_sha256 TEXT,candidate_status TEXT NOT NULL,
        exact_text_ids INTEGER NOT NULL,labels_source_verified INTEGER NOT NULL,
        partial_label_scope INTEGER NOT NULL,heading_label_status TEXT NOT NULL,
        mapped_chunk_count INTEGER NOT NULL,ambiguous_chunk_count INTEGER NOT NULL,
        unmatched_chunk_count INTEGER NOT NULL,current_changed_chunk_count INTEGER NOT NULL,
        candidate_changes_json TEXT NOT NULL,candidate_label_map_json TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,scanned_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS papers_candidate_status ON papers(candidate_status);
    """)
    old = dict(conn.execute("SELECT key,value FROM meta"))
    if old.get("schema_version") not in (None, "1"):
        conn.close()
        raise ValueError("partial-candidate report schema mismatch")
    if old.get("census_identity") not in (None, census_identity):
        conn.close()
        raise ValueError("partial-candidate report is bound to another census")
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('schema_version','1')")
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('census_identity',?)", (census_identity,))
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('partial_scope','uniquely_source_anchored_chunks_only')")
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('run_complete','0')")
    conn.commit()
    return conn


def run(census_path, report_path, *, cache_dir, fulltext_cache_dir, latex_cache_dir,
        commit_every=250, limit=None):
    if commit_every < 1 or (limit is not None and limit < 1):
        raise ValueError("commit_every and limit must be positive")
    census_path = Path(census_path).resolve()
    report_path = Path(report_path).resolve()
    if report_path == census_path:
        raise ValueError("partial-candidate output must be separate from original census")
    census = _readonly(census_path)
    report = None
    try:
        meta = dict(census.execute("SELECT key,value FROM meta"))
        if meta.get("run_complete") != "1":
            raise ValueError("original census must be complete")
        required = {"paper_id", "candidate_status", "heading_label_status", "cache_sha256",
                    "source_sha256", "source_location", "fulltext_source", "input_fingerprint"}
        columns = {r[1] for r in census.execute("PRAGMA table_info(papers)")}
        if not required <= columns:
            raise ValueError("original census schema lacks heading-extension fields")
        report = _setup_report(report_path, _identity(census_path))
        rows = census.execute(
            "SELECT paper_id,source_location,fulltext_source,cache_sha256,source_sha256,"
            "input_fingerprint FROM papers "
            "WHERE heading_label_status='unmatched_or_ambiguous_cached_heading' "
            "AND fulltext_source IN ('openalex-oa-pdf','acl-pdf') ORDER BY paper_id").fetchall()
        processed = reused = candidates = 0
        for row in rows:
            fingerprint = None
            try:
                cache_path, source_path = _paths(row, cache_dir, fulltext_cache_dir, latex_cache_dir)
                cache_bytes = _read(cache_path, document_cache.MAX_BYTES)
                source_bytes = _read(source_path, MAX_SOURCE_BYTES)
                cache_sha = hashlib.sha256(cache_bytes).hexdigest()
                source_sha = hashlib.sha256(source_bytes).hexdigest()
                fingerprint = hashlib.sha256(json.dumps(
                    [row["input_fingerprint"], cache_sha, source_sha], separators=(",", ":")).encode()).hexdigest()
                prior = report.execute("SELECT input_fingerprint FROM papers WHERE paper_id=?",
                                       (row["paper_id"],)).fetchone()
                if prior and prior[0] == fingerprint:
                    reused += 1
                    continue
                if cache_sha != row["cache_sha256"] or source_sha != row["source_sha256"]:
                    raise ValueError("census_hash_mismatch")
                cache = json.loads(cache_bytes)
                if (not isinstance(cache, list) or not cache
                        or any(not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str)
                               or type(chunk.get("chunk_index")) is not int or chunk["chunk_index"] < 0
                               for chunk in cache)
                        or len({chunk["chunk_index"] for chunk in cache}) != len(cache)):
                    raise ValueError("invalid_cache")
                for chunk in cache:
                    if (not isinstance(chunk.get("section_title", ""), str)
                            or not isinstance(chunk.get("section_type", "other"), str)):
                        raise ValueError("invalid_cache_labels")
                source_text = source_bytes.decode("utf-8")
                proposal = _propose(cache, source_text)
                eligible = bool(proposal["changed"])
                status = "partial_source_anchor_candidate" if eligible else "partial_no_label_changes"
                if eligible:
                    candidates += 1
                report.execute("INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    row["paper_id"], row["source_location"], cache_sha, source_sha,
                    "heading_label_candidate" if eligible else status, 0, int(eligible), 1,
                    status, proposal["mapped"], proposal["ambiguous"], proposal["unmatched"],
                    proposal["changed"], json.dumps(proposal["changes"], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(proposal["full_labels"], ensure_ascii=False, separators=(",", ":")),
                    fingerprint, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                status = str(exc) if isinstance(exc, ValueError) and str(exc) else type(exc).__name__
                report.execute("INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    row["paper_id"], row["source_location"], row["cache_sha256"], row["source_sha256"],
                    status, 0, 0, 1, status, 0, 0, 0, 0, "[]", "[]",
                    fingerprint or "", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            processed += 1
            if processed % commit_every == 0:
                report.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('cursor',?)", (row["paper_id"],))
                report.commit()
            if limit is not None and processed + reused >= limit:
                break
        complete = processed + reused >= len(rows)
        report.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('run_complete',?)", ("1" if complete else "0",))
        report.commit()
        aggregate = report.execute(
            "SELECT COUNT(*),COALESCE(SUM(candidate_status='heading_label_candidate'),0),"
            "COALESCE(SUM(mapped_chunk_count),0),COALESCE(SUM(ambiguous_chunk_count),0),"
            "COALESCE(SUM(unmatched_chunk_count),0),COALESCE(SUM(current_changed_chunk_count),0) "
            "FROM papers").fetchone()
        statuses = dict(report.execute("SELECT heading_label_status,COUNT(*) FROM papers GROUP BY heading_label_status"))
        return {"complete": complete, "selected": len(rows), "processed": processed,
                "reused": reused, "reported": aggregate[0], "candidates": aggregate[1],
                "mapped_chunks": aggregate[2], "ambiguous_chunks": aggregate[3],
                "unmatched_chunks": aggregate[4], "changed_chunks": aggregate[5], "statuses": statuses,
                "report": str(Path(report_path).resolve())}
    finally:
        if report is not None:
            report.close()
        census.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--chunk-dir", required=True)
    parser.add_argument("--fulltext-cache-dir", required=True)
    parser.add_argument("--latex-cache-dir", required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.census, args.report, cache_dir=args.chunk_dir,
                         fulltext_cache_dir=args.fulltext_cache_dir,
                         latex_cache_dir=args.latex_cache_dir, limit=args.limit),
                     ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
