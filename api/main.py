import copy
import functools
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional, List, Union

import requests
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


def _state_db_path():
    if os.getenv("STATE_DB_PATH"):
        return os.getenv("STATE_DB_PATH")
    data_dir = os.getenv("DTOX_DATA_DIR")
    return os.path.join(data_dir, "state.db") if data_dir else "/opt/dtox-research/state.db"


def _api_keys_path():
    return Path(os.getenv("API_KEYS_PATH") or os.getenv("KEYS_PATH")
                or os.getenv("RESEARCH_KEYS_PATH") or "/opt/dtox-research-api/keys.json")


try:
    from .search_core import fts_query, fts_query_any, merge_layer_hits, merge_layer_hits_calibrated, reciprocal_rank_fusion, RRF_K
    from .source_representations import MAX_CONTEXTS as _SOURCE_MAX_CONTEXTS, MAX_ORIGINS as _SOURCE_MAX_ORIGINS
    from .boilerplate import is_boilerplate_chunk
    from .exact_match import TitleIndex, extract_ids
    from .evidence import bounded_evidence_page, hydrate_results, provenance, resolution_reasons
    from .source_attribution import curated_source, stored_source_url, valid_source_url
    from .research_bundle import ResearchBundleBody, build_bundle, BUNDLE_CHUNK_LIMIT, BUNDLE_SECONDS, BoundedRetriever
    from .grouped_search import grouped_hits
    from .source_discovery import select_source_candidates, merge_source_candidates
    from .candidate_pool import build_candidate_pool, rows_as_hits
    from .rerank_runtime import RerankRuntime
except ImportError:
    from search_core import fts_query, fts_query_any, merge_layer_hits, merge_layer_hits_calibrated, reciprocal_rank_fusion, RRF_K
    from source_representations import MAX_CONTEXTS as _SOURCE_MAX_CONTEXTS, MAX_ORIGINS as _SOURCE_MAX_ORIGINS
    from boilerplate import is_boilerplate_chunk
    from exact_match import TitleIndex, extract_ids
    from evidence import bounded_evidence_page, hydrate_results, provenance, resolution_reasons
    from source_attribution import curated_source, stored_source_url, valid_source_url
    from research_bundle import ResearchBundleBody, build_bundle, BUNDLE_CHUNK_LIMIT, BUNDLE_SECONDS, BoundedRetriever
    from grouped_search import grouped_hits
    from source_discovery import select_source_candidates, merge_source_candidates
    from candidate_pool import build_candidate_pool, rows_as_hits
    from rerank_runtime import RerankRuntime

# Preamble/author-block/checklist/reference chunks must not stand in for their
# paper in search results. 0 restores the old behavior.
BOILERPLATE_FILTER = os.getenv("BOILERPLATE_FILTER", "1") == "1"
# A paper whose only candidate chunk is boilerplate (often the title page it was
# found by) drops this many places instead of vanishing from the top.
BOILERPLATE_DEMOTE = int(os.getenv("BOILERPLATE_DEMOTE", "3"))

ATTESTOR_URL = os.getenv("ATTESTOR_URL", "http://127.0.0.1:8013")
ATTESTOR_INTERNAL_TOKEN = os.getenv("ATTESTOR_INTERNAL_TOKEN", "")
QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.getenv("QDRANT_COLLECTION", "papers_fulltext")
log = logging.getLogger("dtox-research.api")

# ---------------- hierarchical (two-tier) search ----------------
# papers_fulltext is 9.2M chunks; the canonical paper that introduced an idea
# is one chunk drowning among thousands of descendants' chunks that describe
# the same idea in passing. papers_coarse (pipeline/coarse_index.py) is one
# small in-RAM vector per paper (title+abstract); shortlisting articles there
# first, then searching chunks only within that shortlist, is what actually
# separates the origin paper from its citations. Flagged off by default so it
# can be A/B'd against the existing single-tier path in production.
HIER_SEARCH = os.getenv("HIER_SEARCH", "0") == "1"
SOURCE_DISCOVERY = os.getenv("SOURCE_DISCOVERY", "0") == "1"
SOURCE_GENERATION_MANIFEST = os.getenv("SOURCE_GENERATION_MANIFEST", "")
HIER_GROUPED_SEARCH = os.getenv("HIER_GROUPED_SEARCH", "0") == "1"
COARSE_COLLECTION = os.getenv("COARSE_COLLECTION", "papers_coarse")
HIER_PAPERS = int(os.getenv("HIER_PAPERS", "40"))
# How many extra article candidates the article-level lexical index (see
# PAPER_LEXICAL below) contributes to the stage-A shortlist. Only used when
# both HIER_SEARCH and PAPER_LEXICAL are on.
HIER_LEXICAL_PAPERS = int(os.getenv("HIER_LEXICAL_PAPERS", "15"))
HIER_CHUNKS = int(os.getenv("HIER_CHUNKS", "120"))
# in_citations is in-corpus in-degree (see pipeline/coarse_index.build_payload);
# capped at log1p(1000) so the prior can never exceed HIER_PRIOR_WEIGHT itself.
HIER_PRIOR_WEIGHT = float(os.getenv("HIER_PRIOR_WEIGHT", "0.03"))
HIER_MIX = float(os.getenv("HIER_MIX", "0.5"))
# how long to wait, after stage A+B finish, for the old global chunk channel
# before giving up on it -- it only ever adds a handful of extra papers, never
# gates the response.
HIER_GLOBAL_GRACE = float(os.getenv("HIER_GLOBAL_GRACE", "1.0"))
HIER_GLOBAL_EXTRA = int(os.getenv("HIER_GLOBAL_EXTRA", "3"))
# "mix" (default) is the HIER_MIX paper_score/chunk-score blend plus stage-C
# tail splice, byte-for-byte the original behavior. "rrf" instead fuses three
# paper-level rankings (dense/mix shortlist, article-lexical, old global
# chunk channel) by weighted reciprocal rank fusion -- see _hier_run.
HIER_FUSION = os.getenv("HIER_FUSION", "mix")
HIER_RANK_CHANNELS = os.getenv("HIER_RANK_CHANNELS", "0") == "1"
HIER_W_DENSE = float(os.getenv("HIER_W_DENSE", "1.0"))
HIER_W_LEX = float(os.getenv("HIER_W_LEX", "0.8"))
HIER_W_GLOBAL = float(os.getenv("HIER_W_GLOBAL", "0.5"))
# Fourth RRF channel ("graph", HIER_FUSION="rrf" only): papers cited by >=
# HIER_GRAPH_MIN of the top HIER_GRAPH_POOL fused candidates -- the canon the
# relevant derivative works build on. Each must still pass stage-B chunk
# evidence and the relevance floor like any other candidate. 0 = off.
HIER_GRAPH = os.getenv("HIER_GRAPH", "1") == "1"
HIER_W_GRAPH = float(os.getenv("HIER_W_GRAPH", "0.8"))
HIER_GRAPH_POOL = int(os.getenv("HIER_GRAPH_POOL", "30"))
HIER_GRAPH_MIN = int(os.getenv("HIER_GRAPH_MIN", "3"))
HIER_GRAPH_MAX = int(os.getenv("HIER_GRAPH_MAX", "15"))
# Source quota (needs HIER_GRAPH): an article cited by >= max(HIER_GRAPH_QUOTA_MIN,
# HIER_GRAPH_QUOTA_FRAC of the pool) candidates is the field's primary source;
# RRF alone cannot lift it over dense-ranked derivatives, so it is guaranteed
# rank HIER_GRAPH_QUOTA_POS (1-based) in the pre-filter order, at most
# HIER_GRAPH_QUOTA_MAX such articles per query. It still needs a stage-B chunk
# and must clear the same relevance floor/filters as every other hit. 0 = off.
HIER_GRAPH_QUOTA = os.getenv("HIER_GRAPH_QUOTA", "1") == "1"
HIER_GRAPH_QUOTA_MIN = int(os.getenv("HIER_GRAPH_QUOTA_MIN", "8"))
HIER_GRAPH_QUOTA_FRAC = float(os.getenv("HIER_GRAPH_QUOTA_FRAC", "0.4"))
HIER_GRAPH_QUOTA_POS = int(os.getenv("HIER_GRAPH_QUOTA_POS", "3"))
HIER_GRAPH_QUOTA_MAX = int(os.getenv("HIER_GRAPH_QUOTA_MAX", "1"))
# When a hierarchical search actually runs (not the stage A/B fallback), its
# own paper-level BM25 channel (PAPER_LEXICAL, folded in via _hier_run/the
# paper+chunk fusion below) already covers exact-term matches, so the old
# chunk-level lexical channel -- a 9.2M-row fts.db query on a slow disk, plus
# the _score_specific_papers/_lexical_fallback_results extra-scoring it feeds
# in _run_search's hybrid block -- is redundant work. Default "1" keeps the
# old behavior (chunk lexical always runs) byte-for-byte; "0" skips it, but
# only while hier search is on and actually succeeds -- a stage A/B fallback
# still gets the chunk-level channel, exactly as before.
HIER_CHUNK_LEXICAL = os.getenv("HIER_CHUNK_LEXICAL", "1") == "1"
PAPER_LEXICAL_SOURCE_BALANCE = os.getenv("PAPER_LEXICAL_SOURCE_BALANCE", "0") == "1"
# papers_coarse is migrating from bge-small (384-dim, shared EMBED_URL below) to
# bge-base (768-dim, embed/embed_service.py). Empty by default: stage A embeds
# with the same query vector as chunk search, byte-for-byte the old behavior.
# Once set, only _coarse_paper_search's Qdrant call switches embedder -- stage
# B/C chunk search (papers_fulltext) always stays on bge-small/EMBED_URL. A
# bge-small vector must never reach COARSE_COLLECTION once this is set: Qdrant
# would 400 on the dimension mismatch (384 vs 768) rather than silently degrade.
COARSE_EMBED_URL = os.getenv("COARSE_EMBED_URL", "")
COARSE_EMBED_TIMEOUT = float(os.getenv("COARSE_EMBED_TIMEOUT", "2"))
EMBED_URL = os.getenv("EMBED_URL", "http://127.0.0.1:8006/embed")
EMBED_BATCH_URL = os.getenv("EMBED_BATCH_URL", "http://127.0.0.1:8006/embed_batch")
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
KEYS_PATH = _api_keys_path()

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
# separate from embedding_cache: a coarse-model vector must never be handed
# back to a caller expecting a bge-small one, or vice versa.
coarse_embedding_cache = TTLCache(ttl_seconds=1800, max_size=2000)
source_embedding_cache = TTLCache(ttl_seconds=1800, max_size=2000)
source_health_cache = TTLCache(ttl_seconds=30, max_size=32)


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
TITLE_BUILD_PRIORITY = os.getenv("TITLE_BUILD_PRIORITY", "0") == "1"
# papers_fts.db (see pipeline/build_paper_fts.py): title+abstract only,
# ~164k rows, small enough to sit entirely in the OS page cache -- unlike
# fts.db (9.2M chunks, 13GB, on the slow disk that makes _bm25_candidates
# miss its own LEXICAL_QUERY_TIMEOUT). Off by default so it changes nothing
# until it has actually been built (pipeline/build_paper_fts.py --rebuild).
PAPER_LEXICAL = os.getenv("PAPER_LEXICAL", "0") == "1"
PAPER_FTS_PATH = os.getenv(
    "PAPER_FTS_PATH", os.path.join(os.path.dirname(FTS_DB_PATH) or ".", "papers_fts.db"))
PAPER_BM25_CANDIDATES = 40
SEARCH_EMBED_WAIT_TIMEOUT = float(os.getenv("SEARCH_EMBED_WAIT_TIMEOUT", "2"))
SEARCH_EMBED_HTTP_TIMEOUT = float(os.getenv("SEARCH_EMBED_HTTP_TIMEOUT", "4"))
SEARCH_QDRANT_TIMEOUT = float(os.getenv("SEARCH_QDRANT_TIMEOUT", "6"))
# Hard facet filters (terms, section_type, element_type, year) that leave fewer
# than AUTO_RELAX_MIN results are relaxed automatically, at most
# AUTO_RELAX_MAX_EXTRA extra searches, and the answer says what was dropped.
AUTO_RELAX = os.getenv("AUTO_RELAX", "1") == "1"
AUTO_RELAX_MIN = int(os.getenv("AUTO_RELAX_MIN", "3"))
AUTO_RELAX_MAX_EXTRA = 2
# A framework's own documentation (gh: ids) takes at most this many places per
# 10 requested results unless the query names that framework or project.
GH_DOC_CAP = int(os.getenv("GH_DOC_CAP", "2"))
# Per-stage timings of /v1/search in the log (debugging only).
SEARCH_TIMING = os.getenv("SEARCH_TIMING", "0") == "1"


def _fts_query(text):
    """FTS5 MATCH string: words ANDed, operators stripped, phrases quoted."""
    return fts_query(text)


# Every read-only lookup on the /v1/search path used to open its own
# sqlite3.connect(), and on the production disk a cold connect costs ~0.4s by
# itself -- source_url()/_paper_facts() alone were burning multiple seconds
# per request on nothing but connection setup. A read-only connection has
# nothing to lose by outliving one request, so each worker thread keeps one
# per database and reuses it across calls.
_ro_conn_local = threading.local()


def _ro_conn(db_path, timeout=10):
    """Thread-local, reused sqlite3 read-only connection for `db_path`."""
    conns = getattr(_ro_conn_local, "conns", None)
    if conns is None:
        conns = {}
        _ro_conn_local.conns = conns
    conn = conns.get(db_path)
    if conn is None:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                               timeout=timeout, check_same_thread=False)
        conns[db_path] = conn
    return conn


def _ro_conn_drop(db_path):
    """Forget a cached connection so the next call reconnects from scratch."""
    conns = getattr(_ro_conn_local, "conns", None)
    if conns is not None:
        conns.pop(db_path, None)


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
    deadline = time.monotonic() + LEXICAL_QUERY_TIMEOUT
    try:
        conn = _ro_conn(FTS_DB_PATH)
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        rows = conn.execute(sql, params + [limit * 3]).fetchall()
    except sqlite3.OperationalError:
        # cached connection may have gone stale; reconnect once and retry
        _ro_conn_drop(FTS_DB_PATH)
        try:
            conn = _ro_conn(FTS_DB_PATH)
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            rows = conn.execute(sql, params + [limit * 3]).fetchall()
        except sqlite3.Error:
            return []
    except sqlite3.Error:
        return []
    seen, out = set(), []
    for arxiv_id, _rank in rows:
        if arxiv_id and arxiv_id not in seen:
            seen.add(arxiv_id)
            out.append(arxiv_id)
        if len(out) >= limit:
            break
    return out


_TITLE_BUILD_PRIORITY_CONDITION = threading.Condition()
_ACTIVE_LEXICAL_CALLS = 0


def _track_lexical_call(func):
    @functools.wraps(func)
    def tracked(*args, **kwargs):
        global _ACTIVE_LEXICAL_CALLS
        if not TITLE_BUILD_PRIORITY:
            return func(*args, **kwargs)
        with _TITLE_BUILD_PRIORITY_CONDITION:
            _ACTIVE_LEXICAL_CALLS += 1
        try:
            return func(*args, **kwargs)
        finally:
            with _TITLE_BUILD_PRIORITY_CONDITION:
                _ACTIVE_LEXICAL_CALLS -= 1
                _TITLE_BUILD_PRIORITY_CONDITION.notify_all()
    return tracked


def _yield_title_build():
    with _TITLE_BUILD_PRIORITY_CONDITION:
        _TITLE_BUILD_PRIORITY_CONDITION.wait_for(
            lambda: _ACTIVE_LEXICAL_CALLS == 0, timeout=.05)


@_track_lexical_call
def _paper_bm25(query, layer=None, year_from=None, year_to=None, limit=PAPER_BM25_CANDIDATES):
    """Article ids whose title/abstract contain the query's words, best first.

    Reads papers_fts.db (title+abstract, ~164k rows) instead of the 9.2M-row
    chunk-level fts.db -- small enough to live in the OS page cache, so this
    is meant to answer even when _bm25_candidates misses its own timeout on
    the slow disk under fts.db. Weighted bm25(papers_fts, 0, 3.0, 1.0, 0, 0):
    columns are (arxiv_id, title, abstract, layers, year) in that order, so a
    hit in the title outweighs the same term only in the abstract.
    """
    match = fts_query_any(query)
    if not match:
        return []
    where, params = ["papers_fts MATCH ?"], [match]
    if layer:
        where.append("layers LIKE ?")
        params.append(f"%{layer}%")
    if year_from is not None:
        where.append("CAST(year AS INTEGER) >= ?")
        params.append(year_from)
    if year_to is not None:
        where.append("CAST(year AS INTEGER) <= ?")
        params.append(year_to)
    source_balance = PAPER_LEXICAL_SOURCE_BALANCE and layer == "web3"
    fetch_limit = max(limit, 120) if source_balance and limit <= 120 else limit
    sql = (f"SELECT arxiv_id, rank FROM papers_fts "
           f"WHERE {' AND '.join(where)} AND rank MATCH 'bm25(0, 3.0, 1.0, 0, 0)' "
           "ORDER BY rank LIMIT ?")
    deadline = time.monotonic() + LEXICAL_QUERY_TIMEOUT

    def fetch_rows(conn):
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        try:
            return conn.execute(sql, params + [fetch_limit]).fetchall()
        except sqlite3.OperationalError as error:
            if time.monotonic() >= deadline:
                raise FuturesTimeoutError() from error
            raise
        finally:
            conn.set_progress_handler(None, 0)

    try:
        conn = _ro_conn(PAPER_FTS_PATH)
        rows = fetch_rows(conn)
    except FuturesTimeoutError:
        raise
    except sqlite3.OperationalError:
        # cached connection may have gone stale, or the file doesn't exist yet
        # (PAPER_LEXICAL flipped on before the first --rebuild); reconnect once.
        _ro_conn_drop(PAPER_FTS_PATH)
        try:
            rows = fetch_rows(_ro_conn(PAPER_FTS_PATH))
        except FuturesTimeoutError:
            raise
        except sqlite3.Error:
            if time.monotonic() >= deadline:
                raise FuturesTimeoutError()
            return []
    except sqlite3.Error:
        if time.monotonic() >= deadline:
            raise FuturesTimeoutError()
        return []
    ids = [row[0] for row in rows if row[0]]
    if not source_balance:
        return ids[:limit]
    whitepapers = [pid for pid in ids if pid.startswith("wp:")][:2]
    standards = [pid for pid in ids if pid.startswith(("eip:", "simd:"))][:2]
    fused = reciprocal_rank_fusion(ids, whitepapers, standards)
    return sorted(ids, key=lambda pid: -fused[pid])[:limit]


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
        conn = _ro_conn(STATE_DB_PATH)
        row = conn.execute("SELECT status FROM papers WHERE arxiv_id=?",
                           (arxiv_id,)).fetchone()
    except sqlite3.OperationalError:
        _ro_conn_drop(STATE_DB_PATH)
        try:
            row = _ro_conn(STATE_DB_PATH).execute(
                "SELECT status FROM papers WHERE arxiv_id=?", (arxiv_id,)).fetchone()
        except sqlite3.Error:
            return None
    except sqlite3.Error:
        return None
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


def embed_query_coarse(text: str, timeout=None) -> List[float]:
    """Embed a query with the coarse-index model (COARSE_EMBED_URL), for stage
    A of hierarchical search only. Own LRU cache: this vector's dimension
    (bge-base, 768) differs from embed_query's (bge-small, 384) and the two
    must never be mixed up or handed to the wrong collection. Raises
    requests.RequestException on failure/timeout -- callers are expected to
    fall back to the old (shared-vector) path, exactly as when papers_coarse
    itself is unavailable.
    """
    timeout = COARSE_EMBED_TIMEOUT if timeout is None else timeout
    text = (text or "").strip()
    cached = coarse_embedding_cache.get(text)
    if cached is not None:
        return cached
    resp = requests.post(COARSE_EMBED_URL, json={"text": text, "query": True},
                         timeout=timeout)
    resp.raise_for_status()
    vector = resp.json()["vector"]
    coarse_embedding_cache.set(text, vector)
    return vector


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


def _qdrant_cache_key(vector, qfilter, with_payload=True, collection=None):
    vector_key = hashlib.sha1(
        json.dumps(vector, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    filter_key = json.dumps(qfilter, sort_keys=True, separators=(",", ":")) if qfilter else ""
    # with_payload is part of the identity: a cached no-payload (or partial-
    # payload) result must never be handed to a caller that expects a
    # different set of payload fields to be usable. collection likewise, so a
    # papers_coarse search can never answer a papers_fulltext one from cache.
    # Qdrant also accepts a field-name list for with_payload (partial
    # payload); normalize that to a hashable, order-independent tuple.
    payload_key = (tuple(sorted(with_payload)) if isinstance(with_payload, list)
                  else with_payload)
    return vector_key, filter_key, payload_key, collection or COLLECTION


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


def _filter_publication_points(points):
    """Hide revision-tagged points until their exact snapshot is published.

    Unmarked points predate publication journaling and intentionally remain
    visible without opening state.db. Journal lookups are exact-pair bounded.
    """
    points = list(points or [])
    marked = []
    for point in points:
        payload = point.get("payload") or {}
        if "publication_revision" in payload:
            revision = payload.get("publication_revision")
            paper_id = payload.get("arxiv_id")
            if isinstance(paper_id, str) and paper_id and isinstance(revision, str) and revision:
                marked.append((paper_id, revision))
    if not any("publication_revision" in (point.get("payload") or {}) for point in points):
        return points
    if not marked:
        return [point for point in points
                if "publication_revision" not in (point.get("payload") or {})]

    visible = set()
    unique_pairs = list(dict.fromkeys(marked))
    try:
        conn = _ro_conn(STATE_DB_PATH)
        for start in range(0, len(unique_pairs), 200):
            batch = unique_pairs[start:start + 200]
            exact_pairs = " OR ".join(
                "(arxiv_id=? AND revision=? AND kind='snapshot' AND state='published')"
                for _ in batch)
            params = [value for pair in batch for value in pair]
            rows = conn.execute(
                "SELECT arxiv_id, revision FROM paper_publications "
                f"WHERE {exact_pairs}",
                params,
            ).fetchall()
            visible.update((row[0], row[1]) for row in rows)
    except (sqlite3.Error, OSError):
        # Journal uncertainty fails closed only for revision-tagged points.
        visible.clear()

    def is_visible(point):
        payload = point.get("payload") or {}
        if "publication_revision" not in payload:
            return True
        paper_id, revision = payload.get("arxiv_id"), payload.get("publication_revision")
        return (isinstance(paper_id, str) and bool(paper_id)
                and isinstance(revision, str) and bool(revision)
                and (paper_id, revision) in visible)

    return [point for point in points if is_visible(point)]


def qdrant_search(vector, qfilter, limit, timeout=45, with_payload=True, collection=None):
    collection = collection or COLLECTION
    if isinstance(with_payload, list):
        with_payload = list(dict.fromkeys(with_payload + ["arxiv_id", "publication_revision"]))
    cache_key = _qdrant_cache_key(vector, qfilter, with_payload, collection)
    cached = qdrant_result_cache.get(cache_key)
    if cached is not None and cached["limit"] >= limit:
        return _filter_publication_points(cached["results"][:limit])
    body = {"vector": vector, "limit": limit, "with_payload": with_payload}
    if qfilter:
        body["filter"] = qfilter
    try:
        resp = requests.post(f"{QDRANT_URL}/collections/{collection}/points/search",
                             json=body, timeout=timeout)
        resp.raise_for_status()
        results = resp.json()["result"]
        qdrant_result_cache.set(cache_key, {"limit": limit, "results": results})
        return _filter_publication_points(results)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"qdrant error: {e}")


def qdrant_fetch_payloads(ids, timeout=45):
    """Phase 2 of two-phase retrieval: one batch payload fetch for winners only.

    Reading payload for every dense_limit candidate per layer is what makes
    cold public search slow against the disk-backed 9.2M-point collection
    (11GB of on_disk_payload); only a handful of the 128 candidates ever reach
    the response. Phase 1 fetches id+score only; this fetches payload once,
    for the merged top-K ids.
    """
    if not ids:
        return {}
    try:
        resp = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points",
            json={"ids": ids, "with_payload": True, "with_vector": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        points = resp.json().get("result") or []
        points = _filter_publication_points(points)
        return {p["id"]: (p.get("payload") or {}) for p in points}
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"qdrant error: {e}")


def _hydrate_hits(hits, timeout):
    """Attach payload to a slice of phase-1 (no-payload) hits, one batch call."""
    ids = [h["id"] for h in hits if h.get("id") is not None]
    if not ids:
        return []
    payload_map = qdrant_fetch_payloads(ids, timeout=timeout)
    hydrated = []
    for h in hits:
        payload = payload_map.get(h.get("id"))
        if payload is None:
            continue
        hit = dict(h)
        hit["payload"] = payload
        hydrated.append(hit)
    return hydrated


def two_phase_dense_search(vector, qfilter, dense_limit, final_limit, timeout, layers=None):
    """Two-phase dense retrieval shared by the single-layer and all-layer paths.

    Phase 1 asks Qdrant for id+score only (with_payload=False), per layer when
    `layers` is given (merged by score above each layer's median, see
    merge_layer_hits_calibrated), or as one filtered search otherwise. Phase 2 hydrates only the merged top-K ids -- K =
    final_limit*3, capped by however many candidates phase 1 actually returned
    -- with a single POST /points batch call, preserving phase 1's order and
    score.

    Returns (hydrated_hits, merged_pool, failed_layers). merged_pool is the
    full ranked, still-payload-less candidate list so a caller whose dedupe
    comes up short after phase 2 can hydrate one further slice without paying
    for another Qdrant vector search.
    """
    if layers:
        def search_layer(layer):
            must = list((qfilter or {}).get("must") or [])
            must.append({"key": "layers", "match": {"any": [layer]}})
            return qdrant_search(vector, {"must": must}, dense_limit, timeout=timeout,
                                  with_payload=False)

        result_sets, failed = [], []
        with ThreadPoolExecutor(max_workers=len(layers)) as pool:
            futures = {pool.submit(search_layer, layer): layer
                       for layer in sorted(layers)}
            for future in as_completed(futures):
                layer = futures[future]
                try:
                    result_sets.append((layer, future.result()))
                except HTTPException:
                    failed.append(layer)
        if not result_sets:
            raise HTTPException(status_code=502, detail="qdrant layer searches timed out")
        total_candidates = sum(len(r) for _, r in result_sets)
        merged_pool = merge_layer_hits_calibrated(result_sets, LAYER_SCORE_MEDIAN,
                                                  LAYER_SCORE_MEDIAN_DEFAULT, total_candidates)
    else:
        merged_pool = qdrant_search(vector, qfilter, dense_limit, timeout=timeout,
                                    with_payload=False)
        failed = []
        total_candidates = len(merged_pool)

    k = min(final_limit * 3, total_candidates)
    hydrated = _hydrate_hits(merged_pool[:k], timeout)
    return hydrated, merged_pool[:k], merged_pool[k:], sorted(failed)


# ---------------- hierarchical (two-tier) search: stages A/B/C ----------------

def _coarse_paper_search(vector, layer, year_from, year_to, limit, timeout, query=None):
    """Stage A: shortlist articles from the small in-RAM papers_coarse index.

    Only layer/year are applied here -- section_type/element_type/terms are
    chunk-level facets that papers_coarse's one-point-per-paper payload can't
    answer. Returns papers best-first by paper_score = cosine + a small prior
    for in-corpus citation in-degree (see pipeline/coarse_index.build_payload),
    so a paper the corpus itself treats as canonical (e.g. LoRA, cited by
    thousands of its own descendants) outranks a near-tied chunk-level match
    from one of those descendants.

    When COARSE_EMBED_URL is set, the query is re-embedded with the coarse
    model (papers_coarse_base, bge-base) instead of reusing `vector` (the
    bge-small vector chunk search already computed) -- the two collections are
    on different embedders and their vectors are not interchangeable. On
    failure this raises, letting the caller (_hier_run) fall back exactly as
    it does when papers_coarse itself is unavailable.
    """
    search_vector = vector
    if COARSE_EMBED_URL:
        search_vector = embed_query_coarse(query)
    must = []
    if layer:
        must.append({"key": "layers", "match": {"any": [layer]}})
    if year_from is not None or year_to is not None:
        rng = {}
        if year_from is not None:
            rng["gte"] = year_from
        if year_to is not None:
            rng["lte"] = year_to
        must.append({"key": "year", "range": rng})
    qfilter = {"must": must} if must else None
    hits = qdrant_search(search_vector, qfilter, limit, timeout=timeout, with_payload=True,
                         collection=COARSE_COLLECTION)
    cap = math.log1p(1000)
    papers = []
    for h in hits:
        payload = h.get("payload") or {}
        aid = payload.get("arxiv_id")
        if not aid:
            continue
        cosine = h.get("score") or 0
        in_citations = payload.get("in_citations") or 0
        # min(1.0, ...) is the clamp: the prior can never exceed HIER_PRIOR_WEIGHT.
        factor = min(1.0, math.log1p(max(in_citations, 0)) / cap) if cap else 0.0
        papers.append({"arxiv_id": aid, "paper_score": cosine + HIER_PRIOR_WEIGHT * factor})
    papers.sort(key=lambda p: -p["paper_score"])
    return papers


def _lexical_paper_shortlist(papers, query, layer, year_from, year_to,
                             limit=HIER_LEXICAL_PAPERS, lexical_future=None):
    """Stage A extra: articles found by exact title/abstract term match that
    the coarse dense shortlist didn't surface (e.g. an exact acronym like
    "GRPO" that the coarse embedding doesn't place near the query vector).

    papers_coarse never scored these ids, so there is no cosine to mix in for
    them. Chosen here: give each one the shortlist's own minimum paper_score
    (a point cosine lookup into papers_coarse for just these ids would be
    more precise, but costs another Qdrant round trip on the search hot path
    for a value stage B's chunk evidence -- HIER_MIX -- already dominates
    the final ranking with). A lexical-only paper therefore never outranks
    the dense shortlist on paper_score alone; it only surfaces at all if
    stage B finds it a real, on-topic chunk.

    Returns (papers, lexical_ids): lexical_ids is the raw _paper_bm25 order,
    best-first, kept around for HIER_FUSION="rrf" (see _hier_run) to build
    its own lexical ranking without a second BM25 round trip.
    """
    lexical_ids = (lexical_future.result(timeout=LEXICAL_QUERY_TIMEOUT)
                   if lexical_future is not None else
                   _paper_bm25(query, layer, year_from, year_to, limit=limit))[:limit]
    if not lexical_ids:
        return papers, []
    existing = {p["arxiv_id"] for p in papers}
    floor = min((p["paper_score"] for p in papers), default=0.0)
    for aid in lexical_ids:
        if aid in existing:
            continue
        papers.append({"arxiv_id": aid, "paper_score": floor})
        existing.add(aid)
    return papers, lexical_ids


def _document_layer_filter(must, arxiv_ids):
    ids = list(dict.fromkeys(arxiv_ids))
    legacy = {"must": list(must) + [{"key": "arxiv_id", "match": {"any": ids}}]}
    layer_filters = [item for item in must if item.get("key") == "layers"]
    if not layer_filters or len(ids) > 500:
        return legacy
    requested = []
    for item in layer_filters:
        match = item.get("match") or {}
        if set(item) != {"key", "match"} or set(match) not in ({"any"}, {"value"}):
            return legacy
        values = match["any"] if "any" in match else [match["value"]]
        if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
            return legacy
        requested.append(set(values))
    rows = _evidence_rows(ids)
    verified, unknown = [], []
    for pid in ids:
        row = rows.get(pid) or {}
        layers = row.get("layers")
        if (row.get("arxiv_id") != pid or not isinstance(layers, list) or not layers
                or any(not isinstance(layer, str) or not layer for layer in layers)):
            unknown.append(pid)
        elif all(set(layers) & values for values in requested):
            verified.append(pid)
    if not verified:
        if not unknown:
            return None
        return {"must": list(must) + [{"key": "arxiv_id", "match": {"any": unknown}}]}
    facets = [item for item in must if item.get("key") != "layers"]
    allowed = [pid for pid in ids if pid in verified or pid in unknown]
    result = {"must": facets + [{"key": "arxiv_id", "match": {"any": allowed}}]}
    if unknown:
        result["should"] = [
            {"must": [{"key": "arxiv_id", "match": {"any": verified}}]},
            {"must": [{"key": "arxiv_id", "match": {"any": unknown}}] + layer_filters},
        ]
    return result


class _StageBHits(list):
    def __init__(self, hits, trace, partial=()):
        super().__init__(hits)
        self.trace = trace
        self.partial = list(partial)


def _shortlist_diversity_hits(vector, filt, arxiv_ids, hits, budget, timeout):
    allowed = next(item["match"]["any"] for item in filt["must"]
                   if item.get("key") == "arxiv_id" and "any" in item.get("match", {}))
    covered = {canonical_id((hit.get("payload") or {}).get("arxiv_id")) for hit in hits}
    groups = {}
    for aid in arxiv_ids:
        if aid in allowed and canonical_id(aid) not in covered:
            groups.setdefault(canonical_id(aid), []).append(aid)
    missing = list(groups.values())
    selected = missing[:min(max(budget, 0), 500)]
    trace = {"pooled_chunk_count": len(hits), "missing_source_count": len(missing),
             "rescue_query_count": 0, "rescued_source_count": 0,
             "source_status": {aid: "not_attempted" for group in missing for aid in group}}
    partial = ["stage_b_diversity_budget"] if len(selected) < len(missing) else []
    if not selected:
        return _StageBHits(hits, trace, partial)
    if timeout <= 0:
        return _StageBHits(hits, trace, [*partial, "stage_b_diversity_timeout"])
    searches = []
    for group in selected:
        per_source = copy.deepcopy(filt)
        per_source["must"].append({"key": "arxiv_id", "match": {"any": group}})
        searches.append({"vector": vector, "filter": per_source, "limit": 1,
                         "with_payload": True, "with_vector": False})
    trace["rescue_query_count"] = len(searches)
    try:
        response = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/search/batch",
                                 json={"searches": searches}, timeout=timeout)
        response.raise_for_status()
        results = response.json().get("result")
        if (not isinstance(results, list) or len(results) != len(selected)
                or any(not isinstance(result, list) or len(result) > 1 for result in results)):
            raise ValueError("malformed Qdrant shortlist batch")
        for group, result in zip(selected, results):
            for hit in result:
                score = hit.get("score")
                if (not isinstance(hit.get("payload"), dict)
                        or hit["payload"].get("arxiv_id") not in group
                        or hit.get("id") is None or isinstance(score, bool)
                        or not isinstance(score, (int, float)) or not math.isfinite(score)):
                    raise ValueError("malformed Qdrant shortlist hit")
        visible_ids = {hit["id"] for hit in _filter_publication_points(
            [hit for result in results for hit in result])}
        rescued = []
        for group, result in zip(selected, results):
            visible = [hit for hit in result if hit["id"] in visible_ids]
            for aid in group:
                trace["source_status"][aid] = "rescued" if visible else "no_visible_chunk_returned"
            if visible:
                rescued.append({**visible[0], "stage_b_origin": "shortlist_rescue"})
        trace["rescued_source_count"] = len(rescued)
        return _StageBHits([*hits, *rescued], trace, partial)
    except (requests.RequestException, ValueError, TypeError, AttributeError) as error:
        log.warning("HIER_SEARCH: shortlist diversity incomplete: %s", error)
        for group in selected:
            for aid in group:
                trace["source_status"][aid] = "batch_failed"
        return _StageBHits(hits, trace, [*partial, "stage_b_diversity"])


def _hier_stage_b_hits(vector, must, arxiv_ids, dense_limit, timeout):
    """Stage B: chunk evidence for exactly the stage-A shortlist.

    One filtered two_phase_dense_search over papers_fulltext -- arxiv_id in
    the shortlist, canonical document layer where known, and every chunk
    facet (section_type, element_type, terms, year). Unknown metadata keeps
    the original chunk-layer constraint. If the pooled budget can hide a
    shortlisted source, one bounded batch checks its best matching chunk.
    """
    unique_ids = list(dict.fromkeys(arxiv_ids))
    if not unique_ids:
        return []
    filt = _document_layer_filter(must, unique_ids)
    if filt is None:
        return []
    if HIER_GROUPED_SEARCH:
        try:
            hits = grouped_hits(requests.post, QDRANT_URL, COLLECTION, vector, filt,
                                limit=len(unique_ids), timeout=timeout, group_size=1)
            return _filter_publication_points(hits)
        except (requests.RequestException, ValueError) as error:
            raise HTTPException(status_code=502,
                                detail=f"qdrant grouped search error: {error}") from error
    deadline = time.monotonic() + timeout
    final_limit = max(len(arxiv_ids), 1)
    hits, hydrated_pool, leftover_pool, _ = two_phase_dense_search(
        vector, filt, dense_limit, final_limit, timeout, layers=None)
    if len(hydrated_pool) + len(leftover_pool) >= dense_limit or leftover_pool:
        result = _shortlist_diversity_hits(vector, filt, unique_ids, hits, dense_limit,
                                          deadline - time.monotonic())
        result.trace["pooled_candidate_count"] = len(hydrated_pool) + len(leftover_pool)
        return result
    return hits


def _select_graph_quota(l_graph, graph_counts, pool_size, chunk_ids):
    """Primary sources per HIER_GRAPH_QUOTA: {id: citing count}, most cited
    first, at most HIER_GRAPH_QUOTA_MAX. Only ids with stage-B chunk evidence.
    """
    if not (HIER_GRAPH_QUOTA and l_graph and graph_counts and pool_size):
        return {}
    need = max(HIER_GRAPH_QUOTA_MIN, HIER_GRAPH_QUOTA_FRAC * pool_size)
    cands = [a for a in l_graph if a in chunk_ids and graph_counts.get(a, 0) >= need]
    cands.sort(key=lambda a: -graph_counts[a])
    return {a: graph_counts[a] for a in cands[:HIER_GRAPH_QUOTA_MAX]}


def _inject_graph_quota(results, extra, floor, exempt_ids, limit):
    """Guarantee each quota article rank HIER_GRAPH_QUOTA_POS in the final
    `results` (already fused/sorted). An article that is already at or above
    the slot stays; otherwise it is moved or built from its stage-B chunk, but
    only if that chunk clears the same relevance floor as any other hit.
    Displaces at most HIER_GRAPH_QUOTA_MAX articles out of the top `limit`.
    """
    quota = extra.get("graph_quota") or {}
    chunks = extra.get("quota_chunks") or {}
    if not quota:
        return results
    results = list(results)
    slot = max(HIER_GRAPH_QUOTA_POS - 1, 0)
    placed = 0
    for aid, note in quota.items():
        chunk = chunks.get(aid)
        if chunk is None:
            continue
        if (chunk.get("score") or 0) < floor and aid not in exempt_ids:
            continue
        key = canonical_id(aid)
        cur = next((i for i, r in enumerate(results) if canonical_id(r["arxiv_id"]) == key), None)
        target = min(slot + placed, len(results))
        if cur is not None and cur <= target:
            results[cur]["graph_quota"] = note
            placed += 1
            continue
        if cur is not None:
            item = results.pop(cur)
        else:
            item = _result_from_hit(chunk)
            item["paper_score"] = (extra.get("paper_scores") or {}).get(item["arxiv_id"])
            item["fusion_ranks"] = (extra.get("fusion_ranks") or {}).get(item["arxiv_id"])
            item["channels"] = ["paper", "graph"]
        item["found_via"] = ("paper graph" if item.get("found_via") in (None, "paper")
                             else item["found_via"])
        item["graph_quota"] = note
        results.insert(target, item)
        placed += 1
    return results[:limit]


def _graph_channel(pool_ids, known_ids, chunk_ids, vector, must, timeout, counts_out=None):
    """Fourth RRF channel: articles cited by >= HIER_GRAPH_MIN of `pool_ids`
    (same idea as _foundational), best-first, each with a stage-B best chunk.

    `known_ids` are ids already in the fused pool (a twin of one of them is
    reused instead of a second id for the same paper); `chunk_ids` those that
    already have chunk evidence. Layer/year/section filters are applied by the
    stage-B chunk search itself (`must`); a cited paper with no chunk under
    them is dropped. Returns (ranked ids, {id: chunk}). Never raises.
    """
    try:
        ids = list(pool_ids)[:HIER_GRAPH_POOL]
        if not ids:
            return [], {}
        sql = (f"SELECT dst, COUNT(*) AS n FROM citations WHERE src IN ({','.join('?' * len(ids))}) "
               f"GROUP BY dst ORDER BY n DESC LIMIT 60")
        try:
            # threshold applied after twin merge below: two copies of one paper
            # may each fall under it while together clearing it
            counts = _ro_conn(STATE_DB_PATH).execute(sql, ids).fetchall()
        except sqlite3.Error:
            _ro_conn_drop(STATE_DB_PATH)
            if counts_out is not None:
                counts_out["_error"] = True
            return [], {}
        heads = {}
        for pid, n in counts:
            head = canonical_id(pid)
            slot = heads.setdefault(head, {"n": 0, "ids": []})
            slot["n"] += n
            slot["ids"].append(pid)
        state = _twins()
        ranked = []
        qualified = [kv for kv in heads.items() if kv[1]["n"] >= HIER_GRAPH_MIN]
        for head, slot in sorted(qualified, key=lambda kv: -kv[1]["n"])[:HIER_GRAPH_MAX]:
            members = set(state["members"].get(head, [head])) | set(slot["ids"])
            pick = next((m for m in members if m in known_ids), None) or slot["ids"][0]
            ranked.append(pick)
            if counts_out is not None:
                counts_out[pick] = slot["n"]
        need = [a for a in ranked if a not in chunk_ids]
        chunks = {}
        if need:
            stage_b = _hier_stage_b_hits(vector, must, need, HIER_CHUNKS, timeout)
            if getattr(stage_b, "partial", []):
                if counts_out is not None:
                    counts_out["_error"] = True
            for h in stage_b:
                aid = (h.get("payload") or {}).get("arxiv_id")
                if aid and (aid not in chunks or (h.get("score") or 0) > (chunks[aid].get("score") or 0)):
                    chunks[aid] = h
        return [a for a in ranked if a in chunk_ids or a in chunks], chunks
    except Exception as exc:
        log.warning(f"HIER_GRAPH: channel failed, continuing without it: {exc}")
        if counts_out is not None:
            counts_out["_error"] = True
        return [], {}


def _hier_run(vector, qfilter, must, body, limit, dense_limit, dense_timeout, layers, query="",
              paper_lexical_future=None):
    """Stages A+B+C of HIER_SEARCH, sharing _run_search's old dense-retrieval
    shape so the caller doesn't need to know which path ran.

    Stage C (the old global per-layer chunk search) is fired first so it is
    already in flight while A and B run; that same in-flight call also serves
    as the silent fallback if stage A errors or comes back empty, so a coarse
    outage costs nothing beyond the wasted stage-A round trip -- never extra
    latency on top of the old path.

    Returns (hits, leftover_pool, dense_partial, extra). `extra` is None when
    stage A failed and the old path was used instead (nothing hier-specific to
    report); otherwise a dict with `paper_scores` (arxiv_id -> rounded
    paper_score), `global_channel` ("skipped" when stage C didn't finish
    within HIER_GLOBAL_GRACE, else None), and `global_extra`.

    HIER_FUSION="mix" (default): `global_extra` is up to HIER_GLOBAL_EXTRA raw
    hydrated hits from stage C not already covered by A+B, old-path order
    preserved; the caller splices them onto the tail.

    HIER_FUSION="rrf": stage C is folded into the ranking itself instead of
    being spliced onto the tail, so `global_extra` is always []. `extra` also
    carries `fusion_ranks` (arxiv_id -> {"dense", "lex", "global"} 1-based
    ranks or None) and `found_via` (arxiv_id -> "paper" | "paper lexical") and
    `lex_exempt` (arxiv_ids ranked <=3 in the lexical list, which the caller's
    relevance floor must not drop even below-floor).
    """
    global_pool = ThreadPoolExecutor(max_workers=1)
    global_future = global_pool.submit(
        two_phase_dense_search, vector, qfilter, dense_limit, limit, dense_timeout,
        layers=layers)
    fallback_reason = "coarse_fallback"
    paper_dense_ids = []
    stage_b_partial = []
    try:
        papers = _coarse_paper_search(vector, body.layer, body.year_from, body.year_to,
                                      HIER_PAPERS, dense_timeout, query=query)
        paper_dense_ids = [p["arxiv_id"] for p in papers]
        lexical_ids = []
        paper_lexical_skipped = False
        if PAPER_LEXICAL:
            try:
                papers, lexical_ids = _lexical_paper_shortlist(
                    papers, query, body.layer, body.year_from, body.year_to,
                    lexical_future=paper_lexical_future)
            except FuturesTimeoutError:
                paper_lexical_skipped = True
        if not papers:
            fallback_reason = "no_coarse_candidates"
            raise RuntimeError("papers_coarse returned no candidates")
        arxiv_ids = [p["arxiv_id"] for p in papers]
        stage_b_hits = _hier_stage_b_hits(vector, must, arxiv_ids, HIER_CHUNKS, dense_timeout)
        stage_b_partial = getattr(stage_b_hits, "partial", [])
        stage_b_trace = getattr(stage_b_hits, "trace", None)
        best_chunk = {}
        for h in stage_b_hits:
            aid = (h.get("payload") or {}).get("arxiv_id")
            if not aid:
                continue
            if aid not in best_chunk or (h.get("score") or 0) > (best_chunk[aid].get("score") or 0):
                best_chunk[aid] = h
        merged = []
        for p in papers:
            chunk = best_chunk.get(p["arxiv_id"])
            if chunk is None:
                continue  # no chunk survived stage B's filters for this paper
            final_score = HIER_MIX * p["paper_score"] + (1 - HIER_MIX) * (chunk.get("score") or 0)
            merged.append((final_score, p["paper_score"], chunk))
        if not merged:
            fallback_reason = "no_qualifying_chunks"
            raise RuntimeError("no stage-B chunk evidence for any shortlisted paper")
        merged.sort(key=lambda t: -t[0])
        hits = [chunk for _, _, chunk in merged]
        paper_scores = {chunk["payload"]["arxiv_id"]: round(pscore, 4)
                        for _, pscore, chunk in merged}
    except Exception as exc:
        log.warning(f"HIER_SEARCH: stage A/B failed, falling back to the old path: {exc}")
        try:
            hits, _, leftover_pool, dense_partial = global_future.result(timeout=dense_timeout)
        finally:
            global_pool.shutdown(wait=False)
        if fallback_reason == "coarse_fallback":
            dense_partial = list(dense_partial) + [fallback_reason]
        dense_partial = list(dense_partial) + stage_b_partial
        return hits, leftover_pool, dense_partial, None

    known_ids = {chunk["payload"]["arxiv_id"] for chunk in hits}
    global_channel = None
    c_hits = []
    c_partial = list(stage_b_partial)
    try:
        c_hits, _, _, global_partial = global_future.result(timeout=HIER_GLOBAL_GRACE)
        c_partial.extend(global_partial)
    except (FuturesTimeoutError, HTTPException):
        global_channel = "skipped"
        c_partial.append("global")
    finally:
        global_pool.shutdown(wait=False)

    if HIER_FUSION != "rrf":
        extra = {"paper_scores": paper_scores, "global_channel": global_channel,
                 "global_extra": [], "paper_lexical_used": bool(lexical_ids),
                 "paper_lexical_skipped": paper_lexical_skipped,
                 "stage_b_trace": stage_b_trace}
        for h in c_hits:
            if len(extra["global_extra"]) >= HIER_GLOBAL_EXTRA:
                break
            aid = (h.get("payload") or {}).get("arxiv_id")
            if aid and aid not in known_ids:
                extra["global_extra"].append(h)
                known_ids.add(aid)
        return hits, [], c_partial, extra

    # HIER_FUSION == "rrf": weighted RRF over paper-level rankings instead
    # of the mix blend + tail splice above. The optional rank-channel path
    # keeps coarse-paper and small-chunk cosine scales separate.
    l_lex = [aid for aid in lexical_ids if aid in best_chunk]
    rank_channels = HIER_RANK_CHANNELS
    if rank_channels:
        l_paper_dense = [aid for aid in paper_dense_ids if aid in best_chunk]
        l_chunk = sorted(best_chunk,
                         key=lambda aid: (-(best_chunk[aid].get("score") or 0), aid))
        l_dense = l_paper_dense
        base_lists = [l_paper_dense, l_chunk, l_lex]
        base_weights = [HIER_W_DENSE / 2, HIER_W_DENSE / 2, HIER_W_LEX]
    else:
        l_dense = [chunk["payload"]["arxiv_id"] for _, _, chunk in merged]
        base_lists = [l_dense, l_lex]
        base_weights = [HIER_W_DENSE, HIER_W_LEX]
    global_chunk = {}
    l_global = []
    for h in c_hits:
        aid = (h.get("payload") or {}).get("arxiv_id")
        if not aid:
            continue
        if aid not in l_global:
            l_global.append(aid)
        if rank_channels:
            if aid in best_chunk:
                if (h.get("score") or 0) > (best_chunk[aid].get("score") or 0):
                    best_chunk[aid] = h
            elif (aid not in global_chunk
                  or (h.get("score") or 0) > (global_chunk[aid].get("score") or 0)):
                global_chunk[aid] = h
        elif aid not in best_chunk and aid not in global_chunk:
            global_chunk[aid] = h

    rank_lists = [*base_lists, l_global]
    rank_weights = [*base_weights, HIER_W_GLOBAL]
    fused = reciprocal_rank_fusion(*rank_lists, weights=rank_weights)

    l_graph = []
    graph_chunk = {}
    graph_counts = {}
    graph_pool = 0
    if HIER_GRAPH:
        pre_candidates = list(dict.fromkeys(a for ranked in rank_lists for a in ranked))
        if rank_channels:
            pre_order = {aid: i for i, aid in enumerate(pre_candidates)}
            pre = sorted(pre_candidates,
                         key=lambda aid: (-fused.get(aid, 0.0), pre_order[aid]))
        else:
            pre = sorted(set(pre_candidates), key=lambda aid: -fused.get(aid, 0.0))
        have = set(best_chunk) | set(global_chunk)
        pre = [a for a in pre if a in have]
        graph_counts = {}
        l_graph, graph_chunk = _graph_channel(pre, set(pre), have, vector, must, dense_timeout,
                                              counts_out=graph_counts)
        if graph_counts.pop("_error", False):
            c_partial = list(c_partial) + ["graph"]
        graph_pool = min(len(pre), HIER_GRAPH_POOL)
        if l_graph:
            fused = reciprocal_rank_fusion(
                *rank_lists, l_graph, weights=[*rank_weights, HIER_W_GRAPH])

    dense_rank = {aid: i + 1 for i, aid in enumerate(l_dense)}
    chunk_rank = ({aid: i + 1 for i, aid in enumerate(l_chunk)}
                  if rank_channels else {})
    lex_rank = {aid: i + 1 for i, aid in enumerate(l_lex)}
    global_rank = {aid: i + 1 for i, aid in enumerate(l_global)}

    chunk_by_id = dict(best_chunk)
    for aid, h in global_chunk.items():
        chunk_by_id.setdefault(aid, h)
    for aid, h in graph_chunk.items():
        chunk_by_id.setdefault(aid, h)

    if rank_channels:
        final_lists = [*rank_lists, *([l_graph] if l_graph else [])]
        all_ids = list(dict.fromkeys(aid for ranked in final_lists for aid in ranked))
        tie_order = {aid: i for i, aid in enumerate(all_ids)}
        ordered_ids = sorted(all_ids,
                             key=lambda aid: (-fused.get(aid, 0.0), tie_order[aid]))
    else:
        all_ids = set(l_dense) | set(l_lex) | set(l_global) | set(l_graph)
        ordered_ids = sorted(all_ids, key=lambda aid: -fused.get(aid, 0.0))
    quota_ids = _select_graph_quota(l_graph, graph_counts, graph_pool, set(chunk_by_id))

    lex_exempt = {aid for aid in l_lex if lex_rank[aid] <= 3}
    found_via = {aid: ("paper lexical" if aid in lex_exempt else "paper")
                for aid in ordered_ids}
    fusion_ranks = {aid: {"dense": dense_rank.get(aid), "lex": lex_rank.get(aid),
                          "global": global_rank.get(aid),
                          **({"chunk": chunk_rank.get(aid)} if rank_channels else {})}
                    for aid in ordered_ids}
    if HIER_GRAPH:
        graph_rank = {aid: i + 1 for i, aid in enumerate(l_graph)}
        for aid in ordered_ids:
            fusion_ranks[aid]["graph"] = graph_rank.get(aid)
            if (aid in graph_rank and found_via[aid] == "paper"
                    and aid not in dense_rank and aid not in global_rank):
                found_via[aid] = "paper graph"
    graph_quota = {aid: f"cited by {n} of the top {graph_pool}" for aid, n in quota_ids.items()}

    hits = [chunk_by_id[aid] for aid in ordered_ids if aid in chunk_by_id]
    extra = {
        "paper_scores": paper_scores,
        "paper_lexical_used": bool(lexical_ids),
        "paper_lexical_skipped": paper_lexical_skipped,
        "global_channel": global_channel,
        "global_extra": [],
        "fusion_ranks": fusion_ranks,
        "fusion_scores": {aid: round(score, 6) for aid, score in fused.items()},
        "lex_exempt": lex_exempt,
        "found_via": found_via,
        "graph_quota": graph_quota,
        "quota_chunks": {aid: chunk_by_id[aid] for aid in graph_quota},
        "graph_channel": "skipped" if "graph" in c_partial else None,
        "stage_b_trace": stage_b_trace,
    }
    return hits, [], c_partial, extra


def qdrant_search_layers(vector, base_filter, limit, timeout=SEARCH_QDRANT_TIMEOUT):
    """Search each topical payload index and RRF the lists (payload included).

    Kept, with payload, for callers that need a single-shot result set (e.g.
    the audit/validate paths going through qdrant_search directly per layer
    would duplicate this). The public search endpoint uses the two-phase
    variant above instead; this one is unchanged.

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


def qdrant_scroll_by_arxiv(arxiv_id: str, limit=1000, page=200, payload_fields=None, timeout=30,
                          deadline=None):
    """All chunks of one paper. `payload_fields` narrows the payload to those
    keys (the outline needs no text); default is the whole payload."""
    qfilter = {"must": [{"key": "arxiv_id", "match": {"value": arxiv_id}}]}
    points = []
    raw_seen = 0
    if payload_fields:
        payload_fields = list(dict.fromkeys(list(payload_fields) + ["arxiv_id", "publication_revision"]))
    offset = None
    scroll_deadline = min(deadline, time.monotonic() + timeout) if deadline is not None else None
    while True:
        request_timeout = timeout
        if scroll_deadline is not None:
            request_timeout = min(timeout, scroll_deadline - time.monotonic())
            if request_timeout <= 0:
                raise TimeoutError("paper retrieval time budget exhausted")
        body = {"filter": qfilter, "limit": min(max(limit - raw_seen, 0), page), "with_vector": False,
                "with_payload": {"include": payload_fields} if payload_fields else True}
        if offset:
            body["offset"] = offset
        try:
            resp = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll", json=body, timeout=request_timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"qdrant error: {e}")
        result = resp.json()["result"]
        raw_points = result["points"]
        raw_seen += len(raw_points)
        points.extend(_filter_publication_points(raw_points))
        offset = result.get("next_page_offset")
        if not offset or raw_seen >= limit:
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
    "acl": "ACL Anthology",
    "iacr": "IACR ePrint",
    "author": "Author / institutional copy",
    "eip": "Ethereum EIP",
    "simd": "Solana SIMD",
    "wp": "Industry whitepaper",
    "oa": "OpenAlex work",
    "pmlr": "Proceedings of Machine Learning Research",
    "hal": "HAL Open Science",
    "gh": "GitHub technical documentation",
    "arxiv": "arXiv",
}


def source_of(paper_id):
    pid = str(paper_id or "")
    prefix = pid.split(":", 1)[0] if ":" in pid else "arxiv"
    return prefix if prefix in SOURCE_LABELS else "arxiv"


@functools.lru_cache(maxsize=20000)
def _source_url_for_id(pid, has_payload):
    """The payload-independent half of source_url, cached by paper id.

    A paper's URL never changes, and _result_from_hit calls source_url twice
    per hit (url + arxiv_url) across up to ~80 hits a search. `has_payload`
    reproduces the one payload-dependent branch below (oa/pmlr/hal/gh fall
    back to a formatted URL only when no payload was supplied at all) without
    needing the whole payload dict in the cache key.
    """
    if pid.startswith("iacr:"):
        return f"https://eprint.iacr.org/{pid.split(':', 1)[1]}"
    if pid.startswith("acl:"):
        return f"https://aclanthology.org/{pid.split(':', 1)[1]}/"
    if pid.startswith("eip:"):
        return f"https://eips.ethereum.org/EIPS/eip-{pid.split(':', 1)[1]}"
    if pid.startswith("simd:"):
        return ("https://github.com/solana-foundation/solana-improvement-documents"
                f"/blob/main/proposals/{pid.split(':', 1)[1]}")
    if pid.startswith("wp:"):
        return None  # curated document with no stored origin yet
    if pid.startswith("oa:"):
        return None if has_payload else f"https://openalex.org/{pid.split(':', 1)[1]}"
    if pid.startswith("pmlr:"):
        return None if has_payload else f"https://proceedings.mlr.press/{pid.split(':', 1)[1]}.html"
    if pid.startswith("hal:"):
        return None if has_payload else f"https://hal.science/hal-{pid.split(':', 1)[1]}"
    if pid.startswith("gh:"):
        return None
    if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.-]*/\d{7})(?:v\d+)?", pid):
        return f"https://arxiv.org/abs/{pid}"
    return None


def source_url(paper_id, payload=None):
    """Canonical public URL for a document, by source."""
    pid = str(paper_id or "")
    if pid.startswith("author:"):
        return stored_source_url(payload)
    # a whitepaper carries its own origin: it was fetched from a project site
    stored = stored_source_url(payload)
    if stored:
        return stored
    curated = curated_source(pid, (payload or {}).get("title"))
    if curated:
        return curated["url"]
    return _source_url_for_id(pid, payload is not None)


# ---------------------------------------------------------------------------
# Licensing
# ---------------------------------------------------------------------------
# We index other people's work, so every fragment we hand out says where it
# came from and under what terms. Two of the sources have a single clear
# licence; for arXiv and the industry whitepapers the terms belong to each
# individual document, and pretending otherwise would be worse than saying so.
LICENSE_INFO = {
    "acl": {
        "spdx": None,
        "terms": "ACL Anthology: copyright and licence are paper-specific; check the linked paper.",
        "redistribution": "quote briefly with attribution; check the paper licence",
    },
    "hal": {
        "spdx": None,
        "terms": "HAL repository: licence is document-specific; check the linked deposit.",
        "redistribution": "quote briefly with attribution; check the deposit licence",
    },
    "gh": {
        "spdx": None,
        "terms": "GitHub documentation: reuse depends on the repository licence.",
        "redistribution": "check the repository licence; public access does not grant reuse rights",
    },
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
        "terms": "IACR ePrint: authors retain copyright; acquisition may include "
                 "an abstract or PDF text. Check the linked paper for reuse terms.",
        "redistribution": "quote briefly with attribution",
    },
    "author": {
        "spdx": None,
        "terms": "Author or institutional PDF copy; licence is unknown unless stated on the linked document.",
        "redistribution": "quote briefly with attribution; verify the document's licence",
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
        "terms": "Work discovered through OpenAlex; acquisition may be abstract-only. Licence is set per work; check the linked source before reuse.",
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


# Copies of one paper from different sources (ACL and arXiv, PMLR and arXiv,
# ...), grouped offline by ops/build_twins.py. Without this a search spent
# several of its slots on one paper. Re-read when the file changes, checked at
# most once a minute; a missing or broken file means no grouping, not an error.
TWINS_PATH = os.getenv("TWINS_PATH", "/opt/dtox-research/twins.json")
_twins_state = {"checked": 0.0, "mtime": None, "canonical": {}, "members": {}}
_twins_lock = threading.Lock()


def _twins():
    now = time.monotonic()
    with _twins_lock:
        if now - _twins_state["checked"] < 60:
            return _twins_state
        _twins_state["checked"] = now
        try:
            mtime = os.stat(TWINS_PATH).st_mtime
        except OSError:
            return _twins_state
        if mtime == _twins_state["mtime"]:
            return _twins_state
        try:
            with open(TWINS_PATH) as f:
                canonical = json.load(f).get("canonical") or {}
        except (OSError, ValueError) as e:
            log.warning(f"twins: could not load {TWINS_PATH}: {e}")
            return _twins_state
        members = {}
        for paper_id, head in canonical.items():
            members.setdefault(head, []).append(paper_id)
        _twins_state.update(mtime=mtime, canonical=canonical, members=members)
        return _twins_state


def canonical_id(paper_id):
    return _twins()["canonical"].get(paper_id, paper_id)


def _collapse_twins(results):
    """One result per paper, first (best ranked) copy wins; the other copies
    are named under "twins" so a caller can still reach them."""
    state = _twins()
    out, seen = [], set()
    for r in results:
        head = state["canonical"].get(r["arxiv_id"], r["arxiv_id"])
        if head in seen:
            continue
        seen.add(head)
        others = [m for m in state["members"].get(head, []) if m != r["arxiv_id"]]
        if others:
            r["twins"] = others
        out.append(r)
    return out


def _hit_is_boilerplate(hit):
    p = hit.get("payload") or {}
    return is_boilerplate_chunk(p.get("text"), p.get("section_title"),
                                p.get("section_type"), p.get("element_type"))


def _dedupe_preferring_content(candidate_hits, floor, lex_exempt_ids, limit):
    """One hit per paper, like the plain dedupe, but a boilerplate chunk never
    represents a paper that has a content chunk among the candidates: the best
    ranked content chunk takes the paper's slot. A paper with nothing but
    boilerplate is kept, BOILERPLATE_DEMOTE places lower."""
    slots = {}  # canonical id -> [first hit, first content hit or None]
    for h in candidate_hits:
        aid = h["payload"].get("arxiv_id")
        if (h.get("score") or 0) < floor and aid not in lex_exempt_ids:
            continue
        slot = slots.setdefault(canonical_id(aid), [h, None])
        if slot[1] is None and not _hit_is_boilerplate(h):
            slot[1] = h
    ranked = [(i + (0 if s[1] is not None else BOILERPLATE_DEMOTE + 0.5),
               s[1] if s[1] is not None else s[0])
              for i, s in enumerate(slots.values())]
    ranked.sort(key=lambda item: item[0])
    return [_result_from_hit(h) for _, h in ranked[:limit]]


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
        "fulltext": None,
        "evidence": provenance(paper_id, payload, scope="indexed_chunk"),
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
    deadline = time.monotonic() + LEXICAL_QUERY_TIMEOUT
    fts_sql = (f"SELECT text, arxiv_id, section_type, element_type, layers, year "
              f"FROM chunks WHERE chunks MATCH ? AND arxiv_id IN ({marks}) "
              f"ORDER BY bm25(chunks) LIMIT ?")
    fts_params = [match, *ids, limit * 3]
    try:
        fts = _ro_conn(FTS_DB_PATH)
        fts.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        rows = fts.execute(fts_sql, fts_params).fetchall()
    except sqlite3.OperationalError:
        _ro_conn_drop(FTS_DB_PATH)
        try:
            fts = _ro_conn(FTS_DB_PATH)
            fts.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            rows = fts.execute(fts_sql, fts_params).fetchall()
        except sqlite3.Error:
            return []
    except sqlite3.Error:
        return []
    best = {}
    for row in rows:
        best.setdefault(row[1], row)
    meta_sql = (f"SELECT arxiv_id,title,citation_count,venue,niche_score,matched_terms,source_url "
               f"FROM papers WHERE status='done' AND arxiv_id IN ({marks})")
    try:
        state = _ro_conn(STATE_DB_PATH)
        meta = {row[0]: row[1:] for row in state.execute(meta_sql, ids).fetchall()}
    except sqlite3.OperationalError:
        _ro_conn_drop(STATE_DB_PATH)
        try:
            meta = {row[0]: row[1:] for row in _ro_conn(STATE_DB_PATH).execute(meta_sql, ids).fetchall()}
        except sqlite3.Error:
            return []
    except sqlite3.Error:
        return []
    results = []
    for paper_id in ids:
        row = best.get(paper_id)
        if not row or paper_id not in meta:
            continue
        title, citations, venue, niche, matched, stored_url = meta[paper_id]
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
            "fulltext": None,
            "evidence": provenance(paper_id, {"text": row[0], "section_type": row[2],
                                              "element_type": row[3]}, scope="indexed_chunk"),
            "arxiv_url": source_url(paper_id, payload),
            "found_via": "lexical fallback",
            "channels": ["lexical"],
            "niche_score": niche,
        })
    return results


def _scored_results_for_audit(payload, incomplete=None):
    scored = [hit for hit in payload.get("results", [])
              if type(hit.get("score")) in (int, float) and math.isfinite(hit["score"])
              and hit.get("evidence_state") != "not_read" and hit.get("retrieval_role") != "discovery_only"]
    if payload.get("partial"):
        raise HTTPException(
            status_code=503,
            detail="retrieval incomplete; refusing to infer absence from a partial search")
    if payload.get("source_generation"):
        unqualified = [hit for hit in [*payload.get("results", []), *payload.get("source_candidates", [])]
                       if hit.get("evidence_state") == "not_read" or hit.get("retrieval_role") == "discovery_only"
                       or type(hit.get("score")) not in (int, float) or not math.isfinite(hit["score"])]
        summary = payload.get("retrieval_summary", {})
        count = max(summary.get("discovery_only_count", 0), len({hit["arxiv_id"] for hit in unqualified}))
        if count:
            if incomplete is None:
                raise HTTPException(status_code=503,
                                    detail="discovered candidate accounting includes unread originals; audit incomplete")
            unread = {canonical_id(hit["arxiv_id"]) for hit in unqualified
                      if hit.get("evidence_state") == "not_read" or type(hit.get("score")) not in (int, float)
                      or not math.isfinite(hit["score"])}
            incomplete["incomplete_evidence"] = True
            incomplete["unread_count"] = max(incomplete["unread_count"],
                                             summary.get("unread_original_count", 0), len(unread))
            incomplete["unqualified_count"] = max(incomplete["unqualified_count"], count)
            for hit in unqualified:
                incomplete["sources"].setdefault(canonical_id(hit["arxiv_id"]), {
                    "id": hit["arxiv_id"], "title": hit.get("title"),
                    "evidence_state": hit.get("evidence_state", "not_read"),
                    "retrieval_role": "discovery_only", "verification_required": True})
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
    strict: bool = False
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
    # too few results under terms/section_type/element_type/year: re-run with
    # those filters dropped one at a time and say so in "relaxed". None = the
    # AUTO_RELAX default; False gives the strict, exact-filter behaviour.
    auto_relax: Optional[bool] = None
    # agent-facing shape: the license block is identical for every hit of one
    # source and arxiv_url repeats url, so a result list carries them once
    # (licenses, by source label) instead of per hit. Off by default: the
    # harness and other callers rely on the full shape.
    compact: bool = False


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
    payload = _run_search(body, x_api_key)
    # a copy: `payload` may be the cached object, and scope has its own cache
    scoped = dict(payload)
    scope = _query_scope(body.query)
    if scope.get("in_scope") is False:
        scoped.pop("source_candidates", None)
        scoped.pop("source_candidate_count", None)
        scoped.pop("discovery_note", None)
        summary = scoped.get("retrieval_summary")
        if isinstance(summary, dict):
            summary = dict(summary)
            summary["auxiliary_candidate_count"] = 0
            summary["source_candidates_returned_count"] = 0
            scoped["retrieval_summary"] = summary
    elif scope.get("in_scope") is None and scoped.get("source_candidates"):
        scoped["discovery_note"] = (
            (scoped.get("discovery_note", "") + " ").strip()
            + "Query scope is uncertain; verify topical fit before using these leads.")
    return {**scoped, "scope": scope}


def _compact_search(full):
    """Copy of a search payload without the per-hit license and arxiv_url;
    the licenses go once at the root, keyed by source label. Never mutates
    `full`, which may be the cached object."""
    licenses = {}

    candidate_fields = {
        "arxiv_id", "title", "url", "source", "year", "section_type",
        "provenance_missing", "source_url_origin", "source_url_status", "legacy_id_alias",
        "section_title", "element_type", "fulltext", "evidence", "score",
        "paper_score", "found_via", "channels", "fusion_ranks",
        "retrieval_role", "candidate_reason", "relevance_floor",
        "passes_relevance_floor", "verification_required", "candidate_channel",
        "candidate_channel_note",
        "retrieval_score", "evidence_state", "source_representations", "source_generation",
    }

    def compact_rows(rows, candidates=False):
        compacted = []
        for row in rows:
            row = dict(row)
            lic = row.pop("license", None)
            if lic is not None:
                licenses[row.get("source")] = lic
            row.pop("arxiv_url", None)
            if candidates:
                row = {key: value for key, value in row.items()
                       if key in candidate_fields}
                if isinstance(row.get("evidence"), dict):
                    row["evidence"] = dict(row["evidence"])
                    row["evidence"]["returned_chars"] = 0
            compacted.append(row)
        return compacted

    results = compact_rows(full.get("results", []))
    out = dict(full)
    out["results"] = results
    if "source_candidates" in full:
        out["source_candidates"] = compact_rows(
            full.get("source_candidates", []), candidates=True)
    out["licenses"] = licenses
    return out


# ---------------------------------------------------------------------------
# terms: closed vocabulary, normalised matching
# ---------------------------------------------------------------------------
# The "terms" payload field holds the harvest gate's own vocabulary (pipeline/
# niche_filter.py), stored as written there ("chain-of-thought" AND "chain of
# thought" are separate entries). A caller's "EIP-4844" or "Chain of Thoughts"
# never equals a stored string, so an exact match returned zero silently. Terms
# are compared by a key that ignores case, hyphens, spaces and a plural "s",
# and one caller term expands to every stored spelling with that key.
_TERM_VOCAB = None


def _term_key(term):
    words = re.findall(r"[a-z0-9+#]+", str(term).lower())
    words = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
             for w in words]
    return "".join(words)


def _term_vocab():
    global _TERM_VOCAB
    if _TERM_VOCAB is None:
        vocab = {}
        if _NICHE is not None:
            for group in (getattr(_NICHE, "BROAD_TERMS", {}), getattr(_NICHE, "SPECIFIC_TERMS", {})):
                for names in group.values():
                    for name in names:
                        vocab.setdefault(_term_key(name), set()).add(name)
        _TERM_VOCAB = vocab
    return _TERM_VOCAB


def _resolve_terms(raw_terms):
    """(stored spellings to filter on, caller terms not in the vocabulary).
    Without a loadable vocabulary the terms pass through lower-cased."""
    cleaned = [t.strip() for t in (raw_terms or []) if isinstance(t, str) and t.strip()]
    vocab = _term_vocab()
    if not vocab:
        return [t.lower() for t in cleaned], []
    known, unknown = set(), []
    for t in cleaned:
        hit = vocab.get(_term_key(t))
        if hit:
            known |= hit
        elif t not in unknown:
            unknown.append(t)
    return sorted(known), unknown


# ---------------------------------------------------------------------------
# gh: documentation must not crowd out papers
# ---------------------------------------------------------------------------
# Words by which a query names the project behind each gh:<source> manifest key
# (pipeline GITHUB_DOC_SOURCES). Unlisted keys fall back to their key's words.
GH_SOURCE_ALIASES = {
    "eth-consensus": r"consensus[- ]specs?|beacon|casper|gasper|pyspec|consensus layer",
    "eth-execution": r"execution[- ]specs?|execution layer|pyspec",
    "op-specs": r"optimism|op[- ]stack|op[- ]specs?|bedrock|superchain",
    "nitro": r"nitro|arbitrum",
    "wormhole": r"wormhole",
    "hyperlane": r"hyperlane",
    "sp1": r"sp1|succinct",
    "risc0": r"risc0|risc[- ]zero",
    "halo2": r"halo2",
    "anchor": r"anchor",
    "solana-docs": r"solana|spl|sealevel",
    "agave-svm": r"agave|svm|sealevel",
    "jupiter": r"jupiter|jup",
    "meteora": r"meteora|dlmm",
    "orca-whirlpools": r"orca|whirlpools?",
    "marinade": r"marinade|msol",
    "jito-stakenet": r"jito|stakenet",
    "uniswap": r"uniswap",
    "lido": r"lido|steth",
    "compound-comet": r"compound|comet",
    "gmx-synthetics": r"gmx",
}
_GH_GENERIC_WORDS = {"docs", "doc", "specs", "spec", "com", "svm", "monorepo"}


def _gh_source_key(paper_id):
    parts = str(paper_id or "").split(":")
    return parts[1] if len(parts) > 2 and parts[0] == "gh" else None


def _query_names_gh_source(query, key):
    pattern = GH_SOURCE_ALIASES.get(key)
    if pattern is None:
        words = [w for w in re.split(r"[^a-z0-9]+", str(key).lower())
                 if w and w not in _GH_GENERIC_WORDS]
        if not words:
            return True
        pattern = "|".join(re.escape(w) for w in words)
    return re.search(r"(?<![a-z0-9])(?:" + pattern + r")(?![a-z0-9])", query.lower()) is not None


def _cap_gh_docs(results, query, limit):
    """Drop gh: documents beyond GH_DOC_CAP per 10 requested places, keeping
    the ones whose project the query names. Returns (results, dropped count).
    Framework docs are dense, uniform and always "on topic" for an
    implementation question, so unchecked they fill the top ten."""
    if GH_DOC_CAP <= 0 or not any(_gh_source_key(r.get("arxiv_id")) for r in results):
        return results, 0
    cap = GH_DOC_CAP * max(1, -(-limit // 10))
    kept, seen, dropped = [], 0, 0
    for r in results:
        key = _gh_source_key(r.get("arxiv_id"))
        if key and not _query_names_gh_source(query, key):
            seen += 1
            if seen > cap:
                dropped += 1
                continue
        kept.append(r)
    return kept, dropped


# ---------------------------------------------------------------------------
# auto-relaxing hard filters
# ---------------------------------------------------------------------------
_RELAX_FIELDS = {"terms": ("terms",), "section_type": ("section_type",),
                 "element_type": ("element_type",), "year": ("year_from", "year_to")}


def _describe_filters(body, names, terms):
    parts = []
    for n in names:
        if n == "terms":
            parts.append(f"terms={list(body.terms or terms)}")
        elif n == "year":
            parts.append("year=" + "-".join(str(v) if v is not None else "" for v in
                                             (body.year_from, body.year_to)))
        else:
            parts.append(f"{n}={getattr(body, n)!r}")
    return ", ".join(parts)


def _auto_relax(body, payload, results, active, terms, limit, x_api_key):
    """Too few results under hard filters: re-run with the filters dropped
    (first the first one, then all of them; two extra searches at most) and
    fill up with what comes back. Exact matches stay first; extras carry
    matches_filters=False. Mutates `payload` (a fresh dict, never the cache's)."""
    plans = [active[:1]]
    if len(active) > 1:
        plans.append(list(active))
    for dropped in plans[:AUTO_RELAX_MAX_EXTRA]:
        update = {"auto_relax": False, "diagnose": False, "compact": False, "limit": limit}
        for n in dropped:
            update.update({f: None for f in _RELAX_FIELDS[n]})
        try:
            got = _run_search(body.model_copy(update=update), x_api_key,
                              _top=False).get("results", [])
        except HTTPException:
            continue
        have = {canonical_id(r["arxiv_id"]) for r in results}
        extras = [dict(r, matches_filters=False) for r in got
                  if canonical_id(r["arxiv_id"]) not in have]
        if not extras:
            continue
        exact = [dict(r, matches_filters=True) for r in results]
        merged = (exact + extras)[:limit]
        what = _describe_filters(body, dropped, terms)
        n_exact = len(exact)
        which = "that filter" if len(dropped) == 1 else "those filters"
        note = (f"No result matched {what}; showing results without {which}"
                if not n_exact else
                f"Only {n_exact} result{'s' if n_exact != 1 else ''} matched {what}; the rest "
                f"(matches_filters=false) come from a search without {which}")
        payload["results"] = merged
        payload["count"] = len(merged)
        payload["relaxed"] = {"dropped": list(dropped), "exact_matches": n_exact, "note": note}
        return


# ---------------- exact names, soft layers ----------------

EXACT_MATCH = os.getenv("EXACT_MATCH", "1") == "1"
SOFT_LAYERS = os.getenv("SOFT_LAYERS", "1") == "1"
EXACT_MAX = 3
_TITLE_INDEX = {"built": 0.0, "index": None}
_TITLE_INDEX_LOCK = threading.Lock()
TITLE_INDEX_TTL = 3600


def _build_title_index():
    try:
        rows = _ro_conn(STATE_DB_PATH).execute(
            "SELECT arxiv_id, title, citation_count FROM papers WHERE status='done'").fetchall()
        built = (TitleIndex(rows, checkpoint=_yield_title_build)
                 if TITLE_BUILD_PRIORITY else TitleIndex(rows))
    except Exception:
        _ro_conn_drop(STATE_DB_PATH)
        built = None
    with _TITLE_INDEX_LOCK:
        if built is not None:
            _TITLE_INDEX["index"] = built
            _TITLE_INDEX["built"] = time.time()
        _TITLE_INDEX["loading"] = False


def _title_index():
    """Title phrases of every finished paper, rebuilt hourly in the background.

    Building takes seconds, so a request never waits for it: until the first
    build finishes (started at startup) title matching is simply skipped."""
    with _TITLE_INDEX_LOCK:
        stale = _TITLE_INDEX["index"] is None or time.time() - _TITLE_INDEX["built"] >= TITLE_INDEX_TTL
        if stale and not _TITLE_INDEX.get("loading"):
            _TITLE_INDEX["loading"] = True
            threading.Thread(target=_build_title_index, daemon=True).start()
        return _TITLE_INDEX["index"]


@app.on_event("startup")
def _warm_title_index():
    if EXACT_MATCH:
        _title_index()


def _exact_matches(query):
    """[(paper_id, "exact id" | "title match")] for documents the query names, verified in the corpus."""
    if not EXACT_MATCH:
        return []
    named = [(d, "exact id") for d in extract_ids(query)]
    index = _title_index()
    if index is not None:
        named += index.match(query)
    if not named:
        return []
    rows = _paper_rows([d for d, _ in named])
    out, seen = [], set()
    for doc_id, via in named:
        head = canonical_id(doc_id)
        if doc_id not in rows and head in rows:
            doc_id = head
        if doc_id not in rows or head in seen:
            continue
        seen.add(head)
        out.append((doc_id, via))
    return out[:EXACT_MAX]


def _audit_named_source_ids(claim, phrasings, idea, allow_idea_fallback=False):
    """Explicit document IDs belong to their claim, not to sibling claims."""
    ids = []
    for query in [claim] + list(phrasings):
        ids.extend(extract_ids(query))
    if not ids and allow_idea_fallback:
        idea_ids = extract_ids(idea)
        if len({canonical_id(paper_id) for paper_id in idea_ids}) == 1:
            ids = idea_ids
    out, seen = [], set()
    for paper_id in ids:
        head = canonical_id(paper_id)
        if head in seen:
            continue
        seen.add(head)
        out.append(paper_id)
    return out[:EXACT_MAX]


TITLE_MATCH_SLOT = 2  # a title match may be a coincidence of wording: third place at best


def _place_named(named_items, rest):
    """Identifier matches lead; title matches take at most the third place, or
    keep the better place they already held in `rest`."""
    ids = [r for r in named_items if r.get("found_via") == "exact id"]
    titles = [r for r in named_items if r.get("found_via") != "exact id"]
    heads = {canonical_id(r["arxiv_id"]) for r in named_items}
    others = [r for r in rest if canonical_id(r["arxiv_id"]) not in heads]
    out = ids + others
    for item in titles:
        old = next((i for i, r in enumerate(rest) if canonical_id(r["arxiv_id"]) == canonical_id(item["arxiv_id"])), None)
        slot = min(TITLE_MATCH_SLOT, len(out))
        if old is not None and old < slot:
            slot = old
        out.insert(slot, item)
    return out


def _inject_exact(results, query, constraints, limit, timeout, _deadline=None):
    """Put the documents the query names at the top, whatever else was found.

    Identifiers and titles are not subject queries: EIP-7702 came back outside
    the first ten, and a long HotStuff query lost the paper whose name it
    carried. The filters other than layer still apply, so a named paper the
    caller excluded by year or section stays excluded."""
    named = _exact_matches(query)
    if not named:
        return results
    by_head = {canonical_id(r["arxiv_id"]): r for r in results}
    missing = [d for d, _ in named if canonical_id(d) not in by_head]
    fresh = {}
    if missing:
        budget = {"_deadline": _deadline} if _deadline is not None else {}
        for item in _score_specific_papers(query, missing, None, timeout=timeout,
                                           constraints=constraints, **budget):
            fresh[item["arxiv_id"]] = item
    front = []
    for doc_id, via in named:
        item = by_head.get(canonical_id(doc_id)) or fresh.get(doc_id)
        if item is None:
            continue
        item["found_via"] = via
        channels = item.setdefault("channels", [])
        if "exact" not in channels:
            channels.append("exact")
        if item not in front:
            front.append(item)
    if not front:
        return results
    new_ids = [r["arxiv_id"] for r in front if "niche_score" not in r]
    if new_ids:
        facts = _paper_facts(new_ids)
        for r in front:
            if "niche_score" not in r:
                r["niche_score"] = facts.get(r["arxiv_id"])
                r["rank_score"] = round(_rank_score(r), 4)
    return _collapse_twins(_place_named(front, results))[:limit]


# The agent's own cut-offs are read against this index's calibrated bands: an
# on-topic web3 hit scores 0.72-0.77 and an on-topic LLM hit 0.82-0.92, so a
# min_score of 0.8 or 0.9 silently empties whole layers. A caller may raise the
# calibrated floor by this much and no further; the answer says when it did not
# get exactly what it asked for.
MIN_SCORE_MAX_RAISE = 0.03

# Canon by layer: MemGPT, Generative Agents, Voyager, AutoGen, SayCan and RAG are
# tagged llm-slm only, so layer=ai-agents dropped them. For these two layers the
# other one is searched too and the lists are fused; web3 and builder-tech stay
# hard filters, where excluding the rest is what keeps noise out.
SOFT_LAYER_PARTNER = {"ai-agents": "llm-slm", "llm-slm": "ai-agents"}
_AGENT_CONTEXT = re.compile(
    r"\b(agents?|agentic|multi-?agent|autonomous|tool[\s-]?use|tool[\s-]?calling|"
    r"function[\s-]?calling|planner|planning|embodied)\b", re.I)


def _soft_partner(layer, query):
    partner = SOFT_LAYER_PARTNER.get(layer)
    if not SOFT_LAYERS or partner is None:
        return None
    if layer == "llm-slm" and not _AGENT_CONTEXT.search(query or ""):
        return None
    return partner


def _merge_soft_layers(primary, partner_payload, limit):
    """Rank-fuse the requested layer with its partner, one row per paper."""
    lists = [[r["arxiv_id"] for r in p.get("results", [])] for p in (primary, partner_payload)]
    fused = reciprocal_rank_fusion(*lists)
    pool = {}
    for r in [*partner_payload.get("results", []), *primary.get("results", [])]:
        pool[r["arxiv_id"]] = r
    order = sorted(pool, key=lambda a: (-fused.get(a, 0), lists[0].index(a) if a in lists[0] else 99))
    return _collapse_twins([pool[a] for a in order])[:limit]


def _candidate_matches_active_filters(row, body, terms):
    if body.layer is not None:
        layers = row.get("layers")
        if not isinstance(layers, (list, tuple, set)) or body.layer not in layers:
            return False
    if body.year_from is not None or body.year_to is not None:
        year = row.get("year")
        if isinstance(year, bool) or not isinstance(year, int):
            return False
        if body.year_from is not None and year < body.year_from:
            return False
        if body.year_to is not None and year > body.year_to:
            return False
    if body.section_type is not None and row.get("section_type") != body.section_type:
        return False
    if body.element_type is not None and row.get("element_type") != body.element_type:
        return False
    if terms:
        row_terms = row.get("terms")
        if not isinstance(row_terms, (list, tuple, set)):
            return False
        available = {str(term).casefold() for term in row_terms}
        if not any(term.casefold() in available for term in terms):
            return False
    return True


def _empty_primary_source_candidates(rows, limit, floor, body, terms):
    """Return a small, explicitly unverified lead list from the observed pool only."""
    eligible = []
    for row in rows:
        score = row.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        try:
            if not math.isfinite(score):
                continue
        except (OverflowError, TypeError, ValueError):
            continue
        if not _candidate_matches_active_filters(row, body, terms):
            continue
        hit = {"score": score, "payload": row}
        if _hit_is_boilerplate(hit):
            continue
        eligible.append(row)
    pool_hits = rows_as_hits(eligible)
    pool_ranks = {row.get("arxiv_id"): row.get("fusion_ranks")
                  for row in eligible if row.get("fusion_ranks")}
    candidates = select_source_candidates(
        pool_hits, [], min(limit, 3), floor, canonical_id, _result_from_hit,
        _hit_is_boilerplate, pool_ranks)
    rows_by_key = {canonical_id(row.get("arxiv_id")): row for row in eligible
                   if row.get("arxiv_id")}
    for candidate in candidates:
        provenance_row = rows_by_key.get(canonical_id(candidate.get("arxiv_id")), {})
        for name in ("found_via", "fusion_ranks", "fusion_score", "paper_score", "channels"):
            if name in provenance_row:
                candidate[name] = copy.deepcopy(provenance_row[name])
        candidate["candidate_reason"] = "related_source_after_empty_primary"
    return candidates


_SOURCE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="source-api")
_SOURCE_SLOTS = threading.BoundedSemaphore(4)
_SOURCE_WORKER = threading.local()


class _SourceBusy(RuntimeError):
    pass


def _source_remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("source retrieval deadline")
    return remaining


def _source_call(deadline, function, *args, **kwargs):
    remaining = _source_remaining(deadline)
    if getattr(_SOURCE_WORKER, "active", False):
        result = function(*args, **kwargs)
        _source_remaining(deadline)
        return result
    if not _SOURCE_SLOTS.acquire(blocking=False):
        raise _SourceBusy("source retrieval capacity exhausted")
    def run():
        _SOURCE_WORKER.active = True
        try:
            _source_remaining(deadline)
            return function(*args, **kwargs)
        finally:
            try:
                for conn in getattr(_ro_conn_local, "conns", {}).values():
                    conn.close()
            finally:
                _ro_conn_local.conns = {}
                _SOURCE_WORKER.active = False
    try:
        future = _SOURCE_POOL.submit(run)
    except BaseException:
        _SOURCE_SLOTS.release()
        raise
    future.add_done_callback(lambda finished: _SOURCE_SLOTS.release())
    try:
        result = future.result(timeout=remaining)
        _source_remaining(deadline)
        return result
    finally:
        future.cancel()


def _source_visible(records, timeout):
    deadline = time.monotonic() + timeout

    def read():
        records_by_id = {row["source_id"]: row for row in records if row.get("status") == "done"}
        current = _paper_rows(list(records_by_id))
        jobs = {}
        ids = list(current)
        conn = _ro_conn(STATE_DB_PATH, timeout=max(0.001, _source_remaining(deadline)))
        for start in range(0, len(ids), 200):
            _source_remaining(deadline)
            batch = ids[start:start + 200]
            for sid, revision, state in conn.execute(
                    "SELECT arxiv_id,revision,state FROM paper_publications WHERE kind='snapshot' "
                    "AND state!='superseded' "
                    f"AND arxiv_id IN ({','.join('?' * len(batch))})", batch):
                jobs.setdefault(sid, []).append((revision, state))
        points = []
        for sid in ids:
            record = records_by_id[sid]
            revision, snapshots = record.get("publication_revision"), jobs.get(sid, [])
            if ((revision is None and not snapshots)
                    or (revision is not None and snapshots == [(revision, "published")])):
                points.append({"id": sid, "payload": {"arxiv_id": sid,
                    **({"publication_revision": revision} if revision is not None else {})}})
        visible = _filter_publication_points(points)
        return {point["id"]: records_by_id[point["id"]].get("publication_revision") for point in visible}

    return _source_call(deadline, read)


def _source_embed(text, timeout):
    if not COARSE_EMBED_URL:
        raise HTTPException(status_code=503, detail="approved source embedding endpoint unavailable")
    response = requests.post(COARSE_EMBED_URL, json={"text": text, "query": True}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _source_health(manifest, timeout):
    if not COARSE_EMBED_URL.endswith("/embed"):
        raise ValueError("approved source embedding endpoint unavailable")
    key = (manifest["generation"], manifest["cache_identity"], COARSE_EMBED_URL,
           json.dumps(manifest["embedding"], sort_keys=True))
    cached = source_health_cache.get(key)
    if cached is not None:
        return copy.deepcopy(cached)
    response = requests.get(COARSE_EMBED_URL[:-6] + "/healthz", timeout=min(timeout, 1.0))
    response.raise_for_status()
    health = response.json()
    if (health.get("status") != "ok" or health.get("model") != manifest["model_id"]
            or health.get("embedding") != manifest["embedding"]):
        raise ValueError("source embedding model/hash/contract mismatch")
    source_health_cache.set(key, copy.deepcopy(health))
    return health


def _source_dense(vector, collection, filters, limit, timeout):
    response = requests.post(
        f"{QDRANT_URL}/collections/{collection}/points/search/groups",
        json={"vector": vector, "filter": filters, "group_by": "source_id",
              "group_size": 1, "limit": limit,
              "with_payload": ["representation_id", "generation"]}, timeout=timeout)
    response.raise_for_status()
    return response.json()["result"]


def _source_chunk_must(filters):
    must = []
    for field in ("layer", "section_type", "element_type"):
        value = filters.get(field)
        if value is not None:
            must.append({"key": "layers" if field == "layer" else field,
                         "match": {"any": [value]} if field == "layer" else {"value": value}})
    if filters.get("terms"):
        must.append({"key": "terms", "match": {"any": filters["terms"]}})
    bounds = {key: filters[field] for field, key in (("year_from", "gte"), ("year_to", "lte"))
              if filters.get(field) is not None}
    if bounds:
        must.append({"key": "year", "range": bounds})
    return must


def _source_owned(query, filters, limit, timeout, get_vector):
    deadline = time.monotonic() + timeout
    vector = get_vector(query, _source_remaining(deadline))
    qfilter = filters if "must" in filters else {"must": _source_chunk_must(filters)}
    hits = grouped_hits(requests.post, QDRANT_URL, COLLECTION, vector, qfilter,
                        limit=limit, timeout=_source_remaining(deadline), group_size=1)
    hits = _source_call(deadline, _filter_publication_points, hits)
    records = [{"source_id": hit["payload"]["arxiv_id"], "status": "done",
                "publication_revision": hit["payload"].get("publication_revision")} for hit in hits]
    live = _source_visible(records, _source_remaining(deadline))
    return [{**hit, "payload": {**hit["payload"], "publication_revision": live[hit["payload"]["arxiv_id"]]}}
            for hit in hits if hit["payload"]["arxiv_id"] in live]


def discover_sources(manifest_path, query, *, hybrid=True, limit=50, **kwargs):
    try:
        from . import source_generation
    except ImportError:
        import source_generation
    discover = source_generation.discover_sources
    if not isinstance(query, str) or len(query) > 65536:
        raise ValueError("invalid query")
    deadline = time.monotonic() + kwargs["timeout"]
    try:
        manifest = source_generation.load_manifest(manifest_path)
    except (OSError, ValueError, TypeError, KeyError):
        return discover(manifest_path, query, hybrid=hybrid, limit=limit, **kwargs)
    try:
        health = _source_health(manifest, _source_remaining(deadline))
    except (ValueError, HTTPException, requests.RequestException):
        return {"status": "unavailable", "generation": manifest["generation"], "sources": [],
                "partial": True, "embedding": manifest["embedding"],
                "diagnostics": {"cache_identity": manifest["cache_identity"],
                                "reasons": ["query_embedding_contract_unavailable"]}}
    embed = kwargs["embed_query"]
    embedding_mismatch = threading.Event()
    def verified_embed(text, timeout):
        key = (manifest["generation"], manifest["cache_identity"], COARSE_EMBED_URL, text)
        vector = source_embedding_cache.get(key)
        if vector is None:
            response = embed(text, min(timeout, _source_remaining(deadline)))
            if not isinstance(response, dict) or response.get("embedding") != manifest["embedding"]:
                embedding_mismatch.set()
                raise ValueError("source query vector embedding identity mismatch")
            vector = response.get("vector")
            if (not isinstance(vector, (list, tuple)) or len(vector) != manifest["dimension"]
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
                raise ValueError("invalid source query vector")
            source_embedding_cache.set(key, vector)
        return vector
    kwargs["embed_query"] = verified_embed
    kwargs["runtime_descriptor"] = lambda timeout: health
    kwargs["timeout"] = _source_remaining(deadline)
    report = discover(manifest_path, query, hybrid=hybrid, limit=limit, **kwargs)
    if embedding_mismatch.is_set():
        report.update(status="unavailable", partial=True, sources=[])
        report["diagnostics"]["reasons"].append("query_vector_embedding_contract_unavailable")
    report["embedding"] = manifest["embedding"]
    return report


def _source_exact_metadata(ids, generation, identity, deadline):
    try:
        from . import source_generation, source_representations
    except ImportError:
        import source_generation
        import source_representations
    manifest = source_generation.load_manifest(SOURCE_GENERATION_MANIFEST)
    if (manifest["generation"], manifest["cache_identity"]) != (generation, identity):
        raise ValueError("source generation changed during exact lookup")
    with source_representations.CatalogReader(manifest["catalog_path"], generation, deadline=deadline,
                                              canonical_id=canonical_id) as reader:
        return {canonical_id(pid): reader.source(canonical_id(pid)) for pid in ids}


def _run_source_search(body, query, terms, unknown_terms, x_api_key, dense_timeout,
                       embed_wait_timeout, embed_http_timeout, top, defer_hydration, deadline):
    payload = {"results": [], "count": 0, "usage": USAGE_NOTICE}
    reports, sources, reasons = [], {}, []
    partner = _soft_partner(body.layer, query) if top and not body.strict else None
    layers = [body.layer] + ([partner] if partner else [])
    small_vectors = {}
    def get_vector(text, timeout):
        if text not in small_vectors:
            remaining = min(timeout, _source_remaining(deadline))
            small_vectors[text] = embed_query(text, wait_timeout=min(embed_wait_timeout, remaining / 2),
                                             request_timeout=min(embed_http_timeout, remaining / 2))
        return small_vectors[text]
    def query_owned(text, filters, limit, timeout):
        return _source_owned(text, filters, limit, min(timeout, _source_remaining(deadline)), get_vector)
    try:
        for layer in layers:
            report = _source_call(
                deadline, discover_sources, SOURCE_GENERATION_MANIFEST, query,
                embed_query=_source_embed, query_dense=_source_dense,
                timeout=_source_remaining(deadline), layer=layer, year_from=body.year_from,
                year_to=body.year_to, section_type=body.section_type,
                element_type=body.element_type, terms=terms, canonical_id=canonical_id,
                visible_sources=_source_visible, query_owned=query_owned, hybrid=body.hybrid, limit=50)
            reports.append(report)
            reasons.extend(report.get("diagnostics", {}).get("reasons", []))
            if report.get("status") != "ready" or not report.get("generation"):
                reasons.append("generation_not_ready")
                break
            for row in report.get("sources", []):
                source_id = canonical_id(row["source_id"])
                if source_id not in sources:
                    sources[source_id] = copy.deepcopy(row)
                    continue
                slot = sources[source_id]
                for channel in ("dense", "lexical"):
                    if channel in row["channel_ranks"]:
                        slot["channel_ranks"][channel] = min(
                            slot["channel_ranks"].get(channel, row["channel_ranks"][channel]), row["channel_ranks"][channel])
                representations = {rep["representation_id"]: rep for rep in slot["representations"]}
                for rep in row["representations"]:
                    representations.setdefault(rep["representation_id"], copy.deepcopy(rep))
                slot["representations"] = list(representations.values())
        identities = {(report.get("generation"), report.get("diagnostics", {}).get("cache_identity"))
                      for report in reports}
        if len(identities) > 1:
            sources.clear()
            reasons.append("generation_changed_between_passes")
        if any(report.get("status") != "ready" for report in reports):
            sources.clear()
        generation, identity = next(iter(identities), (None, None))
        diagnostics = [{**copy.deepcopy(report.get("diagnostics", {})), "layer": layer}
                       for layer, report in zip(layers, reports)]
        payload["source_generation"] = {
            "generation": generation, "cache_identity": identity,
            "metadata_authority": "frozen_source_catalog",
            "embedding": reports[0].get("embedding") if reports else None,
            "status": next((r.get("status", "unavailable") for r in reports if r.get("status") != "ready"),
                           "ready" if reports and len(identities) == 1 else "invalid"),
            "diagnostics": {
                "reasons": reasons,
                "limits": sorted({limit for item in diagnostics for limit in item.get("limits", [])}),
                "retrieval_bounded": bool(diagnostics) and all(item.get("retrieval_bounded", False)
                                                               for item in diagnostics),
                "exhaustive": bool(diagnostics) and all(item.get("exhaustive", False) for item in diagnostics),
                "details_truncated": sum(item.get("details_truncated", 0) for item in diagnostics),
                "native_outside_generation": sum(item.get("native_outside_generation", 0) for item in diagnostics),
                "counting": "sum_per_layer_not_unique",
                "by_layer": diagnostics}}
        if len(reports) > 1 and sources:
            for row in sources.values():
                row["rrf_score"] = sum(1.0 / (RRF_K + rank) for channel, rank in row["channel_ranks"].items()
                                       if channel in ("dense", "lexical"))
                def priority(rep):
                    qualifying = (rep["kind"] != "citation_context"
                        and (body.section_type is None or rep.get("section_type") == body.section_type)
                        and (body.element_type is None or rep.get("element_type") == body.element_type)
                        and (not terms or set(terms).intersection(rep.get("terms", []))))
                    return (not qualifying, rep["kind"] == "citation_context", rep["representation_id"])
                merged, origins = [], set()
                for rep in sorted(row["representations"], key=priority):
                    foreign = rep["kind"] == "citation_context"
                    owner = canonical_id(rep["origin_source_id"])
                    if len(merged) >= _SOURCE_MAX_CONTEXTS or (foreign and owner not in origins
                                                             and len(origins) >= _SOURCE_MAX_ORIGINS):
                        continue
                    merged.append(rep)
                    if foreign:
                        origins.add(owner)
                row["representations"] = merged
            sources = dict(sorted(sources.items(), key=lambda item: (-item[1]["rrf_score"], item[0])))
            payload["layer_expanded"] = {"requested": body.layer, "also_searched": partner,
                                         "note": "Best ranks across layers are fused once per dense/lexical channel."}
        partial = bool(reasons) or any(report.get("partial") for report in reports)
        fingerprint = tuple((sid, row.get("publication_revision"), row.get("rrf_score"),
                             tuple(sorted(row.get("channel_ranks", {}).items())),
                             tuple((rep.get("representation_id"), rep.get("origin_publication_revision"))
                                   for rep in row.get("representations", [])))
                            for sid, row in sources.items())
        cache_key = ("source_generation", generation, identity, fingerprint,
                     body.model_dump_json(), top, defer_hydration)
        cached = search_cache.get(cache_key) if not partial and identity else None
        if cached is not None:
            cached = copy.deepcopy(cached)
            cached["source_generation"] = payload["source_generation"]
            return cached
        must = _source_chunk_must({"layer": body.layer, "section_type": body.section_type,
                                  "element_type": body.element_type, "terms": terms,
                                  "year_from": body.year_from, "year_to": body.year_to})
        if partner:
            for condition in must:
                if condition["key"] == "layers":
                    condition["match"]["any"].append(partner)
        own = []
        if sources:
            try:
                remaining = _source_remaining(deadline)
                vector = _source_call(deadline, get_vector, query, remaining)
                if body.dedupe:
                    own = _source_call(deadline, _score_specific_papers, query, list(sources),
                                       vector=vector, timeout=_source_remaining(deadline),
                                       constraints=must, _deadline=deadline)
                else:
                    filt = {"must": [*must, {"key": "arxiv_id", "match": {"any": list(sources)}}]}
                    hits = _source_call(deadline, grouped_hits, requests.post, QDRANT_URL, COLLECTION,
                                        vector, filt, limit=len(sources), timeout=_source_remaining(deadline),
                                        group_size=8)
                    hits = _source_call(deadline, _filter_publication_points, hits)
                    hits = _source_call(deadline, _hydrate_hits, hits, _source_remaining(deadline))
                    own = [dict(_result_from_hit(hit),
                                publication_revision=(hit.get("payload") or {}).get("publication_revision"))
                           for hit in hits]
            except (HTTPException, requests.RequestException, TimeoutError):
                reasons.append("original_evidence_unavailable")
                partial = True
        by_source = {}
        for row in own:
            source_id = canonical_id(row.get("arxiv_id"))
            target = sources.get(source_id)
            if (target is None or not row.get("text") or _hit_is_boilerplate({"payload": row})
                    or target.get("publication_revision") != row.get("publication_revision")):
                continue
            if body.section_type is not None and row.get("section_type") != body.section_type:
                continue
            if body.element_type is not None and row.get("element_type") != body.element_type:
                continue
            if terms and not set(map(str.casefold, terms)).intersection(
                    str(term).casefold() for term in row.get("terms", [])):
                continue
            by_source.setdefault(source_id, []).append(row)
        results = []
        for source_id, source in sources.items():
            fragments = by_source.get(source_id) or [{"arxiv_id": source_id, "text": "", "score": None}]
            for fragment in fragments:
                row = {**fragment, "title": source.get("title"), "year": source.get("year"),
                       "layers": source.get("layers"), "url": source.get("source_url"),
                       "source_url": source.get("source_url"), "metadata_authority": "frozen_source_catalog"}
                readable = (bool(row.get("text")) and type(row.get("score")) in (int, float)
                            and math.isfinite(row["score"]))
                row.update(retrieval_score=source.get("rrf_score"),
                           source_generation=generation,
                           source_representations=copy.deepcopy(source.get("representations", [])),
                           channels=list(source.get("channel_ranks", {})),
                           evidence_state="original_passage_retrieved" if readable else "not_read",
                           retrieval_role="source" if readable else "discovery_only",
                           verification_required=True)
                if not readable:
                    row.update(text="", score=None, fulltext=None)
                results.append(row)
        if not body.dedupe:
            buckets = {}
            for row in results:
                buckets.setdefault(canonical_id(row["arxiv_id"]), []).append(row)
            results = [rows[index] for index in range(8) for rows in buckets.values() if index < len(rows)]
        if (top and reports and all(r.get("status") == "ready" for r in reports)
                and len(identities) == 1 and time.monotonic() < deadline):
            constraints = must if body.strict else [item for item in must if item["key"] != "layers"]
            results = _source_call(deadline, _inject_exact, results, query, constraints,
                                   max(50, len(results)), _source_remaining(deadline), _deadline=deadline)
            fresh = [{"source_id": row["arxiv_id"], "status": "done",
                      "publication_revision": row.get("publication_revision")} for row in results
                     if canonical_id(row["arxiv_id"]) not in sources]
            if fresh:
                live = _source_visible(fresh, _source_remaining(deadline))
                results = [row for row in results if canonical_id(row["arxiv_id"]) in sources
                           or row["arxiv_id"] in live]
            exact_metadata = (_source_call(deadline, _source_exact_metadata,
                              [row["arxiv_id"] for row in results if canonical_id(row["arxiv_id"]) not in sources],
                              generation, identity, deadline) if fresh else {})
            qualified_results = []
            for row in results:
                metadata = sources.get(canonical_id(row["arxiv_id"])) or exact_metadata.get(canonical_id(row["arxiv_id"]))
                if metadata is None:
                    reasons.append("exact_source_outside_generation")
                    partial = True
                    continue
                year = metadata.get("year")
                if ((body.strict and body.layer and body.layer not in metadata["layers"])
                        or (body.year_from is not None and (year is None or year < body.year_from))
                        or (body.year_to is not None and (year is None or year > body.year_to))):
                    continue
                row.update(title=metadata.get("title"), year=year, layers=metadata.get("layers"),
                           source_url=metadata.get("source_url"), url=metadata.get("source_url"),
                           metadata_authority="frozen_source_catalog")
                if "evidence_state" not in row:
                    readable = (bool(row.get("text")) and type(row.get("score")) in (int, float)
                                and math.isfinite(row["score"]))
                    row.update(retrieval_score=None, source_generation=generation,
                               evidence_state="original_passage_retrieved" if readable else "not_read",
                               retrieval_role="source" if readable else "discovery_only",
                               verification_required=True)
                qualified_results.append(row)
            results = qualified_results
        scored_sources = {canonical_id(row["arxiv_id"]) for row in results
                          if row.get("evidence_state") == "original_passage_retrieved"}
        below_score = []
        if body.min_score is not None:
            default_floor = (MIN_RELEVANCE_BY_LAYER.get(body.layer, MIN_RELEVANCE_SCORE) if body.layer else
                             min([MIN_RELEVANCE_SCORE] + list(MIN_RELEVANCE_BY_LAYER.values())))
            floor = body.min_score if body.strict else min(body.min_score, default_floor + MIN_SCORE_MAX_RAISE)
            if body.min_score > floor + 1e-9:
                payload["min_score_note"] = {
                    "requested": body.min_score, "applied": round(floor, 3),
                    "note": "Original chunk cosine threshold capped at the calibrated layer floor + 0.03."}
                for row in results:
                    if type(row.get("score")) in (int, float) and row["score"] < body.min_score:
                        row["below_min_score"] = True
            below_score = [dict(row, retrieval_role="discovery_only",
                                candidate_reason="original_evidence_below_requested_score") for row in results
                           if type(row.get("score")) not in (int, float) or row["score"] < floor]
            results = [row for row in results if type(row.get("score")) in (int, float)
                       and row["score"] >= floor]
            if below_score:
                payload["source_candidates"] = below_score[:body.limit]
                payload["source_candidate_count"] = len(payload["source_candidates"])
        if body.dedupe:
            results = _collapse_twins(results)
        observed = set(sources) | {canonical_id(row["arxiv_id"]) for row in results}
        results = results[:body.limit]
        payload.update(results=results, count=len(results))
        if partial:
            payload.update(partial=True, retrieval_warning="source generation retrieval incomplete")
        active = [name for name, enabled in (
            ("terms", bool(terms)), ("section_type", body.section_type is not None),
            ("element_type", body.element_type is not None),
            ("year", body.year_from is not None or body.year_to is not None)) if enabled]
        relax = not body.strict and (AUTO_RELAX if body.auto_relax is None else body.auto_relax)
        if relax and active and len(results) < AUTO_RELAX_MIN and not partial:
            plans = [active[:1]] + ([active] if len(active) > 1 else [])
            for dropped in plans[:AUTO_RELAX_MAX_EXTRA]:
                update = {"auto_relax": False, "diagnose": False, "compact": False}
                for name in dropped:
                    update.update({field: None for field in _RELAX_FIELDS[name]})
                other = _run_search(body.model_copy(update=update), x_api_key, dense_timeout,
                                    embed_wait_timeout, embed_http_timeout, False, True, _deadline=deadline)
                if other.get("partial") or other.get("source_generation", {}).get("cache_identity") != identity:
                    continue
                extras = [dict(row, matches_filters=False) for row in other["results"]
                          if canonical_id(row["arxiv_id"]) not in observed]
                if extras:
                    results = ([dict(row, matches_filters=True) for row in results] + extras)[:body.limit]
                    payload.update(results=results, count=len(results), relaxed={
                        "dropped": dropped, "exact_matches": len(payload["results"]),
                        "note": "Additional source leads do not match the dropped filters."})
                    observed.update(canonical_id(row["arxiv_id"]) for row in extras)
                    scored_sources.update(canonical_id(row["arxiv_id"]) for row in extras
                                          if row.get("evidence_state") == "original_passage_retrieved")
                    break
        applied = {key: getattr(body, key) for key in ("layer", "section_type", "element_type",
                   "terms", "year_from", "year_to", "min_score") if getattr(body, key) is not None}
        if applied:
            payload["filters_applied"] = applied
        if top:
            lookup = _source_call(deadline, _lookup_note, query, results)
            if lookup:
                payload["title_lookup"] = lookup
        if unknown_terms:
            payload["terms_resolution"] = {"unknown": unknown_terms, "matched": terms,
                                            "note": "Only recognised terms were used."}
        if not results and applied and body.diagnose and not body.strict and not partial:
            probes = []
            for name in applied:
                other = _run_search(body.model_copy(update={name: None, "diagnose": False,
                                    "auto_relax": False, "limit": 3}), x_api_key, dense_timeout,
                                    embed_wait_timeout, embed_http_timeout, False, True, _deadline=deadline)
                if other.get("results") and not other.get("partial"):
                    probes.append({"drop": name, "would_return": other["count"],
                                   "example": (other["results"][0].get("title") or "")[:80]})
            payload["why_empty"] = {"relax_one_of": probes}
        payload["retrieval_summary"] = {
            "scope": "observed_local", "ordering_policy": "canonical source before result cut",
            "observed_candidate_count": len(observed),
            "returned_unique_count": len({canonical_id(row["arxiv_id"]) for row in results}),
            "scored_candidate_count": len(scored_sources),
            "unread_original_count": len(set(sources) - scored_sources),
            "discovery_only_count": len((set(sources) - scored_sources)
                                        | {canonical_id(row["arxiv_id"]) for row in below_score})}
        if not defer_hydration:
            _source_call(deadline, _hydrate_search_payload, payload)
        if (not payload.get("partial") and identity
                and all(canonical_id(row["arxiv_id"]) in sources for row in
                        [*results, *payload.get("source_candidates", [])])):
            search_cache.set(cache_key, copy.deepcopy(payload))
    except Exception as error:
        log.warning("source generation retrieval incomplete: %s", type(error).__name__)
        payload.update(partial=True, retrieval_warning="source generation retrieval incomplete")
        payload.setdefault("source_generation", {"generation": None, "status": "unavailable"})
        payload["source_generation"].setdefault("diagnostics", {}).setdefault("reasons", []).append(
            "source_capacity" if isinstance(error, _SourceBusy) else
            "deadline" if isinstance(error, TimeoutError) else "source_adapter_failed:" + type(error).__name__)
    return payload


def _run_search(body: SearchBody, x_api_key=None,
                dense_timeout=SEARCH_QDRANT_TIMEOUT,
                embed_wait_timeout=SEARCH_EMBED_WAIT_TIMEOUT,
                embed_http_timeout=SEARCH_EMBED_HTTP_TIMEOUT, _top=True,
                _defer_hydration=False, _deadline=None):
    """The search itself, without the rate limiter.

    /v1/validate runs several searches per claim (each phrasing, the corpus
    vocabulary expansion, the limitations lookup) and every one of them used to
    be charged to the caller's 60-per-minute budget. One audit could spend five
    requests, and the regression harness tripped the limit on its own traffic.
    """

    if body.compact:
        # the cache holds the full shape; compacting a copy afterwards keeps
        # both shapes correct without a second cache entry per query
        full = _run_search(body.model_copy(update={"compact": False}), x_api_key,
                           dense_timeout, embed_wait_timeout, embed_http_timeout, _top,
                           _defer_hydration, _deadline)
        return _compact_search(full)

    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    if body.layer is not None and body.layer not in ALLOWED_LAYERS:
        raise HTTPException(status_code=400, detail=f"layer must be one of {sorted(ALLOWED_LAYERS)}")
    limit = min(max(body.limit, 1), 50)

    if SOURCE_GENERATION_MANIFEST:
        _deadline = _deadline if _deadline is not None else (
            time.monotonic() + dense_timeout + embed_wait_timeout + embed_http_timeout)
        try:
            terms, unknown_terms = _source_call(_deadline, _resolve_terms, body.terms)
        except (_SourceBusy, TimeoutError) as error:
            detail = ("source retrieval capacity exhausted" if isinstance(error, _SourceBusy) else
                      "source retrieval deadline during term resolution")
            raise HTTPException(status_code=503, detail=detail) from error
    else:
        terms, unknown_terms = _resolve_terms(body.terms)
    if body.strict and unknown_terms:
        raise HTTPException(status_code=400, detail={"unknown_terms": unknown_terms,
                                                    "note": "strict search cannot drop unknown terms"})
    if SOURCE_GENERATION_MANIFEST:
        deadline = _deadline if _deadline is not None else (
            time.monotonic() + dense_timeout + embed_wait_timeout + embed_http_timeout)
        return _run_source_search(body.model_copy(update={"limit": limit}), query, terms, unknown_terms, x_api_key, dense_timeout,
                                  embed_wait_timeout, embed_http_timeout, _top, _defer_hydration, deadline)
    relax_on = not body.strict and (AUTO_RELAX if body.auto_relax is None else bool(body.auto_relax))
    tm = {"t0": time.monotonic()} if SEARCH_TIMING else None
    cache_key = (query, body.layer, body.section_type, body.element_type,
                 tuple(sorted(terms)), tuple(unknown_terms), body.dedupe, body.min_score,
                 body.year_from, body.year_to, limit, body.hybrid, relax_on, body.strict)
    if SOURCE_DISCOVERY:
        cache_key += ("source_discovery",)
    if not _top:
        cache_key += ("inner",)
    if _defer_hydration:
        cache_key += ("defer_hydration",)
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
    constraints = must if body.strict else [m for m in must if m["key"] != "layers"]

    partner = _soft_partner(body.layer, query) if _top and not body.strict else None
    if partner:
        sibling_body = body.model_copy(update={"layer": partner, "diagnose": False,
                                               "auto_relax": False, "limit": limit})
        with ThreadPoolExecutor(max_workers=1) as side:
            side_future = side.submit(_run_search, sibling_body, x_api_key, dense_timeout,
                                      embed_wait_timeout, embed_http_timeout, False,
                                      _defer_hydration=bool(SOURCE_DISCOVERY))
            payload = dict(_run_search(body, x_api_key, dense_timeout, embed_wait_timeout,
                                       embed_http_timeout, False,
                                       _defer_hydration=bool(SOURCE_DISCOVERY)))
            try:
                other = side_future.result(timeout=dense_timeout + 5)
            except (HTTPException, FuturesTimeoutError):
                other = None
        if other and other.get("results"):
            payload["results"] = _merge_soft_layers(payload, other, limit)
            payload["count"] = len(payload["results"])
            payload["layer_expanded"] = {
                "requested": body.layer, "also_searched": partner,
                "note": (f"{body.layer} is a soft filter: papers tagged {partner} are merged in, "
                         "because canonical work on agents is often tagged llm-slm only")}
            payload.pop("why_empty", None)
        if other is None or other.get("partial"):
            payload["partial"] = True
            payload["retrieval_warning"] = "soft partner retrieval incomplete"
        payload["results"] = _inject_exact(payload["results"], query, constraints, limit, dense_timeout)
        payload["count"] = len(payload["results"])
        if (isinstance(payload.get("retrieval_summary"), dict)
                or isinstance((other or {}).get("retrieval_summary"), dict)):
            final_ids = {canonical_id(row.get("arxiv_id")) for row in payload["results"]
                         if row.get("arxiv_id")}
            payload["retrieval_summary"] = {
                "scope": "observed_local",
                "ordering_policy": "per-pass summaries; no cross-pass candidate-count sum",
                "passes": {
                    "requested": payload.get("retrieval_summary"),
                    "partner": (other or {}).get("retrieval_summary"),
                },
                "returned_unique_count": len(final_ids),
            }
        if (SOURCE_DISCOVERY and body.hybrid and body.dedupe and not body.strict
                and body.min_score is None
                and (isinstance(payload.get("source_candidates"), list)
                     or isinstance((other or {}).get("source_candidates"), list))):
            payload["source_candidates"] = merge_source_candidates(
                payload.get("source_candidates", []),
                (other or {}).get("source_candidates", []), payload["results"], limit,
                canonical_id, reciprocal_rank_fusion)
            payload["source_candidate_count"] = len(payload["source_candidates"])
            payload["discovery_note"] = (
                "Candidates may be weakly relevant or merely related. Read the source and "
                "specific section before using it as evidence; a candidate is never proof "
                "of novelty. Citation-graph ranking is an evidence lead, never corroboration.")
        if not SOURCE_DISCOVERY and (payload["results"] or payload.get("partial")):
            payload.pop("source_candidates", None)
            payload.pop("source_candidate_count", None)
            payload.pop("discovery_note", None)
            summary = payload.get("retrieval_summary")
            if isinstance(summary, dict):
                summary["auxiliary_candidate_count"] = 0
                summary["source_candidates_returned_count"] = 0
        _hydrate_search_payload(payload)
        if payload.get("title_lookup") and payload["count"]:
            named = _lookup_note(query, payload["results"])
            if named is None:
                payload.pop("title_lookup")
        if not payload.get("partial"):
            search_cache.set(cache_key, payload)
        return payload

    # HIER_CHUNK_LEXICAL=0: skip firing the chunk-level BM25 query up front,
    # betting that hierarchical search (already running the paper-level BM25
    # channel) will succeed. Whether that bet paid off is only knowable once
    # _hier_run returns below; if it fell back instead, the chunk channel is
    # fetched then, same as the always-on path just a bit later.
    # Only skip the chunk channel when the paper-level BM25 channel is also
    # on -- that's the channel this optimization assumes is covering for it.
    # Without PAPER_LEXICAL, hier search carries no lexical signal of its own
    # and dropping chunk lexical too would leave exact-term matches uncovered.
    skip_chunk_lexical = HIER_SEARCH and not HIER_CHUNK_LEXICAL and PAPER_LEXICAL
    lexical_executor = (ThreadPoolExecutor(max_workers=2 if PAPER_LEXICAL else 1)
                       if body.hybrid else None)
    lexical_future = (lexical_executor.submit(
        _bm25_candidates, query, body.layer, body.section_type,
        body.element_type, body.year_from, body.year_to)
        if lexical_executor and not skip_chunk_lexical else None)
    paper_lexical_future = (lexical_executor.submit(
        _paper_bm25, query, body.layer, body.year_from, body.year_to)
        if lexical_executor and PAPER_LEXICAL else None)
    vector, hits, leftover_pool, dense_error, dense_partial = None, [], [], None, []
    dense_limit = limit * 4 if body.dedupe else limit
    hier_extra = None
    try:
        vector = embed_query(query, wait_timeout=embed_wait_timeout,
                             request_timeout=embed_http_timeout)
        if tm is not None:
            tm["embed"] = time.monotonic()
        layers = sorted(ALLOWED_LAYERS) if not body.layer else None
        if HIER_SEARCH:
            hits, leftover_pool, dense_partial, hier_extra = _hier_run(
                vector, qfilter, must, body, limit, dense_limit, dense_timeout, layers,
                query=query, paper_lexical_future=paper_lexical_future)
        else:
            hits, _hydrated_slice, leftover_pool, dense_partial = two_phase_dense_search(
                vector, qfilter, dense_limit, limit, dense_timeout, layers=layers)
    except HTTPException as error:
        dense_error = error
    if tm is not None:
        tm["dense"] = time.monotonic()
    # True only when hier search was on AND actually produced its result (not
    # the stage A/B fallback to the old single-tier path).
    hier_used = bool(HIER_SEARCH and hier_extra is not None)
    lexical_skipped = False
    lexical = []
    try:
        if lexical_future is not None:
            lexical = lexical_future.result(timeout=LEXICAL_QUERY_TIMEOUT)
        elif skip_chunk_lexical and not hier_used and body.hybrid:
            # the bet above didn't pay off -- hier fell back to the old path,
            # which needs the chunk-level channel just like before. It was
            # never fired, so fetch it now (rare: only on stage A/B failure).
            lexical = _bm25_candidates(query, body.layer, body.section_type,
                                       body.element_type, body.year_from, body.year_to)
    except FuturesTimeoutError:
        # BM25 missed its own deadline (slow disk under fts.db); the dense
        # results already stand on their own, so answer with those rather
        # than blocking the request on a thread that is already overrunning.
        lexical, lexical_skipped = [], True
    # papers_fts.db is small enough to live in page cache and is meant to
    # survive exactly the case above -- give it its own timeout rather than
    # letting it inherit whatever time _bm25_candidates already burned.
    paper_lexical, paper_lexical_skipped = [], bool(
        (hier_extra or {}).get("paper_lexical_skipped"))
    try:
        paper_lexical = (paper_lexical_future.result(timeout=LEXICAL_QUERY_TIMEOUT)
                         if paper_lexical_future else [])
    except FuturesTimeoutError:
        paper_lexical, paper_lexical_skipped = [], True
    finally:
        if lexical_executor:
            # the FTS thread(s) finish on their own time; cancel_futures only
            # drops futures that hadn't started yet.
            lexical_executor.shutdown(wait=False, cancel_futures=True)

    # HIER_CHUNK_LEXICAL=0, hier search actually used: the chunk channel was
    # deliberately never fired above (not merely timed out), so it must not
    # be reported as available even though lexical_skipped is False here.
    chunk_lexical_skipped_intentionally = skip_chunk_lexical and hier_used
    hier_lexical_already_fused = (
        hier_extra is not None and HIER_FUSION == "rrf"
        and chunk_lexical_skipped_intentionally)

    # PAPER_LEXICAL off (default): `lexical` is untouched, exactly the old
    # chunk-only BM25 list -- no behavior change below this point.
    lexical_channel = None
    if PAPER_LEXICAL and paper_lexical_future is not None:
        chunk_available = not lexical_skipped and not chunk_lexical_skipped_intentionally
        if not paper_lexical_skipped:
            fused_lexical = reciprocal_rank_fusion(
                paper_lexical, lexical if chunk_available else [], weights=[1.0, 0.6])
            lexical = sorted(fused_lexical, key=lambda pid: -fused_lexical[pid])
            lexical_skipped = False
            lexical_channel = "paper" if chunk_lexical_skipped_intentionally else (
                "paper+chunk" if chunk_available else "paper")
        else:
            # the article-level index is the safety net this feature exists
            # for; if it also missed its deadline there is nothing exact-term
            # left to offer regardless of what the chunk channel did.
            lexical = []
            lexical_skipped = True
            lexical_channel = "skipped"

    if tm is not None:
        tm["lexical"] = time.monotonic()
    if dense_error and not lexical:
        raise dense_error

    if body.layer:
        default_floor = MIN_RELEVANCE_BY_LAYER.get(body.layer, MIN_RELEVANCE_SCORE)
    else:
        # unfiltered search spans every topic, so use the gentler bar and let
        # ranking sort it out rather than dropping a whole layer's answers
        default_floor = min([MIN_RELEVANCE_SCORE] + list(MIN_RELEVANCE_BY_LAYER.values()))
    floor = default_floor
    min_score_note = None
    if body.min_score is not None:
        floor = body.min_score if body.strict else min(body.min_score, default_floor + MIN_SCORE_MAX_RAISE)
        if body.min_score > floor + 1e-9:
            min_score_note = {
                "requested": body.min_score, "applied": round(floor, 3),
                "note": ("min_score above the calibrated band would silently drop on-topic hits "
                         "(cosine here runs 0.72-0.77 for web3, 0.82-0.92 for LLM text), so it was "
                         "capped at the layer floor + 0.03. Results scoring under the requested "
                         "value carry below_min_score=true; judge by rank, not by min_score.")}

    # HIER_FUSION="rrf" only: an article ranked <=3 by exact title/abstract
    # match is its own sufficient evidence and must clear the floor check
    # below even when its best chunk scores under it. Empty (thus a no-op)
    # for every other path, including HIER_FUSION="mix".
    lex_exempt_ids = set() if body.strict else ((hier_extra or {}).get("lex_exempt") or set())
    # Bound fusion candidates from the initial dense batch; a single leftover
    # hydration may add hits but does not enlarge this cap or issue a new search.
    candidate_limit = (max(limit, min(len(hits), 120))
                       if body.hybrid and lexical else limit)

    def _filter_and_dedupe(candidate_hits):
        picked = []
        seen = set()
        if BOILERPLATE_FILTER and body.dedupe:
            return _dedupe_preferring_content(candidate_hits, floor, lex_exempt_ids,
                                              candidate_limit)
        for h in candidate_hits:
            p = h["payload"]
            aid = p.get("arxiv_id")
            if (h.get("score") or 0) < floor and aid not in lex_exempt_ids:
                continue
            if body.dedupe:
                key = canonical_id(aid)
                if key in seen:
                    continue
                seen.add(key)
            if len(picked) >= candidate_limit:
                break
            picked.append(_result_from_hit(h))
        return picked

    results = _filter_and_dedupe(hits)
    # dedupe (same arxiv_id across chunks/layers) can leave phase 2's top-K
    # short of `limit` even though phase 1 had more candidates ranked below
    # it; hydrate one further slice rather than re-querying Qdrant. Capped at
    # a single extra round so a pathological query can't cascade into many
    # payload fetches.
    if len(results) < limit and dense_error is None and leftover_pool:
        try:
            extra_hits = _hydrate_hits(leftover_pool, dense_timeout)
        except HTTPException:
            extra_hits = []
            dense_partial = list(dense_partial) + ["payload_hydration"]
        if extra_hits:
            results = _filter_and_dedupe(hits + extra_hits)

    hier_global_skipped = False
    if hier_extra:
        # only set (non-None) under HIER_FUSION="rrf"; both .get()s fall back
        # to the old "paper"/no-field behavior everywhere else.
        found_via_map = hier_extra.get("found_via")
        fusion_ranks_map = hier_extra.get("fusion_ranks")
        for r in results:
            r["paper_score"] = hier_extra["paper_scores"].get(r["arxiv_id"])
            r["found_via"] = (found_via_map.get(r["arxiv_id"], "paper")
                              if found_via_map is not None else "paper")
            if fusion_ranks_map is not None:
                r["fusion_ranks"] = fusion_ranks_map.get(r["arxiv_id"])
        hier_global_skipped = hier_extra["global_channel"] == "skipped"
        if hier_extra["global_extra"]:
            known_arxiv = {canonical_id(r["arxiv_id"]) for r in results}
            added = 0
            for h in hier_extra["global_extra"]:
                if added >= HIER_GLOBAL_EXTRA:
                    break
                if (h.get("score") or 0) < floor:
                    continue
                extra_result = _result_from_hit(h)
                aid = canonical_id(extra_result["arxiv_id"])
                if aid in known_arxiv:
                    continue
                results.append(extra_result)
                known_arxiv.add(aid)
                added += 1

    # lexical candidates the vectors missed, scored by the same vector search so
    # the two halves stay comparable, then fused by rank rather than by score
    if hier_lexical_already_fused:
        fusion_scores = hier_extra.get("fusion_scores") or {}
        for r in results:
            base = ("paper" if r.get("found_via") in
                    ("paper", "paper graph", "paper lexical") else "dense")
            ranks = r.get("fusion_ranks") or {}
            r["channels"] = [base, "lexical"] if ranks.get("lex") is not None else [base]
            r["fusion_score"] = fusion_scores.get(r["arxiv_id"], 0)
    elif body.hybrid and lexical:
        known = {r["arxiv_id"] for r in results}
        # label every result, not only the runs that gained a lexical extra:
        # otherwise the channel is invisible exactly when the two agree
        lex_all = set(lexical)
        for r in results:
            base = "paper" if r.get("found_via") in ("paper", "paper graph") else "dense"
            r["channels"] = [base, "lexical"] if r["arxiv_id"] in lex_all else [base]
        # HIER_CHUNK_LEXICAL=0, hier used: `lexical` here is the paper-level
        # BM25 list only, already folded into hier's own ranking -- scoring
        # its "extras" again through _score_specific_papers (the expensive
        # per-request Qdrant payload fetch this whole feature exists to
        # avoid) or falling back to _lexical_fallback_results would just
        # redo work the hier path already accounted for.
        extra_ids = ([] if chunk_lexical_skipped_intentionally else
                    [pid for pid in lexical if pid not in known][:BM25_CANDIDATES])
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
        raw = ([] if (body.strict or terms or chunk_lexical_skipped_intentionally) else
              _lexical_fallback_results(
                  query, [pid for pid in extra_ids if pid not in scored_ids], limit))
        dense_order = [r["arxiv_id"] for r in results]
        fused = _rrf(dense_order, lexical, weights=[1.0, 0.6])
        lex_set = set(lexical)
        for r in results:
            base = "paper" if r.get("found_via") in ("paper", "paper graph") else "dense"
            r["channels"] = ([base, "lexical"] if r["arxiv_id"] in lex_set
                             else [base])
        pool = {r["arxiv_id"]: r for r in results}
        for item in [*scored, *raw]:
            pool.setdefault(item["arxiv_id"], item)
        for item in pool.values():
            item["fusion_score"] = round(fused.get(item["arxiv_id"], 0), 6)
        results = sorted(pool.values(),
                         key=lambda r: -r.get("fusion_score", 0))[:limit]
    elif results:
        for result in results:
            result["channels"] = (["paper"] if result.get("found_via") in ("paper", "paper graph")
                                  else ["dense"])
    for r in results:
        if (r.get("fusion_ranks") or {}).get("graph") is not None and "graph" not in r.get("channels", []):
            r.setdefault("channels", []).append("graph")
    if hier_extra and hier_extra.get("graph_quota"):
        results = _inject_graph_quota(results, hier_extra, floor, lex_exempt_ids, limit)

    if tm is not None:
        tm["fusion"] = time.monotonic()
    if body.dedupe:
        results = _collapse_twins(results)
    results, gh_capped = _cap_gh_docs(results, query, limit)
    facts = _paper_facts([r["arxiv_id"] for r in results])
    for r in results:
        r["niche_score"] = facts.get(r["arxiv_id"])
        r["rank_score"] = round(_rank_score(r), 4)
    # hier_extra means stage A/B actually produced the dense order (paper_score
    # mixed with best-chunk cosine, plus any stage-C tail preserved in its own
    # order) -- re-sorting by chunk-only rank_score here would undo exactly the
    # ranking HIER_SEARCH exists to produce, and would splice the stage-C tail
    # back into the middle of it.
    if not any("fusion_score" in result for result in results) and not hier_extra:
        results.sort(key=lambda r: -r["rank_score"])
    precut_results = copy.deepcopy(results)
    if _top:
        results = _inject_exact(results, query, constraints, limit, dense_timeout)
    if body.strict:
        results = [r for r in results if isinstance(r.get("score"), (int, float))
                   and (r["score"] >= floor or (body.min_score is None
                                                and r.get("found_via") == "exact id"))]
        for result in results:
            if result.get("found_via") == "exact id":
                result["relevance_kind"] = "identifier"
    if hier_lexical_already_fused:
        results = results[:limit]
    if min_score_note:
        for r in results:
            if r.get("score") is not None and r["score"] < body.min_score:
                r["below_min_score"] = True
    payload = {"results": results, "count": len(results), "usage": USAGE_NOTICE}
    if min_score_note:
        payload["min_score_note"] = min_score_note
    if hier_global_skipped:
        payload["global_channel"] = "skipped"
    if (hier_extra or {}).get("graph_channel") == "skipped":
        payload["graph_channel"] = "skipped"
    if lexical_channel is not None:
        payload["lexical_channel"] = lexical_channel
    elif lexical_skipped:
        # dense results are complete on their own -- this is not "partial",
        # only missing the exact-term channel hybrid search adds on top
        payload["lexical_channel"] = "skipped"
    if (PAPER_LEXICAL_SOURCE_BALANCE and body.layer == "web3"
            and (paper_lexical or (hier_extra or {}).get("paper_lexical_used"))):
        payload["lexical_ranking"] = "source_balanced"
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
    if lexical_skipped or paper_lexical_skipped:
        payload["partial"] = True
        payload.setdefault("retrieval_warning", "lexical retrieval incomplete")
    lookup = _lookup_note(query, results)
    if lookup:
        payload["title_lookup"] = lookup
    applied = {k: v for k, v in (("layer", body.layer), ("section_type", body.section_type),
                                 ("element_type", body.element_type), ("terms", body.terms),
                                 ("year_from", body.year_from), ("year_to", body.year_to),
                                 ("min_score", body.min_score)) if v is not None}
    if applied:
        payload["filters_applied"] = applied
    if gh_capped:
        payload["gh_docs_capped"] = gh_capped
    if unknown_terms:
        payload["terms_resolution"] = {
            "unknown": unknown_terms, "matched": terms,
            "note": ("terms is a closed vocabulary of technical terms (see the terms field "
                     "of results); not in it: " + ", ".join(unknown_terms) + ". "
                     + ("Searched with the recognised terms only." if terms else
                        "Searched without the terms filter; put such identifiers in the query text."))}
    tried_relax = False
    if (relax_on and not dense_error and len(results) < AUTO_RELAX_MIN
            and not payload.get("partial")):
        active = [n for n, on in (("terms", bool(terms)), ("section_type", bool(body.section_type)),
                                  ("element_type", bool(body.element_type)),
                                  ("year", body.year_from is not None or body.year_to is not None)) if on]
        if active:
            tried_relax = True
            if tm is not None:
                tm["pre_relax"] = time.monotonic()
            _auto_relax(body, payload, results, active, terms, limit, x_api_key)
            results = payload["results"]
    if tried_relax:
        precut_results = copy.deepcopy(results)
    use_raw_pool = not body.strict and body.min_score is None and hier_used
    rows, retrieval_summary = build_candidate_pool(
        results, precut_results if use_raw_pool else [],
        ([*hits, *(hier_extra or {}).get("quota_chunks", {}).values()]
         if use_raw_pool else []),
        canonical_id, _result_from_hit,
        found_via=(hier_extra or {}).get("found_via"),
        fusion_ranks=(hier_extra or {}).get("fusion_ranks"),
        fusion_scores=(hier_extra or {}).get("fusion_scores"),
        paper_scores=(hier_extra or {}).get("paper_scores"))
    payload["retrieval_summary"] = retrieval_summary
    if (SOURCE_DISCOVERY and hier_used and body.hybrid and body.dedupe
            and not body.strict and body.min_score is None):
        pool_hits = rows_as_hits(rows)
        pool_ranks = {row.get("arxiv_id"): row.get("fusion_ranks")
                      for row in rows if row.get("fusion_ranks")}
        source_candidates = select_source_candidates(
            pool_hits, results, limit, floor, canonical_id, _result_from_hit,
            _hit_is_boilerplate, pool_ranks)
        rows_by_key = {canonical_id(row.get("arxiv_id")): row for row in rows
                       if row.get("arxiv_id")}
        for candidate in source_candidates:
            provenance_row = rows_by_key.get(canonical_id(candidate.get("arxiv_id")), {})
            for name in ("found_via", "fusion_ranks", "fusion_score", "paper_score", "channels"):
                if name in provenance_row:
                    candidate[name] = copy.deepcopy(provenance_row[name])
        payload["source_candidates"] = source_candidates
        payload["source_candidate_count"] = len(source_candidates)
        retrieval_summary["source_candidates_returned_count"] = len(source_candidates)
        payload["discovery_note"] = (
            "Candidates may be weakly relevant or merely related. Read the source and "
            "specific section before using it as evidence; a candidate is never proof "
            "of novelty. Citation-graph ranking is an evidence lead, never corroboration.")
    elif (not results and not payload.get("partial") and hier_used and body.hybrid
          and body.dedupe and not body.strict and body.min_score is None):
        retrieval_summary["auxiliary_candidate_count"] = 0
        source_candidates = _empty_primary_source_candidates(rows, limit, floor, body, terms)
        if source_candidates:
            payload["source_candidates"] = source_candidates
            payload["source_candidate_count"] = len(source_candidates)
            retrieval_summary["source_candidates_returned_count"] = len(source_candidates)
            retrieval_summary["auxiliary_candidate_count"] = len(source_candidates)
            payload["discovery_note"] = (
                "Primary results are empty. These are bounded related-source leads from "
                "already retrieved candidates, not relevance-verified evidence; inspect "
                "the source and section before use. Citation-graph links are leads, not "
                "corroboration.")
    if not results and applied and body.diagnose and not body.strict and not payload.get("partial"):
        # after a relax attempt every relaxable filter is already known not to
        # be the culprit; only layer / min_score are left to blame
        names = [n for n in applied if not (tried_relax and n in
                 ("terms", "section_type", "element_type", "year_from", "year_to"))]
        relaxations = []

        def _probe(name):
            probe = body.model_copy(update={name: None, "diagnose": False, "limit": 3,
                                            "auto_relax": False})
            try:
                got = _run_search(probe, x_api_key).get("results", [])
            except HTTPException:
                return None
            return ({"drop": name, "would_return": len(got), "example": got[0]["title"][:80]}
                    if got else None)

        if names:
            with ThreadPoolExecutor(max_workers=len(names)) as pool:
                relaxations = [r for r in pool.map(_probe, names) if r]
        payload["why_empty"] = (
            {"relax_one_of": relaxations} if relaxations else
            {"relax_one_of": [], "note": "no single filter explains it: the query itself "
                                         "has no match above the relevance floor"})
        if payload.get("source_candidates"):
            payload["why_empty"] = {
                "relax_one_of": payload["why_empty"].get("relax_one_of", []),
                "note": ("The primary results are empty above the relevance floor; "
                         "related source candidates are listed separately and are not "
                         "primary matches.")}
    if tm is not None:
        end = time.monotonic()
        prev, parts = tm["t0"], []
        for m in ("embed", "dense", "lexical", "fusion", "pre_relax"):
            if m in tm:
                parts.append(f"{m}={tm[m] - prev:.2f}")
                prev = tm[m]
        parts.append(f"tail={end - prev:.2f}")
        log.warning("search_timing total=%.2f %s q=%r n=%d", end - tm["t0"], " ".join(parts),
                 query[:60], len(payload["results"]))
    # a hier search that silently fell back to the old path (coarse stage
    # hiccup under load) is a degraded answer: caching it pinned the worse
    # ranking for the whole TTL
    if not _defer_hydration:
        _hydrate_search_payload(payload)
    if not payload.get("partial") and (hier_used or not HIER_SEARCH):
        search_cache.set(cache_key, payload)
    return payload


# An agent asking for "the spec" wants the algorithm block and the equations,
# not the whole methods narrative: returning every method chunk of a survey
# costs ~66k tokens, which defeats the point of the endpoint.
SPEC_ELEMENT_TYPES = ("algorithm", "equation", "code", "table")
SPEC_PRIORITY = {"algorithm": 0, "equation": 1, "code": 2, "prose": 3, "table": 4}
SPEC_MAX_CHARS = 12000
SPEC_SECONDS = 30


def _trusted_alias_ids(paper_id, twins=None, limit=2):
    """Return only ids explicitly grouped with this id by the twins index."""
    twins = _twins() if twins is None else twins
    if isinstance(twins, tuple):
        canonical_map, members_map = twins
        canonical = canonical_map.get(paper_id, paper_id)
        members = members_map.get(canonical, [])
        return [pid for pid in dict.fromkeys([canonical, *members]) if pid != paper_id][:limit]
    canonical = twins.get("canonical", {}).get(paper_id, paper_id)
    members = twins.get("members", {}).get(canonical, [])
    return [pid for pid in dict.fromkeys([canonical, *members]) if pid != paper_id][:limit]


def _payloads_for_id(points, paper_id):
    return [(point.get("payload") or {}) for point in points
            if (point.get("payload") or {}).get("arxiv_id") in (None, paper_id)]


@app.get("/v1/paper/{arxiv_id:path}/spec")
def paper_spec(
    arxiv_id: str,
    target_elements: Optional[str] = None,
    include_prose: bool = False,
    max_chars: int = SPEC_MAX_CHARS,
    x_api_key: Optional[str] = Header(default=None),
    cursor: Optional[str] = None,
):
    """Structured spec of a paper: math, pseudocode and architecture.

    target_elements: comma-separated subset of algorithm,equation,code,table
                     (default: all four). Prose is excluded unless
                     include_prose=true, and output is capped so the caller
                     can budget context.
    """
    auth_and_limit(x_api_key)
    if type(max_chars) is not int or max_chars <= 0:
        raise HTTPException(status_code=400, detail="max_chars must be a positive integer")
    if cursor is not None and (not isinstance(cursor, (str, dict))
                               or isinstance(cursor, str) and (not cursor or len(cursor) > 1024)):
        raise HTTPException(status_code=400, detail="invalid continuation cursor")

    wanted = SPEC_ELEMENT_TYPES
    if target_elements:
        wanted = tuple(t.strip().lower() for t in target_elements.split(",") if t.strip())
        bad = [t for t in wanted if t not in SPEC_ELEMENT_TYPES + ("prose",)]
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown element types: {bad}")

    deadline = time.monotonic() + SPEC_SECONDS
    points = qdrant_scroll_by_arxiv(arxiv_id, limit=1000, page=200, timeout=SPEC_SECONDS,
                                    deadline=deadline)
    chunk_limit = len(points) >= 1000
    requested_payloads = _payloads_for_id(points, arxiv_id)
    requested_types = {(p.get("element_type") or "").lower() for p in requested_payloads if p.get("text")}
    missing = set(wanted) - requested_types
    tried = [arxiv_id]
    twins = None
    sources = [(arxiv_id, requested_payloads)] if requested_payloads else []
    aliases = []
    budget_exhausted = False
    resolution_errors = []
    if not requested_payloads or missing:
        twins = _twins()
        aliases = _trusted_alias_ids(arxiv_id, twins)
    for alias in aliases:
        if requested_payloads and not missing:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            budget_exhausted = True
            break
        tried.append(alias)
        try:
            alias_points = qdrant_scroll_by_arxiv(alias, limit=1000, page=200,
                                                 timeout=remaining, deadline=deadline)
        except Exception as error:
            resolution_errors.append({"source_id": alias, "error": type(error).__name__})
            if time.monotonic() >= deadline:
                budget_exhausted = True
            continue
        chunk_limit = chunk_limit or len(alias_points) >= 1000
        alias_payloads = _payloads_for_id(alias_points, alias)
        if not alias_payloads:
            continue
        accepted = [p for p in alias_payloads if p.get("text") and
                    (p.get("element_type") or "").lower() in missing]
        if accepted:
            sources.append((alias, accepted))
            missing -= {(p.get("element_type") or "").lower() for p in accepted}
    if not requested_payloads and not sources:
        raise HTTPException(status_code=404, detail="paper not found")

    payload_sources = [(source_id, payload) for source_id, group in sources for payload in group]
    payloads = [payload for _, payload in payload_sources]

    def is_spec(p):
        st = (p.get("section_type") or "").lower()
        title = (p.get("section_title") or "").lower()
        return st in SPEC_SECTION_TYPES or any(kw in title for kw in SPEC_TITLE_KEYWORDS)

    actual_ids = list(dict.fromkeys([source_id for source_id, _ in sources]))
    rows = _evidence_rows(actual_ids)
    top_source = arxiv_id if requested_payloads else actual_ids[0]
    top_payload = requested_payloads[0] if requested_payloads else sources[0][1][0]
    passport = _paper_passport(top_source, top_payload, rows)
    elements = [p for p in payloads if (p.get("element_type") or "").lower() in wanted]
    if include_prose:
        elements += [p for p in payloads
                     if (p.get("element_type") or "").lower() == "prose" and is_spec(p)]
    # Implementable objects first, results tables last: DeepSeekMath's benchmark
    # tables precede its method, and in paper order they filled the whole
    # character budget before the GRPO objective was ever reached.
    elements.sort(key=lambda p: (SPEC_PRIORITY.get((p.get("element_type") or "").lower(), 3),
                                 p.get("chunk_index", 0)))
    title = elements[0].get("title") if elements else (payloads[0].get("title") if payloads else None)

    fragments = []
    for p in elements:
        source_id = next(sid for sid, payload in payload_sources if payload is p)
        text = p.get("text") or ""
        evidence = provenance(source_id, p, _passport_source_metadata(source_id, rows,
                              _paper_passport(source_id, p, rows)),
                              source_url(source_id, rows.get(source_id, {})), scope="indexed_chunk")
        evidence["source_paper_id"] = source_id
        fragments.append({
            "element_type": p.get("element_type"),
            "section_type": p.get("section_type"),
            "section_title": p.get("section_title"),
            "text": text,
            "source_paper_id": source_id,
            "evidence": evidence,
        })
    try:
        page = bounded_evidence_page(fragments, max_chars, cursor)
    except ValueError as error:
        raise HTTPException(status_code=getattr(error, "status_code", 400), detail=str(error)) from error

    repos = []
    for p in elements:
        for r in (p.get("repos") or []):
            if r not in repos:
                repos.append(r)

    reasons = resolution_reasons(missing_elements=missing, errors=resolution_errors,
                                 time_budget_exhausted=budget_exhausted, chunk_limit=chunk_limit)
    page["partial_reasons"] = sorted(set(page["partial_reasons"]) | set(reasons))
    return {
        "arxiv_id": arxiv_id,
        **passport,
        "title": title,
        "repos": repos,
        **page,
        "source_resolution": {"requested_id": arxiv_id, "source_ids": actual_ids,
                              "attempted_ids": tried,
                              "partial": bool(missing or budget_exhausted or resolution_errors or chunk_limit),
                              "time_budget_exhausted": budget_exhausted,
                              "errors": resolution_errors,
                              "missing": {element: "not_found_in_retrieved_chunks" for element in sorted(missing)},
                              "missing_scope": "retrieved_indexed_chunks",
                              "source_wide_absence": "unknown",
                              "caveat": "Missing element types are unknown outside the retrieved indexed chunks.",
                              "reasons": reasons,
                              "partial_reasons": reasons},
        **({"note": "no algorithm/equation/code/table elements found in retrieved indexed chunks"
                    " (try include_prose=true for its method prose)"} if not elements else {}),
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
    rows = _evidence_rows([a, b])

    # our taxonomy calls the results section "experiments"; asking for
    # "results" here is what previously made this endpoint return nothing
    wanted_sections = ("experiments", "analysis", "limitations")

    def get_results_chunks(arxiv_id):
        points = qdrant_scroll_by_arxiv(arxiv_id)
        if not points:
            raise HTTPException(status_code=404, detail=f"paper not found: {arxiv_id}")
        payloads = [pt["payload"] for pt in points]
        passport = _paper_passport(arxiv_id, payloads[0], rows)
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
                        "evidence": dict(provenance(arxiv_id, p, _passport_source_metadata(arxiv_id, rows, passport), passport["url"],
                                                    scope="indexed_chunk"), returned_chars=max_chars),
                    })
                    used = max_chars
                break
            used += len(text)
            chunks.append({
                "element_type": p.get("element_type"),
                "section_type": p.get("section_type"),
                "section_title": p.get("section_title"),
                "text": text,
                "evidence": provenance(arxiv_id, p, _passport_source_metadata(arxiv_id, rows, passport),
                                       passport["url"], scope="indexed_chunk"),
            })
        return {
            "arxiv_id": arxiv_id,
            "skipped_tables_without_numbers": empty_tables,
            **passport,
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
STATE_DB_PATH = _state_db_path()
TRENDS_CACHE = TTLCache(ttl_seconds=1800, max_size=64)


TOPIC_PAPER_LIMIT = 600      # enough papers for year-over-year shares to mean anything


def _papers_about(topic, layer=None, year_from=None, year_to=None, details=None):
    """Bounded dense and exact lexical topic candidates, not a corpus census."""
    must = [{"key": "layers", "match": {"any": [layer]}}] if layer else []
    if year_from is not None or year_to is not None:
        bounds = {}
        if year_from is not None:
            bounds["gte"] = year_from
        if year_to is not None:
            bounds["lte"] = year_to
        must.append({"key": "year", "range": bounds})
    candidate_limit = min(TOPIC_PAPER_LIMIT, 200)
    dense_failed = False
    try:
        vector = embed_query(topic)
        hits = qdrant_search(vector, {"must": must} if must else None, candidate_limit,
                             timeout=SEARCH_QDRANT_TIMEOUT)
    except HTTPException:
        hits, dense_failed = [], True
    lexical = _paper_bm25(topic, layer, year_from, year_to, limit=PAPER_BM25_CANDIDATES)
    rows = _paper_rows(lexical) if lexical else {}
    ids, seen, by_layer = [], set(), {}

    def accept(pid, lay, channel):
        if not pid:
            return
        head = canonical_id(pid)
        counts = by_layer.setdefault(lay or "unknown", {"dense": set(), "lexical": set()})
        counts[channel].add(head)
        if head not in seen:
            seen.add(head)
            ids.append(pid)

    for h in hits:
        pay = h.get("payload") or {}
        lay = _layer_of(pay, layer)
        if h.get("score", 0) < _bands_for(lay)[1]:
            continue
        accept(pay.get("arxiv_id"), lay, "dense")
    words = {w for w in re.findall(r"[a-z0-9]+", topic.lower())
             if w not in CLAIM_WORD_STOP}
    for pid in lexical:
        row = rows.get(pid) or {}
        text = f"{row.get('title') or ''} {row.get('abstract') or ''}".lower()
        if words and words <= set(re.findall(r"[a-z0-9]+", text)):
            accept(pid, _layer_of(row, layer), "lexical")
    if details is not None:
        details.update({
            "status": ("insufficient_coverage" if dense_failed else
                       "no_candidates" if not hits and not lexical else
                       "insufficient_coverage" if not ids else "bounded_sample"),
            "dense_chunk_hits": len(hits), "dense_chunk_cap": candidate_limit,
            "lexical_candidates": len(lexical), "lexical_cap": PAPER_BM25_CANDIDATES,
            "canonical_papers": len(ids), "dense_unavailable": dense_failed,
            "is_lower_bound": len(hits) >= candidate_limit or len(lexical) >= PAPER_BM25_CANDIDATES,
            "thresholds_by_layer": {lay: _bands_for(lay)[1]
                                    for lay in ([layer] if layer else sorted(SCORE_BANDS_BY_LAYER))},
            "by_layer": {lay: {"dense": len(counts["dense"]),
                               "lexical": len(counts["lexical"]),
                               "canonical_papers": len(counts["dense"] | counts["lexical"])}
                         for lay, counts in by_layer.items()},
            "note": "Bounded candidate sample, not exhaustive literature coverage. Dense uses existing "
                    "layer adjacent bands; lexical requires every topic word in title/abstract. "
                    "Channel counts overlap; caps limit candidates, not unique papers.",
        })
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

    topic_ids, topic_coverage = None, None
    if about and about.strip():
        topic_coverage = {}
        topic_ids = _papers_about(about.strip(), layer, year_from, year_to, details=topic_coverage)
        # a narrow topic has fewer papers per year, so the default floor of 5
        # would silently empty the answer
        min_papers = min(min_papers, 3)
    per_term, per_year = _load_term_years(layer, year_from, year_to, topic_ids)
    if topic_coverage is not None:
        topic_coverage["papers_with_term_years"] = sum(per_year.values())
    years = sorted(per_year)
    if not years:
        if topic_coverage is not None:
            if topic_ids:
                topic_coverage["status"] = "insufficient_coverage"
            return {"layer": layer, "about": about, "years": [], "trends": [],
                    "papers_in_topic": len(topic_ids), "topic_coverage": topic_coverage,
                    "status": topic_coverage["status"],
                    "note": "The bounded topic sample has no usable term/year data in this window. "
                            "This does not establish absence of papers in the index or literature."}
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
        "topic_coverage": topic_coverage,
        "status": topic_coverage["status"] if topic_coverage is not None else "corpus_counts",
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
# p50 of on-topic hits per layer (table above); builder-tech is not calibrated
# yet and sits at the midpoint rather than at either extreme.
LAYER_SCORE_MEDIAN = {"llm-slm": 0.898, "ai-agents": 0.860, "web3": 0.806}
LAYER_SCORE_MEDIAN_DEFAULT = 0.860
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
    if type(score) not in (int, float) or not math.isfinite(score):
        return "weak"
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

# Optional cross-encoder passage relevance ranking; it does not verify claims.
RERANK_URL = os.getenv("RERANK_URL", "http://127.0.0.1:8006/rerank")
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "0") == "1"
RERANK_UNCERTAINTY = 0.02   # distance from the direct band that counts as grey
RERANK_TOP_N = 8
CROSS_POSITIVE = 0.0
RERANK_TIMEOUT = 3
_rerank_runtime = RerankRuntime(max_pairs=8, timeout=3, cooldown=60,
                                cache_size=128, cache_ttl=300)


def _rerank_request(query, passages, timeout):
    resp = requests.post(RERANK_URL, json={"query": query, "passages": passages},
                         timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("scores")


def _rerank(query, hits):
    """Bounded cross-encoder passage relevance scores, or None on fallback."""
    passages = [(h.get("text") or "")[:1200] for h in hits]
    return _rerank_runtime.score(query, passages, _rerank_request)


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
    if hit.get("evidence_state") == "not_read" or hit.get("retrieval_role") == "discovery_only":
        return None
    if hit.get("cross_top") and (hit.get("cross_score") or 0) > CROSS_POSITIVE:
        return "cross-encoder ranked this passage as relevant; this is not claim verification"
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
    if hit.get("evidence_state") == "not_read" or hit.get("retrieval_role") == "discovery_only":
        return "weak"
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


def _paper_facts(paper_ids):
    """niche_score per paper, straight from the ingest record.

    Cheaper than storing it in every chunk payload and re-upserting 760k points:
    a search returns at most a few dozen papers, and this is one query."""
    ids = [pid for pid in dict.fromkeys(paper_ids) if pid]
    if not ids:
        return {}
    sql = (f"SELECT arxiv_id, niche_score FROM papers "
          f"WHERE arxiv_id IN ({','.join('?' * len(ids))})")
    try:
        rows = _ro_conn(STATE_DB_PATH).execute(sql, ids).fetchall()
    except sqlite3.OperationalError:
        _ro_conn_drop(STATE_DB_PATH)
        try:
            rows = _ro_conn(STATE_DB_PATH).execute(sql, ids).fetchall()
        except sqlite3.Error:
            return {}
    except sqlite3.Error:
        return {}
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
    r"ethereum|defi|(?-i:MEVs?)|maximal extractable|cryptograph\w*|consensus|validator|"
    r"wallet)\b", re.I)
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


# ---------------- query-level scope signal (search / papers / facets) ----------------
#
# /v1/validate refuses out-of-index ideas, but /v1/search, /v1/papers and
# /v1/facets used to answer "CRISPR" or "AlphaFold" with the same confident
# rows as "MEV": the corpus always has a nearest neighbour, and cosine cannot
# say that the neighbour is beside the point. Results are never removed here
# (the agent decides); the response only says whether the query looks like a
# subject the index covers. Cheap rules first, one small vector probe last.

SCOPE_NOTE = ("the index covers web3, AI agents, LLMs and developer tooling; "
              "this query looks outside it. Treat the results below as nearest "
              "neighbours, not as literature on the subject.")

# Foreign domains beyond validate's list. Only the scope signal uses them: the
# audit gate stays as strict as it was, this one is allowed to be broader
# because it does not refuse anything.
SCOPE_FOREIGN_EXTRA = {
    "biology": re.compile(
        r"\b(?:alphafold|protein (?:structure|folding)|cancer|tumou?r\w*|"
        r"immunotherap\w*|genom\w*|dna sequenc\w*|neuroscien\w*)\b", re.I),
    "physics": re.compile(
        r"\b(?:dark matter|dark energy|photons?|gamma[- ]rays?|neutrinos?|quarks?|"
        r"particle physics|astrophysic\w*|cosmolog\w*|galax(?:y|ies)|"
        r"black holes?|gravitational waves?|quantum (?:computing|field))\b", re.I),
    "sports": re.compile(
        r"\b(?:foot?ball|soccer|basketball|tennis|baseball|cricket|olympic\w*)\b", re.I),
    "food": re.compile(
        r"\b(?:sourdough|bread|recipes?|baking|cooking|cuisine|cake)\b", re.I),
}

# Below this mean cosine of the five nearest papers the corpus has nothing near
# the query at all (measured on the coarse index: sourdough 0.52, football
# 0.57, "footbal tactics" 0.62, cake 0.58; real topics sit at 0.66-0.80).
SCOPE_DENSE_FLOOR = float(os.getenv("SCOPE_DENSE_FLOOR", "0.63"))
SCOPE_PROBE_K = 5
_scope_cache = TTLCache(ttl_seconds=600, max_size=1000)


def _load_niche_filter():
    """The harvest gate's vocabulary is the index's own definition of its
    topics. Loaded by path (the API is deployed without the pipeline package):
    next to this file first, then the repo's pipeline dir, then the live one."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.join(here, "..", "pipeline"), "/opt/dtox-research"):
        path = os.path.join(d, "niche_filter.py")
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("dtox_niche_filter", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            log.warning("niche_filter at %s could not be loaded", path)
    return None


_NICHE = _load_niche_filter()


def _query_names_indexed_topic(query):
    """(True, [terms]) when the query itself contains a term the harvest gate
    treats as on-topic, judged the way the gate judges paper text (case-aware
    for abbreviations, so "MEV" counts and "MeV photons" does not)."""
    if INDEX_SCOPE_ANCHORS.search(query):
        return True, []
    if _NICHE is None:
        return False, []
    try:
        scores = _NICHE.niche_score(query)
        layers = _NICHE.admitted_layers(scores)
    except Exception:
        return False, []
    terms = sorted({t for l in layers for t in scores[l]["matched_terms"]})
    return bool(layers), terms


def _scope_dense_probe(query):
    """Mean cosine of the nearest papers in the paper-level index, or None when
    it cannot be measured. Shares the coarse embedding and the Qdrant result
    caches with the search that normally ran a moment earlier."""
    if not (COARSE_EMBED_URL and HIER_SEARCH):
        return None
    try:
        vector = embed_query_coarse(query)
        hits = qdrant_search(vector, None, 60, timeout=SEARCH_QDRANT_TIMEOUT,
                             with_payload=True, collection=COARSE_COLLECTION)
    except (HTTPException, requests.RequestException):
        return None
    scores = sorted((h.get("score") or 0 for h in hits), reverse=True)[:SCOPE_PROBE_K]
    return sum(scores) / len(scores) if scores else None


def _scope_out(in_scope, confidence, reason, **extra):
    note = (None if in_scope else SCOPE_NOTE if in_scope is False else
            "Coverage looks thin for this query: check the results before relying on them.")
    out = {"in_scope": in_scope, "confidence": confidence, "reason": reason, "note": note}
    out.update(extra)
    return out


def _query_scope(query):
    """{"in_scope", "confidence", "reason", "note"} for one query. Rules in
    order: an explicit foreign domain with no index anchor is out; a term the
    index is built around is in; otherwise the nearest papers can only say
    "in" or "uncertain" (in_scope None), and a query that cannot be measured
    is assumed in scope rather than flagged."""
    query = (query or "").strip()
    cached = _scope_cache.get(query)
    if cached is not None:
        return dict(cached)
    named, terms = _query_names_indexed_topic(query)
    domain = None
    if not named:
        domain = _foreign_scope_domain(query)
        if domain is None:
            for name, pattern in SCOPE_FOREIGN_EXTRA.items():
                if pattern.search(query):
                    domain = name
                    break
    if named:
        out = _scope_out(True, "high", "query names an indexed topic",
                         **({"matched_terms": terms[:5]} if terms else {}))
    elif domain:
        out = _scope_out(False, "high", f"query is about {domain}, which the index does not cover")
    else:
        mean = _scope_dense_probe(query)
        if mean is None:
            out = _scope_out(True, "low", "not measured")
        elif mean < SCOPE_DENSE_FLOOR:
            # uncertain, never "out": "token vesting schedules" and "parimutuel
            # market oracle" fall under this floor and are web3, while "football
            # tactics" sat 0.01 below it; only a named foreign domain says out
            out = _scope_out(None, "low",
                             "few close papers; the topic may be thin here or outside the index",
                             nearest_similarity=round(mean, 3))
        else:
            out = _scope_out(True, "low",
                             "no indexed-topic term in the query, but close papers exist",
                             nearest_similarity=round(mean, 3))
    _scope_cache.set(query, out)
    return dict(out)


def _topic_coverage(text, layer=None, strict=False, probe_limit=None,
                    timeout=SEARCH_QDRANT_TIMEOUT, details=None):
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
        if details is not None:
            details.update(status="unavailable", by_layer={})
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
            if details is not None:
                details.setdefault("by_layer", {})[lay] = {"status": "unavailable"}
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
                    papers.add(canonical_id(pid))
        # Count at the generous floor, but only in layers where something
        # actually reaches the band. A subject with seven papers hovering just
        # above web3's deliberately low floor and nothing near the band is not
        # covered, it is noise -- and it was noise that grew from four papers to
        # seven as the corpus grew, which is how the gate went quiet on robotics.
        per_layer[lay] = len(papers) if (reaches_band or not strict) else 0
        if details is not None:
            details.setdefault("by_layer", {})[lay] = {
                "status": "measured", "canonical_papers": per_layer[lay],
                "chunk_hits": len(hits), "chunk_cap": probe_limit,
                "is_lower_bound": len(hits) >= probe_limit,
                "relevance_floor": floor, "evidence_band": band,
                "reaches_evidence_band": reaches_band,
            }
    if not per_layer:
        return None, {}, None
    if details is not None:
        details["status"] = "measured" if len(per_layer) == len(layers) else "partial"
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
                           constraints=None, _deadline=None):
    """Best chunk of each named paper against the query, scored the usual way,
    so a paper reached through the graph is comparable to one found by search.

    Two-phase, like the rest of dense retrieval: phase 1 asks Qdrant for only
    id/score/arxiv_id/section_type across up to len(paper_ids)*8 candidates
    (enough to cover every chunk of every named paper); phase 2 fetches the
    full payload for just the one winning point per paper. Fetching full
    payload (including chunk text) for all ~320 candidates on every call --
    over an SSH tunnel to Qdrant -- was the single largest cost in a
    hierarchical-search request; this cuts it to a handful of points.
    """
    if not paper_ids:
        return []
    must = [{"key": "arxiv_id", "match": {"any": list(paper_ids)}}]
    must.extend(constraints or [])
    if layer and not any(item.get("key") == "layers" for item in must):
        must.append({"key": "layers", "match": {"any": [layer]}})
    try:
        if _deadline is not None:
            timeout = min(timeout, _source_remaining(_deadline))
        query_vector = vector or (embed_query(query, wait_timeout=timeout / 2,
                                             request_timeout=timeout / 2)
                                  if _deadline is not None else embed_query(query))
        if _deadline is not None:
            timeout = min(timeout, _source_remaining(_deadline))
        if _deadline is not None:
            hits = grouped_hits(requests.post, QDRANT_URL, COLLECTION, query_vector, {"must": must},
                                limit=len(paper_ids), timeout=timeout, group_size=8)
            hits = _source_call(_deadline, _filter_publication_points, hits)
        else:
            hits = qdrant_search(query_vector, {"must": must}, len(paper_ids) * 8,
                                 timeout=timeout,
                                 with_payload=["arxiv_id", "section_type"])
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
            best[pid] = (rank, h)
    if not best:
        return []
    winner_ids = [h["id"] for _, h in best.values()]
    if _deadline is not None:
        timeout = min(timeout, _source_remaining(_deadline))
    full_payloads = qdrant_fetch_payloads(winner_ids, timeout=timeout)
    results = []
    for pid, (_, h) in best.items():
        pay = full_payloads.get(h.get("id")) or {}
        if not pay or pay.get("arxiv_id") != pid:
            continue
        results.append({
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
            "fulltext": None,
            "evidence": provenance(pid, pay, scope="indexed_chunk"),
            **({"publication_revision": pay.get("publication_revision")} if _deadline is not None else {}),
        })
    return results


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
_CLAIM_VECTOR_UNPREPARED = object()


def _canonical_claim(conn, claim, commit=True, prepared_vector=_CLAIM_VECTOR_UNPREPARED):
    """Existing claim node this wording belongs to, or a new one.

    Returns (claim_id, is_new). Only exact wording and explicit links select
    an existing node. A prepared vector avoids embedding inside a write
    transaction; None preserves the embedding-unavailable exact-text fallback."""
    norm = _norm_claim(claim)
    row = conn.execute("SELECT claim_id FROM claim_nodes WHERE claim_norm=?", (norm,)).fetchone()
    if row:
        return _follow_claim_links(conn, row["claim_id"]), False
    linked = _follow_claim_links(conn, norm)
    if linked != norm:
        return linked, False
    vector = prepared_vector
    if vector is _CLAIM_VECTOR_UNPREPARED:
        try:
            vector = embed_query(claim)
        except HTTPException:
            vector = None
    if vector is None:
        return norm, False
    conn.execute(
        "INSERT OR IGNORE INTO claim_nodes (claim_id, claim_norm, claim_text, vector, created_at) "
        "VALUES (?,?,?,?,?)",
        (norm, norm, claim, json.dumps(vector),
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    if commit:
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
                       ("attestation_pda", "TEXT"), ("attestation_tx", "TEXT"),
                       ("signed_issued_at_epoch_ms", "INTEGER")):
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
    if verified:
        identity = f"key:{verified}"
    else:
        identity = f"key:{_reader_id(api_key)}"
    configured = (info or {}).get("reader_model") or (info or {}).get("model")
    model = configured.strip()[:80] if isinstance(configured, str) else ""
    if model and model.casefold() != JUDGMENT_MODEL_UNKNOWN and not model.lower().startswith(("key:", "wallet:")):
        return identity, f"trusted-model:{model}"
    return identity, JUDGMENT_MODEL_UNKNOWN


def _trusted_stored_model(value):
    if isinstance(value, str) and value.startswith("trusted-model:"):
        model = value[len("trusted-model:"):].strip()
        if model and model.casefold() != JUDGMENT_MODEL_UNKNOWN and not model.lower().startswith(("key:", "wallet:")):
            return model
    return JUDGMENT_MODEL_UNKNOWN


def _judgment_status(counts, readers=None, models=None):
    if __package__:
        from .registry_policy import judgment_status
    else:
        from registry_policy import judgment_status
    if isinstance(models, str):
        models = [models]
    models = [m for m in (models or []) if isinstance(m, str)
              and not m.strip().lower().startswith(("key:", "wallet:"))]
    return judgment_status(counts, readers=readers, models=models, quorum=CONFIRMATION_QUORUM)


def _judgments_read_conn():
    try:
        conn = sqlite3.connect(f"file:{JUDGMENTS_DB_PATH}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _read_claim_id(conn, claim):
    norm = _norm_claim(claim)
    row = conn.execute("SELECT claim_id FROM claim_nodes WHERE claim_norm=?", (norm,)).fetchone()
    return _follow_claim_links(conn, row["claim_id"] if row else norm)


def _signing_claim_id(claim):
    conn = _judgments_read_conn()
    if conn is None:
        return _norm_claim(claim)
    try:
        return _read_claim_id(conn, claim)
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        return _follow_claim_links(conn, _norm_claim(claim))
    finally:
        conn.close()


def _prepare_signed_claim_vector(claim, claim_id):
    norm = _norm_claim(claim)
    if claim_id != norm:
        return None
    conn = _judgments_read_conn()
    if conn is not None:
        try:
            if conn.execute("SELECT 1 FROM claim_nodes WHERE claim_norm=?", (norm,)).fetchone():
                return None
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error):
                raise
        finally:
            conn.close()
    try:
        return embed_query(claim)
    except HTTPException:
        return None


def _follow_claim_links(conn, claim_id):
    seen = set()
    for _ in range(64):
        if claim_id in seen:
            return min(seen)
        seen.add(claim_id)
        try:
            row = conn.execute("SELECT to_claim FROM claim_links WHERE from_claim=? "
                               "ORDER BY linked_at DESC, to_claim LIMIT 1", (claim_id,)).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return claim_id
            raise
        if row is None:
            return claim_id
        claim_id = row["to_claim"]
    return claim_id


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
    conn = _judgments_read_conn()
    if conn is None:
        return _norm_claim(claim), {}
    try:
        claim_id = _read_claim_id(conn, claim)
        rows = conn.execute(
            "SELECT paper_id, verdict, reason, judged_by, judged_by_model "
            "FROM claim_judgments WHERE claim_id=? OR claim_norm=?",
            (claim_id, _norm_claim(claim))).fetchall()
    except sqlite3.Error:
        return _norm_claim(claim), {}
    finally:
        conn.close()
    out = {}

    def absorb(rows_, from_node=None):
        for r in rows_:
            e = out.setdefault(r["paper_id"], {"counts": {}, "reasons": [],
                                               "readers": set(), "models": set(),
                                               "from_claim": from_node})
            e["counts"][r["verdict"]] = e["counts"].get(r["verdict"], 0) + 1
            if r["verdict"] in {"asserts", "does_not_assert"}:
                e["readers"].add(r["judged_by"])
                model = _trusted_stored_model(r["judged_by_model"])
                if model != JUDGMENT_MODEL_UNKNOWN:
                    e["models"].add(model)
            if r["reason"]:
                e["reasons"].append(_clip(r["reason"], 300))

    absorb(rows)
    conn = _judgments_read_conn()
    try:
        neighbours = _neighbour_records(conn, claim) if conn is not None else []
        for cand, cand_rows in neighbours:
            absorb([r for r in cand_rows if r["paper_id"] not in out],
                   from_node={"claim_id": cand["claim_id"],
                              "claim_text": cand["claim_text"],
                              "cosine": cand["cosine"]})
    except sqlite3.Error:
        pass
    finally:
        if conn is not None:
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
    conn = _judgments_read_conn()
    if conn is None:
        return {}
    try:
        claim_id = _read_claim_id(conn, claim)
        placeholders = ",".join("?" * len(paper_ids))
        rows = conn.execute(
            f"SELECT paper_id, verdict, reason, judged_by, judged_by_model, judged_at, "
            f"attestation_pda "
            f"FROM claim_judgments WHERE (claim_id=? OR claim_norm=?) "
            f"AND paper_id IN ({placeholders})",
            [claim_id, _norm_claim(claim)] + list(paper_ids),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        entry = out.setdefault(r["paper_id"],
                               {"counts": {}, "reasons": [], "readers_set": set(),
                                "models": set(), "onchain": []})
        entry["counts"][r["verdict"]] = entry["counts"].get(r["verdict"], 0) + 1
        if r["verdict"] in {"asserts", "does_not_assert"}:
            entry["readers_set"].add(r["judged_by"])
            model = _trusted_stored_model(r["judged_by_model"])
            if model != JUDGMENT_MODEL_UNKNOWN:
                entry["models"].add(model)
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
        identity, trusted_model = _reader_identity(api_key, _info, body.judged_by_model)
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
                 reader, trusted_model, now,
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

    claim_id is resolved through existing nodes and explicit canonical links,
    without creating a node, so the same question asked in different words
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

    claim_id = _signing_claim_id(claim)

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

    judged_by is stored as "wallet:<reviewer>". Different wallets are distinct
    readers, but wallet agreement remains pending without trusted model
    diversity. The same wallet repeating itself is still one reader (see
    _judgment_status).
    """
    auth_and_limit(x_api_key, "adjudicate")
    claim = (body.claim or "").strip()
    if not claim:
        raise HTTPException(status_code=400, detail="claim must not be empty")

    claim_id, is_new = _signing_claim_id(claim), False

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

        if resp.status_code in (400, 409, 429):
            item_result["status"] = resp.status_code
            item_result["error"] = _attestor_error_detail(resp)
            results.append(item_result)
            continue
        if resp.status_code != 200:
            raise HTTPException(status_code=503,
                                detail=f"attestor error: {_attestor_error_detail(resp)}")

        attestation = resp.json()
        issued_at_epoch_ms = attestation.get("issued_at_epoch_ms")
        if type(issued_at_epoch_ms) is not int or abs(issued_at_epoch_ms) > 8640000000000000:
            raise HTTPException(status_code=503, detail="attestor returned invalid issued_at_epoch_ms")
        if not isinstance(attestation.get("attestation"), str) or not attestation["attestation"].strip():
            raise HTTPException(status_code=503, detail="attestor returned invalid attestation")
        prepared_vector = _prepare_signed_claim_vector(claim, claim_id)
        identity = f"wallet:{j.reviewer}"
        conn = _judgments_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if _read_claim_id(conn, claim) != claim_id:
                raise HTTPException(status_code=409, detail="claim link changed; prepare and sign a new message")
            written = conn.execute(
                "INSERT INTO claim_judgments "
                "(claim_norm, claim_text, claim_id, paper_id, verdict, reason, judged_by, "
                " judged_by_model, judged_at, layer, attestation_pda, attestation_tx, signed_issued_at_epoch_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(claim_norm, paper_id, judged_by) DO UPDATE SET "
                "verdict=excluded.verdict, reason=excluded.reason, judged_at=excluded.judged_at, "
                "claim_id=excluded.claim_id, attestation_pda=excluded.attestation_pda, "
                "attestation_tx=excluded.attestation_tx, "
                "signed_issued_at_epoch_ms=excluded.signed_issued_at_epoch_ms "
                "WHERE claim_judgments.signed_issued_at_epoch_ms IS NULL "
                "OR excluded.signed_issued_at_epoch_ms > claim_judgments.signed_issued_at_epoch_ms",
                (_norm_claim(claim), claim, claim_id, j.id, j.verdict, (j.reason or "")[:600],
                 identity, JUDGMENT_MODEL_UNKNOWN, now, body.layer,
                 attestation.get("attestation"), attestation.get("signature"), issued_at_epoch_ms),
            )
            if written.rowcount:
                _, created = _canonical_claim(conn, claim, commit=False, prepared_vector=prepared_vector)
                is_new = is_new or created
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
    corpus["as_of_kind"] = "observed_timestamp"
    corpus["note"] = ("as_of is an observed timestamp for this audit, not an immutable "
                      "index snapshot or a reproducibility guarantee. The index and "
                      "mutable sources can change; cite the timestamp and evidence provenance.")
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
    if not fast_mode and not SOURCE_GENERATION_MANIFEST:
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
    coverage_cache, coverage_details = {}, {}
    source_scope_identity = None

    def _check_source_scope(payload):
        nonlocal source_scope_identity
        generation = payload.get("source_generation", {})
        identity = (generation.get("generation"), generation.get("cache_identity"))
        if payload.get("partial") or generation.get("status") != "ready" or not all(identity):
            raise HTTPException(status_code=503, detail="source-generation scope/evidence retrieval unavailable; activation blocked")
        if source_scope_identity is not None and source_scope_identity != identity:
            raise HTTPException(status_code=503, detail="source generation changed during audit; retry against one generation")
        source_scope_identity = identity

    def _coverage_of(text, strict=False):
        key = (text, strict)
        if key not in coverage_cache:
            coverage_details[key] = {}
            if SOURCE_GENERATION_MANIFEST:
                payload = _run_search(SearchBody(query=text, layer=body.layer, limit=50, dedupe=False,
                                      auto_relax=False, diagnose=False), x_api_key,
                                      dense_timeout=audit_qdrant_timeout, embed_wait_timeout=audit_embed_wait,
                                      embed_http_timeout=audit_embed_http, _top=False, _defer_hydration=True)
                _check_source_scope(payload)
                qualified, best = {}, 0
                for hit in payload["results"]:
                    score = hit.get("score")
                    if (hit.get("evidence_state") != "original_passage_retrieved"
                            or type(score) not in (int, float) or not math.isfinite(score)):
                        continue
                    best = max(best, score)
                    for layer in ([body.layer] if body.layer else hit.get("layers") or []):
                        floor = MIN_RELEVANCE_BY_LAYER.get(layer, MIN_RELEVANCE_SCORE)
                        if score >= floor and (not strict or _score_band(score, layer) != "weak"):
                            qualified.setdefault(layer, set()).add(canonical_id(hit["arxiv_id"]))
                by_layer = {layer: len(ids) for layer, ids in qualified.items()}
                coverage_cache[key] = (max(by_layer.values(), default=0), by_layer, best)
                coverage_details[key] = {
                    "metric": "qualified_original_sources_from_source_generation",
                    "generation": source_scope_identity[0], "cache_identity": source_scope_identity[1],
                    "scope": "observed_local", "probe_cap": 50,
                    "by_layer": {layer: {"canonical_papers": count, "is_lower_bound": True,
                        "relevance_floor": MIN_RELEVANCE_BY_LAYER.get(layer, MIN_RELEVANCE_SCORE)}
                        for layer, count in by_layer.items()}}
            else:
                coverage_cache[key] = _topic_coverage(
                    text, body.layer, strict,
                    FAST_COVERAGE_PROBE_LIMIT if fast_mode else COVERAGE_PROBE_LIMIT,
                    timeout=audit_qdrant_timeout,
                    details=coverage_details[key],
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
    scope_unverified = bool(SOURCE_GENERATION_MANIFEST and scope_best < IN_SCOPE_MIN_PAPERS)
    if scope_best < IN_SCOPE_MIN_PAPERS and not SOURCE_GENERATION_MANIFEST:
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
                       min_score=0.60, dedupe=False, diagnose=False,
                       auto_relax=False, **extra)
        payload = _run_search(b, x_api_key, dense_timeout=audit_qdrant_timeout,
                        embed_wait_timeout=audit_embed_wait,
                        embed_http_timeout=audit_embed_http, _top=False)
        if SOURCE_GENERATION_MANIFEST:
            _check_source_scope(payload)
        hits = _scored_results_for_audit(payload, incomplete=claim_evidence)
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
        claim_evidence = {"incomplete_evidence": scope_unverified, "unread_count": 0,
                          "unqualified_count": 0, "sources": {}}
        # coverage has to be measured over the same wordings the evidence was
        # found with, or a claim can show three solid papers and "thin" beside
        # them: the narrow phrasing that finds little is exactly the one a
        # cautious caller writes
        claim_id, registry = _registry_for_claim(claim)
        _link_conn = _judgments_read_conn()
        try:
            same_question = [dict(c, via="wording")
                             for c in _similar_claim_nodes(_link_conn, claim)] if _link_conn else []
        except sqlite3.Error:
            same_question = []
        finally:
            if _link_conn is not None:
                _link_conn.close()
        found, queries_used = _search_union(claim, phrasings)
        retrieved_today_ids = {h["arxiv_id"] for h in found}
        named_source_ids = _audit_named_source_ids(
            claim, phrasings, idea, allow_idea_fallback=len(claim_specs) == 1)
        found_by_head = {canonical_id(h["arxiv_id"]): h for h in found}
        named_source_hits = [found_by_head[canonical_id(pid)] for pid in named_source_ids
                             if canonical_id(pid) in found_by_head]
        named_source_paths = {canonical_id(h["arxiv_id"]): "ordinary_claim_retrieval"
                              for h in named_source_hits}
        lookup_ids = [pid for pid in named_source_ids
                      if canonical_id(pid) not in found_by_head]
        named_lookup_error = False
        if lookup_ids:
            try:
                explicit_hits = _score_specific_papers(
                    claim, lookup_ids, body.layer, timeout=audit_qdrant_timeout)
                named_source_hits.extend(explicit_hits)
                named_source_paths.update({canonical_id(h["arxiv_id"]):
                                           "bounded_explicit_id_lookup"
                                           for h in explicit_hits})
            except Exception as error:
                log.warning("named source lookup unavailable: %s", type(error).__name__)
                named_lookup_error = True
        for hit in named_source_hits:
            hit["found_via"] = "explicitly named ID"
            hit["explicitly_named"] = True
            if not any(canonical_id(h["arxiv_id"]) == canonical_id(hit["arxiv_id"])
                       for h in found):
                found.append(hit)
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
                rec = registry[h["arxiv_id"]]
                h["found_via"] = ("registry: judged against a similar, unlinked claim"
                                  if rec.get("from_claim") else
                                  "registry: judged against this canonical claim before")
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
        _ov_conn = _judgments_read_conn()
        try:
            overlap_nodes = _nodes_sharing_evidence(
                _ov_conn, [h["arxiv_id"] for h in found[:12]], claim_id) if _ov_conn else []
        except sqlite3.Error:
            overlap_nodes = []
        finally:
            if _ov_conn is not None:
                _ov_conn.close()
        for h in found:
            if h["arxiv_id"] in registry:
                h["from_registry"] = True
        found = _hydrate_evidence(found)
        named_source_by_head = {canonical_id(h["arxiv_id"]): h for h in found}
        named_sources = []
        for paper_id in named_source_ids:
            hit = named_source_by_head.get(canonical_id(paper_id))
            if hit is None:
                named_sources.append({
                    "id": paper_id,
                    "found": False,
                    "status": ("lookup_unavailable" if named_lookup_error
                               else "not_returned_by_indexed_lookup"),
                    "why_returned": "The claim or its phrasing explicitly names this document ID.",
                    "note": ("The bounded indexed lookup did not return a matching chunk. "
                             "The source may be absent from this index or the lookup may be "
                             "unavailable; this is not evidence of novelty."),
                    "verification_required": True,
                })
                continue
            named_sources.append({
                "id": hit["arxiv_id"],
                "found": True,
                "status": "found_by_indexed_lookup",
                "title": hit.get("title"),
                "year": hit.get("year"),
                "score": round(hit["score"], 3),
                "band": _evidence_band(hit, body.layer,
                                       _distinctive_words(claim, body.layer)),
                "why_returned": "The claim or its phrasing explicitly names this document ID.",
                "retrieval_path": named_source_paths.get(canonical_id(hit["arxiv_id"]),
                                                         "ordinary_claim_retrieval"),
                "note": "A named source is not proof that the paper makes the claim.",
                "url": hit.get("url") or hit.get("arxiv_url"),
                "provenance": hit.get("evidence"),
                "section": hit.get("section_type"),
                "snippet": _clip(hit.get("text"), 320),
                "verification_required": True,
            })
        seen_nodes = {c["claim_id"] for c in same_question}
        same_question = same_question + [c for c in overlap_nodes
                                         if c["claim_id"] not in seen_nodes]

        # coverage after the search, over every wording actually used: the
        # expansion query is usually the one that names the subject properly,
        # and leaving it out reported a well-stocked subject as thin
        coverage, coverage_by_layer, strict_coverage = 0, {}, 0
        coverage_probes = []
        for q in queries_used:
            got, by_layer, _best = _coverage_of(q)
            if got is None:
                continue
            coverage_probes.append(dict(query=q, **coverage_details[(q, False)]))
            coverage = max(coverage, got)
            strict_coverage = max(strict_coverage, len({canonical_id(h["arxiv_id"])
                for h in found
                if h["arxiv_id"] in retrieved_today_ids
                if _score_band(h.get("score") or 0,
                               _layer_of(h, body.layer)) != "weak"}))
            for lay, n in (by_layer or {}).items():
                coverage_by_layer[lay] = max(coverage_by_layer.get(lay, 0), n)
        # one lookup, not two: _prior_readings only saw this node, so a paper
        # carried into the evidence from a neighbour's record was displayed as
        # "unread" right next to the verdict that rested on it
        readings = registry
        mentions = [h for h in found if h.get("section_type") in CONTEXT_SECTIONS
                    and not h.get("from_registry")]
        hits = [h for h in found if h.get("section_type") not in CONTEXT_SECTIONS
                or h.get("from_registry")]
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
                # Scores rank passage relevance; they do not establish entailment.
                top_ids = {candidates[i]["arxiv_id"] for i in order[:3]
                           if ce_scores[i] > CROSS_POSITIVE}
                for h, sc in zip(candidates, ce_scores):
                    h["cross_score"] = round(float(sc), 3)
                    h["cross_top"] = h["arxiv_id"] in top_ids
                # Apply relative passage-relevance ordering; this is not claim entailment.
                rerank_order = {candidates[i]["arxiv_id"]: n for n, i in enumerate(order)}
                rerank_report = {
                    "ran": True,
                    "score_semantics": "relative passage relevance, not claim entailment",
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
        current_registry = {pid: rec for pid, rec in registry.items() if not rec.get("from_claim")}
        similar_registry = {pid: rec for pid, rec in registry.items() if rec.get("from_claim")}
        by_status = {}
        for rec in current_registry.values():
            by_status[rec["status"]] = by_status.get(rec["status"], 0) + 1
        pending_current = sum(n for status, n in by_status.items()
                              if status not in {"confirmed_prior_art", "ruled_out", "contested"})
        settled = {
            "confirmed_prior_art": len(confirmed) + by_status.get("confirmed_prior_art", 0),
            "ruled_out": len(ruled_out) + by_status.get("ruled_out", 0),
            "pending_quorum": len(read_once) + pending_current,
            "agreed_same_model": by_status.get("agreed_same_model", 0),
            "contested": len(contested) + by_status.get("contested", 0),
            "unread": len(unread_strong),
            "on_record_total": len(registry),
            "current_claim_total": len(current_registry),
            "reported_on_similar_claim": len(similar_registry),
            "note": "Settled and pending counts refer only to the current canonical claim. "
                    "Similar unlinked claims require separate claim verification; agreed_same_model "
                    "is a subset of pending_quorum, not scientific quorum.",
        }
        ruled_out_ids = {h["arxiv_id"] for h in ruled_out} | {
            pid for pid, rec in current_registry.items() if rec["status"] == "ruled_out"}
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
        elif by_status.get("contested", 0):
            verdict = "registry_readings_contested"
            reading = ("Readers disagree about the current canonical claim. The registry is "
                       "unresolved, not empty; review the recorded reasons and evidence before "
                       "drawing a conclusion.")
        elif on_similar and not [p for p in registry_asserts if p not in on_similar]:
            verdict = "prior_art_reported_on_similar_claim"
            reading = ("A reader judged these papers against a claim worded very "
                       "closely to yours, but the two claims are separate records. "
                       "Read them, and if the two wordings ask the same question, "
                       "link the nodes so the next caller inherits the verdict.")
        elif registry_asserts and not confirmed:
            verdict = "prior_art_reported_pending_quorum"
            reading = ("Readers have recorded that these papers assert this claim. Their "
                       "readings remain pending without trusted model diversity; wallet votes "
                       "alone do not establish scientific quorum. Read the evidence and file "
                       "an independent, verified opinion.")
        elif similar_registry and not current_registry:
            verdict = "readings_reported_on_similar_claim"
            reading = ("Readings exist on similar, unlinked claims, not on this canonical claim. "
                       "Their partial or negative verdicts do not settle this claim; verify the "
                       "claim relationship and read the evidence.")
        elif pending_current:
            verdict = "candidates_read_pending_quorum"
            reading = ("Registry readings exist for the current canonical claim but remain "
                       "unsettled. Partial readings or agreement without trusted model diversity "
                       "are not scientific quorum. Review the evidence and unresolved verdicts.")
        elif confirmed:
            verdict = "prior_art_confirmed"
            reading = ("Independent readers agreed these papers make this claim. "
                       "Treat it as existing work and compete on execution.")
        elif direct and not unread_strong:
            verdict = "candidates_read_pending_quorum"
            reading = ("Every candidate has been read once and none is settled yet. "
                       "A second independent reading closes this claim; until then "
                       "each paper carries the earlier reader's reasons.")
        elif ruled_out_ids and not direct:
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
            named_ids_found = [h["arxiv_id"] for h in named_source_hits]
            limitation_scan_ids = list(dict.fromkeys(named_ids_found + list(cited_ids)))[:4]
            for pid in limitation_scan_ids:
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
                                             "year": pay.get("year"), "text": pay.get("text"),
                                             "found_via": ("explicitly named ID" if pid in named_ids_found
                                                           else "cited evidence")})
                        break
            searched_limits = _search(claim, section_type="limitations")
            found_limits += [dict(l, found_via="claim wording") for l in searched_limits]
            ordered_limits = ([l for l in found_limits if l["arxiv_id"] in cited_ids]
                              + [l for l in found_limits if l["arxiv_id"] not in cited_ids])
            seen_limit_ids = set()
            limitations = []
            for limit_item in ordered_limits:
                head = canonical_id(limit_item["arxiv_id"])
                if head in seen_limit_ids:
                    continue
                seen_limit_ids.add(head)
                limitations.append(limit_item)
                if len(limitations) == 3:
                    break
            if hits:
                try:
                    spec = paper_spec(hits[0]["arxiv_id"], target_elements="algorithm,equation",
                                      max_chars=2500, x_api_key=x_api_key)
                    formulas = spec.get("sections", [])[:2]
                except HTTPException:
                    formulas = []

        incomplete_evidence = claim_evidence["incomplete_evidence"]
        if incomplete_evidence:
            uncertainty = ("Topic scope is unverified; the bounded qualified-source count does not establish "
                           "index absence. " if scope_unverified else "")
            if claim_evidence["unqualified_count"]:
                uncertainty += "Some discovered sources lack qualified original evidence. "
            if verdict in {"candidates_ruled_out", "adjacent_work_only", "weak_signal_only", "no_match_in_corpus"}:
                verdict = "incomplete_evidence"
                reading = uncertainty + ("Returned readings remain available, but this audit cannot conclude "
                                         "that matching prior art is absent.")
            else:
                reading = ("Returned originals and recorded readings support this verdict. " + uncertainty +
                           "No absence or exhaustive-reading conclusion follows.")
        years = [h.get("year") for h in hits if h.get("year")]
        claim_reports.append({
            "claim": claim,
            "incomplete_evidence": incomplete_evidence,
            "scope_unverified": scope_unverified,
            "unread_count": claim_evidence["unread_count"],
            "unqualified_count": claim_evidence["unqualified_count"],
            "unread_count_is_lower_bound": bool(claim_evidence["unqualified_count"]),
            "evidence_completeness_note": ("Counts take the maximum across wording/filter searches, not a sum. "
                                           "Unqualified includes unread originals and evidence below the requested "
                                           "score. Source leads never establish claim assertions."),
            "source_candidates": list(claim_evidence["sources"].values())[:50],
            "absence_verdict_allowed": not incomplete_evidence,
            "depth": body.depth,
            "deferred_sections": (["citation_graph", "cross_encoder", "limitations",
                                   "supporting_math"] if fast_mode else []),
            "reranked": rerank_report,
            "phrasings_searched": queries_used,
            "claim_id": claim_id,
            "same_question_candidates": same_question,
            "named_sources": named_sources,
            "registry": [{
                "id": pid,
                "status": rec["status"],
                "readers": rec["readers"],
                "models": rec["distinct_models"],
                "counts": rec["counts"],
                "reasons": rec["reasons"][:2],
                "from_claim": rec.get("from_claim"),
                "claim_scope": "similar_unlinked" if rec.get("from_claim") else "current_canonical",
                "retrieved_today": pid in retrieved_today_ids,
                "hydrated_from_registry": pid in {h["arxiv_id"] for h in hydrated},
                "evidence_returned": pid in {h["arxiv_id"] for h in hits},
            } for pid, rec in sorted(registry.items(),
                                     key=lambda kv: -kv[1]["readers"])[:8]],
            "registry_summary": {
                "current_claim": len(current_registry), "similar_claims": len(similar_registry),
                "retrieved_today": len(set(registry) & retrieved_today_ids),
                "hydrated_from_registry": len({h["arxiv_id"] for h in hydrated}),
                "without_returned_evidence": len(set(registry) - {h["arxiv_id"] for h in hits}),
                "note": "Counts are papers, not wallet votes. Retrieval, hydration and claim scope "
                        "are separate sets. Hydration does not mean today's wording retrieved the paper.",
            },
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
                "is_lower_bound": bool(SOURCE_GENERATION_MANIFEST) or any(lay.get("is_lower_bound", False)
                                      for probe in coverage_probes
                                      for lay in probe.get("by_layer", {}).values()),
                "probe_cap": (50 if SOURCE_GENERATION_MANIFEST else FAST_COVERAGE_PROBE_LIMIT if fast_mode
                              else COVERAGE_PROBE_LIMIT),
                "by_layer": coverage_by_layer,
                "probes": coverage_probes,
                "how_counted": ("Canonical twin IDs count once per layer and wording. by_layer "
                                "takes the maximum count across wordings, not a union; "
                                "papers_about_this_claim takes the largest layer count, not their sum. "
                                "Counts use the existing layer relevance floor. probe_cap limits chunk "
                                "hits, not unique papers. is_lower_bound means a probe reached its "
                                "chunk cap and may be truncated; it does not imply probe_cap papers. "
                                "Registry hydration is excluded from these retrieval counts."),
                "depth": _coverage_grade(coverage),
                "reading": ("Evidence is incomplete; topic coverage cannot establish absence." if incomplete_evidence else
                            "A quiet result over a well-covered subject is a real "
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
                {"verdict": ("thin_retrieval_with_registry_evidence" if registry else "outside_the_index"),
                 "reading": ("Today's bounded wording-based retrieval is thin, but registry evidence "
                             "exists. Zero retrieved papers is not zero indexed literature. Current "
                             "canonical and similar unlinked records are counted separately; similar "
                             "records need claim verification and do not settle this claim."
                             if registry else
                             "The index holds almost nothing on this subject and "
                             "nothing that clears the relevance band for its layer. "
                             "Treat this as a statement about the index, not about "
                             "the literature: no conclusion about novelty follows."),
                 # counted at the strict band, not the permissive one: web3's
                 # floor is deliberately low and let eleven papers look like
                 # coverage for a claim about tactile grippers
                 "papers_on_subject": strict_coverage,
                 "papers_at_relevance_floor": coverage,
                 "current_claim_registry_papers": len(current_registry),
                 "similar_claim_registry_papers": len(similar_registry),
                 "depth": _coverage_grade(coverage)}
                if (not incomplete_evidence and not direct and not adjacent
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
            "verification_required": bool(incomplete_evidence or unread_strong or pending_current
                                          or by_status.get("contested", 0) or similar_registry
                                          or named_sources),
            "settled": settled,
            # was reading hits[0], which after registry hydration can be a
            # paper pinned for its verdict rather than for its score
            "best_score": (round(max(h["score"] for h in direct + adjacent), 3)
                           if (direct or adjacent) else
                           round(hits[0]["score"], 3) if hits else None),
            "matches": {"strong_unread": len(unread_strong),
                        "strong_read": settled["confirmed_prior_art"] + settled["pending_quorum"]
                                       + settled["contested"],
                        "ruled_out": settled["ruled_out"],
                        "similar_claim_read": len(similar_registry),
                        "adjacent": len(adjacent),
                        "returned": len(hits),
                        "note": "strong_unread counts direct retrieval candidates, not proven prior art. "
                                "strong_read includes pinned current-claim registry readings regardless "
                                "of today's score; similar_claim_read is separate and never settles this claim."},
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
                "found_via": l.get("found_via", "claim wording"),
                "still_a_candidate": l["arxiv_id"] not in ruled_out_ids,
                "text": _clip(l.get("text"), 400),
            } for l in limitations],
            "supporting_math": [{
                "element_type": f.get("element_type"),
                "section_title": f.get("section_title"),
                "text": (f.get("text") or "")[:900],
                "latex": ((f.get("text") or "")[:900]
                          if (f.get("evidence") or {}).get("exact_latex") else None),
                "evidence": f.get("evidence"),
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
    similar_total = sum(c["settled"]["reported_on_similar_claim"] for c in claim_reports)
    settled_total = sum(c["settled"]["confirmed_prior_art"] + c["settled"]["ruled_out"]
                        for c in claim_reports)
    unread_total = sum(c["settled"]["unread"] for c in claim_reports)
    counts = {v: sum(1 for c in claim_reports if c["verdict"] == v)
              for v in ("prior_art_confirmed", "prior_art_reported_pending_quorum",
                        "prior_art_reported_on_similar_claim",
                        "readings_reported_on_similar_claim", "registry_readings_contested",
                        "candidates_read_pending_quorum",
                        "candidates_ruled_out", "strong_candidates",
                        "adjacent_work_only", "weak_signal_only",
                        "no_match_in_corpus", "incomplete_evidence")}
    return {
        "idea": idea,
        "layer": body.layer,
        "corpus": corpus,
        **({"in_scope": None if scope_unverified else True,
            "scope_unverified": scope_unverified,
            "scope_assessment": {
                "state": "unverified" if scope_unverified else "verified",
                "reason": ("insufficient_qualified_original_scope_evidence" if scope_unverified else
                           "qualified_original_scope_evidence_observed"),
                "observed_qualified_original_sources": scope_best,
                "minimum_qualified_sources": IN_SCOPE_MIN_PAPERS,
                "is_lower_bound": True,
                "note": "Bounded qualified originals, not nearest-source count or total papers on topic. "
                        "Scope uncertainty is separate from unread originals and does not establish index absence."}}
           if SOURCE_GENERATION_MANIFEST else {}),
        "claims_audited": len(claim_reports),
        "scope_generation": ({"generation": source_scope_identity[0], "cache_identity": source_scope_identity[1],
                              "metric": "qualified_original_sources_from_source_generation"}
                             if source_scope_identity else None),
        "incomplete_evidence": any(c["incomplete_evidence"] for c in claim_reports),
        "unread_count": sum(c["unread_count"] for c in claim_reports),
        "unread_count_scope": "claim-source lower bound; a source may count once per claim",
        "absence_verdict_allowed": not any(c["incomplete_evidence"] for c in claim_reports),
        "verdict_summary": counts,
        "briefing": (
            "What the field already published on this, what those authors admit went "
            "wrong, and where the open ground is. Verdicts here are candidates until "
            "someone reads them."
        ),
        "headline": (
            f"{len(claim_reports)} claims audited: {read_total} papers read against current canonical claims "
            f"({settled_total} settled with trusted model diversity), {unread_total} direct candidates still "
            f"unread; {similar_total} papers reported on similar, unlinked claims require claim verification. "
            f"{counts['no_match_in_corpus']} claims have no match in this index. "
            f"{sum(c['incomplete_evidence'] for c in claim_reports)} claims have incomplete evidence; "
            "absence conclusions are withheld for those claims."
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


# ---------------------------------------------------------------------------
# Exploration: papers as the unit
# ---------------------------------------------------------------------------
# /v1/search answers with passages, which is right for "how does X work" and
# wrong for "which papers matter on X": a survey and its three follow-ups fill
# the page while the paper everyone cites sits at rank 30. These endpoints let
# an agent walk the corpus the way a reader does: list papers on a subject
# ordered by relevance, influence or recency; open one to see its abstract,
# outline and citation neighbours; read one section.

EXPLORE_LAYERS = {"llm-slm", "ai-agents", "web3", "builder-tech"}
EXPLORE_SORTS = ("relevance", "citations", "recent", "foundational")
EXPLORE_POOL = 60             # candidates ranked by relevance
# re-sorts only reorder the head of that list: at rank 57 of "maximal
# extractable value" sat a lattice QCD paper that says "maximally twisted",
# and sorting 60 by citations put it second
EXPLORE_RESORT_POOL = 30
# "foundational": papers the relevant ones cite, counted inside that set.
# Flash Boys 2.0 predates the word MEV and never ranks on wording, yet most
# MEV papers cite it; the graph does not care how either side is phrased.
FOUNDATIONAL_MIN_CITERS = 3
EXPLORE_ABSTRACT_CHARS = 420
SECTION_MAX_CHARS = 8000
NEIGHBOUR_LIMIT = 25
PAPER_COLUMNS = ("arxiv_id, title, year, venue, citation_count, influential, layers, abstract, "
                 "fulltext_source, source_url, parser_mode, parse_status")


def _paper_rows(paper_ids):
    """state.db record for each id that is fully indexed, keyed by id."""
    ids = [pid for pid in dict.fromkeys(paper_ids) if pid]
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        sql = (f"SELECT {PAPER_COLUMNS}, status FROM papers "
               f"WHERE arxiv_id IN ({','.join('?' * len(part))})")
        try:
            rows = _ro_conn(STATE_DB_PATH).execute(sql, part).fetchall()
        except sqlite3.OperationalError:
            _ro_conn_drop(STATE_DB_PATH)
            try:
                conn = _ro_conn(STATE_DB_PATH)
                columns = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
                requested = [c.strip() for c in PAPER_COLUMNS.split(",")] + ["status"]
                compatible = ", ".join(
                    c if c in columns else f"NULL AS {c}" for c in requested)
                sql = (f"SELECT {compatible} FROM papers "
                       f"WHERE arxiv_id IN ({','.join('?' * len(part))})")
                rows = conn.execute(sql, part).fetchall()
            except sqlite3.Error:
                rows = []
        except sqlite3.Error:
            rows = []
        for r in rows:
            if r[-1] != "done":
                continue
            out[r[0]] = {"arxiv_id": r[0], "title": r[1], "year": r[2], "venue": r[3],
                         "citation_count": r[4], "influential_citations": r[5],
                         "layers": [l for l in (r[6] or "").split(",") if l],
                         "abstract": r[7],
                         "fulltext_source": r[8] if len(r) > 8 else None,
                         "source_url": r[9] if len(r) > 9 else None,
                         "parser_mode": r[10] if len(r) > 10 else None,
                         "parse_status": r[11] if len(r) > 11 else None,
                         "link": None, "pdf_url": None, "landing_page_url": None}
    return out


def _hydrate_evidence(results, rows=None):
    if rows is None:
        rows = _evidence_rows([r.get("arxiv_id") or r.get("id") for r in results])
    if any(row.get("metadata_authority") == "frozen_source_catalog" for row in results):
        rows = dict(rows)
        for row in results:
            if row.get("metadata_authority") == "frozen_source_catalog":
                pid = row.get("arxiv_id") or row.get("id")
                rows[pid] = {**rows.get(pid, {}), **{field: row.get(field)
                             for field in ("title", "year", "layers", "source_url")}}
    return hydrate_results(results, rows, source_url, license_of,
                           lambda pid: SOURCE_LABELS[source_of(pid)])


def _hydrate_search_payload(payload):
    candidates = payload.get("source_candidates")
    if candidates is None:
        payload["results"] = _hydrate_evidence(payload["results"])
        return payload
    ids = [row.get("arxiv_id") or row.get("id")
           for row in [*payload.get("results", []), *candidates]]
    rows = _evidence_rows(ids)
    payload["results"] = _hydrate_evidence(payload.get("results", []), rows)
    payload["source_candidates"] = _hydrate_evidence(candidates, rows)
    return payload


def _evidence_rows(ids):
    try:
        return _paper_rows(ids)
    except Exception as error:
        log.warning("evidence metadata unavailable: %s", type(error).__name__)
        return {}


def _paper_passport(pid, fragment, rows):
    item = _hydrate_evidence([dict(fragment, arxiv_id=pid, url=source_url(pid, fragment))], rows)[0]
    return {key: item[key] for key in ("source", "url", "license", "fulltext", "completeness",
                                       "evidence", "provenance_missing", "source_url_origin",
                                       "source_url_status", "legacy_id_alias") if key in item}


def _passport_source_metadata(pid, rows, passport):
    metadata = dict(rows.get(pid) or {})
    metadata["_source_url_origin"] = passport.get("source_url_origin")
    metadata["_source_url_status"] = passport.get("source_url_status")
    metadata["_legacy_id_alias"] = passport.get("legacy_id_alias")
    return metadata


@app.post("/v1/research/bundle")
def research_bundle(body: ResearchBundleBody, x_api_key: Optional[str] = Header(default=None)):
    auth_and_limit(x_api_key)
    deadline = time.monotonic() + BUNDLE_SECONDS
    search_result = _run_search(SearchBody(query=body.query, layer=body.layer, limit=body.limit,
                                          strict=body.strict, diagnose=False), x_api_key)
    selected, seen = [], set()
    for hit in search_result.get("results", []):
        pid = hit.get("arxiv_id")
        if pid and pid not in seen:
            seen.add(pid)
            selected.append(hit)
        if len(selected) >= body.limit:
            break
    if any("evidence" not in hit for hit in selected):
        selected = _hydrate_evidence(selected)
    bounded = BoundedRetriever(
        lambda pid, timeout: qdrant_scroll_by_arxiv(pid, limit=BUNDLE_CHUNK_LIMIT,
                                                   page=BUNDLE_CHUNK_LIMIT, timeout=timeout, deadline=deadline),
        deadline, time.monotonic)
    class AliasAwareRetriever:
        @property
        def calls(self):
            return bounded.calls

        def __call__(self, pid):
            requested_points = bounded(pid)
            requested_payloads = _payloads_for_id(requested_points, pid)
            covered = {"equations": False, "algorithms": False, "tables": False,
                       "limitations": False, "code": False}

            def mark(payloads):
                for payload in payloads:
                    if not payload.get("text"):
                        continue
                    category = {"equation": "equations", "algorithm": "algorithms",
                                "table": "tables", "code": "code"}.get(
                                    (payload.get("element_type") or "").lower())
                    if category:
                        covered[category] = True
                    if ((payload.get("section_type") or "").lower() == "limitations" or
                            "limitation" in (payload.get("section_title") or "").lower()):
                        covered["limitations"] = True
            mark(requested_payloads)
            if all(covered.values()):
                return requested_points

            aliases = _trusted_alias_ids(pid)
            if not aliases:
                return requested_points
            attempted, sources, errors = [pid], [], []

            def add_source(source_id, points, only_missing):
                payloads = _payloads_for_id(points, source_id)
                def fills_missing(p):
                    category = {"equation": "equations", "algorithm": "algorithms",
                                "table": "tables", "code": "code"}.get(
                                    (p.get("element_type") or "").lower())
                    section = (p.get("section_type") or "").lower()
                    title = (p.get("section_title") or "").lower()
                    return category in only_missing or (
                        "limitations" in only_missing and
                        (section == "limitations" or "limitation" in title))
                accepted = payloads if source_id == pid else [p for p in payloads if fills_missing(p)]
                if not accepted:
                    return []
                rows = _evidence_rows([source_id])
                sources.append({"id": source_id, "points": [point for point in points
                    if (point.get("payload") or {}) in accepted],
                    "metadata": rows.get(source_id) or {},
                    "passport": _paper_passport(source_id, accepted[0], rows)})
                return accepted

            exhausted = False
            if requested_payloads:
                valid_requested = [point for point in requested_points
                                   if (point.get("payload") or {}).get("arxiv_id") in (None, pid)]
                if valid_requested:
                    rows = _evidence_rows([pid])
                    sources.append({"id": pid, "points": valid_requested,
                                    "metadata": rows.get(pid) or {},
                                    "passport": _paper_passport(pid, requested_payloads[0], rows)})
            for alias in aliases:
                if all(covered.values()):
                    break
                if time.monotonic() >= deadline:
                    exhausted = True
                    break
                attempted.append(alias)
                need = {category for category, present in covered.items() if not present}
                try:
                    alias_points = bounded(alias)
                except Exception as error:
                    errors.append({"source_id": alias, "error": type(error).__name__})
                    if isinstance(error, TimeoutError) or time.monotonic() >= deadline:
                        exhausted = True
                    continue
                alias_payloads = add_source(alias, alias_points, need)
                mark(alias_payloads)
            resolution = {"requested_id": pid, "source_ids": [s["id"] for s in sources],
                          "attempted_ids": attempted,
                          "partial": bool(exhausted or errors or not all(covered.values())),
                          "time_budget_exhausted": exhausted, "errors": errors}
            return {"sources": sources, "resolution": resolution}

    retrieve = AliasAwareRetriever()
    return build_bundle(body, search_result, selected, retrieve)


def _paper_card(row, abstract_chars=EXPLORE_ABSTRACT_CHARS):
    abstract = (row.get("abstract") or "").strip()
    if abstract_chars is not None and len(abstract) > abstract_chars:
        cut = abstract.rfind(". ", 0, abstract_chars)
        abstract = abstract[:cut + 1] if cut > abstract_chars // 3 else abstract[:abstract_chars] + "..."
    pid = row["arxiv_id"]
    card = {"id": pid, "title": row.get("title"), "year": row.get("year"),
            "venue": row.get("venue"), "citation_count": row.get("citation_count"),
            "layers": row.get("layers"), "source": SOURCE_LABELS[source_of(pid)],
            "url": source_url(pid, row), "fulltext": None,
            "abstract": abstract}
    card = _hydrate_evidence([card], {pid: row})[0]
    others = [m for m in _twins()["members"].get(canonical_id(pid), []) if m != pid]
    if others:
        card["twins"] = others
    return card


def _check_layer(layer):
    if layer and layer not in EXPLORE_LAYERS:
        raise HTTPException(status_code=400, detail=f"layer must be one of {sorted(EXPLORE_LAYERS)}")


def _rank_papers(query, layer, year_from, year_to, pool_size):
    """The pool_size most relevant papers for a query, best first, one row per
    paper (twins folded into the best ranked copy), each with relevance_rank.
    Returns (rows, channel_diagnostics); diagnostics is None when both ranking
    channels completed successfully."""
    channel_diagnostics = {
        "dense": {"status": "ok", "error": None},
        "lexical": {"status": "ok", "error": None},
    }
    try:
        vector = None if COARSE_EMBED_URL else embed_query(query)
        dense = _coarse_paper_search(vector, layer, year_from, year_to,
                                     pool_size, timeout=10, query=query)
    except HTTPException as e:
        dense, dense_error = [], str(e.detail)
        channel_diagnostics["dense"] = {"status": "failed", "error": dense_error}
    try:
        lexical = _paper_bm25(query, layer, year_from, year_to, limit=pool_size)
    except (FuturesTimeoutError, sqlite3.Error) as e:
        lexical = []
        lexical_error = str(e) or type(e).__name__
        channel_diagnostics["lexical"] = {"status": "failed", "error": lexical_error}
    scores = reciprocal_rank_fusion([p["arxiv_id"] for p in dense], lexical,
                                    weights=[HIER_W_DENSE, HIER_W_LEX])
    fused = sorted(scores, key=lambda p: -scores[p])
    # rows first, twins second: a copy without a finished record must not
    # shadow the one that has it
    rows = _paper_rows(fused)
    pool, seen = [], set()
    for pid in fused:
        head = canonical_id(pid)
        if pid not in rows or head in seen:
            continue
        seen.add(head)
        pool.append(rows[pid])
        if len(pool) >= pool_size:
            break
    pool = _put_named_first(pool, query, year_from, year_to, pool_size)
    for n, row in enumerate(pool, start=1):
        row["relevance_rank"] = n
    if all(channel["status"] == "failed" for channel in channel_diagnostics.values()):
        if not pool:
            raise HTTPException(
                status_code=503,
                detail={"message": "paper ranking unavailable: dense and lexical channels failed",
                        "channels": channel_diagnostics},
            )
    diagnostics = (channel_diagnostics
                   if any(channel["status"] == "failed"
                          for channel in channel_diagnostics.values()) else None)
    return pool, diagnostics


def _put_named_first(pool, query, year_from, year_to, pool_size):
    """Papers the query names by id or title go first, whatever layer they are in."""
    named = _exact_matches(query)
    if not named:
        return pool
    rows = _paper_rows([d for d, _ in named])
    front = []
    for doc_id, via in named:
        row = rows.get(doc_id)
        if row is None:
            continue
        if year_from is not None and (row.get("year") or 0) < year_from:
            continue
        if year_to is not None and (row.get("year") or 9999) > year_to:
            continue
        row = dict(row)
        row["found_via"] = via
        front.append(row)
    return _place_named(front, pool)[:pool_size]


class PapersBody(BaseModel):
    query: str = Field(..., min_length=2, max_length=500)
    layer: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    sort: str = "relevance"
    limit: int = Field(default=10, ge=1, le=30)


@app.post("/v1/papers")
def find_papers(body: PapersBody, x_api_key: Optional[str] = Header(default=None)):
    """Papers on a subject, one row each, from the paper-level index (title +
    abstract embeddings fused with title/abstract BM25). sort=citations and
    sort=recent re-order the EXPLORE_POOL most relevant papers, so "the most
    cited papers about MEV" never means "the most cited papers that happen to
    say MEV once"."""
    auth_and_limit(x_api_key)
    _check_layer(body.layer)
    if body.sort not in EXPLORE_SORTS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {list(EXPLORE_SORTS)}")
    pool, channel_diagnostics = _rank_papers(body.query, body.layer, body.year_from, body.year_to,
                                             EXPLORE_POOL)
    if body.sort != "relevance":
        pool = pool[:EXPLORE_RESORT_POOL]
    if body.sort == "citations":
        pool.sort(key=lambda r: (-(r.get("citation_count") or 0), r["relevance_rank"]))
    elif body.sort == "recent":
        pool.sort(key=lambda r: (-(r.get("year") or 0), r["relevance_rank"]))
    elif body.sort == "foundational":
        pool = _foundational(pool, body)
    papers = []
    for row in pool[:body.limit]:
        card = _paper_card(row)
        card["relevance_rank"] = row.get("relevance_rank")
        if "cited_by_pool" in row:
            card["cited_by_pool"] = row["cited_by_pool"]
        if "cited_by_corpus" in row:
            card["cited_by_corpus"] = row["cited_by_corpus"]
        if row.get("found_via"):
            card["found_via"] = row["found_via"]
        papers.append(card)
    out = {"papers": papers, "count": len(papers), "sort": body.sort,
           "pool": len(pool), "scope": _query_scope(body.query), "usage": USAGE_NOTICE}
    if channel_diagnostics:
        out["partial"] = True
        out["channel_diagnostics"] = channel_diagnostics
        dense = channel_diagnostics["dense"]
        lexical = channel_diagnostics["lexical"]
        if dense["status"] == "failed" and lexical["status"] == "failed":
            out["note"] = "dense and lexical ranking unavailable; returning exact named-source matches only"
        elif dense["status"] == "failed":
            out["note"] = (f"dense paper index unavailable ({dense['error']}); "
                           "ranked by title/abstract terms only")
        else:
            out["note"] = (f"title/abstract paper index unavailable ({lexical['error']}); "
                           "ranked by dense similarity only")
    if body.sort in ("citations", "recent"):
        out["note_sort"] = (f"re-sorted from the {EXPLORE_RESORT_POOL} most relevant papers; "
                            "relevance_rank shows where each stood before")
    elif body.sort == "foundational":
        out["note_sort"] = (f"papers most cited by the {EXPLORE_RESORT_POOL} most relevant ones "
                            "(cited_by_pool); relevance_rank is null for those found only "
                            "through citations")
        if any("cited_by_pool" not in p and "cited_by_corpus" in p for p in papers):
            out["note_sort"] += ("; rows without cited_by_pool follow, ordered by how many papers "
                                 "in the whole corpus cite them (cited_by_corpus), then by "
                                 "citation_count, then by relevance: EIPs, whitepapers and code "
                                 "docs carry no citation data, so for them this is relevance order")
    return out


def _foundational(pool, body):
    """Rank by how many of the relevant papers cite each paper, adding cited
    papers that wording alone never surfaced. Year and layer filters still
    apply to what is returned."""
    ids = [r["arxiv_id"] for r in pool]
    if not ids:
        return pool
    sql = (f"SELECT dst, COUNT(*) AS n FROM citations WHERE src IN ({','.join('?' * len(ids))}) "
           f"GROUP BY dst HAVING n >= ? ORDER BY n DESC LIMIT 60")
    try:
        counts = dict(_ro_conn(STATE_DB_PATH).execute(sql, ids + [FOUNDATIONAL_MIN_CITERS]).fetchall())
    except sqlite3.Error:
        _ro_conn_drop(STATE_DB_PATH)
        return pool
    by_id = {r["arxiv_id"]: r for r in pool}
    extra = _paper_rows([d for d in counts if d not in by_id])
    merged = {}
    for pid, n in counts.items():
        row = by_id.get(pid) or extra.get(pid)
        if row is None:
            continue
        if body.layer and body.layer not in (row.get("layers") or []):
            continue
        if body.year_from is not None and (row.get("year") or 0) < body.year_from:
            continue
        if body.year_to is not None and (row.get("year") or 9999) > body.year_to:
            continue
        head = canonical_id(pid)
        if head in merged:
            merged[head]["cited_by_pool"] += n
            continue
        row = dict(row)
        row.setdefault("relevance_rank", None)
        row["cited_by_pool"] = n
        merged[head] = row
    ranked = sorted(merged.values(), key=lambda r: (-r["cited_by_pool"], -(r.get("citation_count") or 0)))
    named = [r for r in pool if r.get("found_via")]
    named_heads = {canonical_id(r["arxiv_id"]) for r in named}
    ranked = named + [r for r in ranked if canonical_id(r["arxiv_id"]) not in named_heads]
    # the pool itself is never dropped: papers nobody in the pool cites (and
    # documents with no citation data at all) follow, ordered by the citations
    # they receive from anywhere in the corpus, so the answer is never empty
    seen = {canonical_id(r["arxiv_id"]) for r in ranked}
    tail = [r for r in pool if canonical_id(r["arxiv_id"]) not in seen]
    if tail:
        tail_ids = [r["arxiv_id"] for r in tail]
        try:
            indegree = dict(_ro_conn(STATE_DB_PATH).execute(
                f"SELECT dst, COUNT(*) FROM citations WHERE dst IN ({','.join('?' * len(tail_ids))}) "
                "GROUP BY dst", tail_ids).fetchall())
        except sqlite3.Error:
            indegree = {}
        tail = [dict(r, cited_by_corpus=indegree.get(r["arxiv_id"], 0)) for r in tail]
        tail.sort(key=lambda r: (-r["cited_by_corpus"], -(r.get("citation_count") or 0),
                                 r.get("relevance_rank") or 0))
    return ranked + tail


OUTLINE_FIELDS = ["section_title", "section_type", "element_type", "chunk_index"]
OUTLINE_PAGE = 500


def _outline(payloads):
    """Sections in paper order with how many chunks of prose, math or tables each
    holds. Built from metadata only (OUTLINE_FIELDS), no chunk text."""
    order, sections = [], {}
    for p in sorted(payloads, key=lambda p: p.get("chunk_index", 0)):
        title = p.get("section_title") or "(untitled)"
        if title not in sections:
            sections[title] = {"section_title": title, "section_type": p.get("section_type"),
                               "chunks": 0, "elements": {}}
            order.append(title)
        s = sections[title]
        s["chunks"] += 1
        et = p.get("element_type") or "prose"
        s["elements"][et] = s["elements"].get(et, 0) + 1
    return [sections[t] for t in order]


# Ranked in SQL: a landmark paper has thousands of neighbours, and hydrating
# every one of them (abstracts included) just to keep the top 25 took 18 s.
# CROSS JOIN pins the order (citations by index, then papers by key); left to
# itself the planner scans every finished paper first (31 s).
_NEIGHBOUR_SQL = {
    "cites": ("SELECT p.arxiv_id, p.title, p.year, p.citation_count, COUNT(*) OVER() "
              "FROM citations c CROSS JOIN papers p ON p.arxiv_id = c.dst "
              "WHERE c.src = ? AND p.status = 'done' "
              "ORDER BY COALESCE(p.citation_count, 0) DESC LIMIT ?"),
    "cited_by": ("SELECT p.arxiv_id, p.title, p.year, p.citation_count, COUNT(*) OVER() "
                 "FROM citations c CROSS JOIN papers p ON p.arxiv_id = c.src "
                 "WHERE c.dst = ? AND p.status = 'done' "
                 "ORDER BY COALESCE(p.citation_count, 0) DESC LIMIT ?"),
}


def _neighbours(direction, paper_id):
    try:
        rows = _ro_conn(STATE_DB_PATH).execute(
            _NEIGHBOUR_SQL[direction], (paper_id, NEIGHBOUR_LIMIT)).fetchall()
    except sqlite3.Error:
        _ro_conn_drop(STATE_DB_PATH)
        return [], 0
    return ([{"id": r[0], "title": r[1], "year": r[2], "citation_count": r[3]} for r in rows],
            rows[0][4] if rows else 0)


# ---- counts and neighbours --------------------------------------------------

FACET_POOL = 200              # papers a query-scoped count is taken over
FACET_TERMS_SAMPLE = 40000    # papers whose matched_terms are read without a query
FACET_TOP_VENUES = 15
FACET_TOP_TERMS = 25
SIMILAR_SLACK = 3             # recommend this many times the limit, twins and gaps thin it


class FacetsBody(BaseModel):
    query: Optional[str] = Field(default=None, min_length=2, max_length=500)
    layer: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None


def _terms_of(raw):
    try:
        terms = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [t for t in terms if isinstance(t, str)] if isinstance(terms, list) else []


def _facet_source(body, pool_ids):
    """(id, year, venue, layers, matched_terms) of every paper counted: the
    ranked pool when there is a query, else all finished papers in the filter."""
    cols = "arxiv_id, year, venue, layers, matched_terms"
    if pool_ids is not None:
        parts = [pool_ids[i:i + 500] for i in range(0, len(pool_ids), 500)]
        sqls = [(f"SELECT {cols} FROM papers WHERE status='done' "
                 f"AND arxiv_id IN ({','.join('?' * len(part))})", part) for part in parts]
    else:
        where, params = ["status='done'"], []
        if body.layer:
            where.append("layers LIKE ?")
            params.append(f"%{body.layer}%")
        if body.year_from is not None:
            where.append("year >= ?")
            params.append(body.year_from)
        if body.year_to is not None:
            where.append("year <= ?")
            params.append(body.year_to)
        sqls = [(f"SELECT {cols} FROM papers WHERE {' AND '.join(where)}", params)]
    rows = []
    for sql, params in sqls:
        try:
            rows += _ro_conn(STATE_DB_PATH).execute(sql, params).fetchall()
        except sqlite3.Error as e:
            _ro_conn_drop(STATE_DB_PATH)
            raise HTTPException(status_code=503, detail=f"state db unavailable: {e}")
    return rows


@app.post("/v1/facets")
def paper_facets(body: FacetsBody, x_api_key: Optional[str] = Header(default=None)):
    """How much the corpus holds on a subject: paper count, papers per year,
    per layer, the venues and the technical terms they carry. With a query the
    count is taken over the FACET_POOL most relevant papers (the size of the
    neighbourhood, capped, not of every mention); without one it covers every
    paper in the layer and year window. Copies of one paper count once."""
    auth_and_limit(x_api_key)
    _check_layer(body.layer)
    out, top = {}, []
    if body.query:
        pool, channel_diagnostics = _rank_papers(body.query, body.layer, body.year_from, body.year_to,
                                                  FACET_POOL)
        rows = _facet_source(body, [r["arxiv_id"] for r in pool])
        top = [_paper_card(r) for r in pool[:5]]
        out["counted_over"] = (f"the {FACET_POOL} papers most relevant to the query"
                               if len(pool) >= FACET_POOL else "every paper the query reached")
        out["scope"] = _query_scope(body.query)
        if channel_diagnostics:
            out["partial"] = True
            out["channel_diagnostics"] = channel_diagnostics
            dense = channel_diagnostics["dense"]
            lexical = channel_diagnostics["lexical"]
            if dense["status"] == "failed" and lexical["status"] == "failed":
                out["note"] = "dense and lexical ranking unavailable; returning exact named-source matches only"
            elif dense["status"] == "failed":
                out["note"] = (f"dense paper index unavailable ({dense['error']}); "
                               "ranked by title/abstract terms only")
            else:
                out["note"] = (f"title/abstract paper index unavailable ({lexical['error']}); "
                               "ranked by dense similarity only")
    else:
        rows = _facet_source(body, None)
        out["counted_over"] = "every paper in the filter"
        out["scope"] = {"in_scope": True, "confidence": "high",
                        "reason": "no query: counts cover the index itself", "note": None}
    seen, years, layers, venues, kept = set(), {}, {}, {}, []
    for pid, year, venue, layer_csv, raw in rows:
        head = canonical_id(pid)
        if head in seen:
            continue
        seen.add(head)
        kept.append(raw)
        if year is not None:
            years[year] = years.get(year, 0) + 1
        for l in (layer_csv or "").split(","):
            if l:
                layers[l] = layers.get(l, 0) + 1
        if venue:
            venues[venue] = venues.get(venue, 0) + 1
    step = max(1, math.ceil(len(kept) / FACET_TERMS_SAMPLE))
    sample = kept[::step]
    counts = {}
    for raw in sample:
        for t in set(_terms_of(raw)):
            counts[t] = counts.get(t, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:FACET_TOP_TERMS]
    out.update({
        "total": len(kept),
        "by_year": dict(sorted(years.items())),
        "by_layer": dict(sorted(layers.items(), key=lambda kv: -kv[1])),
        "top_venues": [{"venue": v, "papers": n} for v, n in
                       sorted(venues.items(), key=lambda kv: (-kv[1], kv[0]))[:FACET_TOP_VENUES]],
        "top_terms": [{"term": t, "papers": n} for t, n in ranked],
    })
    if step > 1:
        out["sampled"] = len(sample)
        out["note_terms"] = f"top_terms counted over an even sample of {len(sample)} papers"
    if body.query:
        out["top_papers"] = top
    out["usage"] = USAGE_NOTICE
    return out


def _coarse_point_id(paper_id):
    """Id of the paper's point in the paper-level index, or None if absent."""
    body = {"filter": {"must": [{"key": "arxiv_id", "match": {"value": paper_id}}]},
            "limit": 1, "with_payload": False, "with_vector": False}
    try:
        resp = requests.post(f"{QDRANT_URL}/collections/{COARSE_COLLECTION}/points/scroll",
                             json=body, timeout=10)
        resp.raise_for_status()
        pts = resp.json()["result"]["points"]
    except (requests.RequestException, KeyError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"qdrant error: {e}")
    return pts[0]["id"] if pts else None


@app.get("/v1/paper/{paper_id:path}/similar")
def similar_papers(paper_id: str, limit: int = 10, layer: Optional[str] = None,
                   x_api_key: Optional[str] = Header(default=None)):
    """Papers nearest to this one in title+abstract embedding space, whether or
    not they share citations or wording with it. Copies of the paper itself
    are left out; copies of a neighbour count once."""
    auth_and_limit(x_api_key)
    _check_layer(layer)
    limit = max(1, min(limit, 30))
    head = canonical_id(paper_id)
    point = None
    for cand in dict.fromkeys([paper_id, head, *_twins()["members"].get(head, [])]):
        point = _coarse_point_id(cand)
        if point is not None:
            break
    if point is None:
        raise HTTPException(status_code=404, detail="paper is not in the paper-level index")
    body = {"positive": [point], "limit": limit * SIMILAR_SLACK + 10, "with_payload": ["arxiv_id"]}
    if layer:
        body["filter"] = {"must": [{"key": "layers", "match": {"any": [layer]}}]}
    try:
        resp = requests.post(f"{QDRANT_URL}/collections/{COARSE_COLLECTION}/points/recommend",
                             json=body, timeout=15)
        resp.raise_for_status()
        hits = resp.json()["result"]
    except (requests.RequestException, KeyError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"qdrant error: {e}")
    scored, seen = [], {head}
    for h in hits:
        aid = (h.get("payload") or {}).get("arxiv_id")
        cid = canonical_id(aid) if aid else None
        if not aid or cid in seen:
            continue
        seen.add(cid)
        scored.append((aid, h.get("score") or 0))
    rows = _paper_rows([a for a, _ in scored])
    papers = []
    for aid, score in scored:
        if aid not in rows:
            continue
        card = _paper_card(rows[aid])
        card["similarity"] = round(score, 4)
        papers.append(card)
        if len(papers) >= limit:
            break
    return {"id": paper_id, "papers": papers, "count": len(papers), "usage": USAGE_NOTICE}


def _load_canonical_section_manifest(paper_id):
    try:
        try:
            from pipeline.document_cache import load_section_manifest
        except ModuleNotFoundError:
            from document_cache import load_section_manifest
        state = _ro_conn(STATE_DB_PATH)
        row = state.execute("SELECT status FROM papers WHERE arxiv_id=?", (paper_id,)).fetchone()
        if not row or row[0] != "done":
            return None, "paper_not_done"
        root = Path(os.getenv("DTOX_DATA_DIR") or Path(STATE_DB_PATH).parent)
        cache_paths = {name: os.environ[setting] for name, setting in (
            ("cache_dir", "DTOX_CHUNK_CACHE_DIR"),
            ("section_cache_dir", "DTOX_SECTION_CACHE_DIR")) if os.getenv(setting)}
        manifest = load_section_manifest(root, paper_id, **cache_paths)
        row = state.execute("SELECT status FROM papers WHERE arxiv_id=?", (paper_id,)).fetchone()
        if not row or row[0] != "done":
            return None, "paper_not_done"
        return manifest, None
    except (ImportError, OSError, ValueError, TypeError, sqlite3.Error):
        return None, "canonical_manifest_unavailable_or_invalid"


def _canonical_section_view(paper_id, payloads, title, section_type):
    manifest, reason = _load_canonical_section_manifest(paper_id)
    if manifest is None:
        return None, None, reason
    sections = manifest["sections"]
    selected = [section for section in sections if (
        section["section_title"] == title if title else section["section_type"] == section_type)]
    if manifest["coverage"] == "examined_sections":
        covered = {(section["section_title"], section["section_type"]) for section in selected}
        requested = {(p.get("section_title") or "", p.get("section_type") or "other") for p in payloads
                     if (p.get("section_title") == title if title else p.get("section_type") == section_type)}
        if not selected or not requested.issubset(covered):
            return None, None, "section_not_fully_examined"
    return selected, manifest, None


@app.get("/v1/paper/{paper_id:path}/section")
def paper_section(paper_id: str, title: Optional[str] = None, section_type: Optional[str] = None,
                  offset: Annotated[int, Query(ge=0)] = 0, max_chars: int = SECTION_MAX_CHARS,
                  x_api_key: Optional[str] = Header(default=None)):
    """Text of one section, with explicit order verification, paged by characters. Pick the
    section by its exact title from /v1/paper/{id}, or by section_type
    (method, experiments, limitations, ...) for every section of that kind."""
    auth_and_limit(x_api_key)
    if not title and not section_type:
        raise HTTPException(status_code=400, detail="pass title or section_type")
    max_chars = max(500, min(max_chars, 20000))
    points = qdrant_scroll_by_arxiv(paper_id, limit=3000)
    if not points:
        raise HTTPException(status_code=404, detail="paper not found")
    payloads = sorted((pt["payload"] for pt in points), key=lambda p: p.get("chunk_index", 0))
    all_sections = _outline(payloads)
    available_sections = all_sections[:50]
    if title:
        chosen = [p for p in payloads if (p.get("section_title") or "") == title]
    else:
        chosen = [p for p in payloads if (p.get("section_type") or "") == section_type]
    canonical, manifest, order_reason = _canonical_section_view(paper_id, payloads, title, section_type)
    if canonical is not None:
        chosen = [dict(section, title=payloads[0].get("title")) for section in canonical]
    if not chosen:
        raise HTTPException(status_code=404, detail={
            "message": "no exact section match; no semantic substitution was made",
            "requested": {"title": title, "section_type": section_type},
            "available_sections": available_sections,
            "available_sections_total": len(all_sections),
            "available_sections_truncated": len(all_sections) > len(available_sections),
            "selection_hints": {
                "exact_title": "Pass one section_title exactly as listed in available_sections.",
                "section_type": "Pass an exact section_type from available_sections; all sections of that type are returned.",
                "outline": f"GET /v1/paper/{paper_id} for the paper outline.",
            },
        })
    text = "\n\n".join(p.get("text") or "" for p in chosen)
    piece = text[offset:offset + max_chars]
    end = offset + len(piece)
    fragment = {"section_title": title, "section_type": section_type, "text": piece}
    passport = _paper_passport(paper_id, fragment, _evidence_rows([paper_id]))
    passport["evidence"]["fragment_scope"] = "returned_section_page"
    passport["evidence"]["offset"] = offset
    passport["evidence"]["order_verified"] = canonical is not None
    order = {"verified": canonical is not None, "scope": manifest["coverage"] if manifest else "legacy_chunk_index"}
    if manifest:
        order.update(source_sha256=manifest["source_sha256"], cache_sha256=manifest["cache_sha256"],
                     parser_revision=manifest["parser_revision"])
    else:
        order["reason"] = order_reason
    return {"id": paper_id, "title": chosen[0].get("title"),
            **passport,
            "section_title": title, "section_type": section_type,
            "text": piece, "offset": offset, "next_offset": end if end < len(text) else None,
            "total_chars": len(text), "section_order": order, "usage": USAGE_NOTICE}


@app.get("/v1/paper/{paper_id:path}")
def paper_card(paper_id: str, x_api_key: Optional[str] = Header(default=None)):
    """One paper: metadata, full abstract, section outline, and its citation
    neighbours inside the corpus (what it cites, what cites it), most cited
    first. The entry point for reading a paper or walking to related work."""
    auth_and_limit(x_api_key)
    rows = _paper_rows([paper_id])
    if paper_id not in rows:
        head = canonical_id(paper_id)
        rows = _paper_rows([head])
        if head not in rows:
            raise HTTPException(status_code=404, detail="paper not in the corpus")
        paper_id = head
    card = _paper_card(rows[paper_id], abstract_chars=None)
    card["influential_citations"] = rows[paper_id].get("influential_citations")
    def outline_points():
        try:
            return qdrant_scroll_by_arxiv(paper_id, limit=3000, page=OUTLINE_PAGE,
                                          payload_fields=OUTLINE_FIELDS)
        except HTTPException:
            return []
    # the scroll and both citation lookups are independent; run them together
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_points = pool.submit(outline_points)
        f_cites = pool.submit(_neighbours, "cites", paper_id)
        f_cited = pool.submit(_neighbours, "cited_by", paper_id)
        points = f_points.result()
        card["cites"], card["cites_in_corpus"] = f_cites.result()
        card["cited_by"], card["cited_by_in_corpus"] = f_cited.result()
    card["outline"] = _outline([pt["payload"] for pt in points])
    card["indexed_chunks"] = len(points)
    card["usage"] = USAGE_NOTICE
    return card


@app.get("/v1/health")
def health():
    samples = list(search_latencies)
    return {
        "status": "ok",
        "optional_reranker": {"enabled": RERANK_ENABLED, **_rerank_runtime.snapshot()},
        "search_latency_seconds": {
            "samples": len(samples),
            "p50": _latency_percentile(samples, 0.50),
            "p95": _latency_percentile(samples, 0.95),
        },
    }
