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
if __package__:
    from pipeline.fts_catalog import CatalogBlocked, _scope_complete, append_points
else:
    from fts_catalog import CatalogBlocked, _scope_complete, append_points

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
    if not _scope_complete(conn):
        raise CatalogBlocked("partial legacy catalog prevents topup")
    conn.commit()
    offset, added, seen, started = None, 0, 0, time.time()
    while True:
        body = {"limit": PAGE, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        result = post("/points/scroll", body)["result"]
        points = result["points"]
        if not points:
            break
        page_points = []
        for pt in points:
            seen += 1
            page_points.append(pt)
        if page_points:
            added += append_points(conn, page_points)
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
    completed = conn.execute("SELECT value FROM build_state WHERE key='completed'").fetchone()
    if (completed and json.loads(completed[0]) is True) or (row and offset is None):
        print("build already completed")
        return 0
    if not _scope_complete(conn):
        raise CatalogBlocked("partial legacy catalog prevents build")
    conn.commit()
    done = 0
    started, page = time.time(), 0

    while True:
        body = {"limit": PAGE, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        result = post("/points/scroll", body)["result"]
        points = result["points"]
        page_points = []
        if not isinstance(points, list):
            raise ValueError("scroll response points must be a list")
        for pt in points:
            page_points.append(pt)
        next_offset = result["next_page_offset"]
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            inserted = append_points(conn, page_points)
            conn.execute("INSERT OR REPLACE INTO build_state (key, value) VALUES ('offset', ?)",
                         (json.dumps(next_offset),))
            conn.execute("INSERT OR REPLACE INTO build_state (key, value) VALUES ('completed', ?)",
                         (json.dumps(next_offset is None),))
        done += inserted
        offset = next_offset
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
