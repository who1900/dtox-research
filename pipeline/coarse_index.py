#!/usr/bin/env python3
"""
Article-level coarse index for two-tier search.

papers_fulltext (9.2M chunks, on-disk vectors) is bottlenecked by the VPS's
random-read latency: a cold query costs 2-5s just walking HNSW on disk. This
collection holds ONE small vector per paper (title + abstract) instead of one
per chunk -- ~164k points, kept fully `on_disk: false` so it lives in RAM.
Query routing embeds the query once, searches this collection to shortlist
the top-N candidate articles, then searches papers_fulltext filtered to just
those arxiv_ids -- turning a full-corpus disk scan into a small filtered one.

This module only builds and keeps papers_coarse in sync. It does not change
how papers_fulltext is searched (that is api/main.py's job, deliberately left
alone here).

CLI:
    python pipeline/coarse_index.py --backfill [--limit N] [--dry-run]
    python pipeline/coarse_index.py --one ARXIV_ID
    python pipeline/coarse_index.py --stats
"""

import argparse
import logging
import os
import time

import requests

import service
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("dtox-research.coarse_index")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

COARSE_COLLECTION = os.getenv("COARSE_COLLECTION", "papers_coarse")
# Separate uuid5 namespace prefix from the chunk points ("{arxiv_id}#{chunk_index}",
# see service.py's _close_out_paper) so a coarse point and a chunk point for the
# same paper never collide even though both live in the same NAMESPACE_URL space.
COARSE_ID_PREFIX = "coarse:"
COARSE_BATCH_SIZE = 32
# Gentle by default: this runs alongside the live ingest pipeline hitting the
# same embed-small service, and must not starve it.
COARSE_SLEEP = float(os.getenv("COARSE_SLEEP", "0.2"))

# ~800 chars is the first three or four sentences of an abstract, which is
# where a paper states its idea; 1500 cost 2-3x the embedding time on this CPU.
ABSTRACT_CHAR_LIMIT = int(os.getenv("COARSE_MAX_CHARS", "800"))
# Backfill spreads batches over every listed embed service (same model); the
# live query service can lend its idle CPU for a one-off rebuild.
COARSE_EMBED_URLS = [u.strip() for u in os.getenv("COARSE_EMBED_URLS", "").split(",") if u.strip()]
MIN_ABSTRACT_LEN = 80
# Mirrors extractor.SKIP_SECTION_TYPES: skip the same low-value sections when
# picking a fallback sentence for abstract-less papers.
FALLBACK_SKIP_SECTIONS = {"related_work", "appendix", "conclusion"}
FALLBACK_SCROLL_LIMIT = 20

# arxiv_id prefix -> source label, mirroring the *_ID_PREFIX constants in
# service.py (there is no dedicated "source" column in state.db; the prefix
# on the id itself is what tells the ingest pipeline where a paper came from).
_ID_PREFIX_SOURCE = (
    (service.ACL_ID_PREFIX, "acl"),
    (service.OPENALEX_ID_PREFIX, "openalex"),
    (service.HAL_ID_PREFIX, "hal"),
    (service.PMLR_ID_PREFIX, "pmlr"),
    (service.IACR_ID_PREFIX, "iacr"),
    (service.EIP_ID_PREFIX, "eip"),
    (service.SIMD_ID_PREFIX, "simd"),
    (service.GITHUB_DOC_ID_PREFIX, "github"),
    (service.WHITEPAPER_ID_PREFIX, "whitepaper"),
)

DONE_ROW_COLUMNS = "arxiv_id, title, abstract, layers, year, niche_score, citation_count, influential"


# ---------------------------------------------------------------------------
# pure helpers (unit-testable, no network/DB)
# ---------------------------------------------------------------------------

def coarse_point_id(arxiv_id):
    """Deterministic point id, same uuid5 approach as the chunk points in
    service.py's _close_out_paper, but namespaced so it never collides with
    a chunk id for the same paper."""
    return str(service.uuid.uuid5(service.NAMESPACE_URL, f"{COARSE_ID_PREFIX}{arxiv_id}"))


def source_for(arxiv_id):
    aid = str(arxiv_id or "")
    for prefix, name in _ID_PREFIX_SOURCE:
        if aid.startswith(prefix):
            return name
    return "arxiv"


def sentence_truncate(text, limit=ABSTRACT_CHAR_LIMIT):
    """Cut text to ~limit chars at a sentence boundary when possible, else at
    a word boundary, so the embedded passage never ends mid-word."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = -1
    for sep in (". ", ".\n", "! ", "? "):
        idx = cut.rfind(sep)
        if idx > best:
            best = idx
    # Only trust the sentence boundary if it doesn't throw away most of the
    # budget (a boundary at char 20 of a 1500 limit is not a useful cut).
    if best > limit * 0.4:
        return cut[: best + 1].strip()
    idx = cut.rfind(" ")
    if idx > 0:
        return cut[:idx].strip()
    return cut.strip()


def fetch_fallback_chunk_text(session, arxiv_id):
    """For papers with no usable abstract: pull the first prose chunk from
    papers_fulltext outside the low-value sections, via a small scroll
    (never a search -- this needs no vector)."""
    body = {
        "filter": {"must": [{"key": "arxiv_id", "match": {"value": arxiv_id}}]},
        "with_payload": ["text", "section_type", "element_type", "chunk_index"],
        "with_vector": False,
        "limit": FALLBACK_SCROLL_LIMIT,
    }
    try:
        resp = session.post(
            f"{service.QDRANT_URL}/collections/{service.COLLECTION_NAME}/points/scroll",
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"coarse: fallback chunk fetch failed for {arxiv_id}: {e}")
        return None
    points = resp.json().get("result", {}).get("points", [])
    candidates = [
        p
        for p in points
        if p.get("payload", {}).get("element_type") == "prose"
        and p.get("payload", {}).get("section_type") not in FALLBACK_SKIP_SECTIONS
        and p.get("payload", {}).get("text")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.get("payload", {}).get("chunk_index", 0))
    return candidates[0]["payload"]["text"]


def build_embed_text(title, abstract, arxiv_id, session=None):
    """Text to embed for one paper: title + abstract when there's a real
    abstract, else title + a fallback chunk fetched via `session`, else just
    the title."""
    title = (title or "").strip()
    abstract = (abstract or "").strip()
    if len(abstract) >= MIN_ABSTRACT_LEN:
        body = sentence_truncate(abstract)
        return f"{title}. {body}" if title else body
    if session is not None:
        chunk_text = fetch_fallback_chunk_text(session, arxiv_id)
        if chunk_text:
            body = sentence_truncate(chunk_text)
            return f"{title}. {body}" if title else body
    return title


def build_payload(row, in_citations=0):
    """`in_citations` is in-corpus in-degree (rows in state.db's citations
    table with dst=arxiv_id) -- a canonicality prior: a paper cited by 7675
    other indexed papers (e.g. LoRA) should outrank its own descendants in
    chunk search, which citation_count/influential (external, often NULL for
    niche/recent work) don't capture on their own."""
    layers = row["layers"].split(",") if row["layers"] else []
    return {
        "arxiv_id": row["arxiv_id"],
        "title": row["title"] or "",
        "layers": layers,
        "year": row["year"],
        "source": source_for(row["arxiv_id"]),
        "niche_score": row["niche_score"] if row["niche_score"] is not None else 0,
        "citation_count": row["citation_count"],
        "influential": row["influential"],
        "in_citations": in_citations or 0,
    }


def fetch_in_citations_all(conn):
    """One GROUP BY over the whole citations table (~3M rows): arxiv_id -> how
    many corpus papers cite it. Used once per --backfill run, not per paper."""
    counts = {}
    for dst, n in conn.execute("SELECT dst, COUNT(*) FROM citations GROUP BY dst"):
        counts[dst] = n
    return counts


def fetch_in_citations_for(conn, arxiv_ids):
    """Point lookup for the incremental path: COUNT(*) grouped by dst, scoped
    to just this batch's arxiv_ids via the citations(dst) index -- not a full
    table scan."""
    if not arxiv_ids:
        return {}
    placeholders = ",".join("?" for _ in arxiv_ids)
    rows = conn.execute(
        f"SELECT dst, COUNT(*) FROM citations WHERE dst IN ({placeholders}) GROUP BY dst",
        list(arxiv_ids),
    ).fetchall()
    return {dst: n for dst, n in rows}


# ---------------------------------------------------------------------------
# Qdrant collection management
# ---------------------------------------------------------------------------

def _ensure_coarse_indexes(session):
    for field, schema in (
        ("layers", "keyword"),
        ("year", "integer"),
        ("arxiv_id", "keyword"),
        ("in_citations", "integer"),
    ):
        session.put(
            f"{service.QDRANT_URL}/collections/{COARSE_COLLECTION}/index",
            json={"field_name": field, "field_schema": schema},
            timeout=30,
        )


def ensure_coarse_collection(session):
    resp = session.get(f"{service.QDRANT_URL}/collections/{COARSE_COLLECTION}", timeout=15)
    if resp.status_code == 200:
        _ensure_coarse_indexes(session)
        return
    log.info(f"creating Qdrant collection {COARSE_COLLECTION}")
    body = {
        "vectors": {"size": 384, "distance": "Cosine", "on_disk": False},
        "hnsw_config": {"m": 16, "ef_construct": 128},
        "on_disk_payload": False,
    }
    r = session.put(f"{service.QDRANT_URL}/collections/{COARSE_COLLECTION}", json=body, timeout=30)
    r.raise_for_status()
    _ensure_coarse_indexes(session)


def get_indexed_arxiv_ids(session):
    """arxiv_ids already present in papers_coarse (payload only, no vectors) --
    used to make --backfill resumable."""
    ids = set()
    offset = None
    while True:
        body = {"with_payload": ["arxiv_id"], "with_vector": False, "limit": 500}
        if offset is not None:
            body["offset"] = offset
        resp = session.post(
            f"{service.QDRANT_URL}/collections/{COARSE_COLLECTION}/points/scroll",
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        for p in result.get("points", []):
            aid = p.get("payload", {}).get("arxiv_id")
            if aid:
                ids.add(aid)
        offset = result.get("next_page_offset")
        if not offset:
            break
    return ids


def upsert_batch(session, points):
    if not points:
        return
    r = session.put(
        f"{service.QDRANT_URL}/collections/{COARSE_COLLECTION}/points",
        params={"wait": "true"},
        json={"points": points},
        timeout=60,
    )
    r.raise_for_status()


# ---------------------------------------------------------------------------
# state.db reads
# ---------------------------------------------------------------------------

def fetch_done_rows(conn, limit=None, only_id=None):
    if only_id:
        return conn.execute(
            f"SELECT {DONE_ROW_COLUMNS} FROM papers WHERE arxiv_id=? AND status='done'",
            (only_id,),
        ).fetchall()
    q = f"SELECT {DONE_ROW_COLUMNS} FROM papers WHERE status='done' ORDER BY arxiv_id"
    if limit:
        q += " LIMIT ?"
        return conn.execute(q, (limit,)).fetchall()
    return conn.execute(q).fetchall()


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def upsert_papers(conn, arxiv_ids, session=None):
    """Embed + upsert coarse points for exactly these arxiv_ids (already
    status='done' in state.db). Called from service.py right after a paper
    closes out, so this must stay small and cheap -- no full scans.
    Returns the number of points written."""
    if not arxiv_ids:
        return 0
    own_session = session is None
    session = session or requests.Session()
    try:
        ensure_coarse_collection(session)
        placeholders = ",".join("?" for _ in arxiv_ids)
        rows = conn.execute(
            f"SELECT {DONE_ROW_COLUMNS} FROM papers "
            f"WHERE status='done' AND arxiv_id IN ({placeholders})",
            list(arxiv_ids),
        ).fetchall()
        if not rows:
            return 0
        in_citations = fetch_in_citations_for(conn, [r["arxiv_id"] for r in rows])
        texts = [build_embed_text(r["title"], r["abstract"], r["arxiv_id"], session=session) for r in rows]
        vectors = service.embed_texts_batch(session, texts)
        points = [
            {
                "id": coarse_point_id(row["arxiv_id"]),
                "vector": vec,
                "payload": build_payload(row, in_citations.get(row["arxiv_id"], 0)),
            }
            for row, vec in zip(rows, vectors)
        ]
        upsert_batch(session, points)
        return len(points)
    finally:
        if own_session:
            session.close()


def backfill(conn, limit=None, dry_run=False):
    session = requests.Session()
    ensure_coarse_collection(session)
    existing = get_indexed_arxiv_ids(session)
    log.info(f"coarse: {len(existing)} papers already indexed")
    rows = fetch_done_rows(conn)
    todo = [r for r in rows if r["arxiv_id"] not in existing]
    if limit:
        todo = todo[:limit]
    total = len(todo)
    log.info(f"coarse: {total} papers to backfill")
    if dry_run or total == 0:
        return total

    log.info("coarse: computing in-corpus citation in-degree (one GROUP BY over citations)")
    in_citations = fetch_in_citations_all(conn)

    urls = COARSE_EMBED_URLS or [service.EMBED_BATCH_URL]
    sessions = [requests.Session() for _ in urls]

    def embed(job):
        slot, rows = job
        texts = [build_embed_text(r["title"], r["abstract"], r["arxiv_id"], session=sessions[slot])
                 for r in rows]
        if not COARSE_EMBED_URLS:
            return rows, service.embed_texts_batch(sessions[slot], texts)
        resp = sessions[slot].post(urls[slot], json={"texts": texts}, timeout=service.EMBED_TIMEOUT)
        resp.raise_for_status()
        return rows, resp.json()["vectors"]

    batches = [todo[i : i + COARSE_BATCH_SIZE] for i in range(0, total, COARSE_BATCH_SIZE)]
    done = 0
    start = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=len(urls))
    for w in range(0, len(batches), len(urls)):
        jobs = [(k, b) for k, b in enumerate(batches[w : w + len(urls)])]
        results = []
        for job, fut in [(j, pool.submit(embed, j)) for j in jobs]:
            try:
                results.append(fut.result())
            except requests.RequestException as e:
                log.warning(f"coarse: embed batch failed, skipping {len(job[1])} papers: {e}")
        for batch_rows, vectors in results:
            points = [
                {
                    "id": coarse_point_id(row["arxiv_id"]),
                    "vector": vec,
                    "payload": build_payload(row, in_citations.get(row["arxiv_id"], 0)),
                }
                for row, vec in zip(batch_rows, vectors)
            ]
            try:
                upsert_batch(session, points)
            except requests.RequestException as e:
                log.warning(f"coarse: upsert failed for batch of {len(points)} papers: {e}")
                continue
            done += len(points)
            if done % 1000 < COARSE_BATCH_SIZE:
                elapsed = time.monotonic() - start
                rate = done / elapsed if elapsed > 0 else 0.0
                log.info(f"coarse: {done}/{total} done ({rate:.1f}/s)")
        time.sleep(COARSE_SLEEP)
    pool.shutdown()
    log.info(f"coarse: backfill complete, {done} papers indexed")
    return done


def stats(conn):
    session = requests.Session()
    total_done = conn.execute("SELECT COUNT(*) FROM papers WHERE status='done'").fetchone()[0]
    count = "?"
    try:
        resp = session.get(f"{service.QDRANT_URL}/collections/{COARSE_COLLECTION}", timeout=15)
        if resp.status_code == 200:
            count = resp.json()["result"]["points_count"]
    except requests.RequestException as e:
        log.warning(f"coarse: stats fetch failed: {e}")
    print(f"papers status='done' in state.db: {total_done}")
    print(f"points in {COARSE_COLLECTION}: {count}")


def main():
    parser = argparse.ArgumentParser(description="papers_coarse article-level index")
    parser.add_argument("--backfill", action="store_true", help="index all done papers not yet in papers_coarse")
    parser.add_argument("--limit", type=int, default=None, help="cap how many new papers --backfill indexes")
    parser.add_argument("--dry-run", action="store_true", help="with --backfill: only report how many are pending")
    parser.add_argument("--one", metavar="ARXIV_ID", help="index (or re-index) a single paper")
    parser.add_argument("--stats", action="store_true", help="print state.db vs papers_coarse counts")
    args = parser.parse_args()

    conn = service.get_conn()
    if args.stats:
        stats(conn)
    elif args.one:
        n = upsert_papers(conn, [args.one])
        log.info(f"coarse: upserted {n} point(s) for {args.one}")
    elif args.backfill:
        backfill(conn, limit=args.limit, dry_run=args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
