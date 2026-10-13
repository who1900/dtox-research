"""Resumable per-paper driver for the section-label corpus migration.

The census and frozen FTS row map are read-only inputs. Each paper gets its own
existing label-repair journal, so memory and recovery scope remain paper-sized.
No extraction, embedding, vector upsert, or global FTS scan is performed here.
"""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import repair_section_labels as legacy
from ops import replace_published_labels as published
from ops import audit_corpus_quality as census_tools
from pipeline import document_cache


class DriverError(RuntimeError):
    pass


def _readonly(path):
    path = Path(path).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _source_sha(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise DriverError("census source is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path, value):
    legacy._atomic(path, legacy._json_bytes(value))


def _open_rowmap(path, source_fts_db):
    conn = _readonly(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"meta", "rows"} <= tables:
            raise DriverError("FTS row map lacks meta/rows tables")
        meta = dict(conn.execute("SELECT key,value FROM meta"))
        if meta.get("complete") != "1":
            raise DriverError("FTS row map is not marked complete")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(rows)")}
        if not {"rowid", "point_id", "arxiv_id"} <= columns:
            raise DriverError("FTS row map schema mismatch")
        identity = json.loads(meta["source_identity"])
        source_path, source_dev, source_ino = identity
        stat = Path(source_fts_db).resolve().stat()
        if (Path(source_path).resolve() != Path(source_fts_db).resolve()
                or stat.st_dev != source_dev or stat.st_ino != source_ino):
            raise DriverError("FTS row map belongs to a different source DB identity")
        return conn
    except Exception:
        conn.close()
        raise


def _rowmap_for_paper(conn, aid):
    rows = conn.execute("SELECT rowid,point_id FROM rows WHERE arxiv_id=? ORDER BY rowid", (aid,)).fetchall()
    rowids = [int(r[0]) for r in rows]
    by_pid = {str(r[1]): int(r[0]) for r in rows}
    if len(by_pid) != len(rows) or len(set(rowids)) != len(rowids):
        raise DriverError("frozen row map has duplicate row IDs or point IDs")
    return rowids, by_pid


def _candidate_rows(census):
    columns = {r[1] for r in census.execute("PRAGMA table_info(papers)")}
    required = {"paper_id", "candidate_status", "exact_text_ids", "cache_sha256",
                "source_sha256", "source_location", "candidate_changes_json",
                "candidate_label_map_json"}
    if not required <= columns:
        raise DriverError("census schema lacks required candidate fields")
    tables = {r[0] for r in census.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "meta" not in tables or dict(census.execute("SELECT key,value FROM meta")).get("run_complete") != "1":
        raise DriverError("corpus census is not marked complete")
    candidate_filter = "candidate_status='candidate' AND exact_text_ids=1"
    if "labels_source_verified" in columns:
        candidate_filter += " OR (candidate_status='heading_label_candidate' AND labels_source_verified=1 AND candidate_changes_json!='[]')"
    optional = [name if name in columns else "NULL AS " + name
                for name in ("fulltext_source", "parser_mode")]
    return census.execute("SELECT paper_id,cache_sha256,source_sha256,source_location,"
        "candidate_changes_json,candidate_label_map_json," + ",".join(optional) + " FROM papers WHERE ("
        + candidate_filter + ") ORDER BY paper_id").fetchall()


def _source_path_for(row, fulltext_cache_dir, latex_cache_dir):
    aid = row["paper_id"]
    name = document_cache.safe_id(aid)
    location = row["source_location"]
    if location == "fulltext_cache":
        source_path = Path(fulltext_cache_dir).resolve() / (name + ".tex")
    elif location == "latex_cache":
        source_path = Path(latex_cache_dir).resolve() / name / "source.tex"
    else:
        raise DriverError("unsupported or absent census source location")
    if _source_sha(source_path) != row["source_sha256"]:
        raise DriverError("source bytes changed since census")
    return source_path


def _source_route(row):
    aid = row["paper_id"]
    if census_tools._is_spec(aid):
        return "spec_markdown"
    if census_tools._is_markdown_route(aid, row["fulltext_source"], row["parser_mode"]):
        return "paper_markdown"
    return "latex"


def _verify_candidate_inputs(row, cache_dir, fulltext_cache_dir, latex_cache_dir):
    aid = row["paper_id"]
    _source_path_for(row, fulltext_cache_dir, latex_cache_dir)
    name = document_cache.safe_id(aid)
    cache_path = Path(cache_dir).resolve() / (name + ".json")
    if cache_path.is_symlink() or not cache_path.is_file():
        raise DriverError("candidate chunk cache is missing or unsafe")
    cache_bytes = document_cache.read_bounded(cache_path)
    if legacy._sha(cache_bytes) != row["cache_sha256"]:
        raise DriverError("chunk cache changed since census")
    chunks = json.loads(cache_bytes)
    if not isinstance(chunks, list):
        raise DriverError("chunk cache is not a JSON list")
    by_index = {c.get("chunk_index"): c for c in chunks if isinstance(c, dict)}
    if len(by_index) != len(chunks) or any(type(i) is not int for i in by_index):
        raise DriverError("cache chunk indices are invalid or duplicated")
    return chunks, by_index


def _change_plan(row, chunks, by_index, rowmap_conn, fts_db, qdrant, collection):
    aid = row["paper_id"]
    rowids, row_by_pid = _rowmap_for_paper(rowmap_conn, aid)
    fts = _readonly(fts_db)
    try:
        if (legacy._exists(fts,'pipeline_fts_scope') and
                fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone()[0]==1):
            current_rows=fts.execute('SELECT row_id,point_id FROM pipeline_fts_points WHERE arxiv_id=? ORDER BY row_id',(aid,)).fetchall()
            current_map={str(pid):int(rid) for rid,pid in current_rows}
            if (len(current_map)!=len(current_rows) or len({r[0]for r in current_rows})!=len(current_rows)
                    or any(current_map.get(pid)!=rid for pid,rid in row_by_pid.items())):
                raise DriverError('complete ownership catalog differs from frozen paper mapping')
            for pid,rid in current_map.items():
                if pid not in row_by_pid:
                    if (not legacy._exists(fts,'dtox_cached_point_receipts') or not fts.execute(
                            'SELECT 1 FROM dtox_cached_point_receipts WHERE point_id=? AND row_id=?',(pid,rid)).fetchone()):
                        raise DriverError('post-census label baseline lacks accepted point receipt')
            rowids,row_by_pid=[int(r[0])for r in current_rows],current_map
        mapped = {}
        for rowid in rowids:
            current = fts.execute("SELECT point_id,arxiv_id FROM chunks WHERE rowid=?", (rowid,)).fetchone()
            if not current or current[1] != aid or str(current[0]) not in row_by_pid:
                raise DriverError("frozen rowmap row does not match current FTS identity")
            point_id = str(current[0])
            if row_by_pid[point_id] != rowid or point_id in mapped:
                raise DriverError("frozen rowmap rowid/point identity mismatch")
            mapped[point_id] = rowid
        if mapped != row_by_pid:
            raise DriverError("frozen rowmap does not cover current FTS identities exactly")
    finally:
        fts.close()
    labels_list = json.loads(row["candidate_label_map_json"])
    sparse = json.loads(row["candidate_changes_json"])
    labels = {}
    for item in labels_list:
        index = item.get("chunk_index")
        if type(index) is not int or index in labels:
            raise DriverError("census complete label map has invalid chunk indices")
        labels[index] = {"chunk_index": index, "section_title": item.get("section_title"),
                         "section_type": item.get("section_type")}
    if set(labels) != set(by_index):
        raise DriverError("census complete label map does not cover every cached chunk")
    candidate_labels = labels
    labels = {}
    unresolved = []
    unresolved_indices = set()
    for index, target in candidate_labels.items():
        invalid_title = not document_cache._label(target["section_title"])
        invalid_type = (not isinstance(target["section_type"], str)
                        or target["section_type"] not in document_cache.SECTION_TYPES)
        if not invalid_title and not invalid_type:
            labels[index] = target
            continue
        current = by_index[index]
        unresolved_indices.add(index)
        unresolved.append({"chunk_index": index, "reason": "invalid_candidate_label",
                           "title_length": (len(target["section_title"])
                                            if isinstance(target["section_title"], str) else None),
                           "invalid_title": invalid_title, "invalid_type": invalid_type})
        labels[index] = {"chunk_index": index, "section_title": current.get("section_title", ""),
                         "section_type": current.get("section_type", "other")}
    points = legacy._qdrant_points(qdrant, collection, aid)
    point_by_id = {legacy._point_id(p): p for p in points}
    if len(point_by_id) != len(points) or set(point_by_id) != set(row_by_pid):
        raise DriverError("Qdrant points differ from complete frozen FTS point map")
    index_to_pid = {}
    for pid, point in point_by_id.items():
        payload = legacy._payload(point)
        index = payload.get("chunk_index")
        if (type(index) is not int or index in index_to_pid or index not in by_index
                or payload.get("arxiv_id") != aid or payload.get("text") != by_index[index].get("text")):
            raise DriverError("Qdrant point does not map exactly to source cache")
        index_to_pid[index] = pid
    if set(index_to_pid) != {legacy._payload(p).get("chunk_index") for p in points}:
        raise DriverError("Qdrant indexed chunk set is not unique")
    changes, seen = [], set()
    for item in sparse:
        index = item.get("chunk_index")
        if type(index) is not int or index in seen or index not in labels:
            raise DriverError("census sparse change has invalid chunk index")
        seen.add(index)
        target = candidate_labels[index]
        if (item.get("section_title"), item.get("section_type")) != (target["section_title"], target["section_type"]):
            raise DriverError("sparse and complete census labels disagree")
        if index in unresolved_indices:
            continue
        target = labels[index]
        pid = index_to_pid.get(index)
        # Excluded cached chunks are handled by the complete cache map only.
        if pid is None:
            continue
        changes.append({"arxiv_id": aid, "rowid": row_by_pid[pid], "point_id": pid,
                        "section_title": target["section_title"], "section_type": target["section_type"]})
    return changes, {aid: list(labels.values())}, {aid: sorted(index_to_pid)}, {aid: rowids}, unresolved


def _journal_detail(plan):
    unresolved = [item for paper in plan.get("papers", [])
                  for item in paper.get("unresolved_chunks", [])]
    return json.dumps({"unresolved_chunks": unresolved}, sort_keys=True) if unresolved else None


def _route(state_db, aid):
    state = _readonly(state_db)
    try:
        active = False
        if legacy._exists(state, "paper_active"):
            active = bool(state.execute("SELECT 1 FROM paper_active WHERE arxiv_id=?", (aid,)).fetchone())
        pubs = []
        if legacy._exists(state, "paper_publications"):
            pubs = state.execute("SELECT kind,state FROM paper_publications WHERE arxiv_id=?", (aid,)).fetchall()
        if active:
            raise DriverError("managed active publication requires a separate revision-aware route")
        if not pubs:
            return legacy
        snapshots = [r for r in pubs if r[0] == "snapshot" and r[1] == "published"]
        if len(snapshots) == 1:
            return published
        raise DriverError("publication records do not match the supported first-snapshot replacement route")
    finally:
        state.close()


def _ledger(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS papers (paper_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                 "attempt INTEGER NOT NULL, journal_path TEXT, detail TEXT, updated_at TEXT NOT NULL)")
    conn.commit()
    return conn


def _record(ledger, aid, status, attempt, journal_path=None, detail=None):
    ledger.execute("INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?)",
        (aid, status, attempt, str(journal_path) if journal_path else None,
         detail, datetime.now(timezone.utc).isoformat()))
    ledger.commit()


def _journal_path(root, aid, attempt):
    name = hashlib.sha256(aid.encode("utf-8")).hexdigest()
    folder = Path(root) / name[:2]
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (name + f".attempt-{attempt:04d}.json")


def _module_for_plan(plan):
    if plan.get("kind") == "section-label-repair": return legacy
    if plan.get("kind") == "published-label-replacement": return published
    raise DriverError("unknown journal kind")


def _plan_paper_id(plan):
    if plan.get("kind") == "section-label-repair":
        papers = plan.get("papers", [])
        return papers[0].get("arxiv_id") if len(papers) == 1 else None
    if plan.get("kind") == "published-label-replacement":
        return plan.get("paper", {}).get("arxiv_id")
    return None


def _latest_journal(journal_dir, aid):
    prefix = hashlib.sha256(aid.encode("utf-8")).hexdigest() + ".attempt-"
    choices = []
    paths = list((Path(journal_dir) / prefix[:2]).glob(prefix + "*.json"))
    # Read journals from earlier flat pilot layouts as well.
    paths += list(Path(journal_dir).glob(prefix + "*.json"))
    for path in paths:
        try:
            attempt = int(path.stem.rsplit("-", 1)[1])
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, json.JSONDecodeError):
            continue
        if _plan_paper_id(plan) == aid:
            choices.append((attempt, path, plan))
    return max(choices, key=lambda item: item[0]) if choices else None


def _verify_after(module, journal_path, qdrant):
    plan = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    if module is legacy:
        if plan.get("phase") != "applied":
            return False
        module._verify(plan, qdrant, "rollback")
    else:
        if plan.get("phase") != "applied":
            return False
        module._verify(plan, qdrant, "after")
    return True


def _publication_context(module, factory):
    """The caller owns reader shutdown/restart and must leave it shut on failure."""
    return factory() if module is published and factory is not None else nullcontext()


def _recover_failed_paper(aid, journal_dir, qdrant, publication_maintenance=None):
    latest = _latest_journal(journal_dir, aid)
    if latest is None:
        return None
    _attempt, path, plan = latest
    module = _module_for_plan(plan)
    phase = plan.get("phase")
    if phase in {"applying", "qdrant_applied", "fts_applied", "cache_applied", "applied", "rolling_back"}:
        with _publication_context(module, publication_maintenance):
            module.rollback_plan(path, qdrant=qdrant)
        plan = json.loads(path.read_text(encoding="utf-8"))
        phase = plan.get("phase")
    if phase == "prepared":
        if module is legacy: module._verify(plan, qdrant, "apply")
        else: module._verify(plan, qdrant, "before")
    elif phase == "rolled_back":
        if module is legacy: module._verify(plan, qdrant, "apply")
        else: module._verify(plan, qdrant, "before")
    else:
        raise DriverError("failed operation could not be restored to its before-image")
    return path


def _resume_one(row, *, state_db, fts_db, census_db, rowmap_conn, qdrant,
                cache_dir, section_cache_dir, fulltext_cache_dir, latex_cache_dir,
                journal_dir, ledger, collection, publication_maintenance=None):
    aid = row["paper_id"]
    old = ledger.execute("SELECT status,attempt,journal_path FROM papers WHERE paper_id=?", (aid,)).fetchone()
    existing = None
    if old and old[2] and Path(old[2]).exists():
        existing = (old[1], Path(old[2]), json.loads(Path(old[2]).read_text(encoding="utf-8")))
    if existing is None:
        existing = _latest_journal(journal_dir, aid)
    if existing:
        prior_attempt, old_journal, prior = existing
        module = _module_for_plan(prior)
        phase = prior.get("phase")
        if phase == "applied" and _verify_after(module, old_journal, qdrant):
            _source_path_for(row, fulltext_cache_dir, latex_cache_dir)
            _record(ledger, aid, "verified", prior_attempt, old_journal, _journal_detail(prior))
            return "verified"
        if phase in {"prepared"}:
            with _publication_context(module, publication_maintenance):
                module.apply_plan(old_journal, qdrant=qdrant)
                if not _verify_after(module, old_journal, qdrant):
                    raise DriverError("after-state verification did not complete")
            prior = json.loads(old_journal.read_text(encoding="utf-8"))
            _record(ledger, aid, "verified", prior_attempt, old_journal, _journal_detail(prior))
            return "verified"
        if phase in {"applying", "qdrant_applied", "fts_applied", "cache_applied", "rolling_back"}:
            with _publication_context(module, publication_maintenance):
                module.rollback_plan(old_journal, qdrant=qdrant)
            _record(ledger, aid, "rolled_back_retryable", prior_attempt, old_journal)
        elif phase == "rolled_back":
            pass
        else:
            raise DriverError("unrecognized or non-resumable journal phase")
    # Read the original census inputs only after recovery, because a completed
    # operation intentionally changed the chunk-cache hash from the census hash.
    chunks, by_index = _verify_candidate_inputs(row, cache_dir, fulltext_cache_dir, latex_cache_dir)
    changes, full_labels, expected_indices, complete_rows, unresolved = _change_plan(
        row, chunks, by_index, rowmap_conn, fts_db, qdrant, collection)
    module = _route(state_db, aid)
    attempt = (existing[0] + 1) if existing else 1
    journal = _journal_path(journal_dir, aid, attempt)
    if journal.exists():
        raise DriverError("next attempt journal already exists; inspect run ledger")
    kwargs = dict(state_db=state_db, fts_db=fts_db, cache_dir=cache_dir,
        section_cache_dir=section_cache_dir, journal_path=journal, qdrant=qdrant,
        collection=collection, cache_labels_by_paper=full_labels,
        expected_indexed_indices_by_paper=expected_indices,
        complete_fts_rowids_by_paper=complete_rows)
    source_path = _source_path_for(row, fulltext_cache_dir, latex_cache_dir)
    source_bytes = document_cache.read_bounded(source_path)
    kwargs.update(source_bytes_by_paper={aid: source_bytes},
        expected_source_sha256_by_paper={aid: row["source_sha256"]},
        source_route_by_paper={aid: _source_route(row)})
    if module is legacy:
        kwargs["manifest"] = True
        kwargs["preserve_invalid_unchanged_before_image"] = True
        kwargs["unresolved_chunks_by_paper"] = {aid: unresolved}
    plan = module.prepare_plan(changes, **kwargs)
    _record(ledger, aid, "prepared", attempt, journal, _journal_detail(plan))
    with _publication_context(module, publication_maintenance):
        module.apply_plan(journal, qdrant=qdrant)
        if not _verify_after(module, journal, qdrant):
            raise DriverError("after-state verification did not complete")
    plan = json.loads(Path(journal).read_text(encoding="utf-8"))
    _record(ledger, aid, "verified", attempt, journal, _journal_detail(plan))
    return "verified"


def run(args, qdrant):
    Path(args.journal_dir).mkdir(parents=True, exist_ok=True)
    census = _readonly(args.census_db)
    rowmap = _open_rowmap(args.rowmap_db, args.fts_db)
    ledger = _ledger(args.run_db or (Path(args.journal_dir) / "repair-run.sqlite3"))
    totals = {"verified": 0, "failed": 0, "skipped_verified": 0, "skipped_failed": 0}
    try:
        rows = _candidate_rows(census)
        totals["candidates"] = len(rows)
        work = []
        for row in rows:
            state = ledger.execute("SELECT status FROM papers WHERE paper_id=?", (row["paper_id"],)).fetchone()
            status = state[0] if state else None
            if status == "manual_attention":
                raise DriverError("repair run has a paper marked manual_attention")
            if status == "verified" and not args.verify_completed:
                totals["skipped_verified"] += 1
            elif status == "failed" and not args.retry_failed:
                totals["skipped_failed"] += 1
            else:
                work.append(row)
        if args.limit:
            work = work[:args.limit]
        deadline = time.monotonic() + args.max_seconds if args.max_seconds else None
        attempted = 0
        for row in work:
            if deadline is not None and attempted and time.monotonic() >= deadline:
                break
            attempted += 1
            try:
                _resume_one(row, state_db=args.state_db, fts_db=args.fts_db,
                    census_db=args.census_db, rowmap_conn=rowmap, qdrant=qdrant,
                    cache_dir=args.cache_dir, section_cache_dir=args.section_cache_dir,
                    fulltext_cache_dir=args.fulltext_cache_dir, latex_cache_dir=args.latex_cache_dir,
                    journal_dir=args.journal_dir, ledger=ledger, collection=args.collection,
                    publication_maintenance=getattr(args, "publication_maintenance", None))
                totals["verified"] += 1
            except Exception as exc:
                prior = ledger.execute("SELECT attempt,journal_path FROM papers WHERE paper_id=?", (row["paper_id"],)).fetchone()
                attempt = prior[0] if prior else 0
                journal = prior[1] if prior else None
                try:
                    recovered = _recover_failed_paper(row["paper_id"], args.journal_dir, qdrant,
                        getattr(args, "publication_maintenance", None))
                    journal = recovered or journal
                except Exception as recovery_exc:
                    _record(ledger, row["paper_id"], "manual_attention", attempt,
                            journal, type(recovery_exc).__name__ + ": recovery failed")
                    raise DriverError("paper recovery failed; keep API maintenance active for manual inspection") from recovery_exc
                _record(ledger, row["paper_id"], "failed", attempt, journal,
                        type(exc).__name__ + ": " + str(exc)[:300])
                totals["failed"] += 1
        totals["scheduled"] = len(work)
        totals["pending"] = len(work) - attempted
        totals["verified_with_unresolved"] = 0
        totals["unresolved_chunks"] = 0
        for (detail,) in ledger.execute("SELECT detail FROM papers WHERE status='verified'"):
            if not detail:
                continue
            try:
                unresolved = json.loads(detail).get("unresolved_chunks", [])
            except (TypeError, json.JSONDecodeError):
                continue
            if unresolved:
                totals["verified_with_unresolved"] += 1
                totals["unresolved_chunks"] += len(unresolved)
        return totals
    finally:
        ledger.close(); rowmap.close(); census.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state-db", "fts-db", "census-db", "rowmap-db", "cache-dir", "section-cache-dir",
                 "fulltext-cache-dir", "latex-cache-dir", "journal-dir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--run-db")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--collection", default="papers_fulltext")
    parser.add_argument("--limit", type=int, default=0, help="process at most this many census candidates")
    parser.add_argument("--max-seconds", type=float, default=0,
                        help="stop between papers after this many seconds; 0 processes the selected scope")
    parser.add_argument("--verify-completed", action="store_true",
                        help="re-read and verify previously completed papers in this invocation")
    parser.add_argument("--retry-failed", action="store_true",
                        help="retry previously failed papers after before-image recovery")
    args = parser.parse_args(argv)
    Path(args.journal_dir).mkdir(parents=True, exist_ok=True)
    if args.limit < 0 or args.max_seconds < 0:
        parser.error("--limit and --max-seconds must be non-negative")
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise SystemExit("qdrant-client is required in the selected runtime") from exc
    client = QdrantClient(url=args.qdrant_url, api_key=os.environ.get("QDRANT_API_KEY"))
    totals = run(args, client)
    print(json.dumps(totals, sort_keys=True))
    return 0 if totals["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
