"""Prepare bounded candidate censuses for source-verified label repairs.

This helper is preparation-only. It reads the original census, label ledger,
cache and source; its only output is a small candidate SQLite database.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from ops import build_partial_heading_candidates as partial
from ops import repair_corpus_labels as driver
from ops import restore_corpus_points as point_driver
from ops import source_heading_labels
from pipeline import document_cache


MAX_SOURCE_BYTES = partial.MAX_SOURCE_BYTES


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return json.dumps([str(path), stat.st_dev, stat.st_ino, stat.st_size,
                       stat.st_mtime_ns], separators=(",", ":"))


def _read_source(path):
    data = document_cache.read_bounded(path, limit=MAX_SOURCE_BYTES)
    return data


def _verified_unresolved_rows(census, ledger, cache_dir, fulltext_cache_dir,
                              latex_cache_dir, ledger_path, *, expected_doc_count=403,
                              expected_unresolved_count=1758,
                              expected_resolvable_count=343,
                              expected_resolved_chunk_count=1134):
    rows = []
    unresolved_total = 0
    for entry in ledger.execute(
            "SELECT paper_id,detail FROM papers WHERE status='verified' AND detail IS NOT NULL ORDER BY paper_id"):
        try:
            unresolved = json.loads(entry['detail']).get('unresolved_chunks', [])
        except (TypeError, json.JSONDecodeError):
            continue
        if unresolved:
            rows.append((entry['paper_id'], unresolved))
            unresolved_total += len(unresolved)
    if len(rows) != expected_doc_count or unresolved_total != expected_unresolved_count:
        raise ValueError("verified unresolved label ledger scope changed")

    columns = {r[1] for r in census.execute("PRAGMA table_info(papers)")}
    required = {"paper_id", "cache_sha256", "source_sha256", "source_location",
                "fulltext_source", "parser_mode"}
    if not required <= columns:
        raise ValueError("original census lacks source-bound label fields")
    selected = []
    outcomes = {"exact": 0, "heading_fallback": 0, "resolved_chunks": 0}
    for aid, unresolved in rows:
        row = census.execute(
            "SELECT paper_id,cache_sha256,source_sha256,source_location,fulltext_source,parser_mode "
            "FROM papers WHERE paper_id=?", (aid,)).fetchone()
        if row is None:
            raise ValueError("verified unresolved label ID is absent from the original census")
        if driver._source_route(row) != "paper_markdown":
            raise ValueError(f"verified unresolved label source is not paper Markdown: {aid}")
        name = document_cache.safe_id(aid)
        cache_path = Path(cache_dir).resolve() / (name + ".json")
        if cache_path.is_symlink() or not cache_path.is_file():
            raise ValueError(f"current chunk cache is missing or unsafe: {aid}")
        cache_bytes = document_cache.read_bounded(cache_path)
        current_cache_sha = point_driver._cache_hash(row, cache_bytes, ledger_path)
        chunks = json.loads(cache_bytes)
        if not isinstance(chunks, list):
            raise ValueError(f"current chunk cache is not a JSON list: {aid}")
        by_index = {chunk.get("chunk_index"): chunk for chunk in chunks if isinstance(chunk, dict)}
        if (len(by_index) != len(chunks) or any(type(index) is not int for index in by_index)
                or len(by_index) != len(chunks)):
            raise ValueError(f"current cache chunk indices are invalid: {aid}")
        unresolved_indices = [item.get("chunk_index") for item in unresolved]
        if (any(type(index) is not int for index in unresolved_indices)
                or len(set(unresolved_indices)) != len(unresolved_indices)
                or not set(unresolved_indices) <= set(by_index)):
            raise ValueError(f"ledger unresolved indices do not map to current cache: {aid}")

        source_path = driver._source_path_for(row, fulltext_cache_dir, latex_cache_dir)
        source_bytes = _read_source(source_path)
        if _sha(source_bytes) != row["source_sha256"]:
            raise ValueError(f"source changed while preparing candidate: {aid}")
        result = source_heading_labels.candidate(chunks, source_bytes.decode("utf-8"))
        labels = result.get("full_label_map", [])
        indices = [item.get("chunk_index") for item in labels]
        complete = (result.get("source_heading_verified") is True
                    and len(labels) == len(chunks)
                    and len(set(indices)) == len(indices)
                    and set(indices) == set(by_index)
                    and all(document_cache._label(item.get("section_title"))
                            and item.get("section_type") in document_cache.SECTION_TYPES
                            for item in labels))
        if result.get("status") not in ("candidate", "no_label_changes") or not complete:
            continue
        if not set(unresolved_indices) <= set(indices):
            raise ValueError(f"source-bound map omits unresolved indices: {aid}")
        exact = result.get("exact_text_ids") is True
        selected.append((aid, row, current_cache_sha, result, exact))
        outcomes["exact" if exact else "heading_fallback"] += 1
        outcomes["resolved_chunks"] += len(unresolved_indices)

    if (len(selected) != expected_resolvable_count
            or outcomes["resolved_chunks"] != expected_resolved_chunk_count):
        raise ValueError("source-bound full-map resolution totals changed")
    return selected, len(rows), unresolved_total, outcomes


def prepare(census_path, ledger_path, output_path, *, cache_dir,
            fulltext_cache_dir, latex_cache_dir, expected_failed_count=17,
            subset="failed", expected_verified_count=403,
            expected_unresolved_count=1758, expected_resolvable_count=343,
            expected_resolved_chunk_count=1134):
    """Create a fresh small census for failed retries or verified unresolved labels."""
    census_path = Path(census_path).resolve()
    ledger_path = Path(ledger_path).resolve()
    output_path = Path(output_path).absolute()
    if output_path.resolve() in {census_path, ledger_path}:
        raise ValueError("candidate output must be separate from source databases")
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError("candidate output already exists; preserve and inspect it")
    if expected_failed_count < 1:
        raise ValueError("expected_failed_count must be positive")

    census = driver._readonly(census_path)
    ledger = driver._readonly(ledger_path)
    temp_path = None
    try:
        source_meta = dict(census.execute("SELECT key,value FROM meta"))
        if source_meta.get("run_complete") != "1":
            raise ValueError("original census is not complete")
        census_identity = _identity(census_path)
        ledger_tables = {row[0] for row in ledger.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "papers" not in ledger_tables:
            raise ValueError("label run ledger has no papers table")
        if subset not in {"failed", "verified-unresolved"}:
            raise ValueError("unsupported label candidate subset")
        if subset == "failed":
            failed_ids = [row[0] for row in ledger.execute(
                "SELECT paper_id FROM papers WHERE status='failed' ORDER BY paper_id")]
            if len(failed_ids) != expected_failed_count or len(set(failed_ids)) != len(failed_ids):
                raise ValueError("failed label ledger count differs from the requested bounded scope")
            columns = {row[1] for row in census.execute("PRAGMA table_info(papers)")}
            required = {"paper_id", "cache_sha256", "source_sha256", "source_location",
                        "fulltext_source", "parser_mode"}
            if not required <= columns:
                raise ValueError("original census lacks source-bound label fields")
            rows_by_id = {}
            for aid in failed_ids:
                row = census.execute(
                    "SELECT paper_id,cache_sha256,source_sha256,source_location,fulltext_source,parser_mode "
                    "FROM papers WHERE paper_id=?", (aid,)).fetchone()
                if row is None:
                    raise ValueError("failed label ID is absent from the original census")
                rows_by_id[aid] = row
            verified_rows = None
        else:
            verified_rows, verified_count, unresolved_count, outcomes = _verified_unresolved_rows(
                census, ledger, cache_dir, fulltext_cache_dir, latex_cache_dir, ledger_path,
                expected_doc_count=expected_verified_count,
                expected_unresolved_count=expected_unresolved_count,
                expected_resolvable_count=expected_resolvable_count,
                expected_resolved_chunk_count=expected_resolved_chunk_count)
            failed_ids = [item[0] for item in verified_rows]
            rows_by_id = {item[0]: item[1] for item in verified_rows}

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="." + output_path.name + ".",
                                         suffix=".tmp", dir=str(output_path.parent))
        os.close(fd)
        temp_path = Path(temp_name)
        out = sqlite3.connect(temp_path)
        try:
            out.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE papers(
                    paper_id TEXT PRIMARY KEY,
                    candidate_status TEXT NOT NULL,
                    exact_text_ids INTEGER NOT NULL,
                    cache_sha256 TEXT NOT NULL,
                    frozen_cache_sha256 TEXT,
                    source_sha256 TEXT NOT NULL,
                    source_location TEXT NOT NULL,
                    fulltext_source TEXT,
                    parser_mode TEXT,
                    labels_source_verified INTEGER NOT NULL,
                    candidate_changes_json TEXT NOT NULL,
                    candidate_label_map_json TEXT NOT NULL
                );
            """)
            for key, value in source_meta.items():
                out.execute("INSERT INTO meta(key,value) VALUES (?,?)", (key, value))
            out.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", [
                ("candidate_subset", ("failed-label-retry" if subset == "failed"
                                      else "verified-unresolved-label-retry")),
                ("source_census_identity", census_identity),
                ("source_census_path", str(census_path)),
                ("selected_failed_count" if subset == "failed" else "selected_verified_count",
                 str(len(failed_ids))),
            ])
            if subset == "verified-unresolved":
                out.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", [
                    ("source_label_ledger_identity", _identity(ledger_path)),
                    ("source_label_ledger_path", str(ledger_path)),
                    ("source_verified_unresolved_count", str(verified_count)),
                    ("source_unresolved_chunk_count", str(unresolved_count)),
                    ("source_bound_selected_count", str(len(verified_rows))),
                    ("source_bound_exact_id_count", str(outcomes["exact"])),
                    ("source_bound_heading_fallback_count", str(outcomes["heading_fallback"])),
                    ("source_bound_resolved_chunk_count", str(outcomes["resolved_chunks"])),
                ])

            for aid in failed_ids:
                row = rows_by_id[aid]
                if subset == "failed":
                    if driver._source_route(row) != "paper_markdown":
                        raise ValueError(f"failed label source is not a paper Markdown route: {aid}")
                    cache, by_index = driver._verify_candidate_inputs(
                        row, cache_dir, fulltext_cache_dir, latex_cache_dir)
                    source_path = driver._source_path_for(row, fulltext_cache_dir, latex_cache_dir)
                    source_bytes = _read_source(source_path)
                    if _sha(source_bytes) != row["source_sha256"]:
                        raise ValueError(f"source changed while preparing candidate: {aid}")
                    result = source_heading_labels.candidate(cache, source_bytes.decode("utf-8"))
                    exact_text_ids = True
                    cache_sha = row["cache_sha256"]
                else:
                    _, _, cache_sha, result, exact_text_ids = next(
                        item for item in verified_rows if item[0] == aid)
                    cache_bytes = document_cache.read_bounded(
                        Path(cache_dir).resolve() / (document_cache.safe_id(aid) + ".json"))
                    if _sha(cache_bytes) != cache_sha:
                        raise ValueError(f"current chunk cache changed while writing candidate: {aid}")
                    cache = json.loads(cache_bytes)
                    by_index = {chunk["chunk_index"]: chunk for chunk in cache}
                if (result.get("status") != "candidate"
                        and not (subset == "verified-unresolved"
                                 and result.get("status") == "no_label_changes")):
                    raise ValueError(f"source-bound candidate is incomplete for {aid}: {result.get('status')}")
                if not result.get("source_heading_verified"):
                    raise ValueError(f"source-bound candidate lacks verified headings: {aid}")
                labels = result["full_label_map"]
                if ({item.get("chunk_index") for item in labels} != set(by_index)
                        or len(labels) != len(by_index)):
                    raise ValueError(f"source-bound map does not cover the cache: {aid}")
                candidate_cache_sha = (cache_sha if subset == "verified-unresolved"
                                       else row["cache_sha256"])
                frozen_cache_sha = (row["cache_sha256"] if subset == "verified-unresolved"
                                    else None)
                out.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    aid, ("candidate" if exact_text_ids else "heading_label_candidate"),
                    int(exact_text_ids), candidate_cache_sha, frozen_cache_sha, row["source_sha256"],
                    row["source_location"], row["fulltext_source"], row["parser_mode"], 1,
                    json.dumps(result["changes"], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(labels, ensure_ascii=False, separators=(",", ":"))))
            out.commit()
        finally:
            out.close()
        if output_path.exists() or output_path.is_symlink():
            raise FileExistsError("candidate output appeared during preparation")
        os.replace(temp_path, output_path)
        temp_path = None
        return {"prepared": len(failed_ids), "source_census_identity": census_identity,
                "candidate_db": str(output_path), "label_ledger_unchanged": True,
                "subset": subset,
                "source_bound": (outcomes if subset == "verified-unresolved" else None)}
    finally:
        census.close()
        ledger.close()
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("/opt/dtox-corpus-quality-20261013"))
    parser.add_argument("--data-dir", type=Path, default=Path("/opt/dtox-research"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-failed-count", type=int, default=17)
    parser.add_argument("--subset", choices=("failed", "verified-unresolved"), default="failed")
    args = parser.parse_args(argv)
    base = args.base
    default_name = ("failed-label-retry-candidates.sqlite" if args.subset == "failed"
                    else "verified-unresolved-label-candidates.sqlite")
    output = args.output or base / default_name
    result = prepare(base / "census.sqlite", base / "label-run.sqlite", output,
        cache_dir=(args.data_dir / "chunk_cache").resolve(),
        fulltext_cache_dir=(args.data_dir / "fulltext_cache").resolve(),
        latex_cache_dir=(args.data_dir / "latex_cache").resolve(),
        expected_failed_count=args.expected_failed_count, subset=args.subset)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
