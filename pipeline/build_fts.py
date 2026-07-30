#!/usr/bin/env python3
"""Build the lexical half of the search out of what Qdrant already holds.

Dense retrieval keeps failing on exact things: a claim naming "GRPO" or
"durable nonce" or a specific paper's method finds neighbours by topic and
misses the term itself. BM25 does not have that failure mode, and the two fail
differently, which is the whole point of fusing them.

Sparse vectors inside Qdrant would mean recreating a collection of 820k points;
an FTS5 table beside it costs nothing and cannot break the live index.

  python3 build_fts.py            build (resumable)
  python3 build_fts.py --stats    show what is in there
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request

QDRANT = "http://localhost:6333/collections/papers_fulltext"
FTS_DB = "/opt/dtox-research/fts.db"
PAGE = 2000


def connect():
    conn = sqlite3.connect(FTS_DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            text,
            point_id UNINDEXED,
            arxiv_id UNINDEXED,
            section_type UNINDEXED,
            element_type UNINDEXED,
            layers UNINDEXED,
            year UNINDEXED,
            tokenize = 'porter unicode61'
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS build_state (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def post(path, body):
    req = urllib.request.Request(QDRANT + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=180).read())


def topup(conn):
    """Insert what the vector index holds and the lexical one does not.

    The first build worked from a snapshot and the live sync was wired in
    later, so everything embedded in between exists as a vector and not as
    text: 81k chunks findable by meaning and invisible to an exact term. This
    walks the whole collection once and fills the holes."""
    have = {row[0] for row in conn.execute("SELECT point_id FROM chunks")}
    print(f"already indexed: {len(have)}")
    offset, added, seen, started = None, 0, 0, time.time()
    while True:
        body = {"limit": PAGE, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        result = post("/points/scroll", body)["result"]
        points = result["points"]
        if not points:
            break
        rows = []
        for pt in points:
            seen += 1
            pid = str(pt["id"])
            if pid in have:
                continue
            p = pt.get("payload") or {}
            if not p.get("text"):
                continue
            rows.append((p["text"], pid, p.get("arxiv_id"), p.get("section_type"),
                         p.get("element_type"), str(p.get("layers") or ""), p.get("year")))
            have.add(pid)
        if rows:
            conn.executemany(
                "INSERT INTO chunks (text, point_id, arxiv_id, section_type, "
                "element_type, layers, year) VALUES (?,?,?,?,?,?,?)", rows)
            conn.commit()
            added += len(rows)
        offset = result.get("next_page_offset")
        if seen % 100000 < PAGE:
            print(f"  scanned {seen}, added {added}, {seen / max(1, time.time() - started):.0f}/s")
        if offset is None:
            break
    print(f"topup done: scanned {seen}, added {added} in {time.time() - started:.0f}s")
    conn.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
    conn.commit()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--topup", action="store_true",
                    help="add points Qdrant has and the index does not")
    args = ap.parse_args()

    conn = connect()
    if args.stats:
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        print(f"indexed chunks: {n}")
        row = conn.execute("SELECT value FROM build_state WHERE key='offset'").fetchone()
        print(f"resume offset: {row[0] if row else 'none'}")
        return 0

    if args.topup:
        return topup(conn)

    row = conn.execute("SELECT value FROM build_state WHERE key='offset'").fetchone()
    offset = json.loads(row[0]) if row else None
    done = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    started, page = time.time(), 0

    while True:
        body = {"limit": PAGE, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        result = post("/points/scroll", body)["result"]
        points = result["points"]
        if not points:
            break
        rows = []
        for pt in points:
            p = pt.get("payload") or {}
            text = p.get("text")
            if not text:
                continue
            rows.append((text, str(pt["id"]), p.get("arxiv_id"), p.get("section_type"),
                         p.get("element_type"), str(p.get("layers") or ""), p.get("year")))
        conn.executemany(
            "INSERT INTO chunks (text, point_id, arxiv_id, section_type, element_type, "
            "layers, year) VALUES (?,?,?,?,?,?,?)", rows)
        done += len(rows)
        offset = result.get("next_page_offset")
        conn.execute("INSERT OR REPLACE INTO build_state (key, value) VALUES ('offset', ?)",
                     (json.dumps(offset),))
        conn.commit()
        page += 1
        if page % 20 == 0:
            rate = done / max(1, time.time() - started)
            print(f"  {done} chunks, {rate:.0f}/s")
        if offset is None:
            break

    print(f"done: {done} chunks in {time.time() - started:.0f}s")
    conn.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
    conn.commit()
    print("index optimised")
    return 0


if __name__ == "__main__":
    sys.exit(main())
