"""Pure functions shared by build_citation_bench.py and run_bench.py.

Kept dependency-free (no requests/sqlite here) so eval/tests/test_bench.py can
exercise them without network or a database.
"""
import hashlib
import math
import re

# ---------------- citation-context filtering ----------------

# A single bracket-group with exactly one number: [31]. Rejects [3,5], [3-7],
# [3, 5, 9], and groups containing a range or a comma.
_SINGLE_BRACKET_RE = re.compile(r"\[\s*(\d+)\s*\]")
_ANY_BRACKET_RE = re.compile(r"\[[^\]]*\]")

# Single author-year markers: "(Hu et al., 2021)" / "Hu et al. (2021)" /
# "(Smith and Jones, 2020)" / "(Smith, 2020)". Deliberately conservative: one
# name-group, one year.
_NAME_GROUP = (
    r"[A-Z][\w'-]+\s+et\s+al\.|"          # Hu et al.
    r"[A-Z][\w'-]+\s+(?:and|&)\s+[A-Z][\w'-]+|"  # Smith and Jones
    r"[A-Z][\w'-]+"                        # Smith
)
_PAREN_AUTHOR_YEAR_RE = re.compile(
    r"\((?:" + _NAME_GROUP + r"),?\s*(\d{4}[a-z]?)\)"
)
_INLINE_AUTHOR_YEAR_RE = re.compile(
    r"\b(?:" + _NAME_GROUP + r")\s*\(\s*(\d{4}[a-z]?)\s*\)"
)

# Any bracket group with a comma or a dash range, or multiple numbers.
_MULTI_BRACKET_RE = re.compile(r"\[\s*\d+\s*(?:[,-]\s*\d+\s*)+\]")

LISTY_PHRASES = [
    "compare with", "compared with", "comparing with",
    "baseline", "baselines",
    "following", "we follow",
    "we use", "we adopt", "we employ",
    "as in", "such as", "similar to",
    "e.g.,", "e.g.", "i.e.,", "i.e.",
]


def _find_all_marker_spans(text):
    """Return list of (start, end) spans for every citation-like marker."""
    spans = []
    for m in _MULTI_BRACKET_RE.finditer(text):
        spans.append((m.start(), m.end(), "multi"))
    for m in _SINGLE_BRACKET_RE.finditer(text):
        # skip if already covered by a multi-bracket span
        if any(m.start() >= s and m.end() <= e for s, e, _k in spans):
            continue
        spans.append((m.start(), m.end(), "single"))
    for m in _PAREN_AUTHOR_YEAR_RE.finditer(text):
        spans.append((m.start(), m.end(), "author_year"))
    for m in _INLINE_AUTHOR_YEAR_RE.finditer(text):
        # avoid double counting a paren-author-year already found
        if any(m.start() >= s and m.end() <= e for s, e, _k in spans):
            continue
        spans.append((m.start(), m.end(), "author_year"))
    spans.sort()
    return spans


def extract_single_marker(text):
    """If `text` contains exactly one citation marker (single-number bracket or
    single author-year), return (marker_kind, span). Otherwise return None.

    Rejects: multiple bracket groups, ranges/lists in one bracket, a bracket
    plus an author-year mention, several author-year mentions.
    """
    spans = _find_all_marker_spans(text)
    if len(spans) != 1:
        return None
    start, end, kind = spans[0]
    if kind == "multi":
        return None
    return kind, (start, end)


def strip_marker(text, span):
    start, end = span
    cleaned = text[:start] + " " + text[end:]
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    return cleaned


def is_listy_or_filler(cleaned_text):
    """Reject context sentences that are citation lists or content-free
    boilerplate ("we use X [1]", "compare with baselines").
    """
    lowered = cleaned_text.lower()
    return any(phrase in lowered for phrase in LISTY_PHRASES)


def is_usable_context(raw_text):
    """Full pipeline: exactly one marker, plausible length, not listy/filler.

    Returns (query, marker_kind) on success, or None.
    """
    if not raw_text:
        return None
    found = extract_single_marker(raw_text)
    if not found:
        return None
    kind, span = found
    cleaned = strip_marker(raw_text, span)
    word_count = len(cleaned.split())
    if not (60 <= len(cleaned) <= 400):
        return None
    if word_count < 8:
        return None
    if is_listy_or_filler(cleaned):
        return None
    # S2 truncates long contexts with an ellipsis, and a second citation often
    # survives as "(Huang et al., 2025a; ...)" or a lone "al., 2021)" fragment:
    # either way the sentence is about more than the target.
    if cleaned.startswith(("…", "...")) or _RESIDUAL_CITE_RE.search(cleaned):
        return None
    return cleaned, kind


_RESIDUAL_CITE_RE = re.compile(
    r"et al|\bal\.,|\b(?:19|20)\d\d[a-z]?\s*[;)]|\[\s*\d")


def content_tokens(text):
    # a five-letter prefix is a crude stem, enough to let "adapters" meet
    # "adaptation" without pulling in a stemming library
    return {t.lower()[:5] for t in _TOKEN_RE.findall(text or "")
            if len(t) >= 4 and t.lower() not in _STOPWORDS and not t.isdigit()}


def is_grounded(query, title, abstract, min_shared=2):
    """The context must share a few content words with the target itself.

    Contexts like "these benchmarks still fall short" cite the right paper but
    could describe a hundred others; no retriever can be graded on them.
    """
    return len(content_tokens(query) & content_tokens(f"{title} {abstract}")) >= min_shared


# ---------------- named vs. descriptive ----------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-']*")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "using", "based", "learning",
    "model", "models", "network", "networks", "method", "methods", "approach",
    "toward", "towards", "system", "systems", "framework", "language", "large",
    "study", "analysis", "paper", "over", "via", "data",
}


def title_signature_tokens(title):
    """Significant tokens from a title: length >= 4 non-stopwords, or ALLCAPS
    acronyms of length >= 2.
    """
    if not title:
        return set()
    tokens = set()
    for tok in _TOKEN_RE.findall(title):
        if tok.isupper() and len(tok) >= 2:
            tokens.add(tok.lower())
        elif len(tok) >= 4 and tok.lower() not in _STOPWORDS:
            tokens.add(tok.lower())
    return tokens


def is_named_query(query, title):
    """True if `query` contains at least one significant token/acronym from
    the target's title (case-insensitive whole-word match).
    """
    sig = title_signature_tokens(title)
    if not sig:
        return False
    query_tokens = {t.lower() for t in _TOKEN_RE.findall(query)}
    return bool(sig & query_tokens)


# ---------------- deterministic split ----------------

def split_for_target(target_id, buckets=("dev", "test")):
    """sha1(target_id) % len(buckets) -> deterministic split, keyed by TARGET
    (not by individual query), so dev/test never mix queries about the same
    target.
    """
    digest = hashlib.sha1(target_id.encode("utf-8")).hexdigest()
    idx = int(digest, 16) % len(buckets)
    return buckets[idx]


def popularity_bucket(in_degree):
    if in_degree <= 4:
        return "1-4"
    if in_degree <= 49:
        return "5-49"
    return "50+"


# ---------------- metrics ----------------

def rank_of_target(result_arxiv_ids, target_id, source_id=None):
    """1-based rank of target_id within result_arxiv_ids, excluding source_id
    (a query should not be able to find the citing paper itself in a corpus
    that includes it). Returns None if not found.
    """
    filtered = [aid for aid in result_arxiv_ids if aid != source_id]
    for i, aid in enumerate(filtered, start=1):
        if aid == target_id:
            return i
    return None


def hit_at_k(rank, k):
    return rank is not None and rank <= k


def reciprocal_rank(rank, k=10):
    if rank is not None and rank <= k:
        return 1.0 / rank
    return 0.0


def ndcg_at_k(rank, k=10):
    """nDCG with exactly one relevant document: DCG = 1/log2(rank+1) if the
    hit is within the top k, ideal DCG = 1/log2(1+1) = 1, so nDCG = DCG.
    """
    if rank is not None and rank <= k:
        return 1.0 / math.log2(rank + 1)
    return 0.0


def aggregate_metrics(rows):
    """rows: iterable of dicts with a 'rank' key (int or None) and optional
    'error'/'partial' flags. Returns a metrics dict.
    """
    rows = list(rows)
    n = len(rows)
    if n == 0:
        return {"n": 0}
    errors = sum(1 for r in rows if r.get("error"))
    partial = sum(1 for r in rows if r.get("partial"))
    latencies = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)

    def pct(p):
        if not latencies:
            return None
        idx = min(len(latencies) - 1, int(round(p * (len(latencies) - 1))))
        return latencies[idx]

    metrics = {
        "n": n,
        "hit@1": sum(hit_at_k(r["rank"], 1) for r in rows) / n,
        "hit@3": sum(hit_at_k(r["rank"], 3) for r in rows) / n,
        "hit@10": sum(hit_at_k(r["rank"], 10) for r in rows) / n,
        "mrr@10": sum(reciprocal_rank(r["rank"], 10) for r in rows) / n,
        "ndcg@10": sum(ndcg_at_k(r["rank"], 10) for r in rows) / n,
        "error_rate": errors / n,
        "partial_rate": partial / n,
        "p50_latency_ms": pct(0.50),
        "p95_latency_ms": pct(0.95),
    }
    return metrics


def group_by(rows, key_fn):
    groups = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r)
    return groups


def compare_metrics(metrics_a, metrics_b):
    """Delta of every shared numeric metric, b - a."""
    delta = {}
    for k in metrics_a:
        va, vb = metrics_a.get(k), metrics_b.get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            delta[k] = round(vb - va, 4)
    return delta
