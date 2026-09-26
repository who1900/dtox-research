"""Rank harness: does the paper that introduced a concept come first?

harness.py checks that the right paper is somewhere in the top ten. That
passed while "GRPO" returned papers that merely use GRPO above the one that
defined it, so this one scores the position. Each expected id is the work that
introduced the idea, checked by hand against the paper itself; the query is
phrased the way a builder would ask, never as the paper's title.

  python3 api/tests/rank_harness.py                 # live API on :8010
  python3 api/tests/rank_harness.py --top 3 --json  # machine-readable
"""
import argparse
import json
import sys
import time

import requests

API = "http://127.0.0.1:8010"
KEYS = "/opt/dtox-research-api/keys.json"

CASES = [
    ("critic-free RL that uses group relative advantages for math reasoning", "2402.03300"),
    ("reasoning model trained with reinforcement learning without supervised fine-tuning first", "2501.12948"),
    ("draft model proposes tokens and the large model verifies them in parallel", "2211.17192"),
    ("speculative sampling to accelerate large language model decoding", "2302.01318"),
    ("low-rank adapters for parameter-efficient fine-tuning", "2106.09685"),
    ("fine-tuning a 4-bit quantized model with low-rank adapters", "2305.14314"),
    ("IO-aware exact attention kernel with tiling", "2205.14135"),
    ("prompting with intermediate reasoning steps improves reasoning", "2201.11903"),
    ("sample many reasoning paths and take the majority answer", "2203.11171"),
    ("interleaving reasoning traces and actions in language agents", "2210.03629"),
    ("language model teaches itself when to call external APIs", "2302.04761"),
    ("agent learns from verbal self-reflection on failures", "2303.11366"),
    ("preference optimization without a separate reward model", "2305.18290"),
    ("paged KV cache memory management for LLM serving", "2309.06180"),
    ("selective state space model as an alternative to transformers", "2312.00752"),
    ("frontrunning bots and priority gas auctions on decentralized exchanges", "1904.05234"),
    ("function that takes sequential time to evaluate but is fast to verify", "iacr:2018/601"),
    ("account abstraction with a separate user operation mempool and bundlers", "eip:4337"),
    ("blob-carrying transactions for rollup data availability", "eip:4844"),
]


def run(top):
    key = next(iter(json.load(open(KEYS))))
    rows = []
    for query, expected in CASES:
        t = time.time()
        resp = requests.post(f"{API}/v1/search", headers={"X-API-Key": key},
                             json={"query": query, "limit": 10}, timeout=120)
        took = time.time() - t
        ids = [r["arxiv_id"] for r in resp.json().get("results", [])] if resp.ok else []
        rank = ids.index(expected) + 1 if expected in ids else None
        rows.append({"query": query, "expected": expected, "rank": rank,
                     "status": resp.status_code, "seconds": round(took, 2),
                     "partial": bool(resp.ok and resp.json().get("partial"))})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = run(args.top)
    hits = sum(1 for r in rows if r["rank"] and r["rank"] <= args.top)
    mrr = sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows)
    secs = sorted(r["seconds"] for r in rows)
    if args.json:
        print(json.dumps({"rows": rows, f"hit@{args.top}": hits, "mrr@10": mrr}, indent=1))
    else:
        for r in rows:
            mark = "ok  " if r["rank"] and r["rank"] <= args.top else "MISS"
            print(f"{mark} rank={str(r['rank']):4} {r['seconds']:5.1f}s "
                  f"{'partial ' if r['partial'] else ''}{r['expected']:14} {r['query'][:60]}")
        print(f"\nhit@{args.top} {hits}/{len(rows)}  MRR@10 {mrr:.3f}  "
              f"p50 {secs[len(secs) // 2]:.1f}s  max {secs[-1]:.1f}s")
    sys.exit(0 if hits == len(rows) else 1)


if __name__ == "__main__":
    main()
