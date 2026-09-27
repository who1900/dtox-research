#!/usr/bin/env python3
"""Probe: would a stronger embedding model rank the right paper higher?

For each benchmark query, the current paper index (bge-small, papers_coarse)
proposes a pool of candidates; the probe re-orders the same pool with another
model and compares where the target lands. It measures ordering power inside
the pool only, which is the cheap question to ask before re-embedding 164k
abstracts for hours.
"""
import argparse
import json
import sqlite3
import time

import numpy as np
import onnxruntime as ort
import requests
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

INSTR = "Represent this sentence for searching relevant passages: "


class Encoder:
    def __init__(self, repo, threads=2, max_len=256):
        self.tok = Tokenizer.from_file(hf_hub_download(repo, "tokenizer.json"))
        self.tok.enable_truncation(max_len)
        self.tok.enable_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(hf_hub_download(repo, "onnx/model_quantized.onnx"), opts,
                                         providers=["CPUExecutionProvider"])
        self.names = {i.name for i in self.sess.get_inputs()}

    def __call__(self, texts):
        out = []
        for i in range(0, len(texts), 32):
            enc = self.tok.encode_batch(texts[i:i + 32])
            feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
            if "token_type_ids" in self.names:
                feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
            cls = self.sess.run(None, feed)[0][:, 0]
            out.append(cls / np.linalg.norm(cls, axis=1, keepdims=True))
        return np.vstack(out)


def rank_in(order, target):
    return order.index(target) + 1 if target in order else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True)
    ap.add_argument("--papers", required=True, help="jsonl: arxiv_id, title, abstract")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--pool", type=int, default=50)
    ap.add_argument("--model", default="Xenova/bge-base-en-v1.5")
    ap.add_argument("--qdrant", default="http://127.0.0.1:6333")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    papers = {}
    for line in open(args.papers):
        p = json.loads(line)
        papers[p["arxiv_id"]] = f"{p['title'] or ''}. {(p['abstract'] or '')[:800]}"
    small, big = Encoder("Xenova/bge-small-en-v1.5"), Encoder(args.model)
    rows = [json.loads(l) for l in open(args.bench)]
    rows = [r for r in rows if args.split == "all" or r["split"] == args.split]
    cache, results, start = {}, [], time.monotonic()
    for i, row in enumerate(rows):
        qv = small([INSTR + row["query"]])[0].tolist()
        hits = requests.post(f"{args.qdrant}/collections/papers_coarse/points/search",
                             json={"vector": qv, "limit": args.pool, "with_payload": ["arxiv_id"]},
                             timeout=60).json()["result"]
        pool = [h["payload"]["arxiv_id"] for h in hits if h["payload"]["arxiv_id"] != row.get("source_id")]
        missing = [p for p in pool if p not in cache and p in papers]
        if missing:
            for pid, vec in zip(missing, big([papers[p] for p in missing])):
                cache[pid] = vec
        bq = big([INSTR + row["query"]])[0]
        scored = sorted((p for p in pool if p in cache), key=lambda p: -float(cache[p] @ bq))
        results.append({"qid": row["qid"], "layer": row["layer"], "bucket": row["bucket"],
                        "in_pool": row["target"] in pool,
                        "small": rank_in(pool, row["target"]), "big": rank_in(scored, row["target"])})
        if (i + 1) % 25 == 0:
            print(f"{i + 1}/{len(rows)} {time.monotonic() - start:.0f}s", flush=True)
    def summary(key):
        n = len(results)
        return {f"hit@{k}": sum(1 for r in results if r[key] and r[key] <= k) / n for k in (1, 3, 10)} | {
            "mrr": sum(1 / r[key] for r in results if r[key] and r[key] <= 10) / n}
    report = {"n": len(results), "target_in_pool": sum(r["in_pool"] for r in results) / len(results),
              "small": summary("small"), "big": summary("big"), "model": args.model, "rows": results}
    json.dump(report, open(args.out, "w"), indent=1)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=1))


if __name__ == "__main__":
    main()
