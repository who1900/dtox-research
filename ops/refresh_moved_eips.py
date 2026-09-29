#!/usr/bin/env python3
"""Replace the "moved" placeholders indexed as eip:N with the real ERC text.

The ERC category left ethereum/EIPs for ethereum/ERCs in 2023; EIPs keeps a
one-line stub per file ("This file was moved to https://github.com/ethereum/ercs/...")
and 365 of them were indexed as if they were specifications (ERC-20, ERC-721,
ERC-4337 among them). The pipeline now reads ERCs through its own source
(erc@all) and never lets a stub overwrite real text; this one-off script fixes
what is already indexed. For each eip:N whose cached text is a stub it

  1. downloads ERCS/erc-N.md from raw.githubusercontent.com (no API quota),
  2. writes it to latex_cache/<id>/source.tex,
  3. deletes the paper's Qdrant points (wait=true) and fts.db rows,
  4. removes fulltext_cache and chunk_cache,
  5. sets title/year/abstract from the ERC front matter and status='quality_checked'
     with extractor_version and fulltext_source cleared: fulltext_step reads the
     cache, chunk_step re-extracts, embed_step re-embeds.

Dry run by default (reads state.db read-only, downloads ERC files, writes nothing).
Every paper is its own commit (sqlite timeout 30). ERCs that are Withdrawn or
Stagnant (the harvest skips those) and ERCs that cannot be fetched are left alone
and reported. Finished ids go to --progress, so a rerun continues; an id that was
started but not finished is retried. A lock file keeps two runs from overlapping.

  refresh_moved_eips.py               # dry run
  refresh_moved_eips.py --apply
"""
import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import time

import requests

import reprocess_lost_body as rlb

RAW_BASE = "https://raw.githubusercontent.com/ethereum/ERCs/master/ERCS/"
LIST_URL = "https://api.github.com/repos/ethereum/ERCs/contents/ERCS"
UA = "dtox-research/1.0 (mailto:aybatanime@gmail.com)"
SKIP_STATUSES = {"withdrawn"}  # keep in step with service.SPEC_SKIP_STATUSES
LIVE_STATUSES = ("done", "chunked", "fulltext_fetched")
MIN_ERC_CHARS = 400
_STUB_RE = re.compile(r"this file was moved to\s+https?://github\.com/ethereum/ercs", re.I)


def parse_front_matter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return meta


def spec_body(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def is_moved_stub(text):
    """Same rule as service.is_moved_stub (kept standalone: no service import)."""
    if not text:
        return False
    body = spec_body(text)
    if len(body.strip()) > 600:
        return False
    if _STUB_RE.search(body):
        return True
    return (parse_front_matter(text).get("status") or "").strip().lower() == "moved"


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def cached_text(data_dir, arxiv_id):
    """The text the indexed copy was built from: fulltext_cache, else latex_cache."""
    sid = rlb.safe_id(arxiv_id)
    text = read_text(os.path.join(data_dir, "fulltext_cache", f"{sid}.tex"))
    if text is None:
        text = read_text(os.path.join(data_dir, "latex_cache", sid, "source.tex"))
    return text


def load_state(path):
    """(finished ids, started-but-unfinished ids) from the progress file."""
    started, finished = set(), set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                (finished if rec.get("state") == "done" else started).add(rec.get("id"))
    return finished, started - finished


def mark(path, arxiv_id, state):
    with open(path, "a") as f:
        f.write(json.dumps({"id": arxiv_id, "state": state, "at": rlb.now_iso()}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def find_candidates(conn, data_dir, retry_ids=()):
    """eip:N papers whose indexed text is a moved-stub (plus interrupted retries)."""
    out = []
    marks = ",".join("?" * len(LIVE_STATUSES))
    rows = conn.execute(f"SELECT arxiv_id, title, status FROM papers "
                        f"WHERE arxiv_id LIKE 'eip:%' AND status IN ({marks}) ORDER BY arxiv_id",
                        LIVE_STATUSES).fetchall()
    for r in rows:
        if r["arxiv_id"] in retry_ids or is_moved_stub(cached_text(data_dir, r["arxiv_id"])):
            out.append(dict(r))
    out.sort(key=lambda d: int(d["arxiv_id"].split(":")[1]))
    return out


def fetch_erc(session, number, pause=0.35):
    """ERC markdown text, or None on 404/transient failure."""
    try:
        r = session.get(f"{RAW_BASE}erc-{number}.md", headers={"User-Agent": UA}, timeout=60)
    except requests.RequestException:
        return None
    finally:
        time.sleep(pause)
    if r.status_code != 200:
        return None
    return r.content.decode("utf-8", errors="replace")


def erc_usable(text):
    """(ok, reason). Real, live ERC text only."""
    if not text or is_moved_stub(text) or len(text) < MIN_ERC_CHARS:
        return False, "no real text"
    if (parse_front_matter(text).get("status") or "").strip().lower() in SKIP_STATUSES:
        return False, "withdrawn"
    return True, ""


def erc_fields(text):
    meta = parse_front_matter(text)
    m = re.search(r"((?:19|20)\d{2})", meta.get("created") or "")
    body = spec_body(text)
    return {
        "title": meta.get("title") or None,
        "year": int(m.group(1)) if m else None,
        "abstract": (meta.get("description") or body.strip()[:800]).strip(),
    }


def write_cache(data_dir, arxiv_id, text):
    d = os.path.join(data_dir, "latex_cache", rlb.safe_id(arxiv_id))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "source.tex.tmp")
    with open(tmp, "w", encoding="utf-8", errors="ignore") as f:
        f.write(text)
    os.replace(tmp, os.path.join(d, "source.tex"))


def remove_derived(data_dir, arxiv_id):
    """fulltext_cache and chunk_cache only; latex_cache holds the new text."""
    sid = rlb.safe_id(arxiv_id)
    for p in (os.path.join(data_dir, "fulltext_cache", f"{sid}.tex"),
              os.path.join(data_dir, "chunk_cache", f"{sid}.json")):
        with contextlib.suppress(FileNotFoundError):
            os.remove(p)


def refresh_one(conn, session, args, arxiv_id, text, fts_ids):
    """Returns False when the paper left the live statuses meanwhile."""
    row = conn.execute("SELECT status FROM papers WHERE arxiv_id=?", (arxiv_id,)).fetchone()
    if row is None or row["status"] not in LIVE_STATUSES:
        return False
    f = erc_fields(text)
    write_cache(args.data_dir, arxiv_id, text)
    rlb.qdrant_delete(session, args.qdrant, args.collection, arxiv_id)
    rlb.fts_delete(args.fts, fts_ids)
    remove_derived(args.data_dir, arxiv_id)
    marks = ",".join("?" * len(LIVE_STATUSES))
    conn.execute(
        "UPDATE papers SET title=COALESCE(?, title), year=COALESCE(?, year), abstract=?, "
        "status='quality_checked', extractor_version=NULL, fulltext_source=NULL, updated_at=? "
        f"WHERE arxiv_id=? AND status IN ({marks})",
        (f["title"], f["year"], f["abstract"], rlb.now_iso(), arxiv_id) + LIVE_STATUSES)
    conn.commit()
    return True


def list_erc_numbers(session):
    """One GitHub API call: numbers of every erc-N.md in ERCs/ERCS."""
    r = session.get(LIST_URL, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    nums = set()
    for e in r.json():
        m = re.match(r"^erc-(\d+)\.md$", e.get("name", ""), re.I)
        if m and e.get("type") == "file":
            nums.add(m.group(1))
    return nums


def run(conn, session, args, log=print):
    finished, pending = load_state(args.progress)
    cands = [c for c in find_candidates(conn, args.data_dir, pending)
             if c["arxiv_id"] not in finished or c["arxiv_id"] in pending]
    log(f"moved-stub papers: {len(cands)} (already refreshed: {len(finished)})")
    plan, skipped = [], []
    for c in cands:
        number = c["arxiv_id"].split(":")[1]
        text = fetch_erc(session, number, args.pause)
        ok, why = erc_usable(text)
        if not ok:
            skipped.append((c["arxiv_id"], why if text else "not fetched"))
            continue
        plan.append((c, text))
    log(f"replaceable: {len(plan)}, left alone: {len(skipped)}")
    for aid, why in skipped:
        log(f"  skip {aid}: {why}")
    if not args.apply:
        for c, text in plan[:args.examples]:
            f = erc_fields(text)
            log(f"  would refresh {c['arxiv_id']} status={c['status']} "
                f"title={f['title']!r} year={f['year']} erc_chars={len(text)}")
        try:
            have = {r["arxiv_id"] for r in conn.execute("SELECT arxiv_id FROM papers WHERE arxiv_id LIKE 'eip:%'")}
            new = sorted(int(n) for n in list_erc_numbers(session) if f"eip:{n}" not in have)
            log(f"ERCs not in the corpus yet (erc@all will add them, minus withdrawn): {len(new)}")
        except (requests.RequestException, ValueError) as e:
            log(f"ERC listing failed: {e}")
        log("dry run, nothing written (use --apply)")
        return len(plan)
    rowids = {} if args.skip_fts else rlb.fts_rowids(args.fts, [c["arxiv_id"] for c, _ in plan])
    n = 0
    for c, text in plan:
        aid = c["arxiv_id"]
        try:
            mark(args.progress, aid, "start")
            ok = refresh_one(conn, session, args, aid, text, rowids.get(aid, []))
        except (requests.RequestException, sqlite3.Error, OSError) as e:
            log(f"failed {aid}: {e}")  # stays 'start': retried on the next run
            continue
        mark(args.progress, aid, "done")
        if ok:
            n += 1
            log(f"refreshed {aid} erc_chars={len(text)} fts_rows={len(rowids.get(aid, []))}")
        else:
            log(f"skipped {aid}: status changed")
    log(f"refreshed {n} of {len(plan)}")
    return n


def parse_args(argv=None):
    d = rlb.DATA_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=os.path.join(d, "state.db"))
    ap.add_argument("--fts", default=os.getenv("FTS_DB_PATH", os.path.join(d, "fts.db")))
    ap.add_argument("--data-dir", default=d)
    ap.add_argument("--qdrant", default=os.getenv("QDRANT_URL", "http://127.0.0.1:16335"))
    ap.add_argument("--collection", default=os.getenv("QDRANT_COLLECTION", "papers_fulltext"))
    ap.add_argument("--progress", default=os.path.join(d, "refresh_moved_progress.jsonl"))
    ap.add_argument("--lock", default=os.path.join(d, "refresh_moved_eips.lock"))
    ap.add_argument("--pause", type=float, default=0.35)
    ap.add_argument("--examples", type=int, default=10)
    ap.add_argument("--skip-fts", action="store_true")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with rlb.single_instance(args.lock) as got:
        if not got:
            print("another run holds the lock, exiting", flush=True)
            return 0
        if args.apply:
            conn = rlb.connect(args.db)
        else:  # dry run never opens state.db for writing
            conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
        session = requests.Session()
        try:
            run(conn, session, args)
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
