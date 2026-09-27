#!/usr/bin/env python3
"""Re-embed papers_coarse into a second collection with a stronger model.

Runs on the Qdrant host. Point ids and payloads are copied from the live
collection so both stay aligned paper for paper; only the vector changes. The
job is resumable: ids already present in the target are skipped.

A probe on the benchmark (eval/coarse_model_probe.py) put bge-base ahead of
bge-small inside the same candidate pool: hit@3 0.287 -> 0.342 on the dev split.

  coarse_reembed.py --papers /tmp/papers_ta.jsonl --dst papers_coarse_base
"""
import argparse
import json
import sys
import time

import requests

sys.path.insert(0, "/opt/dtox-embed")
from embed_service import encode  # noqa: E402

QDRANT = "http://127.0.0.1:6333"
CHAR_LIMIT = 800


def passage(title, abstract):
    """Title plus the first ~800 characters of the abstract, cut at a sentence."""
    abstract = (abstract or "").strip()
    if len(abstract) > CHAR_LIMIT:
        cut = abstract.rfind(". ", 0, CHAR_LIMIT)
        abstract = abstract[:cut + 1] if cut > CHAR_LIMIT // 3 else abstract[:CHAR_LIMIT]
    return f"{title or ''}. {abstract}".strip()


def ensure(dst, dim):
    if requests.get(f"{QDRANT}/collections/{dst}", timeout=30).status_code == 200:
        return
    src = requests.get(f"{QDRANT}/collections/papers_coarse", timeout=30).json()["result"]
    requests.put(f"{QDRANT}/collections/{dst}", json={
        "vectors": {"size": dim, "distance": "Cosine", "on_disk": False},
        "hnsw_config": {"m": 16, "ef_construct": 128}, "on_disk_payload": False}, timeout=60).raise_for_status()
    for field, schema in (src.get("payload_schema") or {}).items():
        requests.put(f"{QDRANT}/collections/{dst}/index?wait=true",
                     json={"field_name": field, "field_schema": schema["data_type"]}, timeout=600).raise_for_status()


def existing_ids(dst):
    ids, offset = set(), None
    while True:
        body = {"limit": 2000, "with_payload": False, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        page = requests.post(f"{QDRANT}/collections/{dst}/points/scroll", json=body, timeout=120).json()["result"]
        ids.update(p["id"] for p in page["points"])
        offset = page.get("next_page_offset")
        if offset is None:
            return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--papers", required=True)
    ap.add_argument("--dst", default="papers_coarse_base")
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()
    texts = {}
    for line in open(args.papers):
        p = json.loads(line)
        texts[p["arxiv_id"]] = passage(p["title"], p["abstract"])
    dim = len(encode(["probe"])[0])
    ensure(args.dst, dim)
    done = existing_ids(args.dst)
    print(f"target has {len(done)} points; dim {dim}", flush=True)
    offset, written, start = None, 0, time.monotonic()
    while True:
        body = {"limit": args.batch, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        page = requests.post(f"{QDRANT}/collections/papers_coarse/points/scroll", json=body, timeout=120).json()["result"]
        todo = [p for p in page["points"] if p["id"] not in done]
        if todo:
            batch_texts = [texts.get(p["payload"]["arxiv_id"]) or p["payload"].get("title") or "" for p in todo]
            vecs = encode(batch_texts)
            points = [{"id": p["id"], "vector": v, "payload": p["payload"]} for p, v in zip(todo, vecs)]
            requests.put(f"{QDRANT}/collections/{args.dst}/points?wait=true", json={"points": points},
                         timeout=300).raise_for_status()
            written += len(points)
            if written % 1024 < args.batch:
                rate = written / (time.monotonic() - start)
                print(f"{written} written ({rate:.1f}/s)", flush=True)
        offset = page.get("next_page_offset")
        if offset is None:
            break
    print(f"done: {written} written", flush=True)


if __name__ == "__main__":
    main()
