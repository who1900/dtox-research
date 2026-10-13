"""Read-only, resumable census of completed corpus extraction artifacts.

The report SQLite file is an operational checkpoint. It stores no chunk or
source text; the source state database and corpus files are opened read-only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import document_cache, extractor

SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 128 * 1024 * 1024
SPEC_PREFIXES = ("eip:", "simd:", "gh:", "wp:")
MARKDOWN_PAPER_PREFIXES = ("acl:", "oa:", "pmlr:", "hal:")


def _readonly(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _file(path):
    path = Path(path)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return {"state": "unsafe_path", "path": path}
    try:
        st = path.stat()
    except FileNotFoundError:
        return {"state": "missing", "path": path}
    except OSError as exc:
        return {"state": "unreadable", "path": path, "error": type(exc).__name__}
    if not path.is_file():
        return {"state": "not_regular_file", "path": path}
    return {"state": "present", "path": path, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _file_fingerprint(info):
    return {key: info.get(key) for key in ("state", "size", "mtime_ns", "error")}


def _is_spec(paper_id):
    return str(paper_id).startswith(SPEC_PREFIXES)


def _is_markdown_route(paper_id, fulltext_source, parser_mode):
    source = (fulltext_source or "").lower()
    if source in {"abstract", "abstract-only", "abstract_fallback", "abstract-fallback"} or source.endswith(
            ("-abstract", "-abstract-fallback")):
        return False
    return (not _is_spec(paper_id) and (source == "oa-mirror-pdf"
            or source.endswith("-pdf") or str(paper_id).startswith(MARKDOWN_PAPER_PREFIXES)))


def _publication_fields(conn, paper_columns):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    joins, selects = [], []
    if "paper_publications" in tables:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_publications)")}
        if {"arxiv_id", "revision"} <= columns:
            joins.append("LEFT JOIN (SELECT arxiv_id,COUNT(*) publication_revision_count "
                         "FROM paper_publications GROUP BY arxiv_id) pub ON pub.arxiv_id=p.arxiv_id")
            selects.append("COALESCE(pub.publication_revision_count,0) AS publication_revision_count")
    if not selects:
        selects.append("0 AS publication_revision_count")
    if "paper_active" in tables:
        active_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_active)")}
        if "arxiv_id" in active_cols:
            selects.append("EXISTS(SELECT 1 FROM paper_active a WHERE a.arxiv_id=p.arxiv_id) AS active_owner")
    if "active_owner" not in " ".join(selects):
        selects.append("0 AS active_owner")
    return joins, selects


def _chunk_metrics(chunks):
    indices, texts = [], []
    replacement_count = 0
    malformed_chunk_count = 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            malformed_chunk_count += 1
            continue
        index = chunk.get("chunk_index")
        indices.append(index)
        text = chunk.get("text")
        if isinstance(text, str):
            texts.append(text)
            replacement_count += text.count("\ufffd")
        else:
            malformed_chunk_count += 1
    index_values = [value for value in indices if type(value) is int]
    duplicate_index_count = len(index_values) - len(set(index_values))
    out_of_order_count = sum(a >= b for a, b in zip(index_values, index_values[1:]))
    noncontiguous = (bool(index_values) and
                     (len(index_values) != len(indices) or index_values != list(range(len(indices)))))
    duplicate_text_count = len(texts) - len(set(texts))
    return {
        "chunk_count": len(chunks), "malformed_chunk_count": malformed_chunk_count,
        "replacement_count": replacement_count, "duplicate_index_count": duplicate_index_count,
        "out_of_order_index_count": out_of_order_count,
        "noncontiguous_index": int(noncontiguous), "duplicate_text_count": duplicate_text_count,
    }


def _candidate(cache, source_text, paper_id, fulltext_source, parser_mode):
    if not _is_markdown_route(paper_id, fulltext_source, parser_mode):
        return {"status": "not_applicable", "exact_text_ids": None, "label_change_count": 0,
                "changes": [], "error": None}
    kind = "spec" if _is_spec(paper_id) else "paper"
    try:
        parsed, _mode = extractor.build_chunks_markdown(source_text, document_kind=kind)
    except Exception as exc:
        return {"status": "parse_error", "exact_text_ids": None, "label_change_count": 0,
                "changes": [], "error": type(exc).__name__}
    if len(cache) != len(parsed):
        return {"status": "identity_or_text_changed", "exact_text_ids": False,
                "label_change_count": 0, "changes": [], "error": None}
    old_indices = [chunk.get("chunk_index") if isinstance(chunk, dict) else None for chunk in cache]
    if (any(type(index) is not int for index in old_indices)
            or len(set(old_indices)) != len(old_indices)):
        return {"status": "identity_or_text_changed", "exact_text_ids": False,
                "label_change_count": 0, "changes": [], "error": None}
    exact = True
    changed = []
    full_labels = []
    for old, new in zip(cache, parsed):
        if not isinstance(old, dict):
            exact = False
            break
        if any(old.get(key) != new.get(key) for key in
               ("chunk_index", "text", "element_type", "has_math", "has_algorithm")):
            exact = False
            break
        if old.get("section_title", "") != new.get("section_title", "") or old.get(
                "section_type", "other") != new.get("section_type", "other"):
            changed.append({"chunk_index": old.get("chunk_index"),
                            "section_title": new.get("section_title", ""),
                            "section_type": new.get("section_type", "other")})
        full_labels.append({"chunk_index": new.get("chunk_index"),
                            "section_title": new.get("section_title", ""),
                            "section_type": new.get("section_type", "other")})
    if not exact:
        return {"status": "identity_or_text_changed", "exact_text_ids": False,
                "label_change_count": 0, "changes": [], "error": None}
    return {"status": "candidate" if changed else "no_label_changes", "exact_text_ids": True,
            "label_change_count": len(changed), "changes": changed,
            "full_labels": full_labels if changed else [], "error": None}


def _state_query(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
    required = {"arxiv_id", "status"}
    if not required <= columns:
        raise ValueError("state database papers table lacks required columns")
    selected = ["p.arxiv_id", "p.status"]
    for field in ("fulltext_source", "parser_mode", "parser_revision", "extractor_version", "parse_status", "updated_at"):
        selected.append(f"p.{field}" if field in columns else f"NULL AS {field}")
    joins, extra = _publication_fields(conn, columns)
    query = (f"SELECT {','.join(selected + extra)} FROM papers p {' '.join(joins)} "
             "WHERE p.status='done' ORDER BY p.arxiv_id")
    return query


def _init_report(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS papers(
        paper_id TEXT PRIMARY KEY,input_fingerprint TEXT NOT NULL,scanned_at TEXT NOT NULL,
        fulltext_source TEXT,source_class TEXT NOT NULL,cache_state TEXT NOT NULL,source_state TEXT NOT NULL,
        cache_sha256 TEXT,source_sha256 TEXT,cache_bytes INTEGER,source_bytes INTEGER,
        parser_mode TEXT,parser_revision TEXT,extractor_version TEXT,parse_status TEXT,
        publication_revision_count INTEGER NOT NULL,active_owner INTEGER NOT NULL,
        chunk_count INTEGER,malformed_chunk_count INTEGER NOT NULL DEFAULT 0,
        cache_replacement_count INTEGER NOT NULL DEFAULT 0,source_replacement_count INTEGER NOT NULL DEFAULT 0,
        duplicate_index_count INTEGER NOT NULL DEFAULT 0,out_of_order_index_count INTEGER NOT NULL DEFAULT 0,
        noncontiguous_index INTEGER NOT NULL DEFAULT 0,duplicate_text_count INTEGER NOT NULL DEFAULT 0,
        candidate_status TEXT NOT NULL,exact_text_ids INTEGER,label_change_count INTEGER NOT NULL DEFAULT 0,
        candidate_changes_json TEXT NOT NULL DEFAULT '[]',candidate_label_map_json TEXT NOT NULL DEFAULT '[]',
        source_location TEXT,diagnostic TEXT);
      CREATE INDEX IF NOT EXISTS papers_cache_state ON papers(cache_state);
      CREATE INDEX IF NOT EXISTS papers_candidate ON papers(candidate_status);
    """)
    version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if version and version[0] != str(SCHEMA_VERSION):
        conn.close()
        raise ValueError("report database schema version mismatch")
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
    for name, declaration in (("candidate_label_map_json", "TEXT NOT NULL DEFAULT '[]'"),
                              ("source_location", "TEXT")):
        if name not in existing_columns:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {declaration}")
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('schema_version',?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def _fingerprint(row, cache_info, source_info, source_location):
    keys = ("arxiv_id", "status", "fulltext_source", "parser_mode", "parser_revision",
            "extractor_version", "parse_status", "updated_at", "publication_revision_count", "active_owner")
    payload = {key: row[key] for key in keys}
    payload["cache"] = _file_fingerprint(cache_info)
    payload["source"] = _file_fingerprint(source_info)
    payload["source_location"] = source_location
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _process(row, cache_info, source_info, fingerprint, source_location):
    aid = row["arxiv_id"]
    output = {"paper_id": aid, "input_fingerprint": fingerprint,
              "fulltext_source": row["fulltext_source"], "parser_mode": row["parser_mode"],
              "parser_revision": str(row["parser_revision"]) if row["parser_revision"] is not None else None,
              "extractor_version": str(row["extractor_version"]) if row["extractor_version"] is not None else None,
              "parse_status": row["parse_status"],
              "publication_revision_count": row["publication_revision_count"],
              "active_owner": int(bool(row["active_owner"])), "cache_state": cache_info["state"],
              "source_state": source_info["state"], "cache_sha256": None, "source_sha256": None,
              "cache_bytes": cache_info.get("size"), "source_bytes": source_info.get("size"),
              "chunk_count": None, "malformed_chunk_count": 0,
              "cache_replacement_count": 0, "source_replacement_count": 0,
              "duplicate_index_count": 0, "out_of_order_index_count": 0,
              "noncontiguous_index": 0, "duplicate_text_count": 0,
              "candidate_status": "not_evaluated", "exact_text_ids": None,
              "label_change_count": 0, "candidate_changes_json": "[]", "diagnostic": None,
              "candidate_label_map_json": "[]", "source_location": source_location,
              "source_class": "unknown"}
    source_label = (row["fulltext_source"] or "").strip().lower()
    if (source_label in {"abstract", "abstract-only", "abstract_fallback", "abstract-fallback"}
            or source_label.endswith(("-abstract", "-abstract-fallback"))):
        output["source_class"] = "abstract_only"
    elif source_info["state"] == "present":
        output["source_class"] = "fulltext"
    elif row["fulltext_source"]:
        output["source_class"] = "source_label_without_file"

    cache = None
    if cache_info["state"] == "present":
        if cache_info["size"] > MAX_INPUT_BYTES:
            output["cache_state"] = "too_large"
        else:
            try:
                raw_cache = cache_info["path"].read_bytes()
                output["cache_sha256"] = hashlib.sha256(raw_cache).hexdigest()
                cache = json.loads(raw_cache)
                if not isinstance(cache, list):
                    cache = None
                    output["cache_state"] = "malformed_shape"
                else:
                    output["chunk_count"] = len(cache)
                    metrics = _chunk_metrics(cache)
                    output["malformed_chunk_count"] = metrics["malformed_chunk_count"]
                    output["cache_replacement_count"] = metrics["replacement_count"]
                    output["duplicate_index_count"] = metrics["duplicate_index_count"]
                    output["out_of_order_index_count"] = metrics["out_of_order_index_count"]
                    output["noncontiguous_index"] = metrics["noncontiguous_index"]
                    output["duplicate_text_count"] = metrics["duplicate_text_count"]
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                output["cache_state"] = "malformed_json" if isinstance(exc, json.JSONDecodeError) else "unreadable"
                output["diagnostic"] = type(exc).__name__
    source_text = None
    if source_info["state"] == "present":
        if source_info["size"] > MAX_INPUT_BYTES:
            output["source_state"] = "too_large"
        else:
            try:
                raw_source = source_info["path"].read_bytes()
                output["source_sha256"] = hashlib.sha256(raw_source).hexdigest()
                source_text = raw_source.decode("utf-8")
                output["source_replacement_count"] = source_text.count("\ufffd")
                if not source_text.strip():
                    output["source_state"] = "empty"
            except (OSError, UnicodeError) as exc:
                output["source_state"] = "invalid_encoding" if isinstance(exc, UnicodeError) else "unreadable"
                output["diagnostic"] = type(exc).__name__
    if cache is not None and source_text is not None:
        result = _candidate(cache, source_text, aid, row["fulltext_source"], row["parser_mode"])
        output["candidate_status"] = result["status"]
        output["exact_text_ids"] = None if result["exact_text_ids"] is None else int(result["exact_text_ids"])
        output["label_change_count"] = result["label_change_count"]
        output["candidate_changes_json"] = json.dumps(result["changes"], ensure_ascii=False, separators=(",", ":"))
        output["candidate_label_map_json"] = json.dumps(
            result.get("full_labels", []), ensure_ascii=False, separators=(",", ":"))
        if result["error"]:
            output["diagnostic"] = result["error"]
    elif cache is None:
        output["candidate_status"] = "cache_unavailable"
    else:
        output["candidate_status"] = "source_unavailable"
    return output


def _summary(conn, scanned, reused, complete):
    def counts(field):
        return {row[0]: row[1] for row in conn.execute(
            f"SELECT {field},COUNT(*) FROM papers GROUP BY {field} ORDER BY {field}")}
    totals = conn.execute("SELECT COUNT(*),COALESCE(SUM(cache_replacement_count),0),"
                          "COALESCE(SUM(source_replacement_count),0),"
                          "COALESCE(SUM(label_change_count),0) FROM papers").fetchone()
    return {"complete": bool(complete), "scanned_this_run": scanned, "reused_this_run": reused,
            "reported_papers": totals[0], "cache_states": counts("cache_state"),
            "source_states": counts("source_state"), "source_classes": counts("source_class"),
            "candidate_statuses": counts("candidate_status"), "u_fffd_cache": totals[1],
            "u_fffd_source": totals[2], "proposed_label_changes": totals[3]}


def run_census(data_dir, report_path, *, chunk_dir=None, source_dir=None, latex_cache_dir=None, limit=None, force=False,
               commit_every=250):
    if commit_every < 1 or (limit is not None and limit < 1):
        raise ValueError("commit_every and limit must be positive")
    data_dir, report_path = Path(data_dir).resolve(), Path(report_path).resolve()
    if report_path == data_dir or data_dir in report_path.parents:
        raise ValueError("report database must be outside the corpus data directory")
    chunk_dir = Path(chunk_dir).resolve() if chunk_dir else data_dir / "chunk_cache"
    source_dir = Path(source_dir).resolve() if source_dir else data_dir / "fulltext_cache"
    latex_cache_dir = Path(latex_cache_dir).resolve() if latex_cache_dir else data_dir / "latex_cache"
    state = _readonly(data_dir / "state.db")
    report = _init_report(report_path)
    scanned = reused = 0
    complete = False
    try:
        query = _state_query(state)
        cursor = state.execute(query)
        while True:
            rows = cursor.fetchmany(200)
            if not rows:
                complete = True
                break
            for row in rows:
                aid = row["arxiv_id"]
                try:
                    filename = document_cache.safe_id(aid) + ".json"
                except Exception:
                    filename = None
                cache_info = (_file(chunk_dir / filename) if filename else {"state": "unsafe_paper_id", "path": chunk_dir})
                source_info = (_file(source_dir / filename.replace(".json", ".tex")) if filename
                               else {"state": "unsafe_paper_id", "path": source_dir})
                source_location = "fulltext_cache"
                if filename and source_info["state"] == "missing":
                    fallback = _file(latex_cache_dir / document_cache.safe_id(aid) / "source.tex")
                    if fallback["state"] == "present":
                        source_info = fallback
                        source_location = "latex_cache"
                fingerprint = _fingerprint(row, cache_info, source_info, source_location)
                old = report.execute("SELECT input_fingerprint FROM papers WHERE paper_id=?", (aid,)).fetchone()
                if not force and old and old[0] == fingerprint:
                    reused += 1
                else:
                    item = _process(row, cache_info, source_info, fingerprint, source_location)
                    item["scanned_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    columns = list(item)
                    report.execute(f"INSERT OR REPLACE INTO papers({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                                   [item[key] for key in columns])
                    scanned += 1
                if (scanned + reused) % commit_every == 0:
                    report.commit()
                    report.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('last_paper_id',?)", (aid,))
                    report.commit()
                if limit is not None and scanned + reused >= limit:
                    break
            if limit is not None and scanned + reused >= limit:
                break
        report.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('run_complete',?)", ("1" if complete else "0",))
        report.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('data_dir',?)", (str(data_dir),))
        report.commit()
        return _summary(report, scanned, reused, complete)
    finally:
        report.close()
        state.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, help="directory containing state.db")
    parser.add_argument("--report", required=True, help="operational report SQLite path outside data-dir")
    parser.add_argument("--chunk-dir", help="physical chunk_cache directory")
    parser.add_argument("--source-dir", help="physical fulltext_cache directory")
    parser.add_argument("--latex-cache-dir", help="physical latex_cache directory used when fulltext_cache source is missing")
    parser.add_argument("--limit", type=int, help="process at most N completed papers")
    parser.add_argument("--force", action="store_true", help="reinspect unchanged inputs")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    print(json.dumps(run_census(args.data_dir, args.report, chunk_dir=args.chunk_dir,
                                source_dir=args.source_dir, latex_cache_dir=args.latex_cache_dir,
                                limit=args.limit, force=args.force),
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
