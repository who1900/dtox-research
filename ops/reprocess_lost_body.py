#!/usr/bin/env python3
"""Re-run papers whose LaTeX body was lost to the old \\input resolver.

assemble_latex_source used to look \\input{sections/x} up by basename and
silently replace a miss with nothing, so about a quarter of the LaTeX papers
kept only a preamble and an abstract (SWE-bench 2310.06770 had 2 chunks, has
154 after the fix). The fix only acts on a fresh download, so finished papers
have to go back through the pipeline: this drops what they left behind and puts
them at 'quality_checked', where fulltext_step picks them up again.

Two modes, both read-only unless --apply is given:

  --build-list   pick status='done' LaTeX papers with an arXiv id and fewer than
                 --max-chunks points in Qdrant, most cited first (then fewest
                 chunks), and write them to --list. --limit N looks only at the
                 N most cited candidates, for a quick sample.
  --feed         (default) count the pipeline queue (quality_checked +
                 fulltext_fetched + chunked); if it is under --target, reset the
                 next (target - queue) papers of the list, at most --batch.

A reset removes the paper's Qdrant points, its fts.db rows, fulltext_cache,
latex_cache and chunk_cache files, then sets status='quality_checked' with
extractor_version and fulltext_source cleared. Every paper is its own commit
(sqlite timeout 30): a transaction held across the run once stalled the live
pipeline for three hours on "database is locked". Finished ids go to --progress
(one JSON line each), so a rerun continues instead of starting over.

papers_fts.db and the coarse index replace their own rows when the paper is
finished again, so they are left alone. fts.db is an FTS5 table with an
unindexed arxiv_id, so finding a paper's rows is a full scan (minutes at 14 GB):
one scan serves the whole batch, then rows go by rowid.

Run under nice; a lock file keeps two runs from overlapping.

  reprocess_lost_body.py --build-list
  reprocess_lost_body.py --feed             # dry run: prints what it would reset
  reprocess_lost_body.py --feed --apply
"""
import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import time

try:
    import fcntl
except ImportError:  # Windows: the unit tests still import this module
    fcntl = None

import requests

DATA_DIR = os.getenv("DTOX_DATA_DIR", "/opt/dtox-research")
QUEUE_STATUSES = ("quality_checked", "fulltext_fetched", "chunked")
ARXIV_ID = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_id(paper_id):
    # same mapping as service.safe_id
    return str(paper_id).replace("/", "_").replace(":", "_")


@contextlib.contextmanager
def single_instance(path):
    """Non-blocking exclusive lock; yields False when another run holds it."""
    if fcntl is None or not hasattr(fcntl, "flock"):
        yield True
        return
    fh = open(path, "a")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        fh.close()


def connect(path, timeout=30):
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def qdrant_count(session, url, collection, arxiv_id):
    r = session.post(f"{url}/collections/{collection}/points/count",
                     json={"filter": {"must": [{"key": "arxiv_id", "match": {"value": arxiv_id}}]},
                           "exact": True}, timeout=60)
    r.raise_for_status()
    return r.json()["result"]["count"]


def qdrant_delete(session, url, collection, arxiv_id):
    r = session.post(f"{url}/collections/{collection}/points/delete", params={"wait": "true"},
                     json={"filter": {"must": [{"key": "arxiv_id", "match": {"value": arxiv_id}}]}},
                     timeout=120)
    r.raise_for_status()


def order_candidates(items):
    """items: dicts with citations and chunks. Most cited first, then fewest chunks."""
    return sorted(items, key=lambda it: (-(it["citations"] or 0), it["chunks"], it["id"]))


def build_list(conn, session, url, collection, max_chunks, limit=0, pause=0.05, log=print):
    sql = ("SELECT arxiv_id, COALESCE(citation_count, 0) AS c FROM papers "
           "WHERE status='done' AND fulltext_source='latex' ORDER BY c DESC, arxiv_id")
    rows = [r for r in conn.execute(sql) if ARXIV_ID.match(r["arxiv_id"])]
    if limit:
        rows = rows[:limit]
    found = []
    for i, r in enumerate(rows):
        n = qdrant_count(session, url, collection, r["arxiv_id"])
        if n < max_chunks:
            found.append({"id": r["arxiv_id"], "citations": r["c"], "chunks": n})
        if pause:
            time.sleep(pause)
        if (i + 1) % 1000 == 0:
            log(f"{i + 1}/{len(rows)} counted, {len(found)} candidates", flush=True)
    return order_candidates(found), len(rows)


def load_progress(path):
    done = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        done.add(json.loads(line)["id"])
                    except (ValueError, KeyError):
                        pass
    return done


def mark_progress(path, arxiv_id):
    with open(path, "a") as f:
        f.write(json.dumps({"id": arxiv_id, "at": now_iso()}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def queue_size(conn):
    marks = ",".join("?" * len(QUEUE_STATUSES))
    return conn.execute(f"SELECT COUNT(*) FROM papers WHERE status IN ({marks})",
                        QUEUE_STATUSES).fetchone()[0]


def pick(items, done, n):
    return [it for it in items if it["id"] not in done][:max(n, 0)]


def fts_rowids(fts_path, ids):
    """One scan of chunks_content (c2 = arxiv_id) for every id: {id: [rowid]}."""
    out = {i: [] for i in ids}
    if not ids or not os.path.exists(fts_path):
        return out
    conn = sqlite3.connect(f"file:{fts_path}?mode=ro", uri=True, timeout=30)
    try:
        marks = ",".join("?" * len(ids))
        for rid, aid in conn.execute(f"SELECT id, c2 FROM chunks_content WHERE c2 IN ({marks})", list(ids)):
            out[aid].append(rid)
    finally:
        conn.close()
    return out


def fts_delete(fts_path, rowids):
    if not rowids:
        return
    conn = sqlite3.connect(fts_path, timeout=30)
    try:
        conn.executemany("DELETE FROM chunks WHERE rowid=?", [(r,) for r in rowids])
        conn.commit()
    finally:
        conn.close()


def remove_caches(data_dir, arxiv_id):
    sid = safe_id(arxiv_id)
    latex = os.path.join(data_dir, "latex_cache", sid)
    for p in (os.path.join(data_dir, "fulltext_cache", f"{sid}.tex"),
              os.path.join(latex, "source.tex"),
              os.path.join(data_dir, "chunk_cache", f"{sid}.json")):
        with contextlib.suppress(FileNotFoundError):
            os.remove(p)
    with contextlib.suppress(OSError):
        os.rmdir(latex)


def reset_one(conn, session, args, arxiv_id, fts_ids):
    """Returns False when the paper is no longer 'done' and was left alone."""
    row = conn.execute("SELECT status FROM papers WHERE arxiv_id=?", (arxiv_id,)).fetchone()
    if row is None or row["status"] != "done":
        return False
    qdrant_delete(session, args.qdrant, args.collection, arxiv_id)
    fts_delete(args.fts, fts_ids)
    remove_caches(args.data_dir, arxiv_id)
    conn.execute("UPDATE papers SET status='quality_checked', extractor_version=NULL, "
                 "fulltext_source=NULL, updated_at=? WHERE arxiv_id=? AND status='done'",
                 (now_iso(), arxiv_id))
    conn.commit()
    return True


def feed(conn, session, args, log=print):
    items = []
    if os.path.exists(args.list):
        with open(args.list) as f:
            items = json.load(f)["items"]
    done = load_progress(args.progress)
    queue = queue_size(conn)
    need = min(args.target - queue, args.batch)
    log(f"queue {queue}, target {args.target}, list {len(items)}, already reset {len(done)}")
    if need <= 0:
        log("queue is full, nothing to reset")
        return 0
    chosen = pick(items, done, need)
    verb = "reset" if args.apply else "would reset"
    if not args.apply:
        for it in chosen:
            log(f"{verb} {it['id']} chunks={it['chunks']} citations={it['citations']}")
        log(f"{verb} {len(chosen)}")
        return len(chosen)
    rowids = {} if args.skip_fts else fts_rowids(args.fts, [it["id"] for it in chosen])
    n = 0
    for it in chosen:
        try:
            ok = reset_one(conn, session, args, it["id"], rowids.get(it["id"], []))
        except (requests.RequestException, sqlite3.Error, OSError) as e:
            # nothing is marked done, the next run retries the paper
            log(f"failed {it['id']}: {e}")
            continue
        if ok:
            mark_progress(args.progress, it["id"])
            n += 1
            log(f"reset {it['id']} chunks={it['chunks']} citations={it['citations']} "
                f"fts_rows={len(rowids.get(it['id'], []))}")
        else:
            mark_progress(args.progress, it["id"])
            log(f"skipped {it['id']}: not 'done' any more")
    log(f"{verb} {n} of {len(chosen)}")
    return n


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--build-list", action="store_true")
    mode.add_argument("--feed", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=os.path.join(DATA_DIR, "state.db"))
    ap.add_argument("--fts", default=os.getenv("FTS_DB_PATH", os.path.join(DATA_DIR, "fts.db")))
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--qdrant", default=os.getenv("QDRANT_URL", "http://127.0.0.1:16335"))
    ap.add_argument("--collection", default=os.getenv("QDRANT_COLLECTION", "papers_fulltext"))
    ap.add_argument("--list", default=os.path.join(DATA_DIR, "reprocess_list.json"))
    ap.add_argument("--progress", default=os.path.join(DATA_DIR, "reprocess_progress.jsonl"))
    ap.add_argument("--lock", default=os.path.join(DATA_DIR, "reprocess_lost_body.lock"))
    ap.add_argument("--max-chunks", type=int, default=40)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target", type=int, default=300)
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--pause", type=float, default=0.05)
    ap.add_argument("--skip-fts", action="store_true")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with single_instance(args.lock) as got:
        if not got:
            print("another run holds the lock, exiting", flush=True)
            return 0
        conn = connect(args.db)
        session = requests.Session()
        try:
            run(conn, session, args)
        finally:
            conn.close()
    return 0


def run(conn, session, args):
    if args.build_list:
        done = load_progress(args.progress)
        items, looked = build_list(conn, session, args.qdrant, args.collection,
                                   args.max_chunks, args.limit, args.pause)
        items = [it for it in items if it["id"] not in done]
        tmp = args.list + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"built": now_iso(), "max_chunks": args.max_chunks, "items": items}, f)
        os.replace(tmp, args.list)
        print(f"{len(items)} candidates of {looked} looked at -> {args.list}", flush=True)
    else:
        feed(conn, session, args)


if __name__ == "__main__":
    sys.exit(main())
