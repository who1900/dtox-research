#!/usr/bin/env python3
"""Article-level lexical index: the small, always-in-cache half of hybrid search.

fts.db (build_fts.py) is 13GB over 9.2M chunks on a slow disk and regularly
misses its own 2s budget under load, at which point the whole lexical channel
is silently skipped -- a query for an exact term like "GRPO" or "durable
nonce" then finds nothing it should have. papers_fts.db indexes only title +
abstract, one row per paper (~164k rows, a few hundred MB): small enough to
live entirely in the OS page cache, so it can answer even when fts.db can't.
It is not a replacement -- a term used only in a paper's body and not its
abstract still needs the chunk-level index -- it is a second, cheap, reliable
channel that the API fuses with whatever the chunk channel manages to return.

Same tokenizer as build_fts.py's `chunks` table, so api/search_core.fts_query()
produces MATCH strings that behave identically against both indexes.

    python3 build_paper_fts.py --rebuild   full, atomic rebuild from state.db
    python3 build_paper_fts.py --stats     row count + file size
"""
import argparse
import logging
import os
import sqlite3
import sys
import time

log = logging.getLogger("dtox-research.build_paper_fts")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

STATE_DB_PATH = os.getenv("STATE_DB_PATH", "/opt/dtox-research/state.db")
FTS_DB_PATH = os.getenv("FTS_DB_PATH", "/opt/dtox-research/fts.db")
# Next to the chunk-level fts.db by default, wherever that lives.
PAPER_FTS_PATH = os.getenv(
    "PAPER_FTS_PATH", os.path.join(os.path.dirname(FTS_DB_PATH) or ".", "papers_fts.db")
)

TOKENIZER = "porter unicode61"
PAGE = 2000

DONE_PAPERS_SQL = "SELECT arxiv_id, title, abstract, layers, year FROM papers WHERE status='done'"


def _schema_sql():
    return f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
            arxiv_id UNINDEXED,
            title,
            abstract,
            layers UNINDEXED,
            year UNINDEXED,
            tokenize = '{TOKENIZER}'
        )
    """


def _row_values(row):
    """arxiv_id, title, abstract, layers, year -- text columns never NULL."""
    arxiv_id, title, abstract, layers, year = row
    return (arxiv_id, title or "", abstract or "", layers or "", year)


def rebuild(state_db_path=STATE_DB_PATH, out_path=PAPER_FTS_PATH, page_size=PAGE):
    """Full rebuild from state.db, written atomically.

    The whole thing is built into a fresh temp file and only os.replace()'d
    into place once it is complete and checkpointed -- readers (the API's
    read-only connection) never see a half-built index, and a crash mid-build
    leaves the live papers_fts.db untouched.
    """
    tmp_path = f"{out_path}.tmp-{os.getpid()}"
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(tmp_path + suffix)
        except OSError:
            pass

    state = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True, timeout=30)
    conn = sqlite3.connect(tmp_path, timeout=60)
    started, total = time.monotonic(), 0
    try:
        conn.execute(_schema_sql())
        cur = state.execute(DONE_PAPERS_SQL)
        while True:
            rows = cur.fetchmany(page_size)
            if not rows:
                break
            conn.executemany(
                "INSERT INTO papers_fts (arxiv_id, title, abstract, layers, year) "
                "VALUES (?,?,?,?,?)",
                [_row_values(r) for r in rows],
            )
            conn.commit()
            total += len(rows)
        conn.execute("INSERT INTO papers_fts(papers_fts) VALUES('optimize')")
        conn.commit()
    except Exception:
        conn.close()
        state.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(tmp_path + suffix)
            except OSError:
                pass
        raise
    else:
        conn.close()
        state.close()

    os.replace(tmp_path, out_path)
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.remove(tmp_path + suffix)
        except OSError:
            pass
    elapsed = time.monotonic() - started
    log.info(f"papers_fts: rebuilt {total} papers into {out_path} in {elapsed:.1f}s")
    return total


def upsert_papers(arxiv_ids, state_db_path=STATE_DB_PATH, out_path=PAPER_FTS_PATH):
    """Incremental sync: delete+insert exactly these arxiv_ids.

    Called from pipeline/service.py right after a paper hits status='done',
    same pattern as coarse_index.upsert_papers -- must stay small and cheap,
    and never allowed to cost a paper (callers are expected to catch and log).
    """
    if not arxiv_ids:
        return 0
    arxiv_ids = list(arxiv_ids)
    state = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True, timeout=30)
    try:
        placeholders = ",".join("?" for _ in arxiv_ids)
        rows = state.execute(
            f"SELECT arxiv_id, title, abstract, layers, year FROM papers "
            f"WHERE status='done' AND arxiv_id IN ({placeholders})",
            arxiv_ids,
        ).fetchall()
    finally:
        state.close()

    conn = sqlite3.connect(out_path, timeout=60)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_schema_sql())
        placeholders = ",".join("?" for _ in arxiv_ids)
        conn.execute(f"DELETE FROM papers_fts WHERE arxiv_id IN ({placeholders})", arxiv_ids)
        if rows:
            conn.executemany(
                "INSERT INTO papers_fts (arxiv_id, title, abstract, layers, year) "
                "VALUES (?,?,?,?,?)",
                [_row_values(r) for r in rows],
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def stats(out_path=PAPER_FTS_PATH):
    if not os.path.exists(out_path):
        print(f"no papers_fts index at {out_path}")
        return 0
    conn = sqlite3.connect(f"file:{out_path}?mode=ro", uri=True, timeout=15)
    try:
        n = conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]
    finally:
        conn.close()
    size_mb = os.path.getsize(out_path) / 1e6
    print(f"indexed papers: {n}")
    print(f"file size: {size_mb:.1f} MB ({out_path})")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="full atomic rebuild from state.db")
    ap.add_argument("--stats", action="store_true", help="show row count and file size")
    args = ap.parse_args()

    if args.stats:
        stats()
        return 0
    if args.rebuild:
        rebuild()
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
