"""Extend a completed quality census with hash-guarded source heading labels."""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from ops import source_heading_labels
from pipeline import document_cache

MAX_SOURCE_BYTES = 128 * 1024 * 1024


def _file_bytes(path, limit):
    path = Path(path)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("unsafe_symlink")
    stat = path.stat()
    if not path.is_file():
        raise ValueError("not_regular_file")
    if stat.st_size > limit:
        raise ValueError("too_large")
    data = path.read_bytes()
    if len(data) != stat.st_size:
        raise ValueError("changed_during_read")
    return data


def _input_paths(row, cache_dir, fulltext_cache_dir, latex_cache_dir):
    name = document_cache.safe_id(row["paper_id"])
    cache_path = Path(cache_dir) / (name + ".json")
    if row["source_location"] == "fulltext_cache":
        source_path = Path(fulltext_cache_dir) / (name + ".tex")
    elif row["source_location"] == "latex_cache":
        source_path = Path(latex_cache_dir) / name / "source.tex"
    else:
        raise ValueError("unsupported_source_location")
    return cache_path, source_path


def _ensure_columns(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
    for name, declaration in (
        ("original_candidate_status", "TEXT"),
        ("labels_source_verified", "INTEGER NOT NULL DEFAULT 0"),
        ("heading_label_status", "TEXT"),
        ("heading_label_fingerprint", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {declaration}")
    conn.commit()


def _fingerprint(cache_hash, source_hash, census_fingerprint):
    value = json.dumps([census_fingerprint, cache_hash, source_hash], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def _evaluate(row, cache_dir, fulltext_cache_dir, latex_cache_dir):
    try:
        cache_path, source_path = _input_paths(row, cache_dir, fulltext_cache_dir, latex_cache_dir)
    except (ValueError, document_cache.ManifestInvalid) as exc:
        return {"status": str(exc) if str(exc) else "unsafe_paper_id", "fingerprint": None}
    try:
        cache_bytes = _file_bytes(cache_path, document_cache.MAX_BYTES)
        source_bytes = _file_bytes(source_path, MAX_SOURCE_BYTES)
    except (OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return {"status": reason, "fingerprint": None}
    cache_hash = hashlib.sha256(cache_bytes).hexdigest()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    fingerprint = _fingerprint(cache_hash, source_hash, row["input_fingerprint"])
    if cache_hash != row["cache_sha256"] or source_hash != row["source_sha256"]:
        return {"status": "census_hash_mismatch", "fingerprint": fingerprint}
    try:
        cache = json.loads(cache_bytes)
        source_text = source_bytes.decode("utf-8")
    except (UnicodeError, json.JSONDecodeError) as exc:
        return {"status": "invalid_input_encoding_or_json", "fingerprint": fingerprint,
                "error": type(exc).__name__}
    result = source_heading_labels.candidate(cache, source_text)
    result["fingerprint"] = fingerprint
    return result


def run(census_path, *, cache_dir, fulltext_cache_dir, latex_cache_dir,
        commit_every=250, limit=None):
    if commit_every < 1 or (limit is not None and limit < 1):
        raise ValueError("commit_every and limit must be positive")
    census_path = Path(census_path)
    conn = sqlite3.connect(census_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        meta = dict(conn.execute("SELECT key,value FROM meta"))
        if meta.get("run_complete") != "1":
            raise ValueError("quality census must be complete before extension")
        _ensure_columns(conn)
        rows = conn.execute(
            "SELECT paper_id,input_fingerprint,cache_sha256,source_sha256,source_location,"
            "candidate_status,candidate_changes_json,candidate_label_map_json,"
            "original_candidate_status,heading_label_status,heading_label_fingerprint "
            "FROM papers WHERE candidate_status='identity_or_text_changed' "
            "OR original_candidate_status='identity_or_text_changed' ORDER BY paper_id").fetchall()
        processed = reused = verified = failed = 0
        cursor = conn.cursor()
        for row in rows:
            result = _evaluate(row, cache_dir, fulltext_cache_dir, latex_cache_dir)
            if (result.get("fingerprint") and row["heading_label_fingerprint"]
                    == result["fingerprint"] and row["heading_label_status"] is not None):
                reused += 1
                continue
            status = result.get("status", "unverified")
            is_verified = bool(result.get("source_heading_verified"))
            if is_verified:
                changes = result["changes"]
                full_map = result["full_label_map"]
                cursor.execute(
                    "UPDATE papers SET candidate_status='heading_label_candidate',"
                    "original_candidate_status=COALESCE(original_candidate_status,'identity_or_text_changed'),"
                    "labels_source_verified=1,heading_label_status=?,heading_label_fingerprint=?,"
                    "label_change_count=?,candidate_changes_json=?,candidate_label_map_json=? "
                    "WHERE paper_id=?",
                    (status, result["fingerprint"], len(changes),
                     json.dumps(changes, ensure_ascii=False, separators=(",", ":")),
                     json.dumps(full_map, ensure_ascii=False, separators=(",", ":")), row["paper_id"]))
                verified += 1
            else:
                # Preserve original diagnostic and exact_text_ids; separately
                # record why heading-only recovery was not proven.
                cursor.execute(
                    "UPDATE papers SET original_candidate_status=COALESCE(original_candidate_status,?),"
                    "candidate_status=COALESCE(original_candidate_status,'identity_or_text_changed'),"
                    "labels_source_verified=0,heading_label_status=?,heading_label_fingerprint=?,"
                    "label_change_count=0,candidate_changes_json='[]',candidate_label_map_json='[]' "
                    "WHERE paper_id=?",
                    (row["candidate_status"], status,
                     result.get("fingerprint"), row["paper_id"]))
                failed += 1
            processed += 1
            if processed % commit_every == 0:
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('heading_label_cursor',?)",
                             (row["paper_id"],))
                conn.commit()
            if limit is not None and processed + reused >= limit:
                break
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('heading_label_last_run_at',?)",
                     (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
        conn.commit()
        counts = dict(conn.execute("SELECT heading_label_status,COUNT(*) FROM papers "
                                   "WHERE original_candidate_status='identity_or_text_changed' "
                                   "GROUP BY heading_label_status"))
        return {"processed": processed, "reused": reused, "verified": verified,
                "not_verified": failed, "heading_label_statuses": counts}
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", required=True)
    parser.add_argument("--chunk-dir", required=True)
    parser.add_argument("--fulltext-cache-dir", required=True)
    parser.add_argument("--latex-cache-dir", required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.census, cache_dir=args.chunk_dir,
                         fulltext_cache_dir=args.fulltext_cache_dir,
                         latex_cache_dir=args.latex_cache_dir, limit=args.limit),
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
