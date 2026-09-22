#!/usr/bin/env python3
"""
dtox-research: durable arXiv research pipeline (harvest -> quality gate ->
fulltext -> chunk -> embed/upsert). Independent from the old dtox-harvester
service and academic_core/academic_core_v2 collections.

All progress lives in SQLite (WAL mode, committed transitions per step) so a
crash or reboot never restarts from zero: on start the service resumes every
paper from whatever status it was last committed at, and every harvest
cursor resumes from its saved next_start.
"""

import fcntl
import gzip
import hashlib
import html
import io
import json
import logging
import threading
import os
import random
import re
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

import extractor
import niche_filter

BASE_DIR = Path(os.getenv("DTOX_DATA_DIR", Path(__file__).resolve().parent))
DB_PATH = BASE_DIR / "state.db"
LOCK_FILE = BASE_DIR / "service.lock"
LOG_FILE = BASE_DIR / "pipeline.log"
LATEX_CACHE_DIR = BASE_DIR / "latex_cache"
EXTRACTOR_VERSION = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dtox-research")
logging.getLogger("pylatexenc").setLevel(logging.WARNING)  # tolerant-parsing notices are noise

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_DELAY_SECONDS = 3
ARXIV_FETCH_TIMEOUT = 60
ARXIV_FETCH_RETRIES = 5
PAGE_SIZE = 100
# arXiv refuses to paginate past ~10k results for one query: start=9900 answers,
# start=10000 returns "Rate exceeded" and start=12000 a 500. Ten of our queries
# had walked their cursor to exactly 10000 and were re-failing every cycle --
# five retries with backoff each, which is what was starving the harvester.
# A query at the wall is capped, not exhausted: its tail is only reachable by
# slicing the query into narrower date ranges.
ARXIV_MAX_START = 9900
DATE_SLICE_START_YEAR = 2015
DATE_SLICE_END_YEAR = 2026
ATOM_NS = "{http://www.w3.org/2005/Atom}"

S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_FIELDS = ("citationCount,influentialCitationCount,venue,year,publicationVenue,"
             "authors.affiliations")

# A paper three weeks old has no citations by definition, and in these fields
# that is exactly when it matters most. Two signals stand in for the citation
# count that cannot exist yet: how strongly the paper belongs to a niche, and
# whether it comes from a group whose output is worth reading on sight.
#
# Affiliation is a weak channel here and the code should not pretend otherwise:
# Semantic Scholar fills it for roughly one author in eight, so it admits
# papers when present and never rejects on absence.
SOTA_MAX_AGE_MONTHS = 12
SOTA_MIN_NICHE = 3
# A strong lexical fit is itself a quality signal in narrow fields where raw
# citation counts lag badly. These two lanes only run after the deterministic
# niche filter has accepted the paper; they do not weaken the off-topic gate.
STRONG_NICHE_AUTO_PASS = 6
NICHE_CITATION_PASS_SCORE = 3
NICHE_CITATION_PASS_MIN = 1
TOP_LABS = (
    "google", "deepmind", "google brain", "openai", "anthropic", "meta ai",
    "facebook ai", "microsoft research", "nvidia", "mistral", "cohere",
    "allen institute", "ai2", "stanford", "berkeley", "mit", "carnegie mellon",
    "princeton", "oxford", "cambridge", "eth zurich", "epfl", "tsinghua",
    "peking university", "ethereum foundation", "protocol labs", "a16z",
    "solana labs", "chainlink labs", "paradigm",
)


def is_top_lab(affiliations):
    joined = " ; ".join(a.lower() for a in (affiliations or []) if a)
    return any(lab in joined for lab in TOP_LABS)
S2_BATCH_SIZE = 500
# Optional API key: unblocks the shared/unauthenticated S2 rate pool. Read from
# env only (set via systemd EnvironmentFile=-/opt/dtox-research/s2.env, which
# is intentionally NOT created/populated by this code -- the operator owns the
# key). Absent by default, in which case behavior is byte-for-byte unchanged
# from before (no header sent, same pause).
S2_API_KEY = (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
S2_PAUSE = 1.2 if S2_API_KEY else 1.5  # limit is 1 req/s cumulative: stay just under it

# Three stages now talk to Semantic Scholar -- quality, recheck and the
# reference crawl -- and the 1 req/s limit is cumulative across all of them, so
# each sleeping politely on its own still adds up to three requests a second.
# One gate paces every S2 call in the process, and a 429 anywhere holds all of
# them back.
S2_PENALTY_SECONDS = 30

# The lexical index lives beside the vector one and has to be written at the
# same moment, or hybrid search silently degrades to dense-only for everything
# harvested after the initial build.
FTS_DB_PATH = os.getenv("FTS_DB_PATH", str(BASE_DIR / "fts.db"))

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "papers_fulltext")
EMBED_BATCH_URL = os.getenv("EMBED_BATCH_URL", "http://127.0.0.1:8005/embed_batch")
EMBED_TIMEOUT = 90
UPSERT_BATCH_SIZE = 64

# --- speed tuning (fulltext/chunk/embed only; harvest/quality untouched) ---
FULLTEXT_CYCLE_LIMIT = 150       # quality_checked papers considered per cycle
FULLTEXT_WORKERS = 4             # concurrent e-print downloads; 10 made arXiv rate-limit the whole IP
FULLTEXT_MIN_JITTER = 0.15
FULLTEXT_MAX_JITTER = 0.6
FULLTEXT_MAX_RETRIES = 3         # retries specifically for 429/503 with backoff
ARXIV_CONTACT_UA = "dtox-research/1.1 (research pipeline; mailto:danik1900@gmail.com)"


# --- arXiv traffic gate -----------------------------------------------------
# arXiv rate-limits per IP across everything we ask of it, so the catalogue API
# and the e-print downloads were competing: bursts of FULLTEXT_WORKERS parallel
# downloads earned a 429 that also killed harvest, and the harvester sat idle
# with its cursor parked. One gate now paces every arXiv request in the process,
# and a 429 anywhere pauses all of them long enough for the limit to clear.
ARXIV_MIN_INTERVAL = 1.5     # seconds between any two arXiv requests
ARXIV_PENALTY_SECONDS = 60   # global cooldown after a 429/503 from arXiv


class _ArxivGate:
    def __init__(self, min_interval):
        self._lock = threading.Lock()
        self._min = min_interval
        self._next = 0.0

    def wait(self):
        """Claim the next send slot, then sleep outside the lock."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._min
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def penalize(self, seconds=ARXIV_PENALTY_SECONDS):
        """Back off every arXiv caller, not just the one that got refused."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


arxiv_gate = _ArxivGate(ARXIV_MIN_INTERVAL)
s2_gate = _ArxivGate(S2_PAUSE)

CHUNK_CYCLE_LIMIT = 200          # local CPU work, no external rate limits

EMBED_CYCLE_LIMIT = 20          # bound commit latency; throughput still comes from 64-chunk batches
                                  # bounds how much work one cycle looks at, not how long until progress lands)
# Two 64-text requests can overlap at full 512-token padding and push the
# shared ONNX arena past the container's 9 GiB limit. 32 keeps the same two
# callers and CPU saturation while bounding peak memory.
EMBED_BATCH_TEXTS = 32
EMBED_WORKERS = 2
                                  # (raised container limit 2GiB->4GiB; keep this at 1 to avoid concurrent memory spikes OOM-killing it)

NAMESPACE_URL = uuid.NAMESPACE_URL

# ---------------------------------------------------------------------------
# arXiv query design (rewritten to fix off-niche pollution)
#
# Previous version harvested bare `cat:cs.CL`/`cat:cs.LG`/`cat:cs.AI`/`cat:cs.MA`
# date-sliced with NO keyword filter, which pulled in speech/vision/tabular/
# graph papers that just happen to live in those categories, then mislabeled
# them llm-slm/ai-agents by whichever query found them first. Now every query
# is a boolean `(cat:... ) AND (all:"phrase" OR all:"phrase" ...)` so arXiv
# itself only returns niche-relevant results. The lexical gate in
# niche_filter.py is still applied on top at insert time as a second,
# deterministic check (belt and suspenders, and it also fixes layer labels).
# ---------------------------------------------------------------------------

BOOLEAN_QUERIES = {
    "llm-slm": [
        '(cat:cs.CL OR cat:cs.LG) AND (all:"large language model" OR all:"small language model" '
        'OR all:"retrieval augmented" OR all:"retrieval-augmented" OR all:"in-context learning" '
        'OR all:"instruction tuning" OR all:"mixture of experts" OR all:"prompt compression" '
        'OR all:"chain of thought")',
        '(cat:cs.CL OR cat:cs.LG) AND (all:"long context" OR all:"context window" OR all:"prompt tuning" '
        'OR all:"decoder-only" OR all:"encoder-decoder language model" OR all:"LLM quantization" '
        'OR all:"knowledge distillation language model" OR all:"state space model mamba")',
    ],
    # cs.CR is where cryptography that ships with LaTeX sources lives. IACR
    # ePrint has more crypto but serves abstracts only (PDFs sit behind a
    # Cloudflare challenge), so this is the one path to full text for the
    # web3 layer: ~4.5k blockchain/ZK papers, complete with algorithms.
    "web3": [
        '(cat:cs.CR OR cat:cs.DC) AND (all:blockchain OR all:"smart contract" '
        'OR all:"zero knowledge" OR all:"consensus protocol" OR all:"distributed ledger")',
        '(cat:cs.CR OR cat:cs.DC) AND (all:"threshold signature" OR all:"multiparty computation" '
        'OR all:"distributed key generation" OR all:"verifiable delay function" '
        'OR all:"byzantine fault" OR all:"proof of stake")',
        '(cat:cs.CR OR cat:cs.DC) AND (all:rollup OR all:"layer 2" OR all:"payment channel" '
        'OR all:"state channel" OR all:"cross-chain" OR all:"decentralized finance" OR all:mev)',
    ],
    "ai-agents": [
        '(cat:cs.AI OR cat:cs.CL OR cat:cs.MA) AND (all:"LLM agent" OR all:"language model agent" '
        'OR all:"llm-based agent" OR all:"tool use" OR all:"function calling" OR all:"agentic" '
        'OR all:"multi-agent")',
        '(cat:cs.AI OR cat:cs.CL OR cat:cs.MA) AND (all:"autonomous agent" OR all:"reasoning and acting" '
        'OR all:"web agent" OR all:"browser agent" OR all:"agent memory" OR all:"agent planning" '
        'OR all:"tool-augmented")',
    ],
}

# Plain keyword ("all:phrase") queries, no category restriction needed because
# the phrases themselves are already niche-specific.
LLM_SLM_QUERIES = [
    "GPT-4 language model",
    "LLaMA language model",
    "Mistral language model",
    "Qwen language model",
]

AI_AGENTS_QUERIES = [
    "autonomous agents benchmark",
    "agentic workflow orchestration",
]

WEB3_QUERIES = [
    "blockchain consensus",
    "smart contract security",
    "solana blockchain",
    "decentralized finance",
    "zero knowledge proof blockchain",
    "MEV blockchain",
    "ethereum scaling rollup",
    "DAO governance blockchain",
    "cross-chain bridge security",
    "blockchain oracle",
    "decentralized exchange automated market maker",
    "smart contract formal verification",
    "proof of stake protocol",
    "stablecoin mechanism",
]

ALLOWED_LAYERS = {"llm-slm", "ai-agents", "web3"}

# ---------------------------------------------------------------------------
# IACR ePrint (second source, web3 layer)
#
# arXiv carries little serious cryptography: ZK proof systems, consensus
# protocols, threshold signatures and MPC are published on eprint.iacr.org
# instead, which is why the web3 layer stayed ~20x smaller than llm-slm.
# IACR exposes OAI-PMH, so metadata (title, abstract, authors, category)
# comes back structured. There are no LaTeX sources there, only PDFs, so
# these papers ride the existing abstract-only path.
# ---------------------------------------------------------------------------
IACR_OAI_URL = "https://eprint.iacr.org/oai"
# ACL Anthology. Measured before building this: 89 424 papers from 2015 on,
# 50 102 of them absent from the index, abstracts present on 100% of records and
# PDFs served without a challenge. Unlike arXiv it is peer-reviewed proceedings,
# so the venue alone answers the question the citation gate exists to ask.
ACL_ID_PREFIX = "acl:"
ACL_START_YEAR = 2015
ACL_TREE_URL = ("https://api.github.com/repos/acl-org/acl-anthology/"
                "git/trees/master?recursive=1")
ACL_XML_BASE = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/"
ACL_PDF_BASE = "https://aclanthology.org/"

# OpenAlex is the discovery layer for peer-reviewed Web3 work that never lands
# on arXiv. Only open-access works with a direct PDF are admitted; the PDF URL
# stays attached to the row so fulltext can be fetched without guessing a
# publisher route. Queries are deliberately Web3-specific and the normal niche
# classifier remains the final gate.
OPENALEX_ID_PREFIX = "oa:"
OPENALEX_API_URL = "https://api.openalex.org/works"
OPENALEX_START_YEAR = 2015
OPENALEX_PAGE_SIZE = 100
OPENALEX_MAX_PAGES = 100
FRESH_WINDOW_DAYS = 5
FRESH_POLL_SECONDS = 60 * 60
SPEC_REFRESH_SECONDS = 24 * 60 * 60
OPENALEX_FRESH_MAX_PAGES = 10
OPENALEX_QUERIES = (
    "zero knowledge proof blockchain", "zk rollup", "blockchain rollup",
    "maximal extractable value blockchain", "smart contract security",
    "blockchain consensus protocol", "threshold signature blockchain",
    "multiparty computation blockchain", "decentralized finance protocol",
    "state channel blockchain", "verifiable delay function blockchain",
    "proof of stake blockchain", "account abstraction blockchain",
    "cross chain bridge security", "decentralized sequencer",
    "blockchain data availability", "polynomial commitment blockchain",
    "recursive snark", "zero knowledge virtual machine", "solana svm",
    "durable nonce solana", "transaction ordering blockchain",
    "blockchain oracle", "automated market maker blockchain",
)
openalex_gate = _ArxivGate(1.0)

# HAL is an open academic archive with direct PDF links.  It is especially
# useful for applied cryptography and protocol-engineering work that never
# reaches arXiv.  Unlike OpenAlex, its search API is public and does not need
# a key; the ordinary lexical gate is still the first relevance filter.
HAL_ID_PREFIX = "hal:"
HAL_API_URL = "https://api.archives-ouvertes.fr/search/"
HAL_PAGE_SIZE = 100
HAL_MAX_PAGES = 20
HAL_MIN_NICHE_AUTO_PASS = 6
HAL_QUERIES = (
    "zero knowledge proof blockchain", "zk rollup blockchain", "blockchain consensus",
    "smart contract security", "threshold signature blockchain", "multiparty computation blockchain",
    "verifiable delay function blockchain", "proof of stake blockchain", "state channel blockchain",
    "cross chain bridge security", "decentralized finance blockchain", "maximal extractable value blockchain",
    "polynomial commitment blockchain", "recursive snark blockchain", "zero knowledge virtual machine",
)
HAL_CURSOR_PREFIX = "hal-v2@"
hal_gate = _ArxivGate(1.0)

# Proceedings of Machine Learning Research: peer-reviewed, openly hosted PDF
# proceedings (ICML, AISTATS, CoRL and many workshops).  It supplies a large
# historical pool outside arXiv and is especially useful for LLM/agent methods.
PMLR_ID_PREFIX = "pmlr:"
PMLR_INDEX_URL = "https://proceedings.mlr.press/"
PMLR_MIN_VOLUME = 100
PMLR_REFRESH_VOLUMES = 5

IACR_ID_PREFIX = "iacr:"
IACR_DELAY_SECONDS = 3
IACR_START_YEAR = 2016
IACR_MAX_PAGES_PER_MONTH = 20  # OAI paginates via resumptionToken; bound the walk
OAI_NS = {
    "o": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


# ---------------------------------------------------------------------------
# Protocol specifications (EIP, Solana SIMD) -- third and fourth sources
#
# arXiv is thin on blockchain and IACR gives abstracts only (their PDFs sit
# behind a Cloudflare challenge), so the web3 layer stalled at ~7% of the base.
# Protocol specs are the primary source practitioners implement against, and
# unlike papers they are published in full as markdown.
# ---------------------------------------------------------------------------
EIP_ID_PREFIX = "eip:"
SIMD_ID_PREFIX = "simd:"
GITHUB_DOC_ID_PREFIX = "gh:"
SPEC_SOURCES = {
    "eip@all": {
        "prefix": EIP_ID_PREFIX,
        "list_url": "https://api.github.com/repos/ethereum/EIPs/contents/EIPS",
        "raw_base": "https://raw.githubusercontent.com/ethereum/EIPs/master/EIPS/",
        "file_re": re.compile(r"^eip-(\d+)\.md$", re.I),
        "source": "eip",
    },
    "simd@all": {
        "prefix": SIMD_ID_PREFIX,
        "list_url": "https://api.github.com/repos/solana-foundation/solana-improvement-documents/contents/proposals",
        "raw_base": "https://raw.githubusercontent.com/solana-foundation/solana-improvement-documents/main/proposals/",
        "file_re": re.compile(r"^(\d+)-.*\.md$"),
        "source": "simd",
    },
}
SPEC_FILES_PER_CYCLE = 40
SPEC_DELAY_SECONDS = 0.4
# Withdrawn/Stagnant proposals describe roads not taken; keeping them would let
# an agent implement against a dead standard.
SPEC_SKIP_STATUSES = {"withdrawn", "stagnant"}

# Curated technical documentation from canonical protocol and ZK repositories.
# This is intentionally a manifest, not GitHub-wide search: every repository
# is a first-party implementation or specification, and only documentation
# paths are eligible.  GitHub's tree API is used once per repo per process;
# raw content downloads then bypass the low unauthenticated API quota.
GITHUB_DOC_SOURCES = {
    "eth-consensus": {"repo": "ethereum/consensus-specs", "branch": "master", "paths": ("specs/",)},
    "eth-execution": {"repo": "ethereum/execution-specs", "branch": "forks/amsterdam", "paths": ("docs/",)},
    "op-specs": {"repo": "ethereum-optimism/specs", "branch": "main", "paths": ("specs/",)},
    "nitro": {"repo": "OffchainLabs/nitro", "branch": "master", "paths": ("docs/decisions/",)},
    "wormhole": {"repo": "wormhole-foundation/wormhole", "branch": "main", "paths": ("docs/",)},
    "hyperlane": {"repo": "hyperlane-xyz/hyperlane-monorepo", "branch": "main", "paths": ("docs/",)},
    "sp1": {"repo": "succinctlabs/sp1", "branch": "main", "paths": ("docs/", "crates/recursion/")},
    "risc0": {"repo": "risc0/risc0", "branch": "main", "paths": ("risc0/zkvm/", "website/api/blockchain-integration/")},
    "halo2": {"repo": "privacy-scaling-explorations/halo2", "branch": "main", "paths": ("book/",)},
    "anchor": {"repo": "coral-xyz/anchor", "branch": "master", "paths": ("docs/content/docs/",)},
}
GITHUB_DOCS_PER_CYCLE = 20
GITHUB_DOC_DELAY_SECONDS = 0.2
GITHUB_DOC_REFRESH_SECONDS = 24 * 60 * 60
_github_tree_cache = {}


WHITEPAPER_ID_PREFIX = "wp:"


def is_spec_source(paper_id) -> bool:
    """True for documents that are plain text or markdown rather than LaTeX:
    protocol specifications and the hand-curated industry whitepapers."""
    return str(paper_id).startswith((EIP_ID_PREFIX, SIMD_ID_PREFIX, GITHUB_DOC_ID_PREFIX,
                                     WHITEPAPER_ID_PREFIX))


def is_non_arxiv(paper_id) -> bool:
    """True for sources Semantic Scholar does not index (IACR, EIP, SIMD)."""
    return str(paper_id).startswith((IACR_ID_PREFIX, EIP_ID_PREFIX, SIMD_ID_PREFIX,
                                     GITHUB_DOC_ID_PREFIX,
                                     ACL_ID_PREFIX, OPENALEX_ID_PREFIX,
                                     PMLR_ID_PREFIX))


def _parse_front_matter(text):
    """Minimal YAML front-matter reader (key: value between --- fences)."""
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


def _spec_body(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def spec_page_url(cfg, number, name):
    if cfg["source"] == "eip":
        return f"https://eips.ethereum.org/EIPS/eip-{number}"
    return ("https://github.com/solana-foundation/solana-improvement-documents"
            f"/blob/main/proposals/{name}")


def spec_list_files(cfg):
    """One GitHub API call for the directory listing. Raw file downloads
    afterwards do not count against the 60/hour unauthenticated API budget."""
    try:
        req = urllib.request.Request(cfg["list_url"], headers={"User-Agent": ARXIV_CONTACT_UA})
        with urllib.request.urlopen(req, timeout=60) as resp:
            entries = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning(f"spec: listing failed for {cfg['list_url']}: {e}")
        return None
    if not isinstance(entries, list):
        log.warning(f"spec: unexpected listing payload for {cfg['list_url']}")
        return None
    return sorted(e["name"] for e in entries
                  if isinstance(e, dict) and e.get("type") == "file"
                  and cfg["file_re"].match(e.get("name", "")))


def fetch_spec_batch(query_key, start, count):
    """Fetch a slice of protocol specs.

    Returns (entries, total_files); (None, None) on a transient failure so the
    cursor stays put and the slice is retried, never silently skipped.
    """
    cfg = SPEC_SOURCES[query_key]
    names = spec_list_files(cfg)
    if names is None:
        return None, None
    entries = []
    for name in names[start:start + count]:
        match = cfg["file_re"].match(name)
        number = match.group(1)
        try:
            req = urllib.request.Request(cfg["raw_base"] + name,
                                         headers={"User-Agent": ARXIV_CONTACT_UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.warning(f"spec: fetch failed for {name}: {e}")
            continue
        time.sleep(SPEC_DELAY_SECONDS)
        meta = _parse_front_matter(text)
        if (meta.get("status") or "").strip().lower() in SPEC_SKIP_STATUSES:
            continue
        year = None
        m = re.search(r"((?:19|20)\d{2})", meta.get("created") or "")
        if m:
            year = int(m.group(1))
        body = _spec_body(text)
        entries.append({
            "arxiv_id": f"{cfg['prefix']}{number}",
            "title": meta.get("title") or f"{cfg['source'].upper()}-{number}",
            "year": year,
            "abstract": (meta.get("description") or body.strip()[:800]).strip(),
            "full_text": text,
            "url": spec_page_url(cfg, number, name),
            "source": cfg["source"],
        })
    return entries, len(names)


def github_doc_files(source_key):
    """Return the allowed Markdown paths for one curated repository.

    Tree results are cached for the process lifetime.  A restart merely costs
    one authenticated-free GitHub API call per curated repository, while raw
    files are fetched from raw.githubusercontent.com.
    """
    cached = _github_tree_cache.get(source_key)
    if cached is not None:
        return cached
    cfg = GITHUB_DOC_SOURCES[source_key]
    branch = urllib.parse.quote(cfg["branch"], safe="")
    url = f"https://api.github.com/repos/{cfg['repo']}/git/trees/{branch}?recursive=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ARXIV_CONTACT_UA})
        with urllib.request.urlopen(req, timeout=60) as resp:
            tree = json.loads(resp.read().decode("utf-8", errors="replace")).get("tree") or []
    except Exception as e:
        log.warning(f"github_docs: tree failed for {cfg['repo']}: {e}")
        return None
    paths = sorted(
        item.get("path") for item in tree
        if isinstance(item, dict) and item.get("type") == "blob"
        and str(item.get("path", "")).lower().endswith((".md", ".mdx"))
        and any(str(item.get("path", "")).startswith(prefix) for prefix in cfg["paths"])
    )
    _github_tree_cache[source_key] = paths
    return paths


def _github_doc_title(text, path):
    match = re.search(r"^\s*#\s+(.+?)\s*$", text, re.M)
    return " ".join((match.group(1) if match else Path(path).stem.replace("-", " ")).split())


def fetch_github_doc_batch(source_key, start, count):
    """Fetch one durable slice of first-party technical Markdown."""
    cfg = GITHUB_DOC_SOURCES[source_key]
    paths = github_doc_files(source_key)
    if paths is None:
        return None, None
    entries = []
    raw_base = f"https://raw.githubusercontent.com/{cfg['repo']}/{cfg['branch']}/"
    page_base = f"https://github.com/{cfg['repo']}/blob/{cfg['branch']}/"
    for path in paths[start:start + count]:
        try:
            req = urllib.request.Request(raw_base + path, headers={"User-Agent": ARXIV_CONTACT_UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.warning(f"github_docs: fetch failed for {cfg['repo']}:{path}: {e}")
            continue
        if len(text.strip()) < 200 or len(text) > 500_000:
            continue
        digest = hashlib.sha256(f"{cfg['repo']}:{path}".encode()).hexdigest()[:16]
        title = _github_doc_title(text, path)
        entries.append({
            "arxiv_id": f"{GITHUB_DOC_ID_PREFIX}{source_key}:{digest}",
            "title": title,
            "year": None,
            "abstract": text[:1200],
            "full_text": text,
            "url": page_base + path,
        })
        time.sleep(GITHUB_DOC_DELAY_SECONDS)
    return entries, len(paths)


def github_docs_step(conn):
    """Incrementally ingest selected protocol/ZK docs without GitHub search."""
    if _poll_due(conn, "github_docs:refresh", GITHUB_DOC_REFRESH_SECONDS):
        conn.execute("UPDATE harvest_cursor SET next_start=0,done=0,last_run_at=NULL "
                     "WHERE query_key LIKE 'ghdoc@%'")
        conn.commit()
        _mark_polled(conn, "github_docs:refresh")
    for key in GITHUB_DOC_SOURCES:
        conn.execute("INSERT OR IGNORE INTO harvest_cursor "
                     "(query_key,next_start,done,layer) VALUES (?,0,0,'web3')", (f"ghdoc@{key}",))
    conn.commit()
    row = conn.execute("SELECT query_key,next_start FROM harvest_cursor WHERE query_key LIKE 'ghdoc@%' "
                       "AND done=0 ORDER BY COALESCE(last_run_at,''),query_key LIMIT 1").fetchone()
    if row is None:
        return 0
    key = row["query_key"].split("@", 1)[1]
    start = int(row["next_start"] or 0)
    entries, total = fetch_github_doc_batch(key, start, GITHUB_DOCS_PER_CYCLE)
    if entries is None:
        return 0
    for entry in entries:
        upsert_discovered(conn, entry["arxiv_id"], entry["title"], entry["year"], "web3",
                          abstract=entry["abstract"], source_url=entry["url"], commit=False)
        write_latex_cache(entry["arxiv_id"], entry["full_text"])
    next_start = start + GITHUB_DOCS_PER_CYCLE
    conn.execute("UPDATE harvest_cursor SET next_start=?,done=?,last_run_at=? WHERE query_key=?",
                 (next_start, 1 if next_start >= total else 0, now_iso(), row["query_key"]))
    conn.commit()
    log.info(f"github_docs: {key} {start}-{next_start} of {total} -> {len(entries)} docs")
    return len(entries)


def iacr_month_keys() -> list:
    keys = []
    for year in range(IACR_START_YEAR, DATE_SLICE_END_YEAR + 1):
        for month in range(1, 13):
            keys.append(f"iacr@{year}-{month:02d}")
    return keys


def _iacr_parse_records(xml_text):
    """Returns (entries, resumption_token). Entry shape matches the arXiv path."""
    root = ET.fromstring(xml_text)
    entries = []
    for rec in root.findall(".//o:record", OAI_NS):
        # the OAI id ("oai:eprint.iacr.org:2025/1040") lives in the record
        # header; dc:identifier holds the public https URL instead
        header_id = rec.find("o:header/o:identifier", OAI_NS)
        eprint_id = None
        if header_id is not None and header_id.text:
            eprint_id = header_id.text.strip().split(":")[-1]  # "2025/1040"
        if not eprint_id:
            for e in rec.findall(".//dc:identifier", OAI_NS):
                if e.text and "eprint.iacr.org/" in e.text:
                    eprint_id = e.text.rstrip("/").split("eprint.iacr.org/")[-1]
                    break
        if not eprint_id or "/" not in eprint_id:
            continue
        title_el = rec.find(".//dc:title", OAI_NS)
        desc_el = rec.find(".//dc:description", OAI_NS)
        date_el = rec.find(".//dc:date", OAI_NS)
        title = (title_el.text or "").strip() if title_el is not None else ""
        if not title:
            continue
        abstract = (desc_el.text or "").strip() if desc_el is not None else ""
        year = None
        if date_el is not None and date_el.text:
            try:
                year = int(date_el.text[:4])
            except ValueError:
                year = None
        if year is None:
            try:
                year = int(eprint_id.split("/")[0])
            except ValueError:
                year = None
        entries.append({
            "arxiv_id": f"{IACR_ID_PREFIX}{eprint_id}",
            "title": title,
            "year": year,
            "abstract": abstract,
        })
    token_el = root.find(".//o:resumptionToken", OAI_NS)
    token = token_el.text.strip() if token_el is not None and token_el.text else None
    return entries, token


def fetch_iacr_month(query_key: str):
    """Fetch one month of IACR records. Returns list of entries, or None on failure
    (None means 'retry later', matching fetch_page's contract, so a network blip
    never marks the month exhausted)."""
    from calendar import monthrange

    try:
        _, year_month = query_key.split("@", 1)
        year_s, month_s = year_month.split("-")
        year, month = int(year_s), int(month_s)
    except ValueError:
        log.warning(f"iacr: bad query key {query_key}")
        return []

    last_day = monthrange(year, month)[1]
    base = (
        f"{IACR_OAI_URL}?verb=ListRecords&metadataPrefix=oai_dc"
        f"&from={year:04d}-{month:02d}-01&until={year:04d}-{month:02d}-{last_day:02d}"
    )
    entries, token, url = [], None, base
    for _page in range(IACR_MAX_PAGES_PER_MONTH):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ARXIV_CONTACT_UA})
            with urllib.request.urlopen(req, timeout=60) as response:
                xml_text = response.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.warning(f"iacr: fetch error for {query_key}: {e}")
            return None if not entries else entries
        try:
            page_entries, token = _iacr_parse_records(xml_text)
        except ET.ParseError as e:
            log.warning(f"iacr: XML parse error for {query_key}: {e}")
            return None if not entries else entries
        entries.extend(page_entries)
        if not token:
            break
        url = f"{IACR_OAI_URL}?verb=ListRecords&resumptionToken={urllib.parse.quote(token)}"
        time.sleep(IACR_DELAY_SECONDS)
    return entries


# Whole categories, swept year by year. The keyword and boolean queries were
# built to find papers already known to be on subject, and they worked: 534
# cursors, every one of them exhausted. What they cannot do is turn up a paper
# nobody thought to name. Measured totals: cs.LG 279k, cs.AI 192k, cs.CL 115k,
# cs.CR 51k, cs.DC 29k, cs.SE 28k, cs.IR 26k, cs.MA 13k -- about 734k records
# against the 212k harvested so far.
#
# Most of that is off subject and will be dropped, which is the point: the
# lexical niche filter is local and free, so a category sweep costs arXiv
# requests and nothing else. The layer a category is filed under only balances
# the round-robin scheduler; upsert_discovered classifies each paper against
# all three niches regardless of which query found it.
CATEGORY_SWEEP = {
    "llm-slm": ["cs.CL", "cs.LG", "cs.IR"],
    "ai-agents": ["cs.AI", "cs.MA", "cs.SE"],
    "web3": ["cs.CR", "cs.DC"],
}


def category_sweep_keys(layer: str) -> list:
    slices = ["pre"] + [str(y) for y in range(DATE_SLICE_START_YEAR, DATE_SLICE_END_YEAR + 1)]
    return [f"cat:{cat}@{suffix}"
            for cat in CATEGORY_SWEEP.get(layer, []) for suffix in slices]


def bool_date_slice_keys(layer: str, idx: int) -> list:
    return [f"bq:{layer}:{idx}@{year}" for year in range(DATE_SLICE_START_YEAR, DATE_SLICE_END_YEAR + 1)]


def build_layer_query_groups() -> list:
    groups = []
    for layer, keyword_queries in (
        ("llm-slm", LLM_SLM_QUERIES),
        ("ai-agents", AI_AGENTS_QUERIES),
        ("web3", WEB3_QUERIES),
    ):
        sliced = []
        for idx in range(len(BOOLEAN_QUERIES.get(layer, []))):
            sliced.extend(bool_date_slice_keys(layer, idx))
        extra = (iacr_month_keys() + list(SPEC_SOURCES)) if layer == "web3" else []
        groups.append((layer, sliced + list(keyword_queries) + extra
                       + category_sweep_keys(layer)))
    return groups


LAYER_QUERY_GROUPS = build_layer_query_groups()


def base_category_of(query_key: str) -> str:
    return query_key.split("@", 1)[0] if "@" in query_key else query_key


def _slice_clause(suffix: str) -> str:
    """submittedDate filter for a slice suffix: 'pre', 'YYYY' or 'YYYY-MM'."""
    if suffix == "pre":
        return f"submittedDate:[199101010000 TO {DATE_SLICE_START_YEAR - 1}12312359]"
    if "-" in suffix:
        from calendar import monthrange
        year_s, month_s = suffix.split("-", 1)
        year, month = int(year_s), int(month_s)
        last_day = monthrange(year, month)[1]
        return (f"submittedDate:[{year:04d}{month:02d}010000 TO "
                f"{year:04d}{month:02d}{last_day:02d}2359]")
    return f"submittedDate:[{suffix}01010000 TO {suffix}12312359]"


def _loose_terms(text: str) -> str:
    """Words ANDed field by field, the way the unsliced query behaved.

    `all:GPT-4 language model` looks like one term but arXiv only reads the
    first word as the field's value and then mis-parses the rest, which silently
    voided the date filter -- a March 2024 slice answered with a 1990 paper.
    Quoting it as a phrase parses fine but is far stricter than the query was
    before slicing: "GPT-4 language model" matches nothing verbatim. Spelling
    the AND out keeps the original meaning and still leaves the date clause
    where arXiv can see it.
    """
    words = [w for w in text.split() if w]
    return " AND ".join(f"all:{w}" for w in words) or f"all:{text}"


def resolve_search_query(query_key: str) -> str:
    if query_key.startswith("bq:") and "@" in query_key:
        left, suffix = query_key.split("@", 1)
        _, layer, idx = left.split(":", 2)
        base = BOOLEAN_QUERIES[layer][int(idx)]
        return f"{base} AND {_slice_clause(suffix)}"
    # kw:<layer>:<text>@<slice> -- a plain keyword query that ran into arXiv's
    # pagination wall and was split into narrower date ranges
    if query_key.startswith("kw:") and "@" in query_key:
        left, suffix = query_key.split("@", 1)
        _, _layer, text = left.split(":", 2)
        return f"({_loose_terms(text)}) AND {_slice_clause(suffix)}"
    base = base_category_of(query_key)
    if query_key.startswith("cat:") and "@" in query_key:
        # Through _slice_clause, not a hand-built year range: a category that
        # hits the pagination wall is split into months, and the old code
        # pasted "2023-05" straight into the date literal.
        return f"{base} AND {_slice_clause(query_key.split('@', 1)[1])}"
    if query_key.startswith("cat:"):
        return query_key
    return f"all:{query_key}"


# ---------------------------------------------------------------------------
# SQLite state (durability core)
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id TEXT PRIMARY KEY,
            title TEXT,
            year INTEGER,
            layers TEXT,
            status TEXT NOT NULL,
            citation_count INTEGER,
            influential INTEGER,
            venue TEXT,
            passed INTEGER,
            fulltext_source TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS harvest_cursor (
            query_key TEXT PRIMARY KEY,
            next_start INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Citation edges. Retrieval by wording is the one thing that keeps failing:
    # the same claim phrased three ways gives three answers. A reference list
    # does not care how the claim was phrased -- if the search found CAKE, then
    # D2O and MEDA are one hop away in its bibliography. Only edges whose target
    # is an arXiv id are kept, since those are the ones we can resolve.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS citations (
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            PRIMARY KEY (src, dst)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_citations_dst ON citations(dst)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status)")
    # migration: extractor_version column (added when rewriting fulltext extraction
    # to be LaTeX-aware; NULL/0 means processed with the old regex chunker)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    if "extractor_version" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN extractor_version INTEGER")
    # migration: abstract column (added with the lexical niche gate rewrite so
    # niche_match() can score title+abstract, not just title)
    if "abstract" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN abstract TEXT")
    # migration: niche_score + matched_terms (relevance scoring on top of the
    # binary lexical gate; see niche_filter.niche_score()). niche_score is the
    # primary-layer weighted score; matched_terms is a JSON list of the
    # unique terms (broad+specific) that fired across all layers.
    if "niche_score" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN niche_score INTEGER")
    if "matched_terms" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN matched_terms TEXT")
    if "source_url" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN source_url TEXT")
    # migration: last_run_at on cursors + scheduler_state, for the layer-fair
    # persistent scheduler. The old in-memory rotation counter restarted at 0 on
    # every service restart, so queries at the tail of the list (web3) were never
    # reached; scheduling state now survives restarts.
    ccols = {r["name"] for r in conn.execute("PRAGMA table_info(harvest_cursor)").fetchall()}
    if "last_run_at" not in ccols:
        conn.execute("ALTER TABLE harvest_cursor ADD COLUMN last_run_at TEXT")
    # migration: layer + refined, for queries split at arXiv's pagination wall.
    # Slices created at runtime are not in ALL_QUERY_KEYS, so they carry their
    # own layer; refined marks a parent already split, which keeps the catch-up
    # pass idempotent across restarts.
    if "layer" not in ccols:
        conn.execute("ALTER TABLE harvest_cursor ADD COLUMN layer TEXT")
    if "twin_checked" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN twin_checked INTEGER NOT NULL DEFAULT 0")
    if "twin_of" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN twin_of TEXT")
    if "refs_fetched" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN refs_fetched INTEGER NOT NULL DEFAULT 0")
    if "refined" not in ccols:
        conn.execute("ALTER TABLE harvest_cursor ADD COLUMN refined INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    # cleanup: legacy bare-category cursors (cat:cs.CL@YYYY etc). Those queries
    # pulled whole categories and flooded the base with off-niche papers; the
    # niche-scoped queries replaced them.
    conn.execute(
        "DELETE FROM harvest_cursor WHERE query_key LIKE 'cat:cs.%'"
    )
    conn.commit()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def upsert_discovered(conn, arxiv_id, title, year, layer, abstract=None,
                      source_url=None, citation_count=None, venue=None, commit=True):
    """Insert/update a harvested paper, gated by the deterministic lexical
    niche filter (niche_filter.niche_match). The arXiv boolean query already
    narrows results, but this is the authoritative second check: it decides
    status (discovered vs off_niche) and layers (matched buckets, NOT just
    whichever query happened to find the paper first).

    Also computes a weighted relevance score (niche_filter.niche_score):
    niche_score = the primary (highest-scoring) layer's weighted score;
    matched_terms = JSON list of the unique broad+specific terms that fired
    across all layers (title+abstract). Pass/fail behavior is unchanged -
    off_niche iff no layer scores > 0, same as the old niche_match() gate."""
    match_text = f"{title} {abstract}" if abstract else title
    scores = niche_filter.niche_score(match_text)
    matched_layers = sorted(l for l, d in scores.items() if d["score"] > 0)
    _, primary_score = niche_filter.primary_layer(scores)
    matched_terms_json = json.dumps(niche_filter.all_matched_terms(scores))

    row = conn.execute("SELECT layers, status, abstract FROM papers WHERE arxiv_id=?", (arxiv_id,)).fetchone()
    if row is None:
        if matched_layers:
            conn.execute(
                "INSERT INTO papers (arxiv_id, title, year, layers, status, abstract, "
                "niche_score, matched_terms, source_url, citation_count, venue, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (arxiv_id, title, year, ",".join(matched_layers), "discovered", abstract,
                 primary_score, matched_terms_json, source_url, citation_count, venue, now_iso()),
            )
        else:
            conn.execute(
                "INSERT INTO papers (arxiv_id, title, year, layers, status, abstract, "
                "niche_score, matched_terms, source_url, citation_count, venue, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (arxiv_id, title, year, "", "off_niche", abstract,
                 primary_score, matched_terms_json, source_url, citation_count, venue, now_iso()),
            )
    else:
        if row["status"] == "off_niche" and matched_layers:
            # a later harvest query / a richer abstract now proves this paper
            # niche after all - resurrect it into the pipeline
            conn.execute(
                "UPDATE papers SET status='discovered', layers=?, abstract=COALESCE(abstract, ?), "
                "niche_score=?, matched_terms=?, updated_at=? WHERE arxiv_id=?",
                (",".join(matched_layers), abstract, primary_score, matched_terms_json, now_iso(), arxiv_id),
            )
        elif row["status"] != "off_niche":
            layers = set(row["layers"].split(",")) if row["layers"] else set()
            new_layers = layers | set(matched_layers)
            if new_layers != layers or (abstract and not row["abstract"]):
                conn.execute(
                    "UPDATE papers SET layers=?, abstract=COALESCE(abstract, ?), "
                    "niche_score=?, matched_terms=?, updated_at=? WHERE arxiv_id=?",
                    (",".join(sorted(new_layers)) if new_layers else layer, abstract,
                     primary_score, matched_terms_json, now_iso(), arxiv_id),
                )
        if source_url or citation_count is not None or venue:
            conn.execute(
                "UPDATE papers SET source_url=COALESCE(source_url, ?), "
                "citation_count=COALESCE(citation_count, ?), venue=COALESCE(venue, ?) "
                "WHERE arxiv_id=?",
                (source_url, citation_count, venue, arxiv_id),
            )
    if commit:
        conn.commit()


def get_cursor(conn, query_key, layer=None):
    row = conn.execute("SELECT next_start, done FROM harvest_cursor WHERE query_key=?", (query_key,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO harvest_cursor (query_key, next_start, done, layer) VALUES (?,0,0,?)",
                     (query_key, layer))
        conn.commit()
        return 0, 0
    return row["next_start"], row["done"]


def set_cursor(conn, query_key, next_start, done):
    conn.execute(
        "UPDATE harvest_cursor SET next_start=?, done=? WHERE query_key=?",
        (next_start, done, query_key),
    )
    conn.commit()


def mark_query_run(conn, query_key):
    """Stamp a query as just-served so the fair scheduler rotates past it."""
    conn.execute(
        "UPDATE harvest_cursor SET last_run_at=? WHERE query_key=?",
        (now_iso(), query_key),
    )
    conn.commit()


def _sched_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM scheduler_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _sched_set(conn, key, value):
    conn.execute(
        "INSERT INTO scheduler_state (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def _poll_due(conn, key, interval_seconds):
    """Restart-safe clock for rolling sources that never become exhausted."""
    raw = _sched_get(conn, key)
    try:
        return time.time() - float(raw) >= interval_seconds
    except (TypeError, ValueError):
        return True


def _mark_polled(conn, key):
    _sched_set(conn, key, str(time.time()))


def pick_next_query(conn):
    """Layer-fair, restart-safe query selection.

    Round-robins across layers first (so web3 gets the same share as llm-slm
    regardless of how many query keys each layer has), then within the chosen
    layer serves the least-recently-run query that is not done. Both the layer
    pointer and per-query timestamps live in SQLite, so a restart continues
    where it left off instead of replaying the head of the list.
    """
    layers = [layer for layer, _ in LAYER_QUERY_GROUPS]
    if not layers:
        return None
    last_layer = _sched_get(conn, "last_layer")
    start_idx = (layers.index(last_layer) + 1) if last_layer in layers else 0
    for offset in range(len(layers)):
        layer = layers[(start_idx + offset) % len(layers)]
        keys = [qk for lay, qk in ALL_QUERY_KEYS if lay == layer]
        if not keys:
            continue
        for qk in keys:
            get_cursor(conn, qk)  # ensure a row exists so ordering sees it
        placeholders = ",".join("?" * len(keys))
        row = conn.execute(
            "SELECT query_key FROM harvest_cursor "
            f"WHERE done=0 AND (query_key IN ({placeholders}) OR layer=?) "
            "AND query_key NOT LIKE 'acl@%' "
            "AND query_key NOT LIKE 'openalex@%' "
            "AND query_key NOT LIKE 'openalex-fresh@%' "
            "AND query_key NOT LIKE 'pmlr@%' "
            "ORDER BY (last_run_at IS NOT NULL), last_run_at ASC LIMIT 1",
            keys + [layer],
        ).fetchone()
        if row is None:
            continue  # every query for this layer is exhausted
        _sched_set(conn, "last_layer", layer)
        return layer, row["query_key"]
    return None


# ---------------------------------------------------------------------------
# HARVEST
# ---------------------------------------------------------------------------

def fetch_page(query_key: str, start: int, max_results: int):
    search_query = resolve_search_query(query_key)
    params = {
        "search_query": search_query,
        "start": start,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "ascending",
    }
    url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"

    last_err = None
    req = urllib.request.Request(url, headers={"User-Agent": ARXIV_CONTACT_UA})
    for attempt in range(ARXIV_FETCH_RETRIES):
        try:
            arxiv_gate.wait()
            with urllib.request.urlopen(req, timeout=ARXIV_FETCH_TIMEOUT) as response:
                raw_xml = response.read()
            break
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if getattr(e, "code", None) in (429, 503):
                arxiv_gate.penalize()
            wait = min(3 * (2 ** attempt), 24)
            log.warning(f"arXiv fetch error (attempt {attempt+1}/{ARXIV_FETCH_RETRIES}): {e}. Retrying in {wait}s...")
            time.sleep(wait)
    else:
        log.error(f"arXiv fetch permanently failed for {query_key} start={start}: {last_err}. cursor NOT advanced.")
        return None

    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as e:
        log.error(f"arXiv XML parse error for {query_key} start={start}: {e}. cursor NOT advanced.")
        return None

    entries = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        title_el = entry.find(f"{ATOM_NS}title")
        id_el = entry.find(f"{ATOM_NS}id")
        published_el = entry.find(f"{ATOM_NS}published")
        summary_el = entry.find(f"{ATOM_NS}summary")
        if title_el is None or id_el is None:
            continue
        title = " ".join(title_el.text.split())
        raw_id = id_el.text.strip()
        # http://arxiv.org/abs/2401.01234v1 -> 2401.01234
        arxiv_id = raw_id.rsplit("/", 1)[-1]
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
        year = int(published_el.text[:4]) if published_el is not None else 0
        abstract = " ".join(summary_el.text.split()) if summary_el is not None and summary_el.text else None
        entries.append({"title": title, "arxiv_id": arxiv_id, "year": year, "abstract": abstract})
    return entries


ALL_QUERY_KEYS = [(layer, qk) for layer, qks in LAYER_QUERY_GROUPS for qk in qks]


def child_keys_for(layer: str, query_key: str) -> list:
    """Narrower slices for a query that hit arXiv's 10k pagination wall.

    A year slice becomes twelve months. A plain keyword query, which carried no
    date filter at all, becomes year slices plus one bucket for everything
    before the first year, and each of those can be split again later. A month
    is the floor: below it the remaining tail is not worth the requests.
    """
    if "@" in query_key:
        left, suffix = query_key.split("@", 1)
        if suffix == "pre" or "-" in suffix:
            return []  # already a month, or the pre-history bucket
        return [f"{left}@{suffix}-{m:02d}" for m in range(1, 13)]
    if query_key.startswith(("bq:", "cat:", "kw:", "iacr@")) or query_key in SPEC_SOURCES:
        return []
    years = [str(y) for y in range(DATE_SLICE_START_YEAR, DATE_SLICE_END_YEAR + 1)]
    return [f"kw:{layer}:{query_key}@{suffix}" for suffix in ["pre"] + years]


def refine_capped_query(conn, layer, query_key, next_start):
    """Close a capped query and open its narrower slices. Returns the children."""
    children = child_keys_for(layer, query_key)
    conn.execute("UPDATE harvest_cursor SET next_start=?, done=1, refined=1 WHERE query_key=?",
                 (next_start, query_key))
    for child in children:
        conn.execute("INSERT OR IGNORE INTO harvest_cursor (query_key, next_start, done, layer) "
                     "VALUES (?,0,0,?)", (child, layer))
    conn.commit()
    return children


def refine_capped_cursors(conn):
    """Catch-up for queries capped before splitting existed. Idempotent."""
    static_layer = {qk: lay for lay, qk in ALL_QUERY_KEYS}
    rows = conn.execute(
        "SELECT query_key, next_start, layer FROM harvest_cursor "
        "WHERE done=1 AND refined=0 AND next_start > ?", (ARXIV_MAX_START,)
    ).fetchall()
    added = 0
    for row in rows:
        qk = row["query_key"]
        layer = row["layer"] or static_layer.get(qk)
        if not layer:
            continue
        children = refine_capped_query(conn, layer, qk, row["next_start"])
        added += len(children)
        if children:
            log.info(f"harvest: '{qk}' was capped, split into {len(children)} slices")
    if added:
        log.info(f"harvest: {added} date slices opened from previously capped queries")
HARVEST_QUERIES_PER_CYCLE = 4  # keep each cycle short so quality/fulltext/chunk/embed get a turn too


def refresh_live_cursors(conn):
    """Reopen append-only sources on a durable schedule.

    Historical cursors really are finite, but the current arXiv year/month,
    current ACL volumes, IACR month and protocol repositories keep changing.
    Treating their first empty page as permanent exhaustion stopped discovery
    while the service itself stayed healthy.  Offsets are preserved for arXiv;
    sources without stable offsets are re-read and deduplicated by paper id.
    """
    now = datetime.now(timezone.utc)
    year = now.strftime("%Y")
    month = now.strftime("%Y-%m")

    if _poll_due(conn, "fresh:hourly", FRESH_POLL_SECONDS):
        patterns = (f"bq:%@{year}", f"kw:%@{year}", f"cat:%@{year}",
                    f"bq:%@{month}", f"kw:%@{month}", f"cat:%@{month}")
        reopened = 0
        for pattern in patterns:
            cur = conn.execute(
                "UPDATE harvest_cursor SET done=0,last_run_at=NULL "
                "WHERE query_key LIKE ? AND refined=0 AND done=1", (pattern,))
            reopened += cur.rowcount
        cur = conn.execute(
            "UPDATE harvest_cursor SET done=0,last_run_at=NULL "
            "WHERE query_key=? AND done=1", (f"iacr@{month}",))
        reopened += cur.rowcount
        conn.commit()
        _mark_polled(conn, "fresh:hourly")
        if reopened:
            log.info(f"fresh: reopened {reopened} live arXiv/IACR cursors")

    if _poll_due(conn, "fresh:daily", SPEC_REFRESH_SECONDS):
        # GitHub specs and current ACL XML files are mutable snapshots, not
        # immutable pages. Re-reading them is cheap and INSERT/UPDATE is
        # idempotent, so newly merged EIPs, SIMDs and ACL papers are admitted.
        cur_specs = conn.execute(
            "UPDATE harvest_cursor SET next_start=0,done=0,last_run_at=NULL "
            "WHERE query_key IN ('eip@all','simd@all')")
        cur_acl = conn.execute(
            "UPDATE harvest_cursor SET next_start=0,done=0,last_run_at=NULL "
            "WHERE query_key LIKE ?", (f"acl@{year}%",))
        conn.commit()
        _mark_polled(conn, "fresh:daily")
        log.info(f"fresh: daily sources reopened specs={cur_specs.rowcount} acl={cur_acl.rowcount}")


def harvest_step(conn):
    """Process one page per turn, picking queries layer-fairly via the
    persistent scheduler so every niche advances even across restarts.
    Returns count of new papers discovered this call."""
    new_count = 0
    if not ALL_QUERY_KEYS:
        return 0
    refresh_live_cursors(conn)
    touched = 0
    attempts = 0
    max_attempts = len(ALL_QUERY_KEYS)
    while touched < HARVEST_QUERIES_PER_CYCLE and attempts < max_attempts:
        picked = pick_next_query(conn)
        attempts += 1
        if picked is None:
            break  # every query exhausted
        layer, query_key = picked
        next_start, done = get_cursor(conn, query_key)
        mark_query_run(conn, query_key)
        if done:
            continue
        touched += 1

        if query_key in SPEC_SOURCES:
            entries, total = fetch_spec_batch(query_key, next_start, SPEC_FILES_PER_CYCLE)
            if entries is None:
                continue  # transient failure: cursor stays, slice is retried
            for e in entries:
                upsert_discovered(conn, e["arxiv_id"], e["title"], e["year"], layer,
                                  abstract=e.get("abstract"))
                # markdown is the fulltext: cache it now, nothing else to fetch
                try:
                    write_latex_cache(e["arxiv_id"], e["full_text"])
                except Exception:
                    pass
                new_count += 1
            advanced = next_start + SPEC_FILES_PER_CYCLE
            set_cursor(conn, query_key, advanced, 1 if advanced >= (total or 0) else 0)
            log.info(f"harvest: {query_key} {next_start}-{advanced} of {total} -> {len(entries)} kept")
            continue

        if query_key.startswith("iacr@"):
            # A month that has not started yet has nothing to give, and with the
            # arXiv plan exhausted these were the only open cursors left: four of
            # every five harvest slots went to fetching an empty future. They are
            # parked instead, and _open_current_iacr_month reopens each one on
            # the day it becomes real.
            if query_key.split("@", 1)[1] > time.strftime("%Y-%m", time.gmtime()):
                set_cursor(conn, query_key, 0, 1)
                continue
            # One OAI request covers a whole month, so there is no offset to
            # advance. Every successful poll is closed and the durable hourly
            # refresher opens the current month again; this avoids hammering an
            # empty month every few seconds.
            entries = fetch_iacr_month(query_key)
            time.sleep(IACR_DELAY_SECONDS)
            if entries is None:
                continue  # transient failure: leave the cursor open
            for e in entries:
                upsert_discovered(conn, e["arxiv_id"], e["title"], e["year"], layer,
                                  abstract=e.get("abstract"))
                new_count += 1
            # A month is finished only once it is in the past. Closing it by
            # equality with "the current month" froze every future month the
            # moment it was first probed: iacr@2026-08 was read empty in July,
            # marked done, and stayed done when August arrived. IACR went
            # silent, and since every arXiv cursor was already exhausted, the
            # whole harvest went silent with it.
            current_month = time.strftime("%Y-%m", time.gmtime())
            month_suffix = query_key.split("@", 1)[1]
            set_cursor(conn, query_key, len(entries), 1)
            log.info(f"harvest: {query_key} -> {len(entries)} records")
            continue

        if next_start > ARXIV_MAX_START:
            children = refine_capped_query(conn, layer, query_key, next_start)
            if children:
                log.info(f"harvest: '{query_key}' hit arXiv's pagination limit at "
                         f"start={next_start}; split into {len(children)} narrower slices")
            else:
                log.info(f"harvest: '{query_key}' capped at start={next_start}; "
                         "no finer slice available")
            continue

        entries = fetch_page(query_key, next_start, PAGE_SIZE)
        time.sleep(ARXIV_DELAY_SECONDS)
        if entries is None:
            # fetch failed - do NOT mark done, cursor stays put, retried next cycle
            continue
        if len(entries) == 0:
            set_cursor(conn, query_key, next_start, 1)
            log.info(f"harvest: query '{query_key}' exhausted at start={next_start}")
            continue
        for e in entries:
            upsert_discovered(conn, e["arxiv_id"], e["title"], e["year"], layer, abstract=e.get("abstract"))
            new_count += 1
        # short page (< PAGE_SIZE) means we've reached the end too
        done_flag = 1 if len(entries) < PAGE_SIZE else 0
        set_cursor(conn, query_key, next_start + len(entries), done_flag)
    return new_count


# ---------------------------------------------------------------------------
# QUALITY
# ---------------------------------------------------------------------------

TOP_VENUES = [
    "neurips", "neural information processing",
    "icml", "international conference on machine learning",
    "iclr", "learning representations",
    "acl", "association for computational linguistics",
    "emnlp", "naacl", "aaai", "ijcai",
    "cvpr", "iccv", "eccv",
    "sigir", "kdd", "www", "the web conference",
    "usenix security", "ccs", "ndss", "osdi", "sosp",
    "financial cryptography",
]


# A citation count is evidence about a paper only once the field has had time
# to react. Judging on it at month two is judging on absence: 91% of this year's
# papers were rejected for having no citations yet, in fields where a paper from
# last year is already stale. The gate now measures age in months and answers
# with three outcomes instead of two -- pass, reject, or "ask again later" --
# so a verdict is never passed at the moment of least information.
DEFER_GIVE_UP_MONTHS = 24   # still nothing by here: the field really did pass it by
# Fourteen days was chosen when the harvest still had a queue and the deferred
# shelf was a place to park things. With the arXiv plan exhausted that shelf is
# a working queue, and 24k papers sitting untouched for two weeks is two weeks
# of an idle pipeline. Five days is still long enough for a citation to appear
# and short enough that the shelf keeps moving.
RECHECK_AFTER_DAYS = 5


def paper_age_months(arxiv_id, year, now=None):
    """Months since publication. arXiv ids carry YYMM, which beats the year
    alone: a January and a December paper of the same year are not equally old."""
    now = now or time.gmtime()
    pid = str(arxiv_id or "")
    head = pid.split(":", 1)[-1].split(".", 1)[0]
    if len(head) == 4 and head.isdigit():
        pub_year = 2000 + int(head[:2])
        pub_month = max(1, min(12, int(head[2:])))
    elif year:
        pub_year, pub_month = int(year), 6  # mid-year, we know nothing finer
    else:
        return DEFER_GIVE_UP_MONTHS  # unknown age: judge it on what we have
    return max(0, (now.tm_year - pub_year) * 12 + (now.tm_mon - pub_month))


# A citation count means something different inside a narrow subject. An audit
# of fifty rejected papers found four that were plainly on topic (niche_score
# 6-11) with 9-13 citations, refused for missing an age threshold by units --
# about 8% of a shelf of 24k, so roughly 1900 papers thrown away for being
# specialised rather than for being weak. The bar is lowered for work that
# clearly belongs to one of the niches, and only for that work.
# The gate was tuned when the harvest still had a queue. With every arXiv
# cursor exhausted, what it now rejects is the only place left to grow, and
# measured against the 57k it had already turned away, the bar was in the wrong
# place rather than merely high: 6849 of them clear it on subject alone.
#
# The loosening is deliberately conditioned on the niche score instead of
# applied across the board. Halving the bar for everyone admits 15084 papers of
# which 4458 are barely on subject; halving it only for papers the lexical gate
# already puts inside a niche admits 10766 of which 140 are. Same order of
# growth, a thirtieth of the dilution.
NICHE_DISCOUNT_FROM = 4
NICHE_DISCOUNT = 0.5
YOUNG_MONTHS = 36            # a citation count means little before this age
YOUNG_NICHE_SCALE = 0.5      # ...so in-niche work gets a second discount here


def citation_threshold(age_months: int, niche_score=None) -> int:
    if age_months < 12:
        base = 3
    elif age_months < 24:
        base = 10
    elif age_months < 36:
        base = 30
    elif age_months < 48:
        base = 60
    elif age_months < 60:
        base = 100
    else:
        base = 150
    if (niche_score or 0) >= NICHE_DISCOUNT_FROM:
        base = max(2, int(round(base * NICHE_DISCOUNT)))
        if age_months <= YOUNG_MONTHS:
            # young and on subject: the field has not had time to react, and
            # waiting for it to react is what left these in the deferred shelf
            base = max(1, int(round(base * YOUNG_NICHE_SCALE)))
    return base


def quality_verdict(venue, citation_count, influential, year, arxiv_id=None,
                    niche_score=None, top_lab=False):
    """"pass", "defer" or "reject"."""
    venue_l = (venue or "").lower()
    if any(v in venue_l for v in TOP_VENUES):
        return "pass"
    if (influential or 0) >= 3:
        return "pass"
    if (niche_score or 0) >= STRONG_NICHE_AUTO_PASS:
        return "pass"
    if ((niche_score or 0) >= NICHE_CITATION_PASS_SCORE
            and (citation_count or 0) >= NICHE_CITATION_PASS_MIN):
        return "pass"
    age_months = paper_age_months(arxiv_id, year)
    # the SOTA lane: too new to be cited, admitted on what it is rather than on
    # how the field has reacted, because it has not had time to react
    if age_months <= SOTA_MAX_AGE_MONTHS and (top_lab or (niche_score or 0) >= SOTA_MIN_NICHE):
        return "pass"
    if (citation_count or 0) >= citation_threshold(age_months, niche_score):
        return "pass"
    if age_months < DEFER_GIVE_UP_MONTHS:
        return "defer"
    return "reject"


def passes_quality_gate(venue, citation_count, influential, year, arxiv_id=None):
    return quality_verdict(venue, citation_count, influential, year, arxiv_id) == "pass"


def fetch_s2_batch(session, arxiv_ids):
    """Citation metadata for a batch of arXiv ids, or None if S2 would not talk."""
    if not arxiv_ids:
        return {}
    s2_gate.wait()
    try:
        resp = session.post(
            S2_API_URL,
            params={"fields": S2_FIELDS},
            json={"ids": [f"ARXIV:{i}" for i in arxiv_ids]},
            headers={"x-api-key": S2_API_KEY} if S2_API_KEY else None,
            timeout=60,
        )
    except requests.RequestException as e:
        log.warning(f"s2: request error: {e}")
        return None
    if resp.status_code == 429:
        log.warning("s2: 429 rate limited, backing off this cycle")
        s2_gate.penalize(S2_PENALTY_SECONDS)
        return None
    if resp.status_code != 200:
        log.warning(f"s2: status {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        data = resp.json()
    except ValueError:
        log.warning("s2: non-JSON response")
        return None
    time.sleep(S2_PAUSE)
    return {pid: paper for pid, paper in zip(arxiv_ids, data)}


S2_REFS_BATCH = 100          # reference lists are heavy; smaller batches than the metadata call
REFS_CYCLE_LIMIT = 100


def fetch_s2_references(session, arxiv_ids):
    """{arxiv_id: [referenced arxiv ids]} for a batch, or None if S2 refused."""
    if not arxiv_ids:
        return {}
    s2_gate.wait()
    try:
        resp = session.post(
            S2_API_URL,
            params={"fields": "references.externalIds"},
            json={"ids": [f"ARXIV:{i}" for i in arxiv_ids]},
            headers={"x-api-key": S2_API_KEY} if S2_API_KEY else None,
            timeout=120,
        )
    except requests.RequestException as e:
        log.warning(f"refs: request error: {e}")
        return None
    if resp.status_code == 429:
        log.warning("refs: 429 rate limited, backing off this cycle")
        s2_gate.penalize(S2_PENALTY_SECONDS)
        return None
    if resp.status_code != 200:
        log.warning(f"refs: status {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        data = resp.json()
    except ValueError:
        log.warning("refs: non-JSON response")
        return None
    out = {}
    for pid, paper in zip(arxiv_ids, data):
        refs = []
        if isinstance(paper, dict):
            for r in (paper.get("references") or []):
                ext = r.get("externalIds") if isinstance(r, dict) else None
                arx = (ext or {}).get("ArXiv")
                if arx:
                    refs.append(arx)
        out[pid] = refs
    time.sleep(S2_PAUSE)
    return out


# IACR full text is unreachable: the PDFs sit behind Cloudflare, which answers
# 403 to this datacenter IP even from a real headless browser with a full
# fingerprint, so the usual challenge-solvers do not apply -- it is not a
# challenge, it is a block on the asset. What is reachable is arXiv, where
# cryptography work is very often cross-posted. Matching an ePrint title to its
# arXiv twin gets the same paper with its full text, legally and for free.
# Measured before building it: 270 of 4826 ePrints already had an exact-title
# twin sitting in the corpus by accident.
TWIN_CYCLE_LIMIT = 20
TWIN_TITLE_MIN_WORDS = 5


def _title_key(title):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (title or "").lower()).split())


def iacr_twin_step(conn):
    """Find the arXiv version of an IACR paper and harvest that instead."""
    rows = conn.execute(
        "SELECT arxiv_id, title, layers FROM papers "
        "WHERE arxiv_id LIKE 'iacr:%' AND status='done' AND twin_checked=0 "
        "LIMIT ?", (TWIN_CYCLE_LIMIT,)).fetchall()
    if not rows:
        return 0
    found = 0
    for row in rows:
        title = row["title"] or ""
        words = _title_key(title).split()
        # commit before the network call: eight stages share one SQLite file, and
        # a write transaction held open across an HTTP request locks every other
        # writer out of the database until the response comes back
        conn.execute("UPDATE papers SET twin_checked=1 WHERE arxiv_id=?", (row["arxiv_id"],))
        conn.commit()
        if len(words) < TWIN_TITLE_MIN_WORDS:
            continue
        quoted = " ".join(words[:14])
        # a title search, not a topical one: exact phrase against the title field
        params = {"search_query": f'ti:"{quoted}"', "start": 0, "max_results": 5,
                  "sortBy": "relevance", "sortOrder": "descending"}
        url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": ARXIV_CONTACT_UA})
        try:
            arxiv_gate.wait()
            with urllib.request.urlopen(req, timeout=ARXIV_FETCH_TIMEOUT) as resp:
                raw = resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if getattr(e, "code", None) in (429, 503):
                arxiv_gate.penalize()
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        for entry in root.findall(f"{ATOM_NS}entry"):
            t_el = entry.find(f"{ATOM_NS}title")
            id_el = entry.find(f"{ATOM_NS}id")
            if t_el is None or id_el is None:
                continue
            if _title_key(t_el.text) != _title_key(title):
                continue  # same words or nothing: a near match is a different paper
            twin = re.sub(r"v\d+$", "", id_el.text.strip().rsplit("/", 1)[-1])
            published = entry.find(f"{ATOM_NS}published")
            summary = entry.find(f"{ATOM_NS}summary")
            upsert_discovered(conn, twin, " ".join(t_el.text.split()),
                              int(published.text[:4]) if published is not None else 0,
                              (row["layers"] or "web3").split(",")[0],
                              abstract=" ".join(summary.text.split()) if summary is not None else None)
            conn.execute("UPDATE papers SET twin_of=? WHERE arxiv_id=?",
                         (row["arxiv_id"], twin))
            conn.commit()
            found += 1
            break
    conn.commit()
    if found:
        log.info(f"iacr twins: {len(rows)} checked, {found} arXiv versions queued")
    return len(rows)


def refs_step(conn, session):
    """Fill in reference edges for papers that have none yet."""
    rows = conn.execute(
        "SELECT arxiv_id FROM papers WHERE status='done' AND refs_fetched=0 "
        "AND arxiv_id NOT LIKE '%:%' LIMIT ?", (REFS_CYCLE_LIMIT,)).fetchall()
    if not rows:
        return 0
    ids = [r["arxiv_id"] for r in rows]
    refs = fetch_s2_references(session, ids)
    if refs is None:
        return 0
    edges = 0
    for pid in ids:
        for dst in refs.get(pid, []):
            conn.execute("INSERT OR IGNORE INTO citations (src, dst) VALUES (?,?)", (pid, dst))
            edges += 1
        conn.execute("UPDATE papers SET refs_fetched=1 WHERE arxiv_id=?", (pid,))
    conn.commit()
    log.info(f"refs: {len(ids)} papers, {edges} edges")
    return len(ids)


def quality_step(conn, session):
    rows = conn.execute("SELECT arxiv_id, year, niche_score FROM papers "
                        "WHERE status='discovered' LIMIT ?", (S2_BATCH_SIZE,)).fetchall()
    if not rows:
        return 0

    # Semantic Scholar does not index IACR ePrint, so a citation gate would
    # reject every crypto paper outright. Those are admitted on the lexical
    # niche score alone (which is what got them harvested in the first place).
    hal_rows = [r for r in rows if str(r["arxiv_id"]).startswith(HAL_ID_PREFIX)]
    iacr_rows = [r for r in rows if is_non_arxiv(r["arxiv_id"])]
    rows = [r for r in rows if not is_non_arxiv(r["arxiv_id"])
            and not str(r["arxiv_id"]).startswith(HAL_ID_PREFIX)]
    hal_done = 0
    for r in hal_rows:
        # HAL records are deposited academic work with a direct PDF.  We do
        # not have a stable external identifier to ask S2 about, so admit only
        # documents that clear the same strong-niche bar used for arXiv.
        passed = (r["niche_score"] or 0) >= HAL_MIN_NICHE_AUTO_PASS
        conn.execute(
            "UPDATE papers SET passed=?, status=?, updated_at=? WHERE arxiv_id=?",
            (1 if passed else 0, "quality_checked" if passed else "deferred",
             now_iso(), r["arxiv_id"]),
        )
        hal_done += 1
    iacr_done = 0
    for r in iacr_rows:
        conn.execute(
            "UPDATE papers SET passed=1, status='quality_checked', updated_at=? WHERE arxiv_id=?",
            (now_iso(), r["arxiv_id"]),
        )
        iacr_done += 1
    if iacr_done or hal_done:
        conn.commit()
    if not rows:
        return iacr_done + hal_done

    ids = [f"ARXIV:{r['arxiv_id']}" for r in rows]
    by_id = {r["arxiv_id"]: r for r in rows}
    s2_gate.wait()
    try:
        resp = session.post(
            S2_API_URL,
            params={"fields": S2_FIELDS},
            json={"ids": ids},
            headers={"x-api-key": S2_API_KEY} if S2_API_KEY else None,
            timeout=60,
        )
    except requests.RequestException as e:
        log.warning(f"quality: S2 request error: {e}")
        return 0

    if resp.status_code == 429:
        log.warning("quality: S2 429 rate limited, backing off this cycle")
        s2_gate.penalize(S2_PENALTY_SECONDS)
        return 0
    if resp.status_code != 200:
        log.warning(f"quality: S2 status {resp.status_code}: {resp.text[:200]}")
        return 0

    try:
        data = resp.json()
    except ValueError:
        log.warning("quality: S2 returned non-JSON")
        return 0

    processed = 0
    for arxiv_id_full, paper in zip(ids, data):
        arxiv_id = arxiv_id_full.split(":", 1)[1]
        row = by_id.get(arxiv_id)
        if row is None:
            continue
        if paper is None:
            citation_count, influential, venue, year, top_lab = 0, 0, None, row["year"], False
        else:
            citation_count = paper.get("citationCount") or 0
            influential = paper.get("influentialCitationCount") or 0
            venue = paper.get("venue") or (paper.get("publicationVenue") or {}).get("name")
            year = paper.get("year") or row["year"]
            top_lab = any(is_top_lab(a.get("affiliations"))
                          for a in (paper.get("authors") or []) if isinstance(a, dict))
        verdict = quality_verdict(venue, citation_count, influential, year, arxiv_id,
                                  row["niche_score"] if "niche_score" in row.keys() else None,
                                  top_lab)
        passed = verdict == "pass"
        new_status = {"pass": "quality_checked", "defer": "deferred"}.get(verdict, "rejected")
        conn.execute(
            """UPDATE papers SET citation_count=?, influential=?, venue=?, year=?,
               passed=?, status=?, updated_at=? WHERE arxiv_id=?""",
            (citation_count, influential, venue, year, 1 if passed else 0, new_status, now_iso(), arxiv_id),
        )
        processed += 1
    conn.commit()
    time.sleep(S2_PAUSE)
    return processed + iacr_done + hal_done


# ---------------------------------------------------------------------------
# FULLTEXT
# ---------------------------------------------------------------------------

def safe_id(paper_id: str) -> str:
    """Filesystem-safe form of a paper id.

    IACR ids look like `iacr:2016/201`; the slash would silently turn into a
    directory boundary and the colon is awkward on some filesystems.
    """
    return str(paper_id).replace("/", "_").replace(":", "_")


def latex_cache_path(arxiv_id: str) -> Path:
    return LATEX_CACHE_DIR / safe_id(arxiv_id) / "source.tex"


def read_latex_cache(arxiv_id: str):
    p = latex_cache_path(arxiv_id)
    if p.exists():
        try:
            return p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
    return None


def write_latex_cache(arxiv_id: str, text: str):
    p = latex_cache_path(arxiv_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", errors="ignore")


def fetch_latex_source(arxiv_id: str, session: requests.Session = None):
    """Return (text, source) where source in {'latex','abstract-fallback'} or (None, None) on hard fail.
    Checks the on-disk LaTeX cache first so re-processing never re-downloads."""
    cached = read_latex_cache(arxiv_id)
    if cached is not None:
        return cached, "latex"
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    http = session or requests
    headers = {"User-Agent": ARXIV_CONTACT_UA}
    resp = None
    for attempt in range(FULLTEXT_MAX_RETRIES):
        # the shared gate spaces these out; it also holds them back while a 429
        # from anywhere in the process is still cooling off
        arxiv_gate.wait()
        try:
            resp = http.get(url, timeout=60, headers=headers)
        except requests.RequestException as e:
            log.warning(f"fulltext: request error for {arxiv_id}: {e}")
            return None, None
        if resp.status_code in (429, 503):
            arxiv_gate.penalize()
            wait = min(5 * (2 ** attempt), 30)
            log.warning(f"fulltext: arXiv {resp.status_code} for {arxiv_id}, backing off {wait}s (attempt {attempt+1}/{FULLTEXT_MAX_RETRIES})")
            time.sleep(wait)
            continue
        break
    if resp is None or resp.status_code in (429, 503):
        return None, "rate-limited"
    if resp.status_code != 200:
        return None, "not-found"
    raw = resp.content
    try:
        # try tar.gz first
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        tex_parts = []
        main_candidates = []
        for member in tf.getmembers():
            if member.isfile() and member.name.endswith(".tex"):
                content = tf.extractfile(member).read().decode("utf-8", errors="ignore")
                main_candidates.append((member.name, content))
        if not main_candidates:
            return None, "no-tex-in-archive"
        # crude heuristic: the file with \documentclass is main
        main = None
        for name, content in main_candidates:
            if "\\documentclass" in content:
                main = content
                break
        if main is None:
            main = max(main_candidates, key=lambda nc: len(nc[1]))[1]
        combined = main
        # resolve simple \input{...} / \include{...} one level deep
        content_by_base = {name.rsplit("/", 1)[-1].replace(".tex", ""): c for name, c in main_candidates}
        def resolve_inputs(text, depth=0):
            if depth > 2:
                return text
            def repl(m):
                base = m.group(1).replace(".tex", "")
                inc = content_by_base.get(base)
                return resolve_inputs(inc, depth + 1) if inc else ""
            return re.sub(r"\\(?:input|include)\{([^}]+)\}", repl, text)
        combined = resolve_inputs(combined)
        write_latex_cache(arxiv_id, combined)
        return combined, "latex"
    except tarfile.ReadError:
        # Might be a single gzip'd .tex file, not a tar
        try:
            decompressed = gzip.decompress(raw)
            text = decompressed.decode("utf-8", errors="ignore")
            if "\\documentclass" in text or "\\section" in text:
                write_latex_cache(arxiv_id, text)
                return text, "latex"
        except Exception:
            pass
        return None, "not-latex"


def fetch_abstract_fallback(arxiv_id: str):
    url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            raw_xml = response.read()
        root = ET.fromstring(raw_xml)
        entry = root.find(f"{ATOM_NS}entry")
        if entry is None:
            return None
        summary_el = entry.find(f"{ATOM_NS}summary")
        if summary_el is None:
            return None
        return " ".join(summary_el.text.split())
    except Exception as e:
        log.warning(f"fulltext: abstract fallback failed for {arxiv_id}: {e}")
        return None


FULLTEXT_DIR = BASE_DIR / "fulltext_cache"

_fulltext_session = None


def get_fulltext_session():
    global _fulltext_session
    if _fulltext_session is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=FULLTEXT_WORKERS + 2, pool_maxsize=FULLTEXT_WORKERS + 2)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _fulltext_session = s
    return _fulltext_session


def _fetch_acl_fulltext(paper_id, stored_abstract=None):
    """PDF -> markdown for one ACL paper. Falls back to the abstract."""
    ident = str(paper_id)[len(ACL_ID_PREFIX):].strip("/")
    url = f"{ACL_PDF_BASE}{ident}.pdf"
    try:
        session = get_fulltext_session()
        resp = session.get(url, timeout=90,
                           headers={"User-Agent": ARXIV_CONTACT_UA})
        if resp.status_code != 200 or not resp.content[:4] == b"%PDF":
            raise ValueError(f"status {resp.status_code}")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name
        try:
            import pymupdf4llm
            text = pymupdf4llm.to_markdown(tmp_path, show_progress=False)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if text and len(text) > 500:
            return paper_id, text, "acl-pdf"
        raise ValueError("pdf produced no usable text")
    except Exception as e:
        log.debug(f"acl fulltext {ident}: {e}")
    if stored_abstract:
        return paper_id, "\\section{Abstract}\n" + stored_abstract, "acl-abstract"
    return paper_id, None, None


def _fetch_openalex_fulltext(paper_id, pdf_url, stored_abstract=None):
    """Fetch one OpenAlex-selected OA PDF and preserve its Markdown structure."""
    if not pdf_url:
        return paper_id, None, None
    try:
        session = get_fulltext_session()
        resp = session.get(pdf_url, timeout=120,
                           headers={"User-Agent": ARXIV_CONTACT_UA})
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            raise ValueError(f"status {resp.status_code}")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name
        try:
            import pymupdf4llm
            text = pymupdf4llm.to_markdown(tmp_path, show_progress=False)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if text and len(text) > 500:
            return paper_id, text, "openalex-oa-pdf"
        raise ValueError("pdf produced no usable text")
    except Exception as e:
        log.debug(f"openalex fulltext {paper_id}: {e}")
    if stored_abstract:
        return paper_id, "# Abstract\n\n" + stored_abstract, "openalex-abstract"
    return paper_id, None, None


def _fetch_one_fulltext(arxiv_id, stored_abstract=None, source_url=None):
    """Runs in a worker thread. Pure I/O, no DB access. Returns (arxiv_id, text, source)."""
    if str(arxiv_id).startswith((EIP_ID_PREFIX, SIMD_ID_PREFIX)):
        # the markdown was stored at harvest time; nothing to download
        cached = read_latex_cache(arxiv_id)
        if not cached:
            return arxiv_id, None, None
        return arxiv_id, cached, "spec-markdown"

    if str(arxiv_id).startswith(ACL_ID_PREFIX):
        # ACL ships PDFs, and unlike IACR it serves them to anyone. pymupdf4llm
        # keeps the section headings, which is what the chunker reads.
        return _fetch_acl_fulltext(arxiv_id, stored_abstract)

    if str(arxiv_id).startswith((OPENALEX_ID_PREFIX, PMLR_ID_PREFIX, HAL_ID_PREFIX)):
        return _fetch_openalex_fulltext(arxiv_id, source_url, stored_abstract)

    if str(arxiv_id).startswith(IACR_ID_PREFIX):
        # IACR ships PDFs only, no LaTeX source, but OAI-PMH already handed us
        # the abstract at harvest time, so there is nothing left to download.
        if not stored_abstract:
            return arxiv_id, None, None
        return arxiv_id, f"\\section{{Abstract}}\n{stored_abstract}", "iacr-abstract"

    session = get_fulltext_session()
    text, source = fetch_latex_source(arxiv_id, session=session)
    if text is None:
        abstract = fetch_abstract_fallback(arxiv_id)
        if abstract is None:
            return arxiv_id, None, None
        text = f"\\section{{Abstract}}\n{abstract}"
        source = "abstract-fallback"
    return arxiv_id, text, source


def fulltext_step(conn, limit=FULLTEXT_CYCLE_LIMIT):
    rows = conn.execute(
        "SELECT arxiv_id, abstract, source_url FROM papers WHERE status='quality_checked' LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        return 0
    FULLTEXT_DIR.mkdir(exist_ok=True)
    processed = 0
    ids = [row["arxiv_id"] for row in rows]
    abstracts = {row["arxiv_id"]: row["abstract"] for row in rows}
    source_urls = {row["arxiv_id"]: row["source_url"] for row in rows}
    # Download/parse concurrently (I/O-bound, no DB touched from worker threads).
    # Each paper's status transition is still committed one at a time, sequentially,
    # in this (main) thread as results arrive -- durability is unchanged: a crash
    # mid-batch leaves in-flight papers at 'quality_checked' and they are retried
    # from scratch next cycle (nothing partially written to state.db).
    with ThreadPoolExecutor(max_workers=FULLTEXT_WORKERS) as pool:
        futures = {pool.submit(_fetch_one_fulltext, aid, abstracts.get(aid),
                               source_urls.get(aid)): aid for aid in ids}
        for future in as_completed(futures):
            arxiv_id = futures[future]
            try:
                arxiv_id, text, source = future.result()
            except Exception as e:
                log.warning(f"fulltext: worker error for {arxiv_id}: {e}")
                continue
            if text is None:
                # transient failure - leave status alone, retried next cycle
                continue
            (FULLTEXT_DIR / f"{safe_id(arxiv_id)}.tex").write_text(text, encoding="utf-8", errors="ignore")
            conn.execute(
                "UPDATE papers SET status='fulltext_fetched', fulltext_source=?, updated_at=? WHERE arxiv_id=?",
                (source, now_iso(), arxiv_id),
            )
            conn.commit()
            processed += 1
    return processed


# ---------------------------------------------------------------------------
# CHUNK (LaTeX-aware: extractor.py owns section classification + element
# extraction; this just wires the DB/session plumbing around it)
# ---------------------------------------------------------------------------

CHUNK_DIR = BASE_DIR / "chunk_cache"


def _make_embed_fn(session):
    def embed_fn(texts):
        return embed_texts_batch(session, texts)
    return embed_fn


def chunk_step(conn, session, limit=CHUNK_CYCLE_LIMIT):
    rows = conn.execute("SELECT arxiv_id FROM papers WHERE status='fulltext_fetched' LIMIT ?", (limit,)).fetchall()
    processed = 0
    CHUNK_DIR.mkdir(exist_ok=True)
    embed_fn = _make_embed_fn(session)
    for row in rows:
        arxiv_id = row["arxiv_id"]
        tex_path = FULLTEXT_DIR / f"{safe_id(arxiv_id)}.tex"
        if not tex_path.exists():
            continue
        text = tex_path.read_text(encoding="utf-8", errors="ignore")
        try:
            # ACL arrives as markdown too (pymupdf4llm keeps the headings), so
            # it takes the markdown path rather than the LaTeX parser, which
            # would find no \section commands and fall back to one flat blob.
            if is_spec_source(arxiv_id) or str(arxiv_id).startswith(
                    (ACL_ID_PREFIX, OPENALEX_ID_PREFIX, PMLR_ID_PREFIX, HAL_ID_PREFIX)):
                chunks, mode = extractor.build_chunks_markdown(text)
            else:
                chunks, mode = extractor.build_chunks(text, embed_fn=embed_fn)
        except Exception as e:
            log.warning(f"chunk: extractor crashed for {arxiv_id}, using bare fallback: {e}")
            chunks = [{
                "chunk_index": 0, "section_type": "other", "section_title": "full text",
                "element_type": "prose", "has_math": False, "has_algorithm": False,
                "part_index": 0, "part_total": 1, "text": text[:extractor.CHUNK_CHAR_LIMIT],
            }] if text.strip() else []
            mode = "legacy"
        (CHUNK_DIR / f"{safe_id(arxiv_id)}.json").write_text(json.dumps(chunks), encoding="utf-8")
        conn.execute(
            "UPDATE papers SET status='chunked', extractor_version=?, updated_at=? WHERE arxiv_id=?",
            (EXTRACTOR_VERSION, now_iso(), arxiv_id),
        )
        conn.commit()
        processed += 1
    return processed


# ---------------------------------------------------------------------------
# EMBED + UPSERT
# ---------------------------------------------------------------------------

def ensure_collection(session):
    resp = session.get(f"{QDRANT_URL}/collections/{COLLECTION_NAME}", timeout=15)
    if resp.status_code == 200:
        # collection exists (from before the LaTeX-aware rewrite); still make sure
        # the new element_type keyword index exists so filtered queries work.
        session.put(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/index",
            json={"field_name": "element_type", "field_schema": "keyword"},
            timeout=30,
        )
        # niche-term facet index (added with the relevance-scoring rewrite);
        # idempotent on an existing collection too.
        session.put(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/index",
            json={"field_name": "terms", "field_schema": "keyword"},
            timeout=30,
        )
        return
    log.info(f"creating Qdrant collection {COLLECTION_NAME}")
    body = {
        "vectors": {"size": 384, "distance": "Cosine"},
        "hnsw_config": {"m": 16, "ef_construct": 128},
    }
    r = session.put(f"{QDRANT_URL}/collections/{COLLECTION_NAME}", json=body, timeout=30)
    r.raise_for_status()
    for field in ("layers", "section_type", "arxiv_id", "element_type", "terms"):
        session.put(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/index",
            json={"field_name": field, "field_schema": "keyword"},
            timeout=30,
        )


def embed_texts_batch(session, texts):
    resp = session.post(EMBED_BATCH_URL, json={"texts": texts}, timeout=EMBED_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["vectors"]


def _run_embed_batch(session, batch):
    """Runs in a worker thread. batch: list of (arxiv_id, chunk_dict). Pure HTTP, no DB access."""
    texts = [c["text"] for _, c in batch]
    vectors = embed_texts_batch(session, texts)
    return batch, vectors


def fts_write(points):
    """Mirror freshly embedded chunks into the FTS5 index.

    Failure here must never cost a paper: the vectors are already written, and
    a missing lexical row only means this chunk is found by meaning and not by
    exact term until the next rebuild."""
    if not points:
        return
    try:
        fts = sqlite3.connect(FTS_DB_PATH, timeout=30)
    except sqlite3.Error as e:
        log.warning(f"fts: cannot open index: {e}")
        return
    try:
        rows = []
        for pt in points:
            pay = pt.get("payload") or {}
            if pay.get("text"):
                rows.append((pay["text"], str(pt["id"]), pay.get("arxiv_id"),
                             pay.get("section_type"), pay.get("element_type"),
                             str(pay.get("layers") or ""), pay.get("year")))
        if rows:
            fts.executemany(
                "INSERT INTO chunks (text, point_id, arxiv_id, section_type, "
                "element_type, layers, year) VALUES (?,?,?,?,?,?,?)", rows)
            fts.commit()
    except sqlite3.Error as e:
        log.warning(f"fts: write failed: {e}")
    finally:
        fts.close()


def embed_step(conn, session, limit=EMBED_CYCLE_LIMIT):
    rows = conn.execute(
        "SELECT arxiv_id, title, year, layers, citation_count, venue, niche_score, matched_terms, "
        "source_url FROM papers WHERE status='chunked' LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return 0
    processed = 0
    paper_meta = {}
    flat_items = []  # (arxiv_id, chunk_dict), preserves per-paper chunk order across papers
    for row in rows:
        arxiv_id = row["arxiv_id"]
        chunk_path = CHUNK_DIR / f"{safe_id(arxiv_id)}.json"
        if not chunk_path.exists():
            continue
        chunks = json.loads(chunk_path.read_text(encoding="utf-8"))
        # chunk_cache keeps the FULL structure (all section/element types) so
        # state.db/chunk_cache reflect the whole paper regardless of the embed
        # filter; only the embeddable subset goes to embed-small/Qdrant.
        embeddable = [c for c in chunks if extractor.should_embed(c)]
        if not embeddable:
            # nothing left to embed after the filter (or paper had 0 chunks):
            # safe to close out immediately, still counted 'done' as usual.
            conn.execute("UPDATE papers SET status='done', updated_at=? WHERE arxiv_id=?", (now_iso(), arxiv_id))
            conn.commit()
            processed += 1
            continue
        paper_meta[arxiv_id] = row
        for c in embeddable:
            flat_items.append((arxiv_id, c))
    if not flat_items:
        return processed

    # Batch embeds across multiple papers at once (was: 1 tiny per-paper call).
    # Batches run concurrently against embed-small; qdrant upsert + status commit
    # still happens per-paper, sequentially, in this thread only.
    # Group by length before batching. The tokenizer pads every text in a batch
    # up to the longest one, so a single 1700-char chunk sitting next to short
    # ones inflates the whole batch: wasted compute, and an arena that grows to
    # fit the worst case and never shrinks. Sorting keeps each batch uniform.
    # (The one-off corpus loader already did this; the live pipeline did not,
    # which is why real batches cost several times what the benchmark predicted.)
    # Papers must stay contiguous: a paper is only written out once every one
    # of its chunks has a vector, and anything still incomplete when this call
    # ends is discarded and redone. Sorting purely by length scattered each
    # paper across the whole run, so almost nothing completed and the work was
    # thrown away (490% CPU for 7 chunks written). Group by paper first, then
    # sort by length inside it to still cut padding waste.
    ordered = sorted(flat_items, key=lambda item: (item[0], len(item[1].get("text") or "")))
    batches = [ordered[i:i + EMBED_BATCH_TEXTS] for i in range(0, len(ordered), EMBED_BATCH_TEXTS)]
    paper_vectors = {}   # arxiv_id -> list[(chunk, vector)], filled as batches complete
    paper_expected = {}
    for aid, c in flat_items:
        paper_expected[aid] = paper_expected.get(aid, 0) + 1
    paper_failed = set()

    def _close_out_paper(arxiv_id):
        """Upsert this paper's completed vectors to qdrant and commit status='done'.
        Called only once a paper's full chunk set has vectors. Runs in the main
        thread (called from inside the as_completed loop below), so state.db
        commits stay strictly sequential -- streaming progress instead of
        waiting for the whole cycle's batches to finish."""
        nonlocal processed
        row = paper_meta[arxiv_id]
        got = paper_vectors.pop(arxiv_id)
        layers = row["layers"].split(",") if row["layers"] else []
        niche_score = row["niche_score"] if row["niche_score"] is not None else 0
        try:
            terms = json.loads(row["matched_terms"]) if row["matched_terms"] else []
        except (TypeError, ValueError):
            terms = []
        terms = terms[:12]  # cap payload size
        # the implementation link is half a paper's value to a practitioner;
        # the source is already on disk, so this costs one re-read
        repos = []
        try:
            src = read_latex_cache(arxiv_id)
            if src:
                repos = extractor.extract_repo_urls(src)
        except Exception:
            repos = []
        points = []
        for c, vec in got:
            point_id = str(uuid.uuid5(NAMESPACE_URL, f"{arxiv_id}#{c['chunk_index']}"))
            points.append({
                "id": point_id,
                "vector": vec,
                "payload": {
                    "arxiv_id": arxiv_id,
                    "title": row["title"],
                    "year": row["year"],
                    "layers": layers,
                    "section_type": c["section_type"],
                    "section_title": c["section_title"],
                    "chunk_index": c["chunk_index"],
                    "element_type": c.get("element_type", "prose"),
                    "has_math": c.get("has_math", False),
                    "has_algorithm": c.get("has_algorithm", False),
                    "part_index": c.get("part_index", 0),
                    "part_total": c.get("part_total", 1),
                    "text": c["text"],
                    "citation_count": row["citation_count"],
                    "venue": row["venue"],
                    "niche_score": niche_score,
                    "terms": terms,
                    "repos": repos,
                    "url": row["source_url"],
                },
            })
        try:
            for i in range(0, len(points), UPSERT_BATCH_SIZE):
                batch_pts = points[i:i + UPSERT_BATCH_SIZE]
                r = session.put(
                    f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points",
                    json={"points": batch_pts},
                    timeout=60,
                )
                r.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"embed: qdrant upsert failed for {arxiv_id}: {e}")
            return
        fts_write(points)
        conn.execute("UPDATE papers SET status='done', updated_at=? WHERE arxiv_id=?", (now_iso(), arxiv_id))
        conn.commit()
        processed += 1

    # Batches run concurrently against embed-small; qdrant upsert + status commit
    # happen per-paper, sequentially, in this (main) thread as soon as a paper's
    # last outstanding chunk lands -- not held back until every batch in the
    # cycle finishes. A crash mid-cycle leaves not-yet-closed-out papers at
    # 'chunked' (idempotent point ids), retried next cycle; already-closed papers
    # are already durably 'done'.
    with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as pool:
        futures = {pool.submit(_run_embed_batch, session, b): b for b in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                _, vectors = future.result()
            except requests.RequestException as e:
                aids = sorted({aid for aid, _ in batch})
                log.warning(f"embed: batch failed ({len(batch)} chunks, papers={aids}): {e}")
                paper_failed.update(aids)
                continue
            touched = set()
            for (aid, c), vec in zip(batch, vectors):
                paper_vectors.setdefault(aid, []).append((c, vec))
                touched.add(aid)
            for aid in touched:
                if aid in paper_failed:
                    continue
                if len(paper_vectors.get(aid, [])) == paper_expected.get(aid, -1):
                    _close_out_paper(aid)
    return processed


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def seed_latex_cache_from_fulltext(conn):
    """One-time backfill: papers already downloaded under the old pipeline have
    their resolved LaTeX sitting in fulltext_cache/<id>.tex. Copy those into
    latex_cache/<id>/source.tex so re-processing never re-hits arXiv for them."""
    if _sched_get(conn, "latex_cache_seeded_v1") == "1":
        return 0
    rows = conn.execute(
        "SELECT arxiv_id FROM papers WHERE fulltext_source='latex' "
        "AND status IN ('fulltext_fetched','chunked','done')"
    ).fetchall()
    seeded = 0
    for row in rows:
        arxiv_id = row["arxiv_id"]
        if latex_cache_path(arxiv_id).exists():
            continue
        src = FULLTEXT_DIR / f"{safe_id(arxiv_id)}.tex"
        if not src.exists():
            continue
        try:
            write_latex_cache(arxiv_id, src.read_text(encoding="utf-8", errors="ignore"))
            seeded += 1
        except OSError:
            continue
    if seeded:
        log.info(f"seeded latex_cache from fulltext_cache for {seeded} papers")
    _sched_set(conn, "latex_cache_seeded_v1", "1")
    return seeded


def status_counts(conn):
    rows = conn.execute("SELECT status, COUNT(*) c FROM papers GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}


def run():
    conn = get_conn()
    init_db(conn)
    session = requests.Session()
    ensure_collection(session)
    seed_latex_cache_from_fulltext(conn)
    refine_capped_cursors(conn)
    log.info(f"S2 API key: {'enabled' if S2_API_KEY else 'absent'}")

    counts = status_counts(conn)
    if counts:
        log.info(f"RESUME: existing state.db found, counts={counts}")
    else:
        log.info("starting fresh (no prior state found)")

    start_watchdog()
    run_stages_parallel()


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------
# The stages used to share one loop, so the slowest of them set the pace for
# all: while harvest waited out arXiv's three-second courtesy delay, the
# embedder sat idle, and vice versa. They are independent by nature -- each
# picks up whatever the previous one has committed to state.db -- so each now
# runs in its own thread with its own connection. WAL mode gives concurrent
# readers plus one writer, and every stage still commits per paper, so
# durability is unchanged: a crash mid-stage leaves the paper at its last
# committed status and the stage resumes there.
STAGE_BUSY_SLEEP = 1     # work remains, come straight back
STAGE_IDLE_SLEEP = 20    # queue empty, stop hammering the database
STAGE_ERROR_SLEEP = 30
REPORT_INTERVAL = 300

_heartbeats = {}
_heartbeats_lock = threading.Lock()


def stage_heartbeat(name):
    with _heartbeats_lock:
        _heartbeats[name] = time.time()


def newest_heartbeat():
    """Most recent sign of life across stages; an idle stage still ticks."""
    with _heartbeats_lock:
        return max(_heartbeats.values()) if _heartbeats else time.time()


def _stage_loop(name, fn, wants_session):
    conn = get_conn()
    session = requests.Session() if wants_session else None
    log.info(f"stage {name}: started")
    while True:
        try:
            processed = fn(conn, session) if wants_session else fn(conn)
            stage_heartbeat(name)
            if processed:
                log.info(f"stage {name}: {processed}")
                time.sleep(STAGE_BUSY_SLEEP)
            else:
                time.sleep(STAGE_IDLE_SLEEP)
        except Exception:
            log.exception(f"stage {name} failed, retrying shortly")
            stage_heartbeat(name)
            time.sleep(STAGE_ERROR_SLEEP)


def _reporter_loop():
    conn = get_conn()
    while True:
        time.sleep(REPORT_INTERVAL)
        try:
            log.info(f"counts={status_counts(conn)}")
            stage_heartbeat("reporter")
        except Exception:
            log.exception("reporter failed")


def recheck_step(conn, session):
    """Re-examine deferred papers once the world has had time to cite them.

    Nothing here lowers the bar: a deferred paper faces exactly the same
    threshold as everyone else, just later, when a citation count means
    something. Papers that reach DEFER_GIVE_UP_MONTHS without clearing it are
    rejected for good."""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - RECHECK_AFTER_DAYS * 86400))
    quarterly = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - 90 * 86400))
    rows = conn.execute(
        "SELECT arxiv_id, year, niche_score FROM papers WHERE status='deferred' "
        "AND updated_at < ? LIMIT ?", (cutoff, S2_BATCH_SIZE)).fetchall()
    if not rows:
        # second chance for papers that were refused while clearly on topic:
        # citations accumulate, and a niche subject accumulates them slowly
        rows = conn.execute(
            "SELECT arxiv_id, year, niche_score FROM papers WHERE status='rejected' "
            "AND niche_score >= ? AND updated_at < ? LIMIT ?",
            (NICHE_DISCOUNT_FROM, quarterly, S2_BATCH_SIZE)).fetchall()
    if not rows:
        return 0

    ids = [r["arxiv_id"] for r in rows]
    meta = fetch_s2_batch(session, ids)
    if meta is None:
        return 0

    promoted = rejected = 0
    for row in rows:
        arxiv_id = row["arxiv_id"]
        paper = meta.get(arxiv_id)
        if paper is None:
            citation_count, influential, venue, year, top_lab = 0, 0, None, row["year"], False
        else:
            citation_count = paper.get("citationCount") or 0
            influential = paper.get("influentialCitationCount") or 0
            venue = paper.get("venue") or (paper.get("publicationVenue") or {}).get("name")
            year = paper.get("year") or row["year"]
        verdict = quality_verdict(venue, citation_count, influential, year, arxiv_id,
                                  row["niche_score"] if "niche_score" in row.keys() else None)
        status = {"pass": "quality_checked", "defer": "deferred"}.get(verdict, "rejected")
        conn.execute(
            "UPDATE papers SET citation_count=?, influential=?, venue=?, year=?, "
            "passed=?, status=?, updated_at=? WHERE arxiv_id=?",
            (citation_count, influential, venue, year, 1 if verdict == "pass" else 0,
             status, now_iso(), arxiv_id),
        )
        promoted += verdict == "pass"
        rejected += verdict == "reject"
    conn.commit()
    if promoted or rejected:
        log.info(f"recheck: {len(rows)} deferred revisited, {promoted} promoted, "
                 f"{rejected} finally rejected")
    return len(rows)


# The bibliography as a harvest source. Every arXiv cursor is exhausted and the
# category sweep is done, but the corpus keeps pointing at work the index does
# not hold: 54 664 distinct papers are cited from inside it and missing from it.
# Unlike a query, this channel needs nobody to think of the right words -- the
# papers were chosen by the authors we already trust, and how often they are
# cited from inside our own three subjects is a relevance signal no search gives.
#
# Measured on 400 candidates cited three or more times: 46% clear the niche
# filter. The rejects are exactly what should be rejected (ResNet, MS COCO,
# Latent Diffusion) and the passes are exactly what should pass (Deep
# Compression, Code as Policies), so the existing filter is doing the work and
# nothing here needs to second-guess it.
# Dropped from 3 to 1 once the stricter tier ran dry: papers cited three or
# more times from inside the corpus lasted a single day (3656 admitted, then
# zero candidates left and the pipeline stood still for 29 hours). Cited once
# is still a deliberate reference by an author already in the index, which is a
# stronger signal than any keyword query, and the niche filter and the quality
# gate both still stand between the candidate and the corpus.
# Remaining at this threshold: 45 263.
GRAPH_HARVEST_MIN_CITES = 1
GRAPH_HARVEST_BATCH = 100
_graph_queue = []


def fetch_s2_titles(session, arxiv_ids):
    """Title, abstract and year for ids we do not hold. Returns None to retry.

    Separate from fetch_s2_batch because the pipeline's field list carries no
    title: everywhere else the title arrives from arXiv at harvest time, and
    here there is no harvest to take it from."""
    if not arxiv_ids:
        return {}
    s2_gate.wait()
    try:
        resp = session.post(
            S2_API_URL,
            params={"fields": "title,abstract,year"},
            json={"ids": [f"ARXIV:{i}" for i in arxiv_ids]},
            headers={"x-api-key": S2_API_KEY} if S2_API_KEY else None,
            timeout=60,
        )
    except requests.RequestException as e:
        log.warning(f"graph_harvest: request error: {e}")
        return None
    if resp.status_code == 429:
        s2_gate.penalize(S2_PENALTY_SECONDS)
        return None
    if resp.status_code != 200:
        log.warning(f"graph_harvest: S2 status {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    time.sleep(S2_PAUSE)
    return {pid: paper for pid, paper in zip(arxiv_ids, data)}


def acl_seed_cursors(conn):
    """One cursor per volume file, taken from the repository tree.

    The file list needs a network call, so it cannot live in ALL_QUERY_KEYS
    (which is built at import time and must not depend on the network being up
    when the service starts). The cursors are the durable part: created once,
    then drained across restarts like every other query."""
    if not _poll_due(conn, "acl:tree", SPEC_REFRESH_SECONDS):
        return 0
    try:
        req = urllib.request.Request(ACL_TREE_URL, headers={
            "User-Agent": ARXIV_CONTACT_UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            tree = json.loads(resp.read())
    except Exception as e:
        log.warning(f"acl: tree listing failed: {e}")
        return 0
    names = sorted({t["path"].split("/")[-1] for t in tree.get("tree", [])
                    if t["path"].startswith("data/xml/") and t["path"].endswith(".xml")})
    added = 0
    for name in names:
        year = name[:4]
        if not (year.isdigit() and int(year) >= ACL_START_YEAR):
            continue
        conn.execute("INSERT OR IGNORE INTO harvest_cursor (query_key, next_start, done, layer) "
                     "VALUES (?,0,0,?)", (f"acl@{name}", "llm-slm"))
        added += 1
    conn.commit()
    _mark_polled(conn, "acl:tree")
    log.info(f"acl: {added} volume files queued")
    return added


def acl_harvest_step(conn):
    """Take one ACL volume file per turn."""
    acl_seed_cursors(conn)
    row = conn.execute(
        "SELECT query_key FROM harvest_cursor WHERE query_key LIKE 'acl@%' AND done=0 "
        "ORDER BY query_key DESC LIMIT 1").fetchone()
    if row is None:
        return 0
    key = row["query_key"]
    name = key.split("@", 1)[1]
    try:
        req = urllib.request.Request(ACL_XML_BASE + name,
                                     headers={"User-Agent": ARXIV_CONTACT_UA})
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read()
    except Exception as e:
        log.warning(f"acl: fetch failed for {name}: {e}")
        return 0                      # cursor stays open, retried next cycle
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log.warning(f"acl: parse failed for {name}: {e}")
        set_cursor(conn, key, 0, 1)   # a broken file will not fix itself
        return 0

    year = int(name[:4])
    taken = 0
    for paper in root.findall(".//paper"):
        url_el = paper.find("url")
        title_el = paper.find("title")
        if url_el is None or url_el.text is None or title_el is None:
            continue
        title = " ".join("".join(title_el.itertext()).split())
        abs_el = paper.find("abstract")
        abstract = " ".join("".join(abs_el.itertext()).split()) if abs_el is not None else None
        if not title:
            continue
        upsert_discovered(conn, f"{ACL_ID_PREFIX}{url_el.text.strip()}", title, year,
                          "llm-slm", abstract=abstract)
        taken += 1
    set_cursor(conn, key, taken, 1)
    conn.commit()
    log.info(f"acl: {name} -> {taken} papers")
    return taken


def _openalex_abstract(inverted):
    if not isinstance(inverted, dict):
        return None
    words = []
    for token, positions in inverted.items():
        for pos in positions or []:
            if isinstance(pos, int):
                words.append((pos, token))
    return " ".join(token for _, token in sorted(words)) or None


def openalex_seed_cursors(conn):
    added = 0
    for idx, _ in enumerate(OPENALEX_QUERIES):
        for key in (f"openalex@{idx}", f"openalex-fresh@{idx}"):
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO harvest_cursor "
                "(query_key,next_start,done,layer) VALUES (?,0,0,'web3')", (key,))
            added += conn.total_changes - before
    conn.commit()
    if added:
        log.info(f"openalex: {added} durable query cursors queued")
    return added


def openalex_harvest_step(conn, session):
    """Discover OA Web3 papers outside arXiv, one durable page at a time."""
    openalex_seed_cursors(conn)
    if _poll_due(conn, "openalex:fresh", FRESH_POLL_SECONDS):
        conn.execute(
            "UPDATE harvest_cursor SET next_start=0,done=0,last_run_at=NULL "
            "WHERE query_key LIKE 'openalex-fresh@%'")
        conn.commit()
        _mark_polled(conn, "openalex:fresh")
    row = conn.execute(
        "SELECT query_key,next_start FROM harvest_cursor "
        "WHERE (query_key LIKE 'openalex@%' OR query_key LIKE 'openalex-fresh@%') "
        "AND done=0 "
        "ORDER BY COALESCE(last_run_at,''), query_key LIMIT 1").fetchone()
    if row is None:
        return 0
    key = row["query_key"]
    fresh = key.startswith("openalex-fresh@")
    idx = int(key.split("@", 1)[1])
    query = OPENALEX_QUERIES[idx]
    page = int(row["next_start"] or 0) + 1
    from_date = ((datetime.now(timezone.utc) - timedelta(days=FRESH_WINDOW_DAYS)).date().isoformat()
                 if fresh else f"{OPENALEX_START_YEAR}-01-01")
    openalex_gate.wait()
    try:
        resp = session.get(OPENALEX_API_URL, params={
            "search": query,
            "filter": (f"from_publication_date:{from_date},"
                       "is_oa:true,has_abstract:true"),
            "sort": "publication_date:desc" if fresh else "cited_by_count:desc",
            "page": page,
            "per-page": OPENALEX_PAGE_SIZE,
            "select": ("id,title,publication_year,cited_by_count,ids,"
                       "abstract_inverted_index,best_oa_location,primary_location"),
        }, timeout=60, headers={"User-Agent": ARXIV_CONTACT_UA})
    except requests.RequestException as e:
        log.warning(f"openalex: request failed for {query!r}: {e}")
        return 0
    if resp.status_code == 429:
        openalex_gate.penalize(60)
        log.warning("openalex: 429 rate limited, backing off")
        return 0
    if resp.status_code != 200:
        log.warning(f"openalex: status {resp.status_code}: {resp.text[:200]}")
        return 0
    try:
        results = resp.json().get("results") or []
    except ValueError:
        return 0

    taken = 0
    for work in results:
        title = (work.get("title") or "").strip()
        location = work.get("best_oa_location") or {}
        pdf_url = location.get("pdf_url")
        if not title or not pdf_url:
            continue
        ids = work.get("ids") or {}
        arxiv_url = ids.get("arxiv")
        if arxiv_url:
            paper_id = arxiv_url.rstrip("/").split("/")[-1]
        else:
            oa_id = (work.get("id") or "").rstrip("/").split("/")[-1]
            if not oa_id:
                continue
            paper_id = OPENALEX_ID_PREFIX + oa_id
        venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
        upsert_discovered(
            conn, paper_id, title, work.get("publication_year"), "web3",
            abstract=_openalex_abstract(work.get("abstract_inverted_index")),
            source_url=pdf_url,
            citation_count=work.get("cited_by_count") or 0,
            venue=venue,
            commit=False,
        )
        taken += 1
    page_limit = OPENALEX_FRESH_MAX_PAGES if fresh else OPENALEX_MAX_PAGES
    done = page >= page_limit or len(results) < OPENALEX_PAGE_SIZE
    conn.execute(
        "UPDATE harvest_cursor SET next_start=?,done=?,last_run_at=? WHERE query_key=?",
        (page, 1 if done else 0, now_iso(), key),
    )
    conn.commit()
    log.info(f"openalex: {query!r} page {page} -> {taken} OA PDFs")
    return taken


def hal_seed_cursors(conn):
    added = 0
    for idx, _ in enumerate(HAL_QUERIES):
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO harvest_cursor "
            "(query_key,next_start,done,layer) VALUES (?,0,0,'web3')", (f"{HAL_CURSOR_PREFIX}{idx}",))
        added += conn.total_changes - before
    conn.commit()
    if added:
        log.info(f"hal: {added} durable query cursors queued")
    return added


def hal_harvest_step(conn, session):
    """Harvest one durable page of full-PDF Web3 research from HAL."""
    hal_seed_cursors(conn)
    row = conn.execute(
        "SELECT query_key,next_start FROM harvest_cursor WHERE query_key LIKE 'hal-v2@%' "
        "AND done=0 ORDER BY COALESCE(last_run_at,''),query_key LIMIT 1"
    ).fetchone()
    if row is None:
        return 0
    key = row["query_key"]
    start = int(row["next_start"] or 0)
    idx = int(key.split("@", 1)[1])
    hal_gate.wait()
    try:
        resp = session.get(HAL_API_URL, params={
            "q": HAL_QUERIES[idx],
            "fq": ["submitType_s:file", "producedDateY_i:[2015 TO *]"],
            "fl": "docid,title_s,abstract_s,producedDateY_i,fileMain_s,uri_s",
            "rows": HAL_PAGE_SIZE,
            "start": start,
            "sort": "producedDateY_i desc",
            "wt": "json",
        }, timeout=60, headers={"User-Agent": ARXIV_CONTACT_UA})
    except requests.RequestException as e:
        log.warning(f"hal: request failed for {HAL_QUERIES[idx]!r}: {e}")
        return 0
    if resp.status_code == 429:
        hal_gate.penalize(60)
        log.warning("hal: 429 rate limited, backing off")
        return 0
    if resp.status_code != 200:
        log.warning(f"hal: status {resp.status_code}: {resp.text[:200]}")
        return 0
    try:
        docs = (resp.json().get("response") or {}).get("docs") or []
    except ValueError:
        return 0

    taken = 0
    for doc in docs:
        docid = str(doc.get("docid") or "").strip()
        titles = doc.get("title_s") or []
        title = " ".join(titles[0].split()) if titles else ""
        pdf_url = doc.get("fileMain_s")
        if not docid or not title or not pdf_url:
            continue
        abstracts = doc.get("abstract_s") or []
        upsert_discovered(
            conn, f"{HAL_ID_PREFIX}{docid}", title,
            doc.get("producedDateY_i") or 0, "web3",
            abstract=_plain_html(abstracts[0]) if abstracts else None,
            source_url=pdf_url, venue="HAL Open Science", commit=False,
        )
        taken += 1
    done = (start // HAL_PAGE_SIZE) + 1 >= HAL_MAX_PAGES or len(docs) < HAL_PAGE_SIZE
    conn.execute(
        "UPDATE harvest_cursor SET next_start=?,done=?,last_run_at=? WHERE query_key=?",
        (start + HAL_PAGE_SIZE, 1 if done else 0, now_iso(), key),
    )
    conn.commit()
    log.info(f"hal: {HAL_QUERIES[idx]!r} start={start} -> {taken} full-PDF records")
    return taken


def _plain_html(value):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def pmlr_seed_cursors(conn, session):
    """Discover new PMLR volumes daily and reopen only the newest few."""
    if not _poll_due(conn, "pmlr:tree", SPEC_REFRESH_SECONDS):
        return 0
    try:
        resp = session.get(PMLR_INDEX_URL, timeout=60,
                           headers={"User-Agent": ARXIV_CONTACT_UA})
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"pmlr: index failed: {e}")
        return 0
    volumes = sorted({int(v) for v in re.findall(r'href=["\'][^"\']*v(\d+)/?["\']', resp.text)
                      if int(v) >= PMLR_MIN_VOLUME}, reverse=True)
    added = 0
    for volume in volumes:
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO harvest_cursor "
            "(query_key,next_start,done,layer) VALUES (?,0,0,'llm-slm')",
            (f"pmlr@v{volume}",))
        added += conn.total_changes - before
    for volume in volumes[:PMLR_REFRESH_VOLUMES]:
        conn.execute(
            "UPDATE harvest_cursor SET done=0,last_run_at=NULL "
            "WHERE query_key=?", (f"pmlr@v{volume}",))
    conn.commit()
    _mark_polled(conn, "pmlr:tree")
    log.info(f"pmlr: {added} new volumes, {len(volumes)} known")
    return added


def pmlr_harvest_step(conn, session):
    """Read one PMLR volume; title and PDF URL are present on its index page."""
    pmlr_seed_cursors(conn, session)
    row = conn.execute(
        "SELECT query_key FROM harvest_cursor WHERE query_key LIKE 'pmlr@%' AND done=0 "
        "ORDER BY CAST(substr(query_key,7) AS INTEGER) DESC LIMIT 1").fetchone()
    if row is None:
        return 0
    key = row["query_key"]
    volume = key.split("@v", 1)[1]
    url = f"{PMLR_INDEX_URL}v{volume}/"
    try:
        resp = session.get(url, timeout=90, headers={"User-Agent": ARXIV_CONTACT_UA})
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"pmlr: volume v{volume} failed: {e}")
        return 0
    published = re.search(
        r"Published\s+as\s+Volume.*?(20\d{2})",
        resp.text[:10000], re.I | re.S,
    )
    year = int(published.group(1)) if published else 0
    taken = 0
    blocks = re.findall(r'<div class="paper">(.*?)</div>', resp.text, re.I | re.S)
    for block in blocks:
        title_m = re.search(r'<p class="title">(.*?)</p>', block, re.I | re.S)
        pdf_m = re.search(r'href="([^"]+\.pdf)"', block, re.I)
        abs_m = re.search(r'href="https?://proceedings\.mlr\.press/(v\d+/[^"/]+)\.html"',
                          block, re.I)
        venue_m = re.search(r'<span class="info">\s*<i>(.*?)</i>', block, re.I | re.S)
        if not title_m or not pdf_m or not abs_m:
            continue
        title = _plain_html(title_m.group(1))
        if not title:
            continue
        upsert_discovered(
            conn, f"{PMLR_ID_PREFIX}{abs_m.group(1)}", title, year, "llm-slm",
            source_url=html.unescape(pdf_m.group(1)),
            venue=_plain_html(venue_m.group(1)) if venue_m else "PMLR",
            commit=False,
        )
        taken += 1
    conn.execute(
        "UPDATE harvest_cursor SET next_start=?,done=1,last_run_at=? WHERE query_key=?",
        (taken, now_iso(), key))
    conn.commit()
    log.info(f"pmlr: v{volume} -> {taken} papers")
    return taken


def graph_harvest_step(conn, session):
    """Pull in works the corpus cites but does not hold."""
    global _graph_queue
    if not _graph_queue:
        # The scan is over two million edges, so it is done once and drained
        # from memory rather than re-run every few seconds.
        _graph_queue = [r["dst"] for r in conn.execute(
            "SELECT c.dst AS dst, COUNT(*) AS k FROM citations c "
            "WHERE NOT EXISTS (SELECT 1 FROM papers p WHERE p.arxiv_id = c.dst) "
            "GROUP BY c.dst HAVING k >= ? ORDER BY k DESC LIMIT 5000",
            (GRAPH_HARVEST_MIN_CITES,))]
        if not _graph_queue:
            return 0
        log.info(f"graph_harvest: {len(_graph_queue)} cited-but-missing papers queued")

    batch, _graph_queue = _graph_queue[:GRAPH_HARVEST_BATCH], _graph_queue[GRAPH_HARVEST_BATCH:]
    meta = fetch_s2_titles(session, batch)
    if meta is None:
        _graph_queue = batch + _graph_queue   # transient failure: keep the slice
        return 0

    taken = unresolved = 0
    for pid in batch:
        paper = meta.get(pid)
        if not paper or not paper.get("title"):
            # Nothing to classify it with. Parked so the scan does not offer it
            # again on every pass; it is not a rejection on the merits.
            conn.execute(
                "INSERT OR IGNORE INTO papers (arxiv_id, title, status, niche_score, updated_at) "
                "VALUES (?,?,?,?,?)", (pid, "", "graph_unresolved", 0, now_iso()))
            unresolved += 1
            continue
        upsert_discovered(conn, pid, paper["title"], paper.get("year"), "llm-slm",
                          abstract=paper.get("abstract"))
        taken += 1
    conn.commit()
    if taken or unresolved:
        log.info(f"graph_harvest: {taken} classified, {unresolved} without metadata, "
                 f"{len(_graph_queue)} left in queue")
    return taken


STAGES = (
    ("harvest", lambda conn: harvest_step(conn), False),
    ("graph_harvest", graph_harvest_step, True),
    ("acl_harvest", lambda conn: acl_harvest_step(conn), False),
    ("openalex", openalex_harvest_step, True),
    ("hal_harvest", hal_harvest_step, True),
    ("github_docs", lambda conn: github_docs_step(conn), False),
    ("pmlr", pmlr_harvest_step, True),
    ("quality", quality_step, True),
    ("fulltext", lambda conn: fulltext_step(conn), False),
    ("chunk", chunk_step, True),
    ("embed", embed_step, True),
    ("recheck", recheck_step, True),
    ("refs", refs_step, True),
    ("iacr_twin", lambda conn: iacr_twin_step(conn), False),
)


def run_stages_parallel():
    threads = []
    for name, fn, wants_session in STAGES:
        stage_heartbeat(name)
        t = threading.Thread(target=_stage_loop, args=(name, fn, wants_session),
                             name=f"stage-{name}", daemon=True)
        t.start()
        threads.append(t)
    threading.Thread(target=_reporter_loop, name="reporter", daemon=True).start()
    log.info(f"running {len(threads)} stages in parallel")
    while True:
        time.sleep(60)
        for t in threads:
            if not t.is_alive():
                log.error(f"{t.name} died; exiting so systemd restarts the service")
                logging.shutdown()
                os._exit(1)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
# Restart=always only helps when the process dies. A stage that wedges (one
# pathological LaTeX file once burned 11.5h at 70% CPU) leaves a live process
# making no progress, which systemd happily leaves alone. The heartbeat turns
# "no progress" into an exit so systemd can do its job.
WATCHDOG_STALL_SECONDS = 45 * 60
WATCHDOG_POLL_SECONDS = 60
_last_heartbeat = time.time()


def heartbeat():
    global _last_heartbeat
    _last_heartbeat = time.time()


def start_watchdog():
    def _watch():
        while True:
            time.sleep(WATCHDOG_POLL_SECONDS)
            stalled = time.time() - newest_heartbeat()
            if stalled > WATCHDOG_STALL_SECONDS:
                log.error(
                    "watchdog: no progress for %.0f min, exiting so systemd restarts",
                    stalled / 60,
                )
                logging.shutdown()
                os._exit(1)

    t = threading.Thread(target=_watch, name="watchdog", daemon=True)
    t.start()
    log.info(f"watchdog armed: exit if no progress for {WATCHDOG_STALL_SECONDS // 60} min")


def main():
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another instance is already running (lock held). Exiting.")
        sys.exit(1)
    try:
        run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("service crashed with an unhandled exception")
        raise


if __name__ == "__main__":
    main()
