#!/usr/bin/env python3
"""Offline check: does the cross-encoder improve the production top 20?

Takes production results for each benchmark query, rescores (query, title +
abstract) with the rerank service and reports hit@k/MRR for several ways of
combining the two orders, before any API code depends on the answer.
"""
import argparse
import json
import sqlite3
import sys
import time

import requests

sys.path.insert(0, "eval")
import bench_lib as bl  # noqa: E402


def fuse(orders, weights, k=60):
    scores = {}
    for order, w in zip(orders, weights):
        for r, pid in enumerate(order):
            scores[pid] = scores.get(pid, 0.0) + w / (k + r + 1)
    return sorted(scores, key=lambda p: -scores[p])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True)
    ap.add_argument("--split", default="dev")
    ap.add_argument("--api", default="http://127.0.0.1:8010")
    ap.add_argument("--rerank", default="http://127.0.0.1:16337/rerank")
    ap.add_argument("--keys", default="/opt/dtox-research-api/keys.json")
    ap.add_argument("--db", default="/opt/dtox-research/state.db")
    ap.add_argument("--depth", type=int, default=20)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    key = next(iter(json.load(open(args.keys))))
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = [json.loads(l) for l in open(args.bench)]
    rows = [r for r in rows if args.split == "all" or r["split"] == args.split]
    variants = {"base": [], "rerank": [], "rrf_w0.5": [], "rrf_w1.0": [], "rrf_w2.0": []}
    rerank_ms = []
    for i, row in enumerate(rows):
        for _ in range(6):
            resp = requests.post(f"{args.api}/v1/search", headers={"X-API-Key": key},
                                 json={"query": row["query"], "limit": args.depth}, timeout=120)
            if resp.status_code != 429:
                break
            time.sleep(10)
        ids = [r["arxiv_id"] for r in resp.json().get("results", []) if r["arxiv_id"] != row.get("source_id")]
        texts = []
        for pid in ids:
            t = db.execute("select title, abstract from papers where arxiv_id=?", (pid,)).fetchone()
            texts.append(f"{(t[0] or '') if t else ''}. {((t[1] or '')[:800]) if t else ''}")
        order_rr = ids
        if ids:
            t0 = time.monotonic()
            scores = requests.post(args.rerank, json={"query": row["query"], "passages": texts},
                                   timeout=60).json()["scores"]
            rerank_ms.append((time.monotonic() - t0) * 1000)
            order_rr = [p for _, p in sorted(zip(scores, ids), key=lambda x: -x[0])]
        orders = {"base": ids, "rerank": order_rr,
                  "rrf_w0.5": fuse([ids, order_rr], [1.0, 0.5]),
                  "rrf_w1.0": fuse([ids, order_rr], [1.0, 1.0]),
                  "rrf_w2.0": fuse([ids, order_rr], [1.0, 2.0])}
        for name, order in orders.items():
            rank = bl.rank_of_target(order[:10], row["target"], source_id=row.get("source_id"))
            variants[name].append({"rank": rank, "error": False, "partial": False, "latency_ms": 0,
                                   "layer": row["layer"], "bucket": row["bucket"], "named": row["named"]})
        if (i + 1) % 25 == 0:
            print(f"{i + 1}/{len(rows)}", flush=True)
    out = {name: bl.aggregate_metrics(v) for name, v in variants.items()}
    rerank_ms.sort()
    out["rerank_ms_p50"] = rerank_ms[len(rerank_ms) // 2] if rerank_ms else None
    json.dump(out, open(args.out, "w"), indent=1)
    for name in variants:
        m = out[name]
        print(f"{name:10} hit@3={m['hit@3']:.3f} hit@10={m['hit@10']:.3f} mrr={m['mrr@10']:.3f}")
    print("rerank p50 ms", out["rerank_ms_p50"])


if __name__ == "__main__":
    main()
