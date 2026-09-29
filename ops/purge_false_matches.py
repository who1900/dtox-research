#!/usr/bin/env python3
"""Remove papers that only got into the corpus through a false niche match.

Terms such as "mev", "stark", "yarn" or "wormhole" used to match case-blind, so
"MeV" (physics), the Stark effect, wormholes in relativity, yarn in textiles
and SNARKs in graph theory pulled unrelated papers into web3 / llm-slm. The
filter is now case-sensitive for those terms (niche_filter.CASE_SENSITIVE_TERMS);
this script re-scores the status='done' papers that matched one of them:

  new layers empty     -> points deleted from Qdrant (papers_fulltext,
                          papers_coarse_base, papers_coarse), row deleted from
                          papers_fts.db, state.db set to status='off_niche',
                          layers='', niche_score / matched_terms recomputed.
                          Text caches and fts.db (chunk index) are left alone,
                          so the step is reversible.
  new layers smaller   -> state.db layers / niche_score / matched_terms and the
                          "layers" payload in Qdrant and papers_fts.db updated.
  same layers          -> only matched_terms / niche_score refreshed in state.db.

Never touched: spec and document ids (eip: simd: wp: gh:, which
upsert_discovered exempts from the niche gate or that are hand-curated), the
built-in KEEP list, and anything passed with --keep. Dry run by default; each
paper is its own commit (timeout 30), finished ids go to --progress. Run under
nice; a lock file stops overlapping runs.

  purge_false_matches.py                 # dry run: counts, per-term split, examples
  purge_false_matches.py --apply
"""
import argparse
import contextlib
import json
import os
import random
import sqlite3
import sys
import time

try:
    import fcntl
except ImportError:  # Windows: the unit tests still import this module
    fcntl = None

import requests

DATA_DIR = os.getenv("DTOX_DATA_DIR", "/opt/dtox-research")
COLLECTIONS = ("papers_fulltext", "papers_coarse_base", "papers_coarse")
PROTECTED_PREFIXES = ("eip:", "simd:", "wp:", "gh:")
KEEP = frozenset({"iacr:2023/1255"})
HIGH_CITATIONS = 200


def _niche_filter():
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (DATA_DIR, os.path.join(here, "..", "pipeline"), here):
        if os.path.exists(os.path.join(d, "niche_filter.py")) and d not in sys.path:
            sys.path.insert(0, d)
    import niche_filter
    return niche_filter


niche_filter = _niche_filter()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextlib.contextmanager
def single_instance(path):
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


def is_protected(paper_id, keep=()):
    return str(paper_id).startswith(PROTECTED_PREFIXES) or paper_id in KEEP or paper_id in keep


def recompute(title, abstract):
    """(layers list, primary score, matched_terms list), as upsert_discovered does."""
    text = f"{title} {abstract}" if abstract else (title or "")
    scores = niche_filter.niche_score(text)
    layers = niche_filter.admitted_layers(scores)
    _, primary = niche_filter.primary_layer(scores)
    return layers, primary, niche_filter.all_matched_terms(scores)


def parse_terms(raw):
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return [t for t in v if isinstance(t, str)] if isinstance(v, list) else []


def find_candidates(conn, keep=()):
    """Plan for every done paper whose matched_terms hold a case-sensitive term."""
    cs = sorted(niche_filter.CASE_SENSITIVE_TERMS)
    where = " OR ".join("matched_terms LIKE ?" for _ in cs)
    rows = conn.execute(
        "SELECT arxiv_id, title, abstract, layers, niche_score, matched_terms, "
        f"COALESCE(citation_count, 0) AS cit FROM papers WHERE status='done' AND ({where})",
        [f'%"{t}"%' for t in cs]).fetchall()
    plans = []
    for r in rows:
        hit = [t for t in parse_terms(r["matched_terms"]) if t in niche_filter.CASE_SENSITIVE_TERMS]
        if not hit or is_protected(r["arxiv_id"], keep):
            continue
        old = sorted(x for x in (r["layers"] or "").split(",") if x)
        new, score, terms = recompute(r["title"], r["abstract"])
        if not new:
            action = "delete"
        elif set(new) < set(old):
            action = "trim"
        else:
            action = "refresh"
        plans.append({"id": r["arxiv_id"], "title": r["title"], "old": old, "new": new,
                      "score": score, "terms": terms, "hit": hit, "cit": r["cit"],
                      "action": action})
    return sorted(plans, key=lambda p: p["id"])


def load_progress(path):
    done = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                with contextlib.suppress(ValueError, KeyError):
                    done.add(json.loads(line)["id"])
    return done


def mark_progress(path, arxiv_id, action):
    with open(path, "a") as f:
        f.write(json.dumps({"id": arxiv_id, "action": action, "at": now_iso()}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _flt(arxiv_id):
    return {"must": [{"key": "arxiv_id", "match": {"value": arxiv_id}}]}


def qdrant_delete(session, url, collection, arxiv_id):
    r = session.post(f"{url}/collections/{collection}/points/delete", params={"wait": "true"},
                     json={"filter": _flt(arxiv_id)}, timeout=120)
    r.raise_for_status()


def qdrant_set_layers(session, url, collection, arxiv_id, layers):
    r = session.post(f"{url}/collections/{collection}/points/payload", params={"wait": "true"},
                     json={"payload": {"layers": layers}, "filter": _flt(arxiv_id)}, timeout=120)
    r.raise_for_status()


def paper_fts_rowids(path, ids):
    """One scan of papers_fts (arxiv_id is UNINDEXED): {id: [rowid]}."""
    out = {i: [] for i in ids}
    if not ids or not path or not os.path.exists(path):
        return out
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        wanted = set(ids)
        for rid, aid in conn.execute("SELECT rowid, arxiv_id FROM papers_fts"):
            if aid in wanted:
                out[aid].append(rid)
    finally:
        conn.close()
    return out


def paper_fts_apply(path, rowids, layers=None):
    """layers=None deletes the rows, otherwise sets the layers column."""
    if not rowids or not path or not os.path.exists(path):
        return
    conn = sqlite3.connect(path, timeout=30)
    try:
        if layers is None:
            conn.executemany("DELETE FROM papers_fts WHERE rowid=?", [(r,) for r in rowids])
        else:
            conn.executemany("UPDATE papers_fts SET layers=? WHERE rowid=?",
                             [(",".join(layers), r) for r in rowids])
        conn.commit()
    finally:
        conn.close()


def apply_one(conn, session, args, plan, fts_rows):
    """False when the paper is no longer 'done' and was left alone."""
    aid = plan["id"]
    row = conn.execute("SELECT status FROM papers WHERE arxiv_id=?", (aid,)).fetchone()
    if row is None or row["status"] != "done":
        return False
    terms_json = json.dumps(plan["terms"])
    if plan["action"] == "delete":
        for c in args.collections:
            qdrant_delete(session, args.qdrant, c, aid)
        paper_fts_apply(args.paper_fts, fts_rows)
        conn.execute("UPDATE papers SET status='off_niche', layers='', niche_score=?, "
                     "matched_terms=?, updated_at=? WHERE arxiv_id=? AND status='done'",
                     (plan["score"], terms_json, now_iso(), aid))
    elif plan["action"] == "trim":
        for c in args.collections:
            qdrant_set_layers(session, args.qdrant, c, aid, plan["new"])
        paper_fts_apply(args.paper_fts, fts_rows, plan["new"])
        conn.execute("UPDATE papers SET layers=?, niche_score=?, matched_terms=?, updated_at=? "
                     "WHERE arxiv_id=? AND status='done'",
                     (",".join(plan["new"]), plan["score"], terms_json, now_iso(), aid))
    else:
        conn.execute("UPDATE papers SET niche_score=?, matched_terms=?, updated_at=? "
                     "WHERE arxiv_id=? AND status='done'",
                     (plan["score"], terms_json, now_iso(), aid))
    conn.commit()
    return True


def _line(p, arrow=False):
    layers = f"{','.join(p['old'])} -> {','.join(p['new'])}" if arrow else ",".join(p["old"])
    return f"  {p['id']} | {(p['title'] or '')[:90]} | {layers} | cit={p['cit']} | {','.join(p['hit'])}"


def report(plans, log=print, seed=0, sample=30):
    dele = [p for p in plans if p["action"] == "delete"]
    trim = [p for p in plans if p["action"] == "trim"]
    log(f"candidates {len(plans)}: delete {len(dele)}, trim layers {len(trim)}, "
        f"refresh only {len(plans) - len(dele) - len(trim)}")
    for action, group in (("delete", dele), ("trim", trim)):
        by = {}
        for p in group:
            for t in p["hit"]:
                by[t] = by.get(t, 0) + 1
        log(f"by term ({action}): " + ", ".join(f"{t}={n}" for t, n in sorted(by.items(), key=lambda x: -x[1])))
    rnd = random.Random(seed)
    log(f"--- {min(sample, len(dele))} random deletions")
    for p in rnd.sample(dele, min(sample, len(dele))):
        log(_line(p))
    high = sorted((p for p in dele if p["cit"] >= HIGH_CITATIONS), key=lambda p: -p["cit"])
    log(f"--- deletions with citations >= {HIGH_CITATIONS}: {len(high)}")
    for p in high:
        log(_line(p))
    high_t = sorted((p for p in trim if p["cit"] >= HIGH_CITATIONS), key=lambda p: -p["cit"])
    log(f"--- layer trims with citations >= {HIGH_CITATIONS}: {len(high_t)}")
    for p in high_t:
        log(_line(p, arrow=True))


def run(conn, session, args, log=print):
    plans = find_candidates(conn, set(args.keep or ()))
    if not args.apply:
        report(plans, log)
        return plans
    done = load_progress(args.progress)
    todo = [p for p in plans if p["id"] not in done]
    rows = paper_fts_rowids(args.paper_fts, [p["id"] for p in todo if p["action"] != "refresh"])
    n = 0
    for p in todo:
        try:
            ok = apply_one(conn, session, args, p, rows.get(p["id"], []))
        except (requests.RequestException, sqlite3.Error, OSError) as e:
            log(f"failed {p['id']}: {e}")
            continue
        mark_progress(args.progress, p["id"], p["action"] if ok else "skipped")
        if ok:
            n += 1
            log(f"{p['action']} {p['id']} {','.join(p['old'])} -> {','.join(p['new'])}")
    log(f"applied {n} of {len(todo)}")
    return plans


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=os.path.join(DATA_DIR, "state.db"))
    ap.add_argument("--paper-fts", default=os.getenv(
        "PAPER_FTS_PATH", os.path.join(DATA_DIR, "papers_fts.db")))
    ap.add_argument("--qdrant", default=os.getenv("QDRANT_URL", "http://127.0.0.1:16335"))
    ap.add_argument("--collections", nargs="+", default=list(COLLECTIONS))
    ap.add_argument("--keep", type=lambda s: [x for x in s.split(",") if x], default=[])
    ap.add_argument("--progress", default=os.path.join(DATA_DIR, "purge_false_matches.jsonl"))
    ap.add_argument("--lock", default=os.path.join(DATA_DIR, "purge_false_matches.lock"))
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with single_instance(args.lock) as got:
        if not got:
            print("another run holds the lock, exiting", flush=True)
            return 0
        if args.apply:
            conn = connect(args.db)
        else:
            conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
        try:
            run(conn, requests.Session(), args)
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
