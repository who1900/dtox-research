#!/usr/bin/env python3
"""Build an independent citation-context search benchmark.

Idea: a sentence where paper A cites paper B is a natural-language query with
a known correct answer (B). Contexts come from the Semantic Scholar Graph API
(GET /graph/v1/paper/arXiv:{id}/citations?fields=contexts,intents,isInfluential,
externalIds), which is unrelated to dtox's own retrieval stack -- so ranking
against it is not circular.

Runs on the server, where state.db (papers + citations tables) lives.

    set -a; . /opt/dtox-research/s2.env; set +a
    python eval/build_citation_bench.py --per-layer 120 --seed 7 \
        --cache-dir eval/.cache_s2 --out eval/bench/citation_bench.jsonl

See eval/README.md for full details and known limitations.
"""
import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval import bench_lib as bl  # noqa: E402

LAYERS = ["llm-slm", "ai-agents", "web3", "builder-tech"]
S2_BASE = "https://api.semanticscholar.org/graph/v1/paper/arXiv:{id}/citations"
S2_FIELDS = "contexts,intents,isInfluential,externalIds"
S2_LIMIT = 1000
MIN_PAUSE_S = 1.1
MAX_CONTEXTS_PER_TARGET = 2
PREFERRED_INTENTS = {"methodology", "background"}


def is_plain_arxiv_id(arxiv_id):
    """True for a plain arXiv id (no source prefix like 'eip:' or 'simd:')."""
    return bool(arxiv_id) and ":" not in arxiv_id


def load_targets(db_path, per_layer, builder_tech_cap, seed):
    """Stratified sample of candidate targets: status='done', plain arXiv id,
    stratified by (primary layer, popularity bucket) where popularity is
    in-corpus in-degree (COUNT(citations.dst = id)).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT arxiv_id, title, layers, year, abstract FROM papers WHERE status='done'"
    ).fetchall()
    conn_close = conn
    in_degree = {}
    for dst, n in conn.execute("SELECT dst, COUNT(*) FROM citations GROUP BY dst"):
        in_degree[dst] = n
    conn_close.close()

    candidates = []
    for row in rows:
        aid = row["arxiv_id"]
        if not is_plain_arxiv_id(aid):
            continue
        layers_field = (row["layers"] or "").split(",") if row["layers"] else []
        layers_field = [l for l in layers_field if l in LAYERS]
        if not layers_field:
            continue
        primary_layer = layers_field[0]
        degree = in_degree.get(aid, 0)
        if degree < 1:
            continue  # nothing to build a bench query from
        bucket = bl.popularity_bucket(degree)
        candidates.append({
            "arxiv_id": aid, "title": row["title"], "abstract": row["abstract"], "layer": primary_layer,
            "bucket": bucket, "year": row["year"], "in_degree": degree,
        })

    rng = random.Random(seed)
    by_stratum = {}
    for c in candidates:
        by_stratum.setdefault((c["layer"], c["bucket"]), []).append(c)
    for key in by_stratum:
        by_stratum[key].sort(key=lambda c: c["arxiv_id"])  # determinism before shuffle
        rng.shuffle(by_stratum[key])

    selected = []
    for layer in LAYERS:
        cap = per_layer
        if layer == "builder-tech":
            available = sum(len(v) for k, v in by_stratum.items() if k[0] == layer)
            cap = min(builder_tech_cap, available)
        buckets_for_layer = [k for k in by_stratum if k[0] == layer]
        if not buckets_for_layer:
            continue
        per_bucket = max(1, cap // len(buckets_for_layer))
        taken = 0
        # round-robin so an exhausted bucket doesn't starve the others of budget
        pools = {k: by_stratum[k] for k in buckets_for_layer}
        idx = 0
        while taken < cap and any(pools.values()):
            key = buckets_for_layer[idx % len(buckets_for_layer)]
            idx += 1
            if pools[key]:
                selected.append(pools[key].pop())
                taken += 1
            if idx > 10000:
                break
    return selected


def _cache_path(cache_dir, arxiv_id):
    safe = arxiv_id.replace("/", "_")
    return Path(cache_dir) / f"{safe}.json"


def fetch_citations(arxiv_id, cache_dir, api_key, session, last_call):
    """GET S2 citations for one target, with disk cache + rate limiting +
    429 backoff. `last_call` is a mutable [float] holding the monotonic time
    of the previous live request, updated in place.
    """
    cache_file = _cache_path(cache_dir, arxiv_id)
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    url = S2_BASE.format(id=arxiv_id)
    headers = {"x-api-key": api_key} if api_key else {}
    params = {"fields": S2_FIELDS, "limit": S2_LIMIT}

    backoff = 2.0
    for attempt in range(6):
        elapsed = time.monotonic() - last_call[0]
        if elapsed < MIN_PAUSE_S:
            time.sleep(MIN_PAUSE_S - elapsed)
        last_call[0] = time.monotonic()
        try:
            resp = session.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if resp.status_code == 429:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if resp.status_code == 404:
            data = {"data": []}
            cache_file.write_text(json.dumps(data), encoding="utf-8")
            return data
        resp.raise_for_status()
        data = resp.json()
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data
    return {"data": []}


def select_contexts_for_target(citation_payload, target, max_contexts=MAX_CONTEXTS_PER_TARGET):
    """Pick up to `max_contexts` usable, diverse citation contexts for one
    target from the raw S2 citations response.
    """
    candidates = []
    for edge in citation_payload.get("data", []):
        contexts = edge.get("contexts") or []
        intents = edge.get("intents") or []
        influential = bool(edge.get("isInfluential"))
        external_ids = (edge.get("citingPaper") or {}).get("externalIds") or edge.get("externalIds") or {}
        source_arxiv = None
        if isinstance(external_ids, dict):
            source_arxiv = external_ids.get("ArXiv")
        citing_paper = edge.get("citingPaper") or {}
        ext = citing_paper.get("externalIds") or {}
        if not source_arxiv and isinstance(ext, dict):
            source_arxiv = ext.get("ArXiv")
        for raw_ctx in contexts:
            usable = bl.is_usable_context(raw_ctx)
            if not usable:
                continue
            query, marker_kind = usable
            if not bl.is_grounded(query, target["title"], target.get("abstract")):
                continue
            named = bl.is_named_query(query, target["title"])
            score = (1 if any(i in PREFERRED_INTENTS for i in intents) else 0) + (1 if influential else 0)
            candidates.append({
                "query": query, "marker_kind": marker_kind, "named": named,
                "intents": intents, "isInfluential": influential,
                "source_id": source_arxiv, "priority": score,
            })
    # diverse + prioritized: sort by priority desc, then dedupe near-identical
    # queries by a cheap normalized-prefix key
    seen_prefix = set()
    candidates.sort(key=lambda c: -c["priority"])
    picked = []
    for c in candidates:
        key = c["query"].lower()[:40]
        if key in seen_prefix:
            continue
        seen_prefix.add(key)
        picked.append(c)
        if len(picked) >= max_contexts:
            break
    return picked


def build_bench(db_path, per_layer, builder_tech_cap, seed, cache_dir, limit_targets, api_key):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    targets = load_targets(db_path, per_layer, builder_tech_cap, seed)
    if limit_targets:
        targets = targets[:limit_targets]

    session = requests.Session()
    last_call = [0.0]
    rows = []
    qid_counter = 0
    for target in targets:
        payload = fetch_citations(target["arxiv_id"], cache_dir, api_key, session, last_call)
        contexts = select_contexts_for_target(payload, target)
        split = bl.split_for_target(target["arxiv_id"])
        for ctx in contexts:
            qid_counter += 1
            rows.append({
                "qid": f"q{qid_counter:05d}",
                "query": ctx["query"],
                "target": target["arxiv_id"],
                "layer": target["layer"],
                "bucket": target["bucket"],
                "named": ctx["named"],
                "source_id": ctx["source_id"],
                "split": split,
                "intents": ctx["intents"],
            })
    return rows, targets


def print_summary(rows, targets):
    print(f"targets sampled: {len(targets)}", file=sys.stderr)
    print(f"queries built:   {len(rows)}", file=sys.stderr)
    by_layer = bl.group_by(rows, lambda r: r["layer"])
    for layer in LAYERS:
        n = len(by_layer.get(layer, []))
        print(f"  layer={layer:<14} queries={n}", file=sys.stderr)
    by_bucket = bl.group_by(rows, lambda r: r["bucket"])
    for bucket in ("1-4", "5-49", "50+"):
        n = len(by_bucket.get(bucket, []))
        print(f"  bucket={bucket:<8} queries={n}", file=sys.stderr)
    by_split = bl.group_by(rows, lambda r: r["split"])
    for split in ("dev", "test"):
        n = len(by_split.get(split, []))
        print(f"  split={split:<6} queries={n}", file=sys.stderr)
    named = sum(1 for r in rows if r["named"])
    print(f"  named={named} descriptive={len(rows) - named}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-layer", type=int, default=120)
    ap.add_argument("--builder-tech-cap", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cache-dir", default="eval/.cache_s2")
    ap.add_argument("--out", default="eval/bench/citation_bench.jsonl")
    ap.add_argument("--limit-targets", type=int, default=None,
                     help="for a trial run: only process the first N sampled targets")
    ap.add_argument("--db-path", default=os.path.join(
        os.getenv("DTOX_DATA_DIR", "."), "state.db"))
    args = ap.parse_args()

    api_key = os.getenv("S2_API_KEY", "")  # never printed/logged

    rows, targets = build_bench(
        args.db_path, args.per_layer, args.builder_tech_cap, args.seed,
        args.cache_dir, args.limit_targets, api_key)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print_summary(rows, targets)
    print(f"wrote {len(rows)} queries to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
