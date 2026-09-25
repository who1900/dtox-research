import hashlib
import json
import math
import os
import re
import sqlite3
import time
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Union

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
try:
    from .search_core import fts_query, merge_layer_hits, reciprocal_rank_fusion
except ImportError:
    from search_core import fts_query, merge_layer_hits, reciprocal_rank_fusion

ATTESTOR_URL = os.getenv("ATTESTOR_URL", "http://127.0.0.1:8013")
ATTESTOR_INTERNAL_TOKEN = os.getenv("ATTESTOR_INTERNAL_TOKEN", "")
QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.getenv("QDRANT_COLLECTION", "papers_fulltext")
EMBED_URL = os.getenv("EMBED_URL", "http://127.0.0.1:8006/embed")
EMBED_BATCH_URL = os.getenv("EMBED_BATCH_URL", "http://127.0.0.1:8006/embed_batch")
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
KEYS_PATH = Path(os.getenv("RESEARCH_KEYS_PATH", "/opt/dtox-research-api/keys.json"))

ALLOWED_LAYERS = {"llm-slm", "web3", "ai-agents", "builder-tech"}
SPEC_SECTION_TYPES = {"method", "architecture"}
SPEC_TITLE_KEYWORDS = ("algorithm", "model", "method")

app = FastAPI(title="dtox-research-api")
search_latencies = deque(maxlen=500)


@app.middleware("http")
async def record_search_latency(request: Request, call_next):
    started = time.monotonic()
    try:
        return await call_next(request)
    finally:
        if request.url.path == "/v1/search":
            search_latencies.append(time.monotonic() - started)


def _latency_percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[max(0, index)], 3)

# ---------------- API keys ----------------

def load_keys():
    with open(KEYS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

_keys_cache = {"data": None, "mtime": 0}

def get_keys():
    try:
        mtime = KEYS_PATH.stat().st_mtime
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="keys.json missing")
    if _keys_cache["data"] is None or mtime != _keys_cache["mtime"]:
        _keys_cache["data"] = load_keys()
        _keys_cache["mtime"] = mtime
    return _keys_cache["data"]


def require_api_key(x_api_key: Optional[str]):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key")
    keys = get_keys()
    info = keys.get(x_api_key)
    if not info:
        raise HTTPException(status_code=401, detail="Invalid API key")
    # A key handed out for a trial should stop working on its own. Keys without
    # an "expires" field are permanent, so this changes nothing for them.
    expires = info.get("expires")
    if expires and datetime.now(timezone.utc).isoformat() > expires:
        raise HTTPException(status_code=401,
                            detail=f"API key expired on {expires[:10]}")
    return x_api_key, info


# ---------------- Rate limiting (token bucket, in-memory per key) ----------------

class TokenBucket:
    def __init__(self, rate_per_min: int):
        self.rate_per_min = rate_per_min
        self.capacity = rate_per_min
        self.tokens = rate_per_min
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.last = now
            self.tokens = min(self.capacity, self.tokens + elapsed * (self.rate_per_min / 60.0))
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


_buckets = {}
_buckets_lock = threading.Lock()


def check_rate_limit(api_key: str, rate_per_min: int):
    with _buckets_lock:
        bucket = _buckets.get(api_key)
        if bucket is None or bucket.rate_per_min != rate_per_min:
            bucket = TokenBucket(rate_per_min)
            _buckets[api_key] = bucket
    if not bucket.allow():
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


# ---------------- Simple TTL LRU cache for /v1/search ----------------

class TTLCache:
    def __init__(self, ttl_seconds=600, max_size=500):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self.data = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            item = self.data.get(key)
            if item is None:
                return None
            value, expires = item
            if time.time() > expires:
                del self.data[key]
                return None
            self.data.move_to_end(key)
            return value

    def set(self, key, value):
        with self.lock:
            self.data[key] = (value, time.time() + self.ttl)
            self.data.move_to_end(key)
            while len(self.data) > self.max_size:
                self.data.popitem(last=False)


search_cache = TTLCache(ttl_seconds=600, max_size=500)
embedding_cache = TTLCache(ttl_seconds=1800, max_size=2000)
qdrant_result_cache = TTLCache(ttl_seconds=600, max_size=1000)


# ---------------- lexical half of the search ----------------
# Dense retrieval fails on exact things: a query naming GRPO, a durable nonce or
# one paper's method finds topical neighbours and misses the term itself. BM25
# has the opposite failure mode, so the two are fused rather than chosen
# between. The lexical index is an FTS5 table built from the payloads Qdrant
# already holds; sparse vectors inside Qdrant would have meant recreating a
# collection of 820k points for the same effect.
FTS_DB_PATH = os.getenv("FTS_DB_PATH", "/opt/dtox-research/fts.db")
BM25_CANDIDATES = 40
LEXICAL_QUERY_TIMEOUT = float(os.getenv("LEXICAL_QUERY_TIMEOUT", "2"))
SEARCH_EMBED_WAIT_TIMEOUT = float(os.getenv("SEARCH_EMBED_WAIT_TIMEOUT", "2"))
SEARCH_EMBED_HTTP_TIMEOUT = float(os.getenv("SEARCH_EMBED_HTTP_TIMEOUT", "4"))
SEARCH_QDRANT_TIMEOUT = float(os.getenv("SEARCH_QDRANT_TIMEOUT", "6"))


def _fts_query(text):
    """FTS5 MATCH string: words ANDed, operators stripped, phrases quoted."""
    return fts_query(text)


def _bm25_candidates(query, layer=None, section_type=None, element_type=None,
                     year_from=None, year_to=None, limit=BM25_CANDIDATES):
    """Paper ids whose text actually contains the query's words, best first."""
    match = _fts_query(query)
    if not match:
        return []
    where, params = ["chunks MATCH ?"], [match]
    if layer:
        where.append("layers LIKE ?")
        params.append(f"%{layer}%")
    if section_type:
        where.append("section_type = ?")
        params.append(section_type)
    if element_type:
        where.append("element_type = ?")
        params.append(element_type)
    if year_from is not None:
        where.append("CAST(year AS INTEGER) >= ?")
        params.append(year_from)
    if year_to is not None:
        where.append("CAST(year AS INTEGER) <= ?")
        params.append(year_to)
    sql = (f"SELECT arxiv_id, bm25(chunks) AS rank FROM chunks "
           f"WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?")
    try:
        conn = sqlite3.connect(f"file:{FTS_DB_PATH}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return []
    try:
        deadline = time.monotonic() + LEXICAL_QUERY_TIMEOUT
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        rows = conn.execute(sql, params + [limit * 3]).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    seen, out = set(), []
    for arxiv_id, _rank in rows:
        if arxiv_id and arxiv_id not in seen:
            seen.add(arxiv_id)
            out.append(arxiv_id)
        if len(out) >= limit:
            break
    return out


# Asking for a specific paper and asking about a subject look identical to a
# vector search, and it answers both the same way: with whatever is nearest.
# A query for FlashAttention came back with two papers on other things and no
# hint that the paper itself is simply not indexed. That is the same failure the
# verdicts had -- confident where it should own up -- so a lookup that finds no
# matching title now says so, without changing what it returns.
_ARXIV_ID_RE = re.compile(r"^\s*(arxiv:)?(\d{4}\.\d{4,5})(v\d+)?\s*$", re.I)
TITLE_MATCH_OVERLAP = 0.6


def _looks_like_a_title(query):
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", query or "")]
    if len(words) < 3:
        return False
    # a title carries capitals or a distinctive name; a topic query rarely does
    return sum(1 for w in words if w[:1].isupper()) >= 2


def _title_overlap(query, title):
    q = {w.lower() for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 2}
    t = {w.lower() for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 2}
    return len(q & t) / max(1, len(q))


def _paper_in_index(arxiv_id):
    try:
        conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT status FROM papers WHERE arxiv_id=?",
                           (arxiv_id,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _lookup_note(query, results):
    """Tell the caller when a request for one paper found only neighbours."""
    m = _ARXIV_ID_RE.match(query or "")
    if m:
        pid = m.group(2)
        status = _paper_in_index(pid)
        if status == "done":
            return None
        return {"asked_for": pid, "exact_match": False,
                "status_in_index": status or "not harvested",
                "note": ("That identifier is not available as an indexed paper. "
                         "The results below are the nearest work by subject, not "
                         "that paper.")}
    if not _looks_like_a_title(query):
        return None
    if any(_title_overlap(query, r.get("title")) >= TITLE_MATCH_OVERLAP for r in results):
        return None
    return {"exact_match": False,
            "note": ("This reads like a request for one specific paper, and no "
                     "title in the index matches it. What follows is the nearest "
                     "work by subject. The index covers three subjects and is not "
                     "complete inside them, so a missing paper is ordinary.")}


def _rrf(*ranked_lists, weights=None):
    """Reciprocal rank fusion over lists of ids, best first.

    The lists are not equals. Dense retrieval is the primary channel and the
    lexical one is there to add what it misses; fused at equal weight the two
    tie at every rank and the tie-break is arbitrary, which cost two harness
    cases their verdict the moment hybrid search was switched on."""
    return reciprocal_rank_fusion(*ranked_lists, weights=weights)


# ---------------- helpers ----------------

# The API and the ingestion pipeline share one embedder, and the pipeline is
# what must not suffer: it is the thing building the asset. Measured, a search
# costs one short embedding (p50 0.05s) while an audit costs a dozen (p50 3.8s,
# p95 23s under two concurrent callers), so the protection is a ceiling on how
# many embeddings the API may have in flight at once, not a request count. Two
# is deliberate: the pipeline runs two embed workers, so at worst the two halves
# split the service evenly and ingestion never drops below half speed.
API_EMBED_SLOTS = threading.Semaphore(2)
EMBED_WAIT_TIMEOUT = 25


def embed_query(text: str, wait_timeout=EMBED_WAIT_TIMEOUT,
                request_timeout=30) -> List[float]:
    text = (text or "").strip()
    cached = embedding_cache.get(text)
    if cached is not None:
        return cached
    if not API_EMBED_SLOTS.acquire(timeout=wait_timeout):
        raise HTTPException(
            status_code=503,
            detail="the embedding service is busy building the index; retry shortly")
    try:
        resp = requests.post(EMBED_URL, json={"text": text, "query": True},
                             timeout=request_timeout)
        resp.raise_for_status()
        vector = resp.json()["vector"]
        embedding_cache.set(text, vector)
        return vector
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"embed service error: {e}")
    finally:
        API_EMBED_SLOTS.release()


def prime_query_embeddings(texts):
    """Embed the known audit phrasings in small batches before retrieval.

    validate_project used to run the same ONNX graph separately for the idea,
    claim, and every rephrasing, then repeat several of them during coverage.
    The passage batch endpoint accepts raw text, so prepend the exact BGE query
    instruction here and seed the ordinary embedding cache with its vectors.
    Unknown corpus-vocabulary expansions still fall back to embed_query.
    """
    pending = []
    seen = set()
    for raw in texts:
        text = (raw or "").strip()
        if text and text not in seen and embedding_cache.get(text) is None:
            seen.add(text)
            pending.append(text)
    for start in range(0, len(pending), 8):
        batch = pending[start:start + 8]
        if not API_EMBED_SLOTS.acquire(timeout=EMBED_WAIT_TIMEOUT):
            return
        try:
            resp = requests.post(
                EMBED_BATCH_URL,
                json={"texts": [QUERY_INSTRUCTION + text for text in batch]},
                timeout=60,
            )
            resp.raise_for_status()
            vectors = resp.json().get("vectors") or []
            if len(vectors) != len(batch):
                return
            for text, vector in zip(batch, vectors):
                embedding_cache.set(text, vector)
        except requests.RequestException:
            return
        finally:
            API_EMBED_SLOTS.release()


def _qdrant_cache_key(vector, qfilter):
    vector_key = hashlib.sha1(
        json.dumps(vector, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    filter_key = json.dumps(qfilter, sort_keys=True, separators=(",", ":")) if qfilter else ""
    return vector_key, filter_key


def prime_qdrant_searches(texts, layer, limit=80):
    """Batch the known layer-filtered searches of one audit into one request.

    Scope, coverage, and evidence all ask Qdrant for the same vector and layer.
    Priming their shared cache through search/batch removes the serial network
    and scheduler round-trips. Unscoped audits are left alone because priming
    every text across all three layers would multiply work rather than merge it.
    """
    if not layer:
        return
    qfilter = {"must": [{"key": "layers", "match": {"any": [layer]}}]}
    searches, keys = [], []
    seen = set()
    for raw in texts:
        text = (raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        vector = embedding_cache.get(text)
        if vector is None:
            continue
        key = _qdrant_cache_key(vector, qfilter)
        cached = qdrant_result_cache.get(key)
        if cached is not None and cached["limit"] >= limit:
            continue
        searches.append({"vector": vector, "filter": qfilter, "limit": limit,
                         "with_payload": True})
        keys.append(key)
    if not searches:
        return
    try:
        resp = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/search/batch",
            json={"searches": searches}, timeout=min(12, SEARCH_QDRANT_TIMEOUT * 2),
        )
        resp.raise_for_status()
        result_sets = resp.json().get("result") or []
        if len(result_sets) != len(keys):
            return
        for key, results in zip(keys, result_sets):
            qdrant_result_cache.set(key, {"limit": limit, "results": results})
    except requests.RequestException:
        return


def qdrant_search(vector, qfilter, limit, timeout=45):
    cache_key = _qdrant_cache_key(vector, qfilter)
    cached = qdrant_result_cache.get(cache_key)
    if cached is not None and cached["limit"] >= limit:
        return cached["results"][:limit]
    body = {"vector": vector, "limit": limit, "with_payload": True}
    if qfilter:
        body["filter"] = qfilter
    try:
        resp = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
                             json=body, timeout=timeout)
        resp.raise_for_status()
        results = resp.json()["result"]
        qdrant_result_cache.set(cache_key, {"limit": limit, "results": results})
        return results
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"qdrant error: {e}")


def qdrant_search_layers(vector, base_filter, limit, timeout=SEARCH_QDRANT_TIMEOUT):
    """Search each topical payload index and RRF the lists.

    A global HNSW request over millions of chunks became the slowest public
    path as the corpus grew. Per-layer filters use the payload indexes and stop
    the LLM-heavy layer from suppressing smaller Web3 and builder-tech layers.
    """
    def search_layer(layer):
        must = list((base_filter or {}).get("must") or [])
        must.append({"key": "layers", "match": {"any": [layer]}})
        return qdrant_search(vector, {"must": must}, limit, timeout=timeout)

    result_sets, failed = [], []
    with ThreadPoolExecutor(max_workers=len(ALLOWED_LAYERS)) as pool:
        futures = {pool.submit(search_layer, layer): layer
                   for layer in sorted(ALLOWED_LAYERS)}
        for future in as_completed(futures):
            layer = futures[future]
            try:
                result_sets.append(future.result())
            except HTTPException:
                failed.append(layer)
    if not result_sets:
        raise HTTPException(status_code=502, detail="qdrant layer searches timed out")
    return merge_layer_hits(result_sets, limit), sorted(failed)


def qdrant_scroll_by_arxiv(arxiv_id: str, limit=1000):
    qfilter = {"must": [{"key": "arxiv_id", "match": {"value": arxiv_id}}]}
    points = []
    offset = None
    while True:
        body = {"filter": qfilter, "limit": min(limit, 200), "with_payload": True, "with_vector": False}
        if offset:
            body["offset"] = offset
        try:
            resp = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll", json=body, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"qdrant error: {e}")
        result = resp.json()["result"]
        points.extend(result["points"])
        offset = result.get("next_page_offset")
        if not offset or len(points) >= limit:
            break
    return points


# ---------------------------------------------------------------------------
# Source attribution
# ---------------------------------------------------------------------------
# Every answer has to point back at something a reader can actually open.
# The id prefix tells us which source a document came from; without this the
# whole index was linked as if it were arXiv, which silently produced dead
# links for IACR papers, protocol specs and the industry whitepapers.
# \rotatebox, \scalebox and friends wrap a table without containing its body,
# so a chunk can be a full table environment and still hold no measurements.
# Returning those under extract_tables_only spends the caller's budget on
# preamble: one D2O request burned 1630 characters on column headers alone.
_DIGIT_RE = re.compile(r"\d")


def _numeric_density(text):
    """Share of characters that are digits.

    A raw digit count is fooled by ontsize{18}{24} and caption numbers: the
    D2O table preamble carries 90 digits and not one measurement. The body of a
    results table is an order of magnitude denser, so density both ranks the
    useful parts first and identifies the ones that are pure formatting."""
    text = text or ""
    return len(_DIGIT_RE.findall(text)) / max(1, len(text))


TABLE_MIN_DENSITY = 0.08


SOURCE_LABELS = {
    "iacr": "IACR ePrint",
    "eip": "Ethereum EIP",
    "simd": "Solana SIMD",
    "wp": "Industry whitepaper",
    "oa": "OpenAlex open-access work",
    "pmlr": "Proceedings of Machine Learning Research",
    "hal": "HAL Open Science",
    "gh": "GitHub technical documentation",
    "arxiv": "arXiv",
}


def source_of(paper_id):
    pid = str(paper_id or "")
    prefix = pid.split(":", 1)[0] if ":" in pid else "arxiv"
    return prefix if prefix in SOURCE_LABELS else "arxiv"


def source_url(paper_id, payload=None):
    """Canonical public URL for a document, by source."""
    pid = str(paper_id or "")
    # a whitepaper carries its own origin: it was fetched from a project site
    if payload and payload.get("url"):
        return payload["url"]
    if pid.startswith("iacr:"):
        return f"https://eprint.iacr.org/{pid.split(':', 1)[1]}"
    if pid.startswith("eip:"):
        return f"https://eips.ethereum.org/EIPS/eip-{pid.split(':', 1)[1]}"
    if pid.startswith("simd:"):
        return ("https://github.com/solana-foundation/solana-improvement-documents"
                f"/blob/main/proposals/{pid.split(':', 1)[1]}")
    if pid.startswith("wp:"):
        return None  # curated document with no stored origin yet
    if pid.startswith("oa:"):
        return payload.get("url") if payload else f"https://openalex.org/{pid.split(':', 1)[1]}"
    if pid.startswith("pmlr:"):
        return payload.get("url") if payload else f"https://proceedings.mlr.press/{pid.split(':', 1)[1]}.html"
    if pid.startswith("hal:"):
        return payload.get("url") if payload else f"https://hal.science/hal-{pid.split(':', 1)[1]}"
    if pid.startswith("gh:"):
        return payload.get("url") if payload else None
    return f"https://arxiv.org/abs/{pid}"


# ---------------------------------------------------------------------------
# Licensing
# ---------------------------------------------------------------------------
# We index other people's work, so every fragment we hand out says where it
# came from and under what terms. Two of the sources have a single clear
# licence; for arXiv and the industry whitepapers the terms belong to each
# individual document, and pretending otherwise would be worse than saying so.
LICENSE_INFO = {
    "eip": {
        "spdx": "CC0-1.0",
        "terms": "Public domain dedication (Ethereum EIPs repository).",
        "redistribution": "unrestricted",
    },
    "simd": {
        "spdx": None,
        "terms": "Solana Improvement Documents, published openly on GitHub.",
        "redistribution": "attribute the proposal and link the repository",
    },
    "iacr": {
        "spdx": None,
        "terms": "IACR ePrint: authors retain copyright. Only title and "
                 "abstract are stored here; full texts are not mirrored.",
        "redistribution": "quote briefly with attribution",
    },
    "arxiv": {
        "spdx": None,
        "terms": "Licence is set per paper by its authors (CC-BY, CC-BY-NC-SA, "
                 "or arXiv's non-exclusive licence). Check the abs page before "
                 "reusing a text beyond quotation.",
        "redistribution": "quote fragments with attribution; check the paper for reuse",
    },
    "wp": {
        "spdx": None,
        "terms": "Published by the project itself and freely available; "
                 "copyright normally stays with the authors.",
        "redistribution": "quote fragments with attribution and a link to the original",
    },
    "oa": {
        "spdx": None,
        "terms": "Open-access copy discovered through OpenAlex. Licence is set per work; check the linked source before reuse.",
        "redistribution": "quote fragments with attribution; check the source licence",
    },
    "pmlr": {
        "spdx": None,
        "terms": "PMLR proceedings are published as freely accessible papers; copyright and reuse terms remain paper-specific.",
        "redistribution": "quote fragments with attribution and link the paper page",
    },
}

USAGE_NOTICE = (
    "This is a search index, not a mirror: rights to the texts stay with their "
    "authors and publishers. Fragments are returned for retrieval and citation, "
    "each with its source link and licence terms. Do not reassemble a whole "
    "document from repeated calls."
)


def license_of(paper_id):
    return LICENSE_INFO.get(source_of(paper_id), LICENSE_INFO["arxiv"])


def _result_from_hit(hit):
    payload = hit.get("payload") or {}
    paper_id = payload.get("arxiv_id")
    return {
        "arxiv_id": paper_id,
        "source": SOURCE_LABELS[source_of(paper_id)],
        "url": source_url(paper_id, payload),
        "license": license_of(paper_id),
        "title": payload.get("title"),
        "section_type": payload.get("section_type"),
        "section_title": payload.get("section_title"),
        "element_type": payload.get("element_type"),
        "year": payload.get("year"),
        "repos": payload.get("repos") or [],
        "terms": payload.get("terms") or [],
        "text": (payload.get("text") or "")[:500],
        "score": hit.get("score"),
        "layers": payload.get("layers"),
        "venue": payload.get("venue"),
        "citation_count": payload.get("citation_count"),
        "fulltext": source_of(paper_id) in FULLTEXT_SOURCES,
        "arxiv_url": source_url(paper_id, payload),
    }


def _lexical_fallback_results(query, paper_ids, limit):
    """Hydrate exact FTS hits without waiting for the vector service.

    These carry no cosine score and therefore cannot become direct evidence in
    validate_project. They keep exact identifiers and protocol terms visible
    when dense retrieval is temporarily unavailable.
    """
    ids = list(dict.fromkeys(pid for pid in paper_ids if pid))[:limit]
    if not ids:
        return []
    match = _fts_query(query)
    if not match:
        return []
    marks = ",".join("?" * len(ids))
    fts = None
    try:
        fts = sqlite3.connect(f"file:{FTS_DB_PATH}?mode=ro", uri=True, timeout=2)
        deadline = time.monotonic() + LEXICAL_QUERY_TIMEOUT
        fts.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        rows = fts.execute(
            f"SELECT text, arxiv_id, section_type, element_type, layers, year "
            f"FROM chunks WHERE chunks MATCH ? AND arxiv_id IN ({marks}) "
            f"ORDER BY bm25(chunks) LIMIT ?", [match, *ids, limit * 3]
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if fts is not None:
            fts.close()
    best = {}
    for row in rows:
        best.setdefault(row[1], row)
    try:
        state = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=2)
        meta = {row[0]: row[1:] for row in state.execute(
            f"SELECT arxiv_id,title,citation_count,venue,niche_score,matched_terms,source_url "
            f"FROM papers WHERE arxiv_id IN ({marks})", ids).fetchall()}
        state.close()
    except sqlite3.Error:
        meta = {}
    results = []
    for paper_id in ids:
        row = best.get(paper_id)
        if not row:
            continue
        title, citations, venue, niche, matched, stored_url = meta.get(
            paper_id, (None, None, None, None, None, None))
        try:
            terms = json.loads(matched) if matched else []
        except (TypeError, json.JSONDecodeError):
            terms = []
        layers = [layer for layer in sorted(ALLOWED_LAYERS) if layer in (row[4] or "")]
        payload = {"source_url": stored_url}
        results.append({
            "arxiv_id": paper_id,
            "source": SOURCE_LABELS[source_of(paper_id)],
            "url": source_url(paper_id, payload),
            "license": license_of(paper_id),
            "title": title,
            "section_type": row[2],
            "section_title": None,
            "element_type": row[3],
            "year": row[5],
            "repos": [],
            "terms": terms,
            "text": (row[0] or "")[:500],
            "score": None,
            "layers": layers,
            "venue": venue,
            "citation_count": citations,
            "fulltext": source_of(paper_id) in FULLTEXT_SOURCES,
            "arxiv_url": source_url(paper_id, payload),
            "found_via": "lexical fallback",
            "channels": ["lexical"],
            "niche_score": niche,
        })
    return results


def _scored_results_for_audit(payload):
    scored = [hit for hit in payload.get("results", [])
              if isinstance(hit.get("score"), (int, float))]
    if payload.get("partial") and not scored:
        raise HTTPException(
            status_code=503,
            detail="dense retrieval unavailable; refusing to audit from lexical-only matches")
    return scored


# ---------------- request/response models ----------------

MIN_RELEVANCE_SCORE = 0.79
# Cosine scores are not comparable across topics: bge-small was trained on
# general English, where LLM wording is far better represented than crypto
# jargon. Measured on this corpus, sound blockchain queries land at 0.72-0.77
# while sound LLM queries land at 0.82-0.92, so a single floor either lets
# nonsense through on one side or silently drops real answers on the other.
MIN_RELEVANCE_BY_LAYER = {"web3": 0.70}


class SearchBody(BaseModel):
    query: str
    layer: Optional[str] = None
    section_type: Optional[str] = None
    # structural facets: element_type isolates equations/algorithms/tables/code,
    # terms pins the hit to concrete technical vocabulary ("kv cache", "x402")
    element_type: Optional[str] = None
    terms: Optional[List[str]] = None
    # one hit per paper by default; several chunks of the same paper crowding
    # the result list is what an agent least wants to spend context on
    dedupe: bool = True
    # cosine scores on this model sit high across the board, so an off-topic
    # query still returns ~0.75 hits that look confident. Measured separation:
    # on-topic queries land 0.815-0.922, off-topic ones (cake recipes, hotels,
    # flu symptoms) top out at 0.766. Anything under the floor is reported as
    # no result rather than as a plausible-looking wrong answer.
    min_score: Optional[float] = None
    # publication-year window; the corpus spans 2004-2026 and in these fields
    # a 2019 result can be actively misleading, so recency is a first-class filter
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    limit: int = Field(default=8, le=50, ge=1)
    # lexical recall on top of the vector search; off only for measurement
    hybrid: bool = True
    # when a stack of filters returns nothing, the caller cannot tell which one
    # did it: element_type + terms + year_from each work alone and together
    # return zero. On an empty result the query is re-run with one filter
    # dropped at a time, and the answer names the culprit. Set False to skip it.
    diagnose: bool = True


# ---------------- auth + rate-limit dependency ----------------

# One allowance for every endpoint meant a caller could spend the whole budget
# on audits, each of which costs a hundred times what a search does. The cheap
# path keeps the old limit; the expensive ones get their own, much smaller.
COSTLY_LIMITS = {"validate": 8, "adjudicate": 20, "trends": 12}


def auth_and_limit(x_api_key: Optional[str], cost: str = "search"):
    api_key, info = require_api_key(x_api_key)
    rate = info.get("rate_limit_per_min", 60)
    check_rate_limit(api_key, rate)
    if cost in COSTLY_LIMITS:
        # a separate bucket, keyed by endpoint, so a burst of audits cannot
        # starve the pipeline through the shared embedder
        check_rate_limit(f"{api_key}:{cost}",
                         info.get(f"{cost}_per_min", COSTLY_LIMITS[cost]))
    return api_key, info


# ---------------- endpoints ----------------

@app.post("/v1/search")
def search(body: SearchBody, x_api_key: Optional[str] = Header(default=None)):
    auth_and_limit(x_api_key)
    return _run_search(body, x_api_key)


def _run_search(body: SearchBody, x_api_key=None,
                dense_timeout=SEARCH_QDRANT_TIMEOUT,
                embed_wait_timeout=SEARCH_EMBED_WAIT_TIMEOUT,
                embed_http_timeout=SEARCH_EMBED_HTTP_TIMEOUT):
    """The search itself, without the rate limiter.

    /v1/validate runs several searches per claim (each phrasing, the corpus
    vocabulary expansion, the limitations lookup) and every one of them used to
    be charged to the caller's 60-per-minute budget. One audit could spend five
    requests, and the regression harness tripped the limit on its own traffic.
    """

    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    if body.layer is not None and body.layer not in ALLOWED_LAYERS:
        raise HTTPException(status_code=400, detail=f"layer must be one of {sorted(ALLOWED_LAYERS)}")
    limit = min(max(body.limit, 1), 50)

    terms = [t.strip().lower() for t in (body.terms or []) if t and t.strip()]
    cache_key = (query, body.layer, body.section_type, body.element_type,
                 tuple(sorted(terms)), body.dedupe, body.min_score,
                 body.year_from, body.year_to, limit, body.hybrid)
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached

    must = []
    if body.layer:
        must.append({"key": "layers", "match": {"any": [body.layer]}})
    if body.section_type:
        must.append({"key": "section_type", "match": {"value": body.section_type}})
    if body.element_type:
        must.append({"key": "element_type", "match": {"value": body.element_type}})
    if terms:
        must.append({"key": "terms", "match": {"any": terms}})
    if body.year_from is not None or body.year_to is not None:
        rng = {}
        if body.year_from is not None:
            rng["gte"] = body.year_from
        if body.year_to is not None:
            rng["lte"] = body.year_to
        must.append({"key": "year", "range": rng})
    qfilter = {"must": must} if must else None

    lexical_executor = ThreadPoolExecutor(max_workers=1) if body.hybrid else None
    lexical_future = (lexical_executor.submit(
        _bm25_candidates, query, body.layer, body.section_type,
        body.element_type, body.year_from, body.year_to)
        if lexical_executor else None)
    vector, hits, dense_error, dense_partial = None, [], None, []
    dense_limit = limit * 4 if body.dedupe else limit
    try:
        vector = embed_query(query, wait_timeout=embed_wait_timeout,
                             request_timeout=embed_http_timeout)
        if body.layer:
            hits = qdrant_search(vector, qfilter, dense_limit,
                                  timeout=dense_timeout)
        else:
            hits, dense_partial = qdrant_search_layers(
                vector, qfilter, dense_limit, timeout=dense_timeout)
    except HTTPException as error:
        dense_error = error
    try:
        lexical = lexical_future.result() if lexical_future else []
    finally:
        if lexical_executor:
            lexical_executor.shutdown(wait=False, cancel_futures=True)
    if dense_error and not lexical:
        raise dense_error

    results = []
    seen_papers = set()
    if body.min_score is not None:
        floor = body.min_score
    elif body.layer:
        floor = MIN_RELEVANCE_BY_LAYER.get(body.layer, MIN_RELEVANCE_SCORE)
    else:
        # unfiltered search spans every topic, so use the gentler bar and let
        # ranking sort it out rather than dropping a whole layer's answers
        floor = min([MIN_RELEVANCE_SCORE] + list(MIN_RELEVANCE_BY_LAYER.values()))
    for h in hits:
        if (h.get("score") or 0) < floor:
            continue
        p = h["payload"]
        if body.dedupe:
            aid = p.get("arxiv_id")
            if aid in seen_papers:
                continue
            seen_papers.add(aid)
        if len(results) >= limit:
            break
        results.append(_result_from_hit(h))

    # lexical candidates the vectors missed, scored by the same vector search so
    # the two halves stay comparable, then fused by rank rather than by score
    if body.hybrid and lexical:
        known = {r["arxiv_id"] for r in results}
        # label every result, not only the runs that gained a lexical extra:
        # otherwise the channel is invisible exactly when the two agree
        lex_all = set(lexical)
        for r in results:
            r["channels"] = ["dense", "lexical"] if r["arxiv_id"] in lex_all else ["dense"]
        extra_ids = [pid for pid in lexical if pid not in known][:BM25_CANDIDATES]
        scored = []
        if extra_ids and vector is not None and dense_error is None:
            scored = _score_specific_papers(query, extra_ids, body.layer,
                                             vector=vector,
                                             timeout=dense_timeout,
                                             constraints=must)
            for h in scored:
                h["found_via"] = "lexical match"
                h["channels"] = ["lexical"]
        scored_ids = {item["arxiv_id"] for item in scored}
        raw = ([] if terms else _lexical_fallback_results(
            query, [pid for pid in extra_ids if pid not in scored_ids], limit))
        dense_order = [r["arxiv_id"] for r in results]
        fused = _rrf(dense_order, lexical, weights=[1.0, 0.6])
        lex_set = set(lexical)
        for r in results:
            r["channels"] = (["dense", "lexical"] if r["arxiv_id"] in lex_set
                             else ["dense"])
        pool = {r["arxiv_id"]: r for r in results}
        for item in [*scored, *raw]:
            pool.setdefault(item["arxiv_id"], item)
        for item in pool.values():
            item["fusion_score"] = round(fused.get(item["arxiv_id"], 0), 6)
        results = sorted(pool.values(),
                         key=lambda r: -r.get("fusion_score", 0))[:limit]
    elif results:
        for result in results:
            result["channels"] = ["dense"]

    facts = _paper_facts([r["arxiv_id"] for r in results])
    for r in results:
        r["niche_score"] = facts.get(r["arxiv_id"])
        r["rank_score"] = round(_rank_score(r), 4)
    if not any("fusion_score" in result for result in results):
        results.sort(key=lambda r: -r["rank_score"])
    payload = {"results": results, "count": len(results), "usage": USAGE_NOTICE}
    if dense_error:
        payload["partial"] = True
        payload["retrieval_warning"] = (
            "dense retrieval exceeded its latency budget; exact lexical matches "
            "are returned without cosine scores")
    elif dense_partial:
        payload["partial"] = True
        payload["retrieval_warning"] = (
            "dense retrieval exceeded its latency budget in layers: "
            + ", ".join(dense_partial))
    lookup = _lookup_note(query, results)
    if lookup:
        payload["title_lookup"] = lookup
    applied = {k: v for k, v in (("layer", body.layer), ("section_type", body.section_type),
                                 ("element_type", body.element_type), ("terms", body.terms),
                                 ("year_from", body.year_from), ("year_to", body.year_to),
                                 ("min_score", body.min_score)) if v is not None}
    if applied:
        payload["filters_applied"] = applied
    if not results and applied and body.diagnose and not dense_error:
        relaxations = []
        for name in applied:
            probe = body.model_copy(update={name: None, "diagnose": False, "limit": 3})
            try:
                got = _run_search(probe, x_api_key).get("results", [])
            except HTTPException:
                continue
            if got:
                relaxations.append({"drop": name, "would_return": len(got),
                                    "example": got[0]["title"][:80]})
        payload["why_empty"] = (
            {"relax_one_of": relaxations} if relaxations else
            {"relax_one_of": [], "note": "no single filter explains it: the query itself "
                                         "has no match above the relevance floor"})
    if not dense_error and not dense_partial:
        search_cache.set(cache_key, payload)
    return payload


# An agent asking for "the spec" wants the algorithm block and the equations,
# not the whole methods narrative: returning every method chunk of a survey
# costs ~66k tokens, which defeats the point of the endpoint.
SPEC_ELEMENT_TYPES = ("algorithm", "equation", "code", "table")
SPEC_MAX_CHARS = 12000


@app.get("/v1/paper/{arxiv_id}/spec")
def paper_spec(
    arxiv_id: str,
    target_elements: Optional[str] = None,
    include_prose: bool = False,
    max_chars: int = SPEC_MAX_CHARS,
    x_api_key: Optional[str] = Header(default=None),
):
    """Structured spec of a paper: math, pseudocode and architecture.

    target_elements: comma-separated subset of algorithm,equation,code,table
                     (default: all four). Prose is excluded unless
                     include_prose=true, and output is capped so the caller
                     can budget context.
    """
    auth_and_limit(x_api_key)

    wanted = SPEC_ELEMENT_TYPES
    if target_elements:
        wanted = tuple(t.strip().lower() for t in target_elements.split(",") if t.strip())
        bad = [t for t in wanted if t not in SPEC_ELEMENT_TYPES + ("prose",)]
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown element types: {bad}")

    points = qdrant_scroll_by_arxiv(arxiv_id)
    if not points:
        raise HTTPException(status_code=404, detail="paper not found")

    def is_spec(p):
        st = (p.get("section_type") or "").lower()
        title = (p.get("section_title") or "").lower()
        return st in SPEC_SECTION_TYPES or any(kw in title for kw in SPEC_TITLE_KEYWORDS)

    payloads = [pt["payload"] for pt in points]
    elements = [p for p in payloads if (p.get("element_type") or "").lower() in wanted]
    if include_prose:
        elements += [p for p in payloads
                     if (p.get("element_type") or "").lower() == "prose" and is_spec(p)]
    if not elements:
        # the paper is in the database, it simply has no math or pseudocode:
        # a distinct answer from "unknown paper", so an automated review can
        # note the absence and carry on instead of breaking on a 404
        return {
            "arxiv_id": arxiv_id,
            "title": (payloads[0].get("title") if payloads else None),
            "repos": [],
            "sections": [],
            "returned": 0,
            "available": 0,
            "chars": 0,
            "truncated": False,
            "note": "paper present but contains no algorithm/equation/code/table elements"
                    " (try include_prose=true for its method prose)",
        }

    elements.sort(key=lambda p: p.get("chunk_index", 0))
    title = elements[0].get("title")

    sections, used, truncated = [], 0, False
    for p in elements:
        text = p.get("text") or ""
        if used + len(text) > max_chars:
            truncated = True
            break
        used += len(text)
        sections.append({
            "element_type": p.get("element_type"),
            "section_type": p.get("section_type"),
            "section_title": p.get("section_title"),
            "text": text,
        })

    repos = []
    for p in elements:
        for r in (p.get("repos") or []):
            if r not in repos:
                repos.append(r)

    return {
        "arxiv_id": arxiv_id,
        "source": SOURCE_LABELS[source_of(arxiv_id)],
        "url": source_url(arxiv_id, elements[0] if elements else None),
        "license": license_of(arxiv_id),
        "title": title,
        "repos": repos,
        "sections": sections,
        "returned": len(sections),
        "available": len(elements),
        "chars": used,
        "truncated": truncated,
    }


@app.get("/v1/compare")
def compare(
    a: str,
    b: str,
    extract_tables_only: bool = False,
    max_chars: int = 12000,
    x_api_key: Optional[str] = Header(default=None),
):
    """Benchmark sections of two papers, side by side.

    Comparison itself is left to the caller: this only isolates the evidence.
    extract_tables_only=true narrows to table elements, which is usually where
    latency/memory numbers live.
    """
    auth_and_limit(x_api_key)

    if not a or not b:
        raise HTTPException(status_code=400, detail="a and b query params required")

    # our taxonomy calls the results section "experiments"; asking for
    # "results" here is what previously made this endpoint return nothing
    wanted_sections = ("experiments", "analysis", "limitations")

    def get_results_chunks(arxiv_id):
        points = qdrant_scroll_by_arxiv(arxiv_id)
        if not points:
            raise HTTPException(status_code=404, detail=f"paper not found: {arxiv_id}")
        payloads = [pt["payload"] for pt in points]
        filtered = [p for p in payloads
                    if (p.get("section_type") or "").lower() in wanted_sections]
        empty_tables = 0
        if extract_tables_only:
            filtered = [p for p in filtered
                        if (p.get("element_type") or "").lower() == "table"]
            if not filtered:  # fall back to any table in the paper
                filtered = [p for p in payloads
                            if (p.get("element_type") or "").lower() == "table"]
            with_numbers = [p for p in filtered
                            if _numeric_density(p.get("text")) >= TABLE_MIN_DENSITY]
            empty_tables = len(filtered) - len(with_numbers)
            # densest first: the caller asked for numbers, so spend the budget
            # on the parts that hold them rather than on the table preamble
            with_numbers.sort(key=lambda p: -_numeric_density(p.get("text")))
            filtered = with_numbers
        if not extract_tables_only:
            filtered.sort(key=lambda p: p.get("chunk_index", 0))
        title = payloads[0].get("title")
        chunks, used = [], 0
        for p in filtered:
            text = p.get("text") or ""
            if used + len(text) > max_chars:
                # one chunk wider than the whole budget used to return nothing
                # at all: a paper with 60 result tables answered "available: 60,
                # chunks: 0". Better a clipped first table than an empty hand.
                if not chunks:
                    chunks.append({
                        "element_type": p.get("element_type"),
                        "section_type": p.get("section_type"),
                        "section_title": p.get("section_title"),
                        "text": text[:max_chars],
                        "clipped": True,
                    })
                    used = max_chars
                break
            used += len(text)
            chunks.append({
                "element_type": p.get("element_type"),
                "section_type": p.get("section_type"),
                "section_title": p.get("section_title"),
                "text": text,
            })
        return {
            "arxiv_id": arxiv_id,
            "skipped_tables_without_numbers": empty_tables,
            "source": SOURCE_LABELS[source_of(arxiv_id)],
            "url": source_url(arxiv_id, payloads[0] if payloads else None),
            "license": license_of(arxiv_id),
            "title": title,
            "results_chunks": chunks,
            "available": len(filtered),
            "chars": used,
        }

    return {"a": get_results_chunks(a), "b": get_results_chunks(b)}


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------
# Term counts per year come from the pipeline's own state database rather than
# from Qdrant: the vector store holds one row per chunk, so counting there
# would weigh a long paper more heavily than a short one. state.db has exactly
# one row per paper. It is opened read-only, and SQLite WAL lets this coexist
# with the writing pipeline.
STATE_DB_PATH = "/opt/dtox-research/state.db"
TRENDS_CACHE = TTLCache(ttl_seconds=1800, max_size=64)


TOPIC_PAPER_LIMIT = 600      # enough papers for year-over-year shares to mean anything
TOPIC_MIN_SCORE = 0.83   # loose topics pull in agentic-RAG noise; measured: 0.78 gave 471 papers with "agentic" on top


def _papers_about(topic, layer=None):
    """arXiv ids of the papers on one subject, for trends inside a topic.

    "What replaced X" is the question people actually ask, and it cannot be
    answered over the whole corpus: every technique inside KV-cache compression
    is a rounding error against the whole LLM literature. This runs one semantic
    query, straight at Qdrant rather than through the 50-result search endpoint,
    and hands the ids to the same counting code."""
    must = [{"key": "layers", "match": {"any": [layer]}}] if layer else []
    vector = embed_query(topic)
    # Asking for 1,800 chunks became slower than Qdrant's 30-second request
    # budget once the collection passed five million points. Trends need a
    # representative topic sample, not an exhaustive nearest-neighbour scan.
    # 200 candidates cover the high-confidence neighbourhood used by trend
    # analysis. Larger requests time out against a five-million-point live
    # collection while mostly adding duplicate chunks below TOPIC_MIN_SCORE.
    candidate_limit = min(TOPIC_PAPER_LIMIT, 200)
    hits = qdrant_search(vector, {"must": must} if must else None, candidate_limit)
    ids = []
    seen = set()
    for h in hits:
        if h.get("score", 0) < TOPIC_MIN_SCORE:
            continue
        pid = (h.get("payload") or {}).get("arxiv_id")
        if pid and pid not in seen:
            seen.add(pid)
            ids.append(pid)
        if len(ids) >= TOPIC_PAPER_LIMIT:
            break
    return ids


def _load_term_years(layer=None, year_from=None, year_to=None, paper_ids=None):
    """Return {term: {year: paper_count}} plus {year: total_papers}."""
    import sqlite3

    where = ["status = 'done'", "matched_terms IS NOT NULL", "year IS NOT NULL"]
    params = []
    if paper_ids is not None:
        if not paper_ids:
            return {}, {}
        where.append(f"arxiv_id IN ({','.join('?' * len(paper_ids))})")
        params.extend(paper_ids)
    if layer:
        where.append("layers LIKE ?")
        params.append(f"%{layer}%")
    if year_from is not None:
        where.append("year >= ?")
        params.append(year_from)
    if year_to is not None:
        where.append("year <= ?")
        params.append(year_to)

    try:
        conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error as e:
        raise HTTPException(status_code=503, detail=f"state db unavailable: {e}")
    try:
        rows = conn.execute(
            f"SELECT year, matched_terms FROM papers WHERE {' AND '.join(where)}", params
        ).fetchall()
    finally:
        conn.close()

    per_term = {}
    per_year = {}
    for year, raw in rows:
        per_year[year] = per_year.get(year, 0) + 1
        try:
            terms = json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(terms, list):
            continue
        for term in set(terms):
            per_term.setdefault(term, {})
            per_term[term][year] = per_term[term].get(year, 0) + 1
    return per_term, per_year


@app.get("/v1/trends")
def trends(
    layer: Optional[str] = None,
    year_from: int = 2023,
    year_to: Optional[int] = None,
    top: int = 20,
    min_papers: int = 5,
    max_share: float = 0.35,
    about: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None),
):
    """Which techniques are gaining or losing ground, by year.

    Shares are normalised against the number of papers published that year, so
    a term does not look like it is growing merely because the corpus grew.
    "emerging" marks terms absent in the first year of the window and present
    in the last one.
    """
    auth_and_limit(x_api_key, "trends")
    if layer is not None and layer not in ALLOWED_LAYERS:
        raise HTTPException(status_code=400, detail=f"layer must be one of {sorted(ALLOWED_LAYERS)}")
    top = min(max(top, 1), 100)

    cache_key = (layer, year_from, year_to, top, min_papers, max_share, about)
    cached = TRENDS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    topic_ids = None
    if about and about.strip():
        topic_ids = _papers_about(about.strip(), layer)
        # a narrow topic has fewer papers per year, so the default floor of 5
        # would silently empty the answer
        min_papers = min(min_papers, 3)
    per_term, per_year = _load_term_years(layer, year_from, year_to, topic_ids)
    years = sorted(per_year)
    if not years:
        return {"years": [], "trends": [], "note": "no papers in this window"}
    first_year, last_year = years[0], years[-1]

    # "language model" appears in 94% of the llm-slm corpus. Its share cannot
    # move, so its growth number is noise from normalisation, and it crowds out
    # the terms a reader actually wants. Anything this ubiquitous is the field's
    # vocabulary rather than a technique competing inside it.
    corpus_papers = sum(per_year.values()) or 1
    # 0.35 was tuned on llm-slm, where "language model" occupies 89%. In web3
    # the vocabulary is specific enough that the same constant excludes nothing
    # and would eventually exclude something real. The cut follows the layer's
    # own distribution: a term far above the typical term is vocabulary.
    shares = sorted(sum(by_year.values()) / corpus_papers
                    for by_year in per_term.values()) or [0]
    typical = shares[len(shares) // 2]
    adaptive_share = max(0.25, min(max_share, typical * 40))
    max_share = adaptive_share
    ubiquitous = []
    items = []
    for term, by_year in per_term.items():
        total = sum(by_year.values())
        if total < min_papers:
            continue
        share_overall = total / corpus_papers
        if share_overall > max_share:
            ubiquitous.append({"term": term, "share": round(share_overall, 3)})
            continue
        # share of that year's papers, so corpus growth does not fake a trend
        first_share = by_year.get(first_year, 0) / max(1, per_year.get(first_year, 0))
        last_share = by_year.get(last_year, 0) / max(1, per_year.get(last_year, 0))
        if first_share > 0:
            growth = round((last_share - first_share) / first_share, 3)
        else:
            growth = None  # no baseline: emerging rather than growing
        items.append({
            "term": term,
            "total_papers": total,
            "by_year": {str(y): by_year.get(y, 0) for y in years},
            "share_first_year": round(first_share, 4),
            "share_last_year": round(last_share, 4),
            "growth": growth,
            "emerging": by_year.get(first_year, 0) == 0 and by_year.get(last_year, 0) > 0,
        })

    # rank by how much of the latest year a term occupies, then by momentum
    items.sort(key=lambda x: (x["share_last_year"], x["growth"] or 0), reverse=True)
    payload = {
        "layer": layer,
        "about": about,
        "papers_in_topic": len(topic_ids) if topic_ids is not None else None,
        "years": [str(y) for y in years],
        "papers_per_year": {str(y): per_year[y] for y in years},
        "trends": items[:top],
        "emerging": [i["term"] for i in items if i["emerging"]][:top],
        "excluded_as_vocabulary": sorted(ubiquitous, key=lambda x: -x["share"])[:12],
        "note": (f"Terms present in more than {int(max_share * 100)}% of the corpus are "
                 "listed under excluded_as_vocabulary instead of trends: they name the "
                 "field, not a technique competing inside it. Raise max_share to see them."),
    }
    TRENDS_CACHE.set(cache_key, payload)
    return payload


# ---------------------------------------------------------------------------
# Project validation (prior-art / feasibility audit)
# ---------------------------------------------------------------------------
# The single most dangerous output this service could produce is "no match
# found, therefore novel". Absence of evidence in a curated corpus is not
# absence in the literature, so every verdict here ships with the corpus
# coverage behind it and an explicit reading of what the number means.
#
# Score bands are empirical, measured on this index: on-topic queries land
# 0.82-0.92, adjacent work 0.78-0.85, off-topic noise tops out at 0.77.
SCORE_DIRECT = 0.87      # essentially the same thing
SCORE_ADJACENT = 0.80    # same problem area, different approach

# One scale for three layers was wrong. Measured on 18 on-topic queries, six per
# layer, top-5 hits each:
#
#   layer        p25     p50     p75
#   llm-slm     0.879   0.898   0.911
#   ai-agents   0.848   0.860   0.889
#   web3        0.779   0.806   0.817
#
# 0.87 sits near p25 for llm-slm and above p90 for web3, so a mainstream web3
# query could not reach "direct" no matter how on-topic it was, and every crypto
# claim read as open ground. The llm-slm pair is kept as the anchor (it is the
# one usage has tested) and the others are shifted by their measured offset, so
# a band means the same percentile position in every layer.
# Taken from the percentiles above rather than from a hand-picked constant:
# direct = p25 of on-topic hits (the quarter that stands out inside its own
# layer), adjacent = p10 (still on-topic, tail end). Anything under p10 is
# noise and must not be called evidence -- the previous offset-based pair put
# web3's floor at 0.71, which quietly promoted 0.759 hits into evidence and
# made "nothing here" read as "adjacent work exists".
SCORE_BANDS_BY_LAYER = {
    "llm-slm": (0.879, 0.843),
    "ai-agents": (0.848, 0.821),
    "web3": (0.779, 0.768),
    # Initial conservative bands for the new supporting-research layer. They
    # will be recalibrated once builder-tech has enough judged examples.
    "builder-tech": (0.850, 0.810),
}

# A paper that earned its layer by a single passing mention is not evidence of
# anything in that layer: a graph-database paper carrying one "blockchain" was
# top prior art for an on-chain oracle claim, ahead of the Chainlink
# whitepaper. niche_score is computed at ingest and says how strongly the paper
# belongs; 1 means one weak term fired.
MIN_NICHE_FOR_DIRECT = 2

# Computing the signal and then ranking by raw cosine anyway is the worst of
# both: a graph-database paper with one "blockchain" in it took first place
# while carrying niche_score 1 in the same response. Ranking now discounts a
# document that barely belongs to the layer it was matched in. The penalty is
# deliberately mild -- it reorders neighbours, it does not bury anything.
# A step function that only punished 1 and 2 left the ordering almost as it
# was: a survey of ChainLink oracles (niche 7) still sat below three DeFi
# papers scoring higher with niche 3-5. The factor is now smooth over the whole
# range, worth at most 6% of the score -- enough to reorder near-ties by how
# strongly a paper belongs to the subject, never enough to invent a ranking.
NICHE_RANK_FLOOR = 0.94
NICHE_RANK_FULL = 10      # niche_score at which no discount remains


def _niche_factor(niche):
    if niche is None:
        return 1.0
    capped = max(0, min(NICHE_RANK_FULL, niche))
    return NICHE_RANK_FLOOR + (1.0 - NICHE_RANK_FLOOR) * capped / NICHE_RANK_FULL


def _rank_score(hit):
    return (hit.get("score") or 0) * _niche_factor(hit.get("niche_score"))
VALIDATE_MAX_CLAIMS = 8


# Where in a paper a match lands decides what it can prove. A related-work
# chunk summarises OTHER people's results, so quoting it as evidence that this
# paper does X credits it with someone else's contribution -- that is exactly
# how a topical near-miss got reported as settled prior art. Introductions have
# a milder version of the same problem: they survey the whole field before
# narrowing, so they can support a claim but must never settle it.
CONTEXT_SECTIONS = {"related_work"}
SURVEY_SECTIONS = {"introduction"}


def _clip(text, limit):
    """Trim to a word boundary so a snippet never ends mid-word."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "..."


def _layer_of(hit, requested_layer=None):
    layers = hit.get("layers") or []
    if isinstance(layers, str):
        layers = [l.strip(" '\"[]") for l in layers.split(",")]
    if requested_layer and requested_layer in layers:
        return requested_layer
    return layers[0] if layers else requested_layer


def _bands_for(layer):
    return SCORE_BANDS_BY_LAYER.get(layer, (SCORE_DIRECT, SCORE_ADJACENT))


def _score_band(score, layer=None, corroborated=False):
    direct, adjacent = _bands_for(layer)
    if corroborated:
        direct -= BAND_EPSILON   # no coin tosses on the edge
    if score >= direct:
        return "direct"
    if score >= adjacent:
        return "adjacent"
    return "weak"





CLAIM_WORD_STOP = {"the", "a", "an", "of", "for", "with", "to", "and", "or",
                   "on", "in", "by", "that", "this", "from", "its", "their",
                   "between", "using", "use", "based", "via", "into", "over",
                   "under", "at", "as", "is", "are", "be", "how", "when",
                   "which", "than", "then", "per", "each", "any", "all"}


def _distinctive_words(text, layer=None):
    """Content words of a claim, minus the layer's own vocabulary."""
    vocab = _layer_vocabulary(layer) if layer else set()
    words = {w for w in re.findall(r"[a-z0-9][a-z0-9\-]*", (text or "").lower())
             if len(w) > 3 and w not in CLAIM_WORD_STOP}
    return {w for w in words if not any(w in v for v in vocab)}


def _title_supports(hit, claim_words):
    """Does the paper's own title carry a distinctive word of the claim?

    Used only to lift the introduction demotion, never to promote on its own.
    An introduction surveys the whole field, which is why it cannot be direct
    evidence by default -- but when the title says "Router-LLMs" and the claim
    is about routing, the paper plainly is the subject, and burying it as
    "adjacent" is how a real precedent gets reported as open ground."""
    if not claim_words:
        return False
    title_words = {w for w in re.findall(r"[a-z0-9][a-z0-9\-]*", (hit.get("title") or "").lower())
                   if len(w) > 3}
    for cw in claim_words:
        for tw in title_words:
            if cw == tw or cw.startswith(tw[:6]) and len(tw) >= 6 or tw.startswith(cw[:6]) and len(cw) >= 6:
                return True
    return False


# p25 as the entry to the top band means three quarters of typical on-topic
# hits qualify, which is how a business-process engine ended up as a "strong
# candidate" for a blockchain oracle claim. Raising the number to p50 would fix
# that and re-break the opposite case (D2O sits at 0.892 against an llm-slm p50
# of 0.898). So the top band keeps its entry score and adds corroboration: a
# strong candidate has to look like the subject's own literature by at least
# one signal that is not the cosine.
# Two harness cases flipped their verdict between identical runs, sitting at
# 0.879 against a direct band of 0.879. The score itself was stable; what moved
# was which chunk of the paper won, and with a hard edge a hundredth of a point
# decides "strong candidate" or "adjacent work". A verdict that depends on a
# coin toss is worse than one that is slightly too generous, so a corroborated
# hit within this margin of the band is treated as being at it.
BAND_EPSILON = 0.006

# Cross-encoder reranking, off unless switched on.
#
# It is the only mechanism that can tell "does this" from "writes about this",
# because it reads the claim and the passage together instead of scoring them
# apart. It is also expensive: 400 ms per pair on this host, no AVX512 for the
# quantised model, and the embedder it shares a container with is the ingestion
# bottleneck. So it fires only where the cheap signals are genuinely undecided
# -- nobody has read this claim, and the best hit sits in the grey zone around
# the band -- and never on the ordinary case that the bands already settle.
RERANK_URL = os.getenv("RERANK_URL", "http://127.0.0.1:8006/rerank")
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "0") == "1"
RERANK_UNCERTAINTY = 0.02   # distance from the direct band that counts as grey
RERANK_TOP_N = 8
CROSS_POSITIVE = 0.0        # ms-marco logits: above zero is a relevant pair
RERANK_TIMEOUT = 30


def _rerank(query, hits):
    """Cross-encoder scores for the passages of these hits, or None."""
    passages = [(h.get("text") or "")[:1200] for h in hits]
    if not passages:
        return None
    try:
        resp = requests.post(RERANK_URL, json={"query": query, "passages": passages},
                             timeout=RERANK_TIMEOUT)
        resp.raise_for_status()
        scores = resp.json().get("scores") or []
    except (requests.RequestException, ValueError):
        return None
    return scores if len(scores) == len(hits) else None


def _needs_rerank(hits, layer, registry):
    """Only where the cheap answer is a coin toss."""
    if not RERANK_ENABLED or registry or not hits:
        return False
    direct_band = _bands_for(layer)[0]
    return any(abs((h.get("score") or 0) - direct_band) <= RERANK_UNCERTAINTY
               for h in hits[:RERANK_TOP_N])
DIRECT_NICHE_CORROBORATION = 5
DIRECT_CITATION_CORROBORATION = 20
DIRECT_SECTIONS = {"method", "experiments"}


def _corroborated(hit, claim_words=None):
    # cross_top only meant "survived the reranked set"; a negative logit is the
    # model saying the passage does not answer the claim, and it was being
    # reported as support
    if hit.get("cross_top") and (hit.get("cross_score") or 0) > CROSS_POSITIVE:
        return "a cross-encoder read the claim and this passage together and agreed"
    if _title_supports(hit, claim_words):
        return "the paper's own title names the claim"
    if (hit.get("niche_score") or 0) >= DIRECT_NICHE_CORROBORATION:
        return "belongs to the subject (niche_score)"
    if (hit.get("citation_count") or 0) >= DIRECT_CITATION_CORROBORATION:
        return "cited by the field"
    if hit.get("section_type") in DIRECT_SECTIONS:
        return "matched in the paper's own method or experiments"
    return None


def _evidence_band(hit, requested_layer=None, claim_words=None):
    # a paper someone has already read against this claim does not have to earn
    # its place again through a score: that is the entire point of keeping the
    # record. It enters the evidence in its own band, so nothing is smuggled
    # into "direct" that a reader did not put there.
    if hit.get("from_registry"):
        return "registry"
    """Score band in the layer's own scale, demoted when the match sits in a
    section that surveys the field, when the paper barely belongs to the layer,
    or when nothing but the cosine supports it."""
    band = _score_band(hit["score"], _layer_of(hit, requested_layer),
                       corroborated=bool(_corroborated(hit, claim_words)))
    if band != "direct":
        return band
    if hit.get("section_type") in SURVEY_SECTIONS and not _title_supports(hit, claim_words):
        return "adjacent"
    niche = hit.get("niche_score")
    if niche is not None and niche < MIN_NICHE_FOR_DIRECT:
        return "adjacent"
    if not _corroborated(hit, claim_words):
        return "adjacent"
    return band


# IACR mirrors nothing but titles and abstracts, so a section or element filter
# silently cannot apply there. The caller has to be told which it is looking at.
FULLTEXT_SOURCES = {"arxiv", "eip", "simd", "wp", "oa"}


def _paper_facts(paper_ids):
    """niche_score per paper, straight from the ingest record.

    Cheaper than storing it in every chunk payload and re-upserting 760k points:
    a search returns at most a few dozen papers, and this is one query."""
    ids = [pid for pid in dict.fromkeys(paper_ids) if pid]
    if not ids:
        return {}
    try:
        conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            f"SELECT arxiv_id, niche_score FROM papers "
            f"WHERE arxiv_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}


COVERAGE_PROBE_LIMIT = 80
FAST_COVERAGE_PROBE_LIMIT = 40
# An idea outside the three niches must be refused, not answered. Cosine is
# useless for deciding this -- a CRISPR idea still scores 0.79 against LLM
# papers -- but coverage is decisive: measured, CRISPR and soft robotics have
# ZERO papers above the relevance band while "speculative decoding" has 148.
IN_SCOPE_MIN_PAPERS = 3
THIN_COVERAGE_PAPERS = 10

# Semantic similarity cannot define the boundary of a specialist index. A
# CRISPR claim matched CRISPR-GPT, which is an LLM-agent paper mentioning gene
# editing examples, and was therefore audited as biology. Explicit foreign
# domains are refused unless the request also names an anchor of one of the
# three indexed niches. This still admits "an LLM agent for CRISPR design" but
# refuses "correct a liver mutation with CRISPR".
INDEX_SCOPE_ANCHORS = re.compile(
    r"\b(?:large language model|small language model|language model|llm|slm|"
    r"transformer|kv[ -]?cache|retrieval[ -]?augmented|rag|ai agent|llm agent|"
    r"agentic|autonomous agent|multi[ -]?agent|blockchain|web3|smart contract|"
    r"zero[ -]?knowledge|zk[ -]?(?:proof|rollup|snark|stark)|rollup|solana|"
    r"ethereum|defi|mev|cryptograph\w*|consensus|validator|wallet)\b", re.I)
FOREIGN_SCOPE_DOMAINS = {
    "biomedicine": re.compile(
        r"\b(?:crispr|gene[ -]?edit\w*|genome[ -]?edit\w*|hepatocyte\w*|"
        r"liver cell\w*|point mutation\w*|base edit(?:ing|or)|clinical trial|"
        r"protein mutation\w*|drug discovery)\b", re.I),
    "materials_science": re.compile(
        r"\b(?:perovskite|photovoltaic|solar cell\w*|humidity degradation|"
        r"battery electrolyte|crystal lattice)\b", re.I),
    "mechanical_robotics": re.compile(
        r"\b(?:soft robotic gripper|soft gripper|tactile feedback control|"
        r"warehouse picking|robotic manipulation)\b", re.I),
}


def _foreign_scope_domain(text):
    if INDEX_SCOPE_ANCHORS.search(text or ""):
        return None
    for domain, pattern in FOREIGN_SCOPE_DOMAINS.items():
        if pattern.search(text or ""):
            return domain
    return None


def _topic_coverage(text, layer=None, strict=False, probe_limit=None,
                    timeout=SEARCH_QDRANT_TIMEOUT):
    """How many papers this index holds on the subject of one claim, per layer.

    "No match" means one thing over 800 papers and another over 3, and that
    difference is most of the answer's value. Each layer is probed inside its
    own filter and its own band, because that is the only comparison that
    separates "not our field" from "our field, thin shelf". Measured: soft
    robotics reaches 1 paper and CRISPR 2, while a parimutuel market reaches 50
    and MEV 23. One embedding, one vector query per layer."""
    layers = [layer] if layer else sorted(SCORE_BANDS_BY_LAYER)
    probe_limit = probe_limit or COVERAGE_PROBE_LIMIT
    try:
        vector = embed_query(text)
    except HTTPException:
        return None, {}, None
    per_layer, best = {}, 0.0
    for lay in layers:
        # coverage answers "does the index know this subject at all", which is
        # the question the relevance floors were calibrated for. The evidence
        # bands answer "is this hit strong enough to cite" and sit higher; using
        # them here refused a real web3 idea outright the moment the bands moved.
        # Two floors, two questions. The relevance floor answers "is anything
        # here on the subject at all" and is deliberately generous, especially
        # in web3. The band answers "is anything here strong enough to be
        # evidence". The gate must use the second: at 24k papers the generous
        # count kept robotics under three and the gate refused it, at 31k the
        # same subject reached seven and the gate went quiet. A threshold on a
        # number that grows with the corpus is a threshold with an expiry date.
        floor = MIN_RELEVANCE_BY_LAYER.get(lay, MIN_RELEVANCE_SCORE)
        band = _bands_for(lay)[1]
        try:
            hits = qdrant_search(vector, {"must": [{"key": "layers",
                                                    "match": {"any": [lay]}}]},
                                 probe_limit, timeout=timeout)
        except HTTPException:
            continue
        papers, reaches_band = set(), False
        for h in hits:
            score = h.get("score") or 0
            best = max(best, score)
            if score >= band:
                reaches_band = True
            if score >= floor:
                pid = (h.get("payload") or {}).get("arxiv_id")
                if pid:
                    papers.add(pid)
        # Count at the generous floor, but only in layers where something
        # actually reaches the band. A subject with seven papers hovering just
        # above web3's deliberately low floor and nothing near the band is not
        # covered, it is noise -- and it was noise that grew from four papers to
        # seven as the corpus grew, which is how the gate went quiet on robotics.
        per_layer[lay] = len(papers) if (reaches_band or not strict) else 0
    if not per_layer:
        return None, {}, None
    return max(per_layer.values()), per_layer, round(best, 3)


COVERAGE_GRADES = ((0, "empty"), (3, "thin"), (25, "moderate"),
                   (80, "well covered"), (200, "saturated"))


def _coverage_grade(n):
    """One word for how much the index holds, in grades rather than a switch.

    A single "well covered" spanning 32 and 167 papers made silence over a thin
    shelf sound like a finding."""
    if n is None:
        return None
    grade = "empty"
    for floor, name in COVERAGE_GRADES:
        if n >= floor:
            grade = name
    return grade


VOCABULARY_SHARE = 0.20
_vocab_cache = TTLCache(ttl_seconds=3600, max_size=8)


def _layer_vocabulary(layer):
    """Terms so common in a layer that adding them to a query says nothing.

    Query expansion first picked the most frequent terms of the top hits, which
    in llm-slm means "language model" and "llm": the expanded query drifted from
    KV-cache entropy to generic model compression and lost the papers it was
    meant to find. Same lesson as the trends endpoint, same fix."""
    cached = _vocab_cache.get(layer)
    if cached is not None:
        return cached
    try:
        per_term, per_year = _load_term_years(layer)
    except HTTPException:
        return set()
    total = sum(per_year.values()) or 1
    common = {term for term, by_year in per_term.items()
              if sum(by_year.values()) / total > VOCABULARY_SHARE}
    _vocab_cache.set(layer, common)
    return common


GRAPH_SEED_PAPERS = 5       # how many top hits to expand from
GRAPH_MAX_NEIGHBOURS = 30   # candidates pulled out of the graph per claim
GRAPH_SUPPORT_BONUS = 0.004  # per extra seed pointing at the same neighbour
# Llama 2 and "Evaluating LLMs Trained on Code" arrived as candidates with two
# seeds behind them. They are not precedents, they are hubs: everything cites
# them, so they surface in any expansion. A citation carries information in
# inverse proportion to how common its target is, exactly like a term in a
# document, so the support of a neighbour is divided by the popularity of that
# neighbour inside our own graph.
GRAPH_HUB_INDEGREE = 60      # above this a paper is background, not a signal


def _graph_coverage():
    """Share of indexed papers whose bibliography has been ingested, and how
    much of what they cite we actually hold. The second number is the honest
    version of "how complete is this index": not a count, but the fraction of
    the works this subfield cites that a reader can open here."""
    try:
        conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return {}
    try:
        done = conn.execute("SELECT COUNT(*) FROM papers WHERE status='done'").fetchone()[0]
        with_refs = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE status='done' AND refs_fetched=1").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
        inside = conn.execute(
            "SELECT COUNT(*) FROM citations WHERE dst IN "
            "(SELECT arxiv_id FROM papers WHERE status='done')").fetchone()[0]
    finally:
        conn.close()
    return {
        "papers_with_bibliography": with_refs,
        "share_of_index": round(with_refs / done, 3) if done else None,
        "cited_works_we_hold": round(inside / edges, 3) if edges else None,
    }


def _citation_neighbours(paper_ids):
    """Papers one hop away in the bibliography, with how many seeds reach them.

    Semantic recall depends on wording, and three phrasings of one claim gave
    three different answers. A reference list does not: once the search finds
    CAKE, D2O sits in its bibliography whatever words the caller chose. Edges
    run both ways -- what a seed cites and what cites it -- because the newer
    work is as interesting as the older.

    The count of distinct seeds reaching a paper is a signal in itself: a work
    that several independent hits both cite is the canonical one for that
    subject."""
    if not paper_ids:
        return {}
    try:
        conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return {}
    try:
        marks = ",".join("?" * len(paper_ids))
        rows = conn.execute(
            f"SELECT src, dst FROM citations WHERE src IN ({marks}) OR dst IN ({marks})",
            list(paper_ids) + list(paper_ids)).fetchall()
        # how often each candidate is cited across the whole graph
        candidates = {r[1] for r in rows} | {r[0] for r in rows}
        indeg = {}
        if candidates:
            cand_list = list(candidates)[:600]
            marks3 = ",".join("?" * len(cand_list))
            indeg = dict(conn.execute(
                f"SELECT dst, COUNT(*) FROM citations WHERE dst IN ({marks3}) GROUP BY dst",
                cand_list).fetchall())
        support = {}
        seeds = set(paper_ids)
        for src, dst in rows:
            # src cites dst. A work that several seeds cite is the older,
            # canonical one; a work citing the seeds is the newer follow-up.
            # Both are useful and they are not the same thing, so the label says
            # which it is.
            if src in seeds and dst not in seeds:
                support.setdefault(dst, {"seeds": set(),
                                         "relation": "cited by"})["seeds"].add(src)
            elif dst in seeds and src not in seeds:
                support.setdefault(src, {"seeds": set(),
                                         "relation": "cites"})["seeds"].add(dst)
        if not support:
            return {}
        cand = list(support)[:400]
        marks2 = ",".join("?" * len(cand))
        present = {r[0] for r in conn.execute(
            f"SELECT arxiv_id FROM papers WHERE status='done' AND arxiv_id IN ({marks2})",
            cand).fetchall()}
    finally:
        conn.close()
    out = {}
    for pid, info in support.items():
        if pid not in present:
            continue
        cited_by_everyone = indeg.get(pid, 0)
        if cited_by_everyone >= GRAPH_HUB_INDEGREE:
            continue  # a foundational paper is not evidence about a specific claim
        # inverse popularity, on the same footing as an IDF weight
        weight = len(info["seeds"]) / math.log(2 + cited_by_everyone)
        out[pid] = {"support": len(info["seeds"]),
                    "cited_in_index": cited_by_everyone,
                    "weight": round(weight, 3),
                    "relation": info["relation"],
                    "via": sorted(info["seeds"])[:3]}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["weight"])[:GRAPH_MAX_NEIGHBOURS])


def _score_specific_papers(query, paper_ids, layer=None, vector=None, timeout=45,
                           constraints=None):
    """Best chunk of each named paper against the query, scored the usual way,
    so a paper reached through the graph is comparable to one found by search."""
    if not paper_ids:
        return []
    must = [{"key": "arxiv_id", "match": {"any": list(paper_ids)}}]
    must.extend(constraints or [])
    if layer and not any(item.get("key") == "layers" for item in must):
        must.append({"key": "layers", "match": {"any": [layer]}})
    try:
        query_vector = vector or embed_query(query)
        hits = qdrant_search(query_vector, {"must": must}, len(paper_ids) * 8,
                             timeout=timeout)
    except HTTPException:
        return []
    best = {}
    for h in hits:
        pay = h.get("payload") or {}
        pid = pay.get("arxiv_id")
        if not pid:
            continue
        surveys = pay.get("section_type") in (SURVEY_SECTIONS | CONTEXT_SECTIONS)
        rank = (0 if surveys else 1, h.get("score") or 0)
        if pid not in best or rank > best[pid][0]:
            best[pid] = (rank, {
                "arxiv_id": pid,
                "source": SOURCE_LABELS[source_of(pid)],
                "url": source_url(pid, pay),
                "title": pay.get("title"),
                "section_type": pay.get("section_type"),
                "section_title": pay.get("section_title"),
                "element_type": pay.get("element_type"),
                "year": pay.get("year"),
                "repos": pay.get("repos") or [],
                "terms": pay.get("terms") or [],
                "text": (pay.get("text") or "")[:500],
                "score": h.get("score"),
                "layers": pay.get("layers"),
                "venue": pay.get("venue"),
                "citation_count": pay.get("citation_count"),
                "fulltext": source_of(pid) in FULLTEXT_SOURCES,
            })
    return [v[1] for v in best.values()]


def _corpus_stats():
    """Total papers and per-layer/per-year spread, used to qualify verdicts."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return {}
    try:
        total = conn.execute("SELECT COUNT(*) FROM papers WHERE status='done'").fetchone()[0]
        by_layer = {}
        for layer in sorted(ALLOWED_LAYERS):
            by_layer[layer] = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE status='done' AND layers LIKE ?",
                (f"%{layer}%",)).fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE status='done' AND year >= 2025").fetchone()[0]
        span = conn.execute(
            "SELECT MIN(year), MAX(year) FROM papers WHERE status='done' AND year IS NOT NULL"
        ).fetchone()
    finally:
        conn.close()
    return {
        "papers_indexed": total,
        "by_layer": by_layer,
        "published_2025_or_later": recent,
        "year_span": list(span) if span else None,
    }


# ---------------------------------------------------------------------------
# Adjudication memory
# ---------------------------------------------------------------------------
# A retrieval score measures what a text says, never what a paper does, and
# nothing cheap closes that gap: a lexical-overlap signal and a claim-versus-
# abstract signal were both measured on this corpus and neither separated real
# prior art from a topical neighbour (a depth-wise cache merge scored above the
# genuine match). Judging that requires reading, and something that can read is
# already in the loop: the agent calling this API, whose tokens its own user
# pays for. So the server stops guessing and starts remembering. Whoever reads
# a candidate files the verdict here, and the next caller with the same claim
# inherits it. Precision then grows with use instead of with spend.
JUDGMENTS_DB_PATH = "/opt/dtox-research-api/judgments.db"
JUDGMENT_VERDICTS = {"asserts", "does_not_assert", "partial"}
# One reader can be careless and a stored mistake would otherwise fossilise, so
# a claim is only settled when independent readers agree.
CONFIRMATION_QUORUM = 2


# Three wordings of one claim were three separate records, so the registry
# could never thicken: a reader's verdict on "entropy-based criterion" told the
# next caller asking about "attention variance" nothing at all. A claim is now
# an entity with a canonical id; a new wording that lands close enough in vector
# space is filed as an alias of the existing one instead of starting over.
# Automatic merging is OFF, and the reason is worth keeping.
#
# A ten-pair probe put same-meaning claims at 0.688-0.752 and different-meaning
# ones at 0.589-0.667, so 0.72 looked like a conservative cut. In production it
# merged "speculative decoding" with "mixture of experts routing" and with
# "long-term memory for an agent", and "MEV in a rollup sequencer" with
# "recursive aggregation of zero knowledge proofs" -- one node swallowing four
# unrelated questions. The sample was far too small to see that, and a registry
# that silently pools verdicts across different claims is worse than no
# registry at all: it produces confident answers about things nobody judged.
#
# Cosine now only proposes. Every wording keeps its own node, and merging is an
# explicit act by a reader who has seen both claims (POST /v1/claims/link, or
# link_claim_nodes over MCP). Same division of labour as adjudication: the
# machine finds candidates, something that can read decides.
CLAIM_ALIAS_SUGGEST_COSINE = 0.70
# Reading a neighbour's record is not merging it. Turning auto-merge off fixed
# the poisoning and broke the feature it was protecting: a verdict filed under
# one wording stopped answering the same question asked in another, which is the
# one thing this registry exists to do. So neighbours above a high bar are read
# and reported, labelled as belonging to a different node, and nothing is
# pooled. The bar sits well above where the damage happened: the merges that
# swallowed unrelated questions were at 0.72-0.78, while two wordings of one
# question measured 0.917.
# 0.85 was set from a ten-pair probe and never fired in practice: a genuine
# rephrasing of one claim measured 0.79 and its neighbour's verdict stayed
# invisible. Lowered to just under that, which is still far above the 0.72-0.78
# zone where auto-merging pooled unrelated questions -- and unlike merging, this
# only shows the record with its origin attached, it never adopts it.
CLAIM_NEIGHBOUR_READ_COSINE = 0.78
CLAIM_ALIAS_MIN_COSINE = None
JUDGMENT_MODEL_UNKNOWN = "unspecified"


def _norm_claim(claim):
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (claim or "").lower()).split())


def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b))  # both sides are L2-normalised


def _unmerge_claim_nodes(conn):
    """Undo the cosine merges. Which of them were right is unknowable now, so
    every wording goes back to standing on its own."""
    conn.execute("UPDATE claim_nodes SET claim_id = claim_norm")
    conn.execute("UPDATE claim_judgments SET claim_id = claim_norm")
    conn.commit()


def _relink_claim_nodes(conn):
    """Re-run aliasing over nodes created before the threshold was measured.

    Runs once per process: the first version used a threshold of 0.90, at which
    nothing ever aliased, so every wording opened its own node and the registry
    fragmented exactly as it was meant not to."""
    rows = conn.execute(
        "SELECT claim_norm, claim_id, vector, created_at FROM claim_nodes "
        "ORDER BY created_at ASC").fetchall()
    parsed = []
    for r in rows:
        try:
            parsed.append((r["claim_norm"], r["claim_id"], json.loads(r["vector"])))
        except (ValueError, TypeError):
            continue
    changed = 0
    for i, (norm, cid, vec) in enumerate(parsed):
        for older_norm, older_cid, older_vec in parsed[:i]:
            if _cosine(vec, older_vec) >= CLAIM_ALIAS_MIN_COSINE and cid != older_cid:
                conn.execute("UPDATE claim_nodes SET claim_id=? WHERE claim_norm=?",
                             (older_cid, norm))
                conn.execute("UPDATE claim_judgments SET claim_id=? WHERE claim_norm=?",
                             (older_cid, norm))
                changed += 1
                break
    # judgments filed before claim_id existed still point at nothing
    conn.execute(
        "UPDATE claim_judgments SET claim_id=(SELECT claim_id FROM claim_nodes "
        "WHERE claim_nodes.claim_norm = claim_judgments.claim_norm) "
        "WHERE claim_id IS NULL")
    conn.commit()
    return changed


_relinked = False


def _canonical_claim(conn, claim):
    """Existing claim node this wording belongs to, or a new one.

    Returns (claim_id, is_new). Matching is by embedding, because that is the
    only thing that survives rewording; the threshold is deliberately high, so
    a near-miss opens its own node rather than poisoning a neighbour's record."""
    norm = _norm_claim(claim)
    row = conn.execute("SELECT claim_id FROM claim_nodes WHERE claim_norm=?", (norm,)).fetchone()
    if row:
        return row["claim_id"], False
    try:
        vector = embed_query(claim)
    except HTTPException:
        return norm, False  # embedding down: fall back to the exact-text record
    conn.execute(
        "INSERT OR IGNORE INTO claim_nodes (claim_id, claim_norm, claim_text, vector, created_at) "
        "VALUES (?,?,?,?,?)",
        (norm, norm, claim, json.dumps(vector),
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()
    return norm, True


def _similar_claim_nodes(conn, claim, limit=3):
    """Existing nodes close enough to be worth a reader's attention.

    Proposals only: nothing is merged until someone who has read both says so."""
    try:
        vector = embed_query(claim)
    except HTTPException:
        return []
    norm = _norm_claim(claim)
    out = []
    for r in conn.execute(
            "SELECT claim_id, claim_norm, claim_text, vector FROM claim_nodes").fetchall():
        if r["claim_norm"] == norm:
            continue
        try:
            sim = _cosine(vector, json.loads(r["vector"]))
        except (ValueError, TypeError):
            continue
        if sim >= CLAIM_ALIAS_SUGGEST_COSINE:
            out.append({"claim_id": r["claim_id"], "claim_text": r["claim_text"],
                        "cosine": round(sim, 3)})
    return sorted(out, key=lambda x: -x["cosine"])[:limit]


def _judgments_conn():
    conn = sqlite3.connect(JUDGMENTS_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_judgments (
            claim_norm TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            paper_id   TEXT NOT NULL,
            verdict    TEXT NOT NULL,
            reason     TEXT,
            judged_by  TEXT NOT NULL,
            judged_at  TEXT NOT NULL,
            PRIMARY KEY (claim_norm, paper_id, judged_by)
        )
        """
    )
    # Each wording of a claim gets a row; rows that mean the same thing share a
    # claim_id, which is what makes the registry compound instead of fragment.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_nodes (
            claim_norm TEXT PRIMARY KEY,
            claim_id   TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            vector     TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_nodes_id ON claim_nodes(claim_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_links (
            from_claim TEXT NOT NULL,
            to_claim   TEXT NOT NULL,
            linked_by  TEXT NOT NULL,
            reason     TEXT,
            linked_at  TEXT NOT NULL,
            PRIMARY KEY (from_claim, to_claim)
        )
        """
    )
    # Features at the moment of judging. Without them a hundred verdicts stay a
    # pile of opinions; with them the bands can be fitted to what readers
    # actually accepted instead of to a percentile picked by hand.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(claim_judgments)").fetchall()}
    for name, decl in (("claim_id", "TEXT"), ("score", "REAL"), ("band", "TEXT"),
                       ("layer", "TEXT"), ("section_type", "TEXT"),
                       ("niche_score", "INTEGER"), ("judged_by_model", "TEXT"),
                       # set only for verdicts backed by a wallet signature verified
                       # by attestor and written on-chain as a SAS attestation
                       ("attestation_pda", "TEXT"), ("attestation_tx", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE claim_judgments ADD COLUMN {name} {decl}")
    return conn


def _reader_id(api_key):
    """Stable pseudonym for a caller: enough to count independent readers,
    not enough to expose the key it came from."""
    return hashlib.sha256((api_key or "anon").encode()).hexdigest()[:16]


def _reader_identity(api_key, info, claimed_model):
    """Who is judging, as far as the server can actually tell.

    judged_by_model started as a free-text argument, which made the whole
    provenance mechanism forgeable by typing a different string: a verdict
    arrived attributed to a model that had never been asked. Identity now comes
    from the authenticated key. A self-declared model name is still recorded,
    because it is useful, but it is marked unverified and never counts towards
    a quorum on its own."""
    verified = (info or {}).get("reader") or (info or {}).get("name")
    claimed = (claimed_model or "").strip()[:80]
    if verified:
        identity = f"key:{verified}"
    else:
        identity = f"key:{_reader_id(api_key)}"
    return identity, (claimed or JUDGMENT_MODEL_UNKNOWN)


def _judgment_status(counts, readers=None, models=None):
    """A settled verdict needs agreement from readers that can actually differ.

    "Independent readers agree" meant two API keys. If both are the same model,
    the second reading repeats the first one's mistakes with the same
    confidence, which is correlation dressed up as confirmation. Agreement
    within one model is reported as agreed_same_model, one step below settled.

    A wallet-signed verdict (judged_by = "wallet:<pubkey>") is a verified
    identity in its own right -- the signature is checked by attestor and the
    verdict is anchored on-chain, so it does not need a self-declared model
    name to be trusted. Two or more distinct wallets are independent readers
    on their own, regardless of what models (if any) they report."""
    asserts = counts.get("asserts", 0)
    denies = counts.get("does_not_assert", 0)
    # only identities the server established itself count towards diversity;
    # an unattributed reading cannot be the second opinion that settles a claim
    known = {m for m in (models or []) if m and m != JUDGMENT_MODEL_UNKNOWN}
    wallets = {r for r in (readers or []) if isinstance(r, str) and r.startswith("wallet:")}
    diverse = models is None or len(known) > 1 or len(wallets) >= CONFIRMATION_QUORUM
    if asserts >= CONFIRMATION_QUORUM and denies == 0:
        return "confirmed_prior_art" if diverse else "agreed_same_model"
    if denies >= CONFIRMATION_QUORUM and asserts == 0:
        return "ruled_out" if diverse else "agreed_same_model"
    if asserts and denies:
        return "contested"
    if asserts or denies or counts.get("partial", 0):
        return "read_once"
    return "unread"


def _citation_neighbourhood(today_ids, judged_ids):
    """Judged papers that sit one citation hop from anything found today."""
    if not today_ids or not judged_ids:
        return set()
    try:
        conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return set()
    try:
        t = ",".join("?" * len(today_ids))
        j = ",".join("?" * len(judged_ids))
        args = list(today_ids) + list(judged_ids)
        rows = conn.execute(
            f"SELECT src, dst FROM citations "
            f"WHERE (src IN ({t}) AND dst IN ({j})) OR (dst IN ({t}) AND src IN ({j}))",
            args + args).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
    judged = set(judged_ids)
    return {p for row in rows for p in row if p in judged}


def _nodes_sharing_evidence(conn, paper_ids, own_claim_id, limit=3):
    """Claim nodes judged against the same papers this search just returned.

    Suggesting nodes by the similarity of two claim sentences puts the weakest
    component in the most important place: that comparison was measured at a
    0.022 margin and rejected as a classifier, yet it still decides whether a
    reader ever finds the existing record. "auxiliary loss that equalises tokens
    per expert" and "MoE routing with load balancing" are one question in two
    vocabularies, and no threshold on wording connects them.

    Overlap of evidence does, and it never touches the words: if the papers
    coming back today are the papers someone already judged under another
    claim, that claim is worth showing. Same shape as the citation graph -- a
    signal built from what the corpus knows rather than from how the caller
    phrased it."""
    if not paper_ids:
        return []
    marks = ",".join("?" * len(paper_ids))
    rows = conn.execute(
        f"SELECT j.claim_id, j.paper_id, n.claim_text "
        f"FROM claim_judgments j LEFT JOIN claim_nodes n ON n.claim_id = j.claim_id "
        f"WHERE j.paper_id IN ({marks})", list(paper_ids)).fetchall()
    by_node = {}
    for r in rows:
        if not r["claim_id"] or r["claim_id"] == own_claim_id:
            continue
        e = by_node.setdefault(r["claim_id"], {"papers": set(), "text": r["claim_text"],
                                               "kind": "same paper"})
        e["papers"].add(r["paper_id"])

    # Direct overlap needs the two searches to land on the very same paper, and
    # with a young registry they rarely do: one judged paper per node against
    # six returned today. The bibliography closes that gap without touching the
    # words -- a paper one citation hop from a judged one is the same
    # neighbourhood, which is exactly the claim the reader would be filing
    # against.
    if len(by_node) < limit:
        judged = conn.execute(
            "SELECT j.claim_id, j.paper_id, n.claim_text FROM claim_judgments j "
            "LEFT JOIN claim_nodes n ON n.claim_id = j.claim_id "
            "WHERE j.claim_id IS NOT NULL").fetchall()
        judged_ids = {r["paper_id"] for r in judged if r["claim_id"] != own_claim_id}
        near = _citation_neighbourhood(list(paper_ids), judged_ids)
        for r in judged:
            cid = r["claim_id"]
            if cid == own_claim_id or cid in by_node or r["paper_id"] not in near:
                continue
            e = by_node.setdefault(cid, {"papers": set(), "text": r["claim_text"],
                                         "kind": "one citation hop"})
            e["papers"].add(r["paper_id"])
    out = [{"claim_id": cid, "claim_text": e["text"] or cid,
            "shared_papers": sorted(e["papers"])[:4],
            "overlap": len(e["papers"]),
            "via": f"shared evidence ({e['kind']})"}
           for cid, e in by_node.items()]
    return sorted(out, key=lambda x: -x["overlap"])[:limit]


def _neighbour_records(conn, claim, limit=3):
    """Judgements filed under a closely worded, unmerged claim node."""
    out = []
    for cand in _similar_claim_nodes(conn, claim, limit=8):
        if cand["cosine"] < CLAIM_NEIGHBOUR_READ_COSINE:
            continue
        rows = conn.execute(
            "SELECT paper_id, verdict, reason, judged_by, judged_by_model "
            "FROM claim_judgments WHERE claim_id=?", (cand["claim_id"],)).fetchall()
        if rows:
            out.append((cand, rows))
        if len(out) >= limit:
            break
    return out


def _registry_for_claim(claim):
    """Every paper ever judged against this claim node.

    The point of the registry is that the answer stops depending on wording. A
    paper a reader has already confirmed sits in the record whether or not
    today's phrasing retrieves it: D2O was found, judged as asserting the
    claim, and then buried in weaker_matches the next time the same question
    was asked in different words."""
    conn = _judgments_conn()
    try:
        claim_id, _ = _canonical_claim(conn, claim)
        rows = conn.execute(
            "SELECT paper_id, verdict, reason, judged_by, judged_by_model "
            "FROM claim_judgments WHERE claim_id=? OR claim_norm=?",
            (claim_id, _norm_claim(claim))).fetchall()
    finally:
        conn.close()
    out = {}

    def absorb(rows_, from_node=None):
        for r in rows_:
            e = out.setdefault(r["paper_id"], {"counts": {}, "reasons": [],
                                               "readers": set(), "models": set(),
                                               "from_claim": from_node})
            e["counts"][r["verdict"]] = e["counts"].get(r["verdict"], 0) + 1
            e["readers"].add(r["judged_by"])
            e["models"].add(r["judged_by_model"] or JUDGMENT_MODEL_UNKNOWN)
            if r["reason"]:
                e["reasons"].append(_clip(r["reason"], 300))

    absorb(rows)
    conn = _judgments_conn()
    try:
        for cand, cand_rows in _neighbour_records(conn, claim):
            absorb([r for r in cand_rows if r["paper_id"] not in out],
                   from_node={"claim_id": cand["claim_id"],
                              "claim_text": cand["claim_text"],
                              "cosine": cand["cosine"]})
    finally:
        conn.close()

    for pid, e in out.items():
        models, readers = e.pop("models"), e.pop("readers")
        e["readers"] = len(readers)
        e["distinct_models"] = sorted(models)
        e["status"] = _judgment_status(e["counts"], readers, models)
        if e.get("from_claim"):
            # read from a neighbour, not from this node: never settled here
            e["status"] = "reported_on_similar_claim"
    return claim_id, out


def _prior_readings(claim, paper_ids):
    """What earlier readers concluded about these papers for this claim.

    Looked up by claim node, so a verdict filed against one wording answers a
    question asked in another."""
    if not paper_ids:
        return {}
    conn = _judgments_conn()
    try:
        claim_id, _ = _canonical_claim(conn, claim)
        placeholders = ",".join("?" * len(paper_ids))
        rows = conn.execute(
            f"SELECT paper_id, verdict, reason, judged_by, judged_by_model, judged_at, "
            f"attestation_pda "
            f"FROM claim_judgments WHERE (claim_id=? OR claim_norm=?) "
            f"AND paper_id IN ({placeholders})",
            [claim_id, _norm_claim(claim)] + list(paper_ids),
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        entry = out.setdefault(r["paper_id"],
                               {"counts": {}, "reasons": [], "readers_set": set(),
                                "models": set(), "onchain": []})
        entry["counts"][r["verdict"]] = entry["counts"].get(r["verdict"], 0) + 1
        entry["readers_set"].add(r["judged_by"])
        entry["models"].add(r["judged_by_model"] or JUDGMENT_MODEL_UNKNOWN)
        if r["reason"]:
            entry["reasons"].append(_clip(r["reason"], 400))
        # a wallet-signed verdict carries a live SAS attestation: surface it so
        # a caller can verify the signature themselves instead of trusting us
        pda = r["attestation_pda"]
        if pda:
            judged_by = r["judged_by"] or ""
            reviewer = judged_by[len("wallet:"):] if judged_by.startswith("wallet:") else judged_by
            entry["onchain"].append({
                "reviewer": reviewer,
                "verdict": r["verdict"],
                "attestation": pda,
                "explorer_url": f"https://explorer.solana.com/address/{pda}?cluster=devnet",
            })
    for pid, entry in out.items():
        models = entry.pop("models")
        readers = entry.pop("readers_set")
        entry["readers"] = len(readers)
        entry["distinct_models"] = sorted(models)
        entry["status"] = _judgment_status(entry["counts"], readers, models)
    return out


class JudgmentItem(BaseModel):
    id: str
    verdict: str
    reason: Optional[str] = None
    # what the search said about this paper when it was judged, so the bands can
    # later be fitted to accepted verdicts instead of to a hand-picked percentile
    score: Optional[float] = None
    band: Optional[str] = None
    section_type: Optional[str] = None
    niche_score: Optional[int] = None


class AdjudicateBody(BaseModel):
    claim: str
    judgments: List[JudgmentItem] = Field(..., min_length=1, max_length=20)
    layer: Optional[str] = None
    # who did the reading: a model name, a version, or a person. Agreement only
    # counts as settled when it comes from readers that are not the same thing.
    judged_by_model: Optional[str] = None


@app.post("/v1/adjudicate")
def adjudicate(body: AdjudicateBody, x_api_key: Optional[str] = Header(default=None)):
    """File what you concluded after reading the candidates for a claim.

    This is the half of validation a search index cannot do. The verdicts land
    in the shared record, so the next caller asking the same thing starts from
    a reading rather than from a cosine score."""
    api_key, _info = auth_and_limit(x_api_key, "adjudicate")
    claim = (body.claim or "").strip()
    if not claim:
        raise HTTPException(status_code=400, detail="claim must not be empty")
    bad = [j.verdict for j in body.judgments if j.verdict not in JUDGMENT_VERDICTS]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"verdict must be one of {sorted(JUDGMENT_VERDICTS)}")

    reader = _reader_id(api_key)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _judgments_conn()
    try:
        claim_id, is_new = _canonical_claim(conn, claim)
        identity, claimed_model = _reader_identity(api_key, _info, body.judged_by_model)
        for j in body.judgments:
            conn.execute(
                "INSERT INTO claim_judgments "
                "(claim_norm, claim_text, claim_id, paper_id, verdict, reason, judged_by, "
                " judged_by_model, judged_at, score, band, layer, section_type, niche_score) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(claim_norm, paper_id, judged_by) DO UPDATE SET "
                "verdict=excluded.verdict, reason=excluded.reason, judged_at=excluded.judged_at, "
                "score=excluded.score, band=excluded.band, niche_score=excluded.niche_score, "
                "claim_id=excluded.claim_id, judged_by_model=excluded.judged_by_model",
                (_norm_claim(claim), claim, claim_id, j.id, j.verdict, (j.reason or "")[:600],
                 reader, identity, now,
                 j.score, j.band, body.layer, j.section_type, j.niche_score),
            )
        conn.commit()
    finally:
        conn.close()

    readings = _prior_readings(claim, [j.id for j in body.judgments])
    conn = _judgments_conn()
    try:
        neighbours = _similar_claim_nodes(conn, claim)
    finally:
        conn.close()
    return {
        "claim": claim,
        "claim_id": claim_id,
        "new_claim_node": is_new,
        "same_question_candidates": neighbours,
        "link_hint": ("If one of these asks the same question as your claim, call "
                      "link_claim_nodes now: you are holding the context that makes "
                      "that judgement cheap, and the next caller inherits your work."),
        "recorded": len(body.judgments),
        "papers": {pid: {"status": r["status"], "readers": r["readers"], "counts": r["counts"]}
                   for pid, r in readings.items()},
        "quorum": CONFIRMATION_QUORUM,
        "note": ("A verdict is settled once independent readers agree. Until then it is "
                 "one reader's opinion and is reported as such."),
    }


# ---------------- wallet-signed verdicts (attestor / Solana Attestation Service) ----------------
#
# /v1/adjudicate trusts the caller's API key as the reader's identity. A
# public, keyless MCP endpoint shares one API key across every anonymous
# caller, so that identity cannot tell readers apart -- nothing stops it from
# filing contradictory verdicts against itself. These two endpoints let a
# caller anchor a verdict to its own wallet instead: it signs the exact
# message /v1/verdict/message hands back, attestor checks the signature and
# writes it on devnet as a SAS attestation, and only then does it land here.

def _attestor_headers():
    return {"X-Internal-Token": ATTESTOR_INTERNAL_TOKEN, "Content-Type": "application/json"}


def _attestor_error_detail(resp):
    try:
        return resp.json().get("error", resp.text)
    except ValueError:
        return resp.text


class VerdictMessageBody(BaseModel):
    claim: str
    paper_id: str
    verdict: str
    evidence_sha256: Optional[str] = None
    # left for the caller to pass through if it already has one (e.g. re-signing
    # a stale message); attestor stamps it itself when omitted
    issued_at: Optional[str] = None


@app.post("/v1/verdict/message")
def verdict_message(body: VerdictMessageBody, x_api_key: Optional[str] = Header(default=None)):
    """Canonical message to sign for a wallet-backed verdict.

    claim_id is resolved through the same claim-node lookup /v1/adjudicate
    uses (_canonical_claim), so the same question asked in different words
    still signs against the one node it belongs to -- the caller never has to
    know the registry's internal id scheme. The returned message is exactly
    what /v1/adjudicate/signed -> attestor will rebuild and check the
    signature against, so signing anything else will fail verification.
    """
    auth_and_limit(x_api_key, "adjudicate")
    claim = (body.claim or "").strip()
    if not claim:
        raise HTTPException(status_code=400, detail="claim must not be empty")
    paper_id = (body.paper_id or "").strip()
    if not paper_id:
        raise HTTPException(status_code=400, detail="paper_id must not be empty")
    if body.verdict not in JUDGMENT_VERDICTS:
        raise HTTPException(status_code=400,
                            detail=f"verdict must be one of {sorted(JUDGMENT_VERDICTS)}")

    conn = _judgments_conn()
    try:
        claim_id, _ = _canonical_claim(conn, claim)
    finally:
        conn.close()

    payload = {
        "claim_id": claim_id,
        "claim_text": claim,
        "paper_id": paper_id,
        "verdict": body.verdict,
    }
    if body.evidence_sha256:
        payload["evidence_sha256"] = body.evidence_sha256
    if body.issued_at:
        payload["issued_at"] = body.issued_at

    try:
        resp = requests.post(f"{ATTESTOR_URL}/message", headers=_attestor_headers(),
                             json=payload, timeout=10)
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"attestor unavailable: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"attestor error: {_attestor_error_detail(resp)}")
    return resp.json()


class SignedJudgmentItem(BaseModel):
    id: str  # paper id
    verdict: str
    reason: Optional[str] = None
    evidence_sha256: Optional[str] = None
    reviewer: str  # base58 ed25519 pubkey
    signature: str  # base58 ed25519 signature over the /v1/verdict/message canonical message
    issued_at: str


class AdjudicateSignedBody(BaseModel):
    claim: str
    layer: Optional[str] = None
    judgments: List[SignedJudgmentItem] = Field(..., min_length=1, max_length=20)


@app.post("/v1/adjudicate/signed")
def adjudicate_signed(body: AdjudicateSignedBody, x_api_key: Optional[str] = Header(default=None)):
    """Wallet-signed /v1/adjudicate: the reviewer identity is a signature, not an API key.

    Each judgment is sent to attestor, which recomputes the canonical message
    from claim_id/paper_id/verdict/evidence, verifies the ed25519 signature
    against the given reviewer pubkey, and -- only once that checks out --
    writes a SAS attestation on devnet. Nothing is written to this registry
    without a matching on-chain attestation: a bad signature fails that one
    item with a 400 and the rest of the batch is still processed; attestor
    being unreachable fails the whole call with 503 and nothing is recorded.

    judged_by is stored as "wallet:<reviewer>", so two different wallets are
    two independent readers for quorum purposes even with no declared model,
    while the same wallet repeating itself is still one reader (see
    _judgment_status).
    """
    auth_and_limit(x_api_key, "adjudicate")
    claim = (body.claim or "").strip()
    if not claim:
        raise HTTPException(status_code=400, detail="claim must not be empty")

    conn = _judgments_conn()
    try:
        claim_id, is_new = _canonical_claim(conn, claim)
    finally:
        conn.close()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    for j in body.judgments:
        item_result = {"id": j.id}
        if j.verdict not in JUDGMENT_VERDICTS:
            item_result["status"] = 400
            item_result["error"] = f"verdict must be one of {sorted(JUDGMENT_VERDICTS)}"
            results.append(item_result)
            continue

        payload = {
            "claim_id": claim_id,
            "claim_text": claim,
            "paper_id": j.id,
            "verdict": j.verdict,
            "reviewer": j.reviewer,
            "signature": j.signature,
            "issued_at": j.issued_at,
        }
        if j.evidence_sha256:
            payload["evidence_sha256"] = j.evidence_sha256

        try:
            resp = requests.post(f"{ATTESTOR_URL}/attest", headers=_attestor_headers(),
                                 json=payload, timeout=20)
        except requests.RequestException as e:
            # a partial batch already attested cannot be undone, but nothing
            # further gets written without its own attestation
            raise HTTPException(status_code=503, detail=f"attestor unavailable: {e}")

        if resp.status_code in (400, 429):
            item_result["status"] = resp.status_code
            item_result["error"] = _attestor_error_detail(resp)
            results.append(item_result)
            continue
        if resp.status_code != 200:
            raise HTTPException(status_code=503,
                                detail=f"attestor error: {_attestor_error_detail(resp)}")

        attestation = resp.json()
        identity = f"wallet:{j.reviewer}"
        conn = _judgments_conn()
        try:
            conn.execute(
                "INSERT INTO claim_judgments "
                "(claim_norm, claim_text, claim_id, paper_id, verdict, reason, judged_by, "
                " judged_by_model, judged_at, layer, attestation_pda, attestation_tx) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(claim_norm, paper_id, judged_by) DO UPDATE SET "
                "verdict=excluded.verdict, reason=excluded.reason, judged_at=excluded.judged_at, "
                "claim_id=excluded.claim_id, attestation_pda=excluded.attestation_pda, "
                "attestation_tx=excluded.attestation_tx",
                (_norm_claim(claim), claim, claim_id, j.id, j.verdict, (j.reason or "")[:600],
                 identity, JUDGMENT_MODEL_UNKNOWN, now, body.layer,
                 attestation.get("attestation"), attestation.get("signature")),
            )
            conn.commit()
        finally:
            conn.close()

        item_result["status"] = 200
        item_result["attestation_pda"] = attestation.get("attestation")
        item_result["attestation_tx"] = attestation.get("signature")
        item_result["explorer_url"] = attestation.get("explorer_url")
        results.append(item_result)

    readings = _prior_readings(claim, [j.id for j in body.judgments])
    return {
        "claim": claim,
        "claim_id": claim_id,
        "new_claim_node": is_new,
        "results": results,
        "papers": {pid: {"status": r["status"], "readers": r["readers"], "counts": r["counts"],
                         "onchain": r["onchain"]}
                   for pid, r in readings.items()},
        "quorum": CONFIRMATION_QUORUM,
    }


class ClaimSpec(BaseModel):
    claim: str
    phrasings: List[str] = Field(default_factory=list, max_length=4)


class LinkClaimBody(BaseModel):
    claim: str
    same_as_claim_id: str
    reason: Optional[str] = None


@app.post("/v1/claims/link")
def link_claims(body: LinkClaimBody, x_api_key: Optional[str] = Header(default=None)):
    """Declare that two wordings ask the same question.

    Cosine can propose this and cannot decide it: at the only threshold that
    merged genuine rewordings, it also merged speculative decoding with agent
    memory. So the decision belongs to whoever has read both claims, and it is
    recorded with their identity like any other verdict."""
    api_key, info = auth_and_limit(x_api_key)
    claim = (body.claim or "").strip()
    if not claim:
        raise HTTPException(status_code=400, detail="claim must not be empty")
    conn = _judgments_conn()
    try:
        target = conn.execute("SELECT claim_id FROM claim_nodes WHERE claim_id=? LIMIT 1",
                              (body.same_as_claim_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404,
                                detail=f"no claim node {body.same_as_claim_id}")
        claim_id, _ = _canonical_claim(conn, claim)
        identity, _model = _reader_identity(api_key, info, None)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("UPDATE claim_nodes SET claim_id=? WHERE claim_id=?",
                     (body.same_as_claim_id, claim_id))
        conn.execute("UPDATE claim_judgments SET claim_id=? WHERE claim_id=?",
                     (body.same_as_claim_id, claim_id))
        conn.execute(
            "INSERT OR REPLACE INTO claim_links (from_claim, to_claim, linked_by, reason, linked_at) "
            "VALUES (?,?,?,?,?)",
            (claim_id, body.same_as_claim_id, identity, (body.reason or "")[:400], now))
        conn.commit()
        merged = conn.execute("SELECT COUNT(*) FROM claim_judgments WHERE claim_id=?",
                              (body.same_as_claim_id,)).fetchone()[0]
    finally:
        conn.close()
    return {
        "claim": claim,
        "now_filed_under": body.same_as_claim_id,
        "judgments_on_that_node": merged,
        "linked_by": identity,
        "note": ("Verdicts recorded under either wording now answer both. Link only "
                 "claims you have read and consider the same question."),
    }


class ValidateBody(BaseModel):
    idea: str
    # a claim can be a bare string or {"claim": ..., "phrasings": [...]}. Recall
    # here depends entirely on wording, and a false "nothing found" is the worst
    # failure this endpoint has: one phrasing missed a paper whose own section
    # was titled "Attention Variance and Information Entropy" because the claim
    # said "entropy-based criterion". The caller is a language model and can
    # spell the same idea three ways for free, so it is asked to.
    claims: List[Union[str, ClaimSpec]] = Field(..., min_length=1)
    layer: Optional[str] = None
    evidence_per_claim: int = Field(default=4, ge=1, le=10)
    depth: str = "full"


@app.post("/v1/validate")
def validate_project(body: ValidateBody, x_api_key: Optional[str] = Header(default=None)):  # noqa: D401
    """Audit a project idea against the literature: prior art, known
    limitations, supporting math, and field dynamics -- with the corpus
    coverage that qualifies every verdict."""
    auth_and_limit(x_api_key, "validate")

    idea = (body.idea or "").strip()
    if not idea:
        raise HTTPException(status_code=400, detail="idea must not be empty")
    claim_specs = []
    for c in body.claims[:VALIDATE_MAX_CLAIMS]:
        if isinstance(c, str):
            text, extra = c.strip(), []
        else:
            text, extra = (c.claim or "").strip(), [x.strip() for x in c.phrasings if x.strip()]
        if text:
            claim_specs.append((text, extra))
    claims = [t for t, _ in claim_specs]
    if not claims:
        raise HTTPException(status_code=400, detail="at least one claim required")
    if body.layer is not None and body.layer not in ALLOWED_LAYERS:
        raise HTTPException(status_code=400, detail=f"layer must be one of {sorted(ALLOWED_LAYERS)}")
    if body.depth not in {"fast", "full"}:
        raise HTTPException(status_code=400, detail="depth must be 'fast' or 'full'")
    fast_mode = body.depth == "fast"
    audit_qdrant_timeout = 12 if fast_mode else 30
    audit_embed_wait = 4 if fast_mode else 10
    audit_embed_http = 8 if fast_mode else 20

    corpus = _corpus_stats()
    corpus["as_of"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    corpus["note"] = ("The index grows continuously, so a verdict is only "
                      "reproducible against the same as_of date. Cite it.")
    layer_size = corpus.get("by_layer", {}).get(body.layer) if body.layer else corpus.get("papers_indexed")

    scope_text = " ".join([idea] + [text for text, _ in claim_specs] +
                          [p for _, phrasings in claim_specs for p in phrasings])
    foreign_domain = _foreign_scope_domain(scope_text)
    if foreign_domain:
        return {
            "idea": idea,
            "in_scope": False,
            "verdict": "out_of_index_scope",
            "corpus": corpus,
            "headline": ("The request is about a domain outside this specialist "
                         "index, so no literature audit was run. This says nothing "
                         "about novelty in the wider literature."),
            "scope_reason": "explicit_foreign_domain_without_index_anchor",
            "foreign_domain": foreign_domain,
            "covered_topics": ["large and small language models", "AI agents",
                               "web3 and applied cryptography"],
            "what_to_do": ("Use a scientific index for this domain. If the actual "
                           "claim is about applying an LLM, an AI agent, or web3 to "
                           "it, state that mechanism explicitly and run the audit again."),
        }

    prime_query_embeddings(
        [idea] + [text for text, _ in claim_specs] +
        [phrasing for _, phrasings in claim_specs for phrasing in phrasings]
    )
    if not fast_mode:
        prime_qdrant_searches(
            [idea] + [text for text, _ in claim_specs] +
            [phrasing for _, phrasings in claim_specs for phrasing in phrasings],
            body.layer,
            COVERAGE_PROBE_LIMIT,
        )

    # Scope check before anything else: a robotics or biotech idea would
    # otherwise collect a page of "no match in corpus" and read as a clean bill.
    # Judged on the best of the idea and every claim, because a one-line idea
    # can be phrased so unusually that it matches nothing while its claims match
    # plenty -- "parimutuel market on Solana settled by an oracle" peaked at
    # 0.703 while the same subject as a claim reached 180 papers.
    coverage_cache = {}

    def _coverage_of(text, strict=False):
        key = (text, strict)
        if key not in coverage_cache:
            coverage_cache[key] = _topic_coverage(
                text, body.layer, strict,
                FAST_COVERAGE_PROBE_LIMIT if fast_mode else COVERAGE_PROBE_LIMIT,
                timeout=audit_qdrant_timeout,
            )
        return coverage_cache[key]

    idea_coverage, idea_by_layer, idea_best = _coverage_of(idea)
    coverage_probes_succeeded = 1 if idea_coverage is not None else 0
    scope_best = idea_coverage or 0
    scope_by_layer = dict(idea_by_layer or {})
    for text, phrasings in claim_specs:
        for probe in [text] + list(phrasings):
            got, by_layer, best = _coverage_of(probe)
            if got is None:
                continue
            coverage_probes_succeeded += 1
            scope_best = max(scope_best, got)
            for lay, n in (by_layer or {}).items():
                scope_by_layer[lay] = max(scope_by_layer.get(lay, 0), n)
            idea_best = max(idea_best or 0, best or 0)
    # Infrastructure failure is not evidence that a topic is outside the
    # corpus. Previously an embed timeout turned every failed probe into zero
    # coverage and produced a confident out_of_index_scope verdict.
    if not coverage_probes_succeeded:
        raise HTTPException(
            status_code=503,
            detail="scope could not be measured because retrieval is temporarily unavailable",
        )
    if scope_best < IN_SCOPE_MIN_PAPERS:
        return {
            "idea": idea,
            "in_scope": False,
            "verdict": "out_of_index_scope",
            "corpus": corpus,
            "headline": ("The index holds essentially nothing on this subject, so no "
                         "audit was run. This is a statement about the index, not "
                         "about the literature: do not read it as novelty."),
            "covered_topics": ["large and small language models", "AI agents",
                               "web3 and applied cryptography"],
            "best_similarity": idea_best,
            "papers_on_this_subject": scope_best,
            "by_layer": scope_by_layer,
            "what_to_do": ("Use a general scientific index for this. If you believe "
                           "the idea does sit in one of the covered topics, restate "
                           "it in that field's vocabulary and try again."),
        }

    def _search(query, **extra):
        # over-fetch: context sections are dropped from the evidence below, and
        # without the margin a claim could end up with nothing left to show
        limit = extra.pop("limit", min(50, body.evidence_per_claim * 3))
        # dedupe off on purpose: with one chunk per paper the winner is often
        # the introduction, and an introduction can never be direct evidence.
        # A paper that also matches in its own method section deserves to be
        # represented by that section instead of by its opening paragraph.
        b = SearchBody(query=query, layer=body.layer, limit=min(50, limit * 3),
                       min_score=0.60, dedupe=False, diagnose=False, **extra)
        hits = _scored_results_for_audit(
            _run_search(b, x_api_key, dense_timeout=audit_qdrant_timeout,
                        embed_wait_timeout=audit_embed_wait,
                        embed_http_timeout=audit_embed_http))
        best = {}
        for h in hits:
            pid = h["arxiv_id"]
            kept = best.get(pid)
            if kept is None:
                best[pid] = h
                continue
            def rank(x):
                # a section that states the paper's own work outranks one that
                # surveys the field, then score decides. The last element is a
                # tie-break: without it two chunks of equal score swapped places
                # between runs and the paper's band swapped with them.
                surveys = x.get("section_type") in (SURVEY_SECTIONS | CONTEXT_SECTIONS)
                return (0 if surveys else 1, x["score"], str(x.get("section_title") or ""))
            if rank(h) > rank(kept):
                best[pid] = h
        return sorted(best.values(), key=lambda h: -h["score"])[:limit]

    def _expansion_query(claim, hits):
        """A rephrasing built from the words the subject's papers actually use.

        Measured on one claim: asked alone, "entropy-based criterion for how
        aggressively to compress" peaks at 0.810 and returns in-context learning
        and LoRA papers -- the claim never says what is being compressed. Glued
        to the idea it does worse (0.794): a long query dilutes. The same claim
        plus two words of subject vocabulary reaches 0.872 and the KV-cache
        papers it was aiming at. So the expansion adds a few precise terms, and
        it seeds them from the idea's own matches as well as the claim's,
        because a claim that missed cannot suggest better words for itself."""
        stop = _layer_vocabulary(body.layer)
        seeds = list(hits[:5])
        if idea:
            try:
                seeds += _search(idea, limit=5)
            except HTTPException:
                pass
        counts = {}
        for hit in seeds:
            for term in (hit.get("terms") or []):
                t = term.strip().lower()
                if t and t not in claim.lower() and t not in stop:
                    counts[t] = counts.get(t, 0) + 1
        # a term must show up in more than one of the best hits to count as the
        # subject's vocabulary rather than one paper's idiom
        picked = [t for t, n in sorted(counts.items(), key=lambda kv: -kv[1])[:3] if n > 1]
        if not picked:
            picked = [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:2]]
        return (f"{claim} {' '.join(picked)}", picked) if picked else (None, [])

    def _search_union(claim, phrasings, **extra):
        """Recall across rephrasings: a paper found by any wording counts once,
        at its best score, tagged with the phrasing that surfaced it."""
        queries = [claim] + list(phrasings)
        best = {}

        def run(q, label=None):
            for hit in _search(q, **extra):
                prev = best.get(hit["arxiv_id"])
                if prev is None or hit["score"] > prev["score"]:
                    best[hit["arxiv_id"]] = dict(hit, found_via=label or q)

        for q in queries:
            run(q)
        expanded, picked = _expansion_query(claim, sorted(best.values(),
                                                          key=lambda h: -h["score"]))
        if expanded:
            run(expanded, label=f"corpus vocabulary: {', '.join(picked)}")
            queries.append(expanded)
        ranked = sorted(best.values(), key=lambda h: -_rank_score(h))
        return ranked, queries

    claim_reports = []
    for claim, phrasings in claim_specs:
        # coverage has to be measured over the same wordings the evidence was
        # found with, or a claim can show three solid papers and "thin" beside
        # them: the narrow phrasing that finds little is exactly the one a
        # cautious caller writes
        claim_id, registry = _registry_for_claim(claim)
        _link_conn = _judgments_conn()
        try:
            same_question = [dict(c, via="wording")
                             for c in _similar_claim_nodes(_link_conn, claim)]
        finally:
            _link_conn.close()
        found, queries_used = _search_union(claim, phrasings)
        # papers today's wording failed to retrieve are fetched by id, so a
        # verdict resting on the record is backed by a citable item rather than
        # by a bare identifier in a side block
        # a paper on the record is pinned whether it was hydrated or retrieved:
        # D2O came back from the search, ranked sixth under this wording, and
        # fell outside the evidence cut while its verdict sat in the registry
        for h in found:
            if h["arxiv_id"] in registry:
                h["from_registry"] = True
        missing_from_today = [pid for pid in registry
                              if pid not in {h["arxiv_id"] for h in found}]
        hydrated = _score_specific_papers(claim, missing_from_today, body.layer) \
            if missing_from_today else []
        if hydrated:
            facts3 = _paper_facts([h["arxiv_id"] for h in hydrated])
            for h in hydrated:
                h["niche_score"] = facts3.get(h["arxiv_id"])
                h["found_via"] = "registry: judged against this claim before"
                h["from_registry"] = True
            found = hydrated + found
        facts = _paper_facts([h["arxiv_id"] for h in found])
        for h in found:
            h.setdefault("niche_score", facts.get(h["arxiv_id"]))
        found.sort(key=lambda h: -_rank_score(h))

        # one hop through the bibliography of the best hits, which is the part
        # of recall that does not depend on how the claim was worded
        # seeding only from today's hits made the graph inherit the mistake it
        # was meant to fix: a wording that missed the precedents expanded from
        # the wrong neighbourhood. Papers on the record are seeds by right.
        seeds, neighbours, graph_added = [], {}, []
        if not fast_mode:
            seeds = list(dict.fromkeys(
                [pid for pid in registry] + [h["arxiv_id"] for h in found[:GRAPH_SEED_PAPERS]]
            ))[:GRAPH_SEED_PAPERS + 3]
            neighbours = _citation_neighbours(seeds)
            known = {h["arxiv_id"] for h in found}
            wanted = [pid for pid in neighbours if pid not in known]
            if wanted:
                scored = _score_specific_papers(claim, wanted, body.layer)
                facts2 = _paper_facts([h["arxiv_id"] for h in scored])
                for h in scored:
                    info = neighbours[h["arxiv_id"]]
                    h["niche_score"] = facts2.get(h["arxiv_id"])
                    h["citation_support"] = info["support"]
                    h["found_via"] = (f"citation graph: {info['relation']} "
                                      f"{', '.join(info['via'])}")
                    h["citation_weight"] = info["weight"]
                    h["cited_in_index"] = info["cited_in_index"]
                    h["graph_bonus"] = min(3.0, info["weight"]) * GRAPH_SUPPORT_BONUS
                    graph_added.append(h)
                found = found + graph_added
                found.sort(key=lambda h: -(_rank_score(h) + h.get("graph_bonus", 0)))

        # the second proposal channel: nodes already judged against the papers
        # this search returned, regardless of how either claim was worded
        _ov_conn = _judgments_conn()
        try:
            overlap_nodes = _nodes_sharing_evidence(
                _ov_conn, [h["arxiv_id"] for h in found[:12]], claim_id)
        finally:
            _ov_conn.close()
        seen_nodes = {c["claim_id"] for c in same_question}
        same_question = same_question + [c for c in overlap_nodes
                                         if c["claim_id"] not in seen_nodes]

        # coverage after the search, over every wording actually used: the
        # expansion query is usually the one that names the subject properly,
        # and leaving it out reported a well-stocked subject as thin
        coverage, coverage_by_layer, strict_coverage = 0, {}, 0
        for q in queries_used:
            got, by_layer, _best = _coverage_of(q)
            if got is None:
                continue
            coverage = max(coverage, got)
            strict_coverage = max(strict_coverage, len([
                h for h in found
                if _score_band(h.get("score") or 0,
                               _layer_of(h, body.layer)) != "weak"]))
            for lay, n in (by_layer or {}).items():
                coverage_by_layer[lay] = max(coverage_by_layer.get(lay, 0), n)
        # one lookup, not two: _prior_readings only saw this node, so a paper
        # carried into the evidence from a neighbour's record was displayed as
        # "unread" right next to the verdict that rested on it
        readings = registry
        mentions = [h for h in found if h.get("section_type") in CONTEXT_SECTIONS]
        hits = [h for h in found if h.get("section_type") not in CONTEXT_SECTIONS]
        # papers on the record are pinned: their score under today's wording is
        # exactly what the registry exists to overrule, so cutting the list by
        # score would drop them again and leave the verdict unsupported
        on_the_record = [h for h in hits if h.get("from_registry")]
        others = [h for h in hits if not h.get("from_registry")]
        hits = on_the_record + others[:body.evidence_per_claim]
        registry_asserts = {pid: rec for pid, rec in registry.items()
                            if rec["counts"].get("asserts", 0) > 0
                            and rec["counts"].get("does_not_assert", 0) == 0}
        claim_words = _distinctive_words(claim, body.layer)
        rerank_report, rerank_order = None, {}
        if not fast_mode and _needs_rerank(hits, body.layer, registry):
            candidates = hits[:RERANK_TOP_N]
            ce_scores = _rerank(claim, candidates)
            if ce_scores:
                order = sorted(range(len(candidates)), key=lambda i: -ce_scores[i])
                # relative, not absolute: the raw logit scale means nothing on
                # its own, but the ordering is what the bi-encoder got wrong
                top_ids = {candidates[i]["arxiv_id"] for i in order[:3]
                           if ce_scores[i] > CROSS_POSITIVE}
                for h, sc in zip(candidates, ce_scores):
                    h["cross_score"] = round(float(sc), 3)
                    h["cross_top"] = h["arxiv_id"] in top_ids
                # the reranked order is the answer, not a note beside it: the
                # evidence was still being sorted by cosine while the model that
                # actually read the passages sat in a field nobody acts on
                rerank_order = {candidates[i]["arxiv_id"]: n for n, i in enumerate(order)}
                rerank_report = {
                    "ran": True,
                    "reason": ("no reading on record and the best hits sit within "
                               f"{RERANK_UNCERTAINTY} of the band"),
                    "order": [candidates[i]["arxiv_id"] for i in order],
                }
                hits = [candidates[i] for i in order] + hits[RERANK_TOP_N:]
        on_record = [h for h in hits if _evidence_band(h, body.layer, claim_words) == "registry"]
        direct = [h for h in hits if _evidence_band(h, body.layer, claim_words) == "direct"]
        adjacent = [h for h in hits if _evidence_band(h, body.layer, claim_words) == "adjacent"]
        weak = [h for h in hits if _evidence_band(h, body.layer, claim_words) == "weak"]

        # a candidate someone has already read is no longer "to check". A loop
        # that records readings and then reports the same headline forever is a
        # loop nobody files a second verdict into.
        def _status_of(h):
            return readings.get(h["arxiv_id"], {}).get("status", "unread")

        strong = list(direct)
        confirmed = [h for h in strong if _status_of(h) == "confirmed_prior_art"]
        ruled_out = [h for h in strong if _status_of(h) == "ruled_out"]
        read_once = [h for h in strong if _status_of(h) == "read_once"]
        contested = [h for h in strong if _status_of(h) == "contested"]
        unread_strong = [h for h in strong if _status_of(h) == "unread"]
        # counting only the retrieval path reported "0 read, 0 unread" while two
        # papers sat in the record with verdicts on them
        by_status = {}
        for rec in registry.values():
            by_status[rec["status"]] = by_status.get(rec["status"], 0) + 1
        settled = {
            "confirmed_prior_art": len(confirmed) + by_status.get("confirmed_prior_art", 0),
            "ruled_out": len(ruled_out) + by_status.get("ruled_out", 0),
            "pending_quorum": len(read_once) + by_status.get("read_once", 0),
            "agreed_same_model": by_status.get("agreed_same_model", 0),
            "contested": len(contested) + by_status.get("contested", 0),
            "unread": len(unread_strong),
            "on_record_total": len(registry),
        }
        ruled_out_ids = {h["arxiv_id"] for h in ruled_out}
        direct = [h for h in direct if h["arxiv_id"] not in ruled_out_ids]

        # a reading already on the record outranks anything today's phrasing did
        # or failed to do: this is the one place where the answer is allowed to
        # stop being a function of the words the caller chose
        settled_asserts = [pid for pid, rec in registry_asserts.items()
                           if rec["status"] == "confirmed_prior_art"]
        on_similar = [pid for pid, rec in registry_asserts.items()
                      if rec["status"] == "reported_on_similar_claim"]
        if settled_asserts:
            verdict = "prior_art_confirmed"
            reading = ("Independent readers have already confirmed these papers make "
                       "this claim, under this or an equivalent wording. Treat it as "
                       "existing work.")
        elif on_similar and not [p for p in registry_asserts if p not in on_similar]:
            verdict = "prior_art_reported_on_similar_claim"
            reading = ("A reader judged these papers against a claim worded very "
                       "closely to yours, but the two claims are separate records. "
                       "Read them, and if the two wordings ask the same question, "
                       "link the nodes so the next caller inherits the verdict.")
        elif registry_asserts and not confirmed:
            verdict = "prior_art_reported_pending_quorum"
            reading = ("A reader has recorded that these papers assert this claim. One "
                       "reading is not a settled verdict, but it outranks a retrieval "
                       "score: read them and file a second opinion.")
        elif confirmed:
            verdict = "prior_art_confirmed"
            reading = ("Independent readers agreed these papers make this claim. "
                       "Treat it as existing work and compete on execution.")
        elif direct and not unread_strong:
            verdict = "candidates_read_pending_quorum"
            reading = ("Every candidate has been read once and none is settled yet. "
                       "A second independent reading closes this claim; until then "
                       "each paper carries the earlier reader's reasons.")
        elif ruled_out and not direct:
            verdict = "candidates_ruled_out"
            reading = ("The strong candidates were read and rejected as not making "
                       "this claim. That is not proof of novelty, only that these "
                       "particular papers are not it.")
        elif direct:
            verdict = "strong_candidates"
            reading = ("These sit where the same idea usually sits, but the score "
                       "measures wording, not identity of the claim. Read each "
                       "snippet and confirm the paper actually asserts this before "
                       "you treat the idea as taken.")
        elif adjacent:
            verdict = "adjacent_work_only"
            reading = ("Nothing matches directly, but the problem area is "
                       "actively worked on. The gap may be real, or may be "
                       "phrasing: re-run with the vocabulary those papers use.")
        elif weak:
            # every hit fell under the layer's band. That is far more often a
            # wording problem than an empty shelf, and "no match" would be read
            # as open ground, so the weak hits are shown with the vocabulary
            # that would find their subject properly.
            verdict = "weak_signal_only"
            reading = ("Nothing cleared the evidence band for this wording. Before "
                       "reading anything into that, re-run the claim using the "
                       "vocabulary listed in suggested_terms: a claim that does not "
                       "name its subject ('compress' without 'KV cache') lands on "
                       "the wrong papers by design.")
        else:
            verdict = "no_match_in_corpus"
            reading = ("No match here. This is NOT confirmation of novelty: "
                       f"the index holds {layer_size or 0} papers in this scope "
                       "against millions in the wider literature. It means the "
                       "claim is worth a targeted patent and venue search.")

        # limitations are worth far more when they belong to a paper already
        # cited as evidence: a limitation from an unrelated paper that merely
        # shares vocabulary reads as damning and proves nothing
        cited_ids = {h["arxiv_id"] for h in direct + adjacent}
        # take the limitations straight from the papers being cited. Searching
        # for them separately answered with whoever happened to match the claim
        # wording, so every limitation arrived flagged from_cited_paper=false
        # and start_here stayed empty while the section existed all along.
        found_limits, limitations, formulas = [], [], []
        if not fast_mode:
            for pid in list(cited_ids)[:4]:
                try:
                    points = qdrant_scroll_by_arxiv(pid, limit=200)
                except HTTPException:
                    continue
                for pt in points:
                    pay = pt.get("payload") or {}
                    if ((pay.get("section_type") or "") == "limitations"
                            and (pay.get("element_type") or "prose") == "prose"
                            and pay.get("text")):
                        found_limits.append({"arxiv_id": pid, "title": pay.get("title"),
                                             "year": pay.get("year"), "text": pay.get("text")})
                        break
            found_limits += _search(claim, section_type="limitations")
            limitations = ([l for l in found_limits if l["arxiv_id"] in cited_ids]
                           + [l for l in found_limits if l["arxiv_id"] not in cited_ids])[:3]
            if hits:
                try:
                    spec = paper_spec(hits[0]["arxiv_id"], target_elements="algorithm,equation",
                                      max_chars=2500, x_api_key=x_api_key)
                    formulas = spec.get("sections", [])[:2]
                except HTTPException:
                    formulas = []

        years = [h.get("year") for h in hits if h.get("year")]
        claim_reports.append({
            "claim": claim,
            "depth": body.depth,
            "deferred_sections": (["citation_graph", "cross_encoder", "limitations",
                                   "supporting_math"] if fast_mode else []),
            "reranked": rerank_report,
            "phrasings_searched": queries_used,
            "claim_id": claim_id,
            "same_question_candidates": same_question,
            "registry": [{
                "id": pid,
                "status": rec["status"],
                "readers": rec["readers"],
                "models": rec["distinct_models"],
                "counts": rec["counts"],
                "reasons": rec["reasons"][:2],
                "from_claim": rec.get("from_claim"),
                "retrieved_today": pid in {h["arxiv_id"] for h in found},
            } for pid, rec in sorted(registry.items(),
                                     key=lambda kv: -kv[1]["readers"])[:8]],
            # neighbours the bibliography points at are shown even when their
            # own similarity to the claim is mediocre: that is the whole point
            # of the channel. They are candidates, not evidence, and are kept
            # apart from evidence so nothing is smuggled past the bands.
            "graph_candidates": [{
                "id": h["arxiv_id"], "title": h["title"], "year": h.get("year"),
                "score": round(h["score"], 3),
                "prior_reading": readings.get(h["arxiv_id"], {"status": "unread"}),
                "citation_support": h.get("citation_support"),
                "cited_in_index": h.get("cited_in_index"),
                "weight": h.get("citation_weight"),
                "found_via": h.get("found_via"),
                "url": h.get("url"),
            } for h in sorted(graph_added,
                              key=lambda x: (-x.get("citation_weight", 0), -x["score"]))
                ][:6],
            "graph_expansion": {
                "seeds": seeds,
                "neighbours_in_index": len(neighbours),
                "added_to_candidates": len(graph_added),
                "graph_coverage": _graph_coverage(),
                "note": ("Papers reached through the bibliography of the best hits. "
                         "This is the recall channel that does not depend on how the "
                         "claim was phrased. citation_support counts how many of the "
                         "seeds point at the same paper; while graph_coverage is low "
                         "most neighbours are reached by a single seed."),
            },
            "corpus_coverage": {
                "papers_about_this_claim": coverage,
                "is_lower_bound": coverage >= (FAST_COVERAGE_PROBE_LIMIT if fast_mode
                                                else COVERAGE_PROBE_LIMIT),
                "probe_cap": (FAST_COVERAGE_PROBE_LIMIT if fast_mode
                              else COVERAGE_PROBE_LIMIT),
                "by_layer": coverage_by_layer,
                "how_counted": ("papers_about_this_claim is the largest of the "
                                "per-layer counts, not their sum: a paper can sit "
                                "in several layers and each layer is probed with "
                                "its own filter and its own band. When is_lower_bound "
                                "is true, the subject has at least probe_cap papers; "
                                "the endpoint stops counting there to keep audits "
                                "responsive while the live index is being written"),
                "depth": _coverage_grade(coverage),
                "reading": ("A quiet result over a well-covered subject is a real "
                            "signal; over a thin one it only shows the index is thin."
                            if coverage is not None else None),
            },
            "verdict": verdict,
            "reading": reading,
            "suggested_terms": sorted({t for h in (weak or hits)[:5]
                                       for t in (h.get("terms") or [])
                                       if t not in _layer_vocabulary(body.layer)})[:8],
            "out_of_scope_signal": (
                # A single similarity threshold cannot draw this line: robotics
                # legitimately overlaps ai-agents through LLM manipulation, so a
                # soft gripper cleared the bar that perovskite cells failed. Two
                # weak signals together do draw it: almost nothing in the index
                # is about the subject, AND nothing reaches the layer's band.
                {"verdict": "outside_the_index",
                 "reading": ("The index holds almost nothing on this subject and "
                             "nothing that clears the relevance band for its layer. "
                             "Treat this as a statement about the index, not about "
                             "the literature: no conclusion about novelty follows."),
                 # counted at the strict band, not the permissive one: web3's
                 # floor is deliberately low and let eleven papers look like
                 # coverage for a claim about tactile grippers
                 "papers_on_subject": strict_coverage,
                 "papers_at_relevance_floor": coverage,
                 "depth": _coverage_grade(coverage)}
                if (not direct and not adjacent
                    and _coverage_grade(coverage) in ("empty", "thin"))
                else None),
            "recall_note": (
                None if direct else
                "No strong candidate does not mean none exists: recall here follows "
                "wording. Re-run this claim phrased the way the nearest hits above "
                "phrase it before concluding the ground is open."),
            "confidence": ("high" if len([h for h in direct
                                          if h.get("section_type") in ("method", "experiments")]) >= 2
                           else "medium" if direct or adjacent else "low"),
            "verification_required": bool(unread_strong),
            "settled": settled,
            # was reading hits[0], which after registry hydration can be a
            # paper pinned for its verdict rather than for its score
            "best_score": (round(max(h["score"] for h in direct + adjacent), 3)
                           if (direct or adjacent) else
                           round(hits[0]["score"], 3) if hits else None),
            "matches": {"strong_unread": len(unread_strong),
                        "strong_read": len(confirmed) + len(read_once) + len(contested),
                        "ruled_out": len(ruled_out),
                        "adjacent": len(adjacent),
                        "returned": len(hits),
                        "note": "strong_unread counts candidates by wording alone; it is "
                                "not a count of papers that make this claim"},
            "year_range": [min(years), max(years)] if years else None,
            "evidence": [{
                "from_registry": h.get("from_registry", False),
                "id": h["arxiv_id"],
                "title": h["title"],
                "year": h.get("year"),
                "score": round(h["score"], 3),
                "band": _evidence_band(h, body.layer, claim_words),
                "niche_score": h.get("niche_score"),
                "rank_score": round(_rank_score(h), 4),
                "cross_score": h.get("cross_score"),
                "cross_top": h.get("cross_top"),
                "corroboration": _corroborated(h, claim_words),
                "fulltext": h.get("fulltext"),
                "citations": h.get("citation_count"),
                "venue": h.get("venue"),
                "found_via": h.get("found_via"),
                "section": h.get("section_type"),
                "url": h.get("url") or h.get("arxiv_url"),
                "repos": h.get("repos") or [],
                "section_title": h.get("section_title"),
                "prior_reading": readings.get(h["arxiv_id"], {"status": "unread"}),
                "snippet": _clip(h.get("text"), 320),
            } for h in sorted(
                on_record + direct + adjacent,
                key=lambda h: (not h.get("from_registry"),
                               rerank_order.get(h["arxiv_id"], 10_000)
                               if rerank_order else 0,
                               -h["score"]))],
            "weaker_matches": [{
                "id": h["arxiv_id"], "title": h["title"], "year": h.get("year"),
                "score": round(h["score"], 3), "section": h.get("section_type"),
                "prior_reading": readings.get(h["arxiv_id"], {"status": "unread"}),
            } for h in weak],
            "context_mentions": [{
                "id": h["arxiv_id"], "title": h["title"], "year": h.get("year"),
                "score": round(h["score"], 3),
                "snippet": _clip(h.get("text"), 240),
            } for h in mentions[:3]],
            "known_limitations": [{
                "id": l["arxiv_id"], "title": l["title"], "year": l.get("year"),
                "from_cited_paper": l["arxiv_id"] in cited_ids,
                "still_a_candidate": l["arxiv_id"] not in ruled_out_ids,
                "text": _clip(l.get("text"), 400),
            } for l in limitations],
            "supporting_math": [{
                "element_type": f.get("element_type"),
                "section_title": f.get("section_title"),
                "latex": (f.get("text") or "")[:900],
            } for f in formulas],
        })

    try:
        dynamics = trends(layer=body.layer, year_from=2023, top=8, x_api_key=x_api_key)
        field = {
            "papers_per_year": dynamics.get("papers_per_year"),
            "rising": [{"term": t["term"], "growth": t["growth"],
                        "by_year": t["by_year"]}
                       for t in dynamics.get("trends", []) if (t.get("growth") or 0) > 0.3][:5],
            "emerging": dynamics.get("emerging", [])[:6],
        }
    except HTTPException:
        field = {}

    read_total = sum(c["settled"]["pending_quorum"] + c["settled"]["confirmed_prior_art"]
                     + c["settled"]["ruled_out"] + c["settled"]["contested"]
                     for c in claim_reports)
    settled_total = sum(c["settled"]["confirmed_prior_art"] + c["settled"]["ruled_out"]
                        for c in claim_reports)
    unread_total = sum(c["settled"]["unread"] for c in claim_reports)
    counts = {v: sum(1 for c in claim_reports if c["verdict"] == v)
              for v in ("prior_art_confirmed", "prior_art_reported_pending_quorum",
                        "prior_art_reported_on_similar_claim",
                        "candidates_read_pending_quorum",
                        "candidates_ruled_out", "strong_candidates",
                        "adjacent_work_only", "weak_signal_only",
                        "no_match_in_corpus")}
    return {
        "idea": idea,
        "layer": body.layer,
        "corpus": corpus,
        "claims_audited": len(claim_reports),
        "verdict_summary": counts,
        "briefing": (
            "What the field already published on this, what those authors admit went "
            "wrong, and where the open ground is. Verdicts here are candidates until "
            "someone reads them."
        ),
        "headline": (
            f"{len(claim_reports)} claims audited: {read_total} candidates already read "
            f"({settled_total} settled by independent agreement), {unread_total} still "
            f"unread. {counts['no_match_in_corpus']} claims have no match in this index."
        ),
        "scope": {
            "sources": ["arXiv", "IACR ePrint", "Ethereum EIPs", "Solana SIMDs",
                        "industry whitepapers"],
            "not_covered": ["patents and patent applications", "journal and conference "
                            "papers never posted to arXiv", "books", "anything outside "
                            "the indexed AI, Web3 and builder-technology scopes"],
            "patent_note": "This is not a patent search and holds no patent literature. "
                           "For a filing decision, search USPTO/EPO/WIPO separately; a "
                           "quiet result here says nothing about what is patented.",
        },
        "start_here": [
            {"claim": c["claim"], "id": l["id"], "title": l["title"],
             "year": l.get("year"), "limitation": l["text"]}
            for c in claim_reports for l in c["known_limitations"]
            if l.get("from_cited_paper") and l.get("still_a_candidate")
        ][:6],
        "next_step": ("Read the snippets and decide which candidates actually assert each "
                      "claim. The public MCP is read-only; authenticated deployments can "
                      "submit adjudications that are remembered for later audits."),
        "claims": claim_reports,
        "field_dynamics": field,
        "usage": USAGE_NOTICE,
        "how_to_read": {
            "bands_used": {lay: {"direct": d, "adjacent": a}
                           for lay, (d, a) in SCORE_BANDS_BY_LAYER.items()},
            "why_per_layer": ("Scores are not comparable across layers: measured on "
                              "on-topic queries, llm-slm hits land near 0.90 and web3 "
                              "near 0.81. Each band is that layer's own percentile "
                              "(direct = p25, adjacent = p10), so one global constant "
                              "no longer calls a good crypto match 'adjacent'."),
            "direct": "at or above the layer's direct band, outside the introduction, "
                      "from a paper that actually belongs to the layer: a strong "
                      "candidate for the same idea, to be confirmed by reading it",
            "adjacent": "between the layer's two bands, or a high score inside an "
                        "introduction, or a paper with niche_score 1: same problem "
                        "area, different route",
            "weak": "below the layer's adjacent band: topical noise, listed under "
                    "weaker_matches and not counted as evidence",
            "context_mentions": "matches inside related-work sections. They show who is "
                                "reading whom, but they describe OTHER papers' results, "
                                "so they never prove what the citing paper itself does",
            "verification": "A score is lexical similarity, not identity of claim. Before "
                            "reporting anything as prior art, read the snippet and decide "
                            "whether that paper asserts this exact claim.",
            "absence": "A claim with no match is a lead, never a novelty certificate. "
                       "This index is curated and finite; the literature is not.",
        },
    }


@app.get("/v1/health")
def health():
    samples = list(search_latencies)
    return {
        "status": "ok",
        "search_latency_seconds": {
            "samples": len(samples),
            "p50": _latency_percentile(samples, 0.50),
            "p95": _latency_percentile(samples, 0.95),
        },
    }
